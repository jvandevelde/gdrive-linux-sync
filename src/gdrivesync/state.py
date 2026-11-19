"""SQLite state DB: the last agreed ("base") state of each synced item."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    file_id         TEXT PRIMARY KEY,
    parent_id       TEXT,
    name            TEXT NOT NULL,       -- remote name
    rel_path        TEXT NOT NULL UNIQUE,-- local path relative to sync root
    is_folder       INTEGER NOT NULL,
    mime_type       TEXT NOT NULL,
    md5             TEXT,                -- remote md5 at last sync (NULL for folders/docs)
    size            INTEGER,
    remote_rev      TEXT,                -- headRevisionId at last sync
    remote_mtime    TEXT,                -- RFC3339 modifiedTime at last sync
    local_mtime_ns  INTEGER,             -- local st_mtime_ns at last sync
    local_size      INTEGER
);
CREATE INDEX IF NOT EXISTS items_parent ON items(parent_id);

-- Write-ahead journal for creates on Drive. A row is written (with a
-- pre-generated Drive file id) *before* the create call and removed once the
-- item is recorded in `items`, so a crash or retry in between can never
-- produce a duplicate: the next attempt reuses the same id.
CREATE TABLE IF NOT EXISTS pending_creates (
    rel_path    TEXT PRIMARY KEY,
    file_id     TEXT NOT NULL,
    parent_id   TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass
class Item:
    file_id: str
    parent_id: str | None
    name: str
    rel_path: str
    is_folder: bool
    mime_type: str
    md5: str | None = None
    size: int | None = None
    remote_rev: str | None = None
    remote_mtime: str | None = None
    local_mtime_ns: int | None = None
    local_size: int | None = None


_COLS = list(Item.__dataclass_fields__)


class StateDB:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.set("schema_version", str(SCHEMA_VERSION))

    def snapshot(self) -> "StateDB":
        """An in-memory copy (for dry runs: decisions proceed, nothing persists)."""
        copy = StateDB(":memory:")
        self.conn.backup(copy.conn)
        return copy

    def close(self) -> None:
        self.conn.close()

    # --- key/value ---------------------------------------------------------
    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    @property
    def page_token(self) -> str | None:
        return self.get("page_token")

    @page_token.setter
    def page_token(self, token: str) -> None:
        self.set("page_token", token)

    # --- items -------------------------------------------------------------
    def upsert(self, item: Item) -> None:
        placeholders = ", ".join("?" for _ in _COLS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in _COLS if c != "file_id")
        with self.conn:
            # A different item may still claim this path (e.g. remote rename/swap).
            self.conn.execute(
                "DELETE FROM items WHERE rel_path = ? AND file_id != ?",
                (item.rel_path, item.file_id),
            )
            self.conn.execute(
                f"INSERT INTO items({', '.join(_COLS)}) VALUES({placeholders}) "
                f"ON CONFLICT(file_id) DO UPDATE SET {updates}",
                [getattr(item, c) for c in _COLS],
            )

    def delete(self, file_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM items WHERE file_id = ?", (file_id,))

    def by_id(self, file_id: str) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE file_id = ?", (file_id,)).fetchone()
        return self._row(row)

    def by_path(self, rel_path: str) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE rel_path = ?", (rel_path,)).fetchone()
        return self._row(row)

    def children(self, parent_id: str) -> list[Item]:
        rows = self.conn.execute("SELECT * FROM items WHERE parent_id = ?", (parent_id,))
        return [self._row(r) for r in rows]

    # --- create journal ----------------------------------------------------
    def add_pending(self, rel_path: str, file_id: str, parent_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO pending_creates VALUES (?, ?, ?, ?)",
                (rel_path, file_id, parent_id, time.time()),
            )

    def pending(self, rel_path: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pending_creates WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        return dict(row) if row else None

    def all_pending(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM pending_creates")]

    def remove_pending(self, rel_path: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM pending_creates WHERE rel_path = ?", (rel_path,))

    def descendants(self, rel_path: str) -> list[Item]:
        """All items below folder rel_path (not including it)."""
        rows = self.conn.execute(
            "SELECT * FROM items WHERE substr(rel_path, 1, ?) = ?",
            (len(rel_path) + 1, rel_path + "/"),
        )
        return [self._row(r) for r in rows]

    def in_dir(self, dir_rel: str) -> list[Item]:
        """Items directly inside local folder dir_rel ("" = sync root)."""
        if dir_rel == "":
            rows = self.conn.execute("SELECT * FROM items WHERE instr(rel_path, '/') = 0")
        else:
            rows = self.conn.execute(
                "SELECT * FROM items WHERE substr(rel_path, 1, ?) = ? "
                "AND instr(substr(rel_path, ?), '/') = 0",
                (len(dir_rel) + 1, dir_rel + "/", len(dir_rel) + 2),
            )
        return [self._row(r) for r in rows]

    def move_prefix(self, old: str, new: str) -> None:
        """Re-path a folder and everything below it."""
        moved = [self.by_path(old)] if self.by_path(old) else []
        moved += self.descendants(old)
        with self.conn:
            for it in moved:
                new_path = new + it.rel_path[len(old):]
                self.conn.execute(
                    "DELETE FROM items WHERE rel_path = ? AND file_id != ?", (new_path, it.file_id)
                )
                self.conn.execute(
                    "UPDATE items SET rel_path = ? WHERE file_id = ?", (new_path, it.file_id)
                )

    def all(self) -> list[Item]:
        return [self._row(r) for r in self.conn.execute("SELECT * FROM items")]

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Item | None:
        if row is None:
            return None
        data = dict(row)
        data["is_folder"] = bool(data["is_folder"])
        return Item(**data)
