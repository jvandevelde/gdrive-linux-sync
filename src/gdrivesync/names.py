"""Mapping between Drive names and local filenames."""

from __future__ import annotations

import os

SLASH_SUBSTITUTE = "∕"  # DIVISION SLASH, looks like '/'

# Extensions for Google-native file stubs.
DOC_STUB_EXT = {
    "application/vnd.google-apps.document": ".gdoc",
    "application/vnd.google-apps.spreadsheet": ".gsheet",
    "application/vnd.google-apps.presentation": ".gslides",
    "application/vnd.google-apps.drawing": ".gdraw",
    "application/vnd.google-apps.form": ".gform",
    "application/vnd.google-apps.map": ".gmap",
    "application/vnd.google-apps.site": ".gsite",
    "application/vnd.google-apps.jam": ".gjam",
}
DEFAULT_STUB_EXT = ".glink"


def to_local(name: str) -> str:
    """Make a Drive name safe as a single Linux path component."""
    name = name.replace("/", SLASH_SUBSTITUTE).replace("\0", "")
    if name in ("", ".", ".."):
        name = name.replace(".", "．") or "_"
    return name


def to_remote(name: str) -> str:
    return name.replace(SLASH_SUBSTITUTE, "/")


def stub_name(name: str, mime: str) -> str:
    return to_local(name) + DOC_STUB_EXT.get(mime, DEFAULT_STUB_EXT)


def dedupe(name: str, taken: set[str]) -> str:
    """Return name, or 'name (2).ext', 'name (3).ext', ... if already taken."""
    if name not in taken:
        return name
    stem, ext = os.path.splitext(name)
    n = 2
    while f"{stem} ({n}){ext}" in taken:
        n += 1
    return f"{stem} ({n}){ext}"
