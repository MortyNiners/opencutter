# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The machine's Pause during a job in the app (0.1.1), with the fake daemon client and, at the
end, with the real service on a simulated machine (tests/stubs.py). Nothing touches Bluetooth.

- The app reads the service's pause fields (paused, resume_ok, pause_ok, no_reply_s,
  indication_age_s, stop_check) and its "left-pause" mark; an older service offers nothing.
- A paused machine: the pill 'Paused' (no pulse), 'Paused on the machine' in the status bar,
  the note (PAUSED_NOTE with Resume, PAUSED_NOTE_KEY without), the pause icon on the step, and
  the progress estimate stops. Nothing is sent but what a click asks for.
- Resume: shown only while the service would send it; one op per click ('Resuming…'), never
  within the guard time after the last answer, never again by itself; NOT_RESUMED when the
  machine is still paused, the refusal's reason when it was not sent.
- Pause (the CAMEO 4 on firmware V5 only, not at the outline check): one op per click; the view
  shows the machine paused only once the service says so.
- Cancel Job while paused: the stop asks for the read-back, 'Stopping…' with CANCELLING_PAUSED
  and the pill 'Cancelling…' until the machine ended the job, then the usual page, and
  STILL_PAUSED when it still reported Paused (Stop Machine says so too). A plain Cancel asks for
  no read-back.
- 'Checking connection' with its note while the job's query has had no answer for 7 s or more.

The window tests need a display: run headless with GDK_BACKEND=broadway and a private
gtk4-broadwayd (they are skipped when GTK cannot open one); the others need none.
"""
import asyncio
import re
import types

import pytest

import stubs
from stubs import ESC05, ESC07, ESC08, ESC09, harness, stops  # noqa: F401

stubs.install()
from opencutter.device import daemon  # noqa: E402
from opencutter.gui import geometry, machine, runner as runner_mod, sending  # noqa: E402
from opencutter.gui.document import PlacedDocument  # noqa: E402
from opencutter.gui.machine import (LEFT_PAUSE_MARK, Link, MachineMonitor,  # noqa: E402
                                    MachineState, StopCheck, checking_note, paused_note,
                                    stop_fields)
from opencutter.gui.runner import (COMMAND_BUSY, NOT_PAUSED, NOT_RESUMED, JobRunner,  # noqa: E402
                                   Outcome, RunStep, StepState, command_note)
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from opencutter.svg.importer import load_svg  # noqa: E402
from gui_fakes import INFO, READY_PING, FakeDaemonClient  # noqa: E402

# a 30 x 20 mm rectangle at x 150..180, y 30..50 on a 300 mm page
DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="300mm" height="300mm" viewBox="0 0 300 300"
  fill="none" stroke="#000"><rect x="150" y="30" width="30" height="20"/></svg>"""
PAUSED_STATE = MachineState(link=Link.CONNECTED, status="3", profile="cameo4", support="tested",
                            paused=True, resume_ok=True)


async def _until(predicate, timeout: float = 10) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


# --- the service's fields ---------------------------------------------------------------------
def test_the_app_reads_the_services_pause_keys_and_marks():
    assert machine.PING_STOP_KEYS == daemon.PING_STOP_KEYS
    assert LEFT_PAUSE_MARK == daemon.LEFT_PAUSE_MARK
    assert set(FakeDaemonClient().stop_fields()) == set(daemon.PING_STOP_KEYS)


def test_the_stop_fields_read_the_pause_fields_and_refuse_odd_values():
    fields = stop_fields({"paused": True, "resume_ok": True, "pause_ok": False,
                          "no_reply_s": 9.4, "indication_age_s": 1.2,
                          "interrupted": LEFT_PAUSE_MARK,
                          "stop_check": {"running": False, "status": "3", "seconds": 15.1,
                                         "still_busy": False, "still_paused": True,
                                         "reason": None}})
    assert fields["paused"] is True and fields["resume_ok"] is True and fields["pause_ok"] is False
    assert fields["no_reply_s"] == 9.4 and fields["indication_age_s"] == 1.2
    assert fields["interrupted"] == LEFT_PAUSE_MARK
    assert fields["stop_check"] == StopCheck(running=False, status="3", seconds=15.1,
                                             still_busy=False, still_paused=True)
    # only a real True counts; seconds must be numbers (not a bool, not NaN, not negative)
    odd = stop_fields({"paused": 1, "resume_ok": "yes", "pause_ok": None, "no_reply_s": True,
                       "interrupted": "left", "stop_check": "running"})
    assert odd["paused"] is odd["resume_ok"] is odd["pause_ok"] is False
    assert odd["no_reply_s"] is None and odd["interrupted"] is None and odd["stop_check"] is None
    for bad in (float("nan"), -1, float("inf"), "9"):
        assert stop_fields({"no_reply_s": bad})["no_reply_s"] is None
    # the indications' age only matters (and is only kept) while a reply is overdue
    assert stop_fields({"no_reply_s": None, "indication_age_s": 3.0})["indication_age_s"] is None
    check = stop_fields({"stop_check": {"running": True, "status": "33", "seconds": "x",
                                        "still_busy": 1, "reason": "no reply\nto ESC 05"}})
    assert check["stop_check"] == StopCheck(running=True, reason="no reply to ESC 05")


def test_an_older_service_offers_nothing():
    """A service without the pause fields (before API 2): nothing paused, nothing offered."""
    fields = stop_fields({"stop_owed": False, "owed_stops_sent": 0, "motion_stopped": False,
                          "interrupted": None, "load_seen": 0})
    assert not fields["paused"] and not fields["resume_ok"] and not fields["pause_ok"]
    assert fields["no_reply_s"] is None and fields["stop_check"] is None
    state = MachineState(link=Link.CONNECTED, status="1", profile="cameo4", support="tested",
                         **fields)
    assert not state.shows_paused and not state.offers_resume and not state.checking
    assert paused_note(state) is None and checking_note(state) is None


def test_the_paused_and_checking_notes():
    assert paused_note(PAUSED_STATE) == machine.PAUSED_NOTE and PAUSED_STATE.offers_resume
    no_resume = MachineState(link=Link.CONNECTED, status="3", profile="cameo4",
                             support="tested", paused=True, resume_ok=False)
    assert paused_note(no_resume) == machine.PAUSED_NOTE_KEY and not no_resume.offers_resume
    # the machine reads Paused before the session said so (or an older service): shown
    # paused, but Resume is only offered once the service would send it
    status_only = MachineState(link=Link.CONNECTED, status="3")
    assert status_only.shows_paused and paused_note(status_only) == machine.PAUSED_NOTE_KEY
    # the service's word counts even when the last status it reports is not Paused (yet)
    session_only = MachineState(link=Link.CONNECTED, status="1", profile="cameo4",
                                support="tested", paused=True, resume_ok=True)
    assert session_only.shows_paused and paused_note(session_only) == machine.PAUSED_NOTE
    assert not MachineState(status="3").shows_paused                # not connected
    busy = MachineState(link=Link.CONNECTED, status="1", no_reply_s=9.8, indication_age_s=1.0)
    assert checking_note(busy) == (machine.CHECKING_NOTE.format(n=9)
                                   + machine.STILL_REPORTS.format(term="Busy"))
    quiet = MachineState(link=Link.CONNECTED, status="1", no_reply_s=12.0, indication_age_s=8.0)
    assert checking_note(quiet) == machine.CHECKING_NOTE.format(n=12)
    assert checking_note(MachineState(link=Link.CONNECTED, status="1", no_reply_s=7.0)) == (
        machine.CHECKING_NOTE.format(n=7))
    for text in (machine.PAUSED_NOTE, machine.PAUSED_NOTE_KEY, machine.CHECKING_NOTE):
        assert "ESC" not in text and "`" not in text


def test_pause_is_offered_only_on_a_tested_machine_that_has_one():
    tested = MachineState(link=Link.CONNECTED, status="1", profile="cameo4", support="tested")
    assert tested.offers_pause
    assert not MachineState(link=Link.CONNECTED, status="1", profile="cameo4",
                            support="untested").offers_pause
    assert not MachineState(link=Link.CONNECTED, status="1", profile="cameo5",
                            support="untested").offers_pause
    assert not MachineState(status="1", profile="cameo4", support="tested").offers_pause


def test_a_job_that_ends_without_our_cancel_is_offered_nothing():
    running = dict(link=Link.CONNECTED, profile="cameo4", support="tested")
    assert not MachineState(status="1", **running).ends_job
    assert not MachineState(status="3", paused=True, **running).ends_job
    assert MachineState(status="4", **running).ends_job                     # Cancelling
    assert MachineState(status="0", interrupted="4", **running).ends_job
    assert MachineState(status="0", interrupted=LEFT_PAUSE_MARK, **running).ends_job
    assert MachineState(status="1", motion_stopped=True, **running).ends_job
    assert not MachineState(status="4", interrupted="4").ends_job          # not connected


def test_the_monitor_keeps_the_tools_when_a_paused_machine_is_not_asked_for_them():
    async def scenario():
        client = FakeDaemonClient()
        monitor = MachineMonitor(client)
        await monitor.refresh()
        assert monitor.state.tools == (2, 4)
        # the service sends no ESC 15 (and never FG) to a paused machine: "tools" is None
        client.pause_machine()
        client.info_reply = {**INFO, "status": "3", "tools": None}
        state = await monitor.refresh()
        assert state.tools == (2, 4) and state.status == "3" and state.firmware == "CAMEO 4 V5.05"
        assert state.paused and state.resume_ok and state.shows_paused
        # a dropped link clears every pause field: nothing is offered without a link
        client.ping_reply = {**READY_PING, "connected": False, "status": None, "device": None,
                             "reason": None, "model": None}
        await monitor.refresh()
        state = monitor.state
        assert not state.paused and not state.resume_ok and not state.offers_resume
    asyncio.run(scenario())


# --- the runner -------------------------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_the_progress_estimate_stops_while_the_machine_is_paused():
    clock = _Clock()
    runner = JobRunner(FakeDaemonClient(), clock=clock)
    runner.summary = {"estimated_seconds": 100}
    runner.started_at = clock.now
    step = RunStep("Cut with the AutoBlade", writes=1, kind="write", state=StepState.ACTIVE,
                   started_at=clock.now)
    runner.steps = [step]
    clock.now += 10
    assert runner.step_progress(step) == pytest.approx(0.10)
    runner.set_paused(True)
    clock.now += 50
    assert runner.step_progress(step) == pytest.approx(0.10)       # frozen while paused
    assert runner.seconds_left() == pytest.approx(90)
    assert runner.elapsed() == pytest.approx(60)                   # the clock goes on
    runner.set_paused(False)
    clock.now += 10
    assert runner.step_progress(step) == pytest.approx(0.20)
    assert runner.paused_seconds() == pytest.approx(50)
    # a step that starts after a pause counts from its own start
    later = RunStep("Return", writes=1, kind="write", state=StepState.ACTIVE,
                    started_at=clock.now, paused_before=runner.paused_seconds())
    clock.now += 5
    assert runner.step_progress(later) == pytest.approx(0.05)


def test_the_notes_of_resume_and_pause():
    assert command_note("resume", {"ok": True, "paused": False}) is None
    assert command_note("resume", {"ok": True, "paused": True}) == NOT_RESUMED
    assert command_note("resume", {"ok": True, "paused": True}, still_paused=False) is None
    assert command_note("pause", {"ok": True, "paused": True}) is None
    assert command_note("pause", {"ok": True, "paused": False}) == NOT_PAUSED
    assert command_note("resume", {"ok": False, "sent": False,
                                   "error": "the machine is not paused (status '1' = Busy)"}) == (
        "Resume was not sent: the machine is not paused (status '1' = Busy).")
    unknown = command_note("pause", {"ok": False, "sent": True, "error": "the pause write "
                                     "failed (BleakError: x); whether it arrived is unknown"})
    assert unknown.startswith("Pause may not have reached the machine (the pause write failed")
    assert unknown.endswith("OpenCutter does not send it again.")
    # the service's reply to a failed write: its write error, said once (its "error" already
    # says that it is not sent again)
    failed = command_note("resume", {
        "ok": False, "sent": True, "errors": ["BleakError: x"],
        "error": "the resume write failed (BleakError: x); whether it arrived is unknown, and it "
                 "is not sent again"})
    assert failed == ("Resume may not have reached the machine (the write failed: BleakError: "
                      "x). OpenCutter does not send it again.")
    # the Pause went out but the machine has not read Paused yet: it may still pause, so the
    # note never sends the user to the machine's own Pause key (a press after it paused would
    # resume it) and never says that the job goes on for sure
    assert "Pause key" not in NOT_PAUSED and "goes on" not in NOT_PAUSED
    assert "may still pause" in NOT_PAUSED and "Cancel Job" in NOT_PAUSED
    assert "press Pause here" in NOT_PAUSED                 # the app's button, not the key


def test_the_finished_page_texts_never_advise_the_pause_key(ui):
    """After a stop no text sends the user to the machine's Pause key (after a cancel it does
    nothing, and before the cancel ended it could make the machine go on)."""
    jobview = ui.jobview
    for text in (jobview.CANCELLING_PAUSED, jobview.STILL_PAUSED, jobview.STOPPING_HINT):
        assert "Pause key" not in text and "press Pause" not in text, text
    assert "Unload" in jobview.STILL_PAUSED and "power button" in jobview.STILL_PAUSED
    assert command_note("resume", {"ok": False, "sent": None, "error": "DaemonError: gone"}) == (
        runner_mod.COMMAND_UNKNOWN.format(what="Resume", reason="DaemonError: gone"))


def test_one_resume_or_pause_at_a_time_and_none_after_cancel(tmp_path):
    async def scenario():
        client = FakeDaemonClient()
        client.hold_step = (1, asyncio.Event())
        client.pause_machine()
        client.command_gate = asyncio.Event()
        runner = JobRunner(client)
        job = asyncio.ensure_future(runner.run_job(_plain_spec(tmp_path)))
        await _until(lambda: 1 in client.steps_started)
        first = asyncio.ensure_future(runner.resume())
        await _until(lambda: client.commands == ["resume"])
        assert runner.command_pending == "resume"
        assert await runner.pause() == {"ok": False, "sent": False, "error": COMMAND_BUSY}
        assert await runner.resume() == {"ok": False, "sent": False, "error": COMMAND_BUSY}
        client.command_gate.set()
        assert (await first)["ok"] and runner.command_pending is None
        assert runner.command_note is None and client.commands == ["resume"]
        await runner.cancel()
        assert await job is Outcome.CANCELLED
        assert (await runner.pause())["sent"] is False and client.commands == ["resume"]
    asyncio.run(scenario())


def test_a_resume_the_machine_ignores_waits_its_time_and_is_not_sent_again(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_mod, "RESUME_SECONDS", 0.3)

    async def scenario():
        client = FakeDaemonClient()
        client.hold_step = (1, asyncio.Event())
        client.pause_machine()
        client.obeys = False
        runner = JobRunner(client)
        runner.set_paused(True)
        job = asyncio.ensure_future(runner.run_job(_plain_spec(tmp_path)))
        await _until(lambda: 1 in client.steps_started)
        loop = asyncio.get_running_loop()
        start = loop.time()
        reply = await runner.resume()
        assert reply["ok"] and reply["paused"] is True
        assert loop.time() - start >= 0.29                    # 'Resuming…' that long
        assert runner.command_note == NOT_RESUMED
        await asyncio.sleep(0.5)
        assert client.commands == ["resume"]                  # never sent again by itself
        runner.set_paused(False)                              # the machine went on: note gone
        assert runner.command_note is None
        await runner.cancel()
        await job
    asyncio.run(scenario())


def test_no_resume_or_pause_once_cancel_was_pressed(tmp_path):
    """Between the Cancel click and the run's end (the run task has not seen its cancel yet)
    a click on Resume or Pause sends nothing."""
    async def scenario():
        client = FakeDaemonClient()
        client.hold_step = (1, asyncio.Event())
        client.pause_machine()
        runner = JobRunner(client)
        job = asyncio.ensure_future(runner.run_job(_plain_spec(tmp_path)))
        await _until(lambda: 1 in client.steps_started)
        cancel = asyncio.ensure_future(runner.cancel(check=True))
        await asyncio.sleep(0)                      # Cancel ran up to the stop's reply
        assert runner.outcome is Outcome.RUNNING    # ... and the run has not ended yet
        assert (await runner.resume())["sent"] is False
        assert (await runner.pause())["sent"] is False
        await cancel
        assert await job is Outcome.CANCELLED
        return client
    client = asyncio.run(scenario())
    assert client.commands == [] and client.stop_checks == [True]


def test_a_resume_the_window_saw_work_leaves_no_note(monkeypatch, tmp_path):
    """The service's answer still said 'paused', but a poll meanwhile saw the machine go on:
    NOT_RESUMED would contradict what the view shows."""
    monkeypatch.setattr(runner_mod, "RESUME_SECONDS", 0.1)

    async def scenario():
        client = FakeDaemonClient()
        client.hold_step = (1, asyncio.Event())
        client.pause_machine()
        client.resume_reply = {"ok": True, "sent": True, "status_before": "3",
                               "status_after": "3", "paused": True, "errors": []}
        client.command_gate = asyncio.Event()
        runner = JobRunner(client)
        runner.set_paused(True)
        job = asyncio.ensure_future(runner.run_job(_plain_spec(tmp_path)))
        await _until(lambda: 1 in client.steps_started)
        asked = asyncio.ensure_future(runner.resume())
        await _until(lambda: client.commands == ["resume"])
        runner.set_paused(False)                    # the window's poll: it went on
        client.command_gate.set()
        assert (await asked)["paused"] is True
        assert runner.command_note is None
        await runner.cancel()
        await job
    asyncio.run(scenario())


def test_a_pause_the_window_saw_before_the_answer_leaves_no_note(tmp_path):
    """The service's answer said 'not paused yet', but a poll meanwhile saw the machine pause:
    the view shows it paused, so NOT_PAUSED would contradict it."""
    async def scenario():
        client = FakeDaemonClient()
        client.hold_step = (1, asyncio.Event())
        client.pause_ok, client.obeys = True, False
        client.command_gate = asyncio.Event()
        runner = JobRunner(client)
        job = asyncio.ensure_future(runner.run_job(_plain_spec(tmp_path)))
        await _until(lambda: 1 in client.steps_started)
        asked = asyncio.ensure_future(runner.pause())
        await _until(lambda: client.commands == ["pause"])
        runner.set_paused(True)                     # the window's poll: paused
        client.command_gate.set()
        reply = await asked
        assert reply["ok"] and reply["paused"] is False
        assert runner.command_note is None
        await runner.cancel()
        await job
    asyncio.run(scenario())


def test_the_note_of_a_click_goes_when_the_next_step_starts(tmp_path):
    async def scenario():
        client = FakeDaemonClient()
        client.hold_step = (1, asyncio.Event())
        client.pause_reply = {"ok": False, "sent": False, "error": "the machine is not "
                              "answering (its status could not be read within 3 s)"}
        runner = JobRunner(client)
        job = asyncio.ensure_future(runner.run_job(_plain_spec(tmp_path)))
        await _until(lambda: 1 in client.steps_started)
        await runner.pause()
        assert runner.command_note == ("Pause was not sent: the machine is not answering (its "
                                       "status could not be read within 3 s).")
        client.hold_step[1].set()
        await _until(lambda: 2 in client.steps_started)
        assert runner.command_note is None
        await runner.cancel()
        await job
    asyncio.run(scenario())


@pytest.mark.parametrize("status_after, still", [("0", False), ("2", False), ("3", True)])
def test_a_paused_cancel_asks_for_the_read_back(status_after, still, tmp_path):
    async def scenario():
        client = FakeDaemonClient()
        client.hold_step = (1, asyncio.Event())
        client.pause_machine()
        client.stop_status_after = status_after
        runner = JobRunner(client)
        job = asyncio.ensure_future(runner.run_job(_plain_spec(tmp_path)))
        await _until(lambda: 1 in client.steps_started)
        reply = await runner.cancel(check=True)
        assert await job is Outcome.CANCELLED
        return client, runner, reply
    client, runner, reply = asyncio.run(scenario())
    assert client.stop_checks == [True] and reply["status_after"] == status_after
    assert runner.paused_cancel and runner.still_paused is still
    assert runner.stop_seconds is not None and not runner.stop_failed


def test_a_plain_cancel_asks_for_no_read_back(tmp_path):
    async def scenario():
        client = FakeDaemonClient()
        client.hold_step = (1, asyncio.Event())
        client.set_status("3")                      # whatever the machine reads: not asked
        runner = JobRunner(client)
        job = asyncio.ensure_future(runner.run_job(_plain_spec(tmp_path)))
        await _until(lambda: 1 in client.steps_started)
        await runner.cancel()
        await job
        return client, runner
    client, runner = asyncio.run(scenario())
    assert client.stop_checks == [False] and not runner.paused_cancel and not runner.still_paused


def _plain_spec(tmp_path) -> dict:
    """A small job without the outline check."""
    preset = next(p for p in BASIC_PRESETS if p.action == "Cut")
    media = geometry.media_rect("custom", False, (254.0, 254.0))
    path = tmp_path / "plain.svg"
    path.write_text(DESIGN)
    doc = PlacedDocument(load_svg(str(path)), media)
    return sending.job_spec(doc, media, preset.to_tool_settings(), False).to_dict()


# --- the window (broadway) --------------------------------------------------------------------
@pytest.fixture(scope="module")
def ui():
    """The GTK modules; skips when GTK cannot open a display."""
    import opencutter.gui  # noqa: F401  (selects the GTK and libadwaita versions)
    from gi.events import GLibEventLoop
    from gi.repository import Adw, Gdk, GLib, Gtk
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("GTK cannot open a display")
    Adw.init()
    from opencutter.gui import jobview, window
    from opencutter.gui.settings import GuiSettings
    return types.SimpleNamespace(GLib=GLib, Gtk=Gtk, GLibEventLoop=GLibEventLoop,
                                 jobview=jobview, window=window, GuiSettings=GuiSettings)


@pytest.fixture
def run(ui):
    loop = ui.GLibEventLoop(ui.GLib.MainContext.default())
    yield loop.run_until_complete
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.wait(pending, timeout=5))
    loop.close()


@pytest.fixture
def svg(tmp_path):
    path = tmp_path / "design.svg"
    path.write_text(DESIGN)
    return str(path)


@pytest.fixture
def make(ui, tmp_path):
    windows = []

    def factory(client: FakeDaemonClient):
        settings = ui.GuiSettings.load(tmp_path / "gui.json")
        settings.units, settings.preview_outline = "mm", True
        settings.media, settings.custom_media_mm = "custom", [254.0, 254.0]
        win = ui.window.MainWindow(None, client, PresetStore(list(BASIC_PRESETS), basic_source()),
                                   settings)
        windows.append(win)
        return win
    yield factory
    for win in windows:
        win.destroy()


@pytest.fixture
def alerts(ui, monkeypatch):
    shown = []

    async def still_paused(parent, body):
        shown.append(("still_paused", body))

    async def still_busy(parent):
        shown.append("still_busy")

    async def lost(parent):
        shown.append("connection_lost")
        return False
    monkeypatch.setattr(ui.window.dialogs, "still_paused_after_stop", still_paused)
    monkeypatch.setattr(ui.window.dialogs, "still_busy_after_stop", still_busy)
    monkeypatch.setattr(ui.window.dialogs, "connection_lost", lost)
    return shown


def _window(make, run, svg, client: FakeDaemonClient):
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    return win


def _spec(ui, win, preview: bool = False) -> dict:
    return ui.window.job_spec(win.doc, win.page_setup.media_rect(),
                              win.send_panel.tool_settings(), preview).to_dict()


def _cut(spec: dict) -> int:
    from opencutter.job.spec import JobSpec
    steps = build_plan(JobSpec.from_dict(spec)).steps
    return next(i for i, s in enumerate(steps) if s.title.startswith("Cut"))


def _pill(win) -> tuple[str, str, bool]:
    """(text, tone, pulsing) of the window's pill."""
    dot = win.pill._dot
    tone = next(t for t in ("neutral", "info", "positive", "warning", "negative")
                if dot.has_css_class(t))
    return win.pill.text, tone, dot.has_css_class("pulse")


def _row(win, index: int):
    return win.job_view._rows[index]


def _label(button) -> str:
    return button.get_child().get_last_child().get_text()


async def _start_cut(win, client, spec, cut):
    """Run the job until its cut step runs (held), and let the monitor poll once."""
    client.hold_step = (cut, asyncio.Event())
    job = asyncio.ensure_future(win.run_job(spec))
    await _until(lambda: cut in client.steps_started)
    await asyncio.sleep(0.05)
    await win.monitor.refresh()
    return job


async def _finish(win, client, job):
    """End the held job with Cancel (a plain one)."""
    if win.runner.outcome is Outcome.RUNNING:
        await win.runner.cancel()
    client.hold_step[1].set()
    await asyncio.wait_for(job, 5)


def test_a_paused_machine_shows_the_pill_the_note_and_resume(ui, make, run, svg):
    client = FakeDaemonClient()
    client.pause_ok = True
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    cut = _cut(spec)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, cut)
        assert view.pause_button.get_visible() and view.pause_button.get_sensitive()
        assert not view.resume_button.get_visible() and _pill(win)[0].startswith("Cutting ")
        client.pause_machine()
        await win.monitor.refresh()
        assert _pill(win) == ("Paused", "warning", False)
        assert win.status_label.get_text() == "Status: Paused on the machine · machine: Paused"
        assert view._paused.get_visible() and view._paused.text == machine.PAUSED_NOTE
        assert view.resume_button.get_visible() and view.resume_button.get_sensitive()
        assert _label(view.resume_button) == ui.jobview.RESUME
        assert not view.pause_button.get_visible()
        assert view.resume_button.has_css_class("suggested-action")
        row = _row(win, cut)
        assert row._icon.get_visible() and not row._spinner.get_visible()
        assert row._icon.get_icon_name() == ui.jobview.PAUSED_ICON
        assert win.runner.machine_paused
        assert win.get_focus() is not view.resume_button       # never focused by the view
        assert not view.resume_button.get_focus_on_click()      # ... nor by a click
        assert not view.pause_button.get_focus_on_click()
        frozen = win.runner.progress()
        await asyncio.sleep(0.3)
        assert win.runner.progress() == frozen                  # the estimate stops
        client.go_on()
        await win.monitor.refresh()
        assert _pill(win)[0].startswith("Cutting ") and not view._paused.get_visible()
        assert not view.resume_button.get_visible() and view.pause_button.get_visible()
        assert row._spinner.get_visible() and not win.runner.machine_paused
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == [] and client.stop_checks == [False]


def test_a_machine_without_resume_says_to_press_its_pause_key(ui, make, run, svg):
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        client.resume_ok = False                                # e.g. not tested with Resume
        await win.monitor.refresh()
        assert _pill(win)[0] == "Paused" and view._paused.text == machine.PAUSED_NOTE_KEY
        assert not view.resume_button.get_visible() and not view.pause_button.get_visible()
        assert await win.resume_job() is None                   # nothing sent
        # an older service: only the status reads Paused
        client.paused = False
        await win.monitor.refresh()
        assert _pill(win)[0] == "Paused" and view._paused.text == machine.PAUSED_NOTE_KEY
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == []


def test_resume_sends_one_op_per_click_and_says_resuming(ui, make, run, svg):
    client = FakeDaemonClient()
    client.command_gate = asyncio.Event()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        await win.monitor.refresh()
        view.resume_button.emit("clicked")
        await _until(lambda: client.commands == ["resume"])
        await asyncio.sleep(0.05)
        assert _label(view.resume_button) == ui.jobview.RESUMING
        assert view.resume_button.get_visible() and not view.resume_button.get_sensitive()
        view.resume_button.emit("clicked")                      # a second click: nothing
        view.emit("resume")
        await asyncio.sleep(0.1)
        assert client.commands == ["resume"] and _pill(win)[0] == "Paused"
        client.command_gate.set()
        await _until(lambda: win.runner.command_pending is None)
        await asyncio.sleep(0.05)
        assert not view.resume_button.get_visible() and _pill(win)[0].startswith("Cutting ")
        assert not view._command.get_visible()
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == ["resume"] and client.stops == 1


def test_a_resume_the_machine_ignores_is_never_sent_again(ui, make, run, svg, monkeypatch):
    monkeypatch.setattr(runner_mod, "RESUME_SECONDS", 0.3)
    client = FakeDaemonClient()
    client.obeys = False
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        await win.monitor.refresh()
        await win.resume_job()
        assert view._command.get_visible() and view._command.text == NOT_RESUMED
        assert view.resume_button.get_visible() and _label(view.resume_button) == "Resume"
        assert await win.resume_job() is None                   # a double click: nothing
        await asyncio.sleep(0.6)
        await win.monitor.refresh()
        assert client.commands == ["resume"] and view._command.text == NOT_RESUMED
        client.go_on()                                          # the Pause key on the machine
        await win.monitor.refresh()
        assert not view._command.get_visible() and _pill(win)[0].startswith("Cutting ")
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == ["resume"]


@pytest.mark.parametrize("reply, text", [
    ({"ok": False, "sent": False, "error": "the machine is not paused (status '1' = Busy)"},
     "Resume was not sent: the machine is not paused (status '1' = Busy)."),
    ({"ok": False, "sent": True, "errors": ["BleakError: x"],
      "error": "the resume write failed (BleakError: x); whether it arrived is unknown, and it "
               "is not sent again"},
     "Resume may not have reached the machine (the write failed: BleakError: x). OpenCutter "
     "does not send it again."),
    ({"ok": False, "sent": None, "error": "DaemonError: the service did not answer"},
     "Resume may not have reached the machine (DaemonError: the service did not answer). "
     "OpenCutter does not send it again."),
])
def test_a_resume_that_did_not_go_through_says_why(ui, make, run, svg, reply, text):
    client = FakeDaemonClient()
    client.resume_reply = reply
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        await win.monitor.refresh()
        await win.resume_job()
        assert win.job_view._command.text == text
        assert win.job_view._paused.text == machine.PAUSED_NOTE      # still paused
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == ["resume"]


def test_pause_sends_one_op_and_the_view_follows_the_service(ui, make, run, svg, monkeypatch):
    monkeypatch.setattr(ui.window, "COMMAND_GUARD_SECONDS", 0.0)
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        # before the service would send it: shown, not sensitive, and a click sends nothing
        assert view.pause_button.get_visible() and not view.pause_button.get_sensitive()
        assert await win.pause_job() is None and client.commands == []
        client.pause_ok = True
        await win.monitor.refresh()
        assert view.pause_button.get_sensitive()
        # a machine that does not pause: the view never takes the click for a pause
        client.obeys = False
        await win.pause_job()
        assert client.commands == ["pause"] and view._command.text == NOT_PAUSED
        assert _pill(win)[0].startswith("Cutting ") and not view.resume_button.get_visible()
        # it pauses: shown once the service says so, not from the click
        client.obeys = True
        client.command_gate = asyncio.Event()
        view.pause_button.emit("clicked")
        await _until(lambda: client.commands == ["pause", "pause"])
        await asyncio.sleep(0.05)
        assert _label(view.pause_button) == ui.jobview.PAUSING
        assert not view.pause_button.get_sensitive()
        assert _pill(win)[0].startswith("Cutting ") and not view._command.get_visible()
        client.command_gate.set()
        await _until(lambda: win.runner.command_pending is None)
        await asyncio.sleep(0.05)
        assert _pill(win) == ("Paused", "warning", False)
        assert view._paused.text == machine.PAUSED_NOTE and view.resume_button.get_visible()
        assert not view.pause_button.get_visible() and not view._command.get_visible()
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == ["pause", "pause"]


def test_pause_is_not_offered_at_the_outline_check_or_on_another_machine(ui, make, run, svg):
    client = FakeDaemonClient()
    client.pause_ok = True
    win = _window(make, run, svg, client)
    spec = _spec(ui, win, preview=True)
    cut = _cut(spec)
    client.hold_step = (cut, asyncio.Event())
    view = win.job_view

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec))
        await _until(lambda: win.runner is not None and win.runner.holding)
        await win.monitor.refresh()
        assert not view.pause_button.get_visible() and await win.pause_job() is None
        # the CAMEO 4 on an untested firmware line: the service sends no Pause, none offered
        client.machine_reply = {"profile": "cameo4", "support": "untested",
                                "refused_reason": None}
        win.runner.confirm_cut()
        await _until(lambda: cut in client.steps_started)
        await win.monitor.refresh()
        assert win.monitor.state.support == "untested" and win.monitor.state.pause_ok
        assert not view.pause_button.get_visible() and await win.pause_job() is None
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == []


def test_cancel_while_paused_says_cancelling_until_the_machine_ended_the_job(ui, make, run, svg):
    client = FakeDaemonClient()
    client.stop_gate = asyncio.Event()
    client.stop_status_after = "0"
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        await win.monitor.refresh()
        win.activate_action("win.cancel-job")
        await _until(lambda: win.runner.outcome is Outcome.CANCELLED)
        await asyncio.sleep(0.05)
        assert client.stop_checks == [True] and win.runner.stop_pending
        assert view._done_title.get_text() == ui.jobview.STOPPING
        assert view._done_hint.get_text() == ui.jobview.CANCELLING_PAUSED
        win._update_pill()
        assert _pill(win) == (ui.jobview.CANCELLING, "warning", True)
        assert not job.done()
        client.stop_gate.set()
        await asyncio.wait_for(job, 5)
        assert view._done_title.get_text() == "Job cancelled"
        assert "check that the tool is up, press Unload, then load the mat" in (
            view._done_hint.get_text())
        assert not view._still_paused.get_visible()
        assert not win.pill.text.startswith(ui.jobview.CANCELLING)
    run(scenario())
    assert client.stops == 1 and client.commands == []


def test_a_machine_still_paused_after_the_stop_is_told(ui, make, run, svg):
    client = FakeDaemonClient()
    client.stop_status_after = "3"
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        await win.monitor.refresh()
        win.cancel_job()
        await asyncio.wait_for(job, 5)
    run(scenario())
    assert view._done_title.get_text() == "Job cancelled" and view._still_paused.get_visible()
    assert re.fullmatch(r"The machine still reports Paused \d+ s after the stop\. Press Unload "
                        r"on the machine; if anything moves, press its power button\.",
                        view._still_paused.text)
    assert ui.jobview.still_paused_text(15.4) == ui.jobview.STILL_PAUSED.format(n=15)


def test_a_paused_cancel_whose_stop_failed_says_so(ui, make, run, svg):
    client = FakeDaemonClient()
    client.stop_reply = {"ok": False, "errors": ["the stop write failed"],
                         "error": "the stop did not get through"}
    client.stop_status_after = "0"
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        await win.monitor.refresh()
        win.cancel_job()
        await asyncio.wait_for(job, 5)
        win._update_pill()
    run(scenario())
    assert client.stop_checks == [True] and win.runner.stop_failed
    assert view._done_title.get_text() == "Stop not confirmed"
    assert ui.jobview.STOP_FAILED_TEXT in view._done_hint.get_text()
    assert not view._still_paused.get_visible() and win.pill.text != ui.jobview.CANCELLING


def test_a_plain_cancel_keeps_the_usual_stopping_page(ui, make, run, svg):
    client = FakeDaemonClient()
    client.stop_gate = asyncio.Event()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        win.cancel_job()
        await _until(lambda: win.runner.outcome is Outcome.CANCELLED)
        await asyncio.sleep(0.05)
        assert win.job_view._done_hint.get_text() == ui.jobview.STOPPING_HINT
        win._update_pill()
        assert win.pill.text != ui.jobview.CANCELLING
        client.stop_gate.set()
        await asyncio.wait_for(job, 5)
    run(scenario())
    assert client.stop_checks == [False] and not win.job_view._still_paused.get_visible()


def test_checking_connection_shows_its_pill_and_note(ui, make, run, svg):
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.set_status("1")
        client.no_reply_s, client.indication_age_s = 9.4, 1.2
        await win.monitor.refresh()
        assert _pill(win) == (machine.CHECKING, "warning", False)
        assert win.status_label.get_text() == "Status: Checking connection · machine: Busy"
        assert view._checking.text == (machine.CHECKING_NOTE.format(n=9)
                                       + machine.STILL_REPORTS.format(term="Busy"))
        client.no_reply_s, client.indication_age_s = 21.0, None
        await win.monitor.refresh()
        assert view._checking.text == machine.CHECKING_NOTE.format(n=21)
        assert win.lookup_action("cancel-job").get_enabled()   # Cancel Job stays available
        client.no_reply_s = None
        await win.monitor.refresh()
        assert not view._checking.get_visible() and _pill(win)[0].startswith("Cutting ")
        client.no_reply_s = 12.0
        await win.monitor.refresh()
        assert view._checking.get_visible()
        await _finish(win, client, job)
        assert not view._checking.get_visible()                # only while the job runs
        assert not win.pill.text.startswith(machine.CHECKING)
    run(scenario())
    assert client.commands == [] and client.stops == 1


def test_left_pause_shows_the_job_ending_with_the_stop(ui, make, run, svg):
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        assert win.job_view.pause_button.get_visible()
        client.set_status("0")
        client.interrupted = LEFT_PAUSE_MARK
        await win.monitor.refresh()
        assert _pill(win) == (ui.jobview.STOPPING, "warning", False)
        assert not win.job_view.pause_button.get_visible()      # the job ends: nothing offered
        assert not win.job_view._commands.get_visible()
        client.pause_ok = True                     # even a service that would still send it
        await win.monitor.refresh()
        assert not win.job_view.pause_button.get_visible() and await win.pause_job() is None
        assert client.commands == []
        await _finish(win, client, job)
    run(scenario())


def test_stop_machine_says_when_the_machine_still_reports_paused(ui, make, run, svg, alerts):
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)

    async def scenario():
        assert await win.run_job(_spec(ui, win)) is Outcome.DONE
        client.set_status("3")
        await win.monitor.refresh()
        reply = await win.stop_machine()
        assert reply["status_after"] == "3"
    run(scenario())
    assert len(alerts) == 1 and alerts[0][0] == "still_paused"
    assert alerts[0][1].startswith("The machine still reports Paused ")
    assert client.stop_checks == [True]


def test_the_window_waits_longer_for_cancels_stop_than_the_client(ui):
    """A paused machine's stop reply comes only after the service's longer read-back: the
    window must not give up on it before the client does."""
    from opencutter.device import client as client_mod
    assert ui.window.STOP_WAIT > client_mod.STOP_TIMEOUT
    assert client_mod.STOP_TIMEOUT > daemon.STOP_LOCK_WAIT + daemon.STOP_CHECK_PAUSED


def test_a_double_click_from_pause_onto_resume_sends_one_op(ui, make, run, svg, monkeypatch):
    """Pause and Resume sit in the same place: the second click of a double click on Pause
    lands on Resume once the machine paused. It sends nothing, and Resume shows that it takes
    no click yet (not a button that looks ready and ignores the click); after the guard time it
    takes one again."""
    monkeypatch.setattr(ui.window, "COMMAND_GUARD_SECONDS", 0.4)
    client = FakeDaemonClient()
    client.pause_ok = True
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.command_gate = asyncio.Event()
        view.pause_button.emit("clicked")
        await _until(lambda: client.commands == ["pause"])
        # while it runs, another click on either button sends nothing
        assert await win.pause_job() is None and await win.resume_job() is None
        client.command_gate.set()
        await _until(lambda: view.resume_button.get_visible())     # the service: paused
        assert not view.resume_button.get_sensitive()
        view.resume_button.emit("clicked")                         # the second click
        await asyncio.sleep(0.1)
        assert client.commands == ["pause"] and _pill(win)[0] == "Paused"
        await _until(lambda: view.resume_button.get_sensitive(), 3)
        assert win.job_view._held == 0
        view.resume_button.emit("clicked")                         # a click of its own
        await _until(lambda: client.commands == ["pause", "resume"])
        await _until(lambda: not view.resume_button.get_visible())
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == ["pause", "resume"]


def test_checking_connection_while_paused_shows_both_notes(ui, make, run, svg):
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        client.no_reply_s, client.indication_age_s = 8.0, 2.5
        await win.monitor.refresh()
        assert _pill(win) == (machine.CHECKING, "warning", False)        # it wins over Paused
        assert win.status_label.get_text() == "Status: Checking connection · machine: Paused"
        assert view._checking.text == (machine.CHECKING_NOTE.format(n=8)
                                       + machine.STILL_REPORTS.format(term="Paused"))
        assert view._paused.text == machine.PAUSED_NOTE and view.resume_button.get_visible()
        assert win.runner.machine_paused
        client.no_reply_s = None                                         # answered again
        await win.monitor.refresh()
        assert _pill(win) == ("Paused", "warning", False) and not view._checking.get_visible()
        assert view._paused.get_visible()
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == []


def test_a_cancel_on_the_machine_while_paused_offers_nothing_more(ui, make, run, svg):
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        await win.monitor.refresh()
        assert view.resume_button.get_visible()
        # a long press of the Pause key: Cancelling, which the session latched
        client.set_status("4")
        client.interrupted = "4"
        client.paused = client.resume_ok = client.pause_ok = False
        await win.monitor.refresh()
        assert _pill(win)[0] == "Cancelling" and not win.runner.machine_paused
        assert not view.resume_button.get_visible() and not view.pause_button.get_visible()
        assert not view._paused.get_visible() and not view._commands.get_visible()
        assert await win.resume_job() is None and await win.pause_job() is None
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == []


def test_a_link_lost_while_paused_offers_nothing(ui, make, run, svg, alerts):
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.pause_machine()
        client.no_reply_s = 9.0
        await win.monitor.refresh()
        client.ping_reply = {**READY_PING, "connected": False, "status": None, "device": None,
                             "reason": None, "model": None}      # the fake still says paused
        await win.monitor.refresh()
        assert _pill(win)[0] == "Connection lost" and not win.runner.machine_paused
        assert not view.resume_button.get_visible() and not view._paused.get_visible()
        assert not view._checking.get_visible()
        assert await win.resume_job() is None
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == []


def test_the_window_asks_nothing_of_a_paused_machine(ui, make, run, svg, monkeypatch):
    """While the job waits on a paused machine the window only pings the service: no info (no
    ESC 15, no FG), no stop, no Resume or Pause, no further step."""
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    cut = _cut(spec)
    asked = []
    info = client.info

    async def counted_info():
        asked.append("info")
        return await info()
    monkeypatch.setattr(client, "info", counted_info)

    async def scenario():
        job = await _start_cut(win, client, spec, cut)
        client.pause_machine()
        pings = client.machine_pings
        for _ in range(5):
            await win.monitor.refresh()
            win.job_view._on_tick()
            await asyncio.sleep(0.05)
        assert client.machine_pings > pings and win.runner.machine_paused
        assert asked == [] and client.stops == 0 and client.commands == []
        assert client.steps_started[-1] == cut
        await _finish(win, client, job)
    run(scenario())


def test_pause_is_never_sent_to_a_machine_shown_paused(ui, make, run, svg):
    """The machine reads Paused before the session said so: Pause is neither shown nor sent,
    even if the service still said that it would send one."""
    client = FakeDaemonClient()
    win = _window(make, run, svg, client)
    spec = _spec(ui, win)
    view = win.job_view

    async def scenario():
        job = await _start_cut(win, client, spec, _cut(spec))
        client.set_status("3")
        client.pause_ok = True
        await win.monitor.refresh()
        assert _pill(win)[0] == "Paused" and not view.pause_button.get_visible()
        assert await win.pause_job() is None
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == []


def test_a_pause_at_the_outline_check_shows_paused_and_resume(ui, make, run, svg):
    client = FakeDaemonClient()
    client.pause_ok = True
    win = _window(make, run, svg, client)
    spec = _spec(ui, win, preview=True)
    cut = _cut(spec)
    client.hold_step = (cut, asyncio.Event())
    view = win.job_view

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec))
        await _until(lambda: win.runner is not None and win.runner.holding)
        client.pause_machine()                     # the Pause key while the app waits for Cut
        await win.monitor.refresh()
        assert _pill(win) == ("Paused", "warning", False)
        assert view._paused.text == machine.PAUSED_NOTE
        assert view.resume_button.get_visible() and not view.pause_button.get_visible()
        assert _row(win, cut).cut_button.get_sensitive()       # Cut still waits for the user
        client.set_status("0")                                 # it went on: nothing was left
        client.paused = client.resume_ok = False
        await win.monitor.refresh()
        assert _pill(win)[0] == "Check the position" and not view.resume_button.get_visible()
        await _finish(win, client, job)
    run(scenario())
    assert client.commands == [] and cut not in client.steps_started


# --- with the real service, on a simulated machine --------------------------------------------
def _square_spec(tmp_path) -> dict:
    path = tmp_path / "square.svg"
    path.write_text("""<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm"
      viewBox="0 0 100 100" fill="none" stroke="#000"><rect x="20" y="30" width="30"
      height="20"/></svg>""")
    letter = geometry.media_rect("letter", False, (0, 0))
    doc = PlacedDocument(load_svg(str(path)), letter)
    vinyl = next(p for p in BASIC_PRESETS if p.action == "Cut")
    return sending.job_spec(doc, letter, vinyl.to_tool_settings(), False).to_dict()


async def _cutting(runner, f) -> None:
    await _until(lambda: runner.current is not None and runner.current.tool_down
                 and f.moving(), 20)


def test_the_monitor_and_the_runner_resume_a_paused_job_through_the_service(harness, tmp_path,
                                                                            monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)
    spec = _square_spec(tmp_path)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.8)
        client = h.client()
        monitor = MachineMonitor(client)
        runner = JobRunner(client)
        task = asyncio.ensure_future(runner.run_job(spec))
        await _cutting(runner, f)
        state = await monitor.refresh()
        assert state.pause_ok and state.offers_pause and not state.shows_paused
        f.press_pause()                                         # the Pause key on the machine
        for _ in range(100):
            state = await monitor.refresh()
            if state.paused:
                break
            await asyncio.sleep(0.05)
        assert state.shows_paused and state.offers_resume and not state.pause_ok
        assert paused_note(state) == machine.PAUSED_NOTE
        reply = await runner.resume()
        assert reply["ok"] and reply["sent"] and reply["paused"] is False, reply
        assert await asyncio.wait_for(task, 30) is Outcome.DONE, runner.error
        return f
    f = harness.run(scenario)
    assert f.writes.count(ESC08) == 1 and ESC09 not in f.writes and ESC07 not in f.writes


def test_the_apps_pause_and_a_paused_cancel_through_the_service(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "PAUSED_POLL", 0.3)
    spec = _square_spec(tmp_path)

    async def scenario(h):
        h.fake_cls.accept_any = True
        f = await h.fresh(tools=" 2, 4", move_s=0.8, cancel_quiet_s=0.6)
        client = h.client()
        monitor = MachineMonitor(client)
        runner = JobRunner(client)
        task = asyncio.ensure_future(runner.run_job(spec))
        await _cutting(runner, f)
        paused = await runner.pause()
        assert paused["ok"] and paused["paused"] is True, paused
        state = await monitor.refresh()
        assert state.paused and state.offers_resume
        at_pause = len(f.writes)
        for _ in range(4):                                # the app's polls go on meanwhile
            await monitor.refresh()
            await asyncio.sleep(0.2)
        assert set(f.writes[at_pause:]) <= {ESC05}        # nothing but ESC 05 meanwhile
        reply = await runner.cancel(check=True)
        assert await asyncio.wait_for(task, 10) is Outcome.CANCELLED
        state = await monitor.refresh()
        return f, reply, state
    f, reply, state = harness.run(scenario)
    assert reply["ok"] and reply["status_after"] == "0", reply
    assert f.writes.count(ESC07) == 1 and ESC08 not in f.writes and stops(f.writes) == 1
    assert state.stop_check is not None and state.stop_check.status == "0"
    assert not state.paused


def test_nothing_in_the_app_sends_pause_or_resume_by_itself():
    """Only the runner's resume() and pause() call the service's ops, and only the window's
    resume_job() and pause_job() (each for a click: the job view's Resume and Pause signals)
    call those."""
    from pathlib import Path
    gui = Path(runner_mod.__file__).parent
    for path in sorted(gui.glob("*.py")):
        source = path.read_text()
        assert not re.search(r"client\.(resume|pause)\b", source), path.name
        calls = re.findall(r"runner\.(resume|pause)\(\)", source)
        assert calls == ([] if path.name != "window.py" else ["resume", "pause"]), path.name
        jobs = re.findall(r"\.(resume_job|pause_job)\(\)", source)
        assert jobs == ([] if path.name != "window.py" else ["resume_job", "pause_job"]), (
            path.name)
    window = (gui / "window.py").read_text()
    assert 'self.job_view.connect("resume", lambda *_: spawn(self.resume_job()))' in window
    assert 'self.job_view.connect("pause", lambda *_: spawn(self.pause_job()))' in window
    assert "getattr(self.client, kind, None)" in (gui / "runner.py").read_text()
