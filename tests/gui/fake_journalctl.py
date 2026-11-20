"""Stand-in for journalctl in GUI tests: canned JSON entries, then one 'live' one."""
import json
import os
import sys
import time

with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["journalctl"] + sys.argv[1:]) + "\n")
now = int(time.time() * 1e6)


def emit(prio, msg, ago_s):
    d = {"__REALTIME_TIMESTAMP": str(now - int(ago_s * 1e6)), "PRIORITY": str(prio),
         "MESSAGE": msg, "_SYSTEMD_USER_UNIT": "gdrivesync.service"}
    print(json.dumps(d), flush=True)


emit(6, "gdrivesync.daemon: Syncing /home/you/Google Drive ⇄ Drive My Drive", 300)
emit(6, "gdrivesync.ops: [My Drive] upload new file Notes/todo.txt (312 B)", 240)
emit(4, "gdrivesync.daemon: [My Drive] full check found differences the change feed missed: 1 downloaded", 200)
emit(3, "gdrivesync.engine: Videos/holiday.mov: HttpError: storage quota exceeded", 120)
emit(6, list("caf\xe9 \xff bytes".encode("latin-1")), 60)  # non-UTF-8 → byte array
emit(6, "2026-01-01 10:00:00 WARNING gdrivesync.engine: old-style line without a level prefix", 30)
if "--follow" in sys.argv:
    time.sleep(1.0)
    emit(6, "gdrivesync.ops: [My Drive] download Photos/new.jpg (2.0 MB) — live", 0)
    time.sleep(10)
