# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The Portrait 3 and Portrait 4 profiles (opencutter.machines.portrait).

Both are UNTESTED (jobs allowed): one holder written in the plain forms with no J, TJ 1..3 on
the Portrait 4 and none on the Portrait 3, whole speeds 1..10, their own TG values with an
UNKNOWN U, the first ESC 15 slot (the second must read 0), no TO or TC, the CAMEO 4's status
digits and ESC 09 twice. FG detection names each of them exactly, never by prefix, and only
with the firmware's four-space tail. Pure checks: device/protocol.py is
imported on the stub Bluetooth of tests/stubs.py only to compare the getters, and nothing
reaches a machine.
"""
import ast
import dataclasses
import itertools
import re
from pathlib import Path

import pytest

import stubs

stubs.install()
from opencutter.device import protocol  # noqa: E402
from opencutter.machines import (CAMEO4, AccelForm, HeaderExtra, HolderForm,  # noqa: E402
                                 Index, Support, ToolReply, UBasis, Verdict, by_fg_model, by_id,
                                 by_usb_id, detect, enabled, enabled_profile, profiles, registry,
                                 safety)
from opencutter.machines import portrait as pt  # noqa: E402
from opencutter.machines.safety import SafetyError, UnsafeProfile  # noqa: E402
from studio_names import FIRMWARE_ADDRESS, STUDIO_NOTATION, studio_names  # noqa: E402

P3, P4 = pt.PORTRAIT3, pt.PORTRAIT4
BOTH = [P3, P4]
SOURCE = Path(pt.__file__).read_text(encoding="utf-8")
ETX = b"\x03"


def ids(p) -> str:
    return p.id


def short(value) -> str | None:
    """Test ids: replies as repr, profiles and lines by pytest's own short id."""
    return repr(value) if isinstance(value, (str, bytes)) else None


def block(p, *, speed=5, force=10, acceleration=None, offset=18, lso=False, overcut=(1, 1),
          blade_depth=None):
    return p.condition_texts(holder=1, speed=speed, force=force, acceleration=acceleration,
                             offset=offset, line_segment_overcut=lso, overcut=overcut,
                             blade_depth=blade_depth)


def write(*texts: str) -> bytes:
    return b"".join(t.encode("ascii") + ETX for t in texts)


# --- registry and the self-check ---------------------------------------------------------------
def test_the_module_declares_both_profiles():
    assert pt.PROFILES == (P3, P4)
    assert [p.id for p in BOTH] == ["portrait3", "portrait4"]
    assert [p.name for p in BOTH] == ["Portrait 3", "Portrait 4"]


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_the_profile_passes_the_safety_self_check(p):
    safety.check_profile(p)
    safety.check_entry(p)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_untested_with_jobs_allowed(p):
    assert p.support is Support.UNTESTED and p.support.jobs_allowed
    assert all(ln.support is Support.UNTESTED and not ln.reason for ln in p.identity.lines)
    assert p in profiles() and p in enabled() and p not in registry.tested()
    assert by_id(p.id) is p and enabled_profile(p.id) is p
    assert p.programs == frozenset()                 # no pinned test programs yet


def test_registry_lookups():
    assert by_fg_model("Portrait 3") is P3 and by_fg_model("Portrait 4") is P4
    for bad in ("PORTRAIT 3", "portrait 3", "Portrait3", "Portrait", "Portrait 3 ", "Portrait 34"):
        assert by_fg_model(bad) not in BOTH, bad
    assert by_usb_id((0x0B4D, 0x113A)) == (P3,)
    assert by_usb_id((0x0B4D, 0x113F)) == (P4,)
    assert "PORTRAIT" in {n.upper() for n in registry.ble_name_prefixes()}
    for p in BOTH:
        assert p.identity.ble_names == ("Portrait",)
        assert p.identity.shared_fg_models == frozenset()


def test_the_portrait_1_and_2_are_not_here():
    texts = set().union(*(p.identity.fg_models | p.identity.shared_fg_models for p in BOTH))
    assert texts == {"Portrait 3", "Portrait 4"}
    usb = set().union(*(p.identity.usb_ids for p in BOTH))
    assert not usb & {(0x0B4D, 0x1123), (0x0B4D, 0x1132)}   # the Portrait 1 and Portrait 2


# --- detection ---------------------------------------------------------------------------------
@pytest.mark.parametrize("reply, p, line, standard", [
    ("Portrait 3 V1.05    ", P3, pt.P3_V1, True),
    ("Portrait 3 V5.02    ", P3, pt.P3_V5, True),
    (b"Portrait 3 V1.05    \x03", P3, pt.P3_V1, True),
    ("Portrait 3 V5.02    \x03", P3, pt.P3_V5, True),
    ("Portrait 4 V1.05    ", P4, pt.P4_V1, True),
    (b"Portrait 4 V1.05    \x03", P4, pt.P4_V1, True),
    ("Portrait 4 V1.04    ", P4, pt.P4_V1, True),
], ids=short)
def test_each_model_is_detected(reply, p, line, standard):
    for index in (None, Index(BOTH)):
        d = detect(reply, index=index)
        assert d.verdict is Verdict.USE and d.profile is p and d.line is line
        assert d.name == p.name and d.support is Support.UNTESTED and not d.tested
        assert d.message == "" and d.reason == "" and d.standard_tail is standard
        assert d.profile.offered_mats(d.support) == p.mats


NEAR_MISSES = [
    "PORTRAIT 3 V1.05    ", "portrait 3 V1.05", "Portrait3 V1.05", "PORTRAIT3 V1.05",
    "Portrait  3 V1.05", "Portrait 3  V1.05", "Portrait 3 PLUS V1.05", "Portrait 3 Plus V1.05",
    "Portrait V1.05", "Portrait 34 V1.05", "Portrait 3- V1.05", "Portrait 3-TEST V1.05",
    "Portrait 3 V1.05 X1Z\x03", "Portrait 3 V1.05 ABC", "Portrait 3 V01.05", "Portrait 3 V1.5",
    "Portrait 3 V1.055", "Portrait 3 v1.05", "Portrait 3 V1,05", "Portrait 3", "Portrait 3 V",
    "Portrait 4V1.05", "Portrait 4\tV1.05", "Portrait 4 V1.05\xa0", "Portrait\xa04 V1.05",
    "Portrait 4 V1.٠٥", "Portrait 2 V1.20    ", "Portrait 5 V1.05", "Silhouette V1.10    ",
    "CAMEO 4 V5.05 Portrait 3 V1.05", "Portrait 3 V1.05 Portrait 4 V1.05",
    b"Portrait 3 V1.05\xff", b"\xffPortrait 3 V1.05",
]


@pytest.mark.parametrize("reply", NEAR_MISSES, ids=repr)
def test_near_misses_never_name_a_portrait(reply):
    d = detect(reply, index=Index([CAMEO4, P3, P4]))
    assert d.verdict is Verdict.UNKNOWN and d.profile is None and d.entry is None
    assert detect(reply).entry not in BOTH           # the whole registry: whatever else it names


@pytest.mark.parametrize("reply, p", [
    ("Portrait 3 V2.00    ", P3), ("Portrait 3 V0.99", P3), ("Portrait 3 V15.02", P3),
    ("Portrait 4 V5.02    ", P4), ("Portrait 4 V2.01", P4),
], ids=short)
def test_an_unlisted_firmware_line_is_recognized_and_refused(reply, p):
    d = detect(reply)
    assert d.verdict is Verdict.RECOGNIZED and d.entry is p and d.profile is None
    assert d.line is None and d.support is Support.RECOGNIZED and not d.tested
    assert d.message.startswith(f"OpenCutter recognizes this {p.name} but cannot drive it yet: ")


def test_the_usb_id_must_agree():
    assert detect("Portrait 3 V1.05    ", usb_id=(0x0B4D, 0x113A)).profile is P3
    assert detect("Portrait 4 V1.05    ", usb_id=(0x0B4D, 0x113F)).profile is P4
    for reply, usb in (("Portrait 3 V1.05", (0x0B4D, 0x113F)),
                       ("Portrait 4 V1.05", (0x0B4D, 0x113A)),
                       ("Portrait 3 V1.05", (0x0B4D, 0x1137)),      # the CAMEO 4's
                       ("CAMEO 4 V5.05", (0x0B4D, 0x113A)),
                       ("Portrait 4 V1.05", (0x0B4D, 0x1132))):     # the Portrait 2's
        d = detect(reply, usb_id=usb)
        assert d.verdict is Verdict.UNKNOWN and "disagree" in d.reason, (reply, usb)


def test_the_cameo4_is_unchanged():
    d = detect("CAMEO 4 V5.05    ")
    assert d.verdict is Verdict.USE and d.profile is CAMEO4 and d.tested


def test_only_the_four_space_tail_names_a_portrait():
    # the firmware pads with exactly four spaces; both models are UNTESTED, so any other
    # padding is UNKNOWN (docs/PROTOCOL.md section 3.4: the exact form below TESTED)
    for reply in ("Portrait 3 V1.05    ", "Portrait 4 V1.05    \x03", b"Portrait 3 V5.02    \x03"):
        d = detect(reply)
        assert d.verdict is Verdict.USE and d.standard_tail is True, reply
    for reply in ("Portrait 3 V1.05", "Portrait 3 V1.05 ", "Portrait 3 V1.05     ",
                  "Portrait 4 V1.05   \x03", "Portrait 4 V1.04", "\x00Portrait 4 V1.05\r\n",
                  "\x00Portrait 4 V1.05    ", "Portrait 4 V1.05    \r\n", b"Portrait 3 V1.05\x03"):
        for index in (None, Index(BOTH)):
            d = detect(reply, index=index)
            assert d.verdict is Verdict.UNKNOWN and d.standard_tail is False, reply
            assert d.profile is None and d.entry is None


def test_the_portrait_2_is_never_taken_for_a_portrait_3_or_4():
    # the same family word and BLE prefix, but its own FG text: recognized and refused
    for reply in ("Portrait 2 V1.20    ", "Portrait 2 V1.20"):
        d = detect(reply)
        assert d.verdict is Verdict.RECOGNIZED and d.entry.id == "portrait2", reply
        assert d.profile is None and d.entry not in BOTH


# --- one holder, plain forms -------------------------------------------------------------------
def test_dialects():
    for p in BOTH:
        assert p.dialect.holders is HolderForm.SINGLE and p.dialect.tool_slots == (0,)
        assert p.dialect.tool_reply is ToolReply.ONE_DIGIT and p.dialect.expand_mode is False
    assert P3.dialect.accel is AccelForm.NONE and P4.dialect.accel is AccelForm.SHARED


def test_portrait3_condition_blocks():
    assert block(P3, blade_depth=1) == ("!5", "FX10", "FC18", "FE0,0", "FF1,1,0", "TF1,1")
    assert block(P3, speed=10, force=7, offset=0, overcut=(0, 0)) == (
        "!10", "FX7", "FC0", "FE0,0", "FF0,0,0")
    assert block(P3, lso=True) == ("!5", "FX10", "FC18", "FE1,0", "FF1,1,0")
    with pytest.raises(ValueError):                  # no TJ on the Portrait 3
        block(P3, acceleration=3)
    with pytest.raises(ValueError):                  # one holder
        P3.condition_texts(holder=2, speed=5, force=10, acceleration=None, offset=18,
                           line_segment_overcut=False, overcut=(1, 1), blade_depth=None)


def test_portrait4_condition_blocks():
    # Studio's single force-and-acceleration command, written as FX then TJ
    assert block(P4, acceleration=3, blade_depth=1) == (
        "!5", "FX10", "TJ3", "FC18", "FE0,0", "FF1,1,0", "TF1,1")
    assert block(P4, acceleration=1) == ("!5", "FX10", "TJ1", "FC18", "FE0,0", "FF1,1,0")
    with pytest.raises(ValueError):                  # the Portrait 4 always gets its TJ
        block(P4, acceleration=None)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_blocks_never_select_a_holder(p):
    accel = None if p.ranges.acceleration is None else 2
    for depth, lso in itertools.product((None, 0, 10), (False, True)):
        texts = block(p, acceleration=accel, blade_depth=depth, lso=lso)
        assert not [t for t in texts if t.startswith("J")]
        assert [t for t in texts if t.startswith(("!", "FX", "TJ"))] == [
            t for t in texts if t.startswith(("!", "FX", "TJ")) and "," not in t]
        assert texts[-1] == ("TF%d,1" % depth if depth is not None else "FF1,1,0")
        safety.check_write(write(*texts), p)


GRAMMAR_REFUSED = [
    b"J1", b"J2", b"!5,1", b"FX10,1", b"FC18,1,1", b"FE0,1", b"FF1,1,1", b"TF1,2", b"TF1",
    b"!0", b"!11", b"!30", b"!05", b"!2.5", b"!5.0", b"APS10", b"APS1", b"FX0", b"FX34", b"FX010",
    b"FC46", b"FC99", b"FE2,0", b"FF10,0,0", b"FF0,10,0", b"TF11,1", b"TG0", b"TG1", b"TG2",
    b"TG5", b"TG7", b"TG9", b"TG13", b"FM0", b"FM1", b"TO", b"TC", b"TR1", b"TR0,1", b"TS0",
    b"TS1", b"BZ", b"SC", b"TA", b"TM", b"TN", b"FP", b"FPG", b"fx10", b"tg3", b"tg11",
    # Studio's combined force-and-acceleration command: OpenCutter writes FX and TJ apart
    b"FX10,TJ3", b"TJ3,1", b"TJ3,0",
    # TF: only the AutoBlade's own form; a third value makes the Portrait 4 forget its depth
    b"TF1,1,1", b"TF1,0", b"TF1,1,0", b"FC18,1", b"FC18,1,0",
    # the rest of the never-send list: feeder, barcode, calibration, persistent writes
    b"FH", b"AF0", b"AH0", b"ACT1,1,1,1,1", b"TB30", b"BM", b"RD", b"TB72", b"TB70", b"FB",
    b"FD", b"FY", b"FU", b"ZF1,1", b"TH", b"TD", b"TE", b"H", b"TG03", b"TG3,0",
]


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_command_grammar(p):
    g = p.setting_grammar()
    for s in range(0, 32):
        assert bool(g.fullmatch(b"!%d" % s)) == (1 <= s <= 10), s
    for f in range(0, 41):
        assert bool(g.fullmatch(b"FX%d" % f)) == (1 <= f <= 33), f
    for o in range(0, 100):
        assert bool(g.fullmatch(b"FC%d" % o)) == (o <= 45), o
    for a in range(0, 11):
        assert bool(g.fullmatch(b"TJ%d" % a)) == (p is P4 and 1 <= a <= 3), a
    for d in range(0, 12):
        assert bool(g.fullmatch(b"TF%d,1" % d)) == (d <= 10), d
    for s, e in itertools.product(range(11), range(11)):
        assert bool(g.fullmatch(b"FF%d,%d,0" % (s, e))) == (s <= 9 and e <= 9), (s, e)
    for t in range(0, 14):
        assert bool(g.fullmatch(b"TG%d" % t)) == (t == p.mats[0].tg), t
    for c in (b"FE0,0", b"FE1,0", b"FN0", b"TB50,0", b"\\30,30", b"\\0,0"):
        assert g.fullmatch(c), c
    for c in GRAMMAR_REFUSED:
        assert not g.fullmatch(c), c


# --- ranges, speed -----------------------------------------------------------------------------
def test_ranges():
    common = {"speed": (1, 10), "force": (1, 33), "offset": (0, 45), "blade_depth": (0, 10),
              "passes": (1, 10)}
    assert P3.ranges.table() == common                       # no acceleration entry: no TJ
    assert P4.ranges.table() == {**common, "acceleration": (1, 3)}
    assert P3.ranges.acceleration is None
    for p in BOTH:
        assert p.ranges.overcut.as_tuple() == (0, 9)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_speed_is_whole_numbers_and_never_aps(p):
    assert p.speed_text(5) == "5" and p.speed_text(10) == "10"
    for bad in (2.5, 5.0, "5", True, None):
        with pytest.raises(ValueError):
            p.speed_text(bad)
    assert p.speed_mm_s(10) == 100.0 and p.slowest_su_per_s == 200.0
    assert not p.setting_grammar().fullmatch(b"APS10")
    assert "APS" not in "".join(block(p, acceleration=None if p is P3 else 3))


# --- holders and tools -------------------------------------------------------------------------
P3_TOOLS = {0: "No Tool Detected", 1: "Ratchet Blade", 2: "AutoBlade", 3: "Deep-Cut Blade",
            4: "Kraft Blade, 2mm", 5: "Rotary Blade", 6: "Punch Tool", 7: "Sketch Pen"}


def test_tool_tables():
    assert P3.tool_names() == {1: P3_TOOLS}
    assert P4.tool_names() == {1: {**P3_TOOLS, 8: "Foil Tool"}}
    for p in BOTH:
        (h,) = p.holders
        assert (h.number, h.label) == (1, "Tool")
        assert p.blade_codes() == {1, 2, 3, 4, 5}
        assert p.unsupported_codes() == {5, 6}               # never checked, as on the CAMEO 4
        assert [(t.code, t.name) for t in h.tools if t.depth] == [(2, "AutoBlade")]
        assert h.tool(0).usable is False and h.tool(7).usable is True
    for code in (8, 9, 10):
        assert P3.tool(1, code) is None, code                # reserved: fail closed
    for code in (9, 10):
        assert P4.tool(1, code) is None, code
    assert P4.tool(1, 8).usable and not P4.tool(1, 8).blade


def test_tool_flags_and_actions():
    # Studio's first-slot list is the CAMEO 4's Tool 1 list, value for value; the Portrait 4's
    # adds the Foil Tool exactly as the CAMEO 4's Tool 2 has it
    assert P3.holders[0].tools == CAMEO4.holder(1).tools
    assert P4.holders[0].tools == CAMEO4.holder(1).tools + (CAMEO4.tool(2, 8),)
    flags = {t.code: (t.blade, t.depth, t.usable, t.action) for t in P4.holders[0].tools}
    assert flags == {
        0: (False, False, False, "Cut"), 1: (True, False, True, "Cut"),
        2: (True, True, True, "Cut"), 3: (True, False, True, "Cut"),
        4: (True, False, True, "Kraft Cut"), 5: (True, False, False, "Rotary Cut"),
        6: (False, False, False, "Punch"), 7: (False, False, True, "Sketch"),
        8: (False, False, True, "Foil"),
    }


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_the_tool_check_reads_the_first_slot(p):
    assert p.holder_codes(" 2, 0") == {1: 2}
    codes = p.holder_codes(" 0, 0")                          # no tool at all
    assert codes == {1: 0} and p.tool(1, codes[1]).usable is False
    # the unused second slot must read 0: anything else fails closed, never a guess
    for reply in (" 7, 3", " 0, 2", " 2, 2", " 0, 7", " 9, 9"):
        assert p.parse_tools(reply) is not None and p.holder_codes(reply) is None, reply
    assert p.tool(1, p.holder_codes(" 9, 0")[1]) is None     # reserved: no tool, refused
    eight = p.tool(1, p.holder_codes(" 8, 0")[1])            # reserved on the Portrait 3 only
    assert (eight.name if eight else None) == ("Foil Tool" if p is P4 else None)
    for reply in (" 10, 0", " 0, 10", "2, 0", " 2,0", " 2, 0 ", " 2, 0\x03", "2", "", None,
                  b" 2, 0", " ٢, 0"):
        assert p.holder_codes(reply) is None, reply              # fail closed


# --- mats --------------------------------------------------------------------------------------
@pytest.mark.parametrize("p, mat, key, label, tg, size, area, window, feed", [
    (P3, pt.MAT_8X12, "8x12", "8 × 12 in", 3, (203.2, 304.8), (30, 30, 6066, 4034),
     (6100, 4120), 13.5),
    (P4, pt.MAT_8_5X12, "8.5x12", "8.5 × 12 in", 11, (215.9, 304.8), (30, 30, 6066, 4288),
     (6100, 4320), 16.0),
], ids=["portrait3", "portrait4"])
def test_mats(p, mat, key, label, tg, size, area, window, feed):
    assert p.mats == (mat,) and p.default_mat == mat.key and p.mat(mat.key) is mat
    assert (mat.key, mat.label, mat.feed_mm, mat.grid_in) == (key, label, feed, None)
    assert (mat.tg, mat.size_mm, mat.area_su) == (tg, size, area)
    # the area is the mat (width x, length y in mm, 20 units per mm) minus 30 units each side
    assert area == (30, 30, round(size[1] * 20) - 30, round(size[0] * 20) - 30)
    # the area stays inside the firmware's window for this TG (305 x 206 or 305 x 216 mm)
    assert area[2] < window[0] and area[3] < window[1]
    assert mat.u_basis is UBasis.UNKNOWN and mat.expected_u is None and not mat.enforce_u
    assert mat.header_extra is None
    assert mat.offered(Support.UNTESTED) and not mat.offered(Support.TESTED)
    assert not mat.offered(Support.RECOGNIZED)
    assert p.offered_mats() == (mat,)
    assert p.mat_texts(mat.key) == (f"TG{tg}", "FN0", "TB50,0")
    assert p.area_texts(mat.key) == ("\\30,30", f"Z{area[2]},{area[3]}")
    assert p.trailer_texts() == CAMEO4.trailer_texts()
    for texts in (p.mat_texts(mat.key), p.area_texts(mat.key), p.trailer_texts()):
        safety.check_write(write(*texts), p)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_only_the_portraits_own_tg_values(p):
    assert all(m.tg != 1 for m in p.mats)                    # the CAMEO 4's 12 x 12 in mat
    assert p.mat("12x12") is None and p.mat("6x8") is None
    own = {m.tg for m in p.mats}
    for tg in range(0, 14):
        data = b"TG%d\x03FN0\x03TB50,0\x03" % tg
        if tg in own:
            safety.check_write(data, p)
        else:
            with pytest.raises(SafetyError):
                safety.check_write(data, p)
    with pytest.raises(ValueError):
        p.mat_texts("12x12")


# --- status, queries, stop ---------------------------------------------------------------------
@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_status_digits(p):
    assert p.status.as_dict() == {"0": "Ready", "1": "Busy", "2": "Load Media", "3": "Paused",
                                  "4": "Cancelling", "5": "Rejected"}
    assert (p.status.ready, p.status.busy, p.status.load) == ("0", "1", "2")
    assert p.status.unloaded_after_header is True
    for reply in ("6", "7", "10", " 0", "0 ", "0\x03", "", None, 0):
        assert not p.status.is_status(reply), reply           # unknown digits abort the step


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_queries(p):
    q = p.queries
    assert q.esc == {0x05, 0x10, 0x15, 0x16}
    assert q.text == {b"FG", b"TB71", b"FA", b"FQ0", b"FQ2", b"TI", b"G", b"[", b"U"}
    assert q.text == set(protocol.SAFE_TEXT_QUERIES) - {b"TO", b"TC"}
    assert b"TI" in q.text and q.max_write == 20
    for ok in (b"\x1b\x05", b"\x1b\x10", b"\x1b\x15", b"\x1b\x16", b"FG\x03", b"TI\x03", b"U\x03",
               b"[\x03", b"TB71\x03"):
        safety.check_query(ok, p)
    for bad in (b"TO\x03", b"TC\x03", b"\x1b\x11", b"\x1b\x12", b"\x1b\x13", b"\x1b\x07",
                b"\x1b\x09", b"\x1b\x00", b"FPG\x03", b"APS\x03", b"fg\x03", b"TG3\x03"):
        with pytest.raises(SafetyError):
            safety.check_query(bad, p)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_stop_is_esc09_twice(p):
    assert p.stop.writes == (b"\x1b\x09", b"\x1b\x09") and p.stop.gap_s == 0.3
    assert all(safety.is_stop_write(w) for w in p.stop.writes)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_transport_values(p):
    assert (p.unit_mm, p.max_write, p.batch_bytes, p.max_coordinate_su) == (0.05, 182, 1024,
                                                                           99_999)
    assert p.lowering_prefixes == (b"D", b"TF")
    assert p.fg_claims_session is True and p.power_cycle_to_connect is True


# --- FM: never on a Portrait -------------------------------------------------------------------
@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_fm_is_never_sent(p):
    key, tg = p.mats[0].key, p.mats[0].tg
    assert not safety.expand_allowed(p)
    assert "FM1" not in p.mat_texts(key)
    for data in (b"TG%d\x03FM1\x03FN0\x03TB50,0\x03" % tg, b"FM1\x03", b"FM0\x03",
                 b"TG9\x03FM1\x03"):
        with pytest.raises(SafetyError):
            safety.check_write(data, p)
    expand = dataclasses.replace(p.mats[0], header_extra=HeaderExtra.EXPAND)
    for bad in (dataclasses.replace(p, mats=(expand,)),
                dataclasses.replace(p, mats=(expand,),
                                    dialect=dataclasses.replace(p.dialect, expand_mode=True))):
        with pytest.raises(UnsafeProfile, match="FM1"):
            safety.check_profile(bad)
    with pytest.raises(UnsafeProfile, match="pinned write"):
        safety.check_profile(p, pinned=[b"TG%d\x03FM1\x03FN0\x03TB50,0\x03" % tg])


# --- the module itself -------------------------------------------------------------------------
def test_the_module_imports_only_profile():
    tree = ast.parse(SOURCE)
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert [(n.module, n.level) for n in imports if isinstance(n, ast.ImportFrom)] == [
        ("__future__", 0), ("profile", 1)]
    assert not [n for n in imports if isinstance(n, ast.Import)]


@pytest.mark.parametrize("pattern", [
    STUDIO_NOTATION.pattern, r"\b[CKP]:\d", FIRMWARE_ADDRESS.pattern,
    r"[A-Za-z]+\.md:\d", r"\bLJ d\d", r"RS §", r"\bAA X\d",   # research references
])
def test_the_module_holds_no_studio_notation(pattern):
    assert not re.search(pattern, SOURCE), pattern


def test_the_module_holds_no_studio_names():
    """Studio's class, enum and column names, kept as digests in tests/studio_names.py."""
    assert not studio_names(SOURCE, profile_module=True)
