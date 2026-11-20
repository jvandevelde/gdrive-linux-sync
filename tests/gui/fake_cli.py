"""Stand-in for the `gdrivesync` CLI in GUI tests: records calls, canned replies."""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(["cli"] + args) + "\n")

if args[:2] == ["config", "--json"]:
    print(open(os.environ["FAKE_CONFIG"]).read())
elif args[:1] == ["config"]:
    pass
elif args[:1] == ["log"]:
    import time
    for line in open(os.environ["FAKE_OPS"]):
        print(line.strip(), flush=True)
    if "--follow" in args:
        time.sleep(1.0)
        print(json.dumps({"ts": "2026-11-20T10:30:00+00:00", "pair": "Main", "op": "trash_remote",
                          "path": "old.txt", "text": "trash on Drive old.txt — deleted locally"}),
              flush=True)
        time.sleep(10)
elif args[:1] == ["whoami"]:
    print("Test User <test@example.com>")
    print("Storage: 1.23 GB used of 15.0 GB")
elif args[:2] == ["pair", "add"] and "/fail" in args:
    print("error: No folder '/fail' on Drive (use --create to make it).", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
