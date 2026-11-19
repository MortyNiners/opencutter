# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The main window with a fake daemon client: loading a design, Send rules in the sidebar,
the job view with the outline gate, and Cancel Job.

Needs a display: run headless with GDK_BACKEND=broadway and a private gtk4-broadwayd. Windows
are never presented and no dialog is shown; the module is skipped when GTK cannot start.
"""
import asyncio

import pytest

import stubs

stubs.install()
import opencutter.gui  # noqa: E402,F401  (selects the GTK and libadwaita versions)
from gi.events import GLibEventLoop  # noqa: E402
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

# init_check() can report success without a display (e.g. on a headless CI runner), so check
# the display itself: building a window without one crashes the interpreter
if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("GTK cannot open a display", allow_module_level=True)
Adw.init()

from opencutter.gui import window as window_mod  # noqa: E402
from opencutter.gui.runner import Outcome  # noqa: E402
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.gui.window import MainWindow  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from gui_fakes import FakeDaemonClient  # noqa: E402

DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="8.5in" height="11in" viewBox="0 0 215.9 279.4"
  fill="none" stroke="#000"><rect x="30" y="40" width="50" height="30"/>
  <path d="M100 40 L130 70 L100 70 Z"/></svg>"""


@pytest.fixture
def run():
    loop = GLibEventLoop(GLib.MainContext.default())
    yield loop.run_until_complete
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


def test_load_design_enables_send(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)

    async def scenario():
        await win.monitor.refresh()
        assert not win.send_panel.send_button.get_sensitive()
        assert win.send_panel.send_button.get_tooltip_text() == "Open a design first."
        assert await win.open_path(svg)
    run(scenario())
    assert win.main_stack.get_visible_child_name() == "design"
    assert win.doc_label.get_text() == "design.svg" and not win.modified_dot.get_visible()
    assert win.entries["X"].get_text() == "1.18" and win.entries["W"].get_text() == "3.94"
    assert win.send_panel.material == "Vinyl" and win.send_panel.action == "Cut"
    assert win.send_panel.send_button.get_sensitive()
    assert win.lookup_action("send").get_enabled() and win.lookup_action("test-cut").get_enabled()
    assert win.pill.text == "Ready"

    win.entries["W"].set_text("2")
    win.entries["W"].emit("activate")
    assert win.doc.size[0] == pytest.approx(50.8) and win.modified_dot.get_visible()


def test_tool_mismatch_disables_send(make, run, svg):
    client = FakeDaemonClient()
    client.set_tools(" 7, 4")
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    panel = win.send_panel
    assert not panel.send_button.get_sensitive() and not panel.test_cut.get_sensitive()
    assert panel.send_button.get_tooltip_text() == "Load the AutoBlade into Tool 1 to send."
    assert panel._mismatch.get_visible()
    assert not win.lookup_action("send").get_enabled()

    client.set_tools(" 2, 4")
    run(win.monitor.refresh())
    assert panel.send_button.get_sensitive() and not panel._mismatch.get_visible()


def test_outside_cut_area_warns_until_fit_to_mat(make, run, svg):
    win = make(FakeDaemonClient())
    run(win.monitor.refresh())
    run(win.open_path(svg))
    win.doc.move_to(280, 10)
    win._sync_placement()
    assert win.area_banner.get_revealed()
    assert win.send_panel.send_button.get_tooltip_text() == "Move the design inside the cut area."
    win.fit_design()
    assert not win.area_banner.get_revealed() and win.send_panel.send_button.get_sensitive()


def test_job_view_gate_and_cancel_job(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(), win.send_panel.tool_settings(), True)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: win.runner is not None and win.runner.holding)
        assert win.side_stack.get_visible_child_name() == "job"
        assert win.pill.text == "Check the position"
        assert not win.placement_bar.get_sensitive() and win.scene.locked
        assert not win.lookup_action("send").get_enabled()
        assert win.lookup_action("cancel-job").get_enabled()
        win.activate_action("win.cancel-job")            # Esc / the big red button: one click
        assert await job is Outcome.CANCELLED
    run(scenario())
    assert client.stops == 1 and client.sessions_closed == 1
    assert win.job_view._done_title.get_text() == "Job cancelled"
    assert not win.lookup_action("cancel-job").get_enabled() and not win.scene.locked


def test_job_runs_to_the_finished_view(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(), win.send_panel.tool_settings(), True)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: win.runner is not None and win.runner.holding)
        gate_row = next(row for row, step in zip(win.job_view._rows, win.runner.steps) if step.confirm)
        gate_row.cut_button.emit("clicked")              # the outline-confirm Cut
        assert await job is Outcome.DONE
    run(scenario())
    assert client.stops == 0 and client.ended == 1
    assert win.job_view._done_title.get_text() == "Cut complete"
    win.job_view.emit("done")
    assert win.side_stack.get_visible_child_name() == "panels"


def test_send_flow_through_both_sheets(make, run, svg):
    """Load Media, then the Send sheet, answered with their buttons. Closing an Adw.Dialog from
    its own "closed" handler aborts libadwaita, so this also guards against that."""
    client = FakeDaemonClient()
    client.set_status("2")
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: win.get_visible_dialog() is not None)
        media = win.get_visible_dialog()
        assert not media._continue.get_sensitive()
        client.set_status("0")
        await win.monitor.refresh()
        assert media._continue.get_sensitive()
        media._continue.emit("clicked")
        await _until(lambda: win.get_visible_dialog() not in (None, media))
        sheet = win.get_visible_dialog()
        assert sheet._send.get_sensitive() and sheet.get_default_widget() is None
        sheet._send.emit("clicked")
        await _until(lambda: win.runner is not None and win.runner.holding)
        await win.runner.cancel()
        await flow
    run(scenario())
    assert len(client.opened) == 1 and client.stops == 1 and win.get_visible_dialog() is None


def test_closing_the_send_sheet_sends_nothing(make, run, svg):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: win.get_visible_dialog() is not None)
        win.get_visible_dialog().close()                 # Escape or the close button
        await flow
    run(scenario())
    assert client.opened == [] and win.runner is None


def test_test_cut_runs_without_the_send_sheet(make, run):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.send_flow(test_cut=True))                    # machine Ready: no sheet is needed
    spec = client.opened[0]
    assert spec["preview_outline"] is False and len(spec["groups"][0]["paths"]) == 2
    assert win.runner.outcome is Outcome.DONE
    assert win.side_stack.get_visible_child_name() == "panels"


def test_lost_connection_asks_with_the_power_hint(make, run, svg, monkeypatch):
    client = FakeDaemonClient()
    client.fail_at, client.fail = 0, ConnectionError("the daemon closed the connection")
    asked = []

    async def fake_dialog(parent):
        asked.append(parent)
        return False
    monkeypatch.setattr(window_mod.dialogs, "connection_lost", fake_dialog)
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(), win.send_panel.tool_settings(), False)
    assert run(win.run_job(spec.to_dict())) is Outcome.LOST
    assert asked == [win]
    assert win.job_view._done_title.get_text() == "Connection lost"


def test_units_follow_preferences(make, run, svg):
    win = make(FakeDaemonClient())
    run(win.open_path(svg))
    win.settings.units = "mm"
    win.apply_settings()
    assert win.entries["X"].get_text() == "30.0" and win.unit_label.get_text() == "mm"
    assert win.scene.media_label == "Letter · 216 × 279 mm"


def test_the_12_by_12_in_caption_does_not_say_its_size_twice(make):
    win = make(FakeDaemonClient())
    win.page_setup.select_media("12x12")
    names = win.page_setup._media_list
    assert win.scene.media_label == "12 × 12 in"               # not "12 × 12 in · 12 × 12 in"
    assert [names.get_string(i) for i in range(names.get_n_items())] == [
        "Letter · 8.5 × 11 in", "A4 · 8.27 × 11.69 in", "12 × 12 in", "Custom…"]
    win.settings.units = "mm"
    win.apply_settings()
    assert win.scene.media_label == "12 × 12 in · 305 × 305 mm"
    assert names.get_string(2) == "12 × 12 in · 305 × 305 mm"


def _widgets(root):
    """``root`` and every widget below it."""
    if isinstance(root, Adw.Dialog):          # a dialog that is not shown keeps its child aside
        root = root.get_child()
    out, todo = [], [root]
    while todo:
        widget = todo.pop()
        out.append(widget)
        child = widget.get_first_child()
        while child is not None:
            todo.append(child)
            child = child.get_next_sibling()
    return out


def _texts(root) -> list[str]:
    return [w.get_label() if isinstance(w, Gtk.Button) else w.get_text()
            for w in _widgets(root) if isinstance(w, (Gtk.Label, Gtk.Button))
            and (w.get_label() if isinstance(w, Gtk.Button) else w.get_text())]


def _menu_labels(model) -> list[str]:
    labels = []
    for i in range(model.get_n_items()):
        text = model.get_item_attribute_value(i, "label", GLib.VariantType("s"))
        if text is not None:
            labels.append(text.get_string())
        for link in ("section", "submenu"):
            sub = model.get_item_link(i, link)
            if sub is not None:
                labels += _menu_labels(sub)
    return labels


def test_first_run_is_welcome_then_connect():
    from opencutter.gui.firstrun import FirstRunDialog
    dialog = FirstRunDialog()
    stack = dialog._stack
    pages = [stack.get_pages().get_item(i).get_name() for i in range(stack.get_pages().get_n_items())]
    assert pages == ["welcome", "connect"]
    words = " ".join(_texts(dialog)).casefold()
    assert "import" not in words and "installer" not in words and "studio" not in words
    assert "get started" in words and "connect your machine" in words
    start = next(w for w in _widgets(dialog) if isinstance(w, Gtk.Button)
                 and w.get_label() == "Get started")
    start.emit("clicked")
    assert stack.get_visible_child_name() == "connect"
    back = next(w for w in _widgets(dialog) if isinstance(w, Gtk.Button) and w.get_label() == "Back")
    back.emit("clicked")
    assert stack.get_visible_child_name() == "welcome"
    steps = [w for w in _widgets(dialog) if isinstance(w, Gtk.Box) and w.has_css_class("sheet-footer")]
    assert len(steps) == 2


def test_nothing_offers_a_preset_import(make):
    from opencutter.gui.dialogs import PreferencesDialog
    from opencutter.gui.firstrun import FirstRunDialog
    from opencutter.gui.materials import MaterialPicker
    from opencutter.gui.sidebar import SendPanel
    win = make(FakeDaemonClient())
    assert win.lookup_action("import-presets") is None
    menus = [w for w in _widgets(win) if isinstance(w, Gtk.MenuButton) and w.get_menu_model()]
    labels = [text for m in menus for text in _menu_labels(m.get_menu_model())]
    assert "Open…" in labels and "Preferences" in labels
    assert not [text for text in labels if "import" in text.casefold()]
    for cls, signal in ((SendPanel, "import-presets"), (MaterialPicker, "import-presets"),
                        (PreferencesDialog, "reimport"), (FirstRunDialog, "presets-imported")):
        assert GObject.signal_lookup(signal, cls) == 0, (cls, signal)
    assert not [t for t in _texts(win.send_panel._picker) if "import" in t.casefold()]


def test_basic_set_note_and_the_source_of_the_presets(tmp_path):
    from opencutter.gui.materials import BASIC_NOTE
    from opencutter.gui.sidebar import SendPanel
    from opencutter.presets.model import file_source
    settings = GuiSettings.load(tmp_path / "gui.json")
    panel = SendPanel(PresetStore(list(BASIC_PRESETS), basic_source()), settings)
    picker = panel._picker
    picker.set_data(panel.store, [], panel.material)
    assert picker._basic.get_visible() and BASIC_NOTE in _texts(picker._basic)
    assert "Speed, Force and Blade Depth in Cut Settings" in BASIC_NOTE
    assert panel.material == "Vinyl" and panel._source.get_text() == "Vinyl · Basic set"

    # the long note wraps inside the list's width instead of asking for a screen-wide popover
    natural_width = picker.get_child().measure(Gtk.Orientation.HORIZONTAL, -1)[1]
    assert 320 <= natural_width <= 420, natural_width

    mine = PresetStore(list(BASIC_PRESETS), file_source("My own cutter settings"))
    panel = SendPanel(mine, settings)
    panel._picker.set_data(mine, [], panel.material)
    assert not panel._picker._basic.get_visible()
    assert panel._source.get_text() == "Vinyl · My own cutter settings"


@pytest.mark.parametrize("description, shown", [(None, "Basic set"), ("", "Presets file"),
                                                ("Shop list, May", "Shop list, May")])
def test_preferences_show_the_source_of_the_presets(tmp_path, description, shown):
    from opencutter.gui.dialogs import PreferencesDialog
    from opencutter.gui.machine import MachineState
    from opencutter.presets.model import file_source
    source = basic_source() if description is None else file_source(description)
    prefs = PreferencesDialog(GuiSettings.load(tmp_path / "gui.json"),
                              PresetStore(list(BASIC_PRESETS), source), MachineState())
    rows = {w.get_title(): w.get_subtitle() for w in _widgets(prefs) if isinstance(w, Adw.ActionRow)}
    assert rows["Source"] == shown and rows["Materials"] == "2"
    assert not [w for w in _widgets(prefs) if isinstance(w, Gtk.Button)
                and "import" in (w.get_label() or "").casefold()]
