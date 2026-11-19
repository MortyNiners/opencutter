# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The CAMEO 4 Plus and CAMEO 4 Pro profiles (opencutter.machines.cameo4_plus_pro).

Both are UNTESTED (jobs allowed) and run the CAMEO 4's firmware, so everything but their
identity, mats and expand mode must equal the CAMEO 4 profile. Detection names each one only
for its exact FG model text, with the firmware's four-space tail; FM is sent only as FM1 after
TG9 FN0 TB50,0 on the Pro's 24 x 24 in mat, never to the Plus. Pure checks on the machines
package: the stub Bluetooth of tests/stubs.py is installed first, and nothing reaches a
machine.
"""
import dataclasses
import re
from pathlib import Path

import pytest

import stubs

stubs.install()
from opencutter.machines import (CAMEO4, HeaderExtra, Index, Support, UBasis,  # noqa: E402
                                 Verdict, ble_name_prefixes, by_fg_model, by_id, by_usb_id,
                                 detect, enabled, enabled_profile, entries, registry, safety)
from opencutter.machines import cameo4 as c4  # noqa: E402
from opencutter.machines import cameo4_plus_pro as pp  # noqa: E402
from opencutter.machines.safety import SafetyError, UnsafeProfile  # noqa: E402
from studio_names import FIRMWARE_ADDRESS, STUDIO_NOTATION, studio_names  # noqa: E402

PLUS, PRO = pp.CAMEO4_PLUS, pp.CAMEO4_PRO
BOTH = [PLUS, PRO]
ETX = b"\x03"
SOURCE = Path(pp.__file__).read_text(encoding="utf-8")


def write(*texts: str) -> bytes:
    return b"".join(t.encode("ascii") + ETX for t in texts)


def replace(obj, **changes):
    return dataclasses.replace(obj, **changes)


# --- the registry --------------------------------------------------------------------------------
def test_both_are_registered_untested_and_enabled():
    assert pp.PROFILES == (PLUS, PRO)
    for p in BOTH:
        assert p in entries() and p in enabled() and p not in registry.tested()
        assert by_id(p.id) is p and enabled_profile(p.id) is p
        assert p.support is Support.UNTESTED and p.support.jobs_allowed
    assert (PLUS.id, PLUS.name) == ("cameo4_plus", "CAMEO 4 Plus")
    assert (PRO.id, PRO.name) == ("cameo4_pro", "CAMEO 4 Pro")
    assert registry.tested() == (CAMEO4,)          # only the CAMEO 4 has cut on hardware


def test_fg_texts_and_usb_ids():
    assert PLUS.identity.fg_models == {"CAMEO 4 PLUS"}
    assert PRO.identity.fg_models == {"CAMEO 4 PRO"}
    assert by_fg_model("CAMEO 4 PLUS") is PLUS and by_fg_model("CAMEO 4 PRO") is PRO
    assert by_fg_model("CAMEO 4") is CAMEO4
    for text in ("CAMEO 4 Plus", "CAMEO 4 Pro", "CAMEO Plus", "CAMEO Pro", "CAMEO 4 PLUS ",
                 "CAMEO 4  PRO", "cameo 4 plus"):
        assert by_fg_model(text) not in (PLUS, PRO), text
    assert by_usb_id((0x0B4D, 0x1138)) == (PLUS,)
    assert by_usb_id((0x0B4D, 0x1139)) == (PRO,)
    assert by_usb_id((0x0B4D, 0x1137)) == (CAMEO4,)
    for p in BOTH:
        assert p.identity.shared_fg_models == frozenset()


def test_firmware_lines_are_the_cameo4_lines_untested():
    for p in BOTH:
        assert [ln.major for ln in p.identity.lines] == [5, 1]
        assert all(ln.support is Support.UNTESTED for ln in p.identity.lines)
        assert p.identity.line(5) is pp.V5 and p.identity.line(1) is pp.V1
    assert [ln.major for ln in c4.IDENTITY.lines] == [5, 1]


def test_ble_discovery_needs_no_new_prefix():
    """The firmware names them 'CAMEO 4 PLUS-<serial>' and 'CAMEO 4 PRO-<serial>': the CAMEO
    prefix discovery already uses covers both."""
    assert PLUS.identity.ble_names == PRO.identity.ble_names == ("CAMEO",)
    assert ble_name_prefixes()[0] == "CAMEO"
    assert ble_name_prefixes().count("CAMEO") == 1
    for name in ("CAMEO 4 PLUS-TEST", "CAMEO 4 PRO-XXXXXXX"):    # made-up serials
        assert any(name.upper().startswith(x.upper()) for x in ble_name_prefixes())


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_the_profile_passes_the_safety_self_check(p):
    safety.check_profile(p)
    safety.check_entry(p)


# --- detection -----------------------------------------------------------------------------------
@pytest.mark.parametrize("reply, p, line", [
    ("CAMEO 4 PLUS V5.05    ", PLUS, pp.V5),
    ("CAMEO 4 PLUS V5.05    \x03", PLUS, pp.V5),
    (b"CAMEO 4 PLUS V5.05    \x03", PLUS, pp.V5),
    (bytearray(b"CAMEO 4 PLUS V5.04    \x03"), PLUS, pp.V5),
    ("CAMEO 4 PLUS V1.20    ", PLUS, pp.V1),
    ("CAMEO 4 PRO V5.05    ", PRO, pp.V5),
    (b"CAMEO 4 PRO V5.05    \x03", PRO, pp.V5),
    ("CAMEO 4 PRO V1.20    ", PRO, pp.V1),
    ("CAMEO 4 PRO V1.02    \x03", PRO, pp.V1),
], ids=repr)
def test_detect_names_each_model(reply, p, line):
    d = detect(reply)
    assert d.verdict is Verdict.USE and d.profile is p and d.entry is p and d.line is line
    assert d.support is Support.UNTESTED and d.tested is False and d.message == ""
    assert d.name == p.name
    assert d.profile.offered_mats(d.support) == p.mats


def test_the_standard_tail_is_four_spaces():
    assert detect("CAMEO 4 PLUS V5.05    \x03").standard_tail is True
    assert detect("CAMEO 4 PRO V5.05    ").standard_tail is True
    assert detect("CAMEO 4 PRO V5.05").standard_tail is False


@pytest.mark.parametrize("reply", ["CAMEO 4 PRO V1.02", "CAMEO 4 PLUS V5.05",
                                   "CAMEO 4 PRO V5.05\r\n", "CAMEO 4 PLUS V5.05   ",
                                   "CAMEO 4 PLUS V5.05     \x03", "\x00CAMEO 4 PRO V5.05    "],
                         ids=repr)
def test_another_tail_is_unknown(reply):
    """Both are UNTESTED: only the firmware's four-space tail names them (a public document
    quotes 'CAMEO 4 PRO V1.02' without it; the firmware sends it)."""
    d = detect(reply)
    assert d.verdict is Verdict.UNKNOWN and d.profile is None and d.entry is None
    assert not d.standard_tail


def test_the_cameo4_is_still_the_cameo4():
    d = detect("CAMEO 4 V5.05    ")
    assert d.profile is CAMEO4 and d.tested
    assert detect("CAMEO 4 V1.20    ").profile is CAMEO4


@pytest.mark.parametrize("version", ["5.05", "1.20"])
@pytest.mark.parametrize("model, p", [("CAMEO 4", CAMEO4), ("CAMEO 4 PLUS", PLUS),
                                      ("CAMEO 4 PRO", PRO)], ids=lambda x: getattr(x, "id", x))
def test_each_family_reply_names_its_own_model_never_a_prefix(model, p, version):
    """'CAMEO 4' is a prefix of the other two texts. Matching is exact, so each standard reply
    (model, firmware version, four spaces, ETX) names its own profile, on both lines."""
    d = detect(f"{model} V{version}    \x03")
    assert d.verdict is Verdict.USE and d.profile is p and d.standard_tail
    assert d.line is p.identity.line(int(version[0]))
    assert d.tested is (p is CAMEO4 and version.startswith("5"))


NEAR_MISSES = [
    "CAMEO 4 Plus V5.05", "CAMEO 4 Pro V5.05", "cameo 4 plus v5.05", "CAMEO 4 PLUS v5.05",
    "CAMEO 4 PLU V5.05", "CAMEO 4 PR V5.05", "CAMEO 4 PLUSV5.05", "CAMEO 4 PROV5.05",
    "CAMEO 4  PLUS V5.05", "CAMEO 4 PLUS  V5.05", "CAMEO  4 PRO V5.05", "CAMEO 4 PLUS\tV5.05",
    "CAMEO 4\tPRO V5.05", "CAMEO 4 PLUS\xa0V5.05", "CAMEO 4 PRO PLUS V5.05",
    "CAMEO 4 PLUS PRO V5.05", "CAMEO 4 PROFESSIONAL V5.05", "CAMEO 4 PRO MARK2 V1.10",
    "CAMEO PLUS V5.05", "CAMEO PRO V5.05", "X CAMEO 4 PLUS V5.05", "CAMEO 4 PLUS X V5.05",
    "CAMEO 4 PLUS", "CAMEO 4 PRO", "CAMEO 4 PLUS V5", "CAMEO 4 PRO V5.5", "CAMEO 4 PRO V5.055",
    "CAMEO 4 PRO V05.05", "CAMEO 4 PLUS V 5.05", "CAMEO 4 PLUS V5.05 X1Z",
    "CAMEO 4 PRO V5.05 ABC", "CAMEO 4 PLUS V5.05\x1b", "CAMEO 4 PRO V5.٠٥",
    b"CAMEO 4 PLUS V5.05\xff", "ＣAMEO 4 PRO V5.05", "CAMEO 4 PLUS V5.05 \x03ABC",
]


@pytest.mark.parametrize("reply", NEAR_MISSES, ids=repr)
def test_near_misses_are_unknown(reply):
    """With only the CAMEO 4 family indexed, a near miss names no model at all."""
    d = detect(reply, index=Index([CAMEO4, PLUS, PRO]))
    assert d.verdict is Verdict.UNKNOWN and d.entry is None and d.profile is None


@pytest.mark.parametrize("reply", NEAR_MISSES + ["CAMEO 4 V5.05", "CAMEO 5 PLUS V1.07    ",
                                                  "CAMEO PRO MARK2 V1.10    ",
                                                  "CAMEO 5 PRO V1.07    ", "CAMEO V1.10    "],
                         ids=repr)
def test_near_misses_never_name_the_plus_or_the_pro(reply):
    """Whatever the other model modules hold, these replies never select the Plus or the Pro."""
    d = detect(reply)
    assert d.entry not in (PLUS, PRO) and d.profile not in (PLUS, PRO)


@pytest.mark.parametrize("reply, p", [("CAMEO 4 PLUS V6.05    ", PLUS),
                                      ("CAMEO 4 PRO V15.05", PRO),
                                      ("CAMEO 4 PRO V0.99", PRO)])
def test_an_unknown_firmware_line_is_recognized_and_refused(reply, p):
    d = detect(reply)
    assert d.verdict is Verdict.RECOGNIZED and d.entry is p and d.profile is None
    assert d.line is None and d.support is Support.RECOGNIZED
    assert d.message.startswith(f"OpenCutter recognizes this {p.name} but cannot drive it yet: ")


def test_the_usb_id_must_agree_with_the_model():
    assert detect("CAMEO 4 PLUS V5.05    ", usb_id=(0x0B4D, 0x1138)).profile is PLUS
    assert detect("CAMEO 4 PRO V5.05    ", usb_id=(0x0B4D, 0x1139)).profile is PRO
    for reply, usb in (("CAMEO 4 PLUS V5.05", 0x1139), ("CAMEO 4 PLUS V5.05", 0x1137),
                       ("CAMEO 4 PRO V5.05", 0x1138), ("CAMEO 4 PRO V5.05", 0x1137),
                       ("CAMEO 4 V5.05", 0x1138), ("CAMEO 4 V5.05", 0x1139)):
        d = detect(reply, usb_id=(0x0B4D, usb))
        assert d.verdict is Verdict.UNKNOWN and "disagree" in d.reason, (reply, usb)


# --- what they share with the CAMEO 4 ------------------------------------------------------------
@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_the_cameo4_values_are_reused(p):
    assert p.holders is c4.HOLDERS and p.ranges is c4.RANGES and p.status is c4.STATUS
    assert p.queries is c4.QUERIES and p.stop is c4.STOP
    for field in ("unit_mm", "max_write", "batch_bytes", "max_coordinate_su",
                  "lowering_prefixes", "fg_claims_session", "power_cycle_to_connect"):
        assert getattr(p, field) == getattr(CAMEO4, field), field
    assert p.programs == frozenset()               # no pinned programs for these models yet
    d = p.dialect
    assert (d.holders, d.accel, d.tool_reply, d.tool_slots, d.speed) == (
        c4.DIALECT.holders, c4.DIALECT.accel, c4.DIALECT.tool_reply, c4.DIALECT.tool_slots,
        c4.DIALECT.speed)


def test_the_dialects_differ_only_in_expand_mode():
    assert PLUS.dialect is c4.DIALECT and PLUS.dialect.expand_mode is False
    assert PRO.dialect == replace(c4.DIALECT, expand_mode=True)


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_ranges_as_specified(p):
    assert p.ranges.table() == {"speed": (1, 30), "force": (1, 33), "acceleration": (1, 10),
                                "offset": (0, 99), "blade_depth": (0, 10), "passes": (1, 10)}
    assert p.ranges.overcut.as_tuple() == (0, 9)
    assert p.speed_text(30) == "30" and p.speed_mm_s(5) == 50.0
    assert p.slowest_su_per_s == CAMEO4.slowest_su_per_s == 200.0
    with pytest.raises(ValueError):
        p.speed_text(2.5)                          # whole numbers only


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_holders_and_tools_as_specified(p):
    assert [(h.number, h.label) for h in p.holders] == [(1, "Tool 1"), (2, "Tool 2")]
    assert p.tool_names() == CAMEO4.tool_names()
    assert p.tool_names()[1] == {0: "No Tool Detected", 1: "Ratchet Blade", 2: "AutoBlade",
                                 3: "Deep-Cut Blade", 4: "Kraft Blade, 2mm", 5: "Rotary Blade",
                                 6: "Punch Tool", 7: "Sketch Pen"}
    assert p.tool_names()[2][4] == "Kraft Blade, 3mm" and p.tool_names()[2][8] == "Foil Tool"
    assert p.tool(1, 2).depth and not p.tool(2, 2).depth     # TF acts on holder 1 only
    assert p.unsupported_codes() == {5, 6}
    assert p.holder_codes(" 2, 7") == {1: 2, 2: 7}
    for reply in (" 15, 2", "2, 7", " 2,7", " 2, 7 ", " 2, 7\x03"):
        assert p.parse_tools(reply) is None, reply


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_reserved_and_two_digit_tool_codes_fail_closed(p):
    """Codes 8..10 in holder 1 and 9..10 in holder 2 are reserved: no tool, so no job. Code 10
    does not even parse: the reply has one digit per holder."""
    assert p.holder_codes(" 8, 9") == {1: 8, 2: 9}
    assert p.tool(1, 8) is None and p.tool(1, 9) is None and p.tool(2, 9) is None
    assert p.tool(2, 8).name == "Foil Tool" and p.tool(2, 8).usable
    for reply in (" 10, 0", " 0, 10", " 1, 2, 3", " a, 1", ""):
        assert p.holder_codes(reply) is None, reply


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_the_condition_blocks_are_the_dual_holder_forms(p):
    """J first, then the per-holder forms, one TJ for both holders, TF only in holder 1."""
    assert p.condition_texts(holder=1, speed=30, force=33, acceleration=10, offset=99,
                             line_segment_overcut=True, overcut=(9, 9), blade_depth=10) == (
        "J1", "!30,1", "FX33,1", "TJ10", "FC99,1,1", "FE1,1", "FF9,9,1", "TF10,1")
    assert p.condition_texts(holder=2, speed=1, force=1, acceleration=1, offset=0,
                             line_segment_overcut=False, overcut=(0, 0), blade_depth=None) == (
        "J2", "!1,2", "FX1,2", "TJ1", "FC0,1,2", "FE0,2", "FF0,0,2")
    with pytest.raises(ValueError):           # the firmware ignores TF in holder 2
        p.condition_texts(holder=2, speed=5, force=10, acceleration=1, offset=18,
                          line_segment_overcut=False, overcut=(0, 0), blade_depth=3)
    grammar = p.setting_grammar()
    for bad in (b"TF3,2", b"!31,1", b"!0,1", b"FX34,1", b"FX0,1", b"TJ0", b"TJ11", b"FC100,1,1",
                b"FF10,0,1", b"J0", b"J3", b"!5", b"FX5", b"FC18", b"FE0,0", b"!2.5,1"):
        assert not grammar.fullmatch(bad), bad


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_status_and_stop_as_specified(p):
    assert p.status.as_dict() == {"0": "Ready", "1": "Busy", "2": "Load Media", "3": "Paused",
                                  "4": "Cancelling", "5": "Rejected"}
    assert (p.status.ready, p.status.busy, p.status.load) == ("0", "1", "2")
    assert p.status.unloaded_after_header is True
    for reply in ("6", "7", "10", "11", "12", "-1", "0\x03", " 0", ""):   # 6, 10..12: Studio only
        assert not p.status.is_status(reply), reply
    assert p.stop.writes == (b"\x1b\x09", b"\x1b\x09") and p.stop.gap_s == 0.3


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_queries_as_specified(p):
    assert p.queries.esc == {0x05, 0x10, 0x15, 0x16}
    assert p.queries.text == {b"FG", b"TB71", b"FA", b"FQ0", b"FQ2", b"TI", b"TO", b"TC", b"G",
                              b"[", b"U"}
    safety.check_query(b"\x1b\x15", p)
    safety.check_query(b"U\x03", p)
    for t in p.queries.text:                   # nothing on the never-send list, no lowercase
        assert not safety.is_never_send(t) and not safety.is_expand_command(t), t
        assert t == t.upper(), t
    for data in (b"\x1b\x11", b"\x1b\x12", b"\x1b\x13", b"\x1b\x00", b"\x1b\x07", b"\x1b\x08",
                 b"\x1b\x09", b"\x1b\x0b", b"FM1\x03", b"FP\x03", b"FPG\x03", b"TB72\x03",
                 b"fg\x03"):
        with pytest.raises(SafetyError):
            safety.check_query(data, p)


def condition_samples(p):
    """Every holder at the low and the high end of every range, with and without a depth."""
    r = p.ranges
    for h in p.holders:
        depths = [None] + ([r.blade_depth.lo, r.blade_depth.hi]
                           if any(t.depth for t in h.tools) else [])
        for top in (False, True):
            pick = (lambda x: x.hi) if top else (lambda x: x.lo)
            for depth in depths:
                yield dict(holder=h.number, speed=pick(r.speed), force=pick(r.force),
                           acceleration=pick(r.acceleration), offset=pick(r.offset),
                           line_segment_overcut=top, overcut=(pick(r.overcut), pick(r.overcut)),
                           blade_depth=depth)


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_the_12x12_job_sends_the_cameo4_bytes(p):
    assert p.mat_texts("12x12") == CAMEO4.mat_texts("12x12") == ("TG1", "FN0", "TB50,0")
    assert p.area_texts("12x12") == CAMEO4.area_texts("12x12") == ("\\30,30", "Z6066,6066")
    assert p.trailer_texts() == CAMEO4.trailer_texts()
    samples = list(condition_samples(p))
    assert len(samples) == 8
    for s in samples:
        texts = p.condition_texts(**s)
        assert texts == CAMEO4.condition_texts(**s)
        safety.check_write(write(*texts), p)
        assert all(p.setting_grammar().fullmatch(t.encode()) for t in texts)


# --- mats ----------------------------------------------------------------------------------------
# Firmware analysis: the Plus accepts TG0..8 and the Pro TG0..9; any other TG silently becomes
# TG0, no mat.
TG_ACCEPTED = {"cameo4_plus": range(0, 9), "cameo4_pro": range(0, 10)}


def test_the_plus_mats():
    assert [m.key for m in PLUS.mats] == ["15x15", "12x12"] and PLUS.default_mat == "15x15"
    m = PLUS.mat("15x15")
    assert (m.tg, m.size_mm, m.area_su) == (8, (381.0, 381.0), (30, 9, 7590, 7320))
    assert (m.expected_u, m.u_basis, m.header_extra) == ((7620, 7420), UBasis.INFERRED, None)
    assert m.grid_in == (15, 15)
    assert PLUS.mat_texts("15x15") == ("TG8", "FN0", "TB50,0")
    assert PLUS.area_texts("15x15") == ("\\30,9", "Z7590,7320")
    assert PLUS.mat("24x24") is None


def test_the_pro_mats():
    assert [m.key for m in PRO.mats] == ["24x24", "12x12"] and PRO.default_mat == "24x24"
    m = PRO.mat("24x24")
    assert (m.tg, m.size_mm, m.area_su) == (9, (609.6, 609.6), (30, 30, 12162, 12162))
    assert (m.expected_u, m.u_basis) == ((12200, 12200), UBasis.INFERRED)
    assert m.header_extra is HeaderExtra.EXPAND and m.grid_in == (24, 24)
    assert PRO.mat_texts("24x24") == ("TG9", "FN0", "TB50,0", "FM1")
    assert PRO.area_texts("24x24") == ("\\30,30", "Z12162,12162")
    assert PRO.mat("15x15") is None


def test_the_24x24_area_needs_fm1():
    """Firmware analysis: after TG9 alone the window is 12200 x 11800 units (590 mm across), so
    the area's 12162 would be clipped. FM1 widens it to 12200: hence EXPAND and U 12200,12200."""
    tg9_alone = (12200, 11800)
    m = PRO.mat("24x24")
    assert m.area_su[3] > tg9_alone[1] and m.area_su[3] <= m.expected_u[1]
    assert m.expected_u == (tg9_alone[0], 12200)


def test_the_larger_mats_stay_off_the_smaller_machines():
    """The CAMEO 4 turns TG8 and TG9 into TG0 (no mat) and the Plus turns TG9 into TG0, so no
    profile of theirs may write them."""
    for tg, smaller in ((8, (CAMEO4,)), (9, (CAMEO4, PLUS))):
        for p in smaller:
            with pytest.raises(SafetyError):
                safety.check_write(write(f"TG{tg}", "FN0", "TB50,0"), p)
    assert CAMEO4.mat("15x15") is None and CAMEO4.mat("24x24") is None


def test_the_12x12_mat_is_the_cameo4_mat_with_an_inferred_u():
    for p in BOTH:
        m = p.mat("12x12")
        assert m is pp.MAT_12X12 and m.header_extra is None
        assert m.u_basis is UBasis.INFERRED and c4.MAT_12X12.u_basis is UBasis.KNOWN
        same = ("key", "label", "tg", "size_mm", "area_su", "expected_u", "grid_in", "feed_mm")
        assert all(getattr(m, f) == getattr(c4.MAT_12X12, f) for f in same)


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_every_mat_is_offered_enforced_and_fits_its_window(p):
    for m in p.mats:
        assert m.offered(Support.UNTESTED) and m.enforce_u and m.basis
        assert m.tg in TG_ACCEPTED[p.id] and m.tg != 0
        a0, b0, a1, b1 = m.area_su
        u_a, u_b = m.expected_u
        assert 0 <= a0 < a1 <= u_a and 0 <= b0 < b1 <= u_b, m.key     # the U is the TG window
        assert a1 <= round(m.size_mm[1] / p.unit_mm) and b1 <= round(m.size_mm[0] / p.unit_mm)
    assert p.offered_mats() == p.mats
    assert p.mat(p.default_mat).offered(p.support)


@pytest.mark.parametrize("p", BOTH, ids=lambda p: p.id)
def test_only_the_own_mats_tg_values_are_written(p):
    own = {m.tg for m in p.mats}
    for tg in range(0, 14):
        data = write(f"TG{tg}", "FN0", "TB50,0")
        if tg in own:
            safety.check_write(data, p)
            assert p.setting_grammar().fullmatch(f"TG{tg}".encode())
        else:
            with pytest.raises(SafetyError):
                safety.check_write(data, p)
            assert not p.setting_grammar().fullmatch(f"TG{tg}".encode())


# --- expand mode (FM) ----------------------------------------------------------------------------
def test_the_plus_never_gets_fm():
    assert "CAMEO 4 PLUS" in safety.NO_EXPAND_MODELS
    assert PLUS.dialect.expand_mode is False and not safety.expand_allowed(PLUS)
    assert all(m.header_extra is None for m in PLUS.mats)
    for m in PLUS.mats:
        assert "FM1" not in PLUS.mat_texts(m.key)
    for cmd in (b"FM1", b"FM0", b"FM", b"FM1,1"):
        assert not PLUS.setting_grammar().fullmatch(cmd)
    for data in (b"TG8\x03FM1\x03FN0\x03TB50,0\x03", b"TG1\x03FM1\x03", b"TG9\x03FM1\x03",
                 b"FM1\x03", b"FM0\x03", b"TG8\x03FM0\x03", b"TG8\x03 FM1\x03",
                 b"TG8\x03FN0\x03TB50,0\x03FM1\x03", b"TG1\x03FN0\x03TB50,0\x03FM1\x03",
                 b"TG9\x03FN0\x03TB50,0\x03FM1\x03"):
        with pytest.raises(SafetyError):
            safety.check_write(data, PLUS)


def test_a_plus_claiming_expand_mode_is_refused():
    with pytest.raises(UnsafeProfile, match="never sent"):
        safety.check_profile(replace(PLUS, dialect=pp.PRO_DIALECT))
    with pytest.raises(UnsafeProfile):
        safety.check_profile(replace(PLUS, mats=(*PLUS.mats, pp.MAT_24X24)))
    with pytest.raises(UnsafeProfile):          # the Plus's data still has no expand mode
        safety.check_profile(replace(PLUS, identity=replace(PLUS.identity,
                                                            fg_models=frozenset({"TEST"})),
                                     mats=(*PLUS.mats, pp.MAT_24X24)))


def test_the_pro_sends_fm1_after_tg9_fn0_tb50_only():
    """Studio's 24 x 24 in header on a CAMEO 4 Pro is TG9 FN0 TB50,0 FM1 (docs/PROTOCOL.md
    section 5.2)."""
    assert PRO.dialect.expand_mode is True and safety.expand_allowed(PRO)
    assert "CAMEO 4 PRO" not in safety.NO_EXPAND_MODELS
    header = write(*PRO.mat_texts("24x24"))
    assert header == b"TG9\x03FN0\x03TB50,0\x03FM1\x03"
    safety.check_write(header, PRO)
    safety.check_profile(PRO, pinned=[header])
    assert "FM1" not in PRO.mat_texts("12x12")
    grammar = PRO.setting_grammar()
    assert grammar.fullmatch(b"FM1")
    for cmd in (b"FM0", b"FM", b"FM1,1", b"FM2", b"fm1"):
        assert not grammar.fullmatch(cmd)
    for data in (b"TG9\x03FM1\x03FN0\x03TB50,0\x03", b"TG9\x03FM1\x03", b"FM1\x03",
                 b"TG1\x03FM1\x03", b"TG1\x03FN0\x03TB50,0\x03FM1\x03", b"TG9\x03FM0\x03",
                 b"TG9\x03FN0\x03TB50,0\x03FM0\x03", b"TG0\x03FM0\x03", b"TG8\x03FM1\x03",
                 b"TG9\x03FN0\x03TB50,0\x03\x03FM1\x03", b"TG9\x03FN0\x03TB50,0\x03 FM1\x03",
                 b"TG9\x03FN0\x03TB50,0\x03FM1 \x03", b"TG9\x03FN0\x03TB50,0\x03fm1\x03",
                 b"TG9\x03FN0\x03FM1\x03", b"FN0\x03TB50,0\x03FM1\x03"):
        with pytest.raises(SafetyError):
            safety.check_write(data, PRO)


def test_fm1_needs_the_pro_dialect_and_the_24x24_mat():
    no_expand = replace(PRO, dialect=pp.PLUS_DIALECT)
    with pytest.raises(UnsafeProfile):
        safety.check_profile(no_expand)
    on_12x12 = replace(PRO, mats=(replace(pp.MAT_12X12, header_extra=HeaderExtra.EXPAND),
                                  pp.MAT_24X24))
    with pytest.raises(UnsafeProfile):
        safety.check_profile(on_12x12)
    without_mat = replace(PRO, mats=(replace(pp.MAT_24X24, header_extra=None), pp.MAT_12X12))
    with pytest.raises(SafetyError):
        safety.check_write(b"TG9\x03FN0\x03TB50,0\x03FM1\x03", without_mat)


# --- repository policy ---------------------------------------------------------------------------
@pytest.mark.parametrize("pattern", [STUDIO_NOTATION.pattern, FIRMWARE_ADDRESS.pattern,
                                     r"[A-Za-z]+\.md:\d", r"ESC0x"])
def test_no_studio_notation_rows_or_addresses_in_the_module(pattern):
    assert not re.search(pattern, SOURCE), pattern


def test_no_studio_names_in_the_module():
    """Studio's group and column names, kept as digests in tests/studio_names.py."""
    assert not studio_names(SOURCE, profile_module=True)
