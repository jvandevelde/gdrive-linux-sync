"""Find (and optionally remove) duplicate items on Drive.

Duplicates here means: same folder, same name, same content (MD5) — or, for
folders, same name where the extras are empty. These are what an interrupted
create produced before the create journal existed, and can't be told apart
from a user's intent in any useful way.
"""

from __future__ import annotations

import logging
import posixpath
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from gdrivesync.localfs import trash_local
from gdrivesync.names import to_local
from gdrivesync.pull import unchanged_since
from gdrivesync.state import Item, StateDB

log = logging.getLogger("gdrivesync.doctor")


@dataclass
class DuplicateGroup:
    keep: Item
    extras: list[Item]


def _keeper(items: list[Item]) -> Item:
    # Prefer the one holding the plain local name (no " (2)" suffix).
    plain = [i for i in items if posixpath.basename(i.rel_path) == to_local(i.name)]
    return min(plain or items, key=lambda i: (len(i.rel_path), i.rel_path))


def find_duplicates(db: StateDB) -> list[DuplicateGroup]:
    files: dict[tuple, list[Item]] = defaultdict(list)
    folders: dict[tuple, list[Item]] = defaultdict(list)
    for it in db.all():
        if it.is_folder:
            folders[(it.parent_id, it.name)].append(it)
        elif it.md5:
            files[(it.parent_id, it.name, it.md5)].append(it)

    groups = []
    for items in files.values():
        if len(items) > 1:
            keep = _keeper(items)
            groups.append(DuplicateGroup(keep, [i for i in items if i is not keep]))
    for items in folders.values():
        if len(items) > 1:
            empty = [i for i in items if not db.descendants(i.rel_path)]
            full = [i for i in items if i not in empty]
            keep = _keeper(full) if full else _keeper(items)
            extras = [i for i in empty if i is not keep]
            if extras:
                groups.append(DuplicateGroup(keep, extras))
    return sorted(groups, key=lambda g: g.keep.rel_path)


def fix_duplicates(client, db: StateDB, root: Path, groups: list[DuplicateGroup]) -> int:
    """Trash the extras on Drive and locally. Returns how many were removed."""
    removed = 0
    for g in groups:
        for it in g.extras:
            client.trash(it.file_id)
            p = root / it.rel_path
            if p.is_dir() and not any(p.iterdir()):
                trash_local(p)
            elif p.is_file() and unchanged_since(it, p.stat()):
                trash_local(p)
            else:
                log.warning("left local %s in place (changed since sync)", it.rel_path)
            db.delete(it.file_id)
            removed += 1
            log.info("removed duplicate %s (kept %s)", it.rel_path, g.keep.rel_path)
    return removed
