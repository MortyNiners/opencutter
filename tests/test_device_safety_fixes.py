# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Regression tests for the stop, pause and link-loss paths of the daemon, the link and the CLI.

Two kinds of tests, neither reaches Bluetooth or D-Bus (stubs.install() runs before the driver
is imported):
  * a real link.SafeLink over a scripted transport (FakeTransport below), for the step
    machinery of the daemon (_run_step, wait_ready, _stop_session) and the link's own limits;
  * the real daemon in the tests' DaemonHarness (private socket and lock in a temp directory,
    the simulated machine of tests/stubs.py), for what goes over the wire.
What the machine is sent stays the same for every normal job (tests/test_profile_
characterization.py and tests/test_device_jobs.py pin those bytes): these tests look only at
the stop (ESC 09 twice), ESC 05 reads and refusals.
"""
import asyncio
import json
import re
import sys
import time
import types

import pytest

import stubs
from stubs import ADDRESS, ESC05, ESC09, ESC15, FG, harness, stops  # noqa: F401

stubs.install()
from opencutter import __main__ as cli  # noqa: E402
from opencutter.device import ble, client as client_mod, daemon, link, protocol  # noqa: E402
from opencutter.device.link import SafeLink  # noqa: E402
from opencutter.device.protocol import CHAR_STATUS, REASON_NO_BLUETOOTH  # noqa: E402
from opencutter.device.transport import BluetoothOff, DeviceRef, PlotterUnreachable  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings  # noqa: E402
from opencutter.machines import CAMEO4  # noqa: E402

U = b"U\x03"
MOVE = b"M100,100\x03"
TRAILER = b"M0,0\x03"
PASSED = [ESC05, FG, ESC05]          # a new link to the tested machine
SECOND = "00:00:00:00:00:02"
VINYL = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                     line_segment_overcut=False, overcut=(1, 1), blade_depth=1, passes=1)
SQUARE = [(60.0, 60.0), (85.4, 60.0), (85.4, 85.4), (60.0, 85.4), (60.0, 60.0)]


def spec_dict(preview: bool = True) -> dict:
    job = JobSpec(mat="12x12", media=(50.0, 50.0, 100.0, 100.0),
                  groups=[CutGroup(settings=VINYL, paths=[SQUARE])], preview_outline=preview)
    return json.loads(json.dumps(job.to_dict()))


# --- a SafeLink over a scripted transport -----------------------------------------------------
class FakeTransport:
    """A dumb pipe whose replies a test scripts. `status`: the ESC 05 replies in order (the last
    one repeats; None: no reply). `hooks`: data -> callable(transport), run when that write goes
    out. `hang`: writes that never get their Write Response. With `serialize` (the default) a
    write waits for the one in flight, as BlueZ runs one WriteValue per characteristic."""
    has_status_channel = True

    def __init__(self, serialize: bool = True):
        self.ref = DeviceRef("ble", ADDRESS, "CAMEO 4-unit")
        self.disconnected = asyncio.Event()
        self.up = True
        self.writes: list[bytes] = []
        self.on_reply = self.on_status = None
        self.status: list[str | None] = ["0"]
        self.u_reply: str | None = "6100,6100"
        self.hooks: dict = {}
        self.hang: set[bytes] = set()
        self.serialize = serialize
        self._bus = asyncio.Lock()

    def is_up(self) -> bool:
        return self.up

    async def connect(self, timeout):
        pass

    async def subscribe(self, on_reply, on_status):
        self.on_reply, self.on_status = on_reply, on_status

    async def unsubscribe(self):
        pass

    def indicate(self, status: str) -> None:
        self.on_status(status.encode() + b"\x03")

    def drop(self) -> None:
        self.up = False
        self.disconnected.set()

    async def write(self, data: bytes) -> None:
        if self.serialize:
            async with self._bus:
                await self._write(data)
        else:
            await self._write(data)

    async def _write(self, data: bytes) -> None:
        if not self.up:
            raise RuntimeError("Not connected")
        self.writes.append(data)
        hook = self.hooks.get(data)
        if hook is not None:
            hook(self)
        if data in self.hang:
            await asyncio.Event().wait()
        loop = asyncio.get_running_loop()
        if data == ESC05:
            reply = self.status.pop(0) if len(self.status) > 1 else self.status[0]
            if reply is not None:
                loop.call_soon(self.on_reply, reply.encode() + b"\x03")
        elif data == ESC15:
            loop.call_soon(self.on_reply, b" 2, 0\x03")
        elif data == U and self.u_reply is not None:
            loop.call_soon(self.on_reply, self.u_reply.encode() + b"\x03")

    async def max_write(self):
        return 244

    async def release(self):
        pass

    async def disconnect(self):
        self.drop()


async def make_link(serialize: bool = True, sess=None) -> tuple[FakeTransport, SafeLink]:
    """A link that passed the gate with the CAMEO 4's profile; status indications reach `sess`
    as they do in the service (daemon._note_status)."""
    t = FakeTransport(serialize)
    lk = SafeLink(t, on_status=lambda s: daemon._note_status(sess, s))
    await lk.open()
    lk.fg_reply = stubs.TESTED_FG
    lk.profile = CAMEO4
    await lk.refresh_write_limit()
    return t, lk


def make_session(wrote: bool = False, name: str = "job") -> daemon.MotionSession:
    plan = types.SimpleNamespace(validator=lambda w: None, required_tools={1: 2}, steps=[],
                                 summary={})
    sess = daemon.MotionSession(name, plan, {}, profile=CAMEO4)
    sess.wrote = sess.motion_possible = wrote
    return sess


def write_step(title: str, writes=(MOVE,)):
    return types.SimpleNamespace(kind="write", writes=list(writes), title=title)


def units_step():
    return types.SimpleNamespace(kind="units", writes=[U], title="Check units (no motion)")


@pytest.fixture
def log(monkeypatch) -> list[str]:
    lines: list[str] = []
    monkeypatch.setattr(daemon, "_log", lines.append)
    return lines


def never_says_press_pause(text: str) -> None:
    """Every 'Pause' (the key, not the state 'Paused') in a text is in 'Do not press Pause':
    never advice to press it."""
    assert len(re.findall(r"\bPause\b", text)) == text.count("Do not press Pause"), text


# --- (1) a stop request is honoured by every wait ---------------------------------------------
def test_a_stop_while_the_last_poll_is_out_never_reports_ok(log):
    async def main():
        sess = make_session()
        t, lk = await make_link(sess=sess)
        t.status = ["0", "1", "0"]                 # pre-step '0', then '1', then '0'
        polls = []

        def on_esc05(_t):
            polls.append(1)
            if len(polls) == 3:                    # the stop op arrives while this poll is out
                asyncio.ensure_future(daemon._stop_session(lk, sess, force=True))
        t.hooks[ESC05] = on_esc05
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
        await asyncio.sleep(0.5)
        return t, res
    t, res = asyncio.run(main())
    assert res["ok"] is False and res["stopped"] is True and res["stop_errors"] == []
    assert res["error"] == daemon.STOPPED_IN_STEP
    assert t.writes.count(ESC09) == 2               # only the stop op's pair


def test_a_stop_during_the_units_step_refuses_it(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.hooks[U] = lambda _t: asyncio.ensure_future(daemon._stop_session(lk, sess, force=True))
        res = await daemon._run_step(lk, asyncio.Lock(), sess, units_step())
        await asyncio.sleep(0.5)
        return t, res
    t, res = asyncio.run(main())
    assert res["ok"] is False and "came during the unit check" in res["error"]
    assert "send the job again" in res["error"]
    assert t.writes.count(ESC09) == 2               # the stop op's pair, nothing more
    assert [w for w in t.writes if w not in (ESC05, ESC09)] == [U]


def test_the_pre_step_wait_ends_at_a_stop(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.status = ["1"]                           # still Busy: the pre-step wait would last 10 s
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, lambda: asyncio.ensure_future(
            daemon._stop_session(lk, sess, force=True)))
        t0 = loop.time()
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
        return t, res, loop.time() - t0
    t, res, took = asyncio.run(main())
    assert took < 3 and res["ok"] is False and MOVE not in t.writes
    assert "A stop request" in res["error"] and t.writes.count(ESC09) == 2


def test_a_stop_between_steps_names_the_stop_request(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        await daemon._stop_session(lk, sess, force=True)
        n = len(t.writes)
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
        return t, n, res
    t, n, res = asyncio.run(main())
    assert res["ok"] is False and t.writes[n:] == []          # no second pair, no query
    assert "`opencutter stop`" in res["error"] and "nothing more was sent" in res["error"]
    assert any("'Cut' refused before its first write" in x for x in log)


# --- (2) a Paused or Cancelling status since the first write ends the job ---------------------
@pytest.mark.parametrize("digit, words", [("3", "paused"), ("4", "cancelled")])
def test_a_pause_indication_then_ready_aborts_the_step_with_one_stop(log, digit, words):
    async def main():
        sess = make_session()
        t, lk = await make_link(sess=sess)
        t.status = ["0", "0"]

        def on_esc05(tr):
            if sess.running and sess.interrupted is None:
                tr.indicate(digit)                 # the panel's Pause, or a cancel on it
        t.hooks[ESC05] = on_esc05
        cut = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
        n = len(t.writes)
        trailer = await daemon._run_step(lk, asyncio.Lock(), sess,
                                         write_step("Return to Origin", [TRAILER]))
        return t, cut, trailer, n
    t, cut, trailer, n = asyncio.run(main())
    assert cut["ok"] is False and cut["stopped"] is True and cut["stop_errors"] == []
    assert f"The machine was {words}" in cut["error"] or f"machine {words}" in cut["error"]
    assert "cannot resume" in cut["error"] and "Unload" in cut["error"]
    never_says_press_pause(cut["error"])
    assert t.writes.count(ESC09) == 2
    assert trailer["ok"] is False and t.writes[n:] == []      # refused before any query
    assert TRAILER not in t.writes


def test_a_cancelling_after_our_own_stop_changes_nothing(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        await daemon._stop_session(lk, sess, force=True)
        t.indicate("4")                            # what our own stop makes the machine show
        return sess
    assert asyncio.run(main()).interrupted is None


def test_rejected_and_busy_are_not_latched(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        for digit in ("1", "5", "0"):
            t.indicate(digit)
        return sess
    assert asyncio.run(main()).interrupted is None


def test_a_pause_at_the_gate_refuses_the_cut_and_sends_the_stop(log):
    async def main():
        sess = make_session(wrote=True)            # header and preview went out
        t, lk = await make_link(sess=sess)
        t.indicate("3")                            # Pause pressed while the app waits at the gate
        t.indicate("0")
        n = len(t.writes)
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
        return t, n, res
    t, n, res = asyncio.run(main())
    assert t.writes[n:] == [ESC09, ESC09]          # the stop, before any query
    assert res["ok"] is False and res["stopped"] is True
    assert "before this step; nothing of this step was sent" in res["error"]
    never_says_press_pause(res["error"])
    assert any("'Cut' refused before its first write" in x for x in log)


def test_a_paused_reply_before_a_later_step_sends_the_stop(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.status = ["3"]                           # read by the pre-step ESC 05, no indication
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
        return t, res
    t, res = asyncio.run(main())
    assert res["ok"] is False and res["stopped"] is True and MOVE not in t.writes
    assert t.writes.count(ESC09) == 2
    assert res["error"].startswith("The machine was paused (status '3' = Paused) before this "
                                   "step; nothing of this step was sent.")


def test_a_paused_reply_before_the_header_sends_no_stop(log):
    async def main():
        sess = make_session(wrote=False)
        t, lk = await make_link(sess=sess)
        t.status = ["3"]
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Header"))
        return t, res
    t, res = asyncio.run(main())
    assert res["ok"] is False and "stopped" not in res and ESC09 not in t.writes
    assert res["error"] == protocol.not_ready_text("3", CAMEO4)
    assert "Nothing was sent" in res["error"]


def test_a_u_timeout_after_a_pause_names_the_pause(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.u_reply = None                           # U is not answered (assumed while paused)
        t.indicate("3")
        res = await daemon._run_step(lk, asyncio.Lock(), sess, units_step())
        return t, res
    t, res = asyncio.run(main())
    assert res["ok"] is False and res["error"] != daemon.UNITS_UNCONFIRMED
    assert "The machine was paused" in res["error"] and res["stopped"] is True
    assert t.writes.count(ESC09) == 2 and U not in t.writes   # refused before U


def test_an_unload_during_a_step_has_the_plain_words(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.status = ["0", "2"]
        return await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
    res = asyncio.run(main())
    assert res["error"] == daemon.MEDIA_CHANGED_STEP and res["stopped"] is True


def test_another_status_during_a_step_keeps_its_words(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.status = ["0", "5"]
        return await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
    res = asyncio.run(main())
    assert "The machine rejected a command (status '5' = Rejected)" in res["error"]
    assert res["stopped"] is True


# --- (3) motion possible: the loss stop between writes ------------------------------------------
def test_a_loss_during_the_pre_step_polls_tries_the_stop(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.status = ["1"]
        seen = []

        def on_esc05(tr):
            seen.append(sess.running)
            if len(seen) == 2:                     # the link drops while the machine reads Busy
                tr.drop()
        t.hooks[ESC05] = on_esc05
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Return to Origin"))
        return seen, res
    seen, res = asyncio.run(main())
    assert seen == [False, False]
    assert res["ok"] is False and res["error"] == daemon.LINK_LOST
    assert res["stopped"] is True and len(res["stop_errors"]) == 2   # it could not get through


def test_motion_possible_ends_only_after_two_ready_reads(log):
    async def main():
        sess = make_session()
        t, lk = await make_link(sess=sess)
        lock = asyncio.Lock()
        assert (await daemon._run_step(lk, lock, sess, write_step("Header")))["ok"]
        after_step = sess.motion_possible          # the step's ready wait read '0' once
        await daemon._pre_step_status(lk, lock, sess)
        return after_step, sess.motion_possible
    assert asyncio.run(main()) == (True, False)


# --- (5) every write has a time bound -----------------------------------------------------------
def test_a_hung_query_write_cannot_block_the_ready_wait(log, monkeypatch):
    monkeypatch.setattr(link, "QUERY_WRITE_TIMEOUT", 0.2)

    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(serialize=False, sess=sess)
        sess.running = True
        t.hang.add(ESC05)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(daemon.MotionAborted, match="no longer answers ESC 05"):
            await asyncio.wait_for(daemon.wait_ready(lk, asyncio.Lock(), sess, 60), 5)
        return loop.time() - t0
    assert asyncio.run(main()) < 2


def test_a_timed_out_query_write_owes_its_reply():
    async def main():
        t, lk = await make_link(serialize=False)
        real = t._write
        late = []

        async def slow(data):
            if data == ESC05 and not late:
                # Acknowledged too late: cancelling the call does not cancel the write in the
                # Bluetooth stack, so it still goes out and is answered.
                async def later():
                    await asyncio.sleep(0.3)
                    await real(data)
                late.append(asyncio.ensure_future(later()))
                await asyncio.shield(late[0])
                return
            await real(data)
        t._write = slow
        with pytest.raises(TimeoutError, match="no Write Response"):
            await lk.esc_query(0x05)
        await asyncio.sleep(0.4)                   # the late reply came and was dropped
        return await lk.query("U")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(link, "QUERY_WRITE_TIMEOUT", 0.1)
        assert asyncio.run(main()) == "6100,6100"


def test_a_motion_write_without_response_ends_the_step_with_the_stop(log, monkeypatch):
    monkeypatch.setattr(link, "MOTION_WRITE_TIMEOUT", 0.2)

    async def main():
        sess = make_session()
        t, lk = await make_link(serialize=False, sess=sess)
        t.hang.add(MOVE)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        res = await asyncio.wait_for(
            daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut")), 5)
        return t, res, loop.time() - t0
    t, res, took = asyncio.run(main())
    assert took < 2 and res["ok"] is False and res["stopped"] is True
    assert "did not acknowledge a write within" in res["error"]
    assert t.writes.count(ESC09) == 2 and res["stop_errors"] == []


def test_a_stop_write_that_times_out_says_so(monkeypatch):
    monkeypatch.setattr(link, "STOP_WRITE_TIMEOUT", 0.05)

    async def main():
        t, lk = await make_link(serialize=False)
        t.hang.add(ESC09)
        return await lk.emergency_stop()
    errors = asyncio.run(main())
    assert errors == ["1b 09: TimeoutError: no Write Response within 0.05 s"] * 2


# --- (9) the log says how many pairs went out -----------------------------------------------------
def test_sent_joined_and_skipped_stops_are_logged_apart(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        first = asyncio.ensure_future(daemon._stop_session(lk, sess, force=True))   # stop op
        await asyncio.sleep(0)
        second = asyncio.ensure_future(daemon._stop_session(lk, sess))   # client gone meanwhile
        await asyncio.gather(first, second)
        await daemon._stop_session(lk, sess)                             # a step's abort path
        return t, lk
    t, lk = asyncio.run(main())
    assert t.writes.count(ESC09) == 2 and lk.stop_sequences == 1
    stop_lines = [x for x in log if "STOP" in x]
    assert stop_lines == ["job: STOP (ESC 09 x2) sent, pair 1 on this link",
                          "job: joined the STOP in flight (pair 1 on this link): sent",
                          "job: STOP already sent (pair 1 on this link); not sent again"]


# --- (7) the refusal texts per status -------------------------------------------------------------
def test_the_refusal_at_open_names_each_status():
    plan = types.SimpleNamespace(steps=[], required_tools={})
    text = {s: daemon.precheck(plan, {}, s, " 2, 0", CAMEO4) for s in "12345"}
    assert text["1"].startswith("The machine is busy (status '1' = Busy)")
    assert text["3"].startswith("The machine is paused (status '3' = Paused). Nothing was sent.")
    assert text["4"].startswith("The machine is still ending a cancel (status '4' = Cancelling)")
    assert text["5"].startswith("The machine rejected a command (status '5' = Rejected)")
    assert text["2"] == ("The machine is not ready (status '2' = Load Media). Load the Cutting "
                         "Mat on the machine right before the job.")
    assert all("Load the Cutting Mat on the machine right before the job." not in text[s]
               for s in "1345")
    for s in "12345":
        never_says_press_pause(text[s])
    assert "Do not press Pause" in text["3"] and "Do not press Pause" in text["4"]


def test_the_stop_texts_never_advise_pause():
    for name in ("job", "dry-run"):
        sess = make_session(wrote=True, name=name)
        for status in "1345":
            for before in (False, True):
                never_says_press_pause(daemon._status_stop_text(status, sess, before))
    assert "run the program again" in daemon._status_stop_text("3", make_session(name="dry-run"))


# --- the real daemon: the exit stop and the loss stop ---------------------------------------------
async def _open(h, program: str = "dry-run"):
    r, w, out = await h.raw([{"op": "motion", "program": program, "options": {}}])
    assert out[0]["ok"], out
    return r, w


async def _step(r, w, i: int, timeout: float = 30) -> dict:
    w.write((json.dumps({"op": "step", "index": i}) + "\n").encode())
    await w.drain()
    return json.loads(await asyncio.wait_for(r.readline(), timeout))


def indicate(f, status: str) -> None:
    f.status = status
    f.cbs[CHAR_STATUS](None, bytearray(status.encode() + b"\x03"))


def drop(f) -> None:
    f.is_connected = False
    f.cb_disc(f)


async def _until(predicate, timeout: float = 15.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.05)


def test_an_exit_during_the_pre_step_polls_sends_the_stop(harness):
    async def scenario(h):
        f = await h.fresh(move_s=0.1)
        r, w = await _open(h)
        for i in (0, 1, 2):
            assert (await _step(r, w, i))["ok"]
        f.status_override = "1"                    # the machine still reads Busy
        w.write((json.dumps({"op": "step", "index": 3}) + "\n").encode())
        await w.drain()
        await asyncio.sleep(1.5)                   # inside the pre-step wait (up to 10 s)
        await h.shutdown()                         # SIGTERM
        w.close()
        return stops(f.writes)
    assert harness.run(scenario) == 1


def test_an_exit_at_the_outline_gate_sends_the_stop(harness):
    async def scenario(h):
        f = await h.fresh(move_s=0.1)
        r, w = await _open(h)
        for i in (0, 1, 2, 3):
            assert (await _step(r, w, i))["ok"]
        await h.shutdown()
        w.close()
        return stops(f.writes)
    assert harness.run(scenario) == 1
    assert ("daemon stopping between steps of a session (the machine may be idle) -> STOP"
            in harness.log)


# --- (4) the stop owed after a link loss -----------------------------------------------------------
async def _ping(h, **flags) -> dict:
    return (await h.raw([{"op": "ping", **flags}]))[2][0]


async def _cut_and_drop(h, before_drop=None) -> dict:
    """Run a job to its cut and drop the link while the machine moves (after `before_drop()`);
    the cut's reply."""
    h.fake_cls.accept_any = True
    f = await h.fresh(tools=" 2, 4", move_s=3.0, fail_when_disconnected=True)
    async with h.client().session() as s:
        opened = await s.open_job(spec_dict())
        assert opened["ok"], opened
        gate = next(i for i, st in enumerate(opened["steps"]) if st["confirm"])
        for i in range(gate):
            assert (await s.step(i, 60))["ok"], i
        task = asyncio.ensure_future(s.step(gate, 60))
        await _until(lambda: _moving(f))
        if before_drop is not None:
            before_drop()
        drop(f)
        return await task


async def _moving(f) -> bool:
    return f.moving()


def test_a_stop_owed_after_a_loss_goes_out_once_on_the_same_machine(harness):
    async def scenario(h):
        first = h.fake
        res = await _cut_and_drop(h)
        assert res["ok"] is False and res["error"] == daemon.LINK_LOST
        assert res["stopped"] is True and res["stop_errors"]
        ping = await _until(lambda: _ping_if(h, lambda p: p["connected"]), timeout=20)
        new = h.fake
        assert new is not first and new.address == ADDRESS
        assert new.writes[:5] == [*PASSED, ESC09, ESC09]     # the gate, then the owed stop
        await asyncio.sleep(0.5)
        assert stops(new.writes) == 1                        # once
        stop = await _ping(h, stop=True)
        assert stop["stop_owed"] is False and stop["owed_stops_sent"] == 1
        return ping
    harness.run(scenario)
    assert any("link back: sending the stop owed from the lost session: sent" in x
               for x in harness.log)
    owed = [x for x in harness.log if x.startswith("a stop is owed to CAMEO 4-fake")]
    assert len(owed) == 1


async def _ping_if(h, predicate):
    try:
        reply = await h.ping()
    except OSError:
        return None
    return reply if predicate(reply) else None


def test_an_owed_stop_never_goes_to_another_machine(harness):
    def second_machine():
        harness.add_machine(SECOND, "CAMEO 4-second")
        harness.bluez.devices[0]["Paired"] = False     # the first machine is not found again

    async def scenario(h):
        res = await _cut_and_drop(h, second_machine)
        assert res["stop_errors"]
        await _until(lambda: _ping_if(h, lambda p: p["connected"]), timeout=20)
        other = h.machine(SECOND)[-1]
        await asyncio.sleep(0.5)
        assert other.writes == PASSED and ESC09 not in other.writes
        assert (await _ping(h, stop=True))["stop_owed"] is True
    harness.run(scenario)


def test_an_owed_machine_gets_a_longer_fg_wait(harness):
    def late_fg():
        harness.fake_cls.fg_delay = 3.6            # busy with queued moves: FG answers late

    async def scenario(h):
        first = h.fake
        res = await _cut_and_drop(h, late_fg)
        assert res["stop_errors"]
        await _until(lambda: _ping_if(h, lambda p: p["connected"]), timeout=25)
        links = [x for x in h.machine() if x is not first]
        assert len(links) == 1                     # the first new link was not dropped
        assert links[0].writes[:5] == [*PASSED, ESC09, ESC09]
    harness.run(scenario, timeout=60)


def test_a_failed_stop_op_is_owed_and_a_later_stop_pays_it(harness):
    async def scenario(h):
        f = await h.fresh()
        f.fail_esc09 = True
        failed = await h.client().stop()
        owed = (await _ping(h, stop=True))["stop_owed"]
        f.fail_esc09 = False
        ok = await h.client().stop()
        return failed, owed, ok, (await _ping(h, stop=True))["stop_owed"]
    failed, owed, ok, after = harness.run(scenario)
    assert failed["ok"] is False and owed is True
    assert ok["ok"] is True and after is False


def test_a_stop_asked_for_while_the_link_is_down_is_owed(harness):
    async def scenario(h):
        f = await h.fresh(move_s=0.2)
        r, w = await _open(h)
        assert (await _step(r, w, 0))["ok"]       # the header: motion was possible since
        h.connected_prop = True                    # keep it from reconnecting
        drop(f)
        await _until(lambda: _ping_if(h, lambda p: not p["connected"]))
        reply = await h.client().stop()
        stop = await _ping(h, stop=True)
        nxt = await _step(r, w, 1)
        w.close()
        return reply, stop, nxt
    reply, stop, nxt = harness.run(scenario)
    assert reply == {"ok": False, "error": daemon.NOT_CONNECTED}       # unchanged reply
    assert stop["stop_owed"] is True and stop["motion_stopped"] is True
    assert nxt["ok"] is False and "A stop request" in nxt["error"]


# --- (6) the stop op without a session, with its status check -------------------------------------
def test_the_stop_op_after_a_run_reads_the_status_back(harness):
    async def scenario(h):
        f = await h.fresh()
        plain = await h.client().stop()
        checked = await h.client().stop(check=True)
        f.status_override = "1"                    # a machine that keeps reporting Busy
        busy = await h.client().stop(check=True)
        f.status_override = None
        return plain, checked, busy, stops(f.writes)
    plain, checked, busy, n = harness.run(scenario)
    assert plain == {"ok": True, "errors": []}     # without check: the reply as always
    assert checked == {"ok": True, "errors": [], "status_after": "0", "still_busy": False}
    assert busy["ok"] is True and busy["still_busy"] is True and busy["status_after"] == "1"
    assert n == 3
    assert any(x.startswith("after the stop the machine reports 1 = Busy") for x in harness.log)


def test_the_stop_op_mid_step_reads_cancelling_then_ready(harness):
    async def scenario(h):
        f = await h.fresh(move_s=5.0)
        r, w = await _open(h)
        w.write((json.dumps({"op": "step", "index": 0}) + "\n").encode())
        await w.drain()
        await asyncio.sleep(1.0)
        checked = await h.client().stop(check=True)
        res = json.loads(await asyncio.wait_for(r.readline(), 10))
        w.close()
        return checked, res, stops(f.writes)
    checked, res, n = harness.run(scenario)
    assert checked["ok"] and checked["status_after"] == "0" and checked["still_busy"] is False
    assert res["ok"] is False and res["stopped"] and n == 1
    assert "[status change] 0 = Ready (ESC 05)" in harness.log    # the log shows the stop


# --- the ping's stop fields ------------------------------------------------------------------------
def test_the_stop_fields_are_asked_for(harness):
    async def scenario(h):
        f = await h.fresh()
        plain, machine, stop, both = (await h.ping(), await _ping(h, machine=True),
                                      await _ping(h, stop=True),
                                      await _ping(h, machine=True, stop=True))
        assert not set(plain) & set(daemon.PING_STOP_KEYS)
        assert set(machine) == set(plain) | set(daemon.PING_MACHINE_KEYS)
        assert set(stop) == set(plain) | set(daemon.PING_STOP_KEYS)
        assert set(both) == set(machine) | set(daemon.PING_STOP_KEYS)
        assert {k: stop[k] for k in daemon.PING_STOP_KEYS} == {
            "stop_owed": False, "owed_stops_sent": 0, "motion_stopped": False,
            "interrupted": None, "load_seen": 0}
        r, w = await _open(h)
        assert (await _step(r, w, 0))["ok"]
        indicate(f, "3")                           # paused on the panel after the header
        await asyncio.sleep(0.1)
        paused = await _ping(h, stop=True)
        await h.client().stop()
        stopped = await _ping(h, stop=True)
        w.close()
        indicate(f, "2")
        indicate(f, "0")
        return paused, stopped, await _ping(h, stop=True)
    paused, stopped, unloaded = harness.run(scenario)
    assert paused["interrupted"] == "3" and paused["motion_stopped"] is False
    assert stopped["motion_stopped"] is True
    assert unloaded["load_seen"] == 1
    assert daemon.PING_STOP_KEYS == ("stop_owed", "owed_stops_sent", "motion_stopped",
                                     "interrupted", "load_seen")


def test_the_client_asks_for_the_stop_fields(harness):
    async def scenario(h):
        c = h.client()
        return await c.ping_stop(), await c.ping_machine(stop=True), await c.ping_machine()
    stop, both, machine = harness.run(scenario)
    assert set(daemon.PING_STOP_KEYS) <= set(stop) and set(daemon.PING_STOP_KEYS) <= set(both)
    assert not set(daemon.PING_STOP_KEYS) & set(machine)
    assert client_mod.STOP_TIMEOUT >= 20


# --- (8) the log: changes only, the statuses after a stop, every link ------------------------------
def test_status_changes_are_logged_once(harness):
    async def scenario(h):
        f = await h.fresh()
        for digit in ("2", "2", "2", "0", "1", "4", "4", "0"):
            indicate(f, digit)
        await asyncio.sleep(0.1)
    harness.run(scenario)
    changes = [x for x in harness.log if x.startswith("[status change]")]
    assert changes == ["[status change] 2 = Load Media", "[status change] 4 = Cancelling"]


def test_status_changes_after_a_stop_are_logged_ready_and_busy_too(harness):
    async def scenario(h):
        f = await h.fresh()
        await h.client().stop()
        for digit in ("1", "4", "0"):
            indicate(f, digit)
        await asyncio.sleep(0.1)
    harness.run(scenario)
    changes = [x for x in harness.log if x.startswith("[status change]")]
    assert changes == ["[status change] 1 = Busy", "[status change] 4 = Cancelling",
                       "[status change] 0 = Ready"]


def test_every_link_and_every_failed_check_is_logged(harness, monkeypatch):
    monkeypatch.setattr(daemon, "RETRY_WAIT", 0.05)
    monkeypatch.setattr(daemon, "STILL_WAITING_EVERY", 0.0)
    harness.fake_cls.fg_reply = None               # FG is never answered

    async def scenario(h):
        await _until(lambda: _links(h, 3), timeout=30)
        h.fake_cls.fg_reply = stubs.TESTED_FG
        await _until(lambda: _ping_if(h, lambda p: p["connected"]), timeout=20)
    harness.run(scenario, timeout=60, until=lambda p: p.get("reason") == "unreachable")
    log = harness.log
    up = [x for x in log if x.startswith("Bluetooth link to CAMEO 4-fake [") and "is up" in x]
    failed = [x for x in log if x.startswith("the FG check on CAMEO 4-fake [")]
    assert len(up) >= 3 and len(failed) >= 2
    assert sum(x.startswith("waiting for the machine") for x in log) == 1
    assert any(x.startswith("still waiting for the machine (") for x in log)
    assert any(x.startswith("connected to CAMEO 4-fake [") for x in log)


async def _links(h, n: int) -> bool:
    return len(h.machine()) >= n


def test_refusals_at_open_and_tool_down_steps(harness):
    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4")
        f.status_override = "3"
        refused = (await h.raw([{"op": "job", "spec": spec_dict()}]))[2][0]
        f.status_override = None
        r, w, out = await h.raw([{"op": "job", "spec": spec_dict()}])
        w.close()
        return refused, out[0]
    refused, opened = harness.run(scenario)
    assert refused["ok"] is False and refused["error"].startswith("The machine is paused")
    assert any(x.startswith("job: refused at open: The machine is paused") for x in harness.log)
    assert [s["tool_down"] for s in opened["steps"]] == [False, False, True, False, True, False]


# --- Bluetooth off, and the connect retry ----------------------------------------------------------
def _find(monkeypatch, **settings):
    bus = type("BlueZ", (stubs.FakeBlueZ,), {"devices": [stubs.bluez_device()], **settings})
    monkeypatch.setattr(ble, "MessageBus", bus)
    return asyncio.run(ble.find_bonded_cameo())


def test_bluetooth_switched_off_is_not_a_pairing_problem(monkeypatch):
    with pytest.raises(BluetoothOff):
        _find(monkeypatch, adapter_powered=False)
    with pytest.raises(BluetoothOff):
        _find(monkeypatch, adapter=False)
    assert _find(monkeypatch).address == ADDRESS
    assert not issubclass(BluetoothOff, PlotterUnreachable)
    not_ready = sys.modules["bleak.exc"].BleakDBusError("org.bluez.Error.NotReady")
    assert ble.bluetooth_off_error(not_ready) and ble.bluetooth_off_error(BluetoothOff("x"))
    assert not ble.bluetooth_off_error(PlotterUnreachable("x"))


def test_the_service_says_bluetooth_is_off(harness):
    harness.bluez.adapter_powered = False

    async def scenario(h):
        reply = await _until(lambda: _ping_if(h, lambda p: p.get("reason")))
        assert reply["reason"] == REASON_NO_BLUETOOTH and h.fake is None
        h.bluez.adapter_powered = True             # switched on again
        await _until(lambda: _ping_if(h, lambda p: p["connected"]), timeout=20)
    harness.run(scenario, until=lambda p: p.get("reason") is not None)
    assert any(x.startswith("waiting for Bluetooth (it is off, or there is no adapter)")
               for x in harness.log)


def test_a_connect_time_out_waits_before_another_attempt(monkeypatch):
    starts: list[float] = []

    class Slow(stubs.FakeClient):
        async def connect(self):
            starts.append(time.monotonic())
            await asyncio.sleep(0.2)               # stands for bleak's 30 s
            raise TimeoutError()
    monkeypatch.setattr(ble, "BleakClient", Slow)
    monkeypatch.setattr(ble, "CONNECT_ATTEMPT", 0.2)
    monkeypatch.setattr(ble, "CONNECT_RETRY_WAIT", 0.1)
    dev = sys.modules["bleak.backends.device"].BLEDevice(ADDRESS, "CAMEO 4-x", {})
    with pytest.raises(PlotterUnreachable):
        asyncio.run(ble.BleTransport(dev).connect(0.3))   # a second attempt cannot end in time
    assert len(starts) == 1
    starts.clear()
    with pytest.raises(PlotterUnreachable):
        asyncio.run(ble.BleTransport(dev).connect(0.8))
    assert len(starts) == 2 and starts[1] - starts[0] >= 0.29   # the attempt, then the pause


# --- the command line --------------------------------------------------------------------------------
def test_the_cli_words_for_a_running_job():
    assert cli.motion_words("job") == "a job from the app"
    assert cli.motion_words("dry-run") == "dry-run (a test program)"
    assert cli.after_stop_line({"ok": True, "errors": []}) is None
    line, alarm = cli.after_stop_line({"status_after": "1", "still_busy": True})
    assert alarm and "PRESS THE POWER BUTTON" in line
    line, alarm = cli.after_stop_line({"status_after": "0", "still_busy": False})
    assert not alarm and "'0' = Ready" in line and "STOP sent" not in line


def test_the_cli_status_names_the_last_reported_status(harness, capsys):
    async def scenario(h):
        await h.fresh()
        r, w = await _open(h)
        await cli.show_status(h.client())
        w.close()
    harness.run(scenario)
    out = capsys.readouterr().out
    assert "running: dry-run (a test program) (last reported status '0' = Ready)" in out


def test_a_refusal_at_start_up_is_dated_in_the_log(monkeypatch, capsys, tmp_path):
    async def refused(*args, **kwargs):
        raise SystemExit("the legacy prototype daemon holds cameo4.lock; stop it first")
    monkeypatch.setattr(daemon, "serve", refused)
    with pytest.raises(SystemExit) as e:
        cli.main(["serve"])
    assert e.value.code == 1
    err = capsys.readouterr().err.splitlines()
    assert "the background service did not start, " in err[-2]
    assert err[-1].endswith(" the legacy prototype daemon holds cameo4.lock; stop it first")
    log = tmp_path / "daemon.log"
    log.write_text("\n".join(err) + "\n")
    assert client_mod.last_log_line(str(log)) == ("the legacy prototype daemon holds "
                                                  "cameo4.lock; stop it first")


# --- each guard on its own (a mutation of any one of them fails a test here) -----------------------
@pytest.mark.parametrize("event", ["stop", "pause"])
def test_wait_ready_ends_at_what_came_while_its_poll_was_out(log, event):
    async def main():
        sess = make_session(wrote=True)
        sess.running = True
        t, lk = await make_link(sess=sess)

        def on_esc05(tr):                          # while this poll is out; it reads '0'
            if event == "stop":
                sess.stop.set()
            else:
                tr.indicate("3")
        t.hooks[ESC05] = on_esc05
        with pytest.raises(daemon.MotionAborted) as e:
            await daemon.wait_ready(lk, asyncio.Lock(), sess, 5)
        return str(e.value)
    text = asyncio.run(main())
    if event == "stop":
        assert text == daemon.STOPPED_IN_STEP
    else:
        assert text.startswith("The machine was paused (status '3' = Paused)")


@pytest.mark.parametrize("event", ["stop", "pause"])
def test_a_step_never_reports_ok_once_its_last_ready_wait_saw_a_stop(log, monkeypatch, event):
    async def ready_but(c, lock, sess, timeout=0.0):
        if event == "stop":
            sess.stop.set()                        # asked for, not sent yet
        else:
            daemon._note_status(sess, "3")
        return "0"
    monkeypatch.setattr(daemon, "wait_ready", ready_but)

    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        return t, await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
    t, res = asyncio.run(main())
    assert res["ok"] is False and res["stopped"] is True and res["stop_errors"] == []
    assert t.writes.count(ESC09) == 2


def test_a_pause_the_session_did_not_hear_is_taken_from_the_link(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=None)        # indications do not reach this session
        t.indicate("3")
        n = len(t.writes)
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Return to Origin",
                                                                           [TRAILER]))
        return t, n, res
    t, n, res = asyncio.run(main())
    assert t.writes[n:] == [ESC09, ESC09]          # the stop, before any query
    assert res["ok"] is False and res["stopped"] is True and TRAILER not in t.writes
    assert res["error"].startswith("The machine was paused")


def test_a_pause_during_u_the_session_did_not_hear_refuses_the_units_step(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=None)
        t.hooks[U] = lambda tr: tr.indicate("3")
        return t, await daemon._run_step(lk, asyncio.Lock(), sess, units_step())
    t, res = asyncio.run(main())
    assert res["ok"] is False and res["stopped"] is True
    assert res["error"].startswith("The machine was paused") and t.writes.count(ESC09) == 2


@pytest.mark.parametrize("case", ["busy", "paused", "stopped"])
def test_motion_stays_possible_unless_the_pre_step_read_is_a_clean_ready(log, monkeypatch, case):
    monkeypatch.setattr(daemon, "PRE_STEP_WAIT", 0.5)

    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        if case == "busy":
            t.status = ["1"]                       # still Busy after the whole pre-step wait
        elif case == "paused":
            t.indicate("3")                        # then the read says '0'
        else:
            sess.stop.set()
        s = await daemon._pre_step_status(lk, asyncio.Lock(), sess)
        return s, sess.motion_possible
    s, possible = asyncio.run(main())
    assert possible is True and s == ("1" if case == "busy" else "0")


def test_a_cancel_during_the_pre_step_polls_sends_the_stop(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.status = ["1"]
        task = asyncio.ensure_future(daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut")))
        await asyncio.sleep(0.6)
        assert not sess.running and sess.motion_possible
        task.cancel()                              # the service ends between two writes
        with pytest.raises(asyncio.CancelledError):
            await task
        return t
    t = asyncio.run(main())
    assert t.writes.count(ESC09) == 2 and MOVE not in t.writes


def test_a_failed_read_after_a_stop_still_reports_the_stop_as_sent(log):
    async def main():
        t, lk = await make_link()
        real = t._write

        async def broken(data):
            if data == ESC05:
                raise RuntimeError("Operation failed (simulated)")   # not a time-out
            await real(data)
        t._write = broken
        state = {"motion": None, "stop_owed": set(), "owed_stops_sent": 0}
        return t, await daemon._stop_op({"op": "stop", "check": True}, lk, asyncio.Lock(), state)
    t, reply = asyncio.run(main())
    assert reply == {"ok": True, "errors": [], "status_after": None, "still_busy": None}
    assert t.writes.count(ESC09) == 2


def test_stops_owed_to_two_machines_are_both_kept(log):
    def link_to(address: str):
        return types.SimpleNamespace(device=types.SimpleNamespace(address=address,
                                                                  name="CAMEO 4-x"))
    first, second = link_to(ADDRESS), link_to(SECOND)
    state = {"c": None, "motion": None, "stop_owed": set()}
    daemon._owe_stop(state, first, "test")
    daemon._owe_stop(state, second, "test")
    daemon._stop_result(state, second, [])         # the second machine's stop got through
    assert state["stop_owed"] == {ADDRESS.upper()}
    assert daemon._stop_fields(state)["stop_owed"] is True
    daemon._stop_result(state, first, [])
    assert daemon._stop_fields(state)["stop_owed"] is False


def test_a_machine_paired_through_two_adapters_is_found_through_the_powered_one(monkeypatch):
    dev = _find(monkeypatch, adapter_powered=False, second_adapter=True)
    assert dev.address == ADDRESS and dev.details["path"].startswith("/org/bluez/hci1/")
    with pytest.raises(BluetoothOff):
        _find(monkeypatch, adapter_powered=False, second_adapter=False)


def test_a_caller_joining_a_stop_that_is_cut_short_never_gets_an_older_result():
    async def main():
        t, lk = await make_link(serialize=False)
        assert await lk.emergency_stop() == []    # an earlier stop that got through
        t.hang.add(ESC09)
        first = asyncio.ensure_future(lk.emergency_stop())
        await asyncio.sleep(0.05)
        joined = asyncio.ensure_future(lk.emergency_stop())
        await asyncio.sleep(0.05)
        first.cancel()
        return await joined
    assert asyncio.run(main()) == ["the stop sequence was cut short"]


def test_the_exit_stop_never_goes_to_another_machine(harness):
    async def scenario(h):
        f = await h.fresh(move_s=0.1)
        r, w = await _open(h)
        for i in (0, 1, 2, 3):                     # to the outline gate: motion still possible
            assert (await _step(r, w, i))["ok"]
        harness.add_machine(SECOND, "CAMEO 4-second")
        harness.bluez.devices[0]["Paired"] = False     # the first machine is not found again
        drop(f)
        await _until(lambda: _ping_if(h, lambda p: p["connected"]), timeout=20)
        other = h.machine(SECOND)[-1]
        await h.shutdown()                         # SIGTERM while the session is still open
        w.close()
        return other.writes, stops(f.writes)
    writes, first = harness.run(scenario)
    assert writes == PASSED and ESC09 not in writes
    assert first == 0                              # the first link was gone: nothing through it


def test_a_late_failed_stop_on_a_lost_link_is_not_owed_once_the_machine_is_back(log):
    def link_to(address: str):
        return types.SimpleNamespace(device=types.SimpleNamespace(address=address,
                                                                  name="CAMEO 4-x"))
    lost, back, other = link_to(ADDRESS), link_to(ADDRESS.lower()), link_to(SECOND)
    state = {"c": back, "motion": None, "stop_owed": set()}
    daemon._stop_result(state, lost, ["1b 09: BleakError: Not connected"] * 2)
    assert state["stop_owed"] == set()             # the new link to it passed its gate already
    assert any("the machine is linked again" in x for x in log)
    state["c"] = other                             # another machine is linked: it stays owed
    daemon._stop_result(state, lost, ["1b 09: BleakError: Not connected"] * 2)
    assert state["stop_owed"] == {ADDRESS.upper()}


def test_a_rejected_reply_before_a_later_step_sends_the_stop(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.status = ["5"]                           # not latched: judged by the pre-step read
        return t, await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
    t, res = asyncio.run(main())
    assert res["ok"] is False and res["stopped"] is True and MOVE not in t.writes
    assert res["error"].startswith("The machine rejected a command (status '5' = Rejected) "
                                   "before this step")
    assert t.writes.count(ESC09) == 2


def test_a_pause_mid_batch_sends_no_further_chunk(log):
    second, third = b"M200,200\x03", b"M300,300\x03"

    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.hooks[MOVE] = lambda tr: tr.indicate("3")   # Pause pressed as the first chunk lands
        res = await daemon._run_step(lk, asyncio.Lock(), sess,
                                     write_step("Cut", [MOVE, second, third]))
        return t, res
    t, res = asyncio.run(main())
    assert MOVE in t.writes and second not in t.writes and third not in t.writes
    assert res["ok"] is False and res["stopped"] is True and t.writes.count(ESC09) == 2


def test_a_latched_pause_does_not_wait_out_a_busy_machine(log):
    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(sess=sess)
        t.status = ["1"]                           # Busy: the pre-step wait would last 10 s
        t.hooks[ESC05] = lambda tr: sess.interrupted or tr.indicate("3")   # paused meanwhile
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
        return res, loop.time() - t0
    res, took = asyncio.run(main())
    assert took < 3 and res["ok"] is False and res["stopped"] is True


def test_a_stop_request_whose_pair_failed_is_sent_again_before_the_next_step(log, monkeypatch):
    monkeypatch.setattr(link, "STOP_WRITE_TIMEOUT", 0.05)

    async def main():
        sess = make_session(wrote=True)
        t, lk = await make_link(serialize=False, sess=sess)
        t.hang.add(ESC09)
        assert await daemon._stop_session(lk, sess, force=True)    # the stop op: it failed
        t.hang.clear()
        n = len(t.writes)
        res = await daemon._run_step(lk, asyncio.Lock(), sess, write_step("Cut"))
        return t.writes[n:], res
    writes, res = asyncio.run(main())
    assert writes == [ESC09, ESC09]                # again, and nothing else
    assert res["ok"] is False and res["stopped"] is True and res["stop_errors"] == []


def test_a_loss_at_the_outline_gate_owes_the_stop_to_the_same_machine(harness):
    async def scenario(h):
        f = await h.fresh(move_s=0.1)
        r, w = await _open(h)
        for i in (0, 1, 2, 3):                     # to the outline gate: motion still possible
            assert (await _step(r, w, i))["ok"]
        drop(f)
        await _until(lambda: _ping_if(h, lambda p: p["connected"] and h.fake is not f),
                     timeout=20)
        await asyncio.sleep(0.5)
        writes = list(h.fake.writes)
        w.close()
        return writes
    writes = harness.run(scenario)
    assert writes[:5] == [*PASSED, ESC09, ESC09] and stops(writes) == 1


def test_a_session_stop_that_fails_on_a_live_link_is_owed(harness):
    async def scenario(h):
        f = await h.fresh(move_s=5.0)
        r, w = await _open(h)
        w.write((json.dumps({"op": "step", "index": 0}) + "\n").encode())
        await w.drain()
        await asyncio.sleep(1.0)
        f.fail_esc09 = True
        reply = await h.client().stop()
        owed = (await _ping(h, stop=True))["stop_owed"]
        f.fail_esc09 = False
        w.close()
        return reply, owed
    reply, owed = harness.run(scenario)
    assert reply["ok"] is False and owed is True


def test_a_late_reply_to_a_timed_out_query_write_never_answers_the_next_query():
    async def main():
        t, lk = await make_link(serialize=False)
        real = t._write
        loop = asyncio.get_running_loop()
        late = []

        async def slow(data):
            if data == ESC05 and not late:
                # acknowledged too late, still answered: its '0' comes while U waits for its own
                async def later():
                    await asyncio.sleep(0.3)
                    await real(data)
                late.append(asyncio.ensure_future(later()))
                await asyncio.shield(late[0])
                return
            if data == U:                          # U's reply comes after that late '0'
                loop.call_later(0.4, lambda: asyncio.ensure_future(real(data)))
                return
            await real(data)
        t._write = slow
        with pytest.raises(TimeoutError, match="no Write Response"):
            await lk.esc_query(0x05)
        return await lk.query("U")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(link, "QUERY_WRITE_TIMEOUT", 0.1)
        assert asyncio.run(main()) == "6100,6100"


def test_the_stop_check_reads_on_while_the_machine_reports_cancelling(log):
    async def main():
        t, lk = await make_link()
        t.status = ["4", "4", "0"]                 # what a stop shows: Cancelling, then Ready
        n = len(t.writes)
        status = await daemon._status_after_stop(lk, asyncio.Lock())
        return status, t.writes[n:]
    status, writes = asyncio.run(main())
    assert status == "0" and writes == [ESC05, ESC05, ESC05]
