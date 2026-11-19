"""Paths and user settings (XDG-compliant)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP = "gdrivesync"


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default) / APP


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share")
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state")

CLIENT_SECRET_FILE = CONFIG_DIR / "client_secret.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
IGNORE_FILE = CONFIG_DIR / "ignore"
TOKEN_FILE = DATA_DIR / "token.json"
STATE_DB = DATA_DIR / "state.db"
OPS_LOG = STATE_DIR / "operations.log"

TMP_DIRNAME = ".gdrivesync-tmp"

BUILTIN_IGNORES = [
    TMP_DIRNAME,
    "*.part",
    "*.swp",
    "~$*",
    ".~lock.*#",
    ".goutputstream-*",
    "*~",
    ".#*",
    "*.tmp",
    ".directory",
]


def _slug(name: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    return "-".join(filter(None, out.split("-"))) or "pair"


@dataclass
class Pair:
    """One local folder kept in sync with one Drive folder."""

    name: str
    local: str
    remote_path: str = "/"  # for display; the id is what's synced
    remote_id: str = "root"  # "root" = all of My Drive
    excluded_folder_ids: list[str] = field(default_factory=list)
    db: str = ""  # state DB filename in DATA_DIR

    def __post_init__(self) -> None:
        if not self.db:
            self.db = f"pair-{_slug(self.name)}.db"

    @property
    def root(self) -> Path:
        return Path(self.local).expanduser()

    @property
    def db_path(self) -> Path:
        return DATA_DIR / self.db


@dataclass
class Settings:
    poll_interval_s: int = 45
    max_transfers: int = 4
    pairs: list[Pair] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        data = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
        s = cls(
            poll_interval_s=data.get("poll_interval_s", cls.poll_interval_s),
            max_transfers=data.get("max_transfers", cls.max_transfers),
            pairs=[Pair(**p) for p in data.get("pairs", [])],
        )
        if "pairs" not in data and ("sync_root" in data or STATE_DB.exists()):
            # Pre-pairs config: a single mirror of all of My Drive.
            s.pairs.append(
                Pair(
                    name="My Drive",
                    local=data.get("sync_root", str(Path.home() / "Google Drive")),
                    remote_path="/",
                    remote_id="root",
                    excluded_folder_ids=data.get("excluded_folder_ids", []),
                    db=STATE_DB.name,
                )
            )
        return s

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        os.replace(tmp, SETTINGS_FILE)

    def pair(self, name: str) -> Pair:
        for p in self.pairs:
            if p.name == name:
                return p
        raise KeyError(name)

    def pair_for_path(self, path: Path) -> Pair | None:
        path = path.expanduser().resolve()
        for p in self.pairs:
            root = p.root.resolve()
            if path == root or root in path.parents:
                return p
        return None
