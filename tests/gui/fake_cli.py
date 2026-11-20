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
elif args[:1] == ["whoami"]:
    print("Test User <test@example.com>")
    print("Storage: 1.23 GB used of 15.0 GB")
elif args[:2] == ["pair", "add"] and "/fail" in args:
    print("error: No folder '/fail' on Drive (use --create to make it).", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
