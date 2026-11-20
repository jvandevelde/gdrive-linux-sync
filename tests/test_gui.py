"""Runs the real tray icon (GTK 3) and settings window (GTK 4 + libadwaita) on
the system Python under Xvfb, against the real sync service on a private
session bus. The gdrivesync CLI and systemctl are recording fakes, and a fake
notification server captures desktop notifications."""

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from jeepney import MessageType, new_method_return
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection

import gdrivesync
from gdrivesync.desktop_install import system_python

HERE = Path(__file__).parent
DESKTOP = Path(gdrivesync.__file__).parent / "desktop"
PY = system_python() or "/usr/bin/python3"  # the one `gdrivesync tray/settings` would use


def _has(code):
    return os.path.exists(PY) and subprocess.run([PY, "-c", code], capture_output=True).returncode == 0


HAVE_GTK4 = _has("import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1');"
                 "from gi.repository import Gtk, Adw")
HAVE_TRAY = _has("import gi; gi.require_version('Gtk','3.0');"
                 "gi.require_version('AyatanaAppIndicator3','0.1');"
                 "from gi.repository import Gtk, AyatanaAppIndicator3")
pytestmark = [
    pytest.mark.skipif(shutil.which("Xvfb") is None, reason="needs Xvfb"),
    pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="needs dbus-daemon"),
]


@pytest.fixture
def display():
    r, w = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
        pass_fds=(w,), stderr=subprocess.DEVNULL,
    )
    os.close(w)
    num = b""
    while not num.endswith(b"\n"):
        chunk = os.read(r, 16)
        if not chunk:
            break
        num += chunk
    os.close(r)
    yield f":{num.decode().strip()}"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def notifications(session_bus):
    """A minimal org.freedesktop.Notifications server; returns the received list."""
    received = []
    conn = open_dbus_connection(bus="SESSION")
    conn.send_and_get_reply(message_bus.RequestName("org.freedesktop.Notifications", 4))
    stop = threading.Event()

    def serve():
        n = 0
        while not stop.is_set():
            try:
                msg = conn.receive(timeout=0.2)
            except TimeoutError:
                continue
            except Exception:  # noqa: BLE001 — connection closed at teardown
                return
            if msg.header.message_type != MessageType.method_call:
                continue
            member = msg.header.fields.get(3)
            if member == "Notify":
                n += 1
                received.append({"summary": msg.body[3], "body": msg.body[4], "icon": msg.body[2]})
                conn.send(new_method_return(msg, "u", (n,)))
            elif member == "GetCapabilities":
                conn.send(new_method_return(msg, "as", (["body"],)))
            elif member == "GetServerInformation":
                conn.send(new_method_return(msg, "ssss", ("fake", "test", "1", "1.2")))
            else:
                conn.send(new_method_return(msg))

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield received
    stop.set()
    t.join(timeout=2)
    conn.close()


def run_harness(script, tmp_path, session_bus, display, root, screenshots=None):
    log = tmp_path / "calls.jsonl"
    cfg = tmp_path / "config.json"
    ops = tmp_path / "ops.jsonl"
    ops.write_text("\n".join(json.dumps(e) for e in [
        {"ts": "2026-11-20T09:00:00+00:00", "pair": "Main", "op": "upload", "path": "a.txt",
         "text": "upload new file a.txt (1.0 KB)"},
        {"ts": "2026-11-20T09:05:00+00:00", "pair": "Main", "op": "conflict",
         "path": "b (conflicted copy 2026-11-20 090500).txt", "src": "b.txt",
         "text": "keep local b.txt as b (conflicted copy 2026-11-20 090500).txt"},
        {"ts": "2026-11-20T09:10:00+00:00", "pair": "Main", "op": "download", "path": "c.pdf",
         "text": "download c.pdf (2.0 MB)"},
    ]) + "\n")
    cfg.write_text(json.dumps({
        "poll_interval_s": 45, "max_transfers": 4, "full_check_hours": 24,
        "prefer_newer_on_rebuild": False,
        "pairs": [{"name": "Main", "local": str(root), "remote_path": "/",
                   "excluded": ["Old projects"], "last_full_check": "2026-11-20 09:12"}],
        "paths": {"config_dir": str(tmp_path / "cfg"),
                  "operation_log": str(tmp_path / "ops.log"),
                  "ignore_file": str(tmp_path / "cfg/ignore")},
        "desktop": {"extension_installed": True, "nautilus_python": True,
                    "tray_autostart": False},
    }))
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "DISPLAY": display, "DBUS_SESSION_BUS_ADDRESS": session_bus,
        "GDK_BACKEND": "x11", "GSK_RENDERER": "cairo", "GTK_A11Y": "none", "NO_AT_BRIDGE": "1",
        "GDRIVESYNC_DESKTOP_DIR": str(DESKTOP),
        "GDRIVESYNC_CLI": f"{PY} {HERE / 'gui/fake_cli.py'}",
        "GDRIVESYNC_SYSTEMCTL": f"{PY} {HERE / 'gui/fake_systemctl.py'}",
        "FAKE_LOG": str(log), "FAKE_CONFIG": str(cfg), "SYNC_ROOT": str(root),
        "FAKE_OPS": str(ops),
        "GDRIVESYNC_JOURNALCTL": f"{PY} {HERE / 'gui/fake_journalctl.py'}",
        "PYTHONPATH": str(HERE / "gui"),
    }
    (tmp_path / "run").mkdir(mode=0o700, exist_ok=True)
    if screenshots:
        env["SCREENSHOT_DIR"] = str(screenshots)
    proc = subprocess.run([PY, str(HERE / "gui" / script)], capture_output=True, text=True,
                          timeout=180, env=env)
    print(proc.stdout)
    print(proc.stderr[-5000:], file=sys.stderr)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "all checks passed" in proc.stdout
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.mark.skipif(not HAVE_GTK4, reason="needs GTK 4 + libadwaita for the system Python")
def test_settings_window(service, session_bus, display, tmp_path):
    drive, root, d, call, client, docs, videos = service
    shots = Path(os.environ.get("GDRIVESYNC_SCREENSHOTS", tmp_path / "shots"))
    shots.mkdir(parents=True, exist_ok=True)
    calls = run_harness("settings_harness.py", tmp_path, session_bus, display, root, shots)
    assert ["cli", "config", "--json"] in calls
    assert (shots / "settings-status.png").stat().st_size > 10_000
    assert d.paused is False  # the window resumed what it paused


@pytest.mark.skipif(not HAVE_TRAY, reason="needs GTK 3 + AyatanaAppIndicator3")
def test_tray(service, session_bus, display, notifications, tmp_path):
    drive, root, d, call, client, docs, videos = service
    run_harness("tray_harness.py", tmp_path, session_bus, display, root)
    summaries = [n["summary"] for n in notifications]
    assert "Edited in two places" in summaries, notifications
    assert "Couldn't sync “big.iso”" in summaries, notifications
    assert "Google Drive sync stopped" in summaries, notifications
