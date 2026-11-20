#!/usr/bin/env python3
"""Settings and status window for gdrivesync (GTK 4 + libadwaita).

Started by `gdrivesync settings` or from the app grid. Reads live status from the
sync service over D-Bus and settings via `gdrivesync config --json`; every change
goes through the same `gdrivesync` CLI commands a user would type, so validation
and locking are shared. Single instance: launching again raises the window.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

import ui_gio  # noqa: E402
from ui_common import (  # noqa: E402
    APP_ID, SERVICE_UNIT, autostart_entry, autostart_path, cli_command, journal_argv, log_time,
    ops_argv, parse_journal_line, parse_ops_line, sentence, short_time, summarize,
)

APPLY_DELAY_MS = 700
MAX_LOG_ROWS = 2000


def parse_args(argv):
    """`--page NAME [--problems]` (from `gdrivesync settings` or the tray)."""
    page, problems = None, False
    it = iter(argv)
    for a in it:
        if a == "--page":
            page = next(it, None)
        elif a == "--problems":
            problems = True
    return page, problems


class LogView:
    """A live log (the service's journal, or the operation log) in a list,
    newest first, with search and a 'problems only' filter."""

    def __init__(self, argv_fn, parse, unavailable, empty):
        self.argv_fn, self.parse, self.unavailable, self.empty = argv_fn, parse, unavailable, empty
        self.rows: list = []  # newest first
        self.query = ""
        self.problems_only = False
        self.stream = None
        self.finished = False
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_filter_func(self._filter)
        self.placeholder = Gtk.Label(label="Loading…", wrap=True, margin_top=24,
                                     margin_bottom=24, margin_start=12, margin_end=12)
        self.placeholder.add_css_class("dim-label")
        self.listbox.set_placeholder(self.placeholder)
        clamp = Adw.Clamp(maximum_size=960, child=self.listbox, margin_top=6,
                          margin_bottom=18, margin_start=12, margin_end=12)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, child=clamp)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

    def start(self):
        if self.stream is None and not self.finished:
            self.stream = ui_gio.LineStream(self.argv_fn(), self._line, self._exit)
            # A follow with nothing to show just waits: don't say "Loading…" forever.
            GLib.timeout_add(2000, self._still_empty)

    def _still_empty(self):
        if not self.rows and not self.finished:
            self.placeholder.set_label(self.empty)
        return False

    def stop(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream = None

    def _line(self, line):
        entry = self.parse(line)
        if entry is None:
            return
        row = self._row(entry)
        self.listbox.prepend(row)
        self.rows.insert(0, row)
        while len(self.rows) > MAX_LOG_ROWS:
            self.listbox.remove(self.rows.pop())
        self._update_placeholder()

    def _exit(self, ok, message):
        self.stream = None
        self.finished = True
        if not self.rows:
            self.placeholder.set_label(self.unavailable + (f"\n{message}" if message else ""))

    def _row(self, e):
        r = Gtk.ListBoxRow(activatable=False)
        r.entry = e
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        t = Gtk.Label(label=log_time(e.ts), xalign=0, valign=Gtk.Align.START, width_chars=8)
        t.add_css_class("dim-label")
        t.add_css_class("numeric")
        m = Gtk.Label(label=e.text, xalign=0, hexpand=True, wrap=True, selectable=True,
                      wrap_mode=2)  # Pango.WrapMode.WORD_CHAR
        if e.level in ("error", "warning"):
            m.add_css_class(e.level)
        box.append(t)
        box.append(m)
        r.set_child(box)
        return r

    def _filter(self, row):
        e = row.entry
        if self.problems_only and not e.problem:
            return False
        return not self.query or self.query in e.text.lower()

    def refilter(self):
        self.listbox.invalidate_filter()
        self._update_placeholder()

    def _update_placeholder(self):
        if self.rows:
            self.placeholder.set_label("Nothing matches")

    def visible(self) -> list:
        """Shown entries, oldest first."""
        return [r.entry for r in reversed(self.rows) if self._filter(r)]

    def visible_text(self) -> str:
        return "\n".join(f"{log_time(e.ts)}  {e.text}" for e in self.visible())


class Section:
    """A PreferencesGroup whose rows can be replaced wholesale."""

    def __init__(self, page: Adw.PreferencesPage, title: str = "", description: str = ""):
        self.group = Adw.PreferencesGroup(title=title, description=description)
        page.add(self.group)
        self.rows: list[Gtk.Widget] = []

    def clear(self):
        for r in self.rows:
            self.group.remove(r)
        self.rows = []

    def add(self, row):
        self.group.add(row)
        self.rows.append(row)
        return row


def row(title, subtitle="", icon=None, activatable=False):
    r = Adw.ActionRow(title=GLib.markup_escape_text(title),
                      subtitle=GLib.markup_escape_text(subtitle))
    r.set_subtitle_lines(2)
    if icon:
        r.add_prefix(Gtk.Image(icon_name=icon))
    r.set_activatable(activatable)
    return r


def button(label=None, icon=None, tooltip=None, css=None, on_click=None):
    b = Gtk.Button(valign=Gtk.Align.CENTER)
    if icon:
        b.set_icon_name(icon)
    if label:
        b.set_label(label)
    if tooltip:
        b.set_tooltip_text(tooltip)
    for c in (css or ["flat"]):
        b.add_css_class(c)
    if on_click:
        b.connect("clicked", lambda *_: on_click())
    return b


class SettingsWindow(Adw.ApplicationWindow):
    def __init__(self, application=None, client: ui_gio.ServiceClient | None = None):
        super().__init__(application=application, title="Google Drive Sync")
        self.set_default_size(720, 680)
        self.client = client or ui_gio.ServiceClient()
        self.state: dict | None = None
        self.config: dict | None = None
        self.restart_needed = False
        self._loading = False
        self._apply_source = 0
        self.busy = 0  # outstanding CLI/systemctl commands (used by tests)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.stack = Adw.ViewStack()
        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        view.add_top_bar(header)
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", lambda *_: self._banner_action())
        view.add_top_bar(self.banner)
        view.set_content(self.stack)

        self._build_status_page()
        self._build_folders_page()
        self._build_log_page()
        self._build_settings_page()
        self._build_account_page()

        self.client.on_change(self.refresh_state)
        self.client.on_availability(lambda _up: self.refresh_state())
        GLib.timeout_add_seconds(10, lambda: self.refresh_state() or True)
        self.refresh_state()
        self.reload_config()
        self.load_account()
        self.load_service_enabled()

    # ================================================================ helpers
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def error(self, heading, body):
        dlg = Adw.AlertDialog(heading=heading, body=body or "Something went wrong.")
        dlg.add_response("ok", "OK")
        dlg.present(self)
        self.last_error = (heading, body)
        self.error_dialog = dlg

    def cli(self, args, on_ok=None, failure="That didn't work"):
        self.busy += 1

        def done(ok, out, err):
            self.busy -= 1
            if ok:
                if on_ok:
                    on_ok(out)
            else:
                self.error(failure, err.splitlines()[-1] if err else "")
        ui_gio.run_cli(args, done)

    def systemctl(self, args, on_done=None):
        self.busy += 1

        def done(ok, out, err):
            self.busy -= 1
            if on_done:
                on_done(ok, out, err)
        ui_gio.systemctl(args, done)

    def restart_service(self, then=None):
        def done(ok, _out, err):
            if ok:
                self.restart_needed = False
                self._update_banner()
                self.toast("Sync service restarted")
            else:
                self.error("Couldn't restart the sync service", err)
            if then:
                then()
        self.systemctl(["restart", SERVICE_UNIT], done)

    def _update_banner(self):
        if self.state is None and not self.client.available:
            self.banner.set_title("The sync service isn't running")
            self.banner.set_button_label("Start")
            self.banner.set_revealed(True)
        elif self.restart_needed:
            self.banner.set_title("Restart the sync service to apply your changes")
            self.banner.set_button_label("Restart")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def _banner_action(self):
        if not self.client.available:
            self.systemctl(["start", SERVICE_UNIT], lambda ok, _o, err: None if ok else
                           self.error("Couldn't start the sync service", err))
        else:
            self.restart_service()

    # ================================================================ status page
    def _build_status_page(self):
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "status", "Status", "emblem-synchronizing-symbolic")
        self.overview = Section(page)
        self.status_icon = Gtk.Image(pixel_size=32)
        self.overview_row = Adw.ActionRow()
        self.overview_row.add_prefix(self.status_icon)
        self.sync_btn = button("Sync Now", on_click=lambda: self.client.call("SyncNow"))
        self.pause_btn = button("Pause", on_click=self.toggle_pause)
        self.overview_row.add_suffix(self.sync_btn)
        self.overview_row.add_suffix(self.pause_btn)
        self.overview.group.add(self.overview_row)
        self.folders_status = Section(page, "Folders")
        self.problems = Section(page, "Problems")
        self.problems.group.set_header_suffix(
            button("Show Log", on_click=lambda: self.show_page("log", problems=True)))
        self.activity = Section(page, "Recent activity")

    def toggle_pause(self):
        paused = bool((self.state or {}).get("paused"))
        self.client.call("Resume" if paused else "Pause", None, lambda *_: self.refresh_state())

    def refresh_state(self):
        if self.client.available:
            self.client.get_state(self.apply_state)
        else:
            self.apply_state(None)

    def apply_state(self, state):
        self.state = state
        s = summarize(state)
        self.summary = s
        self.status_icon.set_from_icon_name(s.icon)
        self.overview_row.set_title(GLib.markup_escape_text(s.label))
        self.overview_row.set_subtitle(GLib.markup_escape_text(s.detail))
        online = state is not None
        self.sync_btn.set_sensitive(online)
        self.pause_btn.set_sensitive(online)
        self.pause_btn.set_label("Resume" if online and state.get("paused") else "Pause")

        self.folders_status.clear()
        self.problems.clear()
        self.activity.clear()
        pairs = (state or {}).get("pairs", [])
        for p, line in zip(pairs, s.lines):
            r = self.folders_status.add(row(p["name"], f"{p['local']}  ⇄  Google Drive {p.get('remote') or ''}"
                                                       f"\n{line.split(': ', 1)[1]}",
                                            icon="folder-remote-symbolic"))
            r.add_suffix(button(icon="folder-open-symbolic", tooltip="Open folder",
                                on_click=lambda path=p["local"]: ui_gio.open_path(path)))
        if not pairs:
            self.folders_status.add(row("No folders are being synced" if online else "—",
                                        "Add one on the Folders page" if online else ""))
        for p in pairs:
            for path, msg in p.get("errors", {}).items():
                self.problems.add(row(os.path.basename(path) or path, f"{p['name']}/{path}: {msg}",
                                      icon="dialog-warning-symbolic"))
        self.problems.group.set_visible(bool(self.problems.rows))
        recent = list(reversed((state or {}).get("recent", [])))[:20]
        for e in recent:
            self.activity.add(row(sentence(e.get("text", e.get("op", ""))),
                                  f"{short_time(e.get('ts', ''))} · {e.get('pair', '')}"))
        if not recent:
            self.activity.add(row("Nothing yet", "Changes appear here as they sync"))
        self._update_banner()

    # ================================================================ folders page
    def _build_folders_page(self):
        self.folders_page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(self.folders_page, "folders", "Folders",
                                        "folder-symbolic")
        self.pair_groups: list[Adw.PreferencesGroup] = []
        self.add_group = Adw.PreferencesGroup()
        add = row("Add a folder pair…", "Sync another local folder with a folder on Google Drive",
                  icon="list-add-symbolic", activatable=True)
        add.connect("activated", lambda *_: self.add_pair_dialog())
        self.add_row = add
        self.add_group.add(add)

    def reload_config(self, then=None):
        def got(out):
            try:
                self.config = json.loads(out)
            except ValueError:
                self.config = None
            self.apply_config()
            if then:
                then()
        self.cli(["config", "--json"], got, "Couldn't read the settings")

    def apply_config(self):
        cfg = self.config or {"pairs": []}
        for g in self.pair_groups:
            self.folders_page.remove(g)
        self.pair_groups = []
        if self.add_group.get_parent():
            self.folders_page.remove(self.add_group)
        for p in cfg["pairs"]:
            g = Adw.PreferencesGroup(
                title=GLib.markup_escape_text(p["name"]),
                description=GLib.markup_escape_text(
                    f"{p['local']}  ⇄  Google Drive {p['remote_path']}\n"
                    f"Last full check: {p.get('last_full_check', 'never')}"),
            )
            suffix = Gtk.Box(spacing=6)
            suffix.append(button(icon="folder-open-symbolic", tooltip="Open folder",
                                 on_click=lambda path=p["local"]: ui_gio.open_path(path)))
            suffix.append(button("Remove…", on_click=lambda name=p["name"]: self.remove_pair(name)))
            g.set_header_suffix(suffix)
            for rel in p.get("excluded", []):
                pending = rel.endswith(" (removal pending)")
                rel_clean = rel.removesuffix(" (removal pending)")
                r = row(rel_clean, "Being removed from this computer…" if pending else
                        "Not synced to this computer (kept on Google Drive)",
                        icon="folder-remote-symbolic")
                if not pending:
                    r.add_suffix(button("Sync Again", on_click=lambda path=os.path.join(
                        p["local"], rel_clean): self.include(path)))
                g.add(r)
            if not p.get("excluded"):
                g.add(row("Every subfolder is synced",
                          "To stop syncing one here, right-click it in Files → "
                          "Remove Local Copy (Keep on Drive)"))
            self.folders_page.add(g)
            self.pair_groups.append(g)
        self.folders_page.add(self.add_group)
        self._apply_settings_values(cfg)

    def include(self, path):
        def done(_res, err):
            if err:
                self.error("Couldn't sync that folder again", err)
            else:
                self.toast(f"“{os.path.basename(path)}” will be downloaded again")
                GLib.timeout_add(800, lambda: self.reload_config() and False)
        self.client.call("Include", GLib.Variant("(s)", (path,)), done)

    def add_pair_dialog(self):
        dlg = Adw.AlertDialog(heading="Sync another folder",
                              body="Choose a folder on this computer and the Google Drive folder "
                                   "to keep it in sync with.")
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        local_row = row("Folder on this computer", "Not chosen yet")
        chosen = {"path": None}

        def pick():
            fd = Gtk.FileDialog(title="Choose a folder to sync")

            def done(d, res):
                try:
                    f = d.select_folder_finish(res)
                except GLib.Error:
                    return
                set_local(f.get_path())
            fd.select_folder(self, None, done)

        def set_local(path):
            chosen["path"] = path
            local_row.set_subtitle(GLib.markup_escape_text(path))
            dlg.set_response_enabled("add", True)

        local_row.add_suffix(button("Choose…", on_click=pick))
        remote = Adw.EntryRow(title="Folder on Google Drive (e.g. /Work, or / for everything)")
        remote.set_text("/")
        name = Adw.EntryRow(title="Name (optional)")
        create = Adw.SwitchRow(title="Create it on Google Drive if it doesn't exist")
        for w in (local_row, remote, name, create):
            box.append(w)
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("add", "Add")
        dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_response_enabled("add", False)
        dlg.set_default_response("add")

        pending = []

        def response(_d, resp):
            if resp != "add" or not chosen["path"]:
                return
            args = ["pair", "add", chosen["path"], remote.get_text().strip() or "/"]
            if name.get_text().strip():
                args += ["--name", name.get_text().strip()]
            if create.get_active():
                args.append("--create")
            pending.append(args)

        def closed(_d):
            # Run only once the dialog is gone, so an error dialog never stacks on it.
            for args in pending:
                self.cli(args, lambda _out: self._pair_changed("Folder added; syncing starts now"),
                         "Couldn't add that folder")

        dlg.connect("response", response)
        dlg.connect("closed", closed)
        dlg.present(self)
        self.add_dialog = (dlg, set_local, remote, name, create)  # for tests
        return dlg

    def remove_pair(self, name):
        dlg = Adw.AlertDialog(
            heading=f"Stop syncing “{name}”?",
            body="Nothing is deleted: the files stay on this computer and on Google Drive, "
                 "they just stop being kept in sync.",
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Stop Syncing")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        confirmed = []

        def response(_d, resp):
            if resp == "remove":
                confirmed.append(True)

        def closed(_d):
            if not confirmed:
                return

            # The service holds the folder's lock: stop it, remove, start again.
            def stopped(_ok, _o, _e):
                def removed(_out):
                    self.systemctl(["start", SERVICE_UNIT],
                                   lambda *_: self._pair_changed(f"Stopped syncing “{name}”",
                                                                 restart=False))
                self.busy += 1

                def done(ok, out, err):
                    self.busy -= 1
                    if ok:
                        removed(out)
                    else:
                        self.systemctl(["start", SERVICE_UNIT])
                        self.error("Couldn't remove that folder", err.splitlines()[-1] if err else "")
                ui_gio.run_cli(["pair", "remove", name], done)
            self.systemctl(["stop", SERVICE_UNIT], stopped)

        dlg.connect("response", response)
        dlg.connect("closed", closed)
        dlg.present(self)
        self.remove_dialog = dlg  # for tests
        return dlg

    def _pair_changed(self, message, restart=True):
        self.toast(message)
        self.reload_config()
        if restart:
            self.restart_service()

    # ================================================================ log page
    def _build_log_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack.add_titled_with_icon(box, "log", "Log", "text-x-generic-symbolic")

        bar = Gtk.Box(spacing=6, margin_top=12, margin_bottom=6, margin_start=12, margin_end=12)
        bar_clamp = Adw.Clamp(maximum_size=960, child=bar)
        seg = Gtk.Box()
        seg.add_css_class("linked")
        self.view_journal = Gtk.ToggleButton(label="Service log", active=True,
                                             tooltip_text="What the sync service reported")
        self.view_ops = Gtk.ToggleButton(label="Operations", group=self.view_journal,
                                         tooltip_text="Every change made to your files")
        seg.append(self.view_journal)
        seg.append(self.view_ops)
        self.log_search = Gtk.SearchEntry(placeholder_text="Search", hexpand=True)
        self.problems_toggle = Gtk.ToggleButton(label="Warnings & errors")
        copy = button(icon="edit-copy-symbolic", tooltip="Copy what's shown", css=[],
                      on_click=self.copy_log)
        self.open_ops_btn = button(icon="document-open-symbolic", css=[],
                                   tooltip="Open the operation log file",
                                   on_click=lambda: self._open_cfg_path("operation_log"))
        for w in (seg, self.log_search, self.problems_toggle, copy, self.open_ops_btn):
            bar.append(w)
        box.append(bar_clamp)

        self.journal_view = LogView(
            journal_argv, parse_journal_line,
            "The service log isn't available (it needs systemd's journalctl).",
            "Nothing logged yet — entries appear here as the sync service runs.")
        self.ops_view = LogView(ops_argv, parse_ops_line, "Couldn't read the operation log.",
                                "No changes have been made yet.")
        self.log_stack = Gtk.Stack(vexpand=True)
        self.log_stack.add_named(self.journal_view.scroller, "journal")
        self.log_stack.add_named(self.ops_view.scroller, "ops")
        box.append(self.log_stack)

        for b in (self.view_journal, self.view_ops):
            b.connect("toggled", lambda btn: btn.get_active() and self._log_view_changed())
        self.log_search.connect("search-changed", self._log_filter_changed)
        self.problems_toggle.connect("toggled", self._log_filter_changed)
        self.stack.connect("notify::visible-child-name", self._page_changed)
        self.connect("close-request", self._stop_logs)
        self._log_view_changed()

    def _page_changed(self, *_):
        if self.stack.get_visible_child_name() == "log":
            self.journal_view.start()
            self.ops_view.start()

    def _stop_logs(self, *_):
        self.journal_view.stop()
        self.ops_view.stop()
        return False

    def current_log(self) -> LogView:
        return self.ops_view if self.view_ops.get_active() else self.journal_view

    def _log_view_changed(self, *_):
        ops = self.view_ops.get_active()
        self.log_stack.set_visible_child_name("ops" if ops else "journal")
        self.problems_toggle.set_label("Deletions & conflicts" if ops else "Warnings & errors")
        self.open_ops_btn.set_visible(ops)

    def _log_filter_changed(self, *_):
        query = self.log_search.get_text().strip().lower()
        for v in (self.journal_view, self.ops_view):
            v.query = query
            v.problems_only = self.problems_toggle.get_active()
            v.refilter()

    def copy_log(self):
        view = self.current_log()
        text = view.visible_text()
        ui_gio.set_clipboard(text)
        self.toast(f"Copied {len(view.visible())} lines")

    def show_page(self, name, problems=False, view=None):
        self.stack.set_visible_child_name(name)
        if name == "log":
            if view == "ops":
                self.view_ops.set_active(True)
            elif view == "journal" or problems:
                self.view_journal.set_active(True)
            self.problems_toggle.set_active(problems)
            self._page_changed()

    # ================================================================ settings page
    def _build_settings_page(self):
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "settings", "Settings", "emblem-system-symbolic")

        sync = Adw.PreferencesGroup(title="Syncing")
        self.poll = Adw.SpinRow.new_with_range(10, 3600, 5)
        self.poll.set_title("Check Google Drive for changes every")
        self.poll.set_subtitle("seconds")
        self.full = Adw.SpinRow.new_with_range(0, 168, 1)
        self.full.set_title("Compare everything with Google Drive every")
        self.full.set_subtitle("hours (0 = never) — catches anything missed")
        self.newer = Adw.SwitchRow(
            title="When rebuilding, newer local files win",
            subtitle="Upload them as a new version instead of keeping a conflicted copy",
        )
        for w in (self.poll, self.full, self.newer):
            sync.add(w)
        self.poll.connect("notify::value", self._settings_edited)
        self.full.connect("notify::value", self._settings_edited)
        self.newer.connect("notify::active", self._settings_edited)
        page.add(sync)

        startup = Adw.PreferencesGroup(title="Startup")
        self.at_login = Adw.SwitchRow(title="Sync when I log in",
                                      subtitle="Runs the sync service in the background")
        self.at_login.connect("notify::active", self._login_toggled)
        self.tray_switch = Adw.SwitchRow(title="Show status icon in the top bar")
        self.tray_switch.connect("notify::active", self._tray_toggled)
        startup.add(self.at_login)
        startup.add(self.tray_switch)
        page.add(startup)

        files = Adw.PreferencesGroup(title="Files (Nautilus)")
        self.ext_row = row("Status icons and right-click menu in Files", "")
        self.ext_btn = button("Install", on_click=self.install_desktop)
        self.ext_row.add_suffix(self.ext_btn)
        files.add(self.ext_row)
        page.add(files)

        adv = Adw.PreferencesGroup(title="Advanced")
        self.log_row = row("Operation log", "Every change made, as JSON lines")
        self.log_row.add_suffix(button("View", on_click=lambda: self.show_page("log", view="ops")))
        self.log_row.add_suffix(button(icon="document-open-symbolic", tooltip="Open the file",
                                       on_click=lambda: self._open_cfg_path("operation_log")))
        self.ignore_row = row("Ignored files", "Patterns of files never to sync (gitignore style)")
        self.ignore_row.add_suffix(button(icon="document-edit-symbolic", tooltip="Edit",
                                          on_click=lambda: self._open_cfg_path("ignore_file",
                                                                               create=True)))
        adv.add(self.log_row)
        adv.add(self.ignore_row)
        page.add(adv)

    def _apply_settings_values(self, cfg):
        self._loading = True
        try:
            self.poll.set_value(cfg.get("poll_interval_s", 45))
            self.full.set_value(cfg.get("full_check_hours", 24))
            self.newer.set_active(bool(cfg.get("prefer_newer_on_rebuild")))
            d = cfg.get("desktop", {})
            self.tray_switch.set_active(bool(d.get("tray_autostart")))
            installed = d.get("extension_installed")
            if not d.get("nautilus_python", True):
                sub = "Needs the python3-nautilus package: sudo apt install python3-nautilus"
            else:
                sub = "Installed — restart Files to pick up updates" if installed else \
                    "Not installed"
            self.ext_row.set_subtitle(GLib.markup_escape_text(sub))
            self.ext_btn.set_label("Reinstall" if installed else "Install")
            paths = cfg.get("paths", {})
            self.log_row.set_subtitle(GLib.markup_escape_text(paths.get("operation_log", "")))
        finally:
            self._loading = False

    def _settings_edited(self, *_):
        if self._loading:
            return
        if self._apply_source:
            GLib.source_remove(self._apply_source)
        self._apply_source = GLib.timeout_add(APPLY_DELAY_MS, self._apply_settings)

    def _apply_settings(self):
        self._apply_source = 0
        args = ["config",
                "--poll-interval", str(int(self.poll.get_value())),
                "--full-check-hours", f"{self.full.get_value():g}",
                "--prefer-newer-on-rebuild", "on" if self.newer.get_active() else "off"]

        def saved(_out):
            self.restart_needed = True
            self._update_banner()
            self.toast("Saved")
        self.cli(args, saved, "Couldn't save the settings")
        return False

    def load_service_enabled(self):
        def done(_ok, out, _err):
            self._loading = True
            self.at_login.set_active(out.strip() == "enabled")
            self._loading = False
        self.systemctl(["is-enabled", SERVICE_UNIT], done)

    def _login_toggled(self, *_):
        if self._loading:
            return
        on = self.at_login.get_active()

        def done(ok, _o, err):
            if not ok:
                self.error("Couldn't change that", err)
                self.load_service_enabled()
            else:
                self.toast("Will sync when you log in" if on else "Won't start at login")
        self.systemctl(["enable", "--now", SERVICE_UNIT] if on else ["disable", SERVICE_UNIT], done)

    def _tray_toggled(self, *_):
        if self._loading:
            return
        path = autostart_path()
        if self.tray_switch.get_active():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(autostart_entry())
            ui_gio.spawn(cli_command() + ["tray"])  # single instance: no-op if running
            self.toast("Status icon will show at login")
        else:
            if os.path.exists(path):
                os.remove(path)
            self.toast("Status icon won't start at login")

    def install_desktop(self):
        def done(_out):
            self.toast("Installed — restart Files: nautilus -q")
            self.reload_config()
        self.cli(["install-desktop"], done, "Couldn't install the Files integration")

    def _open_cfg_path(self, key, create=False):
        path = ((self.config or {}).get("paths") or {}).get(key)
        if not path:
            return
        if create and not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("# One pattern per line, e.g.\n# *.iso\n# node_modules/\n")
        if os.path.exists(path):
            ui_gio.open_path(path)
        else:
            self.toast("Nothing logged yet")

    # ================================================================ account page
    def _build_account_page(self):
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "account", "Account", "avatar-default-symbolic")
        self.account = Section(page, "Google account")
        g = Adw.PreferencesGroup()
        self.login_row = row("Sign in again", "Opens your browser; use this if Google "
                                              "asks you to re-authorize", activatable=True)
        self.login_row.connect("activated", lambda *_: self.sign_in())
        g.add(self.login_row)
        page.add(g)

    def load_account(self):
        self.busy += 1

        def done(ok, out, err):
            self.busy -= 1
            self.account.clear()
            if ok and out.strip():
                lines = out.strip().splitlines()
                self.account.add(row(lines[0], "Signed in", icon="avatar-default-symbolic"))
                for extra in lines[1:]:
                    self.account.add(row(extra, "", icon="drive-harddisk-symbolic"))
            else:
                self.account.add(row("Not signed in", (err.splitlines() or [""])[-1],
                                     icon="dialog-warning-symbolic"))
        ui_gio.run_cli(["whoami"], done)

    def sign_in(self):
        self.toast("Finish signing in in your browser")

        def done(_out):
            self.load_account()
            self.restart_service()
        self.cli(["login"], done, "Signing in didn't finish")


class SettingsApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None

    def do_activate(self):
        if self.window is None:
            self.window = SettingsWindow(application=self)
        self.window.present()

    def do_command_line(self, command_line):
        # Also runs in the first instance when launched again (single instance).
        page, problems = parse_args(command_line.get_arguments()[1:])
        self.activate()
        if page:
            self.window.show_page(page, problems=problems)
        return 0


def main() -> int:
    return SettingsApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
