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


def test_parse_journal_lines():
    import json

    line = json.dumps({"__REALTIME_TIMESTAMP": "1763600000000000", "PRIORITY": "4",
                       "MESSAGE": "gdrivesync.daemon: [Main] full check found differences"})
    e = ui.parse_journal_line(line)
    assert (e.level, e.problem, e.ts) == ("warning", True, 1763600000.0)
    assert e.text == "[Main] full check found differences"  # module prefix dropped
    err = ui.parse_journal_line(json.dumps({"PRIORITY": "3", "MESSAGE": "boom"}))
    assert err.level == "error" and err.problem
    binary = ui.parse_journal_line(json.dumps({"PRIORITY": "6", "MESSAGE": [99, 97, 102, 233]}))
    assert binary.text == "caf�" and not binary.problem
    legacy = ui.parse_journal_line(json.dumps(
        {"PRIORITY": "6", "MESSAGE": "2026-01-01 10:00:00 ERROR gdrivesync.engine: x"}))
    assert legacy.level == "error"
    plain = ui.parse_journal_line("-- No entries --")
    assert plain.text == "-- No entries --" and plain.level == "info"
    assert ui.parse_journal_line("   ") is None


def test_parse_ops_lines():
    import json

    e = ui.parse_ops_line(json.dumps({"ts": "2026-11-20T09:05:00+00:00", "pair": "Work",
                                      "op": "conflict", "text": "keep local a as b"}))
    assert e.text == "[Work] Keep local a as b" and e.level == "warning" and e.problem
    up = ui.parse_ops_line(json.dumps({"ts": "2026-11-20T09:05:00+00:00", "op": "upload",
                                       "path": "x", "text": "upload new file x"}))
    assert not up.problem and up.text == "Upload new file x"
    assert ui.parse_ops_line("not json") is None


def test_log_commands(monkeypatch):
    monkeypatch.setenv("GDRIVESYNC_JOURNALCTL", "journalctl")
    monkeypatch.setenv("GDRIVESYNC_CLI", "gdrivesync")
    j = ui.journal_argv()
    assert j[:4] == ["journalctl", "--user", "--unit", "gdrivesync.service"]
    assert "--follow" in j and j[-2:] == ["--output", "json"]
    assert ui.ops_argv() == ["gdrivesync", "log", "--follow", "--json", "-n", "500"]
