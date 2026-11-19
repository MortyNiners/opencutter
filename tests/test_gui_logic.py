# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""GUI logic without a display: geometry, placement, settings, machine state, Send rules, runner.

Nothing here touches Bluetooth or a real daemon: the runner and the monitor talk to
FakeDaemonClient (tests/gui_fakes.py).
"""
import asyncio
import json
from dataclasses import replace

import pytest

import stubs

stubs.install()
from opencutter.gui import geometry, sending  # noqa: E402
from opencutter.gui.document import PlacedDocument  # noqa: E402
from opencutter.gui.machine import CONNECT_GRACE_SECONDS, Link, MachineMonitor, MachineState, pill  # noqa: E402
from opencutter.gui.runner import JobRunner, Outcome, RunStep, StepState  # noqa: E402
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from opencutter.svg.importer import load_svg  # noqa: E402
from gui_fakes import FakeDaemonClient  # noqa: E402

DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 100 100"
  fill="none" stroke="#000"><rect x="20" y="30" width="40" height="20"/><circle cx="80" cy="40" r="5"/></svg>"""
VINYL = next(p for p in BASIC_PRESETS if p.action == "Cut")
LETTER = geometry.media_rect("letter", False, (0, 0))
READY = MachineState(link=Link.CONNECTED, status="0", tools=(2, 4))


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "design.svg"
    path.write_text(DESIGN)
    return PlacedDocument(load_svg(str(path)), LETTER)


def test_rotary_and_punch_presets_are_not_offered():
    rotary = replace(VINYL, material="Felt", action="Rotary Cut", holder=2, tool="Sketch Pen",
                     tool_code=7, blade_depth=None)
    punch = replace(VINYL, material="Felt", action="Punch", holder=2, tool="Punch Tool",
                    tool_code=6, blade_depth=None)
    kraft = replace(VINYL, material="Felt", action="Kraft Cut", holder=2, tool="Kraft Blade, 3mm",
                    tool_code=4, blade_depth=None)
    store = sending.supported_presets(PresetStore([*BASIC_PRESETS, rotary, punch, kraft], basic_source()))
    assert [p.action for p in store.presets_for("Felt")] == ["Kraft Cut"]
    assert store.source == basic_source() and VINYL in store.presets


# --- geometry --------------------------------------------------------------------------
def test_media_sizes_and_cut_area():
    assert LETTER == (0.0, 0.0, pytest.approx(215.9), pytest.approx(279.4))
    assert geometry.media_rect("letter", True, (0, 0))[2:] == (pytest.approx(279.4), pytest.approx(215.9))
    assert geometry.media_rect("custom", False, (500, 100))[2:] == (geometry.MAT_SIZE_MM, 100)
    assert geometry.cut_area() == (1.5, 1.5, pytest.approx(301.8), pytest.approx(301.8))
    assert geometry.usable_area(LETTER) == (1.5, 1.5, pytest.approx(214.4), pytest.approx(277.9))


def test_units_round_trip():
    assert geometry.format_length(25.4, "in") == "1.00"
    assert geometry.format_length(25.4, "mm") == "25.4"
    assert geometry.parse_length("2,5", "in") == pytest.approx(63.5)
    assert geometry.parse_length("abc", "mm") is None
    assert geometry.format_size(215.9, 279.4, "in") == "8.5 × 11 in"
    assert geometry.format_duration(72) == "1 min 12 s"


# --- placement -------------------------------------------------------------------------
def test_design_starts_where_it_sits_on_its_page(doc):
    assert doc.bbox() == pytest.approx((20, 30, 65, 20))
    assert not doc.modified and doc.path_count == 2
    assert geometry.design_fits(doc.bbox(), LETTER)


def test_move_scale_rotate_and_placed_design(doc):
    doc.move_to(50, 60)
    doc.set_width(130)                         # uniform: height follows
    assert doc.bbox() == pytest.approx((50, 60, 130, 40))
    cx, cy = 50 + 65, 60 + 20
    doc.rotate_quarter()                       # about the centre
    x, y, w, h = doc.bbox()
    assert (w, h) == pytest.approx((40, 130)) and (x + w / 2, y + h / 2) == pytest.approx((cx, cy))
    assert doc.placed().bbox == pytest.approx(doc.bbox())
    assert doc.modified
    lines = doc.polylines()
    assert all(line[0] == line[-1] for line in lines)          # closed outlines end where they start


def test_fit_to_mat_moves_and_shrinks_inside(doc):
    area = geometry.usable_area(LETTER)
    doc.move_to(250, 280)
    assert not geometry.design_fits(doc.bbox(), LETTER)
    doc.fit_into(area)
    assert geometry.design_fits(doc.bbox(), LETTER)
    doc.set_width(1000)
    doc.fit_into(area)
    assert geometry.design_fits(doc.bbox(), LETTER) and doc.size[0] <= area[2]


# --- settings --------------------------------------------------------------------------
def test_settings_defaults_round_trip_and_bad_values(tmp_path):
    path = tmp_path / "gui.json"
    settings = GuiSettings.load(path)
    assert settings.units == "in" and settings.preview_outline and not settings.first_run_done
    settings.units = "mm"
    for material in ("A", "B", "A", "C", "D"):
        settings.remember_material(material)
    settings.save()
    again = GuiSettings.load(path)
    assert again.units == "mm" and again.material == "D" and again.recent_materials == ["D", "C", "A"]
    path.write_text(json.dumps({"units": "furlongs", "custom_media_mm": ["x"], "reveal_mat": "yes"}))
    broken = GuiSettings.load(path)
    assert broken.units == "in" and broken.custom_media_mm == [254.0, 254.0] and broken.reveal_mat is True


# --- machine state ---------------------------------------------------------------------
def test_monitor_states_and_pill():
    async def scenario():
        clock = [0.0]
        client = FakeDaemonClient(ping=None)
        monitor = MachineMonitor(client, clock=lambda: clock[0])
        seen = []
        monitor.add_listener(seen.append)
        await monitor.connect()
        assert client.daemon_started == 1
        assert monitor.state.link is Link.NO_DAEMON and pill(monitor.state)[0] == "Not Connected"

        client.ping_reply = {"ok": True, "connected": False, "status": None, "motion": None, "device": None}
        await monitor.refresh()
        assert pill(monitor.state) == ("Connecting…", "info", True)
        clock[0] += CONNECT_GRACE_SECONDS + 1
        await monitor.refresh()
        assert monitor.state.link is Link.FAILED and pill(monitor.state)[1] == "negative"

        client.ping_reply = {"ok": True, "connected": True, "status": "2", "motion": None,
                             "device": {"name": "CAMEO 4-X", "address": "00:00:00:00:00:00"}}
        client.info_reply = {"ok": True, "status": "2", "tools": " 2, 4", "firmware": "CAMEO 4 V5.05  "}
        await monitor.refresh()
        state = monitor.state
        assert state.connected and state.tools == (2, 4) and state.firmware == "CAMEO 4 V5.05"
        assert pill(state) == ("Load Media", "warning", False)
        assert state.tool_text(1) == "AutoBlade (Detected)" and state.tool_text(2) == "Kraft Blade, 3mm (Detected)"

        count = len(seen)
        await monitor.refresh()                 # nothing changed: no notification
        assert len(seen) == count

        client.ping_reply = {**client.ping_reply, "connected": False}
        await monitor.refresh()                 # a dropped link is reported at once
        assert monitor.state.link is Link.FAILED and monitor.state.tools is None

        client.ping_reply = {**client.ping_reply, "connected": True, "status": "0"}
        client.info_reply = None
        monitor.quiet = True                    # during a job: ping only
        await monitor.refresh()
        assert monitor.state.ready and monitor.state.tools is None
    asyncio.run(scenario())


# --- Send rules and specs --------------------------------------------------------------
def test_send_blockers(doc):
    settings = VINYL.to_tool_settings()

    def check(state):
        return sending.tool_check(settings, state)

    assert sending.send_blocker(None, LETTER, READY, check(READY), False) == "Open a design first."
    assert sending.send_blocker(doc, LETTER, READY, check(READY), True) == "A job is already running."
    offline = MachineState()
    assert sending.send_blocker(doc, LETTER, offline, check(offline), False) == "Connect the machine first."
    unknown = MachineState(link=Link.CONNECTED, status="0")
    assert "report its tools" in sending.send_blocker(doc, LETTER, unknown, check(unknown), False)
    pen = MachineState(link=Link.CONNECTED, status="0", tools=(7, 4))
    assert sending.send_blocker(doc, LETTER, pen, check(pen), False) == \
        "Load the AutoBlade into Tool 1 to send."
    assert sending.send_blocker(doc, LETTER, READY, check(READY), False) is None
    doc.move_to(300, 10)
    assert sending.send_blocker(doc, LETTER, READY, check(READY), False) == \
        "Move the design inside the cut area."
    assert sending.blocker_for_test_cut(READY, check(READY), False) is None


def test_job_spec_from_placed_design_plans(doc):
    spec = sending.job_spec(doc, LETTER, VINYL.to_tool_settings(), preview_outline=True)
    summary, error = sending.plan_summary(spec)
    assert error is None and summary["estimated_seconds"] > 0
    plan = build_plan(spec)
    assert any(step.confirm for step in plan.steps)
    test_spec = sending.spec_for_test_cut(LETTER, VINYL.to_tool_settings())
    assert not test_spec.preview_outline and sending.plan_summary(test_spec)[1] is None
    doc.move_to(400, 0)                         # off the mat: refused before anything is sent
    assert sending.plan_summary(sending.job_spec(doc, LETTER, VINYL.to_tool_settings(), True))[0] is None


# --- runner ----------------------------------------------------------------------------
def _spec(doc, preview=True) -> dict:
    return sending.job_spec(doc, LETTER, VINYL.to_tool_settings(), preview).to_dict()


def test_runner_holds_at_the_outline_gate_then_finishes(doc):
    async def scenario():
        client = FakeDaemonClient()
        updates = []
        runner = JobRunner(client, on_update=lambda r: updates.append([s.state for s in r.steps]))
        task = asyncio.ensure_future(runner.run_job(_spec(doc)))
        while not runner.holding:
            await asyncio.sleep(0.01)
        gate = next(i for i, s in enumerate(runner.steps) if s.confirm)
        assert client.steps_done == list(range(gate)) and runner.current.state is StepState.HOLD
        runner.confirm_cut()
        assert await task is Outcome.DONE
        assert client.steps_done == list(range(len(runner.steps))) and client.ended == 1
        assert client.stops == 0 and runner.progress() == 1.0
    asyncio.run(scenario())


def test_cancel_at_the_gate_stops_and_closes_the_session(doc):
    async def scenario():
        client = FakeDaemonClient()
        runner = JobRunner(client)
        task = asyncio.ensure_future(runner.run_job(_spec(doc)))
        while not runner.holding:
            await asyncio.sleep(0.01)
        reply = await runner.cancel()
        assert reply["ok"] and await task is Outcome.CANCELLED
        assert client.stops == 1 and client.sessions_closed == 1 and client.ended == 0
        assert not any(i >= next(j for j, s in enumerate(runner.steps) if s.confirm)
                       for i in client.steps_started)
    asyncio.run(scenario())


def test_cancel_mid_step_stops_at_once(doc):
    async def scenario():
        client = FakeDaemonClient()
        client.hold_step = (2, asyncio.Event())             # a step that never ends by itself
        runner = JobRunner(client)
        task = asyncio.ensure_future(runner.run_job(_spec(doc, preview=False)))
        while 2 not in client.steps_started:
            await asyncio.sleep(0.01)
        await runner.cancel()
        assert await task is Outcome.CANCELLED and client.stops == 1 and client.sessions_closed == 1
        assert runner.steps[2].state is StepState.FAILED
        client.stop_reply = {"ok": False, "error": "no daemon"}
        assert (await runner.cancel())["ok"]                  # already ended: nothing more to do
    asyncio.run(scenario())


def test_failed_step_and_lost_session(doc):
    async def scenario():
        client = FakeDaemonClient()
        client.fail_at = 1
        runner = JobRunner(client)
        assert await runner.run_job(_spec(doc, preview=False)) is Outcome.FAILED
        assert runner.error == "simulated failure" and runner.steps[1].state is StepState.FAILED

        client = FakeDaemonClient()
        client.fail_at, client.fail = 2, ConnectionError("the daemon closed the connection")
        runner = JobRunner(client)
        assert await runner.run_job(_spec(doc, preview=False)) is Outcome.LOST
        assert "closed" in runner.error

        client = FakeDaemonClient()
        client.open_reply = {"ok": False, "error": "Tool 1: AutoBlade required"}
        runner = JobRunner(client)
        assert await runner.run_job(_spec(doc)) is Outcome.FAILED and not client.steps_started
    asyncio.run(scenario())


def test_progress_is_weighted_by_writes():
    runner = JobRunner(FakeDaemonClient(), clock=lambda: 10.0)
    runner.steps = [RunStep("a", writes=1, state=StepState.DONE),
                    RunStep("b", writes=3, state=StepState.TODO)]
    assert runner.progress() == pytest.approx(0.25)
    runner.summary = {"estimated_seconds": 40}
    runner.steps[1].state, runner.steps[1].started_at = StepState.ACTIVE, 0.0
    assert runner.step_progress(runner.steps[1]) == pytest.approx(10 / 30)


def test_every_css_class_in_the_style_sheet_is_used_by_the_gui():
    """No styling is left behind for widgets that no longer exist (e.g. a removed page)."""
    import re
    from pathlib import Path
    gui = Path(__file__).resolve().parents[1] / "src" / "opencutter" / "gui"
    css = re.sub(r"/\*.*?\*/", "", (gui / "style.css").read_text(), flags=re.S)
    classes = set(re.findall(r"\.([A-Za-z][\w-]*)", re.sub(r"\{[^}]*\}", "{}", css)))
    code = "".join(p.read_text() for p in gui.glob("*.py"))
    assert len(classes) > 20
    assert not sorted(c for c in classes if f'"{c}"' not in code and f"'{c}'" not in code)
