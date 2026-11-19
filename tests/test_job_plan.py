# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Plan tests: step structure, preview and confirm, tools, chunking and the summary."""
import dataclasses
import json
import math

import pytest

from opencutter.job import builder
from opencutter.job.plan import PlanStep, build_plan
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings
from opencutter.job.validate import UnsafeJob

MEDIA = (0.0, 0.0, 304.8, 304.8)
SQUARE = [(10.0, 10.0), (30.0, 10.0), (30.0, 30.0), (10.0, 30.0), (10.0, 10.0)]
AUTOBLADE = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                         line_segment_overcut=False, overcut=(1, 1), blade_depth=1)
PEN = ToolSettings(holder=1, tool_code=7, speed=10, force=7, acceleration=3, offset=0,
                   line_segment_overcut=False, overcut=(0, 0), blade_depth=None)
KRAFT = ToolSettings(holder=2, tool_code=4, speed=6, force=8, acceleration=1, offset=68,
                     line_segment_overcut=False, overcut=(1, 1), blade_depth=None, passes=2)


def spec(*groups, preview=True):
    return JobSpec(mat="12x12", media=MEDIA, groups=list(groups), preview_outline=preview)


def kinds(plan):
    return [s.kind for s in plan.steps]


def test_single_group_with_preview():
    plan = build_plan(spec(CutGroup(AUTOBLADE, [list(SQUARE)])))
    header, units, setup, preview, cut, trailer = plan.steps
    assert kinds(plan) == ["write", "units", "write", "write", "write", "write"]
    assert units.writes == [b"U\x03"]
    assert setup.writes[0].startswith(b"\\30,30\x03Z6066,6066\x03J1\x03")
    assert setup.writes[0].endswith(b"TF1,1\x03") and len(setup.writes) == 1
    assert set(preview.writes[0].split(b"\x03")[:-1]) == {b"M200,200", b"M200,600", b"M600,600",
                                                          b"M600,200"}
    assert [s.confirm for s in plan.steps] == [False, False, False, False, True, False]
    assert cut.writes[0].startswith(b"M220,200\x03D200,200\x03")    # blade overlap lead-in
    assert trailer.title == "Return to Origin"
    assert all(isinstance(s, PlanStep) and s.title and s.expect for s in plan.steps)
    assert plan.required_tools == {1: 2}


def test_without_preview_nothing_needs_confirmation():
    plan = build_plan(spec(CutGroup(AUTOBLADE, [list(SQUARE)]), preview=False))
    assert len(plan.steps) == 5
    assert not any(s.confirm for s in plan.steps)


def test_two_groups_two_holders():
    plan = build_plan(spec(CutGroup(PEN, [list(SQUARE)]), CutGroup(KRAFT, [list(SQUARE)])))
    titles = [s.title for s in plan.steps]
    assert len(plan.steps) == 8                         # header U setup preview cut setup cut trailer
    assert titles[4] == "Sketch with Tool 1 (Sketch Pen)"
    assert titles[6] == "Kraft Cut with Tool 2 (Kraft Blade, 3mm)"
    assert plan.steps[5].writes == [b"J2\x03!6,2\x03FX8,2\x03TJ1\x03FC68,1,2\x03FE0,2\x03FF1,1,2\x03"]
    assert [s.confirm for s in plan.steps].count(True) == 1 and plan.steps[4].confirm
    assert plan.required_tools == {1: 7, 2: 4}
    assert sum(w.count(b"\\30,30") for s in plan.steps for w in s.writes) == 1


def test_preview_traces_the_whole_job():
    far = [(200.0, 250.0), (220.0, 250.0)]
    plan = build_plan(spec(CutGroup(PEN, [list(SQUARE)]), CutGroup(KRAFT, [far])))
    corners = {c for c in plan.steps[3].writes[0].split(b"\x03")[:-1]}
    assert corners == {b"M200,200", b"M200,4400", b"M5000,4400", b"M5000,200"}


def test_passes_repeat_the_path():
    plan = build_plan(spec(CutGroup(KRAFT, [list(SQUARE)])))
    cut = b"".join(plan.steps[4].writes)
    assert cut.count(b"M") == 2
    assert cut == b"".join(builder.path(builder.cutter_contours(CutGroup(KRAFT, [list(SQUARE)])), 2))


def test_long_path_is_chunked_at_etx():
    circle = [(150 + 100 * math.cos(t / 500 * math.tau), 150 + 100 * math.sin(t / 500 * math.tau))
              for t in range(501)]
    plan = build_plan(spec(CutGroup(PEN, [circle]), preview=False))
    cut = plan.steps[3]
    assert len(cut.writes) > 10
    assert all(len(w) <= 182 and w.endswith(b"\x03") for w in cut.writes)
    expected = builder.path(builder.cutter_contours(CutGroup(PEN, [circle])))
    assert b"".join(cut.writes) == b"".join(expected)
    for w in cut.writes:
        plan.validator(w)


def test_summary():
    plan = build_plan(spec(CutGroup(PEN, [list(SQUARE)]), CutGroup(KRAFT, [list(SQUARE)])))
    s = json.loads(json.dumps(plan.summary))
    assert s == plan.summary                            # JSON-safe as it is
    assert s["bbox"] == [10.0, 10.0, 20.0, 20.0]
    assert s["path_length_mm"] == pytest.approx(80 + 2 * (80 + 2))   # Kraft: overlap, 2 passes
    assert s["passes"] == 2
    assert s["estimated_seconds"] > 0
    g1, g2 = s["groups"]
    assert (g1["holder"], g1["tool"], g1["action"]) == (1, "Sketch Pen", "Sketch")
    assert (g2["holder"], g2["tool"], g2["passes"]) == (2, "Kraft Blade, 3mm", 2)
    assert g2["settings"]["offset"] == 68
    assert s["writes"] == sum(len(st.writes) for st in plan.steps if st.kind == "write")


def test_slower_speed_takes_longer():
    slow = dataclasses.replace(PEN, speed=1)
    fast = build_plan(spec(CutGroup(PEN, [list(SQUARE)]))).summary["estimated_seconds"]
    assert build_plan(spec(CutGroup(slow, [list(SQUARE)]))).summary["estimated_seconds"] > fast


def test_collapsed_paths_are_dropped():
    dot = [(50.0, 50.0), (50.01, 50.01)]
    plan = build_plan(spec(CutGroup(PEN, [dot]), CutGroup(KRAFT, [list(SQUARE)])))
    assert plan.required_tools == {2: 4}
    assert plan.steps[2].writes[0].startswith(b"\\30,30\x03Z6066,6066\x03J2\x03")
    with pytest.raises(UnsafeJob, match="nothing to cut"):
        build_plan(spec(CutGroup(PEN, [dot])))


def test_invalid_spec_is_refused():
    bad = dataclasses.replace(PEN, speed=31)
    with pytest.raises(UnsafeJob):
        build_plan(spec(CutGroup(bad, [list(SQUARE)])))


def test_plan_validator_is_bound_to_the_job():
    plan = build_plan(spec(CutGroup(PEN, [list(SQUARE)])))
    for data in (b"FB0,0\x03", b"J2\x03", b"!30,1\x03", b"D6100,6000\x03"):
        with pytest.raises(UnsafeJob):
            plan.validator(data)
