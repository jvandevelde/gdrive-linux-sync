import copy
import json
import os
import re
import shutil

import pytest
from fakedrive import FakeDrive

from gdrivesync import cli, config
from gdrivesync import engine as engine_mod
from gdrivesync.drive import FOLDER_MIME
from gdrivesync.engine import SyncEngine
from gdrivesync.oplog import OpLog, describe, read_entries
from gdrivesync.pull import pull
from gdrivesync.state import StateDB

DOC = "application/vnd.google-apps.document"
WRITES = {"create_folder", "upload_new", "update_content", "update_metadata", "trash",
          "download", "generate_id"}


class ReadOnly:
    """Fails the test if a dry run tries to change Drive."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        if name in WRITES:
            raise AssertionError(f"dry run called {name}()")
        return getattr(self.inner, name)


@pytest.fixture(autouse=True)
def no_real_trash(monkeypatch):
    monkeypatch.setattr(
        engine_mod, "trash_local", lambda p: shutil.rmtree(p) if p.is_dir() else p.unlink()
    )


def build_scenario(tmp_path):
    """A synced tree, then a mix of changes on both sides."""
    drive = FakeDrive()
    docs = drive.add("Docs", mime=FOLDER_MIME)
    old = drive.add("Old", mime=FOLDER_MIME)
    drive.add("in-old.txt", parent=old["id"], data=b"o")
    edit_r = drive.add("edit-remote.txt", parent=docs["id"], data=b"1")
    drive.add("edit-local.txt", parent=docs["id"], data=b"1")
    gone_r = drive.add("delete-remote.txt", data=b"x")
    drive.add("delete-local.txt", data=b"y")
    drive.add("rename-local.txt", data=b"rename me")
    both = drive.add("both.txt", data=b"base")
    drive.add("Notes", mime=DOC)
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "Drive"
    pull(drive, db, root, log=lambda m: None)

    # Drive side
    drive.add("new-remote.txt", parent=docs["id"], data=b"new")
    drive.set_content(drive.files[edit_r["id"]], b"2 remote")
    drive.edit(drive.files[old["id"]], name="Renamed")
    drive.remote_trash(drive.files[gone_r["id"]])
    drive.set_content(drive.files[both["id"]], b"remote side")
    # local side
    (root / "New folder").mkdir()
    (root / "New folder/new-local.txt").write_bytes(b"local new")
    (root / "Docs/edit-local.txt").write_bytes(b"2 local")
    (root / "delete-local.txt").unlink()
    os.rename(root / "rename-local.txt", root / "Docs/renamed-local.txt")
    (root / "both.txt").write_bytes(b"local side")
    return drive, db, root


def snapshot_fs(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for d in dirs:
            out[os.path.relpath(os.path.join(dirpath, d), root)] = "dir"
        for f in files:
            p = os.path.join(dirpath, f)
            out[os.path.relpath(p, root)] = (open(p, "rb").read(), os.stat(p).st_mtime_ns)
    return out


def run(engine):
    engine.remote_pass()
    engine.local_pass()
    if engine.dirty:
        engine.dirty = False
        engine.local_pass()


def normalize(entries):
    def n(x):
        return re.sub(r"conflicted copy [0-9 -]+", "conflicted copy X", x) if x else x
    return sorted((e["op"], n(e["path"]), n(e.get("src"))) for e in entries)


def test_dry_run_changes_nothing_and_predicts_the_real_run(tmp_path):
    drive, db, root = build_scenario(tmp_path)
    fs_before = snapshot_fs(root)
    drive_before = (copy.deepcopy(drive.files), dict(drive.content), list(drive.changelog))
    db_before = [vars(i) for i in db.all()], db.page_token

    printed = []
    dry_log = OpLog(dry_run=True, echo=printed.append)
    dry = SyncEngine(ReadOnly(drive), db.snapshot(), root, settle_s=0, oplog=dry_log, dry_run=True)
    run(dry)

    assert snapshot_fs(root) == fs_before
    assert (drive.files, drive.content, drive.changelog) == drive_before
    assert ([vars(i) for i in db.all()], db.page_token) == db_before
    assert printed and all(line.startswith("would ") for line in printed)

    log_file = tmp_path / "ops.log"
    real = SyncEngine(drive, db, root, settle_s=0, oplog=OpLog(log_file))
    run(real)
    real_entries = list(read_entries(log_file))

    assert normalize(dry_log.recent) == normalize(real_entries)
    ops = {(e["op"], e["path"]) for e in real_entries}
    assert ("download", "Docs/new-remote.txt") in ops
    assert ("download", "Docs/edit-remote.txt") in ops
    assert ("move_local", "Renamed") in ops
    assert ("trash_local", "delete-remote.txt") in ops
    assert ("create_folder", "New folder") in ops
    assert ("upload", "New folder/new-local.txt") in ops
    assert ("update", "Docs/edit-local.txt") in ops
    assert ("trash_remote", "delete-local.txt") in ops
    assert ("move_remote", "Docs/renamed-local.txt") in ops
    assert any(op == "conflict" for op, _ in ops)


def test_dry_run_of_exclusion(tmp_path):
    drive = FakeDrive()
    v = drive.add("Videos", mime=FOLDER_MIME)
    drive.add("a.mp4", parent=v["id"], data=b"a")
    db = StateDB(":memory:")
    root = tmp_path / "Drive"
    pull(drive, db, root, log=lambda m: None)
    log = OpLog(dry_run=True, echo=lambda m: None)
    eng = SyncEngine(ReadOnly(drive), db.snapshot(), root, excluded=[v["id"]],
                     settle_s=0, oplog=log, dry_run=True)
    run(eng)
    assert [(e["op"], e["path"]) for e in log.recent] == [("delete_local", "Videos")]
    assert (root / "Videos/a.mp4").exists()


def test_log_records_are_json_lines(tmp_path):
    path = tmp_path / "state/operations.log"
    log = OpLog(path)
    log.record("upload", "a/b.txt", pair="Work", file_id="x", size=1500)
    log.record("trash_remote", "c.txt", pair="Work", reason="deleted locally")
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["op"] == "upload" and first["size"] == 1500 and "ts" in first
    assert describe(first) == "[Work] upload new file a/b.txt (1.5 KB)"
    assert describe(json.loads(lines[1])) == "[Work] trash on Drive c.txt — deleted locally"


def test_pull_logs_downloads(tmp_path):
    drive = FakeDrive()
    drive.add("a.txt", data=b"a")
    drive.add("Notes", mime=DOC)
    log = OpLog(tmp_path / "ops.log")
    pull(drive, StateDB(":memory:"), tmp_path / "Drive", log=lambda m: None, oplog=log, pair="P")
    assert sorted((e["op"], e["path"]) for e in read_entries(tmp_path / "ops.log")) == [
        ("download", "a.txt"), ("write_link", "Notes.gdoc"),
    ]


def test_log_command(tmp_path, monkeypatch, capsys):
    path = tmp_path / "operations.log"
    monkeypatch.setattr(config, "OPS_LOG", path)
    log = OpLog(path)
    for i in range(5):
        log.record("download", f"f{i}.txt", pair="A" if i % 2 else "B", size=10)
    assert cli.main(["log", "-n", "2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and out[-1].endswith("[B] download f4.txt (10 B)")
    assert cli.main(["log", "--pair", "A", "-n", "0", "--json"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["path"] for r in rows] == ["f1.txt", "f3.txt"]


def test_cli_sync_dry_run(tmp_path, monkeypatch, capsys):
    for name, value in {
        "CONFIG_DIR": tmp_path / "cfg", "DATA_DIR": tmp_path / "data",
        "SETTINGS_FILE": tmp_path / "cfg/settings.json", "STATE_DB": tmp_path / "data/state.db",
        "OPS_LOG": tmp_path / "state/operations.log", "IGNORE_FILE": tmp_path / "cfg/ignore",
    }.items():
        monkeypatch.setattr(config, name, value)
    drive = FakeDrive()
    drive.add("a.txt", data=b"a")
    monkeypatch.setattr(cli, "_client", lambda: ReadOnly(drive))

    root = tmp_path / "Drive"
    s = config.Settings(pairs=[config.Pair("All", str(root), "/", "root")])
    s.save()

    # never synced: shows the first download
    assert cli.main(["sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would download a.txt" in out and not root.exists()

    # synced: shows two-way changes
    db = StateDB(s.pairs[0].db_path)
    pull(drive, db, root, log=lambda m: None)
    db.close()
    drive.add("b.txt", data=b"b")
    (root / "c.txt").write_bytes(b"c")
    assert cli.main(["sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would download b.txt" in out
    assert "would upload new file c.txt" in out
    assert "2 operation(s) would be performed" in out
    assert not (root / "b.txt").exists()
    assert not config.OPS_LOG.exists()


def test_dry_run_full_check_is_read_only(tmp_path):
    drive = FakeDrive()
    f = drive.add("a.txt", data=b"v1")
    db = StateDB(":memory:")
    root = tmp_path / "Drive"
    pull(drive, db, root, log=lambda m: None)
    drive.silently(drive.files[f["id"]], data=b"missed")
    log = OpLog(dry_run=True, echo=lambda m: None)
    eng = SyncEngine(ReadOnly(drive), db.snapshot(), root, settle_s=0, oplog=log, dry_run=True)
    eng.full_check()
    eng.local_pass()
    assert [(e["op"], e["path"]) for e in log.recent] == [("download", "a.txt")]
    assert (root / "a.txt").read_bytes() == b"v1"
    assert db.get("last_full_check") is None
