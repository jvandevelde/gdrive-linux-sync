"""Detection of and recovery from a rejected change-feed position, and the
periodic full comparison with Drive that catches changes the feed missed."""

import shutil
from unittest import mock

import httplib2
import pytest
from fakedrive import FakeDrive
from googleapiclient.errors import HttpError

from gdrivesync import engine as engine_mod
from gdrivesync.daemon import Daemon
from gdrivesync.drive import FOLDER_MIME, DriveClient, InvalidPageToken
from gdrivesync.engine import FULL_CHECK_KEY, SyncEngine, SyncError
from gdrivesync.pull import pull
from gdrivesync.state import StateDB


@pytest.fixture(autouse=True)
def no_real_trash(monkeypatch):
    monkeypatch.setattr(
        engine_mod, "trash_local", lambda p: shutil.rmtree(p) if p.is_dir() else p.unlink()
    )


@pytest.fixture
def env(tmp_path):
    drive = FakeDrive()
    docs = drive.add("Docs", mime=FOLDER_MIME)
    f = {
        "keep": drive.add("keep.txt", parent=docs["id"], data=b"same"),
        "edit": drive.add("edit.txt", parent=docs["id"], data=b"v1"),
        "gone": drive.add("gone.txt", data=b"x"),
        "local": drive.add("local-edit.txt", data=b"base"),
        "both": drive.add("both.txt", data=b"base"),
        "deleted_here": drive.add("deleted-here.txt", data=b"d"),
    }
    db = StateDB(":memory:")
    root = tmp_path / "Drive"
    pull(drive, db, root, log=lambda m: None)
    eng = SyncEngine(drive, db, root, settle_s=0)
    drive.downloads.clear()
    return drive, eng, root, f, docs


def make_changes(drive, root, f, docs):
    drive.add("new.txt", parent=docs["id"], data=b"new")
    drive.set_content(drive.files[f["edit"]["id"]], b"v2")
    drive.remote_trash(drive.files[f["gone"]["id"]])
    drive.set_content(drive.files[f["both"]["id"]], b"remote")
    (root / "local-edit.txt").write_bytes(b"local change")
    (root / "both.txt").write_bytes(b"local")
    (root / "deleted-here.txt").unlink()


def assert_converged(drive, root):
    assert (root / "Docs/new.txt").read_bytes() == b"new"
    assert (root / "Docs/edit.txt").read_bytes() == b"v2"
    assert not (root / "gone.txt").exists()
    assert (root / "both.txt").read_bytes() == b"remote"
    assert any("conflicted copy" in p.name for p in root.iterdir())
    live = drive.live()
    assert live["local-edit.txt"] == b"local change"  # local edit kept and uploaded
    assert "deleted-here.txt" not in live  # local deletion not undone


def test_rejected_position_detected_and_recovered(env):
    drive, eng, root, f, docs = env
    make_changes(drive, root, f, docs)
    drive.reject_old_positions()

    eng.remote_pass()  # detects the rejection and does a full comparison instead
    assert int(eng.db.page_token) >= drive.min_token  # fresh, valid position
    eng.local_pass()
    if eng.dirty:
        eng.local_pass()
    assert_converged(drive, root)
    # unchanged files weren't downloaded again
    assert f["keep"]["id"] not in drive.downloads

    eng.remote_pass()  # back to normal incremental polling
    assert eng.db.get(engine_mod.FEED_RESET_KEY)


def test_repeated_rejection_does_not_loop(env):
    drive, eng, root, f, docs = env
    drive.add("first.txt", data=b"1")
    drive.reject_old_positions()
    eng.remote_pass()
    assert eng.db.get(engine_mod.FEED_RESET_KEY)  # first rejection: recovered
    drive.add("another.txt", data=b"a")
    drive.reject_old_positions()  # rejected again right away
    calls = drive.feed_calls
    with pytest.raises(SyncError, match="keeps rejecting"):
        eng.remote_pass()
    assert drive.feed_calls == calls + 1  # no re-listing storm


def test_full_check_catches_changes_the_feed_missed(env):
    drive, eng, root, f, docs = env
    drive.silently(drive.files[f["edit"]["id"]], data=b"missed edit")
    drive.silently(drive.files[f["gone"]["id"]], trashed=True)
    drive.silently(drive.files[f["keep"]["id"]], name="renamed.txt")

    stats = eng.remote_pass()
    assert not stats.changed  # the feed has nothing

    stats = eng.full_check()
    assert stats.changed
    assert (root / "Docs/edit.txt").read_bytes() == b"missed edit"
    assert not (root / "gone.txt").exists()
    assert (root / "Docs/renamed.txt").read_bytes() == b"same"
    assert eng.db.get(FULL_CHECK_KEY)
    assert drive.downloads == [f["edit"]["id"]]  # only what actually changed


def test_full_check_when_in_sync_changes_nothing(env):
    drive, eng, root, f, docs = env
    stats = eng.full_check()
    assert not stats.changed and drive.downloads == [] and drive.uploads == []


def test_full_check_of_subfolder_pair_whose_folder_was_trashed(tmp_path):
    drive = FakeDrive()
    work = drive.add("Work", mime=FOLDER_MIME)
    drive.add("a.txt", parent=work["id"], data=b"a")
    db = StateDB(":memory:")
    root = tmp_path / "Work"
    pull(drive, db, root, root_id=work["id"], log=lambda m: None)
    eng = SyncEngine(drive, db, root, remote_root_id=work["id"], settle_s=0)
    drive.silently(drive.files[work["id"]], trashed=True)
    with pytest.raises(SyncError, match="deleted or trashed"):
        eng.full_check()
    assert (root / "a.txt").exists()


def test_daemon_recovers_rejected_position_for_all_pairs(tmp_path):
    drive = FakeDrive()
    engines = []
    for name in ("A", "B"):
        folder = drive.add(name, mime=FOLDER_MIME)
        db = StateDB(":memory:")
        pull(drive, db, tmp_path / name, root_id=folder["id"], log=lambda m: None)
        engines.append(SyncEngine(drive, db, tmp_path / name, remote_root_id=folder["id"],
                                  settle_s=0, name=name))
    a_id = engines[0].root_id
    drive.add("new.txt", parent=a_id, data=b"n")
    drive.reject_old_positions()
    d = Daemon(engines, watch=False)
    assert d.poll_remote()
    assert (tmp_path / "A/new.txt").exists()
    assert all(int(e.db.page_token) >= drive.min_token for e in engines)


def test_daemon_periodic_full_check(tmp_path):
    drive = FakeDrive()
    f = drive.add("a.txt", data=b"v1")
    db = StateDB(":memory:")
    pull(drive, db, tmp_path / "D", log=lambda m: None)
    eng = SyncEngine(drive, db, tmp_path / "D", settle_s=0)
    d = Daemon(eng, watch=False, full_check_s=3600)

    d.full_checks_due()  # first time only starts the clock
    drive.silently(drive.files[f["id"]], data=b"missed")
    d.full_checks_due()
    assert (tmp_path / "D/a.txt").read_bytes() == b"v1"  # not due yet

    eng.db.set(FULL_CHECK_KEY, "1")  # pretend the last check was long ago
    d.full_checks_due()
    assert (tmp_path / "D/a.txt").read_bytes() == b"missed"


def test_drive_client_maps_token_errors():
    client = DriveClient.__new__(DriveClient)
    svc = mock.Mock()
    svc.changes.return_value.list.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": 410}), b"{}"
    )
    client._local = mock.Mock(svc=svc)
    with pytest.raises(InvalidPageToken):
        client.changes_since("123")
    svc.changes.return_value.list.return_value.execute.side_effect = HttpError(
        httplib2.Response({"status": 403}), b"{}"
    )
    with pytest.raises(HttpError):
        client.changes_since("123")
