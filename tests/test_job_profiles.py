# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The job layer with machine profiles (opencutter.job and opencutter.machines).

* The CAMEO 4 is unchanged: every job of the characterization corpus gives the same plan with
  the CAMEO 4 profile passed explicitly, with "machine": "cameo4" in the dict, and without it.
* JobSpec.machine: the default, the dict round trip and its type.
* One real UNTESTED profile of the registry per dialect, with the exact bytes: the CAMEO 5
  (two holders, force up to 40), the Portrait 4 (one holder, no J, TJ 1..3), the Portrait 3
  (one holder, no TJ), the CAMEO 4 Pro with its 24 x 24 in mat (FM1 right after TG9 FN0 TB50,0)
  and the CAMEO 4 Plus with its 15 x 15 in mat. The expected U of each mat and whether it is
  enforced (KNOWN and INFERRED) or only logged (UNKNOWN, UNTESTED profiles only).
* Refusals: an unknown or recognized machine, a mat the profile does not have or does not offer,
  a mismatching machine, a profile that fails the self-check, values outside the profile's
  ranges, and the global never-send list on every profile.

Pure: the device modules run on the stub Bluetooth of tests/stubs.py, and nothing reaches a
machine.
"""
import dataclasses
import json

import pytest

import stubs

stubs.install()
from opencutter.job import builder, validate  # noqa: E402
from opencutter.job import plan as job_plan  # noqa: E402
from opencutter.job import spec as job_spec  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, SpecError, ToolSettings  # noqa: E402
from opencutter.job.validate import (UnsafeJob, bind_job, check_plan_sequence,  # noqa: E402
                                     job_profile, make_write_validator, media_box_su,
                                     validate_spec)
from opencutter.machines import (CAMEO4, HolderForm, Mat, StopSequence, Support,  # noqa: E402
                                 UBasis, registry, safety)
from opencutter.machines import cameo4 as c4  # noqa: E402

from test_profile_characterization import (CORPUS, PLAN_DIGESTS, REFERENCE,  # noqa: E402
                                           digest, plan_lines)

E = b"\x03"
CAMEO5 = registry.enabled_profile("cameo5")
CAMEO4_PRO = registry.enabled_profile("cameo4_pro")
CAMEO4_PLUS = registry.enabled_profile("cameo4_plus")
PORTRAIT3 = registry.enabled_profile("portrait3")
PORTRAIT4 = registry.enabled_profile("portrait4")
MEDIA = (20.0, 20.0, 100.0, 80.0)                     # x 20..120 mm, y 20..100 mm
SQUARE = [(30.0, 30.0), (60.0, 30.0), (60.0, 60.0), (30.0, 60.0), (30.0, 30.0)]
OPEN = [(40.0, 40.0), (90.0, 80.0), (110.0, 30.0)]
AUTOBLADE = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                         line_segment_overcut=False, overcut=(1, 1), blade_depth=1)
PEN = ToolSettings(holder=2, tool_code=7, speed=10, force=7, acceleration=3, offset=0,
                   line_segment_overcut=False, overcut=(0, 0), blade_depth=None)
# The cut step of AUTOBLADE on SQUARE (with the blade overlap), the same on every model.
SQUARE_CUT = (b"M620,600\x03D600,600\x03D600,1200\x03D1200,1200\x03D1200,600\x03D600,600\x03"
              b"D600,620\x03")
TRAILER = b"\\0,0\x03M0,0\x03FN0\x03TB50,0\x03"


def g(*cmds: str) -> bytes:
    return b"".join(c.encode() + E for c in cmds)


def spec(machine: str = "cameo4", mat: str = "12x12", *groups: CutGroup, media=MEDIA,
         preview: bool = False, **kw) -> JobSpec:
    return JobSpec(machine=machine, mat=mat, media=media,
                   groups=list(groups) or [CutGroup(AUTOBLADE, [list(SQUARE)])],
                   preview_outline=preview, **kw)


def writes(plan) -> list[bytes]:
    return [w for s in plan.steps if s.kind == "write" for w in s.writes]


def kinds(plan) -> list[str]:
    return [s.kind for s in plan.steps]


# --- the CAMEO 4 is unchanged ------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(CORPUS))
def test_every_corpus_job_gives_the_frozen_plan_with_the_cameo4_profile(name):
    job = JobSpec.from_dict(CORPUS[name])
    assert job.machine == "cameo4"
    want = PLAN_DIGESTS[name]
    explicit = build_plan(job, CAMEO4)
    named = build_plan(JobSpec.from_dict({**CORPUS[name], "machine": "cameo4"}))
    for plan in (explicit, named):
        machine, texts = plan_lines(plan)
        assert (digest(machine), digest(texts)) == want, name
        assert plan.profile_id == "cameo4"
        assert (plan.units_expected, plan.units_enforced) == ((6100, 6100), True)


def test_the_cameo4_builder_defaults_to_the_cameo4_profile():
    assert builder.header("12x12") == builder.header("12x12", CAMEO4) == \
        [b"TG1\x03", b"FN0\x03", b"TB50,0\x03", b"\\30,30\x03", b"Z6066,6066\x03"]
    assert builder.trailer() == builder.trailer(CAMEO4) == [b"\\0,0\x03", b"M0,0\x03",
                                                            b"FN0\x03", b"TB50,0\x03"]
    for s in (AUTOBLADE, PEN):
        assert builder.condition_block(s) == builder.condition_block(s, CAMEO4)
    assert b"".join(builder.condition_block(AUTOBLADE)) == \
        g("J1", "!5,1", "FX10,1", "TJ3", "FC18,1,1", "FE0,1", "FF1,1,1", "TF1,1")
    assert builder.MATS == {"12x12": {"tg": 1, "area_su": (30, 30, 6066, 6066),
                                      "size_mm": (304.8, 304.8)}}
    assert builder.MAX_WRITE == CAMEO4.max_write == 182
    assert validate.RANGES == CAMEO4.ranges.table()
    assert validate.UNSUPPORTED_TOOLS == CAMEO4.unsupported_codes() == {5, 6}
    # The aliases now computed from CAMEO4 keep the literal values they had before profiles.
    assert job_spec.BLADE_TOOLS == frozenset({1, 2, 3, 4, 5})
    assert (job_spec.NO_TOOL, job_spec.AUTOBLADE, job_spec.DEFAULT_MACHINE) == (0, 2, "cameo4")
    assert job_plan._ACTIONS == {4: "Kraft Cut", 5: "Rotary Cut", 6: "Punch", 7: "Sketch",
                                 8: "Foil"}
    assert builder.SU_PER_MM == 20 and validate.MAX_PASSES == 10
    assert validate._MOVE.pattern == rb"[MD](0|[1-9][0-9]{0,4}),(0|[1-9][0-9]{0,4})"


def test_the_cameo4_plan_carries_its_profile():
    plan = build_plan(JobSpec.from_dict(REFERENCE))
    assert plan.profile_id == "cameo4"
    assert plan.units_expected == (6100, 6100) and plan.units_enforced is True
    assert plan.batch_bytes == 1024 and plan.slowest_su_per_s == 200.0
    assert plan.max_coordinate_su == 99_999
    assert plan.summary["machine"] == "cameo4" and plan.summary["units_enforced"] is True
    assert json.loads(json.dumps(plan.summary)) == plan.summary
    units = plan.steps[1]
    assert units.kind == "units" and units.writes == [b"U\x03"]
    assert units.expect == ("The machine should reply 6100,6100: 0.05 mm per unit, mat command "
                            "accepted.")


def test_the_registrys_cameo4_passed_in_is_the_default():
    job = JobSpec.from_dict(REFERENCE)
    assert job_profile(job) is CAMEO4 and job_profile(job, CAMEO4) is CAMEO4
    validate_spec(job, CAMEO4)
    assert media_box_su(job, CAMEO4) == media_box_su(job) == (390, 390, 2010, 2410)
    bound = bind_job(job)
    assert bound.profile is CAMEO4 and bound.mat is c4.MAT_12X12
    assert bound.media_box == (390, 390, 2010, 2410)
    for w in writes(build_plan(job)):
        bound.validator(w)
        make_write_validator(job, CAMEO4)(w)


# --- JobSpec.machine ---------------------------------------------------------------------------
def test_the_machine_defaults_to_the_cameo4():
    job = JobSpec(mat="12x12", media=MEDIA, groups=[CutGroup(AUTOBLADE, [list(SQUARE)])])
    assert job.machine == "cameo4"
    d = job.to_dict()
    assert d["machine"] == "cameo4"
    del d["machine"]                                  # a client from before machine profiles
    assert JobSpec.from_dict(d) == job


@pytest.mark.parametrize("machine", ["cameo4", "cameo5", "portrait4", "cameo4_pro", "nothing"])
def test_the_machine_survives_the_json_round_trip(machine):
    job = spec(machine, "12x12", allow_unrecognized_tool={1: True})
    d = json.loads(json.dumps(job.to_dict()))
    assert d["machine"] == machine
    assert JobSpec.from_dict(d) == job and JobSpec.from_dict(d).machine == machine


@pytest.mark.parametrize("machine", [None, 4, True, ["cameo4"], {"id": "cameo4"}, b"cameo4"])
def test_the_machine_must_be_a_string(machine):
    d = spec().to_dict()
    d["machine"] = machine
    with pytest.raises(SpecError, match="machine"):
        JobSpec.from_dict(d)


# --- one real profile per dialect --------------------------------------------------------------
def test_the_cameo5_writes_the_two_holder_forms_with_its_own_force_range():
    """CAMEO 5 (UNTESTED): the CAMEO 4's forms, FX up to 40, its 12 x 24 in mat (TG2)."""
    assert CAMEO5.support is Support.UNTESTED and CAMEO5.dialect.holders is HolderForm.DUAL
    strong = dataclasses.replace(AUTOBLADE, force=40)
    plan = build_plan(spec("cameo5", "12x24", CutGroup(strong, [list(SQUARE)])))
    assert kinds(plan) == ["write", "units", "write", "write", "write"]
    assert writes(plan) == [
        g("TG2", "FN0", "TB50,0"),
        g("\\30,30", "Z12162,6066", "J1", "!5,1", "FX40,1", "TJ3", "FC18,1,1", "FE0,1",
          "FF1,1,1", "TF1,1"),
        SQUARE_CUT, TRAILER]
    assert plan.profile_id == "cameo5"
    assert (plan.units_expected, plan.units_enforced) == ((12200, 6100), True)
    assert "reply 12200,6100" in plan.steps[1].expect
    assert plan.steps[2].title == "Tool 1: AutoBlade, Blade Depth 1 (0.1 mm), Force 40, Speed 5"
    assert plan.steps[2].expect.endswith("set at its station on the left (about 10 s).")
    assert plan.steps[3].title == "Cut with Tool 1 (AutoBlade)"
    # the CAMEO 4 takes neither FX40 nor the 12 x 24 in mat
    with pytest.raises(UnsafeJob, match="force 40 outside 1..33"):
        validate_spec(spec("cameo4", "12x12", CutGroup(strong, [list(SQUARE)])))
    with pytest.raises(UnsafeJob, match="force 41 outside 1..40"):
        validate_spec(spec("cameo5", "12x12", CutGroup(dataclasses.replace(strong, force=41),
                                                       [list(SQUARE)])))


def test_a_non_square_mat_keeps_width_across_and_length_along_the_feed():
    """12 x 24 in (CAMEO 5): size_mm is (width x, length y) = (304.8, 609.6) and the cut area
    has a = feed = y first, so long media fits along y and is refused across x."""
    mat = CAMEO5.mat("12x24")
    assert mat.size_mm == (304.8, 609.6) and mat.area_su == (30, 30, 12162, 6066)
    long_y = spec("cameo5", "12x24", CutGroup(PEN, [[(20.0, 20.0), (20.0, 580.0)]]),
                  media=(10.0, 10.0, 280.0, 590.0))
    plan = build_plan(long_y)
    assert plan.media_box == media_box_su(long_y) == (190, 190, 12010, 5810)
    assert plan.steps[-2].writes == [g("M400,400", "D11600,400")]
    with pytest.raises(UnsafeJob, match="does not fit on the 12x24 cutting mat"):
        validate_spec(spec("cameo5", "12x24", media=(10.0, 10.0, 590.0, 280.0)))
    with pytest.raises(UnsafeJob, match="does not fit"):
        validate_spec(spec("cameo5", "12x24", media=(0.0, 0.0, 304.9, 100.0)))
    validate_spec(spec("cameo5", "12x24", media=(0.0, 0.0, 304.8, 609.6)))
    with pytest.raises(UnsafeJob, match="outside the cut area"):     # beyond Z12162 along y
        validate_spec(spec("cameo5", "12x24", CutGroup(PEN, [[(20.0, 20.0), (20.0, 608.5)]]),
                           media=(0.0, 0.0, 304.8, 609.6)))
    with pytest.raises(UnsafeJob, match="outside the media"):        # x beyond the media
        validate_spec(spec("cameo5", "12x24", CutGroup(PEN, [[(20.0, 20.0), (300.0, 20.0)]]),
                           media=(10.0, 10.0, 280.0, 590.0)))


def test_the_portrait3_8x12_mat_is_narrow_across():
    """8 x 12 in (Portrait 3): 203.2 mm across, 304.8 mm along the feed; Z6066,4034."""
    validate_spec(spec("portrait3", "8x12", media=(0.0, 0.0, 203.2, 304.8)))
    with pytest.raises(UnsafeJob, match="does not fit on the 8x12 cutting mat"):
        validate_spec(spec("portrait3", "8x12", media=(0.0, 0.0, 250.0, 150.0)))
    wide = CutGroup(dataclasses.replace(PEN, holder=1), [[(10.0, 10.0), (202.0, 10.0)]])
    with pytest.raises(UnsafeJob, match="outside the cut area"):     # beyond Z..,4034 across
        validate_spec(spec("portrait3", "8x12", wide, media=(0.0, 0.0, 203.2, 304.8)))
    assert media_box_su(spec("portrait3", "8x12", media=(0.0, 0.0, 203.2, 304.8))) == \
        (30, 30, 6066, 4034)


def test_the_cameo5_takes_the_foil_tool_in_tool_1():
    foil = dataclasses.replace(PEN, holder=1, tool_code=8)
    plan = build_plan(spec("cameo5", "12x12", CutGroup(foil, [list(OPEN)])))
    assert plan.required_tools == {1: 8}
    assert plan.steps[3].title == "Foil with Tool 1 (Foil Tool)"
    with pytest.raises(UnsafeJob, match="tool code 8 is not valid in Tool 1"):
        validate_spec(spec("cameo4", "12x12", CutGroup(foil, [list(OPEN)])))


def test_the_portrait4_writes_the_one_holder_forms_without_j():
    """Portrait 4 (UNTESTED): one holder, the plain forms (no J, no holder argument), TJ 1..3
    as its own command, TF<d>,1 last; 8.5 x 12 in mat (TG11) with an UNKNOWN U."""
    assert PORTRAIT4.support is Support.UNTESTED
    assert PORTRAIT4.dialect.holders is HolderForm.SINGLE
    plan = build_plan(spec("portrait4", "8.5x12"))
    assert writes(plan) == [
        g("TG11", "FN0", "TB50,0"),
        g("\\30,30", "Z6066,4288", "!5", "FX10", "TJ3", "FC18", "FE0,0", "FF1,1,0", "TF1,1"),
        SQUARE_CUT, TRAILER]
    assert not any(c.startswith(b"J") for w in writes(plan) for c in w.split(E))
    assert plan.profile_id == "portrait4"
    assert plan.units_expected is None and plan.units_enforced is False
    assert plan.summary["units_enforced"] is False
    assert "could not be checked" in plan.steps[1].expect
    assert plan.steps[2].title == "Tool: AutoBlade, Blade Depth 1 (0.1 mm), Force 10, Speed 5"
    assert plan.steps[2].expect == ("The tool settings are sent; the carriage may move. Nothing "
                                    "touches the media. Then the AutoBlade is set at its station "
                                    "(about 10 s).")
    assert plan.steps[3].title == "Cut with the AutoBlade"
    assert "Tool 1" not in " ".join(s.title + s.expect for s in plan.steps)


@pytest.mark.parametrize("change, match", [
    (dict(acceleration=4), "acceleration 4 outside 1..3"),
    (dict(speed=11), "speed 11 outside 1..10"),
    (dict(offset=46), "offset 46 outside 0..45"),
    (dict(holder=2, blade_depth=None), "tool holder must be 1"),
    (dict(tool_code=9, blade_depth=None), "tool code 9 is not valid in Tool"),
])
def test_the_portrait4_ranges_and_holder_are_its_own(change, match):
    s = dataclasses.replace(AUTOBLADE, **change)
    with pytest.raises(UnsafeJob, match=match):
        validate_spec(spec("portrait4", "8.5x12", CutGroup(s, [list(SQUARE)])))


def test_a_one_holder_model_refuses_two_tools():
    pen = CutGroup(dataclasses.replace(PEN, holder=1), [list(OPEN)])
    with pytest.raises(UnsafeJob, match="the Portrait 4 has one tool holder, and the job uses two "
                                        "different tools"):
        validate_spec(spec("portrait4", "8.5x12", CutGroup(AUTOBLADE, [list(SQUARE)]), pen))
    with pytest.raises(UnsafeJob, match="Tool 1 is used with two different tools"):
        validate_spec(spec("cameo5", "12x12", CutGroup(AUTOBLADE, [list(SQUARE)]), pen))


def test_the_portrait4_has_no_second_holder_to_allow():
    with pytest.raises(UnsafeJob, match="holder 1 to a bool"):
        validate_spec(spec("portrait4", "8.5x12", allow_unrecognized_tool={2: True}))
    pen = dataclasses.replace(PEN, holder=1)
    validate_spec(spec("portrait4", "8.5x12", CutGroup(pen, [list(OPEN)]),
                       allow_unrecognized_tool={1: True}))
    with pytest.raises(UnsafeJob, match="needs the AutoBlade detected in Tool"):
        validate_spec(spec("portrait4", "8.5x12", allow_unrecognized_tool={1: True}))


def test_the_portrait3_has_no_tj():
    """Portrait 3 (UNTESTED): one holder and no acceleration command at all; the settings'
    acceleration is not sent (it must still be a whole number)."""
    plan = build_plan(spec("portrait3", "8x12"))
    assert writes(plan) == [
        g("TG3", "FN0", "TB50,0"),
        g("\\30,30", "Z6066,4034", "!5", "FX10", "FC18", "FE0,0", "FF1,1,0", "TF1,1"),
        SQUARE_CUT, TRAILER]
    assert "mat may feed in about 13.5 mm" in plan.steps[0].expect
    for accel in (0, 1, 10, 99):
        other = dataclasses.replace(AUTOBLADE, acceleration=accel)
        assert writes(build_plan(spec("portrait3", "8x12", CutGroup(other, [list(SQUARE)])))) \
            == writes(plan)
    for bad in (None, 2.5, True, "3"):
        other = dataclasses.replace(AUTOBLADE, acceleration=bad)
        with pytest.raises(UnsafeJob, match="acceleration"):
            validate_spec(spec("portrait3", "8x12", CutGroup(other, [list(SQUARE)])))
    check = make_write_validator(spec("portrait3", "8x12"))
    for refused in (g("TJ3"), g("J1"), g("!5,1"), g("FX10,1"), g("TG1")):
        with pytest.raises(UnsafeJob):
            check(refused)


def test_the_cameo4_pro_sends_fm1_right_after_tg9_fn0_tb50():
    """CAMEO 4 Pro (UNTESTED), 24 x 24 in mat: the header is TG9 FN0 TB50,0 FM1 in one write,
    as Studio sends it; FM1 anywhere else is refused."""
    assert CAMEO4_PRO.support is Support.UNTESTED
    job = spec("cameo4_pro", "24x24", media=(20.0, 20.0, 560.0, 560.0))
    plan = build_plan(job)
    header = g("TG9", "FN0", "TB50,0", "FM1")
    assert writes(plan) == [
        header,
        g("\\30,30", "Z12162,12162", "J1", "!5,1", "FX10,1", "TJ3", "FC18,1,1", "FE0,1",
          "FF1,1,1", "TF1,1"),
        SQUARE_CUT, TRAILER]
    assert plan.steps[0].writes == [header]
    assert (plan.units_expected, plan.units_enforced) == ((12200, 12200), True)
    check = plan.validator
    check(header)
    for misplaced in (g("FM1"), g("TG9", "FM1"), g("TG9", "FN0", "FM1"), g("FN0", "TB50,0", "FM1"),
                      g("TG9", "FN0", "TB50,0", "FM1", "FM1"), g("\\30,30", "FM1"),
                      g("FM1", "TG9", "FN0", "TB50,0"), g("TG9", "FN0", "TB50,0", "FM0"),
                      g("TG9", "FN0", "TB50,0", " FM1"), g("M600,600", "FM1")):
        with pytest.raises(UnsafeJob):
            check(misplaced)


def test_the_cameo4_pro_12x12_mat_gets_the_cameo4s_bytes_and_no_fm():
    pro = build_plan(spec("cameo4_pro", "12x12", CutGroup(AUTOBLADE, [list(SQUARE)]),
                          CutGroup(PEN, [list(OPEN)]), preview=True))
    cameo4 = build_plan(spec("cameo4", "12x12", CutGroup(AUTOBLADE, [list(SQUARE)]),
                             CutGroup(PEN, [list(OPEN)]), preview=True))
    assert writes(pro) == writes(cameo4)
    assert [s.title for s in pro.steps] == [s.title for s in cameo4.steps]
    assert (pro.units_expected, pro.units_enforced) == ((6100, 6100), True)
    assert pro.profile_id == "cameo4_pro" and cameo4.profile_id == "cameo4"
    with pytest.raises(UnsafeJob, match="never-send"):
        pro.validator(g("TG1", "FN0", "TB50,0", "FM1"))


def test_the_expand_header_stays_one_write_at_the_smallest_write_size():
    """FM1 must share its write with TG9 FN0 TB50,0: at max_write 50 (the smallest the self-check
    takes: one condition block per write) the header is still one write, the setup is split,
    and every write keeps the global rules."""
    small = dataclasses.replace(CAMEO4_PRO, id="pro_small", max_write=50)
    safety.check_profile(small)
    plan = build_plan(spec("pro_small", "24x24", media=(20.0, 20.0, 560.0, 560.0)), small)
    assert plan.steps[0].writes == [g("TG9", "FN0", "TB50,0", "FM1")]
    assert len(plan.steps[2].writes) == 2
    for w in writes(plan):
        assert len(w) <= 50
        safety.check_write(w, small)
        plan.validator(w)
    assert sum(b"FM1" in w for w in writes(plan)) == 1


@pytest.mark.parametrize("machine", ["cameo4", "cameo4_plus", "cameo5", "cameo5_plus"])
def test_models_without_expand_mode_never_take_fm(machine):
    profile = registry.enabled_profile(machine)
    for m in profile.offered_mats():
        plan = build_plan(spec(machine, m.key))
        assert not any(b"FM" in w for w in writes(plan)), (machine, m.key)
        for fm in (g("FM1"), g(f"TG{m.tg}", "FN0", "TB50,0", "FM1"),
                   g("TG9", "FN0", "TB50,0", "FM1")):
            with pytest.raises(UnsafeJob):
                plan.validator(fm)


def test_the_cameo4_plus_15x15_mat():
    """CAMEO 4 Plus (UNTESTED), 15 x 15 in mat (TG8): its own cut area and an INFERRED U."""
    job = spec("cameo4_plus", "15x15", media=(0.0, 0.0, 381.0, 381.0))
    plan = build_plan(job)
    assert writes(plan)[:2] == [
        g("TG8", "FN0", "TB50,0"),
        g("\\30,9", "Z7590,7320", "J1", "!5,1", "FX10,1", "TJ3", "FC18,1,1", "FE0,1", "FF1,1,1",
          "TF1,1")]
    assert (plan.units_expected, plan.units_enforced) == ((7620, 7420), True)
    assert media_box_su(job) == (30, 9, 7590, 7320)
    with pytest.raises(UnsafeJob, match="does not fit"):
        validate_spec(dataclasses.replace(job, media=(0.0, 0.0, 381.1, 381.0)))
    with pytest.raises(UnsafeJob, match="unknown cutting mat"):
        validate_spec(spec("cameo4", "15x15"))


ENABLED = [p.id for p in registry.enabled()]


@pytest.mark.parametrize("machine", ENABLED)
def test_every_enabled_profile_builds_its_own_dialect(machine):
    """A pen job on every offered mat of every profile jobs may use: the header is the
    profile's, the holder form and TJ follow the dialect, every write keeps the global rules,
    and the plan carries the mat's U and the profile's pacing."""
    profile = registry.enabled_profile(machine)
    pen = dataclasses.replace(PEN, holder=profile.holders[-1].number)
    if profile.ranges.acceleration is not None:
        pen = dataclasses.replace(pen, acceleration=profile.ranges.acceleration.hi)
    for m in profile.offered_mats():
        plan = build_plan(spec(machine, m.key, CutGroup(pen, [list(OPEN)])))
        ws = writes(plan)
        assert ws[0] == b"".join(builder.cmd(t) for t in profile.mat_texts(m.key))
        commands = [c for w in ws for c in w[:-1].split(E)]
        assert any(c.startswith(b"J") for c in commands) == \
            (profile.dialect.holders is HolderForm.DUAL)
        assert any(c.startswith(b"TJ") for c in commands) == \
            (profile.ranges.acceleration is not None)
        for w in ws:
            safety.check_write(w, profile)
            assert len(w) <= profile.max_write
        assert plan.profile_id == machine
        assert plan.units_expected == m.expected_u and plan.units_enforced == m.enforce_u
        assert plan.units_enforced == (m.u_basis in (UBasis.KNOWN, UBasis.INFERRED))
        assert plan.batch_bytes == profile.batch_bytes
        assert plan.slowest_su_per_s == profile.slowest_su_per_s
        assert plan.summary["machine"] == machine


# --- refusals ----------------------------------------------------------------------------------
@pytest.mark.parametrize("machine, match", [
    ("cameo9", "unknown machine 'cameo9'"),
    ("", "unknown machine"),
    ("CAMEO4", "unknown machine"),
    ("cameo3", "recognizes this CAMEO 3 but cannot drive it yet"),
    ("curio2", "recognizes this CURIO 2 but cannot drive it yet"),
    ("portrait2", "recognizes this Portrait 2"),
])
def test_unknown_and_recognized_machines_are_refused(machine, match):
    job = spec(machine)
    for refuse in (validate_spec, build_plan, make_write_validator, media_box_su, bind_job):
        with pytest.raises(UnsafeJob, match=match):
            refuse(job)


@pytest.mark.parametrize("machine", [None, 4, ["cameo4"]])
def test_a_spec_without_a_machine_id_is_refused(machine):
    with pytest.raises(UnsafeJob, match="does not name a machine"):
        validate_spec(dataclasses.replace(spec(), machine=machine))


@pytest.mark.parametrize("machine, mat", [
    ("cameo4", "24x24"), ("cameo4", "15x15"), ("cameo4", "12x24"), ("cameo4", "8x12"),
    ("portrait4", "12x12"), ("portrait4", "8x12"), ("portrait3", "8.5x12"),
    ("cameo5", "24x24"), ("cameo5", "8x12"), ("cameo4_plus", "24x24"), ("cameo4_pro", "15x15"),
])
def test_a_mat_the_profile_does_not_have_is_refused(machine, mat):
    with pytest.raises(UnsafeJob, match="unknown cutting mat"):
        build_plan(spec(machine, mat))


@pytest.mark.parametrize("machine, profile", [
    ("cameo5", CAMEO4), ("cameo4", CAMEO5), ("cameo4_pro", CAMEO4), ("cameo4", CAMEO4_PRO),
    ("portrait4", PORTRAIT3), ("cameo3", CAMEO4), ("cameo9", CAMEO4),
])
def test_a_job_for_another_machine_is_refused(machine, profile):
    job = spec(machine)
    for refuse in (validate_spec, build_plan, make_write_validator, media_box_su, bind_job):
        with pytest.raises(UnsafeJob, match="made for the"):
            refuse(job, profile)


def test_the_mismatch_names_both_machines():
    with pytest.raises(UnsafeJob, match="made for the CAMEO 5, but the machine is the CAMEO 4"):
        build_plan(spec("cameo5"), CAMEO4)
    with pytest.raises(UnsafeJob, match="made for the unknown machine 'cameo9', but the machine "
                                        "is the CAMEO 4"):
        build_plan(spec("cameo9"), CAMEO4)


def test_a_passed_profile_must_be_a_machine_profile():
    cameo3 = registry.by_id("cameo3")
    for machine in ("cameo4", "cameo3"):            # a recognized machine says why, in its words
        with pytest.raises(UnsafeJob, match="recognizes this CAMEO 3 but cannot drive it yet"):
            validate_spec(spec(machine), cameo3)
    for other in ("cameo4", CAMEO4.identity, 42):
        with pytest.raises(UnsafeJob, match="not a machine profile"):
            validate_spec(spec(), other)


def test_a_profile_outside_the_registry_must_pass_the_self_check():
    bad_stop = dataclasses.replace(CAMEO4, id="synthetic",
                                   stop=StopSequence(writes=(b"\x1b\x07", b"\x1b\x09"), gap_s=0.3))
    with pytest.raises(UnsafeJob, match="stop"):
        build_plan(spec("synthetic"), bad_stop)
    too_long = dataclasses.replace(CAMEO4, max_write=600)       # same id, not the registry's
    with pytest.raises(UnsafeJob, match="max_write"):
        build_plan(spec(), too_long)
    fine = dataclasses.replace(CAMEO4, id="synthetic")
    assert writes(build_plan(spec("synthetic"), fine)) == writes(build_plan(spec()))
    with pytest.raises(UnsafeJob, match="unknown machine"):
        build_plan(spec("synthetic"))                           # not without the profile


def test_a_mat_with_an_unknown_u_is_offered_only_on_an_untested_profile():
    unknown = dataclasses.replace(c4.MAT_12X12, key="12x12u", tg=1, expected_u=None,
                                  u_basis=UBasis.UNKNOWN, basis="test: U not known")
    tested = dataclasses.replace(CAMEO4, id="synthetic", mats=(c4.MAT_12X12, unknown))
    with pytest.raises(UnsafeJob, match="not offered"):
        build_plan(spec("synthetic", "12x12u"), tested)
    build_plan(spec("synthetic", "12x12"), tested)
    untested = dataclasses.replace(tested, support=Support.UNTESTED,
                                   identity=dataclasses.replace(CAMEO4.identity, lines=(c4.V1,)))
    plan = build_plan(spec("synthetic", "12x12u"), untested)
    assert plan.units_expected is None and plan.units_enforced is False


def test_a_raised_coordinate_limit_accepts_six_digit_moves():
    """A synthetic profile with a 300 in long mat and max_coordinate_su 199 999: its moves take
    six digits, which the CAMEO 4's validator refuses."""
    long_mat = Mat(key="12x300", label="12 × 300 in", tg=2, size_mm=(304.8, 7620.0),
                   area_su=(30, 30, 152_370, 6066), expected_u=(152_400, 6100),
                   u_basis=UBasis.INFERRED, basis="test: a long synthetic mat")
    long = dataclasses.replace(CAMEO4, id="synthetic_long", max_coordinate_su=199_999,
                               mats=(c4.MAT_12X12, long_mat))
    far = [(50.0, 6000.0), (60.0, 6000.0), (60.0, 6010.0)]
    pen = dataclasses.replace(PEN, holder=1)
    job = spec("synthetic_long", "12x300", CutGroup(pen, [far]), media=(40.0, 5990.0, 30.0, 30.0))
    plan = build_plan(job, long)
    assert kinds(plan) == ["write", "units", "write", "write", "write"]
    cut = plan.steps[3].writes[0]
    assert cut == g("M120000,1000", "D120000,1200", "D120200,1200")
    assert plan.max_coordinate_su == 199_999
    check_plan_sequence(plan)
    plan.validator(cut)
    with pytest.raises(UnsafeJob):
        make_write_validator(spec())(g("M120000,1000"))
    with pytest.raises(UnsafeJob, match="malformed"):
        check_plan_sequence(dataclasses.replace(plan, max_coordinate_su=99_999))


def test_a_move_to_the_origin_inside_the_media_is_a_move():
    """A mat whose cut area starts at the origin (synthetic): the path's M0,0 lies inside the
    media, so a D may follow it. The trailer's M0,0 outside the media still may not."""
    origin = dataclasses.replace(c4.MAT_12X12, key="12x12o", area_su=(0, 0, 6066, 6066),
                                 basis="test: a cut area from the origin")
    profile = dataclasses.replace(CAMEO4, id="synthetic_origin", mats=(origin,),
                                  default_mat="12x12o")
    pen = dataclasses.replace(PEN, holder=1)
    plan = build_plan(spec("synthetic_origin", "12x12o", CutGroup(pen, [[(0.0, 0.0), (10.0, 0.0)]]),
                           media=(0.0, 0.0, 100.0, 100.0)), profile)
    assert plan.steps[-2].writes == [g("M0,0", "D0,200")]
    plan.validator(g("M0,0", "D0,200"))
    away = make_write_validator(spec("synthetic_origin", "12x12o", media=(20.0, 20.0, 80.0, 80.0)),
                                profile)
    away(g("\\0,0", "M0,0", "FN0", "TB50,0"))                      # the trailer
    with pytest.raises(UnsafeJob, match="unknown point"):
        away(g("M0,0", "D600,600"))


def test_the_builders_output_must_fit_the_profiles_grammar(monkeypatch):
    """The grammar is a backstop under the builder: a block the dialect does not allow (here TJ4,
    beyond the Portrait 4's 1..3) refuses the whole job."""
    monkeypatch.setattr(builder, "condition_block",
                        lambda s, profile=CAMEO4: [builder.cmd(t) for t in ("!5", "FX10", "TJ4")])
    with pytest.raises(UnsafeJob, match="TJ4.*outside the command grammar"):
        bind_job(spec("portrait4", "8.5x12"))


NEVER_SEND = (b"FB0", b"TB72,0,0", b"TB70", b"TH", b"TD", b"TE", b"ZF1", b"TA", b"SC1", b"TN1",
              b"FD", b"FP", b"FPG", b"FY0", b"FU1", b"PW", b"S0", b"0TH", b" FPG", b"fpg",
              b"tg1", b"APS30", b"ACT1", b"AOC1", b"TS1", b"TR1", b"BZ1,1,1,1,1,1,1,1",
              b"BZK3", b"TZ1", b"TB24,1", b"TB124", b"TB125,0", b"TB1023,1", b" APS30")


@pytest.mark.parametrize("machine", ENABLED)
def test_every_profile_refuses_the_global_never_send_list(machine):
    profile = registry.enabled_profile(machine)
    check = make_write_validator(spec(machine, profile.default_mat,
                                      media=(0.0, 0.0, 100.0, 100.0)))
    for c in NEVER_SEND:
        with pytest.raises(UnsafeJob, match="never-send"):
            check(c + E)
        with pytest.raises(UnsafeJob):
            check(b"M600,600" + E + c + E)
    for refused in (b"\x1b\x09", b"U\x03", b"FG\x03", b"TG0\x03", b"M600,600"):
        with pytest.raises(UnsafeJob):
            check(refused)
    # behind another command's letters or values, with no ETX between: the firmware starts a
    # command there too (FX10,TJ3 runs TJ3), and the refusal names it
    for c in (b"FX10,TB72", b"FN0FP", b"M600,600FB0", b"FX10,tg1", b"FX10,FM1", b"J1APS30"):
        with pytest.raises(UnsafeJob, match="never-send"):
            check(c + E)
