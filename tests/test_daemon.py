import os
import threading
import time

import pytest
from fakedrive import FakeDrive

from gdrivesync import daemon as daemon_mod
from gdrivesync.daemon import Daemon
from gdrivesync.drive import FOLDER_MIME
from gdrivesync.engine import SyncEngine
from gdrivesync.pull import pull
from gdrivesync.state import StateDB


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def running(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_mod, "DEBOUNCE_S", 0.2)
    monkeypatch.setattr(daemon_mod, "MAX_DELAY_S", 1.0)
    drive = FakeDrive()
    d = drive.add("Folder", mime=FOLDER_MIME)
    drive.add("a.txt", parent=d["id"], data=b"a")
    db = StateDB(tmp_path / "state.db")
    root = tmp_path / "Drive"
    pull(drive, db, root, log=lambda m: None)
    eng = SyncEngine(drive, db, root, settle_s=0.3)
    dm = Daemon(eng, poll_interval_s=0.3)
    t = threading.Thread(target=dm.run, daemon=True)
    t.start()
    yield drive, root, d
    dm.stop()
    t.join(timeout=5)
    assert not t.is_alive()


def test_daemon_end_to_end(running):
    drive, root, folder = running

    # local new file is uploaded
    (root / "Folder/new.txt").write_bytes(b"hello")
    assert wait_for(lambda: drive.live().get("Folder/new.txt") == b"hello")

    # local folder rename is a single remote rename (watcher move event)
    os.rename(root / "Folder", root / "Renamed")
    assert wait_for(lambda: drive.files[folder["id"]]["name"] == "Renamed")
    time.sleep(1.0)
    assert drive.uploads == ["new.txt"]
    assert not drive.files[folder["id"]]["trashed"]

    # remote change arrives locally
    drive.add("from-web.txt", parent=folder["id"], data=b"web")
    assert wait_for(lambda: (root / "Renamed/from-web.txt").exists())
