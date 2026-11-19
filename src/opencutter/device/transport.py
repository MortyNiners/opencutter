# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The transport interface: a dumb pipe to one machine, and how machines are found.

No bleak and no D-Bus here: ble.py implements both protocols for BlueZ (BleTransport,
BleDiscovery); a USB transport comes later as a module of its own. A RawTransport has no safety
logic at all. link.SafeLink, which wraps one, decides what may be written and checks every
reply, the same way over every transport.

RawTransport (one connection to one machine)
  ref                  the DeviceRef it was made for.
  disconnected         an asyncio.Event, set when the connection is lost; connect() clears it.
  has_status_channel   True when the machine reports status changes by itself (BLE indications);
                       False when only ESC 05 polls tell (USB).
  is_up()              True while the connection is up.
  connect(timeout)     connect, giving up after `timeout` seconds (PlotterUnreachable).
  subscribe(on_reply, on_status)
                       deliver the bytes of every reply indication to on_reply and of every
                       status indication to on_status (None: not wanted). Replies are passed on
                       as they come: framing them is the link's job.
  unsubscribe()        stop delivering; never raises.
  write(data)          one acknowledged write of exactly `data`, never split or merged with
                       another. TransportBusy when the transport could not take it (BLE: ATT
                       error 0x0E); whether those bytes reached the machine is then unknown, so
                       it never repeats a write by itself.
  max_write()          the longest single write it can deliver now (BLE: the live MTU - 3, at
                       least MIN_WRITE_LIMIT); the link applies its own limits on top.
  release()            free the resources of a connection that is already gone; never drops a
                       live one.
  disconnect()         drop the connection on purpose; writes nothing to the machine.

Discovery (how the daemon finds a machine; only reads the system's state, sends nothing)
  find(address, skip)  the DeviceRef to try, or PlotterUnreachable. With `address`, that
                       machine; addresses in `skip` (any case) are never returned.
                       BluetoothOff when the computer's Bluetooth is off.
  held_elsewhere(ref)  True when another program holds that machine's connection.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# The smallest write every transport takes: BLE before the MTU exchange (MTU 23 - 3 bytes).
MIN_WRITE_LIMIT = 20


class PlotterUnreachable(RuntimeError):
    """No paired CAMEO was found (or the one named is not paired), or it does not accept a
    connection."""


class BluetoothOff(RuntimeError):
    """The computer's Bluetooth is off or missing: BlueZ lists no adapter, or the adapter of the
    machine (or every adapter) reports Powered false. Not a PlotterUnreachable, which says that
    the machine is not paired or does not accept a connection."""


class TransportBusy(RuntimeError):
    """The transport could not take a write now (BLE: ATT error 0x0E, "Unlikely Error"). Whether
    the bytes reached the machine is unknown: only a write that is harmless to repeat (a
    read-only query) may be written again; for motion data it is an error like any other."""


@dataclass(frozen=True)
class DeviceRef:
    """One machine a Discovery found: what a transport is made for."""
    transport: str                     # "ble" (later "usb")
    address: str                       # BLE address or USB bus path: what skip sets hold
    name: str                          # shown to people
    usb_id: tuple[int, int] | None = None
    # transport-specific (BLE: BlueZ's D-Bus "path" and Device1 "props")
    details: dict = field(default_factory=dict, compare=False)


@runtime_checkable
class RawTransport(Protocol):
    """A dumb pipe to one machine (see the module docstring). No safety logic."""
    ref: DeviceRef
    disconnected: asyncio.Event
    has_status_channel: bool

    def is_up(self) -> bool: ...

    async def connect(self, timeout: float) -> None: ...

    async def subscribe(self, on_reply: Callable[[bytes], None],
                        on_status: Callable[[bytes], None] | None) -> None: ...

    async def unsubscribe(self) -> None: ...

    async def write(self, data: bytes) -> None: ...

    async def max_write(self) -> int: ...

    async def release(self) -> None: ...

    async def disconnect(self) -> None: ...


@runtime_checkable
class Discovery(Protocol):
    """How the daemon finds a machine (see the module docstring). Sends nothing."""

    async def find(self, address: str | None, skip: Collection[str]) -> DeviceRef: ...

    def held_elsewhere(self, ref: DeviceRef) -> bool: ...


def debug(msg: str) -> None:
    """Print to stderr; never raises (a hung-up terminal must not break the link)."""
    with contextlib.suppress(OSError, ValueError):
        print(msg, file=sys.stderr, flush=True)
