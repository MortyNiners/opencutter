# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The CAMEO 5 Alpha and CAMEO 5 Alpha Plus profiles (opencutter.machines.cameo5_alpha).

Both are UNTESTED (jobs allowed). The tests pin their values, check that FG names each model
exactly and that near misses name neither, that both pass the safety self-check, and that FM is
never sent to them. Detection is checked against the real registry and against an index of the
two profiles alone, so these tests do not depend on the other model modules. Pure functions
only: nothing reaches a machine.
"""
import dataclasses
import inspect
import re

import pytest

import stubs

stubs.install()
from opencutter.machines import (CAMEO4, HeaderExtra, Index, Support, UBasis,  # noqa: E402
                                 Verdict, ble_name_prefixes, by_fg_model, by_id, by_usb_id, detect,
                                 enabled, enabled_profile, entries, registry, safety)
from opencutter.machines import cameo5_alpha as alpha  # noqa: E402
from opencutter.machines.safety import SafetyError, UnsafeProfile  # noqa: E402
from studio_names import (FIRMWARE_ADDRESS, FIRMWARE_IMAGE_ID, STUDIO_NOTATION,  # noqa: E402
                          studio_names)

ALPHA, PLUS = alpha.CAMEO5_ALPHA, alpha.CAMEO5_ALPHA_PLUS
BOTH = [ALPHA, PLUS]
ALONE = Index(alpha.PROFILES)       # the two profiles without the other model modules
ETX = b"\x03"
ESC = b"\x1b"


def write(*texts: str) -> bytes:
    return b"".join(t.encode("ascii") + ETX for t in texts)


def ids(p) -> str:
    return p.id


# --- detection -----------------------------------------------------------------------------------
NAMED = [
    ("CAMEO 5 ALPHA V1.04    ", ALPHA),                 # the firmware's own format
    ("CAMEO 5 ALPHA V1.04    \x03", ALPHA),
    (b"CAMEO 5 ALPHA V1.02    \x03", ALPHA),            # older units, as public reports show
    (bytearray(b"CAMEO 5 ALPHA V1.99    \x03"), ALPHA),
    ("CAMEO 5 ALPHA PLUS V1.04    ", PLUS),
    (b"CAMEO 5 ALPHA PLUS V1.04    \x03", PLUS),
    ("CAMEO 5 ALPHA PLUS V1.03    ", PLUS),
]


@pytest.mark.parametrize("index", [None, ALONE], ids=["registry", "alone"])
@pytest.mark.parametrize("reply, profile", NAMED, ids=repr)
def test_fg_names_each_model(reply, profile, index):
    d = detect(reply, index=index)
    assert d.verdict is Verdict.USE and d.profile is profile and d.entry is profile
    assert d.line is alpha.V1 and d.model in profile.identity.fg_models
    assert d.support is Support.UNTESTED and d.tested is False
    assert d.reason == "" and d.message == "" and d.name == profile.name


@pytest.mark.parametrize("reply", ["CAMEO 5 ALPHA V1.02", "CAMEO 5 ALPHA PLUS V1.03",
                                   "CAMEO 5 ALPHA PLUS V1.00\r\n", "CAMEO 5 ALPHA V1.04   ",
                                   "CAMEO 5 ALPHA V1.04     ", " CAMEO 5 ALPHA V1.04    "],
                         ids=repr)
@pytest.mark.parametrize("index", [None, ALONE], ids=["registry", "alone"])
def test_another_tail_is_unknown(reply, index):
    """Both models are UNTESTED: only the firmware's four-space tail names them."""
    d = detect(reply, index=index)
    assert d.verdict is Verdict.UNKNOWN and d.profile is None and d.entry is None
    assert not d.standard_tail


def test_the_standard_tail_is_recorded():
    assert detect("CAMEO 5 ALPHA V1.04    ").standard_tail is True
    assert detect(b"CAMEO 5 ALPHA PLUS V1.04    \x03").standard_tail is True
    assert detect("CAMEO 5 ALPHA PLUS V1.03").standard_tail is False


NEAR_MISSES = [
    "CAMEO 5 Alpha V1.04", "cameo 5 alpha v1.04", "CAMEO 5 ALPHA v1.04", "CAMEO 5 ALPHA PLU V1.04",
    "CAMEO 5 ALPHA  PLUS V1.04", "CAMEO 5 ALPHAPLUS V1.04", "CAMEO 5 ALPHA PLUS EXTRA V1.04",
    "CAMEO 5ALPHA V1.04", "CAMEO  5 ALPHA V1.04", "CAMEO 5 ALPH V1.04", "CAMEO 5 ALPHA+ V1.04",
    "CAMEO 5 Alpha Plus V1.04", "CAMEO 5 ALPHA Plus V1.04", "X CAMEO 5 ALPHA V1.04",
    "CAMEO 5A V1.04", "CAMEO 5APL V1.04",            # the USB printer model names, not FG
    "CAMEO 5 ALPHA-TEST", "CAMEO 5 ALPHA PLUS-TEST",  # the shape of their BLE names
    "CAMEO 5 ALPHA V1.04 X1Z", "CAMEO 5 ALPHA PLUS V1.04 ABC\x03",   # a set 3-byte field
    "CAMEO 5 ALPHA V01.04", "CAMEO 5 ALPHA V1.4", "CAMEO 5 ALPHA V1.044", "CAMEO 5 ALPHA V1",
    "CAMEO 5 ALPHA", "CAMEO 5 ALPHA PLUS", "CAMEO 5 ALPHA\xa0V1.04", "CAMEO 5 ALPHA V1.0٤",
    b"CAMEO 5 ALPHA V1.04\xff", "CAMEO 5 ALPHA V1.04\x1b",
    "CAMEO 5 V1.04", "CAMEO 5 PLUS V1.04", "CAMEO 5 PRO V1.04", "CAMEO V1.04", "CAMEO 4 V1.04",
]


@pytest.mark.parametrize("reply", NEAR_MISSES, ids=repr)
def test_near_misses_name_neither_model(reply):
    assert detect(reply, index=ALONE).verdict is Verdict.UNKNOWN
    d = detect(reply)                       # the real registry may know another model
    assert d.entry not in BOTH and d.profile not in BOTH


@pytest.mark.parametrize("order", [(ALPHA, PLUS), (PLUS, ALPHA)])
def test_matching_is_exact_never_by_prefix(order):
    """'CAMEO 5 ALPHA' is a prefix of 'CAMEO 5 ALPHA PLUS': each text names its own model."""
    idx = Index(order)
    assert detect("CAMEO 5 ALPHA PLUS V1.04    ", index=idx).profile is PLUS
    assert detect("CAMEO 5 ALPHA V1.04    ", index=idx).profile is ALPHA
    assert detect("CAMEO 5 ALPHA PLUS V1.04", index=Index([ALPHA])).verdict is Verdict.UNKNOWN
    assert detect("CAMEO 5 ALPHA V1.04", index=Index([PLUS])).verdict is Verdict.UNKNOWN


@pytest.mark.parametrize("reply, profile", [("CAMEO 5 ALPHA V2.00", ALPHA),
                                            ("CAMEO 5 ALPHA V0.99    ", ALPHA),
                                            ("CAMEO 5 ALPHA PLUS V5.05    ", PLUS),
                                            ("CAMEO 5 ALPHA PLUS V10.01", PLUS)])
def test_an_unknown_firmware_line_is_recognized_and_refused(reply, profile):
    d = detect(reply)
    assert d.verdict is Verdict.RECOGNIZED and d.entry is profile and d.profile is None
    assert d.line is None and d.support is Support.RECOGNIZED and not d.tested
    assert d.message.startswith(f"OpenCutter recognizes this {profile.name} but cannot drive "
                                "it yet: ")


def test_usb_ids():
    assert by_usb_id((0x3844, 0x0001)) == (ALPHA,)
    assert by_usb_id((0x3844, 0x0002)) == (PLUS,)
    for other in [(0x3844, 0x0003), (0x0B4D, 0x0001), (0x0B4D, 0x0002), (0x0B4D, 0x1140)]:
        assert not set(by_usb_id(other)) & set(BOTH), other
    assert detect("CAMEO 5 ALPHA V1.04    ", usb_id=(0x3844, 0x0001)).profile is ALPHA
    assert detect("CAMEO 5 ALPHA PLUS V1.04    ", usb_id=(0x3844, 0x0002)).profile is PLUS
    for reply, usb in [("CAMEO 5 ALPHA V1.04", (0x3844, 0x0002)),
                       ("CAMEO 5 ALPHA PLUS V1.04", (0x3844, 0x0001)),
                       ("CAMEO 5 ALPHA V1.04", (0x0B4D, 0x1137))]:
        d = detect(reply, usb_id=usb)
        assert d.verdict is Verdict.UNKNOWN and "disagree" in d.reason, (reply, usb)


def test_registry():
    for p in BOTH:
        assert p in entries() and p in enabled() and p not in registry.tested()
        assert by_id(p.id) is p and enabled_profile(p.id) is p
    assert (ALPHA.id, PLUS.id) == ("cameo5_alpha", "cameo5_alpha_plus")
    assert (ALPHA.name, PLUS.name) == ("CAMEO 5 Alpha", "CAMEO 5 Alpha Plus")
    assert by_fg_model("CAMEO 5 ALPHA") is ALPHA and by_fg_model("CAMEO 5 ALPHA PLUS") is PLUS
    for bad in ("CAMEO 5 Alpha", "CAMEO 5 ALPHA ", "CAMEO 5", "CAMEO 5 ALPHA V1.04", "CAMEO 5A"):
        assert by_fg_model(bad) not in BOTH, bad
    assert ALPHA.identity.fg_models == {"CAMEO 5 ALPHA"}
    assert PLUS.identity.fg_models == {"CAMEO 5 ALPHA PLUS"}
    for p in BOTH:
        assert not p.identity.shared_fg_models and p.identity.lines == (alpha.V1,)
        assert p.identity.ble_names == ("CAMEO",)
        for name in (f"{p.identity.ble_names[0]} 5 ALPHA-TEST", "cameo 5 alpha plus-TEST"):
            assert any(name.upper().startswith(x.upper()) for x in ble_name_prefixes())


# --- the self-check and the values -------------------------------------------------------------
@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_both_pass_the_safety_self_check(p):
    safety.check_profile(p)
    safety.check_entry(p)


def test_the_plus_differs_only_in_its_identity():
    assert dataclasses.replace(PLUS, id=ALPHA.id, name=ALPHA.name,
                               identity=ALPHA.identity) == ALPHA
    assert alpha.PROFILES == (ALPHA, PLUS)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_support_and_limits(p):
    assert p.support is Support.UNTESTED and alpha.V1.support is Support.UNTESTED
    assert alpha.V1.major == 1 and alpha.V1.reason == ""
    assert p.unit_mm == 0.05 and p.su_per_mm == 20
    assert p.max_write == 182 and p.batch_bytes == 1024 and p.max_coordinate_su == 99_999
    assert p.lowering_prefixes == (b"D", b"TF")
    # not observed on these models: the safe advice to switch it off and on, as on the CAMEO 4
    assert p.fg_claims_session is True and p.power_cycle_to_connect is True
    assert p.programs == frozenset()


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_ranges(p):
    """Studio's ranges intersected with the firmware's: force up to 40 (the CAMEO 4 stops at 33),
    whole speeds 1..30, TJ 1..10."""
    assert p.ranges.table() == {"speed": (1, 30), "force": (1, 40), "acceleration": (1, 10),
                                "offset": (0, 99), "blade_depth": (0, 10), "passes": (1, 10)}
    assert p.ranges.overcut.as_tuple() == (0, 9)
    assert p.speed_text(30) == "30" and p.speed_mm_s(5) == 50.0
    assert p.slowest_su_per_s == 200.0
    for bad in (2.5, True, "5"):
        with pytest.raises(ValueError):
            p.speed_text(bad)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_mats(p):
    assert [m.key for m in p.mats] == ["12x12", "12x24"] and p.default_mat == "12x12"
    assert p.mat("12x12") is alpha.MAT_12X12 and p.mat("12x24") is alpha.MAT_12X24
    for key in ("15x15", "24x24", "8x12", "6x8", "8.5x12", "none"):
        assert p.mat(key) is None, key
    small, long_ = alpha.MAT_12X12, alpha.MAT_12X24
    assert (small.tg, small.size_mm, small.area_su, small.expected_u) == (
        1, (304.8, 304.8), (30, 30, 6066, 6066), (6100, 6100))
    assert (long_.tg, long_.size_mm, long_.area_su, long_.expected_u) == (
        2, (304.8, 609.6), (30, 30, 12162, 6066), (12200, 6100))
    for m in p.mats:
        assert m.u_basis is UBasis.INFERRED and m.enforce_u          # a mismatch refuses the job
        assert m.header_extra is None and m.feed_mm == 16.5 and m.basis
        a0, b0, a1, b1 = m.area_su
        assert 0 < a0 < a1 < m.expected_u[0] and 0 < b0 < b1 < m.expected_u[1]
    assert p.offered_mats() == p.mats
    assert p.offered_mats(Support.RECOGNIZED) == ()


def test_the_12x12_header_is_the_cameo4_header():
    for p in BOTH:
        assert p.mat_texts("12x12") == CAMEO4.mat_texts("12x12") == ("TG1", "FN0", "TB50,0")
        assert p.area_texts("12x12") == CAMEO4.area_texts("12x12") == ("\\30,30", "Z6066,6066")
        assert p.mat_texts("12x24") == ("TG2", "FN0", "TB50,0")
        assert p.area_texts("12x24") == ("\\30,30", "Z12162,6066")
        assert p.trailer_texts() == CAMEO4.trailer_texts()
        with pytest.raises(ValueError):
            p.mat_texts("15x15")


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_holders_and_tools(p):
    assert [h.label for h in p.holders] == ["Tool 1", "Tool 2"]
    common = {0: "No Tool Detected", 1: "Ratchet Blade", 2: "AutoBlade", 3: "Deep-Cut Blade",
              5: "Rotary Blade", 6: "Punch Tool", 7: "Sketch Pen", 8: "Foil Tool"}
    assert p.tool_names() == {1: {**common, 4: "Kraft Blade, 2mm"},
                              2: {**common, 4: "Kraft Blade, 3mm"}}
    for h in p.holders:
        assert {t.code for t in h.tools if t.usable} == {1, 2, 3, 4, 7, 8}
        assert h.tool(8).usable and not h.tool(8).blade      # the Foil Tool, in either holder
    assert p.unsupported_codes() == {5, 6}
    assert p.blade_codes() == {1, 2, 3, 4, 5}
    assert {(h.number, t.code) for h in p.holders for t in h.tools if t.depth} == {(1, 2)}
    # Codes 9..20 are reserved or newer tools never checked: unknown, so they fail closed
    # (a job cannot name them, and a reply with one never matches a job's tool).
    for holder in (1, 2):
        for code in [*range(9, 21), 99]:
            assert p.tool(holder, code) is None, (holder, code)
            assert p.tool_name(holder, code) == f"unknown tool {code}"
    assert p.tool(3, 1) is None and p.tool(0, 1) is None and p.tool(1, True) is None
    # every tool the CAMEO 4 takes means the same here
    for h in CAMEO4.holders:
        for t in h.tools:
            assert p.tool(h.number, t.code) == t, (h.number, t.code)


# TG values the firmware takes (firmware analysis of V1.04): any other value silently selects
# TG0, the roll with no mat, so a mat whose TG is not taken would be cut with the wrong window.
TG_TAKEN = {"cameo5_alpha": {0, 1, 2, 4, 5, 6, 7, 11, 12, 13},
            "cameo5_alpha_plus": {0, 1, 2, 4, 5, 6, 7, 8, 11, 12, 13}}


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_every_mat_tg_is_one_the_firmware_takes(p):
    assert {m.tg for m in p.mats} <= TG_TAKEN[p.id] - {0}
    # 8 x 12 in (TG3) and 24 x 24 in (TG9) become the roll on both models
    assert not {m.tg for m in p.mats} & {3, 9, 10}
    # the Plus differs only in TG8 (15 x 15 in), which is not offered yet
    assert TG_TAKEN["cameo5_alpha_plus"] - TG_TAKEN["cameo5_alpha"] == {8}


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_the_tool_reply_takes_two_digit_codes(p):
    assert p.parse_tools(" 2, 7") == (2, 7) and p.parse_tools(" 0, 0") == (0, 0)
    assert p.parse_tools(" 15, 2") == (15, 2) and p.parse_tools(" 8, 18") == (8, 18)
    assert p.holder_codes(" 2, 18") == {1: 2, 2: 18}
    assert p.holder_codes(" 17, 0") == {1: 17, 2: 0}
    for reply in (" 05, 2", " 100, 2", " 15,2", "15, 2", " 15, 2 ", " 15, 2\x03", "  15, 2",
                  " 15, 2, 1", " -1, 2", " ١٥, 2", None, b" 2, 7", ""):
        assert p.parse_tools(reply) is None and p.holder_codes(reply) is None, repr(reply)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_condition_blocks_use_the_cameo4_forms(p):
    values = dict(speed=5, force=10, acceleration=3, offset=18, line_segment_overcut=False,
                  overcut=(1, 1))
    for holder, depth in [(1, 1), (1, None), (2, None)]:
        texts = p.condition_texts(holder=holder, blade_depth=depth, **values)
        assert texts == CAMEO4.condition_texts(holder=holder, blade_depth=depth, **values)
    assert p.condition_texts(holder=1, speed=30, force=40, acceleration=10, offset=99,
                             line_segment_overcut=True, overcut=(9, 9), blade_depth=10) == (
        "J1", "!30,1", "FX40,1", "TJ10", "FC99,1,1", "FE1,1", "FF9,9,1", "TF10,1")
    with pytest.raises(ValueError):
        p.condition_texts(holder=2, blade_depth=1, **values)     # TF acts on holder 1 only
    with pytest.raises(ValueError):
        p.condition_texts(holder=1, blade_depth=None, **{**values, "speed": 2.5})


ACCEPTED = [b"TG1", b"TG2", b"FN0", b"TB50,0", b"\\30,30", b"Z6066,6066", b"Z12162,6066",
            b"\\0,0", b"J1", b"J2", b"!1,1", b"!30,2", b"FX1,1", b"FX33,1", b"FX34,2", b"FX40,2",
            b"TJ1", b"TJ10", b"FC0,1,1", b"FC99,1,2", b"FE0,1", b"FE1,2", b"FF0,0,1",
            b"FF9,9,2", b"TF0,1", b"TF10,1"]
# Commands Studio or the firmware has for these models that OpenCutter never sends (the family
# analysis): expand mode, curve mode, the actuator, the tool-up speed, the cross cutter, TR, TS,
# the scan and mode variants of TB, the sheet feeder, the test cut, home, curves, the
# diagnostic unlock and the persistent writes.
NOT_SENT = [
    b"FM0", b"FM1", b"BZK1", b"BZK2", b"BZK3", b"AOC1", b"AOC2", b"APS30", b"APS1", b"ACT1",
    b"TR0,1", b"TR1", b"TR99", b"TS0,1", b"TS1", b"TS2", b"TB22", b"TB23", b"TB24", b"TB25",
    b"TB30", b"TB60", b"TB65", b"TB98", b"TB99", b"TB122", b"TB124", b"TB125", b"TB1023,1",
    b"TB70", b"TB71", b"TB72", b"AF0", b"AF1", b"AH0", b"FH", b"H", b"SO0", b"BZ1,1,2,2,3,3,4,4",
    b"TZ1,1", b"BS", b"FPG", b"FPGRFCC1", b"FB0,0", b"TH", b"SC", b"TN", b"FY", b"FU", b"FD",
    b"fpg", b"w1",
]
REFUSED = [
    b"TG0", b"TG3", b"TG4", b"TG8", b"TG9", b"TG10", b"TG11", b"TG12", b"TG13", b"TG01",
    b"J0", b"J3", b"!0,1", b"!31,1", b"!2.5,1", b"!0.5,1", b"!5", b"!5,3", b"!05,1", b"FX0,1",
    b"FX41,1", b"FX40", b"TJ0", b"TJ11", b"TJ3,1", b"FC100,1,1", b"FC18,2,1", b"FC18",
    b"FE2,1", b"FE1,0", b"FF10,0,1", b"FF1,1,0", b"TF11,1", b"TF1,1,1", b"TF1,2", b"TF1",
    b"tg1", b"FQ0", b"FQ2", b"TI", b"TC", b"TO", b"FG", b"U", b"M0,0", b"D1,1",
    *NOT_SENT,
]


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_the_command_grammar(p):
    grammar = p.setting_grammar()
    for c in ACCEPTED:
        assert grammar.fullmatch(c), c
    for c in REFUSED:
        assert not grammar.fullmatch(c), c


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_motion_writes_keep_the_global_rules(p):
    for key in ("12x12", "12x24"):
        safety.check_write(write(*p.mat_texts(key)), p)
        safety.check_write(write(*p.area_texts(key)), p)
    safety.check_write(write(*p.trailer_texts()), p)
    for texts in [("TG3", "FN0", "TB50,0"), ("TG8",), ("TG9",), ("TG0",),   # TG0 is no mat
                  ("TG1", "FM1", "FN0", "TB50,0"), ("TG9", "FM1"), ("FM0",), ("FPG",),
                  ("FB0,0",), ("tg1",)]:
        with pytest.raises(SafetyError):
            safety.check_write(write(*texts), p)


@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_status_stop_and_queries(p):
    assert p.status.as_dict() == CAMEO4.status.as_dict() == {
        "0": "Ready", "1": "Busy", "2": "Load Media", "3": "Paused", "4": "Cancelling",
        "5": "Rejected"}
    assert (p.status.ready, p.status.busy, p.status.load) == ("0", "1", "2")
    assert p.status.unloaded_after_header is True
    for reply in ("6", "10", "11", "12", "", " 0", "00", None):     # unknown digits abort a step
        assert not p.status.is_status(reply), repr(reply)
    assert p.stop.writes == (ESC + b"\x09", ESC + b"\x09") and p.stop.gap_s == 0.3
    assert p.queries.esc == {0x05, 0x10, 0x15, 0x16}
    assert p.queries.text == {b"FG", b"FA", b"FQ2", b"TO", b"G", b"[", b"U"}
    assert p.queries.max_write == 20
    for data in [ESC + b"\x05", ESC + b"\x10", ESC + b"\x15", ESC + b"\x16"] + \
            [q + ETX for q in p.queries.text]:
        safety.check_query(data, p)
    # FQ0 answers in tenths on this firmware; TB71, TI and TC were never seen to answer
    for data in [b"FQ0\x03", b"TB71\x03", b"TI\x03", b"TC\x03", ESC + b"\x11", ESC + b"\x12",
                 ESC + b"\x13", ESC + b"\x07", ESC + b"\x08", ESC + b"\x00", ESC + b"\x09",
                 ESC + b"\x0a", b"FG\x03U\x03", b"U", b"fg\x03", b" FG\x03"]:
        with pytest.raises(SafetyError):
            safety.check_query(data, p)
    for command in NOT_SENT:
        assert command not in p.queries.text, command
        with pytest.raises(SafetyError):
            safety.check_query(command + ETX, p)


# --- FM: never sent to either model ------------------------------------------------------------
@pytest.mark.parametrize("p", BOTH, ids=ids)
def test_fm_is_never_sent(p):
    """Neither model accepts the 24 x 24 in mat (TG9), and the firmware ignores FM on the mats
    offered here: the profiles do not take expand mode, so FM is never sent."""
    assert p.dialect.expand_mode is False and safety.expand_allowed(p) is False
    assert not any(m.header_extra for m in p.mats)
    for key in ("12x12", "12x24"):
        assert not [t for t in p.mat_texts(key) if t.startswith("FM")]
    with_tg9 = dataclasses.replace(p, mats=p.mats + (dataclasses.replace(
        alpha.MAT_12X24, key="24x24", tg=9, size_mm=(609.6, 609.6),
        area_su=(30, 30, 12162, 12162), expected_u=(12200, 12200),
        header_extra=HeaderExtra.EXPAND),))
    with pytest.raises(UnsafeProfile, match="FM1"):
        safety.check_profile(with_tg9)


# --- the repository policy -----------------------------------------------------------------------
def test_the_module_has_no_studio_notation():
    source = inspect.getsource(alpha)
    assert "%" not in source and "Graphtec" not in source
    assert not STUDIO_NOTATION.search(source)                 # notation and file names
    assert not studio_names(source, profile_module=True)      # Studio's own names (digests)
    assert not FIRMWARE_ADDRESS.search(source)                # no firmware addresses
    assert not FIRMWARE_IMAGE_ID.search(source)               # no firmware image ids
