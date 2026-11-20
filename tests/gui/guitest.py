"""Helpers for the GUI harnesses (run on the system Python under Xvfb)."""

import json
import os
import sys
import time

DESKTOP = os.environ["GDRIVESYNC_DESKTOP_DIR"]
sys.path.insert(0, DESKTOP)

from gi.repository import GLib  # noqa: E402

LOG = os.environ["FAKE_LOG"]
SHOTS = os.environ.get("SCREENSHOT_DIR")


def pump(seconds=0.05):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.01)


def wait(cond, timeout=15.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.05)
        if cond():
            return
    raise AssertionError(f"timed out waiting for {what}")


def calls():
    if not os.path.exists(LOG):
        return []
    with open(LOG) as f:
        return [json.loads(line) for line in f if line.strip()]


def called(*prefix):
    return any(c[: len(prefix)] == list(prefix) for c in calls())


def done(msg="all checks passed"):
    print(f"{os.path.basename(sys.argv[0])}: {msg}", flush=True)
    os._exit(0)
