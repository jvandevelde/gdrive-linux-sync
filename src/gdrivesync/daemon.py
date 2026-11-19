"""Background sync service: file watcher + Drive change polling around one
SyncEngine per folder pair.

All sync work happens on one thread (the loop in ``run``), so engines and their
SQLite DBs never see concurrent access. The watchdog thread only enqueues.

Drive's change feed is account-wide, so pairs at the same feed position share a
single ``changes.list`` call; each engine ignores changes outside its folder.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from gdrivesync.config import TMP_DIRNAME
from gdrivesync.drive import InvalidPageToken
from gdrivesync.engine import FULL_CHECK_KEY, SyncEngine
from gdrivesync.state import is_corruption

log = logging.getLogger("gdrivesync.daemon")

DEBOUNCE_S = 2.0  # quiet time after the last local event before scanning
MAX_DELAY_S = 15.0  # ...but never wait longer than this while events keep coming
FULL_SCAN_S = 600.0  # periodic safety-net scan (inotify can drop events)
FULL_SCAN_NO_WATCH_S = 60.0  # when inotify isn't available
MAX_BACKOFF_S = 300.0


class _Handler(FileSystemEventHandler):
    def __init__(self, index: int, root: Path, q: queue.Queue):
        self.index = index
        self.root = str(root)
        self.q = q

    def _rel(self, path: str | bytes) -> str | None:
        path = os.fsdecode(path)
        if path == self.root or not path.startswith(self.root + os.sep):
            return None
        rel = path[len(self.root) + 1 :]
        return None if rel.split("/", 1)[0] == TMP_DIRNAME else rel

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return
        src = self._rel(event.src_path)
        dest = self._rel(getattr(event, "dest_path", "") or "")
        if src is None and dest is None:
            return
        if event.event_type == "moved" and src and dest:
            self.q.put(("move", self.index, (src, dest)))
        self.q.put(("dirty", self.index, None))


@dataclass
class _Slot:
    engine: SyncEngine
    first_dirty: float | None = None
    last_event: float | None = None
    moves: list[tuple[str, str]] = field(default_factory=list)
    disabled: bool = False  # state DB unusable and couldn't be rebuilt

    def mark(self, now: float) -> None:
        self.last_event = now
        self.first_dirty = self.first_dirty or now

    def due(self, now: float) -> bool:
        return self.first_dirty is not None and (
            now - self.last_event >= DEBOUNCE_S or now - self.first_dirty >= MAX_DELAY_S
        )

    def deadline(self) -> float | None:
        if self.first_dirty is None:
            return None
        return min(self.last_event + DEBOUNCE_S, self.first_dirty + MAX_DELAY_S)


class Daemon:
    def __init__(
        self,
        engines: list[SyncEngine] | SyncEngine,
        *,
        poll_interval_s: float = 45.0,
        full_check_s: float = 24 * 3600,
        watch: bool = True,
        rebuild=None,
    ):
        """``rebuild(engine) -> engine`` is called when a pair's state DB turns
        out to be corrupt while running; it should quarantine the DB and return
        a freshly rebuilt engine for the same pair."""
        self.rebuild = rebuild
        if isinstance(engines, SyncEngine):
            engines = [engines]
        if not engines:
            raise ValueError("no folder pairs to sync")
        self.slots = [_Slot(e) for e in engines]
        self.client = engines[0].client
        self.poll_interval_s = poll_interval_s
        self.full_check_s = full_check_s
        self.watch = watch
        self.q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._observer: Observer | None = None
        self.paused = False

    # ---------------------------------------------------------------- control
    def stop(self) -> None:
        self._stop.set()
        self.q.put(("stop", None, None))

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop())

    # ---------------------------------------------------------------- pieces
    def _start_watcher(self) -> bool:
        if not self.watch:
            return False
        try:
            self._observer = Observer()
            for i, slot in enumerate(self.slots):
                root = slot.engine.root
                self._observer.schedule(_Handler(i, root, self.q), str(root), recursive=True)
            self._observer.start()
            return True
        except OSError as e:
            log.warning(
                "File watching unavailable (%s); falling back to scanning every %ds. "
                "Raise fs.inotify.max_user_watches if this is a large tree.",
                e, FULL_SCAN_NO_WATCH_S,
            )
            self._observer = None
            return False

    def _safe(self, what: str, fn, *args, slot: "_Slot | None" = None):
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 — keep the service alive
            if slot is not None and is_corruption(e):
                self._handle_corruption(slot, e)
            else:
                log.exception("%s failed", what)
            return None

    def _handle_corruption(self, slot: "_Slot", e: Exception) -> None:
        eng = slot.engine
        log.error("[%s] sync state is corrupted (%s); rebuilding it", self._label(eng), e)
        if self.rebuild is None:
            slot.disabled = True
            log.error("[%s] paused: restart the service to rebuild", self._label(eng))
            return
        try:
            slot.engine = self.rebuild(eng)
        except Exception:  # noqa: BLE001
            slot.disabled = True
            log.exception("[%s] rebuild failed; this folder is paused until restart",
                          self._label(eng))
            return
        log.warning("[%s] rebuilt sync state; continuing", self._label(slot.engine))
        slot.mark(time.monotonic())

    def _active(self) -> list["_Slot"]:
        return [s for s in self.slots if not s.disabled]

    def _label(self, eng: SyncEngine) -> str:
        return eng.name or str(eng.root)

    def _local(self, slot: _Slot, now: float) -> None:
        eng = slot.engine
        slot.first_dirty = slot.last_event = None
        for src, dest in slot.moves:
            self._safe(f"[{self._label(eng)}] move", eng.local_move_event, src, dest, slot=slot)
        slot.moves.clear()
        stats = self._safe(f"[{self._label(eng)}] local sync", eng.local_pass, slot=slot)
        if stats and stats.changed:
            log.info("[%s] local: %s", self._label(eng), _summary(stats))
        if (stats and stats.deferred) or eng.dirty:
            eng.dirty = False
            slot.first_dirty = slot.last_event = now + eng.settle_s - DEBOUNCE_S

    def poll_remote(self) -> bool:
        """One Drive change-feed poll for every pair. Returns False on any failure."""
        groups: dict[str | None, list[_Slot]] = defaultdict(list)
        for slot in self._active():
            groups[self._safe("read state", lambda e=slot.engine: e.db.page_token, slot=slot)
                   ].append(slot)
        ok = True
        for token, slots in groups.items():
            if token is None:
                continue
            try:
                changes, new_token = self.client.changes_since(token)
            except InvalidPageToken as e:
                # Detected a rejected feed position: recover each affected pair.
                for slot in slots:
                    eng = slot.engine
                    stats = self._safe(f"[{self._label(eng)}] recovery", eng.recover_feed, e,
                                       slot=slot)
                    if stats is None:
                        ok = False
                    else:
                        log.warning("[%s] recovered by full comparison with Drive: %s",
                                    self._label(eng), _summary(stats))
                    if eng.dirty:
                        eng.dirty = False
                        slot.mark(time.monotonic())
                continue
            except Exception:  # noqa: BLE001
                log.exception("polling Drive failed")
                ok = False
                continue
            for slot in slots:
                eng = slot.engine
                stats = self._safe(f"[{self._label(eng)}] remote sync", eng.apply_changes,
                                   changes, new_token, slot=slot)
                if stats is None:
                    ok = False
                elif stats.changed:
                    log.info("[%s] Drive: %s", self._label(eng), _summary(stats))
                if eng.dirty:
                    eng.dirty = False
                    slot.mark(time.monotonic())
        return ok

    def full_checks_due(self) -> None:
        """Periodic safety net on the Drive side (like the local full scan):
        compare each pair with Drive and report anything the change feed missed."""
        if self.full_check_s <= 0:
            return
        now = time.time()
        for slot in self._active():
            eng = slot.engine
            raw = self._safe("read state", eng.db.get, FULL_CHECK_KEY, slot=slot)
            if slot.engine is not eng or slot.disabled:
                continue  # was just rebuilt / paused
            last = float(raw or 0)
            if now - last < self.full_check_s:
                continue
            if last == 0:
                # Never checked (e.g. fresh pull): start the clock instead of
                # re-listing Drive right after a full download.
                eng.db.set(FULL_CHECK_KEY, str(now))
                continue
            stats = self._safe(f"[{self._label(eng)}] full check", eng.full_check, slot=slot)
            if stats is None:
                continue
            if stats.changed or stats.errors:
                log.warning("[%s] full check found differences the change feed missed: %s",
                            self._label(eng), _summary(stats))
            else:
                log.info("[%s] full check: everything matches Drive", self._label(eng))
            if eng.dirty:
                eng.dirty = False
                slot.mark(time.monotonic())

    # ---------------------------------------------------------------- loop
    def run(self) -> None:
        watching = self._start_watcher()
        full_scan_every = FULL_SCAN_S if watching else FULL_SCAN_NO_WATCH_S
        for s in self.slots:
            log.info("Syncing %s ⇄ Drive %s", s.engine.root, self._label(s.engine))
        log.info("watching=%s, polling Drive every %ss", watching, self.poll_interval_s)

        now = time.monotonic()
        next_remote = now
        next_full = now + full_scan_every
        for s in self.slots:  # scan once at startup to catch offline changes
            s.first_dirty, s.last_event = now, now - DEBOUNCE_S
        remote_failures = 0

        try:
            while not self._stop.is_set():
                now = time.monotonic()
                deadlines = [next_remote, next_full] + [
                    d for d in (s.deadline() for s in self.slots) if d is not None
                ]
                timeout = max(0.0, min(min(deadlines) - now, 1.0))
                try:
                    item = self.q.get(timeout=timeout)
                    while True:
                        kind, idx, payload = item
                        if kind == "stop":
                            return
                        slot = self.slots[idx]
                        if kind == "move":
                            slot.moves.append(payload)
                        slot.mark(time.monotonic())
                        item = self.q.get_nowait()
                except queue.Empty:
                    pass

                if self.paused:
                    continue
                now = time.monotonic()

                for slot in self._active():
                    if slot.due(now):
                        self._local(slot, now)

                if now >= next_remote:
                    if self.poll_remote():
                        remote_failures = 0
                        delay = self.poll_interval_s
                    else:
                        remote_failures += 1
                        delay = min(MAX_BACKOFF_S, 15.0 * 2 ** (remote_failures - 1))
                    next_remote = time.monotonic() + delay
                    if remote_failures == 0:
                        self.full_checks_due()

                if now >= next_full:
                    next_full = now + full_scan_every
                    for slot in self._active():
                        if slot.first_dirty is None:
                            slot.first_dirty, slot.last_event = now, now - DEBOUNCE_S
        finally:
            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=5)
            log.info("Stopped.")


def _summary(s) -> str:
    parts = [
        f"{n} {label}"
        for n, label in (
            (s.downloaded, "downloaded"),
            (s.uploaded, "uploaded"),
            (s.folders_created, "folders created"),
            (s.moved, "moved"),
            (s.trashed_local, "removed locally"),
            (s.trashed_remote, "trashed on Drive"),
            (len(s.conflicts), "conflicts"),
            (len(s.errors), "errors"),
        )
        if n
    ]
    return ", ".join(parts) or "no changes"
