"""Losing or corrupting the sync state: detection, quarantine and rebuild."""

import os
import shutil
import sqlite3
import time

import pytest
from fakedrive import FakeDrive

from gdrivesync import cli, config
from gdrivesync import engine as engine_mod
from gdrivesync import pull as pull_mod
from gdrivesync.daemon import Daemon
from gdrivesync.drive import FOLDER_MIME
from gdrivesync.engine import SyncEngine
from gdrivesync.pull import pull
from gdrivesync.state import PairBusy, PairLock, StateDB, is_corruption

DOC = "application/vnd.google-apps.document"


@pytest.fixture(autouse=True)
def fake_trash(monkeypatch):
    trashed = []

    def trash(p):
        trashed.append(p.name)
        shutil.rmtree(p) if p.is_dir() else p.unlink()

    monkeypatch.setattr(engine_mod, "trash_local", trash)
    monkeypatch.setattr(pull_mod, "trash_local", trash)
    return trashed


# ---------------------------------------------------------------- corruption detection


def test_open_checked_quarantines_damaged_files(tmp_path):
    good = tmp_path / "good.db"
    db = StateDB(good)
    db.page_token = "7"
    db.close()
    db, moved = StateDB.open_checked(good)
    assert moved is None and db.page_token == "7"
    db.close()

    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not sqlite at all" * 100)
    db, moved = StateDB.open_checked(junk)
    assert moved.name.startswith("junk.db.corrupt-") and moved.exists()
    assert db.page_token is None  # fresh → triggers a rebuild

    paged = tmp_path / "paged.db"
    db = StateDB(paged)
    for i in range(2000):
        db.set(f"k{i}", "v" * 100)
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    raw = bytearray(paged.read_bytes())
    raw[4096 * 3: 4096 * 3 + 500] = b"\xff" * 500
    paged.write_bytes(bytes(raw))
    db, moved = StateDB.open_checked(paged)
    assert moved is not None and db.page_token is None


def test_locked_is_not_corruption():
    assert is_corruption(sqlite3.DatabaseError("database disk image is malformed"))
    assert is_corruption(sqlite3.DatabaseError("file is not a database"))
    assert not is_corruption(sqlite3.OperationalError("database is locked"))
    assert not is_corruption(ValueError("malformed"))


def test_pair_lock(tmp_path):
    a = PairLock(tmp_path / "p.db").acquire()
    with pytest.raises(PairBusy):
        PairLock(tmp_path / "p.db").acquire()
    a.release()
    with PairLock(tmp_path / "p.db"):
        pass


# ---------------------------------------------------------------- rebuild scenario


def synced_then_state_lost(tmp_path):
    """Sync a tree, change things on both sides while 'the DB is gone'."""
    drive = FakeDrive()
    f = {}
    f["same"] = drive.add("same.txt", data=b"unchanged")
    f["local_newer"] = drive.add("local-newer.txt", data=b"v1")
    f["remote_newer"] = drive.add("remote-newer.txt", data=b"v1")
    f["gone"] = drive.add("deleted-on-drive.txt", data=b"bye")
    f["purged"] = drive.add("purged-on-drive.txt", data=b"long gone")
    f["dir"] = drive.add("Old project", mime=FOLDER_MIME)
    drive.add("a.txt", parent=f["dir"]["id"], data=b"a")
    drive.add("b.txt", parent=f["dir"]["id"], data=b"b")
    f["doc"] = drive.add("Removed doc", mime=DOC)
    root = tmp_path / "Drive"
    db = StateDB(tmp_path / "state.db")
    pull(drive, db, root, log=lambda m: None)
    db.close()
    (tmp_path / "state.db").unlink()  # the sync state is lost

    os.utime(root / "remote-newer.txt", (time.time() - 3600,) * 2)  # older than Drive's edit
    drive.set_content(drive.files[f["remote_newer"]["id"]], b"v2 from phone")
    drive.files[f["remote_newer"]["id"]]["modifiedTime"] = "2099-01-01T00:00:00.000Z"
    (root / "local-newer.txt").write_bytes(b"v2 from laptop")  # mtime: now
    drive.remote_trash(drive.files[f["gone"]["id"]])
    del drive.files[f["purged"]["id"]]  # trash already emptied
    drive.remote_trash(drive.files[f["dir"]["id"]])
    drive.remote_trash(drive.files[f["doc"]["id"]])
    (root / "new-local.txt").write_bytes(b"made offline")
    drive.downloads.clear()
    return drive, root, f


def rebuild(drive, tmp_path, root, **kw):
    db = StateDB(tmp_path / "rebuilt.db")
    res = pull(drive, db, root, log=lambda m: None, **kw)
    return db, res


def test_rebuild_keep_both(tmp_path, fake_trash):
    drive, root, f = synced_then_state_lost(tmp_path)
    db, res = rebuild(drive, tmp_path, root)

    assert res.errors == []
    assert f["same"]["id"] not in drive.downloads  # adopted by checksum
    assert res.adopted == 1
    # both differing files kept as conflicted copies, Drive's version downloaded
    assert sorted(c.split(" (")[0] for c in res.conflicts) == ["local-newer", "remote-newer"]
    assert (root / "remote-newer.txt").read_bytes() == b"v2 from phone"
    # deleted on Drive (in its trash) → moved to local trash, not re-uploaded
    assert sorted(res.deleted_on_drive) == ["Old project", "Old project/a.txt",
                                            "Old project/b.txt", "deleted-on-drive.txt"]
    assert not (root / "Old project").exists() and not (root / "deleted-on-drive.txt").exists()
    assert res.orphan_links == ["Removed doc.gdoc"]
    # can't tell these apart from new files → listed, kept, uploaded later
    assert "new-local.txt" in res.local_only and "purged-on-drive.txt" in res.local_only


def test_rebuild_prefer_newer_then_sync(tmp_path):
    drive, root, f = synced_then_state_lost(tmp_path)
    db, res = rebuild(drive, tmp_path, root, prefer_newer=True)

    assert res.newer_local == ["local-newer.txt"]
    assert [c.split(" (")[0] for c in res.conflicts] == ["remote-newer"]  # Drive was newer
    assert f["local_newer"]["id"] not in drive.downloads

    eng = SyncEngine(drive, db, root, settle_s=0)
    eng.remote_pass()
    eng.local_pass()
    live = drive.live()
    assert live["local-newer.txt"] == b"v2 from laptop"  # uploaded as a new version
    assert drive.files[f["local_newer"]["id"]]["name"] == "local-newer.txt"  # same Drive file
    assert live["new-local.txt"] == b"made offline"
    assert "deleted-on-drive.txt" not in live and "Old project" not in live
    before = list(drive.uploads)
    eng.remote_pass()
    eng.local_pass()
    assert drive.uploads == before  # settled


def test_rebuild_dry_run_changes_nothing(tmp_path):
    drive, root, f = synced_then_state_lost(tmp_path)

    def snapshot():
        return sorted((str(p.relative_to(root)), p.is_dir() or p.read_bytes())
                      for p in root.rglob("*"))

    before = snapshot()
    res = pull(drive, StateDB(":memory:"), root, dry_run=True, prefer_newer=True,
               log=lambda m: None)
    assert snapshot() == before
    assert res.newer_local == ["local-newer.txt"]
    assert "deleted-on-drive.txt" in res.deleted_on_drive
    assert res.orphan_links == ["Removed doc.gdoc"]


def test_normal_repull_does_not_touch_local_only_files(tmp_path):
    drive = FakeDrive()
    gone = drive.add("x.txt", data=b"x")
    db = StateDB(":memory:")
    root = tmp_path / "Drive"
    pull(drive, db, root, log=lambda m: None)
    drive.remote_trash(drive.files[gone["id"]])
    res = pull(drive, db, root, log=lambda m: None)  # has sync history: no reconciliation
    assert res.deleted_on_drive == [] and (root / "x.txt").exists()


# ---------------------------------------------------------------- runtime corruption


def test_daemon_rebuilds_pair_on_runtime_corruption(tmp_path):
    drive = FakeDrive()
    db = StateDB(":memory:")
    pull(drive, db, tmp_path / "D", log=lambda m: None)
    eng = SyncEngine(drive, db, tmp_path / "D", settle_s=0, name="D")

    def boom():
        raise sqlite3.DatabaseError("database disk image is malformed")

    eng.local_pass = boom
    replacement = SyncEngine(drive, StateDB(":memory:"), tmp_path / "D", settle_s=0, name="D")
    calls = []
    d = Daemon(eng, watch=False, rebuild=lambda e: calls.append(e) or replacement)
    d._local(d.slots[0], time.monotonic())
    assert calls == [eng] and d.slots[0].engine is replacement and not d.slots[0].disabled

    eng2 = SyncEngine(drive, StateDB(":memory:"), tmp_path / "D", settle_s=0, name="D")
    eng2.local_pass = boom
    d2 = Daemon(eng2, watch=False)  # no rebuild available → pause that pair, keep running
    d2._local(d2.slots[0], time.monotonic())
    assert d2.slots[0].disabled and d2._active() == []


# ---------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    for name, value in {
        "CONFIG_DIR": tmp_path / "cfg", "DATA_DIR": tmp_path / "data",
        "SETTINGS_FILE": tmp_path / "cfg/settings.json", "STATE_DB": tmp_path / "data/state.db",
        "OPS_LOG": tmp_path / "state/operations.log", "IGNORE_FILE": tmp_path / "cfg/ignore",
    }.items():
        monkeypatch.setattr(config, name, value)
    drive = FakeDrive()
    drive.add("a.txt", data=b"a")
    monkeypatch.setattr(cli, "_client", lambda: drive)
    root = tmp_path / "Drive"
    s = config.Settings(pairs=[config.Pair("All", str(root), "/", "root")])
    s.save()
    assert cli.main(["pull", "-q"]) == 0
    return drive, s.pairs[0], root


def test_cli_pair_rebuild(cli_env, capsys):
    drive, pair, root = cli_env
    drive.downloads.clear()
    assert cli.main(["pair", "rebuild", "All", "--dry-run"]) == 0
    assert "Nothing was changed" in capsys.readouterr().out

    assert cli.main(["pair", "rebuild", "All"]) == 0
    out = capsys.readouterr().out
    assert "Previous sync state kept as" in out
    assert list(config.DATA_DIR.glob("pair-all.db.replaced-*"))
    assert drive.downloads == []  # rebuilt by checksum
    db = StateDB(pair.db_path)
    assert db.page_token is not None and db.by_path("a.txt") is not None


def test_cli_refuses_while_service_holds_the_pair(cli_env):
    drive, pair, root = cli_env
    lock = PairLock(pair.db_path).acquire()
    try:
        assert cli.main(["pair", "rebuild", "All"]) == 1
        assert cli.main(["sync"]) == 1
        assert cli.main(["sync", "--dry-run"]) == 0  # read-only: allowed
    finally:
        lock.release()


def test_corrupt_db_detected_and_rebuilt_on_start(cli_env, capsys):
    drive, pair, root = cli_env
    pair.db_path.write_bytes(b"garbage" * 500)
    drive.downloads.clear()
    engine = cli._build_engine(config.Settings.load(), pair, drive)
    assert engine.db.page_token is not None
    assert list(config.DATA_DIR.glob("pair-all.db.corrupt-*"))
    assert drive.downloads == []
    assert "corrupted" in capsys.readouterr().err
