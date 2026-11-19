# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Pure protocol checks: the query allowlist, the exact reply grammar and the unit check."""
import pytest

from opencutter.device import protocol as p


@pytest.mark.parametrize("data", [b"\x1b\x05", b"\x1b\x10", b"\x1b\x15", b"\x1b\x16",
                                  b"FG\x03", b"TB71\x03", b"U\x03", b"[\x03"])
def test_check_safe_accepts_allowlisted_queries(data):
    p.check_safe(data)


@pytest.mark.parametrize("data", [
    b"", b"\x1b\x09", b"\x1b\x12", b"\x1b\x11", b"\x1b\x05\x1b\x05", b"\x1b",
    b"FG", b"FG\x03FG\x03", b"fg\x03", b" FG\x03", b"0TH\x03", b"FP\x03", b"TB72,0,0\x03",
    b"M200,200\x03", b"D1,1\x03", b"TG1\x03", b"U\x03" + b" " * 30,
])
def test_check_safe_refuses_everything_else(data):
    with pytest.raises(p.UnsafeCommand):
        p.check_safe(data)


def test_unsafe_command_is_a_value_error():
    assert issubclass(p.UnsafeCommand, ValueError)


@pytest.mark.parametrize("reply,ok", [("0", True), ("5", True), ("1", True), ("6", False),
                                      ("    0", False), ("0 ", False), ("0\n", False),
                                      ("", False), (None, False), ("00", False)])
def test_is_status_is_exact(reply, ok):
    assert p.is_status(reply) is ok


@pytest.mark.parametrize("reply,tools", [(" 2, 4", (2, 4)), (" 7, 0", (7, 0)),
                                         ("    7,    4", None), (" 7 , 4", None), ("7,4", None),
                                         (" 7, 4\n", None), ("", None), (None, None),
                                         (" 10, 4", None)])
def test_parse_tools_grammar_is_exact(reply, tools):
    assert p.parse_tools(reply) == tools


def test_status_and_tool_names_use_studio_terms():
    assert p.STATUS_TEXT == {"0": "Ready", "1": "Busy", "2": "Load Media", "3": "Paused",
                             "4": "Cancelling", "5": "Rejected"}
    assert p.TOOL_NAMES[1][4] == "Kraft Blade, 2mm" and p.TOOL_NAMES[2][4] == "Kraft Blade, 3mm"
    assert p.TOOL_NAMES[2][8] == "Foil Tool" and 8 not in p.TOOL_NAMES[1]
    assert p.TOOL_NAMES[1][0] == "No Tool Detected" and p.TOOL_NAMES[1][7] == "Sketch Pen"
    assert p.describe_tools(" 2, 4") == "Tool 1: AutoBlade, Tool 2: Kraft Blade, 3mm"


@pytest.mark.parametrize("data,lowers", [(b"M1,1\x03", False), (b"M1,1\x03D2,2\x03", True),
                                         (b"J1\x03TF1,1\x03", True), (b"TG1\x03FN0\x03", False),
                                         (b"D1,1\x03", True)])
def test_lowers_tool(data, lowers):
    assert p.lowers_tool(data) is lowers


def test_stop_sequence_is_esc09_twice():
    assert p.STOP_SEQUENCE == (b"\x1b\x09", b"\x1b\x09")


@pytest.mark.parametrize("reply,verdict", [("6100,6100", "ok"), ("  6100,  6100", "ok"),
                                           ("3050,3050", "mismatch"), ("  6066,  6066", "mismatch"),
                                           ("", "unknown"), ("?", "unknown")])
def test_evaluate_units(reply, verdict):
    assert p.evaluate_units(reply)[0] == verdict


def test_runtime_path_never_falls_back_to_tmp(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert p.runtime_path(p.SOCKET_NAME) is None
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    assert p.runtime_path(p.LOCK_NAME) == "/run/user/4242/opencutter.lock"
