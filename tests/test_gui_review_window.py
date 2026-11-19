# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Review fixes in the main window, with the fake daemon client (no socket at all).

- gui-1: one send at a time; a second Send/Test Cut never replaces the running job's runner.
- gui-2: typed but unconfirmed lengths are committed before the spec is built; the Send sheet
  checks the current design and media live, shows a snapshot, and a job that changed after the
  sheet was confirmed is not sent but asked again.
- gui-3: after a job the focus is not left on Test Cut (or Send).
- gui-4: closing during a job keeps the window when the stop could not be sent.
- gui-5: the sidebar with Cancel Job stays shown while a job runs; the header has Cancel Job.
- gui-6: the job view says the stop was not confirmed.
- gui-7: the header pill's percentage follows the job view's 1 s tick.
- bytes-3: Line Segment Overcut and Overcut exist only for blade tools.

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

from opencutter.gui import sending, window as window_mod  # noqa: E402
from opencutter.gui.canvas import MatThumbnail  # noqa: E402
from opencutter.gui.runner import JobRunner, Outcome  # noqa: E402
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.gui.sheets import LoadMediaDialog, SendDialog  # noqa: E402
from opencutter.gui.sidebar import SendPanel  # noqa: E402
from opencutter.gui.window import MainWindow  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from gui_fakes import FakeDaemonClient  # noqa: E402

# a 30 x 20 mm rectangle at x 150..180, y 30..50 on a 300 mm page
DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="300mm" height="300mm" viewBox="0 0 300 300"
  fill="none" stroke="#000"><rect x="150" y="30" width="30" height="20"/></svg>"""


@pytest.fixture
def run():
    loop = GLibEventLoop(GLib.MainContext.default())
    yield loop.run_until_complete
    # a failed scenario can leave a job waiting at a held step: end it before the next test
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

    def factory(client: FakeDaemonClient, present: bool = False, **settings_kw) -> MainWindow:
        settings = GuiSettings.load(tmp_path / "gui.json")
        settings.units, settings.preview_outline = "mm", False
        settings.media, settings.custom_media_mm = "custom", [254.0, 254.0]
        for key, value in settings_kw.items():
            setattr(settings, key, value)
        win = MainWindow(None, client, PresetStore(list(BASIC_PRESETS), basic_source()), settings)
        windows.append(win)
        if present:
            win.present()
        return win
    yield factory
    for win in windows:
        win.destroy()


@pytest.fixture
def messages(monkeypatch):
    shown = []
    monkeypatch.setattr(window_mod.dialogs, "show_message",
                        lambda parent, heading, body: shown.append((heading, body)))
    return shown


async def _until(predicate, timeout: float = 10) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def _cut_index(win: MainWindow, test_cut: bool = False) -> int:
    """Index of the cut step in the plan of what the window would send now."""
    media, settings = win.page_setup.media_rect(), win.send_panel.tool_settings()
    spec = (sending.spec_for_test_cut(media, settings) if test_cut else
            sending.job_spec(win.doc, media, settings, win.send_panel.preview.get_active()))
    return next(i for i, step in enumerate(build_plan(spec).steps) if step.title.startswith("Cut"))


def _xs(spec: dict) -> tuple[float, float]:
    xs = [x for group in spec["groups"] for path in group["paths"] for x, _y in path]
    return min(xs), max(xs)


def _rows(sheet: SendDialog) -> dict[str, bool]:
    rows, child = {}, sheet._list.get_first_child()
    while child is not None:
        rows[child.get_last_child().get_text()] = \
            child.get_first_child().get_icon_name() == "object-select-symbolic"
        child = child.get_next_sibling()
    return rows


def _descendants(widget: Gtk.Widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


def _send_sheet(win: MainWindow, other_than=None):
    dialog = win.get_visible_dialog()
    return dialog if isinstance(dialog, SendDialog) and dialog is not other_than else None


class SlowInfoClient(FakeDaemonClient):
    """ESC 05 + ESC 15 + FG take a while, as over BLE: send_flow awaits them."""

    async def info(self) -> dict:
        await asyncio.sleep(0.15)
        return await super().info()


# --- gui-1 ---------------------------------------------------------------------------------
def test_double_test_cut_runs_one_job_and_keeps_its_cancel(make, run):
    client = SlowInfoClient()
    release = asyncio.Event()
    win = make(client)
    run(win.monitor.refresh())
    cut = _cut_index(win, test_cut=True)
    client.hold_step = (cut, release)

    async def scenario():
        assert win.lookup_action("test-cut").get_enabled()
        win.send_panel.test_cut.emit("clicked")                  # first click of a double click
        await asyncio.sleep(0.08)                                # inside the BLE refresh
        for name in ("test-cut", "send", "open"):
            assert not win.lookup_action(name).get_enabled(), name
        assert not win.send_panel.test_cut.get_sensitive()
        assert win.send_panel.test_cut.get_tooltip_text() == window_mod.SENDING
        win.send_panel.test_cut.emit("clicked")                  # second click
        win.activate_action("win.test-cut")
        await asyncio.wait_for(win.send_flow(test_cut=True), 2)  # returns at once: busy
        await _until(lambda: cut in client.steps_started)
        first = win.runner
        assert len(client.opened) == 1 and win._job_running()
        assert win.lookup_action("cancel-job").get_enabled()
        win.activate_action("win.cancel-job")                    # Esc reaches the running job
        await _until(lambda: not win._job_running())
        assert win.runner is first and first.outcome is Outcome.CANCELLED
        await asyncio.sleep(0.3)
    run(scenario())
    assert len(client.opened) == 1 and client.stops == 1
    assert win.lookup_action("test-cut").get_enabled() and win.lookup_action("open").get_enabled()


def test_run_job_never_replaces_a_running_runner(make, run, svg):
    client = FakeDaemonClient()
    release = asyncio.Event()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    cut = _cut_index(win)
    client.hold_step = (cut, release)
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(), win.send_panel.tool_settings(), False)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: cut in client.steps_started)
        first = win.runner
        assert await asyncio.wait_for(win.run_job(spec.to_dict()), 2) is Outcome.FAILED
        assert win.runner is first and first.outcome is Outcome.RUNNING and len(client.opened) == 1
        release.set()
        assert await asyncio.wait_for(job, 5) is Outcome.DONE
    run(scenario())


def test_actions_stay_disabled_while_the_send_sheet_is_open(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: _send_sheet(win) is not None)
        assert not any(win.lookup_action(n).get_enabled() for n in ("send", "test-cut", "open"))
        await asyncio.wait_for(win.send_flow(), 2)               # Ctrl+Enter again: nothing new
        await asyncio.wait_for(win.send_flow(test_cut=True), 2)
        assert client.opened == []
        win.get_visible_dialog().close()
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert client.opened == [] and win.runner is None
    assert all(win.lookup_action(n).get_enabled() for n in ("send", "test-cut", "open"))


def test_closing_the_window_before_run_job_starts_nothing(make, run):
    """Test Cut clicked, then the window closed while the machine state is still read: the job
    must not start on a window that is gone (no Cancel Job would be left)."""
    client = SlowInfoClient()
    win = make(client, present=True)
    run(win.monitor.refresh())

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow(test_cut=True))
        await asyncio.sleep(0.05)                                # inside the BLE refresh
        win.close()
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert client.opened == [] and win.runner is None and not win.get_visible()


# --- gui-2 ---------------------------------------------------------------------------------
def test_typed_x_is_committed_before_the_spec_is_built(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    assert win.doc.bbox()[0] == pytest.approx(150.0, abs=1e-3)

    async def scenario():
        win.entries["X"].set_text("20")                          # typed, no Enter, no Tab
        win.activate_action("win.send")                          # Ctrl+Enter
        await _until(lambda: _send_sheet(win) is not None)
        sheet = win.get_visible_dialog()
        thumb = next(w for w in _descendants(sheet) if isinstance(w, MatThumbnail))
        assert thumb.scene.doc is not win.doc                    # a snapshot, not the live doc
        assert thumb.scene.doc.bbox()[0] == pytest.approx(20.0)
        sheet._send.emit("clicked")
        await _until(lambda: client.opened and not win._job_running())
    run(scenario())
    assert _xs(client.opened[0]) == (pytest.approx(20.0, abs=0.01), pytest.approx(50.0, abs=0.01))
    assert win.doc.bbox()[0] == pytest.approx(20.0)


def test_typed_custom_media_width_is_checked_before_anything_is_shown(make, run, svg, messages):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))

    async def scenario():
        win.sidebar.show_tab("page-setup")
        win.page_setup._custom_w.set_text("100")                 # the real piece is 100 mm wide
        win.activate_action("win.send")
        await _until(lambda: messages)
        await asyncio.sleep(0.2)
    run(scenario())
    assert win.page_setup.media_rect() == (0.0, 0.0, 100.0, 254.0)
    assert messages[0][0] == "This job cannot be sent" and "outside the media" in messages[0][1]
    assert client.opened == [] and win.get_visible_dialog() is None
    assert win.area_banner.get_revealed()


def test_send_sheet_checks_the_cut_area_live(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: _send_sheet(win) is not None)
        sheet = win.get_visible_dialog()
        assert _rows(sheet)["Design on the media and inside the cut area"] and sheet._send.get_sensitive()
        win.doc.move_to(240, 30)                                 # now past the 254 mm media
        win._sync_placement()
        assert not _rows(sheet)["Design on the media and inside the cut area"] and not sheet._send.get_sensitive()
        win.doc.move_to(150, 30)
        win._sync_placement()
        assert _rows(sheet)["Design on the media and inside the cut area"] and sheet._send.get_sensitive()
        sheet.close()
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert client.opened == []


def test_a_job_changed_after_the_sheet_is_asked_again(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: _send_sheet(win) is not None)
        first = win.get_visible_dialog()
        win.doc.move_to(60, 30)                                  # changed after the spec was built
        win._sync_placement()
        first._send.emit("clicked")
        await _until(lambda: _send_sheet(win, other_than=first) is not None)
        assert client.opened == []                               # the old job was not sent
        second = win.get_visible_dialog()
        thumb = next(w for w in _descendants(second) if isinstance(w, MatThumbnail))
        assert thumb.scene.doc.bbox()[0] == pytest.approx(60.0)
        second._send.emit("clicked")
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert len(client.opened) == 1
    assert _xs(client.opened[0]) == (pytest.approx(60.0, abs=0.01), pytest.approx(90.0, abs=0.01))


def test_test_cut_settings_changed_behind_load_media_send_nothing(make, run, messages):
    client = FakeDaemonClient()
    client.set_status("2")                                       # Load Media sheet first
    win = make(client)
    run(win.monitor.refresh())

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow(test_cut=True))
        await _until(lambda: isinstance(win.get_visible_dialog(), LoadMediaDialog))
        media = win.get_visible_dialog()
        win.send_panel._edit("force", 25)                        # different from the preset
        client.set_status("0")
        await win.monitor.refresh()
        media._continue.emit("clicked")
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert client.opened == [] and messages and messages[0][0] == "The settings changed"


# --- gui-3 ---------------------------------------------------------------------------------
def test_focus_leaves_test_cut_after_the_test_cut(make, run):
    client = FakeDaemonClient()
    win = make(client, present=True)
    run(win.monitor.refresh())
    button = win.send_panel.test_cut
    assert not button.get_focus_on_click()

    async def scenario():
        await asyncio.sleep(0.2)
        button.grab_focus()                                      # e.g. reached with Tab
        button.emit("clicked")
        await _until(lambda: client.opened and win.runner.outcome is not Outcome.RUNNING
                     and win.side_stack.get_visible_child_name() == "panels")
        await asyncio.sleep(0.3)
        focus = win.get_focus()
        assert focus is not button
        if focus is not None:                                    # a stray Return or Space
            focus.activate()
        await asyncio.sleep(0.3)
    run(scenario())
    assert len(client.opened) == 1 and win.get_visible_dialog() is None


def test_focus_goes_to_the_canvas_when_a_design_is_open(make, run, svg):
    client = FakeDaemonClient()
    win = make(client, present=True)
    run(win.monitor.refresh())
    run(win.open_path(svg))

    async def scenario():
        await asyncio.sleep(0.3)
        win.send_panel.test_cut.grab_focus()
        await asyncio.wait_for(win.send_flow(test_cut=True), 5)
        await asyncio.sleep(0.2)
    run(scenario())
    assert win.runner.outcome is Outcome.DONE and win.get_focus() is win.canvas


# --- gui-4 ---------------------------------------------------------------------------------
@pytest.mark.parametrize("stop_ok", [False, True])
def test_close_during_job_keeps_the_window_when_the_stop_failed(make, run, svg, monkeypatch, stop_ok):
    client = FakeDaemonClient()
    if not stop_ok:
        client.stop_reply = {"ok": False, "errors": ["1b 09: BleakError: write failed"]}
    release = asyncio.Event()
    alerts = []

    async def yes(_parent):
        return True

    async def alert(parent):
        alerts.append(parent)
    monkeypatch.setattr(window_mod.dialogs, "confirm_quit_during_job", yes)
    monkeypatch.setattr(window_mod.dialogs, "stop_failed", alert)
    win = make(client, present=True)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    cut = _cut_index(win)
    client.hold_step = (cut, release)
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(), win.send_panel.tool_settings(), False)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: cut in client.steps_started)
        win.close()
        await _until(lambda: alerts or not win.get_visible())
        await asyncio.wait_for(job, 5)
    run(scenario())
    assert client.stops == 1
    if stop_ok:
        assert not win.get_visible() and alerts == []
    else:
        assert win.get_visible() and alerts == [win]
        assert win.runner.stop_failed and win.job_view._error.get_visible()


# --- gui-5 ---------------------------------------------------------------------------------
def test_sidebar_with_cancel_job_stays_shown_during_a_job(make, run, svg):
    client = FakeDaemonClient()
    release = asyncio.Event()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    cut = _cut_index(win)
    client.hold_step = (cut, release)
    split = win.split
    assert not split.get_pin_sidebar() and win.sidebar_toggle.get_sensitive()
    assert win.header_send.get_visible() and not win.header_cancel.get_visible()
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(), win.send_panel.tool_settings(), False)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: cut in client.steps_started)
        assert split.get_pin_sidebar() and not win.sidebar_toggle.get_sensitive()
        split.set_collapsed(True)                                # what the 980sp breakpoint does
        await asyncio.sleep(0.1)
        assert split.get_show_sidebar()
        split.set_show_sidebar(False)                            # Esc in overlay, a click outside
        assert split.get_show_sidebar()
        assert win.header_cancel.get_visible() and not win.header_send.get_visible()
        assert win.header_cancel.get_action_name() == "win.cancel-job"
        win.header_cancel.emit("clicked")
        assert await asyncio.wait_for(job, 5) is Outcome.CANCELLED
        split.set_collapsed(False)
    run(scenario())
    assert client.stops == 1
    assert not split.get_pin_sidebar() and win.sidebar_toggle.get_sensitive()
    assert win.header_send.get_visible() and not win.header_cancel.get_visible()


# --- gui-6 ---------------------------------------------------------------------------------
def test_job_view_says_the_stop_was_not_confirmed(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    client.fail_at, client.fail = _cut_index(win), "BleakError: write failed"
    client.stop_errors = ["1b 09: BleakError: write failed"]
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(), win.send_panel.tool_settings(), False)
    assert run(win.run_job(spec.to_dict())) is Outcome.FAILED
    view = win.job_view
    assert win.runner.stop_failed and view._done_title.get_text() == "Stop not confirmed"
    assert "power button now" in view._done_hint.get_text()


# --- gui-7 ---------------------------------------------------------------------------------
def test_pill_percentage_follows_the_one_second_tick(make, run, svg, monkeypatch):
    now = [1000.0]

    class ClockRunner(JobRunner):
        def __init__(self, client, on_update=None):
            super().__init__(client, on_update, clock=lambda: now[0])
    monkeypatch.setattr(window_mod, "JobRunner", ClockRunner)
    client = FakeDaemonClient()
    release = asyncio.Event()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    cut = _cut_index(win)
    client.hold_step = (cut, release)
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(), win.send_panel.tool_settings(), False)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: cut in client.steps_started)
        runner = win.runner
        runner.summary = {**runner.summary, "estimated_seconds": 120}
        step = runner.steps[cut]
        share = 120 * step.writes / sum(s.writes for s in runner.steps)   # the cut step's estimate
        texts = []
        for fraction in (0.0, 0.3, 0.6):
            now[0] = 1000.0 + fraction * share
            win.job_view._on_tick()                              # the job view's 1 s tick
            texts.append(win.pill.text)
            assert win.pill.text == f"Cutting {round(runner.progress() * 100)}%"
        assert len(set(texts)) == 3, texts
        release.set()
        await asyncio.wait_for(job, 5)
    run(scenario())


# --- bytes-3 -------------------------------------------------------------------------------
def test_overcut_settings_only_for_blade_tools(tmp_path):
    settings = GuiSettings.load(tmp_path / "gui.json")
    panel = SendPanel(PresetStore(list(BASIC_PRESETS), basic_source()), settings)
    rows = (panel._lso, panel._overcut)

    panel.select_material("Vinyl")                               # AutoBlade: blade tool
    assert panel.blade_tool and all(w.get_visible() and w.get_sensitive() for w in rows)
    panel._overcut.set_active(False)
    panel._lso.set_active(True)
    s = panel.tool_settings()
    assert s.overcut == (0, 0) and s.line_segment_overcut

    panel.select_material("Sketch Pen")                          # Sketch Pen: no blade
    assert panel.preset().tool_code == 7 and not panel.blade_tool
    assert not any(w.get_visible() or w.get_sensitive() for w in rows)
    panel._overcut.set_active(True)                              # cannot happen from the UI;
    panel._lso.set_active(True)                                  # the edit is dropped anyway
    assert "overcut" not in panel._edits and "line_segment_overcut" not in panel._edits
    panel._edits.update(overcut=(1, 1), line_segment_overcut=True)
    s = panel.tool_settings()
    assert s.overcut == (0, 0) and not s.line_segment_overcut and s.offset == 0
    assert not panel.modified
