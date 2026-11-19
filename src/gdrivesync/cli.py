"""`gdrivesync` command-line entry point."""

from __future__ import annotations

import argparse
import sys

from gdrivesync import __version__, config


def cmd_login(args: argparse.Namespace) -> int:
    from gdrivesync import auth
    from gdrivesync.drive import DriveClient

    creds = auth.login()
    user = DriveClient(creds).about()["user"]
    print(f"Logged in as {user['displayName']} <{user['emailAddress']}>")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    from gdrivesync import auth

    auth.logout()
    print("Logged out.")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    from gdrivesync import auth
    from gdrivesync.drive import DriveClient

    info = DriveClient(auth.load_credentials()).about()
    user, quota = info["user"], info.get("storageQuota", {})
    print(f"{user['displayName']} <{user['emailAddress']}>")
    if "usage" in quota:
        used = int(quota["usage"]) / 1e9
        limit = f"{int(quota['limit']) / 1e9:.1f} GB" if "limit" in quota else "unlimited"
        print(f"Storage: {used:.2f} GB used of {limit}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    from gdrivesync import auth
    from gdrivesync.drive import FOLDER_MIME, DriveClient

    client = DriveClient(auth.load_credentials())
    for f in sorted(client.list_children(args.folder_id), key=lambda f: f["name"].lower()):
        kind = "d" if f["mimeType"] == FOLDER_MIME else "-"
        print(f"{kind} {f['id']}  {f['name']}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    s = config.Settings.load()
    if args.sync_root:
        s.sync_root = args.sync_root
        s.save()
    print(f"config dir : {config.CONFIG_DIR}")
    print(f"data dir   : {config.DATA_DIR}")
    print(f"sync root  : {s.root}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gdrivesync", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="sign in to Google Drive").set_defaults(func=cmd_login)
    sub.add_parser("logout", help="forget stored credentials").set_defaults(func=cmd_logout)
    sub.add_parser("whoami", help="show account and quota").set_defaults(func=cmd_whoami)

    ls = sub.add_parser("ls", help="list a remote folder")
    ls.add_argument("folder_id", nargs="?", default="root")
    ls.set_defaults(func=cmd_ls)

    cfg = sub.add_parser("config", help="show or change settings")
    cfg.add_argument("--sync-root", help="local folder to mirror My Drive into")
    cfg.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:  # surface auth/config problems cleanly
        from gdrivesync.auth import AuthError

        if isinstance(e, AuthError):
            print(f"error: {e}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
