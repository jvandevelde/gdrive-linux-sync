"""The GUIs' shared logic (pure Python, runs on any interpreter)."""

import sys
from pathlib import Path

import gdrivesync

sys.path.insert(0, str(Path(gdrivesync.__file__).parent / "desktop"))
import ui_common as ui  # noqa: E402


def state(paused=False, errors=None, active=None, recent=None, stopped=False):
    return {"paused": paused, "recent": recent or [], "pairs": [{
        "name": "Main", "local": "/d", "remote": "/", "paused": stopped,
        "errors": errors or {}, "active": active or [],
    }]}


def test_summary_states():
    assert ui.summarize(None).state == "offline"
    assert ui.summarize(state()).label == "Up to date"
    assert ui.summarize(state(active=["a", "b"])).label == "Syncing 2 items…"
    assert ui.summarize(state(errors={"x": "boom"})).label == "1 item couldn't sync"
    assert ui.summarize(state(stopped=True)).label == "Main stopped syncing"
    paused = ui.summarize(state(paused=True, errors={"x": "boom"}))
    assert paused.state == "paused" and paused.icon == "media-playback-pause-symbolic"
    assert ui.summarize(state(errors={"x": "e"}, active=["y"])).lines == \
        ["Main: 1 error, syncing 1"]


def test_notifications():
    t = ui.NotificationTracker()
    conflict = {"ts": "t1", "op": "conflict", "pair": "Main", "path": "a (conflicted copy).txt",
                "src": "a.txt"}
    # first snapshot is the baseline: nothing old is announced
    assert t.update(state(recent=[conflict], errors={"old": "e"})) == []
    assert t.update(state(recent=[conflict], errors={"old": "e"})) == []
    new_conflict = dict(conflict, ts="t2", path="b (conflicted copy).txt", src="b.txt")
    out = t.update(state(recent=[conflict, new_conflict],
                         errors={"old": "e", "dir/new.bin": "quota"}))
    assert [o[0] for o in out] == ["Edited in two places", "Couldn't sync “new.bin”"]
    assert "b.txt" in out[0][1] and out[1][1] == "quota"
    many = {f"f{i}": "x" for i in range(5)}
    out = t.update(state(errors=many))
    assert out == [("Couldn't sync 5 items", "f0, f1, f2 and 2 more")]
    assert t.update(None) == [("Google Drive sync stopped",
                               "The sync service isn't running; changes aren't being synced.")]
    assert t.update(None) == []  # only once
    assert t.update(state(errors=many)) == []  # back online: no noise


def test_commands_from_env(monkeypatch):
    monkeypatch.setenv("GDRIVESYNC_CLI", "/opt/venv/bin/gdrivesync")
    monkeypatch.setenv("GDRIVESYNC_SYSTEMCTL", "systemctl")
    assert ui.cli_command() == ["/opt/venv/bin/gdrivesync"]
    assert ui.systemctl_command() == ["systemctl", "--user"]
    assert "Exec=/opt/venv/bin/gdrivesync tray" in ui.autostart_entry()
    monkeypatch.setenv("XDG_CONFIG_HOME", "/x")
    assert ui.autostart_path() == "/x/autostart/gdrivesync-tray.desktop"
