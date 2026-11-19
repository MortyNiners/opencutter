# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Regression tests for the daemon findings of the adversarial review (daemon-1..4, bytes-1/-2).

Everything runs against the simulated machine (tests/stubs.py): stub bleak/dbus_fast, a
private socket and private locks in a temp directory. Nothing reaches Bluetooth, and the
user's real $XDG_RUNTIME_DIR/cameo4.lock is never opened (the legacy lock lies next to the
harness's private LOCK_PATH).
"""
import asyncio
import dataclasses
import fcntl
import json
import os
import re
import sys
import types

import pytest

import stubs
import test_device_jobs as tj
from stubs import ESC09, Answers, harness, stops  # noqa: F401
from test_device_jobs import job  # noqa: F401  (fixture)

stubs.install()
from opencutter import __main__ as cli  # noqa: E402
from opencutter.device import ble, daemon, programs  # noqa: E402
from opencutter.device.client import DaemonError  # noqa: E402
from opencutter.device.protocol import CHAR_STATUS  # noqa: E402
from opencutter.job import validate  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings  # noqa: E402

CUT = programs.CUT_TEST
VINYL = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                     line_segment_overcut=False, overcut=(1, 1), blade_depth=1, passes=1)
SQUARE = [(60.0, 60.0), (85.4, 60.0), (85.4, 85.4), (60.0, 85.4), (60.0, 60.0)]
_D_CMD = re.compile(rb"(^|\x03)D\d")


def job_spec(preview: bool = True, media=(50.0, 50.0, 100.0, 100.0)) -> JobSpec:
    return JobSpec(mat="12x12", media=media, groups=[CutGroup(settings=VINYL, paths=[SQUARE])],
                   preview_outline=preview)


def spec_dict(preview: bool = True) -> dict:
    return json.loads(json.dumps(job_spec(preview).to_dict()))


def blade_down(writes) -> list[bytes]:
    """Data writes with a D (tool down) command."""
    return [w for w in writes if w[:1] != b"\x1b" and _D_CMD.search(w)]


def indicate(f, status: str) -> None:
    """The machine sends a status indication, as the panel does on Unload / Load."""
    f.status = status
    f.cbs[CHAR_STATUS](None, bytearray(status.encode() + b"\x03"))


def flock_free(path: str) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


# --- daemon-1: the legacy prototype daemon's lock, and never joining a link held elsewhere ----
def test_legacy_lock_lies_next_to_the_private_lock(harness):
    assert daemon.legacy_lock_path() == harness.legacy_lock
    assert os.path.dirname(daemon.legacy_lock_path()) == os.path.dirname(daemon.OLD_LOCK_PATH)
    assert os.path.dirname(harness.legacy_lock) == harness.dir


def test_legacy_lock_is_the_legacy_prototype_daemons_lock(monkeypatch):
    """In production the old lock and the legacy lock are in $XDG_RUNTIME_DIR, where builds
    before 0.1.0 and the legacy prototype daemon put them, and the service's own lock is in
    $XDG_RUNTIME_DIR/app/<app id>. (A pure path computation: nothing is opened.)"""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(daemon, "OLD_LOCK_PATH", daemon.runtime_path(daemon.LOCK_NAME))
    monkeypatch.setattr(daemon, "LOCK_PATH", daemon.service_path(daemon.LOCK_NAME))
    assert daemon.OLD_LOCK_PATH == "/run/user/4242/opencutter.lock"
    assert daemon.LOCK_PATH == "/run/user/4242/app/io.github.mortyniners.OpenCutter/opencutter.lock"
    assert daemon.legacy_lock_path() == "/run/user/4242/cameo4.lock"


@pytest.mark.parametrize("force", [False, True])
def test_serve_refuses_while_the_legacy_prototype_daemon_runs(harness, force):
    fd = os.open(harness.legacy_lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)          # what the legacy daemon holds
    try:
        with pytest.raises(SystemExit,
                           match="the legacy prototype daemon holds cameo4.lock; stop it first"):
            asyncio.run(daemon.serve(None, False, force))
    finally:
        os.close(fd)
    assert harness.fake is None                    # no BleakClient was ever constructed
    assert not os.path.exists(harness.socket)
    assert flock_free(harness.lock)                # its own lock was released again

    async def scenario(h):                         # with the old daemon gone, it starts
        assert (await h.ping())["connected"]
    harness.run(scenario)


def test_running_daemon_holds_the_legacy_lock(harness):
    async def scenario(h):
        assert not flock_free(h.legacy_lock)       # the legacy daemon now refuses to start
        assert not flock_free(h.lock)
        await h.shutdown()
        assert flock_free(h.legacy_lock) and flock_free(h.lock)
    harness.run(scenario)


def test_reconnect_never_joins_a_link_held_elsewhere(harness, monkeypatch):
    monkeypatch.setattr(daemon, "HELD_ELSEWHERE_WAIT", 0.2)

    async def scenario(h):
        first = h.fake
        n = len(first.writes)
        # The link drops (power cycle) and another program (e.g. the old daemon) reconnects
        # first, so BlueZ now reports the machine Connected.
        h.connected_prop = True
        first.is_connected = False
        first.cb_disc(first)
        await asyncio.sleep(1.5)                   # several looks at the device
        p = await h.ping()
        assert p["connected"] is False and h.fake is first      # no new BleakClient
        assert sum("not joining" in line for line in h.log) == 1, h.log
        refused = (await h.raw([{"op": "motion", "program": "dry-run", "options": {}}]))[2][0]
        assert refused["ok"] is False and "not connected" in refused["error"]
        assert (await h.client().stop())["ok"] is False
        assert first.writes[n:] == []
        h.connected_prop = False                   # that program let go: reconnect as usual
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while not (h.fake is not first and (await h.ping())["connected"]):
            assert loop.time() < deadline, "did not reconnect"
            await asyncio.sleep(0.1)
    harness.run(scenario)


# --- daemon-2: the mat unloaded and loaded again during a session ------------------------------
def test_mat_reloaded_at_the_outline_gate_refuses_the_cut(harness):
    plan = build_plan(job_spec())
    trailer = plan.steps[-1].writes[0]

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.05)
        async with h.client().session() as s:
            opened = await s.open_job(spec_dict())
            assert opened["ok"], opened
            gate = next(i for i, st in enumerate(opened["steps"]) if st["confirm"])
            for i in range(gate):
                assert (await s.step(i, 60))["ok"], i
            indicate(f, "2")                       # Unload on the panel ...
            await asyncio.sleep(0.2)
            indicate(f, "0")                       # ... and Load again: TG1 is not repeated
            await asyncio.sleep(0.2)
            n = len(f.writes)
            res = await s.step(gate, 60)
            assert res == {"ok": False, "error": daemon.MEDIA_CHANGED, "stopped": False,
                           "stop_errors": []}
            assert f.writes[n:] == []              # nothing more was sent, not even a query
            with pytest.raises(DaemonError):       # the session is over
                await s.step(gate + 1, 10)
        assert blade_down(f.writes) == [] and trailer not in f.writes
        assert stops(f.writes) == 0
        assert (await h.ping())["motion"] is None
        assert any("the mat was unloaded" in line for line in h.log)
    harness.run(scenario)


def test_load_before_the_header_is_not_a_media_change(harness):
    plan = build_plan(job_spec())

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.05)
        async with h.client().session() as s:
            opened = await s.open_job(spec_dict())
            assert opened["ok"], opened
            indicate(f, "2")                       # reloaded after the open, before TG1:
            await asyncio.sleep(0.2)               # the header's TG1 follows a fresh load
            indicate(f, "0")
            await asyncio.sleep(0.2)
            for i in range(len(opened["steps"])):
                assert (await s.step(i, 60))["ok"], i
            await s.end()
        assert tj.plan_writes(f.writes, plan) == [w for st in plan.steps if st.kind == "write"
                                                  for w in st.writes]
    harness.run(scenario)


def test_mat_unloaded_between_chunks_stops_the_step(harness, job):
    plan = job(tj.make_plan(tj.cut_chunks(12, 15)))

    async def scenario(h):
        f = await h.fresh(tools=" 2, 4", move_s=0.2)
        cut = plan.steps[tj.CUT_STEP].writes
        f.on_write = lambda d: (f.cbs[CHAR_STATUS](None, bytearray(b"2\x03"))
                                if d == cut[1] else None)
        r, w, opened = await tj.open_job(h)
        assert opened["ok"], opened
        await tj._run_to_cut(h, r, w)
        res = await tj.step(r, w, tj.CUT_STEP)
        w.close()
        assert res["ok"] is False and res["stopped"] and not res["stop_errors"]
        assert "unloaded" in res["error"]
        assert cut[1] in f.writes and cut[2] not in f.writes
        assert stops(f.writes) == 1 and tj.TRAILER not in f.writes
    harness.run(scenario)


def test_cut_test_reloaded_between_the_previews_cuts_nothing(harness, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "ainput", Answers("yes\n", *["\n"] * 5))

    async def scenario(h):
        f = await h.fresh(tools=" 2, 4", move_s=0.2)
        preview1 = CUT[3].writes[0]

        def reload_later(d):
            if d == preview1:
                loop = asyncio.get_running_loop()
                loop.call_later(0.05, indicate, f, "2")
                loop.call_later(0.10, indicate, f, "0")
        f.on_write = reload_later
        await cli.run_program(h.client(), "cut-test", {})
        assert preview1 in f.writes
        assert CUT[4].writes[0] not in f.writes and CUT[5].writes[0] not in f.writes
        assert blade_down(f.writes) == []
    harness.run(scenario)
    assert daemon.MEDIA_CHANGED_PROGRAM in capsys.readouterr().out


def test_a_status_2_reply_marks_the_media_changed_only_after_the_header():
    class Machine:
        async def esc_query(self, code):
            return "2"

    async def pre_step(wrote: bool):
        sess = daemon.MotionSession("job", None, {})
        sess.wrote = wrote
        return await daemon._pre_step_status(Machine(), asyncio.Lock(), sess), sess.media_changed
    assert asyncio.run(pre_step(True)) == ("2", True)
    assert asyncio.run(pre_step(False)) == ("2", False)


# --- daemon-3 / bytes-1: a job never goes on without a confirmed unit --------------------------
@pytest.mark.parametrize("u_reply", [None, "  ??"], ids=["silent", "no-numbers"])
def test_job_refuses_an_unconfirmed_unit(harness, job, u_reply):
    plan = job(tj.make_plan(tj.cut_chunks(2, 15)))

    async def scenario(h):
        f = await h.fresh(tools=" 2, 4", u_reply=u_reply, move_s=0.1)
        r, w, opened = await tj.open_job(h)
        assert opened["ok"], opened
        assert (await tj.step(r, w, 0))["ok"]
        res = await tj.step(r, w, 1, timeout=20)
        assert res["ok"] is False and res["verdict"] == "unknown" and res["reply"] == u_reply
        assert res["error"] == daemon.UNITS_UNCONFIRMED
        assert res["stopped"] is False and res["stop_errors"] == []
        assert await asyncio.wait_for(r.readline(), 10) == b""   # the session is over
        w.close()
        assert tj.plan_writes(f.writes, plan) == [tj.HEADER]
        assert f.writes[f.writes.index(b"U\x03") + 1:] == []
        assert (await h.ping())["motion"] is None
    harness.run(scenario)


def test_program_leaves_an_unconfirmed_unit_to_the_operator(harness):
    async def scenario(h):
        f = await h.fresh(u_reply=None)
        _r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}},
                                  {"op": "step", "index": 0}, {"op": "step", "index": 1},
                                  {"op": "end"}], timeout=20)
        w.close()
        assert out[2]["ok"] is True and out[2]["verdict"] == "unknown" and out[2]["reply"] is None
        assert b"U\x03" in f.writes
    harness.run(scenario)


def test_gui_job_runner_fails_on_a_silent_u(harness):
    from opencutter.gui.runner import JobRunner, Outcome

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", u_reply=None, move_s=0.05)
        runner = JobRunner(h.client())
        outcome = await runner.run_job(spec_dict(preview=False))
        assert outcome == Outcome.FAILED, runner.error
        assert "unit" in (runner.error or "")
        sent = [w for w in f.writes if w[:1] != b"\x1b"]
        assert sent[-1] == b"U\x03" and not any(b"TF" in w for w in sent)
        assert blade_down(f.writes) == []
    harness.run(scenario)


# --- daemon-4: a stop during an in-flight chunk write --------------------------------------
@pytest.mark.parametrize("serialize,pairs,calls", [(True, 1, ["send", "join"]),
                                                   (False, 2, ["send", "send"])],
                         ids=["bluez-like", "overtaking"])
def test_stop_during_an_in_flight_chunk(harness, job, monkeypatch, serialize, pairs, calls):
    plan = job(tj.make_plan(tj.cut_chunks(40, 4)))
    events = []
    real_stop = ble.Cameo4.emergency_stop

    async def traced_stop(self):
        events.append("join" if self._stop_lock.locked() else "send")
        return await real_stop(self)
    monkeypatch.setattr(ble.Cameo4, "emergency_stop", traced_stop)

    async def scenario(h):
        f = await h.fresh(tools=" 2, 4", move_s=0.2, write_delay=0.8, serialize_writes=serialize)
        cut = plan.steps[tj.CUT_STEP].writes
        f.on_write = lambda d: (asyncio.get_running_loop().create_task(h.client().stop())
                                if d == cut[2] else None)
        r, w, opened = await tj.open_job(h)
        assert opened["ok"], opened
        await tj._run_to_cut(h, r, w)
        res = await tj.step(r, w, tj.CUT_STEP)
        w.close()
        await asyncio.sleep(1.0)
        assert res["ok"] is False and res["stopped"] and not res["stop_errors"]
        assert f.writes.count(ESC09) == 2 * pairs and stops(f.writes) == pairs
        assert events == calls
        assert cut[3] not in f.writes and tj.TRAILER not in f.writes
        if serialize:     # like BlueZ: the pair follows the in-flight chunk's Write Response
            after = f.writes[f.writes.index(cut[2]) + 1:]
            assert after == [ESC09, ESC09]
    harness.run(scenario)


def test_fake_machine_serializes_writes_like_bluez(harness):
    async def scenario(h):
        f = await h.fresh(write_delay=0.3)
        h.fake_cls.accept_any = True
        order = []
        f.on_write = order.append
        from opencutter.device.protocol import CHAR_DATA
        slow = asyncio.create_task(f.write_gatt_char(CHAR_DATA, b"M100,100\x03", True))
        await asyncio.sleep(0.05)
        assert f.in_flight
        fast = asyncio.create_task(f.write_gatt_char(CHAR_DATA, ESC09, True))
        await asyncio.sleep(0.1)
        assert order == [b"M100,100\x03"]          # the ESC 09 waits (InProgress)
        await asyncio.gather(slow, fast)
        assert order == [b"M100,100\x03", ESC09]
    harness.run(scenario)


# --- bytes-2 (daemon side): the job package's whole-plan sequence check -------------------------
def test_check_plan_runs_the_job_sequence_check(monkeypatch):
    seen = []

    def refuse(plan):
        seen.append(plan)
        raise validate.UnsafeJob("a D does not start inside the media")
    monkeypatch.setattr(validate, "check_plan_sequence", refuse)
    plan = build_plan(job_spec())
    with pytest.raises(validate.UnsafeJob):
        daemon.check_plan(plan)
    assert seen == [plan]
    prog = programs.build_program("cut-test", {})                 # programs are checked too
    with pytest.raises(validate.UnsafeJob):
        daemon.check_plan(prog)
    assert seen == [plan, prog]


def test_pinned_programs_and_the_fake_plans_pass_the_sequence_check():
    for name, options in (("dry-run", {}), ("cut-test", {}),
                          ("pen-test", {"pen_force": 1, "allow_unrecognized_pen": False}),
                          ("pen-test", {"pen_force": 7, "allow_unrecognized_pen": True})):
        daemon.check_plan(programs.build_program(name, options))
    daemon.check_plan(tj.make_plan(tj.cut_chunks(12, 15)))


def test_job_refused_at_open_when_the_sequence_check_fails(harness, monkeypatch):
    def refuse(plan):
        raise validate.UnsafeJob("a D does not start inside the media")
    monkeypatch.setattr(validate, "check_plan_sequence", refuse)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh()
        _r, w, opened = await tj.open_job(h, spec_dict())
        w.close()
        assert opened == {"ok": False, "error": "refused: a D does not start inside the media"}
        assert f.writes == []
    harness.run(scenario)


def test_check_plan_fails_closed_without_the_sequence_check(monkeypatch):
    # integration: the sequence check is not optional; if it ever went missing, check_plan
    # raises, and the 'job'/'program' open turns any exception into a refusal
    daemon.check_plan(build_plan(job_spec()))
    monkeypatch.delattr(validate, "check_plan_sequence")
    with pytest.raises(AttributeError):
        daemon.check_plan(build_plan(job_spec()))


def test_real_sequence_check_refuses_a_d_that_starts_outside_the_media():
    plan = build_plan(job_spec())
    daemon.check_plan(plan)                                  # the real plan passes
    units = next(i for i, st in enumerate(plan.steps) if st.kind == "units")
    # a lone D inside the media, right after the unit check: it cuts from where the header
    # left the tool (TB50,0 after FN0), not from an M inside the media
    lone_d = dataclasses.replace(plan.steps[0], writes=[b"D1300,1300\x03"], title="tampered")
    tampered = dataclasses.replace(plan, steps=plan.steps[:units + 1] + [lone_d]
                                   + plan.steps[units + 1:])
    with pytest.raises(validate.UnsafeJob):
        daemon.check_plan(tampered)


@pytest.fixture(autouse=True)
def _never_the_users_runtime_dir(monkeypatch):
    """Belt and braces: fail loudly if a test would open a file (cameo4.lock, opencutter.lock)
    in the user's real $XDG_RUNTIME_DIR, where the old daemon may be running."""
    real_dir = os.environ.get("XDG_RUNTIME_DIR")       # before the harness replaces it
    real_open = os.open

    def guarded(path, *a, **k):
        if real_dir and os.path.dirname(os.path.abspath(path)) == os.path.abspath(real_dir):
            raise AssertionError(f"TEST BUG: {path} is in the user's runtime directory")
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", guarded)
    yield
