import os
import shutil

import pytest
from fakedrive import FakeDrive

from gdrivesync import engine as engine_mod
from gdrivesync.drive import FOLDER_MIME
from gdrivesync.engine import SyncEngine, SyncError
from gdrivesync.pull import pull
from gdrivesync.state import StateDB

DOC = "application/vnd.google-apps.document"


@pytest.fixture
def env(tmp_path, monkeypatch):
    trashed = []

    def fake_trash(p):
        trashed.append(p.name)
        shutil.rmtree(p) if p.is_dir() else p.unlink()

    monkeypatch.setattr(engine_mod, "trash_local", fake_trash)

    drive = FakeDrive()
    db = StateDB(":memory:")
    root = tmp_path / "Drive"

    class Env:
        pass

    e = Env()
    e.drive, e.db, e.root, e.trashed = drive, db, root, trashed

    def start():
        pull(drive, db, root, log=lambda m: None)
        e.eng = SyncEngine(drive, db, root, settle_s=0)
        drive.downloads.clear()
        drive.uploads.clear()
        return e.eng

    def sync():
        r = e.eng.remote_pass()
        lp = e.eng.local_pass()
        if e.eng.dirty:
            e.eng.dirty = False
            lp2 = e.eng.local_pass()
            lp.uploaded += lp2.uploaded
        assert r.errors == [] and lp.errors == [], (r.errors, lp.errors)
        return r, lp

    e.start, e.sync = start, sync
    return e


def write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)  # synced files carry Drive's (2024) mtime, so this always differs


# ---------------------------------------------------------------- Drive → local


def test_remote_new_and_edit(env):
    env.start()
    f = env.drive.add("new.txt", data=b"one")
    env.sync()
    assert (env.root / "new.txt").read_bytes() == b"one"
    env.drive.set_content(env.drive.files[f["id"]], b"two")
    env.sync()
    assert (env.root / "new.txt").read_bytes() == b"two"
    assert env.drive.uploads == []  # no echo uploads


def test_remote_rename_folder_moves_locally_without_downloads(env):
    d = env.drive.add("Old", mime=FOLDER_MIME)
    env.drive.add("a.txt", parent=d["id"], data=b"a")
    env.start()
    env.drive.edit(env.drive.files[d["id"]], name="New")
    env.sync()
    assert (env.root / "New/a.txt").read_bytes() == b"a"
    assert not (env.root / "Old").exists()
    assert env.drive.downloads == [] and env.drive.uploads == []
    assert env.db.by_path("New/a.txt") is not None


def test_remote_trash_removes_local(env):
    f = env.drive.add("a.txt", data=b"a")
    env.start()
    env.drive.remote_trash(env.drive.files[f["id"]])
    env.sync()
    assert not (env.root / "a.txt").exists()
    assert env.trashed == ["a.txt"]


def test_remote_trash_folder_keeps_locally_edited_file(env):
    d = env.drive.add("D", mime=FOLDER_MIME)
    env.drive.add("keep.txt", parent=d["id"], data=b"x")
    env.drive.add("drop.txt", parent=d["id"], data=b"y")
    env.start()
    write(env.root / "D/keep.txt", b"edited locally")
    env.drive.remote_trash(env.drive.files[d["id"]])
    env.sync()
    assert not (env.root / "D/drop.txt").exists()
    assert (env.root / "D/keep.txt").read_bytes() == b"edited locally"
    # the edit wins: folder + file are recreated on Drive
    assert env.drive.live().get("D/keep.txt") == b"edited locally"


def test_remote_folder_appearing_with_contents(env):
    env.start()
    outside = env.drive.add("Moved in", parent=None, mime=FOLDER_MIME)
    env.drive.add("inner.txt", parent=outside["id"], data=b"in")
    env.drive.changelog.clear()  # pretend only the folder move shows up in the feed
    env.db.page_token = "0"
    env.drive.edit(env.drive.files[outside["id"]], parents=["root"])
    env.sync()
    assert (env.root / "Moved in/inner.txt").read_bytes() == b"in"


def test_remote_google_doc_rename(env):
    f = env.drive.add("Notes", mime=DOC)
    env.start()
    env.drive.edit(env.drive.files[f["id"]], name="Minutes")
    env.sync()
    assert (env.root / "Minutes.gdoc").exists() and not (env.root / "Notes.gdoc").exists()


# ---------------------------------------------------------------- local → Drive


def test_local_new_file_and_folder_uploaded_once(env):
    env.start()
    write(env.root / "Projects/plan.md", b"# plan")
    env.sync()
    assert env.drive.live()["Projects"] == "dir"
    assert env.drive.live()["Projects/plan.md"] == b"# plan"
    assert env.drive.uploads == ["plan.md"]
    env.sync()  # echo from change feed + rescan: nothing to do
    assert env.drive.uploads == ["plan.md"] and env.drive.downloads == []


def test_local_edit_uploads(env):
    f = env.drive.add("a.txt", data=b"v1")
    env.start()
    write(env.root / "a.txt", b"v2 local")
    env.sync()
    assert env.drive.content[f["id"]] == b"v2 local"
    env.sync()
    assert env.drive.downloads == []


def test_touch_without_change_does_not_upload(env):
    env.drive.add("a.txt", data=b"same")
    env.start()
    os.utime(env.root / "a.txt")
    env.sync()
    assert env.drive.uploads == []


def test_local_delete_trashes_remote(env):
    f = env.drive.add("a.txt", data=b"v1")
    env.start()
    (env.root / "a.txt").unlink()
    env.sync()
    assert env.drive.files[f["id"]]["trashed"]
    assert env.db.by_id(f["id"]) is None


def test_local_delete_loses_to_remote_edit(env):
    f = env.drive.add("a.txt", data=b"v1")
    env.start()
    (env.root / "a.txt").unlink()
    env.drive.set_content(env.drive.files[f["id"]], b"v2 remote")
    # local pass first, as if the deletion was noticed before the remote poll
    env.eng.local_pass()
    assert (env.root / "a.txt").read_bytes() == b"v2 remote"
    assert not env.drive.files[f["id"]]["trashed"]


def test_local_folder_delete_trashes_folder_only(env):
    d = env.drive.add("D", mime=FOLDER_MIME)
    env.drive.add("a.txt", parent=d["id"], data=b"a")
    env.start()
    shutil.rmtree(env.root / "D")
    env.sync()
    assert env.drive.files[d["id"]]["trashed"]
    assert env.db.all() == []


def test_local_rename_detected_by_scan(env):
    d = env.drive.add("Sub", mime=FOLDER_MIME)
    f = env.drive.add("a.txt", data=b"content")
    env.start()
    os.rename(env.root / "a.txt", env.root / "Sub/b.txt")
    env.sync()
    assert env.drive.files[f["id"]]["name"] == "b.txt"
    assert env.drive.files[f["id"]]["parents"] == [d["id"]]
    assert env.drive.uploads == [] and not env.drive.files[f["id"]]["trashed"]


def test_offline_folder_rename_moves_children_not_reuploads(env):
    d = env.drive.add("Old", mime=FOLDER_MIME)
    f = env.drive.add("a.txt", parent=d["id"], data=b"aaa")
    env.start()
    os.rename(env.root / "Old", env.root / "New")
    env.sync()
    assert env.drive.uploads == []
    assert env.drive.live()["New/a.txt"] == b"aaa"
    assert env.drive.files[f["id"]]["trashed"] is False


def test_watcher_folder_move_event(env):
    d = env.drive.add("Old", mime=FOLDER_MIME)
    env.drive.add("a.txt", parent=d["id"], data=b"a")
    env.start()
    os.rename(env.root / "Old", env.root / "New")
    env.eng.local_move_event("Old", "New")
    assert env.drive.files[d["id"]]["name"] == "New"
    assert env.db.by_path("New/a.txt") is not None
    env.sync()
    assert env.drive.uploads == [] and env.drive.downloads == []
    assert not env.drive.files[d["id"]]["trashed"]


def test_rename_google_doc_stub_renames_doc(env):
    f = env.drive.add("Notes", mime=DOC)
    env.start()
    os.rename(env.root / "Notes.gdoc", env.root / "Ideas.gdoc")
    env.eng.local_move_event("Notes.gdoc", "Ideas.gdoc")
    assert env.drive.files[f["id"]]["name"] == "Ideas"


def test_copied_stub_is_not_uploaded(env):
    env.drive.add("Notes", mime=DOC)
    env.start()
    shutil.copy(env.root / "Notes.gdoc", env.root / "Copy.gdoc")
    env.sync()
    assert env.drive.uploads == []


def test_ignored_files_not_uploaded(env):
    env.start()
    write(env.root / ".~lock.report.odt#", b"lock")
    write(env.root / "notes.txt.swp", b"swap")
    write(env.root / "draft~", b"backup")
    env.sync()
    assert env.drive.uploads == []


def test_unsettled_file_is_deferred(env):
    env.start()
    env.eng.settle_s = 3600
    write(env.root / "big.iso", b"still writing")
    stats = env.eng.local_pass()
    assert stats.deferred == 1 and env.drive.uploads == []


# ---------------------------------------------------------------- conflicts


def test_both_sides_edited_creates_conflicted_copy(env):
    f = env.drive.add("a.txt", data=b"base")
    env.start()
    write(env.root / "a.txt", b"local edit")
    env.drive.set_content(env.drive.files[f["id"]], b"remote edit")
    env.sync()
    assert (env.root / "a.txt").read_bytes() == b"remote edit"
    copies = [p for p in env.root.iterdir() if "conflicted copy" in p.name]
    assert len(copies) == 1 and copies[0].read_bytes() == b"local edit"
    # the conflicted copy is uploaded too
    assert copies[0].name in env.drive.uploads


def test_local_edit_wins_over_remote_delete(env):
    f = env.drive.add("a.txt", data=b"base")
    env.start()
    write(env.root / "a.txt", b"local edit")
    env.drive.remote_trash(env.drive.files[f["id"]])
    env.sync()
    assert (env.root / "a.txt").read_bytes() == b"local edit"
    assert env.drive.live()["a.txt"] == b"local edit"


def test_remote_rename_keeps_pending_local_edit(env):
    f = env.drive.add("a.txt", data=b"base")
    env.start()
    write(env.root / "a.txt", b"local edit")
    env.drive.edit(env.drive.files[f["id"]], name="b.txt")
    env.sync()
    assert (env.root / "b.txt").read_bytes() == b"local edit"
    assert env.drive.content[f["id"]] == b"local edit"


def test_untracked_local_file_in_the_way_of_remote_new(env):
    env.start()
    write(env.root / "a.txt", b"mine")
    env.drive.add("a.txt", data=b"theirs")
    env.eng.remote_pass()
    assert (env.root / "a.txt").read_bytes() == b"theirs"
    assert any("conflicted copy" in p.name for p in env.root.iterdir())


# ---------------------------------------------------------------- safety


def test_missing_root_refuses(env):
    env.drive.add("a.txt", data=b"a")
    env.start()
    shutil.rmtree(env.root)
    with pytest.raises(SyncError):
        env.eng.local_pass()
    assert not any(f["trashed"] for f in env.drive.files.values())


def test_mass_delete_guard(env):
    for i in range(5):
        env.drive.add(f"f{i}.txt", data=b"x")
    env.start()
    env.eng.mass_delete_limit = 3
    for i in range(5):
        (env.root / f"f{i}.txt").unlink()
    with pytest.raises(SyncError):
        env.eng.local_pass()
    assert not any(f["trashed"] for f in env.drive.files.values())
    env.eng.allow_mass_delete = True
    env.eng.local_pass()
    assert all(f["trashed"] for f in env.drive.files.values())


def test_failed_remote_item_is_retried(env):
    env.start()
    f = env.drive.add("a.txt", data=b"a")
    env.drive.corrupt.add(f["id"])
    stats = env.eng.remote_pass()
    assert len(stats.errors) == 1
    env.drive.corrupt.clear()
    env.eng.remote_pass()  # nothing new in the feed, but the retry list has it
    assert (env.root / "a.txt").read_bytes() == b"a"
