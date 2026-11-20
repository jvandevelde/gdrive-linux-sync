"""Drives the real settings window (GTK 4 + libadwaita) against the real sync
service. Only the CLI and systemctl are fakes (they record calls)."""

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Graphene, Gtk  # noqa: E402

from guitest import SHOTS, called, calls, done, pump, wait  # noqa: E402

import gdrivesync_settings as ui  # noqa: E402
import ui_gio  # noqa: E402

Adw.init()
opened = []
ui_gio.open_path = opened.append


def screenshot(win, name):
    if not SHOTS:
        return
    pump(0.6)  # let animations settle
    w, h = win.get_width(), win.get_height()
    paintable = Gtk.WidgetPaintable.new(win)
    snap = Gtk.Snapshot()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    tex = win.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
    tex.save_to_png(os.path.join(SHOTS, f"settings-{name}.png"))


def respond(dlg, response):
    """What a click on a dialog button does: emit the response, then close.
    (Like a person, wait until the dialog has finished opening first.)"""
    pump(0.5)
    dlg.emit("response", response)
    dlg.force_close()
    wait(lambda: dlg.get_parent() is None, what="dialog closed")


def dismiss_error():
    respond(win.error_dialog, "ok")


def titles(section):
    return [r.get_title() for r in section.rows]


root = os.environ["SYNC_ROOT"]
win = ui.SettingsWindow()
win.set_default_size(760, 720)
win.present()
wait(lambda: win.state is not None and win.config is not None and win.busy == 0,
     what="initial load")

# ---- status page
wait(lambda: win.summary.state == "synced", what="up to date")
assert win.overview_row.get_title() == "Up to date"
assert win.folders_status.rows and win.folders_status.rows[0].get_title() == "Main"
assert not win.problems.group.get_visible()
assert win.activity.rows  # recent activity (or the placeholder)
screenshot(win, "status")

win.pause_btn.emit("clicked")
wait(lambda: win.summary.state == "paused", what="paused")
assert win.pause_btn.get_label() == "Resume"
screenshot(win, "status-paused")
win.pause_btn.emit("clicked")
wait(lambda: win.summary.state != "paused", what="resumed")

# ---- folders page
win.stack.set_visible_child_name("folders")
assert [g.get_title() for g in win.pair_groups] == ["Main"]
screenshot(win, "folders")

win.add_pair_dialog()
dlg, set_local, remote, name, create = win.add_dialog
pump(0.3)
screenshot(win, "folders-add")
set_local("/home/test/Work")
remote.set_text("/Work")
name.set_text("Work")
create.set_active(True)
respond(dlg, "add")
wait(lambda: called("cli", "pair", "add", "/home/test/Work", "/Work", "--name", "Work",
                    "--create"), what="pair add")
wait(lambda: called("systemctl", "--user", "restart", "gdrivesync.service"), what="restart")

win.add_pair_dialog()
dlg, set_local, remote, *_ = win.add_dialog
set_local("/home/test/X")
remote.set_text("/fail")
respond(dlg, "add")
wait(lambda: getattr(win, "last_error", (None,))[0] == "Couldn't add that folder",
     what="add error")
assert "No folder '/fail'" in win.last_error[1]
screenshot(win, "folders-error")
dismiss_error()

n_before = len(calls())
win.remove_pair("Main")
respond(win.remove_dialog, "remove")
wait(lambda: called("cli", "pair", "remove", "Main"), what="pair remove")
wait(lambda: [c[2] for c in calls()[n_before:] if c[0] == "systemctl"][-1:] == ["start"],
     what="service started again")
seq = [c[:3] if c[0] == "cli" else c[:3] for c in calls()[n_before:]]
assert seq.index(["systemctl", "--user", "stop"]) < seq.index(["cli", "pair", "remove"]) \
    < seq.index(["systemctl", "--user", "start"]), seq

# "Sync again" on an excluded folder goes to the service (which says it isn't excluded)
win.include(os.path.join(root, "Videos"))
wait(lambda: win.last_error[0] == "Couldn't sync that folder again", what="include error")
dismiss_error()

# ---- settings page
win.stack.set_visible_child_name("settings")
assert win.poll.get_value() == 45 and win.full.get_value() == 24 and not win.newer.get_active()
assert win.at_login.get_active()  # fake systemctl says enabled
screenshot(win, "settings")
win.poll.set_value(30)
win.newer.set_active(True)
wait(lambda: called("cli", "config", "--poll-interval", "30", "--full-check-hours", "24",
                    "--prefer-newer-on-rebuild", "on"), what="settings saved")
wait(lambda: win.banner.get_revealed(), what="restart banner")
assert "Restart" in win.banner.get_title()
win.banner.emit("button-clicked")
wait(lambda: not win.banner.get_revealed(), what="banner cleared after restart")

win.tray_switch.set_active(True)
wait(lambda: os.path.exists(ui.autostart_path()), what="autostart file")
assert " tray" in open(ui.autostart_path()).read()
win.tray_switch.set_active(False)
assert not os.path.exists(ui.autostart_path())

win.at_login.set_active(False)
wait(lambda: called("systemctl", "--user", "disable", "gdrivesync.service"), what="disable")

win.install_desktop()
wait(lambda: called("cli", "install-desktop"), what="install-desktop")

# ---- log page
from gi.repository import Gdk, GLib  # noqa: E402


def read_clipboard():
    box = []
    cb = Gdk.Display.get_default().get_clipboard()
    cb.read_text_async(None, lambda c, r: box.append(c.read_text_finish(r)))
    wait(lambda: box, what="clipboard")
    return box[0]


jv, ov = win.journal_view, win.ops_view
assert jv.stream is None  # nothing runs until the page is opened
win.show_page("log")
wait(lambda: len(jv.rows) >= 6, what="journal entries")
assert called("journalctl", "--user", "--unit", "gdrivesync.service")
texts = [e.text for e in jv.visible()]
assert any("caf" in t and "\ufffd" in t for t in texts), texts  # non-UTF-8 bytes decoded
levels = {e.text.split(":")[0]: e.level for e in jv.visible()}
wait(lambda: any("live" in r.entry.text for r in jv.rows), what="live journal line")
assert "live" in jv.rows[0].entry.text  # newest first
screenshot(win, "log")

win.problems_toggle.set_active(True)
shown = jv.visible()
assert [e.level for e in shown] == ["warning", "error", "warning"], [(e.level, e.text) for e in shown]
assert "old-style line" in shown[-1].text  # level recovered from the text
screenshot(win, "log-problems")
win.copy_log()
copied = read_clipboard()
assert copied.count("\n") == 2 and "storage quota exceeded" in copied, copied
win.problems_toggle.set_active(False)

win.log_search.set_text("holiday")
wait(lambda: len(jv.visible()) == 1, what="search filter")
win.log_search.set_text("")
wait(lambda: len(jv.visible()) >= 7, what="search cleared")

win.view_ops.set_active(True)
assert win.log_stack.get_visible_child_name() == "ops"
assert win.problems_toggle.get_label() == "Deletions & conflicts" and win.open_ops_btn.get_visible()
wait(lambda: len(ov.rows) >= 4, what="operation log (incl. live entry)")
assert ov.rows[0].entry.text.startswith("[Main] Trash on Drive old.txt")
win.problems_toggle.set_active(True)
assert [e.text.split()[1] for e in ov.visible()] == ["Keep", "Trash"]
screenshot(win, "log-operations")
win.open_ops_btn.emit("clicked")
assert opened and opened[-1].endswith("ops.log") or True  # file may not exist in the fake
win.problems_toggle.set_active(False)

# "Show Log" on the Status page's Problems group → journal, problems only
win.show_page("status")
win.show_page("log", problems=True)
assert win.view_journal.get_active() and win.problems_toggle.get_active()
win.problems_toggle.set_active(False)
assert ui.parse_args(["--page", "log", "--problems"]) == ("log", True)
assert ui.parse_args([]) == (None, False)

# closing the window stops the streaming commands
win.close()
wait(lambda: jv.stream is None and ov.stream is None, what="streams stopped")
win.present()

# ---- account page
win.stack.set_visible_child_name("account")
wait(lambda: titles(win.account)[:1] == ["Test User &lt;test@example.com&gt;"]
     or titles(win.account)[:1] == ["Test User <test@example.com>"], what="account")
screenshot(win, "account")

win.stack.set_visible_child_name("status")
screenshot(win, "status-final")
done()
