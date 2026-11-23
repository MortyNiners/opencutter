# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The daemon and the CLI's test programs against a simulated machine.

No Bluetooth can be reached: stubs.install() replaces bleak and dbus_fast before the driver is
imported, and the daemon listens on a private socket with a private lock in a temp directory.
"""
import asyncio
import contextlib
import json
import os
import stat
import sys
import types

import pytest

import stubs
from stubs import ESC05, ESC09, ESC15, Answers, harness, motion_writes, stops  # noqa: F401

stubs.install()
from opencutter import __main__ as cli  # noqa: E402
from opencutter.device import daemon, programs  # noqa: E402
from opencutter.device.protocol import lowers_tool  # noqa: E402

DRY = programs.DRY_RUN
PEN1 = programs.PROGRAMS[("pen-test", 1)]
CUT = programs.CUT_TEST
PEN_OPTS = {"pen_force": 1, "allow_unrecognized_pen": False}


def data(steps):
    return [s.writes[0] for s in steps]


def expected(prog):
    """ESC 15 at open, then every write, each D/TF write preceded by its ESC 15 re-check."""
    out = [ESC15]
    for s in prog:
        if s.kind == "write" and lowers_tool(s.writes[0]):
            out.append(ESC15)
        out.append(s.writes[0])
    return out


@pytest.fixture
def operator(monkeypatch):
    """Make run_program see a terminal; operator(*answers) scripts the prompts."""
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    return lambda *answers: monkeypatch.setattr(cli, "ainput", Answers(*answers))


async def _step_in_background(h, program="dry-run", index=0):
    r, w, out = await h.raw([{"op": "motion", "program": program, "options": {}}])
    assert out[0]["ok"], out
    w.write((json.dumps({"op": "step", "index": index}) + "\n").encode())
    await w.drain()
    return r, w


def test_second_daemon_refused_by_the_lock(harness):
    async def scenario(h):
        with pytest.raises(SystemExit, match="lock"):
            await daemon.serve(None, False)
    harness.run(scenario)


def test_socket_is_private_and_ping_reports_the_machine(harness):
    async def scenario(h):
        assert stat.S_IMODE(os.stat(h.socket).st_mode) == 0o600
        p = await h.ping()
        assert p["ok"] and p["connected"] and p["motion"] is None and p["status"] == "0"
        assert p["device"] == {"name": "CAMEO 4-fake", "address": "00:00:00:00:00:01"}
    harness.run(scenario)


def test_info_and_query_ops(harness):
    async def scenario(h):
        f = await h.fresh(tools=" 2, 4")
        info = await h.client().info()
        assert info == {"ok": True, "status": "0", "tools": " 2, 4", "firmware": "CAMEO 4 V5.05"}
        assert f.writes == [ESC05, ESC15]          # FG: its reply from the model gate
        bad = (await h.raw([{"op": "query", "cmd": "TB72,0,0"}]))[2][0]
        assert not bad["ok"] and "refused" in bad["error"]
        assert (await h.raw([{"op": "esc", "code": 0x12}]))[2][0]["ok"] is False
        assert (await h.raw([{"op": "nope"}]))[2][0]["ok"] is False
        assert f.writes == [ESC05, ESC15]
    harness.run(scenario)


def test_dry_run_sends_exactly_the_pinned_steps(harness, operator):
    async def scenario(h):
        f = await h.fresh()
        operator("yes\n", *["\n"] * 9)
        await cli.run_program(h.client(), "dry-run", {})
        assert motion_writes(f.writes) == [ESC15] + data(DRY)
        assert stops(f.writes) == 0
        assert (await h.ping())["motion"] is None
    harness.run(scenario)


def test_pen_test_refused_with_autoblade_in_tool_1(harness, operator):
    async def scenario(h):
        f = await h.fresh(tools=" 2, 4")
        operator("yes\n")
        with pytest.raises(SystemExit, match="Tool 1"):
            await cli.run_program(h.client(), "pen-test", PEN_OPTS)
        assert motion_writes(f.writes) == [ESC15]
    harness.run(scenario)


def test_pen_test_rechecks_the_tool_before_drawing(harness, operator):
    async def scenario(h):
        f = await h.fresh(tools=" 7, 4")
        operator("yes\n", *["\n"] * 5)
        await cli.run_program(h.client(), "pen-test", PEN_OPTS)
        assert motion_writes(f.writes) == expected(PEN1)
    harness.run(scenario)


def test_code_0_needs_the_explicit_flag(harness, operator):
    async def scenario(h):
        f = await h.fresh(tools=" 0, 4")
        operator("yes\n")
        with pytest.raises(SystemExit):
            await cli.run_program(h.client(), "pen-test", PEN_OPTS)
        assert motion_writes(f.writes) == [ESC15]
        f = await h.fresh(tools=" 0, 4")
        operator("yes\n", *["\n"] * 5)
        await cli.run_program(h.client(), "pen-test", {"pen_force": 7, "allow_unrecognized_pen": True})
        assert motion_writes(f.writes) == expected(programs.PROGRAMS[("pen-test", 7)])
    harness.run(scenario)


def test_blade_swapped_in_before_the_draw_is_caught(harness, operator):
    async def scenario(h):
        f = await h.fresh(tools=" 7, 4")
        setup = PEN1[2].writes[0]
        f.on_write = lambda d: setattr(h.fake_cls, "tools", " 2, 4") if d == setup else None
        operator("yes\n", *["\n"] * 4)
        await cli.run_program(h.client(), "pen-test", PEN_OPTS)
        assert PEN1[3].writes[0] not in f.writes
        assert stops(f.writes) == 0           # refused before the write: nothing to stop
    harness.run(scenario)


@pytest.mark.parametrize("tools", [" 7, 4", " 0, 4"])
def test_cut_test_refused_without_autoblade(harness, operator, tools):
    async def scenario(h):
        f = await h.fresh(tools=tools)
        operator("yes\n")
        with pytest.raises(SystemExit, match="AutoBlade"):
            await cli.run_program(h.client(), "cut-test", {})
        assert motion_writes(f.writes) == [ESC15]
    harness.run(scenario)


def test_cut_test_runs_with_autoblade_and_rechecks_before_tf_and_cut(harness, operator):
    async def scenario(h):
        f = await h.fresh(tools=" 2, 4")
        operator("yes\n", *["\n"] * 7)
        await cli.run_program(h.client(), "cut-test", {})
        assert motion_writes(f.writes) == expected(CUT)
    harness.run(scenario)


def test_cut_test_q_at_the_second_preview_cuts_nothing(harness, operator):
    async def scenario(h):
        f = await h.fresh(tools=" 2, 4")
        operator("yes\n", "\n", "\n", "\n", "\n", "q\n")
        await cli.run_program(h.client(), "cut-test", {})
        assert CUT[3].writes[0] in f.writes and CUT[4].writes[0] not in f.writes
        assert not any(lowers_tool(w) and b"D" in w for w in f.writes)
    harness.run(scenario)


def test_cut_test_pen_swapped_in_before_the_cut_is_caught(harness, operator):
    async def scenario(h):
        f = await h.fresh(tools=" 2, 4")
        preview2 = CUT[4].writes[0]
        f.on_write = lambda d: setattr(h.fake_cls, "tools", " 7, 4") if d == preview2 else None
        operator("yes\n", *["\n"] * 6)
        await cli.run_program(h.client(), "cut-test", {})
        assert CUT[5].writes[0] not in f.writes
    harness.run(scenario)


def test_ctrl_c_mid_step_sends_exactly_one_stop(harness, operator):
    async def scenario(h):
        f = await h.fresh(tools=" 7, 4", move_s=5.0)
        draw = PEN1[3].writes[0]
        hit = asyncio.Event()
        f.on_write = lambda d: hit.set() if d == draw else None
        operator("yes\n", *["\n"] * 5)
        t = asyncio.create_task(cli.run_program(h.client(), "pen-test", PEN_OPTS))
        await asyncio.wait_for(hit.wait(), 20)
        await asyncio.sleep(0.8)
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
        await asyncio.sleep(1.5)
        after = [w for w in f.writes[f.writes.index(draw) + 1:] if w != ESC05]
        assert after == [ESC09, ESC09]
        assert (await h.ping())["motion"] is None
    harness.run(scenario)


def test_client_gone_mid_step_stops(harness):
    async def scenario(h):
        f = await h.fresh(move_s=5.0)
        _r, w = await _step_in_background(h)
        await asyncio.sleep(1.0)
        w.close()
        await asyncio.sleep(1.5)
        assert stops(f.writes) == 1
        assert (await h.ping())["motion"] is None
    harness.run(scenario)


def test_any_byte_mid_step_stops(harness):
    async def scenario(h):
        f = await h.fresh(move_s=5.0)
        _r, w = await _step_in_background(h)
        await asyncio.sleep(1.0)
        w.write(b"x")
        await w.drain()
        await asyncio.sleep(1.5)
        assert stops(f.writes) == 1
        w.close()
    harness.run(scenario)


def test_stop_op_mid_step_and_other_ops_refused_meanwhile(harness):
    async def scenario(h):
        f = await h.fresh(move_s=5.0)
        r, w = await _step_in_background(h)
        await asyncio.sleep(1.0)
        busy = (await h.raw([{"op": "esc", "code": 5}]))[2][0]
        assert not busy["ok"]
        assert not (await h.raw([{"op": "info"}]))[2][0]["ok"]
        second = (await h.raw([{"op": "motion", "program": "dry-run", "options": {}}]))[2][0]
        assert not second["ok"]
        assert (await h.ping())["motion"] == "dry-run"
        st = await h.client().stop()
        res = json.loads(await asyncio.wait_for(r.readline(), 10))
        w.close()
        await asyncio.sleep(0.5)
        assert st["ok"]
        assert res["ok"] is False and res["stopped"] and not res["stop_errors"]
        assert stops(f.writes) == 1
        assert (await h.ping())["motion"] is None
    harness.run(scenario)


def test_explicit_stops_between_steps_always_send(harness):
    async def scenario(h):
        f = await h.fresh()
        r, w, _out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}},
                                  {"op": "step", "index": 0}])
        s1 = await h.client().stop()
        s2 = await h.client().stop()
        w.write((json.dumps({"op": "step", "index": 1}) + "\n").encode())
        await w.drain()
        nxt = json.loads(await asyncio.wait_for(r.readline(), 10))
        w.close()
        assert s1["ok"] and s2["ok"] and stops(f.writes) == 2
        assert nxt["ok"] is False and b"U\x03" not in f.writes
    harness.run(scenario)


def test_failed_stop_is_reported_and_a_retry_resends(harness):
    async def scenario(h):
        f = await h.fresh(move_s=5.0)
        r, w = await _step_in_background(h)
        await asyncio.sleep(1.0)
        f.fail_esc09 = True
        st = await h.client().stop()
        res = json.loads(await asyncio.wait_for(r.readline(), 10))
        f.fail_esc09 = False
        w.close()
        retry = await h.client().stop()
        assert st["ok"] is False and res["stop_errors"]
        assert retry["ok"] and stops(f.writes) == 1
    harness.run(scenario)


def test_stop_without_a_session_and_without_a_daemon(harness):
    async def scenario(h):
        f = await h.fresh()
        assert (await h.client().stop())["ok"] and stops(f.writes) == 1
        from opencutter.device.client import DaemonClient
        gone = await DaemonClient(os.path.join(h.dir, "none.sock")).stop()
        assert gone["ok"] is False and gone["error"]
    harness.run(scenario)


def test_unit_mismatch_stops_after_the_header(harness, operator):
    async def scenario(h):
        f = await h.fresh(u_reply="3050,3050")
        operator("yes\n", "\n", "\n")
        await cli.run_program(h.client(), "dry-run", {})
        assert motion_writes(f.writes) == [ESC15, DRY[0].writes[0], b"U\x03"]
    harness.run(scenario)


def test_silent_u_lets_the_operator_decide(harness, operator):
    async def scenario(h):
        f = await h.fresh(u_reply=None)
        operator("yes\n", "\n", "\n", "no\n")
        await cli.run_program(h.client(), "dry-run", {})
        assert motion_writes(f.writes) == [ESC15, DRY[0].writes[0], b"U\x03"]
        f = await h.fresh(u_reply=None)
        operator("yes\n", "\n", "\n", "yes\n", *["\n"] * 7)
        await cli.run_program(h.client(), "dry-run", {})
        assert motion_writes(f.writes) == [ESC15] + data(DRY)
    harness.run(scenario)


def test_mat_gone_mid_move_stops(harness, operator):
    async def scenario(h):
        f = await h.fresh(move_s=3.0)
        first = DRY[0].writes[0]
        f.on_write = lambda d: setattr(f, "status_override", "2") if d == first else None
        operator("yes\n", "\n")
        await cli.run_program(h.client(), "dry-run", {})
        f.status_override = None
        assert stops(f.writes) == 1 and motion_writes(f.writes)[:2] == [ESC15, first]
    harness.run(scenario)


def test_out_of_order_step_refused(harness):
    async def scenario(h):
        f = await h.fresh()
        _r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}},
                                  {"op": "step", "index": 3}])
        w.close()
        assert out[1]["ok"] is False and motion_writes(f.writes) == [ESC15]
    harness.run(scenario)


def test_briefly_busy_before_a_step_waits(harness):
    async def scenario(h):
        f = await h.fresh()
        r, w, _out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}}])
        f.status_override = "1"
        asyncio.get_running_loop().call_later(1.2, lambda: setattr(f, "status_override", None))
        w.write((json.dumps({"op": "step", "index": 0}) + "\n").encode())
        await w.drain()
        res = json.loads(await asyncio.wait_for(r.readline(), 20))
        w.close()
        assert res["ok"] and DRY[0].writes[0] in f.writes
    harness.run(scenario)


def test_never_ready_before_a_step_writes_nothing(harness):
    async def scenario(h):
        f = await h.fresh()
        r, w, _out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}}])
        f.status_override = "3"
        w.write((json.dumps({"op": "step", "index": 0}) + "\n").encode())
        await w.drain()
        res = json.loads(await asyncio.wait_for(r.readline(), 20))
        w.close()
        f.status_override = None
        assert res["ok"] is False and DRY[0].writes[0] not in f.writes
        assert res["stopped"] is False and res["stop_errors"] == []
    harness.run(scenario)


@pytest.mark.parametrize("override", ["2", "    0"])
def test_no_media_or_padded_status_refused_at_open(harness, override):
    async def scenario(h):
        f = await h.fresh()
        f.status_override = override
        out = (await h.raw([{"op": "motion", "program": "dry-run", "options": {}}]))[2][0]
        f.status_override = None
        assert out["ok"] is False and motion_writes(f.writes) == [ESC15]
    harness.run(scenario)


def test_small_mtu_refused_at_open(harness):
    async def scenario(h):
        f = await h.fresh(mtu_payload=20)
        out = (await h.raw([{"op": "motion", "program": "dry-run", "options": {}}]))[2][0]
        assert out["ok"] is False and "MTU" in out["error"] and motion_writes(f.writes) == [ESC15]
    harness.run(scenario)


def test_live_bluez_mtu_decides_the_write_limit(harness):
    """bleak's cached MTU says 20; BlueZ's live GattCharacteristic1.MTU (stub BlueZ) says 500."""
    async def scenario(h):
        h.fake_cls.char_obj = ("/org/bluez/fake/char001c", {})
        h.bluez.mtu = 500
        await h.fresh(mtu_payload=20)
        _r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}},
                                  {"op": "end"}])
        w.close()
        assert out[0]["ok"] and out[0]["write_limit"] == 497
        assert any("write limit 497" in x for x in h.log)
        h.bluez.mtu = None                     # the live read fails: the safe minimum stays
        await h.fresh(mtu_payload=20)
        out = (await h.raw([{"op": "motion", "program": "dry-run", "options": {}}]))[2][0]
        assert out["ok"] is False and "MTU" in out["error"]
    harness.run(scenario)


def test_daemon_stopped_mid_step_stops_the_machine(harness):
    async def scenario(h):
        f = await h.fresh(move_s=5.0)
        _r, w = await _step_in_background(h)
        await asyncio.sleep(1.0)
        await h.shutdown()
        await asyncio.sleep(1.0)
        assert stops(f.writes) == 1
        w.close()
        assert not os.path.exists(h.socket)
    harness.run(scenario)


def test_serve_refuses_to_join_a_link_held_elsewhere(harness):
    harness.connected_prop = True
    with pytest.raises(SystemExit, match="already connected"):
        asyncio.run(daemon.serve(None, False))
    harness.connected_prop = False


def test_serve_needs_xdg_runtime_dir(harness, monkeypatch):
    monkeypatch.setattr(daemon, "SOCKET_PATH", None)
    with pytest.raises(SystemExit, match="XDG_RUNTIME_DIR"):
        asyncio.run(daemon.serve(None, False))
