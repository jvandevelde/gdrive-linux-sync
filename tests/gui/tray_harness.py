"""Drives the real tray icon (GTK 3 + AyatanaAppIndicator) against the real sync
service; notifications go to a fake notification server on the same bus."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from guitest import called, done, wait  # noqa: E402

import gdrivesync_tray as tray_mod  # noqa: E402
import ui_gio  # noqa: E402

opened = []
ui_gio.open_path = opened.append
assert tray_mod.AppIndicator is not None, "AyatanaAppIndicator3 should be importable"


class App:
    quit_called = False

    def quit(self):
        App.quit_called = True


tray = tray_mod.Tray(App())


def labels():
    return [m.get_label() for m in tray.menu.get_children() if isinstance(m, Gtk.MenuItem)
            and not isinstance(m, Gtk.SeparatorMenuItem)]


wait(lambda: tray.state is not None, what="state")
wait(lambda: tray.summary.state == "synced", what="up to date")
assert tray.indicator.get_icon() == "emblem-ok-symbolic", tray.indicator.get_icon()
assert labels()[0] == "Up to date"
assert "Open “Main” folder" in labels() and "Pause syncing" in labels(), labels()

tray.items["open:Main"].activate()
assert opened and opened[0].endswith("/Drive")

tray.items["pause"].activate()
wait(lambda: tray.summary.state == "paused", what="paused")
assert tray.indicator.get_icon() == "media-playback-pause-symbolic"
assert "Resume syncing" in labels()
tray.items["resume"].activate()
wait(lambda: tray.summary.state != "paused", what="resumed")

tray.items["sync-now"].activate()
tray.items["settings"].activate()
wait(lambda: called("cli", "settings"), what="settings launched")
tray.items["log"].activate()
wait(lambda: called("cli", "settings", "--page", "log"), what="log opened")

# a conflict and an error arriving after the first snapshot → notifications
state = dict(tray.state)
state["recent"] = state.get("recent", []) + [{
    "ts": "2026-11-20T10:00:00+00:00", "op": "conflict", "pair": "Main",
    "path": "Docs/plan (conflicted copy 2026-11-20 100000).txt", "src": "Docs/plan.txt",
    "text": "keep local Docs/plan.txt as …"}]
state["pairs"] = [dict(state["pairs"][0], errors={"Docs/big.iso": "HttpError 403: quota"})]
tray.apply(state)
assert tray.summary.state == "error" and tray.indicator.get_icon() == "dialog-warning-symbolic"
assert labels()[0] == "1 item couldn't sync"
assert tray.items["status"].get_sensitive()  # clickable → the log, problems only
tray.items["status"].activate()
wait(lambda: called("cli", "settings", "--page", "log", "--problems"), what="problems log")

# service gone → offline icon, "Start syncing" item, and a notification
tray.apply(None)
assert tray.indicator.get_icon() == "network-offline-symbolic"
assert labels()[0] == "Sync service isn't running" and "Start syncing" in labels()
tray.items["start"].activate()
wait(lambda: called("systemctl", "--user", "start", "gdrivesync.service"), what="start service")

tray.items["quit"].activate()
assert App.quit_called
# let the async notification calls reach the bus
from guitest import pump  # noqa: E402

pump(1.0)
done()
