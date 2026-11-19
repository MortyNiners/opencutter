# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Review regressions for the job package.

bytes-2: a tool-down move (D) cuts from wherever the tool is. Within one write the validator
refuses a D that follows anything but an in-media M or D, and `check_plan_sequence` (run by
`build_plan`) refuses a plan in which any D does not start from an M or D inside the media.

bytes-3: Rotary Blade and Punch Tool are refused; pens and foil carry no blade settings
(Offset 0, no Line Segment Overcut, Overcut 0,0), as Studio sends them.
"""
import dataclasses
import math

import pytest

import stubs

stubs.install()
from opencutter.device import programs  # noqa: E402
from opencutter.job import builder  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.job.spec import BLADE_TOOLS, CutGroup, JobSpec, ToolSettings  # noqa: E402
from opencutter.job.validate import (UNSUPPORTED_TOOLS, UnsafeJob,  # noqa: E402
                                     check_plan_sequence, make_write_validator, media_box_su,
                                     validate_spec)

E = b"\x03"
MEDIA = (20.0, 20.0, 100.0, 80.0)                     # x 20..120 mm, y 20..100 mm
SQUARE = [(30.0, 30.0), (60.0, 30.0), (60.0, 60.0), (30.0, 60.0), (30.0, 30.0)]
OPEN = [(40.0, 40.0), (90.0, 80.0), (110.0, 30.0)]
CIRCLE = [(70 + 25 * math.cos(t / 400 * math.tau), 60 + 25 * math.sin(t / 400 * math.tau))
          for t in range(401)]
AUTOBLADE = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                         line_segment_overcut=False, overcut=(1, 1), blade_depth=1)
PEN = ToolSettings(holder=1, tool_code=7, speed=10, force=7, acceleration=3, offset=0,
                   line_segment_overcut=False, overcut=(0, 0), blade_depth=None)
FOIL = dataclasses.replace(PEN, holder=2, tool_code=8)
KRAFT = ToolSettings(holder=2, tool_code=4, speed=6, force=8, acceleration=1, offset=68,
                     line_segment_overcut=True, overcut=(1, 1), blade_depth=None, passes=2)
HEADER, UNITS, SETUP, PREVIEW, CUT, TRAILER = range(6)    # steps of job() with the preview


def job(*groups, preview=True, **kw):
    return JobSpec(mat="12x12", media=MEDIA,
                   groups=list(groups) or [CutGroup(AUTOBLADE, [list(SQUARE)])],
                   preview_outline=preview, **kw)


def g(*cmds: str) -> bytes:
    return b"".join(c.encode() + E for c in cmds)


def inside(spec):
    """A point (a, b) in SU in the middle of the spec's media box."""
    a0, b0, a1, b1 = media_box_su(spec)
    return (a0 + a1) // 2, (b0 + b1) // 2


def with_writes(plan, index, writes):
    """`plan` with step `index` holding `writes` instead."""
    steps = list(plan.steps)
    steps[index] = dataclasses.replace(steps[index], writes=list(writes))
    return dataclasses.replace(plan, steps=steps)


def commands(data: bytes) -> list[bytes]:
    return data[:-1].split(E)


# --- bytes-2: the write validator ----------------------------------------------------------
@pytest.mark.parametrize("before", [
    "M0,0", "\\0,0", "FN0", "TB50,0", "TG1", "\\30,30", "Z6066,6066", "J1", "!5,1", "FX10,1",
    "TJ3", "FC18,1,1", "FE0,1", "FF1,1,1", "TF1,1",
])
def test_write_validator_refuses_a_d_after_any_other_command(before):
    spec = job()
    check = make_write_validator(spec)
    a, b = inside(spec)
    check(g(before))                                # the command alone belongs to this job
    check(g(f"D{a},{b}"))                           # and so does the D alone
    with pytest.raises(UnsafeJob, match="unknown point"):
        check(g(before, f"D{a},{b}"))
    with pytest.raises(UnsafeJob, match="unknown point"):
        check(g(f"M{a},{b}", before, f"D{a},{b}"))  # an earlier in-media M does not help


def test_write_validator_still_accepts_cuts_and_their_continuations():
    spec = job()
    check = make_write_validator(spec)
    a, b = inside(spec)
    check(g(f"M{a},{b}", f"D{a},{b + 1}", f"D{a + 1},{b + 1}"))
    check(g(f"D{a},{b}", f"D{a + 1},{b}"))          # a cut split over writes goes on with D
    check(g(f"D{a},{b}", f"M{a + 5},{b}", f"D{a + 5},{b + 5}"))
    check(g("M0,0", f"M{a},{b}", f"D{a},{b + 1}"))  # an in-media M makes the start known again
    check(g("\\0,0", "M0,0", "FN0", "TB50,0"))      # the trailer


@pytest.mark.parametrize("data", [
    b"M0,0\x03D1200,1200\x03", b"\\0,0\x03M0,0\x03D1200,1200\x03", b"TF1,1\x03D1200,1200\x03",
    b"FN0\x03D1200,1200\x03", b"J1\x03D1200,1200\x03", b"FF1,1,1\x03D1200,1200\x03",
])
def test_reviewer_writes_are_refused(data):
    # The reviewer's examples (exp_dstart.py), with the D moved inside this job's media.
    with pytest.raises(UnsafeJob, match="unknown point"):
        make_write_validator(job())(data)


# --- bytes-2: the whole-plan sequence check ------------------------------------------------
SPECS = {
    "autoblade": job(),
    "autoblade, no preview": job(preview=False),
    "pen and kraft, 2 passes": job(CutGroup(PEN, [list(SQUARE)]),
                                   CutGroup(KRAFT, [list(SQUARE), list(OPEN)])),
    "foil, open path": job(CutGroup(FOIL, [list(OPEN)]), preview=False),
    "long cut over many writes": job(CutGroup(AUTOBLADE, [CIRCLE])),
}


@pytest.mark.parametrize("spec", SPECS.values(), ids=SPECS.keys())
def test_every_built_plan_passes_and_keeps_its_media_box(spec):
    plan = build_plan(spec)
    assert plan.media_box == media_box_su(spec)
    check_plan_sequence(plan)


def test_a_cut_split_over_writes_passes():
    plan = build_plan(SPECS["long cut over many writes"])
    cut = plan.steps[CUT]
    assert cut.confirm and len(cut.writes) > 5
    assert any(w.startswith(b"D") for w in cut.writes[1:])     # continuations open with D
    check_plan_sequence(plan)


def test_the_units_step_is_skipped_and_unknown_kinds_are_refused():
    plan = build_plan(job())
    assert plan.steps[UNITS].kind == "units"
    check_plan_sequence(plan)
    steps = list(plan.steps)
    steps[UNITS] = dataclasses.replace(steps[UNITS], kind="move")
    with pytest.raises(UnsafeJob, match="bad step"):
        check_plan_sequence(dataclasses.replace(plan, steps=steps))


def _mutations():
    spec = job()
    plan = build_plan(spec)
    a, b = inside(spec)
    cut = plan.steps[CUT].writes
    no_m = [cut[0].split(E, 1)[1]] + cut[1:]
    assert no_m[0].startswith(b"D")
    trailer = plan.steps[TRAILER].writes
    return spec, {
        "cut step without its leading M": (CUT, no_m),
        "cut step opening with D after the preview": (CUT, [g(f"D{a},{b}")] + cut),
        "D in a write after the setup (TF)": (SETUP, plan.steps[SETUP].writes + [g(f"D{a},{b}")]),
        "D in a write after the header (TB50,0)": (HEADER, plan.steps[HEADER].writes
                                                   + [g(f"D{a},{b}")]),
        "D after the trailer's M0,0": (TRAILER, [g("\\0,0", "M0,0"), g(f"D{a},{b}")]),
        "D after the whole trailer": (TRAILER, trailer + [g(f"D{a},{b}")]),
    }


@pytest.mark.parametrize("name", list(_mutations()[1]))
def test_plan_with_a_d_from_an_unknown_point_is_refused(name):
    spec, mutations = _mutations()
    index, writes = mutations[name]
    check = make_write_validator(spec)
    for w in writes:                                # each write on its own passes
        check(w)
    with pytest.raises(UnsafeJob, match="unknown point"):
        check_plan_sequence(with_writes(build_plan(spec), index, writes))


def test_the_preview_ends_on_an_in_media_m_and_still_a_step_must_open_with_m():
    spec = job()
    plan = build_plan(spec)
    a0, b0, a1, b1 = plan.media_box
    last = commands(plan.steps[PREVIEW].writes[-1])[-1]
    a, b = (int(v) for v in last[1:].split(b","))
    assert last[:1] == b"M" and a0 <= a <= a1 and b0 <= b <= b1
    with pytest.raises(UnsafeJob, match="start of the step"):
        check_plan_sequence(with_writes(plan, CUT, [g(f"D{a},{b + 1}")]))


def test_d_after_an_m_outside_the_media_box_is_refused():
    plan = build_plan(job())
    a, b = inside(job())
    for start in ("M100,100", "M0,0", f"M{plan.media_box[2] + 1},{b}"):
        with pytest.raises(UnsafeJob, match="unknown point"):
            check_plan_sequence(with_writes(plan, CUT, [g(start, f"D{a},{b}")]))
    check_plan_sequence(with_writes(plan, CUT, [g(f"M{a},{b}", f"D{a},{b + 1}")]))


@pytest.mark.parametrize("bad", ["D0800,800", "d800,800", " D800,800", "D800,800,900,900",
                                 "\x7fD800,800"])
def test_malformed_tool_down_moves_are_refused(bad):
    plan = build_plan(job())
    a, b = inside(job())
    with pytest.raises(UnsafeJob, match="malformed"):
        check_plan_sequence(with_writes(plan, CUT, [g(f"M{a},{b}", bad)]))


def test_build_plan_runs_the_sequence_check(monkeypatch):
    real = builder.path
    monkeypatch.setattr(builder, "path", lambda contours, passes=1: real(contours, passes)[1:])
    with pytest.raises(UnsafeJob, match="unknown point"):
        build_plan(job())
    monkeypatch.setattr(builder, "path",
                        lambda contours, passes=1: [b"M0,0\x03"] + real(contours, passes)[1:])
    with pytest.raises(UnsafeJob, match="unknown point"):
        build_plan(job())


@pytest.mark.parametrize("change", [
    {"steps": []}, {"steps": None}, {"steps": ["write"]}, {"media_box": [0, 0, 6066, 6066]},
    {"media_box": (0, 0, 6066)}, {"media_box": (0.0, 0, 6066, 6066)},
])
def test_malformed_plans_are_refused(change):
    with pytest.raises(UnsafeJob):
        check_plan_sequence(dataclasses.replace(build_plan(job()), **change))


@pytest.mark.parametrize("writes", [["M1000,1000\x03"], [b"M1000,1000"], [], (), None,
                                    b"M1000,1000\x03"])
def test_malformed_write_steps_are_refused(writes):
    plan = build_plan(job())
    steps = list(plan.steps)
    steps[CUT] = dataclasses.replace(steps[CUT], writes=writes)
    with pytest.raises(UnsafeJob):
        check_plan_sequence(dataclasses.replace(plan, steps=steps))


@pytest.mark.parametrize("name, options", [("dry-run", {}), ("pen-test", {"pen_force": 1}),
                                           ("pen-test", {"pen_force": 7}), ("cut-test", {})])
def test_pinned_programs_pass_the_predecessor_rule(name, options):
    prog = programs.build_program(name, options)
    assert not hasattr(prog, "media_box")
    check_plan_sequence(prog)


def test_a_program_shaped_plan_is_held_to_the_predecessor_rule():
    prog = programs.build_program("cut-test", {})
    steps = list(prog.steps)
    setup = next(i for i, s in enumerate(steps) if s.kind == "write"
                 and s.writes[0].endswith(b"TF1,1\x03"))
    steps[setup] = dataclasses.replace(steps[setup],
                                       writes=steps[setup].writes + (g("D1100,2800"),))
    with pytest.raises(UnsafeJob, match="unknown point"):
        check_plan_sequence(dataclasses.replace(prog, steps=tuple(steps)))


# --- bytes-3: tool-type rules --------------------------------------------------------------
@pytest.mark.parametrize("holder", [1, 2])
@pytest.mark.parametrize("code", sorted(UNSUPPORTED_TOOLS))
@pytest.mark.parametrize("blade_like", [False, True])
def test_rotary_blade_and_punch_tool_are_refused(holder, code, blade_like):
    s = dataclasses.replace(PEN, holder=holder, tool_code=code)
    if blade_like:
        s = dataclasses.replace(s, offset=40, overcut=(1, 1))
    with pytest.raises(UnsafeJob, match="not supported"):
        validate_spec(job(CutGroup(s, [list(SQUARE)])))
    with pytest.raises(UnsafeJob, match="not supported"):
        build_plan(job(CutGroup(s, [list(SQUARE)])))


NON_BLADES = [(1, 7), (2, 7), (2, 8)]


@pytest.mark.parametrize("holder, code", NON_BLADES)
@pytest.mark.parametrize("change", [
    {"offset": 18}, {"offset": 1}, {"line_segment_overcut": True}, {"overcut": (1, 1)},
    {"overcut": (0, 1)}, {"overcut": (9, 0)},
    {"offset": 18, "line_segment_overcut": True, "overcut": (1, 1)},
])
def test_pens_and_foil_refuse_blade_settings(holder, code, change):
    s = dataclasses.replace(PEN, holder=holder, tool_code=code, **change)
    with pytest.raises(UnsafeJob, match="not a blade"):
        validate_spec(job(CutGroup(s, [list(SQUARE)])))


def test_pen_with_blade_settings_is_refused_from_json():
    # The GUI path: a Sketch preset with the Overcut switches on, as the daemon receives it.
    s = dataclasses.replace(PEN, offset=18, line_segment_overcut=True, overcut=(1, 1))
    with pytest.raises(UnsafeJob, match="not a blade"):
        build_plan(JobSpec.from_dict(job(CutGroup(s, [list(SQUARE)])).to_dict()))


@pytest.mark.parametrize("holder, code", NON_BLADES)
def test_pens_and_foil_without_blade_settings_build(holder, code):
    s = dataclasses.replace(PEN, holder=holder, tool_code=code)
    plan = build_plan(job(CutGroup(s, [list(SQUARE)])))
    setup = plan.steps[SETUP].writes[0]
    assert g(f"FC0,1,{holder}", f"FE0,{holder}", f"FF0,0,{holder}") in setup


@pytest.mark.parametrize("holder", [1, 2])
@pytest.mark.parametrize("code", sorted(BLADE_TOOLS - UNSUPPORTED_TOOLS))
def test_blades_keep_offset_and_overcut(holder, code):
    s = ToolSettings(holder=holder, tool_code=code, speed=5, force=10, acceleration=3, offset=68,
                     line_segment_overcut=True, overcut=(1, 1), blade_depth=None)
    plan = build_plan(job(CutGroup(s, [list(SQUARE)])))
    assert g(f"FC68,1,{holder}", f"FE1,{holder}", f"FF1,1,{holder}") in plan.steps[SETUP].writes[0]


# The pre-review tests in test_job_validate.py used a pen with the AutoBlade's Offset and
# Overcut; these are the same checks with a pen the stricter validator accepts.
def test_one_holder_cannot_need_two_tools_with_a_valid_pen():
    spec = job(CutGroup(PEN, [list(SQUARE)]), CutGroup(AUTOBLADE, [list(SQUARE)]))
    with pytest.raises(UnsafeJob, match="two different tools"):
        validate_spec(spec)


def test_unrecognized_pen_is_allowed_without_tf():
    validate_spec(job(CutGroup(PEN, [list(SQUARE)]), allow_unrecognized_tool={1: True}))


def test_pen_writes_are_refused_by_another_spec():
    check = make_write_validator(job())
    pen = build_plan(job(CutGroup(dataclasses.replace(PEN, holder=2), [list(SQUARE)])))
    with pytest.raises(UnsafeJob):
        check(pen.steps[SETUP].writes[0])
