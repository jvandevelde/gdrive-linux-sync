"""Two-way sync engine.

State-based rather than event-based: every decision compares three things —
the local file, the remote file, and the *base* (last agreed state) in the DB —
so applying the same pass twice is harmless, and our own writes echoing back
(as inotify events or Drive changes) are no-ops.

* ``remote_pass()`` pulls the Drive change feed and applies it locally.
* ``local_pass()`` scans the sync folder and pushes differences to Drive.
* ``local_move_event()`` applies an inotify rename/move directly, so renaming a
  big folder is one API call instead of trash + re-upload.

Policy: edits beat deletes, and when both sides changed the local version is
kept as a "conflicted copy" next to the Drive version.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import posixpath
import shutil
import stat as statmod
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from gdrivesync.config import BUILTIN_IGNORES, TMP_DIRNAME
from gdrivesync.drive import FOLDER_MIME, SHORTCUT_MIME, InvalidPageToken, is_google_doc
from gdrivesync.localfs import IgnoreRules, is_dir_stat, scan, trash_local
from gdrivesync.oplog import OpLog
from gdrivesync.names import DEFAULT_STUB_EXT, DOC_STUB_EXT, dedupe, stub_name, to_local, to_remote
from gdrivesync.pull import (
    PullError,
    conflict_name,
    download_file,
    list_subtree,
    make_item,
    md5_file,
    read_stub_id,
    rfc3339_to_ts,
    unchanged_since,
    write_stub,
)
from gdrivesync.state import Item, StateDB

log = logging.getLogger("gdrivesync.engine")

STUB_EXTS = tuple(set(DOC_STUB_EXT.values()) | {DEFAULT_STUB_EXT})
RETRY_KEY = "remote_retry_ids"
EXCLUDED_KEY = "excluded_paths"  # {folder_id: local rel path it would occupy}
FULL_CHECK_KEY = "last_full_check"  # unix time of the last full comparison with Drive
FEED_RESET_KEY = "last_feed_reset"  # unix time we last recovered from a rejected position
FEED_RESET_COOLDOWN_S = 600


class SyncError(RuntimeError):
    pass


@dataclass
class FakeStat:
    """Stand-in for os.stat_result in dry runs (only the fields we read)."""

    st_size: int = 0
    st_mtime_ns: int = 0
    st_mode: int = statmod.S_IFREG | 0o644

    @property
    def st_mtime(self) -> float:
        return self.st_mtime_ns / 1e9


@dataclass
class PassStats:
    downloaded: int = 0
    uploaded: int = 0
    folders_created: int = 0
    moved: int = 0
    trashed_local: int = 0
    trashed_remote: int = 0
    deferred: int = 0  # files still being written; retry shortly
    conflicts: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(
            (self.downloaded, self.uploaded, self.folders_created, self.moved,
             self.trashed_local, self.trashed_remote, self.conflicts)
        )


class SyncEngine:
    def __init__(
        self,
        client,
        db: StateDB,
        root: Path,
        *,
        remote_root_id: str | None = None,
        excluded: Iterable[str] = (),
        rules: IgnoreRules | None = None,
        settle_s: float = 3.0,
        mass_delete_limit: int = 100,
        name: str = "",
        oplog: OpLog | None = None,
        dry_run: bool = False,
    ):
        """With dry_run=True, pass a StateDB.snapshot(): Drive is only read, the
        local disk isn't touched, and every change is reported via ``oplog``."""
        self.name = name
        self.dry_run = dry_run
        self.oplog = oplog or OpLog(dry_run=dry_run)
        # Dry-run overlay of simulated local changes, so later decisions in the
        # same run see them: (new_rel, old_rel) moves, removed and created paths.
        self._vmoves: list[tuple[str, str]] = []
        self._vgone: set[str] = set()
        self._vcreated: set[str] = set()
        self._dry_ids = 0
        # For status reporting (read from the D-Bus thread):
        self.active: set[str] = set()  # rel paths being transferred right now
        self.listeners: list[Callable[[list[str]], None]] = []  # called with changed rels
        self.client = client
        self.db = db
        self.root = Path(root).expanduser().resolve()
        self.tmp = self.root / TMP_DIRNAME
        self.excluded = set(excluded)
        self.rules = rules or IgnoreRules(BUILTIN_IGNORES)
        self.settle_s = settle_s
        self.mass_delete_limit = mass_delete_limit
        self.allow_mass_delete = False
        recorded = db.get("sync_root")
        if recorded and recorded != str(self.root):
            raise SyncError(f"State DB belongs to {recorded!r}, not {str(self.root)!r}.")
        recorded_id = db.get("root_id")
        if remote_root_id in (None, "root"):
            remote_root_id = recorded_id or client.root_id()
        if recorded_id and recorded_id != remote_root_id:
            raise SyncError("State DB belongs to a different Drive folder.")
        self.root_id = remote_root_id
        db.set("root_id", self.root_id)
        # Set when we create untracked local files (conflicted copies) that the
        # next local pass needs to upload.
        self.dirty = False
        # rel_path -> last error, for status reporting (D-Bus later).
        self.errors: dict[str, str] = {}
        self._stats = PassStats()

    # ================================================================ helpers

    def _abs(self, rel: str) -> Path:
        """Where rel lives on disk (in dry runs, before simulated moves)."""
        for new, old in reversed(self._vmoves):
            if rel == new or rel.startswith(new + "/"):
                rel = old + rel[len(new):]
        return self.root / rel

    # ================================================================ effects
    # Every change to the local disk or to Drive goes through one of these, so
    # it lands in the operation log — and is only simulated in dry-run mode.

    def _op(self, op: str, path: str, **detail) -> None:
        self.oplog.record(op, path, pair=self.name, **detail)
        self._notify(path, detail.get("src"))

    def _notify(self, *rels: str | None) -> None:
        changed = [r for r in rels if r is not None]
        if not changed or self.dry_run:
            return
        for fn in list(self.listeners):
            try:
                fn(changed)
            except Exception:  # noqa: BLE001 — never let a UI hook break syncing
                log.exception("status listener failed")

    @contextmanager
    def _activity(self, rel: str):
        """Mark rel as 'syncing' for the duration of a transfer."""
        if self.dry_run:
            yield
            return
        self.active.add(rel)
        self._notify(rel)
        try:
            yield
        finally:
            self.active.discard(rel)
            self._notify(rel)

    def _fx_download(self, f: dict, rel: str):
        size = int(f.get("size", 0))
        self._stats.downloaded += 1
        if self.dry_run:
            self._op("download", rel, file_id=f["id"], size=size)
            self._vgone.discard(rel)
            self._vcreated.add(rel)
            ts = rfc3339_to_ts(f["modifiedTime"]) if f.get("modifiedTime") else 0
            return FakeStat(size, int(ts * 1e9))
        with self._activity(rel):
            st = download_file(self.client, f, self.root / rel, self.tmp)
        self._op("download", rel, file_id=f["id"], size=st.st_size, md5=f.get("md5Checksum"))
        return st

    def _fx_write_link(self, f: dict, rel: str):
        dest = self._abs(rel)
        if self.dry_run:
            if read_stub_id(dest) != f["id"]:
                self._op("write_link", rel, file_id=f["id"])
            self._vcreated.add(rel)
            return FakeStat(0, 0)
        new = not dest.exists()
        st = write_stub(f, dest)
        if new:
            self._op("write_link", rel, file_id=f["id"])
        return st

    def _fx_mkdir(self, rel: str) -> None:
        p = self._abs(rel)
        if p.is_dir() and rel not in self._vgone:
            return
        if self.dry_run:
            self._vgone.discard(rel)
            self._vcreated.add(rel)
        else:
            p.mkdir(exist_ok=True)
        self._op("mkdir", rel)

    def _fx_move_local(self, src: str, dst: str, *, op: str = "move_local", reason: str | None = None):
        if self.dry_run:
            self._vmoves.append((dst, src))
        else:
            (self.root / src).rename(self.root / dst)
        self._op(op, dst, src=src, reason=reason)

    def _fx_trash_local(self, rel: str, reason: str) -> None:
        if self.dry_run:
            self._vgone.add(rel)
        else:
            trash_local(self.root / rel)
        self._op("trash_local", rel, reason=reason)

    def _fx_delete_local(self, rel: str, reason: str) -> None:
        if self.dry_run:
            self._vgone.add(rel)
        else:
            shutil.rmtree(self.root / rel)
        self._op("delete_local", rel, reason=reason)

    def _new_id(self) -> str:
        if self.dry_run:
            self._dry_ids += 1
            return f"dry-run-{self._dry_ids}"
        return self.client.generate_id()

    def _fx_create_folder(self, rel: str, name: str, parent_id: str, file_id: str) -> dict:
        if self.dry_run:
            f = {"id": file_id, "name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        else:
            f = self.client.create_folder(name, parent_id, file_id=file_id)
        self._op("create_folder", rel, file_id=f["id"])
        return f

    def _fx_upload_new(self, rel, path, name, parent_id, mtime, file_id) -> dict:
        if self.dry_run:
            size = path.stat().st_size
            self._op("upload", rel, file_id=file_id, size=size)
            return {
                "id": file_id, "name": name, "parents": [parent_id],
                "mimeType": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "md5Checksum": md5_file(path), "size": str(size),
            }
        with self._activity(rel):
            f = self.client.upload_new(path, name, parent_id, mtime=mtime, file_id=file_id)
        self._op("upload", rel, file_id=f["id"], size=int(f.get("size", 0)), md5=f.get("md5Checksum"))
        return f

    def _fx_update_content(self, rel, file_id, path, mtime) -> dict:
        if self.dry_run:
            size = path.stat().st_size
            self._op("update", rel, file_id=file_id, size=size)
            f = dict(self.client.get_file(file_id) or {"id": file_id, "name": path.name,
                                                      "mimeType": "application/octet-stream"})
            f.update(md5Checksum=md5_file(path), size=str(size))
            return f
        with self._activity(rel):
            f = self.client.update_content(file_id, path, mtime=mtime)
        self._op("update", rel, file_id=file_id, size=int(f.get("size", 0)), md5=f.get("md5Checksum"))
        return f

    def _fx_move_remote(self, src: str, dst: str, file_id: str, **kw) -> dict | None:
        if self.dry_run:
            f = self.client.get_file(file_id)
            if f is not None:
                f = dict(f)
                if kw.get("name"):
                    f["name"] = kw["name"]
                if kw.get("add_parent"):
                    f["parents"] = [kw["add_parent"]]
        else:
            f = self.client.update_metadata(file_id, **kw)
        self._op("move_remote", dst, src=src, file_id=file_id)
        return f

    def _fx_trash_remote(self, rel: str, file_id: str, reason: str) -> None:
        if not self.dry_run:
            self.client.trash(file_id)
        self._op("trash_remote", rel, file_id=file_id, reason=reason)

    def _virtualize(self, found: dict) -> dict:
        """Dry runs: make a fresh scan reflect the simulated local changes."""
        out = {}
        for rel, st in found.items():
            for new, old in self._vmoves:
                if rel == old or rel.startswith(old + "/"):
                    rel = new + rel[len(old):]
            if rel in self._vgone or any(rel.startswith(g + "/") for g in self._vgone):
                continue
            out[rel] = st
        for rel in self._vcreated:  # simulated downloads replace what's on disk
            if rel not in self._vgone:
                it = self.db.by_path(rel)
                if it is not None:
                    mode = statmod.S_IFDIR | 0o755 if it.is_folder else statmod.S_IFREG | 0o644
                    out[rel] = FakeStat(it.local_size or 0, it.local_mtime_ns or 0, mode)
        return out

    @staticmethod
    def _join(parent_rel: str, name: str) -> str:
        return f"{parent_rel}/{name}" if parent_rel else name

    def _parent_rel(self, parent_id: str) -> str | None:
        """Local folder for a remote parent id, or None if it isn't synced."""
        if parent_id == self.root_id:
            return ""
        it = self.db.by_id(parent_id)
        return it.rel_path if it and it.is_folder else None

    def _parent_id(self, rel: str) -> str | None:
        """Remote id of the local folder containing rel, or None if untracked."""
        d = posixpath.dirname(rel)
        if d == "":
            return self.root_id
        it = self.db.by_path(d)
        return it.file_id if it and it.is_folder else None

    def _local_name(self, f: dict, parent_rel: str) -> str:
        mime = f["mimeType"]
        base = stub_name(f["name"], mime) if is_google_doc(mime) else to_local(f["name"])
        taken = {
            posixpath.basename(it.rel_path)
            for it in self.db.in_dir(parent_rel)
            if it.file_id != f["id"]
        }
        if parent_rel == "":
            taken.add(TMP_DIRNAME)
        return self._join(parent_rel, dedupe(base, taken))

    @staticmethod
    def _remote_name(local_name: str, mime: str) -> str:
        if is_google_doc(mime):
            for ext in STUB_EXTS:
                if local_name.endswith(ext):
                    local_name = local_name[: -len(ext)]
                    break
        return to_remote(local_name)

    def _set_aside(self, rel: str) -> str:
        """Rename an untracked/conflicting local path out of the way."""
        backup_rel = str(conflict_name(self.root / rel).relative_to(self.root))
        self._fx_move_local(rel, backup_rel, op="conflict")
        self._stats.conflicts.append(backup_rel)
        self.dirty = True
        return backup_rel

    def _download(self, f: dict, rel: str):
        return self._fx_download(f, rel)

    def _fail(self, rel: str, e: Exception) -> None:
        msg = f"{type(e).__name__}: {e}"
        log.error("%s: %s", rel, msg)
        self.errors[rel] = msg
        self._stats.errors.append((rel, msg))
        self._notify(rel)

    def _ok(self, rel: str) -> None:
        if self.errors.pop(rel, None) is not None:
            self._notify(rel)

    # ================================================================ remote → local

    def remote_pass(self) -> PassStats:
        token = self.db.page_token
        if token is None:
            raise SyncError("No change-feed position yet; run `gdrivesync pull` first.")
        try:
            changes, new_token = self.client.changes_since(token)
        except InvalidPageToken as e:
            return self.recover_feed(e)
        return self.apply_changes(changes, new_token)

    def recover_feed(self, cause: Exception) -> PassStats:
        """Drive rejected our change-feed position: compare everything with
        Drive directly and continue from a fresh position."""
        last = float(self.db.get(FEED_RESET_KEY) or 0)
        if time.time() - last < FEED_RESET_COOLDOWN_S:
            # Rejected again right after a reset: something else is wrong; don't
            # burn API quota re-listing Drive every poll.
            raise SyncError(f"Drive keeps rejecting the change feed ({cause}); not retrying yet.")
        log.warning("[%s] %s — re-checking everything against Drive", self.name or self.root, cause)
        stats = self.full_check()
        self.db.set(FEED_RESET_KEY, str(time.time()))
        return stats

    def full_check(self) -> PassStats:
        """Compare the whole tree with Drive (not just the change feed) and apply
        any differences. Recovers from a lost/rejected feed position and detects
        anything the feed missed. Uses the same per-item logic as remote_pass, so
        local edits are kept, conflicts become conflicted copies, and unchanged
        files aren't re-downloaded."""
        self._stats = PassStats()
        # Take the new position first so nothing changing during the listing is lost.
        token = self.client.start_page_token()
        drive_root = self.client.root_id()
        if self.root_id != drive_root:
            r = self.client.get_file(self.root_id)
            if r is None or r.get("trashed"):
                raise SyncError(
                    f"The Drive folder for {self.name or self.root} was deleted or trashed. "
                    "Restore it on Drive, or remove this pair."
                )
            listing = list_subtree(self.client, self.root_id, self.excluded)
        else:
            listing = list(self.client.list_all())

        latest: dict[str, dict | None] = {f["id"]: f for f in listing if f["id"] != self.root_id}
        for it in self.db.all():
            latest.setdefault(it.file_id, None)  # tracked but gone from Drive
        failed = self._apply_remote(latest)
        self.db.set(RETRY_KEY, json.dumps(failed))
        self.db.page_token = token
        self.db.set(FULL_CHECK_KEY, str(time.time()))
        return self._stats

    def apply_changes(self, changes: list[dict], new_token: str) -> PassStats:
        """Apply a batch from the Drive change feed (which may be shared between
        pairs — anything outside this pair's folder is ignored)."""
        self._stats = PassStats()
        latest: dict[str, dict | None] = {}
        for c in changes:
            latest[c["fileId"]] = None if c.get("removed") else c.get("file")
        if self.root_id in latest:
            r = latest.pop(self.root_id)
            if r is None or r.get("trashed"):
                # Don't mirror a whole-folder deletion; make the user decide.
                raise SyncError(
                    f"The Drive folder for {self.name or self.root} was deleted or trashed. "
                    "Restore it on Drive, or remove this pair."
                )
        for fid in json.loads(self.db.get(RETRY_KEY) or "[]"):
            if fid not in latest:
                latest[fid] = self.client.get_file(fid)

        failed = self._apply_remote(latest)
        self.db.set(RETRY_KEY, json.dumps(failed))
        self.db.page_token = new_token
        return self._stats

    def _apply_remote(self, latest: dict[str, dict | None]) -> list[str]:
        """Apply changes parents-first. Returns ids that failed (retried next pass)."""
        pending = dict(latest)
        failed: list[str] = []
        while pending:
            progressed = False
            for fid, f in list(pending.items()):
                parent = ((f or {}).get("parents") or [None])[0]
                if (
                    f is not None
                    and parent in pending
                    and parent != fid
                    and self.db.by_id(parent) is None
                ):
                    continue  # its parent folder is new in this batch; do that first
                del pending[fid]
                progressed = True
                self._apply_remote_safely(fid, f, failed)
            if not progressed:  # cycle — shouldn't happen; process the rest anyway
                for fid, f in pending.items():
                    self._apply_remote_safely(fid, f, failed)
                break
        return failed

    def _apply_remote_safely(self, fid: str, f: dict | None, failed: list[str]) -> None:
        try:
            self._apply_remote_one(fid, f)
        except Exception as e:  # noqa: BLE001 — keep going, retry later
            self._fail(self._rel_for_error(fid, f), e)
            failed.append(fid)

    def _rel_for_error(self, fid: str, f: dict | None) -> str:
        """Best guess at the local path an item maps to, for error reporting."""
        item = self.db.by_id(fid)
        if item is not None:
            return item.rel_path
        if f and f.get("parents"):
            parent_rel = self._parent_rel(f["parents"][0])
            if parent_rel is not None:
                try:
                    return self._local_name(f, parent_rel)
                except Exception:  # noqa: BLE001
                    pass
        return (f or {}).get("name", fid)

    def _apply_remote_one(self, fid: str, f: dict | None) -> None:
        if fid == self.root_id:
            return
        item = self.db.by_id(fid)
        parent_rel: str | None = None
        if (
            f is not None
            and not f.get("trashed")
            and f["mimeType"] != SHORTCUT_MIME
            and fid not in self.excluded
            and f.get("parents")
        ):
            parent_rel = self._parent_rel(f["parents"][0])

        if fid in self.excluded:
            alive = f is not None and not f.get("trashed") and f.get("parents")
            if item is None:
                if alive:
                    self._track_excluded_location(f)  # follow renames/moves
            elif not alive:
                self._remote_removed(item)
            # else: still synced locally; reconcile_exclusions() drops it safely
            return

        if parent_rel is None:  # deleted, trashed, or moved out of My Drive
            if item:
                self._remote_removed(item)
        elif item is None:
            self._remote_new(f, parent_rel)
        else:
            self._remote_changed(item, f, parent_rel)

    def _remote_removed(self, item: Item) -> None:
        if item.is_folder:
            for d in sorted(
                self.db.descendants(item.rel_path), key=lambda i: i.rel_path.count("/"), reverse=True
            ):
                self._remove_local(d)
        self._remove_local(item)

    def _remove_local(self, item: Item) -> None:
        p = self._abs(item.rel_path)
        if item.is_folder:
            if p.is_dir():
                leftovers = [
                    e for e in os.listdir(p)
                    if not self.rules.ignored(self._join(item.rel_path, e))
                    and self._join(item.rel_path, e) not in self._vgone
                ]
                if not leftovers:
                    self._fx_trash_local(item.rel_path, "deleted on Drive")
                    self._stats.trashed_local += 1
                else:
                    # Holds locally-changed or new files: keep; they'll be re-uploaded.
                    log.warning("kept folder %s (removed on Drive) — has local changes", item.rel_path)
                    self.dirty = True
            self.db.delete(item.file_id)
            return
        if p.is_file() and not p.is_symlink():
            if is_google_doc(item.mime_type) or unchanged_since(item, p.stat()):
                self._fx_trash_local(item.rel_path, "deleted on Drive")
                self._stats.trashed_local += 1
            else:
                log.warning("kept %s (deleted on Drive, but edited locally) — re-uploading", item.rel_path)
                self.dirty = True
        self.db.delete(item.file_id)
        self._ok(item.rel_path)

    def _remote_new(self, f: dict, parent_rel: str) -> None:
        rel = self._local_name(f, parent_rel)
        dest = self._abs(rel)
        mime = f["mimeType"]

        if mime == FOLDER_MIME:
            if dest.exists() and not dest.is_dir():
                self._set_aside(rel)
            self._fx_mkdir(rel)
            self.db.upsert(make_item(f, rel, None))
            # Contents may predate this change (moved in / restored from trash).
            for child in self.client.list_children(f["id"]):
                if self.db.by_id(child["id"]) is None:
                    self._apply_remote_one(child["id"], child)
            return

        if is_google_doc(mime):
            if dest.exists() and (dest.is_dir() or read_stub_id(dest) != f["id"]):
                self._set_aside(rel)
            self.db.upsert(make_item(f, rel, self._fx_write_link(f, rel)))
            return

        if dest.exists():
            if dest.is_file() and f.get("md5Checksum") and md5_file(dest) == f["md5Checksum"]:
                self.db.upsert(make_item(f, rel, dest.stat()))  # identical — adopt
                return
            self._set_aside(rel)
        self.db.upsert(make_item(f, rel, self._download(f, rel)))
        self._ok(rel)

    def _remote_changed(self, item: Item, f: dict, parent_rel: str) -> None:
        parent_id = f["parents"][0]
        if f["name"] == item.name and parent_id == item.parent_id:
            rel = item.rel_path  # keep any local de-dup suffix
        else:
            rel = self._local_name(f, parent_rel)

        if rel != item.rel_path:
            src, dst = self._abs(item.rel_path), self._abs(rel)
            if dst.exists():
                self._set_aside(rel)
            if src.exists():
                self._fx_move_local(item.rel_path, rel, reason="moved on Drive")
            if item.is_folder:
                self.db.move_prefix(item.rel_path, rel)
            self._stats.moved += 1
            item.rel_path = rel

        dest = self._abs(rel)
        if rel == item.rel_path and not dest.exists() and not dest.is_symlink():
            unchanged_on_drive = item.is_folder or (
                f.get("md5Checksum") == item.md5
                and (item.md5 is not None or f.get("modifiedTime") == item.remote_mtime)
            )
            if unchanged_on_drive:
                # Deleted locally, not changed on Drive: a pending local deletion
                # (the next local pass trashes it on Drive). Don't resurrect it.
                new = make_item(f, rel, None)
                new.md5 = item.md5
                new.local_mtime_ns, new.local_size = item.local_mtime_ns, item.local_size
                self.db.upsert(new)
                return

        if item.is_folder:
            self._fx_mkdir(rel)
            self.db.upsert(make_item(f, rel, None))
            return

        if is_google_doc(item.mime_type):
            self.db.upsert(make_item(f, rel, self._fx_write_link(f, rel)))
            return

        remote_md5 = f.get("md5Checksum")
        if remote_md5 == item.md5 and dest.is_file():
            # Metadata-only change. Keep the old local base so pending local
            # edits are still detected by the next local pass.
            new = make_item(f, rel, None)
            new.local_mtime_ns, new.local_size = item.local_mtime_ns, item.local_size
            self.db.upsert(new)
            return

        if dest.is_dir():
            self._set_aside(rel)
        elif dest.is_file():
            if not unchanged_since(item, dest.stat()):
                if remote_md5 and md5_file(dest) == remote_md5:
                    self.db.upsert(make_item(f, rel, dest.stat()))
                    return
                self._set_aside(rel)  # edited on both sides
        self.db.upsert(make_item(f, rel, self._download(f, rel)))
        self._ok(rel)

    # ================================================================ exclusions

    def _excluded_paths(self) -> dict[str, str]:
        return json.loads(self.db.get(EXCLUDED_KEY) or "{}")

    def _save_excluded_paths(self, paths: dict[str, str]) -> None:
        self.db.set(EXCLUDED_KEY, json.dumps(paths))

    def _track_excluded_location(self, f: dict) -> None:
        paths = self._excluded_paths()
        parent_rel = self._parent_rel(f["parents"][0])
        if parent_rel is None:
            paths.pop(f["id"], None)
        else:
            paths[f["id"]] = self._local_name(f, parent_rel)
        self._save_excluded_paths(paths)

    def reconcile_exclusions(self) -> None:
        """Bring the local tree in line with the excluded-folder list:
        newly excluded folders are removed locally (their content is safe on
        Drive), newly re-included ones are downloaded."""
        paths = self._excluded_paths()
        changed = False
        for fid in self.excluded:
            item = self.db.by_id(fid)
            if item is not None:
                if item.is_folder and self._drop_excluded_folder(item):
                    paths[fid] = item.rel_path
                    changed = True
            elif fid not in paths:
                f = self.client.get_file(fid)
                if f and f.get("parents") and self._parent_rel(f["parents"][0]) is not None:
                    paths[fid] = self._local_name(f, self._parent_rel(f["parents"][0]))
                    changed = True
        for fid in [k for k in paths if k not in self.excluded]:
            del paths[fid]
            changed = True
            f = self.client.get_file(fid)
            if f is not None:
                self._apply_remote_one(fid, f)  # downloads the folder and its contents
        if changed:
            self._save_excluded_paths(paths)

    def _drop_excluded_folder(self, item: Item) -> bool:
        """Remove a newly excluded folder's local copy. Returns False (try again
        later) if it holds anything not yet on Drive."""
        base = self._abs(item.rel_path)
        tracked = {it.rel_path: it for it in self.db.descendants(item.rel_path)}
        local = scan(base, self.rules) if base.is_dir() else {}
        for sub, st in local.items():
            rel = f"{item.rel_path}/{sub}"
            it = tracked.get(rel)
            pending = (
                it is None
                or (not it.is_folder and not is_dir_stat(st) and not unchanged_since(it, st))
            )
            if pending:
                log.warning(
                    "not excluding %s yet: %s has changes that aren't on Drive yet",
                    item.rel_path, rel,
                )
                return False
        if base.exists():
            self._fx_delete_local(item.rel_path, "excluded from sync; still on Drive")
        for it in tracked.values():
            self.db.delete(it.file_id)
        self.db.delete(item.file_id)
        return True

    # ================================================================ local → remote

    def local_pass(self) -> PassStats:
        self._stats = PassStats()
        if not self.root.is_dir():
            raise SyncError(f"Sync folder {self.root} is missing; not syncing deletions.")
        self._guard("(excluded folders)", self.reconcile_exclusions)
        now = time.time()
        found = scan(self.root, self.rules)
        # Never upload into an excluded folder's place.
        if self.dry_run:
            found = self._virtualize(found)
        for ex in self._excluded_paths().values():
            for rel in [r for r in found if r == ex or r.startswith(ex + "/")]:
                del found[rel]
        tracked = {it.rel_path: it for it in self.db.all()}

        def settled(st: os.stat_result) -> bool:
            return now - st.st_mtime >= self.settle_s

        type_changed = [
            it for rel, it in tracked.items()
            if rel in found and is_dir_stat(found[rel]) != it.is_folder
        ]
        missing = {
            rel: it for rel, it in tracked.items() if rel not in found
        }
        new = sorted(
            (rel for rel in found if rel not in tracked or tracked[rel] in type_changed),
            key=lambda r: (r.count("/"), r),
        )
        new_dirs = [r for r in new if is_dir_stat(found[r])]
        new_files = [r for r in new if not is_dir_stat(found[r])]

        # Finish or clean up creates interrupted by a crash.
        self._recover_intents(found, tracked)

        # 0. A path that switched between file and folder: drop the old remote item.
        for it in type_changed:
            self._guard(it.rel_path, self._local_deleted, it)

        # 1. New folders, top-down.
        for rel in new_dirs:
            self._guard(rel, self._local_new_folder, rel)

        # 2. Moves/renames the watcher missed: pair missing items with new files.
        new_files = self._detect_moves(missing, new_files, found, settled)

        n_missing = sum(1 for it in missing.values() if not it.is_folder)
        n_files = sum(1 for it in tracked.values() if not it.is_folder)
        if (
            not self.allow_mass_delete
            and n_missing > self.mass_delete_limit
            and n_missing > n_files // 2
        ):
            raise SyncError(
                f"{n_missing} of {n_files} synced files disappeared locally. Refusing to "
                "trash them on Drive; run `gdrivesync sync --allow-mass-delete` if intended."
            )
        # 3. Deletions (skip items whose parent folder is also gone — trashing
        #    the folder covers them).
        gone = set(missing)
        for rel in sorted(missing, key=lambda r: r.count("/")):
            if posixpath.dirname(rel) in gone:
                self.db.delete(missing[rel].file_id)
                continue
            self._guard(rel, self._local_deleted, missing[rel])

        # 4. New files.
        for rel in new_files:
            if not settled(found[rel]):
                self._stats.deferred += 1
                continue
            self._guard(rel, self._local_new_file, rel, found[rel])

        # 5. Modified files.
        for rel, it in tracked.items():
            st = found.get(rel)
            if st is None or it.is_folder or is_dir_stat(st) or unchanged_since(it, st):
                continue
            if self.db.by_id(it.file_id) is None:
                continue  # handled above
            if not settled(st):
                self._stats.deferred += 1
                continue
            self._guard(rel, self._local_modified, it, st)

        return self._stats

    def _guard(self, rel: str, fn, *args) -> None:
        try:
            fn(*args)
            self._ok(rel)
        except Exception as e:  # noqa: BLE001 — per-item; retried next pass
            self._fail(rel, e)

    def _detect_moves(self, missing, new_files, found, settled) -> list[str]:
        by_size: dict[int, list[Item]] = {}
        stubs: dict[str, Item] = {}
        for it in missing.values():
            if it.is_folder:
                continue
            if is_google_doc(it.mime_type):
                stubs[it.file_id] = it
            elif it.md5 is not None and it.local_size is not None:
                by_size.setdefault(it.local_size, []).append(it)

        remaining = []
        for rel in new_files:
            st = found[rel]
            match: Item | None = None
            if rel.endswith(STUB_EXTS):
                sid = read_stub_id(self._abs(rel))
                match = stubs.pop(sid, None) if sid else None
            elif st.st_size in by_size and settled(st):
                h = md5_file(self._abs(rel))
                cands = by_size[st.st_size]
                match = next((c for c in cands if c.md5 == h), None)
                if match:
                    cands.remove(match)
            if match and self._parent_id(rel) is not None:
                old_rel = match.rel_path
                try:
                    self._local_move(match, rel)
                    del missing[old_rel]
                    continue
                except Exception as e:  # noqa: BLE001
                    self._fail(rel, e)
            remaining.append(rel)
        return remaining

    def local_move_event(self, src_rel: str, dest_rel: str) -> None:
        """Apply a rename/move reported by the file watcher."""
        if self.rules.ignored(src_rel) or self.rules.ignored(dest_rel):
            return  # the next scan sorts it out (e.g. editor backup files)
        item = self.db.by_path(src_rel)
        if item is None or not self._abs(dest_rel).exists():
            return  # untracked, or our own move already recorded
        if self._parent_id(dest_rel) is None:
            return  # destination folder not synced yet; scan will handle as delete+new
        self._guard(dest_rel, self._local_move, item, dest_rel)

    def _local_move(self, item: Item, new_rel: str) -> None:
        parent_id = self._parent_id(new_rel)
        if parent_id is None:
            raise SyncError("destination folder isn't synced yet")
        other = self.db.by_path(new_rel)
        if other and other.file_id != item.file_id:  # moved over an existing file
            self._fx_trash_remote(new_rel, other.file_id, "replaced by a moved file")
            self.db.delete(other.file_id)
            self._stats.trashed_remote += 1

        kw = {}
        name = self._remote_name(posixpath.basename(new_rel), item.mime_type)
        if name != item.name:
            kw["name"] = name
        if parent_id != item.parent_id:
            kw["add_parent"] = parent_id
            if item.parent_id:
                kw["remove_parent"] = item.parent_id
        f = (
            self._fx_move_remote(item.rel_path, new_rel, item.file_id, **kw)
            if kw else self.client.get_file(item.file_id)
        )
        if f is None:
            raise SyncError("file vanished on Drive")

        if item.is_folder:
            self.db.move_prefix(item.rel_path, new_rel)
        new = make_item(f, new_rel, None)
        # Keep the old content base so an edit + rename still uploads.
        new.md5, new.local_mtime_ns, new.local_size = item.md5, item.local_mtime_ns, item.local_size
        self.db.upsert(new)
        self._stats.moved += 1

    def _local_deleted(self, item: Item) -> None:
        if item.is_folder:
            self._fx_trash_remote(item.rel_path, item.file_id, "deleted locally")
            for d in self.db.descendants(item.rel_path):
                self.db.delete(d.file_id)
            self.db.delete(item.file_id)
            self._stats.trashed_remote += 1
            return
        if not is_google_doc(item.mime_type):
            f = self.client.get_file(item.file_id)
            if f and not f.get("trashed") and f.get("md5Checksum") != item.md5:
                # Edited on Drive since we last synced: the edit wins over the delete.
                if self._abs(item.rel_path).parent.is_dir():
                    self.db.upsert(make_item(f, item.rel_path, self._download(f, item.rel_path)))
                    return
        self._fx_trash_remote(item.rel_path, item.file_id, "deleted locally")
        self.db.delete(item.file_id)
        self._stats.trashed_remote += 1

    def _local_new_folder(self, rel: str) -> None:
        parent_id = self._parent_id(rel)
        if parent_id is None:
            raise SyncError("parent folder isn't synced")
        name = to_remote(posixpath.basename(rel))
        existing = [
            c for c in self.client.find_children(parent_id, name)
            if c["mimeType"] == FOLDER_MIME and self.db.by_id(c["id"]) is None
        ]
        if existing:
            f = existing[0]
        else:
            f = self._create_with_intent(
                rel, parent_id, lambda fid: self._fx_create_folder(rel, name, parent_id, fid)
            )
        self.db.upsert(make_item(f, rel, None))
        self.db.remove_pending(rel)
        self._stats.folders_created += 1

    def _create_with_intent(self, rel: str, parent_id: str, create) -> dict:
        """Create on Drive under a pre-generated id journaled in the DB first, so a
        retry or a crash between "Drive created it" and "we recorded it" reuses
        the same id instead of making a duplicate."""
        intent = self.db.pending(rel)
        if intent and intent["parent_id"] != parent_id:
            self._abandon_intent(intent)
            intent = None
        if intent:
            log.info("resuming interrupted create of %s", rel)
            file_id = intent["file_id"]
        else:
            file_id = self._new_id()
            self.db.add_pending(rel, file_id, parent_id)
        return create(file_id)

    def _abandon_intent(self, intent: dict) -> None:
        """Trash whatever an interrupted create left on Drive (if anything)."""
        f = self.client.get_file(intent["file_id"])
        if f is not None and not f.get("trashed") and self.db.by_id(f["id"]) is None:
            self._fx_trash_remote(intent["rel_path"], f["id"], "orphaned partial upload")
            self._stats.trashed_remote += 1
        self.db.remove_pending(intent["rel_path"])

    def _recover_intents(self, found: dict, tracked: dict) -> None:
        """Settle creates journaled by an earlier run that didn't finish."""
        for intent in self.db.all_pending():
            rel = intent["rel_path"]
            if self.db.by_id(intent["file_id"]) is not None:
                self.db.remove_pending(rel)  # it completed; only the cleanup was lost
            elif rel in found and rel not in tracked:
                continue  # still a new local item: this pass resumes it with the same id
            else:
                # The local item is gone (deleted/renamed while we were down).
                self._guard(rel, self._abandon_intent, intent)

    def _local_new_file(self, rel: str, st: os.stat_result) -> None:
        path = self._abs(rel)
        if rel.endswith(STUB_EXTS) and read_stub_id(path):
            return  # a copy of a Google-Docs link file; never upload these
        parent_id = self._parent_id(rel)
        if parent_id is None:
            raise SyncError("parent folder isn't synced")
        name = to_remote(posixpath.basename(rel))
        local_md5 = md5_file(path)
        # Adopt an identical untracked remote file (e.g. after a crash mid-upload).
        f = next(
            (
                c for c in self.client.find_children(parent_id, name)
                if c.get("md5Checksum") == local_md5 and self.db.by_id(c["id"]) is None
            ),
            None,
        )
        if f is None:
            f = self._create_with_intent(
                rel,
                parent_id,
                lambda fid: self._fx_upload_new(rel, path, name, parent_id, st.st_mtime, fid),
            )
            if f.get("md5Checksum") != local_md5:
                # Recovered an earlier, interrupted create whose content is now
                # stale (the file changed since): bring it up to date.
                f = self._fx_update_content(rel, f["id"], path, st.st_mtime)
            self._stats.uploaded += 1
        # Base stat is from *before* the upload: if the file changed meanwhile,
        # the next pass sees the difference and uploads again.
        self.db.upsert(make_item(f, rel, st))
        self.db.remove_pending(rel)

    def _local_modified(self, item: Item, st: os.stat_result) -> None:
        path = self._abs(item.rel_path)
        if is_google_doc(item.mime_type):
            # Link files aren't real content; ignore edits.
            item.local_mtime_ns, item.local_size = st.st_mtime_ns, st.st_size
            self.db.upsert(item)
            return
        local_md5 = md5_file(path)
        if local_md5 == item.md5:  # touched, not changed
            item.local_mtime_ns, item.local_size = st.st_mtime_ns, st.st_size
            self.db.upsert(item)
            return

        f = self.client.get_file(item.file_id)
        if f is None or f.get("trashed"):
            # Deleted on Drive but edited here: the edit wins — upload as new.
            self.db.delete(item.file_id)
            self._local_new_file(item.rel_path, st)
            return
        if f.get("md5Checksum") != item.md5:
            # Changed on both sides.
            self._set_aside(item.rel_path)
            self.db.upsert(make_item(f, item.rel_path, self._download(f, item.rel_path)))
            return
        f = self._fx_update_content(item.rel_path, item.file_id, path, st.st_mtime)
        self.db.upsert(make_item(f, item.rel_path, st))
        self._stats.uploaded += 1
