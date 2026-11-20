"""Nautilus integration for gdrivesync.

Installed by `gdrivesync install-desktop` into
~/.local/share/nautilus-python/extensions/ (needs the python3-nautilus package).
Runs inside Nautilus on the system Python, so it uses only PyGObject and talks to
the gdrivesync service over D-Bus. Every call is asynchronous: if the service is
slow or not running, files simply show no sync status.

Provides:
  * an emblem per file/folder (up to date / syncing / error / conflict),
  * a "Sync status" list-view column,
  * context menu: Open in Google Drive, Copy Google Drive link,
    Remove local copy (keep on Drive)…, and "Sync now" on folder backgrounds.
"""

from __future__ import annotations

import os
from collections import OrderedDict

import gi

try:
    gi.require_version("Nautilus", "4.0")
except ValueError:
    gi.require_version("Nautilus", "3.0")
from gi.repository import Gio, GLib, GObject, Nautilus  # noqa: E402

NAUTILUS_4 = str(getattr(Nautilus, "_version", "4.0")).startswith("4")

BUS_NAME = "io.github.jvandevelde.GDriveSync"
OBJECT_PATH = "/io/github/jvandevelde/GDriveSync"
INTERFACE = BUS_NAME
CALL_TIMEOUT_MS = 3000
STATUS_ATTRIBUTE = "gdrivesync_status"

EMBLEMS = {
    "synced": "emblem-default",
    "syncing": "emblem-synchronizing",
    "error": "emblem-important",
    "conflict": "emblem-important",
}
LABELS = {
    "synced": "Up to date",
    "syncing": "Syncing…",
    "error": "Sync error",
    "conflict": "Conflicted copy",
    "ignored": "Not synced",
}


def file_path(file) -> str | None:
    loc = file.get_location()
    return loc.get_path() if loc is not None else None


def remote_message(message: str) -> str:
    """'GDBus.Error:some.Name: text' -> 'text'."""
    if message.startswith("GDBus.Error:") and ": " in message:
        return message.split(": ", 1)[1]
    return message


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


class SyncClient:
    """Async D-Bus client that follows the service coming and going."""

    MAX_KNOWN = 5000

    def __init__(self):
        self.proxy = None
        self.roots: list[str] = []
        self._known: OrderedDict = OrderedDict()  # path -> Nautilus.FileInfo
        self._watch = Gio.bus_watch_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameWatcherFlags.NONE,
            self._appeared, self._vanished,
        )

    # -------------------------------------------------- connection
    def _appeared(self, connection, _name, _owner):
        Gio.DBusProxy.new(
            connection,
            Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES | Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None, BUS_NAME, OBJECT_PATH, INTERFACE, None, self._proxy_ready,
        )

    def _proxy_ready(self, _source, result):
        try:
            proxy = Gio.DBusProxy.new_finish(result)
        except GLib.Error:
            return
        proxy.connect("g-signal", self._on_signal)
        self.proxy = proxy
        self.call("GetRoots", None, self._roots_loaded)

    def _vanished(self, _connection, _name):
        self.proxy = None
        self.roots = []
        self._invalidate_all()  # clear emblems

    def _roots_loaded(self, result, _error):
        if result is not None:
            self.roots = list(result[0])
            self._invalidate_all()

    # -------------------------------------------------- calls
    def call(self, method, params, callback=None):
        """callback(result_tuple_or_None, error_message_or_None)."""
        if self.proxy is None:
            if callback:
                callback(None, "The gdrivesync service isn't running.")
            return

        def done(proxy, res):
            try:
                out, err = proxy.call_finish(res).unpack(), None
            except GLib.Error as e:
                out, err = None, remote_message(e.message)
            if callback:
                callback(out, err)

        self.proxy.call(method, params, Gio.DBusCallFlags.NO_AUTO_START,
                        CALL_TIMEOUT_MS, None, done)

    def _on_signal(self, _proxy, _sender, signal, params):
        if signal == "StatusChanged":
            for path in params.unpack()[0]:
                f = self._known.get(path)
                if f is not None:
                    f.invalidate_extension_info()

    # -------------------------------------------------- bookkeeping
    def in_roots(self, path: str | None) -> bool:
        return bool(path) and any(under(path, r) for r in self.roots)

    def is_root(self, path: str | None) -> bool:
        return path in self.roots

    def remember(self, path: str, file) -> None:
        self._known[path] = file
        self._known.move_to_end(path)
        while len(self._known) > self.MAX_KNOWN:
            self._known.popitem(last=False)

    def _invalidate_all(self):
        for f in list(self._known.values()):
            f.invalidate_extension_info()


_client: SyncClient | None = None


def client() -> SyncClient:
    global _client
    if _client is None:
        _client = SyncClient()
    return _client


# ------------------------------------------------------------------ UI helpers


def _gtk():
    gi.require_version("Gtk", "4.0" if NAUTILUS_4 else "3.0")
    from gi.repository import Gtk

    return Gtk


def copy_to_clipboard(text: str) -> None:
    Gtk = _gtk()
    if NAUTILUS_4:
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk

        cb = Gdk.Display.get_default().get_clipboard()
        try:
            cb.set(text)
        except (AttributeError, TypeError):
            cb.set_content(Gdk.ContentProvider.new_for_value(text))
    else:
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)


def show_message(title: str, detail: str = "") -> None:
    Gtk = _gtk()
    if hasattr(Gtk, "AlertDialog"):
        dlg = Gtk.AlertDialog()
        dlg.set_message(title)
        dlg.set_detail(detail)
        dlg.show(None)
    else:
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK, text=title)
        dlg.format_secondary_text(detail)
        dlg.connect("response", lambda d, _r: d.destroy())
        dlg.show()


def confirm(title: str, detail: str, action: str, on_yes) -> None:
    Gtk = _gtk()
    if hasattr(Gtk, "AlertDialog"):
        dlg = Gtk.AlertDialog()
        dlg.set_message(title)
        dlg.set_detail(detail)
        dlg.set_buttons(["Cancel", action])
        dlg.set_cancel_button(0)
        dlg.set_default_button(0)

        def chosen(d, res):
            try:
                if d.choose_finish(res) == 1:
                    on_yes()
            except GLib.Error:
                pass

        dlg.choose(None, None, chosen)
    else:
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text=title)
        dlg.format_secondary_text(detail)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, action, Gtk.ResponseType.ACCEPT)

        def response(d, r):
            d.destroy()
            if r == Gtk.ResponseType.ACCEPT:
                on_yes()

        dlg.connect("response", response)
        dlg.show()


# ------------------------------------------------------------------ actions


def open_in_drive(path: str) -> None:
    def got(result, error):
        if result and result[0]:
            Gio.AppInfo.launch_default_for_uri(result[0], None)
        else:
            show_message("Can't open this in Google Drive", error or "It isn't on Drive yet.")
    client().call("GetDriveLink", GLib.Variant("(s)", (path,)), got)


def copy_drive_link(path: str) -> None:
    def got(result, error):
        if result and result[0]:
            copy_to_clipboard(result[0])
        else:
            show_message("No Google Drive link yet", error or "It isn't on Drive yet.")
    client().call("GetDriveLink", GLib.Variant("(s)", (path,)), got)


def remove_local_copy(path: str) -> None:
    name = os.path.basename(path)

    def go():
        def done(_result, error):
            if error:
                show_message(f"Couldn't remove the local copy of “{name}”", error)
        client().call("Exclude", GLib.Variant("(s)", (path,)), done)

    confirm(
        f"Remove the local copy of “{name}”?",
        "The folder stays on Google Drive but is no longer synced to this computer. "
        "Anything inside that hasn't been uploaded yet is uploaded first. "
        f"To get it back: gdrivesync exclude remove '{path}'",
        "Remove local copy",
        go,
    )


def sync_now() -> None:
    client().call("SyncNow", None)


# ------------------------------------------------------------------ extension


class GDriveSyncExtension(GObject.GObject, Nautilus.InfoProvider, Nautilus.MenuProvider,
                          Nautilus.ColumnProvider):
    def __init__(self):
        super().__init__()
        self.client = client()
        self._cancelled: set = set()

    # -- emblems + column ------------------------------------------------
    def get_columns(self, *_args):
        return [
            Nautilus.Column(
                name="GDriveSync::status",
                attribute=STATUS_ATTRIBUTE,
                label="Sync status",
                description="Google Drive sync status",
            )
        ]

    def update_file_info_full(self, provider, handle, closure, file):
        path = file_path(file)
        if not self.client.in_roots(path):
            return Nautilus.OperationResult.COMPLETE
        self.client.remember(path, file)

        def got(result, _error):
            status = result[0] if result else ""
            if status in EMBLEMS:
                file.add_emblem(EMBLEMS[status])
            if status:
                file.add_string_attribute(STATUS_ATTRIBUTE, LABELS.get(status, status))
            key = self._key(handle)
            if key in self._cancelled:
                self._cancelled.discard(key)
                return
            Nautilus.info_provider_update_complete_invoke(
                closure, provider, handle, Nautilus.OperationResult.COMPLETE
            )

        self.client.call("GetFileStatus", GLib.Variant("(s)", (path,)), got)
        return Nautilus.OperationResult.IN_PROGRESS

    def cancel_update(self, provider, handle):
        self._cancelled.add(self._key(handle))

    @staticmethod
    def _key(handle):
        try:
            hash(handle)
            return handle
        except TypeError:
            return id(handle)

    # -- context menus ---------------------------------------------------
    def get_file_items(self, *args):
        files = args[-1]  # (files) on Nautilus 4, (window, files) on 3
        if len(files) != 1:
            return []
        f = files[0]
        path = file_path(f)
        if not self.client.in_roots(path):
            return []
        items = []

        def item(name, label, tip, action):
            mi = Nautilus.MenuItem(name=f"GDriveSync::{name}", label=label, tip=tip)
            mi.connect("activate", lambda *_: action(path))
            items.append(mi)

        item("open", "Open in Google Drive", "Open this item on drive.google.com", open_in_drive)
        item("copy-link", "Copy Google Drive Link", "Copy a link to this item on Google Drive",
             copy_drive_link)
        if f.is_directory() and not self.client.is_root(path):
            item("exclude", "Remove Local Copy (Keep on Drive)…",
                 "Stop syncing this folder to this computer", remove_local_copy)
        return items

    def get_background_items(self, *args):
        folder = args[-1]
        if not self.client.in_roots(file_path(folder)):
            return []
        mi = Nautilus.MenuItem(name="GDriveSync::sync-now", label="Sync with Google Drive Now",
                               tip="Check for changes right away")
        mi.connect("activate", lambda *_: sync_now())
        return [mi]
