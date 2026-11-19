# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The transport split: link.SafeLink over a dumb transport.RawTransport, ble.BleTransport and
discovery for BlueZ, and ble.Cameo4 kept for its callers.

SafeLink runs here over FakeTransport, a RawTransport written for these tests: the pre-gate
allowlist (ESC 05 and FG only) and each profile's queries, equal to machines.safety.check_query;
the profile setter, the FG reply it needs and the registry's self-check it applies; motion writes
(no ESC, ETX, the global floor of machines.safety.check_write, at most min(480,
profile.max_write, transport limit), none below MTU - 3 = 32); the profile's stop, shared by
concurrent callers; ETX framing, late replies and the resync; the ATT 0x0E retry, for queries
only, with a late reply owed for every copy. Cameo4, BleTransport and find_bonded_cameo run
on the stub Bluetooth of tests/stubs.py: nothing reaches a machine.
"""
import ast
import asyncio
import dataclasses
import math
from pathlib import Path

import pytest

import stubs

stubs.install()
from bleak.backends.device import BLEDevice  # noqa: E402  (the stub)
from bleak.exc import BleakDBusError, BleakError  # noqa: E402  (the stub)
from opencutter.device import ble, link, programs, protocol  # noqa: E402
from opencutter.device.link import MIN_SESSION_WRITE_LIMIT, SafeLink  # noqa: E402
from opencutter.device.protocol import (CHAR_DATA, CHAR_REPLY, CHAR_STATUS,  # noqa: E402
                                        SERVICE, UnsafeCommand)
from opencutter.device.transport import (MIN_WRITE_LIMIT, DeviceRef, Discovery,  # noqa: E402
                                         PlotterUnreachable, RawTransport, TransportBusy)
from opencutter.machines import (CAMEO4, MachineProfile, StopSequence, Support,  # noqa: E402
                                 ble_name_prefixes, by_id, profiles, recognized_models, safety)
from opencutter.machines.detect import detect  # noqa: E402

ESC, ETX = b"\x1b", b"\x03"
ESC05, ESC09, ESC10, ESC15, ESC16 = (ESC + bytes([c]) for c in (0x05, 0x09, 0x10, 0x15, 0x16))
FG, U = b"FG" + ETX, b"U" + ETX
TESTED_FG = stubs.TESTED_FG
ADDRESS, SECOND = stubs.ADDRESS, "00:00:00:00:00:02"
PROFILES = profiles()
DEVICE = Path(__file__).resolve().parent.parent / "src" / "opencutter" / "device"
# the link's own pauses, read before the fixture below shortens them for every test
REAL_BUSY_RETRY_DELAYS, REAL_DUPLICATE_WAIT = link.BUSY_RETRY_DELAYS, link.DUPLICATE_WAIT


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """No half-second pause after subscribing, short busy pauses and duplicate waits."""
    monkeypatch.setattr(link, "SUBSCRIBE_SETTLE", 0)
    monkeypatch.setattr(link, "BUSY_RETRY_DELAYS", (0.001, 0.002, 0.004))
    monkeypatch.setattr(link, "DUPLICATE_WAIT", 0.05)


@pytest.fixture(params=["self-check", "no self-check"])
def self_check(request, monkeypatch):
    """With "no self-check", machines.safety.check_profile lets everything through: the link's
    own checks of the profile must hold by themselves."""
    if request.param == "no self-check":
        monkeypatch.setattr(safety, "check_profile", lambda *args, **kwargs: None)


def run(coro):
    return asyncio.run(coro)


def replace(obj, **changes):
    return dataclasses.replace(obj, **changes)


class FakeTransport:
    """A RawTransport that answers like a machine. `answers` maps a write to its reply: a str
    (sent with ETX every time), a list (one item per write, in order; None: no reply), or a
    tuple of raw indications (bytes, ETX included, each delivered on its own). `delay` seconds
    before a reply. The next `busy` writes raise TransportBusy (`busy_reaches`: they still
    reach the machine and are answered). `fail` maps a write to the exception it raises.
    `attempts` logs every write call, `writes` the ones that went out."""
    has_status_channel = True

    def __init__(self, limit: object = 497, fg: str = TESTED_FG, status_channel: bool = True):
        self.ref = DeviceRef("fake", ADDRESS, "CAMEO 4-fake")
        self.disconnected = asyncio.Event()
        self.has_status_channel = status_channel
        self.up = False
        self.limit = limit
        self.answers: dict = {ESC05: "0", ESC10: "   0", ESC15: " 2, 4", ESC16: "   0", FG: fg,
                              U: "  6100,  6100"}
        for q in safety.READ_ONLY_QUERIES - {b"FG", b"U"}:
            self.answers[q + ETX] = "0"
        self.delay = 0.0
        self.busy = 0
        self.busy_reaches = False
        self.fail: dict = {}
        self.attempts: list[bytes] = []
        self.writes: list[bytes] = []
        self.times: list[float] = []
        self.on_reply = self.on_status = None
        self.connects = self.subscribes = self.unsubscribes = self.releases = 0

    def is_up(self) -> bool:
        return self.up

    async def connect(self, timeout: float) -> None:
        self.connects += 1
        self.up = True
        self.disconnected.clear()

    async def subscribe(self, on_reply, on_status) -> None:
        self.subscribes += 1
        self.on_reply, self.on_status = on_reply, on_status

    async def unsubscribe(self) -> None:
        self.unsubscribes += 1

    async def write(self, data: bytes) -> None:
        self.attempts.append(data)
        if self.busy:
            self.busy -= 1
            if self.busy_reaches:
                self._answer(data)
            raise TransportBusy("busy (simulated)")
        if data in self.fail:
            raise self.fail[data]
        self.writes.append(data)
        self.times.append(asyncio.get_running_loop().time())
        self._answer(data)

    def _answer(self, data: bytes) -> None:
        answer = self.answers.get(data)
        if isinstance(answer, list):
            answer = answer.pop(0) if answer else None
        if answer is None or self.on_reply is None:
            return
        chunks = answer if isinstance(answer, tuple) else (answer.encode() + ETX,)
        loop = asyncio.get_running_loop()
        for i, chunk in enumerate(chunks):
            loop.call_later(self.delay + 0.001 * i, self.on_reply, chunk)

    async def max_write(self) -> int:
        if isinstance(self.limit, Exception):
            raise self.limit
        return self.limit

    async def release(self) -> None:
        self.releases += 1

    async def disconnect(self) -> None:
        self.drop()

    def drop(self) -> None:
        self.up = False
        self.disconnected.set()


async def opened(t: FakeTransport | None = None, **kwargs) -> SafeLink:
    """A SafeLink over `t`, connected and subscribed, before the gate."""
    lk = SafeLink(t if t is not None else FakeTransport(), **kwargs)
    return await lk.open()


async def gated(profile: MachineProfile = CAMEO4, t: FakeTransport | None = None) -> SafeLink:
    """A SafeLink that passed the gate with `profile` (ESC 05, FG, then the profile), with its
    write limit measured."""
    lk = await opened(t)
    await lk.esc_query(0x05)
    await lk.query("FG")
    lk.profile = profile
    await lk.refresh_write_limit()
    return lk


async def accepted_query(lk: SafeLink, data: bytes) -> bool:
    """True when the link wrote the query (it is answered); False when it refused it."""
    before = len(lk.transport.attempts)
    try:
        await lk._request(data, timeout=0.5)
    except UnsafeCommand:
        assert len(lk.transport.attempts) == before, data      # nothing was written
        return False
    return True


def check_query_accepts(data: bytes, profile: MachineProfile | None) -> bool:
    try:
        safety.check_query(data, profile)
    except safety.SafetyError:
        return False
    return True


def check_write_accepts(data: bytes, profile: MachineProfile) -> bool:
    try:
        safety.check_write(data, profile)
    except safety.SafetyError:
        return False
    return True


def motion(n: int) -> bytes:
    """One move of exactly n bytes (n >= 5)."""
    return b"M" + b"1" * (n - 4) + b",1" + ETX


def allow_all(_data: bytes) -> None:
    """A validator that accepts everything: only the link's own checks are left."""


# --- the interface ---------------------------------------------------------------------------------
def imported_modules(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


@pytest.mark.parametrize("name", ["link.py", "transport.py"])
def test_link_and_transport_import_no_bluetooth(name):
    found = imported_modules(DEVICE / name)
    assert not [m for m in found if m.split(".")[0] in ("bleak", "dbus_fast")], found
    if name == "transport.py":                    # the interface needs nothing of opencutter
        assert not [m for m in found if m.startswith(".")], found


def test_the_transports_follow_the_protocols(fake_client):
    assert isinstance(FakeTransport(), RawTransport)
    assert isinstance(ble.BleTransport(BLEDevice(ADDRESS, "CAMEO 4-fake", {})), RawTransport)
    assert isinstance(ble.BleDiscovery(), Discovery)
    assert ble.BleTransport.has_status_channel is True


def test_device_ref_is_frozen_and_hashable():
    a = DeviceRef("ble", ADDRESS, "CAMEO 4-fake", details={"props": {"Connected": True}})
    b = DeviceRef("ble", ADDRESS, "CAMEO 4-fake")
    assert a == b and hash(a) == hash(b) and len({a, b}) == 1     # details are not compared
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.address = SECOND
    assert a.usb_id is None and b.details == {}


def test_the_exceptions_keep_their_meaning():
    assert ble.PlotterUnreachable is PlotterUnreachable            # one class for the daemon
    assert issubclass(PlotterUnreachable, RuntimeError)
    # a busy write may have reached the machine: never a ValueError, which the daemon reads as
    # "refused, nothing was sent"
    assert issubclass(TransportBusy, RuntimeError) and not issubclass(TransportBusy, ValueError)
    assert MIN_WRITE_LIMIT == ble.MIN_WRITE_LIMIT == 20
    assert MIN_SESSION_WRITE_LIMIT == 32


# --- the query allowlist ---------------------------------------------------------------------------
QUERY_CORPUS = sorted({
    *(ESC + bytes([code]) for code in range(256)), ESC, ESC05 + b"\x05", ESC05 + ETX,
    *(q + ETX for q in safety.READ_ONLY_QUERIES), *safety.READ_ONLY_QUERIES,
    b"FG\x03\x03", b"FG\x03FG\x03", b"fg\x03", b"Fg\x03", b"FP\x03", b"FPG\x03", b"FM1\x03",
    b"FM0\x03", b"M0,0\x03", b"D0,0\x03", b"TG1\x03", b"TB72\x03", b"TB70\x03", b"\x00FG\x03",
    b" FG\x03", b"FG \x03", b"U\x03\x00", b"", ETX, b"X" * 20 + ETX, b"FQ1\x03", b"TB71,1\x03",
    b"TB24\x03", b"APS\x03", b"!1\x03", b"J1\x03", b"FX1\x03", b"TF1,1\x03"})


def test_before_the_gate_only_esc05_and_fg():
    async def scenario():
        lk = await opened()
        return {data for data in QUERY_CORPUS if await accepted_query(lk, data)}
    assert run(scenario()) == {ESC05, FG}
    assert {d for d in QUERY_CORPUS if check_query_accepts(d, None)} == {ESC05, FG}


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_after_the_gate_the_profiles_queries_and_nothing_more(profile):
    async def scenario():
        lk = await gated(profile)
        return {data for data in QUERY_CORPUS if await accepted_query(lk, data)}
    got = run(scenario())
    assert got == {d for d in QUERY_CORPUS if check_query_accepts(d, profile)}
    assert got == ({ESC + bytes([c]) for c in profile.queries.esc}
                   | {q + ETX for q in profile.queries.text})
    assert ESC + b"\x11" not in got and ESC09 not in got and FG in got and U in got


def test_the_portraits_get_no_to_or_tc():
    for profile in (by_id("portrait3"), by_id("portrait4")):
        async def scenario():
            lk = await gated(profile)
            return [await accepted_query(lk, q) for q in (b"TO\x03", b"TC\x03", U, ESC15)]
        assert run(scenario()) == [False, False, True, True], profile.id


def test_queries_must_be_ascii_text():
    async def scenario():
        lk = await gated()
        with pytest.raises(UnicodeEncodeError):
            await lk.query("FGé")
        with pytest.raises(UnsafeCommand):
            await lk.query("fg")
        with pytest.raises(UnsafeCommand):
            await lk.esc_query(0x11)
        with pytest.raises(ValueError):
            await lk.esc_query(0x1105)
        return lk.transport.attempts
    assert run(scenario()) == [ESC05, FG]


def test_nothing_but_esc05_and_fg_before_the_gate():
    async def scenario():
        lk = await opened()
        await lk.esc_query(0x05)
        await lk.query("FG")
        with pytest.raises(UnsafeCommand, match="before the model gate"):
            await lk.write_validated(b"M1,1\x03", allow_all)
        errors = await lk.emergency_stop()
        assert lk.motion_limit() == 0
        return lk.transport.attempts, errors
    attempts, errors = run(scenario())
    assert attempts == [ESC05, FG]
    assert len(errors) == 1 and errors[0].startswith("no stop sent") and "model gate" in errors[0]


# --- the gate --------------------------------------------------------------------------------------
def test_a_profile_needs_an_fg_reply_on_this_link():
    async def scenario():
        lk = await opened()
        with pytest.raises(UnsafeCommand, match="no FG reply"):
            lk.profile = CAMEO4
        await lk.esc_query(0x05)
        with pytest.raises(UnsafeCommand, match="no FG reply"):
            lk.profile = CAMEO4
        assert lk.profile is None and lk.fg_reply is None
        assert await lk.query("FG") == TESTED_FG
        lk.profile = CAMEO4
        return lk
    lk = run(scenario())
    assert lk.profile is CAMEO4 and lk.fg_reply == TESTED_FG


BAD_STOPS = [
    ("ESC 07", StopSequence(writes=(ESC + b"\x07", ESC + b"\x07"), gap_s=0.3)),
    ("one ESC 09", StopSequence(writes=(ESC09,), gap_s=0.3)),
    ("three ESC 09", StopSequence(writes=(ESC09,) * 3, gap_s=0.3)),
    ("a list", StopSequence(writes=[ESC09, ESC09], gap_s=0.3)),
    ("ESC 09 with ETX", StopSequence(writes=(ESC09 + ETX, ESC09), gap_s=0.3)),
    ("no gap", StopSequence(writes=(ESC09, ESC09), gap_s=0)),
    ("a long gap", StopSequence(writes=(ESC09, ESC09), gap_s=2.5)),
    ("a negative gap", StopSequence(writes=(ESC09, ESC09), gap_s=-0.3)),
    ("NaN", StopSequence(writes=(ESC09, ESC09), gap_s=math.nan)),
    ("infinity", StopSequence(writes=(ESC09, ESC09), gap_s=math.inf)),
    ("a text gap", StopSequence(writes=(ESC09, ESC09), gap_s="0.3")),
    ("a bool gap", StopSequence(writes=(ESC09, ESC09), gap_s=True)),
]


@pytest.mark.parametrize("stop", [s for _, s in BAD_STOPS], ids=[n for n, _ in BAD_STOPS])
def test_a_profile_with_a_bad_stop_is_refused(stop, self_check):
    async def scenario():
        lk = await opened()
        await lk.query("FG")
        with pytest.raises(UnsafeCommand):
            lk.profile = replace(CAMEO4, stop=stop)
        assert lk.profile is None
        lk.profile = CAMEO4
        with pytest.raises(UnsafeCommand):
            lk.profile = replace(CAMEO4, stop=stop)
        return lk
    assert run(scenario()).profile is CAMEO4                         # left as it was


SELF_CHECK_FAILURES = [
    ("max_write above 480", replace(CAMEO4, max_write=600)),
    ("max_write below its own commands", replace(CAMEO4, max_write=20)),
    ("an ESC 11 query", replace(CAMEO4, queries=replace(CAMEO4.queries,
                                                        esc=CAMEO4.queries.esc | {0x11}))),
    ("a lowercase query", replace(CAMEO4, queries=replace(CAMEO4.queries,
                                                          text=CAMEO4.queries.text | {b"fg"}))),
    ("no stop at all", replace(CAMEO4, stop=None)),
    ("no support level", replace(CAMEO4, support=None)),
]


@pytest.mark.parametrize("profile", [p for _, p in SELF_CHECK_FAILURES],
                         ids=[n for n, _ in SELF_CHECK_FAILURES])
def test_a_profile_that_fails_the_self_check_is_refused(profile):
    """A profile made outside the registry is held to the registry's self-check, and a
    malformed one is refused like any other (UnsafeCommand, never another error)."""
    with pytest.raises(safety.UnsafeProfile):
        safety.check_profile(profile)

    async def scenario():
        lk = await opened()
        await lk.query("FG")
        with pytest.raises(UnsafeCommand):
            lk.profile = profile
        return lk
    assert run(scenario()).profile is None


def test_a_profile_jobs_may_not_use_is_refused(self_check):
    async def scenario():
        lk = await opened()
        await lk.query("FG")
        with pytest.raises(UnsafeCommand):
            lk.profile = replace(CAMEO4, support=Support.RECOGNIZED)
        lk.profile = by_id("cameo4_plus")                               # UNTESTED: jobs allowed
        return lk
    assert run(scenario()).profile.support is Support.UNTESTED


@pytest.mark.parametrize("value", [CAMEO4.queries, "cameo4", CAMEO4.stop, 1, True,
                                   *recognized_models()[:3]],
                         ids=lambda v: getattr(v, "id", None) or type(v).__name__)
def test_only_a_machine_profile_opens_the_gate(value):
    async def scenario():
        lk = await opened()
        await lk.query("FG")
        with pytest.raises(UnsafeCommand, match="not a machine profile"):
            lk.profile = value
        return lk
    assert run(scenario()).profile is None


def test_none_closes_the_gate_again():
    async def scenario():
        lk = await gated()
        assert await accepted_query(lk, ESC15)
        lk.profile = None
        assert lk.motion_limit() == 0
        with pytest.raises(UnsafeCommand):
            await lk.write_validated(b"M1,1\x03", allow_all)
        return [await accepted_query(lk, q) for q in (ESC15, U, ESC05, FG)]
    assert run(scenario()) == [False, False, True, True]


def test_the_fg_reply_is_kept_only_before_the_gate():
    async def scenario():
        t = FakeTransport()
        t.answers[FG] = [TESTED_FG, "CAMEO 5 V1.07    "]
        lk = await gated(t=t)
        assert await lk.query("FG") == "CAMEO 5 V1.07    "        # answered, not kept
        return lk
    lk = run(scenario())
    assert lk.fg_reply == TESTED_FG and lk.detection.profile is CAMEO4


FG_REPLIES = [TESTED_FG, "CAMEO 4 V5.05", "CAMEO 4 V5.99    ", "CAMEO 4 V1.20    ",
              "CAMEO 4 PLUS V5.05    ", "CAMEO 4 PRO V5.05    ", "CAMEO 5 V1.07    ",
              "CAMEO 5 PLUS V1.07    ", "Portrait 3 V1.05    ", "Portrait 4 V1.05    ",
              "CAMEO 4 V1.20", "CAMEO 4 PLUS V5.05", "CAMEO 3 V1.60    ", "CURIO 2 V1.16    ",
              "Portrait 2 V1.20    ", "CAMEO 5 PRO V1.07    ", "CAMEO V1.00    ",
              "CAMEO 4 V15.05", "cameo 4 v5.05", "", "    ", "1", "0", "\x00"]


@pytest.mark.parametrize("reply", FG_REPLIES, ids=repr)
def test_use_detected_profile_takes_what_fg_names(reply):
    expected = detect(reply).profile

    async def scenario():
        t = FakeTransport(fg=reply)
        lk = await opened(t)
        assert await lk.query("FG") == reply
        if expected is None:
            with pytest.raises(UnsafeCommand, match="names no model"):
                lk.use_detected_profile()
        else:
            assert lk.use_detected_profile() is expected
        return lk
    lk = run(scenario())
    assert lk.profile is expected
    assert lk.detection == detect(reply)


def test_use_detected_profile_needs_an_fg_reply():
    async def scenario():
        lk = await opened()
        assert lk.detection is None
        with pytest.raises(UnsafeCommand):
            lk.use_detected_profile()
        return lk
    assert run(scenario()).profile is None


def test_a_new_connection_starts_before_the_gate():
    async def scenario():
        t = FakeTransport()
        lk = await gated(t=t)
        lk.machine = "CAMEO 4 V5.05"
        t.drop()
        assert not lk.is_connected and not lk.transport_up()
        await lk.open()
        assert lk.is_connected and lk.transport_up()
        assert (lk.profile, lk.fg_reply, lk.machine) == (None, None, None)
        assert not await accepted_query(lk, ESC15)
        return t
    t = run(scenario())
    assert (t.connects, t.subscribes) == (2, 2)


def test_open_is_idempotent_and_close_unsubscribes():
    async def scenario():
        t = FakeTransport()
        lk = SafeLink(t)
        async with lk as same:
            assert same is lk
            await lk.open()
            assert (t.connects, t.subscribes) == (1, 1)
        assert t.unsubscribes == 1
        await lk.open()                                   # subscribes again, same connection
        assert (t.connects, t.subscribes) == (1, 2)
        await lk.release()
        await lk.disconnect()
        assert t.releases == 1 and lk.disconnected.is_set() and not lk.is_connected
        return t
    run(scenario())


def test_without_a_status_channel_only_replies_are_subscribed():
    async def scenario():
        t = FakeTransport(status_channel=False)
        await opened(t)
        return t
    t = run(scenario())
    assert t.on_reply is not None and t.on_status is None


def test_the_device_is_the_transports_ref():
    lk = SafeLink(FakeTransport())
    assert lk.device is lk.transport.ref and lk.device.address == ADDRESS
    assert lk.profile is None and lk.machine is None and lk.last_status is None


# --- motion writes ---------------------------------------------------------------------------------
LIMITS = [(182, 497, 182), (182, 100, 100), (182, 182, 182), (182, 32, 32), (182, 33, 33),
          (480, 497, 480), (480, 500, 480), (480, 480, 480), (300, 497, 300), (300, 250, 250)]


@pytest.mark.parametrize("max_write, measured, limit", LIMITS)
def test_the_write_limit_is_480_the_profiles_and_the_transports(max_write, measured, limit):
    profile = replace(CAMEO4, max_write=max_write)

    async def scenario():
        lk = await gated(profile, FakeTransport(limit=measured))
        assert lk.write_limit() == measured and lk.motion_limit() == limit
        await lk.write_validated(motion(limit), allow_all)
        with pytest.raises(UnsafeCommand, match="write limit"):
            await lk.write_validated(motion(limit + 1), allow_all)
        return lk.transport.writes[2:]
    assert run(scenario()) == [motion(limit)]


def test_the_480_cap_holds_even_for_a_profile_that_skipped_the_setter():
    """The setter refuses a profile above the firmware's 480-byte cap; a profile that reached
    the link without it (as the class-level default of a bare Cameo4 does) is still capped."""
    wide = replace(CAMEO4, max_write=600)

    async def scenario():
        lk = await gated(t=FakeTransport(limit=497))
        with pytest.raises(UnsafeCommand, match="max_write"):
            lk.profile = wide
        assert lk.profile is CAMEO4
        lk._profile = wide
        assert lk.motion_limit() == 480
        with pytest.raises(UnsafeCommand, match="write limit 480"):
            await lk.write_validated(motion(481), allow_all)
        await lk.write_validated(motion(480), allow_all)
        return lk.transport.writes[2:]
    assert run(scenario()) == [motion(480)]


@pytest.mark.parametrize("measured", [MIN_WRITE_LIMIT, 21, 31])
def test_no_motion_write_below_mtu_minus_3_of_32(measured):
    async def scenario():
        lk = await gated(t=FakeTransport(limit=measured))
        assert lk.motion_limit() == 0
        for data in (motion(5), b"M200,200\x03"):
            with pytest.raises(UnsafeCommand, match=f"write limit {measured}"):
                await lk.write_validated(data, allow_all)
        return lk.transport.writes
    assert run(scenario()) == [ESC05, FG]


@pytest.mark.parametrize("limit", [ValueError("no MTU"), "497", 497.0, True, 0, -5, None])
def test_a_failed_measurement_keeps_the_safe_minimum(limit):
    async def scenario():
        lk = await opened(FakeTransport(limit=limit))
        assert lk.write_limit() == MIN_WRITE_LIMIT                 # before any measurement
        return await lk.refresh_write_limit(), lk.write_limit()
    assert run(scenario()) == (MIN_WRITE_LIMIT, MIN_WRITE_LIMIT)


@pytest.mark.parametrize("data", [b"", b"M1,1", b"\x1bM1,1\x03", b"M1,1\x03\x1b\x09", ESC09,
                                  bytearray(b"M1,1\x03"), "M1,1\x03", memoryview(b"M1,1\x03"),
                                  None], ids=repr)
def test_motion_writes_are_bytes_with_etx_and_without_esc(data):
    async def scenario():
        lk = await gated()
        with pytest.raises(UnsafeCommand, match="ETX-terminated GPGL without ESC"):
            await lk.write_validated(data, allow_all)
        return lk.transport.attempts
    assert run(scenario()) == [ESC05, FG]


MOTION_CORPUS = sorted({
    *programs.PINNED_WRITES, b"M1,1\x03", b"D5,5\x03", b"TG1\x03FN0\x03TB50,0\x03", b"TG2\x03",
    b"TG8\x03", b"TG9\x03FN0\x03TB50,0\x03", b"TG9\x03FN0\x03TB50,0\x03FM1\x03", b"TG9\x03FM1\x03",
    b"TG9\x03FN0\x03TB50,0\x03FM0\x03", b"FM1\x03", b"FM0\x03", b"TG0\x03", b"TG14\x03",
    b"FB1\x03", b"TB72\x03", b"TB70\x03", b"fpg\x03", b" FP\x03", b"FPG\x03", b"0TH\x03",
    b"APS1\x03", b"BZ1\x03", b"BZK1\x03", b"TS1\x03", b"TR1\x03", b"TZ1\x03", b"TB24\x03",
    b"ACT\x03", b"AOC\x03", b"PW\x03", b"S0\x03", b"m1,1\x03", b"M1,1\x03\x03", b"\x03",
    b"M1,\x80\x03", b"\x00M1,1\x03", b"!5,1\x03FX10,1\x03", b"J1\x03", b"TF1,1\x03",
    b"\\30,30\x03Z6066,6066\x03", b"\\0,0\x03M0,0\x03FN0\x03TB50,0\x03", b"M1,1\x03\x00\x03"})


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_the_global_floor_holds_whatever_the_validator_says(profile):
    async def scenario():
        lk = await gated(profile)
        got = set()
        for data in MOTION_CORPUS:
            try:
                await lk.write_validated(data, allow_all)
            except UnsafeCommand:
                continue
            got.add(data)
        return got, lk.transport.writes[2:]
    got, sent = run(scenario())
    assert got == {d for d in MOTION_CORPUS if check_write_accepts(d, profile)}
    assert sent == [d for d in MOTION_CORPUS if d in got]
    assert not {b"FB1\x03", b"fpg\x03", b" FP\x03", b"0TH\x03", b"FM1\x03", b"APS1\x03"} & got
    assert (b"TG9\x03FN0\x03TB50,0\x03FM1\x03" in got) == safety.expand_allowed(profile)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_the_floor_reads_a_command_from_every_letter(profile):
    """The firmware starts a command at a letter without an ETX before it (FX10,TJ3 runs TJ3):
    a never-send command, a lowercase one or FM behind another command's values never goes out,
    whatever the validator says."""
    embedded = (b"FX10,TB72\x03", b"FN0FP\x03", b"FX10,tg1\x03", b"FX10,FM1\x03",
                b"M100,100FB0\x03", b"J1TB72\x03", b"FX10,APS1\x03")

    async def scenario():
        lk = await gated(profile)
        for data in embedded:
            with pytest.raises(UnsafeCommand):
                await lk.write_validated(data, allow_all)
        return lk.transport.writes[2:]
    assert run(scenario()) == []


def test_the_cameo4_takes_every_pinned_write_and_never_fm():
    async def scenario():
        lk = await gated()
        for data in sorted(programs.PINNED_WRITES):
            await lk.write_validated(data, programs.validate_pinned)
        for data in (b"TG9\x03FN0\x03TB50,0\x03FM1\x03", b"TG2\x03"):
            with pytest.raises(UnsafeCommand):
                await lk.write_validated(data, allow_all)
        return lk.transport.writes[2:]
    assert run(scenario()) == sorted(programs.PINNED_WRITES)


def test_the_validator_runs_first_and_its_refusal_sends_nothing():
    calls = []

    def refuse(data):
        calls.append(data)
        raise UnsafeCommand("not this one")

    async def scenario():
        lk = await gated()
        with pytest.raises(UnsafeCommand, match="not this one"):
            await lk.write_validated(b"M1,1\x03", refuse)
        with pytest.raises(UnsafeCommand, match="not this one"):     # before the link's checks
            await lk.write_validated(b"FB1", refuse)
        return lk.transport.attempts
    assert run(scenario()) == [ESC05, FG] and calls == [b"M1,1\x03", b"FB1"]


def test_a_busy_motion_write_is_never_repeated():
    async def scenario():
        lk = await gated()
        lk.transport.busy = 1
        with pytest.raises(TransportBusy):
            await lk.write_validated(b"M1,1\x03", allow_all)
        return lk.transport
    t = run(scenario())
    assert t.attempts == [ESC05, FG, b"M1,1\x03"] and t.writes == [ESC05, FG]


def test_motion_write_errors_reach_the_caller():
    async def scenario():
        lk = await gated()
        lk.transport.fail[b"M1,1\x03"] = BleakError("ATT write failed (simulated)")
        with pytest.raises(BleakError):
            await lk.write_validated(b"M1,1\x03", allow_all)
        return lk.transport.attempts
    assert run(scenario()) == [ESC05, FG, b"M1,1\x03"]


# --- the ATT 0x0E retry: queries only ------------------------------------------------------------
@pytest.mark.parametrize("busy", [1, 2, 3])
@pytest.mark.parametrize("query", [ESC05, FG], ids=["ESC 05", "FG"])
def test_a_busy_query_is_written_again(busy, query):
    async def scenario():
        lk = await opened()
        lk.transport.busy = busy
        reply = await lk._request(query)
        return reply, lk.transport
    reply, t = run(scenario())
    assert reply == ("0" if query == ESC05 else TESTED_FG)
    assert t.attempts == [query] * (busy + 1) and t.writes == [query]


def test_the_busy_pauses():
    # pauses of 0.1, 0.2 and 0.4 s: at most four writes of one query
    assert REAL_BUSY_RETRY_DELAYS == (0.1, 0.2, 0.4)
    assert 0 < REAL_DUPLICATE_WAIT <= 1


def test_a_query_that_stays_busy_fails_after_four_writes():
    async def scenario():
        lk = await opened()
        lk.transport.busy = 4
        with pytest.raises(TransportBusy):
            await lk.esc_query(0x05)
        assert await lk.esc_query(0x05) == "0"                     # the link goes on
        return lk.transport.attempts
    assert run(scenario()) == [ESC05] * 5


def test_the_late_replies_of_a_query_that_stayed_busy_are_dropped():
    """Every busy write reached the machine after all: none of their late replies may answer
    the next query."""
    async def scenario():
        t = FakeTransport()
        lk = await opened(t)
        t.answers[ESC05] = ["1", "1", "1", "1", "0"]
        t.delay = 0.02                                   # within DUPLICATE_WAIT (0.05 here)
        t.busy, t.busy_reaches = 4, True
        with pytest.raises(TransportBusy):
            await lk.esc_query(0x05)
        return await lk.esc_query(0x05)
    assert run(scenario()) == "0"


def test_a_repeated_query_that_times_out_owes_a_reply_for_every_copy():
    """Both copies reached the machine and are answered late: neither answers the next query."""
    async def scenario():
        t = FakeTransport()
        lk = await opened(t)
        t.answers[ESC05] = ["1", "1", "0"]
        t.delay = 0.1
        t.busy, t.busy_reaches = 1, True
        with pytest.raises(TimeoutError):
            await lk._request(ESC05, timeout=0.05)
        assert lk._owed == 2
        t.busy_reaches = False
        return await lk.esc_query(0x05), lk._owed
    assert run(scenario()) == ("0", 0)


def test_owing_a_copy_that_was_never_answered_costs_a_timeout_not_a_wrong_reply():
    async def scenario():
        t = FakeTransport()
        lk = await opened(t)
        t.answers[ESC05] = ["1", "0", "0"]
        t.delay = 0.1
        t.busy = 1                                       # the busy copy did not reach it
        with pytest.raises(TimeoutError):
            await lk._request(ESC05, timeout=0.05)
        with pytest.raises(TimeoutError):                # its own reply paid the debt
            await lk._request(ESC05, timeout=0.3)
        assert lk._owed == 0                             # two timeouts in a row: resync
        return await lk.esc_query(0x05)
    assert run(scenario()) == "0"


def test_the_second_reply_of_a_repeated_query_is_dropped():
    """The busy write reached the machine after all: both copies are answered."""
    async def scenario():
        t = FakeTransport()
        lk = await gated(t=t)
        t.answers[ESC05] = ["1", "0", "2"]
        t.busy, t.busy_reaches = 1, True
        first = await lk.esc_query(0x05)
        t.busy_reaches = False
        return first, await lk.esc_query(0x15), await lk.esc_query(0x05)
    assert run(scenario()) == ("1", " 2, 4", "2")


def test_the_stop_is_never_repeated_on_busy():
    async def scenario():
        lk = await gated(replace(CAMEO4, stop=replace(CAMEO4.stop, gap_s=0.01)))
        lk.transport.busy = 1
        return await lk.emergency_stop(), lk.transport
    errors, t = run(scenario())
    assert t.attempts[2:] == [ESC09, ESC09] and t.writes[2:] == [ESC09]
    assert len(errors) == 1 and errors[0].startswith("1b 09: TransportBusy")


# --- replies ---------------------------------------------------------------------------------------
def test_a_reply_in_pieces_is_one_reply_and_never_stripped():
    async def scenario():
        t = FakeTransport()
        t.answers[FG] = (b"CAMEO 4 V5", b".05    ", b"\x03")         # one reply, three pieces
        lk = await opened(t)
        return await lk.query("FG"), lk._buf
    assert run(scenario()) == (TESTED_FG, bytearray())


def test_replies_are_framed_by_etx():
    lk = SafeLink(FakeTransport())
    lk._on_reply(b"0\x032")
    assert lk._replies.qsize() == 1 and lk._buf == bytearray(b"2")
    lk._on_reply(b"\x03\x03 2, 4\x03")
    got = [lk._replies.get_nowait() for _ in range(lk._replies.qsize())]
    assert got == [b"0", b"2", b"", b" 2, 4"] and lk._buf == bytearray()


def test_a_late_reply_is_dropped_by_count():
    async def scenario():
        t = FakeTransport()
        lk = await opened(t)
        t.answers[ESC05] = ["1", "0"]
        t.delay = 0.3
        with pytest.raises(TimeoutError):
            await lk._request(ESC05, timeout=0.05)
        assert lk._owed == 1
        await asyncio.sleep(0.4)                     # the late '1' comes and is dropped
        assert lk._owed == 0
        t.delay = 0.0
        return await lk.esc_query(0x05)
    assert run(scenario()) == "0"


def test_two_timeouts_in_a_row_resync():
    async def scenario():
        t = FakeTransport()
        lk = await opened(t)
        t.answers[ESC05] = [None, None, "0"]
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await lk._request(ESC05, timeout=0.05)
        assert lk._owed == 0                         # a reply was probably lost
        return await lk.esc_query(0x05)
    assert run(scenario()) == "0"


def test_replies_that_came_unasked_are_skipped():
    async def scenario():
        t = FakeTransport()
        lk = await opened(t)
        t.on_reply(b"9\x03")
        t.on_reply(b"8\x03")
        return await lk.esc_query(0x05)
    assert run(scenario()) == "0"


def test_status_indications_and_exact_esc05_replies_set_last_status():
    seen = []

    async def scenario():
        t = FakeTransport()
        lk = await opened(t, on_status=seen.append)
        t.on_status(b"2\x03")
        assert lk.last_status == "2"
        lk.on_status = lambda s: 1 / 0                 # a broken callback breaks nothing
        t.on_status(b"1\x03")
        assert lk.last_status == "1"
        t.answers[ESC05] = [" 0", "6", "0"]
        statuses = []
        for _ in range(3):
            statuses.append((await lk.esc_query(0x05), lk.last_status))
        return statuses
    assert run(scenario()) == [(" 0", "1"), ("6", "1"), ("0", "0")]
    assert seen == ["2"]


def test_after_the_gate_last_status_follows_the_profiles_digits():
    profile = replace(CAMEO4, status=replace(CAMEO4.status, texts=CAMEO4.status.texts[:5]))

    async def scenario():
        t = FakeTransport()
        lk = await gated(profile, t)
        t.answers[ESC05] = ["5", "4"]
        return [(await lk.esc_query(0x05), lk.last_status) for _ in range(2)]
    assert run(scenario()) == [("5", "0"), ("4", "4")]


def test_u_replies_come_back_unstripped():
    async def scenario():
        lk = await gated()
        return await lk.query("U")
    assert run(scenario()) == "  6100,  6100"


# --- the stop --------------------------------------------------------------------------------------
@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_the_stop_is_the_profiles(profile):
    quick_stop = replace(profile, stop=replace(profile.stop, gap_s=0.05))

    async def scenario():
        lk = await gated(quick_stop)
        return await lk.emergency_stop(), lk.transport
    errors, t = run(scenario())
    assert errors == [] and t.writes[2:] == list(protocol.stop_sequence(profile)) == [ESC09] * 2
    # the profile's gap, not a constant (clock tolerance; every model's own gap is 0.3 s)
    assert 0.04 <= t.times[3] - t.times[2] < 0.25


def test_the_cameo4_stop_waits_its_gap():
    async def scenario():
        lk = await gated()
        return await lk.emergency_stop(), lk.transport
    errors, t = run(scenario())
    assert errors == [] and t.writes[2:] == [ESC09, ESC09]
    assert CAMEO4.stop.gap_s - 0.01 <= t.times[3] - t.times[2] < CAMEO4.stop.gap_s + 0.25


def test_concurrent_stops_share_one_sequence():
    async def scenario():
        lk = await gated(replace(CAMEO4, stop=replace(CAMEO4.stop, gap_s=0.05)))
        lk.transport.fail[ESC09] = RuntimeError("no ack (simulated)")
        results = await asyncio.gather(lk.emergency_stop(), lk.emergency_stop(),
                                       lk.emergency_stop())
        return results, lk.transport.attempts
    results, attempts = run(scenario())
    assert attempts[2:] == [ESC09, ESC09]
    assert results[0] == results[1] == results[2]
    assert results[0] == ["1b 09: RuntimeError: no ack (simulated)"] * 2


def test_a_stop_does_not_wait_for_a_query_in_flight():
    async def scenario():
        t = FakeTransport()
        lk = await gated(replace(CAMEO4, stop=replace(CAMEO4.stop, gap_s=0.01)), t)
        t.answers[ESC05] = [None]
        query = asyncio.create_task(lk._request(ESC05, timeout=2.0))
        await asyncio.sleep(0.05)
        errors = await lk.emergency_stop()
        stopped_first = not query.done()
        query.cancel()
        return errors, stopped_first, t.writes
    errors, stopped_first, writes = run(scenario())
    assert errors == [] and stopped_first and writes[-2:] == [ESC09, ESC09]


def test_a_stop_write_that_hangs_times_out(monkeypatch):
    monkeypatch.setattr(link, "STOP_WRITE_TIMEOUT", 0.05)

    async def scenario():
        t = FakeTransport()
        lk = await gated(replace(CAMEO4, stop=replace(CAMEO4.stop, gap_s=0.01)), t)
        real = t.write

        async def hang(data):
            if data == ESC09:
                await asyncio.sleep(10)
            await real(data)
        t.write = hang
        return await lk.emergency_stop()
    errors = run(scenario())
    assert len(errors) == 2 and all(e.startswith("1b 09: TimeoutError") for e in errors)


# --- Cameo4: the daemon's link, kept for its callers ----------------------------------------------
@pytest.fixture
def fake_client(monkeypatch):
    """bleak's BleakClient is the simulated machine of tests/stubs.py (any ETX-terminated write
    without ESC is motion)."""
    cls = type("Fake", (stubs.FakeClient,), {"accept_any": True, "instances": []})
    monkeypatch.setattr(ble, "BleakClient", cls)
    return cls


def device(name: str = "CAMEO 4-fake") -> BLEDevice:
    return BLEDevice(ADDRESS, name, {"path": "/org/bluez/hci0/dev_x", "props": {}})


async def cameo4_gated(fg: str = TESTED_FG) -> ble.Cameo4:
    c = ble.Cameo4(device())
    await c.open()
    await c.esc_query(0x05)
    reply = await c.query("FG")
    c.machine = protocol.model_text(reply)
    await c.refresh_write_limit()
    return c


def test_cameo4_is_a_safelink_over_ble(fake_client):
    dev = device()
    c = ble.Cameo4(dev, verbose=False, on_status=print)
    assert isinstance(c, SafeLink) and isinstance(c.transport, ble.BleTransport)
    assert c.client is c.transport.client and isinstance(c.client, fake_client)
    assert c.device is dev and c.transport.ref == DeviceRef("ble", ADDRESS, "CAMEO 4-fake")
    assert (c.profile, c.machine, c.fg_reply, c.last_status) == (None, None, None, None)
    assert c.write_limit() == MIN_WRITE_LIMIT and c.motion_limit() == 0
    assert c.on_status is print and c.verbose is False


def test_cameo4_runs_the_gate_over_the_stub_client(fake_client):
    async def scenario():
        c = ble.Cameo4(device())
        await c.open()
        assert c.is_connected and c.transport_up()
        assert await c.esc_query(0x05) == "0"
        with pytest.raises(UnsafeCommand):
            await c.esc_query(0x15)                       # not before the gate
        fg = await c.query("FG")
        c.machine = protocol.model_text(fg)              # what the daemon does today
        assert c.profile is CAMEO4 and c.machine == "CAMEO 4 V5.05"
        assert await c.esc_query(0x15) == " 2, 4"
        assert await c.refresh_write_limit() == 497 and c.motion_limit() == 182
        await c.write_validated(b"M200,200\x03", programs.validate_pinned)
        errors = await c.emergency_stop()
        return errors, fake_client.instance.writes
    errors, writes = run(scenario())
    assert errors == [] and writes == [ESC05, FG, ESC15, b"M200,200\x03", ESC09, ESC09]


@pytest.mark.parametrize("reply", ["CAMEO 3 V1.60    ", "CAMEO 5 PRO V1.07    ", "", "1",
                                   "CAMEO 4 V1.20"], ids=repr)
def test_cameo4_machine_refuses_a_model_fg_did_not_name(fake_client, reply):
    fake_client.fg_reply = reply

    async def scenario():
        c = ble.Cameo4(device())
        await c.open()
        await c.query("FG")
        with pytest.raises(UnsafeCommand, match="names no model"):
            c.machine = protocol.model_text(reply)
        return c
    c = run(scenario())
    assert c.machine is None and c.profile is None


def test_cameo4_machine_takes_an_untested_profile_fg_names(fake_client):
    fake_client.fg_reply = "CAMEO 4 PLUS V5.05    "

    async def scenario():
        c = await cameo4_gated()
        return c, await c.esc_query(0x15)
    c, tools = run(scenario())
    assert c.profile is by_id("cameo4_plus") and c.profile.support is Support.UNTESTED
    assert tools == " 2, 4"


def test_cameo4_machine_none_closes_the_gate(fake_client):
    async def scenario():
        c = await cameo4_gated()
        c.machine = None
        with pytest.raises(UnsafeCommand):
            await c.esc_query(0x15)
        return c
    c = run(scenario())
    assert c.profile is None and c.machine is None


def test_cameo4_keeps_a_profile_the_daemon_set(fake_client):
    fake_client.fg_reply = "CAMEO 4 PLUS V5.05    "

    async def scenario():
        c = ble.Cameo4(device())
        await c.open()
        await c.query("FG")
        c.profile = CAMEO4                              # the daemon's decision stands
        c.machine = "CAMEO 4 PLUS V5.05"
        return c
    assert run(scenario()).profile is CAMEO4


def bare_cameo(limit: int = 497, write=None):
    """A Cameo4 made without __init__, as tests/test_device_programs.py makes one."""
    c = object.__new__(ble.Cameo4)
    c.verbose = False
    c._write_limit = limit
    c.client = type("Client", (), {"write_gatt_char": staticmethod(write)})() if write else None
    return c


def test_a_cameo4_made_without_init_checks_with_the_cameo4s_profile():
    sent = []

    async def write(ch, data, response):
        sent.append((ch, data, response))
    c = bare_cameo(write=write)
    assert c.profile is CAMEO4 and c.machine is None and c.motion_limit() == 182
    run(ble.Cameo4.write_validated(c, b"M200,200\x03", programs.validate_pinned))
    assert sent == [(CHAR_DATA, b"M200,200\x03", True)]
    for bad in (motion(183), b"FM1\x03", b"\x1bM1,1\x03", b"M1,1"):
        with pytest.raises(UnsafeCommand):
            run(ble.Cameo4.write_validated(c, bad, allow_all))
    with pytest.raises(UnsafeCommand, match="write limit 20"):
        run(ble.Cameo4.write_validated(bare_cameo(limit=20), b"M200,200\x03", allow_all))
    for query in (ESC09, ESC + b"\x11", b"M1,1\x03", b"FP\x03"):
        with pytest.raises(UnsafeCommand):
            run(ble.Cameo4._request(bare_cameo(), query))
    assert len(sent) == 1


def test_the_bluetooth_names_stay_patchable(monkeypatch):
    made = []

    class Client(stubs.FakeClient):
        def __init__(self, dev, timeout, disconnected_callback):
            super().__init__(dev, timeout, disconnected_callback)
            made.append((dev, timeout))
    monkeypatch.setattr(ble, "BleakClient", Client)
    dev = device()
    c = ble.Cameo4(dev)
    assert made == [(dev, 30)] and isinstance(c.client, Client)
    bus = type("BlueZ", (stubs.FakeBlueZ,), {"devices": [stubs.bluez_device()], "looks": 0})
    monkeypatch.setattr(ble, "MessageBus", bus)
    assert run(ble.find_bonded_cameo()).address == ADDRESS and bus.looks == 1


# --- BleTransport ----------------------------------------------------------------------------------
def test_ble_transport_writes_one_write_request_each(fake_client):
    async def scenario():
        t = ble.BleTransport(device())
        await t.connect(1.0)
        replies = []
        await t.subscribe(replies.append, None)
        await t.write(ESC05)
        await t.write(b"M1,1\x03")
        await asyncio.sleep(0.1)
        return t, replies
    t, replies = run(scenario())
    assert t.client.writes == [ESC05, b"M1,1\x03"] and t.is_up() and replies == [b"0\x03"]


@pytest.mark.parametrize("text, busy", [
    ("Operation failed with ATT error: 0x0e (Unlikely Error)", True),
    ("[org.bluez.Error.Failed] Operation failed with ATT error: 0x0E", True),
    ("Unlikely Error", True), ("ATT error: 0x0e1", False), ("ATT error: 0x0f", False),
    ("ATT write failed (simulated)", False), ("org.bluez.Error.InProgress", False)])
def test_att_error_0x0e_becomes_transport_busy(text, busy):
    class Client:
        async def write_gatt_char(self, ch, data, response):
            raise BleakDBusError(text)
    with pytest.raises(TransportBusy if busy else BleakDBusError):
        run(ble._gatt_write(Client(), ESC05))


class GattError(BleakError):
    """Like bleak 3's GATT protocol error: the ATT error code in `code`, a text without it."""

    def __init__(self, code):
        super().__init__(code, "GATT Protocol Error")

    @property
    def code(self):
        if self.args[0] == "unreadable":
            raise ValueError("no such code")
        return self.args[0]


@pytest.mark.parametrize("code, busy", [(0x0E, True), (0x0F, False), (0x01, False),
                                        (0xFE, False), ("unreadable", False)])
def test_bleak_3s_gatt_error_0x0e_becomes_transport_busy(code, busy):
    class Client:
        async def write_gatt_char(self, ch, data, response):
            raise GattError(code)
    with pytest.raises(TransportBusy if busy else GattError):
        run(ble._gatt_write(Client(), ESC05))


def test_only_bleak_errors_are_read_for_0x0e():
    class Client:
        async def write_gatt_char(self, ch, data, response):
            raise RuntimeError("ATT error: 0x0e")
    with pytest.raises(RuntimeError, match="0x0e") as e:
        run(ble._gatt_write(Client(), ESC05))
    assert not isinstance(e.value, TransportBusy)


def test_a_busy_ble_query_is_retried_and_a_busy_motion_write_is_not(fake_client, monkeypatch):
    class Busy(fake_client):
        busy = 0

        async def write_gatt_char(self, ch, data, response):
            self.calls = getattr(self, "calls", []) + [data]
            if type(self).busy:
                type(self).busy -= 1
                raise BleakError("Operation failed with ATT error: 0x0e (Unlikely Error)")
            return await super().write_gatt_char(ch, data, response)
    monkeypatch.setattr(ble, "BleakClient", Busy)

    async def scenario():
        c = await cameo4_gated()
        Busy.busy = 2
        status = await c.esc_query(0x05)
        Busy.busy = 1
        with pytest.raises(TransportBusy):
            await c.write_validated(b"M200,200\x03", programs.validate_pinned)
        return status, c.client
    status, client = run(scenario())
    assert status == "0"
    assert client.calls[-4:] == [ESC05, ESC05, ESC05, b"M200,200\x03"]
    assert b"M200,200\x03" not in client.writes


def test_ble_transport_subscribes_to_replies_and_status(fake_client):
    got = []

    async def scenario():
        t = ble.BleTransport(device())
        await t.connect(1.0)
        await t.subscribe(lambda d: got.append(("reply", d)), lambda d: got.append(("status", d)))
        t.client.cbs[CHAR_REPLY](None, bytearray(b"0\x03"))
        t.client.cbs[CHAR_STATUS](None, bytearray(b"2\x03"))
        await t.unsubscribe()
        only = ble.BleTransport(device())
        await only.subscribe(got.append, None)
        return set(only.client.cbs)
    assert run(scenario()) == {CHAR_REPLY}
    assert got == [("reply", b"0\x03"), ("status", b"2\x03")]
    assert all(type(d) is bytes for _, d in got)


def test_ble_transport_connect_retries_while_bluez_is_busy(fake_client, monkeypatch):
    monkeypatch.setattr(ble, "CONNECT_RETRY_WAIT", 0.01)
    tries = []

    async def connect(self):
        tries.append(1)
        if len(tries) < 3:
            raise BleakDBusError("org.bluez.Error.InProgress")
        self.is_connected = True
    fake_client.connect = connect

    async def scenario():
        t = ble.BleTransport(device())
        t.disconnected.set()
        await t.connect(5.0)
        return t
    t = run(scenario())
    assert len(tries) == 3 and t.is_up() and not t.disconnected.is_set()


def test_ble_transport_connect_gives_up_with_the_power_cycle_text(fake_client):
    async def timeout(self):
        raise TimeoutError

    async def other(self):
        raise BleakDBusError("org.bluez.Error.Failed")
    fake_client.connect = timeout
    with pytest.raises(PlotterUnreachable, match="switch it off and on"):
        run(ble.BleTransport(device()).connect(-1.0))
    fake_client.connect = other
    with pytest.raises(BleakDBusError):
        run(ble.BleTransport(device()).connect(5.0))


@pytest.mark.parametrize("cached, live, obj, expected", [
    (497, None, None, 497), (20, 500, ("/org/bluez/fake/char", {}), 497),
    (20, None, ("/org/bluez/fake/char", {}), 20), (180, 185, ("/org/bluez/fake/char", {}), 182),
    (497, 23, ("/org/bluez/fake/char", {}), 497)])
def test_ble_transport_measures_mtu_minus_3(fake_client, monkeypatch, cached, live, obj,
                                            expected):
    fake_client.mtu_payload, fake_client.char_obj = cached, obj
    monkeypatch.setattr(ble, "MessageBus", type("BlueZ", (stubs.FakeBlueZ,), {"mtu": live}))
    assert run(ble.BleTransport(device()).max_write()) == expected


def test_ble_transport_from_a_device_ref(fake_client):
    ref = DeviceRef("ble", ADDRESS, "CAMEO 4-fake", details={"path": "/x", "props": {}})
    t = ble.BleTransport(ref)
    assert t.ref == ref and t.client.address == ADDRESS
    assert (t.device.address, t.device.name, t.device.details) == (ADDRESS, "CAMEO 4-fake",
                                                                    {"path": "/x", "props": {}})
    with pytest.raises(ValueError, match="not a Bluetooth device"):
        ble.BleTransport(DeviceRef("usb", "1-2", "CAMEO 4-fake"))


def test_ble_transport_release_never_drops_a_live_link(fake_client):
    async def scenario():
        t = ble.BleTransport(device())
        await t.connect(1.0)
        await t.release()
        live = t.is_up()
        await t.disconnect()
        return live, t.is_up()
    assert run(scenario()) == (True, False)


def test_a_dropped_ble_link_sets_disconnected(fake_client):
    async def scenario():
        c = await cameo4_gated()
        c.client.is_connected = False
        c.client.cb_disc(c.client)
        return c
    c = run(scenario())
    assert c.disconnected.is_set() and not c.is_connected and not c.transport_up()


# --- discovery -------------------------------------------------------------------------------------
def find(monkeypatch, devices, address=None, skip=()):
    monkeypatch.setattr(ble, "MessageBus", type("BlueZ", (stubs.FakeBlueZ,), {"devices": devices}))
    return run(ble.find_bonded_cameo(address, skip=skip))


def test_the_name_prefixes_are_the_registrys():
    assert set(ble_name_prefixes()) == {"CAMEO", "Portrait", "CURIO"}


@pytest.mark.parametrize("name, found", [
    ("CAMEO 4-fake", True), ("CAMEO 5 ALPHA-fake", True), ("CAMEO3-fake", True),
    ("cameo 4-fake", True), ("Portrait 3-fake", True), ("PORTRAIT4-fake", True),
    ("Portrait2-fake", True), ("CURIO 2-fake", True), ("curio-fake", True),
    ("Headphones", False), ("Silhouette-fake", False), ("My CAMEO", False), ("", False),
    ("Alta-fake", False)])
def test_discovery_takes_the_names_of_the_registry(monkeypatch, name, found):
    devices = [stubs.bluez_device(ADDRESS, name)]
    if found:
        assert find(monkeypatch, devices).name == name
    else:
        with pytest.raises(PlotterUnreachable, match="No paired CAMEO was found"):
            find(monkeypatch, devices)


def test_discovery_asks_the_registry_each_time(monkeypatch):
    monkeypatch.setattr(ble, "ble_name_prefixes", lambda: ("XYZ",))
    assert find(monkeypatch, [stubs.bluez_device(ADDRESS, "CAMEO 4-fake"),
                              stubs.bluez_device(SECOND, "xyz-fake")]).address == SECOND


def test_discovery_still_needs_the_service_and_pairing(monkeypatch):
    devices = [stubs.bluez_device(ADDRESS, "Portrait 3-fake", uuids=[]),
               stubs.bluez_device(SECOND, "CURIO 2-fake", paired=False)]
    with pytest.raises(PlotterUnreachable, match="No paired CAMEO was found"):
        find(monkeypatch, devices)
    devices.append(stubs.bluez_device("00:00:00:00:00:03", "Portrait 4-fake",
                                      uuids=[SERVICE.upper()]))
    assert find(monkeypatch, devices).name == "Portrait 4-fake"


def test_the_discovery_messages_are_unchanged(monkeypatch):
    with pytest.raises(PlotterUnreachable) as e:
        find(monkeypatch, [])
    assert str(e.value) == ("No paired CAMEO was found. Pair it first in the system Bluetooth "
                            "settings.")
    with pytest.raises(PlotterUnreachable) as e:
        find(monkeypatch, [stubs.bluez_device(SECOND, "Renamed", paired=False)], address=SECOND)
    assert str(e.value) == (f"The machine {SECOND} is known to the system but not paired. "
                            "Pair it first in the system Bluetooth settings.")


def test_a_non_text_name_is_not_a_machine(monkeypatch):
    with pytest.raises(PlotterUnreachable):
        find(monkeypatch, [stubs.bluez_device(ADDRESS, 4)])


def test_ble_discovery_gives_a_device_ref(monkeypatch):
    devices = [stubs.bluez_device(ADDRESS, "CAMEO 4-fake", connected=True),
               stubs.bluez_device(SECOND, "Portrait 4-fake")]
    monkeypatch.setattr(ble, "MessageBus", type("BlueZ", (stubs.FakeBlueZ,), {"devices": devices}))
    discovery = ble.BleDiscovery()
    first = run(discovery.find())
    second = run(discovery.find(None, skip={ADDRESS.lower()}))
    named = run(discovery.find(SECOND.lower()))
    assert (first.transport, first.address, first.name) == ("ble", ADDRESS, "CAMEO 4-fake")
    assert first.details["props"]["Connected"] is True and first.usb_id is None
    assert discovery.held_elsewhere(first) and not discovery.held_elsewhere(second)
    assert second == named and second.address == SECOND
    assert not discovery.held_elsewhere(DeviceRef("ble", ADDRESS, "x"))
    with pytest.raises(PlotterUnreachable):
        run(discovery.find(None, skip=[ADDRESS, SECOND]))
