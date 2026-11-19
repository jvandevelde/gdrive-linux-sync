"""Nautilus extension for gdrivesync (placeholder — milestone 3).

Runs inside Nautilus with the *system* Python: only `gi` imports allowed.
Install: copy to ~/.local/share/nautilus-python/extensions/ (needs python3-nautilus),
then `nautilus -q`.
"""

from gi.repository import GObject, Nautilus  # noqa: F401


class GDriveSyncExtension(GObject.GObject, Nautilus.MenuProvider):
    """Will provide status emblems (InfoProvider) and context-menu actions
    by talking to the gdrivesync daemon over D-Bus. Not implemented yet."""

    def get_file_items(self, files):
        return []
