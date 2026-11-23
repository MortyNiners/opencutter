# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The window in another language: with a test catalog (made in tmp_path, never one of po/) the
main window, its sidebar, the job view and the Preferences show the translated texts, the keys
behind them stay English, and the Language row stores the choice for the next start
(app.setup_language). Every other test runs in English (tests/conftest.py).

Needs a display: run headless with GDK_BACKEND=broadway and a private gtk4-broadwayd. Windows
are never presented; the module is skipped when GTK cannot start."""
import importlib.util
import sys
from pathlib import Path

import pytest

import stubs

stubs.install()
import opencutter.gui  # noqa: E402,F401  (selects the GTK and libadwaita versions)
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("GTK cannot open a display", allow_module_level=True)
Adw.init()

from opencutter import i18n  # noqa: E402
from opencutter.gui import app as app_mod  # noqa: E402
from opencutter.gui.dialogs import RESTART_FOR_LANGUAGE, PreferencesDialog  # noqa: E402
from opencutter.gui.machine import MachineState  # noqa: E402
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.gui.window import MainWindow  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from gui_fakes import FakeDaemonClient  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("translations",
                                               REPO / "build-aux" / "translations.py")
tr = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("translations", tr)   # dataclasses look it up
_spec.loader.exec_module(tr)

# a made-up language: every text in brackets, so nothing here is anybody's translation
XX = [
    (None, "Send", "[Send]"),
    (None, "Page Setup", "[Page Setup]"),
    (None, "Test Cut", "[Test Cut]"),
    (None, "Preferences", "[Preferences]"),
    (None, "Language", "[Language]"),
    (None, "System default", "[System default]"),
    (None, RESTART_FOR_LANGUAGE, "[Restart]"),
    (None, "Not Connected", "[Not Connected]"),
    (None, "No design open", "[No design open]"),
    ("material", "Vinyl", "[Vinyl]"),
    ("action", "Cut", "[Cut]"),
    ("tool holder", "Tool {number}", "[Tool {number}]"),
    (None, "Material", "[Material]"),
    ("paper size", "Letter", "[Letter]"),
]


def catalog(tmp_path: Path, lang: str = "xx") -> Path:
    header = tr.Message(msgid="", msgstr=[tr.header_text(
        [*tr.HEADER_FIELDS, ("Language", lang), ("Plural-Forms", "nplurals=2; plural=(n != 1);")])])
    messages = [header] + [tr.Message(msgid=m, context=c, msgstr=[s]) for c, m, s in XX]
    path = tr.mo_path(lang, tmp_path / "locale")
    path.parent.mkdir(parents=True)
    path.write_bytes(tr.compile_mo(messages))
    return tmp_path / "locale"


def _widgets(root: Gtk.Widget):
    stack = [root]
    while stack:
        widget = stack.pop()
        yield widget
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()


def _labels(root: Gtk.Widget) -> set[str]:
    return {w.get_label() for w in _widgets(root) if isinstance(w, Gtk.Label)}


@pytest.fixture
def window(tmp_path):
    made = []

    def factory() -> MainWindow:
        settings = GuiSettings.load(tmp_path / "gui.json")
        win = MainWindow(None, FakeDaemonClient(ping=None),
                         PresetStore(list(BASIC_PRESETS), basic_source()), settings)
        made.append(win)
        return win
    yield factory
    for win in made:
        win.destroy()


def test_the_window_shows_the_translated_texts(tmp_path, window):
    i18n.setup("xx", localedir=catalog(tmp_path))
    win = window()
    labels = _labels(win)
    assert {"[Send]", "[Page Setup]", "[No design open]", "[Material]"} <= labels
    assert win.send_panel.test_cut.get_label() == "[Test Cut]"
    assert win.pill.text == "[Not Connected]"
    # the keys behind the texts stay English: the verified Basic set's vinyl is chosen
    assert win.send_panel.material == "Vinyl" and win.send_panel.action == "Cut"
    assert win.send_panel._material_label.get_text() == "[Vinyl]"
    assert win.send_panel._actions.get_string(0) == "[Cut]"
    assert win.send_panel._cards[1]._title.get_text().startswith("[Tool 1]")
    assert win.send_panel._cards[1].title == "Tool 1"
    # a text the catalog does not have stays English
    assert "Reveal Cutting Mat" in labels


def test_the_same_window_in_english(window):
    win = window()
    labels = _labels(win)
    assert {"Send", "Page Setup", "No design open", "Material"} <= labels
    assert win.pill.text == "Not Connected"
    assert win.send_panel._material_label.get_text() == "Vinyl"


def test_the_language_row_offers_the_catalogs_and_says_to_restart(tmp_path, monkeypatch):
    locale = catalog(tmp_path)
    monkeypatch.setattr(i18n, "LOCALE_DIR", locale)
    i18n.setup("xx", localedir=locale)
    settings = GuiSettings.load(tmp_path / "gui.json")
    prefs = PreferencesDialog(settings, PresetStore(list(BASIC_PRESETS), basic_source()),
                              MachineState())
    row = prefs.language_row
    assert row.get_title() == "[Language]" and prefs.get_title() == "[Preferences]"
    names = [row.get_model().get_string(i) for i in range(row.get_model().get_n_items())]
    assert names == ["[System default]", "English", "xx"]
    assert prefs.language_codes == ["", "en", "xx"]
    assert row.get_selected() == 0 and not row.get_subtitle()
    row.set_selected(2)
    assert GuiSettings.load(tmp_path / "gui.json").language == "xx"
    assert row.get_subtitle() == "[Restart]"
    row.set_selected(0)                      # back to the one this run started with
    assert GuiSettings.load(tmp_path / "gui.json").language == ""
    assert not row.get_subtitle()


def test_the_next_start_uses_the_stored_language(tmp_path, monkeypatch):
    locale = catalog(tmp_path)
    monkeypatch.setattr(i18n, "LOCALE_DIR", locale)
    monkeypatch.delenv("LANGUAGE", raising=False)
    settings = GuiSettings.load(tmp_path / "gui.json")
    settings.language = "xx"
    assert app_mod.setup_language(settings) == "xx"
    assert i18n._("Send") == "[Send]" and app_mod.os.environ["LANGUAGE"] == "xx"
    # a language whose catalog is gone: the system's (here C: English), LANGUAGE untouched
    monkeypatch.delenv("LANGUAGE", raising=False)
    settings.language = "ro"
    assert app_mod.setup_language(settings) == "en"
    assert i18n._("Send") == "Send" and "LANGUAGE" not in app_mod.os.environ
    # English chosen on purpose: GTK follows it too
    settings.language = "en"
    assert app_mod.setup_language(settings) == "en"
    assert app_mod.os.environ["LANGUAGE"] == "en"


def _preferences(settings: GuiSettings) -> PreferencesDialog:
    return PreferencesDialog(settings, PresetStore(list(BASIC_PRESETS), basic_source()),
                             MachineState())


def test_the_restart_hint_compares_with_the_language_this_run_started_with(tmp_path, monkeypatch):
    """Preferences opened again after a change still says to restart, and going back to the
    language in use takes the hint away: the row compares with the choice the run started
    with (app.setup_language), not with the one saved since."""
    locale = catalog(tmp_path)
    monkeypatch.setattr(i18n, "LOCALE_DIR", locale)
    monkeypatch.delenv("LANGUAGE", raising=False)
    settings = GuiSettings.load(tmp_path / "gui.json")
    assert app_mod.setup_language(settings) == "en" and i18n.started_choice == ""
    first = _preferences(settings)
    first.language_row.set_selected(2)                          # xx, from the next start
    assert first.language_row.get_subtitle() == RESTART_FOR_LANGUAGE
    again = _preferences(settings)                              # the same run
    assert again.language_codes[again.language_row.get_selected()] == "xx"
    assert again.language_row.get_subtitle() == RESTART_FOR_LANGUAGE
    again.language_row.set_selected(0)                          # System default: in use
    assert not again.language_row.get_subtitle()
    again.language_row.set_selected(1)                          # English
    assert again.language_row.get_subtitle() == RESTART_FOR_LANGUAGE
    # a run that started with xx: xx is the one in use
    settings.language = "xx"
    assert app_mod.setup_language(settings) == "xx" and i18n.started_choice == "xx"
    row = _preferences(settings).language_row
    assert row.get_selected() == 2 and not row.get_subtitle()
    # a saved language whose catalog is gone: the run uses the system's, so the row says to
    # restart for the saved one
    monkeypatch.delenv("LANGUAGE", raising=False)
    settings.language = "ro"
    assert app_mod.setup_language(settings) == "en" and i18n.started_choice == ""
    assert _preferences(settings).language_row.get_subtitle() == RESTART_FOR_LANGUAGE


def test_the_default_media_size_names_the_paper_in_the_window_language(tmp_path):
    i18n.setup("xx", localedir=catalog(tmp_path))
    prefs = _preferences(GuiSettings.load(tmp_path / "gui.json"))
    model = prefs.media_row.get_model()
    sizes = [model.get_string(i) for i in range(model.get_n_items())]
    assert any(s.startswith("[Letter] · ") for s in sizes), sizes
    assert not any(s.startswith("Letter ") for s in sizes)
