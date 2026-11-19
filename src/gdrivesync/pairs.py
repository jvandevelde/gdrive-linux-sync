"""Managing local ↔ Drive folder pairs: resolving Drive paths and validation."""

from __future__ import annotations

from pathlib import Path

from gdrivesync import config
from gdrivesync.config import Pair, Settings
from gdrivesync.drive import FOLDER_MIME


class PairError(ValueError):
    pass


def normalize_remote_path(path: str) -> str:
    parts = [p for p in path.strip().split("/") if p]
    return "/" + "/".join(parts)


def resolve_remote_path(client, path: str, *, create: bool = False) -> str:
    """Folder id for a My Drive path like "/Work/Reports" ("/" = My Drive root)."""
    folder_id = client.root_id()
    walked = ""
    for part in [p for p in normalize_remote_path(path).split("/") if p]:
        walked += "/" + part
        matches = [c for c in client.find_children(folder_id, part) if c["mimeType"] == FOLDER_MIME]
        if len(matches) > 1:
            raise PairError(f"{walked!r} is ambiguous: Drive has {len(matches)} folders with that name.")
        if matches:
            folder_id = matches[0]["id"]
        elif create:
            folder_id = client.create_folder(part, folder_id)["id"]
        else:
            raise PairError(f"No folder {walked!r} on Drive (use --create to make it).")
    return folder_id


def remote_lineage(client, folder_id: str) -> list[str]:
    """[folder_id, parent, grandparent, …, My Drive root]."""
    root = client.root_id()
    chain = [folder_id]
    seen = {folder_id}
    while chain[-1] != root:
        f = client.get_file(chain[-1])
        parents = (f or {}).get("parents") or []
        if not parents or f.get("trashed"):
            raise PairError("That Drive folder isn't in My Drive (or is in the trash).")
        if parents[0] in seen:
            break
        chain.append(parents[0])
        seen.add(parents[0])
    return chain


def validate_new_pair(client, settings: Settings, pair: Pair) -> None:
    if any(p.name == pair.name for p in settings.pairs):
        raise PairError(f"A pair named {pair.name!r} already exists.")
    if any(p.db == pair.db for p in settings.pairs):
        raise PairError(f"Pick a different name; {pair.name!r} is too similar to an existing pair.")

    local = pair.root.resolve()
    if local in (Path.home().resolve(), Path("/")):
        raise PairError(f"Can't sync {local}: choose a dedicated folder.")
    for own in (config.CONFIG_DIR.resolve(), config.DATA_DIR.resolve()):
        if local == own or local in own.parents or own in local.parents:
            raise PairError(f"Can't sync {local}: it overlaps gdrivesync's own files ({own}).")
    if local.exists() and not local.is_dir():
        raise PairError(f"{local} exists and isn't a folder.")
    for other in settings.pairs:
        o = other.root.resolve()
        if local == o or o in local.parents or local in o.parents:
            raise PairError(f"{local} overlaps the local folder of pair {other.name!r} ({o}).")

    root = client.root_id()
    new_id = root if pair.remote_id == "root" else pair.remote_id
    new_chain = remote_lineage(client, new_id)
    for other in settings.pairs:
        other_id = root if other.remote_id == "root" else other.remote_id
        if other_id in new_chain or new_id in remote_lineage(client, other_id):
            raise PairError(
                f"Drive folder {pair.remote_path!r} overlaps pair {other.name!r} "
                f"({other.remote_path!r}). Pairs can't be nested; exclude a subfolder instead."
            )


# --------------------------------------------------------------------------- exclusions


def _locate(settings: Settings, path: Path) -> tuple[Pair, str]:
    pair = settings.pair_for_path(path)
    if pair is None:
        raise PairError(f"{path} isn't inside any synced folder.")
    rel = str(path.expanduser().resolve().relative_to(pair.root.resolve()))
    if rel == ".":
        raise PairError("That's the pair's top folder; use `gdrivesync pair remove` instead.")
    return pair, rel


def exclude_path(settings: Settings, path: Path) -> Pair:
    """Mark a synced local folder as excluded (takes effect on the next sync pass)."""
    from gdrivesync.state import StateDB

    pair, rel = _locate(settings, path)
    db = StateDB(pair.db_path)
    try:
        item = db.by_path(rel)
    finally:
        db.close()
    if item is None:
        raise PairError(f"{rel!r} isn't synced yet (wait for it to upload first).")
    if not item.is_folder:
        raise PairError("Only folders can be excluded; use the ignore file for single files.")
    if item.file_id not in pair.excluded_folder_ids:
        pair.excluded_folder_ids.append(item.file_id)
    return pair


def include_path(settings: Settings, path: Path) -> Pair:
    """Undo exclude_path: the folder is downloaded again on the next pass."""
    import json

    from gdrivesync.engine import EXCLUDED_KEY
    from gdrivesync.state import StateDB

    pair, rel = _locate(settings, path)
    db = StateDB(pair.db_path)
    try:
        paths = json.loads(db.get(EXCLUDED_KEY) or "{}")
        pending = db.by_path(rel)  # excluded, but local copy not removed yet
    finally:
        db.close()
    ids = [fid for fid, p in paths.items() if p == rel and fid in pair.excluded_folder_ids]
    if pending and pending.file_id in pair.excluded_folder_ids:
        ids.append(pending.file_id)
    if not ids:
        raise PairError(f"{rel!r} isn't an excluded folder of pair {pair.name!r}.")
    pair.excluded_folder_ids = [f for f in pair.excluded_folder_ids if f not in ids]
    return pair


def excluded_listing(pair: Pair) -> list[str]:
    import json

    from gdrivesync.engine import EXCLUDED_KEY
    from gdrivesync.state import StateDB

    if not pair.db_path.exists():
        return list(pair.excluded_folder_ids)
    db = StateDB(pair.db_path)
    try:
        paths = json.loads(db.get(EXCLUDED_KEY) or "{}")
        out = []
        for fid in pair.excluded_folder_ids:
            item = db.by_id(fid)
            out.append(paths.get(fid) or (item.rel_path + " (removal pending)" if item else fid))
        return out
    finally:
        db.close()
