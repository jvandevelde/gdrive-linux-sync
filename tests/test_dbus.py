"""The service's D-Bus API, tested on a private session bus."""

import json
import os
import shutil
import subprocess
import time

import pytest
from conftest import wait_for
from jeepney.bus_messages import MatchRule, message_bus

from gdrivesync import config
from gdrivesync.dbus_service import BUS_NAME, ERROR_NAME, INTERFACE, OBJECT_PATH, DBusService

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="needs dbus-daemon")


def test_roots_and_statuses(service):
    drive, root, d, call, client, docs, videos = service
    assert call("GetRoots") == [str(root)]
    assert call("GetFileStatus", "s", str(root / "Docs/report.pdf")) == "synced"
    assert call("GetFileStatus", "s", str(root / "Docs")) == "synced"
    assert call("GetFileStatus", "s", str(root / "Notes.gdoc")) == "synced"
    assert call("GetFileStatus", "s", str(root)) in ("synced", "syncing")
    (root / "notes.txt.swp").write_bytes(b"x")
    assert call("GetFileStatus", "s", str(root / "notes.txt.swp")) == "ignored"
    assert call("GetFileStatus", "s", "/etc/passwd") == ""
    assert call("GetFileStatus", "s", str(root / "missing.txt")) == ""
    (root / "a (conflicted copy 2026-01-02 101112).txt").write_bytes(b"c")
    assert call("GetFileStatus", "s",
                str(root / "a (conflicted copy 2026-01-02 101112).txt")) == "conflict"
    many = call("GetFileStatuses", "as", [str(root / "Docs/report.pdf"), "/nope"])
    assert dict(many) == {str(root / "Docs/report.pdf"): "synced", "/nope": ""}


def test_drive_links(service):
    drive, root, d, call, client, docs, videos = service
    assert call("GetDriveLink", "s", str(root / "Docs")) == \
        f"https://drive.google.com/drive/folders/{docs['id']}"
    assert call("GetDriveLink", "s", str(root / "Docs/report.pdf")).startswith(
        "https://drive.google.com/file/d/")
    assert call("GetDriveLink", "s", str(root / "nope")) == ""


def test_new_file_goes_syncing_then_synced_with_signals(service):
    drive, root, d, call, client, docs, videos = service
    rule = MatchRule(type="signal", interface=INTERFACE, member="StatusChanged")
    client.send_and_get_reply(message_bus.AddMatch(rule))
    new = root / "Docs/new.txt"
    with client.filter(rule) as signals:
        new.write_bytes(b"hello")
        assert call("GetFileStatus", "s", str(new)) == "syncing"
        assert wait_for(lambda: drive.live().get("Docs/new.txt") == b"hello")
        assert wait_for(lambda: call("GetFileStatus", "s", str(new)) == "synced")
        changed = set()
        end = time.monotonic() + 5
        while str(new) not in changed and time.monotonic() < end:
            msg = client.recv_until_filtered(signals, timeout=5)
            changed.update(msg.body[0])
    assert {str(new), str(root / "Docs"), str(root)} <= changed  # parents refreshed too


def test_error_status(service):
    drive, root, d, call, client, docs, videos = service
    bad = drive.add("broken.bin", parent=docs["id"], data=b"x")
    drive.corrupt.add(bad["id"])
    assert wait_for(lambda: call("GetFileStatus", "s", str(root / "Docs/broken.bin")) == "error")
    assert call("GetFileStatus", "s", str(root / "Docs")) == "error"
    state = json.loads(call("GetState"))
    assert "Docs/broken.bin" in state["pairs"][0]["errors"]


def test_pause_resume_and_state(service):
    drive, root, d, call, client, docs, videos = service
    call("Pause")
    assert json.loads(call("GetState"))["paused"] is True
    time.sleep(0.5)
    (root / "while-paused.txt").write_bytes(b"p")
    time.sleep(1.0)
    assert "while-paused.txt" not in drive.live()
    call("Resume")
    assert wait_for(lambda: "while-paused.txt" in drive.live())
    state = json.loads(call("GetState"))
    assert state["paused"] is False
    assert state["pairs"][0]["name"] == "Main"
    assert any(e["path"] == "while-paused.txt" for e in state["recent"])


def test_exclude_via_dbus(service):
    drive, root, d, call, client, docs, videos = service
    msg = call("Exclude", "s", str(root / "Videos"))
    assert "stays on Google Drive" in msg
    assert wait_for(lambda: not (root / "Videos").exists())
    assert videos["id"] in config.Settings.load().pairs[0].excluded_folder_ids
    assert not drive.files[videos["id"]]["trashed"]
    with pytest.raises(RuntimeError) as e:
        call("Exclude", "s", str(root / "Docs/report.pdf"))
    assert ERROR_NAME in str(e.value) and "Only folders" in str(e.value)

    call("Include", "s", str(root / "Videos"))
    assert wait_for(lambda: (root / "Videos/clip.mp4").exists())


def test_sync_now_and_errors(service):
    drive, root, d, call, client, docs, videos = service
    drive.add("from-web.txt", data=b"w")
    d.poll_interval_s = 3600  # only SyncNow can pick it up quickly now
    time.sleep(0.5)
    call("SyncNow")
    assert wait_for(lambda: (root / "from-web.txt").exists())
    with pytest.raises(RuntimeError, match="UnknownMethod"):
        call("NoSuchMethod")


def test_introspection_with_gdbus(service, session_bus):
    if shutil.which("gdbus") is None:
        pytest.skip("needs gdbus")
    out = subprocess.run(
        ["gdbus", "introspect", "--session", "--dest", BUS_NAME, "--object-path", OBJECT_PATH],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": session_bus},
    ).stdout
    assert "GetFileStatus" in out and "StatusChanged" in out
    out = subprocess.run(
        ["gdbus", "call", "--session", "--dest", BUS_NAME, "--object-path", OBJECT_PATH,
         "--method", f"{INTERFACE}.GetRoots"],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": session_bus},
    ).stdout
    assert "Drive" in out


def test_second_service_backs_off(service):
    drive, root, d, call, client, docs, videos = service
    other = DBusService(d)
    assert other.start() is False  # name already owned
    other.stop()
