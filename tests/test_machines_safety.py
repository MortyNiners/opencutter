# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The global never-send rules and the profile self-check (opencutter.machines.safety).

The never-send list is today's (job/validate.py) without FM, which became a per-profile rule,
and with the commands no OpenCutter job needs (APS, ACT, AOC, TS, TR, BZ, BZK, TZ and the
TB24/124/125/1023 scans); FM1 stands only after TG9 FN0 TB50,0; the query rules equal
device/protocol.py's allowlist for the CAMEO 4; and a profile that breaks
a rule (an ESC 11 or lowercase query, a stop that is not ESC 09 twice, FP in a pinned write, FM
outside its place, ...) is refused. Pure checks: the device modules run on the stub Bluetooth
of tests/stubs.py, and nothing reaches a machine.
"""
import dataclasses

import pytest

import stubs

stubs.install()
from opencutter.device import programs, protocol  # noqa: E402
from opencutter.job import validate  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings  # noqa: E402
from opencutter.machines import (CAMEO4, AccelForm, FirmwareLine, HeaderExtra,  # noqa: E402
                                 Identity, IntRange, RecognizedModel, StatusModel,
                                 StopSequence, Support, Tool, ToolReply, UBasis, registry,
                                 safety)
from opencutter.machines import cameo4 as c4  # noqa: E402
from opencutter.machines.safety import SafetyError, UnsafeProfile  # noqa: E402

ESC = b"\x1b"
VINYL = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                     line_segment_overcut=False, overcut=(1, 1), blade_depth=1, passes=2)
PEN = ToolSettings(holder=2, tool_code=7, speed=10, force=7, acceleration=1, offset=0,
                   line_segment_overcut=False, overcut=(0, 0), blade_depth=None)
SQUARE = [(60.0, 60.0), (85.4, 60.0), (85.4, 85.4), (60.0, 85.4), (60.0, 60.0)]


def replace(obj, **changes):
    return dataclasses.replace(obj, **changes)


def mat12(**changes):
    return dataclasses.replace(c4.MAT_12X12, **changes)


# --- the global list ---------------------------------------------------------------------------
TODAY = {b"FB", b"TB72", b"TB70", b"TH", b"TD", b"TE", b"ZF", b"TA", b"SC", b"TN", b"FD", b"FP",
         b"FY", b"FU", b"PW", b"S0", b"S1", b"S2", b"S3", b"S7", b"S8", b"S9"}
STRICTER = {b"AOC", b"ACT", b"TS", b"BZK", b"APS", b"BZ", b"TR", b"TZ", b"TB24", b"TB124",
            b"TB125", b"TB1023"}


def test_the_never_send_list_is_todays_without_fm_and_stricter():
    assert b"FM" in validate._NEVER_SEND                       # today: FM is refused everywhere
    assert TODAY <= set(validate._NEVER_SEND)                   # the job layer may take more
    assert set(validate._NEVER_SEND) - {b"FM"} <= set(safety.NEVER_SEND_PREFIXES)
    assert set(safety.NEVER_SEND_PREFIXES) == TODAY | STRICTER
    assert set(safety.STRICTER_PREFIXES) == STRICTER
    assert len(safety.NEVER_SEND_PREFIXES) == len(TODAY | STRICTER)
    assert safety.FILLER == validate._FILLER
    assert {b"FP", b"PW", b"FB", b"TB72", b"TB70", b"ZF", b"TA", b"TH", b"FY", b"FU", b"TD",
            b"TE", b"FD", b"TN", b"SC"} <= set(safety.NEVER_SEND_PREFIXES)
    assert b"FM" not in safety.NEVER_SEND_PREFIXES


STRICTER_CORPUS = sorted({
    *(p + tail for p in STRICTER for tail in (b"", b"0", b"1", b"1,1", b"30", b"0,0,0")),
    *(filler + p for p in STRICTER for filler in (b" ", b"0", b"+", b"\x00", b"\x7f", b",-.9")),
    *(p.lower() for p in STRICTER), b"APS30", b"ACT1,2,3,4,5", b"AOC1", b"TS0,1", b"TS2",
    b"TR0,1", b"TR1", b"TR99", b"BZ1,1,1,1,1,1,1,1,1,1", b"BZK3", b"TZ1", b"TB24,1", b"TB1240",
    b"TB125,0", b"TB1023,1",
})


@pytest.mark.parametrize("command", STRICTER_CORPUS, ids=repr)
def test_the_stricter_entries_are_refused_everywhere(command):
    assert safety.is_never_send(command)
    assert not safety.text_query_allowed(command, CAMEO4)
    with pytest.raises(SafetyError, match="never-send|printable"):   # NUL, DEL: not printable
        safety.check_write(b"TG1\x03" + command + b"\x03", CAMEO4)
    assert not CAMEO4.setting_grammar().fullmatch(command)


@pytest.mark.parametrize("command", [b"TB50,0", b"TB71", b"TB2", b"TB12", b"TB102", b"TB1",
                                     b"TB5", b"TG1", b"TF1,1", b"TJ3", b"T", b"A", b"B", b"AP",
                                     b"AC", b"AO", b"FN0", b"Z30,30"], ids=repr)
def test_the_stricter_entries_leave_other_commands_alone(command):
    assert not safety.is_never_send(command)


NEVER_SEND_CORPUS = sorted({
    *(p + tail for p in validate._NEVER_SEND for tail in (b"", b"0", b"1,1", b"G", b"GRFCC1")),
    *(filler + b"FP" for filler in (b" ", b"0", b"+", b"\x00", b"\x7f", b",-.9")),
    b"FPG", b"fp", b"fpg", b"tg1", b"j1", b"m1,1", b"q", b"w1", b"0th", b" fm1",
    b"FM", b"FM0", b"FM1", b" FM1", b"0FM1", b"Fm1", b"FN0", b"TG1", b"M0,0", b"D5,5",
    b"S4", b"S5", b"TB71", b"TB50,0", b"J1", b"", b"\\30,30", b"!5,1", b"[", b"U",
})


@pytest.mark.parametrize("command", NEVER_SEND_CORPUS, ids=repr)
def test_is_never_send_equals_the_validator_except_fm(command):
    if safety.is_expand_command(command):
        assert validate._is_never_send(command)                # today: refused as never-send
        assert not safety.is_never_send(command)               # now: the per-profile FM rule
    else:
        assert safety.is_never_send(command) == validate._is_never_send(command)


# The firmware starts a new command at a command letter even without an ETX before it
# (FX10,TJ3 runs TJ3), so a never-send command, a lowercase one, FM or TG behind another
# command's letters or values is refused like the same command alone.
EMBEDDED = (b"FX10,TB72", b"FX10,FB1", b"FN0FP", b"M100,100FB0", b"J1TB72", b"FX10,FM1",
            b"FX10,ZF", b"FX10,PW", b"FX10,tg1", b"FX10,APS1", b"FXTB72", b"FX10,TG1",
            b"TB50,0FM1", b"!5,1S0", b"D1,1fpg", b"0FX10,TH")


@pytest.mark.parametrize("profile", sorted(registry.profiles(), key=lambda p: p.id),
                         ids=lambda p: p.id)
def test_a_command_behind_another_one_without_etx_is_checked_too(profile):
    for command in EMBEDDED:
        with pytest.raises(SafetyError):
            safety.check_write(command + b"\x03", profile)
        with pytest.raises(SafetyError):            # also inside a write the floor takes
            safety.check_write(b"TG%d\x03" % profile.mat(profile.default_mat).tg + command
                               + b"\x03", profile)
        assert validate._is_never_send(command) == (b"TG1" not in command), command
    # the commands OpenCutter writes have their letters only at their start: still accepted
    for texts in [profile.mat_texts(m.key) for m in profile.mats] + [profile.trailer_texts()]:
        safety.check_write(b"".join(t.encode("ascii") + b"\x03" for t in texts), profile)


def test_command_starts_follows_the_firmwares_parser():
    assert safety.command_starts(b"FX10,TJ3") == [b"FX10,TJ3", b"X10,TJ3", b"TJ3", b"J3"]
    assert safety.command_starts(b" 0TB50,0") == [b"TB50,0", b"B50,0"]
    assert safety.command_starts(b"\\30,30") == [b"\\30,30"]
    assert safety.command_starts(b"") == [b""]
    for ok in (b"TB50,0", b"FX10,1", b"FC18,1,1", b"\\30,30", b"Z6066,6066", b"M1,1", b"D5,5",
               b"TG9", b"FN0", b"TJ3", b"FE0,1", b"FF1,0,1", b"TF1,1", b"!10,1", b"J2", b"TB71"):
        assert not safety.is_never_send(ok) and not safety.is_expand_command(ok), ok
    assert safety.is_expand_command(b"FX10,FM1") and safety.has_tg(b"FX10,TG1")


# --- queries -----------------------------------------------------------------------------------
QUERY_CORPUS = sorted({
    *(ESC + bytes([code]) for code in range(256)), ESC, ESC + b"\x05\x05", ESC + b"\x05\x03",
    *(q + b"\x03" for q in protocol.SAFE_TEXT_QUERIES), *protocol.SAFE_TEXT_QUERIES,
    b"FG\x03\x03", b"FG\x03FG\x03", b"fg\x03", b"Fg\x03", b"FP\x03", b"FPG\x03", b"FM1\x03",
    b"FM0\x03", b"M0,0\x03", b"TG1\x03", b"TB72\x03", b"TB70\x03", b"\x00FG\x03", b" FG\x03",
    b"FG \x03", b"U\x03\x00", b"", b"\x03", b"X" * 20 + b"\x03", b"FQ1\x03", b"FQ3\x03",
    b"TB71,1\x03", b"\x1bFG\x03",
})


def accepted(check, data) -> bool:
    try:
        check(data)
    except (protocol.UnsafeCommand, SafetyError):
        return False
    return True


@pytest.mark.parametrize("data", QUERY_CORPUS, ids=repr)
def test_check_query_equals_check_safe_for_the_cameo4(data):
    assert accepted(lambda d: safety.check_query(d, CAMEO4), data) == \
        accepted(protocol.check_safe, data)


def test_before_the_gate_only_esc05_and_fg():
    for data in QUERY_CORPUS:
        assert accepted(safety.check_query, data) == (data in (ESC + b"\x05", b"FG\x03")), data
    assert safety.PRE_GATE_ESC == {0x05} and safety.PRE_GATE_TEXT == {b"FG"}


def test_esc_rules():
    allowed = {code for code in range(256) if safety.esc_query_allowed(code, CAMEO4)}
    assert allowed == {0x05, 0x10, 0x15, 0x16} == set(safety.QUERY_ESC)
    assert {code for code in range(256) if safety.esc_query_allowed(code)} == {0x05}
    assert not safety.esc_query_allowed(True, CAMEO4) and not safety.esc_query_allowed("5")
    assert not (safety.NEVER_ESC & (safety.QUERY_ESC | {safety.STOP_ESC}))
    assert {0x11, 0x12, 0x13, 0x00} <= safety.NEVER_ESC
    # ESC 07 and ESC 08 are the user's Pause and Resume: never a query or the stop either
    assert not ({safety.PAUSE_ESC, safety.RESUME_ESC} & (safety.QUERY_ESC | {safety.STOP_ESC}))
    assert safety.is_stop_write(b"\x1b\x09") and not safety.is_stop_write(b"\x1b\x09\x1b\x09")
    assert not safety.is_stop_write(bytearray(b"\x1b\x09")) and not safety.is_stop_write(b"\x09")


def test_text_queries_never_take_a_lowercase_letter_or_a_never_send_command():
    for text in (b"fg", b"Fg", b"FG", b"FP", b"FPG", b"FM1", b"TB72", b" FP", b"", "FG", None):
        assert safety.text_query_allowed(text, CAMEO4) == (text == b"FG"), text


def test_text_queries_are_read_only_getters_only():
    """Every profile's getters lie within READ_ONLY_QUERIES (the CAMEO 4's allowlist), and a
    motion or setting command never passes as a query, even when a profile lists it."""
    assert safety.READ_ONLY_QUERIES == protocol.SAFE_TEXT_QUERIES
    assert safety.PRE_GATE_TEXT <= safety.READ_ONLY_QUERIES
    assert safety.REQUIRED_TEXT <= safety.READ_ONLY_QUERIES
    unchecked = replace(CAMEO4, queries=replace(Q, text=Q.text | {b"M0,0", b"D1,1", b"H"}))
    for text in (b"M0,0", b"D1,1", b"H"):
        assert not safety.text_query_allowed(text, unchecked)
        with pytest.raises(SafetyError):
            safety.check_query(text + b"\x03", unchecked)


# --- writes ------------------------------------------------------------------------------------
def job_writes() -> list[bytes]:
    spec = JobSpec(mat="12x12", media=(50.0, 50.0, 100.0, 100.0),
                   groups=[CutGroup(VINYL, [SQUARE]), CutGroup(PEN, [SQUARE[:3]])])
    plan = build_plan(spec)
    return [w for step in plan.steps if step.kind == "write" for w in step.writes]


def test_every_cameo4_write_keeps_the_rules():
    writes = job_writes() + sorted(programs.PINNED_WRITES)
    assert len(writes) > 10
    for data in writes:
        safety.check_write(data, CAMEO4)


@pytest.mark.parametrize("data", [
    b"", b"TG1", b"TG1\x03\x1b\x09", b"\x1bTG1\x03", b"TG1\x03\x03", b"\x03",
    b"M1,1\x03" * 40, "TG1\x03", bytearray(b"TG1\x03"), b"TG1\x03FP\x03", b"FPG\x03",
    b" FP\x03", b"0TH\x03", b"fp\x03", b"tg1\x03", b"PW\x03", b"S0\x03", b"TB72\x03",
    b"TG8\x03", b"TG2\x03", b"TG0\x03", b"TG01\x03", b" TG1\x03", b"TG1\x03FM1\x03", b"FM1\x03",
    b"FM0\x03", b"TG1\x03FN0\x03FM1\x03", b"M1,1\x03\xe9\x03", b"M1,1\x03\x7f\x03",
])
def test_check_write_refuses(data):
    with pytest.raises(SafetyError):
        safety.check_write(data, CAMEO4)


# --- FM: a per-profile rule ----------------------------------------------------------------------
def pro_like(fg: str = "CAMEO 4 PRO", expand_mode: bool = True, tg: int = 9,
             extra=HeaderExtra.EXPAND):
    """A synthetic 24-inch profile (not a real model's values) whose data defines expand mode,
    named like a model of the expand allowlist."""
    mat = replace(c4.MAT_12X12, key="24x24", label="24 × 24 in", tg=tg, size_mm=(609.6, 609.6),
                  area_su=(30, 30, 12162, 12162), expected_u=(12200, 12200),
                  u_basis=UBasis.INFERRED, basis="test", header_extra=extra)
    return replace(CAMEO4, id="test_pro", name=fg, support=Support.UNTESTED,
                   identity=Identity(frozenset({fg}), (FirmwareLine(5, Support.UNTESTED, "t"),)),
                   dialect=replace(CAMEO4.dialect, expand_mode=expand_mode),
                   mats=(c4.MAT_12X12, mat))


HEADER_24 = b"TG9\x03FN0\x03TB50,0\x03"          # the 24 x 24 in header before FM1


def test_fm1_right_after_tg9_fn0_tb50_where_allowed():
    """Studio's 24 x 24 in header is TG9 FN0 TB50,0 FM1: FM1 goes right after the TB50,0 that
    follows TG9, in the same write, as Studio sends it (docs/PROTOCOL.md section 5.2)."""
    pro = pro_like()
    safety.check_profile(pro, pinned=[HEADER_24 + b"FM1\x03"])
    assert pro.mat_texts("24x24") == ("TG9", "FN0", "TB50,0", "FM1")
    assert pro.mat_texts("12x12") == ("TG1", "FN0", "TB50,0")       # never with a 12-inch mat
    assert pro.setting_grammar().fullmatch(b"FM1")
    assert not pro.setting_grammar().fullmatch(b"FM0")
    assert safety.expand_allowed(pro) and not safety.expand_allowed(CAMEO4)
    assert safety.EXPAND_AFTER == (b"FN0", b"TB50,0")
    for data in (HEADER_24 + b"FM1\x03", HEADER_24 + b"FM1\x03\\30,30\x03Z12162,12162\x03"):
        safety.check_write(data, pro)
    for data in (b"TG9\x03FM1\x03FN0\x03TB50,0\x03",                # not Studio's order
                 b"TG9\x03FM1\x03", b"FM1\x03", b"TG1\x03FN0\x03TB50,0\x03FM1\x03",
                 b"FN0\x03TB50,0\x03FM1\x03", b"TB50,0\x03FM1\x03", b"TG9\x03TB50,0\x03FM1\x03",
                 b"TG9\x03FN0\x03FM1\x03", b"TG9\x03TB50,0\x03FN0\x03FM1\x03",
                 b"TG9\x03FN0\x03 TB50,0\x03FM1\x03", b"TG9\x03FN0\x03TB50,0 \x03FM1\x03",
                 b" TG9\x03FN0\x03TB50,0\x03FM1\x03",
                 HEADER_24 + b"FM1\x03FM1\x03", HEADER_24 + b"FM0\x03", HEADER_24 + b" FM1\x03",
                 HEADER_24 + b"FM1 \x03", HEADER_24 + b"FM\x03", HEADER_24 + b"\x03FM1\x03",
                 HEADER_24 + b"fm1\x03", HEADER_24 + b"FM1,1\x03",
                 HEADER_24 + b"M0,0\x03FN0\x03TB50,0\x03FM1\x03",
                 b"\\0,0\x03M0,0\x03FN0\x03TB50,0\x03FM1\x03"):         # after the trailer
        with pytest.raises(SafetyError):
            safety.check_write(data, pro)
    with pytest.raises(SafetyError):                 # the header and FM1 in two writes
        safety.check_write(b"FM1\x03", pro)


@pytest.mark.parametrize("fg", sorted(safety.NO_EXPAND_MODELS))
def test_fm_is_never_sent_to_the_no_expand_models(fg):
    with pytest.raises(UnsafeProfile, match="never sent"):
        safety.check_profile(pro_like(fg=fg))


def test_expand_mode_is_an_allowlist():
    """Only the models of EXPAND_MODELS may take expand mode: a new profile cannot take it by
    itself, and a shared FG text outside the list takes it away."""
    assert safety.EXPAND_MODELS == {"CAMEO 4 PRO", "CAMEO PRO MARK2"}
    assert not safety.EXPAND_MODELS & safety.NO_EXPAND_MODELS
    for fg in sorted(safety.EXPAND_MODELS):
        safety.check_profile(pro_like(fg=fg))
        assert safety.expand_allowed(pro_like(fg=fg))
    for fg in ("TEST PRO", "CAMEO 5 ALPHA", "Portrait 4", "CAMEO 4 PRO X"):
        assert not safety.expand_allowed(pro_like(fg=fg))
        with pytest.raises(UnsafeProfile, match="FM"):
            safety.check_profile(pro_like(fg=fg))
        with pytest.raises(SafetyError):
            safety.check_write(b"TG9\x03FN0\x03TB50,0\x03FM1\x03", pro_like(fg=fg))
    shared = replace(pro_like(), identity=replace(pro_like().identity,
                                                  shared_fg_models=frozenset({"CAMEO"})))
    assert not safety.expand_allowed(shared)
    with pytest.raises(UnsafeProfile):
        safety.check_profile(shared)


def test_fm1_never_goes_with_a_12_inch_mat():
    twelve = replace(pro_like().mats[1], size_mm=(304.8, 304.8), area_su=(30, 30, 6066, 6066),
                     expected_u=(6100, 6100))
    with pytest.raises(UnsafeProfile, match="12-inch"):
        safety.check_profile(replace(pro_like(), mats=(c4.MAT_12X12, twelve)))


@pytest.mark.parametrize("profile, pinned", [
    (pro_like(expand_mode=False), ()),                               # its data has no FM
    (pro_like(tg=8), ()),                                            # FM1 only after TG9
    (replace(CAMEO4, mats=(replace(c4.MAT_12X12, header_extra=HeaderExtra.EXPAND),)), ()),
    (CAMEO4, [b"TG1\x03FN0\x03TB50,0\x03FM1\x03"]),                  # FM on a CAMEO 4
    (pro_like(), [b"TG9\x03FM1\x03FN0\x03TB50,0\x03"]),              # before FN0 TB50,0
    (pro_like(), [b"FN0\x03TB50,0\x03FM1\x03"]),                      # no TG9 before it
    (pro_like(), [b"FM0\x03"]),
    (pro_like(extra=None), [b"TG9\x03FN0\x03TB50,0\x03FM1\x03"]),    # no mat with expand mode
], ids=["no-expand-data", "after-tg8", "12x12-mat", "cameo4-pinned", "early", "no-tg9", "fm0",
        "no-expand-mat"])
def test_fm_outside_its_place_is_refused(profile, pinned):
    with pytest.raises(UnsafeProfile):
        safety.check_profile(profile, pinned)


# --- the profile self-check --------------------------------------------------------------------
def test_the_cameo4_passes_the_self_check_with_its_pinned_writes():
    safety.check_profile(CAMEO4, pinned=programs.PINNED_WRITES)
    safety.check_entry(CAMEO4, pinned=programs.PINNED_WRITES)


Q = CAMEO4.queries
BAD_PROFILES = {
    "esc-11-query": replace(CAMEO4, queries=replace(Q, esc=Q.esc | {0x11})),
    "esc-12-query": replace(CAMEO4, queries=replace(Q, esc=Q.esc | {0x12})),
    "esc-07-query": replace(CAMEO4, queries=replace(Q, esc=Q.esc | {0x07})),
    "esc-09-query": replace(CAMEO4, queries=replace(Q, esc=Q.esc | {0x09})),
    "no-esc-15": replace(CAMEO4, queries=replace(Q, esc=Q.esc - {0x15})),
    "lowercase-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"fg"})),
    "mixed-case-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"Fg"})),
    "fp-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"FPG"})),
    "fm-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"FM1"})),
    "persistent-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"TB72"})),
    "etx-in-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"FG\x03U"})),
    "no-u-query": replace(CAMEO4, queries=replace(Q, text=Q.text - {b"U"})),
    "long-query-limit": replace(CAMEO4, queries=replace(Q, max_write=64)),
    "esc-07-stop": replace(CAMEO4, stop=StopSequence((b"\x1b\x07", b"\x1b\x09"), 0.3)),
    "one-stop": replace(CAMEO4, stop=StopSequence((b"\x1b\x09",), 0.3)),
    "joined-stop": replace(CAMEO4, stop=StopSequence((b"\x1b\x09\x1b\x09",), 0.3)),
    "three-stops": replace(CAMEO4, stop=StopSequence((b"\x1b\x09",) * 3, 0.3)),
    "no-stop-gap": replace(CAMEO4, stop=StopSequence((b"\x1b\x09",) * 2, 0)),
    "recognized-support": replace(CAMEO4, support=Support.RECOGNIZED),
    "wider-line": replace(CAMEO4, support=Support.UNTESTED),        # its V5 line is TESTED
    "no-usable-line": replace(CAMEO4, identity=replace(CAMEO4.identity, lines=(
        FirmwareLine(5, Support.RECOGNIZED, "t", "no reason to drive it"),))),
    "line-without-reason": replace(CAMEO4, identity=replace(CAMEO4.identity, lines=(
        c4.V5, FirmwareLine(1, Support.RECOGNIZED, "t")))),
    "no-fg-model": replace(CAMEO4, identity=replace(CAMEO4.identity, fg_models=frozenset())),
    "uppercase-id": replace(CAMEO4, id="CAMEO4"),
    "two-digit-tool-one-digit-reply": replace(CAMEO4, holders=(
        replace(CAMEO4.holders[0], tools=CAMEO4.holders[0].tools + (Tool(12, "Twelve"),)),
        CAMEO4.holders[1])),
    "usable-no-tool": replace(CAMEO4, holders=(
        replace(CAMEO4.holders[0], tools=(Tool(0, "No Tool Detected"),)
                + CAMEO4.holders[0].tools[1:]), CAMEO4.holders[1])),
    "depth-in-holder-2": replace(CAMEO4, holders=(
        CAMEO4.holders[0], replace(CAMEO4.holders[1], tools=tuple(
            replace(t, depth=t.code == 2) for t in CAMEO4.holders[1].tools)))),
    "single-slot-for-two": replace(CAMEO4, dialect=replace(CAMEO4.dialect, tool_slots=(0,))),
    "accel-without-tj": replace(CAMEO4, dialect=replace(CAMEO4.dialect, accel=AccelForm.NONE)),
    "overcut-10": replace(CAMEO4, ranges=replace(CAMEO4.ranges, overcut=IntRange(0, 10))),
    "speed-0": replace(CAMEO4, ranges=replace(CAMEO4.ranges, speed=IntRange(0, 30))),
    "depth-11": replace(CAMEO4, ranges=replace(CAMEO4.ranges, blade_depth=IntRange(0, 11))),
    "tg0-mat": replace(CAMEO4, mats=(replace(c4.MAT_12X12, tg=0),)),
    "area-off-the-mat": replace(CAMEO4, mats=(mat12(area_su=(30, 30, 6200, 6066)),)),
    "empty-area": replace(CAMEO4, mats=(mat12(area_su=(30, 30, 30, 6066)),)),
    "unknown-u-with-value": replace(CAMEO4, mats=(mat12(u_basis=UBasis.UNKNOWN),)),
    "known-u-without-value": replace(CAMEO4, mats=(mat12(expected_u=None),)),
    "unoffered-default": replace(CAMEO4, mats=(mat12(u_basis=UBasis.UNKNOWN, expected_u=None),)),
    "missing-default": replace(CAMEO4, default_mat="12x24"),
    "status-without-ready": replace(CAMEO4, status=StatusModel((("1", "Busy"), ("2", "Load")))),
    "padded-status": replace(CAMEO4, status=StatusModel((("0", "Ready"), ("1", "Busy"),
                                                         ("2", "Load"), (" 3", "Paused")))),
    "unit-0.1": replace(CAMEO4, unit_mm=0.1),
    "write-over-cap": replace(CAMEO4, max_write=481),
    "small-batch": replace(CAMEO4, batch_bytes=100),
    "no-tf-lowering": replace(CAMEO4, lowering_prefixes=(b"D",)),
    "no-basis": replace(CAMEO4, basis=" "),
    # a motion or setting command is never a read-only query
    "draw-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"D100,100"})),
    "move-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"M0,0"})),
    "home-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"H"})),
    "mat-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"TG1"})),
    "depth-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"TF1,1"})),
    "speed-query": replace(CAMEO4, queries=replace(Q, text=Q.text | {b"!5,1"})),
    "float-esc-query": replace(CAMEO4, queries=replace(Q, esc=frozenset({5.0, 0x15}))),
    # ranges no firmware takes
    "speed-31": replace(CAMEO4, ranges=replace(CAMEO4.ranges, speed=IntRange(1, 31))),
    "force-41": replace(CAMEO4, ranges=replace(CAMEO4.ranges, force=IntRange(1, 41))),
    "acceleration-11": replace(CAMEO4, ranges=replace(CAMEO4.ranges,
                                                      acceleration=IntRange(1, 11))),
    "passes-11": replace(CAMEO4, ranges=replace(CAMEO4.ranges, passes=IntRange(1, 11))),
    # a cut area the U reply cannot confirm
    "area-beyond-u": replace(CAMEO4, mats=(mat12(expected_u=(6050, 6100)),)),
    "area-beyond-u-b": replace(CAMEO4, mats=(mat12(expected_u=(6100, 6050)),)),
    "bool-stop-gap": replace(CAMEO4, stop=StopSequence((b"\x1b\x09",) * 2, True)),
    # shapes the checks cannot read, or that could change after the check
    "no-dialect": replace(CAMEO4, dialect=None),
    "no-status": replace(CAMEO4, status=None),
    "no-queries": replace(CAMEO4, queries=None),
    "no-stop": replace(CAMEO4, stop=None),
    "no-ranges": replace(CAMEO4, ranges=None),
    "holders-list": replace(CAMEO4, holders=list(CAMEO4.holders)),
    "holder-not-holder": replace(CAMEO4, holders=("Tool 1",)),
    "tools-list": replace(CAMEO4, holders=(replace(CAMEO4.holders[0],
                                                   tools=list(CAMEO4.holders[0].tools)),
                                           CAMEO4.holders[1])),
    "mats-list": replace(CAMEO4, mats=list(CAMEO4.mats)),
    "mat-not-mat": replace(CAMEO4, mats=("12x12",)),
    "fg-models-set": replace(CAMEO4, identity=replace(CAMEO4.identity, fg_models={"CAMEO 4"})),
    "lines-list": replace(CAMEO4, identity=replace(CAMEO4.identity, lines=[c4.V5])),
}


@pytest.mark.parametrize("name", sorted(BAD_PROFILES))
def test_a_bad_profile_is_refused(name):
    with pytest.raises(UnsafeProfile):
        safety.check_profile(BAD_PROFILES[name])
    with pytest.raises(UnsafeProfile):
        safety.check_entry(BAD_PROFILES[name])


@pytest.mark.parametrize("pinned", [
    b"TG1\x03FP\x03", b"FP\x03", b"FPG\x03", b" FP\x03", b"0FP\x03", b"fp\x03",
    b"J1\x03FX1,1\x03FPGRFCC1\x03", b"\x1b\x09", b"TG1\x03\x1b\x11", b"TH\x03", b"TG7\x03",
])
def test_fp_or_anything_never_send_in_a_pinned_write_is_refused(pinned):
    with pytest.raises(UnsafeProfile, match="pinned write"):
        safety.check_profile(CAMEO4, pinned=[*sorted(programs.PINNED_WRITES), pinned])


def test_the_refusal_names_every_problem():
    bad = replace(CAMEO4, queries=replace(Q, esc=Q.esc | {0x11}, text=Q.text | {b"fg"}),
                  stop=StopSequence((b"\x1b\x07", b"\x1b\x09"), 0.3))
    with pytest.raises(UnsafeProfile) as e:
        safety.check_profile(bad, pinned=[b"FP\x03"])
    text = str(e.value)
    assert text.startswith("profile 'cameo4': ")
    for part in ("never 11", "b'fg'", "ESC 09 twice", "pinned write b'FP\\x03'"):
        assert part in text, part


def test_check_entry_refuses_what_is_not_an_entry():
    for bad in (None, "cameo4", CAMEO4.identity):
        with pytest.raises(UnsafeProfile):
            safety.check_entry(bad)


# --- recognition-only entries ------------------------------------------------------------------
def cameo3(**changes) -> RecognizedModel:
    entry = RecognizedModel("test_cameo3", "CAMEO 3", Identity(frozenset({"CAMEO 3"})),
                            "there is no known way to stop it from the computer",
                            "test: firmware and machine data")
    return replace(entry, **changes)


def test_a_recognized_model_passes_with_a_reason():
    safety.check_recognized(cameo3())
    safety.check_entry(cameo3())
    assert cameo3().support is Support.RECOGNIZED
    assert cameo3().message == ("OpenCutter recognizes this CAMEO 3 but cannot drive it yet: "
                                "there is no known way to stop it from the computer.")


@pytest.mark.parametrize("changes", [
    dict(reason=""), dict(reason="   "), dict(reason="no stop is known."), dict(reason=" x"),
    dict(id="CAMEO 3"), dict(name=""), dict(basis=""),
    dict(identity=Identity(frozenset({"CAMEO 3"}), (FirmwareLine(1, Support.UNTESTED, "t"),))),
    dict(identity=Identity(frozenset())), dict(identity=Identity(frozenset({"CAMEO  3"}))),
    dict(identity=Identity(frozenset({"CAMEO 3"}), usb_ids=frozenset({(0x0B4D, 0x10000)}))),
    dict(identity=Identity(frozenset({"CAMEO 3"}), shared_fg_models=frozenset({"CAMEO 3"}))),
], ids=repr)
def test_a_bad_recognized_model_is_refused(changes):
    with pytest.raises(UnsafeProfile):
        safety.check_recognized(cameo3(**changes))


def test_a_recognized_model_has_no_programs():
    with pytest.raises(UnsafeProfile):
        safety.check_entry(cameo3(), pinned=[b"TG1\x03"])


def test_tool_codes_of_two_digits_need_the_two_digit_reply():
    holders = (replace(CAMEO4.holders[0], tools=CAMEO4.holders[0].tools + (Tool(12, "T"),)),
               CAMEO4.holders[1])
    ok = replace(CAMEO4, holders=holders, dialect=replace(CAMEO4.dialect,
                                                          tool_reply=ToolReply.TWO_DIGITS))
    safety.check_profile(ok)
