"""Crash/retry safety of creates on Drive, and the duplicate doctor."""

import shutil
from unittest import mock

import httplib2
import pytest
from fakedrive import FakeDrive
from googleapiclient.errors import HttpError

from gdrivesync import engine as engine_mod
from gdrivesync.doctor import find_duplicates, fix_duplicates
from gdrivesync.drive import FOLDER_MIME, DriveClient
from gdrivesync.engine import SyncEngine
from gdrivesync.pull import pull
from gdrivesync.state import StateDB


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine_mod, "trash_local", lambda p: shutil.rmtree(p) if p.is_dir() else p.unlink()
    )
    drive = FakeDrive()
    db = StateDB(":memory:")
    root = tmp_path / "Drive"
    pull(drive, db, root, log=lambda m: None)
    eng = SyncEngine(drive, db, root, settle_s=0)
    return drive, db, root, eng


def names_on_drive(drive):
    return sorted(p for p in drive.live())


def test_crash_after_create_does_not_duplicate(env):
    drive, db, root, eng = env
    (root / "a.txt").write_bytes(b"hello")
    drive.crash_after_create.add("a.txt")
    stats = eng.local_pass()
    assert len(stats.errors) == 1  # "crashed" after Drive created it
    assert db.pending("a.txt") is not None

    stats = eng.local_pass()  # next run resumes with the same id
    assert stats.errors == []
    assert names_on_drive(drive) == ["a.txt"]
    assert drive.uploads == ["a.txt"]  # content sent once
    assert db.by_path("a.txt") is not None and db.all_pending() == []

    eng.remote_pass()  # the create echoing back in the change feed is a no-op
    assert sorted(p.name for p in root.iterdir() if p.name != ".gdrivesync-tmp") == ["a.txt"]
    assert drive.downloads == []


def test_crash_then_edit_brings_drive_up_to_date(env):
    drive, db, root, eng = env
    (root / "a.txt").write_bytes(b"v1")
    drive.crash_after_create.add("a.txt")
    eng.local_pass()
    (root / "a.txt").write_bytes(b"v2 after restart")
    eng.local_pass()
    assert names_on_drive(drive) == ["a.txt"]
    assert drive.live()["a.txt"] == b"v2 after restart"


def test_crash_then_local_delete_trashes_orphan(env):
    drive, db, root, eng = env
    (root / "a.txt").write_bytes(b"hello")
    drive.crash_after_create.add("a.txt")
    eng.local_pass()
    (root / "a.txt").unlink()
    eng.local_pass()
    assert names_on_drive(drive) == []
    assert db.all_pending() == []


def test_crash_then_local_rename_moves_not_duplicates(env):
    drive, db, root, eng = env
    (root / "a.txt").write_bytes(b"hello")
    drive.crash_after_create.add("a.txt")
    eng.local_pass()
    (root / "a.txt").rename(root / "b.txt")
    eng.local_pass()
    assert names_on_drive(drive) == ["b.txt"]


def test_crash_during_folder_create(env):
    drive, db, root, eng = env
    (root / "Docs").mkdir()
    (root / "Docs/x.txt").write_bytes(b"x")
    drive.crash_after_create.add("Docs")
    eng.local_pass()
    eng.local_pass()
    assert names_on_drive(drive) == ["Docs", "Docs/x.txt"]


def test_crash_after_record_before_journal_cleanup(env):
    drive, db, root, eng = env
    (root / "a.txt").write_bytes(b"hello")
    with mock.patch.object(StateDB, "remove_pending", side_effect=RuntimeError("crash")):
        eng.local_pass()
    assert db.all_pending()
    eng.local_pass()
    assert db.all_pending() == [] and names_on_drive(drive) == ["a.txt"]


# ---------------------------------------------------------------- DriveClient contract


def _http_error(status):
    return HttpError(httplib2.Response({"status": status}), b"{}")


def test_drive_client_create_returns_existing_on_conflict():
    client = DriveClient.__new__(DriveClient)
    existing = {"id": "gen1", "name": "a.txt"}
    client.get_file = mock.Mock(return_value=existing)

    def do():
        raise _http_error(409)

    assert client._create_idempotent("gen1", do) == existing


def test_drive_client_create_reraises_when_nothing_created():
    client = DriveClient.__new__(DriveClient)
    client.get_file = mock.Mock(return_value=None)

    def do():
        raise _http_error(400)

    with pytest.raises(HttpError):
        client._create_idempotent("gen1", do)


# ---------------------------------------------------------------- doctor


def test_doctor_finds_and_removes_legacy_duplicates(tmp_path, monkeypatch):
    import gdrivesync.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "trash_local", lambda p: shutil.rmtree(p) if p.is_dir() else p.unlink())
    drive = FakeDrive()
    drive.add("report.pdf", data=b"same")
    drive.add("report.pdf", data=b"same")  # duplicate from an old interrupted upload
    drive.add("report.pdf", data=b"different")  # same name, different content: keep
    drive.add("Photos", mime=FOLDER_MIME)
    empty = drive.add("Photos", mime=FOLDER_MIME)
    d = drive.add("Photos", mime=FOLDER_MIME)
    drive.add("pic.jpg", parent=d["id"], data=b"jpg")
    db = StateDB(":memory:")
    root = tmp_path / "Drive"
    pull(drive, db, root, log=lambda m: None)

    groups = find_duplicates(db)
    extras = sorted(i.rel_path for g in groups for i in g.extras)
    assert extras == ["Photos", "Photos (2)", "report (2).pdf"]
    kept_folder = next(g.keep for g in groups if g.keep.is_folder)
    assert kept_folder.file_id == d["id"]  # the non-empty one survives

    assert fix_duplicates(drive, db, root, groups) == 3
    alive = sorted(
        (f["name"], drive.content.get(f["id"])) for f in drive.files.values() if not f["trashed"]
    )
    assert alive == [("Photos", None), ("pic.jpg", b"jpg"),
                     ("report.pdf", b"different"), ("report.pdf", b"same")]
    assert drive.files[empty["id"]]["trashed"]
    assert find_duplicates(db) == []
