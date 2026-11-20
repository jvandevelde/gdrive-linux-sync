"""Shared fixtures: a private D-Bus session bus and a running sync service."""

import shutil
import subprocess
import threading
import time

import pytest
from fakedrive import FakeDrive
from jeepney import DBusAddress, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection

from gdrivesync import config
from gdrivesync import daemon as daemon_mod
from gdrivesync import engine as engine_mod
from gdrivesync.daemon import Daemon
from gdrivesync.dbus_service import BUS_NAME, INTERFACE, OBJECT_PATH, DBusService
from gdrivesync.drive import FOLDER_MIME
from gdrivesync.engine import SyncEngine
from gdrivesync.pull import pull
from gdrivesync.state import StateDB

API = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=INTERFACE)


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def session_bus(monkeypatch):
    proc = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--nopidfile", "--print-address=1"],
        stdout=subprocess.PIPE, text=True,
    )
    address = proc.stdout.readline().strip()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    yield address
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def service(tmp_path, monkeypatch, session_bus):
    monkeypatch.setattr(daemon_mod, "DEBOUNCE_S", 0.2)
    monkeypatch.setattr(daemon_mod, "MAX_DELAY_S", 1.0)
    monkeypatch.setattr(engine_mod, "trash_local",
                        lambda p: shutil.rmtree(p) if p.is_dir() else p.unlink())
    for name, value in {
        "CONFIG_DIR": tmp_path / "cfg", "DATA_DIR": tmp_path / "data",
        "SETTINGS_FILE": tmp_path / "cfg/settings.json", "STATE_DB": tmp_path / "data/state.db",
    }.items():
        monkeypatch.setattr(config, name, value)

    drive = FakeDrive()
    docs = drive.add("Docs", mime=FOLDER_MIME)
    drive.add("report.pdf", parent=docs["id"], data=b"pdf")
    videos = drive.add("Videos", mime=FOLDER_MIME)
    drive.add("clip.mp4", parent=videos["id"], data=b"mp4")
    drive.add("Notes", mime="application/vnd.google-apps.document")
    root = (tmp_path / "Drive").resolve()
    pair = config.Pair("Main", str(root), "/", "root")
    config.Settings(pairs=[pair]).save()
    db = StateDB(pair.db_path)
    pull(drive, db, root, log=lambda m: None)
    eng = SyncEngine(drive, db, root, settle_s=0.2, name="Main")
    d = Daemon(eng, poll_interval_s=0.3, full_check_s=0)
    api = DBusService(d)
    assert api.start()
    t = threading.Thread(target=d.run, daemon=True)
    t.start()
    client = open_dbus_connection(bus="SESSION")

    def call(method, sig=None, *args):
        reply = client.send_and_get_reply(new_method_call(API, method, sig, args), timeout=5)
        if reply.header.message_type == MessageType.error:
            raise RuntimeError(reply.header.fields[4], *reply.body)  # 4 = error_name
        return reply.body[0] if reply.body else None

    yield drive, root, d, call, client, docs, videos
    client.close()
    d.stop()
    t.join(timeout=5)
    api.stop()
