# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The daemon's ping says why it holds no link ("reason"), and sends nothing for it.

Runs the real daemon against the simulated machine (tests/stubs.py): stub bleak/dbus_fast, a
private socket and private locks in a temp directory; nothing reaches Bluetooth.
"""
import asyncio
import sys

import pytest

import stubs
from stubs import harness  # noqa: F401

stubs.install()
from opencutter.device import daemon  # noqa: E402
from opencutter.device.protocol import (REASON_HELD_ELSEWHERE, REASON_NO_BLUETOOTH,  # noqa: E402
                                        REASON_NOT_PAIRED, REASON_UNREACHABLE,
                                        REASON_WRONG_MODEL)


async def _ping_until(h, predicate, timeout: float = 10.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            reply = await h.ping()
        except OSError:                 # the socket is not there yet
            reply = None
        if reply is not None and predicate(reply):
            return reply
        assert loop.time() < deadline, f"timed out, last ping {reply}"
        await asyncio.sleep(0.05)


async def _serve_without_link(h, scenario):
    """Run serve() (which never connects here) around ``scenario(h)``, then stop it."""
    task = asyncio.create_task(daemon.serve(None, False))
    try:
        return await asyncio.wait_for(scenario(h), 30)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_ping_has_no_reason_while_connected(harness):
    async def scenario(h):
        reply = await h.ping()
        assert reply["connected"] is True and reply["reason"] is None
        assert set(reply) == {"ok", "connected", "status", "motion", "device", "reason",
                              "model", "stopping", "version", "build", "api", "pid",
                              "started_by"}
        assert reply["model"] == "CAMEO 4 V5.05"
    harness.run(scenario)


def test_reason_values():
    assert (REASON_HELD_ELSEWHERE, REASON_NOT_PAIRED, REASON_UNREACHABLE, REASON_NO_BLUETOOTH,
            REASON_WRONG_MODEL) == ("held-elsewhere", "not-paired", "unreachable",
                                    "no-bluetooth", "wrong-model")


@pytest.mark.parametrize("devices", [[], [{"Paired": False}]], ids=["none", "known-unpaired"])
def test_no_paired_machine(harness, devices):
    """BlueZ lists no CAMEO, or one that is known but not paired: nothing is connected."""
    harness.bluez.devices[:] = [stubs.bluez_device(**props) for props in devices]

    async def scenario(h):
        reply = await _ping_until(h, lambda r: r.get("reason"))
        assert reply["connected"] is False and reply["reason"] == REASON_NOT_PAIRED
        assert reply["model"] is None
        assert h.fake is None                       # no BleakClient was ever made
    asyncio.run(_serve_without_link(harness, scenario))


def test_a_machine_that_does_not_accept_the_connection(harness, monkeypatch):
    async def refuse(self):
        raise sys.modules["bleak.exc"].BleakError("simulated: no answer")
    monkeypatch.setattr(harness.fake_cls, "connect", refuse)

    async def scenario(h):
        reply = await _ping_until(h, lambda r: r.get("reason"))
        assert reply["connected"] is False and reply["reason"] == REASON_UNREACHABLE
        assert h.fake.writes == []                  # nothing was sent for it
    asyncio.run(_serve_without_link(harness, scenario))


def test_a_link_held_elsewhere_after_a_drop_then_cleared(harness, monkeypatch):
    monkeypatch.setattr(daemon, "HELD_ELSEWHERE_WAIT", 0.2)

    async def scenario(h):
        first = h.fake
        n = len(first.writes)
        h.connected_prop = True                     # another program reconnects first
        first.is_connected = False
        first.cb_disc(first)
        reply = await _ping_until(h, lambda r: r.get("reason") == REASON_HELD_ELSEWHERE)
        assert reply["connected"] is False and h.fake is first
        assert first.writes[n:] == []               # nothing sent while it is held elsewhere
        h.connected_prop = False                    # that program let go: reconnect as usual
        reply = await _ping_until(h, lambda r: r["connected"])
        assert reply["reason"] is None
    harness.run(scenario)


def test_held_elsewhere_is_reported_only_when_seen_twice(harness, monkeypatch):
    """Right after our own link drops, BlueZ may still report it Connected for a moment."""
    monkeypatch.setattr(daemon, "HELD_ELSEWHERE_WAIT", 1.5)

    async def scenario(h):
        first = h.fake
        h.connected_prop = True
        first.is_connected = False
        first.cb_disc(first)
        await _ping_until(h, lambda r: not r["connected"])
        await asyncio.sleep(0.3)                    # one look so far
        assert (await h.ping())["reason"] is None
        reply = await _ping_until(h, lambda r: r.get("reason"), timeout=5)
        assert reply["reason"] == REASON_HELD_ELSEWHERE
        h.connected_prop = False
        await _ping_until(h, lambda r: r["connected"])
    harness.run(scenario)


def test_a_reason_that_no_longer_holds_is_cleared_while_connecting(harness, monkeypatch):
    """The other program let go: while our own connect still runs (with BlueZ's timeouts that
    can take a minute), the ping must not keep saying that the link is held elsewhere."""
    monkeypatch.setattr(daemon, "HELD_ELSEWHERE_WAIT", 0.2)
    connecting = asyncio.Event()
    finish = asyncio.Event()

    async def slow_connect(self):
        connecting.set()
        await finish.wait()
        self.is_connected = True

    async def scenario(h):
        first = h.fake
        h.connected_prop = True                     # another program reconnects first
        first.is_connected = False
        first.cb_disc(first)
        await _ping_until(h, lambda r: r.get("reason") == REASON_HELD_ELSEWHERE)
        monkeypatch.setattr(h.fake_cls, "connect", slow_connect)
        h.connected_prop = False                    # it let go; our connect is slow
        await asyncio.wait_for(connecting.wait(), 10)
        reply = await h.ping()
        assert reply["connected"] is False and reply["reason"] is None
        finish.set()
        reply = await _ping_until(h, lambda r: r["connected"])
        assert reply["reason"] is None
    harness.run(scenario)


def test_not_paired_is_cleared_once_the_machine_is_found(harness, monkeypatch):
    connecting = asyncio.Event()
    finish = asyncio.Event()
    harness.bluez.devices[0]["Paired"] = False      # known to BlueZ, not paired yet

    async def slow_connect(self):
        connecting.set()
        await finish.wait()
        self.is_connected = True
    monkeypatch.setattr(harness.fake_cls, "connect", slow_connect)

    async def scenario(h):
        await _ping_until(h, lambda r: r.get("reason") == REASON_NOT_PAIRED)
        assert h.fake is None
        h.bluez.devices[0]["Paired"] = True         # the user paired it: found at the next look
        await asyncio.wait_for(connecting.wait(), 10)
        reply = await h.ping()
        assert reply["connected"] is False and reply["reason"] is None
        finish.set()
        await _ping_until(h, lambda r: r["connected"])
    asyncio.run(_serve_without_link(harness, scenario))


def test_unreachable_stays_while_retrying(harness, monkeypatch):
    """'unreachable' is kept across connect attempts, so the GUI's text does not flicker."""
    attempts = []

    async def refuse(self):
        attempts.append(1)
        if len(attempts) >= 2:
            reply = await harness.ping()            # during the second attempt
            attempts.append(reply["reason"])
        raise sys.modules["bleak.exc"].BleakError("simulated: no answer")
    monkeypatch.setattr(harness.fake_cls, "connect", refuse)

    async def scenario(h):
        await _ping_until(h, lambda r: len(attempts) >= 3, timeout=15)
        assert attempts[2] == REASON_UNREACHABLE
    asyncio.run(_serve_without_link(harness, scenario))


# --- Bluetooth itself cannot be reached ---------------------------------------------------------
def _no_bluez(calls: list | None = None):
    """find_bonded_cameo as it fails when the system bus or BlueZ cannot be reached: dbus_fast
    cannot open the bus socket (in a Flatpak without the org.bluez permission, with Bluetooth
    not running, ...)."""
    async def find(_address=None, skip=()):
        if calls is not None:
            calls.append(1)
        raise FileNotFoundError(2, "No such file or directory")
    return find


def test_no_bluetooth_at_start_up_is_a_reason_not_a_crash(harness):
    """The start-up look at a link held elsewhere cannot look without BlueZ: the service starts
    anyway (it never joins a held link later either) and says why it has no link."""
    harness.bluez.unreachable = True                # the real find_bonded_cameo cannot connect

    async def scenario(h):
        reply = await _ping_until(h, lambda r: r.get("reason"))
        assert reply["connected"] is False and reply["reason"] == REASON_NO_BLUETOOTH
        assert reply["stopping"] is False and h.fake is None      # no BleakClient was made
        assert any("waiting for Bluetooth (BlueZ cannot be reached)" in x for x in h.log), h.log
    asyncio.run(_serve_without_link(harness, scenario))


def test_no_bluetooth_keeps_looking(harness, monkeypatch):
    calls: list = []
    monkeypatch.setattr(daemon, "find_bonded_cameo", _no_bluez(calls))

    async def scenario(h):
        await _ping_until(h, lambda r: r.get("reason") == REASON_NO_BLUETOOTH)
        await _ping_until(h, lambda r: len(calls) >= 2)            # it keeps looking
    asyncio.run(_serve_without_link(harness, scenario))


def test_the_held_elsewhere_check_at_start_up_returns_without_bluez(monkeypatch):
    stubs.install()
    monkeypatch.setattr(daemon, "find_bonded_cameo", _no_bluez())
    asyncio.run(daemon._refuse_if_held_elsewhere(None, False))    # no SystemExit, no error


def test_no_bluetooth_is_cleared_once_bluez_answers(harness):
    harness.bluez.unreachable = True

    async def scenario(h):
        await _ping_until(h, lambda r: r.get("reason") == REASON_NO_BLUETOOTH)
        h.bluez.unreachable = False                 # Bluetooth was turned on
        reply = await _ping_until(h, lambda r: r["connected"], timeout=15)
        assert reply["reason"] is None
    asyncio.run(_serve_without_link(harness, scenario))
