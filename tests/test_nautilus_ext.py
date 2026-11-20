"""Runs the Nautilus extension (on the system Python with PyGObject) against the
real sync service on a private D-Bus session bus. Only Nautilus is faked."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import gdrivesync

HERE = Path(__file__).parent
EXTENSION = Path(gdrivesync.__file__).parent / "desktop/gdrivesync_nautilus.py"


def system_python_with_gi():
    for exe in ("/usr/bin/python3", "/usr/bin/python3.12", "/usr/bin/python3.11",
                "/usr/bin/python3.13"):
        if not os.path.exists(exe):
            continue
        ok = subprocess.run(
            [exe, "-c", "import gi; gi.require_version('Gio', '2.0'); "
                        "from gi.repository import Gio"],
            capture_output=True,
        ).returncode == 0
        if ok:
            return exe
    return None


PY = system_python_with_gi()

pytestmark = [
    pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="needs dbus-daemon"),
    pytest.mark.skipif(PY is None, reason="needs a system Python with PyGObject"),
]


def test_extension_against_running_service(service, session_bus):
    drive, root, d, call, client, docs, videos = service
    proc = subprocess.run(
        [PY, str(HERE / "nautilus_harness.py"), str(EXTENSION), str(root)],
        capture_output=True, text=True, timeout=120,
        env={"DBUS_SESSION_BUS_ADDRESS": session_bus, "PATH": os.environ["PATH"],
             "HOME": os.environ.get("HOME", "/tmp")},
    )
    print(proc.stdout)
    print(proc.stderr, file=sys.stderr)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "all checks passed" in proc.stdout
    assert videos["id"] in d.slots[0].engine.excluded


def test_extension_is_valid_python_for_old_interpreters():
    # Ubuntu 22.04 ships Python 3.10: the extension must not need anything newer.
    src = EXTENSION.read_text()
    compile(src, str(EXTENSION), "exec", flags=0, dont_inherit=True)
    assert "match " not in src  # no pattern matching (3.10 has it, but keep it simple)
