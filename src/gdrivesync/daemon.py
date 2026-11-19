"""Background sync service: file watcher + Drive change polling around SyncEngine.

All sync work happens on one thread (the loop in ``run``), so the engine and
the SQLite DB never see concurrent access. The watchdog thread only enqueues.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import threading
import time
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
    def __init__(self, root: Path, q: queue.Queue):
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
            self.q.put(("move", src, dest))
        self.q.put(("dirty", None, None))


class Daemon:
    def __init__(self, engine: SyncEngine, *, poll_interval_s: float = 45.0, watch: bool = True):
        self.engine = engine
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

    # ---------------------------------------------------------------- loop
    def _start_watcher(self) -> bool:
        if not self.watch:
            return False
        try:
            self._observer = Observer()
            self._observer.schedule(_Handler(self.engine.root, self.q), str(self.engine.root), recursive=True)
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

    def run(self) -> None:
        eng = self.engine
        watching = self._start_watcher()
        full_scan_every = FULL_SCAN_S if watching else FULL_SCAN_NO_WATCH_S
        log.info("Syncing %s (watching=%s, poll every %ss)", eng.root, watching, self.poll_interval_s)

        now = time.monotonic()
        next_remote = now  # poll Drive right away
        next_full = now + full_scan_every
        first_dirty: float | None = now  # scan once at startup to catch offline changes
        last_event: float | None = now - DEBOUNCE_S
        moves: list[tuple[str, str]] = []
        remote_failures = 0

        try:
            while not self._stop.is_set():
                now = time.monotonic()
                deadlines = [next_remote, next_full]
                if first_dirty is not None:
                    deadlines.append(min(last_event + DEBOUNCE_S, first_dirty + MAX_DELAY_S))
                timeout = max(0.0, min(min(deadlines) - now, 1.0))
                try:
                    item = self.q.get(timeout=timeout)
                    while True:
                        kind, a, b = item
                        if kind == "stop":
                            return
                        if kind == "move":
                            moves.append((a, b))
                        now = time.monotonic()
                        last_event = now
                        first_dirty = first_dirty or now
                        item = self.q.get_nowait()
                except queue.Empty:
                    pass

                if self.paused:
                    continue
                now = time.monotonic()

                if first_dirty is not None and (
                    now - last_event >= DEBOUNCE_S or now - first_dirty >= MAX_DELAY_S
                ):
                    first_dirty = last_event = None
                    for src, dest in moves:
                        self._safe("move", eng.local_move_event, src, dest)
                    moves.clear()
                    stats = self._safe("local sync", eng.local_pass)
                    if stats and stats.changed:
                        log.info("local pass: %s", _summary(stats))
                    if (stats and stats.deferred) or eng.dirty:
                        eng.dirty = False
                        first_dirty = last_event = now + eng.settle_s - DEBOUNCE_S

                if now >= next_remote:
                    stats = self._safe("remote sync", eng.remote_pass)
                    if stats is None:
                        remote_failures += 1
                        delay = min(MAX_BACKOFF_S, 15.0 * 2 ** (remote_failures - 1))
                    else:
                        remote_failures = 0
                        delay = self.poll_interval_s
                        if stats.changed:
                            log.info("remote pass: %s", _summary(stats))
                    next_remote = time.monotonic() + delay
                    if eng.dirty:
                        eng.dirty = False
                        first_dirty = last_event = time.monotonic()

                if now >= next_full:
                    next_full = now + full_scan_every
                    first_dirty = first_dirty or now
                    last_event = last_event or now - DEBOUNCE_S
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
