import hashlib
import json
import os
from pathlib import Path

import pytest

from gdrivesync.drive import FOLDER_MIME, SHORTCUT_MIME
from gdrivesync.pull import PullError, pull
from gdrivesync.state import StateDB

DOC = "application/vnd.google-apps.document"


class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.content: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.corrupt: set[str] = set()
        self._n = 0

    def add(self, name, parent="root", mime="text/plain", data=b"", **extra):
        self._n += 1
        fid = extra.pop("id", f"id{self._n}")
        f = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "parents": [parent] if parent else [],
            "createdTime": f"2024-01-01T00:00:{self._n:02d}.000Z",
            "modifiedTime": "2024-06-01T12:00:00.000Z",
            "webViewLink": f"https://drive.google.com/file/d/{fid}",
            **extra,
        }
        if mime != FOLDER_MIME and not mime.startswith("application/vnd.google-apps."):
            self.set_content(f, data)
        self.files[fid] = f
        return f

    def set_content(self, f, data):
        f["md5Checksum"] = hashlib.md5(data).hexdigest()
        f["size"] = str(len(data))
        self.content[f["id"]] = data

    # DriveLike
    def root_id(self):
        return "root"

    def start_page_token(self):
        return "tok42"

    def list_all(self):
        return list(self.files.values())

    def download(self, file_id, dest: Path):
        self.downloads.append(file_id)
        data = self.content[file_id]
        dest.write_bytes(b"garbage" if file_id in self.corrupt else data)


@pytest.fixture
def env(tmp_path):
    drive = FakeDrive()
    db = StateDB(":memory:")
    root = tmp_path / "Drive"

    def run(**kw):
        return pull(drive, db, root, log=lambda m: None, **kw)

    return drive, db, root, run


def test_initial_pull_builds_tree(env):
    drive, db, root, run = env
    docs = drive.add("Docs", mime=FOLDER_MIME)
    drive.add("a.txt", parent=docs["id"], data=b"hello")
    drive.add("a.txt", parent=docs["id"], data=b"second")  # duplicate name
    drive.add("Q1/Q2", data=b"slash")
    drive.add("Notes", mime=DOC)
    drive.add("link", mime=SHORTCUT_MIME)
    drive.add("shared-with-me.txt", parent=None, data=b"x")
    drive.add("deleted.txt", data=b"x", trashed=True)
    drive.add(".gdrivesync-tmp", data=b"name clash with our tmp dir")

    res = run()

    assert res.errors == []
    assert (root / "Docs/a.txt").read_bytes() == b"hello"
    assert (root / "Docs/a (2).txt").read_bytes() == b"second"
    assert (root / "Q1∕Q2").read_bytes() == b"slash"
    assert (root / ".gdrivesync-tmp (2)").exists()
    stub = json.loads((root / "Notes.gdoc").read_text())
    assert stub["url"].startswith("https://drive.google.com/")
    assert not (root / "shared-with-me.txt").exists()
    assert not (root / "deleted.txt").exists()
    assert not (root / ".gdrivesync-tmp").exists()  # cleaned up after run
    assert res.skipped == 1 and res.folders == 1 and res.stubs == 1
    assert res.downloaded == 4
    assert db.page_token == "tok42"
    # mtime follows Drive's modifiedTime
    assert int(os.stat(root / "Docs/a.txt").st_mtime) == 1717243200


def test_rerun_is_noop(env):
    drive, db, root, run = env
    drive.add("a.txt", data=b"hello")
    run()
    drive.downloads.clear()
    res = run()
    assert drive.downloads == []
    assert res.unchanged == 1 and res.downloaded == 0


def test_remote_update_overwrites_untouched_local(env):
    drive, db, root, run = env
    f = drive.add("a.txt", data=b"v1")
    run()
    drive.set_content(f, b"v2")
    res = run()
    assert (root / "a.txt").read_bytes() == b"v2"
    assert res.conflicts == []


def test_local_edit_becomes_conflicted_copy(env):
    drive, db, root, run = env
    f = drive.add("a.txt", data=b"v1")
    run()
    (root / "a.txt").write_bytes(b"my local edit")
    drive.set_content(f, b"v2")
    res = run()
    assert (root / "a.txt").read_bytes() == b"v2"
    assert len(res.conflicts) == 1
    assert "conflicted copy" in res.conflicts[0]
    assert (root / res.conflicts[0]).read_bytes() == b"my local edit"


def test_preexisting_identical_file_is_adopted(env):
    drive, db, root, run = env
    drive.add("a.txt", data=b"same")
    root.mkdir()
    (root / "a.txt").write_bytes(b"same")
    res = run()
    assert res.adopted == 1 and drive.downloads == []


def test_remote_rename_moves_without_download(env):
    drive, db, root, run = env
    sub = drive.add("Sub", mime=FOLDER_MIME)
    f = drive.add("a.txt", data=b"hello")
    run()
    drive.downloads.clear()
    f["name"] = "b.txt"
    f["parents"] = [sub["id"]]
    res = run()
    assert res.moved == 1 and drive.downloads == []
    assert (root / "Sub/b.txt").read_bytes() == b"hello"
    assert not (root / "a.txt").exists()
    assert db.by_id(f["id"]).rel_path == "Sub/b.txt"


def test_checksum_mismatch_reports_error_and_keeps_token_unset(env):
    drive, db, root, run = env
    f = drive.add("a.txt", data=b"hello")
    drive.add("ok.txt", data=b"fine")
    drive.corrupt.add(f["id"])
    res = run()
    assert [p for p, _ in res.errors] == ["a.txt"]
    assert not (root / "a.txt").exists()
    assert (root / "ok.txt").exists()
    assert db.page_token is None
    # retry succeeds
    drive.corrupt.clear()
    res = run()
    assert res.errors == [] and db.page_token == "tok42"


def test_removed_remote_items_are_forgotten_but_kept_locally(env):
    drive, db, root, run = env
    f = drive.add("a.txt", data=b"hello")
    run()
    del drive.files[f["id"]]
    res = run()
    assert res.forgotten == 1
    assert (root / "a.txt").exists()
    assert db.by_id(f["id"]) is None


def test_excluded_folder_skipped(env):
    drive, db, root, run = env
    big = drive.add("Big", mime=FOLDER_MIME)
    drive.add("huge.bin", parent=big["id"], data=b"x" * 10)
    res = run(excluded=[big["id"]])
    assert not (root / "Big").exists() and res.downloaded == 0


def test_dry_run_touches_nothing(env):
    drive, db, root, run = env
    drive.add("a.txt", data=b"hello")
    res = run(dry_run=True)
    assert res.downloaded == 1 and not root.exists() and db.all() == []


def test_refuses_different_root(env, tmp_path):
    drive, db, root, run = env
    run()
    with pytest.raises(PullError):
        pull(drive, db, tmp_path / "Elsewhere", log=lambda m: None)
