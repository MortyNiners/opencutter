# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Machine profiles in the daemon and the command line.

* The model gate picks the profile from FG on the new link (machines.detect): a USE verdict
  (the tested CAMEO 4 on V5, or an UNTESTED model in its firmware's exact FG form) opens the
  link with that profile; a model OpenCutter only recognizes, or does not know, is refused as
  before (ESC 05 and FG only, link released, address skipped, reason "wrong-model"). A ping
  that asks for them ({"op": "ping", "machine": true}) names the profile, its support level
  and, for a recognized model, the reason in plain words; the plain ping keeps its keys.
* A job runs only when it was made for the connected machine (JobSpec.machine, then the plan's
  profile_id), a program only when it is pinned for it; otherwise nothing is sent.
* Per profile: the ESC 15 grammar and holders, the U check per mat (KNOWN and INFERRED enforced,
  UNKNOWN logged and shown), the status digits, the batch size, the pacing and the stop.
* The command line shows the profile and its support, lists the test programs of the profile,
  and its help says which models are tested, untested and recognized.

The CAMEO 4's bytes and logs are the other device tests' (unchanged). Everything here runs
against the stub BlueZ and the simulated machine of tests/stubs.py (FakeClient), with profiles of
the registry or made up here and registered for one test only: nothing reaches Bluetooth, D-Bus
or a real machine.
"""
import asyncio
import dataclasses
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import types

import pytest

import service_stubs as ss
import stubs
from stubs import ADDRESS, ESC05, ESC09, FG, harness, standard_fg, stops  # noqa: F401

stubs.install()
import fake_daemon  # noqa: E402
from opencutter import __main__ as cli  # noqa: E402
from opencutter.device import daemon, portal, programs  # noqa: E402
from opencutter.device.client import DaemonClient, service_socket_path  # noqa: E402
from opencutter.device.protocol import (CHAR_STATUS, REASON_WRONG_MODEL,  # noqa: E402
                                        evaluate_units, wrong_model_message)
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings  # noqa: E402
from opencutter.machines import (CAMEO4, FirmwareLine, Identity, IntRange,  # noqa: E402
                                 StatusModel, StopSequence, Support, UBasis, registry)

CAMEO5 = registry.enabled_profile("cameo5")
PORTRAIT4 = registry.enabled_profile("portrait4")
CAMEO3 = registry.by_id("cameo3")
PASSED = [ESC05, FG, ESC05]          # a new link that passed the gate: ESC 05, FG, ESC 05 again
UNITS = b"U\x03"
MEDIA = (20.0, 20.0, 100.0, 80.0)
SQUARE = [(30.0, 30.0), (60.0, 30.0), (60.0, 60.0), (30.0, 60.0), (30.0, 30.0)]
ZIGZAG = [(30.0 + 40.0 * (i % 2), 30.0 + 0.5 * i) for i in range(120)]   # a long open path
AUTOBLADE = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                         line_segment_overcut=False, overcut=(1, 1), blade_depth=1)

# A TESTED model that exists only in these tests: the CAMEO 4's commands, with its own FG text
# and BLE name, a lowest Speed of 2 (400 plotter units per second), a batch of 400 bytes, a stop
# gap of 0.5 s and one more status digit.
TEST_CUTTER = dataclasses.replace(
    CAMEO4, id="test_cutter", name="Test Cutter",
    identity=Identity(fg_models=frozenset({"TEST CUTTER"}),
                      lines=(FirmwareLine(1, Support.TESTED, "made up for this test"),),
                      ble_names=("Test",)),
    ranges=dataclasses.replace(CAMEO4.ranges, speed=IntRange(2, 30)),
    status=StatusModel(texts=(*CAMEO4.status.texts, ("6", "Heating"))),
    stop=StopSequence(writes=(ESC09, ESC09), gap_s=0.5),
    batch_bytes=400, programs=frozenset(), basis="made up for this test: no such machine")
# Another one whose ESC 05 digits differ from the CAMEO 4's: busy is '7' and load (an unloaded
# mat) is '8', while '1' and '2' mean states the session machinery does not wait on.
TEST_DIGITS = dataclasses.replace(
    TEST_CUTTER, id="test_digits", name="Digit Cutter",
    identity=Identity(fg_models=frozenset({"DIGIT CUTTER"}),
                      lines=(FirmwareLine(1, Support.TESTED, "made up for this test"),),
                      ble_names=("Digit",)),
    status=StatusModel(texts=(("0", "Ready"), ("1", "Paused"), ("2", "Rejected"),
                              ("4", "Cancelling"), ("7", "Busy"), ("8", "Load Media")),
                       busy="7", load="8"))
# And one whose FG does not claim the session: no second status read after FG.
QUIET_CUTTER = dataclasses.replace(
    TEST_CUTTER, id="quiet_cutter", name="Quiet Cutter",
    identity=Identity(fg_models=frozenset({"QUIET CUTTER"}),
                      lines=(FirmwareLine(1, Support.TESTED, "made up for this test"),),
                      ble_names=("Quiet",)),
    fg_claims_session=False)


def spec(machine: str = "cameo4", mat: str = "12x12", paths=None) -> dict:
    """A JobSpec dict as a client sends it: the AutoBlade in Tool 1 on `paths`."""
    job = JobSpec(machine=machine, mat=mat, media=MEDIA,
                  groups=[CutGroup(AUTOBLADE, paths or [list(SQUARE)])], preview_outline=False)
    return json.loads(json.dumps(job.to_dict()))


def plan_of(job: dict):
    return build_plan(JobSpec.from_dict(job))


def plan_writes(plan) -> list[bytes]:
    return [w for s in plan.steps if s.kind == "write" for w in s.writes]


def sent(writes: list[bytes]) -> list[bytes]:
    """The motion writes of a write log (no ESC query, FG or U)."""
    return [w for w in writes if w[:1] != b"\x1b" and w not in (FG, UNITS)]


async def step(r, w, index: int, timeout: float = 60) -> dict:
    w.write((json.dumps({"op": "step", "index": index}) + "\n").encode())
    await w.drain()
    return json.loads(await asyncio.wait_for(r.readline(), timeout))


async def run_job(h, job: dict) -> tuple[dict, list[dict]]:
    """Open a job session and run its steps until one fails; returns (open reply, results)."""
    r, w, out = await h.raw([{"op": "job", "spec": job}])
    results: list[dict] = []
    try:
        if out[0].get("ok"):
            for i in range(len(out[0]["steps"])):
                results.append(await step(r, w, i))
                if not results[-1].get("ok"):
                    break
    finally:
        w.close()
    return out[0], results


def indicate(f, status: str) -> None:
    """The machine sends a status indication, as the panel does on Unload / Load."""
    f.status = status
    f.cbs[CHAR_STATUS](None, bytearray(status.encode() + b"\x03"))


def wrong_model(ping: dict) -> bool:
    return ping.get("reason") == REASON_WRONG_MODEL


async def machine_ping(h) -> dict:
    """The ping with the machine profile fields ({"op": "ping", "machine": true})."""
    return (await h.raw([{"op": "ping", "machine": True}]))[2][0]


async def _until(predicate, timeout: float = 15.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.05)


# --- the gate picks the profile ------------------------------------------------------------------
def test_the_tested_cameo4_is_named_in_the_ping(harness):
    async def scenario(h):
        ping = await machine_ping(h)
        assert (ping["profile"], ping["support"], ping["refused_reason"]) == (
            "cameo4", "tested", None)
        assert h.fake.writes == PASSED
        assert ("connected to CAMEO 4-fake [00:00:00:00:00:01], CAMEO 4 V5.05, status 0 = Ready"
                in h.log)
        r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}}])
        w.close()
        assert out[0]["ok"] and out[0]["profile"] == "cameo4"
    harness.run(scenario)


@pytest.mark.parametrize("profile, version, model", [
    (CAMEO5, "V1.07", "CAMEO 5 V1.07"), (PORTRAIT4, "V1.05", "Portrait 4 V1.05"),
    (CAMEO4, "V1.20", "CAMEO 4 V1.20"),          # the CAMEO 4's untested firmware line
])
def test_an_untested_model_is_used_with_its_profile(harness, profile, version, model):
    harness.simulate(profile, version)

    async def scenario(h):
        ping = await machine_ping(h)
        assert ping["connected"] and ping["reason"] is None and ping["model"] == model
        assert (ping["profile"], ping["support"], ping["refused_reason"]) == (
            profile.id, "untested", None)
        assert h.fake.writes == PASSED              # FG claims the session: the status again
        assert (f"connected to {profile.name}-fake [00:00:00:00:00:01], {model} (untested), "
                "status 0 = Ready") in h.log
        assert h.fake_cls.instances == [h.fake] and h.fake.is_connected
        info = await h.client().info()
        assert info == {"ok": True, "status": "0", "tools": " 2, 4", "firmware": model}
    harness.run(scenario)


@pytest.mark.parametrize("fg, entry, name", [
    (standard_fg("CAMEO 3", "V1.60"), "cameo3", "CAMEO 3"),        # a recognized model
    (standard_fg("CURIO 2", "V1.16"), "curio2", "CURIO 2"),
    (standard_fg("CAMEO 4", "V9.05"), "cameo4", "CAMEO 4"),        # a firmware line it lacks
])
def test_a_recognized_model_is_refused_with_its_reason(harness, monkeypatch, fg, entry, name):
    monkeypatch.setattr(daemon, "RETRY_WAIT", 0.05)
    harness.fake_cls.fg_reply = fg
    harness.bluez.devices[0]["Name"] = f"{name}-fake"
    model = fg.strip()

    async def scenario(h):
        machine = h.fake
        ping = await machine_ping(h)
        reason = ping["refused_reason"]
        assert ping["connected"] is False and ping["device"] is None
        assert (ping["reason"], ping["model"], ping["profile"], ping["support"]) == (
            REASON_WRONG_MODEL, model, entry, "recognized")
        assert reason.startswith(f"OpenCutter recognizes this {name} but cannot drive it yet: ")
        if entry == "cameo3":
            assert reason == CAMEO3.message
        refusal = f"{reason} Nothing more is sent to it."
        assert daemon.refused_message(model, reason) == refusal
        for op in ({"op": "esc", "code": 5}, {"op": "info"}, {"op": "stop"},
                   {"op": "motion", "program": "dry-run", "options": {}},
                   {"op": "job", "spec": spec()}):
            assert (await h.raw([op]))[2][0] == {"ok": False, "error": refusal}, op
        lines = [x for x in h.log if "reports itself as" in x]
        assert lines == [f'the machine {name}-fake [00:00:00:00:00:01] reports itself as '
                         f'"{model}"; {reason[:-1]}; releasing the link']
        looks = h.bluez.looks
        await _until(lambda: _more_looks(h, looks + 3))           # it looks again: skipped
        assert h.machine() == [machine] and not machine.is_connected
        assert machine.writes == [ESC05, FG]
    harness.run(scenario, until=wrong_model)


async def _more_looks(h, n: int) -> bool:
    return h.bluez.looks >= n


@pytest.mark.parametrize("fg", [
    "CAMEO 5 PRO V1.07    ",      # a model no profile names
    "CAMEO 5 V1.07",              # an untested model, but not in its firmware's exact form
    "Portrait 4 V1.05  ",
])
def test_an_unknown_model_keeps_todays_refusal(harness, fg):
    harness.fake_cls.fg_reply = fg
    model = re.sub(" +", " ", fg.strip())

    async def scenario(h):
        ping = await machine_ping(h)
        assert (ping["reason"], ping["model"], ping["profile"], ping["support"],
                ping["refused_reason"]) == (REASON_WRONG_MODEL, model, None, None, None)
        assert (await h.raw([{"op": "esc", "code": 5}]))[2][0] == {
            "ok": False, "error": wrong_model_message(model)}
        assert any("OpenCutter is tested only with the CAMEO 4 (firmware V5.xx) and does not use "
                   "it; releasing the link" in x for x in h.log)
        assert h.fake.writes == [ESC05, FG]
    harness.run(scenario, until=wrong_model)


def test_a_test_profile_registered_here_is_named_by_fg(harness):
    harness.register(TEST_CUTTER)
    harness.simulate(TEST_CUTTER)

    async def scenario(h):
        ping = await machine_ping(h)
        assert (ping["profile"], ping["support"], ping["model"]) == (
            "test_cutter", "tested", "TEST CUTTER V1.05")
        assert ("connected to Test Cutter-fake [00:00:00:00:00:01], TEST CUTTER V1.05, status 0 "
                "= Ready") in h.log
    harness.run(scenario)


def test_the_status_is_read_again_only_where_fg_claims_the_session(harness):
    harness.register(QUIET_CUTTER)
    harness.simulate(QUIET_CUTTER)

    async def scenario(h):
        ping = await machine_ping(h)
        assert ping["connected"] and ping["profile"] == "quiet_cutter"
        assert h.fake.writes == [ESC05, FG]                  # no ESC 05 after FG
        assert ("connected to Quiet Cutter-fake [00:00:00:00:00:01], QUIET CUTTER V1.05, "
                "status 0 = Ready") in h.log
    harness.run(scenario)


def test_the_machine_fields_are_asked_for(harness):
    """The plain ping keeps its keys (clients and tests compare them as a whole); a ping with
    "machine": true adds exactly the profile, its support level and the refusal reason."""
    async def scenario(h):
        plain, detail = await h.ping(), await machine_ping(h)
        assert not set(plain) & set(daemon.PING_MACHINE_KEYS)
        assert set(detail) == set(plain) | set(daemon.PING_MACHINE_KEYS)
        assert {k: detail[k] for k in daemon.PING_MACHINE_KEYS} == {
            "profile": "cameo4", "support": "tested", "refused_reason": None}
        for flag in (False, 1, "yes", None):              # only exactly true asks for them
            reply = (await h.raw([{"op": "ping", "machine": flag}]))[2][0]
            assert not set(reply) & set(daemon.PING_MACHINE_KEYS)
        assert cli.PING_MACHINE_KEYS == daemon.PING_MACHINE_KEYS
    harness.run(scenario)


def test_without_a_machine_the_machine_fields_are_none(harness):
    harness.bluez.devices[:] = []

    async def scenario(h):
        detail = await machine_ping(h)
        assert detail["connected"] is False and detail["reason"] == "not-paired"
        assert {k: detail[k] for k in daemon.PING_MACHINE_KEYS} == dict.fromkeys(
            daemon.PING_MACHINE_KEYS)
    harness.run(scenario, until=lambda ping: ping.get("reason") == "not-paired")


def test_a_late_status_reply_is_any_drivable_models_status(monkeypatch):
    for reply in ("0", "1", "2", "3", "4", "5"):
        assert daemon._late_status(reply)
    for reply in ("6", "7", " 0", "0 ", "CAMEO 4 V5.05", "", None, 0):
        assert not daemon._late_status(reply)
    # a digit only one profile sends is a late status reply too, not a model
    monkeypatch.setattr(registry, "INDEX", registry.Index((*registry.entries(), TEST_DIGITS)))
    assert daemon._late_status("7") and daemon._late_status("8")


# --- jobs and programs only for the connected machine ------------------------------------------
@pytest.mark.parametrize("machine, error", [
    ("cameo5", "this job was made for the CAMEO 5, but the machine is the CAMEO 4"),
    ("cameo3", "this job was made for the CAMEO 3, but the machine is the CAMEO 4"),
    ("nope", "this job was made for an unknown machine 'nope', but the machine is the CAMEO 4"),
    (5, "the job does not name a machine"),
])
def test_a_job_for_another_machine_is_refused_before_anything_is_sent(harness, monkeypatch,
                                                                      machine, error):
    job = spec("cameo5") if machine == "cameo5" else {**spec(), "machine": machine}
    built = []
    real = daemon.build_job
    monkeypatch.setattr(daemon, "build_job", lambda s: built.append(s) or real(s))

    async def scenario(h):
        opened = (await h.raw([{"op": "job", "spec": job}]))[2][0]
        assert opened == {"ok": False, "error": f"refused: {error}"}
        assert h.fake.writes == PASSED                        # nothing after the gate
        assert built == []                                    # not even a plan was built
    harness.run(scenario)


def test_a_job_without_a_machine_is_a_cameo4_job(harness):
    """A client from before machine profiles sends no "machine": a CAMEO 4 job. It runs on a
    CAMEO 4 and is refused on any other machine."""
    harness.simulate(CAMEO5, tools=" 2, 0")
    job = spec()
    del job["machine"]

    async def scenario(h):
        opened = (await h.raw([{"op": "job", "spec": job}]))[2][0]
        assert opened == {"ok": False, "error": "refused: this job was made for the CAMEO 4, "
                                                "but the machine is the CAMEO 5"}
        assert h.fake.writes == PASSED
    harness.run(scenario)


def test_a_plan_for_another_machine_is_refused(harness, monkeypatch):
    other = plan_of(spec("cameo5"))
    monkeypatch.setattr(daemon, "build_job", lambda _spec: (other, {}))

    async def scenario(h):
        opened = (await h.raw([{"op": "job", "spec": spec()}]))[2][0]
        assert opened == {"ok": False, "error": "refused: this job was made for the CAMEO 5, "
                                                "but the machine is the CAMEO 4"}
        assert h.fake.writes == PASSED
    harness.run(scenario)


def test_programs_run_only_where_they_are_pinned(harness):
    harness.simulate(CAMEO5, tools=" 2, 0")

    async def scenario(h):
        for name in ("dry-run", "pen-test", "cut-test"):
            opened = (await h.raw([{"op": "motion", "program": name, "options": {}}]))[2][0]
            assert opened == {"ok": False,
                              "error": "refused: there are no test programs for the CAMEO 5"}
        assert h.fake.writes == PASSED
    harness.run(scenario)


def test_the_session_reads_the_profile_of_the_link(monkeypatch):
    sess = daemon.MotionSession("job", None, {})
    assert sess.profile is CAMEO4
    dry = {"op": "motion", "program": "dry-run", "options": {}}
    with pytest.raises(ValueError, match="no test programs for the CAMEO 5"):
        daemon._open_request(dry, CAMEO5)
    opened = daemon._open_request(dry, CAMEO4)
    assert opened.profile is CAMEO4 and opened.plan.profile_id == "cameo4"
    job = daemon._open_request({"op": "job", "spec": spec("cameo5")}, CAMEO5)
    assert job.profile is CAMEO5 and job.plan.profile_id == "cameo5"
    # a program pinned for another machine is refused too (Program.profile_id)
    foreign = dataclasses.replace(programs.build_program("dry-run", {}), profile_id="cameo5")
    monkeypatch.setattr(daemon, "build_program", lambda *_: foreign)
    with pytest.raises(ValueError, match="this program was made for the CAMEO 5, but the "
                                         "machine is the CAMEO 4"):
        daemon._open_request(dry, CAMEO4)


# --- jobs on other models ----------------------------------------------------------------------
def test_a_cameo5_job_runs_with_its_profile(harness):
    """A CAMEO 5 job, with a two-digit code in Tool 2 (the CAMEO 5's ESC 15 grammar, an unknown
    tool in the holder the job does not use): every tool check, at open and before every write
    that lowers a tool, reads it with the CAMEO 5's grammar."""
    harness.simulate(CAMEO5, tools=" 2, 12", u_by_tg={1: "6100,6100"})
    harness.fake_cls.accept_any = True
    harness.fake_cls.move_s = 0.05
    job = spec("cameo5")
    plan = plan_of(job)

    async def scenario(h):
        f = h.fake
        opened, results = await run_job(h, job)
        assert opened["ok"] and opened["profile"] == "cameo5" and opened["tools"] == " 2, 12"
        assert [r["ok"] for r in results] == [True] * len(plan.steps), results
        assert f.writes.count(stubs.ESC15) > 1                # the tool check before a D, too
        assert results[1]["verdict"] == "ok" and results[1]["reply"] == "6100,6100"
        assert sent(f.writes) == plan_writes(plan)
        assert f.last_tg == 1 and stops(f.writes) == 0
    harness.run(scenario)


def test_a_u_mismatch_on_the_12x24_mat_refuses_the_job_after_the_header(harness):
    """The CAMEO 5's 12 x 24 in mat (TG2) expects 12200,6100 (INFERRED: enforced). A machine that
    still answers 6100,6100, the 12 x 12 in reply, did not take TG2: nothing more is sent."""
    harness.simulate(CAMEO5, tools=" 2, 0", u_by_tg={2: "6100,6100"})
    harness.fake_cls.accept_any = True
    harness.fake_cls.move_s = 0.05
    job = spec("cameo5", "12x24")
    plan = plan_of(job)
    assert plan.units_expected == (12200, 6100) and plan.units_enforced is True

    async def scenario(h):
        opened, results = await run_job(h, job)
        assert opened["ok"]
        header, units = results
        assert header["ok"] and units["ok"] is False and units["verdict"] == "mismatch"
        assert units["reply"] == "6100,6100"
        assert "not 12200,6100" in units["error"] and "(TG2)" in units["error"]
        assert "comes from the firmware analysis" in units["error"]
        assert sent(h.fake.writes) == list(plan.steps[0].writes)
        assert "job: U -> '6100,6100' (mismatch)" in h.log
    harness.run(scenario)


def test_an_unknown_u_reply_is_logged_and_shown_but_not_enforced(harness):
    """The Portrait 4's mat has no known U reply (UNKNOWN, an UNTESTED profile): the reply is
    logged and shown, the job goes on, and the units step says the cut area was not checked."""
    harness.simulate(PORTRAIT4, tools=" 2, 0", u_reply="  6100,  4300")
    harness.fake_cls.accept_any = True
    harness.fake_cls.move_s = 0.05
    job = spec("portrait4", "8.5x12")
    plan = plan_of(job)
    assert plan.units_enforced is False and plan.units_expected is None

    async def scenario(h):
        opened, results = await run_job(h, job)
        assert opened["ok"] and opened["profile"] == "portrait4"
        assert [r["ok"] for r in results] == [True] * len(plan.steps)
        units = results[1]
        assert units["verdict"] == "unchecked" and units["reply"] == "  6100,  4300"
        assert "not checked" in units["message"] and "could not be checked" in units["message"]
        assert "job: U -> '  6100,  4300' (unchecked)" in h.log
        assert sent(h.fake.writes) == plan_writes(plan)
        assert b"J1\x03" not in b"".join(plan_writes(plan))          # one holder: no J
    harness.run(scenario)


@pytest.mark.parametrize("u_reply, verdict", [(None, "unknown"), ("  ??", "unknown"),
                                              ("  12", "mismatch")])
def test_an_unknown_u_mat_still_needs_a_u_reply(harness, u_reply, verdict):
    """Not enforced is not unchecked: no reply, or a reply that is not a U reply, still refuses
    the job (fail closed on unknown reply formats)."""
    harness.simulate(PORTRAIT4, tools=" 2, 0", u_reply=u_reply)
    harness.fake_cls.accept_any = True
    harness.fake_cls.move_s = 0.05
    job = spec("portrait4", "8.5x12")
    plan = plan_of(job)

    async def scenario(h):
        opened, results = await run_job(h, job)
        assert opened["ok"]
        assert results[0]["ok"] and results[1]["ok"] is False
        assert results[1]["verdict"] == verdict
        if verdict == "unknown":
            assert results[1]["error"] == daemon.UNITS_UNCONFIRMED
        assert sent(h.fake.writes) == list(plan.steps[0].writes)
    harness.run(scenario, timeout=60)


@pytest.mark.parametrize("profile, job, tools, error", [
    (PORTRAIT4, spec("portrait4", "8.5x12"), " 2, 3",
     "unclear tool reply (ESC 15): ' 2, 3'"),            # one holder: the other slot must be 0
    (PORTRAIT4, spec("portrait4", "8.5x12"), " 7, 0",
     "Tool: Sketch Pen (code 7); this needs AutoBlade (code 2)."),
    (CAMEO5, spec("cameo5"), " 12, 0",                    # two digits: read, then refused
     "Tool 1: unknown tool 12 (code 12); this needs AutoBlade (code 2) in Tool 1."),
    (CAMEO5, spec("cameo5"), " 012, 0", "unclear tool reply (ESC 15): ' 012, 0'"),
])
def test_the_tool_check_reads_the_reply_with_the_profile(harness, profile, job, tools, error):
    harness.simulate(profile, tools=tools)
    harness.fake_cls.accept_any = True

    async def scenario(h):
        opened = (await h.raw([{"op": "job", "spec": job}]))[2][0]
        assert opened["ok"] is False and opened["error"] == error and opened["tools"] == tools
        assert sent(h.fake.writes) == []
    harness.run(scenario)


def test_tool_problem_with_each_grammar():
    assert daemon.tool_problem({1: 2}, {}, " 12, 2") == "unclear tool reply (ESC 15): ' 12, 2'"
    assert daemon.tool_problem({1: 2}, {}, " 12, 2", CAMEO5) == (
        "Tool 1: unknown tool 12 (code 12); this needs AutoBlade (code 2) in Tool 1.")
    assert daemon.tool_problem({1: 2}, {}, " 2, 12", CAMEO5) is None
    assert daemon.tool_problem({2: 7}, {}, " 2, 12", CAMEO5) == (
        "Tool 2: unknown tool 12 (code 12); this needs Sketch Pen (code 7) in Tool 2.")
    assert daemon.tool_problem({1: 2}, {}, " 2, 0", PORTRAIT4) is None
    assert daemon.tool_problem({1: 2}, {1: True}, " 0, 0", PORTRAIT4) is None
    assert daemon.tool_problem({1: 2}, {}, " 2, 4", PORTRAIT4) == (
        "unclear tool reply (ESC 15): ' 2, 4'")
    assert daemon.tool_problem({2: 2}, {}, " 2, 0", PORTRAIT4) == "unknown tool holder 2"
    assert daemon.tool_problem({True: 2}, {}, " 2, 4") == "unknown tool holder True"


# --- status digits, batches, pacing and the stop per profile -----------------------------------
def test_batches_and_pacing_follow_the_profile(harness, monkeypatch):
    harness.register(TEST_CUTTER)
    harness.simulate(TEST_CUTTER, tools=" 2, 4")
    harness.fake_cls.accept_any = True
    harness.fake_cls.move_s = 0.3            # each batch is one busy run of the machine
    job = spec("test_cutter", paths=[list(ZIGZAG)])
    plan = plan_of(job)
    cut = plan.steps[3].writes
    assert len(cut) >= 3 and sum(map(len, cut)) > 2 * TEST_CUTTER.batch_bytes
    expected = []
    for st in plan.steps:
        if st.kind == "write":
            pos = (0, 0)
            for batch in daemon._batches(st.writes, 400):
                travel, pos = daemon._travel(batch, pos)
                expected.append(daemon.READY_TIMEOUT + travel / 400.0)
    seen: list[tuple[str, float]] = []
    real_wait = daemon.wait_ready

    async def traced(c, lock, sess, timeout=daemon.READY_TIMEOUT):
        seen.append((sess.profile.id, timeout))
        return await real_wait(c, lock, sess, timeout)
    monkeypatch.setattr(daemon, "wait_ready", traced)

    async def scenario(h):
        f = h.fake
        opened, results = await run_job(h, job)
        assert opened["ok"] and [r["ok"] for r in results] == [True] * len(plan.steps)
        assert sent(f.writes) == plan_writes(plan)
        assert max(f.runs) <= 400 < 1024 and max(f.runs) > TEST_CUTTER.max_write
        assert {pid for pid, _ in seen} == {"test_cutter"}
        assert [t for _, t in seen] == pytest.approx(expected)
        assert TEST_CUTTER.slowest_su_per_s == 400.0 != daemon.SLOWEST_SU_PER_S
    harness.run(scenario)


@pytest.mark.parametrize("register, words", [(True, "Heating"), (False, "?")])
def test_status_digits_are_the_profiles(harness, register, words):
    """'6' is a status of the test profile ("Heating") and none of the CAMEO 4's: either way it
    is not ready, so the job is refused at open, with the profile's words."""
    if register:
        harness.register(TEST_CUTTER)
        harness.simulate(TEST_CUTTER)
    machine = "test_cutter" if register else "cameo4"

    async def scenario(h):
        h.fake.status_override = "6"
        opened = (await h.raw([{"op": "job", "spec": spec(machine)}]))[2][0]
        assert opened["ok"] is False and opened["status"] == "6"
        assert opened["error"] == (f"The machine is not ready (status '6' = {words}). Load the "
                                   "Cutting Mat on the machine right before the job.")
        assert sent(h.fake.writes) == []
    harness.run(scenario)


@pytest.mark.parametrize("digit, words", [("6", "Heating"), ("7", "?")])
def test_any_other_status_aborts_a_step_and_stops(harness, digit, words):
    harness.register(TEST_CUTTER)
    harness.simulate(TEST_CUTTER)
    harness.fake_cls.accept_any = True
    harness.fake_cls.move_s = 0.3

    async def scenario(h):
        f = h.fake
        f.on_write = lambda d: setattr(f, "status_override", digit) if d[:2] == b"TG" else None
        opened, results = await run_job(h, spec("test_cutter"))
        assert opened["ok"]
        [header] = results
        assert header["ok"] is False and header["stopped"] is True
        assert header["error"] == f"unexpected status '{digit}' = {words}"
        assert stops(f.writes) == 1
    harness.run(scenario)


def test_the_busy_and_load_digits_are_the_profiles(harness):
    """On a model whose busy digit is '7' and whose load digit is '8': the ready waits take '7'
    for busy, a '2' (Rejected there) mid-job is not an unloaded mat, and an '8' is: every later
    step is refused without sending anything."""
    harness.register(TEST_DIGITS)
    harness.simulate(TEST_DIGITS, busy_status="7")
    harness.fake_cls.accept_any = True
    harness.fake_cls.move_s = 0.8            # busy past the first ready poll (0.5 s)
    job = spec("test_digits")
    plan = plan_of(job)
    assert [s.kind for s in plan.steps][:4] == ["write", "units", "write", "write"]

    async def scenario(h):
        f = h.fake
        replies: list[str] = []
        status = f.cur_status
        f.cur_status = lambda: replies.append(status()) or replies[-1]
        r, w, out = await h.raw([{"op": "job", "spec": job}])
        try:
            assert out[0]["ok"] and out[0]["profile"] == "test_digits"
            assert [(await step(r, w, i))["ok"] for i in (0, 1)] == [True, True]
            assert "7" in replies                       # it was busy, and that was waited out
            indicate(f, "2")                            # Rejected on this model: not unloaded
            await asyncio.sleep(0.2)
            indicate(f, "0")
            await asyncio.sleep(0.2)
            f.moving_until = asyncio.get_running_loop().time() + 1.0   # busy ('7') a moment
            n = len(replies)
            assert (await step(r, w, 2))["ok"]
            assert replies[n] == "7"                    # waited out before the step's writes
            indicate(f, "8")                            # Load Media: the mat was unloaded
            await asyncio.sleep(0.2)
            indicate(f, "0")
            await asyncio.sleep(0.2)
            n = len(f.writes)
            assert await step(r, w, 3) == {"ok": False, "error": daemon.MEDIA_CHANGED,
                                           "stopped": False, "stop_errors": []}
            assert f.writes[n:] == []                   # nothing more, not even a query
        finally:
            w.close()
        assert sent(f.writes) == [*plan.steps[0].writes, *plan.steps[2].writes]
        assert "job: the mat was unloaded (status 8) during the session; nothing more will be " \
               "sent" in h.log
        assert "[status change] 2 = Rejected" in h.log
    harness.run(scenario)


def test_a_session_needs_a_write_limit_of_32_bytes(harness):
    """MTU - 3 below 32: no session opens (every allowed query's reply must fit in one
    indication), whatever the plan's writes."""
    async def scenario(h):
        f = await h.fresh(mtu_payload=31)
        out = (await h.raw([{"op": "motion", "program": "dry-run", "options": {}}]))[2][0]
        assert out["ok"] is False and out["error"] == (
            "Bluetooth MTU too small: the link takes writes of at most 31 bytes, and a session "
            "needs 32")
        assert sent(f.writes) == []
    harness.run(scenario)


def test_a_write_longer_than_the_profile_takes_is_refused_at_open(harness, monkeypatch):
    """The longest write a session may hold is min(480, the profile's max_write, MTU - 3)
    (link.motion_limit): a write above the CAMEO 4's 182 bytes is refused at open, before
    anything is sent, even on a link that takes 497."""
    plan = plan_of(spec())
    long = b"".join(b"M%d,1000\x03" % (1000 + i) for i in range(25))
    assert CAMEO4.max_write < len(long) < 497
    odd = dataclasses.replace(
        plan, steps=[*plan.steps[:-1], dataclasses.replace(plan.steps[-1], writes=[long])],
        validator=lambda _w: None)
    monkeypatch.setattr(daemon, "build_job", lambda _spec: (odd, {}))

    async def scenario(h):
        opened = (await h.raw([{"op": "job", "spec": spec()}]))[2][0]
        assert opened["ok"] is False and opened["error"] == (
            f"a write has {len(long)} bytes, but the CAMEO 4 takes writes of at most 182; long "
            "writes are never sent")
        assert sent(h.fake.writes) == []
    harness.run(scenario)


def test_the_stop_is_the_profiles(harness):
    harness.register(TEST_CUTTER)
    harness.simulate(TEST_CUTTER)

    async def scenario(h):
        f = h.fake
        loop = asyncio.get_running_loop()
        times: list[float] = []
        f.on_write = lambda d: times.append(loop.time()) if d == ESC09 else None
        assert await h.client().stop() == {"ok": True, "errors": []}
        assert f.writes[len(PASSED):] == [ESC09, ESC09] and len(times) == 2
        assert times[1] - times[0] >= 0.45                       # the profile's gap, 0.5 s
    harness.run(scenario)


def test_the_u_check_uses_the_mat_of_the_plan():
    class Old:                       # a plan object from before machine profiles
        summary: dict = {}
    assert daemon._units_expectation(Old(), CAMEO4) == (CAMEO4.mat("12x12"), None)
    dry = programs.build_program("dry-run", {})
    assert daemon._units_expectation(dry, CAMEO4) == (CAMEO4.mat("12x12"), None)
    wide = plan_of(spec("cameo5", "12x24"))
    assert daemon._units_expectation(wide, CAMEO5) == (CAMEO5.mat("12x24"), None)
    portrait = plan_of(spec("portrait4", "8.5x12"))
    mat, enforced = daemon._units_expectation(portrait, PORTRAIT4)
    assert mat is PORTRAIT4.mat("8.5x12") and mat.u_basis is UBasis.UNKNOWN
    assert evaluate_units(" 1,2", mat, enforced)[0] == "unchecked"
    # an UNKNOWN U reply goes unchecked only where the mat is offered: on an UNTESTED profile
    tested = dataclasses.replace(PORTRAIT4, support=Support.TESTED)
    assert daemon._units_expectation(portrait, tested) == (None, True)
    odd = types.SimpleNamespace(units_expected=(1, 2), units_enforced=True, summary={})
    assert daemon._units_expectation(odd, CAMEO4) == ((1, 2), True)
    # only a mat of the profile whose U reply is UNKNOWN makes the check not enforced
    loose = types.SimpleNamespace(units_expected=None, units_enforced=False,
                                  summary={"mat": "12x12"})
    assert daemon._units_expectation(loose, CAMEO4) == (None, True)
    assert evaluate_units("6100,6100", None, True)[0] == "mismatch"
    loose.units_expected = (6100, 6100)
    assert daemon._units_expectation(loose, CAMEO4) == ((6100, 6100), True)


# --- the background status line --------------------------------------------------------------
def test_the_background_line_names_the_machine():
    assert portal.background_status("CAMEO 4") == portal.BACKGROUND_STATUS
    assert portal.background_status("CAMEO 5") == (
        "Holds the CAMEO 5 link. Use Disconnect in OpenCutter to release it; do not close it here.")
    for name in [p.name for p in registry.profiles()] + ["X" * 80, "", " ", "a\nb", "CAMEO 5\x00"]:
        line = portal.background_status(name)
        assert 0 < len(line) <= portal.MAX_STATUS and line.isprintable()
        if line == portal.BACKGROUND_STATUS_ANY:
            continue
        assert name.strip() in line
    assert portal.background_status("CAMEO 5 Alpha Plus") == portal.BACKGROUND_STATUS_ANY


@pytest.mark.parametrize("profile", [CAMEO4, CAMEO5])
def test_the_service_names_another_machine_in_background_apps(harness, monkeypatch, profile):
    started, ended = [], []

    async def announce(log, message=portal.BACKGROUND_STATUS):
        started.append(message)
        try:
            await asyncio.sleep(3600)
        finally:
            ended.append(message)
    monkeypatch.setattr(daemon, "announce_background", announce)
    monkeypatch.setattr(daemon, "in_flatpak", lambda: True)
    if profile is not CAMEO4:
        harness.simulate(profile)

    async def scenario(h):
        assert (await h.ping())["connected"]
        await asyncio.sleep(0.05)
        return list(started), list(ended)
    first, gone = harness.run(scenario)
    if profile is CAMEO4:
        assert first == [portal.BACKGROUND_STATUS] and gone == []
    else:
        assert first == [portal.BACKGROUND_STATUS, portal.background_status("CAMEO 5")]
        assert gone == [portal.BACKGROUND_STATUS]
    assert ended == started                                    # all of them end with the service


# --- the command line --------------------------------------------------------------------------
@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    return lambda *answers: monkeypatch.setattr(cli, "ainput", stubs.Answers(*answers))


def test_cli_shows_the_profile_of_an_untested_model(harness, operator, capsys):
    harness.simulate(CAMEO5, tools=" 2, 0")

    async def scenario(h):
        capsys.readouterr()
        await cli.show_status(h.client())
        assert capsys.readouterr().out.splitlines() == [
            "machine: CAMEO 5-fake [00:00:00:00:00:01]", "model:   CAMEO 5 V1.05",
            "profile: CAMEO 5 (cameo5), not yet tested on this model",
            "Status (ESC 05): '0' = Ready"]
        await cli.show_info(h.client())
        assert capsys.readouterr().out.splitlines() == [
            "Status   (ESC 05): '0' = Ready",
            "Tools    (ESC 15): ' 2, 0' = Tool 1: AutoBlade, Tool 2: No Tool Detected",
            "Sensor   (ESC 16): '   0'",
            "Firmware (FG):     CAMEO 5 V1.05",
            "Profile:           CAMEO 5 (cameo5), not yet tested on this model",
            "Test programs:     none for this machine yet"]
        assert await cli.service_status() is True
        assert ("  model:   CAMEO 5 V1.05, not yet tested on this model (profile cameo5)"
                in capsys.readouterr().out.splitlines())
        operator()                                              # never asked: refused first
        writes = list(h.fake.writes)
        with pytest.raises(SystemExit) as e:
            await cli.run_program(h.client(), "dry-run", {})
        assert e.value.code == "There are no test programs for the CAMEO 5; nothing was sent."
        assert h.fake.writes == writes                          # not even a session was opened
    harness.run(scenario)


def test_cli_shows_the_profile_and_programs_of_the_cameo4(harness, capsys):
    async def scenario(h):
        capsys.readouterr()
        await cli.show_status(h.client())
        out = capsys.readouterr().out.splitlines()
        assert out[2] == "profile: CAMEO 4 (cameo4), tested on real hardware"
        await cli.show_info(h.client())
        out = capsys.readouterr().out.splitlines()
        assert out[-2:] == ["Profile:           CAMEO 4 (cameo4), tested on real hardware",
                            "Test programs:     dry-run, pen-test, cut-test"]
        assert out[1] == "Tools    (ESC 15): ' 2, 4' = Tool 1: AutoBlade, Tool 2: Kraft Blade, 3mm"
    harness.run(scenario)


def test_cli_texts_for_a_recognized_and_an_unknown_model():
    recognized = {"connected": False, "reason": REASON_WRONG_MODEL, "model": "CAMEO 3 V1.60",
                  "profile": "cameo3", "support": "recognized", "refused_reason": CAMEO3.message}
    assert cli._model_line(recognized) == ("CAMEO 3 V1.60, refused: OpenCutter recognizes this "
                                           "CAMEO 3 but cannot drive it yet: there is no known "
                                           "way to stop it from the computer, and it does not "
                                           "report which tools are loaded")
    assert cli._not_connected(recognized) == f"{CAMEO3.message} Nothing more is sent to it."
    assert cli._not_connected(recognized) == daemon.refused_message("CAMEO 3 V1.60",
                                                                    CAMEO3.message)
    unknown = {**recognized, "model": "CAMEO 5 PRO V1.07", "profile": None, "support": None,
               "refused_reason": None}
    assert cli._not_connected(unknown) == wrong_model_message("CAMEO 5 PRO V1.07")
    assert cli._model_line(unknown) == ("CAMEO 5 PRO V1.07, refused: OpenCutter cannot drive "
                                        "this model")
    tested = {"connected": True, "reason": None, "model": "CAMEO 4 V5.05", "profile": "cameo4",
              "support": "tested", "refused_reason": None}
    assert cli._model_line(tested) == "CAMEO 4 V5.05"
    assert cli.ping_profile(tested) is CAMEO4
    assert cli.ping_profile({"connected": True}) is CAMEO4          # a service before profiles
    assert cli.ping_profile({"profile": "no_such_machine"}) is None
    # the CAMEO 4 on its untested firmware line: the model was tested, this firmware not yet
    v1 = {**tested, "model": "CAMEO 4 V1.20", "support": "untested"}
    assert cli._model_line(v1) == ("CAMEO 4 V1.20, not yet tested with this firmware (profile "
                                   "cameo4)")
    assert cli._profile_words(v1) == "CAMEO 4 (cameo4), not yet tested with this firmware"
    assert cli._profile_words({**v1, "profile": "cameo5", "model": "CAMEO 5 V1.07"}) == (
        "CAMEO 5 (cameo5), not yet tested on this model")
    assert cli.program_names("cameo4") == ("dry-run", "pen-test", "cut-test")
    assert cli.program_names("cameo5") == () == cli.program_names(None)


def test_cli_help_says_which_models_are_tested_untested_and_recognized(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    tested, untested, known = re.search(
        r"Machines: OpenCutter has been tested on real hardware only with the (.*?)\. "
        r"It also drives these models, which have not been tested on real hardware yet: "
        r"(.*?)\. It knows these models but cannot drive them yet: (.*?)\. The daemon reads",
        text).groups()
    assert tested == "CAMEO 4 (firmware V5.xx)"
    names = re.split(r", | and ", untested)
    assert "CAMEO 4 (firmware V1.xx)" in names
    assert set(names) - {"CAMEO 4 (firmware V1.xx)"} == {
        p.name for p in registry.profiles() if p.support is Support.UNTESTED}
    assert set(re.split(r", | and ", known)) == {m.name for m in registry.recognized_models()}
    for line in ("dry-run motion test with the tool always up; CAMEO 4 only",
                 "cut-test cut one 1-inch square in adhesive vinyl; CAMEO 4 only",
                 "pen-test draw a 20 mm square with a Sketch Pen in Tool 1; CAMEO 4 only"):
        assert line in text
    with pytest.raises(SystemExit):
        cli.main(["serve", "--help"])
    assert ("the first paired machine whose name starts with CAMEO, Portrait or CURIO"
            in " ".join(capsys.readouterr().out.split()))


# --- the simulated machines and the daemon's code ----------------------------------------------
@pytest.mark.parametrize("profile", registry.profiles(), ids=lambda p: p.id)
def test_the_fake_daemon_simulates_every_profile(profile):
    codes = profile.holder_codes(fake_daemon.tools_reply(profile))
    assert codes is not None and set(codes) == {h.number for h in profile.holders}
    for holder, code in codes.items():
        assert profile.tool(holder, code).usable
    for tg, reply in fake_daemon.units_by_tg(profile).items():
        [mat] = [m for m in profile.mats if m.tg == tg]
        assert evaluate_units(reply, mat)[0] == ("ok" if mat.enforce_u else "unchecked")


def test_a_service_process_simulates_another_model(monkeypatch):
    """service_stubs.spawn_stub_daemon(fake=...) passes FakeClient settings to the real service
    in its own process: here a CAMEO 5 whose U reply after TG1 comes from u_by_tg (its keys go
    through JSON as text) and that answers no other U."""
    runtime = tempfile.mkdtemp(prefix="ocp-", dir="/tmp" if len(tempfile.gettempdir()) > 60
                               else None)            # short: the socket path has a size limit
    os.chmod(runtime, 0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.delenv("OPENCUTTER_SOCKET", raising=False)
    writes = os.path.join(runtime, "w.json")
    proc = ss.spawn_stub_daemon(runtime, writes, idle_exit=900,
                                log=os.path.join(runtime, "daemon.log"),
                                fg_reply=standard_fg("CAMEO 5", "V1.05"),
                                fake={"tools": " 2, 0", "u_reply": None,
                                      "u_by_tg": {1: "6100,6100"}, "accept_any": True,
                                      "move_s": 0.05})
    try:
        path = service_socket_path()

        async def connected():
            ping = await DaemonClient(path).request({"op": "ping", "machine": True})
            return ping if ping.get("connected") else None

        async def scenario():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 20
            while True:
                try:
                    ping = await connected()
                except Exception:  # noqa: BLE001 - not listening yet
                    ping = None
                if ping:
                    break
                assert loop.time() < deadline, "the service did not connect"
                await asyncio.sleep(0.1)
            async with DaemonClient(path).session() as s:
                opened = await s.open_job(spec("cameo5"))
                header = await s.step(0, 60)
                units = await s.step(1, 60)
                await s.end()
            ended = await DaemonClient(path).shutdown()
            return ping, opened, header, units, ended
        ping, opened, header, units, ended = asyncio.run(scenario())
        assert proc.wait(10) == 0
    finally:
        ss.finish(proc)
        shutil.rmtree(runtime, ignore_errors=True)
    assert (ping["profile"], ping["support"], ping["model"]) == ("cameo5", "untested",
                                                                 "CAMEO 5 V1.05")
    assert opened["ok"] and opened["profile"] == "cameo5" and opened["tools"] == " 2, 0"
    assert header["ok"] and (units["ok"], units["verdict"], units["reply"]) == (
        True, "ok", "6100,6100")
    assert ended["ok"]


def test_the_daemon_reads_the_link_through_the_profile_only():
    source = inspect.getsource(daemon)
    assert "c.client" not in source and ".client." not in source
    assert "is_tested_model" not in source and "STATUS_TEXT" not in source
    assert "parse_tools" not in source
    assert daemon.BATCH_BYTES == CAMEO4.batch_bytes == 1024
    assert daemon.SLOWEST_SU_PER_S == CAMEO4.slowest_su_per_s == 200.0


@pytest.mark.parametrize("model", ["CAMEO V1.10", "CAMEO V1.10    ", "Silhouette V1.00    "])
def test_a_name_several_models_report_is_refused_for_that_reason(model):
    """'CAMEO V1.10' is what a CAMEO 1 reports, and a CAMEO Pro MK-II with some of its settings:
    the service and the command line say that the model cannot be told apart, not just that
    OpenCutter cannot drive it (the GUI says the same: gui.machine.SHARED_MODEL)."""
    shown = model.strip()
    want = (f'This machine reports itself as "{shown}", a name that more than one model uses, '
            "so OpenCutter cannot tell which model it is and will not send anything to it.")
    assert wrong_model_message(model) == want
    assert daemon.refused_message(model) == want
    ping = {"connected": False, "reason": REASON_WRONG_MODEL, "model": shown, "profile": None,
            "support": None, "refused_reason": None}
    assert cli._not_connected(ping) == want
    assert cli._model_line(ping) == (f"{shown}, refused: more than one model reports this name, "
                                     "and OpenCutter cannot tell which one it is")
    # a model no profile names gets the plain refusal
    assert wrong_model_message("CAMEO 5 PRO V1.07") == (
        'This machine reports itself as "CAMEO 5 PRO V1.07". OpenCutter cannot drive it and '
        "will not send anything to it.")
