#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""OpenCutter's translations, without host gettext tools (standard library only).

    python3 build-aux/translations.py extract       # po/opencutter.pot from the marked texts
    python3 build-aux/translations.py init ro       # start po/ro.po and add ro to po/LINGUAS
    python3 build-aux/translations.py update        # merge the template into every po/<lang>.po
    python3 build-aux/translations.py compile       # po/<lang>.po -> the package's .mo catalogs
    python3 build-aux/translations.py check         # all of the above is up to date (exit 1 if not)
    python3 build-aux/translations.py sdk-check     # msgfmt --check of every .po (host or GNOME SDK)

extract reads the files listed in po/POTFILES and takes every call of the markers of
src/opencutter/i18n.py whose texts are string literals: _("..."), N_("..."), ngettext(a, b, n),
pgettext(ctx, "..."), NC_(ctx, "..."), npgettext(ctx, a, b, n). A comment that starts with
"Translators:" on the lines right above the call goes into the template for the translators. The
template names each text's file but not its line, so that it changes only when a text changes.

compile writes src/opencutter/locale/<lang>/LC_MESSAGES/opencutter.mo for every language of
po/LINGUAS. The catalogs are package data and are kept in the repository: a source checkout, a
pip install and the Flatpak (which installs the package with pip) all use them as they are, and
tests/test_i18n.py checks that they match the .po files. A text marked fuzzy, or not translated,
stays English. compile refuses a translation whose {placeholders} or markup tags differ from the
English text: .format() would fail, or the label would show the tags.

sdk-check is an extra check by GNU msgfmt: the host's, else the GNOME SDK's through Flatpak
(flatpak run --command=msgfmt org.gnome.Sdk//51), which only reads the po folder.
"""
from __future__ import annotations

import argparse
import ast
import io
import re
import shutil
import string
import struct
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOMAIN = "opencutter"
PO_DIR = REPO / "po"
LOCALE_DIR = REPO / "src" / "opencutter" / "locale"
SDK = "org.gnome.Sdk//51"
WIDTH = 79
MO_MAGIC = 0x9504_12DE              # the first word of a GNU .mo file (little-endian)
# the first comment lines of every file of po/ (the header entry's comments)
SPDX = ["SPDX-License-Identifier: GPL-3.0-or-later",
        "SPDX-FileCopyrightText: 2026 OpenCutter contributors"]
HEADER_FIELDS = (
    ("Project-Id-Version", "OpenCutter"),
    ("Report-Msgid-Bugs-To", "https://github.com/MortyNiners/opencutter/issues"),
    ("MIME-Version", "1.0"),
    ("Content-Type", "text/plain; charset=UTF-8"),
    ("Content-Transfer-Encoding", "8bit"),
)
# Plural forms of the languages OpenCutter is translated into first (gettext's own table); a
# language missing here gets two forms, which its translator may correct in the header.
PLURAL_FORMS = {
    "de": "nplurals=2; plural=(n != 1);",
    "es": "nplurals=2; plural=(n != 1);",
    "fr": "nplurals=2; plural=(n > 1);",
    "it": "nplurals=2; plural=(n != 1);",
    "pt": "nplurals=2; plural=(n != 1);",
    "pt_BR": "nplurals=2; plural=(n > 1);",
    "ro": "nplurals=3; plural=(n==1 ? 0 : (n==0 || (n%100 > 0 && n%100 < 20)) ? 1 : 2);",
    "ru": "nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
          "(n%100<10 || n%100>=20) ? 1 : 2);",
}
# marker -> the positions of (context, singular, plural) among its arguments
KEYWORDS = {
    "_": (None, 0, None),
    "N_": (None, 0, None),
    "ngettext": (None, 0, 1),
    "pgettext": (0, 1, None),
    "NC_": (0, 1, None),
    "npgettext": (0, 1, 2),
}
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
TAG = re.compile(r"</?([a-z]+)(?:\s[^<>]*)?>")
BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)")
LANGUAGE = re.compile(r"[a-z]{2,3}(_[A-Z]{2})?(@[a-z]+)?")


@dataclass
class Message:
    """One entry of a .po or .pot file."""
    msgid: str
    context: str | None = None
    plural: str | None = None
    msgstr: list[str] = field(default_factory=lambda: [""])
    comments: list[str] = field(default_factory=list)      # the translator's own ("# ")
    extracted: list[str] = field(default_factory=list)     # for the translators ("#. ")
    references: list[str] = field(default_factory=list)    # "#: "
    flags: list[str] = field(default_factory=list)         # "#, "
    obsolete: bool = False

    @property
    def key(self) -> tuple[str | None, str]:
        return self.context, self.msgid

    @property
    def fuzzy(self) -> bool:
        return "fuzzy" in self.flags

    def translated(self) -> bool:
        return all(self.msgstr) and not self.fuzzy


# --- reading and writing .po files -------------------------------------------------------------
def _unquote(text: str, where: str) -> str:
    text = text.strip()
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        raise ValueError(f"{where}: not a quoted string: {text}")
    try:
        return ast.literal_eval(text)
    except (SyntaxError, ValueError) as e:
        raise ValueError(f"{where}: bad string {text}: {e}") from None


def parse_po(text: str, name: str = "<po>") -> list[Message]:
    """The entries of a .po file (the header is the entry with the empty msgid)."""
    messages: list[Message] = []
    pending = Message(msgid="", msgstr=[])
    current: Message | None = None
    target: tuple[str, int] | None = None

    def finish() -> None:
        nonlocal current, pending, target
        if current is not None:
            if not current.msgstr:
                raise ValueError(f"{name}: no msgstr for {current.msgid!r}")
            messages.append(current)
        current, pending, target = None, Message(msgid="", msgstr=[]), None

    def start(obsolete: bool) -> Message:
        nonlocal current
        if current is not None and current.msgstr:
            finish()
        if current is None:
            current = pending
            current.obsolete = obsolete
        return current

    for number, raw in enumerate(text.splitlines(), 1):
        where = f"{name}:{number}"
        line = raw.strip()
        obsolete = line.startswith("#~")
        if obsolete:
            line = line[2:].strip()
            if line.startswith("|"):          # "#~| msgid ...": the previous text (msgmerge
                continue                      # --previous), like "#|" for a live entry
        if not line:
            if not obsolete and current is not None and current.msgstr:
                finish()
            continue
        if line.startswith("#"):
            if current is not None and current.msgstr:
                finish()
            if line.startswith("#."):
                pending.extracted.append(line[2:].strip())
            elif line.startswith("#:"):
                pending.references += line[2:].split()
            elif line.startswith("#,"):
                pending.flags += [f.strip() for f in line[2:].split(",") if f.strip()]
            elif not line.startswith("#|"):
                pending.comments.append(line[1:].strip())
            continue
        keyword, _, rest = line.partition(" ")
        if keyword == "msgctxt":
            m = start(obsolete)
            m.context = _unquote(rest, where)
            target = ("context", 0)
        elif keyword == "msgid":
            m = current if current is not None and current.context is not None \
                and not current.msgstr and target == ("context", 0) else start(obsolete)
            m.msgid = _unquote(rest, where)
            target = ("msgid", 0)
        elif current is None:
            raise ValueError(f"{where}: {keyword} before msgid")
        elif keyword == "msgid_plural":
            current.plural = _unquote(rest, where)
            target = ("plural", 0)
        elif keyword == "msgstr":
            current.msgstr = [_unquote(rest, where)]
            target = ("msgstr", 0)
        elif keyword.startswith("msgstr[") and keyword.endswith("]"):
            index = int(keyword[7:-1])
            if index != len(current.msgstr):
                raise ValueError(f"{where}: msgstr[{index}] out of order")
            current.msgstr.append(_unquote(rest, where))
            target = ("msgstr", index)
        elif line.startswith('"'):
            if target is None:
                raise ValueError(f"{where}: a string outside an entry")
            more = _unquote(line, where)
            kind, index = target
            if kind == "context":
                current.context += more
            elif kind == "msgid":
                current.msgid += more
            elif kind == "plural":
                current.plural += more
            else:
                current.msgstr[index] += more
        else:
            raise ValueError(f"{where}: cannot read {raw!r}")
    finish()
    return messages


def _quote(text: str) -> str:
    out = (text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
           .replace("\r", "\\r").replace("\n", "\\n"))
    return f'"{out}"'


def _chunks(text: str, room: int) -> list[str]:
    """`text` cut after newlines and, past `room` characters, after spaces."""
    pieces: list[str] = []
    for part in re.split(r"(?<=\n)", text):
        if not part:
            continue
        while len(_quote(part)) > room:
            cut = part.rfind(" ", 0, room - 2)
            if cut <= 0:
                break
            pieces.append(part[:cut + 1])
            part = part[cut + 1:]
        pieces.append(part)
    return pieces


def _field(keyword: str, text: str, prefix: str = "") -> list[str]:
    first = f"{prefix}{keyword} {_quote(text)}"
    if len(first) <= WIDTH and "\n" not in text[:-1]:
        return [first]
    return [f'{prefix}{keyword} ""'] + [prefix + _quote(c)
                                        for c in _chunks(text, WIDTH - len(prefix))]


def format_message(m: Message) -> str:
    prefix = "#~ " if m.obsolete else ""
    lines = [f"# {c}".rstrip() for c in m.comments]
    if not m.obsolete:
        lines += [f"#. {c}" for c in m.extracted]
        lines += [f"#: {r}" for r in m.references]
    if m.flags:                           # an obsolete entry keeps its flags (fuzzy), as GNU
        lines.append("#, " + ", ".join(m.flags))   # msgmerge does
    if m.context is not None:
        lines += _field("msgctxt", m.context, prefix)
    lines += _field("msgid", m.msgid, prefix)
    if m.plural is not None:
        lines += _field("msgid_plural", m.plural, prefix)
        for i, s in enumerate(m.msgstr):
            lines += _field(f"msgstr[{i}]", s, prefix)
    else:
        lines += _field("msgstr", m.msgstr[0], prefix)
    return "\n".join(lines) + "\n"


def format_po(messages: list[Message]) -> str:
    """The text of a .po file; the header entry's comments come first."""
    return "\n".join(format_message(m) for m in messages)


def header_text(fields: list[tuple[str, str]]) -> str:
    return "".join(f"{k}: {v}\n" for k, v in fields)


def header_fields(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


# --- extracting the marked texts ---------------------------------------------------------------
def potfiles(po_dir: Path = PO_DIR) -> list[str]:
    """The source files listed in POTFILES (paths from the repository root)."""
    lines = (po_dir / "POTFILES").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _translator_comments(source: str) -> dict[int, list[str]]:
    """line -> the "Translators:" comment of the comment lines right above it."""
    comments: dict[int, str] = {}
    code_lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            comments[tok.start[0]] = tok.string.lstrip("#").strip()
        elif tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                              tokenize.ENDMARKER):
            for line in range(tok.start[0], tok.end[0] + 1):
                code_lines.add(line)
    found: dict[int, list[str]] = {}
    for line in sorted(code_lines):
        block, above = [], line - 1
        while above in comments and above not in code_lines:
            block.insert(0, comments[above])
            above -= 1
        start = next((i for i, c in enumerate(block) if c.startswith("Translators:")), None)
        if start is not None:
            found[line] = [" ".join(block[start:])]
    return found


def extract_file(path: Path, name: str) -> list[tuple[tuple[int, int], Message]]:
    """The marked texts of one source file, with their places."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, str(path))
    notes = _translator_comments(source)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in KEYWORDS):
            continue
        ctx_at, one_at, many_at = KEYWORDS[node.func.id]

        def literal(i):
            if i is None:
                return None
            if i >= len(node.args):
                return False
            arg = node.args[i]
            return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                else False

        context, msgid, plural = literal(ctx_at), literal(one_at), literal(many_at)
        if False in (context, msgid, plural) or not msgid:
            continue                  # a variable: a text marked elsewhere, translated here
        m = Message(msgid=msgid, context=context, plural=plural,
                    msgstr=["", ""] if plural is not None else [""], references=[name])
        lines = range(node.lineno, (node.end_lineno or node.lineno) + 1)
        for line in lines:
            m.extracted += notes.get(line, [])
        if PLACEHOLDER.search(msgid + (plural or "")):
            m.flags.append("python-brace-format")
        found.append(((node.lineno, node.col_offset), m))
    return sorted(found, key=lambda t: t[0])


def extract(repo: Path = REPO, po_dir: Path = PO_DIR) -> list[Message]:
    """Every marked text of the POTFILES, in their order, each once."""
    messages: dict[tuple, Message] = {}
    for name in potfiles(po_dir):
        for _place, m in extract_file(repo / name, name):
            key = (m.context, m.msgid)
            if key in messages:
                known = messages[key]
                if (known.plural is None) != (m.plural is None) or (
                        m.plural is not None and known.plural != m.plural):
                    raise ValueError(f"{name}: {m.msgid!r} is marked with two plural forms")
                if name not in known.references:
                    known.references.append(name)
                known.extracted += [c for c in m.extracted if c not in known.extracted]
                known.flags += [f for f in m.flags if f not in known.flags]
            else:
                messages[key] = m
    return list(messages.values())


def template_text(messages: list[Message]) -> str:
    header = Message(msgid="", msgstr=[header_text(
        [*HEADER_FIELDS, ("Plural-Forms", "nplurals=INTEGER; plural=EXPRESSION;")])],
        comments=[*SPDX, "", "The template of OpenCutter's translations, made by",
                  "build-aux/translations.py extract: do not edit it. po/GLOSSARY.md explains "
                  "the terms."])
    return format_po([header, *messages])


# --- languages and their .po files -------------------------------------------------------------
def linguas(po_dir: Path = PO_DIR) -> list[str]:
    """The languages of po/LINGUAS."""
    path = po_dir / "LINGUAS"
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        words += line.split("#", 1)[0].split()
    return words


def new_po(lang: str, template: list[Message]) -> str:
    fields = [*HEADER_FIELDS, ("Language", lang),
              ("Plural-Forms", PLURAL_FORMS.get(lang, PLURAL_FORMS.get(
                  lang.split("_")[0], "nplurals=2; plural=(n != 1);")))]
    header = Message(msgid="", msgstr=[header_text(fields)],
                     comments=[*SPDX, "", f"OpenCutter's translation into {lang}. po/GLOSSARY.md "
                               "explains the terms."])
    nplurals = nplurals_of(fields[-1][1])
    body = []
    for m in template:
        body.append(Message(msgid=m.msgid, context=m.context, plural=m.plural,
                            msgstr=[""] * (nplurals if m.plural is not None else 1),
                            extracted=list(m.extracted), references=list(m.references),
                            flags=list(m.flags)))
    return format_po([header, *body])


def nplurals_of(plural_forms: str) -> int:
    m = re.search(r"nplurals\s*=\s*(\d+)", plural_forms or "")
    return int(m.group(1)) if m else 2


def merge(po_text: str, template: list[Message], name: str = "<po>") -> str:
    """`po_text` brought up to date with the template: the translations of texts still there
    are kept (with the translator's comments and flags), new texts come untranslated, and the
    translations of texts that are gone are kept at the end as obsolete entries."""
    old = parse_po(po_text, name)
    header = next((m for m in old if m.msgid == "" and m.context is None), None)
    if header is None:
        raise ValueError(f"{name}: no header")
    nplurals = nplurals_of(header_fields(header.msgstr[0]).get("Plural-Forms", ""))
    known = {m.key: m for m in old if not (m.msgid == "" and m.context is None)}
    body = []
    for t in template:
        prev = known.pop(t.key, None)
        size = nplurals if t.plural is not None else 1
        msgstr = [""] * size
        comments, flags = [], [f for f in t.flags]
        if prev is not None and (prev.plural is None) == (t.plural is None):
            msgstr = (list(prev.msgstr) + [""] * size)[:size]
            comments = list(prev.comments)
            if prev.fuzzy or (prev.plural != t.plural and t.plural is not None):
                flags.insert(0, "fuzzy")
        body.append(Message(msgid=t.msgid, context=t.context, plural=t.plural, msgstr=msgstr,
                            comments=comments, extracted=list(t.extracted),
                            references=list(t.references), flags=flags))
    # a text that is gone keeps its flags: a fuzzy translation stays fuzzy if the text comes back
    gone = [Message(msgid=m.msgid, context=m.context, plural=m.plural, msgstr=list(m.msgstr),
                    comments=list(m.comments), flags=list(m.flags), obsolete=True)
            for m in known.values() if any(m.msgstr)]
    header.obsolete = False
    return format_po([header, *body, *gone])


# --- checks of a translation -------------------------------------------------------------------
def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


def problems_of(messages: list[Message], name: str) -> list[str]:
    """What would break the window: a translation with other {placeholders} or markup tags
    than the English text, a brace .format() cannot read, a header without UTF-8 or plural
    forms."""
    found = []
    header = next((m for m in messages if m.msgid == "" and m.context is None), None)
    fields = header_fields(header.msgstr[0]) if header is not None else {}
    if "charset=UTF-8" not in fields.get("Content-Type", ""):
        found.append(f"{name}: the header must say charset=UTF-8")
    if "Plural-Forms" not in fields or "INTEGER" in fields["Plural-Forms"]:
        found.append(f"{name}: the header needs the language's Plural-Forms")
    nplurals = nplurals_of(fields.get("Plural-Forms", ""))
    for m in messages:
        if m.obsolete or (m.msgid == "" and m.context is None) or not any(m.msgstr):
            continue
        what = f"{name}: {m.msgid[:50]!r}"
        if m.plural is not None and len(m.msgstr) != nplurals:
            found.append(f"{what}: {len(m.msgstr)} plural forms, the header says {nplurals}")
        english = placeholders(m.msgid) | placeholders(m.plural or "")
        for s in m.msgstr:
            if not s:
                continue
            ours = placeholders(s)
            if m.plural is None and ours != english:
                found.append(f"{what}: placeholders {sorted(ours)}, the English text has "
                             f"{sorted(english)}")
            elif m.plural is not None and not ours <= english:
                found.append(f"{what}: unknown placeholders {sorted(ours - english)}")
            if english or "python-brace-format" in m.flags:
                try:
                    list(string.Formatter().parse(s))
                except ValueError as e:
                    found.append(f"{what}: {e}")
            if sorted(TAG.findall(s)) != sorted(TAG.findall(m.msgid)):
                found.append(f"{what}: markup tags {sorted(TAG.findall(s))}, the English text "
                             f"has {sorted(TAG.findall(m.msgid))}")
            elif TAG.search(m.msgid) and BARE_AMP.search(s):
                found.append(f"{what}: a bare & in markup (write &amp;)")
    return found


# --- compiling ---------------------------------------------------------------------------------
def compile_mo(messages: list[Message]) -> bytes:
    """The GNU .mo catalog of the translated entries (the header always), sorted, without a
    hash table: what Python's gettext reads."""
    entries: dict[bytes, bytes] = {}
    for m in messages:
        if m.obsolete:
            continue
        is_header = m.msgid == "" and m.context is None
        if not is_header and not m.translated():
            continue
        key = m.msgid if m.plural is None else f"{m.msgid}\x00{m.plural}"
        if m.context is not None:
            key = f"{m.context}\x04{key}"
        entries[key.encode("utf-8")] = "\x00".join(m.msgstr).encode("utf-8")
    keys = sorted(entries)
    count = len(keys)
    origs_at = 28
    trans_at = origs_at + 8 * count
    data_at = trans_at + 8 * count
    orig_table, trans_table, data = [], [], bytearray()
    for k in keys:
        orig_table.append((len(k), data_at + len(data)))
        data += k + b"\x00"
    for k in keys:
        v = entries[k]
        trans_table.append((len(v), data_at + len(data)))
        data += v + b"\x00"
    out = struct.pack("<7I", MO_MAGIC, 0, count, origs_at, trans_at, 0, data_at)
    out += b"".join(struct.pack("<2I", *e) for e in orig_table)
    out += b"".join(struct.pack("<2I", *e) for e in trans_table)
    return out + bytes(data)


def mo_path(lang: str, locale_dir: Path = LOCALE_DIR) -> Path:
    return locale_dir / lang / "LC_MESSAGES" / f"{DOMAIN}.mo"


def read_po(lang: str, po_dir: Path = PO_DIR) -> list[Message]:
    path = po_dir / f"{lang}.po"
    return parse_po(path.read_text(encoding="utf-8"), str(path.relative_to(path.parents[1])))


def compile_all(po_dir: Path = PO_DIR, locale_dir: Path = LOCALE_DIR) -> list[str]:
    """Write the catalog of every language of LINGUAS; remove those of other languages.
    Returns the problems (nothing is written then)."""
    langs = linguas(po_dir)
    catalogs, problems = {}, []
    for lang in langs:
        if not LANGUAGE.fullmatch(lang) or not (po_dir / f"{lang}.po").is_file():
            problems.append(f"po/LINGUAS: {lang} has no po/{lang}.po")
            continue
        messages = read_po(lang, po_dir)
        problems += problems_of(messages, f"po/{lang}.po")
        catalogs[lang] = compile_mo(messages)
    if problems:
        return problems
    for lang, data in catalogs.items():
        path = mo_path(lang, locale_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_bytes() != data:
            path.write_bytes(data)
    if locale_dir.is_dir():
        for old in locale_dir.iterdir():
            if old.is_dir() and old.name not in catalogs:
                shutil.rmtree(old)
    return []


def check_all(repo: Path = REPO, po_dir: Path = PO_DIR, locale_dir: Path = LOCALE_DIR) -> list[str]:
    """Everything that is not up to date: the template, the .po files' checks, the catalogs."""
    problems = []
    template = po_dir / f"{DOMAIN}.pot"
    if not template.is_file() or template.read_text(encoding="utf-8") != template_text(
            extract(repo, po_dir)):
        problems.append("po/opencutter.pot is not up to date: run build-aux/translations.py "
                        "extract")
    langs = linguas(po_dir)
    for po in sorted(po_dir.glob("*.po")):
        if po.stem not in langs:
            problems.append(f"po/{po.name} is not in po/LINGUAS")
    for lang in langs:
        if not (po_dir / f"{lang}.po").is_file():
            problems.append(f"po/LINGUAS: {lang} has no po/{lang}.po")
            continue
        messages = read_po(lang, po_dir)
        problems += problems_of(messages, f"po/{lang}.po")
        path = mo_path(lang, locale_dir)
        if not path.is_file() or path.read_bytes() != compile_mo(messages):
            problems.append(f"{path.relative_to(repo)} does not match po/{lang}.po: run "
                            "build-aux/translations.py compile")
    if locale_dir.is_dir():
        for folder in sorted(locale_dir.iterdir()):
            if folder.name not in langs:
                problems.append(f"{folder.relative_to(repo)}: a catalog of a language not in "
                                "po/LINGUAS")
    return problems


def sdk_check(po_dir: Path = PO_DIR) -> int:
    """GNU msgfmt --check of every .po file of LINGUAS: the host's msgfmt, else the GNOME
    SDK's (Flatpak), which only gets to read the po folder."""
    files = [f"{lang}.po" for lang in linguas(po_dir)]
    if not files:
        print("no languages in po/LINGUAS")
        return 0
    if shutil.which("msgfmt"):
        command = ["msgfmt"]
    elif shutil.which("flatpak"):
        command = ["flatpak", "run", f"--filesystem={po_dir}:ro", "--command=msgfmt", SDK]
    else:
        print("neither msgfmt nor flatpak is installed", file=sys.stderr)
        return 1
    status = 0
    for name in files:
        r = subprocess.run([*command, "--check", "--statistics", "-o", "/dev/null", name],
                           cwd=po_dir, capture_output=True, text=True)
        print(f"{name}: {(r.stderr or r.stdout).strip()}")
        status |= r.returncode
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("extract")
    init = sub.add_parser("init")
    init.add_argument("language")
    sub.add_parser("update")
    sub.add_parser("compile")
    sub.add_parser("check")
    sub.add_parser("sdk-check")
    args = parser.parse_args(argv)
    template = PO_DIR / f"{DOMAIN}.pot"
    if args.command == "extract":
        template.write_text(template_text(extract()), encoding="utf-8")
        return 0
    if args.command == "init":
        lang = args.language
        if not LANGUAGE.fullmatch(lang):
            print(f"not a language code: {lang}", file=sys.stderr)
            return 1
        path = PO_DIR / f"{lang}.po"
        if path.exists():
            print(f"po/{lang}.po exists already: use update", file=sys.stderr)
            return 1
        path.write_text(new_po(lang, parse_po(template.read_text(encoding="utf-8"))[1:]),
                        encoding="utf-8")
        if lang not in linguas():
            with open(PO_DIR / "LINGUAS", "a", encoding="utf-8") as f:
                f.write(f"{lang}\n")
        return 0
    if args.command == "update":
        messages = parse_po(template.read_text(encoding="utf-8"), "po/opencutter.pot")[1:]
        for lang in linguas():
            path = PO_DIR / f"{lang}.po"
            path.write_text(merge(path.read_text(encoding="utf-8"), messages, f"po/{lang}.po"),
                            encoding="utf-8")
        return 0
    if args.command == "compile":
        problems = compile_all()
    elif args.command == "check":
        problems = check_all()
    else:
        return sdk_check()
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
