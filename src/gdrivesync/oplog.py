"""Operation log: one JSON line per change made to the local disk or to Drive.

Default file: ``~/.local/state/gdrivesync/operations.log`` (rotated at 10 MB,
5 old files kept). Every record is also emitted to the ``gdrivesync.ops``
logger, so it shows up in the service's journal. In dry-run mode nothing is
written; records are printed as "would …" instead.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
from collections import deque
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("gdrivesync.ops")

MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5

_file_loggers: dict[Path, logging.Logger] = {}
_lock = threading.Lock()


def _file_logger(path: Path) -> logging.Logger:
    with _lock:
        lg = _file_loggers.get(path)
        if lg is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            lg = logging.getLogger(f"gdrivesync.oplog.file.{len(_file_loggers)}")
            lg.propagate = False
            lg.setLevel(logging.INFO)
            h = logging.handlers.RotatingFileHandler(
                path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
            )
            h.setFormatter(logging.Formatter("%(message)s"))
            lg.addHandler(h)
            _file_loggers[path] = lg
        return lg


def _size(n) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


_TEMPLATES = {
    "download": "download {path} ({size})",
    "write_link": "write Google Docs link {path}",
    "mkdir": "create local folder {path}",
    "move_local": "move local {src} -> {path}",
    "conflict": "keep local {src} as {path} (conflicted copy)",
    "trash_local": "move local {path} to trash",
    "delete_local": "remove local copy of {path}",
    "create_folder": "create Drive folder {path}",
    "upload": "upload new file {path} ({size})",
    "update": "upload new version of {path} ({size})",
    "move_remote": "move on Drive {src} -> {path}",
    "trash_remote": "trash on Drive {path}",
}


def describe(entry: dict) -> str:
    tmpl = _TEMPLATES.get(entry.get("op", ""), "{op} {path}")
    text = tmpl.format(
        op=entry.get("op"),
        path=entry.get("path"),
        src=entry.get("src"),
        size=_size(entry.get("size")),
    )
    if entry.get("reason"):
        text += f" — {entry['reason']}"
    if entry.get("pair"):
        text = f"[{entry['pair']}] {text}"
    return text


class OpLog:
    def __init__(
        self,
        path: Path | None = None,
        *,
        dry_run: bool = False,
        echo: Callable[[str], None] | None = None,
    ):
        self.path = path
        self.dry_run = dry_run
        self.echo = echo or print
        self.recent: deque[dict] = deque(maxlen=200)  # for status UIs later
        self.count = 0

    def record(self, op: str, path: str, *, pair: str = "", **detail) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pair": pair,
            "op": op,
            "path": path,
            **{k: v for k, v in detail.items() if v is not None},
        }
        self.count += 1
        if self.dry_run:
            entry["dry_run"] = True
            self.recent.append(entry)
            self.echo("would " + describe(entry))
            return
        self.recent.append(entry)
        log.info(describe(entry))
        if self.path is not None:
            _file_logger(self.path).info(json.dumps(entry, ensure_ascii=False))


def read_entries(path: Path) -> Iterator[dict]:
    """All entries, oldest first, across rotated files."""
    files = [Path(f"{path}.{i}") for i in range(BACKUPS, 0, -1)] + [path]
    for f in files:
        if not f.exists():
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
