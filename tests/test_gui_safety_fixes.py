# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Safety fixes in the app, with the fake daemon client (no socket, no Bluetooth).

- Stopping: Cancel's stop may answer late. The run shows 'Stopping…' until the reply, then
  'Job cancelled' or 'Stop not confirmed'; a failed stop is always shown (a toast that stays,
  the red alert), whenever the reply comes; Send, Test Cut, Open, Cut Again and Done wait for
  it; closing the window waits for it, and keeps the window with the power-button alert when it
  failed. "Cancel Job and quit" asks once.
- Stop Machine: once a run has ended, the finished page (and the header after anything but a
  completed run) sends the service's stop op without a session; insensitive while not
  connected; the reply is shown.
- The next job: after any run that went to the machine, and after the link dropped, the next
  Send, Cut Again, Send Again… or Test Cut first shows the Load sheet, which waits until the
  machine reported the mat unloaded and then Ready. The Load sheet for a busy, paused,
  cancelling or rejecting machine says what to do instead, never to press Pause to continue.
- Media: the Send sheet names the Media Size; the Load sheet says where the sheet goes and that
  Media Size must match it; a design off the media is not called outside the cut area.
- Texts during and after a job: the pill and status bar follow the step (Preparing, Previewing
  outline, Check the position, Cutting N%, Returning to origin), the machine's Paused, a lost
  link (Cut disabled at the gate); 'Connection lost during the job' unless a cut step had
  started; the lost-link dialog follows the run, not only the state after it; Send Again… is
  not Try again; a refusal before any step says Nothing was sent; a stop from another client at
  the gate ends the run there; a stop the service owes the machine is told.
- The canvas parked after a job does not move the design with the arrow keys until clicked.
- The app's own log file in the state folder.
- Esc, sent through the private broadway display: with "A job is running" open it keeps the job
  (the dialog's own Esc), on the main window it cancels the job.

Needs a display: run headless with GDK_BACKEND=broadway and a private gtk4-broadwayd. The
module is skipped when GTK cannot open one; the Esc tests also need broadway's input socket.
"""
import asyncio
import base64
import logging
import os
import socket
import stat
import struct
import sys
import threading
import time

import pytest

import stubs

stubs.install()
import opencutter.gui  # noqa: E402,F401  (selects the GTK and libadwaita versions)
from gi.events import GLibEventLoop  # noqa: E402
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("GTK cannot open a display", allow_module_level=True)
Adw.init()

from opencutter.device import daemon, protocol  # noqa: E402
from opencutter.gui import app as app_mod, geometry, jobview, machine, sending, sheets  # noqa: E402
from opencutter.gui import window as window_mod  # noqa: E402
from opencutter.gui.machine import Link, MachineMonitor, MachineState  # noqa: E402
from opencutter.gui.runner import (LINK_LOST, STOPPED_OUTSIDE, JobRunner, Outcome,  # noqa: E402
                                   StepState, activity)
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.gui.sheets import LoadMediaDialog, SendDialog  # noqa: E402
from opencutter.gui.window import MainWindow  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from gui_fakes import READY_PING, FakeDaemonClient, FakeSession  # noqa: E402

# a 30 x 20 mm rectangle at x 150..180, y 30..50 on a 300 mm page
DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="300mm" height="300mm" viewBox="0 0 300 300"
  fill="none" stroke="#000"><rect x="150" y="30" width="30" height="20"/></svg>"""
DROPPED = {**READY_PING, "connected": False, "status": None, "device": None, "reason": None,
           "model": None}


@pytest.fixture
def run():
    loop = GLibEventLoop(GLib.MainContext.default())
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
def make(tmp_path):
    windows = []

    def factory(client: FakeDaemonClient, present: bool = False, application=None,
                **settings_kw) -> MainWindow:
        settings = GuiSettings.load(tmp_path / "gui.json")
        settings.units, settings.preview_outline = "mm", True
        settings.media, settings.custom_media_mm = "custom", [254.0, 254.0]
        for key, value in settings_kw.items():
            setattr(settings, key, value)
        win = MainWindow(application, client, PresetStore(list(BASIC_PRESETS), basic_source()),
                         settings)
        windows.append(win)
        if present:
            win.present()
        return win
    yield factory
    for win in windows:
        win.destroy()


@pytest.fixture
def toasts(monkeypatch):
    """Every toast of every window: (markup, stays)."""
    shown = []
    monkeypatch.setattr(MainWindow, "toast", lambda self, markup: shown.append((markup, False)))
    monkeypatch.setattr(MainWindow, "_toast_stays", lambda self, markup: shown.append((markup, True)))
    return shown


@pytest.fixture
def alerts(monkeypatch):
    """The blocking alerts (and the connection-lost dialog, answered Close)."""
    shown = []

    async def stop_failed(parent):
        shown.append("stop_failed")

    async def stop_not_sent(parent):
        shown.append("stop_not_sent")

    async def still_busy(parent):
        shown.append("still_busy")

    async def lost(parent):
        shown.append(("connection_lost", parent.connection_lost_texts()))
        return False
    monkeypatch.setattr(window_mod.dialogs, "stop_failed", stop_failed)
    monkeypatch.setattr(window_mod.dialogs, "stop_not_sent", stop_not_sent)
    monkeypatch.setattr(window_mod.dialogs, "still_busy_after_stop", still_busy)
    monkeypatch.setattr(window_mod.dialogs, "connection_lost", lost)
    return shown


async def _until(predicate, timeout: float = 10) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def _spec(win: MainWindow, preview: bool = True) -> dict:
    return window_mod.job_spec(win.doc, win.page_setup.media_rect(), win.send_panel.tool_settings(),
                               preview).to_dict()


def _index(spec: dict, prefix: str) -> int:
    from opencutter.job.spec import JobSpec
    steps = build_plan(JobSpec.from_dict(spec)).steps
    return next(i for i, s in enumerate(steps) if s.title.startswith(prefix))


def _ready_window(make, run, svg, client=None, **kw) -> MainWindow:
    client = client or FakeDaemonClient()
    win = make(client, **kw)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    return win


def _texts(widget: Gtk.Widget) -> list[str]:
    out = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label) and child.get_visible():
            out.append(child.get_text())
        out += _texts(child)
        child = child.get_next_sibling()
    return out


# --- stopping: pending, late failure, Done/Send wait, closing --------------------------------------
def test_cancel_shows_stopping_until_the_reply_and_everything_waits_for_it(make, run, svg, toasts):
    client = FakeDaemonClient()
    client.stop_gate = asyncio.Event()
    win = _ready_window(make, run, svg, client)
    view = win.job_view

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.activate_action("win.cancel-job")
        await _until(lambda: win.runner.outcome is Outcome.CANCELLED)
        await asyncio.sleep(0.1)
        runner = win.runner
        assert runner.stop_pending and view._done_title.get_text() == jobview.STOPPING
        assert not view._done_button.get_sensitive() and not view._again.get_sensitive()
        for name in ("send", "test-cut", "open"):
            assert not win.lookup_action(name).get_enabled(), name
        assert win.send_panel.send_button.get_tooltip_text() == window_mod.WAITING_FOR_STOP
        win._leave_job()                                       # Done: refused while pending
        win._cut_again()
        assert win.side_stack.get_visible_child_name() == "job" and len(client.opened) == 1
        assert not job.done()                                  # run_job waits for the reply
        client.stop_gate.set()
        assert await asyncio.wait_for(job, 5) is Outcome.CANCELLED
    run(scenario())
    assert not win.runner.stop_pending and view._done_title.get_text() == "Job cancelled"
    assert view._done_button.get_sensitive() and win.lookup_action("send").get_enabled()
    assert toasts == [] and client.stops == 1


def test_a_stop_failure_that_comes_late_is_still_shown(make, run, svg, toasts, monkeypatch):
    monkeypatch.setattr(window_mod, "STOP_WAIT", 0.1)          # run_job gives up waiting first
    client = FakeDaemonClient()
    client.stop_gate = asyncio.Event()
    client.stop_reply = {"ok": False, "error": "BleakError: write failed"}
    win = _ready_window(make, run, svg, client)
    view = win.job_view

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.activate_action("win.cancel-job")
        assert await asyncio.wait_for(job, 5) is Outcome.CANCELLED
        assert win.runner.stop_pending and toasts == []        # no reply yet
        client.stop_gate.set()
        await _until(lambda: not win.runner.stop_pending)
    run(scenario())
    assert win.runner.stop_failed
    assert toasts == [(window_mod.STOP_FAILED_TOAST, True)]   # stays until dismissed
    assert view._done_title.get_text() == "Stop not confirmed" and view._error.get_visible()
    assert "power button now" in view._done_hint.get_text()
    assert not view._again.get_visible()                       # no Send Again… now


def test_closing_right_after_cancel_waits_for_the_stop_and_keeps_the_window_if_it_failed(
        make, run, svg, toasts, alerts):
    client = FakeDaemonClient()
    client.stop_gate = asyncio.Event()
    client.stop_reply = {"ok": False, "error": "BleakError: write failed"}
    win = _ready_window(make, run, svg, client, present=True)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.activate_action("win.cancel-job")
        await _until(lambda: win.runner.outcome is Outcome.CANCELLED)
        win.close()                                            # right after Cancel
        await asyncio.sleep(0.2)
        assert win.get_visible() and (window_mod.GLib.markup_escape_text(
            window_mod.WAITING_FOR_STOP), False) in toasts
        client.stop_gate.set()
        await _until(lambda: alerts)
        await asyncio.wait_for(job, 5)
    run(scenario())
    assert alerts == ["stop_failed"] and win.get_visible()
    assert (window_mod.STOP_FAILED_TOAST, True) not in toasts  # shown once, by the alert


def test_closing_right_after_cancel_closes_once_the_stop_got_through(make, run, svg, toasts, alerts):
    client = FakeDaemonClient()
    client.stop_gate = asyncio.Event()
    win = _ready_window(make, run, svg, client, present=True)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.activate_action("win.cancel-job")
        await _until(lambda: win.runner.outcome is Outcome.CANCELLED)
        win.close()
        await asyncio.sleep(0.2)
        assert win.get_visible()
        client.stop_gate.set()
        await _until(lambda: not win.get_visible())
        await asyncio.wait_for(job, 5)
    run(scenario())
    assert alerts == [] and client.stops == 1


class SlowCloseSession(FakeSession):
    async def __aexit__(self, *exc) -> None:
        await asyncio.sleep(0.3)                               # the session ends after the stop reply
        await super().__aexit__(*exc)


def test_cancel_job_and_quit_asks_once_when_the_session_ends_late(make, run, svg, monkeypatch):
    monkeypatch.setattr(window_mod, "QUIT_WAIT", 0.05)          # the session ends after it
    client = FakeDaemonClient()
    client.session = lambda: SlowCloseSession(client)
    asked = []

    async def yes(_parent):
        asked.append(1)
        return True
    monkeypatch.setattr(window_mod.dialogs, "confirm_quit_during_job", yes)
    win = _ready_window(make, run, svg, client, present=True)
    cut = _index(_spec(win), "Cut")
    client.hold_step = (cut, asyncio.Event())

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.runner.confirm_cut()
        await _until(lambda: cut in client.steps_started)
        win.close()
        await _until(lambda: not win.get_visible())
        await asyncio.wait_for(job, 5)
    run(scenario())
    assert asked == [1] and client.stops == 1


# --- Stop Machine ----------------------------------------------------------------------------
def test_stop_machine_after_a_cancelled_run(make, run, svg, toasts, alerts):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    view = win.job_view

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.activate_action("win.cancel-job")
        await asyncio.wait_for(job, 5)
        assert win.header_stop.get_visible() and view.stop_button.get_visible()
        assert view.stop_button.has_css_class("destructive-action")
        assert win.lookup_action("stop-machine").get_enabled()
        assert not win.lookup_action("cancel-job").get_enabled()   # Esc stays Cancel Job's
        win.activate_action("win.stop-machine")
        await _until(lambda: client.stops == 2 and toasts)
        # still Busy a few seconds after the stop: said, with the power button
        client.set_status("1")
        await win.stop_machine()
        # no link: the button stays, insensitive, and says what to do
        client.ping_reply = dict(DROPPED)
        await win.monitor.refresh()
        assert not win.lookup_action("stop-machine").get_enabled()
        assert view.stop_button.get_tooltip_text() == window_mod.STOP_UNAVAILABLE
        assert not view._again.get_visible()                    # no Send Again… without a link
        client.ping_reply = dict(READY_PING)
        await win.monitor.refresh()
        client.stop_reply = {"ok": False, "error": "the machine is not connected yet"}
        await win.stop_machine()
    run(scenario())
    assert client.stop_checks == [False, True, True, True]     # Cancel's plain stop, then checked
    assert toasts[0] == (GLib.markup_escape_text(window_mod.STOP_SENT), False)
    assert alerts == ["still_busy", "stop_not_sent"]
    win.job_view.emit("done")
    assert not win.header_stop.get_visible()


def test_stop_machine_is_on_the_finished_page_of_a_completed_run_too(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    spec = _spec(win, preview=False)
    assert run(win.run_job(spec)) is Outcome.DONE
    view = win.job_view
    assert view.stop_button.get_visible() and not view.stop_button.has_css_class("destructive-action")
    assert win.lookup_action("stop-machine").get_enabled()
    assert not win.header_stop.get_visible() and view._again.get_label() == jobview.CUT_AGAIN


# --- the next job: Unload and Load first -------------------------------------------------------------
@pytest.mark.parametrize("how", ["cancel", "done", "test-cut"])
def test_the_next_job_first_asks_to_unload_and_load_the_mat(make, run, svg, how):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        if how == "cancel":
            job = asyncio.ensure_future(win.run_job(_spec(win)))
            await _until(lambda: win.runner is not None and win.runner.holding)
            win.activate_action("win.cancel-job")
            await job
            assert win.job_view._again.get_label() == jobview.SEND_AGAIN
            win.job_view._again.emit("clicked")                 # Send Again…
        else:
            assert await win.run_job(_spec(win, preview=False), test_cut=how == "test-cut") \
                is Outcome.DONE
            win._leave_job()
            asyncio.ensure_future(win.send_flow(test_cut=how == "test-cut"))
        await _until(lambda: isinstance(win.get_visible_dialog(), LoadMediaDialog))
        sheet = win.get_visible_dialog()
        assert win.monitor.state.ready and not sheet._continue.get_sensitive()
        expected = sheets.RELOAD_AFTER_STOP if how == "cancel" else sheets.RELOAD_AFTER_DONE
        assert sheet._text.get_label().startswith(expected)
        assert sheet._state.get_text() == "Loaded before the last job"
        client.set_status("2")                                  # Unload
        await win.monitor.refresh()
        assert not sheet._continue.get_sensitive() and sheet._state.get_text() == "Waiting for media…"
        client.set_status("0")                                  # Load
        await win.monitor.refresh()
        assert sheet._continue.get_sensitive()
        sheet.close()
        await asyncio.sleep(0.1)
    run(scenario())
    assert len(client.opened) == 1 and not win._reload_needed


def test_an_unload_between_two_polls_counts_and_the_checklist_waits_for_it(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    assert run(win.run_job(_spec(win, preview=False))) is Outcome.DONE
    assert win._reload_needed
    run(win.monitor.refresh())
    assert win._reload_needed
    client.load_seen += 1                                       # Unload and Load between polls
    run(win.monitor.refresh())
    assert not win._reload_needed


def test_an_unload_while_the_stop_reply_is_awaited_counts(make, run, svg):
    client = FakeDaemonClient()
    client.stop_gate = asyncio.Event()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.activate_action("win.cancel-job")
        await _until(lambda: win.runner.outcome is Outcome.CANCELLED)
        assert win._reload_needed                              # from the end of the run
        client.set_status("2")                                 # Unload, before the reply
        await win.monitor.refresh()
        client.set_status("0")
        await win.monitor.refresh()
        client.stop_gate.set()
        await asyncio.wait_for(job, 5)
    run(scenario())
    assert not win._reload_needed


def test_a_dropped_link_needs_a_reload_before_the_next_job(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    assert not win._reload_needed
    client.ping_reply = dict(DROPPED)
    run(win.monitor.refresh())
    assert win._reload_needed
    client.ping_reply = dict(READY_PING)
    run(win.monitor.refresh())
    assert win._reload_needed                                   # back, but the mat not reloaded
    client.set_status("2")
    run(win.monitor.refresh())
    assert not win._reload_needed


@pytest.mark.parametrize("status, heading", [("1", "Machine Busy"), ("3", "Machine Paused"),
                                             ("4", "Machine Cancelling"),
                                             ("5", "Machine Held Elsewhere")])
def test_the_load_sheet_never_tells_a_busy_or_paused_machine_to_load(status, heading):
    sheet = LoadMediaDialog(None, "Letter · 8.5 × 11 in")
    sheet.update(MachineState(link=Link.CONNECTED, status=status))
    text = sheet._text.get_text()
    assert sheet._heading.get_text() == heading and sheet.get_title() == heading
    assert "Load</b> button" not in sheet._text.get_label() and "press the Load" not in text
    assert not sheet._continue.get_sensitive()
    if status in ("3", "4"):
        assert "Do not press Pause to continue." in text and "Unload" in text
    assert "Pause" not in text.replace("Do not press Pause to continue", "")
    sheet.update(MachineState(link=Link.CONNECTED, status="2"))
    assert sheet._heading.get_text() == "Load Cutting Mat"


def test_the_load_sheet_says_where_the_sheet_goes_and_names_the_media_size():
    sheet = LoadMediaDialog(None, "Letter · 8.5 × 11 in")
    text = sheet._text.get_text()
    assert text.startswith("Place the media on the mat, line the mat up")
    assert "top-left corner of the sheet on the top-left corner of the mat's grid" in text
    assert "Media Size in Page Setup must match the sheet (now Letter · 8.5 × 11 in)." in text


# --- media and placement texts -------------------------------------------------------------------
def test_a_design_off_the_media_is_not_called_outside_the_cut_area(make, run, svg):
    win = _ready_window(make, run, svg)
    media = win.page_setup.media_rect()                          # custom 254 x 254 mm
    win.doc.move_to(240, 30)                                    # inside the cut area, past the media
    win._sync_placement()
    assert geometry.placement_problem(win.doc.bbox(), media) == "media"
    assert win.area_banner.get_revealed() and win.area_banner.get_button_label() == "Fit to Media"
    assert "off the media" in win.area_banner.get_title()
    assert win.send_panel.send_button.get_tooltip_text() == sending.OFF_MEDIA
    win.doc.move_to(290, 30)                                    # past the cut area too
    win._sync_placement()
    assert win.area_banner.get_title() == window_mod.OUTSIDE_CUT_AREA
    assert win.area_banner.get_button_label() == "Fit to Mat"
    assert win.send_panel.send_button.get_tooltip_text() == sending.OUTSIDE_CUT_AREA
    win.fit_design()
    assert not win.area_banner.get_revealed()


def test_the_send_sheet_names_the_media_size_and_notes_a_mat_sized_one(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client, media="12x12")

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: isinstance(win.get_visible_dialog(), SendDialog))
        sheet = win.get_visible_dialog()
        texts = _texts(sheet)
        assert "Media" in texts and win.page_setup.media_label() in texts
        assert sheet.notes == [window_mod.MEDIA_IS_MAT.format(media=win.page_setup.media_label())]
        assert sheet._send.get_sensitive()                       # a note blocks nothing
        sheet.close()
        await asyncio.wait_for(flow, 5)
    run(scenario())


# --- texts during the job -------------------------------------------------------------------------
def test_the_pill_and_status_bar_follow_the_step(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    spec = _spec(win)
    header, cut, trailer = _index(spec, "Header"), _index(spec, "Cut"), _index(spec, "Return")
    seen = {}

    async def scenario():
        for index in (header, cut, trailer):
            client.hold_step = (index, asyncio.Event())
            if index == header:
                job = asyncio.ensure_future(win.run_job(spec))
            await _until(lambda: index in client.steps_started)
            await asyncio.sleep(0.05)
            win._update_pill()
            seen[index] = (win.pill.text, win.status_label.get_text())
            if index == header:
                client.hold_step[1].set()
                await _until(lambda: win.runner.holding)
                win._update_pill()
                seen["gate"] = (win.pill.text, win.status_label.get_text())
                client.hold_step = None
                win.runner.confirm_cut()
            else:
                client.hold_step[1].set()
        assert await asyncio.wait_for(job, 5) is Outcome.DONE
    run(scenario())
    assert seen[header] == ("Preparing", "Status: Preparing · machine: Ready")
    assert seen["gate"] == ("Check the position", "Status: Waiting for Cut (outline check) · machine: Ready")
    assert seen[cut][0].startswith("Cutting ") and seen[cut][0].endswith("%")
    assert seen[trailer][0] == "Returning to origin"


def test_a_paused_machine_shows_paused_not_a_percentage(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    spec = _spec(win, preview=False)
    cut = _index(spec, "Cut")
    release = asyncio.Event()
    client.hold_step = (cut, release)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec))
        await _until(lambda: cut in client.steps_started)
        client.set_status("3")
        await win.monitor.refresh()
        assert (win.pill.text == "Paused"
                and win.status_label.get_text() == "Status: Paused on the machine · machine: Paused")
        client.set_status("0")
        client.interrupted = "4"                               # what the session saw
        await win.monitor.refresh()
        assert win.pill.text == "Cancelling"
        release.set()
        await asyncio.wait_for(job, 5)
    run(scenario())


def test_the_app_reads_the_services_texts_and_keys():
    assert LINK_LOST == daemon.LINK_LOST
    assert machine.PING_STOP_KEYS == daemon.PING_STOP_KEYS


def test_activity_names_the_steps_without_titles_from_the_service():
    runner = JobRunner(FakeDaemonClient())
    from opencutter.gui.runner import RunStep
    runner.steps = [RunStep("a", kind="write", tool_down=False, state=StepState.ACTIVE),
                    RunStep("b", kind="write", tool_down=True)]
    assert activity(runner, "Cutting") == ("Preparing", False)
    runner.steps[0].state, runner.steps[1].state = StepState.DONE, StepState.ACTIVE
    assert activity(runner, "Cutting") == ("Cutting", True)


def test_a_link_lost_at_the_gate_disables_cut_and_cancel_says_nothing_was_moving(
        make, run, svg, alerts, toasts):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        client.ping_reply = dict(DROPPED)
        client.stop_reply = {"ok": False, "error": daemon.NOT_CONNECTED}
        await win.monitor.refresh()
        gate = next(row for row, step in zip(win.job_view._rows, win.runner.steps) if step.confirm)
        assert not gate.cut_button.get_sensitive() and gate._gate_note.get_visible()
        assert win.pill.text == "Connection lost" and win.job_view._lost.get_visible()
        assert not win.banner.get_revealed()                    # not during the job
        win.activate_action("win.cancel-job")
        assert await asyncio.wait_for(job, 5) is Outcome.CANCELLED
    run(scenario())
    runner, view = win.runner, win.job_view
    assert runner.link_lost and not runner.stop_failed and runner.cancelled_at_gate
    assert view._done_title.get_text() == "Job cancelled" and not view._error.get_visible()
    assert "waited at the outline check" in view._done_hint.get_text()
    assert toasts == []                                         # not a failed stop
    assert alerts == [("connection_lost", (window_mod.JOB_LOST_HEADING, window_mod.LOST_BODY, True))]
    assert win.banner.get_revealed()                            # re-evaluated after the job
    assert not view._again.get_visible()


def test_a_fast_reconnect_still_gets_the_dialog_and_a_cut_step_says_while_cutting(
        make, run, svg, alerts):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    spec = _spec(win, preview=False)
    cut = _index(spec, "Cut")
    release = asyncio.Event()
    client.hold_step = (cut, release)
    client.fail_at, client.fail = cut, LINK_LOST

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec))
        await _until(lambda: cut in client.steps_started)
        client.ping_reply = dict(DROPPED)
        await win.monitor.refresh()
        client.ping_reply = dict(READY_PING)                    # back before the step ends
        await win.monitor.refresh()
        release.set()
        assert await asyncio.wait_for(job, 5) is Outcome.FAILED
    run(scenario())
    assert alerts == [("connection_lost",
                       (window_mod.CUT_LOST_HEADING, window_mod.LOST_BACK_BODY, False))]


def test_a_refusal_before_any_step_says_nothing_was_sent(make, run, svg, alerts):
    client = FakeDaemonClient()
    client.open_reply = {"ok": False, "error": protocol.not_ready_text("3")}
    win = _ready_window(make, run, svg, client)
    assert run(win.run_job(_spec(win))) is Outcome.FAILED
    view = win.job_view
    assert win.runner.refused_before_start and view._done_title.get_text() == "Nothing was sent"
    assert jobview.POWER_HINT not in view._done_hint.get_text()
    assert not win._reload_needed and alerts == []


def test_a_stop_from_another_client_at_the_gate_ends_the_run_there(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        client.motion_stopped = True                           # `opencutter stop`
        await win.monitor.refresh()
        assert await asyncio.wait_for(job, 5) is Outcome.CANCELLED
    run(scenario())
    assert win.runner.error == STOPPED_OUTSIDE and client.stops == 0
    assert win.job_view._done_title.get_text() == "Job cancelled"


def test_a_stop_owed_to_the_machine_is_told_and_so_is_its_sending(make, run, svg, toasts):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    assert run(win.run_job(_spec(win, preview=False))) is Outcome.DONE
    client.ping_reply = dict(DROPPED)
    client.stop_owed = True
    run(win.monitor.refresh())
    assert client.stop_pings >= 1 and win.monitor.state.stop_owed
    assert toasts[-1] == (GLib.markup_escape_text(window_mod.OWED_STOP.format(name="CAMEO 4")), True)
    assert win.job_view._owed.get_visible()
    client.ping_reply = dict(READY_PING)
    client.stop_owed, client.owed_stops_sent = False, 1         # sent on the new link
    run(win.monitor.refresh())
    assert toasts[-1] == (GLib.markup_escape_text(window_mod.OWED_STOP_SENT.format(name="CAMEO 4")),
                          False)
    assert not win.job_view._owed.get_visible()


def test_a_dropped_link_says_so_and_a_held_one_after_it_gets_the_softer_text():
    async def scenario():
        client = FakeDaemonClient()
        monitor = MachineMonitor(client, clock=lambda: 0.0)
        await monitor.refresh()
        client.ping_reply = dict(DROPPED)
        await monitor.refresh()
        assert monitor.state.link is Link.FAILED
        assert monitor.state.problem == machine.link_dropped()
        client.ping_reply = {**DROPPED, "reason": protocol.REASON_HELD_ELSEWHERE}
        await monitor.refresh()
        assert monitor.state.problem == machine.held_after_drop()
        assert "Pause" not in monitor.state.problem
        client.ping_reply = dict(READY_PING)
        await monitor.refresh()
        client.ping_reply = {**DROPPED, "reason": protocol.REASON_UNREACHABLE}
        await monitor.refresh()
        assert monitor.state.problem == machine.SWITCH_OFF_ON
    asyncio.run(scenario())


def test_the_finished_pages_give_reload_advice_and_never_mention_pause(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    view = win.job_view
    client.fail_at = 2
    assert run(win.run_job(_spec(win, preview=False))) is Outcome.FAILED
    hint = view._done_hint.get_text()
    assert "check that the tool is up, press Unload, then load the mat" in hint
    assert view._again.get_label() == jobview.SEND_AGAIN and view._again.get_visible()
    client.fail_at = None
    run(win.monitor.refresh())
    client.set_status("2")
    run(win.monitor.refresh())
    client.set_status("0")
    run(win.monitor.refresh())
    win._leave_job()
    assert run(win.run_job(_spec(win, preview=False))) is Outcome.DONE
    assert "Unload" in view._done_hint.get_text() and "Load the mat again" in view._done_hint.get_text()
    for text in (jobview.DONE_HINT, jobview.RELOAD_AFTER_STOP, jobview.RELOAD_AFTER_STOP_FAILED,
                 jobview.GATE_LINK_LOST, jobview.STOP_OWED, jobview.STILL_BUSY):
        assert "Pause" not in text


def test_still_busy_after_the_run_is_noted_on_the_finished_page(make, run, svg, monkeypatch):
    monkeypatch.setattr(window_mod, "STILL_BUSY_SECONDS", 0.2)
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.activate_action("win.cancel-job")
        await job
        client.set_status("1")
        await win.monitor.refresh()
        assert not win.job_view._still_busy.get_visible()
        await asyncio.sleep(0.4)
        assert win.job_view._still_busy.get_visible()
        client.set_status("0")
        await win.monitor.refresh()
        assert not win.job_view._still_busy.get_visible()
    run(scenario())


# --- the canvas after a job ---------------------------------------------------------------------
class _Gesture:
    def get_current_button(self) -> int:
        return 1


def test_the_parked_canvas_moves_nothing_until_it_is_clicked(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client, present=True)
    run(asyncio.sleep(0.3))
    assert run(win.run_job(_spec(win, preview=False), test_cut=False)) is Outcome.DONE
    win.job_view.emit("done")
    canvas = win.canvas
    assert win.get_focus() is canvas and not canvas.armed
    before = win.doc.bbox()
    assert canvas._on_key(None, Gdk.KEY_Right, 0, 0) is True   # taken, nothing moves
    assert win.doc.bbox() == before
    canvas._on_drag_begin(_Gesture(), 1.0, 1.0)                 # a click on the canvas
    canvas._on_drag_end()
    assert canvas.armed and canvas._on_key(None, Gdk.KEY_Right, 0, 0) is True
    assert win.doc.bbox()[0] > before[0]


# --- the app's log ------------------------------------------------------------------------------
@pytest.fixture
def app_log(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    logger = logging.getLogger("opencutter")
    saved = (list(logger.handlers), logger.level, sys.excepthook, threading.excepthook)
    yield tmp_path / "state" / "opencutter" / app_mod.APP_LOG
    for handler in logger.handlers:
        if handler not in saved[0]:
            handler.close()
    logger.handlers[:] = saved[0]
    logger.setLevel(saved[1])
    sys.excepthook, threading.excepthook = saved[2], saved[3]


def test_the_app_writes_its_own_log_in_the_state_folder(app_log):
    handler = app_mod.setup_logging()
    assert handler is not None and app_mod.app_log_path() == str(app_log)
    logging.getLogger("opencutter.gui.runner").info("a step of the job")
    try:
        raise ValueError("a view bug")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    thread = threading.Thread(target=lambda: 1 / 0, name="worker")
    thread.start()
    thread.join()
    handler.flush()
    text = app_log.read_text()
    assert "a step of the job" in text and "ValueError: a view bug" in text
    assert "ZeroDivisionError" in text and "worker" in text
    assert stat.S_IMODE(os.stat(app_log).st_mode) == 0o600


def test_the_app_log_is_rotated_and_never_follows_a_link(app_log, tmp_path):
    app_log.parent.mkdir(parents=True)
    app_log.write_text("x" * (app_mod.LOG_ROTATE_BYTES + 1))
    handler = app_mod.setup_logging()
    assert handler is not None
    assert (app_log.parent / "app.log.1").stat().st_size > app_mod.LOG_ROTATE_BYTES
    assert app_log.stat().st_size < 1000
    logger = logging.getLogger("opencutter")
    logger.removeHandler(handler)
    handler.close()
    app_log.unlink()
    target = tmp_path / "elsewhere.txt"
    target.write_text("")
    app_log.symlink_to(target)
    assert app_mod.setup_logging() is None                       # refused: the app runs on
    assert target.read_text() == ""


# --- Esc, through the private broadway display ---------------------------------------------------
class Broadway:
    """Key presses through the private gtk4-broadwayd's input socket, as its web client sends
    them: a websocket at /socket (protocol "broadway"); an input event is big-endian int32s
    (the event type, serial, time, then its fields: keysym and modifier state for a key). Like
    the web client, it answers the display's roundtrips (op 16 -> event 14), which GTK's frame
    clock waits for: without the answers, dialogs never finish opening."""

    def __init__(self):
        display = os.environ.get("BROADWAY_DISPLAY", "")
        if os.environ.get("GDK_BACKEND") != "broadway" or not display.lstrip(":").isdigit():
            pytest.skip("needs the private broadway display")
        port = 8080 + int(display.lstrip(":"))
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /socket HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           "Sec-WebSocket-Protocol: broadway\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(4096)
        assert head.startswith(b"HTTP/1.1 101"), head[:80]
        self._buf = head.split(b"\r\n\r\n", 1)[1]
        self.sock.settimeout(None)
        self._lock = threading.Lock()
        self.serial = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read(self) -> None:
        try:
            while True:
                b0, b1 = self._take(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._take(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._take(8))[0]
                if b1 & 0x80:
                    self._take(4)
                payload = self._take(n)
                if b0 & 0x0F == 2:
                    self._commands(payload)
                elif b0 & 0x0F == 8:
                    return
        except (OSError, EOFError, struct.error):
            return

    # the display's ops: (op -> bytes after its op and serial), as the web client reads them
    _SIZES = {0: 3, 1: 0, 2: 10, 3: 2, 4: 2, 5: 2, 6: 2, 7: 2, 9: 4, 10: 0, 12: 2, 14: 4}

    def _commands(self, data: bytes) -> None:
        pos = 0
        while pos < len(data):
            op = data[pos]
            self.serial = struct.unpack_from("<I", data, pos + 1)[0]
            pos += 5
            if op in self._SIZES:
                pos += self._SIZES[op]
            elif op == 8:                                        # move/resize
                flags = data[pos + 2]
                pos += 3 + (4 if flags & 1 else 0) + (4 if flags & 2 else 0)
            elif op == 13:                                       # upload texture
                pos += 8 + struct.unpack_from("<I", data, pos + 4)[0]
            elif op == 15:                                       # set nodes
                pos += 6 + 4 * struct.unpack_from("<I", data, pos + 2)[0]
            elif op == 16:                                       # roundtrip: answer it
                surface, tag = struct.unpack_from("<HI", data, pos)
                pos += 6
                self._send(struct.pack(">3iiI", 14, self.serial, self._now(), surface, tag))
            else:
                return                                           # not known: skip the rest

    @staticmethod
    def _now() -> int:
        return int(time.monotonic() * 1000) % (1 << 31)

    def _send(self, payload: bytes) -> None:
        mask = os.urandom(4)
        with self._lock:
            self.sock.sendall(bytes([0x82, 0x80 | len(payload)]) + mask
                              + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def key(self, keysym: int) -> None:
        for kind in (7, 8):                                     # press, release
            self._send(struct.pack(">5i", kind, self.serial, self._now(), keysym, 0))

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def broadway():
    bw = Broadway()
    yield bw
    bw.close()


def _application():
    """An application with the app's accelerators (when it can register here), so that the
    test also sees what an application accelerator would do."""
    application = Adw.Application(application_id="io.github.mortyniners.OpenCutter.EscTest",
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
    try:
        if not application.register(None):
            return None
    except GLib.Error:
        return None
    for action, accels in app_mod.ACCELS.items():
        application.set_accels_for_action(action, accels)
    return application


def test_escape_is_not_an_application_accelerator():
    assert not [a for a, accels in app_mod.ACCELS.items() if "Escape" in accels]
    shortcuts = [i.get_property("accelerator") for i in window_mod._shortcuts_section()]
    assert "Escape" in shortcuts                                # still listed for Cancel Job


def test_escape_with_the_job_running_dialog_open_keeps_the_job(make, run, svg, broadway):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client, present=True, application=_application())
    spec = _spec(win, preview=False)
    cut = _index(spec, "Cut")
    release = asyncio.Event()
    client.hold_step = (cut, release)

    async def scenario():
        await asyncio.sleep(0.5)
        job = asyncio.ensure_future(win.run_job(spec))
        await _until(lambda: cut in client.steps_started)
        win.close()                                             # "A job is running"
        await _until(lambda: isinstance(win.get_visible_dialog(), Adw.AlertDialog))
        dialog = win.get_visible_dialog()
        assert dialog.get_heading() == "A job is running"
        await asyncio.sleep(0.3)
        broadway.key(Gdk.KEY_Escape)
        await _until(lambda: win.get_visible_dialog() is None, timeout=5)
        await asyncio.sleep(0.5)
        assert win._job_running() and client.stops == 0 and win.get_visible()
        broadway.key(Gdk.KEY_Escape)                            # on the main window: Cancel Job
        assert await asyncio.wait_for(job, 5) is Outcome.CANCELLED
    run(scenario())
    assert client.stops == 1


# --- review: every kind of ended run ------------------------------------------------------------
class BrokenSession(FakeSession):
    """A session whose socket breaks at the step `client.fail_at` (the run ends LOST)."""

    async def step(self, index, timeout=400):
        if index == self.client.fail_at:
            self.client.steps_started.append(index)
            hold = self.client.hold_step
            if hold is not None and index == hold[0]:
                await hold[1].wait()
            raise ConnectionResetError("socket closed")
        return await super().step(index, timeout)


async def _end_run(win: MainWindow, client: FakeDaemonClient, kind: str) -> Outcome:
    """Run a job that ends as `kind`: done, test (a completed Test Cut), cancel-gate,
    cancel-cut, failed, failed-stop (the service's own stop failed), lost, refused (before any
    step), outside (`opencutter stop` at the gate)."""
    spec = _spec(win)
    cut = _index(spec, "Cut")
    if kind in ("done", "test"):
        return await win.run_job(_spec(win, preview=False), test_cut=kind == "test")
    if kind == "refused":
        client.open_reply = {"ok": False, "error": "refused: the machine is busy"}
        try:
            return await win.run_job(spec)
        finally:
            client.open_reply = None
    if kind == "lost":
        client.session = lambda: BrokenSession(client)
    if kind in ("failed", "failed-stop", "lost"):
        client.fail_at = cut
    if kind == "failed-stop":
        client.stop_errors = ["1b 09: BleakError: write failed"]
    if kind == "cancel-cut":
        client.hold_step = (cut, asyncio.Event())
    job = asyncio.ensure_future(win.run_job(spec))
    try:
        await _until(lambda: win.runner is not None and win.runner.holding)
        if kind == "cancel-gate":
            win.activate_action("win.cancel-job")
        elif kind == "outside":
            client.motion_stopped = True
            await win.monitor.refresh()
        else:
            win.runner.confirm_cut()
            if kind == "cancel-cut":
                await _until(lambda: cut in client.steps_started)
                win.activate_action("win.cancel-job")
        return await asyncio.wait_for(job, 5)
    finally:
        client.fail_at, client.stop_errors, client.hold_step = None, [], None
        client.motion_stopped = False
        client.session = lambda: FakeSession(client)


ENDED = ["done", "test", "cancel-gate", "cancel-cut", "failed", "failed-stop", "lost", "refused",
         "outside"]


@pytest.mark.parametrize("kind", ENDED)
def test_stop_machine_is_on_screen_after_every_kind_of_ended_run(make, run, svg, alerts,
                                                                  monkeypatch, kind):
    with_stop = []
    monkeypatch.setattr(MainWindow, "_toast_with_stop", lambda self, markup: with_stop.append(markup))
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        assert await _end_run(win, client, kind) is not Outcome.RUNNING
        on_page = (win.side_stack.get_visible_child_name() == "job"
                   and win.job_view.stop_button.get_visible())
        assert on_page or win.header_stop.get_visible() or with_stop, "no Stop Machine on screen"
        assert win.lookup_action("stop-machine").get_enabled()
        stops = client.stops
        win.activate_action("win.stop-machine")
        await _until(lambda: client.stops == stops + 1)
    run(scenario())
    assert client.stop_checks[-1] is True                       # the service's checked stop
    if kind == "test":                                          # the panels came back at once
        assert win.side_stack.get_visible_child_name() == "panels" and len(with_stop) == 1


@pytest.mark.parametrize("kind", ENDED)
def test_stop_machine_never_sends_without_a_link(make, run, svg, alerts, kind):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        await _end_run(win, client, kind)
        client.ping_reply = dict(DROPPED)
        await win.monitor.refresh()
        stops = client.stops
        assert not win.lookup_action("stop-machine").get_enabled()
        win.activate_action("win.stop-machine")
        await asyncio.sleep(0.2)
        assert client.stops == stops
    run(scenario())


def test_the_completed_test_cut_toast_carries_stop_machine(make, run, svg):
    win = _ready_window(make, run, svg)
    win._toast_with_stop("<b>Test Complete.</b>")               # builds the real toast


def test_stop_machine_is_off_while_a_job_runs(make, run, svg, alerts):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        await _end_run(win, client, "cancel-gate")              # Stop Machine was on before
        assert win.lookup_action("stop-machine").get_enabled()
        stops = client.stops
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner.holding)
        assert not win.lookup_action("stop-machine").get_enabled()
        assert not win.header_stop.get_visible()
        assert await win.stop_machine() is None and client.stops == stops
        win.runner.confirm_cut()
        await asyncio.wait_for(job, 5)
    run(scenario())


@pytest.mark.parametrize("kind", ["done", "test", "cancel-cut", "failed", "lost", "outside"])
@pytest.mark.parametrize("how", ["again", "send", "test-cut"])
def test_the_load_sheet_comes_before_every_next_job(make, run, svg, alerts, kind, how):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        await _end_run(win, client, kind)
        await win.monitor.refresh()
        opened = len(client.opened)
        if how == "again":
            again = win.job_view._again
            if not (win.side_stack.get_visible_child_name() == "job" and again.get_visible()):
                return                                           # no Cut Again / Send Again… here
            again.emit("clicked")
        else:
            win.activate_action(f"win.{how}")
        await _until(lambda: win.get_visible_dialog() is not None or len(client.opened) > opened)
        sheet = win.get_visible_dialog()
        assert len(client.opened) == opened and isinstance(sheet, LoadMediaDialog)
        assert win.monitor.state.ready and not sheet._continue.get_sensitive()
        sheet.close()
        await asyncio.sleep(0.1)
    run(scenario())


@pytest.mark.parametrize("how", ["failed-stop", "lost"])
def test_cancel_job_and_quit_keeps_the_window_when_the_run_broke_meanwhile(
        make, run, svg, monkeypatch, toasts, alerts, how):
    """While "A job is running" is open, the step fails and the service's own stop with it, or
    the session breaks (no stop at all); "Cancel Job and quit" then finds the run ended: the
    window stays, with the alert."""
    client = FakeDaemonClient()
    if how == "lost":
        client.session = lambda: BrokenSession(client)
    win = _ready_window(make, run, svg, client, present=True)
    spec = _spec(win)
    cut = _index(spec, "Cut")
    answer = asyncio.Event()

    async def quit_when_told(_parent):
        await answer.wait()
        return True
    monkeypatch.setattr(window_mod.dialogs, "confirm_quit_during_job", quit_when_told)
    client.hold_step = (cut, asyncio.Event())
    client.fail_at, client.stop_errors = cut, ["1b 09: BleakError: write failed"]

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.runner.confirm_cut()
        await _until(lambda: cut in client.steps_started)
        win.close()                                             # "A job is running"
        await asyncio.sleep(0.1)
        client.hold_step[1].set()
        assert await asyncio.wait_for(job, 5) in (Outcome.FAILED, Outcome.LOST)
        answer.set()                                            # Cancel Job and quit
        await _until(lambda: "stop_failed" in alerts)
    run(scenario())
    assert win.get_visible() and "stop_failed" in alerts
    assert win.runner.stop_failed or win.runner.outcome is Outcome.LOST


def test_the_load_sheet_after_a_dropped_link_does_not_speak_of_a_last_job(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    client.ping_reply = dict(DROPPED)
    run(win.monitor.refresh())
    client.ping_reply = dict(READY_PING)
    run(win.monitor.refresh())

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: isinstance(win.get_visible_dialog(), LoadMediaDialog))
        sheet = win.get_visible_dialog()
        assert sheet._text.get_label().startswith(sheets.RELOAD_AFTER_DROP)
        assert sheet._state.get_text() == "Loaded before the connection dropped"
        assert not sheet._continue.get_sensitive()
        sheet.close()
        await asyncio.wait_for(flow, 5)
    run(scenario())


def test_a_link_drop_while_the_send_sheet_is_open_blocks_its_send(make, run, svg):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: isinstance(win.get_visible_dialog(), SendDialog))
        sheet = win.get_visible_dialog()
        assert sheet._send.get_sensitive()
        client.ping_reply = dict(DROPPED)
        await win.monitor.refresh()
        client.ping_reply = dict(READY_PING)                    # back, Ready, mat not reloaded
        await win.monitor.refresh()
        assert win.monitor.state.ready and not sheet._send.get_sensitive()
        sheet.close()
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert client.opened == []


def test_a_link_lost_before_any_cut_step_says_during_the_job(make, run, svg, alerts):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    spec = _spec(win)
    client.fail_at, client.fail = _index(spec, "Header"), LINK_LOST
    client.ping_reply = dict(DROPPED)                           # still down after the run
    assert run(win.run_job(spec)) is Outcome.FAILED
    assert alerts == [("connection_lost", (window_mod.JOB_LOST_HEADING, window_mod.LOST_BODY, True))]


def test_a_refusal_without_a_link_gets_no_connection_lost_dialog(make, run, svg, alerts):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)
    client.open_reply = {"ok": False, "error": daemon.NOT_CONNECTED}
    client.ping_reply = dict(DROPPED)
    assert run(win.run_job(_spec(win))) is Outcome.FAILED
    assert win.runner.refused_before_start and alerts == []
    assert win.job_view._done_title.get_text() == jobview.NOTHING_SENT


def test_run_job_waits_while_the_last_stop_is_pending(make, run, svg):
    client = FakeDaemonClient()
    client.stop_gate = asyncio.Event()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win.activate_action("win.cancel-job")
        await _until(lambda: win.runner.outcome is Outcome.CANCELLED)
        runner = win.runner
        # refused at once: the stop is pending
        assert await asyncio.wait_for(win.run_job(_spec(win)), 2) is Outcome.FAILED
        assert win.runner is runner and len(client.opened) == 1
        client.stop_gate.set()
        await asyncio.wait_for(job, 5)
    run(scenario())


def test_two_cancels_share_one_stop_and_its_reply(make, run, svg, toasts):
    client = FakeDaemonClient()
    client.stop_gate = asyncio.Event()
    client.stop_reply = {"ok": False, "error": "BleakError: write failed"}
    win = _ready_window(make, run, svg, client)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        runner = win.runner
        first = asyncio.ensure_future(runner.cancel())
        second = asyncio.ensure_future(runner.cancel())
        await asyncio.sleep(0.05)
        client.stop_gate.set()
        replies = await asyncio.gather(first, second)
        await asyncio.wait_for(job, 5)
        return replies
    first, second = run(scenario())
    assert client.stops == 1 and first == second and not first["ok"]
    assert win.runner.stop_failed and win.runner.outcome is Outcome.CANCELLED
    assert toasts == [(window_mod.STOP_FAILED_TOAST, True)]     # once


def test_texts_of_the_finished_pages_are_plain_and_complete(make, run, svg, alerts):
    assert "`" not in STOPPED_OUTSIDE
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(_spec(win)))
        await _until(lambda: win.runner is not None and win.runner.holding)
        client.ping_reply = dict(DROPPED)
        client.stop_reply = {"ok": False, "error": daemon.NOT_CONNECTED}
        await win.monitor.refresh()
        win.activate_action("win.cancel-job")
        await asyncio.wait_for(job, 5)
    run(scenario())
    hint = win.job_view._done_hint.get_text()
    assert "waited at the outline check" in hint
    assert "check that the tool is up, press Unload, then load the mat" in hint
    assert "Pause" not in hint


def test_the_parked_canvas_takes_the_arrow_keys_without_a_design_too(make, run):
    win = make(FakeDaemonClient())
    assert win.doc is None
    win.canvas.armed = False
    assert win.canvas._on_key(None, Gdk.KEY_Down, 0, 0) is True    # the focus stays put
    win.canvas.armed = True
    assert win.canvas._on_key(None, Gdk.KEY_Down, 0, 0) is False


@pytest.mark.parametrize("which", ["connection", "menu"])
def test_escape_in_an_open_popover_keeps_the_job(make, run, svg, broadway, which):
    client = FakeDaemonClient()
    win = _ready_window(make, run, svg, client, present=True, application=_application())
    spec = _spec(win, preview=False)
    cut = _index(spec, "Cut")
    release = asyncio.Event()
    client.hold_step = (cut, release)

    def menu_button(widget):
        if isinstance(widget, Gtk.MenuButton) and widget.get_icon_name() == "open-menu-symbolic":
            return widget
        child = widget.get_first_child()
        while child is not None:
            if (found := menu_button(child)) is not None:
                return found
            child = child.get_next_sibling()
        return None

    async def scenario():
        await asyncio.sleep(0.5)
        job = asyncio.ensure_future(win.run_job(spec))
        await _until(lambda: cut in client.steps_started)
        if which == "connection":
            popover = win.connection
            popover.popup()
        else:
            button = menu_button(win)
            button.popup()
            popover = button.get_popover()
        await _until(lambda: popover.get_mapped(), timeout=5)
        await asyncio.sleep(0.4)
        broadway.key(Gdk.KEY_Escape)
        await _until(lambda: not popover.get_mapped(), timeout=5)
        await asyncio.sleep(0.5)
        assert win._job_running() and client.stops == 0
        release.set()
        assert await asyncio.wait_for(job, 5) is Outcome.DONE
    run(scenario())
