"""`gdrivesync install-desktop`: hook gdrivesync into the GNOME desktop.

* Nautilus extension → ~/.local/share/nautilus-python/extensions/
* file types for the .gdoc/.gsheet/… link files + a handler that opens them in
  the browser (double-click in Nautilus)
* a Nautilus sidebar bookmark per synced folder

Everything goes in the user's home; `--uninstall` removes it again.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from gdrivesync import config

MIME_TYPES = [
    "application/x-gdrivesync-gdoc",
    "application/x-gdrivesync-gsheet",
    "application/x-gdrivesync-gslides",
    "application/x-gdrivesync-gdraw",
    "application/x-gdrivesync-gform",
    "application/x-gdrivesync-glink",
]
DESKTOP_ID = "gdrivesync-open.desktop"
APP_ID = "io.github.jvandevelde.GDriveSync"
BOOKMARK_TAG = "gdrivesync"


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def paths() -> dict[str, Path]:
    data = _data_home()
    return {
        "extension": data / "nautilus-python/extensions/gdrivesync_nautilus.py",
        "mime": data / "mime/packages/gdrivesync.xml",
        "desktop": data / "applications" / DESKTOP_ID,
        "bookmarks": _config_home() / "gtk-3.0/bookmarks",  # GTK 4 Nautilus reads it too
        "app": data / "applications" / f"{APP_ID}.desktop",
        "autostart": _config_home() / "autostart/gdrivesync-tray.desktop",
    }


def desktop_dir() -> Path:
    return Path(str(resources.files("gdrivesync") / "desktop"))


def system_python() -> str | None:
    """The distro Python that has PyGObject (the venv's Python usually doesn't)."""
    for exe in ("/usr/bin/python3", "/usr/bin/python3.12", "/usr/bin/python3.13",
                "/usr/bin/python3.11", "/usr/bin/python3.10"):
        if os.path.exists(exe) and subprocess.run(
            [exe, "-c", "import gi"], capture_output=True
        ).returncode == 0:
            return exe
    return None


def cli_exe() -> str:
    found = shutil.which("gdrivesync")
    if found:
        return found
    candidate = Path(sys.executable).parent / "gdrivesync"
    return str(candidate) if candidate.exists() else "gdrivesync"


def app_entry(cli: str) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Google Drive Sync",
        "Comment=Settings and status for gdrivesync",
        f"Exec={cli} settings",
        "Icon=folder-remote",
        "Categories=Network;FileTransfer;GTK;",
        "Keywords=google;drive;sync;cloud;",
        "StartupNotify=true",
        "",
    ])


def tray_autostart_entry(cli: str) -> str:
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


def nautilus_python_installed() -> bool:
    patterns = [
        "/usr/lib/*/nautilus/extensions-4/libnautilus-python.so",
        "/usr/lib/nautilus/extensions-4/libnautilus-python.so",
        "/usr/lib*/nautilus/extensions-3.0/libnautilus-python.so",
        "/usr/lib/*/nautilus/extensions-3.0/libnautilus-python.so",
    ]
    return any(glob.glob(p) for p in patterns)


def _gdrivesync_exe() -> str:
    found = shutil.which("gdrivesync")
    return found or f"{sys.executable} -m gdrivesync"


def _run(cmd: list[str]) -> None:
    if shutil.which(cmd[0]):
        subprocess.run(cmd, check=False, capture_output=True)


def _desktop_entry() -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Google Drive (gdrivesync)",
        "Comment=Open Google Docs, Sheets and Slides links in the browser",
        f"Exec={_gdrivesync_exe()} open %f",
        "Icon=web-browser",
        "NoDisplay=true",
        "Terminal=false",
        "MimeType=" + ";".join(MIME_TYPES) + ";",
        "",
    ])


def _bookmark_lines(settings: config.Settings) -> list[str]:
    return [f"{p.root.resolve().as_uri()} {p.name}" for p in settings.pairs]


def install(settings: config.Settings, *, bookmarks: bool = True,
            tray_autostart: bool = True) -> list[str]:
    """Install everything; returns human-readable notes."""
    notes = []
    p = paths()
    pkg = resources.files("gdrivesync") / "desktop"

    p["extension"].parent.mkdir(parents=True, exist_ok=True)
    p["extension"].write_text((pkg / "gdrivesync_nautilus.py").read_text())
    notes.append(f"Nautilus extension → {p['extension']}")

    p["mime"].parent.mkdir(parents=True, exist_ok=True)
    p["mime"].write_text((pkg / "gdrivesync-mime.xml").read_text())
    _run(["update-mime-database", str(_data_home() / "mime")])
    p["desktop"].parent.mkdir(parents=True, exist_ok=True)
    p["desktop"].write_text(_desktop_entry())
    _run(["update-desktop-database", str(p["desktop"].parent)])
    for mt in MIME_TYPES:
        _run(["xdg-mime", "default", DESKTOP_ID, mt])
    notes.append("Google Docs link files (.gdoc, .gsheet, …) now open in the browser")

    cli = cli_exe()
    p["app"].write_text(app_entry(cli))
    notes.append("“Google Drive Sync” settings app added to the app grid")
    if tray_autostart:
        p["autostart"].parent.mkdir(parents=True, exist_ok=True)
        p["autostart"].write_text(tray_autostart_entry(cli))
        notes.append("Status icon starts at login (start it now: gdrivesync tray &)")

    if bookmarks and settings.pairs:
        existing = p["bookmarks"].read_text().splitlines() if p["bookmarks"].exists() else []
        known = {line.split(" ", 1)[0] for line in existing}
        added = [b for b in _bookmark_lines(settings) if b.split(" ", 1)[0] not in known]
        if added:
            p["bookmarks"].parent.mkdir(parents=True, exist_ok=True)
            p["bookmarks"].write_text("\n".join(existing + added) + "\n")
            notes.append(f"Sidebar bookmarks added: {', '.join(b.split(' ', 1)[1] for b in added)}")

    if not nautilus_python_installed():
        notes.append("⚠ python3-nautilus isn't installed — the extension needs it: "
                     "sudo apt install python3-nautilus")
    notes.append("Restart Nautilus to load the extension: nautilus -q")
    return notes


def uninstall(settings: config.Settings) -> list[str]:
    p = paths()
    removed = []
    for key in ("extension", "mime", "desktop", "app", "autostart"):
        if p[key].exists():
            p[key].unlink()
            removed.append(str(p[key]))
    _run(["update-mime-database", str(_data_home() / "mime")])
    _run(["update-desktop-database", str(p["desktop"].parent)])
    if p["bookmarks"].exists():
        ours = {b.split(" ", 1)[0] for b in _bookmark_lines(settings)}
        lines = p["bookmarks"].read_text().splitlines()
        kept = [line for line in lines if line.split(" ", 1)[0] not in ours]
        if kept != lines:
            p["bookmarks"].write_text("\n".join(kept) + ("\n" if kept else ""))
            removed.append("sidebar bookmarks")
    return [f"removed {r}" for r in removed] + ["Restart Nautilus: nautilus -q"]
