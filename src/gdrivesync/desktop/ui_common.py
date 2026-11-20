"""Logic shared by the tray icon and the settings window. Pure Python (no GTK),
so it runs on any interpreter and is unit-tested directly."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass, field

APP_ID = "io.github.jvandevelde.GDriveSync"
BUS_NAME = APP_ID
OBJECT_PATH = "/io/github/jvandevelde/GDriveSync"
INTERFACE = BUS_NAME
SERVICE_UNIT = "gdrivesync.service"

ICONS = {
    "synced": "emblem-ok-symbolic",
    "syncing": "emblem-synchronizing-symbolic",
    "paused": "media-playback-pause-symbolic",
    "error": "dialog-warning-symbolic",
    "offline": "network-offline-symbolic",
}


def cli_command() -> list[str]:
    """How to run the gdrivesync CLI (set by `gdrivesync tray/settings`)."""
    cli = os.environ.get("GDRIVESYNC_CLI") or shutil.which("gdrivesync") or "gdrivesync"
    return shlex.split(cli)


def systemctl_command() -> list[str]:
    return shlex.split(os.environ.get("GDRIVESYNC_SYSTEMCTL", "systemctl")) + ["--user"]


def autostart_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "gdrivesync-tray.desktop")


def autostart_entry() -> str:
    cli = " ".join(shlex.quote(c) for c in cli_command())
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Google Drive Sync status icon",
        f"Exec={cli} tray",
        "Icon=folder-remote",
        "NoDisplay=true",
        "X-GNOME-Autostart-enabled=true",
        "X-GNOME-Autostart-Delay=5",
        "",
    ])


def sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


@dataclass
class Summary:
    state: str  # offline | paused | error | syncing | synced
    label: str
    detail: str = ""
    lines: list[str] = field(default_factory=list)  # one per folder pair

    @property
    def icon(self) -> str:
        return ICONS[self.state]


def summarize(state: dict | None) -> Summary:
    """Overall status from the service's GetState() (None = service not running)."""
    if state is None:
        return Summary("offline", "Sync service isn't running",
                       "Start it from the menu, or: systemctl --user start gdrivesync")
    pairs = state.get("pairs", [])
    errors = sum(len(p.get("errors", {})) for p in pairs)
    active = sum(len(p.get("active", [])) for p in pairs)
    stopped = [p["name"] for p in pairs if p.get("paused")]
    lines = []
    for p in pairs:
        bits = []
        if p.get("paused"):
            bits.append("stopped (see log)")
        if p.get("errors"):
            bits.append(plural(len(p["errors"]), "error"))
        if p.get("active"):
            bits.append(f"syncing {len(p['active'])}")
        lines.append(f"{p['name']}: {', '.join(bits) or 'up to date'}")
    if state.get("paused"):
        return Summary("paused", "Syncing is paused", "Resume to continue syncing", lines)
    if errors or stopped:
        label = plural(errors, "item") + " couldn't sync" if errors else \
            f"{', '.join(stopped)} stopped syncing"
        return Summary("error", label, "Open settings for details", lines)
    if active:
        return Summary("syncing", f"Syncing {plural(active, 'item')}…", "", lines)
    return Summary("synced", "Up to date", "", lines)


class NotificationTracker:
    """Turns successive GetState() snapshots into desktop notifications for
    things worth interrupting for: new conflicts, new errors, service lost."""

    def __init__(self):
        self._started = False
        self._seen_ops: set = set()
        self._errors: set = set()
        self._online = False

    def update(self, state: dict | None) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if state is None:
            if self._online:
                out.append(("Google Drive sync stopped",
                            "The sync service isn't running; changes aren't being synced."))
            self._online = False
            return out
        was_online, self._online = self._online, True

        ops = {(e.get("ts"), e.get("op"), e.get("path"), e.get("pair")): e
               for e in state.get("recent", [])}
        errors = {(p["name"], path): msg
                  for p in state.get("pairs", []) for path, msg in p.get("errors", {}).items()}

        if self._started:
            for key, e in ops.items():
                if key in self._seen_ops or e.get("op") != "conflict":
                    continue
                out.append((
                    "Edited in two places",
                    f"“{e.get('src', '')}” changed both here and on Google Drive. "
                    f"Your version was kept as “{os.path.basename(e.get('path', ''))}”.",
                ))
            new_errors = [k for k in errors if k not in self._errors]
            if len(new_errors) == 1:
                (pair, path), = new_errors
                out.append((f"Couldn't sync “{os.path.basename(path)}”", errors[(pair, path)]))
            elif new_errors:
                names = ", ".join(os.path.basename(p) for _, p in new_errors[:3])
                more = f" and {len(new_errors) - 3} more" if len(new_errors) > 3 else ""
                out.append((f"Couldn't sync {len(new_errors)} items", names + more))
            if not was_online:
                pass  # coming back online isn't worth a notification
        self._seen_ops |= set(ops)
        self._errors = set(errors)
        self._started = True
        return out


def short_time(iso: str) -> str:
    """'2026-11-20T10:15:03+00:00' -> local 'HH:MM' (or the date if not today)."""
    from datetime import datetime

    try:
        t = datetime.fromisoformat(iso).astimezone()
    except (TypeError, ValueError):
        return ""
    now = datetime.now().astimezone()
    return t.strftime("%H:%M") if t.date() == now.date() else t.strftime("%b %d")


# ---------------------------------------------------------------- log viewer

LOG_LINES = 500
PROBLEM_OPS = {"conflict", "trash_remote", "trash_local", "delete_local"}
_LEVEL_WORD = re.compile(r"\b(CRITICAL|ERROR|WARNING)\b")
_MODULE_PREFIX = re.compile(r"^gdrivesync(\.\w+)*: ")


def journal_argv(lines: int = LOG_LINES) -> list[str]:
    cmd = shlex.split(os.environ.get("GDRIVESYNC_JOURNALCTL", "journalctl"))
    return cmd + ["--user", "--unit", SERVICE_UNIT, "--no-pager", "--lines", str(lines),
                  "--follow", "--output", "json"]


def ops_argv(lines: int = LOG_LINES) -> list[str]:
    return cli_command() + ["log", "--follow", "--json", "-n", str(lines)]


@dataclass
class LogEntry:
    ts: float  # unix time
    level: str  # error | warning | info
    text: str
    problem: bool


def parse_journal_line(line: str) -> LogEntry | None:
    """One `journalctl --output json` line. Non-JSON lines (journalctl's own
    messages, e.g. 'No entries') are kept as plain text."""
    import json
    import time

    if not line.strip():
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return LogEntry(time.time(), "info", line.strip(), False)
    msg = d.get("MESSAGE", "")
    if isinstance(msg, list):  # binary-safe encoding for non-UTF-8 messages
        msg = bytes(msg).decode("utf-8", "replace")
    elif msg is None:
        msg = ""
    try:
        prio = int(d.get("PRIORITY", 6))
    except (TypeError, ValueError):
        prio = 6
    if prio == 6:  # older lines logged without a level prefix
        m = _LEVEL_WORD.search(msg[:60])
        if m:
            prio = 4 if m.group(1) == "WARNING" else 3
    try:
        ts = int(d.get("__REALTIME_TIMESTAMP", 0)) / 1e6
    except (TypeError, ValueError):
        ts = 0.0
    level = "error" if prio <= 3 else "warning" if prio == 4 else "info"
    msg = _MODULE_PREFIX.sub("", msg, count=1)  # "gdrivesync.ops: …" → "…"
    return LogEntry(ts, level, msg, prio <= 4)


def parse_ops_line(line: str) -> LogEntry | None:
    """One `gdrivesync log --json` line."""
    import json
    from datetime import datetime

    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    try:
        ts = datetime.fromisoformat(d.get("ts", "")).timestamp()
    except ValueError:
        ts = 0.0
    op = d.get("op", "")
    text = sentence(d.get("text") or f"{op} {d.get('path', '')}")
    if d.get("pair"):
        text = f"[{d['pair']}] {text}"
    return LogEntry(ts, "warning" if op == "conflict" else "info", text, op in PROBLEM_OPS)


def log_time(ts: float) -> str:
    from datetime import datetime

    if not ts:
        return ""
    t = datetime.fromtimestamp(ts)
    return t.strftime("%H:%M:%S") if t.date() == datetime.now().date() else \
        t.strftime("%b %d %H:%M")
