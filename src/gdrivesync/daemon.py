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
from gdrivesync.engine import SyncEngine

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
        self, engines: list[SyncEngine] | SyncEngine, *, poll_interval_s: float = 45.0, watch: bool = True
    ):
        if isinstance(engines, SyncEngine):
            engines = [engines]
        if not engines:
            raise ValueError("no folder pairs to sync")
        self.slots = [_Slot(e) for e in engines]
        self.client = engines[0].client
        self.poll_interval_s = poll_interval_s
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

    def _safe(self, what: str, fn, *args):
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001 — keep the service alive
            log.exception("%s failed", what)
            return None

    def _label(self, eng: SyncEngine) -> str:
        return eng.name or str(eng.root)

    def _local(self, slot: _Slot, now: float) -> None:
        eng = slot.engine
        slot.first_dirty = slot.last_event = None
        for src, dest in slot.moves:
            self._safe(f"[{self._label(eng)}] move", eng.local_move_event, src, dest)
        slot.moves.clear()
        stats = self._safe(f"[{self._label(eng)}] local sync", eng.local_pass)
        if stats and stats.changed:
            log.info("[%s] local: %s", self._label(eng), _summary(stats))
        if (stats and stats.deferred) or eng.dirty:
            eng.dirty = False
            slot.first_dirty = slot.last_event = now + eng.settle_s - DEBOUNCE_S

    def poll_remote(self) -> bool:
        """One Drive change-feed poll for every pair. Returns False on any failure."""
        groups: dict[str | None, list[_Slot]] = defaultdict(list)
        for slot in self.slots:
            groups[slot.engine.db.page_token].append(slot)
        ok = True
        for token, slots in groups.items():
            if token is None:
                continue
            try:
                changes, new_token = self.client.changes_since(token)
            except Exception:  # noqa: BLE001
                log.exception("polling Drive failed")
                ok = False
                continue
            for slot in slots:
                eng = slot.engine
                stats = self._safe(f"[{self._label(eng)}] remote sync", eng.apply_changes, changes, new_token)
                if stats is None:
                    ok = False
                elif stats.changed:
                    log.info("[%s] Drive: %s", self._label(eng), _summary(stats))
                if eng.dirty:
                    eng.dirty = False
                    slot.mark(time.monotonic())
        return ok

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

                for slot in self.slots:
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

                if now >= next_full:
                    next_full = now + full_scan_every
                    for slot in self.slots:
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
