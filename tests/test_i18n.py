# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The translations (opencutter.i18n, po/, build-aux/translations.py): every marked text is in
po/opencutter.pot, the compiled catalogs match the .po files, a translation that would break the
window is refused, the machine's own words are in the catalog, and the window's language follows
the system or the Preferences. No display needed; the catalogs used here are made in tmp_path."""
import ast
import gettext
import importlib.util
import sys
import io
import re
from pathlib import Path

import pytest

from opencutter import i18n
from opencutter.gui.settings import GuiSettings
from opencutter.machines import enabled
from opencutter.presets.model import BASIC_PRESETS

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "build-aux" / "translations.py"
_spec = importlib.util.spec_from_file_location("translations", SCRIPT)
tr = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("translations", tr)   # dataclasses look it up
_spec.loader.exec_module(tr)

MARKERS = set(tr.KEYWORDS)


def make_po(lang: str, entries: list[tuple], plural_forms: str | None = None) -> str:
    """A .po file: entries are (context, msgid, msgstr) or (context, msgid, plural, [forms])."""
    messages = []
    for e in entries:
        if len(e) == 3:
            messages.append(tr.Message(msgid=e[1], context=e[0], msgstr=[e[2]]))
        else:
            messages.append(tr.Message(msgid=e[1], context=e[0], plural=e[2], msgstr=list(e[3])))
    fields = [*tr.HEADER_FIELDS, ("Language", lang),
              ("Plural-Forms", plural_forms or tr.PLURAL_FORMS.get(lang, "nplurals=2; plural=(n != 1);"))]
    header = tr.Message(msgid="", msgstr=[tr.header_text(fields)], comments=list(tr.SPDX))
    return tr.format_po([header, *messages])


def install(tmp_path: Path, lang: str, entries: list[tuple], **kw) -> Path:
    """Compile a catalog for `lang` into tmp_path/locale and return that folder."""
    messages = tr.parse_po(make_po(lang, entries, **kw))
    path = tr.mo_path(lang, tmp_path / "locale")
    path.parent.mkdir(parents=True)
    path.write_bytes(tr.compile_mo(messages))
    return tmp_path / "locale"


# --- the template and the catalogs of the repository ------------------------------------------
def test_the_template_and_the_catalogs_are_up_to_date():
    """po/opencutter.pot holds every marked text and nothing else, every language of LINGUAS has
    its .po and a catalog compiled from it, and no translation would break the window. On a
    failure: python3 build-aux/translations.py extract (or update, compile)."""
    assert tr.check_all() == []


def test_every_marked_text_is_in_the_template():
    pot = tr.parse_po((REPO / "po" / "opencutter.pot").read_text(encoding="utf-8"))
    in_template = {(m.context, m.msgid, m.plural) for m in pot if m.msgid}
    marked = {(m.context, m.msgid, m.plural) for m in tr.extract()}
    assert marked == in_template
    assert len(marked) > 400


def _marked_files() -> set[str]:
    found = set()
    for path in sorted((REPO / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in MARKERS and node.args
                    and any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                            for a in node.args)):
                found.add(path.relative_to(REPO).as_posix())
                break
    return found


def test_every_file_with_marked_texts_is_in_potfiles():
    listed = tr.potfiles()
    assert len(listed) == len(set(listed))
    assert all((REPO / name).is_file() for name in listed)
    assert _marked_files() == set(listed)


def test_the_catalogs_are_package_data():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"locale/*/LC_MESSAGES/*.mo"' in pyproject
    assert i18n.LOCALE_DIR == REPO / "src" / "opencutter" / "locale"
    assert set(i18n.available_languages()) == set(tr.linguas())


def test_the_po_folder_files_carry_the_spdx_header():
    for name in ("POTFILES", "LINGUAS", "opencutter.pot", *(f"{l}.po" for l in tr.linguas())):
        lines = (REPO / "po" / name).read_text(encoding="utf-8").splitlines()
        assert lines[:2] == ["# SPDX-License-Identifier: GPL-3.0-or-later",
                             "# SPDX-FileCopyrightText: 2026 OpenCutter contributors"], name


def test_the_glossary_names_every_language_and_holds_no_translation():
    text = (REPO / "po" / "GLOSSARY.md").read_text(encoding="utf-8")
    for code in ("ro", "de", "fr", "es", "it", "pt", "ru"):
        assert f"`{code}`" in text, code
    for term in ("Material", "Action", "Tool", "Blade Depth", "Force", "Speed", "Passes",
                 "Test Cut", "Send", "Cutting Mat", "Page Setup", "Load Media", "AutoBlade"):
        assert f"**{term}**" in text, term


# --- the machine's and the presets' own words -------------------------------------------------
def _template_keys() -> set[tuple[str | None, str]]:
    pot = tr.parse_po((REPO / "po" / "opencutter.pot").read_text(encoding="utf-8"))
    return {(m.context, m.msgid) for m in pot}


def test_every_word_of_the_machine_profiles_is_in_the_catalog():
    keys = _template_keys()
    for p in enabled():
        for _digit, term in p.status.texts:
            assert (i18n.STATUS, term) in keys, (p.id, term)
        for h in p.holders:
            label = "Tool {number}" if re.fullmatch(r"Tool [0-9]+", h.label) else h.label
            assert (i18n.HOLDER, label) in keys, (p.id, h.label)
            for t in h.tools:
                assert t.name in i18n.PRODUCT_NAMES or (i18n.TOOL, t.name) in keys, (p.id, t.name)
                assert (i18n.ACTION, t.action) in keys, (p.id, t.action)


def test_every_word_of_the_basic_set_is_in_the_catalog():
    keys = _template_keys()
    for p in BASIC_PRESETS:
        assert (i18n.MATERIAL, p.material) in keys
        assert (i18n.MATERIAL, p.material_type) in keys
        assert (i18n.ACTION, p.action) in keys
        assert p.tool in i18n.PRODUCT_NAMES or (i18n.TOOL, p.tool) in keys


# --- the tool: reading, writing, compiling, checking -----------------------------------------
ENTRIES = [
    (None, "Send", "Trimite"),
    ("action", "Cut", "Tăiere"),
    ("button", "Cut", "Taie"),
    (None, "{n} pass", "{n} passes", ["{n} trecere", "{n} treceri", "{n} de treceri"]),
    (None, "Line one\nand a \"quoted\" second line with a\ttab", "Rândul unu\nși al doilea"),
    (None, "Untranslated", ""),
]


def test_a_po_file_reads_back_as_it_was_written():
    text = make_po("ro", ENTRIES)
    messages = tr.parse_po(text)
    assert tr.format_po(messages) == text
    by_key = {m.key: m for m in messages}
    assert by_key[("action", "Cut")].msgstr == ["Tăiere"]
    assert by_key[(None, "{n} pass")].plural == "{n} passes"
    assert by_key[(None, "Line one\nand a \"quoted\" second line with a\ttab")].msgstr == [
        "Rândul unu\nși al doilea"]


def test_a_compiled_catalog_is_what_gettext_reads():
    messages = tr.parse_po(make_po("ro", ENTRIES))
    data = tr.compile_mo(messages)
    assert data == tr.compile_mo(tr.parse_po(make_po("ro", ENTRIES)))     # the same bytes
    t = gettext.GNUTranslations(io.BytesIO(data))
    assert t.gettext("Send") == "Trimite"
    assert t.pgettext("action", "Cut") == "Tăiere" and t.pgettext("button", "Cut") == "Taie"
    assert t.gettext("Cut") == "Cut"
    assert [t.ngettext("{n} pass", "{n} passes", n) for n in (1, 2, 20)] == [
        "{n} trecere", "{n} treceri", "{n} de treceri"]
    assert t.gettext("Untranslated") == "Untranslated"
    assert t.info()["language"] == "ro"


def test_a_fuzzy_or_obsolete_entry_stays_english():
    text = make_po("de", [(None, "Send", "Senden"), (None, "Open", "Öffnen")])
    text = text.replace('msgid "Open"', '#, fuzzy\nmsgid "Open"')
    text += '\n#~ msgid "Gone"\n#~ msgstr "Weg"\n'
    messages = tr.parse_po(text)
    assert [m.msgid for m in messages if m.obsolete] == ["Gone"]
    t = gettext.GNUTranslations(io.BytesIO(tr.compile_mo(messages)))
    assert t.gettext("Send") == "Senden"
    assert t.gettext("Open") == "Open" and t.gettext("Gone") == "Gone"


def test_merge_keeps_translations_adds_new_texts_and_keeps_old_ones_obsolete():
    old = make_po("fr", [(None, "Send", "Envoyer"), (None, "Gone", "Parti")])
    template = [tr.Message(msgid="Send", references=["a.py"]),
                tr.Message(msgid="New", references=["b.py"]),
                tr.Message(msgid="{n} path", plural="{n} paths", msgstr=["", ""])]
    merged = tr.parse_po(tr.merge(old, template))
    by_key = {m.key: m for m in merged}
    assert by_key[(None, "Send")].msgstr == ["Envoyer"]
    assert by_key[(None, "Send")].references == ["a.py"]
    assert by_key[(None, "New")].msgstr == [""]
    assert by_key[(None, "{n} path")].msgstr == ["", ""]
    assert by_key[(None, "Gone")].obsolete and by_key[(None, "Gone")].msgstr == ["Parti"]
    assert tr.merge(tr.format_po(merged), template) == tr.format_po(merged)


def test_a_new_po_file_has_the_languages_plural_forms():
    template = [tr.Message(msgid="{n} path", plural="{n} paths", msgstr=["", ""]),
                tr.Message(msgid="Send")]
    for lang, forms in (("ro", 3), ("ru", 3), ("fr", 2), ("de", 2)):
        messages = tr.parse_po(tr.new_po(lang, template))
        header = tr.header_fields(messages[0].msgstr[0])
        assert header["Language"] == lang
        assert tr.nplurals_of(header["Plural-Forms"]) == forms
        assert messages[1].msgstr == [""] * forms
        assert tr.problems_of(messages, lang) == []


@pytest.mark.parametrize("msgid, msgstr, problem", [
    ("Use {name}", "Folosește {nume}", "placeholders"),
    ("Use {name}", "Folosește", "placeholders"),
    ("Use {name}", "Folosește {name", "'}'"),           # .format() cannot read it
    ("Press <b>Load</b>.", "Apăsați Load.", "markup tags"),
    ("Press <b>Load</b>.", "Apăsați <b>Load</b> & gata.", "bare &"),
])
def test_a_translation_that_would_break_the_window_is_refused(msgid, msgstr, problem, tmp_path):
    messages = tr.parse_po(make_po("ro", [(None, msgid, msgstr)]))
    found = tr.problems_of(messages, "po/ro.po")
    assert any(problem in f for f in found), found
    po_dir = tmp_path / "po"
    po_dir.mkdir()
    (po_dir / "LINGUAS").write_text("ro\n")
    (po_dir / "ro.po").write_text(make_po("ro", [(None, msgid, msgstr)]))
    assert tr.compile_all(po_dir, tmp_path / "locale") == found
    assert not (tmp_path / "locale").exists()          # nothing written


def test_compile_writes_the_catalogs_of_linguas_and_removes_others(tmp_path):
    po_dir, locale = tmp_path / "po", tmp_path / "locale"
    po_dir.mkdir()
    (po_dir / "LINGUAS").write_text("# planned\nde fr\n")
    (po_dir / "de.po").write_text(make_po("de", [(None, "Send", "Senden")]))
    (po_dir / "fr.po").write_text(make_po("fr", [(None, "Send", "Envoyer")]))
    stale = tr.mo_path("it", locale)
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    assert tr.compile_all(po_dir, locale) == []
    assert sorted(p.name for p in locale.iterdir()) == ["de", "fr"]
    assert i18n.available_languages(locale) == ["de", "fr"]
    (po_dir / "de.po").write_text(make_po("de", [(None, "Send", "Schicken")]))
    assert tr.compile_mo(tr.read_po("de", po_dir)) != tr.mo_path("de", locale).read_bytes()


def test_extract_takes_literals_contexts_plurals_and_translator_comments(tmp_path):
    source = tmp_path / "src" / "mod.py"
    source.parent.mkdir()
    source.write_text(
        "from opencutter.i18n import _, N_, NC_, ngettext, pgettext\n"
        "A = N_('Constant')\n"
        "# Translators: a short word.\n"
        "B = pgettext('button', 'Cut')\n"
        "C = ngettext('{n} path', '{n} paths', 3).format(n=3)\n"
        "D = _(A)               # a variable: marked where it was made\n"
        "E = NC_('action', 'Cut')\n"
        "F = _('Constant')\n")
    po_dir = tmp_path / "po"
    po_dir.mkdir()
    (po_dir / "POTFILES").write_text("# comment\nsrc/mod.py\n")
    messages = tr.extract(tmp_path, po_dir)
    assert [(m.context, m.msgid, m.plural) for m in messages] == [
        (None, "Constant", None), ("button", "Cut", None), (None, "{n} path", "{n} paths"),
        ("action", "Cut", None)]
    assert messages[1].extracted == ["Translators: a short word."]
    assert messages[2].flags == ["python-brace-format"]
    assert messages[0].references == ["src/mod.py"]


# --- the window's language --------------------------------------------------------------------
def test_without_setup_every_text_is_english():
    assert i18n._("Send") == "Send"
    assert i18n.pgettext("action", "Cut") == "Cut"
    assert i18n.ngettext("{n} pass", "{n} passes", 2) == "{n} passes"
    assert i18n.translate_text("Use CAMEO 5") == "Use CAMEO 5"
    assert i18n.current_language() == "en"


def test_setup_takes_the_chosen_language_or_the_systems(tmp_path, monkeypatch):
    locale = install(tmp_path, "ro", ENTRIES)
    i18n.setup("ro", localedir=locale)
    assert i18n._("Send") == "Trimite" and i18n.current_language() == "ro"
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "ro_RO.UTF-8")
    i18n.setup(None, localedir=locale)                 # ro_RO falls back to ro
    assert i18n._("Send") == "Trimite"
    monkeypatch.setenv("LANGUAGE", "de:ro")            # the first with a catalog
    i18n.setup("", localedir=locale)
    assert i18n._("Send") == "Trimite"
    i18n.setup("de", localedir=locale)                 # no catalog: English
    assert i18n._("Send") == "Send" and i18n.current_language() == "en"
    i18n.use_english()
    assert i18n._("Send") == "Send"


def test_the_language_names_are_their_own():
    assert i18n.language_name("ro") == "Română" and i18n.language_name("ru") == "Русский"
    assert i18n.language_name("pt_BR") == "Português" and i18n.language_name("xx") == "xx"
    for code in ("ro", "de", "fr", "es", "it", "pt", "ru"):
        assert code in i18n.LANGUAGE_NAMES
    assert i18n.valid_language("") and i18n.valid_language("pt_BR")
    assert not i18n.valid_language("../ro") and not i18n.valid_language(None)


def test_apply_language_choice_sets_language_only_for_a_choice(monkeypatch):
    monkeypatch.setenv("LANGUAGE", "fr")
    i18n.apply_language_choice("")
    assert i18n.os.environ["LANGUAGE"] == "fr"
    i18n.apply_language_choice("ro")
    assert i18n.os.environ["LANGUAGE"] == "ro"


def test_the_language_is_kept_in_the_settings(tmp_path):
    settings = GuiSettings.load(tmp_path / "gui.json")
    assert settings.language == ""
    settings.language = "ro"
    settings.save()
    assert GuiSettings.load(tmp_path / "gui.json").language == "ro"
    (tmp_path / "gui.json").write_text('{"language": "../../etc"}')
    assert GuiSettings.load(tmp_path / "gui.json").language == ""
    (tmp_path / "gui.json").write_text('{"language": 7}')
    assert GuiSettings.load(tmp_path / "gui.json").language == ""


# --- the finished English sentences ------------------------------------------------------------
SENTENCES = [
    (None, "Use {name}", "Folosește {name}"),
    (None, "The {name} is free for other apps. Press Connect to use it here again.",
     "{name} este liber pentru alte aplicații. Apăsați Conectare ca să-l folosiți din nou aici."),
    (None, "{what} was not sent: {reason}.", "{what} nu a fost trimis: {reason}."),
    (None, "Resume", "Reluare"),
    (None, "the Bluetooth link was lost", "legătura Bluetooth s-a pierdut"),
    (None, "status {status} = {term}", "starea {status} = {term}"),
    (None, "The machine is busy ({shown}): it is still working, or another connection such as "
     "USB holds it. Nothing was sent. Wait until it shows Ready, then try again.",
     "Aparatul e ocupat ({shown}). Nu s-a trimis nimic."),
    ("machine status", "Busy", "Ocupat"),
    ("tool holder", "Tool {number}", "Unealta {number}"),
    (None, "The {name} has no {label}.", "{name} nu are {label}."),
    (None, "{title}. {problem}", "{title}: {problem}"),
    (None, "Load the {tool} into {holder}.", "Puneți {tool} în {holder}."),
    ("tool", "Sketch Pen", "Pix de schiță"),
]


def test_translate_text_translates_finished_sentences(tmp_path):
    from opencutter.device.protocol import not_ready_text
    from opencutter.gui import machine, runner, sending
    i18n.setup("ro", localedir=install(tmp_path, "ro", SENTENCES))
    t = i18n.translate_text
    assert t(sending.USE_MACHINE.format(name="CAMEO 5")) == "Folosește CAMEO 5"
    assert t(machine.released("CAMEO 4")) == ("CAMEO 4 este liber pentru alte aplicații. Apăsați "
                                              "Conectare ca să-l folosiți din nou aici.")
    note = runner.COMMAND_REFUSED.format(what="Resume", reason=runner.LINK_LOST)
    assert t(note) == "Reluare nu a fost trimis: legătura Bluetooth s-a pierdut."
    assert t(not_ready_text("1")) == "Aparatul e ocupat (starea '1' = Ocupat). Nu s-a trimis nimic."
    assert t(sending.NO_SUCH_HOLDER.format(name="CAMEO 5", label="Tool 3")) == (
        "CAMEO 5 nu are Unealta 3.")
    assert t("Load the Sketch Pen into Tool 2.") == "Puneți Pix de schiță în Unealta 2."
    assert t("Busy") == "Ocupat"
    assert i18n.status_label("Busy") == "Ocupat" and i18n.holder_label("Tool 1") == "Unealta 1"
    # a sentence no template made stays as it was, and a template with little fixed text
    # ("{title}. {problem}") never takes a sentence apart
    assert t("Something else. Entirely.") == "Something else. Entirely."
    assert t(None) is None and t("") == ""


def test_translate_text_survives_a_broken_catalog(tmp_path):
    """A catalog compiled without the checks (by hand): a translation with another placeholder
    must not raise in the window, the English sentence is shown."""
    messages = tr.parse_po(make_po("ro", [(None, "Use {name}", "Folosește {nume}")]))
    path = tr.mo_path("ro", tmp_path / "locale")
    path.parent.mkdir(parents=True)
    path.write_bytes(tr.compile_mo(messages))
    i18n.setup("ro", localedir=tmp_path / "locale")
    assert i18n.translate_text("Use CAMEO 5") == "Use CAMEO 5"


# --- no sentence is half translated ----------------------------------------------------------
# made-up translations in brackets: nobody's translation, and easy to spot in a sentence
LOOSE = [
    (None, "{action} with the {tool}", "[{action} WITH {tool}]"),
    (None, "Blade Depth {value}", "[DEPTH {value}]"),
    (None, "Speed {value}", "[SPEED {value}]"),
    (None, "Use {name}", "[USE {name}]"),
    (None, "status {status} = {term}", "[STATUS {status} = {term}]"),
    ("machine status", "Cancelling", "[Cancelling]"),
    ("machine status", "Busy", "[Busy]"),
    ("action", "Cut", "[Cut]"),
    ("tool", "Ratchet Blade", "[Ratchet Blade]"),
    ("tool holder", "Tool {number}", "[Tool {number}]"),
]


def _job_session(name: str = "job"):
    from types import SimpleNamespace
    from opencutter.machines import CAMEO4
    return SimpleNamespace(name=name, profile=CAMEO4)


def test_a_short_template_never_takes_a_piece_of_another_sentence(tmp_path):
    """The service's stop text on a cancel holds " with the ", and a validation refusal starts
    with "Blade Depth": without their own templates in the catalog they stay wholly English,
    never "... ended the job [WITH stop] and sends ..."."""
    from opencutter.device import daemon
    from opencutter.job import validate
    i18n.setup("ro", localedir=install(tmp_path, "ro", LOOSE))
    t = i18n.translate_text
    stop = daemon._status_stop_text("4", _job_session())
    assert stop == ("The machine cancelled the job (status '4' = Cancelling). OpenCutter ended "
                    "the job with the stop and sends nothing more. Press Unload on the machine, "
                    "check that the tool is up, then load the mat and send the job again.")
    assert t(stop) == stop
    for status in ("1", "3", "5", "7"):
        for before in (False, True):
            text = daemon._status_stop_text(status, _job_session(), before)
            assert t(text) == text, text
    depth = validate.DEPTH_NEEDS_DETECTED.format(tool="Ratchet Blade", holder="Tool 1")
    assert depth == "Blade Depth (TF) needs the Ratchet Blade detected in Tool 1"
    assert t(depth) == depth
    for text in ("Use the Pause key on the machine to continue.", "Speed 1 is too slow; try 2.",
                 "OpenCutter ended the job with the stop and sends nothing more",
                 "OpenCutter ended the job with the stop"):
        assert t(text) == text
    # the same short templates still translate what they made
    assert t("Cut with the Ratchet Blade") == "[[Cut] WITH [Ratchet Blade]]"
    assert t("Speed 5") == "[SPEED 5]" and t("Blade Depth 3") == "[DEPTH 3]"
    assert t("Use CAMEO 5 Alpha Plus") == "[USE CAMEO 5 Alpha Plus]"


def test_the_stop_and_refusal_texts_translate_from_their_own_templates(tmp_path):
    from opencutter.device import daemon
    from opencutter.gui import runner
    from opencutter.job import validate
    entries = LOOSE + [
        (None, daemon.STOP_CANCELLING, "[CANCELLED ({shown}); UNLOAD]"),
        (None, daemon.STOP_STILL_BUSY_BEFORE, "[BUSY {seconds} s ({shown}); NOTHING; UNLOAD]"),
        (None, runner.COMMAND_REFUSED, "[{what} NOT SENT: {reason}]"),
        (None, "Resume", "[Resume]"),
        (None, daemon.NOT_PAUSED, "[NOT PAUSED ({shown})]"),
        (None, daemon.NO_JOB_RUNNING, "[NO JOB]"),
        (None, validate.DEPTH_NEEDS_DETECTED, "[DEPTH NEEDS {tool} IN {holder}]"),
        (None, validate.OUTSIDE_MEDIA, "[GROUP {group}: ({x}, {y}) OUTSIDE]"),
    ]
    i18n.setup("ro", localedir=install(tmp_path, "ro", entries))
    t = i18n.translate_text
    assert t(daemon._status_stop_text("4", _job_session())) == (
        "[CANCELLED ([STATUS '4' = [Cancelling]]); UNLOAD]")
    assert t(daemon._status_stop_text("1", _job_session(), True)) == (
        f"[BUSY {daemon.PRE_STEP_WAIT:g} s ([STATUS '1' = [Busy]]); NOTHING; UNLOAD]")
    shown = daemon.STATUS_SHOWN.format(status=repr("1"), term="Busy")
    for reason, expected in ((daemon.NOT_PAUSED.format(shown=shown),
                              "[NOT PAUSED ([STATUS '1' = [Busy]])]"),
                             (daemon.NO_JOB_RUNNING, "[NO JOB]")):
        note = runner.command_note("resume", {"ok": False, "sent": False, "error": reason})
        assert t(note) == f"[[Resume] NOT SENT: {expected}]"
    assert t(validate.DEPTH_NEEDS_DETECTED.format(tool="Ratchet Blade", holder="Tool 1")) == (
        "[DEPTH NEEDS [Ratchet Blade] IN [Tool 1]]")
    assert t("group 1: point (150.00, 30.00) mm lies outside the media") == (
        "[GROUP 1: (150.00, 30.00) OUTSIDE]")


def test_the_marked_service_and_validation_texts_keep_their_english():
    """The service's texts (its log, the command line) are what they were before they were
    marked: a job's and a program's stop texts, the refusals of Resume and Pause, and the
    validation refusals."""
    from opencutter.device import daemon
    from opencutter.job import validate
    job, program = _job_session(), _job_session("program")
    assert daemon._status_stop_text("3", job, True) == (
        "The machine was paused (status '3' = Paused) before this step; nothing of this step "
        "was sent. OpenCutter ended the job with the stop and sends nothing more: press Unload "
        "on the machine (or switch it off), check that the tool is up, then load the mat and "
        "send the job again.")
    assert daemon._status_stop_text("4", program) == (
        "The machine cancelled the program (status '4' = Cancelling). OpenCutter ended the "
        "program with the stop and sends nothing more. Press Unload on the machine, check that "
        "the tool is up, then load the mat and run the program again.")
    assert daemon._status_stop_text("7", program, True) == (
        "unexpected status '7' = ? before this step; nothing of this step was sent, and "
        "OpenCutter ended the program with the stop")
    assert daemon._status_stop_text("7", job) == "unexpected status '7' = ?"
    assert daemon.UNTESTED_COMMAND["pause"].format(model="CAMEO 5") == (
        "the CAMEO 5 has not been tested with pause: press its Pause key")
    assert daemon.NO_STATUS_WITHIN.format(seconds=f"{daemon.RESUME_LOCK_WAIT:g}") == (
        f"the machine is not answering (its status could not be read within "
        f"{daemon.RESUME_LOCK_WAIT:g} s)")
    assert daemon.COMMAND_WRITE_FAILED["resume"].format(error="OSError: x") == (
        "the resume write failed (OSError: x); whether it arrived is unknown, and it is not "
        "sent again")
    assert validate.OUTSIDE_CUT_AREA.format(group=2, x=f"{1.5:.2f}", y=f"{-3:.2f}") == (
        "group 2: point (1.50, -3.00) mm lies outside the cut area")
    assert validate.TOO_MANY_POINTS.format(count=validate.MAX_POINTS) == "more than 500000 points"


def test_a_placeholder_value_is_translated_in_its_word_context_first(tmp_path):
    """The holder of a one-holder model is "Tool": in "{holder}: {tool}, ..." it is the holder,
    as the sidebar names it, not the plain word of the sidebar's Tool column."""
    entries = [(None, "Tool", "[tool column]"), ("tool holder", "Tool", "[holder]"),
               ("tool", "Sketch Pen", "[pen]"),
               (None, "{holder}: {tool}, Force {force}, Speed {speed}",
                "[{holder}: {tool}, F{force}, S{speed}]"),
               (None, "Load the {tool} into {holder}.", "[LOAD {tool} INTO {holder}]")]
    i18n.setup("ro", localedir=install(tmp_path, "ro", entries))
    t = i18n.translate_text
    assert i18n.holder_label("Tool") == "[holder]"
    assert t("Tool: Sketch Pen, Force 10, Speed 5") == "[[holder]: [pen], F10, S5]"
    assert t("Load the Sketch Pen into Tool.") == "[LOAD [pen] INTO [holder]]"
    assert t("Tool") == "[tool column]"            # the word alone: the plain catalog


def test_a_word_placeholder_never_takes_a_piece_of_another_sentence(tmp_path):
    """A holder, tool or action placeholder holds a name: with any template, long or short, a
    piece of another sentence there (even one another template translates) leaves the whole
    sentence English, never half translated."""
    entries = [("tool", "Sketch Pen", "[pen]"), ("tool holder", "Tool {number}", "[T{number}]"),
               (None, "Load the {tool} into {holder}.", "[LOAD {tool} INTO {holder}]"),
               (None, "{action} with the {tool}", "[{action} WITH {tool}]"),
               (None, "{problem}; nothing was sent", "[{problem}; NOTHING]"),
               (None, "the Bluetooth link was lost", "[LINK LOST]"),
               (None, "unexpected status {status} = {term}", "[UNEXPECTED {status} = {term}]")]
    i18n.setup("ro", localedir=install(tmp_path, "ro", entries))
    t = i18n.translate_text
    for text in ("Load the Sketch Pen into Tool 1 and press Unload when done.",
                 "the Bluetooth link was lost; nothing was sent with the pen"):
        assert t(text) == text
    # what the templates made still translates; a value without a letter is a plain value
    assert t("Load the Sketch Pen into Tool 1.") == "[LOAD [pen] INTO [T1]]"
    assert t("the Bluetooth link was lost; nothing was sent") == "[[LINK LOST]; NOTHING]"
    assert t("unexpected status '7' = ?") == "[UNEXPECTED '7' = ?]"


def test_the_window_shows_these_texts_in_its_language(tmp_path):
    """Stop Machine's alert and the job view (STILL_PAUSED), the manual mode's material, and
    the hint under a machine of another model."""
    from opencutter.gui import jobview, machine
    from opencutter.gui.geometry import media_label
    english_hint = machine.drivable_hint(shown=True)
    assert english_hint == machine.OTHER_MACHINE_HINT          # the same English text
    entries = [(None, jobview.STILL_PAUSED, "[STILL PAUSED {n} s]"),
               (None, "Cut Settings", "[Cut Settings]"),
               ("paper size", "Letter", "[Letter]"),
               (None, "{items} and {last}", "{items} [AND] {last}"),
               ("list separator", ", ", " [,] "),
               ("list separator", " or ", " [OR] "),
               (None, "{model} (firmware {lines})", "{model} [FIRMWARE {lines}]"),
               (None, "OpenCutter drives these models: {models}. {hint}", "[DRIVES {models}] {hint}"),
               (None, machine.CONNECT_HINT_TAIL, "[PAIR IT]")]
    i18n.setup("ro", localedir=install(tmp_path, "ro", entries))
    assert jobview.still_paused_text(6.2) == "[STILL PAUSED 6 s]"
    assert i18n.material_label(i18n.MANUAL_MATERIAL) == "[Cut Settings]"
    assert i18n.material_label("Vinyl") == "Vinyl"              # not in this catalog
    assert media_label("Letter") == "[Letter]"
    hint = machine.drivable_hint(shown=True)
    assert hint.startswith("[DRIVES CAMEO 4 [FIRMWARE V1.xx [OR] V5.xx] [,] ") and hint.endswith(
        " [AND] Portrait 4] [PAIR IT]")
    assert " or " not in hint and " and " not in hint and "firmware" not in hint
    assert machine.OTHER_MACHINE_HINT == english_hint            # the English text is kept


# --- build-aux/translations.py and the files GNU gettext writes --------------------------------
def test_an_obsolete_entry_keeps_its_fuzzy_flag():
    """A fuzzy translation whose text goes away and comes back stays fuzzy: compile leaves it
    out, as before the text went away."""
    old = make_po("de", [(None, "Keep", "[keep]"), (None, "Gone", "[fuzzy old]")])
    old = old.replace('msgid "Gone"', '#, fuzzy\nmsgid "Gone"')
    without = [tr.Message(msgid="Keep")]
    gone = tr.merge(old, without)
    assert '#, fuzzy\n#~ msgid "Gone"\n#~ msgstr "[fuzzy old]"' in gone
    assert tr.parse_po(gone)[-1].flags == ["fuzzy"] and tr.parse_po(gone)[-1].obsolete
    back = tr.merge(gone, [tr.Message(msgid="Keep"), tr.Message(msgid="Gone")])
    entry = {m.key: m for m in tr.parse_po(back)}[(None, "Gone")]
    assert entry.fuzzy and not entry.obsolete and entry.msgstr == ["[fuzzy old]"]
    t = gettext.GNUTranslations(io.BytesIO(tr.compile_mo(tr.parse_po(back))))
    assert t.gettext("Gone") == "Gone" and t.gettext("Keep") == "[keep]"
    assert tr.merge(back, [tr.Message(msgid="Keep"), tr.Message(msgid="Gone")]) == back


def test_a_previous_text_line_of_an_obsolete_entry_is_read():
    """msgmerge --previous writes '#~| msgid' lines before an obsolete entry: skipped, like
    '#|' before a live one."""
    text = make_po("de", [(None, "Keep", "[keep]")])
    text += '\n#, fuzzy\n#~| msgid "Old"\n#~ msgid "Older"\n#~ msgstr "[older]"\n'
    messages = tr.parse_po(text)
    last = messages[-1]
    assert last.obsolete and last.msgid == "Older" and last.msgstr == ["[older]"]
    assert last.fuzzy
    assert [m.msgid for m in messages if not m.obsolete] == ["", "Keep"]
