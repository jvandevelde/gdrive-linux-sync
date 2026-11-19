"""One-way initial sync: mirror My Drive into the local sync root.

Safe to re-run: files already in sync are skipped, remote renames/moves are
applied locally with a rename instead of a re-download, and a local file that
differs from the remote is never overwritten — it's kept as a conflicted copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from gdrivesync.config import TMP_DIRNAME
from gdrivesync.drive import FOLDER_MIME, SHORTCUT_MIME, is_google_doc
from gdrivesync.names import dedupe, stub_name, to_local
from gdrivesync.state import Item, StateDB


class DriveLike(Protocol):
    def root_id(self) -> str: ...
    def list_children(self, folder_id: str) -> Iterable[dict]: ...
    def start_page_token(self) -> str: ...
    def list_all(self) -> Iterable[dict]: ...
    def download(self, file_id: str, dest: Path) -> None: ...


class PullError(RuntimeError):
    pass


@dataclass
class PullResult:
    folders: int = 0
    downloaded: int = 0
    downloaded_bytes: int = 0
    stubs: int = 0
    unchanged: int = 0
    adopted: int = 0  # local file already had identical content
    moved: int = 0  # remote rename/move applied locally
    skipped: int = 0  # shortcuts
    forgotten: int = 0  # DB rows no longer present remotely
    conflicts: list[str] = field(default_factory=list)  # conflicted-copy paths
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, message)


@dataclass
class _Download:
    file: dict
    rel: str


# --------------------------------------------------------------------------- helpers


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rfc3339_to_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def conflict_name(path: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H%M%S")
    stem, ext = os.path.splitext(path.name)
    candidate = path.with_name(f"{stem} (conflicted copy {stamp}){ext}")
    n = 2
    while candidate.exists():
        candidate = path.with_name(f"{stem} (conflicted copy {stamp} {n}){ext}")
        n += 1
    return candidate


def make_item(f: dict, rel: str, st: os.stat_result | None) -> Item:
    return Item(
        file_id=f["id"],
        parent_id=(f.get("parents") or [None])[0],
        name=f["name"],
        rel_path=rel,
        is_folder=f["mimeType"] == FOLDER_MIME,
        mime_type=f["mimeType"],
        md5=f.get("md5Checksum"),
        size=int(f["size"]) if "size" in f else None,
        remote_rev=f.get("headRevisionId"),
        remote_mtime=f.get("modifiedTime"),
        local_mtime_ns=st.st_mtime_ns if st else None,
        local_size=st.st_size if st else None,
    )


def unchanged_since(prev: Item, st: os.stat_result) -> bool:
    return prev.local_size == st.st_size and prev.local_mtime_ns == st.st_mtime_ns


def list_subtree(client: DriveLike, folder_id: str, excluded: Iterable[str] = ()) -> list[dict]:
    """Everything below folder_id (one listing call per folder; skips excluded)."""
    excluded = set(excluded)
    out: list[dict] = []
    stack = [folder_id]
    while stack:
        for f in client.list_children(stack.pop()):
            out.append(f)
            if f["mimeType"] == FOLDER_MIME and f["id"] not in excluded:
                stack.append(f["id"])
    return out


def build_tree(
    files: Iterable[dict], root_id: str, excluded: Iterable[str] = ()
) -> tuple[list[tuple[dict, str]], int]:
    """Walk My Drive from the root. Returns ([(file, rel_path)] parents-first,
    number of skipped shortcuts). Local names are made safe and unique per folder;
    the oldest item keeps the plain name."""
    excluded = set(excluded)
    children: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        if f.get("trashed"):
            continue
        parents = f.get("parents") or []
        if parents:
            children[parents[0]].append(f)

    out: list[tuple[dict, str]] = []
    skipped = 0
    stack = [(root_id, "")]
    while stack:
        parent_id, prefix = stack.pop()
        kids = sorted(children.get(parent_id, []), key=lambda f: (f.get("createdTime", ""), f["id"]))
        taken = {TMP_DIRNAME} if prefix == "" else set()
        for f in kids:
            mime = f["mimeType"]
            if mime == SHORTCUT_MIME:
                skipped += 1
                continue
            if f["id"] in excluded:
                continue
            local = stub_name(f["name"], mime) if is_google_doc(mime) else to_local(f["name"])
            local = dedupe(local, taken)
            taken.add(local)
            rel = f"{prefix}/{local}" if prefix else local
            out.append((f, rel))
            if mime == FOLDER_MIME:
                stack.append((f["id"], rel))
    return out, skipped


# --------------------------------------------------------------------------- pull


def pull(
    client: DriveLike,
    db: StateDB,
    root: Path,
    *,
    root_id: str | None = None,
    excluded: Iterable[str] = (),
    max_workers: int = 4,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
    oplog=None,
    pair: str = "",
) -> PullResult:
    def rec(op: str, path: str, **detail) -> None:
        if oplog is not None and not dry_run:
            oplog.record(op, path, pair=pair, **detail)

    root = root.expanduser().resolve()
    recorded_root = db.get("sync_root")
    if recorded_root and recorded_root != str(root):
        raise PullError(
            f"State DB belongs to sync root {recorded_root!r}, not {str(root)!r}. "
            "Move the folder back, or delete the state DB to start over."
        )

    res = PullResult()

    # Grab the change-feed position *before* listing so nothing that changes
    # during the (possibly long) listing is missed later.
    page_token = client.start_page_token()
    drive_root = client.root_id()
    if root_id in (None, "root"):
        root_id = drive_root
    recorded_id = db.get("root_id")
    if recorded_id and recorded_id != root_id:
        raise PullError("State DB belongs to a different Drive folder.")
    if root_id == drive_root:
        log("Listing My Drive…")
        listing = client.list_all()
    else:
        log("Listing folder…")
        listing = list_subtree(client, root_id, excluded)
    entries, res.skipped = build_tree(listing, root_id, excluded)
    log(f"Found {len(entries)} items.")

    tmp = root / TMP_DIRNAME
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)  # leftovers from an interrupted run
        tmp.mkdir()
        db.set("sync_root", str(root))
        db.set("root_id", root_id)

    seen: set[str] = set()
    downloads: list[_Download] = []
    failed_dirs: set[str] = set()

    for f, rel in entries:
        seen.add(f["id"])
        if any(rel.startswith(d + "/") for d in failed_dirs):
            continue  # parent folder couldn't be created; already reported
        dest = root / rel
        mime = f["mimeType"]
        try:
            if mime == FOLDER_MIME:
                if dest.exists() and not dest.is_dir():
                    raise PullError("a file is in the way of this folder")
                if not dry_run:
                    if not dest.is_dir():
                        dest.mkdir()
                        rec("mkdir", rel)
                    db.upsert(make_item(f, rel, None))
                res.folders += 1
            elif is_google_doc(mime):
                if not dry_run and not dest.exists():
                    rec("write_link", rel, file_id=f["id"])
                _write_stub(f, dest, rel, db, dry_run)
                res.stubs += 1
            else:
                if f.get("capabilities", {}).get("canDownload") is False:
                    raise PullError("Drive does not allow downloading this file")
                job = _plan_file(f, rel, dest, root, db, res, dry_run, log, rec)
                if job:
                    downloads.append(job)
        except (OSError, PullError) as e:
            res.errors.append((rel, str(e)))
            if mime == FOLDER_MIME:
                failed_dirs.add(rel)

    # Disk space guard.
    need = sum(int(d.file.get("size", 0)) for d in downloads)
    if not dry_run and need:
        free = shutil.disk_usage(root).free
        if need > free:
            raise PullError(
                f"Not enough disk space: need {need / 1e9:.2f} GB, {free / 1e9:.2f} GB free."
            )

    if dry_run:
        for d in downloads:
            log(f"would download {d.rel}")
        res.downloaded = len(downloads)
        res.downloaded_bytes = need
        return res

    if downloads:
        log(f"Downloading {len(downloads)} files ({need / 1e6:.1f} MB)…")
        _run_downloads(client, db, root, tmp, downloads, res, max_workers, log, rec)

    # Forget DB rows for items that no longer exist remotely (local files are left alone).
    for item in db.all():
        if item.file_id not in seen:
            db.delete(item.file_id)
            res.forgotten += 1

    shutil.rmtree(tmp, ignore_errors=True)

    if not res.errors:
        db.page_token = page_token
    return res


def stub_content(f: dict) -> str:
    return json.dumps(
        {"url": f.get("webViewLink"), "id": f["id"], "mimeType": f["mimeType"]}, indent=2
    ) + "\n"


def read_stub_id(path: Path) -> str | None:
    """If path is one of our Google-Docs link files, return the file id it points at."""
    try:
        if path.stat().st_size > 4096:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("id"), str) and "url" in data:
        return data["id"]
    return None


def write_stub(f: dict, dest: Path) -> os.stat_result:
    """Write (or refresh) a Google-Docs link file; returns its stat."""
    content = stub_content(f)
    if not dest.exists() or dest.read_text(errors="replace") != content:
        dest.write_text(content)
    if f.get("modifiedTime"):
        ts = rfc3339_to_ts(f["modifiedTime"])
        os.utime(dest, (ts, ts))
    return dest.stat()


def download_file(client: DriveLike, f: dict, dest: Path, tmp: Path) -> os.stat_result:
    """Download f to dest atomically via tmp/, verifying MD5 and setting mtime."""
    tmp.mkdir(parents=True, exist_ok=True)
    part = tmp / f"{f['id']}.part"
    try:
        client.download(f["id"], part)
        expected = f.get("md5Checksum")
        if expected and md5_file(part) != expected:
            raise PullError("checksum mismatch after download")
        if f.get("modifiedTime"):
            ts = rfc3339_to_ts(f["modifiedTime"])
            os.utime(part, (ts, ts))
        os.replace(part, dest)
        return dest.stat()
    finally:
        part.unlink(missing_ok=True)


def _write_stub(f: dict, dest: Path, rel: str, db: StateDB, dry_run: bool) -> None:
    if dest.is_dir():
        raise PullError("a folder is in the way of this file")
    if dry_run:
        return
    db.upsert(make_item(f, rel, write_stub(f, dest)))


def _plan_file(
    f: dict,
    rel: str,
    dest: Path,
    root: Path,
    db: StateDB,
    res: PullResult,
    dry_run: bool,
    log: Callable[[str], None],
    rec: Callable[..., None] = lambda *a, **k: None,
) -> _Download | None:
    """Decide what to do for a binary file. Returns a download job or None."""
    prev = db.by_id(f["id"])
    remote_md5 = f.get("md5Checksum")

    if dest.exists() or dest.is_symlink():
        if dest.is_dir() or dest.is_symlink():
            raise PullError("a folder or symlink is in the way of this file")
        st = dest.stat()
        # Already in sync and untouched since.
        if prev and prev.rel_path == rel and prev.md5 == remote_md5 and unchanged_since(prev, st):
            res.unchanged += 1
            return None
        # Same content already present (e.g. copied in manually) — just adopt it.
        if remote_md5 and md5_file(dest) == remote_md5:
            if not dry_run:
                db.upsert(make_item(f, rel, st))
            res.adopted += 1
            return None
        # Remote changed and the local copy is the untouched previous version: overwrite.
        if prev and prev.rel_path == rel and unchanged_since(prev, st):
            return _Download(f, rel)
        # Local content differs and wasn't produced by us: keep it as a conflicted copy.
        backup = conflict_name(dest)
        log(f"conflict: {rel} -> {backup.name}")
        if not dry_run:
            dest.rename(backup)
            rec("conflict", str(backup.relative_to(root)), src=rel)
        res.conflicts.append(str(backup.relative_to(root)))
        return _Download(f, rel)

    # Remote rename/move of a file we already have: move it locally instead of re-downloading.
    if prev and prev.rel_path != rel and prev.md5 == remote_md5:
        old = root / prev.rel_path
        if old.is_file() and not old.is_symlink() and unchanged_since(prev, old.stat()):
            log(f"move: {prev.rel_path} -> {rel}")
            if not dry_run:
                old.rename(dest)
                rec("move_local", rel, src=prev.rel_path, reason="moved on Drive")
                db.upsert(make_item(f, rel, dest.stat()))
            res.moved += 1
            return None

    return _Download(f, rel)


def _run_downloads(
    client: DriveLike,
    db: StateDB,
    root: Path,
    tmp: Path,
    jobs: list[_Download],
    res: PullResult,
    max_workers: int,
    log: Callable[[str], None],
    rec: Callable[..., None] = lambda *a, **k: None,
) -> None:
    def work(job: _Download) -> os.stat_result:
        return download_file(client, job.file, root / job.rel, tmp)

    total = len(jobs)
    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {pool.submit(work, j): j for j in jobs}
        for n, fut in enumerate(as_completed(futures), 1):
            job = futures[fut]
            try:
                st = fut.result()
            except Exception as e:  # noqa: BLE001 — report per-file, keep going
                res.errors.append((job.rel, f"{type(e).__name__}: {e}"))
                log(f"[{n}/{total}] FAILED {job.rel}: {e}")
                continue
            db.upsert(make_item(job.file, job.rel, st))  # DB writes stay on this thread
            rec("download", job.rel, file_id=job.file["id"], size=st.st_size,
                md5=job.file.get("md5Checksum"))
            res.downloaded += 1
            res.downloaded_bytes += st.st_size
            log(f"[{n}/{total}] {job.rel}")
    except KeyboardInterrupt:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
