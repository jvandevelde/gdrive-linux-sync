#!/usr/bin/env python3
"""Status icon for gdrivesync (top bar, via Ubuntu's AppIndicator support).

Started by `gdrivesync tray` (and at login, see `gdrivesync install-desktop`).
GTK 3 because libayatana-appindicator is GTK 3; it only talks to the sync
service over D-Bus. Single instance: starting it again does nothing.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ValueError, ImportError):
    AppIndicator = None

import ui_gio  # noqa: E402
from ui_common import (  # noqa: E402
    APP_ID, SERVICE_UNIT, NotificationTracker, cli_command, sentence, short_time, summarize,
)  # noqa: E402

REFRESH_S = 10
RECENT_ITEMS = 8


class Tray:
    def __init__(self, app: Gtk.Application, client: ui_gio.ServiceClient | None = None):
        self.app = app
        self.client = client or ui_gio.ServiceClient()
        self.tracker = NotificationTracker()
        self.state: dict | None = None
        self.summary = summarize(None)
        self._layout_key = None  # rebuild the menu only when its shape changes

        self.menu = Gtk.Menu()
        self.indicator = None
        if AppIndicator is not None:
            self.indicator = AppIndicator.Indicator.new(
                "gdrivesync", self.summary.icon,
                AppIndicator.IndicatorCategory.APPLICATION_STATUS,
            )
            self.indicator.set_title("Google Drive Sync")
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_menu(self.menu)
        else:
            print("gdrivesync tray: AyatanaAppIndicator3 isn't available "
                  "(sudo apt install gir1.2-ayatanaappindicator3-0.1)", file=sys.stderr)

        self._build_menu()
        self.client.on_change(self.refresh)
        self.client.on_availability(lambda _up: self.refresh())
        GLib.timeout_add_seconds(REFRESH_S, self._tick)

    # -------------------------------------------------- state
    def _tick(self):
        self.refresh()
        return True

    def refresh(self):
        if self.client.available:
            self.client.get_state(self.apply)
        else:
            self.apply(None)

    def apply(self, state: dict | None):
        self.state = state
        self.summary = summarize(state)
        for title, body in self.tracker.update(state):
            ui_gio.notify(title, body, self.summary.icon)
        if self.indicator is not None:
            self.indicator.set_icon_full(self.summary.icon, self.summary.label)
        self._build_menu()

    # -------------------------------------------------- menu
    def _layout(self):
        s = self.state or {}
        return (
            self.state is None,
            self.summary.state == "error",
            bool(s.get("paused")),
            tuple(p["name"] for p in s.get("pairs", [])),
            tuple((e.get("ts"), e.get("path")) for e in s.get("recent", [])[-RECENT_ITEMS:]),
        )

    def _build_menu(self):
        key = self._layout()
        if key == self._layout_key:
            self.status_item.set_label(self.summary.label)
            return
        self._layout_key = key
        for child in self.menu.get_children():
            self.menu.remove(child)
        self.items = {}

        if self.summary.state == "error":  # clickable: shows what went wrong
            self.status_item = self._add("status", self.summary.label,
                                         lambda _w: self.open_settings("--page", "log", "--problems"))
        else:
            self.status_item = self._add("status", self.summary.label, None, sensitive=False)
        state = self.state
        if state is None:
            self._add("start", "Start syncing", self.start_service)
        else:
            for p in state.get("pairs", []):
                self._add(f"open:{p['name']}", f"Open “{p['name']}” folder",
                          lambda _w, path=p["local"]: ui_gio.open_path(path))
            self._sep()
            self._add("sync-now", "Sync now", lambda _w: self.client.call("SyncNow"))
            if state.get("paused"):
                self._add("resume", "Resume syncing", lambda _w: self._pause(False))
            else:
                self._add("pause", "Pause syncing", lambda _w: self._pause(True))
            recent = list(reversed(state.get("recent", [])[-RECENT_ITEMS:]))
            if recent:
                sub = Gtk.Menu()
                for e in recent:
                    mi = Gtk.MenuItem(label=f"{short_time(e.get('ts', ''))}  {sentence(e.get('text', ''))}")
                    mi.connect("activate", lambda _w, e=e: self._reveal(e))
                    sub.append(mi)
                top = Gtk.MenuItem(label="Recent activity")
                top.set_submenu(sub)
                self.menu.append(top)
                self.items["recent"] = top
        self._sep()
        self._add("log", "Show log…", lambda _w: self.open_settings("--page", "log"))
        self._add("settings", "Settings…", lambda _w: self.open_settings())
        self._add("quit", "Hide status icon", lambda _w: self.app.quit())
        self.menu.show_all()

    def _add(self, key, label, handler, sensitive=True):
        mi = Gtk.MenuItem(label=label)
        mi.set_sensitive(sensitive)
        if handler:
            mi.connect("activate", handler)
        self.menu.append(mi)
        self.items[key] = mi
        return mi

    def _sep(self):
        self.menu.append(Gtk.SeparatorMenuItem())

    # -------------------------------------------------- actions
    def _pause(self, pause: bool):
        self.client.call("Pause" if pause else "Resume", None, lambda *_: self.refresh())

    def _reveal(self, entry: dict):
        pair = next((p for p in (self.state or {}).get("pairs", [])
                     if p["name"] == entry.get("pair")), None)
        if pair is None:
            return
        path = os.path.join(pair["local"], entry.get("path", ""))
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if os.path.isdir(folder):
            ui_gio.open_path(folder)

    def start_service(self, *_):
        def done(ok, _out, err):
            if not ok:
                ui_gio.notify("Couldn't start syncing", err or "systemctl failed")
        ui_gio.systemctl(["start", SERVICE_UNIT], done)

    def open_settings(self, *args):
        ui_gio.spawn(cli_command() + ["settings", *args])


class TrayApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID + ".Tray", flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.tray = None

    def do_activate(self):
        if self.tray is None:
            self.tray = Tray(self)
            self.hold()  # no windows; stay alive


def main() -> int:
    return TrayApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
