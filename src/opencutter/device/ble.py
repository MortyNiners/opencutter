# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""BLE link to a bonded machine through BlueZ (bleak + dbus-fast).

Link behaviour: the machine accepts a connection only right after power-on, and bluetoothd
drops the link about 2 s after the last D-Bus client using it exits. So one process (the
daemon) holds the link; everything else talks to that process.

The parts:
  * BleTransport: transport.RawTransport over bleak. A dumb pipe: one ATT Write Request (with
    response) per write to CHAR_DATA, replies and status from the CHAR_REPLY and CHAR_STATUS
    indications, and the live MTU from BlueZ. ATT error 0x0E ("Unlikely Error") becomes
    transport.TransportBusy; it never repeats a write by itself.
  * find_bonded_cameo() and BleDiscovery: the paired machines in BlueZ whose name starts with
    one of the registry's BLE name prefixes (machines.ble_name_prefixes: CAMEO, Portrait,
    CURIO) and that offer the vendor service. Nothing is sent to any machine.
  * Cameo4: the daemon's link, a link.SafeLink over a BleTransport. Every check is SafeLink's:
    the read-only allowlist (ESC 05 and FG before the model gate, the profile's queries after
    it), motion writes only through write_validated() and at most min(480, profile.max_write,
    MTU - 3) bytes (none below MTU - 3 = 32), and the profile's stop, ESC 09 twice. The name
    stays from when the CAMEO 4 was the only machine.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable, Collection

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError, BleakError
from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from ..machines import CAMEO4, ble_name_prefixes
from .link import SafeLink
from .protocol import CHAR_DATA, CHAR_REPLY, CHAR_STATUS, SERVICE
from .transport import (MIN_WRITE_LIMIT, BluetoothOff, DeviceRef, PlotterUnreachable,
                        TransportBusy, debug)

__all__ = ["BleDiscovery", "BleTransport", "BluetoothOff", "Cameo4", "MIN_WRITE_LIMIT",
           "PlotterUnreachable", "bluetooth_off_error", "device_ref", "find_bonded_cameo"]

CONNECT_RETRY_WAIT = 2.0     # seconds between connects while BlueZ says one is in progress
CONNECT_ATTEMPT = 30.0       # bleak's own time-out for one connect attempt
ADAPTER = "org.bluez.Adapter1"
# What BlueZ and bleak report when the computer's Bluetooth is off (an adapter that is not
# powered answers Connect with NotReady; bleak has its own error when no adapter is there).
_BLUETOOTH_OFF_DBUS = frozenset({"org.bluez.Error.NotReady"})
_BLUETOOTH_OFF_TYPES = frozenset({"BleakBluetoothNotAvailableError"})
# BlueZ reports ATT error 0x0E as "Operation failed with ATT error: 0x0e"; bleak 3 turns that
# into a GATT protocol error with code 0x0E ("Unlikely Error"), older bleak passes the D-Bus
# error on and adds "(Unlikely Error)". The firmware never picks that code itself: it comes
# from its BLE stack under load (firmware analysis).
_ATT_BUSY = re.compile(r"att error:\s*0x0e\b|unlikely error", re.IGNORECASE)
ATT_UNLIKELY_ERROR = 0x0E


def _paired(props: dict) -> bool:
    """True when BlueZ reports the device Paired (or Bonded, which newer BlueZ also has)."""
    return props.get("Paired") is True or props.get("Bonded") is True


def _named_like_a_machine(name: str) -> bool:
    """True when the BlueZ name starts with one of the registry's BLE name prefixes (any case)."""
    return name.upper().startswith(tuple(p.upper() for p in ble_name_prefixes()))


async def find_bonded_cameo(address: str | None = None,
                            skip: Collection[str] = ()) -> BLEDevice:
    """Find the paired machine in BlueZ (works even while it is connected and not advertising).

    Only a device that BlueZ reports Paired (or Bonded) is returned. With `address`, the device
    with that address, without the name and service checks; when BlueZ knows it but it is not
    paired, PlotterUnreachable says to pair it first. Otherwise the first paired device whose
    name starts with one of the registry's BLE name prefixes (CAMEO, Portrait, CURIO; any case)
    and that offers the vendor BLE service (SERVICE). Which model it is, FG decides later.
    Addresses in `skip` (any case) are never returned: the daemon skips the machines its model
    gate refused. Nothing is sent to any machine: this only reads BlueZ's objects.
    details = {"path": D-Bus path, "props": Device1 properties}.

    Raises BluetoothOff when BlueZ lists no adapter, when the machine is paired only through
    adapters that report Powered false, or when no machine is found and every adapter is off:
    BlueZ keeps listing paired devices while Bluetooth is off, and they cannot be reached then.
    (A machine paired through two adapters is found through the one that is powered.)"""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        intro = await bus.introspect("org.bluez", "/")
        om = bus.get_proxy_object("org.bluez", "/", intro).get_interface(
            "org.freedesktop.DBus.ObjectManager")
        objects = await om.call_get_managed_objects()
    finally:
        bus.disconnect()
    skipped = {a.upper() for a in skip}
    known_unpaired = False
    behind_off_adapter = False     # a paired machine was found, but only on an adapter that is off
    adapters = {path: _powered(ifaces[ADAPTER]) for path, ifaces in objects.items()
                if isinstance(ifaces, dict) and ADAPTER in ifaces}
    if not adapters:
        raise BluetoothOff("Bluetooth is off or not available: the system lists no Bluetooth "
                           "adapter.")
    for path, ifaces in objects.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev:
            continue
        props = {k: v.value for k, v in dev.items()}
        addr = props.get("Address")
        if not isinstance(addr, str) or addr.upper() in skipped:
            continue
        name = props.get("Name", "") or ""
        uuids = [u.lower() for u in props.get("UUIDs", [])]
        if address:
            if addr.upper() != address.upper():
                continue
        elif not (isinstance(name, str) and _named_like_a_machine(name) and SERVICE in uuids):
            continue
        if not _paired(props):
            known_unpaired = True
            continue
        if adapters.get(_adapter_of(path)) is False:
            behind_off_adapter = True
            continue
        return BLEDevice(addr, name, {"path": path, "props": props})
    if behind_off_adapter:
        raise BluetoothOff("Bluetooth is off: the adapter of the machine is not powered.")
    if not any(adapters.values()):
        raise BluetoothOff("Bluetooth is off: no Bluetooth adapter is powered.")
    if address and known_unpaired:
        raise PlotterUnreachable(f"The machine {address} is known to the system but not paired. "
                                 "Pair it first in the system Bluetooth settings.")
    raise PlotterUnreachable("No paired CAMEO was found. "
                             "Pair it first in the system Bluetooth settings.")


def _powered(adapter: dict) -> bool:
    """An Adapter1's Powered property (False when it is missing or not exactly true)."""
    value = adapter.get("Powered")
    return getattr(value, "value", value) is True


def _adapter_of(path: object) -> str | None:
    """The adapter path of a device path: /org/bluez/hci0 for /org/bluez/hci0/dev_..."""
    if not isinstance(path, str) or "/dev_" not in path:
        return None
    return path.rsplit("/dev_", 1)[0]


def bluetooth_off_error(e: BaseException) -> bool:
    """True for an error that means the computer's Bluetooth is off or missing: BluetoothOff,
    BlueZ's org.bluez.Error.NotReady (an adapter that is not powered), or bleak's
    BleakBluetoothNotAvailableError."""
    if isinstance(e, BluetoothOff):
        return True
    if getattr(e, "dbus_error", None) in _BLUETOOTH_OFF_DBUS:
        return True
    return any(cls.__name__ in _BLUETOOTH_OFF_TYPES for cls in type(e).__mro__)


def device_ref(device: BLEDevice) -> DeviceRef:
    """The DeviceRef of a BLEDevice that find_bonded_cameo() returned."""
    details = device.details if isinstance(device.details, dict) else {}
    return DeviceRef("ble", device.address, device.name or "", details=dict(details))


class BleDiscovery:
    """BlueZ's paired machines as a transport.Discovery. Only reads BlueZ's objects."""

    async def find(self, address: str | None = None, skip: Collection[str] = ()) -> DeviceRef:
        """find_bonded_cameo() as a DeviceRef (PlotterUnreachable when none is found)."""
        return device_ref(await find_bonded_cameo(address, skip))

    def held_elsewhere(self, ref: DeviceRef) -> bool:
        """True when BlueZ reports the device Connected. Whenever we hold no link of our own,
        that link belongs to another program (an older daemon, a test, another app)."""
        props = (ref.details or {}).get("props") or {}
        return bool(props.get("Connected"))


def _att_busy(e: BaseException) -> bool:
    """True for bleak's report of ATT error 0x0E ("Unlikely Error"): bleak 3 raises a GATT
    protocol error whose `code` is 0x0E; older bleak a D-Bus error with BlueZ's text."""
    try:
        code = getattr(e, "code", None)
    except Exception:  # noqa: BLE001 - an unreadable code is no busy report
        code = None
    if isinstance(code, int) and not isinstance(code, bool) and code == ATT_UNLIKELY_ERROR:
        return True
    parts = (str(e), getattr(e, "dbus_error", None), getattr(e, "dbus_error_details", None))
    return any(isinstance(p, str) and _ATT_BUSY.search(p) for p in parts)


async def _gatt_write(client, data: bytes) -> None:
    """One ATT Write Request (with response) of exactly `data` to CHAR_DATA. ATT error 0x0E
    becomes TransportBusy (the bytes may or may not have reached the machine); every other
    error is raised as it is."""
    try:
        await client.write_gatt_char(CHAR_DATA, data, response=True)
    except BleakError as e:
        if _att_busy(e):
            raise TransportBusy(f"the machine's Bluetooth was busy (ATT error 0x0E): {e}") from e
        raise


class BleTransport:
    """BlueZ as a transport.RawTransport: one bleak connection to one machine. No safety logic:
    link.SafeLink checks everything that is written through it."""
    has_status_channel = True       # the status characteristic indicates every change

    def __init__(self, device: BLEDevice | DeviceRef, verbose: bool = False):
        if isinstance(device, DeviceRef):
            if device.transport != "ble":
                raise ValueError(f"not a Bluetooth device: {device!r}")
            device = BLEDevice(device.address, device.name, dict(device.details))
        self.device = device
        self.ref = device_ref(device)
        self.verbose = verbose
        self.disconnected = asyncio.Event()
        self.client = BleakClient(device, timeout=CONNECT_ATTEMPT,
                                  disconnected_callback=self._on_disconnect)

    def _on_disconnect(self, _client) -> None:
        self.disconnected.set()

    def is_up(self) -> bool:
        """True while bleak reports the link connected."""
        return bool(self.client.is_connected)

    async def connect(self, timeout: float = 45.0) -> None:
        """Connect, retrying while BlueZ reports a connection already in progress.

        bleak answers InProgress by cancelling the pending attempt, so this is a retry, not a
        wait. It recovers in practice because the machine keeps advertising for a while after
        power-on. After a time-out (an attempt takes up to CONNECT_ATTEMPT seconds) it tries
        again only when another full attempt can end before `timeout`, after a pause of
        CONNECT_RETRY_WAIT; otherwise PlotterUnreachable says to switch the machine off and on."""
        self.disconnected.clear()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                await self.client.connect()
                return
            except BleakDBusError as e:
                if e.dbus_error != "org.bluez.Error.InProgress" or loop.time() > deadline:
                    raise
                if self.verbose:
                    debug("  BlueZ connection already in progress, retrying...")
                await asyncio.sleep(CONNECT_RETRY_WAIT)
            except TimeoutError:
                if loop.time() + CONNECT_RETRY_WAIT + CONNECT_ATTEMPT > deadline:
                    raise PlotterUnreachable(
                        "The machine is not reachable over Bluetooth. It accepts a connection only "
                        "right after power-on: switch it off and on, then retry.") from None
                await asyncio.sleep(CONNECT_RETRY_WAIT)

    async def subscribe(self, on_reply: Callable[[bytes], None],
                        on_status: Callable[[bytes], None] | None) -> None:
        """Deliver the reply indications (and the status indications, unless None) as bytes."""
        await self.client.start_notify(CHAR_REPLY, lambda _char, data: on_reply(bytes(data)))
        if on_status is not None:
            await self.client.start_notify(CHAR_STATUS,
                                           lambda _char, data: on_status(bytes(data)))

    async def unsubscribe(self) -> None:
        """Stop both indications; never raises."""
        for ch in (CHAR_REPLY, CHAR_STATUS):
            with contextlib.suppress(Exception):
                await self.client.stop_notify(ch)

    async def write(self, data: bytes) -> None:
        """One ATT Write Request of exactly `data` (TransportBusy on ATT error 0x0E)."""
        await _gatt_write(self.client, data)

    async def max_write(self) -> int:
        """MTU - 3 for the data characteristic, at least MIN_WRITE_LIMIT.

        bleak caches the characteristic's MTU from service discovery, which can still be the
        pre-exchange 23 because BlueZ does not always signal the later change. So also read the
        live org.bluez.GattCharacteristic1.MTU from bluetoothd: a local D-Bus property read,
        nothing is sent to the machine. Any failure keeps what was found so far."""
        limit = MIN_WRITE_LIMIT
        try:
            char = self.client.services.get_characteristic(CHAR_DATA)
            limit = max(limit, int(char.max_write_without_response_size))
            path = char.obj[0]
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                intro = await bus.introspect("org.bluez", path)
                props = bus.get_proxy_object("org.bluez", path, intro).get_interface(
                    "org.freedesktop.DBus.Properties")
                mtu = await props.call_get("org.bluez.GattCharacteristic1", "MTU")
                limit = max(limit, int(mtu.value) - 3)
            finally:
                bus.disconnect()
        except Exception as e:  # noqa: BLE001 - the safe minimum stays
            if self.verbose:
                debug(f"  (MTU read: {type(e).__name__}: {e}; limit {limit})")
        return limit

    async def release(self) -> None:
        """Free the D-Bus resources of a client whose link is already gone. Never drops a live
        link."""
        if not self.client.is_connected:
            with contextlib.suppress(Exception):
                await self.client.disconnect()

    async def disconnect(self) -> None:
        """BlueZ drops the connection (Device1.Disconnect). Nothing is written to the machine:
        no command, no stop, and its indications are not touched."""
        await self.client.disconnect()


class Cameo4(SafeLink):
    """The daemon's link to a machine over BlueZ: a SafeLink over a BleTransport.

    Kept for its callers and tests: `device` is the BLEDevice it was made for, `client` is
    bleak's BleakClient (every write goes through it), and `machine`, which the daemon sets
    once the model gate passed, also opens the gate with the profile that this link's FG reply
    names when the daemon did not set `profile` itself (machines.detect; it raises
    UnsafeCommand when there is none). Setting `machine` to None closes the gate.

    An object made without __init__ (the tests' bare write guards) has no transport and checks
    with the CAMEO 4's profile, as this class always did; a link made by __init__ starts before
    the gate."""
    _profile = CAMEO4
    _machine: str | None = None

    def __init__(self, device: BLEDevice, verbose: bool = False,
                 on_status: Callable[[str], None] | None = None,
                 on_status_change: Callable[[str | None, str, str], None] | None = None):
        transport = BleTransport(device, verbose=verbose)
        self.client = transport.client
        super().__init__(transport, verbose=verbose, on_status=on_status,
                         on_status_change=on_status_change)
        self.device = device

    @property
    def machine(self) -> str | None:
        """The machine's model as people may see it (protocol.model_text of its FG reply), set
        by the daemon only once this link passed the model gate. None: not checked, not usable."""
        return self._machine

    @machine.setter
    def machine(self, text: str | None) -> None:
        if not text:
            self.profile = None
        elif self.profile is None:
            self.use_detected_profile()
        self._machine = text

    async def _raw_write(self, data: bytes) -> None:
        await _gatt_write(self.client, data)
