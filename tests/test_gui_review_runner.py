# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Review fixes in the GUI's JobRunner (no display needed).

- bytes-1 / daemon-3: a unit check (U) whose verdict is not 'ok' ends the run before the next
  step, whatever the daemon answered (defense in depth), with the reload instruction.
- gui-6: a failed step whose daemon-side stop did not get through sets ``stop_failed``.

Nothing reaches Bluetooth: FakeDaemonClient (tests/gui_fakes.py), or the real daemon on the
simulated machine (tests/stubs.py: fake BLE, private socket and lock).
"""
import asyncio

import pytest

import stubs
from stubs import harness, stops  # noqa: F401

stubs.install()
from opencutter.gui import geometry, sending  # noqa: E402
from opencutter.gui.document import PlacedDocument  # noqa: E402
from opencutter.gui.runner import RELOAD_HINT, JobRunner, Outcome, StepState  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS  # noqa: E402
from opencutter.svg.importer import load_svg  # noqa: E402
from gui_fakes import UNITS_UNKNOWN, FakeDaemonClient  # noqa: E402

DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 100 100"
  fill="none" stroke="#000"><rect x="20" y="30" width="30" height="20"/></svg>"""
VINYL = next(p for p in BASIC_PRESETS if p.action == "Cut")
LETTER = geometry.media_rect("letter", False, (0, 0))


@pytest.fixture
def spec(tmp_path) -> dict:
    path = tmp_path / "square.svg"
    path.write_text(DESIGN)
    doc = PlacedDocument(load_svg(str(path)), LETTER)
    return sending.job_spec(doc, LETTER, VINYL.to_tool_settings(), preview_outline=False).to_dict()


def _units_index(runner: JobRunner) -> int:
    return next(i for i, s in enumerate(runner.steps) if s.kind == "units")


def test_runner_keeps_the_step_kinds(spec):
    client = FakeDaemonClient()
    runner = JobRunner(client)
    assert asyncio.run(runner.run_job(spec)) is Outcome.DONE
    kinds = [s.kind for s in runner.steps]
    assert "units" in kinds and set(kinds) <= {"write", "units"}
    assert client.steps_done == list(range(len(runner.steps)))


@pytest.mark.parametrize("reply", [
    UNITS_UNKNOWN,                                                           # U never answered
    {"ok": True, "verdict": "unknown", "reply": "ERR", "message": "U reply without numbers: 'ERR'"},
    {"ok": True},                                                            # no verdict at all
])
def test_unconfirmed_units_end_the_run_before_the_next_step(spec, reply):
    client = FakeDaemonClient()
    client.units_reply = reply
    runner = JobRunner(client)
    assert asyncio.run(runner.run_job(spec)) is Outcome.FAILED
    units = _units_index(runner)
    assert client.steps_started == list(range(units + 1))        # nothing after U was asked for
    assert runner.steps[units].state is StepState.FAILED
    assert all(s.state is StepState.TODO for s in runner.steps[units + 1:])
    assert RELOAD_HINT in runner.error and "did not confirm the unit" in runner.error
    if reply.get("message"):
        assert reply["message"] in runner.error
    assert client.sessions_closed == 1 and client.ended == 0 and client.stops == 0
    assert not runner.stop_failed


def test_failed_step_with_stop_errors_sets_stop_failed(spec):
    client = FakeDaemonClient()
    client.fail_at, client.fail = 3, "BleakError: write failed"
    client.stop_errors = ["1b 09: BleakError: write failed"]
    runner = JobRunner(client)
    assert asyncio.run(runner.run_job(spec)) is Outcome.FAILED
    assert runner.stop_failed and runner.error == "BleakError: write failed"


def test_failed_step_without_stop_errors_keeps_stop_failed_false(spec):
    client = FakeDaemonClient()
    client.fail_at = 3
    runner = JobRunner(client)
    assert asyncio.run(runner.run_job(spec)) is Outcome.FAILED
    assert not runner.stop_failed


def _no_tool_down_after_u(writes: list[bytes]) -> bool:
    data = [w for w in writes if w[:1] != b"\x1b"]
    after = data[next(i for i, w in enumerate(data) if w.startswith(b"U")) + 1:] \
        if any(w.startswith(b"U") for w in data) else data
    return not any(b"D" in w or b"TF" in w for w in after)


def test_silent_u_through_the_real_daemon_cuts_nothing(harness, spec):
    """The reviewers' experiment (verify-bytes/test_v_units.py): U never answers. Before the fix
    the GUI went on to TF and the blade cut; now the run fails with nothing lowered."""
    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.05, u_reply=None)
        runner = JobRunner(h.client())
        outcome = await runner.run_job(spec)
        await asyncio.sleep(0.2)
        return outcome, runner, list(f.writes)
    outcome, runner, writes = harness.run(scenario)
    assert outcome is Outcome.FAILED, runner.error
    assert b"U\x03" in writes                          # the check really ran
    assert _no_tool_down_after_u(writes), writes
    assert "unload" in (runner.error or "").lower()
    assert runner.steps[_units_index(runner)].state is StepState.FAILED


def test_confirmed_units_through_the_real_daemon_still_cut(harness, spec):
    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.05)
        runner = JobRunner(h.client())
        outcome = await runner.run_job(spec)
        return outcome, runner, list(f.writes)
    outcome, runner, writes = harness.run(scenario)
    assert outcome is Outcome.DONE, runner.error
    assert stops(writes) == 0 and any(b"D" in w for w in writes if w[:1] != b"\x1b")
