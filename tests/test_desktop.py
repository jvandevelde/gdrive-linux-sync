import json

import pytest
from fakedrive import FakeDrive

from gdrivesync import cli, config, desktop_install
from gdrivesync.pull import pull
from gdrivesync.state import StateDB


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdgcfg"))
    for name, value in {
        "CONFIG_DIR": tmp_path / "cfg", "DATA_DIR": tmp_path / "data",
        "SETTINGS_FILE": tmp_path / "cfg/settings.json", "STATE_DB": tmp_path / "data/state.db",
    }.items():
        monkeypatch.setattr(config, name, value)
    root = tmp_path / "Google Drive"
    root.mkdir()
    s = config.Settings(pairs=[config.Pair("My Drive", str(root), "/", "root")])
    s.save()
    return tmp_path, s, root


def test_install_and_uninstall(home, capsys):
    tmp, settings, root = home
    assert cli.main(["install-desktop"]) == 0
    p = desktop_install.paths()
    ext = p["extension"].read_text()
    assert "class GDriveSyncExtension" in ext
    assert p["mime"].read_text().count("<glob pattern=") == 9
    entry = p["desktop"].read_text()
    assert " open %f" in entry and "application/x-gdrivesync-gdoc;" in entry
    marks = p["bookmarks"].read_text().splitlines()
    assert marks == [f"{root.resolve().as_uri()} My Drive"]
    assert "%20" in marks[0]  # space in folder name is URI-encoded
    out = capsys.readouterr().out
    assert "nautilus -q" in out
    assert " settings" in p["app"].read_text() and "Name=Google Drive Sync" in p["app"].read_text()
    assert " tray" in p["autostart"].read_text()

    assert cli.main(["config", "--json"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert cfg["pairs"][0]["name"] == "My Drive" and cfg["desktop"]["extension_installed"]
    assert cfg["desktop"]["tray_autostart"] is True

    cli.main(["install-desktop"])  # idempotent: no duplicate bookmark
    assert len(p["bookmarks"].read_text().splitlines()) == 1

    p["bookmarks"].write_text("file:///home/other Other\n" + p["bookmarks"].read_text())
    assert cli.main(["install-desktop", "--uninstall"]) == 0
    assert not p["extension"].exists() and not p["mime"].exists() and not p["desktop"].exists()
    assert not p["app"].exists() and not p["autostart"].exists()
    assert p["bookmarks"].read_text() == "file:///home/other Other\n"  # others' bookmarks kept


def test_open_links(home, capsys):
    tmp, settings, root = home
    drive = FakeDrive()
    drive.add("Notes", mime="application/vnd.google-apps.document")
    f = drive.add("a.pdf", data=b"x")
    db = StateDB(settings.pairs[0].db_path)
    pull(drive, db, root, log=lambda m: None)
    db.close()

    assert cli.main(["open", "--print", str(root / "Notes.gdoc")]) == 0
    assert capsys.readouterr().out.strip() == json.loads((root / "Notes.gdoc").read_text())["url"]
    assert cli.main(["open", "--print", str(root / "a.pdf")]) == 0
    assert capsys.readouterr().out.strip() == f"https://drive.google.com/file/d/{f['id']}/view"
    (root / "local.txt").write_text("x")
    assert cli.main(["open", "--print", str(root / "local.txt")]) == 1
    assert cli.main(["open", "--print", "/etc/hostname"]) == 1


def test_journal_command(monkeypatch):
    seen = []
    monkeypatch.setattr("os.execvp", lambda exe, argv: seen.append(argv))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.delenv("GDRIVESYNC_JOURNALCTL", raising=False)
    assert cli.main(["journal", "-f", "--errors", "-n", "20"]) == 0
    assert seen == [["journalctl", "--user", "--unit", "gdrivesync.service", "--no-pager",
                     "--lines", "20", "--follow", "--priority", "warning"]]


def test_service_logs_carry_journal_priorities(monkeypatch):
    import io
    import logging

    monkeypatch.setenv("JOURNAL_STREAM", "8:1234")
    cli._setup_logging(False)
    buf = io.StringIO()
    logging.getLogger().handlers[0].setStream(buf)
    logging.getLogger("gdrivesync.engine").warning("careful")
    logging.getLogger("gdrivesync.engine").info("fine")
    try:
        raise ValueError("bad")
    except ValueError:
        logging.getLogger("gdrivesync.daemon").exception("failed")
    lines = buf.getvalue().splitlines()
    assert lines[0] == "<4>gdrivesync.engine: careful"
    assert lines[1] == "<6>gdrivesync.engine: fine"
    assert lines[2] == "<3>gdrivesync.daemon: failed"
    assert all(line.startswith("<3>") for line in lines[3:])  # traceback lines too
    monkeypatch.delenv("JOURNAL_STREAM")
    cli._setup_logging(False)  # back to timestamped output for terminals


def test_log_json_has_text(tmp_path, monkeypatch, capsys):
    from gdrivesync.oplog import OpLog

    monkeypatch.setattr(config, "OPS_LOG", tmp_path / "ops.log")
    OpLog(tmp_path / "ops.log").record("upload", "a.txt", pair="P", size=10)
    assert cli.main(["log", "--json"]) == 0
    entry = json.loads(capsys.readouterr().out.strip())
    assert entry["text"] == "upload new file a.txt (10 B)" and entry["pair"] == "P"
