import json
import shutil
from pathlib import Path

import pytest
from fakedrive import FakeDrive

from gdrivesync import config
from gdrivesync import engine as engine_mod
from gdrivesync.config import Pair, Settings
from gdrivesync.daemon import Daemon
from gdrivesync.drive import FOLDER_MIME
from gdrivesync.engine import SyncEngine, SyncError
from gdrivesync.pairs import (
    PairError,
    exclude_path,
    include_path,
    resolve_remote_path,
    validate_new_pair,
)
from gdrivesync.pull import pull
from gdrivesync.state import StateDB


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "cfg/settings.json")
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "data/state.db")
    monkeypatch.setattr(
        engine_mod, "trash_local", lambda p: shutil.rmtree(p) if p.is_dir() else p.unlink()
    )


# ---------------------------------------------------------------- settings


def test_legacy_settings_migrate_to_one_pair(tmp_path):
    config.CONFIG_DIR.mkdir()
    config.SETTINGS_FILE.write_text(json.dumps(
        {"sync_root": "/x/GD", "poll_interval_s": 30, "excluded_folder_ids": ["abc"]}
    ))
    s = Settings.load()
    assert s.poll_interval_s == 30
    assert len(s.pairs) == 1
    p = s.pairs[0]
    assert (p.local, p.remote_id, p.excluded_folder_ids, p.db) == ("/x/GD", "root", ["abc"], "state.db")
    s.save()
    assert Settings.load().pairs == s.pairs


def test_legacy_db_without_settings_file_migrates():
    config.DATA_DIR.mkdir()
    config.STATE_DB.write_bytes(b"")
    assert [p.remote_path for p in Settings.load().pairs] == ["/"]


def test_fresh_install_has_no_pairs():
    assert Settings.load().pairs == []


# ---------------------------------------------------------------- validation


@pytest.fixture
def drive():
    d = FakeDrive()
    work = d.add("Work", mime=FOLDER_MIME)
    d.add("Reports", parent=work["id"], mime=FOLDER_MIME)
    d.add("Photos", mime=FOLDER_MIME)
    return d


def test_resolve_remote_path(drive):
    assert resolve_remote_path(drive, "/") == "root"
    reports = resolve_remote_path(drive, "Work/Reports/")
    assert drive.files[reports]["name"] == "Reports"
    with pytest.raises(PairError, match="No folder"):
        resolve_remote_path(drive, "/Nope")
    new = resolve_remote_path(drive, "/New/Deep", create=True)
    assert drive.path_of(new) == "New/Deep"
    drive.add("Photos", mime=FOLDER_MIME)
    with pytest.raises(PairError, match="ambiguous"):
        resolve_remote_path(drive, "/Photos")


def _pair(drive, name, local, path):
    rid = "root" if path == "/" else resolve_remote_path(drive, path)
    return Pair(name=name, local=str(local), remote_path=path, remote_id=rid)


def test_validate_pairs(drive, tmp_path):
    s = Settings()
    work = _pair(drive, "Work", tmp_path / "Work", "/Work")
    validate_new_pair(drive, s, work)
    s.pairs.append(work)

    validate_new_pair(drive, s, _pair(drive, "Photos", tmp_path / "Photos", "/Photos"))
    with pytest.raises(PairError, match="overlaps pair"):  # nested on Drive
        validate_new_pair(drive, s, _pair(drive, "Rep", tmp_path / "Rep", "/Work/Reports"))
    with pytest.raises(PairError, match="overlaps pair"):  # all of My Drive contains /Work
        validate_new_pair(drive, s, _pair(drive, "All", tmp_path / "All", "/"))
    with pytest.raises(PairError, match="overlaps the local folder"):
        validate_new_pair(drive, s, _pair(drive, "P", tmp_path / "Work/sub", "/Photos"))
    with pytest.raises(PairError, match="already exists"):
        validate_new_pair(drive, s, _pair(drive, "Work", tmp_path / "Other", "/Photos"))
    with pytest.raises(PairError, match="dedicated folder"):
        validate_new_pair(drive, s, _pair(drive, "Home", Path.home(), "/Photos"))
    with pytest.raises(PairError, match="gdrivesync's own files"):
        validate_new_pair(drive, s, _pair(drive, "Data", config.DATA_DIR, "/Photos"))


# ---------------------------------------------------------------- subfolder pairs


def make_engine(drive, tmp_path, name, remote_id, excluded=()):
    db = StateDB(":memory:")
    root = tmp_path / name
    pull(drive, db, root, root_id=remote_id, excluded=excluded, log=lambda m: None)
    return SyncEngine(drive, db, root, remote_root_id=remote_id, excluded=excluded,
                      settle_s=0, name=name)


def test_subfolder_pair_syncs_only_its_folder(drive, tmp_path):
    work_id = resolve_remote_path(drive, "/Work")
    drive.add("w.txt", parent=work_id, data=b"w")
    drive.add("outside.txt", data=b"o")
    eng = make_engine(drive, tmp_path, "Work", work_id)
    assert sorted(p.name for p in eng.root.iterdir()) == ["Reports", "w.txt"]

    drive.add("more-outside.txt", data=b"x")
    (eng.root / "local.txt").write_bytes(b"up")
    eng.remote_pass()
    eng.local_pass()
    assert not (eng.root / "more-outside.txt").exists()
    assert drive.live()["Work/local.txt"] == b"up"


def test_move_between_pairs(drive, tmp_path):
    work_id = resolve_remote_path(drive, "/Work")
    photos_id = resolve_remote_path(drive, "/Photos")
    f = drive.add("pic.jpg", parent=work_id, data=b"jpg")
    work = make_engine(drive, tmp_path, "Work", work_id)
    drive.add("unrelated.txt", parent=None, data=b"")  # pairs now start at different positions
    photos = make_engine(drive, tmp_path, "Photos", photos_id)

    drive.edit(drive.files[f["id"]], parents=[photos_id])
    d = Daemon([work, photos], watch=False)
    calls = []
    real = drive.changes_since
    drive.changes_since = lambda t: calls.append(t) or real(t)
    assert d.poll_remote()

    assert not (work.root / "pic.jpg").exists()
    assert (photos.root / "pic.jpg").read_bytes() == b"jpg"
    assert len(calls) == 2  # different start positions → one call each
    calls.clear()
    assert d.poll_remote()
    assert len(calls) == 1  # now in step: one shared feed call


def test_pair_root_trashed_is_not_mirrored(drive, tmp_path):
    work_id = resolve_remote_path(drive, "/Work")
    drive.add("keep.txt", parent=work_id, data=b"k")
    eng = make_engine(drive, tmp_path, "Work", work_id)
    drive.remote_trash(drive.files[work_id])
    with pytest.raises(SyncError, match="deleted or trashed"):
        eng.remote_pass()
    assert (eng.root / "keep.txt").exists()


# ---------------------------------------------------------------- exclusions


@pytest.fixture
def media(tmp_path):
    drive = FakeDrive()
    videos = drive.add("Videos", mime=FOLDER_MIME)
    drive.add("big.mp4", parent=videos["id"], data=b"v" * 100)
    drive.add("doc.txt", data=b"d")
    eng = make_engine(drive, tmp_path, "All", "root")
    return drive, eng, videos


def sync(eng):
    eng.remote_pass()
    eng.local_pass()


def test_exclude_after_sync_removes_local_copy_only(media):
    drive, eng, videos = media
    eng.excluded.add(videos["id"])
    sync(eng)
    assert not (eng.root / "Videos").exists()
    assert eng.db.by_id(videos["id"]) is None
    assert not drive.files[videos["id"]]["trashed"]  # still on Drive
    assert drive.live()["Videos/big.mp4"] == b"v" * 100

    # remote changes inside it are ignored; a local folder with that name isn't uploaded
    drive.add("new.mp4", parent=videos["id"], data=b"n")
    (eng.root / "Videos").mkdir()
    (eng.root / "Videos/local.mp4").write_bytes(b"l")
    sync(eng)
    assert not (eng.root / "Videos/new.mp4").exists()
    assert "Videos/local.mp4" not in drive.live()
    assert [f["name"] for f in drive.files.values() if f["name"] == "Videos"] == ["Videos"]


def test_exclude_waits_for_pending_local_changes(media):
    drive, eng, videos = media
    (eng.root / "Videos/edited.mp4").write_bytes(b"new local file")
    eng.excluded.add(videos["id"])
    eng.local_pass()  # can't drop yet (unsynced file) — but it gets uploaded
    assert drive.live()["Videos/edited.mp4"] == b"new local file"
    eng.local_pass()  # now everything inside is on Drive
    assert not (eng.root / "Videos").exists()


def test_reinclude_downloads_again(media):
    drive, eng, videos = media
    eng.excluded.add(videos["id"])
    sync(eng)
    drive.edit(drive.files[videos["id"]], name="Movies")  # renamed while excluded
    sync(eng)
    eng.excluded.discard(videos["id"])
    sync(eng)
    assert (eng.root / "Movies/big.mp4").read_bytes() == b"v" * 100


def test_exclude_include_cli_helpers(media):
    drive, eng, videos = media
    s = Settings(pairs=[Pair("All", str(eng.root), "/", "root", db="all.db")])
    # point the pair's DB at the engine's in-memory state by copying it to disk
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    disk = StateDB(s.pairs[0].db_path)
    for it in eng.db.all():
        disk.upsert(it)
    disk.close()

    with pytest.raises(PairError, match="Only folders"):
        exclude_path(s, eng.root / "doc.txt")
    with pytest.raises(PairError, match="isn't inside"):
        exclude_path(s, eng.root.parent / "elsewhere")
    exclude_path(s, eng.root / "Videos")
    assert s.pairs[0].excluded_folder_ids == [videos["id"]]
    include_path(s, eng.root / "Videos")  # before the local copy was dropped
    assert s.pairs[0].excluded_folder_ids == []
