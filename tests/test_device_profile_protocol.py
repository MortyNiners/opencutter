# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""device/protocol.py and device/programs.py with machine profiles.

The query allowlist before the model gate (ESC 05 and FG only) and per profile, always inside the
global rules of machines.safety; the ESC 05 replies, the ESC 15 grammar (two digits on the CAMEO
5 generation, the one-holder slot rule) and the stop of each profile; the U check against the
exact (a, b) pair of the mat in use, with 'unchecked' for a mat whose U reply is not known; the
model gate names built on machines.detect; and the pinned programs keyed by profile id, which
only the CAMEO 4 has. Without a profile every function keeps the CAMEO 4's behaviour. Pure
checks on the stub Bluetooth of tests/stubs.py: nothing reaches a machine.
"""
import dataclasses
import re
from collections.abc import Callable

import pytest

import stubs

stubs.install()
from opencutter import machines  # noqa: E402
from opencutter.device import programs, protocol  # noqa: E402
from opencutter.device.programs import Step  # noqa: E402
from opencutter.device.protocol import UnsafeCommand  # noqa: E402
from opencutter.machines import (CAMEO4, Queries, StopSequence, Support, UBasis,  # noqa: E402
                                 by_id, profiles, safety)
from opencutter.machines import cameo5 as c5  # noqa: E402
from opencutter.machines import portrait as pt  # noqa: E402
from opencutter.machines.detect import detect, fg_text  # noqa: E402
from opencutter.machines.safety import SafetyError  # noqa: E402

ESC = b"\x1b"
ETX = b"\x03"
PROFILES = profiles()
OTHERS = tuple(p for p in PROFILES if p is not CAMEO4)
CAMEO5 = c5.CAMEO5
PORTRAIT3, PORTRAIT4 = pt.PORTRAIT3, pt.PORTRAIT4
ALPHA = by_id("cameo5_alpha")


def replace(obj, **changes):
    return dataclasses.replace(obj, **changes)


def accepted(check, data) -> bool:
    try:
        check(data)
    except (UnsafeCommand, SafetyError):
        return False
    return True


# --- the query allowlist -----------------------------------------------------------------------
QUERY_CORPUS = sorted({
    *(ESC + bytes([code]) for code in range(256)), ESC, ESC + b"\x05\x05", ESC + b"\x05\x03",
    *(q + ETX for q in safety.READ_ONLY_QUERIES), *safety.READ_ONLY_QUERIES,
    b"FG\x03\x03", b"FG\x03FG\x03", b"fg\x03", b"Fg\x03", b"FP\x03", b"FPG\x03", b"FM1\x03",
    b"FM0\x03", b"M0,0\x03", b"TG1\x03", b"TB72\x03", b"TB70\x03", b"\x00FG\x03", b" FG\x03",
    b"FG \x03", b"U\x03\x00", b"", ETX, b"X" * 20 + ETX, b"FQ1\x03", b"FQ3\x03", b"TB71,1\x03",
    b"\x1bFG\x03", b"U" + b" " * 30 + ETX, b"APS\x03", b"BZK\x03", b"TS\x03", b"TB24\x03",
})


@pytest.mark.parametrize("data", QUERY_CORPUS, ids=repr)
def test_without_a_query_set_the_cameo4_allowlist_holds(data):
    ok = accepted(protocol.check_safe, data)
    assert ok == accepted(lambda d: protocol.check_safe(d, None), data)
    assert ok == accepted(lambda d: protocol.check_safe(d, CAMEO4), data)
    assert ok == accepted(lambda d: protocol.check_safe(d, CAMEO4.queries), data)
    esc = len(data) == 2 and data[:1] == ESC and data[1] in protocol.SAFE_ESC_QUERIES
    assert ok == (esc or (data[-1:] == ETX and data[:-1] in protocol.SAFE_TEXT_QUERIES))


@pytest.mark.parametrize("data", QUERY_CORPUS, ids=repr)
def test_before_the_gate_only_esc05_and_fg(data):
    ok = accepted(lambda d: protocol.check_safe(d, queries=protocol.PRE_GATE), data)
    assert ok == (data in (b"\x1b\x05", b"FG\x03"))
    assert ok == accepted(lambda d: safety.check_query(d, None), data)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_each_profile_allows_its_own_queries_and_nothing_else(profile):
    for data in QUERY_CORPUS:
        ok = accepted(lambda d: protocol.check_safe(d, queries=profile.queries), data)
        assert ok == accepted(lambda d: protocol.check_safe(d, profile), data), data
        assert ok == accepted(lambda d: safety.check_query(d, profile), data), data
    for code in profile.queries.esc:
        protocol.check_safe(ESC + bytes([code]), profile)
    for text in profile.queries.text:
        protocol.check_safe(text + ETX, profile)


def test_the_getters_other_models_lack_are_refused_there():
    for profile in (PORTRAIT3, PORTRAIT4):
        for text in (b"TO", b"TC"):
            protocol.check_safe(text + ETX)                     # the CAMEO 4 has them
            with pytest.raises(UnsafeCommand):
                protocol.check_safe(text + ETX, profile)
    for text in sorted(CAMEO4.queries.text - ALPHA.queries.text):
        with pytest.raises(UnsafeCommand):
            protocol.check_safe(text + ETX, ALPHA)


def test_a_query_set_cannot_widen_the_global_rules():
    wide = Queries(esc=frozenset({0x05, 0x07, 0x09, 0x11, 0x12, 0x13, 0x00}),
                   text=frozenset({b"FG", b"FP", b"FPG", b"fg", b"M0,0", b"FM1", b"TB72", b"TS",
                                   b"X" * 25}),
                   max_write=40)
    allowed = {d for d in QUERY_CORPUS + [b"X" * 25 + ETX, b"TS\x03"]
               if accepted(lambda d: protocol.check_safe(d, wide), d)}
    assert allowed == {b"FG\x03", b"\x1b\x05"}


@pytest.mark.parametrize("data", [bytearray(b"FG\x03"), "FG\x03", memoryview(b"FG\x03"), None,
                                  bytearray(b"\x1b\x05")])
def test_a_query_must_be_bytes(data):
    with pytest.raises(UnsafeCommand):
        protocol.check_safe(data)


@pytest.mark.parametrize("queries", [
    "cameo4", 0, frozenset({b"FG"}), object(),
    Queries(esc=[0x05], text=frozenset({b"FG"})), Queries(esc=frozenset({0x05}), text={b"FG"}),
    Queries(esc=frozenset({0x05}), text=frozenset({b"FG"}), max_write=None),
    Queries(esc=frozenset({0x05}), text=frozenset({b"FG"}), max_write=20.0),
    replace(CAMEO4, queries=None),
])
def test_an_unknown_or_malformed_query_set_refuses_everything(queries):
    for data in (b"FG\x03", b"\x1b\x05", b"\x1b\x07"):
        with pytest.raises(UnsafeCommand):
            protocol.check_safe(data, queries)


def test_the_cameo4_aliases_come_from_its_profile():
    assert protocol.SAFE_ESC_QUERIES == CAMEO4.queries.esc == frozenset({0x05, 0x10, 0x15, 0x16})
    assert protocol.SAFE_TEXT_QUERIES == CAMEO4.queries.text
    assert protocol.MAX_QUERY_WRITE == CAMEO4.queries.max_write == 20
    assert protocol.MAX_DATA_WRITE == CAMEO4.max_write == 182
    assert protocol.STATUS_TEXT == CAMEO4.status.as_dict()
    assert protocol.TOOL_NAMES == CAMEO4.tool_names()
    assert protocol.PRE_GATE.esc == {0x05} and protocol.PRE_GATE.text == {b"FG"}


# --- ESC 05 ------------------------------------------------------------------------------------
@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_status_replies_are_the_profiles_and_exact(profile):
    texts = profile.status.as_dict()
    for reply in [*texts, "6", "9", "10", "12", "", " 0", "0 ", "00", "0\x03", "٠", None, 0, b"0"]:
        assert protocol.is_status(reply, profile) is (reply in texts if isinstance(reply, str)
                                                      else False), repr(reply)
        want = texts.get(reply, "?") if isinstance(reply, str) else "?"
        assert protocol.status_text(reply, profile) == want
    assert protocol.status_text("6", profile, default="unknown") == "unknown"


def test_status_defaults_to_the_cameo4():
    for reply in ["0", "5", "6", "    0"]:
        assert protocol.is_status(reply) is protocol.is_status(reply, CAMEO4)
        assert protocol.status_text(reply) == protocol.STATUS_TEXT.get(reply, "?")


def test_a_status_digit_a_profile_does_not_list_is_no_status():
    four = replace(CAMEO4, status=replace(CAMEO4.status, texts=CAMEO4.status.texts[:5]))
    assert protocol.is_status("4", four) and not protocol.is_status("5", four)
    assert protocol.status_text("5", four) == "?"


# --- ESC 15 ------------------------------------------------------------------------------------
@pytest.mark.parametrize("reply, cameo4, cameo5", [
    (" 2, 4", (2, 4), (2, 4)), (" 7, 0", (7, 0), (7, 0)), (" 0, 0", (0, 0), (0, 0)),
    (" 15, 2", None, (15, 2)), (" 2, 18", None, (2, 18)), (" 10, 10", None, (10, 10)),
    (" 99, 99", None, (99, 99)), (" 05, 2", None, None), (" 100, 2", None, None),
    (" 2, 015", None, None), (" 15,2", None, None), ("15, 2", None, None), (" 15, 2 ", None, None),
    ("  2, 4", None, None), (" 2, 4\x03", None, None), (" -1, 2", None, None),
    (" ٢, ٤", None, None), (" 2, 4, 1", None, None), ("", None, None), (None, None, None),
])
def test_tool_replies_follow_the_profiles_grammar(reply, cameo4, cameo5):
    assert protocol.parse_tools(reply) == cameo4
    assert protocol.parse_tools(reply, CAMEO4) == cameo4
    for profile in (CAMEO5, c5.CAMEO5_PLUS, c5.CAMEO_PRO_MK2, ALPHA):
        assert protocol.parse_tools(reply, profile) == cameo5, profile.id


@pytest.mark.parametrize("reply, codes", [
    (" 7, 0", (7,)), (" 0, 0", (0,)), (" 2, 0", (2,)), (" 7, 3", None), (" 0, 7", None),
    (" 7, 9", None), (" 12, 0", None), (" 7,0", None), ("", None),
])
def test_a_one_holder_model_reads_the_first_slot_and_the_other_must_be_0(reply, codes):
    for profile in (PORTRAIT3, PORTRAIT4):
        assert protocol.parse_tools(reply, profile) == codes


def test_tool_names_and_the_holder_labels():
    assert protocol.describe_tools(" 2, 4") == "Tool 1: AutoBlade, Tool 2: Kraft Blade, 3mm"
    assert protocol.describe_tools(" 2, 4", CAMEO4) == protocol.describe_tools(" 2, 4")
    assert protocol.describe_tools(" 7, 0", PORTRAIT4) == "Tool: Sketch Pen"
    assert protocol.describe_tools(" 8, 0", PORTRAIT4) == "Tool: Foil Tool"
    assert protocol.describe_tools(" 8, 0", PORTRAIT3) == "Tool: unknown tool 8"
    assert protocol.describe_tools(" 7, 3", PORTRAIT4) == "unclear reply ' 7, 3'"
    assert protocol.describe_tools(" 15, 8", CAMEO5) == "Tool 1: unknown tool 15, Tool 2: Foil Tool"
    assert protocol.describe_tools(" 15, 8") == "unclear reply ' 15, 8'"
    assert protocol.tool_name(1, 8) == "unknown tool 8" and protocol.tool_name(2, 8) == "Foil Tool"
    assert protocol.tool_name(1, 8, CAMEO5) == "Foil Tool"
    assert protocol.tool_name(2, 7, PORTRAIT3) == "unknown tool 7"
    for profile in PROFILES:
        for h in profile.holders:
            for t in h.tools:
                assert protocol.tool_name(h.number, t.code, profile) == t.name


# --- the stop ----------------------------------------------------------------------------------
@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_every_profile_stops_with_esc09_twice(profile):
    assert protocol.stop_sequence(profile) == (b"\x1b\x09", b"\x1b\x09")


def test_the_stop_defaults_to_the_cameo4_and_refuses_anything_else():
    assert protocol.stop_sequence() == protocol.STOP_SEQUENCE == CAMEO4.stop.writes
    for writes in [(b"\x1b\x09",), (b"\x1b\x09",) * 3, (b"\x1b\x07", b"\x1b\x09"),
                   (b"\x1b\x09\x1b\x09",), [b"\x1b\x09", b"\x1b\x09"], (b"\x1b\x00", b"\x1b\x09")]:
        with pytest.raises(UnsafeCommand):
            protocol.stop_sequence(replace(CAMEO4, stop=StopSequence(writes, 0.3)))


# --- writes that lower a tool ------------------------------------------------------------------
@pytest.mark.parametrize("data, lowers", [
    (b"M1,1\x03", False), (b"M1,1\x03D2,2\x03", True), (b"J1\x03TF1,1\x03", True),
    (b"TG1\x03FN0\x03", False), (b"D1,1\x03", True), (b" D1,1\x03", True), (b"0TF1,1\x03", True),
    (b"\x00D1,1\x03", True), (b"FN0\x03\\0,0\x03", False), (b"", False),
    (b"M1,1D2,2\x03", True), (b"FX10,TF1,1\x03", True), (b"J1\x1bD1,1\x03", True),
])
def test_lowers_tool(data, lowers):
    assert protocol.lowers_tool(data) is lowers
    for profile in PROFILES:
        assert protocol.lowers_tool(data, profile) is lowers, profile.id


def test_lowers_tool_reads_the_profiles_prefixes():
    pen_only = replace(CAMEO4, lowering_prefixes=(b"D",))
    assert not protocol.lowers_tool(b"TF1,1\x03", pen_only)
    assert protocol.lowers_tool(b"D1,1\x03", pen_only)


def test_lowers_tool_gives_the_first_letter_answer_for_every_pinned_write():
    for data in programs.PINNED_WRITES:
        first = any(c.startswith(CAMEO4.lowering_prefixes) for c in data.split(ETX))
        assert protocol.lowers_tool(data) is first, data


# --- the units check ---------------------------------------------------------------------------
@pytest.mark.parametrize("reply, verdict", [
    ("6100,6100", "ok"), ("  6100,  6100", "ok"), ("6100, 6100", "ok"),
    ("6100,4120", "mismatch"), ("4120,6100", "mismatch"), ("12200,6100", "mismatch"),
    ("3050,3050", "mismatch"), ("6100", "mismatch"), ("6100,6100,6100", "mismatch"),
    ("06100,6100", "mismatch"), ("-6100,6100", "mismatch"), ("U6100,6100", "mismatch"),
    ("", "unknown"), ("?", "unknown"), ("  ??", "unknown"), ("٦١٠٠,٦١٠٠", "unknown"),
    (None, "unknown"), (6100, "unknown"),
])
def test_the_12x12_mat_needs_exactly_6100_6100(reply, verdict):
    assert protocol.evaluate_units(reply)[0] == verdict
    assert protocol.evaluate_units(reply, CAMEO4.mat("12x12"))[0] == verdict
    assert protocol.evaluate_units(reply, (6100, 6100))[0] == verdict


def test_the_pair_is_compared_in_order_for_each_mat():
    m12x24, m8x12 = c5.MAT_12X24, c5.MAT_8X12
    assert m12x24.u_basis is UBasis.INFERRED and m8x12.u_basis is UBasis.INFERRED
    for mat in (m12x24, m8x12, c5.MAT_12X12):
        a, b = mat.expected_u
        assert protocol.evaluate_units(f"  {a},  {b}", mat)[0] == "ok"
        assert protocol.evaluate_units(f"{a},{b}", mat.expected_u)[0] == "ok"
        assert protocol.evaluate_units(f"{b + 1},{a}", mat)[0] == "mismatch"
    assert m12x24.expected_u == (12200, 6100) and m8x12.expected_u == (6100, 4120)
    assert protocol.evaluate_units("6100,12200", m12x24)[0] == "mismatch"
    assert protocol.evaluate_units("6100,6100", m12x24)[0] == "mismatch"
    assert protocol.evaluate_units("6100,6100", m8x12)[0] == "mismatch"
    assert protocol.evaluate_units("6100,4120", m8x12)[0] == "ok"
    assert protocol.evaluate_units("6100,4120", (6100, 4120))[0] == "ok"


def test_the_messages_name_the_mat_and_ask_for_a_report_when_inferred():
    verdict, msg = protocol.evaluate_units("6100,6100", c5.MAT_12X24)
    assert verdict == "mismatch" and "TG2" in msg and "12200,6100" in msg and "report" in msg
    verdict, msg = protocol.evaluate_units("12200,12200")
    assert verdict == "mismatch" and "0.025 mm" in msg
    verdict, msg = protocol.evaluate_units("6100,4120")
    assert verdict == "mismatch" and "TG1" in msg and "6100,6100" in msg and "report" in msg
    verdict, msg = protocol.evaluate_units("6100", c5.MAT_12X24)
    assert verdict == "mismatch" and "firmware analysis" in msg and "report" in msg
    assert protocol.evaluate_units("  6100,  6100") == (
        "ok", "U = '6100,  6100': 0.05 mm per unit, confirmed")


def test_an_unknown_u_is_logged_and_shown_but_never_enforced():
    mat = PORTRAIT3.mat(PORTRAIT3.default_mat)
    assert mat.u_basis is UBasis.UNKNOWN and mat.expected_u is None and not mat.enforce_u
    for reply in ["6100,4120", "  6100,  4120", "1,1", "0,0", "99999,99999"]:
        # the mat itself, or what a plan for it carries: (units_expected, units_enforced)
        for args in [(mat,), (mat, None), (mat, False), (None, False), ((6100, 6100), False)]:
            verdict, msg = protocol.evaluate_units(reply, *args)
            assert verdict == "unchecked", (reply, args)
            assert "could not be checked" in msg and reply.strip() in msg
    # a reply that is not a U reply is still not accepted
    assert protocol.evaluate_units("", mat)[0] == "unknown"
    assert protocol.evaluate_units("  ??", None, False)[0] == "unknown"
    assert protocol.evaluate_units("6100", mat)[0] == "mismatch"
    assert protocol.evaluate_units("6100,6100,1", None, False)[0] == "mismatch"


@pytest.mark.parametrize("args", [(None,), (None, None), (None, True),
                                  (PORTRAIT3.mat(PORTRAIT3.default_mat), True)])
def test_a_missing_pair_alone_never_passes(args):
    # Plan's defaults (units_expected None, units_enforced True) are a check that cannot pass,
    # and so is None without enforced=False: only an UNKNOWN U is left unchecked.
    for reply in ["6100,6100", "  6100,  6100", "0,0"]:
        verdict, msg = protocol.evaluate_units(reply, *args)
        assert verdict == "mismatch" and "cannot be confirmed" in msg, (reply, args)


def test_a_known_u_cannot_be_switched_off():
    for mat in (CAMEO4.mat("12x12"), c5.MAT_12X24):
        a, b = mat.expected_u
        assert protocol.evaluate_units(f"{a},{b}", mat, False)[0] == "ok"
        assert protocol.evaluate_units(f"{b},{a + 1}", mat, False)[0] == "mismatch"
        assert protocol.evaluate_units(f"{b},{a + 1}", mat, True)[0] == "mismatch"
    assert protocol.evaluate_units("6100,4120", (6100, 6100), True)[0] == "mismatch"
    assert protocol.evaluate_units("6100,6100", (6100, 6100), None)[0] == "ok"


@pytest.mark.parametrize("mat", [
    replace(CAMEO4.mat("12x12"), expected_u=None),
    replace(CAMEO4.mat("12x12"), expected_u=(6100,)),
    replace(CAMEO4.mat("12x12"), expected_u=(6100.0, 6100)),
    replace(c5.MAT_12X24, expected_u=None),
    replace(CAMEO4.mat("12x12"), u_basis="unknown", expected_u=None),   # not a UBasis: enforced
])
def test_an_enforced_mat_without_a_pair_is_a_programming_error(mat):
    with pytest.raises(ValueError):
        protocol.evaluate_units("6100,6100", mat)


@pytest.mark.parametrize("enforced", [0, 1, "yes", "False"])
def test_enforced_is_a_bool(enforced):
    with pytest.raises(ValueError):
        protocol.evaluate_units("6100,6100", (6100, 6100), enforced)


@pytest.mark.parametrize("reply", ["9" * 10 + ",6100", "6100," + "1" * 5000, "1" * 5000])
def test_overlong_numbers_are_no_u_reply(reply):
    assert protocol.evaluate_units(reply)[0] == "mismatch"
    assert protocol.evaluate_units(reply, None, False)[0] == "mismatch"


@pytest.mark.parametrize("expected", [(6100,), (6100, 6100, 1), (0, 6100), (6100.0, 6100),
                                      [6100, 6100], (True, 6100), "6100,6100", 6100])
def test_a_malformed_expectation_is_a_programming_error(expected):
    with pytest.raises(ValueError):
        protocol.evaluate_units("6100,6100", expected)


def test_the_verdicts_are_the_four_documented_ones():
    assert protocol.UNITS_VERDICTS == ("ok", "mismatch", "unknown", "unchecked")
    assert protocol.DEFAULT_UNITS_MAT is CAMEO4.mat("12x12")


# --- the model gate ----------------------------------------------------------------------------
GATE_CORPUS = [
    "CAMEO 4 V5.05    ", "CAMEO 4 V5.05", "CAMEO 4 V5.05\r\n", b"CAMEO 4 V5.05    \x03",
    "CAMEO 4 V1.20    ", "CAMEO 4 V1.20", "CAMEO 4 PLUS V5.05    ", "CAMEO 4 PRO V5.05    ",
    "CAMEO 5 V1.07    ", "CAMEO 5 PLUS V1.07    ", "CAMEO 5 PRO V1.07    ",
    "CAMEO 5 ALPHA V1.04    ", "CAMEO PRO MARK2 V1.10    ", "Portrait 3 V1.05    ",
    "Portrait 4 V1.05    ", "CURIO 2 V1.16    ", "CAMEO 3 V1.60    ", "CAMEO 4 V9.99    ",
    "CAMEO 4 V15.05", "", "   ", None, 405, ["CAMEO 4 V5.05"], b"\xffCAMEO 4 V5.05",
]


@pytest.mark.parametrize("reply", GATE_CORPUS, ids=ascii)
def test_is_tested_model_is_detects_tested(reply):
    d = detect(reply)
    assert protocol.is_tested_model(reply) is d.tested
    assert d.tested is (d.profile is CAMEO4 and d.line is not None
                        and d.line.support is Support.TESTED)


def test_the_tested_machine_text_comes_from_the_registry():
    assert machines.tested() == (CAMEO4,)
    assert protocol.TESTED_FIRMWARE == "V5.xx" and protocol.TESTED_MACHINE == \
        "CAMEO 4 (firmware V5.xx)"
    assert protocol._fg_text is fg_text and protocol._FG_PADDING == "\x03\x00 \t\n\r\x0b\x0c"
    assert protocol.wrong_model_message("CAMEO 3 V1.60    ") == (
        'This machine reports itself as "CAMEO 3 V1.60". OpenCutter cannot drive it and will '
        "not send anything to it.")
    assert protocol.reported_model(b"  \x03") is None
    assert protocol.reported_model("Portrait 3 V1.05    ") == "Portrait 3 V1.05"


# --- pinned programs ---------------------------------------------------------------------------
def test_programs_are_keyed_by_profile_and_the_cameo4_names_are_aliases():
    assert set(programs.PINNED_PROGRAMS) == set(programs.PROGRAMS_BY_PROFILE) == {"cameo4"}
    pinned = programs.PINNED_PROGRAMS["cameo4"]
    assert programs.PROGRAMS_BY_PROFILE["cameo4"] is programs.PROGRAMS is pinned.programs
    assert pinned.profile is CAMEO4 and pinned.mat == "12x12"
    assert pinned.required_tools is programs.REQUIRED_TOOLS
    assert pinned.writes == programs.PINNED_WRITES
    assert pinned.validator is programs.validate_pinned
    assert pinned.test_area == programs.TEST_AREA and pinned.names == CAMEO4.programs
    assert programs.PROGRAMS[("dry-run", 0)] is programs.DRY_RUN
    assert programs.PROGRAMS[("cut-test", 0)] is programs.CUT_TEST


@pytest.mark.parametrize("name, options", [
    ("dry-run", {}), ("cut-test", {}), ("pen-test", {}), ("pen-test", {"pen_force": 7}),
    ("pen-test", {"pen_force": 1, "allow_unrecognized_pen": True}),
])
def test_the_cameo4_programs_are_unchanged_with_its_profile(name, options):
    default = programs.build_program(name, options)
    explicit = programs.build_program(name, options, CAMEO4)
    assert default == explicit and default.profile_id == explicit.profile_id == "cameo4"
    variant = options.get("pen_force", 1) if name == "pen-test" else 0
    assert explicit.steps is programs.PROGRAMS[(name, variant)]
    assert explicit.validator is programs.validate_pinned
    assert explicit.units_expected == (6100, 6100) == CAMEO4.mat("12x12").expected_u
    assert explicit.units_enforced is True
    assert protocol.evaluate_units("6100,6100", explicit.units_expected,
                                   explicit.units_enforced)[0] == "ok"
    assert (explicit.batch_bytes, explicit.slowest_su_per_s, explicit.max_coordinate_su) == (
        CAMEO4.batch_bytes, CAMEO4.slowest_su_per_s, CAMEO4.max_coordinate_su)
    assert explicit.summary == {"program": name, "options": explicit.options}


@pytest.mark.parametrize("profile", OTHERS, ids=lambda p: p.id)
def test_a_profile_without_programs_gets_none(profile):
    assert profile.programs == frozenset()
    for name, options in [("dry-run", {}), ("pen-test", {"pen_force": 1}), ("cut-test", {})]:
        with pytest.raises(ValueError, match=f"no test programs for the {re.escape(profile.name)}"):
            programs.build_program(name, options, profile)


@pytest.mark.parametrize("profile", [
    replace(CAMEO4, max_write=20), replace(CAMEO4, name="CAMEO 4 copy"), None, "cameo4",
    replace(CAMEO5, id="cameo4"),
])
def test_only_the_registrys_own_profile_runs_the_pinned_bytes(profile):
    with pytest.raises(ValueError, match="no test programs"):
        programs.build_program("dry-run", {}, profile)


@pytest.mark.parametrize("force", [1.0, 7.0, True, "7", None, 0, 2])
def test_the_pen_force_is_one_of_the_pinned_variants_and_an_int(force):
    with pytest.raises(ValueError, match="pen_force must be 1 or 7"):
        programs.build_program("pen-test", {"pen_force": force}, CAMEO4)


@pytest.mark.parametrize("profile_id", ["cameo5", "", None, ("cameo4",)])
def test_a_program_of_an_unknown_profile_writes_nothing(profile_id):
    prog = programs.Program("dry-run", {}, programs.DRY_RUN, {}, profile_id=profile_id)
    assert prog.units_expected is None and prog.units_enforced is True
    assert protocol.evaluate_units("6100,6100", prog.units_expected,
                                   prog.units_enforced)[0] == "mismatch"
    for data in sorted(programs.PINNED_WRITES):
        with pytest.raises(UnsafeCommand):
            prog.validator(data)


def test_the_pinned_writes_pass_the_global_safety_check():
    safety.check_profile(CAMEO4, pinned=programs.PINNED_PROGRAMS["cameo4"].writes)
    for data in programs.PINNED_WRITES:
        safety.check_write(data, CAMEO4)


# --- the import-time self-check ----------------------------------------------------------------
GOOD = programs.PINNED_PROGRAMS["cameo4"]
EVERY = re.compile(rb".*", re.DOTALL)


def exact(writes: frozenset) -> Callable[[bytes], None]:
    """A validator that takes exactly `writes`, like validate_pinned for the real programs."""
    def check(data: bytes) -> None:
        if data not in writes:
            raise UnsafeCommand("not a pinned motion write")
    return check


def pinned_with(**changes) -> dict:
    """GOOD with `changes` and, unless one is given, a validator for exactly its writes: so a
    case is refused for the reason it names, not because validate_pinned lacks a new write."""
    new = replace(GOOD, **changes)
    if "validator" not in changes:
        new = replace(new, validator=exact(new.writes))
    return {"cameo4": new}


def program_with(*steps: Step, required: dict | None = None, name: str = "dry-run",
                 **changes) -> dict:
    progs = {**GOOD.programs, (name, 0): tuple(steps)}
    tools = {**GOOD.required_tools, name: required if required is not None else {}}
    return pinned_with(programs=progs, required_tools=tools, **changes)


HEADER, UNITS, TRAILER = programs.DRY_RUN[0], programs.DRY_RUN[1], programs.DRY_RUN[-1]


def write(data: bytes) -> Step:
    return Step("write", (data,), "a step", "what happens")


def test_the_self_check_passes_today():
    programs._self_check(programs.PINNED_PROGRAMS, PROFILES)


def test_the_self_check_reads_a_good_copy_as_good():
    programs._self_check({"cameo4": replace(GOOD)}, [replace(p) for p in PROFILES])
    programs._self_check(pinned_with(), PROFILES)
    pen = {**GOOD.programs, ("pen-test", 3): GOOD.programs[("pen-test", 1)]}
    programs._self_check(pinned_with(programs=pen), PROFILES)   # another pen force: allowed


@pytest.mark.parametrize("pinned, registered, reason", [
    ({}, PROFILES, "lists the programs"),                     # the CAMEO 4 lists programs
    (programs.PINNED_PROGRAMS, [*PROFILES, replace(CAMEO5, id="cameo5_x", name="X",
                                                   programs=frozenset({"dry-run"}))],
     "lists the programs"),
    (programs.PINNED_PROGRAMS, OTHERS, "registry's profile"),  # not a registered profile
    (pinned_with(profile=replace(CAMEO4, max_write=100)), PROFILES, "registry's profile"),
    ({"cameo4x": GOOD}, PROFILES, "registry's profile"),
    ({"cameo5": replace(GOOD, profile=CAMEO5)}, PROFILES, "differ from the profile's"),
    (pinned_with(programs={k: v for k, v in GOOD.programs.items() if k[0] != "cut-test"}),
     PROFILES, "differ from the profile's"),
    (pinned_with(required_tools={**GOOD.required_tools, "extra": {}}), PROFILES,
     "differ from the profile's"),
    (pinned_with(mat="12x24"), PROFILES, "mat whose U reply is checked"),
    (pinned_with(test_area=(6067, 3400)), PROFILES, "test area"),   # beyond the mat's cut area
    (pinned_with(test_area=(1600, 6067)), PROFILES, "test area"),
    (pinned_with(test_area=(0, 3400)), PROFILES, "test area"),
    (pinned_with(test_area=[1600, 3400]), PROFILES, "test area"),
    (pinned_with(validator=lambda data: None), PROFILES, "validator accepts"),
    (pinned_with(validator=exact(GOOD.writes | {b"M0,0\x03"})), PROFILES, "validator accepts"),
    (pinned_with(validator=exact(GOOD.writes - {programs.CUT_TEST[-2].writes[0]})), PROFILES,
     "validator refuses"),
    (pinned_with(programs={**GOOD.programs, ("dry-run", 1): programs.DRY_RUN}), PROFILES,
     "variant"),
    (pinned_with(programs={**GOOD.programs, ("pen-test", 0): GOOD.programs[("pen-test", 1)]}),
     PROFILES, "variant"),                                    # the variants build_program reads
    (program_with(UNITS, HEADER, TRAILER), PROFILES, "starts with the header"),
    (program_with(write(b"TG1\x03"), UNITS, TRAILER), PROFILES, "starts with the header"),
    (program_with(HEADER, TRAILER), PROFILES, "starts with the header"),   # no U check
    (program_with(HEADER, UNITS, Step("write", (b"M200,200\x03", b"M0,0\x03"), "t", "e")),
     PROFILES, "one write or the U check"),
    (program_with(HEADER, Step("units", (b"FG\x03",), "t", "e"), TRAILER), PROFILES,
     "one write or the U check"),
    (program_with(HEADER, UNITS, Step("units", (b"U\x03", b"U\x03"), "t", "e"), TRAILER),
     PROFILES, "one write or the U check"),
    (program_with(HEADER, UNITS, Step("move", (b"M200,200\x03",), "t", "e"), TRAILER),
     PROFILES, "one write or the U check"),
    (program_with(HEADER, UNITS, write(b"D200,200\x03"), TRAILER), PROFILES,
     "dry-run must never lower"),
    (program_with(HEADER, UNITS, write(b"D200,200\x03"), TRAILER, required={1: 7}),
     PROFILES, "dry-run must never lower"),                   # even with a tool declared
    (program_with(HEADER, UNITS, write(b"J1TF1,1\x03"), TRAILER, required={1: 7},
                  grammar=EVERY), PROFILES, "dry-run must never lower"),   # TF with no ETX before
    (program_with(HEADER, UNITS, write(b"D200,200\x03"), TRAILER, name="cut-test"), PROFILES,
     "declares no required tool"),
    (program_with(HEADER, UNITS, write(b"D200,200\x03"), TRAILER, name="cut-test",
                  required={1: 5}), PROFILES, "not a usable tool"),   # the Rotary Blade
    (program_with(HEADER, UNITS, write(b"D200,200\x03"), TRAILER, name="cut-test",
                  required={1: 0}), PROFILES, "not a usable tool"),   # no tool detected
    (program_with(HEADER, UNITS, write(b"D200,200\x03"), TRAILER, name="cut-test",
                  required={3: 2}), PROFILES, "not a usable tool"),   # no holder 3
])
def test_the_self_check_refuses(pinned, registered, reason):
    with pytest.raises(RuntimeError, match=re.escape(reason)):
        programs._self_check(pinned, registered)


@pytest.mark.parametrize("data", [b"M1700,200\x03", b"M200,3401\x03", b"FX33,1\x03",
                                  b"M200,200\x03" * 21, b"FP\x03", b"FM1\x03", b"TG2\x03",
                                  b"m200,200\x03", b"M200,200\x1b\x03", b"M200,200"])
def test_a_pinned_write_outside_the_grammar_or_the_test_area_is_refused(data):
    with pytest.raises(UnsafeCommand):
        programs._self_check(program_with(HEADER, UNITS, write(data), TRAILER), PROFILES)


@pytest.mark.parametrize("data", [b"M200,200TF1,1\x03", b"D1,2,3\x03", b"M-1,5\x03", b"M\x03",
                                  b"D200,1700\x03", b"M 200,200\x03", b"M200,0200\x03"])
def test_a_move_outside_the_test_area_is_refused_whatever_the_grammar(data):
    pinned = program_with(HEADER, UNITS, write(data), TRAILER, grammar=EVERY,
                          test_area=(1600, 1600))
    with pytest.raises(UnsafeCommand):
        programs._self_check(pinned, PROFILES)


@pytest.mark.parametrize("data", [b"FP\x03", b"FPG\x03", b"FM1\x03", b"TG2\x03", b"m200,200\x03",
                                  b"TB72,0,0\x03", b"APS5\x03", b" FP\x03", b"0TB70\x03"])
def test_the_global_rules_hold_even_with_a_grammar_that_takes_everything(data):
    pinned = program_with(HEADER, UNITS, write(data), TRAILER, grammar=EVERY)
    with pytest.raises(SafetyError):
        programs._self_check(pinned, PROFILES)
