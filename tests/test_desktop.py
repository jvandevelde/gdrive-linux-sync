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

    cli.main(["install-desktop"])  # idempotent: no duplicate bookmark
    assert len(p["bookmarks"].read_text().splitlines()) == 1

    p["bookmarks"].write_text("file:///home/other Other\n" + p["bookmarks"].read_text())
    assert cli.main(["install-desktop", "--uninstall"]) == 0
    assert not p["extension"].exists() and not p["mime"].exists() and not p["desktop"].exists()
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
