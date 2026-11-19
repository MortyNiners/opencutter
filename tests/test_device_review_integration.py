# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Review integration between the daemon and the job package (bytes-2, after both fixes).

The daemon's check_plan always runs the job package's real check_plan_sequence: a plan whose
per-write validator accepts every write, but where a D starts from an unknown point, is refused
when the session opens and nothing reaches the machine. Simulated machine only (tests/stubs.py),
private socket and locks; nothing reaches Bluetooth.
"""
import dataclasses
import json

import stubs
import test_device_jobs as tj
from stubs import harness  # noqa: F401  (fixture)

stubs.install()
from opencutter.device import daemon  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings  # noqa: E402

VINYL = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                     line_segment_overcut=False, overcut=(1, 1), blade_depth=1, passes=1)
SQUARE = [(60.0, 60.0), (85.4, 60.0), (85.4, 85.4), (60.0, 85.4), (60.0, 60.0)]


def spec() -> JobSpec:
    return JobSpec(mat="12x12", media=(50.0, 50.0, 100.0, 100.0),
                   groups=[CutGroup(settings=VINYL, paths=[SQUARE])], preview_outline=False)


def tampered_plan():
    """The real plan with one extra write step right after the unit check: a lone D inside the
    media. Its write passes the per-write validator (a write may open with D when a chunked cut
    continues), but it would cut from where the setup left the tool."""
    plan = build_plan(spec())
    units = next(i for i, st in enumerate(plan.steps) if st.kind == "units")
    lone_d = dataclasses.replace(plan.steps[units + 1], writes=[b"D1300,1300\x03"],
                                 title="tampered")
    plan.validator(lone_d.writes[0])                      # the per-write check alone accepts it
    return dataclasses.replace(plan, steps=plan.steps[:units + 1] + [lone_d]
                               + plan.steps[units + 1:])


def test_a_tampered_plan_is_refused_at_open_with_nothing_sent(harness, monkeypatch):
    plan = tampered_plan()
    monkeypatch.setattr(daemon, "build_job", lambda _spec: (plan, {}))

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh()
        _r, w, opened = await tj.open_job(h, json.loads(json.dumps(spec().to_dict())))
        w.close()
        assert opened["ok"] is False
        assert opened["error"].startswith("refused: step 'tampered'")
        assert "would cut from an unknown point" in opened["error"]
        assert f.writes == []                              # not even a status query was written
        assert (await h.ping())["motion"] is None
    harness.run(scenario)


def test_the_untampered_plan_opens(harness):
    async def scenario(h):
        h.fake_cls.accept_any = True
        await h.fresh()
        _r, w, opened = await tj.open_job(h, json.loads(json.dumps(spec().to_dict())))
        w.close()
        assert opened["ok"] is True, opened
    harness.run(scenario)
