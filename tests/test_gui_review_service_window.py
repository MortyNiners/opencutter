# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The main window and the background service, with the fake daemon client (no socket at all):
Disconnect in the connection popover, the 'Updating…' line while a service of another version
is replaced, the 'another version' banner and Send blocked while such a service runs a job;
no logout or suspend while a job runs; and closing the window or quitting the app ends a
flatpak-spawn helper that still waits for a starting service.

Needs a display: run headless with GDK_BACKEND=broadway and a private gtk4-broadwayd. The
module is skipped when GTK cannot open one.
"""
import asyncio

import pytest

import stubs

stubs.install()
import opencutter.gui  # noqa: E402,F401  (selects the GTK and libadwaita versions)
from gi.events import GLibEventLoop  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("GTK cannot open a display", allow_module_level=True)
Adw.init()

from opencutter.gui import app as app_mod, window as window_mod  # noqa: E402
from opencutter.gui.connection import (DISCONNECT_BUSY, RELEASE_HINT,  # noqa: E402
                                       failure_text)
from opencutter.gui.machine import (ANOTHER_VERSION, DISCONNECTING,  # noqa: E402
                                    NOT_DISCONNECTED_BUSY, OTHER_VERSION_BUSY, RELEASED,
                                    UPDATING_SERVICE, Link)
from opencutter.gui.runner import Outcome  # noqa: E402
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.gui.window import MainWindow  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from gui_fakes import OTHER_BUILD, READY_PING, FakeDaemonClient  # noqa: E402

DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="8.5in" height="11in" viewBox="0 0 215.9 279.4"
  fill="none" stroke="#000"><rect x="30" y="40" width="50" height="30"/></svg>"""


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

    def factory(client: FakeDaemonClient) -> MainWindow:
        settings = GuiSettings.load(tmp_path / "gui.json")
        win = MainWindow(None, client, PresetStore(list(BASIC_PRESETS), basic_source()), settings)
        windows.append(win)
        return win
    yield factory
    for win in windows:
        win.destroy()


async def _until(predicate, timeout: float = 10) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def test_disconnect_releases_the_machine_and_connect_starts_it_again(make, run):
    client = FakeDaemonClient()
    client.spawn_ping = dict(READY_PING)
    win = make(client)
    popover = win.connection
    run(win.monitor.connect())
    assert popover._disconnect.get_visible() and popover._disconnect.get_sensitive()
    assert not popover._connect.get_visible()
    assert popover._hint.get_visible() and popover._hint.get_text() == RELEASE_HINT

    async def disconnect():
        popover._disconnect.emit("clicked")
        await _until(lambda: win.monitor.state.link is Link.NO_DAEMON)
    run(disconnect())
    assert client.shutdowns == 1 and client.stops == 0 and client.opened == []
    assert win.pill.text == "Not Connected" and not win.banner.get_revealed()
    assert not popover._disconnect.get_visible() and popover._connect.get_visible()
    assert popover._connect.get_label() == "Connect"
    assert popover._hint.get_visible() and popover._hint.get_text() == RELEASED
    assert not popover._help.get_visible()          # no pairing tips after a deliberate release
    for _ in range(3):
        run(win.monitor.refresh())
    assert client.daemon_started == 1                    # nothing starts by itself

    async def connect():
        popover._connect.emit("clicked")
        await _until(lambda: win.monitor.state.connected)
    run(connect())
    assert client.daemon_started == 2 and popover._disconnect.get_visible()
    assert not popover._help.get_visible()             # connected: the details instead
    assert win.pill.text == "Ready"


def test_no_banner_and_nothing_sent_while_disconnect_waits(make, run, svg):
    """While the service releases the link it answers connected=false: no 'Could not Connect'
    banner meanwhile, and Send and Test Cut are blocked until it has ended."""
    client = FakeDaemonClient()
    client.shutdown_gate = asyncio.Event()
    win = make(client)
    run(win.monitor.connect())
    run(win.open_path(svg))
    assert win.lookup_action("send").get_enabled()

    async def scenario():
        task = asyncio.ensure_future(win.disconnect_machine())
        await _until(lambda: win.monitor.state.blocker == DISCONNECTING)
        client.ping_reply = {**READY_PING, "connected": False, "status": None, "device": None,
                             "stopping": True}               # it is releasing the link
        for _ in range(3):
            await win.monitor.refresh()
        assert not win.banner.get_revealed() and win.pill.text != "Could not Connect"
        assert not win.lookup_action("send").get_enabled()
        assert not win.lookup_action("test-cut").get_enabled()
        assert win.send_panel.test_cut.get_tooltip_text() == DISCONNECTING
        client.shutdown_gate.set()
        assert await task is True
    run(scenario())
    assert win.monitor.state.note == RELEASED and not win.banner.get_revealed()
    assert client.opened == [] and client.stops == 0


def test_disconnect_is_not_available_during_a_job(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    popover = win.connection
    run(win.monitor.refresh())
    run(win.open_path(svg))
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(),
                               win.send_panel.tool_settings(), True)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: win.runner is not None and win.runner.holding)
        assert popover._disconnect.get_visible() and not popover._disconnect.get_sensitive()
        assert popover._disconnect.get_tooltip_text() == DISCONNECT_BUSY
        popover._disconnect.emit("clicked")             # (cannot be clicked; and if it were)
        assert not await win.disconnect_machine()
        assert client.shutdowns == 0
        win.activate_action("win.cancel-job")
        assert await job is Outcome.CANCELLED
    run(scenario())
    assert popover._disconnect.get_sensitive() and popover._disconnect.get_tooltip_text() is None
    client.ping_reply = {**READY_PING, "motion": "job", "status": "1"}   # a job of the command line
    run(win.monitor.refresh())
    assert not popover._disconnect.get_sensitive()


def test_a_refused_disconnect_says_why(make, run, monkeypatch):
    client = FakeDaemonClient()
    client.shutdown_reply = {"ok": False, "error": "a program or job is running", "pid": 1,
                             "busy": True}
    win = make(client)
    toasts = []
    monkeypatch.setattr(win, "toast", toasts.append)
    run(win.monitor.refresh())
    assert run(win.disconnect_machine()) is False
    assert toasts == [NOT_DISCONNECTED_BUSY]           # plain words, not the service's error
    assert win.monitor.state.connected


def test_updating_line_while_a_service_of_another_version_is_replaced(make, run):
    client = FakeDaemonClient()
    client.identity = dict(OTHER_BUILD)
    client.spawn_ping = dict(READY_PING)
    client.shutdown_gate = asyncio.Event()
    win = make(client)
    popover = win.connection

    async def scenario():
        task = asyncio.ensure_future(win.monitor.connect())
        await _until(lambda: win.monitor.state.note == UPDATING_SERVICE)
        assert win.pill.text == "Connecting…" and not win.banner.get_revealed()
        assert popover._scanning.get_visible()
        assert popover._scanning_text.get_text() == UPDATING_SERVICE
        assert not win.send_panel.test_cut.get_sensitive()
        client.shutdown_gate.set()
        await task
    run(scenario())
    assert win.monitor.state.connected and client.shutdowns == 1
    assert popover._scanning_text.get_text() == "Scanning for devices"


def test_another_version_banner_and_try_again(make, run):
    client = FakeDaemonClient()
    client.spawn_ping = dict(READY_PING)
    win = make(client)
    run(win.monitor.connect())
    client.identity = dict(OTHER_BUILD)               # first replace already done this run
    win.monitor._replaces_left = 0
    run(win.monitor.refresh())
    assert win.banner.get_revealed() and win.banner.get_title() == failure_text(ANOTHER_VERSION)
    assert win.banner.get_title() == ("Could not Connect. Another OpenCutter version is using the "
                                      "CAMEO 4. Close it, then press Try again.")
    assert win.banner.get_button_label() == "Try again" and win.pill.text == "Could not Connect"
    assert ANOTHER_VERSION in win.connection._alert_text.get_text()
    assert win.connection._connect.get_label() == "Try again"
    assert client.shutdowns == 0
    assert not win.lookup_action("test-cut").get_enabled()           # nothing goes to it
    assert win.send_panel.test_cut.get_tooltip_text() == "Connect the machine first."

    async def try_again():
        win.banner.emit("button-clicked")
        await _until(lambda: win.monitor.state.connected)
    run(try_again())
    assert client.shutdowns == 1 and not win.banner.get_revealed()


def test_send_is_blocked_while_another_version_runs_a_job(make, run, svg):
    client = FakeDaemonClient(ping={**READY_PING, "motion": "job", "status": "1"})
    client.identity = dict(OTHER_BUILD)
    win = make(client)
    run(win.monitor.connect())
    run(win.open_path(svg))
    panel = win.send_panel
    assert win.monitor.state.blocker == OTHER_VERSION_BUSY and client.shutdowns == 0
    assert not panel.send_button.get_sensitive() and not panel.test_cut.get_sensitive()
    assert panel._why.get_text() == OTHER_VERSION_BUSY
    assert panel.test_cut.get_tooltip_text() == OTHER_VERSION_BUSY
    assert not win.lookup_action("send").get_enabled()
    assert not win.lookup_action("test-cut").get_enabled()
    assert win.pill.text == "Busy"
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(),
                               panel.tool_settings(), False)
    assert run(win.run_job(spec.to_dict())) is Outcome.FAILED     # even called directly:
    assert client.opened == [] and client.sessions_closed == 0     # nothing was sent
    win.job_view.emit("done")
    client.ping_reply = {**READY_PING}                  # its job ended: replaced once
    client.spawn_ping = dict(READY_PING)
    run(win.monitor.refresh())
    assert client.shutdowns == 1 and win.monitor.state.blocker is None
    assert panel.send_button.get_sensitive()


# --- no logout or suspend while a job runs ------------------------------------------------------
class FakeApplication:
    """Records Gtk.Application.inhibit and uninhibit."""

    def __init__(self, cookie: int = 7, fail: bool = False):
        self.cookie, self.fail = cookie, fail
        self.inhibited: list[tuple] = []
        self.uninhibited: list[int] = []

    def inhibit(self, window, flags, reason):
        if self.fail:
            raise RuntimeError("no session manager")
        self.inhibited.append((window, flags, reason))
        return self.cookie

    def uninhibit(self, cookie):
        self.uninhibited.append(cookie)


def _job_until_the_outline_gate(win, app, run, svg):
    run(win.monitor.refresh())
    run(win.open_path(svg))
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(),
                               win.send_panel.tool_settings(), True)
    win.get_application = lambda: app

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: win.runner is not None and win.runner.holding)
        during = (list(app.inhibited), list(app.uninhibited))
        win.activate_action("win.cancel-job")
        return await job, during
    return run(scenario())


def test_a_job_keeps_the_desktop_from_logging_out_or_suspending(make, run, svg):
    """GNOME suspends an idle computer on battery after 15 minutes, and a logout would end the
    background service in the middle of a cut: while a job runs, the window asks the desktop
    not to (Gtk.Application.inhibit; in the Flatpak through the Inhibit portal), and lets go
    when it ends, however it ends."""
    win, app = make(FakeDaemonClient()), FakeApplication()
    outcome, (inhibited, uninhibited) = _job_until_the_outline_gate(win, app, run, svg)
    assert outcome is Outcome.CANCELLED
    flags = Gtk.ApplicationInhibitFlags.LOGOUT | Gtk.ApplicationInhibitFlags.SUSPEND
    assert inhibited == [(win, flags, "A job is running on the CAMEO 4")] and uninhibited == []
    assert window_mod.JOB_INHIBIT_FLAGS == flags
    assert app.uninhibited == [7] and len(app.inhibited) == 1


@pytest.mark.parametrize("app", [FakeApplication(cookie=0), FakeApplication(fail=True)],
                         ids=["refused", "fails"])
def test_a_desktop_that_does_not_agree_never_stops_the_job(make, run, svg, app):
    win = make(FakeDaemonClient())
    outcome, _ = _job_until_the_outline_gate(win, app, run, svg)
    assert outcome is Outcome.CANCELLED and app.uninhibited == []


# --- the flatpak-spawn helper at the window's close and the app's quit -----------------------------
def test_closing_the_window_ends_a_waiting_spawn_helper(make, run):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    assert client.helpers_ended == 0
    assert win._on_close_request(win) is False           # no job: the window closes
    assert client.helpers_ended == 1


def test_closing_during_a_job_ends_no_helper_before_the_job_ended(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(),
                               win.send_panel.tool_settings(), True)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: win.runner is not None and win.runner.holding)
        win._closing = True                            # (the quit dialog is already asking)
        assert win._on_close_request(win) is True      # kept open while the job runs
        assert client.helpers_ended == 0
        win._closing = False
        win.activate_action("win.cancel-job")
        return await job
    assert run(scenario()) is Outcome.CANCELLED


def test_quitting_the_app_ends_a_waiting_spawn_helper(monkeypatch):
    app = app_mod.OpenCutterApp()                      # never registered or run
    chained = []
    monkeypatch.setattr(app_mod.Adw.Application, "do_shutdown", lambda self: chained.append(self))
    app.do_shutdown()                                  # no window yet: nothing to end
    assert chained == [app]
    client = FakeDaemonClient()
    app.window = type("Window", (), {"client": client})()
    app.do_shutdown()
    assert client.helpers_ended == 1 and chained == [app, app]
