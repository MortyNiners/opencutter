# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The main window for four user requests, with the fake daemon client (no socket at all).

- Align: the Align menu next to Rotate (Center to Page, Align Left/Center/Right/Top/Middle/
  Bottom) moves the design like typing X and Y, inside the usable area of the media.
- No unverified material is chosen for the user: with none, Send and Test Cut say
  "Choose a material first." and nothing can be sent.
- Why it cannot connect: the banner, the connection popover and the first run say it.
- The Blade Depth with the material: the material list shows what picking a material applies,
  and the Cut Settings and the Send sheet show the same Blade Depth.

Needs a display: run headless with GDK_BACKEND=broadway and a private gtk4-broadwayd. The
module is skipped when GTK cannot open one.
"""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

import stubs

stubs.install()
import opencutter.gui  # noqa: E402,F401  (selects the GTK and libadwaita versions)
from gi.events import GLibEventLoop  # noqa: E402
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("GTK cannot open a display", allow_module_level=True)
Adw.init()

from opencutter.gui import app as app_mod, sending, window as window_mod  # noqa: E402
from opencutter.gui.connection import failure_text  # noqa: E402
from opencutter.gui.firstrun import FirstRunDialog  # noqa: E402
from opencutter.gui.geometry import (ALIGNMENTS, align_rect, format_length, usable_area)  # noqa: E402
from opencutter.gui.machine import (ANOTHER_PROGRAM, ANOTHER_PROGRAM_AT_START,  # noqa: E402
                                    CONNECT_GRACE_SECONDS, SWITCH_OFF_ON, Link, MachineState)
from opencutter.gui.runner import Outcome  # noqa: E402
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.gui.window import ALIGN_ICON, CENTER_TO_PAGE_ACCEL, MainWindow  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from gui_fakes import FakeDaemonClient, imported_store  # noqa: E402

IN = 25.4
# a 100 x 30 mm design (a rectangle and a triangle) at x 30, y 40 on a Letter page
DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="8.5in" height="11in" viewBox="0 0 215.9 279.4"
  fill="none" stroke="#000"><rect x="30" y="40" width="50" height="30"/>
  <path d="M100 40 L130 70 L100 70 Z"/></svg>"""
WAITING = {"ok": True, "connected": False, "status": None, "motion": None, "device": None}
LEGACY = ("the legacy prototype daemon holds cameo4.lock; stop it first "
          "(lock /tmp/oct-x/cameo4.lock is held)")


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

    def factory(client: FakeDaemonClient, store: PresetStore | None = None, **settings_kw) -> MainWindow:
        settings = GuiSettings.load(tmp_path / "gui.json")
        settings.preview_outline = False
        for key, value in settings_kw.items():
            setattr(settings, key, value)
        store = store or PresetStore(list(BASIC_PRESETS), basic_source())
        win = MainWindow(None, client, store, settings)
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


def _widgets(root):
    if isinstance(root, Adw.Dialog):
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


def _menu_items(model) -> list[tuple[str, str]]:
    """(label, detailed action) of every item, sections flattened."""
    items = []
    for i in range(model.get_n_items()):
        text = model.get_item_attribute_value(i, "label", GLib.VariantType("s"))
        action = model.get_item_attribute_value(i, "action", GLib.VariantType("s"))
        target = model.get_item_attribute_value(i, "target", None)
        if text is not None:
            detailed = action.get_string() + (f"::{target.get_string()}" if target else "")
            items.append((text.get_string(), detailed))
        section = model.get_item_link(i, "section")
        if section is not None:
            items += _menu_items(section)
    return items


def _activate(win: MainWindow, detailed: str) -> None:
    """Activate a menu item's action the way the menu does: from its detailed name."""
    _ok, name, target = Gio.Action.parse_detailed_name(detailed)
    assert win.activate_action(name, target), detailed


def _entries(win: MainWindow) -> dict[str, str]:
    return {name: entry.get_text() for name, entry in win.entries.items()}


# --- Align -----------------------------------------------------------------------------------
def test_align_button_sits_after_rotate_with_the_menu(make):
    win = make(FakeDaemonClient())
    bar = win.placement_bar
    children = []
    child = bar.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()
    rotate = next(w for w in children if isinstance(w, Gtk.Button)
                  and w.get_action_name() == "win.rotate")
    assert children[children.index(rotate) + 1] is win.align_button
    button = win.align_button
    assert button.get_tooltip_text() == "Align" and button.get_icon_name() == ALIGN_ICON
    assert button.get_always_show_arrow() and button.has_css_class("flat")
    assert _menu_items(button.get_menu_model()) == [
        (label, f"win.align::{key}") for key, label in ALIGNMENTS]
    assert button.get_menu_model().get_n_items() == 3          # Page | horizontal | vertical


def test_the_align_icon_is_found_on_the_apps_icon_path():
    """The icons are package data (gui/icons), so an installed copy (pipx, the Flatpak) finds
    them as well as a checkout does."""
    assert app_mod.ICONS == Path(app_mod.__file__).with_name("icons")
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    theme.add_search_path(str(app_mod.ICONS))                   # what the app does at start-up
    assert ALIGN_ICON.endswith("-symbolic") and theme.has_icon(ALIGN_ICON)
    icon = theme.lookup_icon(ALIGN_ICON, None, 16, 1, Gtk.TextDirection.NONE, 0)
    assert icon.get_file().get_path().startswith(str(app_mod.ICONS))


@pytest.mark.parametrize("key, label", ALIGNMENTS)
def test_each_menu_item_moves_the_design_and_updates_x_and_y(make, run, svg, key, label):
    client = FakeDaemonClient()
    win = make(client, media="custom", custom_media_mm=[12 * IN, 5 * IN])
    run(win.monitor.refresh())
    run(win.open_path(svg))
    win.doc.move_to(250.0, 100.0)                  # partly off the 12 x 5 in sheet
    win._sync_placement()
    assert win.area_banner.get_revealed() and not win.send_panel.send_button.get_sensitive()
    before = win.doc.bbox()
    media = win.page_setup.media_rect()
    detailed = dict((lbl, act) for lbl, act in _menu_items(win.align_button.get_menu_model()))[label]
    _activate(win, detailed)
    x, y = align_rect(before, usable_area(media), key, page=media)
    assert win.doc.bbox() == (x, y, before[2], before[3])
    assert _entries(win)["X"] == format_length(x, "in") and _entries(win)["Y"] == format_length(y, "in")
    assert win.modified_dot.get_visible()
    if key == "center-page":                       # now inside: the warning goes, Send is enabled
        assert not win.area_banner.get_revealed() and win.send_panel.send_button.get_sensitive()
        assert win.lookup_action("send").get_enabled()


def test_center_to_page_on_the_default_mat_and_on_a_12_by_5_in_sheet(make, run, svg):
    win = make(FakeDaemonClient(), media="12x12")
    run(win.monitor.refresh())
    run(win.open_path(svg))
    assert _entries(win) == {"X": "1.18", "Y": "1.57", "W": "3.94", "H": "1.18"}
    _activate(win, "win.align::center-page")
    assert _entries(win) == {"X": "4.03", "Y": "5.41", "W": "3.94", "H": "1.18"}
    x, y, w, h = win.doc.bbox()
    assert (x + w / 2, y + h / 2) == pytest.approx((6 * IN, 6 * IN))

    page = win.page_setup                          # Page Setup: Custom, 12 x 5 in (typed, no Enter)
    page.select_media("custom")
    page._custom_w.set_text("12")
    page._custom_h.set_text("5")
    _activate(win, "win.align::center-page")       # the typed size counts, as it would for Send
    assert win.page_setup.media_rect() == pytest.approx((0, 0, 12 * IN, 5 * IN))
    assert _entries(win)["X"] == "4.03" and _entries(win)["Y"] == "1.91"
    x, y, w, h = win.doc.bbox()
    assert (x + w / 2, y + h / 2) == pytest.approx((6 * IN, 2.5 * IN))   # the sheet's centre
    assert win.send_panel.send_button.get_sensitive()


def test_align_takes_a_typed_width_first(make, run, svg):
    win = make(FakeDaemonClient(), media="12x12", units="mm")
    run(win.monitor.refresh())
    run(win.open_path(svg))
    win.entries["W"].set_text("200")               # typed, not confirmed with Enter
    _activate(win, "win.align::center")
    x, _y, w, _h = win.doc.bbox()
    assert w == pytest.approx(200) and x + w / 2 == pytest.approx(6 * IN)
    assert _entries(win)["W"] == "200.0"


def test_align_is_disabled_without_a_design_and_while_a_job_runs(make, run, svg):
    client = FakeDaemonClient()
    win = make(client, media="12x12")
    run(win.monitor.refresh())
    assert not win.lookup_action("align").get_enabled()
    assert not win.placement_bar.get_sensitive()
    win.align_design("center-page")                # nothing to align: nothing happens
    run(win.open_path(svg))
    assert win.lookup_action("align").get_enabled() and win.placement_bar.get_sensitive()

    release = asyncio.Event()
    settings = win.send_panel.tool_settings()
    spec = window_mod.job_spec(win.doc, win.page_setup.media_rect(), settings, False)
    client.hold_step = (2, release)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(spec.to_dict()))
        await _until(lambda: 2 in client.steps_started)
        before = win.doc.bbox()
        assert not win.lookup_action("align").get_enabled()
        assert not win.placement_bar.get_sensitive()
        win.activate_action("win.align", GLib.Variant("s", "center-page"))
        win.align_design("right")                  # even when called directly
        assert win.doc.bbox() == before
        release.set()
        assert await asyncio.wait_for(job, 5) is Outcome.DONE
    run(scenario())
    assert win.lookup_action("align").get_enabled()
    win.align_design("no-such-alignment")          # an unknown key moves nothing
    assert _entries(win)["X"] == "1.18"


def test_center_to_page_has_a_shortcut_listed_in_keyboard_shortcuts():
    assert app_mod.ACCELS["win.align::center-page"] == [CENTER_TO_PAGE_ACCEL] == ["<Control><Shift>c"]
    others = [accel for action, accels in app_mod.ACCELS.items() if action != "win.align::center-page"
              for accel in accels]
    parsed = [Gtk.accelerator_parse(a) for a in others]
    assert Gtk.accelerator_parse(CENTER_TO_PAGE_ACCEL) not in parsed
    app = app_mod.OpenCutterApp()                  # never registered or run
    assert app.get_accels_for_action("win.align::center-page") == ["<Shift><Control>c"]
    section = window_mod._shortcuts_section()        # what the Keyboard Shortcuts dialog lists
    items = {section.get_item(i).get_title(): section.get_item(i).get_accelerator()
             for i in range(section.get_n_items())}
    assert items["Center to Page"] == CENTER_TO_PAGE_ACCEL
    assert "Nudge the design by 0.1 in (Shift: 1 in)" in items     # the placement shortcuts
    assert isinstance(window_mod._shortcuts_dialog(), Adw.ShortcutsDialog)


# --- no unverified material is chosen for the user -----------------------------------------
def test_an_imported_store_without_verified_rows_chooses_nothing(make, run, svg, monkeypatch):
    client = FakeDaemonClient()
    win = make(client, store=imported_store())
    run(win.monitor.refresh())
    panel = win.send_panel
    # at start-up, with no design open yet, the reason under Send is already the material
    assert panel._why.get_visible() and panel._why.get_text() == "Choose a material first."
    assert win.header_send.get_tooltip_text() == "Choose a material first."
    run(win.open_path(svg))
    assert panel.material is None and panel.action is None and panel.preset() is None
    assert panel._material_label.get_text() == "Choose a material"
    assert panel._material_label.has_css_class("dim-label")
    assert not panel._action.get_sensitive() and panel._actions.get_n_items() == 0
    assert not any(card.get_sensitive() for card in panel._cards.values())
    assert not panel._settings_toggle.get_sensitive() and panel._summary.get_text() == ""
    assert not panel.send_button.get_sensitive() and not panel.test_cut.get_sensitive()
    assert panel._why.get_visible() and panel._why.get_text() == "Choose a material first."
    assert panel.send_button.get_tooltip_text() == "Choose a material first."
    assert panel.test_cut.get_tooltip_text() == "Choose a material first."
    assert win.header_send.get_tooltip_text() == "Choose a material first."
    assert not win.lookup_action("send").get_enabled()
    assert not win.lookup_action("test-cut").get_enabled()
    assert win.placement_info.get_text() == "2 paths · No material"

    shown = []
    monkeypatch.setattr(window_mod.dialogs, "show_message", lambda *a: shown.append(a))
    assert win._prepare(False) is None and win._prepare(True) is None
    run(win.send_flow())                           # e.g. from a stale shortcut: nothing at all
    run(win.send_flow(test_cut=True))
    assert win.get_visible_dialog() is None and not shown
    spec = sending.spec_for_test_cut(win.page_setup.media_rect(), BASIC_PRESETS[0].to_tool_settings())
    assert run(win.run_job(spec.to_dict(), test_cut=True)) is Outcome.FAILED
    assert client.opened == [] and client.steps_started == []


def test_picking_a_material_clears_the_block(make, run, svg):
    win = make(FakeDaemonClient(), store=imported_store())
    run(win.monitor.refresh())
    run(win.open_path(svg))
    panel = win.send_panel
    panel._picker.set_data(panel.store, [], None, panel.picker_line)
    panel._picker.emit("picked", "Test Vinyl, Glossy")
    assert panel.material == "Test Vinyl, Glossy" and panel.action == "Cut"
    assert not panel._material_label.has_css_class("dim-label")
    assert panel.send_button.get_sensitive() and panel.test_cut.get_sensitive()
    assert not panel._why.get_visible() and win.lookup_action("send").get_enabled()
    assert win.settings.material == "Test Vinyl, Glossy"          # remembered for the next start


def test_enter_in_the_empty_material_search_picks_nothing(make, run, tmp_path):
    """The search field has the focus as the Material list opens: a stray Enter (or Enter after
    typing and clearing a search) must not pick the first row, an untested preset."""
    win = make(FakeDaemonClient(), store=imported_store())
    run(win.monitor.refresh())
    panel = win.send_panel
    picker, search = panel._picker, panel._picker._search
    picker.set_data(panel.store, [], None, panel.picker_line)    # the list as it opens
    for typed in ("", "card", "   "):
        search.set_text(typed)
        search.set_text("")                        # typed and cleared, or nothing at all
        search.emit("activate")
    search.set_text("   ")
    search.emit("activate")
    assert panel.material is None and win.settings.material is None
    assert not (tmp_path / "gui.json").exists()    # nothing remembered for the next start
    assert not win.lookup_action("test-cut").get_enabled()
    assert panel.test_cut.get_tooltip_text() == "Choose a material first."
    search.set_text("glossy vinyl")                # a search: Enter picks its first match,
    search.emit("activate")                        # even before the search delay ran out
    assert panel.material == "Test Vinyl, Glossy"

    basic = make(FakeDaemonClient()).send_panel    # the Basic set keeps its verified vinyl
    basic._picker.set_data(basic.store, [], basic.material, basic.picker_line)
    basic._picker._search.emit("activate")
    assert basic.material == "Vinyl"


def test_a_verified_row_is_chosen_with_its_own_action_and_tool(make):
    store = imported_store()
    score = next(p for p in store.presets if p.material == "Test Card, Smooth" and p.action == "Score")
    store.presets[store.presets.index(score)] = replace(score, verified=True)
    win = make(FakeDaemonClient(), store=store)
    panel = win.send_panel
    assert (panel.material, panel.action, panel.holder) == ("Test Card, Smooth", "Score", 2)
    assert panel.preset().verified and panel.tool_check() is not None


def test_a_remembered_material_is_kept(make):
    win = make(FakeDaemonClient(), store=imported_store(), material="Test Felt")
    assert win.send_panel.material == "Test Felt"
    gone = make(FakeDaemonClient(), store=imported_store(), material="Not In This Store")
    assert gone.send_panel.material is None


def test_the_basic_set_still_opens_on_its_vinyl(make):
    panel = make(FakeDaemonClient()).send_panel
    assert (panel.material, panel.action, panel.holder) == ("Vinyl", "Cut", 1)
    assert panel.summary_text() == "Blade Depth 1 · Force 10 · Speed 5 · 1 pass"
    assert panel._summary.get_tooltip_text() == "Blade Depth 1 = 0.1 mm"
    assert panel._sliders["blade_depth"].value.get_tooltip_text() == "0.1 mm"
    panel.select_material("Sketch Pen")
    assert panel.summary_text() == "Force 7 · Speed 5 · 1 pass"
    assert panel._summary.get_tooltip_text() is None


# --- the settings line in the material list ------------------------------------------------
@pytest.mark.parametrize("which", ["basic", "imported"])
@pytest.mark.parametrize("tools", [(2, 4), (7, 4), (1, 4)])
def test_the_material_list_shows_what_picking_applies(make, run, which, tools):
    client = FakeDaemonClient()
    client.set_tools(f" {tools[0]}, {tools[1]}")
    store = imported_store() if which == "imported" else None
    win = make(client, store=store)
    run(win.monitor.refresh())
    panel = win.send_panel
    class Opened:                                  # the Material button as the list opens
        @staticmethod
        def get_active():
            return True
    for material in panel.store.materials():
        panel._on_picker_open(Opened(), None)       # fills the list as a click on Material does
        line = panel._picker.line(material)
        panel._picker.emit("picked", material)
        applied = panel.preset()
        assert applied is not None and line == sending.preset_line(applied), material
        settings = panel.tool_settings()
        assert settings == applied.to_tool_settings()
        summary = panel.summary_text(with_passes=False).split(" · ")[:-1]     # without Accel.
        assert line.split(" · ")[2:2 + len(summary)] == summary, (line, summary)


def test_material_rows_have_the_line_and_a_full_tooltip(make):
    win = make(FakeDaemonClient(), store=imported_store())
    panel = win.send_panel
    picker = panel._picker
    picker.set_data(panel.store, [], None, panel.picker_line)
    rows = [w for w in _widgets(picker) if isinstance(w, Gtk.ListBoxRow)]
    assert len(rows) == len(panel.store.materials())
    for row in rows:
        line = picker.line(row.material)
        assert row.line == line and line
        tooltip = row.get_tooltip_text()
        assert tooltip.startswith(f"{row.material}\n{line}")
        assert (" mm" in tooltip) == ("Blade Depth" in line)
        assert row._line.get_ellipsize() is not None and row._line.has_css_class("dim-label")
    vinyl = next(r for r in rows if r.material == "Test Vinyl, Glossy")
    assert vinyl.line == "Cut · AutoBlade · Blade Depth 5 · Force 33 · Speed 7"
    assert vinyl.get_tooltip_text().endswith("Blade Depth 5 = 0.5 mm")
    # search and the Basic-set note are unchanged
    picker._search.set_text("vinyl")
    picker._on_search(picker._search)
    assert {r.material for r in rows if r.get_child_visible() and picker._filter(r)} == \
        {m for m in panel.store.materials() if "Vinyl" in m}
    assert not picker._basic.get_visible()


def test_the_send_sheet_shows_the_same_blade_depth(make, run, svg, monkeypatch):
    seen = []

    class Sheet:
        def __init__(self, summary, scene, checks):
            seen.append(summary)

        def update(self, state):
            pass

        def connect(self, *args):
            pass

        async def ask(self, parent):
            return False
    monkeypatch.setattr(window_mod, "SendDialog", Sheet)
    win = make(FakeDaemonClient())
    run(win.monitor.refresh())
    run(win.open_path(svg))
    run(win.send_flow())
    [summary] = seen
    assert summary.settings == "Blade Depth 1 · Force 10 · Speed 5 · Accel. 3"
    assert summary.settings_tooltip == "Blade Depth 1 = 0.1 mm"
    from opencutter.gui.sheets import SendDialog, SendSummary
    real = SendDialog(SendSummary(**{**summary.__dict__}), win.scene, lambda state: [])
    value = next(w for w in _widgets(real) if isinstance(w, Gtk.Label)
                 and w.get_text() == summary.settings)
    assert value.get_tooltip_text() == f"{summary.settings}\nBlade Depth 1 = 0.1 mm"


# --- why it cannot connect -----------------------------------------------------------------
def test_banner_and_popover_say_another_program_holds_the_link(make, run):
    win = make(FakeDaemonClient(ping={**WAITING, "reason": "held-elsewhere"}))
    run(win.monitor.refresh())
    assert win.monitor.state.link is Link.FAILED and win.pill.text == "Could not Connect"
    assert win.banner.get_revealed() and win.banner.get_title() == failure_text(ANOTHER_PROGRAM)
    popover = win.connection
    assert popover._alert.get_visible() and ANOTHER_PROGRAM in popover._alert_text.get_text()
    assert popover._connect.get_visible() and popover._connect.get_label() == "Try again"


def test_banner_for_a_daemon_that_could_not_start(make, run):
    client = FakeDaemonClient(ping=None)
    client.spawn_error = LEGACY
    win = make(client)
    run(win.monitor.connect())
    assert client.daemon_started == 1
    assert win.banner.get_revealed() and win.banner.get_title() == failure_text(ANOTHER_PROGRAM_AT_START)
    assert win.banner.get_button_label() == "Try again"            # what the text says to press
    assert win.status_label.get_text() == "Status: Could not Connect"
    client.spawn_error = None                      # Try again: this time it starts
    from gui_fakes import READY_PING
    client.ping_reply = dict(READY_PING)

    async def try_again():
        win.banner.emit("button-clicked")
        await _until(lambda: win.monitor.state.connected)
    run(try_again())
    assert client.daemon_started == 2 and not win.banner.get_revealed()


def test_an_unreachable_machine_then_the_generic_text(make, run):
    clock = [0.0]
    client = FakeDaemonClient(ping={**WAITING, "reason": "unreachable"})
    win = make(client)
    win.monitor._clock = lambda: clock[0]
    run(win.monitor.refresh())
    assert win.monitor.state.link is Link.CONNECTING and not win.banner.get_revealed()
    clock[0] += CONNECT_GRACE_SECONDS + 1
    run(win.monitor.refresh())
    assert win.banner.get_revealed() and win.banner.get_title() == failure_text(SWITCH_OFF_ON)
    client.ping_reply = dict(WAITING)              # an older daemon without the reason field
    run(win.monitor.refresh())
    assert win.banner.get_title() == failure_text(None)
    assert win.banner.get_title().startswith("Could not Connect. Make sure the CAMEO 4")


def test_the_first_run_says_why_too():
    dialog = FirstRunDialog()
    dialog.update_machine(MachineState(link=Link.FAILED, problem=ANOTHER_PROGRAM))
    assert dialog._problem.get_visible() and dialog._problem.get_text() == ANOTHER_PROGRAM
    assert dialog._connect.get_label() == "Try again"
    dialog.update_machine(MachineState())
    assert not dialog._problem.get_visible() and dialog._connect.get_label() == "Connect"


def test_every_could_not_connect_text_fits_the_smallest_window(make, run, tmp_path):
    """At the window's minimum size (760 x 560) the header with "Could not Connect", the banner
    with its button below a text of up to three lines, the canvas, the sidebar and the status
    bar all fit, also with a long file name: nothing is clipped and the window need not grow."""
    from opencutter.gui import machine
    display = Gdk.Display.get_default()
    provider = app_mod.load_style(display)          # the app's own sizes: pills, paddings
    gtk_settings = Gtk.Settings.get_default()
    animations = gtk_settings.get_property("gtk-enable-animations")
    gtk_settings.set_property("gtk-enable-animations", False)       # the banner shows at once
    try:
        win = make(FakeDaemonClient(ping=None), store=imported_store())
        assert win.get_size_request() == (760, 560)
        win.set_default_size(760, 560)
        win.present()
        content = win.get_content()
        statusbar = win.status_label.get_parent()
        long_name = tmp_path / "a-rather-long-file-name-for-my-vinyl-sticker-final-v2.svg"
        long_name.write_text(DESIGN)
        problems = [None, machine.ANOTHER_PROGRAM_AT_START, machine.SERVICE_BUSY,
                    machine.SERVICE_NOT_STARTED, machine.startup_problem("RuntimeError: x"),
                    machine.ANOTHER_VERSION, machine.OLDER_SERVICE, machine.SERVICE_NOT_REPLACED,
                    machine.SERVICE_NOT_STOPPED, machine.APP_OUTDATED, machine.SERVICE_ENDING]
        for opened in (False, True):
            if opened:
                run(win.open_path(str(long_name)))
            for problem in problems:
                win._on_machine(MachineState(link=Link.FAILED, problem=problem))
                run(asyncio.sleep(0.15))           # laid out at 760 x 560 (the narrow layout)
                where = (opened, problem)
                assert win.get_width() == 760 and win.get_height() == 560, where
                assert win.banner.get_revealed() and win.split.get_collapsed(), where
                assert content.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= 760, where
                split_min = win.split.measure(Gtk.Orientation.VERTICAL, win.split.get_width())[0]
                assert win.split.get_height() >= split_min, where            # nothing squeezed
                ok, bounds = statusbar.compute_bounds(win)
                assert ok and bounds.get_y() + bounds.get_height() <= 560, where   # nor cut off
        assert win.doc_label.get_tooltip_text() == long_name.name      # shortened in the header
    finally:
        gtk_settings.set_property("gtk-enable-animations", animations)
        Gtk.StyleContext.remove_provider_for_display(display, provider)
