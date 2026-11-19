# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Static checks of the pinned programs and of the write guards (no daemon, no Bluetooth)."""
import asyncio
import sys
import types

import pytest

import stubs

stubs.install()
from opencutter.device import ble, daemon, programs  # noqa: E402
from opencutter.device.protocol import STOP_SEQUENCE, UnsafeCommand, check_safe, lowers_tool  # noqa: E402
from opencutter.job import builder  # noqa: E402

E = b"\x03"
# The exact bytes proven on the real machine; they must never change.
GOLDEN = {
    "dry-run": [b"TG1\x03FN0\x03TB50,0\x03", b"U\x03", b"\\30,30\x03Z6066,6066\x03J1\x03!1,1\x03",
                b"M200,200\x03", b"M1200,200\x03", b"M1200,1200\x03", b"M200,1200\x03",
                b"M200,200\x03", b"\\0,0\x03M0,0\x03FN0\x03TB50,0\x03"],
    "pen-test": [b"TG1\x03FN0\x03TB50,0\x03", b"U\x03",
                 b"\\30,30\x03Z6066,6066\x03J1\x03!1,1\x03FX1,1\x03TJ1\x03FC0,1,1\x03FE0,1\x03FF0,0,1\x03",
                 b"M200,200\x03D600,200\x03D600,600\x03D200,600\x03D200,200\x03M200,100\x03D300,100\x03",
                 b"\\0,0\x03M0,0\x03FN0\x03TB50,0\x03"],
    "cut-test": [b"TG1\x03FN0\x03TB50,0\x03", b"U\x03",
                 b"\\30,30\x03Z6066,6066\x03J1\x03!5,1\x03FX10,1\x03TJ3\x03FC18,1,1\x03FE0,1\x03"
                 b"FF1,1,1\x03TF1,1\x03",
                 b"M1016,2794\x03", b"M1524,3302\x03",
                 b"M1016,2814\x03D1016,2794\x03D1524,2794\x03D1524,3302\x03D1016,3302\x03"
                 b"D1016,2794\x03D1036,2794\x03",
                 b"\\0,0\x03M0,0\x03FN0\x03TB50,0\x03"],
}
PEN1 = programs.PROGRAMS[("pen-test", 1)]


def test_stubs_refuse_when_a_real_module_is_loaded(monkeypatch):
    stubs.install()                                           # idempotent with our own stubs
    monkeypatch.setitem(sys.modules, "bleak.real", types.ModuleType("bleak.real"))
    with pytest.raises(RuntimeError, match="refusing"):
        stubs.install()


def _bytes(prog):
    return [s.writes[0] for s in prog]


def test_program_bytes_are_unchanged():
    assert _bytes(programs.DRY_RUN) == GOLDEN["dry-run"]
    assert _bytes(PEN1) == GOLDEN["pen-test"]
    assert _bytes(programs.PROGRAMS[("pen-test", 7)]) == [
        w.replace(b"FX1,1", b"FX7,1") for w in GOLDEN["pen-test"]]
    assert _bytes(programs.CUT_TEST) == GOLDEN["cut-test"]
    assert all(len(s.writes) == 1 for p in programs.PROGRAMS.values() for s in p)


def test_step_texts_are_english_and_present():
    for prog in programs.PROGRAMS.values():
        for s in prog:
            assert s.title and s.expect
            assert all(ord(ch) < 128 for ch in s.title + s.expect), s.title


@pytest.mark.parametrize("data", sorted(programs.PINNED_WRITES))
def test_check_safe_refuses_pinned_motion(data):
    with pytest.raises(UnsafeCommand):
        check_safe(data)


def test_pen_test_matches_the_job_builder():
    head = b"".join(builder.header("12x12"))
    square = b"".join(builder.path([[(200, 200), (600, 200), (600, 600), (200, 600), (200, 200)],
                                    [(200, 100), (300, 100)]], 1))
    trailer = b"".join(builder.trailer())
    assert head == PEN1[0].writes[0] + b"\\30,30\x03Z6066,6066\x03"
    assert square == PEN1[3].writes[0]
    assert trailer == PEN1[4].writes[0]


def test_dry_run_never_lowers_a_tool():
    assert not any(lowers_tool(w) for s in programs.DRY_RUN for w in s.writes if s.kind == "write")


def test_every_lowering_program_declares_its_tool():
    for (name, _), prog in programs.PROGRAMS.items():
        if any(lowers_tool(w) for s in prog if s.kind == "write" for w in s.writes):
            assert programs.REQUIRED_TOOLS[name]
    assert programs.REQUIRED_TOOLS == {"dry-run": {}, "pen-test": {1: 7}, "cut-test": {1: 2}}


def test_cut_test_square_is_one_grid_square_with_overlap():
    cut = programs.CUT_TEST[5].writes[0]
    for corner in (b"D1016,2794", b"D1524,2794", b"D1524,3302", b"D1016,3302"):
        assert corner + E in cut
    assert 1524 - 1016 == 508 == 3302 - 2794             # 25.4 mm at 0.05 mm per unit
    assert cut.startswith(b"M1016,2814\x03") and cut.endswith(b"D1036,2794\x03")   # 1 mm overlap
    assert programs.CUT_TEST[5].confirm and not programs.CUT_TEST[4].confirm


def test_cut_test_uses_the_vinyl_settings():
    assert programs.CUT_TEST[2].writes[0] == (
        b"\\30,30\x03Z6066,6066\x03J1\x03!5,1\x03FX10,1\x03TJ3\x03FC18,1,1\x03FE0,1\x03FF1,1,1\x03TF1,1\x03")
    # its title writes the depth as a job's setup step does (job.plan.blade_depth_text)
    from opencutter.job.plan import blade_depth_text
    assert programs.CUT_TEST[2].title == (
        f"Vinyl settings: Tool 1: AutoBlade, {blade_depth_text(1, mm=True)}, Force 10, Speed 5")


@pytest.mark.parametrize("bad", [b"M1700,200\x03", b"D200,3500\x03", b"D200,99999\x03",
                                 b"FB0,0\x03", b"TB72,1\x03", b"Z6100,6100\x03", b"M200,200",
                                 b"\x1bM200,200\x03", b"M200,200\x03 \x03"])
def test_pinned_grammar_refuses(bad):
    with pytest.raises(UnsafeCommand):
        programs._check_grammar(bad)


@pytest.mark.parametrize("name,opts", [
    ("dry-run", {"pen_force": 1}), ("pen-test", {"pen_force": 33}), ("pen-test", {"pen_force": True}),
    ("pen-test", {"x": 1}), ("cut", {}), (None, {}), ("pen-test", {"allow_unrecognized_pen": "yes"}),
    ("cut-test", {"allow_unrecognized_pen": True}), ("dry-run", []),
])
def test_build_program_refuses(name, opts):
    with pytest.raises(ValueError):
        programs.build_program(name, opts)


def test_build_program_resolves_options():
    pen = programs.build_program("pen-test", {"pen_force": 7, "allow_unrecognized_pen": True})
    assert pen.steps == programs.PROGRAMS[("pen-test", 7)]
    assert pen.required_tools == {1: 7} and pen.allow_unrecognized == {1: True}
    assert pen.summary == {"program": "pen-test",
                           "options": {"pen_force": 7, "allow_unrecognized_pen": True}}
    cut = programs.build_program("cut-test", {})
    assert cut.allow_unrecognized == {} and cut.validator is programs.validate_pinned


def test_tool_problem_needs_the_exact_reply():
    pen, allow = {1: 7}, {1: True}
    for t in ["    7,    4", " 7 , 4", "7,4", "", None, " 2, 4"]:
        assert daemon.tool_problem(pen, {}, t), t
    assert daemon.tool_problem(pen, {}, " 7, 4") is None
    assert daemon.tool_problem(pen, allow, " 0, 4") is None
    assert "Tool 1" in daemon.tool_problem(pen, {}, " 2, 4")


def test_tool_problem_cut_needs_autoblade_never_a_pen_or_code_0():
    cut = programs.build_program("cut-test", {})
    assert daemon.tool_problem(cut.required_tools, cut.allow_unrecognized, " 2, 4") is None
    for t in [" 7, 4", " 0, 4", " 1, 4", " 4, 2"]:
        assert "AutoBlade" in daemon.tool_problem(cut.required_tools, cut.allow_unrecognized, t)
    assert daemon.tool_problem({}, {}, " 2, 4")          # declares no tool: nothing may go down


def test_tool_problem_checks_every_used_holder():
    assert daemon.tool_problem({1: 2, 2: 4}, {}, " 2, 4") is None
    assert "Tool 2" in daemon.tool_problem({1: 2, 2: 4}, {}, " 2, 0")
    assert daemon.tool_problem({1: 2, 2: 4}, {2: True}, " 2, 0") is None
    assert daemon.tool_problem({1: 2, 2: 4}, {1: True}, " 2, 0")
    assert daemon.tool_problem({3: 2}, {}, " 2, 4")


def _dummy_cameo(limit=497):
    c = object.__new__(ble.Cameo4)
    c.verbose = False
    c._write_limit = limit
    c.client = types.SimpleNamespace(write_gatt_char=None)    # any write would crash the test
    return c


@pytest.mark.parametrize("bad", [b"D600,200\x03", b"M200,200\x03 ", b"M200,200", b"TG1\x03",
                                 b"FN1\x03", b"TF1,1\x03", b"M2000,200\x03", b"\x1b\x05",
                                 b"FX33,1\x03", b""])
def test_write_validated_refuses_anything_unpinned(bad):
    with pytest.raises(UnsafeCommand):
        asyncio.run(ble.Cameo4.write_validated(_dummy_cameo(), bad, programs.validate_pinned))


@pytest.mark.parametrize("bad", [b"\x1bM1,1\x03", b"M1,1", b"M1,1\x03" * 40, b""])
def test_write_validated_has_its_own_transport_checks(bad):
    with pytest.raises(UnsafeCommand):
        asyncio.run(ble.Cameo4.write_validated(_dummy_cameo(), bad, lambda data: None))


def test_write_validated_respects_the_live_write_limit():
    big = programs.CUT_TEST[2].writes[0]                  # 81 bytes
    with pytest.raises(UnsafeCommand, match="write limit"):
        asyncio.run(ble.Cameo4.write_validated(_dummy_cameo(limit=20), big, programs.validate_pinned))


def test_write_validated_writes_with_response():
    sent = []

    async def write(ch, data, response):
        sent.append((data, response))
    c = _dummy_cameo()
    c.client = types.SimpleNamespace(write_gatt_char=write)
    asyncio.run(ble.Cameo4.write_validated(c, b"M200,200\x03", programs.validate_pinned))
    assert sent == [(b"M200,200\x03", True)]


def test_stop_sequence_and_query_guard():
    assert STOP_SEQUENCE == (b"\x1b\x09", b"\x1b\x09")
    with pytest.raises(UnsafeCommand):
        asyncio.run(ble.Cameo4._request(_dummy_cameo(), b"\x1b\x09"))
