# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The window's layout with texts longer than English (a test catalog made in tmp_path, never
one of po/): the canvas footer and the status bar shrink with an ellipsis instead of widening
the window, the header buttons show their icons alone when the header is short of room (never
in English at the window's minimum width), the Send sheet grows for a longer summary, and the
connection popover shows the tools in the window's language.

Needs a display: run headless with GDK_BACKEND=broadway and a private gtk4-broadwayd. The
module is skipped when GTK cannot start."""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

import stubs

stubs.install()
import opencutter.gui  # noqa: E402,F401  (selects the GTK and libadwaita versions)
from gi.events import GLibEventLoop  # noqa: E402
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("GTK cannot open a display", allow_module_level=True)
Adw.init()

from opencutter import i18n  # noqa: E402
from opencutter.gui import app as app_mod  # noqa: E402
from opencutter.gui.jobview import STOP_MACHINE  # noqa: E402
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.gui.sheets import SEND_WIDTH, SendDialog, SendSummary  # noqa: E402
from opencutter.gui.window import MIN_WIDTH, MainWindow  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from gui_fakes import FakeDaemonClient  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("translations",
                                               REPO / "build-aux" / "translations.py")
tr = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("translations", tr)   # dataclasses look it up
_spec.loader.exec_module(tr)

DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="300mm" height="300mm" viewBox="0 0 300 300"
  fill="none" stroke="#000"><rect x="150" y="30" width="30" height="20"/></svg>"""


def _long(text: str) -> str:
    """A made-up translation twice as long as the English text (nobody's translation)."""
    return f"[{text} {text}]"


# a made-up language whose texts are all much longer than English
LONG = [(None, text, _long(text)) for text in (
    "Open", "Send", "Cancel Job", STOP_MACHINE, "Reveal Cutting Mat", "Not Connected",
    "Placement is locked while the machine is moving", "Settings", "Material")] + [
    (None, "{tool} (Detected)", "[{tool}: detected, detected]"),
    (None, "{n} path", "[{n} path path path]"), (None, "{n} paths", "[{n} paths paths paths]"),
    ("machine status", "Ready", "[Ready, ready to go]"),
    ("tool", "Kraft Blade, 3mm", "[Kraft]"),
    ("tool holder", "Tool {number}", "[Tool holder {number}]"),
]


def catalog(tmp_path: Path, lang: str = "xx") -> Path:
    header = tr.Message(msgid="", msgstr=[tr.header_text(
        [*tr.HEADER_FIELDS, ("Language", lang), ("Plural-Forms", "nplurals=2; plural=(n != 1);")])])
    messages = [header]
    for context, msgid, msgstr in LONG:
        if msgid == "{n} paths":
            continue
        if msgid == "{n} path":
            messages.append(tr.Message(msgid=msgid, plural="{n} paths",
                                       msgstr=[msgstr, "[{n} paths paths paths]"]))
        else:
            messages.append(tr.Message(msgid=msgid, context=context, msgstr=[msgstr]))
    path = tr.mo_path(lang, tmp_path / "locale")
    path.parent.mkdir(parents=True)
    path.write_bytes(tr.compile_mo(messages))
    return tmp_path / "locale"


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
def make(tmp_path):
    """Windows with the app's style, laid out at once (no animations)."""
    display = Gdk.Display.get_default()
    provider = app_mod.load_style(display)          # the app's own sizes: pills, paddings
    gtk_settings = Gtk.Settings.get_default()
    animations = gtk_settings.get_property("gtk-enable-animations")
    gtk_settings.set_property("gtk-enable-animations", False)
    made = []

    def factory(client: FakeDaemonClient) -> MainWindow:
        settings = GuiSettings.load(tmp_path / "gui.json")
        win = MainWindow(None, client, PresetStore(list(BASIC_PRESETS), basic_source()), settings)
        made.append(win)
        return win
    yield factory
    for win in made:
        win.destroy()
    gtk_settings.set_property("gtk-enable-animations", animations)
    Gtk.StyleContext.remove_provider_for_display(display, provider)


def _show(win: MainWindow, run, width: int, height: int = 560) -> None:
    win.set_default_size(width, height)
    win.present()
    run(asyncio.sleep(0.3))
    assert win.get_width() == width


def _min_width(widget: Gtk.Widget) -> int:
    return widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0]


def _header_texts(win: MainWindow) -> list[bool]:
    return [text.get_visible() for text in win._header_texts]


def _window(make, run, tmp_path) -> MainWindow:
    svg = tmp_path / "design.svg"
    svg.write_text(DESIGN)
    win = make(FakeDaemonClient())
    run(win.monitor.refresh())
    run(win.open_path(str(svg)))
    return win


def test_longer_texts_never_widen_the_window(make, run, tmp_path):
    i18n.setup("xx", localedir=catalog(tmp_path))
    win = _window(make, run, tmp_path)
    assert win.header_send.get_child().get_last_child().get_text() == _long("Send")
    # beside the sidebar: the footer's "Reveal Cutting Mat" shrinks, the window does not grow
    _show(win, run, 1000)
    assert not win.split.get_collapsed()
    assert _min_width(win.get_content()) <= 1000


def test_longer_texts_fit_the_smallest_window(make, run, tmp_path):
    i18n.setup("xx", localedir=catalog(tmp_path))
    win = _window(make, run, tmp_path)
    # after a run (Stop Machine and Send in the header): the header buttons show their icons
    # alone, and the status bar's texts shrink
    win.header_stop.set_visible(True)
    win._fit_header()
    _show(win, run, MIN_WIDTH)
    assert win.split.get_collapsed() and win.sidebar_toggle.get_visible()
    assert _header_texts(win) == [False] * 4
    assert _min_width(win.get_content()) <= MIN_WIDTH
    assert win.header_send.get_tooltip_text()                 # the texts stay in the tooltips


def test_english_keeps_its_header_texts_at_the_minimum_width(make, run, tmp_path):
    win = _window(make, run, tmp_path)
    win.header_stop.set_visible(True)
    win._fit_header()
    _show(win, run, MIN_WIDTH)
    assert win.split.get_collapsed()
    assert _header_texts(win) == [True] * 4
    assert _min_width(win.get_content()) <= MIN_WIDTH


def test_the_send_sheet_grows_for_a_longer_summary(make, run, tmp_path):
    win = _window(make, run, tmp_path)

    def sheet(settings: str) -> SendDialog:
        return SendDialog(SendSummary(document="design.svg", material="Vinyl", action="Cut",
                                      tool="Tool 1 · AutoBlade", settings=settings, passes=1,
                                      preview_outline=True, estimated_seconds=40, paths=1),
                          win.scene, lambda state: [])
    assert sheet("Blade Depth 1 · Force 10 · Speed 5").get_content_width() == SEND_WIDTH
    longer = sheet("Blade Depth 1 · Force 10 · Speed 5 · " * 3)
    assert longer.get_content_width() > SEND_WIDTH


def test_the_connection_popover_shows_the_tools_in_the_windows_language(make, run, tmp_path):
    i18n.setup("xx", localedir=catalog(tmp_path))
    win = make(FakeDaemonClient())
    run(win.monitor.refresh())
    rows = dict(win.connection.tool_rows())
    assert rows["Tool 2"] == "[[Kraft]: detected, detected]"
    assert rows["Tool 1"] == "[AutoBlade: detected, detected]"
