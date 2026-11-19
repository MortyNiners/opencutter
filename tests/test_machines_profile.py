# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The machine profile dataclasses, and the CAMEO 4 profile against today's constants.

The CAMEO 4 profile must describe exactly what OpenCutter sends today: its values are compared
with the constants of device/protocol.py, job/builder.py, job/validate.py, job/plan.py,
device/daemon.py and device/programs.py (imported here only; opencutter.machines imports none
of them), and its rendered header, condition blocks and trailer with the builder's bytes. The
device modules run on the stub Bluetooth of tests/stubs.py: nothing reaches a machine.
"""
import ast
import dataclasses
import itertools
from pathlib import Path

import pytest

import stubs

stubs.install()
from opencutter.device import daemon, programs, protocol  # noqa: E402
from opencutter.job import builder, plan, validate  # noqa: E402
from opencutter.job.spec import AUTOBLADE, BLADE_TOOLS, NO_TOOL, ToolSettings  # noqa: E402
from opencutter.machines import (CAMEO4, AccelForm, Dialect, FirmwareLine, Holder,  # noqa: E402
                                 HolderForm, Identity, IntRange, MachineProfile, Mat, Support,
                                 ToolReply, UBasis, ble_name_prefixes, safety)
from opencutter.machines import cameo4 as c4  # noqa: E402

MACHINES = Path(__file__).resolve().parent.parent / "src" / "opencutter" / "machines"


def settings_corpus() -> list[ToolSettings]:
    """Valid CAMEO 4 condition settings: every usable tool in both holders, the range ends and
    a typical value of each setting."""
    out = []
    for holder, code in sorted((h, c) for h, tools in protocol.TOOL_NAMES.items() for c in tools):
        if code == NO_TOOL or code in validate.UNSUPPORTED_TOOLS:
            continue
        blade = code in BLADE_TOOLS
        depths = [None, 0, 1, 10] if (holder, code) == (1, AUTOBLADE) else [None]
        for (speed, force, accel), depth in itertools.product(
                [(1, 1, 1), (30, 33, 10), (5, 10, 3)], depths):
            blade_values = [(0, False, (0, 0)), (99, True, (9, 9)), (18, False, (1, 2))]
            for offset, lso, overcut in blade_values if blade else [(0, False, (0, 0))]:
                out.append(ToolSettings(holder=holder, tool_code=code, speed=speed, force=force,
                                        acceleration=accel, offset=offset,
                                        line_segment_overcut=lso, overcut=overcut,
                                        blade_depth=depth))
    return out


def texts(profile: MachineProfile, s: ToolSettings) -> tuple[str, ...]:
    return profile.condition_texts(holder=s.holder, speed=s.speed, force=s.force,
                                   acceleration=s.acceleration, offset=s.offset,
                                   line_segment_overcut=s.line_segment_overcut,
                                   overcut=s.overcut, blade_depth=s.blade_depth)


SETTINGS = settings_corpus()


# --- the CAMEO 4 profile equals today's constants ---------------------------------------------
def test_the_corpus_covers_every_usable_tool():
    used = {(s.holder, s.tool_code) for s in SETTINGS}
    assert used == {(1, 1), (1, 2), (1, 3), (1, 4), (1, 7), (2, 1), (2, 2), (2, 3), (2, 4),
                    (2, 7), (2, 8)}
    assert any(s.blade_depth is not None for s in SETTINGS)


def test_tools_equal_todays_tables():
    assert CAMEO4.tool_names() == protocol.TOOL_NAMES
    assert CAMEO4.blade_codes() == BLADE_TOOLS
    assert CAMEO4.unsupported_codes() == validate.UNSUPPORTED_TOOLS
    depth = {(h.number, t.code) for h in CAMEO4.holders for t in h.tools if t.depth}
    assert depth == {(1, AUTOBLADE)}
    assert [h.label for h in CAMEO4.holders] == ["Tool 1", "Tool 2"]
    for h in CAMEO4.holders:
        assert h.tool(NO_TOOL).usable is False
        for t in h.tools:
            assert t.action == plan._ACTIONS.get(t.code, "Cut"), (h.number, t.code)
            assert CAMEO4.tool_name(h.number, t.code) == protocol.tool_name(h.number, t.code)
    for holder, code in [(1, 8), (1, 9), (2, 9), (3, 1), (0, 0)]:
        assert CAMEO4.tool_name(holder, code) == protocol.tool_name(holder, code)
    assert CAMEO4.tool(1, True) is None and CAMEO4.tool(True, 1) is None


def test_status_queries_and_stop_equal_today():
    assert CAMEO4.status.as_dict() == protocol.STATUS_TEXT
    assert (CAMEO4.status.ready, CAMEO4.status.busy, CAMEO4.status.load) == ("0", "1", "2")
    assert CAMEO4.status.unloaded_after_header is True
    for reply in ["0", "1", "2", "3", "4", "5", "6", "9", "10", "", " 0", "0 ", "00", "5\x03",
                  "٠", None, 0, b"0"]:
        assert CAMEO4.status.is_status(reply) == (isinstance(reply, str)
                                                  and protocol.is_status(reply)), repr(reply)
    assert CAMEO4.queries.esc == protocol.SAFE_ESC_QUERIES
    assert CAMEO4.queries.text == protocol.SAFE_TEXT_QUERIES
    assert CAMEO4.queries.max_write == protocol.MAX_QUERY_WRITE
    assert CAMEO4.stop.writes == protocol.STOP_SEQUENCE
    assert protocol.stop_sequence(CAMEO4) == protocol.STOP_SEQUENCE
    # Today's gap between the two ESC 09 writes (docs/PROTOCOL.md section 8). The link sleeps
    # exactly profile.stop.gap_s (tests/test_device_transport.py times it), so the profile's
    # value is the one to check, not a literal in the source.
    assert CAMEO4.stop.gap_s == 0.3


def test_limits_and_ranges_equal_today():
    assert CAMEO4.max_write == protocol.MAX_DATA_WRITE == builder.MAX_WRITE == 182
    assert CAMEO4.batch_bytes == daemon.BATCH_BYTES
    assert CAMEO4.slowest_su_per_s == daemon.SLOWEST_SU_PER_S
    assert CAMEO4.su_per_mm == builder.SU_PER_MM and CAMEO4.unit_mm == 0.05
    assert CAMEO4.ranges.table() == validate.RANGES
    assert CAMEO4.ranges.overcut.as_tuple() == (0, 9)              # validate checks 0..9 inline
    top = CAMEO4.max_coordinate_su
    assert validate._MOVE.fullmatch(b"M%d,%d" % (top, top))
    assert not validate._MOVE.fullmatch(b"M%d,0" % (top + 1))
    assert CAMEO4.programs == {name for name, _ in programs.PROGRAMS}
    assert CAMEO4.lowering_prefixes == (b"D", b"TF")
    for data in [b"D1,1\x03", b"M1,1\x03", b"TF1,1\x03", b"J1\x03", b"FX1,1\x03D5,5\x03"]:
        lowers = any(c.startswith(CAMEO4.lowering_prefixes) for c in data.split(b"\x03"))
        assert lowers == protocol.lowers_tool(data), data
    assert CAMEO4.fg_claims_session is True and CAMEO4.power_cycle_to_connect is True


def test_mats_equal_today():
    assert {m.key: {"tg": m.tg, "area_su": m.area_su, "size_mm": m.size_mm}
            for m in CAMEO4.mats} == builder.MATS
    mat = CAMEO4.mat("12x12")
    assert mat is c4.MAT_12X12 and CAMEO4.default_mat == "12x12"
    assert mat.expected_u == (6100, 6100) and mat.u_basis is UBasis.KNOWN and mat.enforce_u
    assert mat.header_extra is None and mat.grid_in == (12, 12) and mat.feed_mm == 16.5
    assert protocol.evaluate_units("  6100,  6100")[0] == "ok"
    assert CAMEO4.offered_mats() == CAMEO4.mats
    assert CAMEO4.mat("12x24") is None and CAMEO4.mat(None) is None


def test_identity_and_firmware_lines():
    ident = CAMEO4.identity
    assert ident.fg_models == {"CAMEO 4"} and not ident.shared_fg_models
    assert ident.line(5) is c4.V5 and c4.V5.support is Support.TESTED
    assert ident.line(1) is c4.V1 and c4.V1.support is Support.UNTESTED
    assert ident.line(6) is None and ident.line(15) is None
    assert protocol.TESTED_FIRMWARE == f"V{c4.V5.major}.xx"
    assert ident.usb_ids == {(0x0B4D, 0x1137)}
    # the CAMEO 4's BLE name prefix; discovery (ble.find_bonded_cameo) accepts every prefix of
    # the registry, compared without regard to case
    assert ident.ble_names == ("CAMEO",)
    assert "CAMEO" in ble_name_prefixes()
    assert CAMEO4.support is Support.TESTED and CAMEO4.id == "cameo4"
    assert CAMEO4.name == "CAMEO 4"


# --- rendering: the builder's exact bytes ------------------------------------------------------
def test_header_and_trailer_render_todays_bytes():
    cmd = builder.cmd
    assert [cmd(t) for t in CAMEO4.mat_texts("12x12")] == builder.mat_commands("12x12")
    assert [cmd(t) for t in CAMEO4.area_texts("12x12")] == builder.area_commands("12x12")
    assert [cmd(t) for t in CAMEO4.trailer_texts()] == builder.trailer()
    with pytest.raises(ValueError):
        CAMEO4.mat_texts("12x24")


@pytest.mark.parametrize("s", SETTINGS, ids=repr)
def test_condition_blocks_render_todays_bytes(s):
    assert [builder.cmd(t) for t in texts(CAMEO4, s)] == builder.condition_block(s)


@pytest.mark.parametrize("bad", [
    dict(holder=3), dict(holder=True), dict(speed=True), dict(speed=2.5), dict(force="10"),
    dict(acceleration=None), dict(overcut=(1,)), dict(line_segment_overcut=1),
    dict(holder=2, blade_depth=1),
])
def test_condition_texts_refuse_what_cannot_be_written(bad):
    values = dict(holder=1, speed=5, force=10, acceleration=3, offset=18,
                  line_segment_overcut=False, overcut=(1, 1), blade_depth=None)
    with pytest.raises(ValueError):
        CAMEO4.condition_texts(**{**values, **bad})


# --- the command grammar -----------------------------------------------------------------------
def candidate_commands() -> set[bytes]:
    """Commands near the grammar: every form with numbers around each range, leading zeros,
    missing and extra parts."""
    nums = [b"", b"00", b"01", b"012", b"99999", b"100000", b"6066", b"-1", b"+1", b" 1"] + \
        [str(i).encode() for i in range(0, 41)] + [b"98", b"99", b"100"]
    holders = [b"", b",0", b",1", b",2", b",3", b",12"]
    out: set[bytes] = set()
    for n in nums:
        out |= {b"TG" + n, b"J" + n, b"TJ" + n, b"FN" + n, b"FM" + n, b"TB50," + n, b"TB" + n}
        for h in holders:
            out |= {b"!" + n + h, b"FX" + n + h, b"TJ" + n + h, b"FE" + n + h, b"TF" + n + h,
                    b"FC" + n + b",1" + h, b"FC" + n + b",2" + h, b"FC" + n + h,
                    b"FF" + n + b",1" + h, b"FF1," + n + h}
        for m in [b"0", b"30", b"6066", b"99999", b"100000", b"01", b""]:
            out |= {b"\\" + n + b"," + m, b"Z" + n + b"," + m}
    return out


def test_setting_grammar_accepts_what_the_builder_emits():
    grammar = CAMEO4.setting_grammar()
    emitted = {c[:-1] for c in builder.header("12x12") + builder.trailer()}
    for s in SETTINGS:
        emitted |= {c[:-1] for c in builder.condition_block(s)}
    emitted.discard(validate.ORIGIN_MOVE)
    for c in emitted:
        assert grammar.fullmatch(c), c
        assert validate._SETTING.fullmatch(c), c


def test_setting_grammar_equals_todays_except_the_unused_mat_types():
    """For the CAMEO 4 the profile's grammar accepts exactly what job/validate.py's accepts,
    except TG2..TG7: only the 12x12 mat (TG1) is in the profile. Stricter, never wider."""
    grammar = CAMEO4.setting_grammar()
    corpus = candidate_commands()
    assert len(corpus) > 3000
    new = {c for c in corpus if grammar.fullmatch(c)}
    old = {c for c in corpus if validate._SETTING.fullmatch(c)}
    assert new <= old
    assert old - new == {b"TG%d" % n for n in range(2, 8)}


def test_setting_grammar_refuses_the_never_send_list():
    grammar = CAMEO4.setting_grammar()
    for prefix in validate._NEVER_SEND:
        for tail in (b"", b"0", b"1", b"1,1", b"0,0,0"):
            assert not grammar.fullmatch(prefix + tail), prefix + tail
    for c in (b"tg1", b"j1", b"fx1,1", b"FM0", b"FM1", b"FPG", b"ESC"):
        assert not grammar.fullmatch(c), c


def test_the_job_layers_never_send_list_is_the_global_one_plus_fm():
    """job/validate._NEVER_SEND, which docs/PROTOCOL.md section 6 must list, is the global list
    of machines/safety.py plus FM, so the stricter entries are named too; the CAMEO 4's write
    validator refuses each of them as a never-send command (alone, with values and behind the
    filler bytes the firmware skips)."""
    assert set(validate._NEVER_SEND) == set(safety.NEVER_SEND_PREFIXES) | {safety.FM_PREFIX}
    assert len(validate._NEVER_SEND) == len(set(validate._NEVER_SEND))
    assert set(safety.STRICTER_PREFIXES) <= set(validate._NEVER_SEND)
    check = validate.make_write_validator(plan_spec())
    for prefix in validate._NEVER_SEND:
        for c in (prefix, prefix + b"1", prefix + b"1,1", b"0" + prefix, b" " + prefix):
            with pytest.raises(validate.UnsafeJob, match="never-send"):
                check(c + b"\x03")


def plan_spec():
    """A small CAMEO 4 job on the 12 x 12 in mat."""
    from opencutter.job.spec import CutGroup, JobSpec
    s = ToolSettings(holder=1, tool_code=AUTOBLADE, speed=5, force=10, acceleration=3, offset=18,
                     line_segment_overcut=False, overcut=(1, 1), blade_depth=1)
    return JobSpec(mat="12x12", media=(0.0, 0.0, 100.0, 100.0),
                   groups=[CutGroup(s, [[(10.0, 10.0), (20.0, 20.0)]])])


# --- ESC 15 ------------------------------------------------------------------------------------
@pytest.mark.parametrize("reply", [
    " 0, 0", " 2, 4", " 7, 8", " 9, 9", " 1, 0", " 10, 2", " 2, 10", " 15, 2", "2, 4", " 2,4",
    "  2, 4", " 2, 4 ", " 2, 4\x03", " 2, 4, 1", " 2", "", " -1, 2", " +2, 4", " a, 4",
])
def test_tools_reply_equals_today(reply):
    assert CAMEO4.parse_tools(reply) == protocol.parse_tools(reply)


def test_tools_reply_fails_closed():
    assert CAMEO4.parse_tools(" ٢, ٤") is None             # today's \d also took these digits
    assert CAMEO4.parse_tools(None) is None and CAMEO4.parse_tools(b" 2, 4") is None
    assert CAMEO4.parse_tools(" 15, 2") is None              # two digits: refused on the CAMEO 4
    assert CAMEO4.holder_codes(" 2, 7") == {1: 2, 2: 7}
    assert CAMEO4.holder_codes(" 2, 17") is None
    two = dataclasses.replace(CAMEO4, dialect=dataclasses.replace(
        CAMEO4.dialect, tool_reply=ToolReply.TWO_DIGITS))
    assert two.parse_tools(" 15, 4") == (15, 4) and two.parse_tools(" 4, 18") == (4, 18)
    for reply in (" 05, 4", " 100, 4", " 15,4", "15, 4"):
        assert two.parse_tools(reply) is None, reply


# --- speed -------------------------------------------------------------------------------------
def test_speed_is_whole_numbers():
    assert CAMEO4.speed_text(5) == "5" and CAMEO4.speed_text(30) == "30"
    for bad in (True, 2.5, "5", None):
        with pytest.raises(ValueError):
            CAMEO4.speed_text(bad)
    s = SETTINGS[0]
    assert CAMEO4.speed_mm_s(s.speed) == s.speed * 10.0            # job/plan.py's estimate
    assert CAMEO4.speed_mm_s(5) == 50.0


# --- a one-holder profile (synthetic: the forms, not a real model) ---------------------------
def one_holder(accel: AccelForm) -> MachineProfile:
    tools = CAMEO4.holders[0].tools
    return dataclasses.replace(
        CAMEO4, id="test_one_holder", name="TEST ONE", support=Support.UNTESTED,
        identity=Identity(frozenset({"TEST ONE"}), (FirmwareLine(1, Support.UNTESTED, "test"),)),
        holders=(Holder(1, "Tool", tools),),
        dialect=Dialect(HolderForm.SINGLE, accel, ToolReply.ONE_DIGIT, (0,)),
        ranges=dataclasses.replace(CAMEO4.ranges, speed=IntRange(1, 10),
                                   acceleration=IntRange(1, 3) if accel is AccelForm.SHARED
                                   else None))


def test_one_holder_forms_have_no_j_and_no_holder_suffix():
    p = one_holder(AccelForm.NONE)
    safety.check_profile(p)
    values = dict(holder=1, speed=5, force=10, offset=18, line_segment_overcut=False,
                  overcut=(1, 1), blade_depth=1)
    assert p.condition_texts(acceleration=None, **values) == (
        "!5", "FX10", "FC18", "FE0,0", "FF1,1,0", "TF1,1")
    with pytest.raises(ValueError):
        p.condition_texts(acceleration=3, **values)
    with pytest.raises(ValueError):
        p.condition_texts(acceleration=None, **{**values, "holder": 2})
    grammar = p.setting_grammar()
    for c in (b"!5", b"!10", b"FX10", b"FC18", b"FE1,0", b"FF1,1,0", b"TF1,1", b"TG1"):
        assert grammar.fullmatch(c), c
    for c in (b"J1", b"!5,1", b"FX10,1", b"TJ3", b"!11", b"FC18,1,1", b"FE0,1", b"FF1,1,1"):
        assert not grammar.fullmatch(c), c
    assert p.holder_codes(" 7, 0") == {1: 7}               # the first slot

    shared = one_holder(AccelForm.SHARED)
    safety.check_profile(shared)
    assert shared.condition_texts(acceleration=3, **values) == (
        "!5", "FX10", "TJ3", "FC18", "FE0,0", "FF1,1,0", "TF1,1")
    assert shared.setting_grammar().fullmatch(b"TJ3")
    assert not shared.setting_grammar().fullmatch(b"TJ4")


def test_a_one_holder_reply_needs_0_in_the_unused_slot():
    """On a one-holder model the ESC 15 slot no holder owns must read 0; any other code there
    fails the tool check closed (None: the caller shows the raw reply), never a guess."""
    p = one_holder(AccelForm.NONE)
    assert p.holder_codes(" 7, 0") == {1: 7} and p.holder_codes(" 0, 0") == {1: 0}
    for reply in (" 7, 3", " 0, 2", " 2, 2", " 0, 9", " 1, 1"):
        assert p.parse_tools(reply) is not None, reply      # the grammar still reads it
        assert p.holder_codes(reply) is None, reply
    for reply in (" 7, 00", " 7,0", "7, 0", " 7, 0 ", None):
        assert p.holder_codes(reply) is None, reply
    second = dataclasses.replace(p, dialect=dataclasses.replace(p.dialect, tool_slots=(1,)))
    safety.check_profile(second)
    assert second.holder_codes(" 0, 7") == {1: 7} and second.holder_codes(" 7, 0") is None
    two = dataclasses.replace(p, dialect=dataclasses.replace(p.dialect,
                                                             tool_reply=ToolReply.TWO_DIGITS))
    assert two.holder_codes(" 17, 0") == {1: 17} and two.holder_codes(" 17, 10") is None
    # two holders own both slots: nothing changes for the CAMEO 4
    assert CAMEO4.holder_codes(" 7, 3") == {1: 7, 2: 3}
    assert CAMEO4.holder_codes(" 0, 2") == {1: 0, 2: 2}


# --- mats, support levels, immutability --------------------------------------------------------
def test_mats_are_offered_by_their_u_basis():
    base = c4.MAT_12X12
    known = base
    inferred = dataclasses.replace(base, key="i", u_basis=UBasis.INFERRED)
    unknown = dataclasses.replace(base, key="u", u_basis=UBasis.UNKNOWN, expected_u=None)
    assert [m.offered(Support.TESTED) for m in (known, inferred, unknown)] == [True, True, False]
    assert [m.offered(Support.UNTESTED) for m in (known, inferred, unknown)] == [True, True, True]
    assert [m.offered(Support.RECOGNIZED) for m in (known, inferred, unknown)] == [False] * 3
    assert known.enforce_u and inferred.enforce_u and not unknown.enforce_u
    p = dataclasses.replace(CAMEO4, mats=(known, inferred, unknown))
    assert p.offered_mats() == (known, inferred)
    assert p.offered_mats(Support.TESTED.narrower(Support.UNTESTED)) == (known, inferred, unknown)


def test_support_levels():
    hi, mid, lo = Support.TESTED, Support.UNTESTED, Support.RECOGNIZED
    assert hi.narrower(mid) is mid and mid.narrower(hi) is mid and mid.narrower(lo) is lo
    assert hi.narrower(hi) is hi
    assert [s.jobs_allowed for s in (hi, mid, lo)] == [True, True, False]
    assert hi.rank > mid.rank > lo.rank


def test_ranges_are_whole_numbers():
    r = IntRange(1, 30)
    assert 1 in r and 30 in r and 0 not in r and 31 not in r
    assert True not in r and 5.0 not in r and "5" not in r
    assert list(r.values())[:2] == [1, 2] and r.as_tuple() == (1, 30)


def test_profiles_are_frozen_and_hashable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        CAMEO4.max_write = 480
    with pytest.raises(dataclasses.FrozenInstanceError):
        c4.MAT_12X12.tg = 2
    assert hash(CAMEO4) == hash(dataclasses.replace(CAMEO4))
    assert isinstance(CAMEO4, MachineProfile) and isinstance(c4.MAT_12X12, Mat)


# --- the package stays pure ----------------------------------------------------------------------
STDLIB = {"__future__", "re", "dataclasses", "enum", "math", "collections", "operator", "typing"}


@pytest.mark.parametrize("path", sorted(MACHINES.glob("*.py")), ids=lambda p: p.name)
def test_the_package_imports_nothing_else_from_opencutter(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in STDLIB, (path.name, alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.level == 1, (path.name, node.module)
            else:
                assert node.module.split(".")[0] in STDLIB, (path.name, node.module)


MODEL_MODULES = sorted(p for p in MACHINES.glob("*.py")
                       if p.stem not in ("__init__", "profile", "safety", "detect", "registry"))


@pytest.mark.parametrize("path", MODEL_MODULES, ids=lambda p: p.name)
def test_a_model_module_only_declares_values(path):
    """registry.py's contract: a model module imports from .profile, and may import shared
    values from another model module (cameo4_plus_pro reuses cameo4's), never .safety, .detect
    or .registry."""
    models = {p.stem for p in MODEL_MODULES}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level:
            names = [node.module] if node.module else [a.name for a in node.names]
            for name in names:
                assert name == "profile" or name in models - {path.stem}, (path.name, name)


def test_every_model_module_declares_profiles():
    from opencutter.machines import registry
    names = sorted(p.stem for p in MACHINES.glob("*.py")
                   if p.stem not in ("__init__", "profile", "safety", "detect", "registry"))
    assert names == sorted(m.__name__.rsplit(".", 1)[1] for m in registry.MODULES)
    for module in registry.MODULES:
        assert type(module.PROFILES) is tuple, module.__name__
