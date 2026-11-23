# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Test doubles that keep every test away from Bluetooth, D-Bus and the real daemon.

install() replaces `bleak` and `dbus_fast` in sys.modules with stubs that raise if anything
real is constructed (BleakClient, MessageBus). Call it BEFORE importing opencutter.device.ble
or opencutter.device.daemon. It refuses to run if the real modules are already imported,
because then a forgotten patch could reach the real machine.

FakeClient simulates the machine behind bleak's BleakClient (a CAMEO 4 on firmware V5.05 unless
a test makes it another model: its FG reply, its ESC 15 reply in that model's grammar and its U
reply per TG), FakeBlueZ stands in for BlueZ on the system bus (the paired machines that
find_bonded_cameo lists, and the live MTU), and DaemonHarness runs
opencutter.device.daemon.serve() against them on a private socket and lock in a temp directory
(a private $XDG_RUNTIME_DIR: the service's directory app/<app id> is created inside it). A
harness can register machine profiles that exist only in its test (register) and make the
simulated machine report one of them (simulate).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import types

import pytest

_MARK = "_opencutter_test_stub"
_NAMES = ("bleak", "bleak.backends", "bleak.backends.device", "bleak.exc",
          "dbus_fast", "dbus_fast.aio")

ESC05, ESC09, ESC15, ESC16 = b"\x1b\x05", b"\x1b\x09", b"\x1b\x15", b"\x1b\x16"
ESC07, ESC08 = b"\x1b\x07", b"\x1b\x08"      # the user's Pause and Resume (CAMEO 4)
FG = b"FG\x03"
TESTED_FG = "CAMEO 4 V5.05    "     # the FG reply of the one tested machine (padded, ETX on the wire)
ADDRESS = "00:00:00:00:00:01"        # the simulated machine (a made-up address)
_TG = re.compile(rb"TG(0|[1-9][0-9]?)")


def standard_fg(model: str, version: str = "V1.07") -> str:
    """An FG reply in the exact form every firmware sends: the model text, a space, the version
    and four spaces (ETX is added on the wire). Below TESTED the daemon drives a model only with
    this form (machines.detect)."""
    return f"{model} {version}    "


def install() -> None:
    """Install the stubs (idempotent); raise RuntimeError if real bleak/dbus_fast are loaded."""
    loaded = [n for n in sys.modules if n.split(".")[0] in ("bleak", "dbus_fast")]
    if loaded:
        if all(getattr(sys.modules[n], _MARK, False) for n in loaded):
            return
        raise RuntimeError("refusing to run: the real bleak/dbus_fast are already imported")
    modules = {n: types.ModuleType(n) for n in _NAMES}
    for mod in modules.values():
        setattr(mod, _MARK, True)

    class NoRealClient:
        def __init__(self, *a, **k):
            raise RuntimeError("TEST BUG: real BleakClient constructed")

    class BLEDevice:
        def __init__(self, address, name, details, **kwargs):
            self.address, self.name, self.details = address, name, details

    class BleakError(Exception):
        pass

    class BleakDBusError(BleakError):
        def __init__(self, dbus_error="", error_body=None):
            super().__init__(dbus_error)
            self.dbus_error = dbus_error

    class NoRealBus:
        def __init__(self, *a, **k):
            raise RuntimeError("TEST BUG: real D-Bus MessageBus constructed")

    modules["bleak"].BleakClient = NoRealClient
    modules["bleak.backends.device"].BLEDevice = BLEDevice
    modules["bleak.exc"].BleakError = BleakError
    modules["bleak.exc"].BleakDBusError = BleakDBusError
    modules["dbus_fast"].BusType = types.SimpleNamespace(SYSTEM=1)
    modules["dbus_fast.aio"].MessageBus = NoRealBus
    modules["bleak"].backends = modules["bleak.backends"]
    modules["bleak.backends"].device = modules["bleak.backends.device"]
    modules["bleak"].exc = modules["bleak.exc"]
    modules["dbus_fast"].aio = modules["dbus_fast.aio"]
    sys.modules.update(modules)


def stops(writes: list[bytes]) -> int:
    """Number of complete stop sequences (ESC 09 twice) in a write log."""
    return writes.count(ESC09) // 2


def motion_writes(writes: list[bytes]) -> list[bytes]:
    """The write log without the ESC 05 status polls."""
    return [w for w in writes if w != ESC05]


class FakeClient:
    """Simulated machine behind bleak's BleakClient interface: a CAMEO 4 V5.05 by default.

    Replies to ESC 05/15/16, U and FG. FG answers `fg_reply` (the tested CAMEO 4's by default;
    None: no reply; standard_fg() builds another model's), or for the machine with that address
    `fg_by_address[address]`, and leaves the status at `fg_status` when that is set (a CAMEO 4
    goes from 1 to 2: FG claims the session); its reply comes `fg_delay` seconds later than the
    others. ESC 15 answers `tools` exactly as given, so a test writes it in the grammar of the
    model it simulates (' 2, 4' on the CAMEO 4; ' 12, 0' with two-digit codes; ' 7, 0' for a
    one-holder model). U answers `u_by_tg[n]` after an accepted motion write with TG<n> (None:
    no reply), and `u_reply` otherwise (also before any TG). A new connection starts
    at `start_status`. A motion write keeps it busy for move_s more seconds (writes queue up);
    while busy, ESC 05 answers `busy_status` ('1' on the CAMEO 4; a test profile may use
    another digit).
    ESC 09 while busy is the hard stop: '4', then '0', queue cleared, as in the firmware. Every
    write is logged in `writes`; bytes it does not know go to `unexpected`. DaemonHarness gives
    each test its own subclass, so class settings are local; each link made is in `instances`
    there (one FakeClient per link, like bleak's BleakClient).

    Pause (the firmware analysis of V5.05, simplified): ESC 07, or press_pause() on the panel,
    pauses a machine that is busy or idle: status '3' (indicated), the motion still queued is
    kept (`paused_left` seconds of it; motion written while paused is added). ESC 08, or
    press_pause() again, goes on from there: '1' while that runs, or '0' at once when nothing
    was left; ESC 08 does nothing unless paused. While paused U and FG are not answered. ESC 09
    while paused ends the job: with motion left it stays '3' for `cancel_quiet_s`, then '4',
    then '0'; with nothing left '4' at once, then '0'. hold_pause() (a long press) is the same
    cancel, press_unload() a cancel followed by '2' (the mat goes out). Each change is
    indicated. A status set by a test without these (status = '3') keeps the old behaviour.

    Writes are serialized like BlueZ's: one WriteValue per characteristic is in flight at a
    time, and a second one gets org.bluez.Error.InProgress, which bleak 3.x retries every
    10 ms. So a write (an ESC 09 included) never overtakes one whose Write Response is still
    due. serialize_writes = False gives the old fake, where a write can overtake another."""
    tools = " 2, 4"
    u_reply: str | None = "6100,6100"
    u_by_tg: types.MappingProxyType = types.MappingProxyType({})   # TG value -> U reply after it
    fg_reply: str | None = TESTED_FG
    fg_by_address: types.MappingProxyType = types.MappingProxyType({})   # address -> FG reply
    fg_status: str | None = None   # the status FG leaves the machine in (None: unchanged)
    fg_delay = 0.0             # seconds more before the FG reply comes (3 s and more: too late)
    start_status = "0"
    busy_status = "1"          # the ESC 05 reply while motion is queued
    move_s = 0.6
    write_delay = 0.0          # seconds a data write takes to be acknowledged
    serialize_writes = True    # one write in flight at a time, as with BlueZ (see above)
    mtu_payload = 497          # what bleak's cached characteristic reports (MTU-3)
    char_obj = None            # (D-Bus path, props) like bleak's BlueZ backend; None = no path
    accept: frozenset = frozenset()   # motion writes this machine accepts
    accept_any = False         # accept any ETX-terminated data write as motion
    # like bleak on a dead link: a write while not connected raises BleakError("Not connected")
    # and a dead link delivers no reply (False: the old fake, which takes writes after a drop)
    fail_when_disconnected = False
    cancel_quiet_s = 5.0       # a cancel while paused with motion left: '3' this long, then '4'
    command_delay = 0.0        # seconds an ESC 07 or ESC 08 write takes to be acknowledged
    instance = None            # the last one made
    instances: list | None = None   # every one made (a harness's subclass sets a list)

    def __init__(self, dev, timeout, disconnected_callback):
        self.address = getattr(dev, "address", None)
        self.cb_disc = disconnected_callback
        self.is_connected = False
        self.cbs = {}
        self.writes: list[bytes] = []
        self.unexpected: list[bytes] = []
        self.moving_until = 0.0
        self.queued = 0             # motion bytes written since the machine was last idle
        self.max_queued = 0
        self.runs: list[int] = []   # bytes queued per busy period (one entry per idle -> busy)
        self.status = type(self).start_status
        self.status_override = None
        self.fail_esc09 = False
        self.on_write = None
        self.in_flight = False      # a write's Write Response is still due
        self.last_tg: int | None = None   # the TG of the last accepted motion write that had one
        self.paused_left: float | None = None   # paused (ESC 07, press_pause): motion left, s
        self.ignored: list[bytes] = []   # U and FG that a paused machine did not answer
        type(self).instance = self
        if type(self).instances is not None:
            type(self).instances.append(self)
        cls = type(self)
        self.services = types.SimpleNamespace(
            get_characteristic=lambda u: types.SimpleNamespace(
                max_write_without_response_size=cls.mtu_payload, obj=cls.char_obj))

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, ch, cb):
        self.cbs[ch] = cb

    async def stop_notify(self, ch):
        pass

    def _reply(self, text: str, delay: float = 0.0) -> None:
        from opencutter.device.protocol import CHAR_REPLY
        if type(self).fail_when_disconnected and not self.is_connected:
            return
        loop = asyncio.get_running_loop()
        loop.call_later(0.02 + delay, self.cbs[CHAR_REPLY], None,
                        bytearray(text.encode() + b"\x03"))

    def moving(self) -> bool:
        return asyncio.get_running_loop().time() < self.moving_until

    # the panel and the pause ---------------------------------------------------------------
    def show(self, status: str) -> None:
        """Set the status and indicate it, as the machine does on every change."""
        from opencutter.device.protocol import CHAR_STATUS
        self.status = status
        cb = self.cbs.get(CHAR_STATUS)
        if cb is not None and (self.is_connected or not type(self).fail_when_disconnected):
            cb(None, bytearray(status.encode() + b"\x03"))

    def _later(self, seconds: float, status: str) -> None:
        asyncio.get_running_loop().call_later(seconds, self.show, status)

    def pause_now(self) -> None:
        """ESC 07, or a short press of Pause while busy or idle (ignored while paused)."""
        if self.paused_left is not None or self.status not in ("0", "1"):
            return
        loop = asyncio.get_running_loop()
        self.paused_left = max(0.0, self.moving_until - loop.time())
        self.moving_until = 0.0
        self.show("3")

    def go_on(self) -> None:
        """ESC 08, or a short press of Pause while paused: on from where it stopped."""
        if self.paused_left is None:
            return
        left, self.paused_left = self.paused_left, None
        loop = asyncio.get_running_loop()
        if left > 0:
            self.moving_until = loop.time() + left
            self.show("1")
            self.status = "0"               # Busy while moving (cur_status), Ready after
            loop.call_later(left, lambda: self.moving() or self.show("0"))
        else:
            self.show("0")

    def cancel_paused(self, then: str | None = None) -> None:
        """The cancel of a paused machine (ESC 09, a long press, Unload): see the class doc.
        `then`: a status shown 0.5 s after the '0' (Unload: '2')."""
        left, self.paused_left = self.paused_left, None
        quiet = type(self).cancel_quiet_s if left else 0.0
        self.queued = 0
        self._later(quiet, "4")
        self._later(quiet + 0.3, "0")
        if then is not None:
            self._later(quiet + 0.8, then)

    def press_pause(self) -> None:
        """A short press of the panel's Pause key."""
        if self.paused_left is None:
            self.pause_now()
        else:
            self.go_on()

    def hold_pause(self) -> None:
        """A long press of Pause while paused: a cancel."""
        if self.paused_left is not None:
            self.cancel_paused()

    def press_unload(self) -> None:
        """Unload while paused: flush, cancel, then the mat goes out ('4', '0', '2')."""
        if self.paused_left is not None:
            self.paused_left = 0.0
            self.cancel_paused(then="2")

    def cur_status(self) -> str:
        if self.status_override is not None:
            return self.status_override
        if self.status == "0" and self.moving():
            return type(self).busy_status
        return self.status

    def _is_motion(self, data: bytes) -> bool:
        if data in type(self).accept:
            return True
        return type(self).accept_any and data[:1] != b"\x1b" and data.endswith(b"\x03")

    async def write_gatt_char(self, ch, data, response):
        from opencutter.device.protocol import CHAR_DATA
        assert ch == CHAR_DATA and response is True
        if type(self).fail_when_disconnected and not self.is_connected:
            raise sys.modules["bleak.exc"].BleakError("Not connected")
        if not type(self).serialize_writes:
            return await self._write(bytes(data))
        while self.in_flight:            # BlueZ: InProgress; bleak retries every 10 ms
            await asyncio.sleep(0.01)
        self.in_flight = True
        try:
            return await self._write(bytes(data))
        finally:
            self.in_flight = False

    async def _write(self, data: bytes) -> None:
        loop = asyncio.get_running_loop()
        if data == ESC09 and self.fail_esc09:
            raise sys.modules["bleak.exc"].BleakError("ATT write failed (simulated)")
        self.writes.append(data)
        if self.on_write:
            self.on_write(data)
        paused = self.paused_left is not None
        if data in (ESC07, ESC08):
            if type(self).command_delay:
                await asyncio.sleep(type(self).command_delay)
            self.pause_now() if data == ESC07 else self.go_on()
        elif paused and data == ESC09:
            self.cancel_paused()
        elif paused and data in (b"U\x03", FG):
            self.ignored.append(data)          # a paused machine answers no command
        elif paused and self._is_motion(data):
            self.paused_left += type(self).move_s
            self.queued += len(data)
        elif data == ESC05:
            self._reply(self.cur_status())
        elif data == ESC15:
            self._reply(type(self).tools)
        elif data == ESC16:
            self._reply("   0")
        elif data == b"U\x03":
            by_tg = type(self).u_by_tg
            reply = by_tg[self.last_tg] if self.last_tg in by_tg else type(self).u_reply
            if reply is not None:
                self._reply(reply)
        elif data == FG:
            reply = type(self).fg_by_address.get(self.address, type(self).fg_reply)
            if reply is not None:
                self._reply(reply, type(self).fg_delay)
            if type(self).fg_status is not None:
                self.status = type(self).fg_status
        elif data == ESC09:
            if self.moving():
                self.moving_until = 0.0
                self.queued = 0
                self.status = "4"
                loop.call_later(0.3, lambda: setattr(self, "status", "0"))
        elif self._is_motion(data):
            for cmd in data.split(b"\x03"):
                tg = _TG.fullmatch(cmd)
                if tg:
                    self.last_tg = int(tg[1])
            if not self.moving():
                self.queued = 0
                self.runs.append(0)
            self.queued += len(data)
            self.runs[-1] += len(data)
            self.max_queued = max(self.max_queued, self.queued)
            self.moving_until = max(loop.time(), self.moving_until) + type(self).move_s
        else:
            self.unexpected.append(data)
            raise AssertionError(f"machine received unexpected bytes {data!r}")
        if type(self).write_delay and data[:1] != b"\x1b":
            await asyncio.sleep(type(self).write_delay)


def bluez_device(address: str = ADDRESS, name: str = "CAMEO 4-fake", *, paired: bool = True,
                 connected: bool = False, uuids: list[str] | None = None, **props) -> dict:
    """The Device1 properties of a machine as BlueZ lists it: paired, not connected, offering
    the vendor BLE service, unless told otherwise. Extra properties (Bonded=...) go in as given."""
    from opencutter.device.protocol import SERVICE
    return {"Address": address, "Name": name, "UUIDs": [SERVICE] if uuids is None else uuids,
            "Paired": paired, "Connected": connected, **props}


class FakeBlueZ:
    """Stands in for dbus_fast's MessageBus on the system bus (ble.MessageBus), so that the real
    find_bonded_cameo and refresh_write_limit run without D-Bus.

    BlueZ's object tree lists an adapter and `devices` (Device1 properties as bluez_device()
    makes them), in order. The data characteristic's MTU property answers `mtu` (None: that
    read fails). With `unreachable`, the bus itself cannot be reached (Bluetooth off, or a
    Flatpak without the org.bluez permission). `adapter_powered` is the adapter's Powered
    property (False: Bluetooth switched off, the paired devices still listed); with `adapter`
    False no adapter is listed at all. `second_adapter` (None: none) is the Powered property of
    a second adapter, hci1, through which every device is paired as well (listed after hci0's).
    `looks` counts the GetManagedObjects calls. A harness gives each test its own subclass, so
    these settings are local."""
    devices: tuple | list = ()          # Device1 properties; a subclass sets its own list
    mtu: int | None = None
    unreachable = False
    adapter = True
    adapter_powered = True
    second_adapter: bool | None = None
    looks = 0

    class _Variant:
        def __init__(self, value):
            self.value = value

    def __init__(self, bus_type=None):
        assert bus_type == 1                     # the stub's BusType.SYSTEM

    async def connect(self) -> "FakeBlueZ":
        if type(self).unreachable:
            raise FileNotFoundError(2, "No such file or directory")
        return self

    async def introspect(self, service: str, path: str) -> str:
        assert service == "org.bluez", service
        return path

    def get_proxy_object(self, service: str, path: str, intro: str):
        cls = type(self)
        variant = FakeBlueZ._Variant

        async def call_get_managed_objects() -> dict:
            assert path == "/", path
            cls.looks += 1
            objects = ({"/org/bluez/hci0": {
                "org.bluez.Adapter1": {"Powered": variant(cls.adapter_powered)}}}
                if cls.adapter else {})
            adapters = ["hci0"]
            if cls.second_adapter is not None:
                objects["/org/bluez/hci1"] = {
                    "org.bluez.Adapter1": {"Powered": variant(cls.second_adapter)}}
                adapters.append("hci1")
            for hci in adapters:
                for dev in cls.devices:
                    where = f"/org/bluez/{hci}/dev_" + str(dev.get("Address")).replace(":", "_")
                    objects[where] = {"org.freedesktop.DBus.Properties": {},
                                      "org.bluez.Device1": {k: variant(v)
                                                            for k, v in dev.items()}}
            return objects

        async def call_get(interface: str, prop: str):
            assert (interface, prop) == ("org.bluez.GattCharacteristic1", "MTU")
            if cls.mtu is None:
                raise RuntimeError("no MTU property (simulated)")
            return variant(cls.mtu)

        def get_interface(name: str):
            if name == "org.freedesktop.DBus.ObjectManager":
                return types.SimpleNamespace(call_get_managed_objects=call_get_managed_objects)
            assert name == "org.freedesktop.DBus.Properties", name
            return types.SimpleNamespace(call_get=call_get)
        return types.SimpleNamespace(get_interface=get_interface)

    def disconnect(self) -> None:
        pass


class Answers:
    """Scripted operator for the CLI's ainput()."""

    def __init__(self, *answers: str):
        self.answers = list(answers)

    async def __call__(self, prompt: str) -> str:
        if not self.answers:
            raise AssertionError(f"unexpected prompt {prompt!r}")
        return self.answers.pop(0)


class DaemonHarness:
    """Runs opencutter.device.daemon.serve() against a FakeClient on private temp paths.

    The daemon finds the machine with the real find_bonded_cameo, in `bluez` (a FakeBlueZ):
    one paired machine, CAMEO 4-fake at ADDRESS, unless a test changes `bluez.devices`
    (add_machine() adds another one with its own FG reply)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        install()
        from opencutter.device import ble, daemon, programs
        self.daemon = daemon
        self.monkeypatch = monkeypatch
        base = tempfile.gettempdir()
        self.dir = tempfile.mkdtemp(prefix="oct-", dir=base if len(base) < 60 else "/tmp")
        from opencutter import APP_ID
        self.service_dir = os.path.join(self.dir, "app", APP_ID)    # protocol.runtime_dir()
        self.socket = os.path.join(self.service_dir, "opencutter.sock")
        self.lock = os.path.join(self.service_dir, "opencutter.lock")
        self.old_socket = os.path.join(self.dir, "opencutter.sock")  # builds before 0.1.0
        self.old_lock = os.path.join(self.dir, "opencutter.lock")
        self.legacy_lock = os.path.join(self.dir, "cameo4.lock")   # daemon.legacy_lock_path()
        monkeypatch.setenv("XDG_RUNTIME_DIR", self.dir)
        monkeypatch.setenv("XDG_STATE_HOME", self.dir)
        monkeypatch.delenv("FLATPAK_ID", raising=False)
        monkeypatch.setattr(daemon, "SOCKET_PATH", self.socket)
        monkeypatch.setattr(daemon, "LOCK_PATH", self.lock)
        monkeypatch.setattr(daemon, "OLD_LOCK_PATH", self.old_lock)
        self.fake_cls = type("Fake", (FakeClient,), {"accept": programs.PINNED_WRITES,
                                                     "instances": [], "fg_by_address": {}})
        monkeypatch.setattr(ble, "BleakClient", self.fake_cls)
        self.bluez = type("BlueZ", (FakeBlueZ,), {"devices": [bluez_device()]})
        monkeypatch.setattr(ble, "MessageBus", self.bluez)
        assert daemon.find_bonded_cameo is ble.find_bonded_cameo   # the real one, on self.bluez
        self.log: list[str] = []
        monkeypatch.setattr(daemon, "_log", self.log.append)
        self._sigint = signal.getsignal(signal.SIGINT)
        self.task: asyncio.Task | None = None

    async def _find(self, address=None, skip=()):
        """The real find_bonded_cameo on this harness's BlueZ (for tests that wrap it)."""
        from opencutter.device import ble
        return await ble.find_bonded_cameo(address, skip=skip)

    @property
    def connected_prop(self) -> bool:
        """BlueZ reports the first machine Connected (by us, or by another program)."""
        return bool(self.bluez.devices[0]["Connected"])

    @connected_prop.setter
    def connected_prop(self, value: bool) -> None:
        self.bluez.devices[0]["Connected"] = value

    def add_machine(self, address: str, name: str, fg: str | None = TESTED_FG, **props) -> dict:
        """Another machine in BlueZ (paired by default), whose FG answers `fg`."""
        dev = bluez_device(address, name, **props)
        self.bluez.devices.append(dev)
        self.fake_cls.fg_by_address[address] = fg
        return dev

    def register(self, *profiles) -> None:
        """Add machine profiles that exist only in this test to the registry, for this test only
        (the harness's monkeypatch undoes it): FG names them (machines.detect) and a JobSpec finds
        them by id. Each one must pass the registry's self-check (machines.safety.check_entry)."""
        from opencutter.machines import registry, safety
        for profile in profiles:
            safety.check_entry(profile)
        self.monkeypatch.setattr(registry, "INDEX",
                                 registry.Index((*registry.entries(), *profiles)))

    def simulate(self, profile, version: str | None = None, **settings) -> None:
        """Make the first simulated machine report `profile`'s model in the firmware's exact form
        (standard_fg: its FG model text and `version`, by default its first firmware line with
        minor 05) under the BlueZ name '<name>-fake'. `settings` are FakeClient class settings
        (tools=..., u_by_tg=...)."""
        model = sorted(profile.identity.fg_models)[0]
        version = version or f"V{profile.identity.lines[0].major}.05"
        self.fake_cls.fg_reply = standard_fg(model, version)
        self.bluez.devices[0]["Name"] = f"{profile.name}-fake"
        for key, value in settings.items():
            setattr(self.fake_cls, key, value)

    def machine(self, address: str = ADDRESS) -> list[FakeClient]:
        """Every simulated link made to the machine with `address`, in order."""
        return [f for f in self.fake_cls.instances if f.address == address]

    @property
    def fake(self) -> FakeClient:
        return self.fake_cls.instance

    def client(self):
        from opencutter.device.client import DaemonClient
        return DaemonClient(self.socket)

    async def start(self, idle_exit: float = 0, address: str | None = None, force: bool = False,
                    until=None) -> None:
        """Start serve() and wait until its ping says connected, or until `until(ping)`."""
        self.task = asyncio.create_task(self.daemon.serve(address, False, force,
                                                          idle_exit=idle_exit))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if self.task.done():
                self.task.result()
            if not os.path.exists(self.socket):
                continue
            if until is not None:
                if until(await self.ping()):
                    return
            elif self.fake and self.fake.is_connected and (await self.ping()).get("connected"):
                return
        raise AssertionError("fake daemon did not come up")

    async def shutdown(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    async def raw(self, lines: list[dict], read: bool = True, timeout: float = 15):
        """Open a connection, send `lines`, read one reply per line; returns (r, w, replies)."""
        r, w = await asyncio.open_unix_connection(self.socket, limit=64 << 20)
        out = []
        for line in lines:
            w.write((json.dumps(line) + "\n").encode())
            await w.drain()
            if read:
                out.append(json.loads(await asyncio.wait_for(r.readline(), timeout)))
        return r, w, out

    async def ping(self) -> dict:
        return (await self.raw([{"op": "ping"}]))[2][0]

    async def fresh(self, **settings) -> FakeClient:
        """Reset the fake machine (and set class settings such as tools=...) between scenarios."""
        for key in ("tools", "u_reply", "u_by_tg", "move_s", "write_delay", "mtu_payload",
                    "serialize_writes", "fg_reply", "fg_status", "busy_status",
                    "fail_when_disconnected", "cancel_quiet_s", "command_delay"):
            setattr(self.fake_cls, key, getattr(FakeClient, key))
        for key, value in settings.items():
            setattr(self.fake_cls, key, value)
        f = self.fake
        f.status_override = None
        f.status = "0"
        f.moving_until = 0.0
        f.paused_left = None
        f.ignored.clear()
        f.fail_esc09 = False
        f.on_write = None
        f.last_tg = None
        await asyncio.sleep(0.05)
        f.writes.clear()
        f.queued = f.max_queued = 0
        f.runs.clear()
        return f

    def run(self, scenario, timeout: float = 90, **start):
        """Start the daemon (`start`: the options of start()), await scenario(self), stop the
        daemon; returns the scenario's result."""
        async def main():
            await self.start(**start)
            try:
                return await asyncio.wait_for(scenario(self), timeout)
            finally:
                await self.shutdown()
        try:
            return asyncio.run(main())
        finally:
            signal.signal(signal.SIGINT, self._sigint)   # serve() ignores SIGINT while stopping

    def close(self) -> None:
        signal.signal(signal.SIGINT, self._sigint)
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def harness(monkeypatch):
    """A DaemonHarness, cleaned up after the test. Import this name into a test module."""
    h = DaemonHarness(monkeypatch)
    yield h
    unexpected = [w for f in h.fake_cls.instances for w in f.unexpected]
    assert not unexpected, f"unexpected writes: {unexpected}"
    h.close()
