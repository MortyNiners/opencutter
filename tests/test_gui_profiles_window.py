# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The main window for every machine profile, with the fake daemon client (no socket at all).

- Page Setup: the detected machine "(Detected)", else a choice of the machines OpenCutter
  drives; the machine's mats, one that may not be used listed but not chosen; media clamped to
  the mat.
- A detected machine of another profile switches the layout, keeps the design and shows a
  banner; Send waits for "Use {name}", also in the Send sheet, and a switch while Load Media
  or a job is open sends nothing for the other machine. Jobs carry the layout's machine and
  mat; the offline choice never overrides a connected machine.
- The canvas draws the layout's mat, also a non-square one.
- The Send panel: one Tool card per holder, the machine's slider ranges, no Acceleration row on
  a machine without one, and Cut Settings for a machine without materials.
- The connection popover, the status bar, the Send sheet, the job view, Preferences, About and
  the first run name the machine, and say nothing about which models were tested; recognized
  ones get their name and plain reason.

The CAMEO 4 window itself is checked, unchanged, by the other window tests. Needs a display:
run headless with GDK_BACKEND=broadway and a private gtk4-broadwayd; the module is skipped
when GTK cannot open one. Nothing here touches Bluetooth.
"""
import asyncio
import logging
from dataclasses import replace

import pytest

import stubs

stubs.install()
import opencutter.gui  # noqa: E402,F401  (selects the GTK and libadwaita versions)
from gi.events import GLibEventLoop  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("GTK cannot open a display", allow_module_level=True)
Adw.init()

from opencutter.gui import canvas, dialogs, geometry, sending  # noqa: E402
from opencutter.gui import window as window_mod  # noqa: E402
from opencutter.gui.connection import failure_text  # noqa: E402
from opencutter.gui.dialogs import PreferencesDialog  # noqa: E402
from opencutter.gui.firstrun import FirstRunDialog  # noqa: E402
from opencutter.gui.machine import (NOT_SUPPORTED, OTHER_MACHINE_HINT, UNSUPPORTED,  # noqa: E402
                                    WRONG_MODEL, WRONG_MODEL_UNKNOWN, Link, MachineState,
                                    machine_choices, released)
from opencutter.gui.materials import BASIC_NOTE  # noqa: E402
from opencutter.gui.runner import Outcome  # noqa: E402
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.gui.sheets import LoadMediaDialog, SendDialog  # noqa: E402
from opencutter.gui.window import MainWindow  # noqa: E402
from opencutter.machines import (CAMEO4, FirmwareLine, Identity, Support, UBasis,  # noqa: E402
                                 by_id, enabled, recognized_models, registry, safety)
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from gui_fakes import (CAMEO3_REASON, RECOGNIZED_FIELDS, RECOGNIZED_PING,  # noqa: E402
                       UNITS_UNCHECKED, WRONG_MODEL_PING, FakeDaemonClient)

IN = 25.4
CAMEO5 = by_id("cameo5")
PORTRAIT3 = by_id("portrait3")
PORTRAIT4 = by_id("portrait4")
# a 100 x 30 mm design (a rectangle and a triangle) at x 30, y 40 on a Letter page
DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="8.5in" height="11in" viewBox="0 0 215.9 279.4"
  fill="none" stroke="#000"><rect x="30" y="40" width="50" height="30"/>
  <path d="M100 40 L130 70 L100 70 Z"/></svg>"""
# A tested machine made up for these tests only: the CAMEO 4 with a 12 x 24 in mat whose U
# reply is not known, so that mat may not be used on it (Mat.offered)
WIDE = replace(CAMEO5.mat("12x24"), expected_u=None, u_basis=UBasis.UNKNOWN, basis="test only")
TEST_CUTTER = replace(CAMEO4, id="test_cutter", name="Test Cutter",
                      identity=Identity(fg_models=frozenset({"TEST CUTTER"}),
                                        lines=(FirmwareLine(5, Support.TESTED, "test only"),)),
                      mats=(*CAMEO4.mats, WIDE), programs=frozenset(), basis="test only")


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

    def factory(client: FakeDaemonClient, machine: str | None = None,
                store: PresetStore | None = None) -> MainWindow:
        settings = GuiSettings.load(tmp_path / "gui.json")
        settings.preview_outline = False
        if machine is not None:
            settings.set_machine(machine)
        store = store or PresetStore(list(BASIC_PRESETS), basic_source())
        win = MainWindow(None, client, store, settings)
        windows.append(win)
        return win
    yield factory
    for win in windows:
        win.destroy()


@pytest.fixture
def test_cutter(monkeypatch):
    """TEST_CUTTER in the registry, for one test."""
    safety.check_entry(TEST_CUTTER)
    monkeypatch.setattr(registry, "INDEX", registry.Index((*registry.entries(), TEST_CUTTER)))
    return TEST_CUTTER


async def _until(predicate, timeout: float = 10) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def _strings(model: Gtk.StringList) -> list[str]:
    return [model.get_string(i) for i in range(model.get_n_items())]


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


def _shown_texts(root) -> list[str]:
    """What a user can read in ``root`` once it is shown: the text of every label, the title
    and subtitle of every row, and every tooltip, of the widgets that are visible up to
    ``root`` (only the revealed part of a banner or revealer, only a stack's visible page)."""
    if isinstance(root, Adw.Dialog):
        root = root.get_child()
    out, todo = [], [root]
    while todo:
        widget = todo.pop()
        if widget is not root and not widget.get_visible():
            continue
        if isinstance(widget, Adw.Banner) and not widget.get_revealed():
            continue
        if isinstance(widget, Gtk.Revealer) and not widget.get_reveal_child():
            continue
        if isinstance(widget, Gtk.Label):
            out.append(widget.get_text())
        if isinstance(widget, Adw.PreferencesRow):
            out.append(widget.get_title())
        if isinstance(widget, Adw.ActionRow):
            out.append(widget.get_subtitle() or "")
        if isinstance(widget, Adw.Banner):
            out.append(widget.get_title())
        out.append(widget.get_tooltip_text() or "")
        if isinstance(widget, Gtk.Stack):
            page = widget.get_visible_child()
            todo.extend([page] if page is not None else [])
            continue
        child = widget.get_first_child()
        while child is not None:
            todo.append(child)
            child = child.get_next_sibling()
    return [t for t in out if t]


def _about_testing(texts: list[str]) -> list[str]:
    """The texts that say whether a model was tested, verified or works."""
    words = ("tested", "verified", "works with")
    return [t for t in texts if any(w in t.lower() for w in words)]


def _rows(sheet: SendDialog) -> dict[str, bool]:
    rows, child = {}, sheet._list.get_first_child()
    while child is not None:
        rows[child.get_last_child().get_text()] = \
            child.get_first_child().get_icon_name() == "object-select-symbolic"
        child = child.get_next_sibling()
    return rows


def _connected(client: FakeDaemonClient, make, run, profile_id: str, tools: str = " 2, 0",
               use: bool = True) -> MainWindow:
    """A window whose service has a connected machine of ``profile_id`` (the layout follows
    it); with ``use``, the user pressed "Use {name}"."""
    client.set_machine(profile_id, tools=tools)
    win = make(client)
    run(win.monitor.refresh())
    assert win.layout.machine == profile_id
    if use:
        win.machine_banner.emit("button-clicked")
    return win


# --- Page Setup ------------------------------------------------------------------------------------
def test_with_no_machine_connected_page_setup_offers_every_machine_it_drives(make):
    win = make(FakeDaemonClient(ping=None))
    page = win.page_setup
    assert page._machine.get_visible() and not page._machine_name.get_visible()
    assert page._detected.get_text() == "Not detected yet"
    labels = _strings(page._machine_list)
    assert labels == [c.name for c in machine_choices()] == [p.name for p in enabled()]
    assert labels[page._machine.get_selected()] == "CAMEO 4"
    assert "CAMEO 5" in labels and "Portrait 4" in labels and not _about_testing(labels)
    assert not [name for name in labels for m in recognized_models() if name.startswith(m.name + " ")
                or name == m.name]                            # never a model it only recognizes
    # the CAMEO 4's layout is as before: its one mat, its caption, two Tool cards and labels
    assert _strings(page._mat_list) == ["CAMEO 12 × 12 in"] and not page._mat_note.get_visible()
    assert win.scene.mat.key == "12x12" and win.scene.profile is CAMEO4
    assert canvas.mat_caption(win.scene.mat) == "CUTTING MAT 12 × 12 in"
    assert [(n, c.title) for n, c in win.send_panel._cards.items()] == [(1, "Tool 1"), (2, "Tool 2")]
    assert [w.get_text() for w in win.tool_labels.values()] == ["Tool 1: —", "Tool 2: —"]
    assert win.connection.tool_rows() == [("Tool 1", "—"), ("Tool 2", "—")]
    assert not win.machine_banner.get_revealed()
    assert not _about_testing(_shown_texts(win.connection))
    assert win.connection._power_row.get_visible()


def test_choosing_another_machine_with_none_connected(make, tmp_path):
    win = make(FakeDaemonClient(ping=None))
    page = win.page_setup
    page._machine.set_selected(page.machine_choice_ids.index("portrait4"))
    assert win.layout.machine == "portrait4" and win.monitor.choice == "portrait4"
    assert GuiSettings.load(tmp_path / "gui.json").machine == "portrait4"     # remembered
    assert not win.machine_banner.get_revealed()              # chosen, nothing to accept
    # its one mat, the media clamped to it, one Tool card, one status bar label
    assert _strings(page._mat_list) == ["Portrait 8.5 × 12 in"]
    assert page.mat_key == "8.5x12" and win.scene.mat.key == "8.5x12"
    page.select_media("12x12")                                 # a sheet bigger than the mat
    assert page.media_rect() == pytest.approx((0, 0, 8.5 * IN, 12 * IN))
    assert win.scene.media_label == "12 × 12 in · 8.5 × 12 in"
    assert list(win.send_panel._cards) == [1] and win.send_panel._cards[1].title == "Tool"
    assert [w.get_text() for w in win.tool_labels.values()] == ["Tool: —"]
    assert win.connection.tool_rows() == [("Tool", "—")]
    # the texts name it, switched off and on to connect, and say nothing about testing
    assert not _about_testing(_shown_texts(win.connection) + _shown_texts(page))
    assert win.connection._power_row.get_visible() and win.pill._name.get_text() == "Portrait 4"
    # how the CAMEO 5 takes a connection was not seen either: the same advice, and no claim
    page._machine.set_selected(page.machine_choice_ids.index("cameo5"))
    assert win.layout.machine == "cameo5" and win.connection._power_row.get_visible()
    assert win.connection._power_hint.get_text() == (
        "If the app can’t connect to the CAMEO 5, switch the machine off and on.")
    win._on_machine(MachineState(link=Link.FAILED))
    assert win.banner.get_title() == failure_text(
        None, hint="Make sure the CAMEO 5 is switched on and paired. If it still fails, switch "
                   "it off and on again.")
    assert "connects to it by itself" not in win.banner.get_title()


def test_a_mat_the_machine_may_not_use_is_listed_but_cannot_be_chosen(make, test_cutter):
    win = make(FakeDaemonClient(ping=None), machine="test_cutter")
    page = win.page_setup
    assert _strings(page._mat_list) == ["Test 12 × 12 in", "Test 12 × 24 in"]
    usable, wide = page.mat_choices
    assert usable.sensitive and usable.note is None
    assert not wide.sensitive and wide.note == geometry.NOT_MEASURED == (
        "Not measured on this machine yet")
    # the list's rows: dimmed, with the tooltip, and not selectable (the factory's description)
    assert page._describe_mat("Test 12 × 24 in") == (False, "Not measured on this machine yet")
    assert page._describe_mat("Test 12 × 12 in") == (True, None)
    page._mat.set_selected(1)                                  # if it were picked anyway
    assert page._mat.get_selected() == 0 and page.mat_key == "12x12"
    assert win.settings.mat_for(test_cutter) == "12x12" and win.scene.mat.key == "12x12"


def test_a_mat_whose_cut_area_cannot_be_checked_says_so(make, run, svg):
    win = _connected(FakeDaemonClient(), make, run, "portrait4")
    page = win.page_setup
    [choice] = page.mat_choices
    assert choice.sensitive and choice.note == geometry.UNCHECKED_MAT
    assert page._mat_note.get_visible() and page._mat_note.get_text() == geometry.UNCHECKED_MAT
    assert "cannot be checked" in geometry.UNCHECKED_MAT
    # and the Send sheet notes it, and only that (it does not block Send)
    run(win.open_path(svg))
    win.send_panel.use_cut_settings()

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: isinstance(win.get_visible_dialog(), SendDialog))
        sheet = win.get_visible_dialog()
        assert sheet.notes == [geometry.UNCHECKED_MAT]
        assert sheet._send.get_sensitive()
        sheet.close()
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert win.client.opened == []


def test_the_media_is_clamped_to_a_non_square_mat(make):
    win = make(FakeDaemonClient(ping=None), machine="cameo5")
    page = win.page_setup
    assert _strings(page._mat_list) == ["CAMEO 12 × 12 in", "CAMEO 12 × 24 in"]
    page._mat.set_selected(1)
    assert page.mat_key == "12x24" and win.settings.mat_for(CAMEO5) == "12x24"
    assert win.scene.mat == CAMEO5.mat("12x24") and win.canvas._fit
    assert page._media_keys == ["letter", "a4", "12x24", "custom"]
    page.select_media("12x24")
    assert page.media_rect() == pytest.approx((0, 0, 12 * IN, 24 * IN))
    assert win.scene.media_label == "12 × 24 in"
    win.settings.landscape = True                              # 24 in wide: clamped to 12 in
    win._on_page_setup()
    assert page.media_rect() == pytest.approx((0, 0, 12 * IN, 12 * IN))


def test_another_mat_refits_the_canvas_and_preferences_choose_it(make, monkeypatch):
    win = make(FakeDaemonClient(ping=None), machine="cameo5")
    fits = []
    monkeypatch.setattr(win.canvas, "fit", lambda: fits.append(win.scene.mat.key))
    win.page_setup._mat.set_selected(1)
    assert fits == ["12x24"]
    win._on_page_setup()                                       # the same mat: no new fit
    assert fits == ["12x24"]
    # Preferences' Default Cutting Mat is the layout's mat
    prefs = PreferencesDialog(win.settings, win.store, win.monitor.state)
    prefs.connect("changed", lambda *_: win.apply_settings())
    assert _strings(prefs.mat_row.get_model()) == ["CAMEO 12 × 12 in", "CAMEO 12 × 24 in"]
    prefs.mat_row.set_selected(0)
    assert win.page_setup.mat_key == "12x12" and win.scene.mat.key == "12x12"
    assert fits == ["12x24", "12x12"]


def test_jobs_carry_the_layouts_machine_and_mat(make, run, svg):
    win = _connected(FakeDaemonClient(), make, run, "cameo5")
    run(win.open_path(svg))
    win.send_panel.use_cut_settings()
    win.page_setup._mat.set_selected(1)
    spec = win._prepare(False)[0]
    assert (spec.machine, spec.mat) == ("cameo5", "12x24")
    assert sending.plan_summary(spec)[1] is None
    test = win._prepare(True)[0]
    assert (test.machine, test.mat) == ("cameo5", "12x24")


def test_the_offline_choice_never_overrides_a_connected_machine_or_a_running_send(make, run):
    win = make(FakeDaemonClient())
    run(win.monitor.refresh())
    win.choose_machine("cameo5")                               # e.g. a stale dropdown signal
    assert win.layout.machine == "cameo4" and win.layout.pending is None
    assert not win.machine_banner.get_revealed()
    offline = make(FakeDaemonClient(ping=None))
    offline._sending = True                                    # a Send or Test Cut under way
    page = offline.page_setup
    page._machine.set_selected(page.machine_choice_ids.index("cameo5"))
    assert offline.layout.machine == "cameo4" and page.profile is CAMEO4
    assert page.machine_choice_ids[page._machine.get_selected()] == "cameo4"
    offline._sending = False
    page._machine.set_selected(page.machine_choice_ids.index("cameo5"))
    assert offline.layout.machine == "cameo5"


def test_a_connected_machine_whose_profile_was_not_reported_is_not_named_as_detected(make, run):
    """The service did not say which profile it detected: Page Setup names what the machine
    reports itself as, never the layout's machine."""
    client = FakeDaemonClient()

    async def no_fields():
        raise OSError("no answer")
    client.ping_machine = no_fields
    win = make(client, machine="cameo5")
    run(win.monitor.refresh())
    assert win.monitor.state.connected and win.monitor.state.detected is None
    page = win.page_setup
    assert page._machine_name.get_text() == "CAMEO 4 V5.05"
    assert page._detected.get_text() == "(Detected)" and not _about_testing(_shown_texts(page))


def test_the_inhibit_reason_names_the_layouts_machine(make, monkeypatch):
    assert window_mod.JOB_INHIBIT_REASON == "A job is running on the CAMEO 4"
    win = make(FakeDaemonClient(ping=None), machine="portrait4")
    reasons = []

    class App:
        def inhibit(self, _window, _flags, reason):
            reasons.append(reason)
            return 7

        def uninhibit(self, _cookie):
            pass
    monkeypatch.setattr(win, "get_application", lambda: App())
    assert win._inhibit_session() is not None
    assert reasons == ["A job is running on the Portrait 4"]


# --- the canvas -------------------------------------------------------------------------------------
@pytest.mark.parametrize("profile, key, grid, lines", [
    (CAMEO4, "12x12", (12, 12), (23, 23)),
    (CAMEO5, "12x24", (12, 24), (23, 47)),
    (PORTRAIT3, "8x12", (8, 12), (15, 23)),
    (PORTRAIT4, "8.5x12", (8, 12), (16, 23)),                 # no printed grid: every half inch
    (CAMEO4, replace(CAMEO4.mat("12x12"), grid_in=(10, 11)), (10, 11), (19, 21)),   # a smaller grid
])
def test_the_canvas_grid_follows_the_mat(profile, key, grid, lines):
    mat = profile.mat(key) if isinstance(key, str) else key
    size = geometry.mat_size(mat, profile)
    assert canvas.mat_grid(mat, size) == grid
    assert canvas.half_inch_lines(mat, size) == lines
    assert canvas.mat_caption(mat) == f"CUTTING MAT {mat.label}"


def test_a_non_square_mat_fits_the_view():
    mat = CAMEO5.mat("12x24")
    scene = canvas.Scene(media=(0, 0, 100, 100), mat=mat, profile=CAMEO5)
    # the scene's cut area and fit rule are those of its mat, not the CAMEO 4's 12 x 12 in
    assert scene.cut_area == geometry.cut_area("12x24", CAMEO5) != geometry.cut_area()
    low = (20.0, 400.0, 50.0, 50.0)                              # below the 12 in of a 12 x 12
    assert canvas.Scene(media=(0, 0, 12 * IN, 24 * IN), mat=mat, profile=CAMEO5).fits(low)
    assert not canvas.Scene(media=(0, 0, 12 * IN, 24 * IN)).fits(low)
    view = canvas.fit_view(1000, 600, scene)
    height = 600 - 2 * canvas.FIT_MARGIN - canvas.PAD_TOP - canvas.FEED_LABEL_SPACE - canvas.PAD_BOTTOM
    assert view.scale == pytest.approx(height / (24 * IN))       # the length decides
    left, top = view.to_px(0, 0)
    right, bottom = view.to_px(12 * IN, 24 * IN)
    assert left >= canvas.FIT_MARGIN and right <= 1000 - canvas.FIT_MARGIN
    assert top >= canvas.FIT_MARGIN and bottom <= 600 - canvas.FIT_MARGIN
    assert (left + right) / 2 == pytest.approx((1000 + canvas.PAD_LEFT - canvas.PAD_RIGHT) / 2)
    # tall and narrow: now the width decides
    narrow = canvas.fit_view(300, 2000, scene)
    assert narrow.scale == pytest.approx((300 - 2 * canvas.FIT_MARGIN - canvas.PAD_LEFT
                                          - canvas.PAD_RIGHT) / (12 * IN))
    # the CAMEO 4's mat is fitted as before (a square)
    square = canvas.Scene(media=(0, 0, 100, 100))
    assert canvas.fit_view(1000, 600, square).scale == pytest.approx(height / (12 * IN))
    # and it draws, with labels and in the thumbnail
    widget = Gtk.Label()
    for labels in (True, False):
        snapshot = Gtk.Snapshot()
        canvas.render(snapshot, widget, scene, canvas.fit_view(1000, 600, scene, labels), labels)
        assert snapshot.to_node() is not None


# --- a detected machine of another profile ----------------------------------------------------------
def test_a_detected_machine_switches_the_layout_and_send_waits_for_use(make, run, svg, tmp_path):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    assert win.send_panel.send_button.get_sensitive() and win.layout.machine == "cameo4"
    placed = win.doc.bbox()

    client.set_machine("cameo5", tools=" 2, 0")
    run(win.monitor.refresh())
    # the layout follows: the CAMEO 5's mat, the design where it was, a banner with Use
    assert win.layout.machine == "cameo5" and win.monitor.choice == "cameo5"
    assert GuiSettings.load(tmp_path / "gui.json").machine == "cameo5"
    assert win.scene.profile is CAMEO5 and win.scene.mat.key == "12x12"
    assert win.doc.bbox() == placed
    assert win.machine_banner.get_revealed()
    assert win.machine_banner.get_title() == sending.MACHINE_SWITCHED.format(name="CAMEO 5",
                                                                            old="CAMEO 4")
    assert win.machine_banner.get_button_label() == "Use CAMEO 5"
    assert win.page_setup._machine_name.get_text() == "CAMEO 5"
    assert win.page_setup._detected.get_text() == "(Detected)"
    assert not _about_testing(_shown_texts(win.page_setup))
    assert not win.page_setup._machine.get_visible()            # no offline choice now
    assert win.connection._hint.get_text() == "Releases the CAMEO 5 so other apps can connect to it."
    # no materials for it: Cut Settings, then Send still waits for Use
    panel = win.send_panel
    assert panel._why.get_text() == sending.CHOOSE_MATERIAL
    panel.use_cut_settings()
    waiting = sending.CONFIRM_MACHINE.format(name="CAMEO 5")
    assert panel._why.get_text() == waiting and panel.test_cut.get_tooltip_text() == waiting
    assert not win.lookup_action("send").get_enabled()
    assert not win.lookup_action("test-cut").get_enabled()
    win.machine_banner.emit("button-clicked")
    assert not win.machine_banner.get_revealed()
    assert panel.send_button.get_sensitive() and win.lookup_action("send").get_enabled()
    spec, settings, _doc = win._prepare(False)
    assert (spec.machine, spec.mat) == ("cameo5", "12x12")
    assert (settings.force, settings.speed, settings.tool_code) == (1, 5, 2)
    assert sending.plan_summary(spec)[1] is None

    # the CAMEO 4 again: back, and Send waits for Use once more
    client.ping_reply = dict(client.ping_reply, model="CAMEO 4 V5.05")
    client.machine_reply = None
    client.set_tools(" 2, 4")
    run(win.monitor.refresh())
    assert win.layout.machine == "cameo4" and win.machine_banner.get_button_label() == "Use CAMEO 4"
    assert panel.material == "Vinyl" and not panel.manual       # its materials are back
    assert panel._why.get_text() == sending.CONFIRM_MACHINE.format(name="CAMEO 4")


def test_the_send_sheet_has_no_note_for_an_untested_machine_and_a_switch_blocks_it(make, run,
                                                                                    svg):
    client = FakeDaemonClient()
    win = _connected(client, make, run, "cameo5")
    run(win.open_path(svg))
    win.send_panel.use_cut_settings()

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: isinstance(win.get_visible_dialog(), SendDialog))
        sheet = win.get_visible_dialog()
        assert sheet.get_title() == "Send to CAMEO 5"
        assert sheet.notes == [] and not sheet._notes.get_visible()
        assert not _about_testing(_shown_texts(sheet))
        assert _rows(sheet) == {"Tool 1: AutoBlade detected": True, "Cutting mat loaded (12 × 12 in)": True,
                                "Design on the media and inside the cut area": True}
        assert sheet._send.get_sensitive()
        # another machine while the sheet is open: the layout switches, the sheet cannot send
        client.set_machine("cameo5_plus", tools=" 2, 0")
        await win.monitor.refresh()
        rows = _rows(sheet)
        assert rows[sending.CONFIRM_MACHINE.format(name="CAMEO 5 Plus")] is False
        assert not sheet._send.get_sensitive()
        sheet.close()
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert client.opened == []


def test_a_switch_right_after_the_send_sheet_was_confirmed_sends_nothing(make, run, svg,
                                                                         monkeypatch):
    """The machine changes between the click on Send in the sheet and the job: the last check
    before the job stops it."""
    shown = []
    monkeypatch.setattr(window_mod.dialogs, "show_message",
                        lambda parent, heading, body: shown.append((heading, body)))
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: isinstance(win.get_visible_dialog(), SendDialog))
        sheet = win.get_visible_dialog()
        assert sheet._send.get_sensitive()
        sheet._send.emit("clicked")                      # the answer is in, the flow not yet on
        client.set_machine("cameo5", tools=" 2, 0")
        await win.monitor.refresh()
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert shown == [window_mod.MACHINE_CHANGED] and client.opened == []


def test_nothing_is_sent_while_use_waits_even_for_the_same_job(make, run, monkeypatch):
    """A Test Cut waits in the Load Media sheet; meanwhile a CAMEO 5 is detected, then the CAMEO 4
    again: the job is the same as before, but the user has not pressed "Use CAMEO 4"."""
    shown = []
    monkeypatch.setattr(window_mod.dialogs, "show_message",
                        lambda parent, heading, body: shown.append((heading, body)))
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    client.set_status("2")                                       # the mat is not loaded yet
    run(win.monitor.refresh())
    before = win._prepare(True)[0].to_dict()

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow(test_cut=True))
        await _until(lambda: isinstance(win.get_visible_dialog(), LoadMediaDialog))
        sheet = win.get_visible_dialog()
        cameo4 = (dict(client.ping_reply), client.info_reply)
        client.set_machine("cameo5", tools=" 2, 0")
        await win.monitor.refresh()
        assert win.layout.machine == "cameo5"
        client.ping_reply, client.info_reply = cameo4
        client.machine_reply = None
        client.set_status("0")
        await win.monitor.refresh()
        assert win.layout.machine == "cameo4" and win.layout.pending == "cameo4"
        assert win._prepare(True)[0].to_dict() == before           # the very same job
        assert sheet._continue.get_sensitive()
        sheet._continue.emit("clicked")
        await asyncio.wait_for(flow, 5)
    run(scenario())
    assert shown == [window_mod.MACHINE_CHANGED] and client.opened == []


def test_a_machine_detected_while_load_media_is_open_sends_nothing(make, run, svg, monkeypatch):
    """Send waits in the Load Media sheet; meanwhile a Portrait 4 is detected and the mat is
    loaded. No Send sheet for a job made for the CAMEO 4 appears: the app says the machine
    changed, and nothing is sent."""
    shown = []
    monkeypatch.setattr(window_mod.dialogs, "show_message",
                        lambda parent, heading, body: shown.append((heading, body)))
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    client.set_status("2")
    run(win.monitor.refresh())

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: isinstance(win.get_visible_dialog(), LoadMediaDialog))
        sheet = win.get_visible_dialog()
        client.set_machine("portrait4", tools=" 2, 0")
        client.set_status("0")
        await win.monitor.refresh()
        assert win.layout.machine == "portrait4" and sheet._continue.get_sensitive()
        sheet._continue.emit("clicked")
        await asyncio.wait_for(flow, 5)
        assert not isinstance(win.get_visible_dialog(), SendDialog)
    run(scenario())
    assert shown == [window_mod.MACHINE_CHANGED] and client.opened == []


def test_a_machine_switch_while_a_job_runs_breaks_nothing(make, run, svg, monkeypatch, caplog):
    """The job view stays the job's, the panels follow the new machine, and the job's own
    action names it when it has ended."""
    caplog.set_level(logging.ERROR)
    toasts = []
    monkeypatch.setattr(MainWindow, "toast", lambda self, markup: toasts.append(markup))
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    run(win.open_path(svg))
    gate = asyncio.Event()
    client.hold_step = (1, gate)

    async def scenario():
        job = asyncio.ensure_future(win.run_job(win._prepare(False)[0].to_dict()))
        await _until(lambda: 1 in client.steps_started)
        client.set_machine("portrait4", tools=" 2, 0")
        await win.monitor.refresh()
        assert win.layout.machine == "portrait4" and list(win.send_panel._cards) == [1]
        assert win.side_stack.get_visible_child_name() == "job"
        assert win.job_view.info.action == "Cut" and win.send_panel.action is None
        gate.set()
        return await asyncio.wait_for(job, 5)
    assert run(scenario()) is Outcome.DONE
    assert any(t.startswith("<b>Cut complete</b>") for t in toasts), toasts
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    win._leave_job()
    assert win.send_panel._why.get_text() == sending.CHOOSE_MATERIAL


def test_the_cameo4_send_sheet_has_no_notes(make, run, svg):
    win = make(FakeDaemonClient())
    run(win.monitor.refresh())
    run(win.open_path(svg))

    async def scenario():
        flow = asyncio.ensure_future(win.send_flow())
        await _until(lambda: isinstance(win.get_visible_dialog(), SendDialog))
        sheet = win.get_visible_dialog()
        assert sheet.get_title() == "Send to CAMEO 4" and sheet.notes == []
        assert not sheet._notes.get_visible()
        assert list(_rows(sheet)) == ["Tool 1: AutoBlade detected", "Cutting mat loaded",
                                      "Design on the media and inside the cut area"]
        sheet.close()
        await asyncio.wait_for(flow, 5)
    run(scenario())


# --- the Send panel ----------------------------------------------------------------------------------
def test_a_one_holder_machine_has_one_tool_card(make, run, svg):
    client = FakeDaemonClient()
    win = _connected(client, make, run, "portrait4")
    panel = win.send_panel
    assert list(panel._cards) == [1]
    [card] = panel._cards.values()
    assert card.title == "Tool" and card._title.get_text() == "Tool · AutoBlade"
    assert [w.get_text() for w in win.tool_labels.values()] == ["Tool: AutoBlade (Detected)"]
    assert win.connection.tool_rows() == [("Tool", "AutoBlade (Detected)")]
    assert not _about_testing(_shown_texts(win.connection))
    panel.use_cut_settings()
    assert card.get_active() and card.get_sensitive()
    card.set_active(False)                                     # a click on the lone card
    assert card.get_active() and panel.holder == 1
    # a Sketch Pen is loaded: the plain words for a one-holder machine
    run(win.open_path(svg))
    client.set_tools(" 7, 0")
    run(win.monitor.refresh())
    assert panel.send_button.get_tooltip_text() == "Load the AutoBlade into the tool holder to send."
    assert panel._mismatch.get_visible()
    assert panel._mismatch_title.get_text() == "Load the AutoBlade into the tool holder."


@pytest.mark.parametrize("profile_id, reply", [("portrait4", " 0, 2"), ("portrait3", " 0, 7")])
def test_a_one_holder_tool_reply_in_the_other_slot_is_shown_and_sends_nothing(make, run, svg,
                                                                               profile_id, reply):
    """A well-formed reply with the code in the slot no holder owns fails closed: the raw reply
    shows in the status bar, the popover and the card, and Send and Test Cut say why."""
    client = FakeDaemonClient()
    win = _connected(client, make, run, profile_id, tools=reply)
    run(win.open_path(svg))
    panel = win.send_panel
    panel.use_cut_settings()
    for _ in range(2):
        run(win.monitor.refresh())
    state = win.monitor.state
    assert state.tools is None and state.tools_reply == reply
    shown = f"Unclear reply “{reply.strip()}”"
    name = by_id(profile_id).name
    why = (f"The {name} reported its tool as “{reply.strip()}”, which OpenCutter cannot read for "
           "this model, so nothing is sent. Please report this reply.")
    assert [w.get_text() for w in win.tool_labels.values()] == [f"Tool: {shown}"]
    assert win.connection.tool_rows() == [("Tool", shown)]
    [card] = panel._cards.values()
    assert card._desc.get_text() == shown and card.has_css_class("warning")
    assert not panel.send_button.get_sensitive() and panel._why.get_text() == why
    assert not panel.test_cut.get_sensitive() and panel.test_cut.get_tooltip_text() == why
    # the machine reports the tool in its own slot: read, and Send may go
    client.set_tools(" 2, 0")
    run(win.monitor.refresh())
    assert win.monitor.state.tools == (2,) and win.monitor.state.tools_reply is None
    assert win.connection.tool_rows() == [("Tool", "AutoBlade (Detected)")]
    assert panel.send_button.get_sensitive()


def test_the_cut_settings_tool_list_names_the_chosen_holders_tools(make, run, svg):
    """Tool 1 and Tool 2 of a CAMEO 5 take the same codes, but code 4 is the 2 mm Kraft Blade in
    Tool 1 and the 3 mm one in Tool 2: the list follows the holder, not only its codes."""
    client = FakeDaemonClient()
    win = _connected(client, make, run, "cameo5", tools=" 2, 4")
    run(win.open_path(svg))
    panel = win.send_panel
    panel.use_cut_settings()
    tool1 = [c for c in sending.tool_cards(CAMEO5) if c.holder == 1][0].tools
    tool2 = [c for c in sending.tool_cards(CAMEO5) if c.holder == 2][0].tools
    assert [c for c, _ in tool1] == [c for c, _ in tool2] and tool1 != tool2
    assert _strings(panel._manual_tool_list) == [n for _, n in tool1]
    panel._cards[2].set_active(True)
    assert panel.holder == 2 and _strings(panel._manual_tool_list) == [n for _, n in tool2]
    index = panel._manual_codes.index(4)
    assert _strings(panel._manual_tool_list)[index] == "Kraft Blade, 3mm"
    panel._manual_tool.set_selected(index)
    assert panel.tool_settings().holder == 2 and panel.tool_settings().tool_code == 4
    assert panel._cards[2]._title.get_text() == "Tool 2 · Kraft Blade, 3mm"
    panel._cards[1].set_active(True)
    assert _strings(panel._manual_tool_list) == [n for _, n in tool1]


@pytest.mark.parametrize("profile_id", ["cameo4_plus", "cameo4_pro"])
def test_the_basic_set_note_claims_no_test_on_the_plus_and_the_pro(make, run, profile_id):
    from opencutter.gui.materials import BASIC_NOTE_UNTESTED
    win = _connected(FakeDaemonClient(), make, run, profile_id, tools=" 2, 4")
    panel = win.send_panel
    panel._picker.set_data(panel.store, [], panel.material, panel.picker_line)
    assert not any(p.verified for p in panel.store.presets) and panel.store.presets
    assert panel._picker._basic.get_visible()
    assert panel._picker._basic_note.get_text() == BASIC_NOTE_UNTESTED == (
        "Basic set from the CAMEO 4. Start light: adjust Speed, Force and Blade Depth in Cut "
        "Settings, then make a Test Cut.")
    assert not _about_testing(_shown_texts(panel._picker))
    # the CAMEO 4 keeps its note: its vinyl setting was cut on one
    cameo4 = make(FakeDaemonClient(), machine="cameo4").send_panel
    cameo4._picker.set_data(cameo4.store, [], cameo4.material, cameo4.picker_line)
    assert cameo4._picker._basic_note.get_text() == BASIC_NOTE


def test_cut_settings_take_the_machines_ranges(make, run):
    win = _connected(FakeDaemonClient(), make, run, "cameo5")
    panel = win.send_panel
    assert panel.material is None and panel._material_label.get_text() == sending.NO_MATERIALS
    assert panel._source.get_text() == ""                       # not "Basic set": none is for it
    assert panel._empty.get_visible() and panel._empty_note.text == sending.NO_MATERIALS_HINT
    assert not panel._settings_toggle.get_sensitive()
    panel._use_cut_settings.emit("clicked")
    assert panel.manual and panel.material == "Cut Settings" and panel.action == "Cut"
    assert not panel._empty.get_visible() and panel._settings_toggle.get_active()
    sliders = panel._sliders
    assert sliders["force"].range == (1, 40)                   # the CAMEO 5 generation
    assert sliders["speed"].range == (1, 30) and sliders["acceleration"].range == (1, 10)
    assert sliders["blade_depth"].range == (0, 10) and sliders["blade_depth"].scale.get_visible()
    assert panel.summary_text() == "Blade Depth 1 · Force 1 · Speed 5 · 1 pass"
    sliders["force"].scale.set_value(40)
    assert panel.tool_settings().force == 40 and panel.modified and panel._reset.get_visible()
    panel.use_cut_settings()                                  # again: the values stay
    assert panel.tool_settings().force == 40
    # a Sketch Pen in Tool 1: no Blade Depth, no overcut
    names = _strings(panel._manual_tool_list)
    assert panel._manual_tool.get_visible() and "Sketch Pen" in names and "Rotary Blade" not in names
    panel._manual_tool.set_selected(names.index("Sketch Pen"))
    settings = panel.tool_settings()
    assert (settings.tool_code, settings.blade_depth, settings.force) == (7, None, 1)
    assert panel.action == "Sketch" and not sliders["blade_depth"].scale.get_visible()
    assert not any(w.get_visible() for w in panel._blade_rows)
    # Tool 2
    panel._cards[2].set_active(True)
    assert panel.holder == 2 and panel.tool_settings().holder == 2
    # another machine: the values were for the CAMEO 5, so they are dropped; the user presses
    # Use Cut Settings again for the Portrait 4 (one holder: Tool)
    win.client.set_machine("portrait4", tools=" 2, 0")
    run(win.monitor.refresh())
    assert not panel.manual and panel.material is None and panel.tool_settings() is None
    assert panel._empty.get_visible() and panel.holder == 1
    panel.use_cut_settings()
    assert panel.tool_settings().force == 1 and panel.tool_settings().holder == 1


def test_a_switch_never_chooses_an_unverified_preset_for_the_user(make, run, tmp_path):
    """The CAMEO 4 starts on its verified Vinyl, chosen by the app. On a CAMEO 4 Plus that row
    is not verified: nothing is chosen. A material the user picked stays."""
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    panel = win.send_panel
    assert panel.material == "Vinyl" and panel.preset().verified
    assert win.settings.material is None                     # the app's choice, not the user's
    client.set_machine("cameo4_plus", tools=" 2, 4")
    run(win.monitor.refresh())
    assert win.layout.machine == "cameo4_plus" and "Vinyl" in panel.store.materials()
    assert panel.material is None and panel._why.get_text() == sending.CHOOSE_MATERIAL
    # back on the CAMEO 4, the user picks Vinyl: it stays on the Plus (an unverified row the
    # user chose), and Send still waits for Use
    client.ping_reply = dict(client.ping_reply, model="CAMEO 4 V5.05")
    client.machine_reply = None
    run(win.monitor.refresh())
    assert win.layout.machine == "cameo4" and panel.material == "Vinyl"
    panel.select_material("Vinyl")
    client.set_machine("cameo4_plus", tools=" 2, 4")
    run(win.monitor.refresh())
    assert panel.material == "Vinyl" and not panel.preset().verified
    assert panel.test_cut.get_tooltip_text() == sending.CONFIRM_MACHINE.format(name="CAMEO 4 Plus")


def test_a_material_list_shown_before_a_machine_switch_picks_nothing(make, run):
    client = FakeDaemonClient()
    win = make(client)
    run(win.monitor.refresh())
    panel = win.send_panel
    picker = panel._picker
    win.present()
    panel._material_button.popup()
    assert picker.get_visible() and "Vinyl" in picker._store.materials()
    remembered = win.settings.recent_materials
    client.set_machine("cameo5", tools=" 2, 0")
    run(win.monitor.refresh())
    assert not picker.get_visible()          # the CAMEO 4's list is closed
    picker._pick("Vinyl")                    # a row of it, picked all the same
    assert panel.material is None and panel._material_label.get_text() == sending.NO_MATERIALS
    assert win.settings.recent_materials == remembered


@pytest.mark.parametrize("profile_id, acceleration, speed", [("portrait3", None, (1, 10)),
                                                             ("portrait4", (1, 3), (1, 10))])
def test_the_acceleration_row_is_hidden_on_a_machine_without_one(make, run, profile_id,
                                                                   acceleration, speed):
    win = _connected(FakeDaemonClient(), make, run, profile_id)
    panel = win.send_panel
    panel.use_cut_settings()
    row = panel._sliders["acceleration"]
    assert row.scale.get_visible() is (acceleration is not None)
    if acceleration:
        assert row.range == acceleration
    assert panel._sliders["speed"].range == speed
    assert ("Accel." in panel.summary_text(with_passes=False)) is (acceleration is not None)


def test_the_cameo4_sliders_are_as_before(make):
    panel = make(FakeDaemonClient(ping=None)).send_panel
    ranges = {key: row.range for key, row in panel._sliders.items()}
    assert ranges == {"blade_depth": (0, 10), "force": (1, 33), "speed": (1, 30),
                      "acceleration": (1, 10)}
    assert not panel._empty.get_visible() and not panel._manual_tool.get_visible()
    panel.use_cut_settings()                     # only for a machine without materials
    assert not panel.manual and panel.material == "Vinyl"


def test_the_material_list_says_when_a_machine_has_no_materials(make, run):
    win = _connected(FakeDaemonClient(), make, run, "cameo5")
    panel, picker = win.send_panel, win.send_panel._picker
    picker.set_data(panel.store, [], panel.material, panel.picker_line)
    assert picker._empty.get_visible() and not picker._search.get_visible()
    texts = [w.get_text() for w in _widgets(picker._empty) if isinstance(w, Gtk.Label)]
    assert sending.NO_MATERIALS in texts and sending.NO_MATERIALS_HINT in texts
    assert "Cut Settings" in sending.NO_MATERIALS_HINT
    picker._use.emit("clicked")
    assert panel.manual and panel.material == "Cut Settings"
    # the CAMEO 4 Plus has the Basic set (not verified on it: nothing is chosen for the user)
    plus = _connected(FakeDaemonClient(), make, run, "cameo4_plus", tools=" 2, 4")
    assert plus.send_panel.store.materials() and plus.send_panel.material is None
    assert not plus.send_panel._empty.get_visible()


# --- a recognized machine ------------------------------------------------------------------------------
def test_a_recognized_machine_is_named_with_its_reason(make, run, tmp_path):
    client = FakeDaemonClient(ping=RECOGNIZED_PING)
    client.machine_reply = dict(RECOGNIZED_FIELDS)
    win = make(client)
    run(win.monitor.refresh())
    state = win.monitor.state
    assert state.wrong_model and state.recognized == "cameo3"
    assert CAMEO3_REASON.startswith("OpenCutter recognizes this CAMEO 3 but cannot drive it yet: ")
    assert win.banner.get_revealed() and win.banner.get_title() == f"Unsupported Machine. {CAMEO3_REASON}"
    assert not win.banner.get_button_label()
    pop = win.connection
    assert pop._alert_text.get_text() == f"Unsupported Machine. {CAMEO3_REASON}"
    assert pop._device_name.get_text() == "CAMEO 3 V1.60"
    assert pop._hint.get_text() == OTHER_MACHINE_HINT
    assert win.pill.text == "Unsupported Machine"
    assert win.status_label.get_tooltip_text() == CAMEO3_REASON
    assert win.send_panel._why.get_text() == NOT_SUPPORTED
    assert win.page_setup._detected.get_text() == "Not detected"
    prefs = PreferencesDialog(win.settings, win.store, state)
    assert prefs.support_row.get_subtitle() == "CAMEO 3 · Recognized, not supported yet"
    first = FirstRunDialog()
    first.update_machine(state)
    assert first._problem.get_text() == CAMEO3_REASON and first._hint.get_text() == OTHER_MACHINE_HINT


# --- the job view ------------------------------------------------------------------------------------
def test_the_job_view_says_when_the_cut_area_could_not_be_checked(make, run, svg):
    client = FakeDaemonClient()
    client.units_reply = dict(UNITS_UNCHECKED)
    win = _connected(client, make, run, "portrait4")
    run(win.open_path(svg))
    win.send_panel.use_cut_settings()
    spec, _settings, _doc = win._prepare(False)
    assert (spec.machine, spec.mat) == ("portrait4", "8.5x12")
    assert run(win.run_job(spec.to_dict())) is Outcome.DONE, win.runner.error
    note = win.runner.units_note
    assert note.startswith("The cut area could not be checked on this model.")
    view = win.job_view
    assert view._units.get_visible() and view._units.text == note
    assert view._done_units.get_visible() and view._done_units.text == note
    assert client.opened[0]["machine"] == "portrait4"


def test_the_load_media_sheet_names_the_mat_where_there_are_several():
    one = LoadMediaDialog()
    assert one._text.get_text().startswith("Place the media on the mat, line the mat up")
    wide = LoadMediaDialog("12 × 24 in")
    assert wide._text.get_text().startswith("Place the media on the 12 × 24 in mat, line the mat up")
    loading = MachineState(link=Link.CONNECTED, status="2", profile="portrait4", support="untested")
    wide.update(loading)
    assert wide._state.get_text() == "Waiting for media…" and not wide._continue.get_sensitive()


# --- Preferences, About and the first run ---------------------------------------------------------
def test_preferences_show_the_mats_and_the_model_of_the_layouts_machine(tmp_path):
    """The Model row names the model alone for every machine OpenCutter drives, tested or not;
    only a refused machine gets a word after its name."""
    settings = GuiSettings.load(tmp_path / "gui.json")
    store = PresetStore(list(BASIC_PRESETS), basic_source())
    prefs = PreferencesDialog(settings, store, MachineState())
    rows = {w.get_title(): w.get_subtitle() for w in _widgets(prefs) if isinstance(w, Adw.ActionRow)}
    assert rows["Default Cutting Mat"] == "CAMEO 12 × 12 in" and rows["Model"] == "CAMEO 4"

    settings.set_machine("cameo5")
    prefs = PreferencesDialog(settings, store, MachineState(choice="cameo5"))
    assert _strings(prefs.mat_row.get_model()) == ["CAMEO 12 × 12 in", "CAMEO 12 × 24 in"]
    assert prefs.support_row.get_subtitle() == "CAMEO 5"
    prefs.mat_row.set_selected(1)
    assert GuiSettings.load(tmp_path / "gui.json").mats == {"cameo5": "12x24"}

    v1 = MachineState(link=Link.CONNECTED, status="0", profile="cameo4", support="untested")
    prefs = PreferencesDialog(GuiSettings.load(tmp_path / "gui.json"), store, v1)
    assert prefs.support_row.get_subtitle() == "CAMEO 4"
    refused = MachineState(link=Link.FAILED, wrong_model=True, model="CAMEO 5 PRO V1.07")
    prefs = PreferencesDialog(GuiSettings.load(tmp_path / "gui.json"), store, refused)
    assert prefs.support_row.get_subtitle() == "CAMEO 5 PRO V1.07 · Not supported"


def test_preferences_show_the_media_size_in_use_after_a_machine_switch(make, run):
    """The media size is shared by every machine: a 24 x 24 in sheet chosen on the CAMEO 4 Pro
    stays the one in use on a CAMEO 4 (clamped to its mat), so Preferences lists and selects it
    as Page Setup does, and does not show Letter instead."""
    client = FakeDaemonClient(ping=None)
    win = make(client, machine="cameo4_pro")
    win.page_setup.select_media("24x24")
    client.set_machine("cameo4", support="tested", tools=" 2, 4", version="V5.05")
    run(win.monitor.refresh())
    assert win.layout.machine == "cameo4" and win.settings.media == "24x24"
    assert win.page_setup.media_key == "24x24"
    prefs = PreferencesDialog(win.settings, win.store, win.monitor.state)
    [media] = [w for w in _widgets(prefs)
               if isinstance(w, Adw.ComboRow) and w.get_title() == "Default Media Size"]
    labels = _strings(media.get_model())
    assert labels[media.get_selected()].startswith("24 × 24 in")
    assert labels[:3] == ["Letter · 8.5 × 11 in", "A4 · 8.27 × 11.69 in", "12 × 12 in · 12 × 12 in"]
    # a size of this machine's own mat is listed as before, with nothing added
    win.settings.media = "letter"
    prefs = PreferencesDialog(win.settings, win.store, win.monitor.state)
    [media] = [w for w in _widgets(prefs)
               if isinstance(w, Adw.ComboRow) and w.get_title() == "Default Media Size"]
    assert len(_strings(media.get_model())) == 3 and media.get_selected() == 0


def test_preferences_offer_only_the_mats_the_machine_may_use(tmp_path, test_cutter):
    settings = GuiSettings.load(tmp_path / "gui.json")
    settings.set_machine("test_cutter")
    store = PresetStore(list(BASIC_PRESETS), basic_source())
    prefs = PreferencesDialog(settings, store, MachineState(choice="test_cutter"))
    assert _strings(prefs.mat_row.get_model()) == ["Test 12 × 12 in"]


@pytest.mark.parametrize("profile", enabled(), ids=lambda p: p.id)
def test_the_connection_tips_claim_only_what_was_seen_on_the_model(profile):
    """Only the tested CAMEO 4 was seen to accept a connection only right after it is switched
    on. Every other model gets the advice to switch it off and on, but no claim either way."""
    from opencutter.gui.connection import POWER_ON_HINT, power_on_hint
    from opencutter.gui.firstrun import switch_on_step
    from opencutter.gui.machine import another_program, power_cycle_seen, texts_for
    assert profile.power_cycle_to_connect is True
    seen = power_cycle_seen(profile)
    assert seen == (profile.support is Support.TESTED) == (profile is CAMEO4)
    hint, step = power_on_hint(profile.name, seen), switch_on_step(profile)
    held = another_program(**texts_for(profile))
    for text in (hint, step, held):
        assert "off and on" in text and "connects to it by itself" not in text, text
        assert ("only right after" in text) == (seen and text is not held), text
    if profile is CAMEO4:
        assert hint == POWER_ON_HINT


def test_after_disconnect_the_popover_gives_no_connect_tips(make):
    win = make(FakeDaemonClient(ping=None), machine="cameo5")
    pop = win.connection
    pop.update(MachineState(choice="cameo5"))
    assert pop._help.get_visible()
    pop.update(MachineState(note=released("CAMEO 5"), released=True, choice="cameo5"))
    assert not pop._help.get_visible()
    assert pop._hint.get_text() == "The CAMEO 5 is free for other apps. Press Connect to use it here again."


def test_about_names_the_machines_it_drives_and_says_nothing_about_testing():
    text = dialogs.about_comments()
    drives, recognized = text.split(" Recognized, not supported yet: ", 1)
    assert drives == ("Send SVG designs to a Silhouette cutting machine over Bluetooth. Drives the "
                      "CAMEO 4 (firmware V1.xx or V5.xx), CAMEO 4 Plus, CAMEO 4 Pro, CAMEO 5, "
                      "CAMEO 5 Plus, CAMEO Pro MK-II, CAMEO 5 Alpha, CAMEO 5 Alpha Plus, Portrait "
                      "3 and Portrait 4.")
    for p in enabled():
        assert p.name in drives, p.name
    for m in recognized_models():
        assert m.name in recognized and f" {m.name}," not in drives, m.name
    assert dialogs.NOT_AFFILIATED in text and not _about_testing([text])


def test_the_first_run_says_your_machine_until_one_is_detected():
    dialog = FirstRunDialog()
    assert dialog._welcome.get_text() == "Cut your designs on your machine from Linux."
    assert "off and on" in dialog.step_texts[1]
    cameo5 = MachineState(link=Link.CONNECTED, status="0", profile="cameo5", support="untested")
    dialog.update_machine(cameo5)
    assert dialog._welcome.get_text() == "Cut your designs on a Silhouette CAMEO 5 from Linux."
    # not seen on it: the advice to switch it off and on, and no claim about the model
    assert dialog.step_texts[1] == ("Switch the CAMEO 5 on. If the app can’t connect, switch it "
                                    "off and on.")
    portrait = MachineState(link=Link.CONNECTED, status="0", profile="portrait4", support="untested")
    dialog.update_machine(portrait)
    assert dialog.step_texts[1].startswith("Switch the Portrait 4 on.")
    assert "off and on" in dialog.step_texts[1] and "only right after" not in dialog.step_texts[1]
    dialog.update_machine(MachineState())                     # not connected: keeps the name
    assert "Portrait 4" in dialog._welcome.get_text()


# --- no word about testing -----------------------------------------------------------------------
# Every firmware line OpenCutter drives: (profile id, line, support level). Only the CAMEO 4 on V5
# is tested; the CAMEO 4 on V1 and every other model are driven but were never run.
DRIVEN_LINES = [(p.id, line.major, p.support.narrower(line.support).value) for p in enabled()
                for line in p.identity.lines if p.support.narrower(line.support).jobs_allowed]
# The refusal of a machine no profile names (WRONG_MODEL, WRONG_MODEL_UNKNOWN): OpenCutter
# cannot drive it, with no word about testing.
REFUSAL = "OpenCutter cannot drive it and will not send jobs to it."


async def _send_sheet_texts(win: MainWindow) -> list[str]:
    """What the Send sheet shows, read while it is open; it is closed again."""
    flow = asyncio.ensure_future(win.send_flow())
    await _until(lambda: isinstance(win.get_visible_dialog(), SendDialog))
    sheet = win.get_visible_dialog()
    texts = _shown_texts(sheet) + list(sheet.notes)
    assert sheet._send.get_sensitive()                  # nothing about the model blocks Send
    sheet.close()
    await asyncio.wait_for(flow, 5)
    return texts


async def _job_texts(win: MainWindow, client: FakeDaemonClient) -> list[str]:
    """What the window shows while a job runs (held at its second step) and once it is done."""
    gate = asyncio.Event()
    client.hold_step = (1, gate)
    job = asyncio.ensure_future(win.run_job(win._prepare(False)[0].to_dict()))
    await _until(lambda: 1 in client.steps_started)
    assert win.side_stack.get_visible_child_name() == "job"
    texts = _shown_texts(win)
    gate.set()
    assert await asyncio.wait_for(job, 5) is Outcome.DONE, win.runner.error
    return texts + _shown_texts(win)


def _material_list_texts(panel) -> list[str]:
    """What the material list shows once opened; nothing on a machine without materials."""
    if not panel.store.presets:
        return []
    panel._picker.set_data(panel.store, [], panel.material, panel.picker_line)
    return _shown_texts(panel._picker)


def _dialog_texts(win: MainWindow, state: MachineState) -> list[str]:
    """Preferences, About, both pages of the first run and the Load Media sheet for ``state``."""
    prefs = PreferencesDialog(win.settings, win.store, state)
    texts = _shown_texts(prefs)
    dialogs.show_about(win)
    about = win.get_visible_dialog()
    assert isinstance(about, Adw.AboutDialog)
    texts += _shown_texts(about) + [about.get_comments()]
    about.close()
    first = FirstRunDialog()
    first.update_machine(state)
    texts += _shown_texts(first)
    first._stack.set_visible_child_name("connect")
    texts += _shown_texts(first)
    load = LoadMediaDialog(win._mat_name())
    load.update(state)
    return texts + _shown_texts(load)


def _assert_nothing_about_testing(texts: list[str], profile_id: str) -> None:
    """No text says whether a model was tested, not even the CAMEO 4's Basic set note (it shows
    on the CAMEO 4 alone)."""
    assert (BASIC_NOTE in texts) == (profile_id == "cameo4")
    found = _about_testing(texts)
    assert not found, found


@pytest.mark.parametrize("profile_id,major,support", DRIVEN_LINES)
def test_no_shown_text_says_whether_the_machine_was_tested(make, run, svg, profile_id, major,
                                                           support):
    """A connected machine OpenCutter drives, tested or never run, on any of its firmware
    lines, looks like any other: the window (banners, status bar, tooltips, the job view while
    a job runs and once it is done), the connection popover, Page Setup, the material list, the
    Send sheet, Preferences, About, the first run and Load Media say nothing about testing."""
    client = FakeDaemonClient()
    client.set_machine(profile_id, support=support, version=f"V{major}.07",
                       tools=" 2, 4" if profile_id.startswith("cameo4") else " 2, 0")
    profile = by_id(profile_id)
    if profile.mat(profile.default_mat).u_basis is UBasis.UNKNOWN:
        client.units_reply = dict(UNITS_UNCHECKED)      # its job view notes that, nothing else
    win = make(client)
    run(win.monitor.refresh())
    state = win.monitor.state
    assert state.connected and (state.profile, state.support) == (profile_id, support)
    texts = _shown_texts(win) + _shown_texts(win.connection) + _shown_texts(win.page_setup)
    win.machine_banner.emit("button-clicked")          # "Use {name}" where the layout switched
    run(win.open_path(svg))
    panel = win.send_panel
    texts += _material_list_texts(panel)
    if not panel.store.presets:
        panel.use_cut_settings()
    elif panel.material is None:
        panel.select_material("Vinyl")
    texts += _shown_texts(win) + run(_send_sheet_texts(win)) + run(_job_texts(win, client))
    assert PreferencesDialog(win.settings, win.store, state).support_row.get_subtitle() == (
        profile.name)
    texts += _dialog_texts(win, state)
    assert profile.name in " ".join(texts)
    _assert_nothing_about_testing(texts, profile_id)


@pytest.mark.parametrize("profile_id", [p.id for p in enabled()])
def test_no_shown_text_says_whether_the_chosen_machine_was_tested(make, run, profile_id):
    """With no machine connected, the machine chosen in Page Setup, by its plain name, is not
    told apart by whether it was tested either."""
    win = make(FakeDaemonClient(ping=None), machine=profile_id)
    run(win.monitor.refresh())
    state = win.monitor.state
    assert not state.connected and state.machine_profile.id == profile_id
    assert PreferencesDialog(win.settings, win.store, state).support_row.get_subtitle() == (
        state.machine_profile.name)
    texts = (_shown_texts(win) + _shown_texts(win.connection) + _shown_texts(win.page_setup)
             + _strings(win.page_setup._machine_list) + _material_list_texts(win.send_panel)
             + _dialog_texts(win, state))
    assert state.machine_profile.name in " ".join(texts)
    _assert_nothing_about_testing(texts, profile_id)


@pytest.mark.parametrize("ping,fields", [
    (WRONG_MODEL_PING, None),                                  # a model no profile names
    ({**WRONG_MODEL_PING, "model": None}, None),               # no model at all
    ({**WRONG_MODEL_PING, "model": "CAMEO 4 V9.99"}, None),    # a CAMEO 4 line it does not drive
    (RECOGNIZED_PING, RECOGNIZED_FIELDS)])                     # recognized, not supported yet
def test_a_refused_machine_reads_nothing_about_testing(make, run, ping, fields):
    """A machine the service refused: nothing it is shown says whether a model was tested, not
    even the refusal of a model no profile names (OpenCutter cannot drive it)."""
    assert REFUSAL in WRONG_MODEL and REFUSAL in WRONG_MODEL_UNKNOWN
    assert not _about_testing([WRONG_MODEL, WRONG_MODEL_UNKNOWN])
    client = FakeDaemonClient(ping=ping)
    client.machine_reply = fields
    win = make(client)
    run(win.monitor.refresh())
    state = win.monitor.state
    assert state.wrong_model
    texts = (_shown_texts(win) + _shown_texts(win.connection) + _shown_texts(win.page_setup)
             + _material_list_texts(win.send_panel) + _dialog_texts(win, state))
    assert any(UNSUPPORTED in t for t in texts)
    found = _about_testing(texts)
    assert not found, found
    assert (fields is None) == any(REFUSAL in t for t in texts)


def test_no_gui_text_constant_speaks_of_testing():
    """The GUI's module-level texts: none says whether a model was tested, not the refusal of a
    model no profile names (WRONG_MODEL, WRONG_MODEL_UNKNOWN) and not the CAMEO 4's Basic set
    note (BASIC_NOTE)."""
    import importlib
    import pkgutil
    import opencutter.gui as gui
    found = set()
    for info in pkgutil.iter_modules(gui.__path__):
        module = importlib.import_module(f"opencutter.gui.{info.name}")
        found |= {value for name, value in vars(module).items()
                  if name.isupper() and isinstance(value, str) and _about_testing([value])}
    assert not found, found
