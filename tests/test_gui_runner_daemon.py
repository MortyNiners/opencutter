# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The GUI's JobRunner and MachineMonitor against the real daemon and client, on a simulated
machine (tests/stubs.py: fake BLE, private socket and lock). Nothing reaches Bluetooth."""
import asyncio

import stubs
from stubs import harness, stops  # noqa: F401

stubs.install()
from opencutter.gui import geometry, sending  # noqa: E402
from opencutter.gui.document import PlacedDocument  # noqa: E402
from opencutter.gui.machine import MachineMonitor  # noqa: E402
from opencutter.gui.runner import JobRunner, Outcome, StepState  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS  # noqa: E402
from opencutter.svg.importer import load_svg  # noqa: E402

DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 100 100"
  fill="none" stroke="#000"><rect x="20" y="30" width="30" height="20"/></svg>"""
VINYL = next(p for p in BASIC_PRESETS if p.action == "Cut")
LETTER = geometry.media_rect("letter", False, (0, 0))


def _spec(tmp_path) -> dict:
    path = tmp_path / "square.svg"
    path.write_text(DESIGN)
    doc = PlacedDocument(load_svg(str(path)), LETTER)
    return sending.job_spec(doc, LETTER, VINYL.to_tool_settings(), preview_outline=True).to_dict()


async def _until(predicate, timeout: float = 20) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def test_monitor_and_job_through_the_daemon(harness, tmp_path):
    spec = _spec(tmp_path)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.1)
        client = h.client()
        monitor = MachineMonitor(client)
        state = await monitor.refresh()
        assert state.ready and state.tools == (2, 4) and state.firmware == "CAMEO 4 V5.05"

        runner = JobRunner(client)
        task = asyncio.ensure_future(runner.run_job(spec))
        await _until(lambda: runner.holding)
        at_gate = len(f.writes)
        await asyncio.sleep(0.3)
        assert f.writes[at_gate:] == []                    # nothing is sent while the gate waits
        runner.confirm_cut()
        assert await task is Outcome.DONE, runner.error
        assert all(s.state is StepState.DONE for s in runner.steps)
        assert stops(f.writes) == 0 and any(b"D" in w for w in f.writes if w[:1] != b"\x1b")
    harness.run(scenario)


def test_cancel_job_stops_the_machine_through_the_daemon(harness, tmp_path):
    spec = _spec(tmp_path)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=3.0)
        runner = JobRunner(h.client())
        task = asyncio.ensure_future(runner.run_job(spec))
        await _until(lambda: runner.holding)
        runner.confirm_cut()
        await _until(lambda: runner.current is not None and runner.current.confirm
                     and runner.current.state is StepState.ACTIVE and f.moving())
        reply = await runner.cancel()
        assert reply["ok"], reply
        assert await task is Outcome.CANCELLED
        assert stops(f.writes) >= 1 and not f.moving()
    harness.run(scenario)
