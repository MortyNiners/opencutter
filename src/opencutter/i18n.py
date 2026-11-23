# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Translations of the window's texts: gettext, domain 'opencutter'.

English is the source language and the fallback. The compiled catalogs are package data
(``locale/<language>/LC_MESSAGES/opencutter.mo`` next to this file), so a source checkout, a pip
install and the Flatpak find them in the same place. They are compiled from po/<language>.po by
build-aux/translations.py (a pure-Python tool: no host gettext needed) and kept in the repository; a test
checks that they match the .po files.

Only the window is translated. The command line, the background service and the logs stay in
English: ``setup()`` is called by the GUI's main() alone, and until then every function here
gives the English text.

Marking texts
  _("text")                     a text shown in the window, translated when called;
  ngettext(one, many, n)        a text with a count (placeholders: ``{n}``);
  pgettext(context, "text")     one English word with two meanings ("Cut" the action, "Cut" the
                                button; "Portrait" the orientation, "Portrait" the machine);
  N_("text"), NC_(context, ...) mark a text for the catalog without translating it: a module
                                constant (imported before setup()) or a text the service sends.
                                The place that shows it translates it (_(), pgettext()).
  Placeholders are named (``{name}``) so a translation may put them in another order; the
  English text stays the one the code had.

Finished English sentences: the service's texts (refusals, reasons, step titles) reach the window
as finished sentences, and the machine state keeps its texts in English too (the logic compares
them). Their templates are marked with N_(), and ``translate_text()`` translates such a sentence
where the window shows it: the exact text when it is in the catalog, else the most specific marked
template it was made from, with its placeholder values translated in turn.
"""
from __future__ import annotations

import gettext
import os
import re
from pathlib import Path

DOMAIN = "opencutter"
LOCALE_DIR = Path(__file__).with_name("locale")
SOURCE_LANGUAGE = "en"
# The languages' own names, for the Preferences row (a language missing here shows its code).
LANGUAGE_NAMES = {
    "de": "Deutsch",
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "it": "Italiano",
    "pt": "Português",
    "ro": "Română",
    "ru": "Русский",
}
# a language code as gettext names it: "de", "pt_BR", "sr@latin"
LANGUAGE_CODE = re.compile(r"[a-z]{2,3}(_[A-Z]{2})?(@[a-z]+)?")
SERVICE_DEPTH = 3                 # how deep translate_text() translates placeholder values
                                  # (a refusal of Resume, its reason, the status in it)
MIN_TEMPLATE_TEXT = 8             # fixed characters a template needs unless it starts with them
# A template with less fixed text than this ("{action} with the {tool}", "Speed {value}") is a
# short one: it takes a sentence only when each placeholder value it leaves untranslated is a
# plain value (a number, a name: PLAIN_VALUE_WORDS words at most, PLAIN_VALUE_LENGTH characters
# at most, no end of a sentence or clause in it), never a piece of another sentence. A short
# template that starts with a placeholder has only a connective (" with the ") to go by: there
# a plain value has LOOSE_VALUE_WORDS words at most.
STRONG_TEMPLATE_TEXT = 16
PLAIN_VALUE_WORDS = 4
LOOSE_VALUE_WORDS = 2
PLAIN_VALUE_LENGTH = 32

_translation: gettext.NullTranslations = gettext.NullTranslations()
_templates: list[tuple[int, re.Pattern, str]] | None = None
# The Preferences' language choice this run started with ('' the system's language), as
# gui.app.setup_language() used it; None before that. The Language row compares with it.
started_choice: str | None = None


def _(message: str) -> str:
    """The translation of `message`, or `message` itself."""
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """The translation of a text with the count `n`, in the right plural form."""
    return _translation.ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    """The translation of `message` in `context` (one English word, two meanings)."""
    return _translation.pgettext(context, message)


def npgettext(context: str, singular: str, plural: str, n: int) -> str:
    """ngettext() in `context`."""
    return _translation.npgettext(context, singular, plural, n)


def N_(message: str) -> str:
    """Marks `message` for the catalog; the place that shows it translates it."""
    return message


def NC_(context: str, message: str) -> str:
    """Marks `message` in `context` for the catalog; shown with pgettext(context, message)."""
    return message


# The contexts of the machine's and the presets' own words (pgettext): the labels below, and the
# placeholder values translate_text() meets ("Busy" in "It still reports its status (Busy).").
ACTION = "action"                 # what a preset does: Cut, Score, Sketch
MATERIAL = "material"             # a material or material type of the Basic set
TOOL = "tool"                     # a tool the machine detects: Ratchet Blade, Sketch Pen
HOLDER = "tool holder"            # Tool 1, Tool 2, Tool
STATUS = "machine status"         # what the machine reports: Ready, Busy, Load Media, Paused
WORD_CONTEXTS = (STATUS, TOOL, HOLDER, ACTION, MATERIAL)


def _label(context: str, text: str | None) -> str | None:
    return pgettext(context, text) if text else text


def action_label(name: str | None) -> str | None:
    """An action's name ('Cut', 'Sketch') in the window's language; a name of a presets file
    that is not in the catalog stays as written."""
    return _label(ACTION, name)


# the material the sidebar names in its manual mode (no preset: the Cut Settings' own values)
MANUAL_MATERIAL = N_("Cut Settings")


def material_label(name: str | None) -> str | None:
    """A material (or material type) of the Basic set, or MANUAL_MATERIAL, in the window's
    language; a presets file's own names stay as written."""
    if name == MANUAL_MATERIAL:
        return _(name)
    return _label(MATERIAL, name)


def tool_label(name: str | None) -> str | None:
    """A tool's name ('Sketch Pen') in the window's language."""
    return _label(TOOL, name)


def holder_label(label: str | None) -> str | None:
    """A tool holder ('Tool 1', 'Tool') in the window's language."""
    if label and _HOLDER_NUMBER.fullmatch(label):
        return pgettext(HOLDER, "Tool {number}").format(number=label[5:])
    return _label(HOLDER, label)


def status_label(text: str | None) -> str | None:
    """A machine status ('Ready', 'Load Media') or a link state ('Not Connected') in the
    window's language."""
    if not text:
        return text
    shown = pgettext(STATUS, text)
    return shown if shown != text else translate_text(text)


def available_languages(localedir: Path | str | None = None) -> list[str]:
    """The languages with a compiled catalog, sorted by code."""
    folder = Path(localedir) if localedir is not None else LOCALE_DIR
    try:
        found = [p.name for p in folder.iterdir()
                 if (p / "LC_MESSAGES" / f"{DOMAIN}.mo").is_file()]
    except OSError:
        return []
    return sorted(code for code in found if LANGUAGE_CODE.fullmatch(code))


def language_name(code: str) -> str:
    """The language's own name ('Deutsch'), or its code when it has none here."""
    return LANGUAGE_NAMES.get(code) or LANGUAGE_NAMES.get(code.split("_")[0]) or code


def valid_language(code: object) -> bool:
    """True for a language code the Preferences may store ('' is the system's language)."""
    return code == "" or (isinstance(code, str) and LANGUAGE_CODE.fullmatch(code) is not None)


def setup(language: str | None = None,
          localedir: Path | str | None = None) -> gettext.NullTranslations:
    """Use the catalog of `language` ('ro', 'pt_BR'), or with None or '' the system's language
    (LANGUAGE, LC_ALL, LC_MESSAGES, LANG, as gettext reads them). A language without a catalog
    gives English. Returns the translations now in use."""
    global _translation, _templates
    languages = [language] if language else None
    _translation = gettext.translation(
        DOMAIN, str(localedir if localedir is not None else LOCALE_DIR), languages=languages,
        fallback=True)
    _templates = None
    return _translation


def use_english() -> None:
    """Back to the English texts (what every process has before setup()); no language choice
    recorded (started_choice)."""
    global _translation, _templates, started_choice
    _translation = gettext.NullTranslations()
    _templates = None
    started_choice = None


def current_language() -> str:
    """The language of the catalog in use, or 'en' for the English texts."""
    info = _translation.info() if isinstance(_translation, gettext.GNUTranslations) else {}
    return info.get("language") or SOURCE_LANGUAGE


def apply_language_choice(language: str | None) -> None:
    """Make a language chosen in the Preferences that of the whole window, GTK's and
    libadwaita's own texts included: LANGUAGE before GTK starts. '' or None leaves the
    system's language."""
    if language:
        os.environ["LANGUAGE"] = language


# --- the service's texts ------------------------------------------------------------------------
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_HOLDER_NUMBER = re.compile(r"Tool [0-9]+")
# the end of a sentence or a clause inside a placeholder value: not a plain value
_CLAUSE_END = re.compile(r"[.;:!?](?:\s|$)")
_WORD = re.compile(r"[^\W\d_]")    # a letter: a value without one ("?", "3") is no sentence
# the word context of a placeholder's value, tried before the plain catalog: in
# "{holder}: {tool}, ..." the holder "Tool" is the holder, not the plain word "Tool"
PLACEHOLDER_CONTEXTS = {"holder": HOLDER, "tool": TOOL, "action": ACTION, "material": MATERIAL,
                        "term": STATUS}


def _template_pattern(msgid: str) -> re.Pattern | None:
    """The regular expression that matches the English sentences made from `msgid` (a template
    with named placeholders), each placeholder a named group; None for a text without one."""
    parts, seen, pos = [], set(), 0
    for m in _PLACEHOLDER.finditer(msgid):
        parts.append(re.escape(msgid[pos:m.start()]))
        name = m.group(1)
        parts.append(f"(?P={name})" if name in seen else f"(?P<{name}>.+?)")
        seen.add(name)
        pos = m.end()
    if not seen:
        return None
    parts.append(re.escape(msgid[pos:]))
    return re.compile("".join(parts), re.DOTALL)


def _service_templates() -> list[tuple[int, re.Pattern, str]]:
    """The catalog's translated templates, the most specific (the most fixed text) first."""
    global _templates
    if _templates is None:
        catalog = getattr(_translation, "_catalog", {})
        found = []
        for msgid, msgstr in catalog.items():
            if not isinstance(msgid, str) or "\x04" in msgid or not msgid or not msgstr:
                continue
            pattern = _template_pattern(msgid)
            fixed = len(_PLACEHOLDER.sub("", msgid))
            # too little fixed text ("{title}. {problem}", "{items} and {last}") would match
            # sentences it never made
            if pattern is not None and (fixed >= MIN_TEMPLATE_TEXT or msgid[0] != "{"):
                found.append((fixed, pattern, msgid))
        found.sort(key=lambda t: -t[0])
        _templates = found
    return _templates


def _plain_value(value: str, words: int = PLAIN_VALUE_WORDS) -> bool:
    """True for a placeholder value that may stay as it is inside a translated sentence: a
    number, a name (at most `words` words); not a piece of a sentence."""
    return (len(value) <= PLAIN_VALUE_LENGTH and len(value.split()) <= words
            and _CLAUSE_END.search(value) is None)


def _context_word(name: str, value: str) -> str | None:
    """A placeholder value in its placeholder's word context (PLACEHOLDER_CONTEXTS) in the
    window's language, or None when it is not a word of that context."""
    context = PLACEHOLDER_CONTEXTS.get(name)
    if context is None or not value:
        return None
    shown = holder_label(value) if context == HOLDER else _translation.pgettext(context, value)
    return shown if shown != value else None


def _value_text(name: str, value: str, depth: int) -> str:
    """A placeholder value in the window's language: in its placeholder's word context first
    (PLACEHOLDER_CONTEXTS), else as translate_text() translates it."""
    shown = _context_word(name, value)
    return shown if shown is not None else translate_text(value, depth)


def translate_text(text: str | None, depth: int = SERVICE_DEPTH) -> str | None:
    """A finished English sentence (from the service, or made from a marked template) in the
    window's language: its translation when the sentence itself is in the catalog, else the
    translation of the most specific marked template it matches, with the placeholder values
    translated the same way (each in its word context first, PLACEHOLDER_CONTEXTS). A short
    template (less than STRONG_TEMPLATE_TEXT fixed characters) takes the sentence only when
    every value it would leave in English is a plain value, so that a template never puts a
    loose piece of a sentence into its translation; a value of a word-context placeholder
    (PLACEHOLDER_CONTEXTS) must be a word of its context or a plain value with any template.
    Anything else comes back as it was: a sentence no template made stays wholly English."""
    if not text or not isinstance(_translation, gettext.GNUTranslations):
        return text
    translated = _translation.gettext(text)
    if translated != text:
        return translated
    for context in WORD_CONTEXTS:
        word = _translation.pgettext(context, text)
        if word != text:
            return word
    if _HOLDER_NUMBER.fullmatch(text):
        return holder_label(text)
    if depth <= 0:
        return text
    for fixed, pattern, msgid in _service_templates():
        m = pattern.fullmatch(text)
        if m is None:
            continue
        found = m.groupdict()
        values = {k: _value_text(k, v, depth - 1) for k, v in found.items()}
        words = LOOSE_VALUE_WORDS if msgid.startswith("{") else PLAIN_VALUE_WORDS
        if fixed < STRONG_TEMPLATE_TEXT and any(
                values[k] == v and not _plain_value(v, words) for k, v in found.items()):
            continue                      # a short template around a piece of another sentence
        if any(k in PLACEHOLDER_CONTEXTS and _context_word(k, v) is None
               and _WORD.search(v) and not _plain_value(v, words) for k, v in found.items()):
            continue                      # a holder, tool, action, material or status that is
                                          # a piece of another sentence, whatever the template
        try:
            return _translation.gettext(msgid).format(**values)
        except (KeyError, IndexError, ValueError):
            return text
    return text


# The machine profiles' and the Basic set's own words, marked here for the catalog
# (opencutter.machines may import nothing of OpenCutter) and shown with the labels above.
# tests/test_i18n.py checks that every word of the registry is here.
MACHINE_WORDS = (
    # what the machine reports (ESC 05), as status_label() shows it
    NC_("machine status", "Ready"),
    NC_("machine status", "Busy"),
    NC_("machine status", "Load Media"),
    NC_("machine status", "Paused"),
    NC_("machine status", "Cancelling"),
    NC_("machine status", "Rejected"),
    # the tools the machine detects (ESC 15)
    NC_("tool", "No Tool Detected"),
    NC_("tool", "Ratchet Blade"),
    NC_("tool", "Deep-Cut Blade"),
    NC_("tool", "Kraft Blade, 2mm"),
    NC_("tool", "Kraft Blade, 3mm"),
    NC_("tool", "Rotary Blade"),
    NC_("tool", "Punch Tool"),
    NC_("tool", "Sketch Pen"),
    NC_("tool", "Foil Tool"),
    # the tool holders
    NC_("tool holder", "Tool {number}"),
    NC_("tool holder", "Tool"),
    # actions: of the tools, and of presets files (Score, No Cut)
    NC_("action", "Cut"),
    NC_("action", "Kraft Cut"),
    NC_("action", "Rotary Cut"),
    NC_("action", "Punch"),
    NC_("action", "Sketch"),
    NC_("action", "Foil"),
    NC_("action", "Score"),
    NC_("action", "No Cut"),
    # the Basic set's materials and material types
    NC_("material", "Vinyl"),
    NC_("material", "Paper"),
    NC_("material", "Sketch Pen"),
)
# product names that stay as they are in every language
PRODUCT_NAMES = frozenset({"AutoBlade"})
