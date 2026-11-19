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


def cmd_pull(args: argparse.Namespace) -> int:
    from gdrivesync import auth
    from gdrivesync.drive import DriveClient
    from gdrivesync.pull import pull
    from gdrivesync.state import StateDB

    settings = config.Settings.load()
    client = DriveClient(auth.load_credentials())
    db = StateDB(config.STATE_DB)
    log = (lambda _msg: None) if args.quiet else print
    try:
        res = pull(
            client,
            db,
            settings.root,
            excluded=settings.excluded_folder_ids,
            max_workers=settings.max_transfers,
            dry_run=args.dry_run,
            log=log,
        )
    finally:
        db.close()

    verb = "Would download" if args.dry_run else "Downloaded"
    print(
        f"\n{verb} {res.downloaded} files ({res.downloaded_bytes / 1e6:.1f} MB); "
        f"{res.folders} folders, {res.stubs} Google Docs links, "
        f"{res.unchanged} unchanged, {res.adopted} already present, {res.moved} moved."
    )
    if res.skipped:
        print(f"Skipped {res.skipped} shortcuts.")
    if res.forgotten:
        print(f"{res.forgotten} items no longer on Drive were dropped from tracking "
              "(local copies left in place).")
    if res.conflicts:
        print(f"\n{len(res.conflicts)} local files differed from Drive and were kept as:")
        for c in res.conflicts:
            print(f"  {c}")
    if res.errors:
        print(f"\n{len(res.errors)} errors (re-run `gdrivesync pull` to retry):", file=sys.stderr)
        for path, msg in res.errors:
            print(f"  {path}: {msg}", file=sys.stderr)
        return 2
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

    pl = sub.add_parser("pull", help="download My Drive into the sync folder")
    pl.add_argument("-n", "--dry-run", action="store_true", help="show what would happen")
    pl.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    pl.set_defaults(func=cmd_pull)

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
        from gdrivesync.pull import PullError

        if isinstance(e, (AuthError, PullError)):
            print(f"error: {e}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
