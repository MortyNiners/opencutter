# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Builder tests: units, command order, chunking, overlap, and golden byte streams.

The golden streams are the writes of the fixed test programs of the project's earlier reference
prototype (pen-test with force 1 and 7, cut-test). The cut-test stream ran on a real CAMEO 4.
"""
import pytest

from opencutter.job import builder
from opencutter.job.builder import (chunk, condition_block, cutter_contours, doc_to_cutter,
                                    header, path, round_away, su, trailer, with_overlap)
from opencutter.job.plan import build_plan
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings

E = b"\x03"
HEADER_W = b"TG1\x03FN0\x03TB50,0\x03"
TRAILER_W = b"\\0,0\x03M0,0\x03FN0\x03TB50,0\x03"
PEN_TEST = {force: [
    HEADER_W,
    b"\\30,30\x03Z6066,6066\x03J1\x03!1,1\x03FX%d,1\x03TJ1\x03FC0,1,1\x03FE0,1\x03FF0,0,1\x03" % force,
    b"M200,200\x03D600,200\x03D600,600\x03D200,600\x03D200,200\x03M200,100\x03D300,100\x03",
    TRAILER_W,
] for force in (1, 7)}
CUT_TEST_SETUP = (b"\\30,30\x03Z6066,6066\x03J1\x03!5,1\x03FX10,1\x03TJ3\x03FC18,1,1\x03FE0,1"
                  b"\x03FF1,1,1\x03TF1,1\x03")
CUT_TEST_PREVIEW = [b"M1016,2794\x03", b"M1524,3302\x03"]      # two tool-up preview steps
CUT_TEST_CUT = (b"M1016,2814\x03D1016,2794\x03D1524,2794\x03D1524,3302\x03D1016,3302\x03"
                b"D1016,2794\x03D1036,2794\x03")
CUT_TEST = [HEADER_W, CUT_TEST_SETUP, *CUT_TEST_PREVIEW, CUT_TEST_CUT, TRAILER_W]


def pen(force=1):
    return ToolSettings(holder=1, tool_code=7, speed=1, force=force, acceleration=1, offset=0,
                        line_segment_overcut=False, overcut=(0, 0), blade_depth=None)


def oracal_651():
    return ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                        line_segment_overcut=False, overcut=(1, 1), blade_depth=1)


def writes(plan):
    return [w for s in plan.steps if s.kind == "write" for w in s.writes]


# --- units and coordinates ----------------------------------------------------------------------
def test_round_away_and_su():
    assert [round_away(v) for v in (0.5, 1.5, 2.5, -0.5, -2.5, 0.49)] == [1, 2, 3, -1, -3, 0]
    assert su(0.05) == 1 and su(1) == 20 and su(25.4) == 508 and su(139.7) == 2794
    assert su(304.8) == 6096


def test_doc_to_cutter_is_feed_then_carriage():
    assert doc_to_cutter(10, 30) == (600, 200)       # a = su(y) feed, b = su(x) carriage


# --- commands ------------------------------------------------------------------------------------
def test_header_and_trailer():
    assert b"".join(header("12x12")) == HEADER_W + b"\\30,30\x03Z6066,6066\x03"
    assert b"".join(trailer()) == TRAILER_W


def test_condition_block_order_and_tf_last():
    block = [c[:-1] for c in condition_block(oracal_651())]
    assert block == [b"J1", b"!5,1", b"FX10,1", b"TJ3", b"FC18,1,1", b"FE0,1", b"FF1,1,1", b"TF1,1"]
    kraft = ToolSettings(holder=2, tool_code=4, speed=6, force=8, acceleration=1, offset=68,
                         line_segment_overcut=True, overcut=(1, 1), blade_depth=None)
    assert b"".join(condition_block(kraft)) == b"J2\x03!6,2\x03FX8,2\x03TJ1\x03FC68,1,2\x03FE1,2\x03FF1,1,2\x03"


def test_path_repeats_passes():
    sq = [(200, 200), (600, 200), (600, 600)]
    one = path([sq])
    assert one == [b"M200,200\x03", b"D600,200\x03", b"D600,600\x03"]
    assert path([sq], passes=3) == one * 3


# --- chunking ---------------------------------------------------------------------------------
def test_chunk_splits_only_at_etx():
    cmds = [builder.cmd(f"D{1000 + i},{2000 + i}") for i in range(100)]
    ws = chunk(cmds)
    assert len(ws) > 1
    assert all(len(w) <= builder.MAX_WRITE and w.endswith(E) for w in ws)
    assert b"".join(ws) == b"".join(cmds)
    starts = {c[:1] for w in ws for c in w.split(E)[:-1]}
    assert starts == {b"D"}                            # no command was cut in two
    assert len(ws[0]) + len(cmds[0]) > builder.MAX_WRITE   # greedy: writes are filled up


def test_chunk_refuses_a_command_longer_than_a_write():
    with pytest.raises(ValueError):
        chunk([b"M" + b"1" * 200 + E])
    assert chunk([]) == []


# --- contours ---------------------------------------------------------------------------------
def test_closed_blade_contour_gets_studio_overlap():
    sq = [(0, 0), (508, 0), (508, 508), (0, 508), (0, 0)]
    assert with_overlap(sq) == [(0, 20)] + sq + [(20, 0)]


def test_overlap_walks_over_short_edges():
    # closing edge only 10 SU long: the lead-in continues on the edge before it
    poly = [(0, 0), (500, 0), (500, 500), (10, 500), (0, 490), (0, 10), (0, 0)]
    out = with_overlap(poly)
    assert out[:2] == [(0, 20), (0, 10)] and out[-1] == (20, 0)
    assert out[2:-1] == poly


def test_cutter_contours_overlap_only_for_closed_blade_paths():
    closed = [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 10.0)]
    opened = [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0)]
    blade = cutter_contours(CutGroup(oracal_651(), [closed, opened]))
    assert len(blade[0]) == len(closed) + 2 and len(blade[1]) == len(opened)
    drawn = cutter_contours(CutGroup(pen(), [closed]))
    assert drawn == [[doc_to_cutter(x, y) for x, y in closed]]


def test_cutter_contours_drop_repeats_and_collapsed_paths():
    g = CutGroup(pen(), [[(10.0, 10.0), (10.0, 10.0), (10.01, 10.0), (20.0, 10.0)],
                         [(30.0, 30.0), (30.01, 30.01)]])
    assert cutter_contours(g) == [[(200, 200), (200, 400)]]


def test_test_cut_contours():
    tri, sq = builder.test_cut_contours(20.0, 30.0)
    assert tri[0] == tri[-1] and sq[0] == sq[-1]
    assert min(p[0] for p in sq) == 20 and max(p[0] for p in sq) == 30
    assert min(p[1] for p in sq) == 30 and max(p[1] for p in sq) == 40
    assert all(20 < x < 30 and 30 < y < 40 for x, y in tri)


# --- golden streams of the reference test programs -----------------------------------------------
@pytest.mark.parametrize("force", [1, 7])
def test_pen_test_stream_is_reproduced(force):
    square = [(10.0, 10.0), (10.0, 30.0), (30.0, 30.0), (30.0, 10.0), (10.0, 10.0)]
    tick = [(5.0, 10.0), (5.0, 15.0)]
    spec = JobSpec(mat="12x12", media=(0.0, 0.0, 304.8, 304.8),
                   groups=[CutGroup(pen(force), [square, tick])], preview_outline=False)
    plan = build_plan(spec)
    assert [s.kind for s in plan.steps] == ["write", "units", "write", "write", "write"]
    assert plan.steps[1].writes == [b"U\x03"]
    assert writes(plan) == PEN_TEST[force]


def cut_test_spec(preview):
    # one 1-inch grid square on a sheet covering the full width and 5 squares down
    square = [(139.7, 50.8), (139.7, 76.2), (165.1, 76.2), (165.1, 50.8), (139.7, 50.8)]
    return JobSpec(mat="12x12", media=(0.0, 0.0, 304.8, 127.0),
                   groups=[CutGroup(oracal_651(), [square])], preview_outline=preview)


def test_cut_test_stream_is_reproduced():
    plan = build_plan(cut_test_spec(preview=False))
    assert writes(plan) == [w for w in CUT_TEST if w not in CUT_TEST_PREVIEW]
    assert plan.required_tools == {1: 2}


def test_cut_test_with_preview_traces_the_same_corners():
    plan = build_plan(cut_test_spec(preview=True))
    preview = plan.steps[3]
    assert preview.writes == [b"M1016,2794\x03M1016,3302\x03M1524,3302\x03M1524,2794\x03M1016,2794\x03"]
    assert all(w in preview.writes[0] for w in CUT_TEST_PREVIEW)
    assert [w for s in plan.steps if s is not preview for w in s.writes if s.kind == "write"] == \
        [w for w in CUT_TEST if w not in CUT_TEST_PREVIEW]
