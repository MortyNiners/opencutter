# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""End to end, offline: an SVG file through the whole app chain to the bytes the machine gets.

load_svg -> placement on the media (the GUI's PlacedDocument) -> a BASIC preset -> JobSpec ->
the real daemon (opencutter.device.daemon.serve) on a private socket and lock, with stub
bleak/dbus_fast and the simulated machine of tests/stubs.py -> a DaemonClient session that
runs every step. Nothing reaches Bluetooth or the real daemon socket.
"""
import asyncio
import json
from pathlib import Path

import stubs
from stubs import ESC05, ESC09, ESC15, harness, stops  # noqa: F401

stubs.install()
from opencutter.device.client import DaemonClient  # noqa: E402
from opencutter.device.protocol import lowers_tool  # noqa: E402
from opencutter.gui import geometry, sending  # noqa: E402
from opencutter.gui.document import PlacedDocument  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.job.spec import JobSpec  # noqa: E402
from opencutter.job.validate import make_write_validator  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS  # noqa: E402
from opencutter.svg.importer import load_svg  # noqa: E402

DATA = Path(__file__).with_name("data")
UNITS = b"U\x03"


def _vinyl_spec(preview_outline: bool = True) -> dict:
    """shapes.svg (curves, closed and open paths) on Letter media, cut with the BASIC Vinyl
    preset, as the GUI builds it; returned in its JSON wire form."""
    media = geometry.media_rect("letter", False, (0.0, 0.0))
    doc = PlacedDocument(load_svg(str(DATA / "shapes.svg")), media)
    doc.move_to(40.0, 30.0)
    assert geometry.design_fits(doc.bbox(), media)
    vinyl = next(p for p in BASIC_PRESETS if p.material == "Vinyl" and p.action == "Cut")
    spec = sending.job_spec(doc, media, vinyl.to_tool_settings(), preview_outline)
    return json.loads(json.dumps(spec.to_dict()))


def _data_writes(writes: list[bytes]) -> list[bytes]:
    """The machine's write log without ESC queries and the U unit query."""
    return [w for w in writes if w[:1] != b"\x1b" and w != UNITS]


def _assert_tools_rechecked(writes: list[bytes]) -> None:
    """Every write that can lower a tool comes right after a fresh ESC 15."""
    for i, w in enumerate(writes):
        if w[:1] != b"\x1b" and lowers_tool(w):
            assert i > 0 and writes[i - 1] == ESC15, f"no tool re-check before write {i}"


def test_svg_to_machine_bytes_through_the_daemon(harness):
    spec_dict = _vinyl_spec()
    spec = JobSpec.from_dict(spec_dict)
    plan = build_plan(spec)
    expected = [w for s in plan.steps if s.kind == "write" for w in s.writes]
    cut = next(s for s in plan.steps if s.confirm)
    assert sum(map(len, cut.writes)) > 2 * 1024        # the cut goes out in several batches

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.05)
        async with h.client().session() as session:
            opened = await session.open_job(spec_dict)
            assert opened["ok"], opened
            assert [s["title"] for s in opened["steps"]] == [s.title for s in plan.steps]
            assert opened["summary"] == plan.summary
            for i in range(len(opened["steps"])):
                result = await session.step(i, timeout=60)
                assert result["ok"], (i, opened["steps"][i]["title"], result)
            await session.end()
        return list(f.writes), f.max_queued

    writes, max_queued = harness.run(scenario)

    # exactly the plan's bytes, in order, with nothing added or left out
    assert _data_writes(writes) == expected
    # every byte that reached the machine is valid for this job
    validator = make_write_validator(spec)
    for w in _data_writes(writes):
        validator(w)
    # the only other traffic: one U query (after the header) and the ESC 05 / ESC 15 queries
    assert writes.count(UNITS) == 1
    assert writes.index(UNITS) > writes.index(expected[0])
    assert {w for w in writes if w[:1] == b"\x1b"} <= {ESC05, ESC15}
    # no stop, at most one batch queued, and the tools re-read before anything goes down
    assert ESC09 not in writes and stops(writes) == 0
    assert max_queued <= 1024
    _assert_tools_rechecked(writes)
    assert expected[-1].endswith(b"M0,0\x03FN0\x03TB50,0\x03")


def _cancel_mid_cut(harness, cancel) -> tuple[dict, list[bytes], int, bool]:
    """Run the job up to its cut step, call `cancel(client, session)` once the machine is
    cutting, and return (cut step result, writes, index of the first stop, still moving)."""
    spec_dict = _vinyl_spec(preview_outline=False)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.4)
        client = h.client()
        async with client.session() as session:
            opened = await session.open_job(spec_dict)
            assert opened["ok"], opened
            cut = next(i for i, s in enumerate(opened["steps"]) if s["title"].startswith("Cut"))
            for i in range(cut):
                assert (await session.step(i, timeout=60))["ok"]
            running = asyncio.ensure_future(session.step(cut, timeout=60))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 20
            while not any(b"D" in w for w in _data_writes(f.writes)[-1:]) or not f.moving():
                assert loop.time() < deadline, "the cut never started"
                await asyncio.sleep(0.02)
            result = await cancel(client, session, running)
        await asyncio.sleep(0.5)                 # anything still queued would arrive now
        first_stop = f.writes.index(ESC09)
        return result, list(f.writes), first_stop, f.moving()

    return harness.run(scenario)


def _assert_stopped_cleanly(writes: list[bytes], first_stop: int, moving: bool) -> None:
    assert writes[first_stop:first_stop + 2] == [ESC09, ESC09]
    assert not any(w[:1] != b"\x1b" for w in writes[first_stop:]), "data written after the stop"
    assert not moving


def test_cancel_job_mid_cut_stops_the_machine(harness):
    async def cancel_job(client: DaemonClient, session, running):
        reply = await client.stop()              # what Cancel Job sends, on its own connection
        assert reply["ok"], reply
        return await running

    result, writes, first_stop, moving = _cancel_mid_cut(harness, cancel_job)
    assert result["ok"] is False and result["stopped"] is True and not result["stop_errors"]
    _assert_stopped_cleanly(writes, first_stop, moving)


def test_dropping_the_session_mid_cut_stops_the_machine(harness):
    async def drop(client: DaemonClient, session, running):
        running.cancel()
        await session.__aexit__(None, None, None)   # the app went away mid-cut
        await asyncio.sleep(1.0)
        return {}

    _, writes, first_stop, moving = _cancel_mid_cut(harness, drop)
    _assert_stopped_cleanly(writes, first_stop, moving)
    assert stops(writes) == 1
