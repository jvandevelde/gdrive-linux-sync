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
import os
import posixpath
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from gdrivesync.config import BUILTIN_IGNORES, TMP_DIRNAME
from gdrivesync.drive import FOLDER_MIME, SHORTCUT_MIME, is_google_doc
from gdrivesync.localfs import IgnoreRules, is_dir_stat, scan, trash_local
from gdrivesync.names import DEFAULT_STUB_EXT, DOC_STUB_EXT, dedupe, stub_name, to_local, to_remote
from gdrivesync.pull import (
    PullError,
    conflict_name,
    download_file,
    make_item,
    md5_file,
    read_stub_id,
    unchanged_since,
    write_stub,
)
from gdrivesync.state import Item, StateDB

log = logging.getLogger("gdrivesync.engine")

STUB_EXTS = tuple(set(DOC_STUB_EXT.values()) | {DEFAULT_STUB_EXT})
RETRY_KEY = "remote_retry_ids"


class SyncError(RuntimeError):
    pass


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
        excluded: Iterable[str] = (),
        rules: IgnoreRules | None = None,
        settle_s: float = 3.0,
        mass_delete_limit: int = 100,
    ):
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
        self.root_id = db.get("root_id") or client.root_id()
        db.set("root_id", self.root_id)
        # Set when we create untracked local files (conflicted copies) that the
        # next local pass needs to upload.
        self.dirty = False
        # rel_path -> last error, for status reporting (D-Bus later).
        self.errors: dict[str, str] = {}
        self._stats = PassStats()

    # ================================================================ helpers

    def _abs(self, rel: str) -> Path:
        return self.root / rel

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
        src = self._abs(rel)
        backup = conflict_name(src)
        src.rename(backup)
        backup_rel = str(backup.relative_to(self.root))
        log.warning("conflict: kept local %s as %s", rel, backup_rel)
        self._stats.conflicts.append(backup_rel)
        self.dirty = True
        return backup_rel

    def _download(self, f: dict, rel: str) -> os.stat_result:
        st = download_file(self.client, f, self._abs(rel), self.tmp)
        self._stats.downloaded += 1
        log.info("downloaded %s", rel)
        return st

    def _fail(self, rel: str, e: Exception) -> None:
        msg = f"{type(e).__name__}: {e}"
        log.error("%s: %s", rel, msg)
        self.errors[rel] = msg
        self._stats.errors.append((rel, msg))

    def _ok(self, rel: str) -> None:
        self.errors.pop(rel, None)

    # ================================================================ remote → local

    def remote_pass(self) -> PassStats:
        token = self.db.page_token
        if token is None:
            raise SyncError("No change-feed position yet; run `gdrivesync pull` first.")
        self._stats = PassStats()
        changes, new_token = self.client.changes_since(token)
        latest: dict[str, dict | None] = {}
        for c in changes:
            latest[c["fileId"]] = None if c.get("removed") else c.get("file")
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
            name = (f or {}).get("name", fid)
            self._fail(name, e)
            failed.append(fid)

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
                ]
                if not leftovers:
                    trash_local(p)
                    self._stats.trashed_local += 1
                else:
                    # Holds locally-changed or new files: keep; they'll be re-uploaded.
                    log.warning("kept folder %s (removed on Drive) — has local changes", item.rel_path)
                    self.dirty = True
            self.db.delete(item.file_id)
            return
        if p.is_file() and not p.is_symlink():
            if is_google_doc(item.mime_type) or unchanged_since(item, p.stat()):
                trash_local(p)
                self._stats.trashed_local += 1
                log.info("removed %s (deleted on Drive)", item.rel_path)
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
            dest.mkdir(exist_ok=True)
            self.db.upsert(make_item(f, rel, None))
            # Contents may predate this change (moved in / restored from trash).
            for child in self.client.list_children(f["id"]):
                if self.db.by_id(child["id"]) is None:
                    self._apply_remote_one(child["id"], child)
            return

        if is_google_doc(mime):
            if dest.exists() and (dest.is_dir() or read_stub_id(dest) != f["id"]):
                self._set_aside(rel)
            self.db.upsert(make_item(f, rel, write_stub(f, dest)))
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
                src.rename(dst)
            if item.is_folder:
                self.db.move_prefix(item.rel_path, rel)
            log.info("moved %s -> %s (on Drive)", item.rel_path, rel)
            self._stats.moved += 1
            item.rel_path = rel

        dest = self._abs(rel)
        if item.is_folder:
            dest.mkdir(exist_ok=True)
            self.db.upsert(make_item(f, rel, None))
            return

        if is_google_doc(item.mime_type):
            self.db.upsert(make_item(f, rel, write_stub(f, dest)))
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

    # ================================================================ local → remote

    def local_pass(self) -> PassStats:
        self._stats = PassStats()
        if not self.root.is_dir():
            raise SyncError(f"Sync folder {self.root} is missing; not syncing deletions.")
        now = time.time()
        found = scan(self.root, self.rules)
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
            self.client.trash(other.file_id)
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
        f = self.client.update_metadata(item.file_id, **kw) if kw else self.client.get_file(item.file_id)
        if f is None:
            raise SyncError("file vanished on Drive")

        if item.is_folder:
            self.db.move_prefix(item.rel_path, new_rel)
        new = make_item(f, new_rel, None)
        # Keep the old content base so an edit + rename still uploads.
        new.md5, new.local_mtime_ns, new.local_size = item.md5, item.local_mtime_ns, item.local_size
        self.db.upsert(new)
        log.info("moved %s -> %s (on Drive)", item.rel_path, new_rel)
        self._stats.moved += 1

    def _local_deleted(self, item: Item) -> None:
        if item.is_folder:
            self.client.trash(item.file_id)
            for d in self.db.descendants(item.rel_path):
                self.db.delete(d.file_id)
            self.db.delete(item.file_id)
            self._stats.trashed_remote += 1
            log.info("trashed folder %s on Drive", item.rel_path)
            return
        if not is_google_doc(item.mime_type):
            f = self.client.get_file(item.file_id)
            if f and not f.get("trashed") and f.get("md5Checksum") != item.md5:
                # Edited on Drive since we last synced: the edit wins over the delete.
                if self._abs(item.rel_path).parent.is_dir():
                    self.db.upsert(make_item(f, item.rel_path, self._download(f, item.rel_path)))
                    return
        self.client.trash(item.file_id)
        self.db.delete(item.file_id)
        self._stats.trashed_remote += 1
        log.info("trashed %s on Drive", item.rel_path)

    def _local_new_folder(self, rel: str) -> None:
        parent_id = self._parent_id(rel)
        if parent_id is None:
            raise SyncError("parent folder isn't synced")
        name = to_remote(posixpath.basename(rel))
        existing = [
            c for c in self.client.find_children(parent_id, name)
            if c["mimeType"] == FOLDER_MIME and self.db.by_id(c["id"]) is None
        ]
        f = existing[0] if existing else self.client.create_folder(name, parent_id)
        self.db.upsert(make_item(f, rel, None))
        self._stats.folders_created += 1
        log.info("created folder %s on Drive", rel)

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
            f = self.client.upload_new(path, name, parent_id, mtime=st.st_mtime)
            self._stats.uploaded += 1
            log.info("uploaded %s", rel)
        # Base stat is from *before* the upload: if the file changed meanwhile,
        # the next pass sees the difference and uploads again.
        self.db.upsert(make_item(f, rel, st))

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
        f = self.client.update_content(item.file_id, path, mtime=st.st_mtime)
        self.db.upsert(make_item(f, item.rel_path, st))
        self._stats.uploaded += 1
        log.info("uploaded %s", item.rel_path)
