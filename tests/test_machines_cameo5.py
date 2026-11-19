# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The CAMEO 5, CAMEO 5 Plus and CAMEO Pro MK-II profiles (opencutter.machines.cameo5).

All three are UNTESTED: ported from Studio's machine data and the firmware analysis, never run
on a machine. These tests pin the ported values (exact FG texts, two-digit ESC 15 codes, force
1..40, whole-number speeds and never APS, mats only where the TG value, window and area are
known, FM1 after TG9 FN0 TB50,0 on the MK-II's 24 x 24 in mat only, the status digits, the stop
and the queries) and check that every profile keeps the global safety rules. Pure values:
nothing here imports the device modules or reaches a machine.
"""
import dataclasses
import inspect
import re

import pytest

from opencutter.machines import (CAMEO4, HeaderExtra, Index, Support, ToolReply, UBasis,
                                 Verdict, by_fg_model, by_id, by_usb_id, detect, enabled,
                                 enabled_profile, fg_candidates, registry, safety)
from opencutter.machines import cameo5 as c5
from opencutter.machines.safety import SafetyError, UnsafeProfile
from studio_names import FIRMWARE_ADDRESS, STUDIO_NOTATION, studio_names

C5, PLUS, MK2 = c5.CAMEO5, c5.CAMEO5_PLUS, c5.CAMEO_PRO_MK2
ALL = (C5, PLUS, MK2)
ETX = b"\x03"
ESC = b"\x1b"
# Only these profiles and the CAMEO 4: other model modules cannot change what is tested here.
OWN = Index([CAMEO4, *ALL])


def replace(obj, **changes):
    return dataclasses.replace(obj, **changes)


def write(*commands: bytes) -> bytes:
    return b"".join(c + ETX for c in commands)


# --- the module and the registry -----------------------------------------------------------------
def test_the_module_declares_three_untested_profiles():
    assert c5.PROFILES == (C5, PLUS, MK2)
    assert [(p.id, p.name) for p in ALL] == [("cameo5", "CAMEO 5"), ("cameo5_plus", "CAMEO 5 Plus"),
                                            ("cameo_pro_mk2", "CAMEO Pro MK-II")]
    for p in ALL:
        assert p.support is Support.UNTESTED and p.support.jobs_allowed
        assert [(ln.major, ln.support) for ln in p.identity.lines] == [(1, Support.UNTESTED)]
        assert p in enabled() and p not in registry.tested()
        assert by_id(p.id) is p and enabled_profile(p.id) is p
        assert p.programs == frozenset()                    # no pinned programs yet
        assert hash(p) == hash(replace(p))


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_the_profile_passes_the_safety_self_check(profile):
    safety.check_profile(profile)
    safety.check_entry(profile)


def test_registry_lookups():
    for p, fg, usb in [(C5, "CAMEO 5", 0x1140), (PLUS, "CAMEO 5 PLUS", 0x1141),
                       (MK2, "CAMEO PRO MARK2", 0x1146)]:
        assert p.identity.fg_models == {fg} and p.identity.shared_fg_models == frozenset()
        assert by_fg_model(fg) is p
        assert p.identity.usb_ids == {(0x0B4D, usb)} and by_usb_id((0x0B4D, usb)) == (p,)
        assert p.identity.ble_names == ("CAMEO",)
    assert "CAMEO" in registry.ble_name_prefixes()
    # 'CAMEO 5 PRO' (0b4d:1142) is a variant of the V1.07 image that no known product uses
    assert not set(by_usb_id((0x0B4D, 0x1142))) & set(ALL)
    assert by_fg_model("CAMEO 5 PRO") is None and fg_candidates("CAMEO 5 PRO") == ()
    # an MK-II with another variant setting reports a bare 'CAMEO': the profile never claims it
    assert not set(fg_candidates("CAMEO")) & set(ALL) and by_fg_model("CAMEO") not in ALL


# --- detection ----------------------------------------------------------------------------------
@pytest.mark.parametrize("reply, profile, version", [
    ("CAMEO 5 V1.07    ", C5, (1, 7)),
    ("CAMEO 5 V1.07    \x03", C5, (1, 7)),
    (b"CAMEO 5 V1.07    \x03", C5, (1, 7)),
    ("CAMEO 5 V1.06    \x03", C5, (1, 6)),
    ("CAMEO 5 PLUS V1.07    ", PLUS, (1, 7)),
    (b"CAMEO 5 PLUS V1.07    \x03", PLUS, (1, 7)),
    ("CAMEO PRO MARK2 V1.10    ", MK2, (1, 10)),
    ("CAMEO PRO MARK2 V1.09    \x03", MK2, (1, 9)),
], ids=repr)
@pytest.mark.parametrize("index", [None, OWN], ids=["registry", "own"])
def test_each_model_is_detected_by_its_exact_fg_text(reply, profile, version, index):
    d = detect(reply, index=index)
    assert d.verdict is Verdict.USE and d.profile is profile and d.entry is profile
    assert d.line is profile.identity.lines[0] and d.version == version
    assert d.support is Support.UNTESTED and not d.tested
    assert d.name == profile.name and d.reason == "" and d.message == ""


def test_the_standard_tail_is_recorded():
    assert detect("CAMEO 5 PLUS V1.07    \x03").standard_tail is True
    assert detect("CAMEO 5 PLUS V1.07").standard_tail is False


@pytest.mark.parametrize("reply, usb", [("CAMEO 5 V1.07    ", 0x1140),
                                        ("CAMEO 5 PLUS V1.07    ", 0x1141),
                                        ("CAMEO PRO MARK2 V1.10    ", 0x1146)])
def test_the_usb_id_must_agree(reply, usb):
    assert detect(reply, usb_id=(0x0B4D, usb)).profile in ALL
    for other in {0x1140, 0x1141, 0x1142, 0x1146, 0x1137} - {usb}:
        d = detect(reply, usb_id=(0x0B4D, other))
        assert d.verdict is Verdict.UNKNOWN and d.profile is None and "disagree" in d.reason


NEAR_MISSES = [
    "CAMEO 5 PRO V1.07    ",         # a firmware variant with no known product: refused
    "CAMEO 5 PRO V1.07",
    "CAMEO 5 Plus V1.07",            # Studio's spelling, not the firmware's
    "cameo 5 v1.07", "CAMEO 5 v1.07", "Cameo 5 V1.07",
    "CAMEO 5  V1.07", "CAMEO  5 V1.07", "CAMEO 5PLUS V1.07", "CAMEO 5 PLUS  V1.07",
    "CAMEO 5 PLUS PRO V1.07", "CAMEO 5 PLU V1.07", "CAMEO 5 P V1.07", "CAMEO 5X V1.07",
    "CAMEO 5 V1.07 X1Z", "CAMEO 5 V1.07 ABC\x03", "CAMEO 5 V1.07X",       # a set 3-byte field
    "CAMEO 5 V01.07", "CAMEO 5 V1.7", "CAMEO 5 V1.070", "CAMEO 5 V1", "CAMEO 5",
    "CAMEO 5 V1.07\x1b", "CAMEO 5\xa0V1.07",
    "CAMEO Pro Mark2 V1.10", "CAMEO PRO MARK 2 V1.10", "CAMEO PRO MARK2V1.10",
    "CAMEO PRO MARKII V1.10", "CAMEO PRO MK2 V1.10", "CAMEO PRO V1.10", "CAMEO PRO2 V1.10",
    "CAMEO PRO MARK2 V01.10", "CAMEO PRO MARK2 V1.10 X1Z", "CAMEO  V1.10", "CAMEO V1.10    ",
    "CAMEO5 V1.07", "CAMEO 5-A1B2C3D V1.07", "CAMEO 5-A1B2C3D",
    "CAMEO 5 ALPHA V1.04    ", "CAMEO 5 ALPHA PLUS V1.04    ",   # 'CAMEO 5' is only a prefix
]


@pytest.mark.parametrize("reply", NEAR_MISSES, ids=repr)
def test_near_misses_name_no_profile(reply):
    d = detect(reply, index=OWN)
    assert d.verdict is Verdict.UNKNOWN and d.entry is None and d.profile is None
    assert d.message.endswith(".")
    real = detect(reply)                              # whatever the other model modules hold
    assert real.profile not in ALL and real.entry not in ALL


@pytest.mark.parametrize("usb", [None, (0x0B4D, 0x1142), (0x0B4D, 0x1140)])
def test_the_cameo_5_pro_is_unknown(usb):
    """No entry in the whole registry names it: an FG text that matches no entry is UNKNOWN."""
    for reply in ("CAMEO 5 PRO V1.07    ", b"CAMEO 5 PRO V1.07    \x03"):
        d = detect(reply, usb_id=usb)
        assert d.verdict is Verdict.UNKNOWN and d.entry is None and d.profile is None


def test_the_mk2_usb_id_does_not_settle_a_bare_cameo():
    """Its other variant settings report 'CAMEO', with TG values the profile does not describe."""
    for reply in ("CAMEO V1.10    ", "CAMEO  V1.10"):
        for index in (None, OWN):
            d = detect(reply, usb_id=(0x0B4D, 0x1146), index=index)
            assert d.verdict is not Verdict.USE and d.profile is None and d.entry not in ALL


@pytest.mark.parametrize("reply, profile", [("CAMEO 5 V2.00", C5), ("CAMEO 5 PLUS V5.05", PLUS),
                                            ("CAMEO PRO MARK2 V0.99    ", MK2)])
def test_an_unlisted_firmware_line_is_recognized(reply, profile):
    d = detect(reply)
    assert d.verdict is Verdict.RECOGNIZED and d.entry is profile and d.line is None
    assert d.profile is None and d.support is Support.RECOGNIZED and not d.tested
    assert d.message.startswith(f"OpenCutter recognizes this {profile.name} but cannot drive it "
                                "yet: ")


# --- ranges, speed and the command grammar ------------------------------------------------------
@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_ranges(profile):
    assert profile.ranges.table() == {"speed": (1, 30), "force": (1, 40), "acceleration": (1, 10),
                                      "offset": (0, 99), "blade_depth": (0, 10),
                                      "passes": (1, 10)}
    assert profile.ranges.overcut.as_tuple() == (0, 9)
    assert profile.unit_mm == 0.05 and profile.su_per_mm == 20
    assert profile.slowest_su_per_s == 200.0            # Speed 1 = 1 cm/s
    assert (profile.max_write, profile.batch_bytes, profile.max_coordinate_su) == (182, 1024,
                                                                                    99_999)
    assert profile.lowering_prefixes == (b"D", b"TF")
    # not observed on these models: the safe advice to switch it off and on, as on the CAMEO 4
    assert profile.fg_claims_session is True and profile.power_cycle_to_connect is True


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_speed_is_whole_numbers_and_aps_is_never_sent(profile):
    assert profile.speed_text(30) == "30" and profile.speed_mm_s(5) == 50.0
    for bad in (2.5, 5.0, "5", True, None):
        with pytest.raises(ValueError):
            profile.speed_text(bad)
    grammar = profile.setting_grammar()
    for command in (b"!1,1", b"!30,2", b"!5,1"):
        assert grammar.fullmatch(command), command
    for command in (b"!0,1", b"!31,1", b"!2.5,1", b"!5.0,1", b"!05,1", b"!5", b"!5,3", b"!300,1",
                    b"APS1", b"APS30", b"APS40", b"APS", b"!5,1APS30"):
        assert not grammar.fullmatch(command), command


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_force_runs_to_40(profile):
    grammar = profile.setting_grammar()
    for command in (b"FX1,1", b"FX33,2", b"FX34,1", b"FX40,1", b"FX40,2", b"TJ1", b"TJ10",
                    b"FC0,1,1", b"FC99,1,2", b"FE0,1", b"FE1,2", b"FF0,0,1", b"FF9,9,2",
                    b"TF0,1", b"TF10,1", b"J1", b"J2"):
        assert grammar.fullmatch(command), command
    for command in (b"FX0,1", b"FX41,1", b"FX40", b"FX40,3", b"FX040,1", b"TJ0", b"TJ11",
                    b"FC100,1,1", b"FC5,2,1", b"FF10,0,1", b"TF11,1", b"TF5,2", b"TF5", b"J0",
                    b"J3", b"FX40,1TJ3"):
        assert not grammar.fullmatch(command), command


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_the_grammar_refuses_the_never_send_list_and_this_generations_extras(profile):
    grammar = profile.setting_grammar()
    for command in (b"FP", b"FPG", b"FPGRFCC1", b"FB1", b"TB72", b"TB70", b"FY1", b"FU1", b"SC1",
                    b"FD1", b"TN1", b"ZF1", b"FM0", b"TB24", b"TB124", b"TB125", b"TB23",
                    b"TB30", b"TR0,1", b"TR1", b"TS1", b"TS0,1", b"ACT1", b"AF1", b"AH0",
                    b"BZ1,1,1,1,1,1,1,1,1,1", b"TM1", b"L0", b"SO0", b"H", b"tg1", b"fx1,1"):
        assert not grammar.fullmatch(command), command
    assert bool(grammar.fullmatch(b"FM1")) is (profile is MK2)


def test_condition_blocks():
    for p in ALL:
        assert p.condition_texts(holder=1, speed=30, force=40, acceleration=10, offset=99,
                                 line_segment_overcut=True, overcut=(9, 9), blade_depth=10) == (
            "J1", "!30,1", "FX40,1", "TJ10", "FC99,1,1", "FE1,1", "FF9,9,1", "TF10,1")
        assert p.condition_texts(holder=2, speed=5, force=12, acceleration=3, offset=0,
                                 line_segment_overcut=False, overcut=(0, 0), blade_depth=None) == (
            "J2", "!5,2", "FX12,2", "TJ3", "FC0,1,2", "FE0,2", "FF0,0,2")
        with pytest.raises(ValueError):                 # no Blade Depth in holder 2
            p.condition_texts(holder=2, speed=5, force=12, acceleration=3, offset=0,
                              line_segment_overcut=False, overcut=(0, 0), blade_depth=1)
        with pytest.raises(ValueError):                 # tenths are never written
            p.condition_texts(holder=1, speed=2.5, force=12, acceleration=3, offset=0,
                              line_segment_overcut=False, overcut=(0, 0), blade_depth=None)
        with pytest.raises(ValueError):                 # TJ is always written
            p.condition_texts(holder=1, speed=5, force=12, acceleration=None, offset=0,
                              line_segment_overcut=False, overcut=(0, 0), blade_depth=None)
        assert p.trailer_texts() == ("\\0,0", "M0,0", "FN0", "TB50,0")


# --- holders and the ESC 15 reply ---------------------------------------------------------------
COMMON = {0: "No Tool Detected", 1: "Ratchet Blade", 2: "AutoBlade", 3: "Deep-Cut Blade",
          5: "Rotary Blade", 6: "Punch Tool", 7: "Sketch Pen", 8: "Foil Tool"}


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_holders_and_tools(profile):
    assert [h.label for h in profile.holders] == ["Tool 1", "Tool 2"]
    assert profile.tool_names() == {1: {**COMMON, 4: "Kraft Blade, 2mm"},
                                    2: {**COMMON, 4: "Kraft Blade, 3mm"}}
    assert profile.blade_codes() == {1, 2, 3, 4, 5}
    assert profile.unsupported_codes() == {5, 6}
    usable = {(h.number, t.code) for h in profile.holders for t in h.tools if t.usable}
    assert usable == {(n, c) for n in (1, 2) for c in (1, 2, 3, 4, 7, 8)}
    depth = {(h.number, t.code) for h in profile.holders for t in h.tools if t.depth}
    assert depth == {(1, 2)}                            # the AutoBlade in holder 1 only
    actions = {t.code: t.action for h in profile.holders for t in h.tools}
    assert actions == {0: "Cut", 1: "Cut", 2: "Cut", 3: "Cut", 4: "Kraft Cut", 5: "Rotary Cut",
                       6: "Punch", 7: "Sketch", 8: "Foil"}
    for h in profile.holders:
        assert h.tool(0).usable is False
        for code in range(9, 100):                      # reserved or never seen: fail closed
            assert profile.tool(h.number, code) is None
            assert profile.tool_name(h.number, code) == f"unknown tool {code}"


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_the_tool_reply_has_two_digit_codes(profile):
    assert profile.dialect.tool_reply is ToolReply.TWO_DIGITS
    assert profile.dialect.tool_slots == (0, 1)
    for reply, codes in [(" 2, 7", (2, 7)), (" 0, 0", (0, 0)), (" 15, 4", (15, 4)),
                         (" 18, 18", (18, 18)), (" 1, 17", (1, 17)), (" 99, 10", (99, 10))]:
        assert profile.parse_tools(reply) == codes, reply
        assert profile.holder_codes(reply) == {1: codes[0], 2: codes[1]}
    for reply in (" 100, 1", " 1, 100", " 01, 2", " 2, 07", "15, 4", " 15,4", "  15, 4",
                  " 15, 4 ", " 15, 4\x03", " 1, 2, 3", " 1", "", " -1, 2", " ١, 2", " 2, ٧",
                  None, 15, b" 2, 7"):
        assert profile.parse_tools(reply) is None, reply
        assert profile.holder_codes(reply) is None
    assert CAMEO4.parse_tools(" 15, 4") is None         # the CAMEO 4 keeps one digit


# --- mats, TG values and the U check ------------------------------------------------------------
# TG values each model takes (firmware analysis); any other value selects the roll window.
ACCEPTED_TG = {C5: {0, 1, 2, 4, 5, 6, 7, 11, 12, 13},
               PLUS: {0, 1, 2, 4, 5, 6, 7, 8, 11, 12, 13},
               MK2: {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11}}
MATS = {C5: (["12x12", "12x24"], "12x12"),
        PLUS: (["15x15", "12x12", "12x24"], "15x15"),
        MK2: (["24x24", "15x15", "12x12", "12x24", "8x12"], "24x24")}
# key: (TG, size, area, expected U), the U being the TG window in plotter units
MAT_VALUES = {
    "12x12": (1, (304.8, 304.8), (30, 30, 6066, 6066), (6100, 6100)),
    "12x24": (2, (304.8, 609.6), (30, 30, 12162, 6066), (12200, 6100)),
    "15x15": (8, (381.0, 381.0), (30, 9, 7590, 7320), (7620, 7420)),
    "8x12": (3, (203.2, 304.8), (30, 30, 6066, 4034), (6100, 4120)),
    "24x24": (9, (609.6, 609.6), (30, 30, 12162, 12162), (12200, 12200)),
}
# The firmware's TG windows in mm, feed x carriage (the same entries in both images); FM1 widens
# TG9's carriage side from 590 to 610 mm. The expected U is the window in plotter units.
TG_WINDOW_MM = {1: (305, 305), 2: (610, 305), 3: (305, 206), 8: (381, 371), 9: (610, 610)}


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_mats(profile):
    keys, default = MATS[profile]
    assert [m.key for m in profile.mats] == keys and profile.default_mat == default
    assert profile.offered_mats() == profile.mats            # every mat is offered
    assert profile.offered_mats(Support.RECOGNIZED) == ()
    for m in profile.mats:
        tg, size, area, u = MAT_VALUES[m.key]
        assert (m.tg, m.size_mm, m.area_su, m.expected_u) == (tg, size, area, u), m.key
        assert u == tuple(round(v / profile.unit_mm) for v in TG_WINDOW_MM[m.tg]), m.key
        if m.key != "15x15":                    # 15 x 15 in: the area Studio sent, not this rule
            # the mat (feed, carriage) in plotter units, minus 30 units on each side
            assert area == (30, 30, round(size[1] / profile.unit_mm) - 30,
                            round(size[0] / profile.unit_mm) - 30), m.key
        assert m.u_basis is UBasis.INFERRED and m.enforce_u     # inferred, so enforced
        assert m.tg in ACCEPTED_TG[profile] and m.tg != 0
        assert m.label == m.key.replace("x", " × ") + " in"
        assert m.grid_in == tuple(int(v) for v in m.key.split("x"))
        a0, b0, a1, b1 = m.area_su
        assert 0 <= a0 < a1 < u[0] and 0 <= b0 < b1 < u[1]        # inside the TG window
        assert profile.area_texts(m.key) == (f"\\{a0},{b0}", f"Z{a1},{b1}")


def test_only_mats_with_a_known_area_are_listed():
    """The electrostatic mats (TG12, TG13), the 8.5 x 12 in mat (TG11) and the scanning mats
    (TG4..6) have no known cut area on this generation, so they are left out."""
    for p in ALL:
        assert not {m.tg for m in p.mats} & {0, 4, 5, 6, 7, 10, 11, 12, 13}


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_tg_values_outside_the_profiles_mats_are_refused(profile):
    own = {m.tg for m in profile.mats}
    for tg in range(0, 16):
        data = write(b"TG%d" % tg, b"FN0", b"TB50,0")
        if tg in own:
            assert tg in ACCEPTED_TG[profile]
            safety.check_write(data, profile)
        else:
            with pytest.raises(SafetyError, match="TG"):
                safety.check_write(data, profile)
            assert not profile.setting_grammar().fullmatch(b"TG%d" % tg)
    # the values each model turns into the roll window are never one of its mats
    for tg in set(range(14)) - ACCEPTED_TG[profile]:
        assert tg not in own


# --- FM: only FM1 after TG9 FN0 TB50,0, on the MK-II's 24 x 24 in mat ----------------------------
def test_expand_mode_only_on_the_mk2():
    assert MK2.dialect.expand_mode is True and safety.expand_allowed(MK2)
    for p in (C5, PLUS):
        assert p.dialect.expand_mode is False and not safety.expand_allowed(p)
        assert not p.identity.fg_models.isdisjoint(safety.NO_EXPAND_MODELS)
        assert all(m.header_extra is None for m in p.mats)
    assert [m.key for m in MK2.mats if m.header_extra is HeaderExtra.EXPAND] == ["24x24"]
    assert MK2.mat("24x24").tg == safety.EXPAND_TG == 9


def test_the_header_carries_fm1_only_for_the_mk2_24x24_mat():
    """Studio's 24 x 24 in header is TG9 FN0 TB50,0 FM1 (docs/PROTOCOL.md section 5.2)."""
    assert MK2.mat_texts("24x24") == ("TG9", "FN0", "TB50,0", "FM1")
    for p in ALL:
        for m in p.mats:
            if (p, m.key) != (MK2, "24x24"):
                assert p.mat_texts(m.key) == (f"TG{m.tg}", "FN0", "TB50,0"), (p.id, m.key)
    safety.check_write(write(b"TG9", b"FN0", b"TB50,0", b"FM1"), MK2)


TG9_HEADER = (b"TG9", b"FN0", b"TB50,0")


@pytest.mark.parametrize("profile, data", [
    (MK2, write(b"TG1", b"FN0", b"TB50,0", b"FM1")),        # a 12-inch mat
    (MK2, write(b"TG8", b"FN0", b"TB50,0", b"FM1")),        # 15 x 15 in
    (MK2, write(b"TG2", b"FM1")),
    (MK2, write(b"TG9", b"FM1", b"FN0", b"TB50,0")),        # before FN0 TB50,0: not Studio's
    (MK2, write(b"TG9", b"FM1")),
    (MK2, write(b"TG9", b"FN0", b"FM1")),
    (MK2, write(b"TG9", b"TB50,0", b"FM1")),
    (MK2, write(b"FN0", b"TB50,0", b"FM1")),
    (MK2, write(b"FM1")),
    (MK2, write(*TG9_HEADER, b"FM0")),
    (MK2, write(*TG9_HEADER, b"FM1,1")),
    (MK2, write(*TG9_HEADER, b" FM1")),
    (MK2, write(*TG9_HEADER, b"fm1")),
    (MK2, write(*TG9_HEADER, b"FM1", b"FM1")),
    (C5, write(b"TG1", b"FN0", b"TB50,0", b"FM1")),
    (C5, write(*TG9_HEADER, b"FM1")),
    (PLUS, write(b"TG8", b"FN0", b"TB50,0", b"FM1")),
    (PLUS, write(b"TG1", b"FN0", b"TB50,0", b"FM1")),
    (PLUS, write(*TG9_HEADER, b"FM1")),
], ids=repr)
def test_fm_anywhere_else_is_refused(profile, data):
    with pytest.raises(SafetyError):
        safety.check_write(data, profile)


@pytest.mark.parametrize("bad", [
    replace(C5, dialect=replace(C5.dialect, expand_mode=True)),
    replace(PLUS, dialect=replace(PLUS.dialect, expand_mode=True)),
    replace(PLUS, mats=tuple(replace(m, header_extra=HeaderExtra.EXPAND) if m.key == "15x15"
                             else m for m in PLUS.mats)),
    replace(MK2, mats=tuple(replace(m, header_extra=HeaderExtra.EXPAND) if m.key == "12x12"
                            else m for m in MK2.mats)),
    replace(MK2, dialect=replace(MK2.dialect, expand_mode=False)),
    replace(MK2, identity=replace(MK2.identity, shared_fg_models=frozenset({"CAMEO 5"}))),
], ids=["cameo5-expand", "plus-expand", "plus-15x15-fm", "mk2-12x12-fm", "mk2-no-expand-data",
        "mk2-says-cameo5"])
def test_the_self_check_refuses_fm_out_of_place(bad):
    with pytest.raises(UnsafeProfile, match="FM|expand"):
        safety.check_profile(bad)


# --- status, stop and queries -------------------------------------------------------------------
@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_status_digits(profile):
    s = profile.status
    assert s.as_dict() == {"0": "Ready", "1": "Busy", "2": "Load Media", "3": "Paused",
                           "4": "Cancelling", "5": "Rejected"}
    assert (s.ready, s.busy, s.load, s.unloaded_after_header) == ("0", "1", "2", True)
    for reply in ("6", "10", "11", "12", "9", "", " 0", "0 ", "00", "0\x03", None, 0, b"0"):
        assert not s.is_status(reply), repr(reply)    # unknown digits abort the step


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_the_stop_is_esc_09_twice(profile):
    assert profile.stop.writes == (ESC + b"\x09", ESC + b"\x09") and profile.stop.gap_s == 0.3
    assert all(safety.is_stop_write(w) for w in profile.stop.writes)


QUERIES = [b"FG", b"TB71", b"FA", b"FQ0", b"FQ2", b"TI", b"TO", b"TC", b"G", b"[", b"U"]


@pytest.mark.parametrize("profile", ALL, ids=lambda p: p.id)
def test_queries(profile):
    q = profile.queries
    assert q.esc == {0x05, 0x10, 0x15, 0x16} and q.text == set(QUERIES) and q.max_write == 20
    for code in q.esc:
        safety.check_query(ESC + bytes([code]), profile)
    for text in QUERIES:
        safety.check_query(text + ETX, profile)
    for data in (ESC + b"\x11", ESC + b"\x12", ESC + b"\x13", ESC + b"\x09", ESC + b"\x07",
                 ESC + b"\x00", ESC + b"\x15\x15", b"APS1" + ETX, b"FPG" + ETX,
                 b"FPGRFCC1" + ETX, b"FM1" + ETX, b"TB24" + ETX, b"TR1" + ETX, b"TS1" + ETX,
                 b"ACT1" + ETX, b"FQ1" + ETX, b"TB72" + ETX, b"fg" + ETX, b"FG", b"FG" + ETX * 2,
                 b"U" + ETX + b"G" + ETX):
        with pytest.raises(SafetyError):
            safety.check_query(data, profile)
    with pytest.raises(SafetyError):                    # before the gate: ESC 05 and FG only
        safety.check_query(ESC + b"\x15")
    safety.check_query(b"FG" + ETX)


# --- repository policy --------------------------------------------------------------------------
def test_the_module_holds_typed_values_in_our_own_words():
    source = inspect.getsource(c5)
    # Studio notation, its file names and firmware code or RAM addresses
    assert not re.search(STUDIO_NOTATION.pattern, source, re.IGNORECASE)
    assert not FIRMWARE_ADDRESS.search(source)
    # Studio's class names, column names and enum keys (kept as digests in tests/studio_names.py)
    assert not studio_names(source, profile_module=True)
