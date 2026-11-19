"""In-memory stand-in for DriveClient, with a change feed."""

import copy
import hashlib
import threading
from pathlib import Path

from gdrivesync.drive import FOLDER_MIME, GOOGLE_APPS_PREFIX, InvalidPageToken


class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.content: dict[str, bytes] = {}
        self.changelog: list[str] = []
        self.downloads: list[str] = []
        self.uploads: list[str] = []
        self.corrupt: set[str] = set()
        # Names whose next create "succeeds on Drive" but then raises, like a
        # timeout after the server committed or a crash before we record it.
        self.crash_after_create: set[str] = set()
        self._gen = 0
        self.min_token = 0  # positions older than this are rejected
        self.feed_calls = 0
        self._n = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ test helpers
    def _touch(self, fid):
        self.changelog.append(fid)

    def add(self, name, parent="root", mime="text/plain", data=b"", **extra):
        with self._lock:
            self._n += 1
            n = self._n
        fid = extra.pop("id", f"id{n}")
        f = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": [parent] if parent else [],
            "createdTime": f"2024-01-01T00:00:{n % 60:02d}.{n:03d}Z",
            "modifiedTime": "2024-06-01T12:00:00.000Z",
            "webViewLink": f"https://drive.google.com/file/d/{fid}",
            "trashed": False,
            **extra,
        }
        self.files[fid] = f
        if mime != FOLDER_MIME and not mime.startswith(GOOGLE_APPS_PREFIX):
            self.set_content(f, data)
        self._touch(fid)
        return f

    def set_content(self, f, data):
        f["md5Checksum"] = hashlib.md5(data).hexdigest()
        f["size"] = str(len(data))
        f["headRevisionId"] = f"rev-{len(self.changelog)}"
        f["modifiedTime"] = "2024-07-01T12:00:00.000Z"
        self.content[f["id"]] = data
        self._touch(f["id"])

    def edit(self, f, **fields):
        f.update(fields)
        self._touch(f["id"])

    def remote_trash(self, f):
        self.edit(f, trashed=True)

    def path_of(self, fid):
        parts = []
        f = self.files[fid]
        while f["parents"] and f["parents"][0] != "root":
            parts.append(f["name"])
            f = self.files[f["parents"][0]]
        parts.append(f["name"])
        return "/".join(reversed(parts))

    def live(self):
        """{path: content-or-'dir'} for every non-trashed item reachable from root."""
        out = {}
        for fid, f in self.files.items():
            g, ok = f, True
            while True:
                if g["trashed"]:
                    ok = False
                    break
                p = (g.get("parents") or [None])[0]
                if p == "root":
                    break
                if p is None or p not in self.files:
                    ok = False
                    break
                g = self.files[p]
            if ok:
                out[self.path_of(fid)] = "dir" if f["mimeType"] == FOLDER_MIME else self.content.get(fid)
        return out

    # ------------------------------------------------------------ DriveClient API
    def root_id(self):
        return "root"

    def start_page_token(self):
        return str(len(self.changelog))

    def reject_old_positions(self):
        """Make every position handed out so far invalid."""
        self.min_token = len(self.changelog)

    def silently(self, f, data=None, **fields):
        """Change a file without it showing up in the change feed (a 'missed' change)."""
        n = len(self.changelog)
        if data is not None:
            self.set_content(f, data)
        f.update(fields)
        del self.changelog[n:]

    def changes_since(self, token):
        self.feed_calls += 1
        if not str(token).isdigit() or int(token) < self.min_token:
            raise InvalidPageToken(f"rejected {token!r}")
        ids = self.changelog[int(token):]
        changes = []
        for fid in dict.fromkeys(ids):
            f = self.files.get(fid)
            changes.append(
                {"fileId": fid, "removed": f is None, "file": copy.deepcopy(f) if f else None}
            )
        return changes, str(len(self.changelog))

    def list_all(self):
        return [copy.deepcopy(f) for f in self.files.values() if not f["trashed"]]

    def list_children(self, folder_id):
        return [
            copy.deepcopy(f) for f in self.files.values()
            if not f["trashed"] and f["parents"][:1] == [folder_id]
        ]

    def find_children(self, parent_id, name):
        return [f for f in self.list_children(parent_id) if f["name"] == name]

    def get_file(self, fid):
        f = self.files.get(fid)
        return copy.deepcopy(f) if f else None

    def download(self, fid, dest: Path):
        self.downloads.append(fid)
        dest.write_bytes(b"garbage" if fid in self.corrupt else self.content[fid])

    def generate_id(self):
        self._gen += 1
        return f"gen{self._gen}"

    def _create(self, name, file_id, make):
        # Same contract as DriveClient._create_idempotent: an id that already
        # exists means an earlier attempt succeeded, so return that file.
        if file_id and file_id in self.files:
            return copy.deepcopy(self.files[file_id])
        f = make()
        if name in self.crash_after_create:
            self.crash_after_create.discard(name)
            raise ConnectionError("simulated crash after Drive committed the create")
        return copy.deepcopy(f)

    def create_folder(self, name, parent_id, file_id=None):
        extra = {"id": file_id} if file_id else {}
        return self._create(name, file_id, lambda: self.add(name, parent_id, FOLDER_MIME, **extra))

    def upload_new(self, path, name, parent_id, mtime=None, file_id=None):
        extra = {"id": file_id} if file_id else {}

        def make():
            self.uploads.append(name)
            return self.add(name, parent_id, data=Path(path).read_bytes(), **extra)

        return self._create(name, file_id, make)

    def update_content(self, fid, path, mtime=None):
        f = self.files[fid]
        self.uploads.append(f["name"])
        self.set_content(f, Path(path).read_bytes())
        return copy.deepcopy(f)

    def update_metadata(self, fid, *, name=None, add_parent=None, remove_parent=None):
        f = self.files[fid]
        if name:
            f["name"] = name
        if add_parent:
            f["parents"] = [add_parent]
        self._touch(fid)
        return copy.deepcopy(f)

    def trash(self, fid):
        if fid in self.files:
            self.remote_trash(self.files[fid])
