"""Session-bus API of the sync service (used by the Nautilus extension, and later
the tray app).

Runs on its own thread with its own connection. Status queries read through a
separate read-only DB connection, so they never wait for the sync thread; anything
that changes sync state is handed to the sync thread via ``Daemon.call_soon``.

    busctl --user introspect io.github.jvandevelde.GDriveSync /io/github/jvandevelde/GDriveSync
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from jeepney import DBusAddress, HeaderFields, MessageType, new_error, new_method_return, new_signal
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection

from gdrivesync import config
from gdrivesync.status import StatusIndex

log = logging.getLogger("gdrivesync.dbus")

BUS_NAME = "io.github.jvandevelde.GDriveSync"
OBJECT_PATH = "/io/github/jvandevelde/GDriveSync"
INTERFACE = BUS_NAME
ERROR_NAME = BUS_NAME + ".Error"
SIGNAL_FLUSH_S = 0.3
MAX_BATCH = 500

INTROSPECTION = f"""<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="{INTERFACE}">
    <method name="GetRoots"><arg type="as" direction="out"/></method>
    <method name="GetFileStatus">
      <arg name="path" type="s" direction="in"/><arg type="s" direction="out"/>
    </method>
    <method name="GetFileStatuses">
      <arg name="paths" type="as" direction="in"/><arg type="a{{ss}}" direction="out"/>
    </method>
    <method name="GetDriveLink">
      <arg name="path" type="s" direction="in"/><arg type="s" direction="out"/>
    </method>
    <method name="GetState"><arg name="json" type="s" direction="out"/></method>
    <method name="SyncNow"/>
    <method name="Pause"/>
    <method name="Resume"/>
    <method name="Exclude">
      <arg name="path" type="s" direction="in"/><arg type="s" direction="out"/>
    </method>
    <method name="Include">
      <arg name="path" type="s" direction="in"/><arg type="s" direction="out"/>
    </method>
    <signal name="StatusChanged"><arg name="paths" type="as"/></signal>
    <signal name="StateChanged"/>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/>
  </interface>
</node>
"""


class ApiError(Exception):
    pass


class DBusService:
    def __init__(self, daemon, *, bus: str = "SESSION"):
        self.daemon = daemon
        self.bus = bus
        self.index = StatusIndex(daemon)
        self._changed: set[str] = set()
        self._state_changed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.available = False
        self._thread = threading.Thread(target=self._run, name="dbus", daemon=True)
        self._emitter = DBusAddress(OBJECT_PATH, interface=INTERFACE)
        daemon.status_listeners.append(self._on_status_changed)

    # ---------------------------------------------------------------- lifecycle
    def start(self, timeout: float = 5.0) -> bool:
        self._thread.start()
        self._ready.wait(timeout)
        return self.available

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.index.close()

    def _run(self) -> None:
        try:
            conn = open_dbus_connection(bus=self.bus)
        except Exception as e:  # noqa: BLE001 — headless box, no session bus
            log.info("No D-Bus session bus (%s); desktop integration disabled.", e)
            self._ready.set()
            return
        try:
            reply = conn.send_and_get_reply(message_bus.RequestName(BUS_NAME, 4))  # DO_NOT_QUEUE
            if reply.body[0] != 1:  # PRIMARY_OWNER
                log.warning("D-Bus name %s is taken; desktop integration disabled.", BUS_NAME)
                self._ready.set()
                return
            self.available = True
            self._ready.set()
            log.info("D-Bus API available as %s", BUS_NAME)
            last_flush = time.monotonic()
            while not self._stop.is_set():
                try:
                    msg = conn.receive(timeout=SIGNAL_FLUSH_S)
                except TimeoutError:
                    msg = None
                if msg is not None and msg.header.message_type == MessageType.method_call:
                    conn.send(self._dispatch(msg))
                if time.monotonic() - last_flush >= SIGNAL_FLUSH_S:
                    self._flush_signals(conn)
                    last_flush = time.monotonic()
        except Exception:  # noqa: BLE001
            log.exception("D-Bus service stopped")
        finally:
            self.available = False
            self._ready.set()
            conn.close()

    # ---------------------------------------------------------------- signals
    def _on_status_changed(self, paths: list[str]) -> None:
        roots = self.index.roots()
        with self._lock:
            for p in paths:
                self._changed.add(p)
                # Folder emblems summarize their contents: refresh the ancestors too.
                root = next((r for r in roots if p == r or p.startswith(r + "/")), None)
                parent = os.path.dirname(p)
                while root and len(parent) >= len(root):
                    self._changed.add(parent)
                    parent = os.path.dirname(parent)

    def notify_state_changed(self) -> None:
        with self._lock:
            self._state_changed = True

    def _flush_signals(self, conn) -> None:
        with self._lock:
            changed, self._changed = sorted(self._changed), set()
            state, self._state_changed = self._state_changed, False
        for i in range(0, len(changed), MAX_BATCH):
            conn.send(new_signal(self._emitter, "StatusChanged", "as", (changed[i:i + MAX_BATCH],)))
        if state:
            conn.send(new_signal(self._emitter, "StateChanged"))

    # ---------------------------------------------------------------- methods
    def _dispatch(self, msg):
        fields = msg.header.fields
        iface = fields.get(HeaderFields.interface)
        member = fields.get(HeaderFields.member)
        path = fields.get(HeaderFields.path)
        try:
            if iface == "org.freedesktop.DBus.Introspectable" and member == "Introspect":
                return new_method_return(msg, "s", (INTROSPECTION if path == OBJECT_PATH else "<node/>",))
            if iface == "org.freedesktop.DBus.Peer" and member == "Ping":
                return new_method_return(msg)
            if path != OBJECT_PATH or iface not in (None, INTERFACE):
                return new_error(msg, "org.freedesktop.DBus.Error.UnknownObject", "s",
                                 (f"No object {path} / {iface}",))
            handler = getattr(self, f"_m_{member}", None)
            if handler is None:
                return new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod", "s",
                                 (f"No method {member}",))
            sig, body = handler(*msg.body)
            return new_method_return(msg, sig, body)
        except ApiError as e:
            return new_error(msg, ERROR_NAME, "s", (str(e),))
        except TypeError as e:
            return new_error(msg, "org.freedesktop.DBus.Error.InvalidArgs", "s", (str(e),))
        except Exception as e:  # noqa: BLE001
            log.exception("D-Bus %s failed", member)
            return new_error(msg, "org.freedesktop.DBus.Error.Failed", "s", (str(e),))

    def _m_GetRoots(self):
        return "as", (self.index.roots(),)

    def _m_GetFileStatus(self, path):
        return "s", (self.index.status(path),)

    def _m_GetFileStatuses(self, paths):
        return "a{ss}", ({p: self.index.status(p) for p in paths},)

    def _m_GetDriveLink(self, path):
        return "s", (self.index.link(path),)

    def _m_GetState(self):
        recent, seen = [], set()
        pairs = []
        for slot in self.daemon.slots:
            eng = slot.engine
            for e in list(eng.oplog.recent):
                key = (e.get("ts"), e.get("op"), e.get("path"), e.get("pair"))
                if key not in seen:
                    seen.add(key)
                    recent.append(e)
            errors = dict(list(eng.errors.items())[:50])
            pairs.append({
                "name": eng.name,
                "local": str(eng.root),
                "status": self.index.status(str(eng.root)),
                "paused": slot.disabled,
                "errors": errors,
                "active": sorted(eng.active)[:50],
            })
        recent.sort(key=lambda e: e.get("ts", ""))
        state = {"paused": self.daemon.paused, "pairs": pairs, "recent": recent[-20:]}
        return "s", (json.dumps(state),)

    def _m_SyncNow(self):
        self.daemon.sync_now()
        return None, ()

    def _m_Pause(self):
        self.daemon.paused = True
        self.notify_state_changed()
        return None, ()

    def _m_Resume(self):
        self.daemon.paused = False
        self.daemon.sync_now()
        self.notify_state_changed()
        return None, ()

    def _m_Exclude(self, path):
        from gdrivesync.pairs import PairError, exclude_path

        settings = config.Settings.load()
        try:
            pair = exclude_path(settings, Path(path))
        except PairError as e:
            raise ApiError(str(e)) from None
        settings.save()
        ids = set(pair.excluded_folder_ids)
        self._apply_exclusions(pair.name, ids)
        return "s", (f"{path} will be removed locally; it stays on Google Drive.",)

    def _m_Include(self, path):
        from gdrivesync.pairs import PairError, include_path

        settings = config.Settings.load()
        try:
            pair = include_path(settings, Path(path))
        except PairError as e:
            raise ApiError(str(e)) from None
        settings.save()
        self._apply_exclusions(pair.name, set(pair.excluded_folder_ids))
        return "s", (f"{path} will be downloaded again.",)

    def _apply_exclusions(self, pair_name: str, ids: set[str]) -> None:
        def apply():
            slot = self.daemon.slot_for(pair_name)
            if slot is not None:
                slot.engine.excluded = set(ids)
                slot.mark(time.monotonic())
        self.daemon.call_soon(apply)
