# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The machine model gate: a link is used only when FG names a machine OpenCutter drives.

OpenCutter has been tested on one CAMEO 4 with firmware V5.05; other models are driven when FG
names one of their profiles (tests/test_device_profiles_daemon.py). Other Silhouette cutters
offer the same BLE service, so the daemon reads FG right after ESC 05 on every new link and
sends nothing more to any machine it does not drive. The refused machine here reports a model
no profile names ("CAMEO 5 PRO"; a CAMEO 5 itself is driven, untested). Everything here runs
against the stub BlueZ and the simulated machine of tests/stubs.py: nothing reaches Bluetooth or
D-Bus.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import types

import pytest

import service_stubs as ss
import stubs
from stubs import ADDRESS, ESC05, FG, TESTED_FG, harness  # noqa: F401

stubs.install()
from opencutter import __main__ as cli  # noqa: E402
from opencutter.device import ble, daemon, protocol  # noqa: E402
from opencutter.device.client import DaemonClient, service_socket_path  # noqa: E402
from opencutter.device.protocol import (REASON_NOT_PAIRED, REASON_UNREACHABLE,  # noqa: E402
                                        REASON_WRONG_MODEL, is_tested_model, model_text,
                                        wrong_model_message)

CAMEO5 = "CAMEO 5 PRO V1.07"      # a model no profile names: refused (docs/PROTOCOL.md)
SECOND = "00:00:00:00:00:02"          # a second, made-up machine
HEX = "AA:BB:CC:DD:EE:FF"             # a made-up address with letters (BlueZ writes upper case)
REFUSED = wrong_model_message(CAMEO5)
PASSED = [ESC05, FG, ESC05]          # a new link to the tested machine: ESC 05, FG, ESC 05 again


# --- the pure check ------------------------------------------------------------------------------
@pytest.mark.parametrize("reply", [
    "CAMEO 4 V5.05    ", "CAMEO 4 V5.05", "CAMEO 4 V5.00", "CAMEO 4 V5.99", "  CAMEO 4 V5.05\x03",
    "\x00CAMEO 4 V5.05\x00", "CAMEO 4 V5.05\r\n", b"CAMEO 4 V5.05    ", bytearray(b"CAMEO 4 V5.10"),
])
def test_the_tested_machine_passes(reply):
    assert is_tested_model(reply) is True


@pytest.mark.parametrize("reply", [
    "CAMEO 4 PLUS V5.05", "CAMEO 4 PRO V5.05", "CAMEO 5 V1.07", "CAMEO 5 PLUS V1.07",
    "CAMEO 5 ALPHA V1.00", "CAMEO 4 V1.20", "PORTRAIT 3 V1.20", "PORTRAIT 4 V1.00",
    "CURIO 2 V1.00", "CAMEO 3 V3.00", "", "   ", "\x03", "CAMEO 4", "CAMEO 4 V5", "CAMEO 4 V5.",
    "CAMEO 4 V5.5", "CAMEO 4 V5.055", "CAMEO 4 V5.05 X", "X CAMEO 4 V5.05", "cameo 4 v5.05",
    "CAMEO 4 v5.05", "CAMEO  4 V5.05", "CAMEO 4 V6.05", "CAMEO 4 V15.05", "CAMEO 4\tV5.05",
    "CAMEO 4 V5.0\x005", "CAMEO 4\x00V5.05", "CAMEO 4 V5.05\x1b", "CAMEO 4 V5.05\x7f",
    "CAMEO 4 V5.٠٥", "CAMEO 4 V5.05 ", "CAMEO 4 V5.05", "1", " 2, 4",
    None, 405, b"CAMEO 4 V5.05\xff", b"\xffCAMEO 4 V5.05", ["CAMEO 4 V5.05"],
])
def test_everything_else_is_refused(reply):
    assert is_tested_model(reply) is False


@pytest.mark.parametrize("reply, shown", [
    ("CAMEO 4 V5.05    ", "CAMEO 4 V5.05"),
    ("CAMEO 5 V1.07\x03", "CAMEO 5 V1.07"),
    ("  CAMEO   5    PLUS  V1.07  ", "CAMEO 5 PLUS V1.07"),
    ("CAMEO\x1b[31m 5", "CAMEO?[31m 5"),
    ("CAMEO 5é �", "CAMEO 5? ?"),
    (b"CAMEO 5 \xff", "CAMEO 5 ?"),
    ("<b>CAMEO</b> & 5", "<b>CAMEO</b> & 5"),       # printable: the GUI escapes it itself
    ("X" * 100, "X" * 37 + "..."),
    ("", "?"), ("\x00\x03  ", "?"), (None, "?"), (17, "?"),
])
def test_model_text_is_printable_ascii_and_short(reply, shown):
    text = model_text(reply)
    assert text == shown
    assert len(text) <= protocol.MODEL_TEXT_MAX and text.isascii() and text.isprintable()
    assert model_text(text) == text


def test_the_texts():
    assert REASON_WRONG_MODEL == "wrong-model"
    assert protocol.TESTED_MACHINE == "CAMEO 4 (firmware V5.xx)"
    assert REFUSED == ('This machine reports itself as "CAMEO 5 PRO V1.07". OpenCutter cannot '
                       "drive it and will not send anything to it.")
    assert wrong_model_message("\x1b[2J" + "Y" * 80).count("Y") == 33   # 40 characters


def test_fg_is_allowlisted_and_nothing_new_is():
    """FG is the only query the gate adds; the allowlist did not grow."""
    protocol.check_safe(FG)
    assert protocol.SAFE_TEXT_QUERIES == frozenset({
        b"FG", b"TB71", b"FA", b"FQ0", b"FQ2", b"TI", b"TO", b"TC", b"G", b"[", b"U"})
    assert protocol.SAFE_ESC_QUERIES == frozenset({0x05, 0x10, 0x15, 0x16})


# --- find_bonded_cameo: paired machines only, and the skip set ---------------------------------
def _find(monkeypatch, devices, address=None, skip=()):
    bus = type("BlueZ", (stubs.FakeBlueZ,), {"devices": devices})
    monkeypatch.setattr(ble, "MessageBus", bus)
    return asyncio.run(ble.find_bonded_cameo(address, skip=skip))


def test_find_needs_a_paired_machine(monkeypatch):
    with pytest.raises(ble.PlotterUnreachable, match="No paired CAMEO was found"):
        _find(monkeypatch, [stubs.bluez_device(paired=False)])
    with pytest.raises(ble.PlotterUnreachable, match="No paired CAMEO was found"):
        _find(monkeypatch, [])
    dev = _find(monkeypatch, [stubs.bluez_device()])
    assert (dev.address, dev.name) == (ADDRESS, "CAMEO 4-fake")
    assert dev.details["props"]["Paired"] is True
    assert dev.details["path"] == "/org/bluez/hci0/dev_" + ADDRESS.replace(":", "_")


def test_find_takes_bonded_too(monkeypatch):
    dev = _find(monkeypatch, [stubs.bluez_device(paired=False, Bonded=True)])
    assert dev.address == ADDRESS


@pytest.mark.parametrize("value", [1, "yes", None])
def test_find_wants_paired_exactly_true(monkeypatch, value):
    with pytest.raises(ble.PlotterUnreachable):
        _find(monkeypatch, [stubs.bluez_device(paired=value)])


def test_find_skips_unpaired_and_other_devices(monkeypatch):
    devices = [stubs.bluez_device(paired=False),
               stubs.bluez_device("00:00:00:00:00:03", "Headphones"),
               stubs.bluez_device("00:00:00:00:00:04", "CAMEO 4-nouuid", uuids=[]),
               stubs.bluez_device(SECOND, "CAMEO 4-second")]
    assert _find(monkeypatch, devices).address == SECOND


def test_find_with_an_address(monkeypatch):
    """With an address the name and the service are not checked, but Paired is."""
    other = stubs.bluez_device(SECOND, "Renamed", uuids=[])
    found = _find(monkeypatch, [stubs.bluez_device(), other], address=SECOND.lower())
    assert found.name == "Renamed"
    with pytest.raises(ble.PlotterUnreachable, match="known to the system but not paired") as e:
        _find(monkeypatch, [stubs.bluez_device(SECOND, "Renamed", paired=False)], address=SECOND)
    assert "Pair it first" in str(e.value)
    with pytest.raises(ble.PlotterUnreachable, match="No paired CAMEO was found"):
        _find(monkeypatch, [stubs.bluez_device()], address=SECOND)


def test_find_skips_the_addresses_it_is_given(monkeypatch):
    devices = [stubs.bluez_device(), stubs.bluez_device(SECOND, "CAMEO 4-second")]
    assert _find(monkeypatch, devices, skip={ADDRESS.lower()}).address == SECOND
    assert _find(monkeypatch, devices, skip=[ADDRESS]).address == SECOND
    with pytest.raises(ble.PlotterUnreachable):
        _find(monkeypatch, devices, skip={ADDRESS, SECOND})
    with pytest.raises(ble.PlotterUnreachable):
        _find(monkeypatch, devices, address=ADDRESS, skip={ADDRESS})
    assert _find(monkeypatch, devices).address == ADDRESS        # no skip set: the first one


def test_find_compares_addresses_in_any_case(monkeypatch):
    """BlueZ writes addresses in upper case; `--address` and the skip set may be lower case."""
    devices = [stubs.bluez_device(HEX, "CAMEO 4-hex"), stubs.bluez_device(SECOND, "CAMEO 4-second")]
    assert _find(monkeypatch, devices).address == HEX
    assert _find(monkeypatch, devices, skip={HEX.lower()}).address == SECOND
    assert _find(monkeypatch, devices, skip=[HEX.lower(), "unrelated"]).address == SECOND
    assert _find(monkeypatch, devices, address=HEX.lower()).address == HEX
    with pytest.raises(ble.PlotterUnreachable):
        _find(monkeypatch, devices, address=HEX.lower(), skip={HEX.lower()})


# --- the daemon --------------------------------------------------------------------------------
def wrong_model(ping: dict) -> bool:
    return ping.get("reason") == REASON_WRONG_MODEL


async def _until(predicate, timeout: float = 15.0, step: float = 0.05):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(step)


def test_the_tested_machine_connects_after_esc05_fg_esc05(harness):
    harness.fake_cls.start_status = "1"         # Busy after power-on ...
    harness.fake_cls.fg_status = "2"            # ... and FG claims the session: Load Media

    async def scenario(h):
        ping = await h.ping()
        assert h.fake.writes == PASSED
        assert ping["connected"] is True and ping["reason"] is None
        assert ping["model"] == "CAMEO 4 V5.05" and ping["status"] == "2"
        assert ping["device"] == {"name": "CAMEO 4-fake", "address": ADDRESS}
        assert ("connected to CAMEO 4-fake [00:00:00:00:00:01], CAMEO 4 V5.05, status 2 = "
                "Load Media") in h.log
    harness.run(scenario)


def test_a_refused_machine_gets_esc05_and_fg_and_nothing_else(harness):
    harness.fake_cls.fg_reply = CAMEO5

    async def scenario(h):
        machine = h.fake
        assert not machine.is_connected                      # its link was released
        ping = await h.ping()
        assert ping["connected"] is False and ping["device"] is None and ping["status"] is None
        assert ping["reason"] == REASON_WRONG_MODEL and ping["model"] == CAMEO5
        lines = [x for x in h.log if "reports itself as" in x]
        assert lines == ['the machine CAMEO 4-fake [00:00:00:00:00:01] reports itself as '
                         '"CAMEO 5 PRO V1.07"; OpenCutter is tested only with the CAMEO 4 '
                         '(firmware V5.xx) and does not use it; releasing the link']
        # every op that would write to a machine says why, and sends nothing
        for op in ({"op": "motion", "program": "dry-run", "options": {}},
                   {"op": "job", "spec": {}}, {"op": "info"}, {"op": "esc", "code": 5},
                   {"op": "query", "cmd": "FG"}, {"op": "stop"}):
            reply = (await h.raw([op]))[2][0]
            assert reply == {"ok": False, "error": REFUSED}, (op, reply)
        assert await h.client().stop() == {"ok": False, "error": REFUSED}
        async with h.client().session() as s:
            assert await s.open_program("dry-run", {}) == {"ok": False, "error": REFUSED}
        await asyncio.sleep(4)                                 # the service looks again: skipped
        ping = await h.ping()
        assert wrong_model(ping) and ping["model"] == CAMEO5 and h.bluez.looks >= 2
        assert h.machine() == [machine]                        # never connected again
        assert machine.writes == [ESC05, FG]                   # exactly, and nothing after
        assert sum("reports itself as" in x for x in h.log) == 1
        assert (await h.raw([{"op": "shutdown"}]))[2][0] == {"ok": True}
        await asyncio.wait_for(h.task, 10)
        assert machine.writes == [ESC05, FG]
    harness.run(scenario, until=wrong_model)


# 'CAMEO 4 PLUS V5.05' and 'CAMEO 4 V1.20' are driven (untested) only in the firmware's exact
# form, with four spaces after the version; without them they are refused (machines.detect)
@pytest.mark.parametrize("reply", [
    "CAMEO  4 V5.05", "  CAMEO 4   V5.05  ", "CAMEO 4 V5.05 X", "CAMEO 4 PLUS V5.05",
    "cameo 4 v5.05", "CAMEO 4 V1.20", "CAMEO 4 V5.05\x1b", "",
])
def test_the_service_judges_the_raw_reply(harness, reply):
    """Near misses, at the service: the raw FG reply is judged, not the text shown for it (which
    collapses spaces). Each one is refused with ESC 05 and FG sent, and nothing else."""
    harness.fake_cls.fg_reply = reply
    shown = model_text(reply) if reply.strip() else None      # an empty reply names no model

    async def scenario(h):
        ping = await h.ping()
        assert ping["connected"] is False and ping["device"] is None
        assert ping["reason"] == REASON_WRONG_MODEL and ping["model"] == shown
        assert (await h.raw([{"op": "esc", "code": 5}]))[2][0] == {
            "ok": False, "error": wrong_model_message(shown)}
        assert h.machine() == [h.fake] and h.fake.writes == [ESC05, FG]
    harness.run(scenario, until=wrong_model)


def test_an_empty_fg_reply_names_no_model(harness):
    harness.fake_cls.fg_reply = "   "

    async def scenario(h):
        ping = await h.ping()
        assert ping["reason"] == REASON_WRONG_MODEL and ping["model"] is None
        assert wrong_model_message(None) == (
            "This machine did not report its model. OpenCutter cannot drive it and will not "
            "send anything to it.")
        assert (await h.raw([{"op": "stop"}]))[2][0] == {"ok": False,
                                                       "error": wrong_model_message(None)}
        assert [x for x in h.log if "CAMEO 4-fake [" in x and "model" in x] == [
            "the machine CAMEO 4-fake [00:00:00:00:00:01] did not report its model (an empty FG "
            "reply); OpenCutter is tested only with the CAMEO 4 (firmware V5.xx) and does not use "
            "it; releasing the link"]
        assert h.fake.writes == [ESC05, FG]
    harness.run(scenario, until=wrong_model)


def test_the_refused_model_is_not_replaced_by_an_accepted_one(harness):
    """A CAMEO 5 is refused, a CAMEO 4 is used, then the CAMEO 4 goes away (removed to pair it
    again): the reason falls back to wrong-model, and the model is still the refused one's."""
    harness.bluez.devices[0]["Name"] = "CAMEO 5-fake"
    harness.fake_cls.fg_by_address[ADDRESS] = CAMEO5
    harness.add_machine(SECOND, "CAMEO 4-second")

    async def scenario(h):
        ping = await h.ping()
        assert ping["connected"] and ping["model"] == "CAMEO 4 V5.05"
        used = h.fake
        h.bluez.devices[1]["Paired"] = False
        used.is_connected = False
        used.cb_disc(used)
        ping = await _until(lambda: _ping_if(h, wrong_model))
        assert ping["connected"] is False and ping["model"] == CAMEO5
        assert (await h.raw([{"op": "motion", "program": "dry-run", "options": {}}]))[2][0] == {
            "ok": False, "error": REFUSED}
        assert h.machine(ADDRESS)[0].writes == [ESC05, FG] and used.writes == PASSED
    harness.run(scenario)


def test_another_paired_machine_is_tried_after_a_refusal(harness):
    harness.bluez.devices[0]["Name"] = "CAMEO 5-fake"
    harness.fake_cls.fg_by_address[ADDRESS] = CAMEO5
    harness.add_machine(SECOND, "CAMEO 4-second")

    async def scenario(h):
        ping = await h.ping()
        assert ping["connected"] and ping["reason"] is None and ping["model"] == "CAMEO 4 V5.05"
        assert ping["device"] == {"name": "CAMEO 4-second", "address": SECOND}
        [refused] = h.machine(ADDRESS)
        [used] = h.machine(SECOND)
        assert refused.writes == [ESC05, FG] and not refused.is_connected
        assert used.writes == PASSED
        info = await h.client().info()
        assert info == {"ok": True, "status": "0", "tools": " 2, 4", "firmware": "CAMEO 4 V5.05"}
        # the link to the good machine drops (power cycle): the refused one is still skipped
        used.is_connected = False
        used.cb_disc(used)
        await _until(lambda: _connected_again(h, used))
        assert h.machine(ADDRESS) == [refused] and refused.writes == [ESC05, FG]
        assert h.machine(SECOND)[-1].writes == PASSED                # the gate ran again
        assert sum("reports itself as" in x for x in h.log) == 1
    harness.run(scenario)


async def _connected_again(h, old) -> bool:
    return h.fake is not old and (await h.ping())["connected"]


def test_a_good_machine_found_later_clears_wrong_model(harness):
    harness.fake_cls.fg_by_address[ADDRESS] = CAMEO5

    async def scenario(h):
        assert wrong_model(await h.ping())
        h.add_machine(SECOND, "CAMEO 4-second")                  # paired while it waits
        ping = await _until(lambda: _ping_if(h, lambda p: p["connected"]))
        assert ping["reason"] is None and ping["model"] == "CAMEO 4 V5.05"
        assert h.machine(SECOND)[0].writes == PASSED and h.machine(ADDRESS)[0].writes == [ESC05, FG]
    harness.run(scenario, until=wrong_model)


async def _ping_if(h, predicate):
    ping = await h.ping()
    return ping if predicate(ping) else None


def test_force_does_not_bypass_the_gate(harness):
    harness.fake_cls.fg_reply = CAMEO5
    harness.connected_prop = True                      # held elsewhere: --force joins it

    async def scenario(h):
        ping = await h.ping()
        assert ping["connected"] is False and ping["model"] == CAMEO5
        assert h.machine() == [h.fake] and h.fake.writes == [ESC05, FG]
        assert (await h.raw([{"op": "motion", "program": "dry-run", "options": {}}]))[2][0] == {
            "ok": False, "error": REFUSED}
        assert h.fake.writes == [ESC05, FG]
    harness.run(scenario, force=True, until=wrong_model)


def test_force_still_connects_the_tested_machine(harness):
    harness.connected_prop = True

    async def scenario(h):
        assert (await h.ping())["model"] == "CAMEO 4 V5.05" and h.fake.writes == PASSED
    harness.run(scenario, force=True)


def test_address_of_a_refused_machine_waits_until_the_idle_exit(harness, monkeypatch):
    """`serve --address` (in lower case) names a machine the gate refused: the service waits,
    without looking at BlueZ again, until its idle time is over. The retry between looks is
    made short here, so a service that kept looking would look many times."""
    monkeypatch.setattr(daemon, "RETRY_WAIT", 0.05)
    harness.bluez.devices[0]["Address"] = HEX
    harness.fake_cls.fg_reply = CAMEO5

    async def scenario():
        task = asyncio.create_task(daemon.serve(HEX.lower(), False, idle_exit=2.0))
        harness.task = task
        ping = await _until(lambda: _ping_quietly(harness))
        assert wrong_model(ping) and ping["model"] == CAMEO5
        looks, lines = harness.bluez.looks, len(harness.log)
        await asyncio.sleep(1.0)
        assert wrong_model(await harness.ping())
        assert harness.bluez.looks == looks             # it waits: no more looks, no new link
        assert not any("waiting for" in x for x in harness.log[lines:]), harness.log[lines:]
        await asyncio.wait_for(task, 10)                # no request for 2 s: it ends by itself
        return looks
    asyncio.run(scenario())
    assert any("no request for 2 s" in x for x in harness.log), harness.log
    [link] = harness.fake_cls.instances
    assert link.address == HEX and link.writes == [ESC05, FG] and not link.is_connected


def test_address_of_a_refused_machine_still_takes_the_shutdown_op(harness):
    harness.fake_cls.fg_reply = CAMEO5

    async def scenario(h):
        assert (await h.raw([{"op": "shutdown"}]))[2][0] == {"ok": True}
        await asyncio.wait_for(h.task, 10)
        assert h.fake.writes == [ESC05, FG]
    harness.run(scenario, address=ADDRESS, until=wrong_model)


async def _ping_quietly(h):
    try:
        ping = await h.ping()
    except OSError:
        return None
    return ping if ping.get("reason") else None


def test_address_of_an_unpaired_machine_is_not_paired(harness):
    harness.bluez.devices[0]["Paired"] = False

    async def scenario(h):
        ping = await h.ping()
        assert ping["reason"] == REASON_NOT_PAIRED and h.fake is None
        assert any("known to the system but not paired" in x for x in h.log), h.log
    harness.run(scenario, address=ADDRESS, until=lambda p: p.get("reason") == REASON_NOT_PAIRED)


def test_no_answer_to_fg_is_never_accepted(harness):
    """FG times out: a failed connect probe (unreachable, try again later). Its late reply must
    never answer a later query, so that link is released and the next try uses a new one."""
    harness.fake_cls.fg_reply = None

    async def scenario(h):
        first = h.machine()[0]
        ping = await h.ping()
        assert ping["connected"] is False and ping["reason"] == REASON_UNREACHABLE
        assert ping["model"] is None
        await _until(lambda: _links(h, 2))
        assert not first.is_connected                  # released, not reused
        *done, newest = h.machine()
        assert done and all(link.writes == [ESC05, FG] for link in done)
        assert newest.writes == [ESC05, FG][:len(newest.writes)]    # it may still be under way
        assert (await h.raw([{"op": "esc", "code": 5}]))[2][0]["ok"] is False
        h.fake_cls.fg_reply = TESTED_FG                # it answers FG now
        ping = await _until(lambda: _ping_if(h, lambda p: p["connected"]))
        assert ping["model"] == "CAMEO 4 V5.05" and h.fake.writes == PASSED
        assert all(not link.is_connected for link in h.machine()[:-1])
    harness.run(scenario, until=lambda p: p.get("reason") == REASON_UNREACHABLE)


async def _links(h, n: int) -> bool:
    return len(h.machine()) >= n


def test_a_late_fg_reply_never_answers_a_later_query(harness):
    """FG answers 3.6 s late (after the 3 s timeout): that link is dropped, and the reply,
    when it comes, reaches no query of the link that is used."""
    harness.fake_cls.fg_delay = 3.6

    async def scenario(h):
        first = h.machine()[0]
        h.fake_cls.fg_delay = 0.0                      # the next link answers at once
        ping = await _until(lambda: _ping_if(h, lambda p: p["connected"]))
        used = h.fake
        assert used is not first and not first.is_connected
        assert first.writes == [ESC05, FG] and used.writes == PASSED
        assert ping["model"] == "CAMEO 4 V5.05"
        await asyncio.sleep(1.0)                       # the late reply has arrived by now
        info = await h.client().info()
        assert info == {"ok": True, "status": "0", "tools": " 2, 4", "firmware": "CAMEO 4 V5.05"}
        status = (await h.raw([{"op": "esc", "code": 5}]))[2][0]
        assert status == {"ok": True, "reply": "0"}
    harness.run(scenario, until=lambda p: p.get("reason") == REASON_UNREACHABLE)


def test_a_status_reply_to_fg_is_a_late_esc05_reply_not_a_model(harness):
    """Replies carry no id. When two ESC 05 timed out in a row, Cameo4 stops counting late
    replies, so a late status digit can come back as the answer to FG. That is not a model:
    the machine is not refused for it (a failed probe on a new link), and it is never taken
    for the tested machine either."""
    harness.fake_cls.fg_reply = "1"

    async def scenario(h):
        first = h.machine()[0]
        ping = await h.ping()
        assert ping["reason"] == REASON_UNREACHABLE and ping["model"] is None
        assert not any("reports itself as" in x for x in h.log)
        assert any("a late status reply" in x for x in h.log), h.log
        await _until(lambda: _links(h, 2))
        assert not first.is_connected and first.writes == [ESC05, FG]
        h.fake_cls.fg_reply = TESTED_FG                 # the next link answers FG itself
        ping = await _until(lambda: _ping_if(h, lambda p: p["connected"]))
        assert ping["model"] == "CAMEO 4 V5.05" and h.fake.writes == PASSED
    harness.run(scenario, until=lambda p: p.get("reason") == REASON_UNREACHABLE)


def test_a_reconnect_runs_the_gate_again(harness):
    async def scenario(h):
        first = h.fake
        assert first.writes == PASSED
        h.fake_cls.fg_reply = CAMEO5                    # another machine answers after the drop
        first.is_connected = False
        first.cb_disc(first)
        ping = await _until(lambda: _ping_if(h, wrong_model))
        second = h.fake
        assert second is not first and second.writes == [ESC05, FG]
        assert ping["connected"] is False and ping["model"] == CAMEO5
        assert (await h.client().stop()) == {"ok": False, "error": REFUSED}
        assert second.writes == [ESC05, FG] and first.writes == PASSED
    harness.run(scenario)


def test_a_reconnect_to_the_tested_machine_passes_again(harness):
    async def scenario(h):
        first = h.fake
        first.is_connected = False
        first.cb_disc(first)
        await _until(lambda: _connected_again(h, first))
        assert h.fake.writes == PASSED and (await h.ping())["model"] == "CAMEO 4 V5.05"
    harness.run(scenario)


def test_a_slow_status_after_a_passing_fg_keeps_the_link(harness):
    """FG passed; the ESC 05 right after it (only a status read) answers 3.5 s late. The link
    is used all the same: the tested CAMEO 4 accepts a new link only after a power cycle. The
    late reply is dropped by count and never answers a later query."""
    real_reply = harness.fake_cls._reply

    def reply(self, text, delay=0.0):
        if self.writes == PASSED and len(harness.fake_cls.instances) == 1:
            delay += 3.5
        real_reply(self, text, delay)
    harness.fake_cls._reply = reply

    async def scenario(h):
        [link] = h.fake_cls.instances
        ping = await h.ping()
        assert ping["connected"] and ping["model"] == "CAMEO 4 V5.05" and link.writes == PASSED
        assert any("no reply to the status read after FG" in x for x in h.log), h.log
        await asyncio.sleep(1.0)                       # the late reply has arrived by now
        info = await h.client().info()
        assert info == {"ok": True, "status": "0", "tools": " 2, 4", "firmware": "CAMEO 4 V5.05"}
        assert (await h.raw([{"op": "esc", "code": 5}]))[2][0] == {"ok": True, "reply": "0"}
        assert h.fake_cls.instances == [link] and link.is_connected
    harness.run(scenario)


# --- several paired machines: each one gets its turn ------------------------------------------
def test_a_first_machine_that_does_not_answer_fg_does_not_hide_a_second_one(harness):
    harness.fake_cls.fg_by_address[ADDRESS] = None          # FG never answers
    harness.add_machine(SECOND, "CAMEO 4-second")

    async def scenario(h):
        ping = await _until(lambda: _ping_if(h, lambda p: p["connected"]), timeout=20)
        assert ping["device"]["address"] == SECOND and ping["model"] == "CAMEO 4 V5.05"
        [silent] = h.machine(ADDRESS)
        assert silent.writes == [ESC05, FG] and not silent.is_connected
        assert h.machine(SECOND)[0].writes == PASSED
    harness.run(scenario, until=lambda p: p.get("reason") == REASON_UNREACHABLE)


def test_a_first_machine_that_is_switched_off_does_not_hide_a_second_one(harness):
    harness.bluez.devices[0]["Name"] = "CAMEO 5-off"
    harness.fake_cls.fg_by_address[ADDRESS] = CAMEO5
    harness.add_machine(SECOND, "CAMEO 4-on")
    real_connect = harness.fake_cls.connect

    async def connect(self):
        if self.address == ADDRESS:
            raise RuntimeError("le-connection-abort-by-local (simulated: switched off)")
        await real_connect(self)
    harness.fake_cls.connect = connect

    async def scenario(h):
        ping = await h.ping()
        assert ping["device"]["address"] == SECOND and ping["model"] == "CAMEO 4 V5.05"
        assert [link.writes for link in h.machine(ADDRESS)] == [[]]
        assert h.machine(SECOND)[0].writes == PASSED
    harness.run(scenario)


def test_paired_machines_are_tried_in_turn(harness, monkeypatch):
    """Neither machine accepts a connection: they are tried in turn, and nothing is sent."""
    monkeypatch.setattr(daemon, "RETRY_WAIT", 0.05)
    harness.add_machine(SECOND, "CAMEO 4-second")

    async def connect(self):
        raise RuntimeError("le-connection-abort-by-local (simulated: switched off)")
    harness.fake_cls.connect = connect

    async def scenario(h):
        await _until(lambda: _made(h, 6))
        assert [link.address for link in h.fake_cls.instances[:6]] == [ADDRESS, SECOND] * 3
        assert all(link.writes == [] for link in h.fake_cls.instances)
        assert (await h.ping())["reason"] == REASON_UNREACHABLE
    harness.run(scenario, until=lambda p: p.get("reason") == REASON_UNREACHABLE)


async def _made(h, n: int) -> bool:
    return len(h.fake_cls.instances) >= n


# --- the stop when the service ends: never through a link that did not pass the gate ----------
def test_a_signal_after_a_relink_sends_no_stop_to_a_machine_in_the_gate(harness):
    """A step waits for its ESC 05 reply (it holds the query lock) when the CAMEO 4's link drops.
    The service finds a paired CAMEO 5 and opens a link to it, whose gate waits for that lock.
    A signal then ends the service: the CAMEO 5 must get no ESC 09, nor anything else."""
    async def scenario(h):
        a = await h.fresh(move_s=5.0)
        r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}}])
        assert out[0]["ok"], out
        w.write((json.dumps({"op": "step", "index": 0}) + "\n").encode())
        await w.drain()
        await asyncio.sleep(1.2)                         # moving: wait_ready polls ESC 05
        h.add_machine(SECOND, "CAMEO 5-fake", fg=CAMEO5)
        h.bluez.devices.insert(0, h.bluez.devices.pop())  # the CAMEO 5 is listed first
        dropped = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_write(data):
            if data == ESC05 and not dropped.is_set():
                dropped.set()
                a._reply = lambda *args, **kwargs: None   # that reply never comes
                a.fail_esc09 = True                        # a dead link: writes fail

                def drop():
                    a.is_connected = False
                    a.cb_disc(a)
                loop.call_soon(drop)
        a.on_write = on_write
        await asyncio.wait_for(dropped.wait(), 5)
        await _until(lambda: _linked(h, SECOND))        # its link is up, its gate waits
        await h.shutdown()                               # SIGTERM
        w.close()
        return [link.writes for link in h.machine(SECOND)]
    assert harness.run(scenario) == [[]]
    assert "daemon stopping during a step -> STOP" not in harness.log


def test_a_signal_while_a_new_link_is_in_the_gate_sends_it_no_stop(harness):
    """A step's motion write is in flight when the CAMEO 4's link drops; the service connects to
    a paired CAMEO 5 and is inside its gate (FG answers slowly) when a signal ends it."""
    harness.add_machine(SECOND, "CAMEO 5-second", fg=CAMEO5)

    async def scenario(h):
        first = h.fake
        async with h.client().session() as s:
            assert (await s.open_program("dry-run", {}))["ok"]
            h.fake_cls.write_delay = 5.0                   # the next motion write stays in flight
            running = asyncio.ensure_future(s.step(0, timeout=60))
            await _until(lambda: _in_flight(first))
            h.bluez.devices[0]["Paired"] = False           # the CAMEO 5 is found next
            h.fake_cls.fg_delay = 2.5                      # its FG answer takes a moment
            first.is_connected = False
            first.cb_disc(first)
            await _until(lambda: _in_gate(h, SECOND))
            await h.shutdown()                             # SIGTERM
            running.cancel()
        return [link.writes for link in h.machine(SECOND)]
    assert harness.run(scenario) == [[ESC05, FG]]


async def _linked(h, address: str) -> bool:
    return any(link.is_connected for link in h.machine(address))


async def _in_flight(link) -> bool:
    return link.in_flight


async def _in_gate(h, address: str) -> bool:
    links = h.machine(address)
    return bool(links) and links[-1].is_connected and FG in links[-1].writes


# --- belt and braces: a link that did not pass the gate is never written to -------------------
class UncheckedLink:
    """A connected link whose model was never checked (Cameo4.machine is None)."""
    is_connected = True
    machine = None
    last_status = "0"
    device = types.SimpleNamespace(name="CAMEO 4-fake", address=ADDRESS)

    async def emergency_stop(self):
        raise AssertionError("ESC 09 sent through an unchecked link")

    async def esc_query(self, code):
        raise AssertionError("ESC query sent through an unchecked link")

    async def query(self, cmd):
        raise AssertionError("query sent through an unchecked link")

    async def refresh_write_limit(self):
        raise AssertionError("MTU read for an unchecked link")


class Writer:
    def __init__(self):
        self.lines: list[dict] = []

    def write(self, data: bytes) -> None:
        self.lines.append(json.loads(data))

    async def drain(self) -> None:
        pass


@pytest.mark.parametrize("reason, error", [(None, daemon.NOT_CONNECTED),
                                           (REASON_WRONG_MODEL, REFUSED)])
def test_ops_and_sessions_refuse_an_unchecked_link(reason, error):
    state = {"motion": None, "reason": reason, "refused_model": CAMEO5 if reason else None,
             "stopping": False, "started_by": None, "sessions": 0}

    async def scenario():
        lock = asyncio.Lock()
        link = UncheckedLink()
        for op in ({"op": "stop"}, {"op": "esc", "code": 5}, {"op": "query", "cmd": "FG"},
                   {"op": "info"}):
            assert await daemon._one_shot(op, link, lock, state) == {"ok": False, "error": error}
        ping = await daemon._one_shot({"op": "ping"}, link, lock, state)
        assert ping["connected"] is False and ping["reason"] == reason
        assert ping["model"] == (CAMEO5 if reason else None)
        for req in ({"op": "motion", "program": "dry-run", "options": {}},
                    {"op": "job", "spec": {}}):
            writer = Writer()
            await daemon._session(None, writer, req, link, lock, state)
            assert writer.lines == [{"ok": False, "error": error}]
            assert state["motion"] is None
    asyncio.run(scenario())


# --- the command line --------------------------------------------------------------------------
@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    return lambda *answers: monkeypatch.setattr(cli, "ainput", stubs.Answers(*answers))


def test_cli_refuses_a_refused_machine_with_the_model_message(harness, operator, capsys):
    harness.fake_cls.fg_reply = CAMEO5

    async def scenario(h):
        for show in (cli.show_status, cli.show_info):
            with pytest.raises(SystemExit) as e:
                await show(h.client())
            assert e.value.code == REFUSED                # a text: exit status 1
        operator("yes\n")
        with pytest.raises(SystemExit) as e:
            await cli.run_program(h.client(), "dry-run", {})
        assert e.value.code == REFUSED
        capsys.readouterr()
        assert await cli.service_status() is True
        out = capsys.readouterr().out.splitlines()
        assert "  machine: not connected (wrong-model)" in out
        assert ("  model:   CAMEO 5 PRO V1.07, refused: OpenCutter cannot drive this "
                "model") in out
        assert h.fake.writes == [ESC05, FG]
    harness.run(scenario, until=wrong_model)


def test_cli_shows_the_model_of_the_connected_machine(harness, capsys):
    async def scenario(h):
        await h.fresh()
        await cli.show_status(h.client())
        status = capsys.readouterr().out.splitlines()
        assert status[:2] == ["machine: CAMEO 4-fake [00:00:00:00:00:01]", "model:   CAMEO 4 V5.05"]
        await cli.show_info(h.client())
        assert "Firmware (FG):     CAMEO 4 V5.05" in capsys.readouterr().out
        assert await cli.service_status() is True
        assert "  model:   CAMEO 4 V5.05" in capsys.readouterr().out.splitlines()
    harness.run(scenario)


def test_cli_session_refused_for_the_model_exits_non_zero(harness, operator):
    """The ping said connected, but the service refuses the session: the model message."""
    harness.fake_cls.fg_reply = CAMEO5

    async def scenario(h):
        client = h.client()
        real_ping = client.ping

        async def stale_ping():                    # an earlier look, before the refusal
            return {**(await real_ping()), "connected": True, "reason": None}
        client.ping = stale_ping
        operator("yes\n")
        with pytest.raises(SystemExit) as e:
            await cli.run_program(client, "dry-run", {})
        assert e.value.code == f"Refused, nothing was sent: {REFUSED}"
        assert h.fake.writes == [ESC05, FG]
    harness.run(scenario, until=wrong_model)


def test_cli_model_line_of_a_machine_without_a_model():
    refused_ping = {"connected": False, "reason": REASON_WRONG_MODEL, "model": None}
    assert cli._model_line(refused_ping) == (
        "not reported, refused: OpenCutter cannot drive this model")
    assert cli._model_line({"connected": False, "reason": None, "model": None}) is None
    assert cli._model_line({"connected": True, "reason": None, "model": "CAMEO 4 V5.05"}) == (
        "CAMEO 4 V5.05")


def test_cli_help_names_the_model(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    top = " ".join(capsys.readouterr().out.split())
    assert "machine status (ESC 05)" not in top
    assert top.count("status the machine, its model and its status (ESC 05)") == 2  # doc, list
    with pytest.raises(SystemExit):
        cli.main(["service", "--help"])
    service = " ".join(capsys.readouterr().out.split())
    assert ("status version, build, pid, socket, machine and model of the background service; "
            "exit status 1 if none runs") in service


# --- a real service process on the simulated machine ---------------------------------------------
def test_a_service_process_refuses_another_model(monkeypatch):
    runtime = tempfile.mkdtemp(prefix="ocm-", dir="/tmp" if len(tempfile.gettempdir()) > 60
                               else None)            # short: the socket path has a size limit
    os.chmod(runtime, 0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.delenv("OPENCUTTER_SOCKET", raising=False)
    writes = os.path.join(runtime, "w.json")
    log = os.path.join(runtime, "daemon.log")
    proc = ss.spawn_stub_daemon(runtime, writes, idle_exit=900, log=log,
                                fg_reply="CAMEO 5 PLUS V1.07")
    try:
        path = service_socket_path()

        async def look():
            ping = await DaemonClient(path).ping()
            return ping if ping and ping.get("reason") else None

        async def scenario():
            ping = await _until(look, timeout=20)
            stop = await DaemonClient(path).stop()
            ended = await DaemonClient(path).shutdown()
            return ping, stop, ended
        ping, stop, ended = asyncio.run(scenario())
        assert proc.wait(10) == 0
        with open(log) as f:
            lines = f.read()
        with open(writes) as f:
            machine = json.load(f)
    finally:
        ss.finish(proc)
        shutil.rmtree(runtime, ignore_errors=True)
    assert 'reports itself as "CAMEO 5 PLUS V1.07"' in lines and "connected to" not in lines
    assert ping["reason"] == REASON_WRONG_MODEL and ping["model"] == "CAMEO 5 PLUS V1.07"
    assert ping["connected"] is False and ping["device"] is None
    assert stop == {"ok": False, "error": wrong_model_message("CAMEO 5 PLUS V1.07")}
    assert ended["ok"]
    assert [bytes.fromhex(w) for w in machine["writes"]] == [ESC05, FG]
    assert machine["connected"] is False
