"""Drives the real Nautilus extension against the real sync service, with only
Nautilus itself faked. Run by test_nautilus_ext.py with the *system* Python
(which has PyGObject, like Nautilus does):

    python3 nautilus_harness.py EXTENSION_FILE SYNC_ROOT

Exits non-zero (with a traceback) on any failed check.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path

import gi

_real_require = gi.require_version


def _require(ns, version):  # Nautilus/Gtk/Gdk aren't installed here
    if ns not in ("Nautilus", "Gtk", "Gdk"):
        _real_require(ns, version)


gi.require_version = _require
from gi.repository import Gio, GLib, GObject  # noqa: E402

# ---------------------------------------------------------------- fake Nautilus
N = types.ModuleType("gi.repository.Nautilus")
N._version = "4.0"
completions = []


class _Iface:
    pass


N.InfoProvider = type("InfoProvider", (_Iface,), {})
N.MenuProvider = type("MenuProvider", (_Iface,), {})
N.ColumnProvider = type("ColumnProvider", (_Iface,), {})


class OperationResult:
    COMPLETE, IN_PROGRESS, FAILED = 0, 1, 2


N.OperationResult = OperationResult
N.info_provider_update_complete_invoke = (
    lambda closure, provider, handle, result: completions.append((handle, result))
)


class MenuItem(GObject.Object):
    __gsignals__ = {"activate": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, name, label, tip):
        super().__init__()
        self.name, self.label, self.tip = name, label, tip


class Column:
    def __init__(self, **kw):
        self.__dict__.update(kw)


N.MenuItem, N.Column = MenuItem, Column
sys.modules["gi.repository.Nautilus"] = N
import gi.repository  # noqa: E402

gi.repository.Nautilus = N


class FakeFile:
    def __init__(self, path, is_dir=False):
        self.path, self.dir = str(path), is_dir
        self.emblems, self.attrs, self.invalidated = [], {}, 0

    def get_location(self):
        return Gio.File.new_for_path(self.path)

    def add_emblem(self, e):
        self.emblems.append(e)

    def add_string_attribute(self, k, v):
        self.attrs[k] = v

    def invalidate_extension_info(self):
        self.invalidated += 1

    def is_directory(self):
        return self.dir


# ---------------------------------------------------------------- load extension
ext_file, root = sys.argv[1], Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("gdrivesync_nautilus", ext_file)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

launched, clipboard, messages = [], [], []
Gio.AppInfo.launch_default_for_uri = lambda uri, ctx: launched.append(uri)
mod.copy_to_clipboard = clipboard.append
mod.show_message = lambda title, detail="": messages.append((title, detail))
mod.confirm = lambda title, detail, action, on_yes: on_yes()

loop = GLib.MainLoop()


def run_until(cond, timeout=10.0, what="condition"):
    deadline = GLib.get_monotonic_time() + int(timeout * 1e6)

    def tick():
        if cond() or GLib.get_monotonic_time() > deadline:
            loop.quit()
            return False
        return True

    GLib.timeout_add(20, tick)
    loop.run()
    assert cond(), f"timed out waiting for {what}"


def pump(seconds):
    """Let the main loop run for a while (async replies, signals)."""
    GLib.timeout_add(int(seconds * 1000), lambda: loop.quit() or False)
    loop.run()


def status_of(ff, handle):
    r = ext.update_file_info_full("provider", handle, "closure", ff)
    if r == OperationResult.COMPLETE:
        return r
    run_until(lambda: any(h == handle for h, _ in completions), what=f"status of {ff.path}")
    return r


ext = mod.GDriveSyncExtension()
client = mod.client()
run_until(lambda: client.roots, what="service roots")
assert client.roots == [str(root)], client.roots

# column
cols = ext.get_columns()
assert len(cols) == 1 and cols[0].attribute == mod.STATUS_ATTRIBUTE

# emblems
pdf = FakeFile(root / "Docs/report.pdf")
assert status_of(pdf, "h1") == OperationResult.IN_PROGRESS
assert pdf.emblems == ["emblem-default"] and pdf.attrs[mod.STATUS_ATTRIBUTE] == "Up to date"

docs = FakeFile(root / "Docs", is_dir=True)
status_of(docs, "h2")
assert docs.emblems == ["emblem-default"]

outside = FakeFile("/etc/hostname")
assert ext.update_file_info_full("p", "h3", "c", outside) == OperationResult.COMPLETE
assert outside.emblems == []

# a new local file: "syncing", then the service's StatusChanged signal makes
# Nautilus re-query, and it's "up to date"
new = root / "Docs/new.txt"
new.write_bytes(b"hello from the harness")
nf = FakeFile(new)
status_of(nf, "h4")
assert nf.emblems == ["emblem-synchronizing"], nf.emblems
run_until(lambda: nf.invalidated > 0, what="StatusChanged signal for new file")
for i in range(50):
    nf.emblems.clear()
    status_of(nf, f"h5-{i}")
    if nf.emblems == ["emblem-default"]:
        break
    pump(0.2)
assert nf.emblems == ["emblem-default"], nf.emblems

# cancelled updates don't complete
before = len(completions)
ext.update_file_info_full("p", "h6", "c", FakeFile(root / "Docs/report.pdf"))
ext.cancel_update("p", "h6")
pump(0.5)
assert not any(h == "h6" for h, _ in completions[before:])

# menus
names = lambda items: [i.name.split("::")[1] for i in items]  # noqa: E731
assert names(ext.get_file_items([pdf])) == ["open", "copy-link"]
assert names(ext.get_file_items([FakeFile(root / "Videos", True)])) == ["open", "copy-link",
                                                                        "exclude"]
assert names(ext.get_file_items([FakeFile(root, True)])) == ["open", "copy-link"]  # root: no exclude
assert ext.get_file_items([outside]) == []
assert ext.get_file_items([pdf, docs]) == []  # multi-select: nothing
assert names(ext.get_file_items(None, [pdf])) == ["open", "copy-link"]  # Nautilus 3 signature

items = {i.name: i for i in ext.get_file_items([pdf])}
items["GDriveSync::open"].emit("activate")
run_until(lambda: launched, what="open in Drive")
assert launched[0].startswith("https://drive.google.com/file/d/"), launched
items["GDriveSync::copy-link"].emit("activate")
run_until(lambda: clipboard, what="copy link")
assert clipboard[0] == launched[0]

# "open" on something not on Drive yet → message, no crash
untracked = root / "not-yet.txt"
items = {i.name: i for i in ext.get_file_items([FakeFile(untracked)])}
untracked.write_bytes(b"x")
items["GDriveSync::open"].emit("activate")
run_until(lambda: messages, what="error message")

# remove local copy (exclude) → the service drops the folder locally
videos = {i.name: i for i in ext.get_file_items([FakeFile(root / "Videos", True)])}
videos["GDriveSync::exclude"].emit("activate")
run_until(lambda: not (root / "Videos").exists(), what="Videos removed locally")

# background "sync now"
bg = ext.get_background_items(FakeFile(root / "Docs", True))
assert names(bg) == ["sync-now"]
bg[0].emit("activate")
assert ext.get_background_items(FakeFile("/tmp", True)) == []

print("nautilus harness: all checks passed", flush=True)
os._exit(0)
