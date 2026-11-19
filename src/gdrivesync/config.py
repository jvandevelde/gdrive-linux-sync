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

CLIENT_SECRET_FILE = CONFIG_DIR / "client_secret.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
IGNORE_FILE = CONFIG_DIR / "ignore"
TOKEN_FILE = DATA_DIR / "token.json"
STATE_DB = DATA_DIR / "state.db"

TMP_DIRNAME = ".gdrivesync-tmp"

BUILTIN_IGNORES = [
    TMP_DIRNAME,
    "*.part",
    "*.swp",
    "~$*",
    ".~lock.*#",
    ".goutputstream-*",
]


@dataclass
class Settings:
    sync_root: str = str(Path.home() / "Google Drive")
    poll_interval_s: int = 45
    max_transfers: int = 4
    excluded_folder_ids: list[str] = field(default_factory=list)

    @property
    def root(self) -> Path:
        return Path(self.sync_root).expanduser()

    @classmethod
    def load(cls) -> "Settings":
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text())
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=2) + "\n")
