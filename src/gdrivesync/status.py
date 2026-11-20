"""Per-path sync status, safe to query from a thread other than the sync thread.

Status is derived, not stored: the item's last-synced state (read through a
separate read-only DB connection), the file on disk, and the engine's live
`errors` / `active` sets.
"""

from __future__ import annotations

import os
import posixpath
import re
import stat
from pathlib import Path

from gdrivesync.drive import FOLDER_MIME, is_google_doc
from gdrivesync.pull import read_stub_id, unchanged_since
from gdrivesync.state import StateDB

SYNCED = "synced"
SYNCING = "syncing"
ERROR = "error"
CONFLICT = "conflict"
IGNORED = "ignored"
NONE = ""  # not inside a synced folder / doesn't exist

STATUSES = (SYNCED, SYNCING, ERROR, CONFLICT, IGNORED)
CONFLICT_RE = re.compile(r" \(conflicted copy \d{4}-\d\d-\d\d[ \d]*\)")


def _keys(collection) -> list[str]:
    """Copy the keys of a dict/set another thread may be mutating."""
    for _ in range(5):
        try:
            return list(collection)
        except RuntimeError:  # changed size during iteration
            continue
    return []


def drive_link(item) -> str:
    if item.is_folder or item.mime_type == FOLDER_MIME:
        return f"https://drive.google.com/drive/folders/{item.file_id}"
    if is_google_doc(item.mime_type):
        return f"https://drive.google.com/open?id={item.file_id}"
    return f"https://drive.google.com/file/d/{item.file_id}/view"


class PairStatus:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.root
        path = getattr(engine.db, "path", ":memory:")
        # Own read-only connection for file DBs; tests with in-memory DBs share.
        self.db = engine.db if path == ":memory:" else StateDB(path, readonly=True)

    def close(self) -> None:
        if self.db is not self.engine.db:
            self.db.close()

    def rel(self, path: str) -> str | None:
        """Path relative to this pair's root ("" for the root), or None if outside."""
        p = os.path.normpath(path)
        root = str(self.root)
        if p == root:
            return ""
        if p.startswith(root + os.sep):
            return p[len(root) + 1 :]
        return None

    def status(self, rel: str) -> str:
        eng = self.engine
        if rel and eng.rules.ignored(rel):
            return IGNORED
        errors = _keys(eng.errors)
        active = _keys(eng.active)
        if rel in errors:
            return ERROR
        if any(a == rel or a.startswith(rel + "/") or not rel for a in active):
            return SYNCING
        try:
            st = os.lstat(self.root / rel if rel else self.root)
        except FileNotFoundError:
            return NONE
        if stat.S_ISLNK(st.st_mode):
            return IGNORED
        if CONFLICT_RE.search(posixpath.basename(rel)):
            return CONFLICT
        if stat.S_ISDIR(st.st_mode):
            if rel and self.db.by_path(rel) is None:
                return SYNCING  # new folder, not on Drive yet
            prefix = rel + "/" if rel else ""
            if any(e.startswith(prefix) for e in errors):
                return ERROR
            return SYNCED
        item = self.db.by_path(rel)
        if item is None:
            # A copied Google-Docs link file is never uploaded.
            return IGNORED if read_stub_id(self.root / rel) else SYNCING
        if is_google_doc(item.mime_type):
            return SYNCED
        return SYNCED if unchanged_since(item, st) else SYNCING

    def link(self, rel: str) -> str:
        item = self.db.by_path(rel) if rel else None
        if rel == "":
            rid = self.engine.root_id
            return f"https://drive.google.com/drive/folders/{rid}"
        return drive_link(item) if item else ""


class StatusIndex:
    """Status lookups across all pairs of a running daemon."""

    def __init__(self, daemon):
        self.daemon = daemon
        self._cache: dict[int, PairStatus] = {}

    def _pairs(self) -> list[PairStatus]:
        out = []
        live = set()
        for slot in self.daemon.slots:
            eng = slot.engine  # may be swapped by a rebuild
            ps = self._cache.get(id(eng))
            if ps is None or ps.engine is not eng:
                ps = self._cache[id(eng)] = PairStatus(eng)
            out.append(ps)
            live.add(id(eng))
        for key in [k for k in self._cache if k not in live]:
            self._cache.pop(key).close()
        return out

    def roots(self) -> list[str]:
        return [str(ps.root) for ps in self._pairs()]

    def locate(self, path: str) -> tuple[PairStatus, str] | tuple[None, None]:
        for ps in self._pairs():
            rel = ps.rel(path)
            if rel is not None:
                return ps, rel
        return None, None

    def status(self, path: str) -> str:
        ps, rel = self.locate(path)
        return ps.status(rel) if ps else NONE

    def link(self, path: str) -> str:
        ps, rel = self.locate(path)
        return ps.link(rel) if ps else ""

    def close(self) -> None:
        for ps in self._cache.values():
            ps.close()
        self._cache.clear()
