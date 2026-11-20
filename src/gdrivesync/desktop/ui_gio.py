"""Gio helpers shared by the tray and settings UIs (no GTK): an async client
for the sync service, running the CLI / systemctl without blocking the UI,
and desktop notifications."""

from __future__ import annotations

import json
import os

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from ui_common import BUS_NAME, INTERFACE, OBJECT_PATH, cli_command, systemctl_command  # noqa: E402

CALL_TIMEOUT_MS = 5000


def remote_message(message: str) -> str:
    if message.startswith("GDBus.Error:") and ": " in message:
        return message.split(": ", 1)[1]
    return message


class ServiceClient:
    """Follows the sync service on the session bus. Listeners:
    on_change(fn)        → fn() when sync status/state changed (debounced)
    on_availability(fn)  → fn(available: bool) when the service appears/vanishes"""

    def __init__(self, debounce_ms: int = 400):
        self.proxy = None
        self._change, self._avail = [], []
        self._debounce_ms = debounce_ms
        self._pending = 0
        self._watch = Gio.bus_watch_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameWatcherFlags.NONE,
            self._appeared, self._vanished,
        )

    @property
    def available(self) -> bool:
        return self.proxy is not None

    def on_change(self, fn):
        self._change.append(fn)

    def on_availability(self, fn):
        self._avail.append(fn)

    def _appeared(self, connection, _name, _owner):
        Gio.DBusProxy.new(
            connection,
            Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES | Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None, BUS_NAME, OBJECT_PATH, INTERFACE, None, self._ready,
        )

    def _ready(self, _src, res):
        try:
            self.proxy = Gio.DBusProxy.new_finish(res)
        except GLib.Error:
            return
        self.proxy.connect("g-signal", self._on_signal)
        for fn in list(self._avail):
            fn(True)

    def _vanished(self, _connection, _name):
        # Also called once at startup if the service isn't running yet.
        self.proxy = None
        for fn in list(self._avail):
            fn(False)

    def _on_signal(self, _proxy, _sender, _signal, _params):
        if self._pending:
            return
        self._pending = GLib.timeout_add(self._debounce_ms, self._fire)

    def _fire(self):
        self._pending = 0
        for fn in list(self._change):
            fn()
        return False

    def call(self, method, params=None, callback=None):
        """callback(result_tuple_or_None, error_message_or_None)."""
        if self.proxy is None:
            if callback:
                GLib.idle_add(lambda: callback(None, "The sync service isn't running.") and False)
            return

        def done(proxy, res):
            try:
                out, err = proxy.call_finish(res).unpack(), None
            except GLib.Error as e:
                out, err = None, remote_message(e.message)
            if callback:
                callback(out, err)

        self.proxy.call(method, params, Gio.DBusCallFlags.NO_AUTO_START, CALL_TIMEOUT_MS,
                        None, done)

    def get_state(self, callback):
        """callback(state_dict_or_None)."""
        def got(result, _err):
            callback(json.loads(result[0]) if result else None)
        self.call("GetState", None, got)


def run(argv, callback=None):
    """Run a command without blocking; callback(ok, stdout, stderr)."""
    try:
        proc = Gio.Subprocess.new(
            argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
        )
    except GLib.Error as e:
        if callback:
            GLib.idle_add(lambda: callback(False, "", e.message) and False)
        return

    def done(p, res):
        try:
            _ok, out, err = p.communicate_utf8_finish(res)
        except GLib.Error as e:
            out, err = "", e.message
        if callback:
            callback(p.get_successful(), out or "", (err or "").strip())

    proc.communicate_utf8_async(None, None, done)


def run_cli(args, callback=None):
    run(cli_command() + list(args), callback)


def systemctl(args, callback=None):
    run(systemctl_command() + list(args), callback)


def notify(title: str, body: str, icon: str = "folder-remote"):
    """Desktop notification via org.freedesktop.Notifications (fire and forget)."""
    def got_bus(_src, res):
        try:
            bus = Gio.bus_get_finish(res)
        except GLib.Error:
            return
        bus.call(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)",
                         ("Google Drive Sync", 0, icon, title, body, [], {}, -1)),
            None, Gio.DBusCallFlags.NONE, 3000, None, lambda *_: None,
        )
    Gio.bus_get(Gio.BusType.SESSION, None, got_bus)


def open_path(path: str):
    Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)


def open_uri(uri: str):
    Gio.AppInfo.launch_default_for_uri(uri, None)


def file_exists(path: str) -> bool:
    return os.path.exists(path)


def spawn(argv) -> bool:
    """Start a long-running program (e.g. the tray) without waiting for it."""
    try:
        Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE)
        return True
    except GLib.Error:
        return False
