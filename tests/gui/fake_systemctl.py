"""Stand-in for `systemctl --user` in GUI tests."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["systemctl"] + args) + "\n")
if "is-enabled" in args:
    print("enabled")
sys.exit(0)
