# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Validator tests: the spec checks and the byte-level write validator."""
import dataclasses
import math

import pytest

from opencutter.job import validate
from opencutter.job.plan import build_plan
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings
from opencutter.job.validate import UnsafeJob, make_write_validator, media_box_su, validate_spec

MEDIA = (20.0, 20.0, 100.0, 80.0)                     # x 20..120 mm, y 20..100 mm
SQUARE = [(30.0, 30.0), (60.0, 30.0), (60.0, 60.0), (30.0, 60.0), (30.0, 30.0)]


def autoblade(**kw):
    s = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                     line_segment_overcut=False, overcut=(1, 1), blade_depth=1)
    return dataclasses.replace(s, **kw)


def job(settings=None, paths=None, **kw):
    return JobSpec(mat="12x12", media=MEDIA,
                   groups=[CutGroup(settings or autoblade(), paths or [list(SQUARE)])], **kw)


# --- validate_spec ------------------------------------------------------------------------------
def test_valid_spec_passes():
    validate_spec(job())


@pytest.mark.parametrize("point", [(19.0, 50.0), (121.0, 50.0), (50.0, 19.0), (50.0, 100.6)])
def test_point_outside_media_is_rejected(point):
    with pytest.raises(UnsafeJob, match="outside the media"):
        validate_spec(job(paths=[[(50.0, 50.0), point]]))


def test_media_tolerance_is_half_a_millimetre():
    validate_spec(job(paths=[[(19.5, 19.6), (120.5, 100.5)]]))


def test_point_outside_cut_area_is_rejected():
    spec = JobSpec(mat="12x12", media=(0.0, 0.0, 304.8, 304.8),
                   groups=[CutGroup(autoblade(), [[(1.0, 50.0), (50.0, 50.0)]])])
    with pytest.raises(UnsafeJob, match="cut area"):
        validate_spec(spec)


@pytest.mark.parametrize("point", [(math.nan, 50.0), (50.0, math.inf), (True, 50.0), ("50", 50.0)])
def test_non_finite_or_non_numeric_point_is_rejected(point):
    with pytest.raises(UnsafeJob):
        validate_spec(job(paths=[[(50.0, 50.0), point]]))


@pytest.mark.parametrize("field, value", [
    ("speed", 0), ("speed", 31), ("force", 0), ("force", 34), ("acceleration", 0),
    ("acceleration", 11), ("offset", -1), ("offset", 100), ("overcut", (10, 0)),
    ("overcut", (0, -1)), ("overcut", (1,)), ("blade_depth", 11), ("blade_depth", -1),
    ("passes", 0), ("passes", 11), ("speed", True), ("speed", 5.0), ("force", "10"),
    ("line_segment_overcut", 1), ("holder", 3), ("holder", 0),
])
def test_out_of_range_settings_are_rejected(field, value):
    with pytest.raises(UnsafeJob):
        validate_spec(job(autoblade(**{field: value})))


def test_tf_only_for_autoblade_in_holder_1():
    with pytest.raises(UnsafeJob, match="Blade Depth"):
        validate_spec(job(autoblade(holder=2)))
    with pytest.raises(UnsafeJob, match="Blade Depth"):
        validate_spec(job(autoblade(tool_code=1)))
    validate_spec(job(autoblade(holder=2, blade_depth=None)))


@pytest.mark.parametrize("holder, code", [(1, 0), (1, 8), (2, 0), (2, 9), (1, -1)])
def test_tool_code_must_be_valid_for_the_holder(holder, code):
    with pytest.raises(UnsafeJob, match="tool code"):
        validate_spec(job(autoblade(holder=holder, tool_code=code, blade_depth=None)))


def test_one_holder_cannot_need_two_tools():
    pen = autoblade(tool_code=7, offset=0, overcut=(0, 0), blade_depth=None)
    spec = JobSpec(mat="12x12", media=MEDIA,
                   groups=[CutGroup(pen, [list(SQUARE)]), CutGroup(autoblade(), [list(SQUARE)])])
    with pytest.raises(UnsafeJob, match="two different tools"):
        validate_spec(spec)


def test_unrecognized_tool_is_not_allowed_with_tf():
    with pytest.raises(UnsafeJob, match="AutoBlade detected"):
        validate_spec(job(allow_unrecognized_tool={1: True}))
    validate_spec(job(allow_unrecognized_tool={2: True}))
    pen = autoblade(tool_code=7, offset=0, overcut=(0, 0), blade_depth=None)
    validate_spec(job(pen, allow_unrecognized_tool={1: True}))
    with pytest.raises(UnsafeJob):
        validate_spec(job(allow_unrecognized_tool={3: True}))


@pytest.mark.parametrize("media", [(-1.0, 0.0, 10.0, 10.0), (300.0, 0.0, 10.0, 10.0),
                                   (0.0, 0.0, 0.0, 10.0), (0.0, 0.0, 10.0, -5.0),
                                   (0.0, 0.0, 304.9, 10.0), [0.0, 0.0, 10.0, 10.0]])
def test_media_must_fit_the_mat(media):
    with pytest.raises(UnsafeJob):
        validate_spec(dataclasses.replace(job(), media=media))


def test_unknown_mat_and_empty_jobs_are_rejected():
    with pytest.raises(UnsafeJob, match="mat"):
        validate_spec(dataclasses.replace(job(), mat="12x24"))
    with pytest.raises(UnsafeJob):
        validate_spec(dataclasses.replace(job(), groups=[]))
    with pytest.raises(UnsafeJob):
        validate_spec(job(paths=[[(50.0, 50.0)]]))
    with pytest.raises(UnsafeJob):
        validate_spec(JobSpec(mat="12x12", media=MEDIA, groups=[CutGroup(autoblade(), [])]))


def test_point_limit(monkeypatch):
    monkeypatch.setattr(validate, "MAX_POINTS", 9)
    validate_spec(job(paths=[list(SQUARE), list(SQUARE)[:4]]))
    with pytest.raises(UnsafeJob, match="points"):
        validate_spec(job(paths=[list(SQUARE), list(SQUARE)]))


# --- write validator ------------------------------------------------------------------------------
@pytest.fixture
def check():
    return make_write_validator(job())


def test_accepts_every_write_of_its_own_plan(check):
    plan = build_plan(job())
    for step in plan.steps:
        if step.kind == "write":
            for w in step.writes:
                check(w)


def test_origin_return_is_the_only_move_outside_the_media(check):
    check(b"\\0,0\x03M0,0\x03")
    for bad in (b"D0,0\x03", b"M0,1\x03", b"M389,600\x03", b"D600,2411\x03"):
        with pytest.raises(UnsafeJob):
            check(bad)
    a0, b0, a1, b1 = media_box_su(job())
    check(b"M%d,%d\x03D%d,%d\x03" % (a0, b0, a1, b1))
    with pytest.raises(UnsafeJob, match="outside the media"):
        check(b"D%d,%d\x03" % (a1 + 1, b1))


@pytest.mark.parametrize("data", [
    b"SO0\x03", b"L0\x03", b"H\x03", b"U\x03", b"FG\x03", b"TG2\x03", b"FN1\x03", b"J2\x03",
    b"J0\x03", b"!5,2\x03", b"BE1,2\x03", b"M200.5,600\x03", b"M0600,600\x03", b"M-1,600\x03",
    b"M600, 600\x03", b"D600,600,700,700\x03", b"m600,600\x03", b"\x03", b"M600,600\x03\x03",
    b"",
])
def test_rejects_unknown_or_malformed_commands(check, data):
    with pytest.raises(UnsafeJob):
        check(data)


@pytest.mark.parametrize("data", [
    b"FB0,0\x03", b"TB72,0,0\x03", b"TB70\x03", b"THname\x03", b"TD1,1\x03", b"TE\x03",
    b"ZF1,1\x03", b"TA1,1\x03", b"SC1,0,0\x03", b"TN1\x03", b"FD\x03", b"FPGRFCC1\x03",
    b"FM1\x03", b"FY0\x03", b"FU100\x03", b"0TH\x03", b",TB72,1,1\x03", b"q\x03",
    b"S0030000FC\x03", b"PW\x03",
])
def test_rejects_never_send_commands(check, data):
    with pytest.raises(UnsafeJob, match="never-send"):
        check(data)


@pytest.mark.parametrize("data", [b"\x1b\x09", b"\x1b\x05", b"M600,600\x03\x1b\x09",
                                  b"M600\x1b,600\x03"])
def test_rejects_esc(check, data):
    with pytest.raises(UnsafeJob, match="ESC"):
        check(data)


def test_write_length_limit(check):
    move = b"M1000,1000\x03"
    check(move * (182 // len(move)))
    with pytest.raises(UnsafeJob, match="bytes"):
        check(move * (182 // len(move) + 1))
    with pytest.raises(UnsafeJob):
        check(b"M1000,1000")                            # no ETX
    with pytest.raises(UnsafeJob):
        check("M1000,1000\x03")                         # not bytes


def test_rejects_writes_of_another_spec(check):
    faster = build_plan(job(autoblade(speed=10)))
    elsewhere = build_plan(dataclasses.replace(
        job(paths=[[(150.0, 150.0), (200.0, 150.0)]]), media=(140.0, 140.0, 100.0, 100.0)))
    pen = build_plan(job(autoblade(holder=2, tool_code=7, offset=0, overcut=(0, 0),
                                   blade_depth=None)))
    for other in (faster.steps[2].writes[0], elsewhere.steps[4].writes[0],
                  pen.steps[2].writes[0]):
        with pytest.raises(UnsafeJob):
            check(other)
