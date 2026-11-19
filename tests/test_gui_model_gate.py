# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The app with a machine the service refused for its model (the ping's reason wrong-model).

OpenCutter's service sends nothing to a machine that reports a model it does not drive. The
app says so at once, in the pill, the status bar, the banner, the connection popover, the first
run and under Send, and offers no Try again that could never work. The machine state, the
texts and the Send rules need no display; the window, the popover and the first run are skipped
without one (run them on a private gtk4-broadwayd, README.md).
Nothing here reaches Bluetooth: the service is a fake client, or the real daemon on the
simulated machine of tests/stubs.py.
"""
import asyncio

import pytest

import stubs
from stubs import ESC05, FG, harness  # noqa: F401

stubs.install()
import opencutter.gui  # noqa: E402,F401  (selects the GTK and libadwaita versions)
from gi.events import GLibEventLoop  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from opencutter.device.protocol import (REASON_UNREACHABLE, REASON_WRONG_MODEL,  # noqa: E402
                                        wrong_model_message)
from opencutter.gui import machine  # noqa: E402
from opencutter.gui.connection import (device_line, failure_markup, failure_text,  # noqa: E402
                                       failure_title)
from opencutter.gui.machine import (CONNECT_GRACE_SECONDS, NOT_SUPPORTED,  # noqa: E402
                                    OTHER_MACHINE_HINT, SWITCH_OFF_ON, UNSUPPORTED, WRONG_MODEL,
                                    WRONG_MODEL_UNKNOWN, Link, MachineMonitor, MachineState, pill)
from opencutter.gui.runner import JobRunner, Outcome  # noqa: E402
from opencutter.gui.sending import blocker_for_test_cut, send_blocker  # noqa: E402
from gui_fakes import INFO, READY_PING, WRONG_MODEL_PING, FakeDaemonClient  # noqa: E402

# a model no profile names, refused now and later (the CAMEO 5 itself is driven)
CAMEO5 = "CAMEO 5 PRO V1.07"
# the text the app shows for it, word for word (typographic quotes)
CAMEO5_PROBLEM = ("This machine reports itself as “CAMEO 5 PRO V1.07”. OpenCutter cannot drive "
                  "it and will not send jobs to it.")
WAITING = {"ok": True, "connected": False, "status": None, "motion": None, "device": None}
LETTER = (0.0, 0.0, 215.9, 279.4)
DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="8.5in" height="11in" viewBox="0 0 215.9 279.4"
  fill="none" stroke="#000"><rect x="30" y="40" width="50" height="30"/></svg>"""


class CountingClient(FakeDaemonClient):
    """Counts info() calls: nothing may be asked of a machine the service refused."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.info_calls = 0

    async def info(self) -> dict:
        self.info_calls += 1
        return await super().info()


def refused(model: str = CAMEO5) -> MachineState:
    return MachineState(link=Link.FAILED, problem=machine.wrong_model_problem(model), model=model,
                        wrong_model=True)


def monitor_for(ping: dict | None, clock=lambda: 0.0) -> tuple[MachineMonitor, CountingClient]:
    client = CountingClient(ping=ping)
    return MachineMonitor(client, clock=clock), client


# --- the texts ---------------------------------------------------------------------------------
def test_the_texts_word_for_word():
    assert WRONG_MODEL.format(model=CAMEO5) == CAMEO5_PROBLEM
    assert machine.wrong_model_problem(CAMEO5) == CAMEO5_PROBLEM
    assert machine.reason_problem(REASON_WRONG_MODEL, CAMEO5) == CAMEO5_PROBLEM
    assert machine.reason_problem(REASON_WRONG_MODEL) == WRONG_MODEL_UNKNOWN == (
        "This machine did not report its model. OpenCutter cannot drive it and will not send "
        "jobs to it.")
    assert machine.wrong_model_problem("   ") == WRONG_MODEL_UNKNOWN
    assert machine.wrong_model_problem(["CAMEO 5"]) == WRONG_MODEL_UNKNOWN
    assert UNSUPPORTED == "Unsupported Machine"
    assert NOT_SUPPORTED == "This machine is not supported."
    assert OTHER_MACHINE_HINT == (
        "OpenCutter drives these models: CAMEO 4 (firmware V1.xx or V5.xx), CAMEO 4 Plus, CAMEO 4 "
        "Pro, CAMEO 5, CAMEO 5 Plus, CAMEO Pro MK-II, CAMEO 5 Alpha, CAMEO 5 Alpha Plus, "
        "Portrait 3 and Portrait 4. To use one, pair it in Settings → Bluetooth and switch it on: "
        "OpenCutter connects to it by itself.")
    assert "tested" not in OTHER_MACHINE_HINT          # the app does not say which ones were
    # nothing of the gate's texts tells to press Try again or to switch the machine off and on
    for text in (CAMEO5_PROBLEM, WRONG_MODEL_UNKNOWN, NOT_SUPPORTED, OTHER_MACHINE_HINT):
        assert "Try again" not in text and "off and on" not in text
    # the service's own refusal (command line, a refused session) names the same model
    assert '"CAMEO 5 PRO V1.07"' in wrong_model_message(CAMEO5)


@pytest.mark.parametrize("reported, shown", [
    ("CAMEO 5 PRO V1.07    ", "CAMEO 5 PRO V1.07"),
    ("CAMEO\x1b[31m 5\x00", "CAMEO?[31m 5"),           # control characters never reach a label
    ("CAMEO 5\nV1.07", "CAMEO 5?V1.07"),
    ("CAMEO 5 – V1.07", "CAMEO 5 ? V1.07"),            # printable ASCII only
    ("X" * 60, "X" * 37 + "..."),                      # at most 40 characters
])
def test_the_model_is_shown_as_short_printable_text(reported, shown):
    assert machine.shown_model(reported) == shown
    assert machine.wrong_model_problem(reported) == WRONG_MODEL.format(model=shown)
    assert machine.shown_model(None) is None and machine.shown_model("") is None


def test_markup_escapes_what_the_machine_reports():
    problem = machine.wrong_model_problem("CAMEO <b>5</b> & Co")
    markup = failure_markup(problem, UNSUPPORTED)
    assert markup.startswith("<b>Unsupported Machine.</b> This machine reports itself as ")
    assert "“CAMEO &lt;b&gt;5&lt;/b&gt; &amp; Co”" in markup
    ok, _attrs, text, _accel = Pango.parse_markup(markup, -1, "\0")
    assert ok and text == f"Unsupported Machine. {problem}"
    assert failure_text(problem, UNSUPPORTED) == f"Unsupported Machine. {problem}"
    # the other failures read as before
    assert failure_text(SWITCH_OFF_ON) == f"Could not Connect. {SWITCH_OFF_ON}"
    assert failure_title(MachineState(link=Link.FAILED)) == "Could not Connect"
    assert failure_title(refused()) == UNSUPPORTED


def test_the_device_row_names_the_refused_machine_by_its_model():
    assert device_line(refused()) == (CAMEO5, "Not Supported", "negative")
    assert device_line(MachineState(link=Link.FAILED, wrong_model=True))[0] == "Unknown machine"
    assert device_line(MachineState()) == ("CAMEO 4", "Not Connected", "neutral")
    assert device_line(MachineState(link=Link.FAILED)) == ("CAMEO 4", "Not Connected", "negative")
    assert device_line(MachineState(link=Link.CONNECTED, device_name="CAMEO 4-TEST",
                                    model="CAMEO 4 V5.05")) == ("CAMEO 4-TEST", "Connected",
                                                                "positive")


# --- MachineState and the monitor ----------------------------------------------------------------
def test_wrong_model_is_shown_at_once():
    """No grace time: the service has already decided, and waiting does not change it."""
    async def scenario():
        monitor, client = monitor_for(dict(WRONG_MODEL_PING))
        await monitor.connect()
        state = monitor.state
        assert state.link is Link.FAILED and state.wrong_model and not state.connected
        assert state.model == CAMEO5 and state.problem == CAMEO5_PROBLEM
        assert state.status_text == UNSUPPORTED
        assert pill(state) == ("Unsupported Machine", "negative", False)
        assert state.tools is None and state.status is None and state.firmware is None
        assert state.tool_code(1) is None and not state.ready
        assert client.info_calls == 0 and client.daemon_started == 1
        await monitor.refresh()
        assert monitor.state == state and client.info_calls == 0     # nothing is asked of it
    asyncio.run(scenario())


def test_the_model_follows_the_ping():
    """Accepted machine, refused machine, then the service tries another machine (unreachable:
    a fresh grace time, the old model gone), then a CAMEO 4 connects."""
    async def scenario():
        clock = [0.0]
        monitor, client = monitor_for(dict(READY_PING), clock=lambda: clock[0])
        await monitor.refresh()
        assert monitor.state.connected and monitor.state.model == "CAMEO 4 V5.05"
        assert not monitor.state.wrong_model and monitor.state.firmware == "CAMEO 4 V5.05"
        client.ping_reply = dict(WRONG_MODEL_PING)        # the link dropped; another machine
        await monitor.refresh()
        assert monitor.state.wrong_model and monitor.state.model == CAMEO5
        assert monitor.state.firmware is None and monitor.state.device_name is None
        client.ping_reply = {**WAITING, "reason": REASON_UNREACHABLE, "model": None}
        clock[0] += 3 * CONNECT_GRACE_SECONDS
        await monitor.refresh()                            # a CAMEO 4 is being tried
        assert monitor.state.link is Link.CONNECTING and not monitor.state.wrong_model
        assert monitor.state.model is None and monitor.state.problem == SWITCH_OFF_ON
        client.ping_reply = dict(WRONG_MODEL_PING)
        await monitor.refresh()
        assert monitor.state.wrong_model
        client.ping_reply = dict(READY_PING)                # straight to a CAMEO 4
        await monitor.refresh()
        state = monitor.state
        assert state.connected and not state.wrong_model and state.problem is None
        assert state.model == "CAMEO 4 V5.05" and state.status_text == "Ready"
    asyncio.run(scenario())


def test_a_wrong_model_reason_without_a_model_still_refuses():
    async def scenario():
        monitor, _client = monitor_for({**WRONG_MODEL_PING, "model": None})
        await monitor.refresh()
        assert monitor.state.wrong_model and monitor.state.model is None
        assert monitor.state.problem == WRONG_MODEL_UNKNOWN
    asyncio.run(scenario())


def test_firmware_from_info_is_short_plain_text():
    async def scenario():
        client = CountingClient(info={**INFO, "firmware": "CAMEO 4 V5.05\x1b\x07    "})
        monitor = MachineMonitor(client)
        await monitor.refresh()
        assert monitor.state.firmware == "CAMEO 4 V5.05??" and client.info_calls == 1
    asyncio.run(scenario())


# --- Send and Test Cut -------------------------------------------------------------------------
def test_send_and_test_cut_say_the_machine_is_not_supported():
    """Nothing on screen can change it, so it comes before the material and the design."""
    state = refused()
    for chosen in (True, False):
        assert send_blocker(None, LETTER, state, None, False, chosen) == NOT_SUPPORTED
        assert blocker_for_test_cut(state, None, False, chosen) == NOT_SUPPORTED
    assert send_blocker(None, LETTER, state, None, True) == "A job is already running."
    assert blocker_for_test_cut(state, None, True) == "A job is already running."
    # every other state reads as before
    assert blocker_for_test_cut(MachineState(), None, False) == "Connect the machine first."
    assert blocker_for_test_cut(MachineState(link=Link.FAILED), None, False) == (
        "Connect the machine first.")


# --- the real service on the simulated machine ---------------------------------------------------
def test_the_monitor_and_a_job_against_a_service_that_refused_the_machine(harness, tmp_path):
    """The real daemon refuses a model no profile names: the app shows the model and the text,
    and a job it tries anyway is refused by the service with nothing sent to the machine."""
    harness.fake_cls.fg_reply = CAMEO5

    async def scenario(h):
        client = h.client()
        monitor = MachineMonitor(client, clock=lambda: 0.0)
        state = await monitor.refresh()
        assert state.link is Link.FAILED and state.wrong_model
        assert state.model == CAMEO5 and state.problem == CAMEO5_PROBLEM
        runner = JobRunner(client)
        spec = {"mat": "12x12", "media": list(LETTER), "groups": [], "preview_outline": True}
        assert await runner.run_job(spec) is Outcome.FAILED
        assert runner.error == wrong_model_message(CAMEO5)
        assert h.fake.writes == [ESC05, FG]                  # exactly, and nothing after
    harness.run(scenario, until=lambda p: p.get("reason") == REASON_WRONG_MODEL)


# --- the window, the popover and the first run (a display) -------------------------------------
@pytest.fixture(scope="module")
def display():
    # init_check() can report success without a display: check the display itself
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("GTK cannot open a display")
    Adw.init()
    return Gdk.Display.get_default()


@pytest.fixture
def run(display):
    loop = GLibEventLoop(GLib.MainContext.default())
    yield loop.run_until_complete
    loop.close()


@pytest.fixture
def make(display, tmp_path):
    from opencutter.gui.settings import GuiSettings
    from opencutter.gui.window import MainWindow
    from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source
    windows = []

    def factory(client: FakeDaemonClient):
        settings = GuiSettings.load(tmp_path / "gui.json")
        win = MainWindow(None, client, PresetStore(list(BASIC_PRESETS), basic_source()), settings)
        windows.append(win)
        return win
    yield factory
    for win in windows:
        win.destroy()


@pytest.fixture
def svg(tmp_path):
    path = tmp_path / "design.svg"
    path.write_text(DESIGN)
    return str(path)


def _popover_texts(pop) -> dict:
    return {"alert": pop._alert.get_visible() and pop._alert_text.get_text(),
            "name": pop._device_name.get_text(), "state": pop._device_state.get_text(),
            "connect": pop._connect.get_visible(), "disconnect": pop._disconnect.get_visible(),
            "hint": pop._hint.get_visible() and pop._hint.get_text(),
            "help": pop._help.get_visible(), "note": pop._note.get_visible(),
            "props": pop._props.get_visible(), "firmware": pop._firmware.get_text()}


def test_the_window_says_unsupported_machine_everywhere(make, run, svg):
    client = CountingClient(ping=WRONG_MODEL_PING)
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    assert win.doc is not None and win.send_panel.material is not None    # all else is ready
    # the pill and the status bar: the refused machine is never called a CAMEO 4
    assert win.pill.text == UNSUPPORTED and not win.pill._name.get_visible()
    assert win.pill.get_tooltip_text() == "CAMEO 5 PRO V1.07: Unsupported Machine"
    assert "CAMEO 4" not in win.pill.get_tooltip_text()
    assert win.status_label.get_text() == "Status: Unsupported Machine"
    assert [w.get_text() for w in win.tool_labels.values()] == ["Tool 1: —", "Tool 2: —"]
    # nothing will be detected on a refused machine: no "yet"
    assert win.page_setup._detected.get_text() == "Not detected"
    assert win.send_panel._cards[win.send_panel.holder]._desc.get_text() == "Not detected"
    # the banner: at once, the model and the text, and no Try again
    assert win.banner.get_revealed()
    assert win.banner.get_title() == f"Unsupported Machine. {CAMEO5_PROBLEM}"
    assert not win.banner.get_button_label()
    # Send and Test Cut: disabled, with the short reason
    assert not win.lookup_action("send").get_enabled()
    assert not win.lookup_action("test-cut").get_enabled()
    assert not win.send_panel.send_button.get_sensitive()
    assert win.send_panel._why.get_text() == NOT_SUPPORTED and win.send_panel._why.get_visible()
    assert win.send_panel.test_cut.get_tooltip_text() == NOT_SUPPORTED
    assert win.header_send.get_tooltip_text() == NOT_SUPPORTED
    # the connection popover: the model, no Try again, none of the pairing and power tips
    assert _popover_texts(win.connection) == {
        "alert": f"Unsupported Machine. {CAMEO5_PROBLEM}", "name": CAMEO5,
        "state": "Not Supported", "connect": False, "disconnect": False,
        "hint": OTHER_MACHINE_HINT, "help": False, "note": False, "props": False,
        "firmware": "—"}
    assert client.info_calls == 0


def test_a_cameo_4_switched_on_later_is_used_and_the_texts_come_back(make, run):
    clock = [0.0]
    client = FakeDaemonClient(ping=WRONG_MODEL_PING)
    win = make(client)
    win.monitor._clock = lambda: clock[0]
    run(win.monitor.refresh())
    assert win.banner.get_revealed() and not win.banner.get_button_label()
    # the service tries a CAMEO 4 that does not answer: Connecting, then Could not Connect
    client.ping_reply = {**WAITING, "reason": REASON_UNREACHABLE, "model": None}
    run(win.monitor.refresh())
    assert win.pill.text == "Connecting…" and not win.banner.get_revealed()
    assert win.pill._name.get_visible() and win.pill.get_tooltip_text() == "CAMEO 4: Connecting…"
    assert win.page_setup._detected.get_text() == "Not detected yet"
    clock[0] += CONNECT_GRACE_SECONDS + 1
    run(win.monitor.refresh())
    assert win.pill.text == "Could not Connect"
    assert win.banner.get_title() == f"Could not Connect. {SWITCH_OFF_ON}"
    assert win.banner.get_button_label() == "Try again"
    texts = _popover_texts(win.connection)
    assert texts["name"] == "CAMEO 4" and texts["state"] == "Not Connected"
    assert texts["connect"] and texts["help"] and texts["note"] and not texts["hint"]
    assert win.connection._connect.get_label() == "Try again"
    # it answers now: connected, and the Firmware row shows what it reports itself as
    client.ping_reply = dict(READY_PING)
    run(win.monitor.refresh())
    assert win.monitor.state.connected and not win.banner.get_revealed()
    assert win.pill.text == "Ready" and win.send_panel.test_cut.get_tooltip_text() is None
    assert _popover_texts(win.connection) == {
        "alert": False, "name": "CAMEO 4-TEST", "state": "Connected", "connect": False,
        "disconnect": True, "hint": "Releases the CAMEO 4 so other apps can connect to it.",
        "help": False, "note": True, "props": True, "firmware": "CAMEO 4 V5.05"}


def test_the_firmware_row_shows_the_model_also_without_info(make, run):
    client = FakeDaemonClient(info=None)
    win = make(client)
    run(win.monitor.refresh())
    assert win.monitor.state.firmware is None
    assert win.connection._firmware.get_text() == "CAMEO 4 V5.05"     # the ping's model


def test_what_the_machine_reports_is_never_markup(make, run):
    model = "CAMEO <i>5</i> & V1"
    client = FakeDaemonClient(ping={**WRONG_MODEL_PING, "model": model})
    win = make(client)
    run(win.monitor.refresh())
    assert win.pill.get_tooltip_text() == f"{model}: Unsupported Machine"
    pop = win.connection
    assert pop._alert_text.get_text() == (f"Unsupported Machine. This machine reports itself as "
                                          f"“{model}”. OpenCutter cannot drive it and will not "
                                          "send jobs to it.")
    assert pop._device_name.get_text() == model
    assert win.banner.get_title().endswith(f"“{model}”. OpenCutter cannot drive it and will not "
                                           "send jobs to it.")
    assert not win.banner.get_use_markup()


def test_a_long_model_is_ellipsized_in_the_device_row(make, run):
    model = "SILHOUETTE CAMEO 5 PLUS ALPHA V1.07 BETA 2 BUILD 7"
    client = FakeDaemonClient(ping={**WRONG_MODEL_PING, "model": model})
    win = make(client)
    run(win.monitor.refresh())
    shown = win.monitor.state.model
    assert len(shown) == 40 and shown.endswith("...")
    name = win.connection._device_name
    assert name.get_text() == shown and name.get_tooltip_text() == shown
    assert name.get_ellipsize() == Pango.EllipsizeMode.END


def test_the_first_run_shows_the_model_without_try_again(display):
    """The first run of a new owner of another model: no Try again, and none of the advice
    that cannot help (switch it off and on, "the machine moves only after you press Send"),
    but how to use a CAMEO 4 instead, as in the connection popover."""
    from opencutter.gui.firstrun import FirstRunDialog
    dialog = FirstRunDialog()
    steps = [row.get_visible() for row in dialog._steps]
    assert steps == [True, True] and dialog._note.get_visible()
    assert not dialog._hint.get_visible()
    dialog.update_machine(refused())
    assert dialog._device_name.get_text() == CAMEO5
    assert dialog._device_state.get_text() == "Not Supported"
    assert dialog._problem.get_visible() and dialog._problem.get_text() == CAMEO5_PROBLEM
    assert dialog._hint.get_visible() and dialog._hint.get_text() == OTHER_MACHINE_HINT
    assert not dialog._hint.get_use_markup()
    assert not any(row.get_visible() for row in dialog._steps)
    assert not dialog._note.get_visible()
    assert not dialog._connect.get_visible()
    assert dialog._finish.get_label() == "Skip for now"
    dialog.update_machine(MachineState(link=Link.FAILED, problem=SWITCH_OFF_ON))
    assert dialog._device_name.get_text() == "CAMEO 4"
    assert dialog._device_state.get_text() == "Could not Connect"
    assert dialog._connect.get_visible() and dialog._connect.get_label() == "Try again"
    assert all(row.get_visible() for row in dialog._steps) and dialog._note.get_visible()
    assert not dialog._hint.get_visible()


def test_a_cameo_4_of_another_firmware_line_is_told_which_one_is_used(make, run):
    """'CAMEO 4 V9.99' is refused too: the popover's hint names the firmware line, so it does
    not read as if the user's CAMEO 4 should simply be paired and switched on."""
    client = FakeDaemonClient(ping={**WRONG_MODEL_PING, "model": "CAMEO 4 V9.99"})
    win = make(client)
    run(win.monitor.refresh())
    texts = _popover_texts(win.connection)
    assert texts["name"] == "CAMEO 4 V9.99" and texts["state"] == "Not Supported"
    assert texts["hint"] == OTHER_MACHINE_HINT
    assert "CAMEO 4 (firmware V1.xx or V5.xx)" in texts["hint"]
    assert not texts["help"] and not texts["connect"]
    assert win.banner.get_title() == (
        "Unsupported Machine. This machine reports itself as “CAMEO 4 V9.99”. OpenCutter cannot "
        "drive it and will not send jobs to it.")


def test_a_machine_that_reported_no_model(make, run):
    client = FakeDaemonClient(ping={**WRONG_MODEL_PING, "model": None})
    win = make(client)
    run(win.monitor.refresh())
    assert win.banner.get_title() == f"Unsupported Machine. {WRONG_MODEL_UNKNOWN}"
    assert win.pill.text == UNSUPPORTED
    assert win.pill.get_tooltip_text() == "Unknown machine: Unsupported Machine"
    texts = _popover_texts(win.connection)
    assert texts["name"] == "Unknown machine" and texts["state"] == "Not Supported"


def test_the_smallest_window_holds_the_unsupported_machine_state(make, run, svg):
    """At 760 x 560 the pill's longer text and the banner with a 40-character model fit, with
    a design open and without: nothing is clipped and the window need not grow."""
    from opencutter.gui import app as app_mod
    display = Gdk.Display.get_default()
    provider = app_mod.load_style(display)
    gtk_settings = Gtk.Settings.get_default()
    animations = gtk_settings.get_property("gtk-enable-animations")
    gtk_settings.set_property("gtk-enable-animations", False)       # the banner shows at once
    try:
        client = FakeDaemonClient(ping={**WRONG_MODEL_PING, "model": "W" * 50})
        win = make(client)
        win.set_default_size(760, 560)
        win.present()
        run(win.monitor.refresh())
        content = win.get_content()
        statusbar = win.status_label.get_parent()
        for opened in (False, True):
            if opened:
                run(win.open_path(svg))
            run(asyncio.sleep(0.15))
            assert win.get_width() == 760 and win.get_height() == 560, opened
            assert win.banner.get_revealed() and win.pill.text == UNSUPPORTED
            assert content.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= 760, opened
            split_min = win.split.measure(Gtk.Orientation.VERTICAL, win.split.get_width())[0]
            assert win.split.get_height() >= split_min, opened
            ok, bounds = statusbar.compute_bounds(win)
            assert ok and bounds.get_y() + bounds.get_height() <= 560, opened
    finally:
        gtk_settings.set_property("gtk-enable-animations", animations)
        Gtk.StyleContext.remove_provider_for_display(display, provider)
