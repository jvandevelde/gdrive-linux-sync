"""`gdrivesync` command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from gdrivesync import __version__, config

RESTART_HINT = "If the service is running: systemctl --user restart gdrivesync"


def _oplog():
    from gdrivesync.oplog import OpLog

    return OpLog(config.OPS_LOG)


def _client():
    from gdrivesync import auth
    from gdrivesync.drive import DriveClient

    return DriveClient(auth.load_credentials())


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)


def _selected_pairs(settings: config.Settings, name: str | None) -> list[config.Pair]:
    from gdrivesync.pairs import PairError

    if not settings.pairs:
        raise PairError(
            "No synced folders yet. Add one, e.g.:\n"
            "  gdrivesync pair add ~/'Google Drive' /          # all of My Drive\n"
            "  gdrivesync pair add ~/Work /Work                # one Drive folder"
        )
    if name is None:
        return settings.pairs
    try:
        return [settings.pair(name)]
    except KeyError:
        raise PairError(f"No pair named {name!r}.") from None


# ---------------------------------------------------------------- account


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
    info = _client().about()
    user, quota = info["user"], info.get("storageQuota", {})
    print(f"{user['displayName']} <{user['emailAddress']}>")
    if "usage" in quota:
        used = int(quota["usage"]) / 1e9
        limit = f"{int(quota['limit']) / 1e9:.1f} GB" if "limit" in quota else "unlimited"
        print(f"Storage: {used:.2f} GB used of {limit}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    from gdrivesync.drive import FOLDER_MIME
    from gdrivesync.pairs import resolve_remote_path

    client = _client()
    target = args.folder
    folder_id = resolve_remote_path(client, target) if target.startswith("/") else target
    for f in sorted(client.list_children(folder_id), key=lambda f: f["name"].lower()):
        kind = "d" if f["mimeType"] == FOLDER_MIME else "-"
        print(f"{kind} {f['id']}  {f['name']}")
    return 0


# ---------------------------------------------------------------- pairs


def _last_full_check(pair) -> str:
    from datetime import datetime

    from gdrivesync.engine import FULL_CHECK_KEY
    from gdrivesync.state import StateDB

    if not pair.db_path.exists():
        return "never (not synced yet)"
    db = StateDB(pair.db_path)
    try:
        ts = db.get(FULL_CHECK_KEY)
    finally:
        db.close()
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else "never"


def cmd_pair_list(args: argparse.Namespace) -> int:
    from gdrivesync.pairs import excluded_listing

    settings = config.Settings.load()
    if not settings.pairs:
        print("No synced folders. Add one with `gdrivesync pair add LOCAL /DRIVE/PATH`.")
        return 0
    for p in settings.pairs:
        print(f"{p.name}\n  local : {p.root}\n  Drive : {p.remote_path}")
        print(f"  last full check: {_last_full_check(p)}")
        for ex in excluded_listing(p):
            print(f"  excluded: {ex}")
    return 0


def cmd_pair_add(args: argparse.Namespace) -> int:
    from gdrivesync.pairs import normalize_remote_path, resolve_remote_path, validate_new_pair

    settings = config.Settings.load()
    client = _client()
    remote_path = normalize_remote_path(args.remote)
    remote_id = "root" if remote_path == "/" else resolve_remote_path(
        client, remote_path, create=args.create
    )
    name = args.name or ("My Drive" if remote_path == "/" else remote_path.rsplit("/", 1)[-1])
    pair = config.Pair(
        name=name,
        local=str(Path(args.local).expanduser().resolve()),
        remote_path=remote_path,
        remote_id=remote_id,
    )
    validate_new_pair(client, settings, pair)
    settings.pairs.append(pair)
    settings.save()
    print(f"Added {name!r}: {pair.root} ⇄ Drive {remote_path}")
    print(f"Download it now with `gdrivesync pull --pair '{name}'` (the service also does this "
          f"on start). {RESTART_HINT}")
    return 0


def cmd_pair_remove(args: argparse.Namespace) -> int:
    settings = config.Settings.load()
    pair = _selected_pairs(settings, args.name)[0]
    held = _locks([pair])
    settings.pairs.remove(pair)
    settings.save()
    for suffix in ("", "-wal", "-shm"):
        Path(str(pair.db_path) + suffix).unlink(missing_ok=True)
    for lk in held:
        lk.release()
    Path(str(pair.db_path) + ".lock").unlink(missing_ok=True)
    print(f"Stopped syncing {pair.name!r}. Local files in {pair.root} and the Drive folder "
          f"were left untouched. {RESTART_HINT}")
    return 0


def cmd_pair_rebuild(args: argparse.Namespace) -> int:
    """Throw away a pair's sync state and rebuild it by comparing the local folder
    with Drive (checksums; nothing identical is downloaded again)."""
    from gdrivesync.state import quarantine

    settings = config.Settings.load()
    pair = _selected_pairs(settings, args.name)[0]
    prefer = settings.prefer_newer_on_rebuild if args.prefer_newer is None else args.prefer_newer
    client = _client()
    print(f"== Rebuilding {pair.name}: {pair.root} ⇄ Drive {pair.remote_path}"
          + ("  (dry run)" if args.dry_run else ""))
    if args.dry_run:
        from gdrivesync.localfs import IgnoreRules
        from gdrivesync.pull import pull
        from gdrivesync.state import StateDB

        res = pull(
            client, StateDB(":memory:"), pair.root,
            root_id=pair.remote_id, excluded=pair.excluded_folder_ids,
            dry_run=True, prefer_newer=prefer, rules=IgnoreRules.load(),
            log=lambda m: print("  " + m),
        )
        _print_pull(res, dry_run=True)
        print("\nNothing was changed. Run without --dry-run to rebuild.")
        return 0

    held = _locks([pair])  # noqa: F841
    old = quarantine(pair.db_path, "replaced")
    res = _pull_pair(client, settings, pair, oplog=_oplog(), prefer_newer=prefer)
    _print_pull(res, dry_run=False)
    if old:
        print(f"\nPrevious sync state kept as {old} (safe to delete).")
    print(RESTART_HINT.replace("If the service is running", "If you stopped the service, start it"))
    if res.errors:
        print("Some items failed; run `gdrivesync pair rebuild` again to retry.", file=sys.stderr)
        return 2
    return 0


def cmd_exclude(args: argparse.Namespace) -> int:
    from gdrivesync.pairs import exclude_path, excluded_listing, include_path

    settings = config.Settings.load()
    if args.action == "list":
        for p in settings.pairs:
            for ex in excluded_listing(p):
                print(f"{p.name}: {ex}")
        return 0
    if not args.path:
        print("error: give a local folder path", file=sys.stderr)
        return 1
    path = Path(args.path)
    if args.action == "add":
        pair = exclude_path(settings, path)
        settings.save()
        print(f"Excluded {path}. On the next sync its local copy is removed (it stays on Drive);"
              f" folders with changes not yet uploaded are removed once those finish.")
    else:
        pair = include_path(settings, path)
        settings.save()
        print(f"Re-included {path}; it will be downloaded on the next sync.")
    print(f"Run `gdrivesync sync --pair '{pair.name}'`, or: {RESTART_HINT}")
    return 0


# ---------------------------------------------------------------- syncing


def _locks(pairs):
    """Take the per-pair locks (fails if the service or another command holds one)."""
    from gdrivesync.state import PairLock

    held = []
    try:
        for p in pairs:
            held.append(PairLock(p.db_path).acquire())
    except Exception:
        for lk in held:
            lk.release()
        raise
    return held


def _open_state(pair):
    """Open a pair's state DB, moving a corrupted one aside (→ rebuild)."""
    from gdrivesync.state import StateDB

    db, moved = StateDB.open_checked(pair.db_path)
    if moved:
        print(f"[{pair.name}] sync state was corrupted; kept it as {moved.name} and rebuilding.",
              file=sys.stderr)
    return db


def _dry_state(pair):
    """A throwaway in-memory copy of a pair's state (fresh if missing or damaged)."""
    from gdrivesync.state import StateDB, is_corruption

    if not pair.db_path.exists():
        return StateDB(":memory:")
    try:
        db = StateDB(pair.db_path)
        try:
            snap = db.snapshot()
            snap.conn.execute("PRAGMA quick_check").fetchone()
            return snap
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        if not is_corruption(e):
            raise
        print(f"[{pair.name}] sync state is corrupted; showing a rebuild.", file=sys.stderr)
        return StateDB(":memory:")


def _print_pull(res, dry_run: bool) -> None:
    verb = "Would download" if dry_run else "Downloaded"
    print(
        f"{verb} {res.downloaded} files ({res.downloaded_bytes / 1e6:.1f} MB); "
        f"{res.folders} folders, {res.stubs} Google Docs links, "
        f"{res.unchanged} unchanged, {res.adopted} already present, {res.moved} moved."
    )
    if res.skipped:
        print(f"Skipped {res.skipped} shortcuts.")
    if res.forgotten:
        print(f"{res.forgotten} items no longer on Drive were dropped from tracking "
              "(local copies left in place).")
    if res.conflicts:
        print(f"{len(res.conflicts)} local files differed from Drive and were kept as:")
        for c in res.conflicts:
            print(f"  {c}")
    would = "would be " if dry_run else ""
    if res.newer_local:
        print(f"{len(res.newer_local)} local files are newer than Drive and {would or 'will be '}"
              "uploaded as new versions (Drive keeps the old version in its history):")
        for p in res.newer_local:
            print(f"  {p}")
    if res.deleted_on_drive:
        print(f"{len(res.deleted_on_drive)} local items were deleted on Drive (found in Drive's "
              f"trash) and {would or 'were '}moved to your trash:")
        for p in res.deleted_on_drive:
            print(f"  {p}")
    if res.orphan_links:
        print(f"{len(res.orphan_links)} Google Docs links point at documents that are gone and "
              f"{would or 'were '}moved to your trash:")
        for p in res.orphan_links:
            print(f"  {p}")
    if res.local_only:
        print(f"{len(res.local_only)} local files aren't on Drive and will be uploaded by the next "
              "sync. Delete any you had deleted on Drive before that:")
        for p in res.local_only[:50]:
            print(f"  {p}")
        if len(res.local_only) > 50:
            print(f"  … and {len(res.local_only) - 50} more")
    for path, msg in res.errors:
        print(f"  error {path}: {msg}", file=sys.stderr)


def _pull_pair(client, settings, pair, *, dry_run=False, log=print, oplog=None,
               prefer_newer=None):
    from gdrivesync.localfs import IgnoreRules
    from gdrivesync.pull import pull

    db = _dry_state(pair) if dry_run else _open_state(pair)
    try:
        return pull(
            client, db, pair.root,
            root_id=pair.remote_id,
            excluded=pair.excluded_folder_ids,
            max_workers=settings.max_transfers,
            dry_run=dry_run,
            log=log,
            oplog=oplog,
            pair=pair.name,
            prefer_newer=settings.prefer_newer_on_rebuild if prefer_newer is None else prefer_newer,
            rules=IgnoreRules.load(),
        )
    finally:
        db.close()


def cmd_pull(args: argparse.Namespace) -> int:
    settings = config.Settings.load()
    client = _client()
    failed = False
    pairs = _selected_pairs(settings, args.pair)
    held = [] if args.dry_run else _locks(pairs)
    for pair in pairs:
        print(f"== {pair.name}: {pair.root} ⇄ Drive {pair.remote_path}")
        res = _pull_pair(
            client, settings, pair,
            dry_run=args.dry_run,
            log=(lambda _m: None) if args.quiet else print,
            oplog=None if args.dry_run else _oplog(),
        )
        _print_pull(res, args.dry_run)
        failed |= bool(res.errors)
    if failed:
        print("Some downloads failed; re-run `gdrivesync pull` to retry.", file=sys.stderr)
    return 2 if failed else 0


def _build_engine(settings, pair, client, oplog=None, rules=None):
    """A SyncEngine for one pair. Runs the initial download first if the pair has
    no sync state yet — which is also how lost or corrupted state is rebuilt."""
    from gdrivesync.engine import SyncEngine
    from gdrivesync.localfs import IgnoreRules
    from gdrivesync.pull import PullError

    db = _open_state(pair)
    if db.page_token is None:
        plog = logging.getLogger("gdrivesync.pull")
        plog.info("[%s] no sync state: downloading / reconciling %s", pair.name, pair.remote_path)
        db.close()
        res = _pull_pair(client, settings, pair, log=plog.info, oplog=oplog)
        for label, items in (("newer locally, will upload", res.newer_local),
                             ("deleted on Drive, moved to trash", res.deleted_on_drive),
                             ("orphaned Docs link, moved to trash", res.orphan_links),
                             ("only local, will upload", res.local_only)):
            for path in items:
                plog.warning("[%s] %s: %s", pair.name, label, path)
        if res.errors:
            for path, msg in res.errors:
                plog.error("[%s] %s: %s", pair.name, path, msg)
            raise PullError(
                f"Initial download of {pair.name!r} had {len(res.errors)} errors; re-run to retry."
            )
        db = _open_state(pair)
    return SyncEngine(
        client, db, pair.root,
        remote_root_id=pair.remote_id,
        excluded=pair.excluded_folder_ids,
        rules=rules or IgnoreRules.load(),
        name=pair.name,
        oplog=oplog,
    )


def _engines(settings, pairs, client, oplog=None):
    """One SyncEngine per pair, running the initial download for new pairs."""
    from gdrivesync.localfs import IgnoreRules

    rules = IgnoreRules.load()
    return [_build_engine(settings, p, client, oplog, rules) for p in pairs]


def _dry_run_sync(args, settings, pairs) -> int:
    """Run the real sync logic against a throwaway copy of each pair's state:
    Drive is only read, the local disk isn't touched."""
    from gdrivesync.engine import SyncEngine
    from gdrivesync.localfs import IgnoreRules
    from gdrivesync.oplog import OpLog
    from gdrivesync.state import StateDB

    client = _client()
    total, errors = 0, 0
    for pair in pairs:
        print(f"== {pair.name}: {pair.root} ⇄ Drive {pair.remote_path}  (dry run)")
        snap = _dry_state(pair)
        if snap.page_token is None:
            snap.close()
            print("  no sync state: this is what the first download / rebuild would do")
            res = _pull_pair(client, settings, pair, dry_run=True, log=lambda m: print("  " + m))
            _print_pull(res, dry_run=True)
            total += res.downloaded + len(res.conflicts)
            continue
        oplog = OpLog(dry_run=True, echo=lambda m: print("  " + m))
        engine = SyncEngine(
            client, snap, pair.root,
            remote_root_id=pair.remote_id,
            excluded=pair.excluded_folder_ids,
            rules=IgnoreRules.load(),
            settle_s=0,
            name="",
            oplog=oplog,
            dry_run=True,
        )
        engine.allow_mass_delete = args.allow_mass_delete
        remote = engine.full_check() if args.full else engine.remote_pass()
        stats = [remote, engine.local_pass()]
        if engine.dirty:
            stats.append(engine.local_pass())
        for st in stats:
            for path, msg in st.errors:
                print(f"  error {path}: {msg}", file=sys.stderr)
                errors += 1
        if oplog.count == 0:
            print("  nothing to do")
        total += oplog.count
    print(f"\n{total} operation(s) would be performed. Nothing was changed.")
    return 2 if errors else 0


def cmd_sync(args: argparse.Namespace) -> int:
    from gdrivesync.daemon import _summary

    _setup_logging(args.verbose)
    settings = config.Settings.load()
    pairs = _selected_pairs(settings, args.pair)
    if args.dry_run:
        logging.getLogger("gdrivesync.ops").setLevel(logging.WARNING)
        return _dry_run_sync(args, settings, pairs)
    held = _locks(pairs)  # noqa: F841 — held until exit
    errors = 0
    for engine in _engines(settings, pairs, _client(), _oplog()):
        engine.settle_s = 0
        engine.allow_mass_delete = args.allow_mass_delete
        remote = engine.full_check() if args.full else engine.remote_pass()
        local = engine.local_pass()
        if engine.dirty:  # conflicted copies to upload
            local = engine.local_pass()
        print(f"== {engine.name}")
        print(f"  Drive -> local: {_summary(remote)}")
        print(f"  local -> Drive: {_summary(local)}")
        for path, msg in remote.errors + local.errors:
            print(f"  error {path}: {msg}", file=sys.stderr)
        errors += len(remote.errors) + len(local.errors)
    return 2 if errors else 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from gdrivesync.daemon import Daemon

    _setup_logging(args.verbose)
    settings = config.Settings.load()
    pairs = _selected_pairs(settings, None)
    held = _locks(pairs)  # noqa: F841 — held for the life of the service
    client, oplog = _client(), _oplog()
    engines = _engines(settings, pairs, client, oplog)

    def rebuild(engine):
        from gdrivesync.state import quarantine

        fresh = config.Settings.load()  # exclusions may have changed since start
        pair = fresh.pair(engine.name)
        try:
            engine.db.close()
        except Exception:  # noqa: BLE001
            pass
        moved = quarantine(pair.db_path, "corrupt")
        logging.getLogger("gdrivesync").error("[%s] corrupted state kept as %s", pair.name, moved)
        return _build_engine(fresh, pair, client, oplog)

    d = Daemon(
        engines,
        poll_interval_s=settings.poll_interval_s,
        full_check_s=settings.full_check_hours * 3600,
        rebuild=rebuild,
    )
    d.install_signal_handlers()
    from gdrivesync.dbus_service import DBusService

    api = DBusService(d)
    api.start()
    try:
        d.run()
    finally:
        api.stop()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from gdrivesync.doctor import find_duplicates, fix_duplicates
    from gdrivesync.state import StateDB

    _setup_logging(False)
    settings = config.Settings.load()
    found_any = False
    client = None
    pairs = [p for p in _selected_pairs(settings, None) if p.db_path.exists()]
    held = _locks(pairs) if args.fix else []  # noqa: F841
    for pair in pairs:
        db = _open_state(pair) if args.fix else _dry_state(pair)
        try:
            pending = db.all_pending()
            if pending:
                print(f"[{pair.name}] {len(pending)} interrupted uploads will be resumed or "
                      "cleaned up on the next sync:")
                for p in pending:
                    print(f"  {p['rel_path']}")
            groups = find_duplicates(db)
            if not groups:
                continue
            found_any = True
            n = sum(len(g.extras) for g in groups)
            print(f"[{pair.name}] {n} duplicate(s) on Drive:")
            for g in groups:
                for it in g.extras:
                    print(f"  {it.rel_path}  (same as {g.keep.rel_path})")
            if args.fix:
                client = client or _client()
                removed = fix_duplicates(client, db, pair.root, groups, _oplog(), pair.name)
                print(f"  moved {removed} duplicate(s) to the trash.")
        finally:
            db.close()
    if not found_any:
        print("No duplicates found.")
        return 0
    if not args.fix:
        print("\nRun `gdrivesync doctor --fix` to move the duplicates to the trash "
              "(on Drive and locally). Stop the service first.")
        return 1
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    import json
    import time
    from collections import deque
    from datetime import datetime

    from gdrivesync.oplog import describe, read_entries

    path = config.OPS_LOG

    def show(e: dict) -> None:
        if args.pair and e.get("pair") != args.pair:
            return
        if args.json:
            print(json.dumps(e, ensure_ascii=False))
            return
        try:
            ts = datetime.fromisoformat(e["ts"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            ts = e.get("ts", "?")
        print(f"{ts}  {describe(e)}")

    entries = [e for e in read_entries(path) if not args.pair or e.get("pair") == args.pair]
    for e in deque(entries, maxlen=args.n) if args.n else entries:
        show(e)
    if not entries and not args.follow:
        print(f"No operations logged yet ({path}).")
    if args.follow:
        pos = path.stat().st_size if path.exists() else 0
        try:
            while True:
                time.sleep(1)
                if not path.exists():
                    continue
                size = path.stat().st_size
                if size < pos:  # rotated
                    pos = 0
                if size > pos:
                    with open(path, encoding="utf-8") as fh:
                        fh.seek(pos)
                        for line in fh:
                            try:
                                show(json.loads(line))
                            except ValueError:
                                pass
                        pos = fh.tell()
        except KeyboardInterrupt:
            pass
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    """Open a .gdoc/.gsheet/… link file (or any synced item) in the browser."""
    import json
    import webbrowser

    path = Path(args.path).expanduser()
    url = None
    try:
        data = json.loads(path.read_text()) if path.is_file() and path.stat().st_size < 4096 else {}
        url = data.get("url") if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        url = None
    if not url:
        from gdrivesync.pairs import PairError, _locate
        from gdrivesync.state import StateDB
        from gdrivesync.status import drive_link

        try:
            pair, rel = _locate(config.Settings.load(), path)
        except PairError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        db = StateDB(pair.db_path, readonly=True)
        try:
            item = db.by_path(rel)
        finally:
            db.close()
        if item is None:
            print(f"error: {path} isn't on Google Drive yet", file=sys.stderr)
            return 1
        url = drive_link(item)
    if args.print:
        print(url)
    else:
        webbrowser.open(url)
    return 0


def cmd_install_desktop(args: argparse.Namespace) -> int:
    from gdrivesync import desktop_install

    settings = config.Settings.load()
    if args.uninstall:
        notes = desktop_install.uninstall(settings)
    else:
        notes = desktop_install.install(settings, bookmarks=not args.no_bookmarks)
    for n in notes:
        print(n)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    s = config.Settings.load()
    changed = False
    if args.poll_interval is not None:
        s.poll_interval_s = max(10, args.poll_interval)
        changed = True
    if args.max_transfers is not None:
        s.max_transfers = max(1, args.max_transfers)
        changed = True
    if args.prefer_newer_on_rebuild is not None:
        s.prefer_newer_on_rebuild = args.prefer_newer_on_rebuild == "on"
        changed = True
    if args.full_check_hours is not None:
        s.full_check_hours = max(0.0, args.full_check_hours)
        changed = True
    if changed:
        s.save()
    print(f"config dir     : {config.CONFIG_DIR}")
    print(f"data dir       : {config.DATA_DIR}")
    print(f"ignore file    : {config.IGNORE_FILE}")
    print(f"operation log  : {config.OPS_LOG}")
    print(f"poll interval  : {s.poll_interval_s}s")
    print(f"max transfers  : {s.max_transfers}")
    print(f"rebuild: newer local wins : {'on' if s.prefer_newer_on_rebuild else 'off (conflicted copies)'}")
    print(f"full check     : " + (f"every {s.full_check_hours:g}h" if s.full_check_hours else "off"))
    print(f"synced folders : {len(s.pairs)} (see `gdrivesync pair list`)")
    if changed:
        print(RESTART_HINT)
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gdrivesync", description="Google Drive sync for Linux.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="sign in to Google Drive").set_defaults(func=cmd_login)
    sub.add_parser("logout", help="forget stored credentials").set_defaults(func=cmd_logout)
    sub.add_parser("whoami", help="show account and quota").set_defaults(func=cmd_whoami)

    ls = sub.add_parser("ls", help="list a Drive folder (by /path or id)")
    ls.add_argument("folder", nargs="?", default="/")
    ls.set_defaults(func=cmd_ls)

    pr = sub.add_parser("pair", help="manage synced folder pairs")
    prs = pr.add_subparsers(dest="pair_cmd", required=True)
    prs.add_parser("list", help="show synced folders").set_defaults(func=cmd_pair_list)
    pa = prs.add_parser("add", help="sync a local folder with a Drive folder")
    pa.add_argument("local", help="local folder, e.g. ~/Work")
    pa.add_argument("remote", help="Drive path, e.g. /Work (use / for all of My Drive)")
    pa.add_argument("--name", help="label for this pair (default: Drive folder name)")
    pa.add_argument("--create", action="store_true", help="create the Drive folder if missing")
    pa.set_defaults(func=cmd_pair_add)
    pm = prs.add_parser("remove", help="stop syncing a pair (files are left in place)")
    pm.add_argument("name")
    pm.set_defaults(func=cmd_pair_remove)
    rb = prs.add_parser("rebuild", help="rebuild a pair's sync state by comparing with Drive")
    rb.add_argument("name")
    rb.add_argument("-n", "--dry-run", action="store_true", help="show what would happen")
    newer = rb.add_mutually_exclusive_group()
    newer.add_argument("--prefer-newer", dest="prefer_newer", action="store_true", default=None,
                       help="upload local files newer than Drive as new versions "
                            "instead of keeping conflicted copies")
    newer.add_argument("--keep-both", dest="prefer_newer", action="store_false",
                       help="always keep differing local files as conflicted copies")
    rb.set_defaults(func=cmd_pair_rebuild)

    ex = sub.add_parser("exclude", help="stop/start syncing a subfolder (selective sync)")
    ex.add_argument("action", choices=["add", "remove", "list"])
    ex.add_argument("path", nargs="?", help="local folder path inside a synced folder")
    ex.set_defaults(func=cmd_exclude)

    pl = sub.add_parser("pull", help="download Drive into the local folders (no uploads)")
    pl.add_argument("--pair", help="only this pair")
    pl.add_argument("-n", "--dry-run", action="store_true", help="show what would happen")
    pl.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    pl.set_defaults(func=cmd_pull)

    sy = sub.add_parser("sync", help="run one two-way sync pass and exit")
    sy.add_argument("--pair", help="only this pair")
    sy.add_argument("-v", "--verbose", action="store_true")
    sy.add_argument("-n", "--dry-run", action="store_true",
                    help="show every change that would be made, without making any")
    sy.add_argument("--full", action="store_true",
                    help="compare everything with Drive instead of only recent changes")
    sy.add_argument("--allow-mass-delete", action="store_true",
                    help="allow trashing many files on Drive that disappeared locally")
    sy.set_defaults(func=cmd_sync)

    lg = sub.add_parser("log", help="show the log of operations performed")
    lg.add_argument("-n", type=int, default=50, help="how many recent entries (0 = all)")
    lg.add_argument("-f", "--follow", action="store_true", help="keep printing new entries")
    lg.add_argument("--pair", help="only this pair")
    lg.add_argument("--json", action="store_true", help="raw JSON lines")
    lg.set_defaults(func=cmd_log)

    dm = sub.add_parser("daemon", help="run the background sync service")
    dm.add_argument("-v", "--verbose", action="store_true")
    dm.set_defaults(func=cmd_daemon)

    dr = sub.add_parser("doctor", help="find duplicate files/folders on Drive")
    dr.add_argument("--fix", action="store_true", help="move the duplicates to the trash")
    dr.set_defaults(func=cmd_doctor)

    op = sub.add_parser("open", help="open a Google Docs link file or synced item in the browser")
    op.add_argument("path")
    op.add_argument("--print", action="store_true", help="print the URL instead of opening it")
    op.set_defaults(func=cmd_open)

    ins = sub.add_parser("install-desktop",
                         help="install the Nautilus extension, file types and sidebar bookmarks")
    ins.add_argument("--uninstall", action="store_true")
    ins.add_argument("--no-bookmarks", action="store_true", help="don't add sidebar bookmarks")
    ins.set_defaults(func=cmd_install_desktop)

    cfg = sub.add_parser("config", help="show or change settings")
    cfg.add_argument("--poll-interval", type=int, help="seconds between Drive checks")
    cfg.add_argument("--max-transfers", type=int, help="parallel downloads during pull")
    cfg.add_argument("--prefer-newer-on-rebuild", choices=["on", "off"],
                     help="when rebuilding sync state, upload newer local files as new versions")
    cfg.add_argument("--full-check-hours", type=float,
                     help="how often the service compares everything with Drive (0 = never)")
    cfg.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:  # surface expected problems without a traceback
        from gdrivesync.auth import AuthError
        from gdrivesync.engine import SyncError
        from gdrivesync.pairs import PairError
        from gdrivesync.pull import PullError
        from gdrivesync.state import PairBusy

        if isinstance(e, (AuthError, PullError, SyncError, PairError, PairBusy)):
            print(f"error: {e}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
