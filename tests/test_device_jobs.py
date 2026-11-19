# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Job sessions ('job' op) against a simulated machine, with a fake plan from the contract.

The fake plan has the shape of opencutter.job.plan.Plan (steps with writes, required_tools,
validator, summary). One integration test runs a plan from the real job package.
"""
import asyncio
import json
import re
from dataclasses import dataclass, field

import pytest

import stubs
from stubs import ESC09, ESC15, harness, motion_writes, stops  # noqa: F401

stubs.install()
from opencutter.device import daemon, programs  # noqa: E402
from opencutter.device.protocol import lowers_tool  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings  # noqa: E402

_CMD = re.compile(
    rb"TG1|FN0|TB50,0|\\(0|30),(0|30)|Z6066,6066|J[12]|![0-9]{1,2},[12]|FX[0-9]{1,2},[12]"
    rb"|TJ[0-9]{1,2}|FC[0-9]{1,2},1,[12]|FE[01],[12]|FF[0-9],[0-9],[12]|TF[0-9]{1,2},1"
    rb"|[MD][0-9]{1,4},[0-9]{1,4}")


class FakeValidator:
    """Strict byte grammar like the job package's validator; `reject` refuses chosen writes."""

    def __init__(self):
        self.reject: set[bytes] = set()

    def __call__(self, data: bytes) -> None:
        if data in self.reject:
            raise ValueError("rejected by the test validator")
        if not data or len(data) > 182 or b"\x1b" in data or not data.endswith(b"\x03"):
            raise ValueError("bad write")
        for cmd in data[:-1].split(b"\x03"):
            if not _CMD.fullmatch(cmd):
                raise ValueError(f"bad command {cmd!r}")


@dataclass
class FakeStep:
    kind: str
    writes: list
    title: str
    expect: str = "expected"
    confirm: bool = False


@dataclass
class FakePlan:
    steps: list
    required_tools: dict
    validator: FakeValidator = field(default_factory=FakeValidator)
    summary: dict = field(default_factory=lambda: {"length_mm": 1.0})


def g(*cmds: str) -> bytes:
    return b"".join(c.encode() + b"\x03" for c in cmds)


def cut_chunks(n: int, per_chunk: int) -> list[bytes]:
    """n chunks, each one M and `per_chunk` short D moves inside the cutting area."""
    out = []
    for k in range(n):
        a, b = 1000 + 10 * k, 1000
        out.append(g(f"M{a},{b}", *(f"D{a + 2 * (j % 2)},{b + 10 * j}" for j in range(1, per_chunk + 1))))
    return out


HEADER = g("TG1", "FN0", "TB50,0")
SETUP = g("\\30,30", "Z6066,6066", "J1", "!5,1", "FX10,1", "TJ3", "FC18,1,1", "FE0,1", "FF1,1,1", "TF1,1")
TRAILER = g("\\0,0", "M0,0", "FN0", "TB50,0")
CUT_STEP = 3


def make_plan(cut: list[bytes], required=None) -> FakePlan:
    return FakePlan([FakeStep("write", [HEADER], "Header"),
                     FakeStep("units", [b"U\x03"], "Unit check"),
                     FakeStep("write", [SETUP], "Setup"),
                     FakeStep("write", cut, "Cut", confirm=True),
                     FakeStep("write", [TRAILER], "Return to Origin")],
                    {1: 2} if required is None else required)


@pytest.fixture
def job(harness, monkeypatch):
    """job(plan, allow) makes the daemon open `plan` for any JobSpec."""
    def use(plan, allow=None):
        monkeypatch.setattr(daemon, "build_job", lambda spec: (plan, dict(allow or {})))
        harness.fake_cls.accept = programs.PINNED_WRITES | {
            w for s in plan.steps if s.kind == "write" for w in s.writes}
        return plan
    return use


async def open_job(h, spec=None):
    r, w, out = await h.raw([{"op": "job", "spec": {"fake": True} if spec is None else spec}])
    return r, w, out[0]


async def step(r, w, index, timeout=60):
    w.write((json.dumps({"op": "step", "index": index}) + "\n").encode())
    await w.drain()
    return json.loads(await asyncio.wait_for(r.readline(), timeout))


def plan_writes(writes, plan):
    """The write log reduced to the plan's motion writes."""
    mine = {w for s in plan.steps if s.kind == "write" for w in s.writes}
    return [w for w in writes if w in mine]


def assert_rechecked(writes):
    """Every write that can lower a tool is right after an ESC 15 re-check."""
    for i, w in enumerate(writes):
        if w[:1] != b"\x1b" and w != b"U\x03" and lowers_tool(w):
            assert writes[i - 1] == ESC15, (i, w)


def test_job_open_reply_and_full_run_in_batches(harness, job):
    plan = job(make_plan(cut_chunks(12, 15)))
    assert all(len(c) == 176 for c in plan.steps[CUT_STEP].writes)

    async def scenario(h):
        f = await h.fresh(tools=" 2, 4", move_s=0.2)
        r, w, opened = await open_job(h)
        assert opened["ok"], opened
        assert opened["status"] == "0" and opened["tools"] == " 2, 4" and opened["write_limit"] == 497
        assert opened["summary"] == {"length_mm": 1.0}
        assert [s["writes"] for s in opened["steps"]] == [1, 1, 1, 12, 1]
        assert [s["confirm"] for s in opened["steps"]] == [False, False, False, True, False]
        assert {"kind", "title", "expect"} <= set(opened["steps"][0])
        assert (await h.ping())["motion"] == "job"
        for i in range(len(plan.steps)):
            res = await step(r, w, i)
            assert res["ok"], (i, res)
            assert "seconds" in res
        w.write(b'{"op":"end"}\n')
        await w.drain()
        assert json.loads(await r.readline()) == {"ok": True}
        w.close()
        assert plan_writes(f.writes, plan) == [x for s in plan.steps if s.kind == "write"
                                                for x in s.writes]
        assert_rechecked(f.writes)
        # header, setup, three cut batches of <= 1024 bytes (5 + 5 + 2 chunks), trailer: the
        # machine was idle (ready wait) before each batch
        assert f.runs == [len(HEADER), len(SETUP), 880, 880, 352, len(TRAILER)]
        assert f.max_queued <= 1024 and stops(f.writes) == 0
        assert (await h.ping())["motion"] is None
    harness.run(scenario)


async def _run_to_cut(h, r, w):
    for i in range(CUT_STEP):
        res = await step(r, w, i)
        assert res["ok"], (i, res)


def test_stop_op_is_checked_between_chunks(harness, job):
    plan = job(make_plan(cut_chunks(40, 4)))       # 55-byte chunks: 18 per batch

    async def scenario(h):
        f = await h.fresh(tools=" 2, 4", move_s=0.2, write_delay=0.03)
        cut = plan.steps[CUT_STEP].writes
        f.on_write = lambda d: (asyncio.get_running_loop().create_task(h.client().stop())
                                if d == cut[2] else None)
        r, w, opened = await open_job(h)
        assert opened["ok"], opened
        await _run_to_cut(h, r, w)
        res = await step(r, w, CUT_STEP)
        w.close()
        assert res["ok"] is False and res["stopped"] and not res["stop_errors"]
        first_stop = f.writes.index(ESC09)
        assert not plan_writes(f.writes[first_stop:], plan)
        written = [c for c in cut if c in f.writes]
        assert 3 <= len(written) < 18, len(written)    # stopped inside the first batch
        # The fake serializes writes like BlueZ, so the stop never overtakes a chunk in flight:
        # whatever the timing, exactly one ESC 09 pair goes out (later stop calls join it).
        assert f.writes.count(ESC09) == 2 and stops(f.writes) == 1
        assert TRAILER not in f.writes
    harness.run(scenario)


def test_tools_rechecked_before_every_d_write(harness, job):
    plan = job(make_plan(cut_chunks(6, 15)))

    async def scenario(h):
        f = await h.fresh(tools=" 2, 4", move_s=0.2)
        cut = plan.steps[CUT_STEP].writes
        f.on_write = lambda d: setattr(h.fake_cls, "tools", " 7, 4") if d == cut[1] else None
        r, w, opened = await open_job(h)
        assert opened["ok"], opened
        await _run_to_cut(h, r, w)
        res = await step(r, w, CUT_STEP)
        w.close()
        assert res["ok"] is False and res["stopped"] and "AutoBlade" in res["error"]
        assert cut[1] in f.writes and cut[2] not in f.writes
        assert stops(f.writes) == 1
        assert_rechecked(f.writes)
    harness.run(scenario)


def test_wrong_tool_refused_at_open_and_before_tf(harness, job):
    plan = job(make_plan(cut_chunks(2, 15)))

    async def scenario(h):
        f = await h.fresh(tools=" 7, 4")
        _r, w, opened = await open_job(h)
        w.close()
        assert opened["ok"] is False and "AutoBlade" in opened["error"]
        assert motion_writes(f.writes) == [ESC15]
        f = await h.fresh(tools=" 2, 4")
        f.on_write = lambda d: setattr(h.fake_cls, "tools", " 0, 4") if d == HEADER else None
        r, w, opened = await open_job(h)
        assert opened["ok"]
        assert (await step(r, w, 0))["ok"] and (await step(r, w, 1))["ok"]
        res = await step(r, w, 2)
        w.close()
        assert res["ok"] is False and res["stopped"] is False and "nothing was sent" in res["error"]
        assert SETUP not in f.writes and stops(f.writes) == 0
        assert plan_writes(f.writes, plan) == [HEADER]
    harness.run(scenario)


def test_holder_2_tool_and_allow_unrecognized(harness, job):
    kraft = g("J2", "!3,2", "FX20,2", "TJ3", "FC68,1,2", "FE0,2", "FF1,1,2")
    cut = [g("M2000,2000", "D2000,2500", "D2500,2500")]
    plan = FakePlan([FakeStep("write", [HEADER], "Header"), FakeStep("write", [kraft], "Setup"),
                     FakeStep("write", cut, "Cut")], {2: 4})

    async def scenario(h):
        job(plan)
        f = await h.fresh(tools=" 2, 0")
        _r, w, opened = await open_job(h)
        w.close()
        assert opened["ok"] is False and "Tool 2" in opened["error"]
        job(plan, {2: True})
        f = await h.fresh(tools=" 2, 0", move_s=0.1)
        r, w, opened = await open_job(h)
        assert opened["ok"], opened
        for i in range(3):
            assert (await step(r, w, i))["ok"]
        w.close()
        assert plan_writes(f.writes, plan) == [HEADER, kraft, cut[0]]
    harness.run(scenario)


def test_validator_rejection_at_open_sends_nothing(harness, job):
    plan = job(make_plan(cut_chunks(3, 15)))
    plan.validator.reject = {plan.steps[CUT_STEP].writes[2]}

    async def scenario(h):
        f = await h.fresh()
        _r, w, opened = await open_job(h)
        w.close()
        assert opened["ok"] is False and "refused" in opened["error"]
        assert f.writes == []
        assert (await h.ping())["motion"] is None
    harness.run(scenario)


@pytest.mark.parametrize("bad_index", [0, 3])
def test_validator_rejection_mid_step_stops_before_writing(harness, job, bad_index):
    plan = job(make_plan(cut_chunks(6, 15)))

    async def scenario(h):
        f = await h.fresh(tools=" 2, 4", move_s=0.2)
        cut = plan.steps[CUT_STEP].writes
        r, w, opened = await open_job(h)
        assert opened["ok"]
        plan.validator.reject = {cut[bad_index]}        # accepted at open, refused at write time
        await _run_to_cut(h, r, w)
        res = await step(r, w, CUT_STEP)
        w.close()
        assert res["ok"] is False and "refused" in res["error"]
        assert cut[bad_index] not in f.writes
        assert [c for c in cut if c in f.writes] == cut[:bad_index]
        assert res["stopped"] is (bad_index > 0)
        assert stops(f.writes) == (1 if bad_index else 0)
    harness.run(scenario)


def test_client_eof_mid_cut_stops(harness, job):
    plan = job(make_plan(cut_chunks(12, 15)))

    async def scenario(h):
        f = await h.fresh(tools=" 2, 4", move_s=0.5)
        cut = plan.steps[CUT_STEP].writes
        hit = asyncio.Event()
        f.on_write = lambda d: hit.set() if d == cut[0] else None
        r, w, opened = await open_job(h)
        assert opened["ok"]
        await _run_to_cut(h, r, w)
        w.write(b'{"op":"step","index":3}\n')
        await w.drain()
        await asyncio.wait_for(hit.wait(), 10)
        await asyncio.sleep(0.3)
        w.close()
        await asyncio.sleep(1.5)
        assert stops(f.writes) == 1
        assert not plan_writes(f.writes[f.writes.index(ESC09):], plan)
        assert cut[-1] not in f.writes
        assert (await h.ping())["motion"] is None
    harness.run(scenario)


def test_job_refused_when_the_plan_cannot_be_built(harness, monkeypatch):
    def broken(spec):
        raise ValueError("a point lies outside the media")
    monkeypatch.setattr(daemon, "build_job", broken)

    async def scenario(h):
        f = await h.fresh()
        _r, w, opened = await open_job(h)
        w.close()
        assert opened == {"ok": False, "error": "refused: a point lies outside the media"}
        _r, w, opened = await open_job(h, spec=[1, 2])
        w.close()
        assert opened["ok"] is False and "spec" in opened["error"]
        assert f.writes == []
    harness.run(scenario)


def test_job_with_writes_longer_than_the_mtu_refused_at_open(harness, job):
    job(make_plan(cut_chunks(2, 15)))

    async def scenario(h):
        f = await h.fresh(mtu_payload=100)
        _r, w, opened = await open_job(h)
        w.close()
        assert opened["ok"] is False and "MTU" in opened["error"]
        assert not [x for x in f.writes if x[:1] != b"\x1b"]
    harness.run(scenario)


def test_malformed_plans_are_refused():
    v = FakeValidator()
    for bad in (FakePlan([], {}), FakePlan([FakeStep("move", [HEADER], "x")], {}),
                FakePlan([FakeStep("write", [], "x")], {}),
                FakePlan([FakeStep("write", ["TG1"], "x")], {}),
                FakePlan([FakeStep("write", [b"FB0,0\x03"], "x")], {}, v)):
        with pytest.raises((ValueError, TypeError)):
            daemon.check_plan(bad)
    daemon.check_plan(make_plan(cut_chunks(2, 3)))


def test_batches_and_travel():
    sizes = [176] * 12
    batches = daemon._batches([b"x" * n for n in sizes])
    assert [len(b) for b in batches] == [5, 5, 2]
    assert all(sum(map(len, b)) <= daemon.BATCH_BYTES for b in batches)
    assert daemon._batches([b"x" * 1024, b"y"]) == [[b"x" * 1024], [b"y"]]
    travel, end = daemon._travel([g("M0,0", "D0,300"), g("D400,300", "TF1,1")], (0, 0))
    assert travel == 700 and end == (400, 300)


def test_real_job_plan_runs_through_the_daemon(harness):
    settings = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                            line_segment_overcut=False, overcut=(1, 1), blade_depth=1, passes=1)
    square = [(60.0, 60.0), (70.0, 60.0), (70.0, 70.0), (60.0, 70.0), (60.0, 60.0)]
    spec = JobSpec(mat="12x12", media=(50.0, 50.0, 100.0, 100.0),
                   groups=[CutGroup(settings=settings, paths=[square])])
    plan = build_plan(spec)
    spec_dict = json.loads(json.dumps(spec.to_dict()))

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.1)
        r, w, opened = await open_job(h, spec_dict)
        assert opened["ok"], opened
        assert len(opened["steps"]) == len(plan.steps)
        for i in range(len(plan.steps)):
            res = await step(r, w, i)
            assert res["ok"], (i, opened["steps"][i], res)
        w.close()
        assert plan_writes(f.writes, plan) == [x for s in plan.steps if s.kind == "write"
                                                for x in s.writes]
        assert_rechecked(f.writes)
        assert stops(f.writes) == 0 and f.max_queued <= 1024
    harness.run(scenario)
