# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The machine's Pause key during a job, the user's Pause and Resume, the cancel of a paused
machine and the patience of the ESC 05 waits (OpenCutter 0.1.1).

Nothing here reaches Bluetooth or D-Bus: stubs.install() runs before the driver is imported.
Two kinds of tests, as in test_device_safety_fixes.py:
  * a real link.SafeLink over a scripted machine (Machine below), for the daemon's waits and the
    link's patience: what is written while the machine is paused, how often, and when a wait
    ends. Times are scaled down (PAUSED_POLL, STATUS_OVERDUE, REPLY_TIMEOUT patched) so that the
    rules can be seen in a fraction of a second;
  * the real daemon in the tests' DaemonHarness, whose simulated machine knows Pause, Resume
    and the cancel of a paused job (tests/stubs.py FakeClient), for what goes over the wire,
    the ops and the ping.
The bytes of a normal job are not touched by any of this (tests/test_profile_
characterization.py pins them): only ESC 05, the stop pair and the user's ESC 07 / ESC 08.
"""
import asyncio
import dataclasses
import itertools
import json
import re
import types

import pytest

import stubs
from stubs import ESC05, ESC07, ESC08, ESC09, ESC15, FG, harness, stops  # noqa: F401

stubs.install()
from opencutter import __main__ as cli  # noqa: E402
from opencutter.device import client as client_mod, daemon, link, protocol  # noqa: E402
from opencutter.device.link import SafeLink  # noqa: E402
from opencutter.device.protocol import CHAR_STATUS, UnsafeCommand  # noqa: E402
from opencutter.device.transport import DeviceRef, TransportBusy  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings  # noqa: E402
from opencutter.machines import CAMEO4, Support, safety  # noqa: E402
from opencutter.machines.profile import FirmwareLine, Identity  # noqa: E402

U = b"U\x03"
MOVE = b"M100,100\x03"
SECOND, THIRD = b"M200,200\x03", b"M300,300\x03"
BIG = MOVE * 11                               # 99 bytes: eleven of them make two batches
PEN = ToolSettings(holder=1, tool_code=7, speed=1, force=1, acceleration=1, offset=0,
                   line_segment_overcut=False, overcut=(0, 0), blade_depth=None, passes=1)
SQUARE = [(60.0, 60.0), (85.4, 60.0), (85.4, 85.4), (60.0, 85.4), (60.0, 60.0)]


# --- a scripted machine behind a real SafeLink -----------------------------------------------
class Machine:
    """A RawTransport whose machine a test scripts. `status`: what ESC 05 answers now (read when
    the query is written). `latency`: seconds until the reply of each ESC 05 in turn (None: the
    reply is lost; when the list is empty, 0.01). `ack`: seconds until the Write Response of
    each ESC 05 in turn (0 when empty). `hooks`: data -> callable(machine) when it is written.
    `log`: (loop time, data) of every write. Writes are serialized as BlueZ does unless
    `serialize` is False. With `fifo`, replies come in the order of the queries, as from the
    machine (a reply is never earlier than the one before it)."""
    has_status_channel = True

    def __init__(self, serialize: bool = True):
        self.ref = DeviceRef("ble", stubs.ADDRESS, "CAMEO 4-unit")
        self.disconnected = asyncio.Event()
        self.up = True
        self.log: list[tuple[float, bytes]] = []
        self.on_reply = self.on_status = None
        self.status = "0"
        self.latency: list[float | None] = []
        self.ack: list[float] = []
        self.u_reply: str | None = "6100,6100"
        self.hooks: dict = {}
        self.serialize = serialize
        self.busy_on: set[bytes] = set()
        self._bus = asyncio.Lock()
        self.fifo = False
        self._next_reply = 0.0

    @property
    def writes(self) -> list[bytes]:
        return [d for _, d in self.log]

    def is_up(self) -> bool:
        return self.up

    async def connect(self, timeout):
        pass

    async def subscribe(self, on_reply, on_status):
        self.on_reply, self.on_status = on_reply, on_status

    async def unsubscribe(self):
        pass

    def indicate(self, status: str) -> None:
        self.status = status
        self.on_status(status.encode() + b"\x03")

    def later(self, seconds: float, status: str) -> None:
        asyncio.get_running_loop().call_later(seconds, self.indicate, status)

    async def write(self, data: bytes) -> None:
        if self.serialize:
            async with self._bus:
                await self._write(data)
        else:
            await self._write(data)

    async def _write(self, data: bytes) -> None:
        if not self.up:
            raise RuntimeError("Not connected")
        loop = asyncio.get_running_loop()
        self.log.append((loop.time(), data))
        if data in self.busy_on:
            raise TransportBusy("busy (simulated)")
        hook = self.hooks.get(data)
        if hook is not None:
            hook(self)
        if data == ESC05:
            ack = self.ack.pop(0) if self.ack else 0.0
            latency = self.latency.pop(0) if self.latency else 0.01
            reply = self.status.encode() + b"\x03"
            if latency is not None:
                when = loop.time() + ack + latency
                if self.fifo:
                    when = self._next_reply = max(when, self._next_reply + 0.001)
                loop.call_at(when, self.on_reply, reply)
            if ack:
                await asyncio.sleep(ack)
        elif data == ESC15:
            loop.call_later(0.01, self.on_reply, b" 7, 0\x03")
        elif data == U and self.u_reply is not None:
            loop.call_later(0.01, self.on_reply, self.u_reply.encode() + b"\x03")

    async def max_write(self):
        return 244

    async def release(self):
        pass

    async def disconnect(self):
        self.up = False
        self.disconnected.set()


async def make_link(sess=None, serialize: bool = True, profile=CAMEO4,
                    fg: str = stubs.TESTED_FG) -> tuple[Machine, SafeLink]:
    """A link that passed the gate; status indications reach `sess` as in the service."""
    m = Machine(serialize)
    lk = SafeLink(m, on_status=lambda s: daemon._note_status(sess, s))
    await lk.open()
    lk.fg_reply = fg
    lk.profile = profile
    await lk.refresh_write_limit()
    return m, lk


def make_session(wrote: bool = True, name: str = "job") -> daemon.MotionSession:
    plan = types.SimpleNamespace(validator=lambda w: None, required_tools={1: 7}, steps=[],
                                 summary={})
    sess = daemon.MotionSession(name, plan, {}, profile=CAMEO4)
    sess.wrote = sess.motion_possible = wrote
    return sess


def write_step(title: str = "Cut", writes=(MOVE,)):
    return types.SimpleNamespace(kind="write", writes=list(writes), title=title)


def units_step():
    return types.SimpleNamespace(kind="units", writes=[U], title="Check units (no motion)")


@pytest.fixture
def log(monkeypatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(daemon, "_log", lines.append)
    return lines


@pytest.fixture
def fast(monkeypatch):
    """Scaled-down times: a paused machine is read every 0.2 s, a reply is overdue after 0.7 s
    (at least 0.3 s after its Write Response), a write unacknowledged after 0.2 s, a reply
    still due 3 s after the last query or reply is lost."""
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.2)
    monkeypatch.setattr(link, "STATUS_OVERDUE", 0.7)
    monkeypatch.setattr(link, "REPLY_TIMEOUT", 0.3)
    monkeypatch.setattr(link, "QUERY_WRITE_TIMEOUT", 0.2)
    monkeypatch.setattr(link, "REPLY_LOST_AFTER", 3.0)


def between(m: Machine, start: float, end: float) -> list[tuple[float, bytes]]:
    return [(t, d) for t, d in m.log if start <= t < end]


# --- (1) the panel's Pause in the middle of a batch: wait, then go on --------------------------
def test_a_pause_mid_batch_waits_with_only_esc05_and_goes_on_without_a_stop(log, fast):
    marks: dict[str, float] = {}

    async def main():
        sess = make_session()
        m, lk = await make_link(sess)
        loop = asyncio.get_running_loop()

        def paused(mm):
            if "paused" not in marks:
                marks["paused"] = loop.time()
                mm.indicate("3")                       # Pause pressed as the first chunk lands
                mm.later(1.2, "1")                     # pressed again 1.2 s later
                mm.later(1.6, "0")

        m.hooks[MOVE] = paused
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step(writes=[MOVE, SECOND,
                                                                                   THIRD]))
        return m, res, sess
    m, res, sess = asyncio.run(main())
    assert res["ok"] is True and ESC09 not in m.writes
    assert m.writes.index(SECOND) > m.writes.index(MOVE) and THIRD in m.writes
    resumed = next(t for t, d in m.log if d == SECOND)
    assert resumed - marks["paused"] >= 1.15                   # nothing written meanwhile
    during = between(m, marks["paused"] + 0.001, resumed)
    assert during and all(d == ESC05 for _, d in during)       # only ESC 05 while paused
    gaps = [b[0] - a[0] for a, b in zip(during, during[1:])]
    assert gaps and all(g >= 0.18 for g in gaps)               # every PAUSED_POLL, not faster
    assert len(during) <= 1.2 / 0.2 + 1
    assert sess.paused is None and sess.interrupted is None and sess.pauses == 1
    assert any(x.startswith("job: the machine paused (3) during 'Cut', batch 1/1; waiting")
               for x in log)
    assert any(x.startswith("job: the machine goes on (1) after 1 s") for x in log)


def test_the_paused_cadence_and_the_patience_constants():
    assert daemon.PAUSED_POLL == 2.5 and daemon.BUSY_POLL == 0.4 and daemon.FIRST_POLL == 0.5
    assert protocol.STATUS_OVERDUE == 7.0 and link.STOP_QUIET == link.RESUME_QUIET == 0.3


# --- (2) how a pause ends ----------------------------------------------------------------------
@pytest.mark.parametrize("mid_batch, ours, ends", [
    (False, False, False),           # between batches: it went on with nothing left
    (True, True, False),             # our Resume less than RESUME_WINDOW s ago
    (True, False, True),             # mid-batch, no Busy and no Cancelling seen: maybe a cancel
])
def test_ready_after_a_pause(log, mid_batch, ours, ends):
    sess = make_session()
    sess.batch_open = mid_batch
    daemon._note_status(sess, "3")
    assert sess.paused == "3" and sess.paused_mid_batch is mid_batch
    if ours:
        sess.resume_sent_at = daemon._now()
    daemon._note_status(sess, "0")
    assert sess.paused is None
    assert sess.interrupted == (daemon.LEFT_PAUSE_MARK if ends else None)
    assert daemon._status_stop_text(daemon.LEFT_PAUSE_MARK, sess) == daemon.LEFT_PAUSE


def test_ready_long_after_our_resume_is_ambiguous(log):
    sess = make_session()
    sess.batch_open = True
    daemon._note_status(sess, "3")
    sess.resume_sent_at = daemon._now() - daemon.RESUME_WINDOW - 1
    daemon._note_status(sess, "0")
    assert sess.interrupted == daemon.LEFT_PAUSE_MARK
    program = make_session(name="dry-run")
    assert daemon._status_stop_text(daemon.LEFT_PAUSE_MARK, program) == \
        daemon.LEFT_PAUSE_PROGRAM


def test_busy_after_a_pause_goes_on_and_a_later_ready_is_plain(log):
    sess = make_session()
    sess.batch_open = True
    for digit in ("3", "3", "1", "0"):
        daemon._note_status(sess, digit)
    assert sess.paused is None and sess.interrupted is None and sess.pauses == 1


def test_a_pause_after_our_own_stop_is_not_a_pause(log):
    sess = make_session()
    sess.stop.set()
    daemon._note_status(sess, "3")
    daemon._note_status(sess, "4")
    assert sess.paused is None and sess.interrupted is None


@pytest.mark.parametrize("end", ["4", "2"])
def test_a_pause_then_a_cancel_or_an_unload_on_the_panel_ends_with_one_pair(log, fast, end):
    async def main():
        sess = make_session()
        m, lk = await make_link(sess)

        def paused(mm):
            if not sess.pauses:
                mm.indicate("3")
                mm.later(0.5, end)
        m.hooks[MOVE] = paused
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step(writes=[MOVE, SECOND]))
        await asyncio.sleep(0.1)
        return m, res
    m, res = asyncio.run(main())
    assert res["ok"] is False and res["stopped"] is True and res["stop_errors"] == []
    assert m.writes.count(ESC09) == 2 and SECOND not in m.writes      # the pair all the same
    if end == "4":
        assert res["error"].startswith("The machine cancelled the job (status '4' = "
                                       "Cancelling).")
    else:
        assert res["error"] == daemon.MEDIA_CHANGED_STEP


def test_a_pause_between_batches_goes_on_after_a_fresh_ready(log, fast):
    async def main():
        sess = make_session()
        m, lk = await make_link(sess)
        sess.running = True                         # the step runs; its batch was read ready
        daemon._note_status(sess, "3")
        m.status = "3"
        m.later(0.5, "0")                           # Pause pressed again: nothing was left
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        status = await daemon._write_chunk(lk, asyncio.Lock(), sess, MOVE, "0")
        return m, status, loop.time() - t0, sess
    m, status, took, sess = asyncio.run(main())
    assert took >= 0.5 and status == "0" and m.writes[-1] == MOVE
    assert set(m.writes[:-1]) == {ESC05} and sess.batch_open is True


def test_no_tool_check_reaches_a_paused_machine_before_a_lowering_write(log, fast):
    down = b"D100,100\x03"

    async def main():
        sess = make_session()
        m, lk = await make_link(sess)
        sess.running = sess.batch_open = True       # in the middle of a batch
        daemon._note_status(sess, "3")
        m.status = "3"
        loop = asyncio.get_running_loop()
        m.later(0.7, "1")                           # Pause pressed again
        t0 = loop.time()
        await daemon._write_chunk(lk, asyncio.Lock(), sess, down, "1")
        return m, t0
    m, t0 = asyncio.run(main())
    first_tool_check = next(t for t, d in m.log if d == ESC15)
    assert first_tool_check - t0 >= 0.7             # ESC 15 only once it went on
    assert [d for t, d in m.log if t < first_tool_check] == [ESC05] * len(
        [1 for t, d in m.log if t < first_tool_check])
    assert m.writes[-2:] == [ESC15, down]


# --- (3) a pause at the outline check: the cut waits ---------------------------------------------
@pytest.mark.parametrize("goes_on", ["0", "1"])
def test_a_pause_at_the_outline_check_makes_the_cut_wait(log, fast, goes_on):
    async def main():
        sess = make_session()                       # header and outline went out
        m, lk = await make_link(sess)
        m.indicate("3")                             # Pause while the app shows Check the position
        loop = asyncio.get_running_loop()
        m.later(0.8, goes_on)
        if goes_on == "1":
            m.later(1.0, "0")
        t0 = loop.time()
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step())
        return m, res, loop.time() - t0, t0
    m, res, took, t0 = asyncio.run(main())
    assert res["ok"] is True and took >= 0.8 and ESC09 not in m.writes
    first = next(t for t, d in m.log if d == MOVE)
    assert first - t0 >= 0.8 and all(d == ESC05 for t, d in m.log if t < first)


# --- patience: late replies, lost replies, write stalls ------------------------------------------
@pytest.mark.parametrize("late", [0.35, 0.65, 0.8], ids=["3.5s", "6.5s", "8s"])
def test_late_replies_never_end_the_ready_wait(log, fast, late):
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        m.status = "1"
        m.latency = [late] * 4
        asyncio.get_running_loop().call_later(2.0, setattr, m, "status", "0")
        return await asyncio.wait_for(daemon.wait_ready(lk, asyncio.Lock(), sess, 60), 15), m
    status, m = asyncio.run(main())
    assert status == "0" and ESC09 not in m.writes


def test_a_lost_reply_is_asked_again_once_it_is_overdue(log, fast):
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        m.latency = [None]                          # the first reply is lost
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        status = await daemon.wait_ready(lk, asyncio.Lock(), sess, 60)
        return status, m, t0
    status, m, t0 = asyncio.run(main())
    asks = [t for t, d in m.log if d == ESC05]
    assert status == "0" and len(asks) >= 2 and asks[1] - asks[0] >= 0.69
    assert any(x.startswith("job: ESC 05 not answered for 1 s (write acknowledged after 0.00 s; "
                            "no status indication since); asking again, nothing new is sent")
               for x in log)
    assert any(x.startswith("job: ESC 05 answered again after ") for x in log)


def test_no_answer_for_long_with_indications_never_aborts_and_shows_checking(log, fast):
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        m.status = "1"
        m.latency = [None] * 1000                   # every reply is lost
        loop = asyncio.get_running_loop()
        for i in range(1, 12):
            loop.call_later(i * 0.5, m.indicate, "1")   # the machine still reports Busy
        wait = asyncio.ensure_future(daemon.wait_ready(lk, asyncio.Lock(), sess, 60))
        await asyncio.sleep(6.0)
        checking = lk.no_reply_seconds()
        state = {"c": lk, "motion": sess, "support": "tested"}
        fields = daemon._stop_fields(state)
        alive = not wait.done()
        n = m.writes.count(ESC05)
        errors = await daemon._stop_session(lk, sess, force=True)
        with pytest.raises(daemon.MotionAborted) as e:
            await asyncio.wait_for(wait, 2)
        return alive, checking, fields, n, errors, str(e.value), m, lk
    alive, checking, fields, n, errors, text, m, lk = asyncio.run(main())
    assert alive and checking is not None and checking >= 5.0
    assert fields["no_reply_s"] >= 5.0 and fields["indication_age_s"] < 1.0
    assert 6 <= n <= 10                             # one re-ask per overdue period, no more
    assert errors == [] and m.writes.count(ESC09) == 2 and text == daemon.STOPPED_IN_STEP
    assert lk.no_reply_seconds() is None
    overdue = [x for x in log if "not answered for" in x]
    assert len(overdue) == 1 and " since, last '1' " in overdue[0]


def test_a_query_write_without_its_write_response_is_waited_for_never_written_again(log, fast):
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        m.status = "1"
        m.ack = [1.5]                               # the Write Response comes 1.5 s late
        asyncio.get_running_loop().call_later(2.5, setattr, m, "status", "0")
        status = await daemon.wait_ready(lk, asyncio.Lock(), sess, 60)
        return status, m
    status, m = asyncio.run(main())
    asks = [t for t, d in m.log if d == ESC05]
    assert status == "0" and asks[1] - asks[0] >= 1.5          # no second write meanwhile
    assert any(x.startswith("job: ESC 05 write not acknowledged after 0.2 s") for x in log)
    assert any(x.startswith("job: ESC 05 answered again after 1.") and
               x.endswith("(1 query, 0 late replies dropped)") for x in log), log


def test_the_ready_timeout_counts_busy_replies_only(log, fast):
    async def main():
        sess = make_session()
        sess.running = sess.batch_open = True
        m, lk = await make_link(sess)
        m.status = "1"
        m.latency = [0.01, None, None, None, None, 0.01]   # about 3 s without a reply
        loop = asyncio.get_running_loop()
        loop.call_later(3.4, m.indicate, "3")       # then paused for 3 s
        loop.call_later(6.4, m.indicate, "1")       # Pause pressed again
        loop.call_later(6.5, setattr, m, "status", "0")
        t0 = loop.time()
        status = await daemon.wait_ready(lk, asyncio.Lock(), sess, 1.0)
        return status, loop.time() - t0, sess
    status, took, sess = asyncio.run(main())
    assert status == "0" and took > 6.4 and sess.interrupted is None


def test_a_reply_that_is_not_fresh_does_not_end_the_wait_for_the_next_batch(log, fast):
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        m.latency = [1.6]                           # a '0' that comes more than twice overdue
        return await daemon.wait_ready(lk, asyncio.Lock(), sess, 60), m
    status, m = asyncio.run(main())
    assert status == "0" and m.writes.count(ESC05) >= 3


def test_a_machine_that_stays_busy_still_ends_at_the_ready_timeout(log, fast):
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        m.status = "1"
        with pytest.raises(daemon.MotionAborted, match="still Busy after 1 s"):
            await daemon.wait_ready(lk, asyncio.Lock(), sess, 1.0)
    asyncio.run(main())


def test_a_ready_that_is_not_fresh_is_read_again(log):
    calls = []

    class Link:
        is_connected = True
        last_status = "1"

        async def patient_esc(self, code, stop=None, report=None):
            calls.append(code)
            return ("0", False) if len(calls) == 1 else ("0", True)

    async def main():
        sess = make_session()
        sess.running = True
        return await daemon.wait_ready(Link(), asyncio.Lock(), sess, 60)
    assert asyncio.run(main()) == "0" and calls == [0x05, 0x05]


def test_patient_esc_says_whether_the_reply_is_fresh(fast):
    async def main():
        m, lk = await make_link()
        first = await lk.patient_esc(0x05)
        m.latency = [None]
        second = await lk.patient_esc(0x05)        # asked again: not fresh
        third = await lk.patient_esc(0x05)
        return first, second, third
    first, second, third = asyncio.run(main())
    assert first == ("0", True) and second == ("0", False)
    assert third[0] == "0"


def test_the_owed_reply_patterns_never_abort_a_ready_wait(log, monkeypatch):
    """Every pattern of four polls after a normal one, each reply coming at once, 3.5 s or
    6.5 s late or never (scaled by 0.05), then normal ones: with the owed-reply bookkeeping of
    0.1.0 and its miss counter many of them aborted the job. None may end the wait but ready."""
    monkeypatch.setattr(link, "STATUS_OVERDUE", 0.35)
    monkeypatch.setattr(link, "REPLY_TIMEOUT", 0.15)
    monkeypatch.setattr(link, "REPLY_LOST_AFTER", 1.5)
    monkeypatch.setattr(daemon, "BUSY_POLL", 0.02)
    monkeypatch.setattr(daemon, "FIRST_POLL", 0.0)
    values = {"n": 0.01, "L3": 0.175, "L6": 0.325, "X": None}

    async def one(combo):
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        m.status = "1"
        m.latency = [0.01] + [values[c] for c in combo]
        asks = 0

        def count(mm):
            nonlocal asks
            asks += 1
            if asks == 6:
                mm.status = "0"
        m.hooks[ESC05] = count
        return await asyncio.wait_for(daemon.wait_ready(lk, asyncio.Lock(), sess, 600), 20)

    async def main():
        combos = list(itertools.product(values, repeat=4))
        return combos, await asyncio.gather(*(one(c) for c in combos), return_exceptions=True)
    combos, results = asyncio.run(main())
    failed = [(" ".join(c), r) for c, r in zip(combos, results) if r != "0"]
    assert not failed, failed[:5]


# --- the cancel of a paused machine --------------------------------------------------------------
def test_the_stop_holds_queries_for_its_quiet_window_and_reads_paused_back(log):
    async def main():
        m, lk = await make_link()
        loop = asyncio.get_running_loop()
        m.status = "3"
        m.later(1.0, "4")
        m.later(1.3, "0")
        state = {"motion": None, "stop_owed": set(), "owed_stops_sent": 0}
        reply = await daemon._stop_op({"op": "stop", "check": True}, lk, asyncio.Lock(), state)
        return m, reply, state, loop
    m, reply, state, loop = asyncio.run(main())
    assert reply == {"ok": True, "errors": [], "status_after": "0", "still_busy": False}
    stop_times = [t for t, d in m.log if d == ESC09]
    reads = [t for t, d in m.log if d == ESC05]
    assert len(stop_times) == 2 and reads[0] - stop_times[-1] >= 0.29   # the quiet window
    assert set(m.writes) == {ESC05, ESC09}
    assert state["stop_check"]["status"] == "0" and state["stop_check"]["running"] is False
    assert state["stop_check"]["still_paused"] is False and state["stop_check"]["seconds"] >= 0.7
    assert any(re.fullmatch(r"cancel while paused: the machine read 3 for \d+\.\d s, then 4, "
                            r"then 0", x) for x in log), log


def test_a_machine_still_paused_after_the_stop_is_reported(log, monkeypatch):
    monkeypatch.setattr(daemon, "STOP_CHECK_PAUSED", 1.0)

    async def main():
        m, lk = await make_link()
        m.status = "3"                              # it never leaves Pause
        state = {"motion": None, "stop_owed": set(), "owed_stops_sent": 0}
        return await daemon._stop_op({"op": "stop", "check": True}, lk, asyncio.Lock(), state)
    reply = asyncio.run(main())
    assert reply["status_after"] == "3" and reply["still_busy"] is False
    assert any(x.startswith("after the stop the machine still reports 3 = Paused 1 s later: "
                            "press Unload") for x in log)
    line, alarm = cli.after_stop_line(reply)
    assert alarm and "still reports Paused 15 s after the stop: press Unload" in line


def test_a_failed_stop_logs_what_each_write_did(log, monkeypatch):
    monkeypatch.setattr(link, "STOP_WRITE_TIMEOUT", 0.05)

    async def main():
        m, lk = await make_link(serialize=False)
        count = [0]

        async def second_hangs(data, real=m._write):
            if data == ESC09:
                count[0] += 1
                if count[0] == 2:
                    await asyncio.Event().wait()
            await real(data)
        m._write = second_hangs
        sess = make_session()
        return await daemon._stop_session(lk, sess, force=True)
    errors = asyncio.run(main())
    assert errors == ["1b 09: TimeoutError: no Write Response within 0.05 s"]
    assert any(x.startswith("job: STOP (ESC 09 x2) FAILED, pair 1 on this link: write 1 "
                            "acknowledged after 0.0") and
               x.endswith("; write 2 no Write Response within 0.05 s") for x in log)


def test_a_read_back_that_gets_no_status_says_why(log):
    async def main():
        m, lk = await make_link()
        m.latency = [None] * 10                     # ESC 05 is never answered
        state = {"motion": None, "stop_owed": set(), "owed_stops_sent": 0}
        return await daemon._stop_op({"op": "stop", "check": True}, lk, asyncio.Lock(), state), \
            state
    reply, state = asyncio.run(main())
    assert reply == {"ok": True, "errors": [], "status_after": None, "still_busy": None}
    assert "after the stop: the status could not be read (no reply to ESC 05 within 3 s)" in log
    assert state["stop_check"]["reason"] == "no reply to ESC 05 within 3 s"


def test_a_slow_write_response_is_logged(log, monkeypatch):
    monkeypatch.setattr(link, "SLOW_WRITE_LOG", 0.1)

    async def main():
        m, lk = await make_link()
        lk.on_slow_write = daemon._log_slow_write
        m.ack = [0.2]
        return await lk.esc_query(0x05)
    assert asyncio.run(main()) == "0"
    assert any(x.startswith("slow Write Response: 1b 05 after 0.2 s") for x in log)


# --- the safety floor of ESC 07 and ESC 08 -------------------------------------------------------
def test_esc_07_and_08_are_never_a_query_a_motion_write_or_the_stop():
    for data in (ESC07, ESC08):
        with pytest.raises(UnsafeCommand):
            protocol.check_safe(data, CAMEO4)
        with pytest.raises(safety.SafetyError):
            safety.check_query(data, CAMEO4)
        with pytest.raises(safety.SafetyError):
            safety.check_write(data, CAMEO4)
        with pytest.raises(safety.SafetyError):
            safety.check_write(MOVE[:-1] + data + b"\x03", CAMEO4)
        assert not safety.is_stop_write(data)
    assert safety.PAUSE_WRITE == ESC07 and safety.RESUME_WRITE == ESC08
    assert CAMEO4.pause == ESC07 and CAMEO4.resume == ESC08
    assert {0x00, 0x11, 0x12, 0x13} <= safety.NEVER_ESC and not {0x07, 0x08} & safety.NEVER_ESC
    assert not ({0x07, 0x08} & safety.QUERY_ESC)
    for p in safety_profiles():
        assert p.support is Support.TESTED or (p.pause is None and p.resume is None), p.id


def safety_profiles():
    from opencutter.machines import registry
    return registry.profiles()


@pytest.mark.parametrize("bad", [
    dict(resume=ESC09), dict(resume=ESC07), dict(resume=ESC08 + b"\x03"), dict(pause=ESC08),
    dict(pause=b"\x07"), dict(pause="\x1b\x07")])
def test_a_profile_can_only_have_the_exact_pause_and_resume(bad):
    with pytest.raises(safety.UnsafeProfile):
        safety.check_profile(dataclasses.replace(CAMEO4, **bad))


def test_the_link_writes_esc_08_only_as_resume_and_never_twice():
    async def main():
        m, lk = await make_link()
        with pytest.raises(UnsafeCommand):
            await lk.esc_query(0x08)
        with pytest.raises(UnsafeCommand):
            await lk.write_validated(ESC08, lambda d: None)
        took = await lk.resume()
        m.busy_on.add(ESC07)
        with pytest.raises(TransportBusy):
            await lk.pause()                        # not written again
        return m, lk, took
    m, lk, took = asyncio.run(main())
    assert m.writes == [ESC08, ESC07] and lk.resume_writes == 1 and lk.pause_writes == 1
    assert took >= 0


@pytest.mark.parametrize("case", ["untested-profile", "untested-firmware", "no-resume",
                                  "before-gate"])
def test_the_link_refuses_resume_where_it_was_not_tested(case):
    async def main():
        if case == "untested-profile":
            untested = (FirmwareLine(5, Support.UNTESTED, "made up for this test"),)
            profile = dataclasses.replace(CAMEO4, support=Support.UNTESTED, identity=dataclasses.
                                          replace(CAMEO4.identity, lines=untested))
            m, lk = await make_link(profile=profile)
        elif case == "untested-firmware":
            m, lk = await make_link(fg="CAMEO 4 V1.20    ")
        elif case == "no-resume":
            m, lk = await make_link(profile=dataclasses.replace(CAMEO4, resume=None))
        else:
            m, lk = await make_link()
            lk.profile = None
        with pytest.raises(UnsafeCommand):
            await lk.resume()
        return m
    assert asyncio.run(main()).writes == []


def test_no_resume_while_a_stop_is_being_sent(monkeypatch):
    async def main():
        m, lk = await make_link()
        m.hooks[ESC09] = lambda mm: None
        stop = asyncio.ensure_future(lk.emergency_stop())
        await asyncio.sleep(0.05)                   # between the two stop writes
        with pytest.raises(UnsafeCommand):
            await lk.resume()
        await stop
        return m
    assert asyncio.run(main()).writes == [ESC09, ESC09]


# --- the real daemon: the machine's Pause key, the ops, the ping ---------------------------------
def spec_dict() -> dict:
    job = JobSpec(mat="12x12", media=(50.0, 50.0, 100.0, 100.0),
                  groups=[CutGroup(settings=PEN, paths=[SQUARE])], preview_outline=True)
    return json.loads(json.dumps(job.to_dict()))


def timed(f) -> list[tuple[float, bytes]]:
    """Record the loop time of every write of the simulated machine."""
    times: list[tuple[float, bytes]] = []
    f.on_write = lambda data: times.append((asyncio.get_running_loop().time(), data))
    return times


async def _until(predicate, timeout: float = 15.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def _ping(h) -> dict:
    return (await h.raw([{"op": "ping", "stop": True}]))[2][0]


async def _moving(f) -> bool:
    return f.moving()


async def _paused(h) -> bool:
    return (await _ping(h))["paused"]


async def _open_job(h, s):
    """Open the pen job and run it to its cut step (the outline check confirmed)."""
    opened = await s.open_job(spec_dict())
    assert opened["ok"], opened
    gate = next(i for i, st in enumerate(opened["steps"]) if st["confirm"])
    for i in range(gate):
        assert (await s.step(i, 60))["ok"], i
    return opened, gate


def test_the_pause_key_mid_cut_then_again_completes_the_job(harness, monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0", move_s=0.8)
        async with h.client().session() as s:
            opened, gate = await _open_job(h, s)
            times = timed(f)
            cut = asyncio.ensure_future(s.step(gate, 60))
            await _until(lambda: _moving(f))
            f.press_pause()
            loop = asyncio.get_running_loop()
            t_paused = loop.time()
            await _until(lambda: _paused(h))
            ping = await _ping(h)
            await asyncio.sleep(1.5)
            assert not cut.done()                   # it waits, with no time limit
            t_resumed = loop.time()
            f.press_pause()
            res = await cut
            rest = [await s.step(i, 60) for i in range(gate + 1, len(opened["steps"]))]
            await s.end()
        return res, rest, ping, times, t_paused, t_resumed, f
    res, rest, ping, times, t_paused, t_resumed, f = harness.run(scenario)
    assert res["ok"] is True and all(r["ok"] for r in rest), (res, rest)
    assert ESC09 not in f.writes and ESC08 not in f.writes and f.ignored == []
    assert ping["paused"] is True and ping["resume_ok"] is True and ping["pause_ok"] is False
    assert ping["interrupted"] is None and ping["motion_stopped"] is False
    during = [d for t, d in times if t_paused < t < t_resumed]
    assert during and set(during) == {ESC05}


def test_the_resume_op_sends_one_esc_08_with_a_quiet_window(harness, monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0", move_s=0.8)
        async with h.client().session() as s:
            opened, gate = await _open_job(h, s)
            cut = asyncio.ensure_future(s.step(gate, 60))
            await _until(lambda: _moving(f))
            f.press_pause()
            await _until(lambda: _paused(h))
            times = timed(f)
            reply = await h.client().resume()
            again = await h.client().resume()      # it goes on: a second click is refused
            res = await cut
            await s.end()
        return reply, again, res, times, f
    reply, again, res, times, f = harness.run(scenario)
    assert reply["ok"] is True and reply["sent"] is True and reply["status_before"] == "3"
    assert reply["paused"] is False and reply["status_after"] in ("1", "0")
    assert again["ok"] is False and again["sent"] is False
    assert "not paused" in again["error"] or "no job" in again["error"]
    assert res["ok"] is True and f.writes.count(ESC08) == 1 and ESC09 not in f.writes
    sent = next(t for t, d in times if d == ESC08)
    after = [t for t, d in times if d == ESC05 and t > sent]
    assert after and after[0] - sent >= 0.29        # no query in its quiet window
    assert any(x == "job: resume asked: ESC 08 sent (the machine read 3)" for x in harness.log)


def test_the_pause_op_pauses_a_running_job_and_resume_goes_on(harness, monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0", move_s=0.8)
        async with h.client().session() as s:
            opened, gate = await _open_job(h, s)
            before = await _ping(h)
            cut = asyncio.ensure_future(s.step(gate, 60))
            await _until(lambda: _moving(f))
            ok_now = (await _ping(h))["pause_ok"]
            paused = await h.client().pause()
            ping = await _ping(h)
            twice = await h.client().pause()
            await asyncio.sleep(0.8)
            assert not cut.done()
            resumed = await h.client().resume()
            res = await cut
            await s.end()
        return before, ok_now, paused, ping, twice, resumed, res, f
    before, ok_now, paused, ping, twice, resumed, res, f = harness.run(scenario)
    assert before["pause_ok"] is True and ok_now is True
    assert paused["ok"] is True and paused["paused"] is True and paused["status_after"] == "3"
    assert ping["paused"] is True and ping["resume_ok"] is True and ping["pause_ok"] is False
    assert twice["ok"] is False and twice["error"] == "the machine is already paused"
    assert resumed["ok"] is True and res["ok"] is True
    assert f.writes.count(ESC07) == 1 and f.writes.count(ESC08) == 1 and ESC09 not in f.writes
    assert any(x == "job: pause asked: ESC 07 sent (the machine read 1)" for x in harness.log)


def test_the_resume_and_pause_ops_refuse_without_a_paused_job(harness):
    async def scenario(h):
        f = await h.fresh()
        none = await h.client().resume(), await h.client().pause()
        r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}}])
        assert out[0]["ok"]
        not_started = await h.client().pause()
        not_paused = await h.client().resume()
        w.write((json.dumps({"op": "step", "index": 0}) + "\n").encode())
        await w.drain()
        assert json.loads(await asyncio.wait_for(r.readline(), 30))["ok"]
        f.press_pause()                             # paused at the panel between steps
        await asyncio.sleep(0.1)
        await h.client().stop()
        stopped = await h.client().resume()
        w.close()
        return none, not_started, not_paused, stopped, f
    none, not_started, not_paused, stopped, f = harness.run(scenario)
    assert none[0] == {"ok": False, "sent": False, "status_before": None, "status_after": "0",
                       "paused": False, "errors": [], "error": "no job is running"}
    assert none[1]["error"] == "no job is running"
    assert not_started["error"] == "the job has not started yet"
    assert not_paused["error"] == "the machine is not paused (status '0' = Ready)"
    assert stopped["error"] == "no job is running" or stopped["error"].startswith("a stop")
    assert ESC07 not in f.writes and ESC08 not in f.writes


def test_the_resume_op_refuses_when_the_fresh_status_is_not_paused(harness):
    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0")
        async with h.client().session() as s:
            opened, gate = await _open_job(h, s)
            stubs_indicate(f, "3")                  # the session pauses on an indication...
            f.status = "0"                          # ...but a fresh ESC 05 reads Ready
            assert (await _ping(h))["paused"] is True
            reply = await h.client().resume()
            await s.end()
        return reply, f
    reply, f = harness.run(scenario)
    assert reply["ok"] is False and reply["sent"] is False and reply["status_before"] == "0"
    assert ESC08 not in f.writes


def stubs_indicate(f, status: str) -> None:
    f.status = status
    f.cbs[CHAR_STATUS](None, bytearray(status.encode() + b"\x03"))


def test_resume_and_pause_are_refused_on_a_machine_without_them(harness):
    no_commands = dataclasses.replace(
        CAMEO4, id="test_plain", name="Plain Cutter",
        identity=Identity(fg_models=frozenset({"PLAIN CUTTER"}),
                          lines=(FirmwareLine(1, Support.TESTED, "made up for this test"),),
                          ble_names=("Plain",)),
        programs=frozenset(), pause=None, resume=None, basis="made up for this test")
    harness.register(no_commands)
    harness.simulate(no_commands, tools=" 7, 0")

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0")
        spec = {**spec_dict(), "machine": "test_plain"}
        async with h.client().session() as s:
            opened = await s.open_job(spec)
            assert opened["ok"], opened
            assert (await s.step(0, 60))["ok"]
            stubs_indicate(f, "3")
            ping = await _ping(h)
            reply = await h.client().resume()
            await h.client().stop()
        return ping, reply, f
    ping, reply, f = harness.run(scenario)
    assert ping["paused"] is True and ping["resume_ok"] is False and ping["pause_ok"] is False
    assert reply["error"] == ("the Plain Cutter has not been tested with resume: press its "
                              "Pause key")
    assert ESC08 not in f.writes


def test_a_cancel_while_paused_reads_back_until_ready_and_sends_nothing_else(harness,
                                                                            monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0", move_s=0.8, cancel_quiet_s=1.5)
        async with h.client().session() as s:
            opened, gate = await _open_job(h, s)
            cut = asyncio.ensure_future(s.step(gate, 60))
            await _until(lambda: _moving(f))
            f.press_pause()
            await _until(lambda: _paused(h))
            times = timed(f)
            checked = await h.client().stop(check=True)
            res = await cut
            ping = await _ping(h)
        return checked, res, ping, times, f
    checked, res, ping, times, f = harness.run(scenario)
    assert checked == {"ok": True, "errors": [], "status_after": "0", "still_busy": False}
    assert res["ok"] is False and res["stopped"] is True and res["error"] == daemon.STOPPED_IN_STEP
    assert stops(f.writes) == 1 and f.writes.count(ESC09) == 2       # one pair, never a second
    after = [d for t, d in times]
    assert set(after) == {ESC05, ESC09}             # no FG, no U, no ESC 15 after the stop
    stop_at = max(t for t, d in times if d == ESC09)
    reads = [t for t, d in times if d == ESC05 and t > stop_at]
    assert reads and reads[0] - stop_at >= 0.29     # the quiet window
    assert ping["stop_check"]["status"] == "0" and ping["stop_check"]["seconds"] >= 1.0
    assert any(re.fullmatch(r"job: cancel while paused: the machine read 3 for \d+\.\d s, then "
                            r"4, then 0", x) for x in harness.log), harness.log


@pytest.mark.parametrize("key", ["unload", "long-press"])
def test_a_panel_cancel_while_paused_ends_the_job_with_one_pair(harness, monkeypatch, key):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0", move_s=0.8, cancel_quiet_s=0.5)
        async with h.client().session() as s:
            opened, gate = await _open_job(h, s)
            cut = asyncio.ensure_future(s.step(gate, 60))
            await _until(lambda: _moving(f))
            f.press_pause()
            await _until(lambda: _paused(h))
            f.press_unload() if key == "unload" else f.hold_pause()
            res = await cut
            await asyncio.sleep(1.0)
        return res, f
    res, f = harness.run(scenario)
    assert res["ok"] is False and res["stopped"] is True
    assert res["error"].startswith("The machine cancelled the job (status '4' = Cancelling).")
    assert stops(f.writes) == 1 and f.writes[-1] == ESC09     # nothing more after the pair
    assert ESC08 not in f.writes


def test_nothing_but_esc05_reaches_a_paused_machine_at_the_unit_check(harness, monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0")
        async with h.client().session() as s:
            opened = await s.open_job(spec_dict())
            assert opened["ok"]
            assert (await s.step(0, 60))["ok"]      # the header
            f.press_pause()                         # paused before the unit check
            await asyncio.sleep(0.1)
            n = len(f.writes)
            units = asyncio.ensure_future(s.step(1, 60))
            await asyncio.sleep(1.0)
            waiting = list(f.writes[n:])
            f.press_pause()
            res = await units
            await s.end()
        return waiting, res, f
    waiting, res, f = harness.run(scenario)
    assert waiting and set(waiting) == {ESC05}       # no U, no ESC 15 while it is paused
    assert res["ok"] is True and res["verdict"] == "ok" and f.ignored == []


def test_info_never_sends_fg_and_queries_wait_while_paused(harness):
    async def scenario(h):
        f = await h.fresh(tools=" 2, 4")
        info = await h.client().info()
        f.status_override = "3"                     # paused by someone, no job of ours
        stubs_indicate(f, "3")
        paused_info = await h.client().info()
        query = (await h.raw([{"op": "query", "cmd": "FA"}]))[2][0]
        tools = (await h.raw([{"op": "esc", "code": 0x15}]))[2][0]
        status = (await h.raw([{"op": "esc", "code": 0x05}]))[2][0]
        f.status_override = None
        stubs_indicate(f, "0")
        await h.client().stop()
        after_stop = (await h.raw([{"op": "query", "cmd": "FA"}]))[2][0]
        return info, paused_info, query, tools, status, after_stop, f
    info, paused_info, query, tools, status, after_stop, f = harness.run(scenario)
    assert info == {"ok": True, "status": "0", "tools": " 2, 4", "firmware": "CAMEO 4 V5.05"}
    assert paused_info == {"ok": True, "status": "3", "tools": None, "firmware": "CAMEO 4 V5.05"}
    assert query["ok"] is False and "nothing but ESC 05" in query["error"]
    assert tools["ok"] is False and status == {"ok": True, "reply": "3"}
    assert after_stop["ok"] is False and "a stop went out" in after_stop["error"]
    assert FG not in f.writes and b"FA\x03" not in f.writes


def test_a_one_shot_failure_is_logged_once_until_it_works_again(log):
    state: dict = {}
    for _ in range(3):
        daemon._note_op(state, "info", {"ok": False, "error": "TimeoutError: no reply"},
                        failed=True)
    daemon._note_op(state, "info", {"ok": True})
    daemon._note_op(state, "resume", {"ok": False, "sent": False, "error": "no job is running"})
    daemon._note_op(state, "esc", {"ok": False, "error": "refused: the machine reports '3'"})
    daemon._note_op(state, "ping", {"ok": False, "error": "x"}, failed=True)
    assert log == ["the info op failed: TimeoutError: no reply",
                   "the info op works again (after 3 failures)"]


# --- the command line ----------------------------------------------------------------------------
def test_the_watch_lines_of_a_running_step():
    seen: dict = {}
    assert cli.watch_lines({"paused": False, "status": "1"}, seen) == []
    paused = cli.watch_lines({"paused": True, "status": "3", "resume_ok": True}, seen)
    assert paused == ["      .. the machine is paused (status '3'). Press its Pause key to go "
                      "on, run 'opencutter resume' in another terminal, or press Ctrl+C to "
                      "stop."]
    assert cli.watch_lines({"paused": True, "status": "3", "resume_ok": True}, seen) == []
    assert cli.watch_lines({"paused": False, "status": "1", "interrupted": None}, seen) == [
        "      .. the machine goes on (status '1')."]
    assert cli.watch_lines({"paused": False, "no_reply_s": 7.4}, seen) == [
        "      .. no answer from the machine for 7 s; still waiting. Ctrl+C stops."]
    assert cli.watch_lines({"paused": False, "no_reply_s": 9.4}, seen) == []
    key_only = cli.watch_lines({"paused": True, "status": "3", "resume_ok": False}, {})
    assert key_only == ["      .. the machine is paused (status '3'). Press its Pause key to "
                        "go on, or press Ctrl+C to stop."]


def test_a_cli_step_has_no_time_limit_and_watches_the_service(monkeypatch, capsys):
    monkeypatch.setattr(cli, "WATCH_EVERY", 0.05)
    asked: list = []

    class Sess:
        async def step(self, index, timeout=400):
            asked.append(timeout)
            await asyncio.sleep(0.6)
            return {"ok": True, "status": "0", "seconds": 0.6}

    class Client:
        n = 0

        async def ping_stop(self):
            self.n += 1
            return {"paused": 2 <= self.n <= 5, "status": "3" if self.n <= 5 else "1",
                    "resume_ok": True}
    res = asyncio.run(cli.run_step(Client(), Sess(), 3))
    out = capsys.readouterr().out
    assert res["ok"] and asked == [None]
    assert "the machine is paused (status '3')" in out and "the machine goes on (status '1')" in out


def test_a_cli_step_gives_up_after_three_unanswered_pings(monkeypatch):
    monkeypatch.setattr(cli, "WATCH_EVERY", 0.02)
    cancelled = []

    class Sess:
        async def step(self, index, timeout=400):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    class Client:
        async def ping_stop(self):
            return None
    with pytest.raises(client_mod.DaemonError, match="3 pings in a row"):
        asyncio.run(cli.run_step(Client(), Sess(), 0))
    assert cancelled == [True]
    assert cli.STOP_CHECK_PAUSED == 15 and not hasattr(cli, "STEP_TIMEOUT")


def test_opencutter_resume_and_pause_from_a_terminal(harness, capsys, monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0", move_s=0.8)
        refused = await cli.command(h.client(), "resume")
        async with h.client().session() as s:
            opened, gate = await _open_job(h, s)
            cut = asyncio.ensure_future(s.step(gate, 60))
            await _until(lambda: _moving(f))
            paused = await cli.command(h.client(), "pause")
            await asyncio.sleep(0.3)
            await cli.show_status(h.client())
            resumed = await cli.command(h.client(), "resume")
            res = await cut
            await s.end()
        return refused, paused, resumed, res
    refused, paused, resumed, res = harness.run(scenario)
    out, err = capsys.readouterr()
    assert refused is False and "Not sent: no job is running" in err
    assert paused is True and "Pause sent (ESC 07); the machine now reports 3 = Paused." in out
    assert "Paused (the open job waits for the machine)" in out
    assert resumed is True and ("Resume sent (ESC 08); the machine now reports 1 = Busy." in out
                                or "Resume sent (ESC 08); the machine now reports 0 = Ready."
                                in out)
    assert res["ok"] is True


def test_the_cli_parser_has_resume_and_pause(capsys):
    help_text = cli._parser().format_help()
    assert "resume" in help_text and "pause" in help_text and "CAMEO 4 only" in help_text
    assert cli._pause_machines() == ["CAMEO 4"]


# --- replies still due, and nothing but ESC 05 after a stop ---------------------------------------
def test_a_late_ready_reply_never_ends_the_wait_for_the_next_batch(log, fast):
    """Every reply comes 16 s late (scaled): queries are asked again while earlier ones are
    still due, so a '0' read for one batch can arrive while the wait for the next one runs. It
    must not end that wait while the machine is still busy with the next batch."""
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        loop = asyncio.get_running_loop()
        m.status = "1"
        m.latency = [1.6] * 400
        loop.call_later(1.0, setattr, m, "status", "0")
        first = await asyncio.wait_for(daemon.wait_ready(lk, asyncio.Lock(), sess, 600), 60)
        t1 = loop.time()
        m.status = "1"                           # the next batch: busy for 6 s
        loop.call_later(6.0, setattr, m, "status", "0")
        second = await asyncio.wait_for(daemon.wait_ready(lk, asyncio.Lock(), sess, 600), 60)
        return first, second, loop.time() - t1, lk
    first, second, took, lk = asyncio.run(main())
    assert first == second == "0" and took >= 6.0, took
    assert lk.replies_pending() == 0
    assert any(x.startswith("job: a ready reply came while ") for x in log)


def test_no_late_reply_pattern_ends_the_wait_for_the_next_batch(log, monkeypatch):
    """Patterns of late replies (at once; 10 s late, overdue once; 18 s late, overdue twice: the
    owed count resyncs) around the end of one batch's wait, scaled by 0.05; the machine answers
    in order. It then runs the next batch for 1 s: no reply read for the first batch may end
    that wait earlier."""
    monkeypatch.setattr(link, "STATUS_OVERDUE", 0.35)
    monkeypatch.setattr(link, "REPLY_TIMEOUT", 0.15)
    monkeypatch.setattr(link, "REPLY_LOST_AFTER", 1.5)
    monkeypatch.setattr(daemon, "BUSY_POLL", 0.02)
    monkeypatch.setattr(daemon, "FIRST_POLL", 0.0)
    values = {"n": 0.01, "L1": 0.5, "L2": 0.9}

    async def one(combo):
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        loop = asyncio.get_running_loop()
        m.status, m.fifo = "1", True
        m.latency = [0.01] + [values[c] for c in combo] * 3
        asks = 0

        def count(mm):
            nonlocal asks
            asks += 1
            if asks == 4:
                mm.status = "0"
        m.hooks[ESC05] = count
        await asyncio.wait_for(daemon.wait_ready(lk, asyncio.Lock(), sess, 600), 30)
        m.hooks.clear()
        m.status = "1"                           # the next batch runs for 1 s
        t1 = loop.time()
        loop.call_later(1.0, setattr, m, "status", "0")
        await asyncio.wait_for(daemon.wait_ready(lk, asyncio.Lock(), sess, 600), 30)
        return loop.time() - t1

    async def main():
        combos = list(itertools.product(values, repeat=4))
        return combos, await asyncio.gather(*(one(c) for c in combos), return_exceptions=True)
    combos, results = asyncio.run(main())
    early = [(" ".join(c), r) for c, r in zip(combos, results)
             if not isinstance(r, float) or r < 1.0]
    assert not early, early[:5]


def test_drain_replies_waits_for_the_replies_due_and_forgets_lost_ones(fast):
    async def main():
        m, lk = await make_link()
        loop = asyncio.get_running_loop()
        m.status, m.latency = "3", [0.5]
        with pytest.raises(TimeoutError):
            await lk._request(ESC05, timeout=0.1)
        m.status = "1"
        late = lk.replies_pending()
        t0 = loop.time()
        came = await lk.drain_replies()
        waited = loop.time() - t0
        state = (lk.replies_pending(), lk.last_reply_text, lk._owed)
        m.latency = [None]                       # this reply is lost
        with pytest.raises(TimeoutError):
            await lk._request(ESC05, timeout=0.1)
        t0 = loop.time()
        lost = await lk.drain_replies()
        gave_up = loop.time() - t0
        stop = asyncio.Event()
        stop.set()
        m.latency = [0.5]
        with pytest.raises(TimeoutError):
            await lk._request(ESC05, timeout=0.1)
        with pytest.raises(link.QueryAbandoned):
            await lk.drain_replies(stop)
        return late, came, waited, state, lost, gave_up, m
    late, came, waited, state, lost, gave_up, m = asyncio.run(main())
    assert late == 1 and came is True and 0.3 < waited < 1.0
    assert state == (0, "3", 0)                  # the late reply, nothing owed afterwards
    assert lost is False and 2.8 < gave_up < 3.6
    assert m.writes == [ESC05, ESC05, ESC05]     # a drain writes nothing


def test_a_patient_query_is_never_written_once_the_stop_was_asked_for():
    async def main():
        m, lk = await make_link()
        stop = asyncio.Event()
        stop.set()
        for code in (0x05, 0x15):
            with pytest.raises(link.QueryAbandoned):
                await lk.patient_esc(code, stop=stop)
        return m, lk
    m, lk = asyncio.run(main())
    assert m.writes == [] and lk.replies_pending() == 0 and lk._owed == 0


def test_no_tool_check_goes_out_after_a_stop_that_came_while_the_lock_was_busy(log):
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        lock = asyncio.Lock()
        await lock.acquire()                     # e.g. a pause op or the stop's read-back
        check = asyncio.ensure_future(daemon._tool_check(lk, lock, sess))
        await asyncio.sleep(0.05)
        await daemon._stop_session(lk, sess, force=True)
        await asyncio.sleep(0.5)                 # the stop's quiet window is over
        lock.release()
        with pytest.raises(daemon.MotionAborted) as e:
            await check
        return m, str(e.value)
    m, text = asyncio.run(main())
    assert m.writes == [ESC09, ESC09] and text == daemon.STOPPED_IN_STEP


def test_no_unit_check_goes_out_after_a_stop_that_came_while_the_lock_was_busy(log):
    async def main():
        sess = make_session()
        m, lk = await make_link(sess)
        lock = asyncio.Lock()
        await lock.acquire()
        units = asyncio.ensure_future(daemon._units_step(lk, lock, sess))
        await asyncio.sleep(0.05)
        await daemon._stop_session(lk, sess, force=True)
        await asyncio.sleep(0.5)
        lock.release()
        with pytest.raises(daemon.StepRefused) as e:
            await units
        return m, e.value
    m, refused = asyncio.run(main())
    assert m.writes == [ESC09, ESC09] and U not in m.writes and refused.stop is True


def test_no_resume_once_a_stop_went_out_while_it_waited_for_a_quiet_window():
    async def main():
        m, lk = await make_link()
        loop = asyncio.get_running_loop()
        lk._quiet_until = loop.time() + 0.2      # the quiet window of an earlier Pause
        resume = asyncio.ensure_future(lk.resume())
        await asyncio.sleep(0.05)
        await lk.emergency_stop()                # ends before that window
        with pytest.raises(UnsafeCommand):
            await resume
        return m, lk
    m, lk = asyncio.run(main())
    assert m.writes == [ESC09, ESC09] and lk.resume_writes == 0


def test_resume_is_refused_when_its_status_read_may_be_an_earlier_reply(log):
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        lk.machine = "CAMEO 4"
        sess.link = lk
        m.status = "3"
        m.indicate("3")                          # the session waits on the paused machine
        m.latency = [0.6, 0.6, 0.01]
        for _ in range(2):                       # two replies late: the count resyncs
            with pytest.raises(TimeoutError):
                await lk._request(ESC05, timeout=0.1)
        state = {"motion": sess, "c": lk, "support": "tested"}
        reply = await daemon._command_op("resume", lk, asyncio.Lock(), state)
        return m, reply, sess
    m, reply, sess = asyncio.run(main())
    assert reply["ok"] is False and reply["sent"] is False
    assert "could not be read for sure" in reply["error"]
    assert ESC08 not in m.writes and sess.paused == "3"


def test_a_link_lost_while_paused_ends_the_job_and_owes_the_stop(harness, monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)

    async def scenario(h):
        h.fake_cls.accept_any = True
        first = await h.fresh(tools=" 7, 0", move_s=0.8, fail_when_disconnected=True)
        async with h.client().session() as s:
            opened, gate = await _open_job(h, s)
            cut = asyncio.ensure_future(s.step(gate, 60))
            await _until(lambda: _moving(first))
            first.press_pause()
            await _until(lambda: _paused(h))
            first.is_connected = False             # the link drops while it is paused
            first.cb_disc(first)
            res = await asyncio.wait_for(cut, 15)

        async def back():
            try:
                ping = await h.ping()
            except OSError:
                return None
            return ping if ping["connected"] and h.fake is not first else None
        await _until(back, timeout=20)
        await asyncio.sleep(0.5)
        return res, first, h.fake
    res, first, new = harness.run(scenario)
    assert res["ok"] is False and res["error"] == daemon.LINK_LOST and res["stopped"] is True
    assert new.writes[:5] == [ESC05, FG, ESC05, ESC09, ESC09]   # the owed stop, once
    assert stops(new.writes) == 1
    assert any("link back: sending the stop owed from the lost session: sent" in x
               for x in harness.log)


def test_a_pause_while_the_tool_check_is_out_holds_the_lowering_write(log, fast):
    """The pause comes while ESC 15 is out, right before a write that can lower the tool: the
    write waits (checked again under the query lock) until the machine goes on, and the tools
    are read again first."""
    down = b"D100,100\x03"

    async def main():
        sess = make_session()
        sess.running = sess.batch_open = True
        m, lk = await make_link(sess)
        loop = asyncio.get_running_loop()
        went_on = []

        def on_tools(mm):
            if not sess.pauses:
                mm.indicate("3")                 # Pause pressed while ESC 15 is out
                loop.call_later(0.6, lambda: (went_on.append(loop.time()), mm.indicate("1")))
        m.hooks[ESC15] = on_tools
        await daemon._write_chunk(lk, asyncio.Lock(), sess, down, "0")
        return m, went_on
    m, went_on = asyncio.run(main())
    first = m.writes.index(ESC15)
    written = next(t for t, d in m.log if d == down)
    assert went_on and written > went_on[0]
    between = m.writes[first + 1:m.writes.index(down)]
    assert set(between) <= {ESC05, ESC15} and between[-1] == ESC15   # tools read again
    assert ESC09 not in m.writes


def test_a_pause_while_the_tool_check_waits_for_the_lock_writes_no_esc_15(log, fast):
    """The pause comes while the tool check waits for the query lock: ESC 15 is not written
    to the paused machine (nothing but ESC 05 until it goes on)."""
    down = b"D100,100\x03"

    async def main():
        sess = make_session()
        sess.running = sess.batch_open = True
        m, lk = await make_link(sess)
        lock = asyncio.Lock()
        await lock.acquire()                     # e.g. the user's pause op holds it
        chunk = asyncio.ensure_future(daemon._write_chunk(lk, lock, sess, down, "0"))
        await asyncio.sleep(0.05)
        m.indicate("3")
        lock.release()
        await asyncio.sleep(0.6)
        paused_writes = list(m.writes)
        m.indicate("1")
        await asyncio.wait_for(chunk, 5)
        return m, paused_writes
    m, paused_writes = asyncio.run(main())
    assert paused_writes and set(paused_writes) == {ESC05}
    assert m.writes[-2:] == [ESC15, down] and ESC09 not in m.writes


def test_a_pause_while_u_waits_for_the_lock_writes_no_u(log, fast):
    async def main():
        sess = make_session()
        m, lk = await make_link(sess)
        lock = asyncio.Lock()
        await lock.acquire()
        units = asyncio.ensure_future(daemon._units_step(lk, lock, sess))
        await asyncio.sleep(0.05)
        m.indicate("3")
        lock.release()
        await asyncio.sleep(0.6)
        paused_writes = list(m.writes)
        m.indicate("1")
        res = await asyncio.wait_for(units, 5)
        return m, paused_writes, res
    m, paused_writes, res = asyncio.run(main())
    assert paused_writes and set(paused_writes) == {ESC05}
    assert res["ok"] is True and m.writes[-1] == U and m.writes.count(U) == 1


def test_ready_long_after_a_resume_sent_during_the_pause_is_ambiguous(log):
    sess = make_session()
    sess.batch_open = True
    daemon._note_status(sess, "3")
    sess.paused_at = daemon._now() - daemon.RESUME_WINDOW - 5
    sess.resume_sent_at = daemon._now() - daemon.RESUME_WINDOW - 1   # after the pause began
    daemon._note_status(sess, "0")
    assert sess.paused is None and sess.interrupted == daemon.LEFT_PAUSE_MARK


@pytest.mark.parametrize("when", ["between-steps", "at-the-pre-step-read"])
def test_a_cancel_seen_before_a_step_refuses_it_with_one_pair(log, when):
    async def main():
        sess = make_session()
        m, lk = await make_link(sess)
        if when == "between-steps":
            m.indicate("4")                      # a cancel on the panel after the last step
        else:
            m.hooks[ESC05] = lambda mm: mm.indicate("4") if not sess.interrupted else None
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
        return m, res
    m, res = asyncio.run(main())
    assert res["ok"] is False and res["stopped"] is True and MOVE not in m.writes
    assert m.writes.count(ESC09) == 2
    assert res["error"].startswith("The machine cancelled the job (status '4' = Cancelling)")


def test_no_resume_or_pause_on_the_cameo_4s_untested_firmware(harness):
    harness.simulate(CAMEO4, "V1.20")

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 7, 0")
        async with h.client().session() as s:
            opened = await s.open_job(spec_dict())
            assert opened["ok"], opened
            assert (await s.step(0, 60))["ok"]
            running = await _ping(h)
            paused_reply = await h.client().pause()
            stubs_indicate(f, "3")
            paused = await _ping(h)
            resumed_reply = await h.client().resume()
            await h.client().stop()
        return running, paused_reply, paused, resumed_reply, f
    running, paused_reply, paused, resumed_reply, f = harness.run(scenario)
    assert running["pause_ok"] is False and paused["paused"] is True
    assert paused["resume_ok"] is False
    for reply, kind in ((paused_reply, "pause"), (resumed_reply, "resume")):
        assert reply["ok"] is False and reply["sent"] is False
        assert reply["error"] == f"the CAMEO 4 has not been tested with {kind}: press its Pause key"
    assert ESC07 not in f.writes and ESC08 not in f.writes


@pytest.mark.parametrize("digit", ["5", "6"], ids=["rejected", "not-a-status"])
@pytest.mark.parametrize("kind", ["resume", "pause"])
def test_resume_and_pause_need_the_right_status_on_the_fresh_read(log, kind, digit):
    """The fresh ESC 05 read gives Rejected, or a digit that is no CAMEO 4 status (it does not
    change the last status): Resume needs Paused, Pause needs Ready or Busy, so neither is
    sent."""
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        lk.machine = "CAMEO 4"
        sess.link = lk
        m.status = "3" if kind == "resume" else "1"
        m.indicate(m.status)
        m.status = digit                         # what the fresh read gets
        state = {"motion": sess, "c": lk, "support": "tested"}
        reply = await daemon._command_op(kind, lk, asyncio.Lock(), state)
        return m, reply
    m, reply = asyncio.run(main())
    assert reply["ok"] is False and reply["sent"] is False and reply["status_before"] == digit
    words = "not paused" if kind == "resume" else "neither ready nor busy"
    assert f"the machine is {words} (status '{digit}'" in reply["error"]
    assert ESC07 not in m.writes and ESC08 not in m.writes


def test_the_stop_op_never_waits_for_a_query_that_is_not_answered(log, fast):
    """A session query is out and never answered (it holds the query lock): the stop op's pair
    goes out at once, and the wait ends."""
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        lk.machine = "CAMEO 4"
        sess.link = lk
        m.status = "1"
        m.latency = [None] * 100
        lock = asyncio.Lock()
        wait = asyncio.ensure_future(daemon.wait_ready(lk, lock, sess, 600))
        await asyncio.sleep(1.0)                 # the query is out, the lock held
        assert lock.locked()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        state = {"motion": sess, "c": lk, "support": "tested", "stop_owed": set(),
                 "owed_stops_sent": 0}
        stop = asyncio.ensure_future(daemon._stop_op({"op": "stop"}, lk, lock, state))
        while m.writes.count(ESC09) < 2 and loop.time() - t0 < 3:
            await asyncio.sleep(0.01)
        took = loop.time() - t0
        reply = await asyncio.wait_for(stop, 5)
        with pytest.raises(daemon.MotionAborted) as e:
            await asyncio.wait_for(wait, 2)
        return took, reply, str(e.value)
    took, reply, text = asyncio.run(main())
    assert took < 0.5 and reply == {"ok": True, "errors": []}
    assert text == daemon.STOPPED_IN_STEP


def test_a_busy_reply_after_a_long_silence_adds_little_to_the_ready_timeout(log, fast):
    """About 4.6 s (scaled) without a reply, then Busy replies: the silence is not Busy time
    (one reply adds at most BUSY_CREDIT_MAX), so a 3 s READY_TIMEOUT is not used up."""
    async def main():
        sess = make_session()
        sess.running = True
        m, lk = await make_link(sess)
        m.status = "1"
        m.latency = [0.01] + [None] * 6
        asyncio.get_running_loop().call_later(6.2, setattr, m, "status", "0")
        return await asyncio.wait_for(daemon.wait_ready(lk, asyncio.Lock(), sess, 3.0), 30), m
    status, m = asyncio.run(main())
    assert status == "0" and ESC09 not in m.writes


@pytest.mark.parametrize("digit", ["5", "6"], ids=["rejected", "not-a-status"])
def test_a_paused_machine_that_reports_an_unknown_status_ends_the_job(log, fast, digit):
    async def main():
        sess = make_session()
        sess.running = sess.batch_open = True
        m, lk = await make_link(sess)
        m.status = "3"
        m.indicate("3")
        asyncio.get_running_loop().call_later(0.5, setattr, m, "status", digit)
        with pytest.raises(daemon.MotionAborted) as e:
            await asyncio.wait_for(daemon.wait_ready(lk, asyncio.Lock(), sess, 60), 10)
        return m, str(e.value), sess
    m, text, sess = asyncio.run(main())
    assert sess.paused is None and f"'{digit}'" in text
    assert any(f"while paused the machine reported '{digit}'" in x for x in log)


def test_after_a_pause_between_batches_the_next_batch_waits_for_ready(log, fast):
    """The pause came between batches and ends with Busy: the next batch's first write waits
    for a fresh ready, so the machine never holds more than one batch."""
    async def main():
        sess = make_session()
        m, lk = await make_link(sess)
        sess.running = True                      # the step runs; its last batch read ready
        daemon._note_status(sess, "3")
        m.status = "3"
        t0 = asyncio.get_running_loop().time()
        m.later(0.4, "1")                        # it goes on, busy for a while
        m.later(1.4, "0")
        await daemon._write_chunk(lk, asyncio.Lock(), sess, MOVE, "0")
        return m, t0
    m, t0 = asyncio.run(main())
    written = next(t for t, d in m.log if d == MOVE)
    assert written >= t0 + 1.4 and ESC09 not in m.writes
    assert set(m.writes[:-1]) == {ESC05}


def test_a_paused_machine_is_read_at_the_paused_cadence_not_the_busy_one(log, monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 1.0)
    monkeypatch.setattr(daemon, "BUSY_POLL", 0.05)

    async def main():
        sess = make_session()
        sess.running = sess.batch_open = True
        m, lk = await make_link(sess)
        m.status = "3"
        m.indicate("3")
        m.later(3.5, "1")
        m.later(3.6, "0")
        await asyncio.wait_for(daemon._wait_while_paused(lk, asyncio.Lock(), sess), 10)
        return m
    m = asyncio.run(main())
    assert 2 <= m.writes.count(ESC05) <= 4 and set(m.writes) == {ESC05}


def test_no_query_in_the_quiet_window_after_the_users_resume(log, fast):
    """Resume while the session waits between batches: the machine goes on with nothing left
    ('0' at once) and the session wants to read ESC 05 right away; that read waits for the
    Resume's quiet window (link.RESUME_QUIET)."""
    async def main():
        sess = make_session()
        sess.running = True                      # between batches: nothing of ours is left
        m, lk = await make_link(sess)
        lk.machine = "CAMEO 4"
        sess.link = lk
        m.status = "3"
        m.indicate("3")
        lock = asyncio.Lock()
        wait = asyncio.ensure_future(daemon.wait_ready(lk, lock, sess, 600))
        await asyncio.sleep(0.6)

        def went_on(mm):
            mm.status = "0"
            asyncio.get_running_loop().call_soon(mm.indicate, "0")
        m.hooks[ESC08] = went_on
        state = {"motion": sess, "c": lk, "support": "tested"}
        reply = await daemon._command_op("resume", lk, lock, state)
        status = await asyncio.wait_for(wait, 5)
        return m, reply, status
    m, reply, status = asyncio.run(main())
    assert reply["ok"] is True and status == "0" and m.writes.count(ESC08) == 1
    sent = next(t for t, d in m.log if d == ESC08)
    after = [t for t, d in m.log if d == ESC05 and t > sent]
    assert after and after[0] - sent >= link.RESUME_QUIET - 0.01
