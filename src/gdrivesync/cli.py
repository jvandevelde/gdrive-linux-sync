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


def _setup_logging(verbose: bool) -> None:
    import logging

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)


def _engine(initial_pull: bool = True):
    """Build a SyncEngine, running the initial pull first if never synced."""
    from gdrivesync import auth
    from gdrivesync.drive import DriveClient
    from gdrivesync.engine import SyncEngine
    from gdrivesync.localfs import IgnoreRules
    from gdrivesync.pull import pull
    from gdrivesync.state import StateDB

    settings = config.Settings.load()
    client = DriveClient(auth.load_credentials())
    db = StateDB(config.STATE_DB)
    if db.page_token is None and initial_pull:
        import logging

        logging.getLogger("gdrivesync").info("First run: downloading My Drive…")
        res = pull(
            client, db, settings.root,
            excluded=settings.excluded_folder_ids,
            max_workers=settings.max_transfers,
            log=logging.getLogger("gdrivesync.pull").info,
        )
        if res.errors:
            for path, msg in res.errors:
                logging.getLogger("gdrivesync.pull").error("%s: %s", path, msg)
            from gdrivesync.pull import PullError

            raise PullError(f"Initial download had {len(res.errors)} errors; re-run to retry.")
    engine = SyncEngine(
        client, db, settings.root,
        excluded=settings.excluded_folder_ids,
        rules=IgnoreRules.load(),
    )
    return engine, settings


def cmd_sync(args: argparse.Namespace) -> int:
    from gdrivesync.daemon import _summary

    _setup_logging(args.verbose)
    engine, _ = _engine()
    engine.settle_s = 0
    engine.allow_mass_delete = args.allow_mass_delete
    remote = engine.remote_pass()
    local = engine.local_pass()
    if engine.dirty:  # conflicted copies to upload
        local = engine.local_pass()
    print(f"Drive -> local: {_summary(remote)}")
    print(f"local -> Drive: {_summary(local)}")
    errors = remote.errors + local.errors
    for path, msg in errors:
        print(f"  error {path}: {msg}", file=sys.stderr)
    return 2 if errors else 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from gdrivesync.daemon import Daemon

    _setup_logging(args.verbose)
    engine, settings = _engine()
    d = Daemon(engine, poll_interval_s=settings.poll_interval_s)
    d.install_signal_handlers()
    d.run()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from gdrivesync.doctor import find_duplicates, fix_duplicates
    from gdrivesync.state import StateDB

    _setup_logging(False)
    db = StateDB(config.STATE_DB)
    pending = db.all_pending()
    if pending:
        print(f"{len(pending)} interrupted uploads will be resumed or cleaned up on the next sync:")
        for p in pending:
            print(f"  {p['rel_path']}")
    groups = find_duplicates(db)
    if not groups:
        print("No duplicates found.")
        return 0
    n = sum(len(g.extras) for g in groups)
    print(f"{n} duplicate(s) on Drive:")
    for g in groups:
        for it in g.extras:
            print(f"  {it.rel_path}  (same as {g.keep.rel_path})")
    if not args.fix:
        print("\nRun `gdrivesync doctor --fix` to move the duplicates to the trash "
              "(on Drive and locally). Stop the service first.")
        return 1
    from gdrivesync import auth
    from gdrivesync.drive import DriveClient

    removed = fix_duplicates(
        DriveClient(auth.load_credentials()), db, config.Settings.load().root, groups
    )
    print(f"Moved {removed} duplicate(s) to the trash.")
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

    sy = sub.add_parser("sync", help="run one two-way sync pass and exit")
    sy.add_argument("-v", "--verbose", action="store_true")
    sy.add_argument("--allow-mass-delete", action="store_true",
                    help="allow trashing many files on Drive that disappeared locally")
    sy.set_defaults(func=cmd_sync)

    dm = sub.add_parser("daemon", help="run the background sync service")
    dm.add_argument("-v", "--verbose", action="store_true")
    dm.set_defaults(func=cmd_daemon)

    dr = sub.add_parser("doctor", help="find duplicate files/folders on Drive")
    dr.add_argument("--fix", action="store_true", help="move the duplicates to the trash")
    dr.set_defaults(func=cmd_doctor)

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
        from gdrivesync.engine import SyncError
        from gdrivesync.pull import PullError

        if isinstance(e, (AuthError, PullError, SyncError)):
            print(f"error: {e}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
