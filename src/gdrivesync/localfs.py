"""Local filesystem helpers: ignore rules, scanning, trashing."""

from __future__ import annotations

import fnmatch
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from gdrivesync import config


class IgnoreRules:
    """gitignore-ish: a pattern without '/' matches any path component;
    a pattern with '/' matches the path relative to the sync root (or a prefix of it)."""

    def __init__(self, patterns: Iterable[str]):
        self.component: list[str] = []
        self.anchored: list[str] = []
        for raw in patterns:
            p = raw.strip()
            if not p or p.startswith("#"):
                continue
            p = p.strip("/") if "/" in p.rstrip("/") else p.rstrip("/")
            (self.anchored if "/" in p else self.component).append(p)

    @classmethod
    def load(cls) -> "IgnoreRules":
        patterns = list(config.BUILTIN_IGNORES)
        if config.IGNORE_FILE.exists():
            patterns += config.IGNORE_FILE.read_text().splitlines()
        return cls(patterns)

    def ignored(self, rel: str) -> bool:
        parts = rel.split("/")
        for pat in self.component:
            if any(fnmatch.fnmatchcase(part, pat) for part in parts):
                return True
        for pat in self.anchored:
            for i in range(1, len(parts) + 1):
                if fnmatch.fnmatchcase("/".join(parts[:i]), pat):
                    return True
        return False


def scan(root: Path, rules: IgnoreRules) -> dict[str, os.stat_result]:
    """Every non-ignored regular file and directory under root, keyed by relative
    path. Symlinks and special files are skipped."""
    out: dict[str, os.stat_result] = {}

    def walk(path: str, prefix: str) -> None:
        try:
            entries = list(os.scandir(path))
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return
        for e in entries:
            rel = prefix + e.name
            if rules.ignored(rel):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                out[rel] = st
                walk(e.path, rel + "/")
            elif stat.S_ISREG(st.st_mode):
                out[rel] = st

    walk(str(root), "")
    return out


def is_dir_stat(st: os.stat_result) -> bool:
    return stat.S_ISDIR(st.st_mode)


def trash_local(path: Path) -> None:
    """Move to the desktop trash (recoverable). Falls back to a private trash dir."""
    try:
        subprocess.run(["gio", "trash", str(path)], check=True, capture_output=True)
        return
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    dest = config.DATA_DIR / "trash" / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / path.name
    n = 2
    while target.exists():
        target = dest / f"{path.name}.{n}"
        n += 1
    shutil.move(str(path), target)
