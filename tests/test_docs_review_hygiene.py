# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Review regressions for the public docs (hygiene-1, -3, -5, -6).

The docs are for public contributors: they must not point into a private workspace, they must
keep docs/PROTOCOL.md in step with the code (UUIDs, write size, stop sequence, never-send list,
emitted commands, the worked example, every machine profile of the registry), and the README
and the metadata must state the test status of each model and the Basic set honestly and
document the presets file the app reads. Every list of the bases a profile value may have names
all four, and every basis in the registry is one of them. The README's Install section names the
newest stable release and its known limitations.
"""
import xml.etree.ElementTree as ET
import json
import re
import tomllib
from pathlib import Path

import pytest

import stubs
import studio_names

stubs.install()
from opencutter import APP_ID  # noqa: E402
from opencutter.device import protocol  # noqa: E402
from opencutter.job import builder, validate  # noqa: E402
from opencutter.job.plan import build_plan  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, ToolSettings  # noqa: E402
from opencutter.machines import Support, registry  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
ARCHITECTURE = REPO / "ARCHITECTURE.md"
CONTRIBUTING = REPO / "CONTRIBUTING.md"
PROTOCOL = REPO / "docs" / "PROTOCOL.md"
METAINFO = REPO / "data" / f"{APP_ID}.metainfo.xml"
PACKAGE_INIT = REPO / "src" / "opencutter" / "__init__.py"
DOCS = [README, ARCHITECTURE, CONTRIBUTING, PROTOCOL]
PROTOTYPE = "cameo4_" + "ble"        # the private prototype's script name, not written out here


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def section(doc: str, number: str) -> str:
    """The body of '## <number>. ...' up to the next '## ' heading."""
    m = re.search(rf"^## {re.escape(number)}\. .*?$(.*?)(?=^## |\Z)", doc, re.M | re.S)
    assert m, f"section {number} missing"
    return m[1]


def flat(s: str) -> str:
    """Collapse line breaks and runs of spaces, so phrases can be found across wrapped lines."""
    return re.sub(r"\s+", " ", s)


# --- hygiene-3: no private workspace, owner tags or brief wording ------------------------------
@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_docs_do_not_point_outside_the_repository(path):
    doc = text(path)
    assert "../" not in doc
    assert "$DESIGN" not in doc and "your brief" not in doc
    assert "Ground truth" not in doc
    assert not re.search(r"\[\w+ owner\]|\(owner: ?\w+\)", doc)


def test_architecture_mentions_the_legacy_socket_only_as_the_legacy_prototype_daemon():
    doc = flat(text(ARCHITECTURE))
    assert PROTOTYPE not in doc
    for m in re.finditer(r"cameo4\.(sock|lock)", doc):
        around = doc[max(0, m.start() - 120): m.end() + 120]
        assert "legacy prototype daemon" in around, around


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_relative_links_resolve_inside_the_repository(path):
    for target in re.findall(r"\]\(([^)\s]+)\)", text(path)):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        resolved = (path.parent / target.split("#")[0]).resolve()
        assert resolved.is_relative_to(REPO), target
        assert resolved.exists(), target


def test_architecture_points_contributors_at_the_public_docs():
    doc = text(ARCHITECTURE)
    for link in ("docs/PROTOCOL.md", "CONTRIBUTING.md"):
        assert f"]({link})" in doc


def test_section_references_in_the_code_exist_in_docs_protocol():
    headings = set(re.findall(r"^#{2,3} (\d+(?:\.\d+)?)\.? ", text(PROTOCOL), re.M))
    refs = []
    for src in (REPO / "src").rglob("*.py"):
        refs += [(src.name, n) for n in
                 re.findall(r"PROTOCOL\.md,? (?:section )?(\d+(?:\.\d+)?)", src.read_text())]
    assert refs, "the code no longer cites docs/PROTOCOL.md sections; drop this test"
    for name, number in refs:
        assert number in headings, f"{name} cites PROTOCOL.md {number}, which does not exist"


# --- hygiene-3: docs/PROTOCOL.md is clean-room and matches the code ----------------------------
# Studio's internal file and symbol names and the tags of evidence notes, kept as digests
# (studio_names.digest) so that this file does not publish them itself, and three plain markers.
STUDIO_INTERNAL_DIGESTS = frozenset({
    "1cbbd7a0f4e164fa6e01", "4fc1ed8182f8b8884225", "be86990252612605263f",
    "a9ddf02d9a749a49db0c", "dbb3e077b0ac73c34fd7", "f9e1a5b2a7f4de9fd4a5",
    "c20081d7c3b9d3014bed", "037cede3349a4d491055",
})
INTERNAL_MARKERS = ("disassembly listing", "M4 ", "M0 ")


def studio_internals(doc: str, digests: frozenset[str] = STUDIO_INTERNAL_DIGESTS) -> list[str]:
    """The words of `doc` (also hyphenated ones) whose digest is one of `digests`, and the plain
    markers it contains."""
    words = set(re.findall(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*", doc))
    words |= set(re.findall(r"[A-Za-z0-9_]+", doc))
    found = {w for w in words if studio_names.digest(w) in digests}
    return sorted(found) + [m for m in INTERNAL_MARKERS if m in doc]


def test_the_studio_internals_check_really_matches():
    made_up = frozenset({studio_names.digest("made_up_name"), studio_names.digest("X-TAG")})
    assert studio_internals("see made_up_name.part and [X-TAG]", made_up) == \
        ["X-TAG", "made_up_name"]
    assert studio_internals("a disassembly listing", made_up) == ["disassembly listing"]
    assert studio_internals("nothing here, made_up_names", made_up) == []
    assert len(STUDIO_INTERNAL_DIGESTS) == 8


def test_protocol_has_no_firmware_addresses_or_studio_internals():
    doc = text(PROTOCOL)
    assert not re.search(r"0x[0-9a-fA-F]{6,}", doc)          # firmware or Studio addresses
    assert not studio_internals(doc)


@pytest.mark.parametrize("path", sorted((REPO / "src").rglob("*.py")),
                         ids=lambda p: str(p.relative_to(REPO)))
def test_code_comments_name_no_studio_internals(path):
    code = path.read_text(encoding="utf-8")
    assert not studio_internals(code), path.name


def test_protocol_lists_the_gatt_uuids_of_the_code():
    doc = section(text(PROTOCOL), "1")
    for uuid in (protocol.SERVICE, protocol.CHAR_DATA, protocol.CHAR_REPLY, protocol.CHAR_STATUS):
        assert f"`{uuid}`" in doc


def test_protocol_framing_matches_the_write_limits():
    doc = flat(section(text(PROTOCOL), "2"))
    assert builder.MAX_WRITE == protocol.MAX_DATA_WRITE == 182
    assert "**182 bytes**" in doc and "MTU − 3" in doc
    assert "alone" in doc and "exactly two bytes" in doc
    assert "Write Without Response" in doc
    assert protocol.MAX_QUERY_WRITE == 20 and "longer than 20 bytes" in doc


def test_protocol_describes_every_status_and_tool_code():
    doc = section(text(PROTOCOL), "3")
    for digit, name in protocol.STATUS_TEXT.items():
        assert f"| `{digit}` | {name} |" in doc
    for holder, tools in protocol.TOOL_NAMES.items():
        for code, name in tools.items():
            assert name in doc, (holder, code)
    assert "6100,6100" in doc


def test_protocol_never_send_list_names_every_refused_command():
    doc = section(text(PROTOCOL), "6")
    for cmd in validate._NEVER_SEND:
        assert f"`{cmd.decode()}`" in doc, cmd
    for esc in ("ESC 11", "ESC 12", "ESC 13", "ESC 00", "ESC 07"):
        assert esc in doc
    rows = [r for r in doc.splitlines() if r.startswith("| ") and not r.startswith("| Never")
            and not r.startswith("|---")]
    assert len(rows) >= 20 and all(r.count("|") == 3 and r.split("|")[2].strip() for r in rows)


def test_protocol_stop_sequence_matches_the_code():
    assert protocol.STOP_SEQUENCE == (b"\x1b\x09", b"\x1b\x09")
    doc = flat(section(text(PROTOCOL), "8"))
    assert "ESC 09, then ESC 09 again" in doc and "power button" in doc
    assert "ESC 07 (pause) is not used" in doc


def oracal_651() -> ToolSettings:
    return ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                        line_segment_overcut=False, overcut=(1, 1), blade_depth=1)


def example_plan():
    square = [(139.7, 50.8), (139.7, 76.2), (165.1, 76.2), (165.1, 50.8), (139.7, 50.8)]
    return build_plan(JobSpec(mat="12x12", media=(0.0, 0.0, 304.8, 127.0),
                              groups=[CutGroup(oracal_651(), [square])], preview_outline=False))


def test_protocol_worked_example_is_what_the_builder_emits():
    block = re.search(r"^### 7\.3 .*?```\n(.*?)```", text(PROTOCOL), re.M | re.S)
    assert block
    lines = [line.split()[0].replace(".", "\x03").encode() for line in block[1].splitlines()
             if line.strip()]
    steps = example_plan().steps
    assert [s.kind for s in steps] == ["write", "units", "write", "write", "write"]
    assert lines == [b"".join(s.writes) for s in steps]
    assert all(len(s.writes) == 1 for s in steps)       # each line of the example is one write


def test_protocol_command_table_covers_every_emitted_command():
    doc = section(text(PROTOCOL), "5")
    pen = ToolSettings(holder=1, tool_code=7, speed=5, force=7, acceleration=3, offset=0,
                       line_segment_overcut=False, overcut=(0, 0), blade_depth=None)
    kraft = ToolSettings(holder=2, tool_code=4, speed=6, force=8, acceleration=1, offset=68,
                         line_segment_overcut=True, overcut=(1, 1), blade_depth=None)
    cmds = builder.header("12x12") + builder.trailer() + builder.path([[(600, 600), (700, 700)]])
    for s in (oracal_651(), pen, kraft):
        cmds += builder.condition_block(s)
    mnemonics = {re.match(rb"[A-Z]+|\\|!", c)[0].decode() for c in cmds}
    assert mnemonics == {"TG", "FN", "TB", "\\", "Z", "M", "D", "J", "!", "FX", "TJ", "FC", "FE",
                         "FF", "TF"}
    for mn in mnemonics:
        assert f"| `{mn}" in doc, mn


# --- hygiene-5 and hygiene-6: honest README status and Basic set --------------------------------
def test_readme_status_says_what_ran_on_a_real_machine_and_what_did_not():
    """Since the first real cuts (2026-11-17) the status names the one machine OpenCutter cut
    on, and still lists what was not tried on a real machine."""
    status = flat(re.search(r"> \*\*Status:.*?(?=\n\n)", text(README), re.S)[0].replace("> ", ""))
    assert "mostly run against" not in status
    assert "only run against a simulated machine" not in status
    assert "tested on one machine" in status and "one real CAMEO 4" in status
    assert "Oracal 651" in status
    assert "Not yet tried on a real machine" in status and "Cancel Job" in status
    assert "has not been measured" in status
    assert "outline preview" in status and "scrap material" in status


def test_readme_basic_set_wording_matches_the_presets():
    from opencutter.presets.model import BASIC_PRESETS
    doc = flat(text(README))
    assert "tested on our machine" not in doc
    assert ("one vinyl cut setting verified on our CAMEO 4 (the same values as Studio's "
            "\"Vinyl, Silhouette Oracal 651\" preset) and one untested Sketch Pen starting "
            "point") in doc
    assert [p.verified for p in BASIC_PRESETS] == [True, False]
    assert [p.tool for p in BASIC_PRESETS] == ["AutoBlade", "Sketch Pen"]


@pytest.mark.parametrize("top", ["src", "tests"])
def test_code_does_not_name_the_private_prototype(top):
    named = [str(p.relative_to(REPO)) for p in (REPO / top).rglob("*.py")
             if PROTOTYPE in p.read_text(encoding="utf-8")]
    assert not named
    daemon = (REPO / "src" / "opencutter" / "device" / "daemon.py").read_text(encoding="utf-8")
    assert "legacy prototype daemon" in daemon


def test_readme_does_not_mention_the_private_prototype():
    doc = flat(text(README))
    assert PROTOTYPE not in doc and "cameo4.sock" not in doc and "cameo4.lock" not in doc
    first_run = flat(re.search(r"^## First run$(.*?)(?=^## )", text(README), re.M | re.S)[1])
    assert "prototype" not in first_run and "Import" not in first_run


def test_architecture_keeps_the_legacy_daemon_note_next_to_its_lock():
    doc = flat(text(ARCHITECTURE))
    m = re.search(r"the flock on `\$XDG_RUNTIME_DIR/cameo4\.lock`, the lock of the legacy "
                  r"prototype daemon, .{0,200}?legacy prototype daemon stops it before starting "
                  r"OpenCutter, then switches the machine off and on", doc)
    assert m, "the legacy-daemon note must stay next to the legacy-lock check"


# --- hygiene-1: presets without Silhouette files; CONTRIBUTING rules ---------------------------
def readme_presets() -> str:
    return re.search(r"^## Material presets$(.*?)(?=^## )", text(README), re.M | re.S)[1]


@pytest.mark.parametrize("path", [README, ARCHITECTURE], ids=lambda p: p.name)
def test_docs_describe_no_way_to_read_studio_files(path):
    rule = "reads, decrypts or redistributes"            # the rule that forbids it may say so
    doc = flat(text(path)).casefold().replace(rule, "")
    for word in ("decrypt", "installer", "eula", "py7zr", "pseudo", "key table", "import presets"):
        assert word not in doc, (path.name, word)


def test_readme_material_presets_basic_set_cut_settings_and_the_file():
    doc = flat(readme_presets())
    assert "Cut Settings" in doc and "Blade Depth" in doc and "Test Cut" in doc
    assert "`$XDG_DATA_HOME/opencutter/presets.json`" in doc
    assert "`~/.local/share/opencutter/presets.json`" in doc
    assert "uses the Basic set" in doc


def test_readme_example_presets_file_is_the_basic_vinyl_preset(tmp_path):
    from opencutter.presets.model import BASIC_PRESETS, PresetStore, presets_path
    block = re.search(r"```json\n(.*?)```", readme_presets(), re.S)[1]
    data = json.loads(block)
    assert data["format"] == "opencutter-presets" and data["schema"] == 1
    assert data["presets"] == [BASIC_PRESETS[0].to_dict()]
    path = tmp_path / "presets.json"
    path.write_text(block)
    store = PresetStore.load(path)
    assert store.presets == [BASIC_PRESETS[0]] and store.source["kind"] == "file"
    assert store.description == data["description"]
    assert presets_path().name == "presets.json"


def test_readme_documents_every_preset_field_with_its_range():
    from dataclasses import fields

    from opencutter.job.validate import RANGES
    from opencutter.presets.model import Preset
    doc = readme_presets()
    rows = {m[1]: m[2] for m in re.finditer(r"^\| `(\w+)` \| (.*) \|$", doc, re.M)}
    assert {f.name for f in fields(Preset)} <= rows.keys()
    for name, (lo, hi) in RANGES.items():
        assert f"`{lo}`..`{hi}`" in rows[name], name
    assert "`[0..9, 0..9]`" in rows["overcut"] and "`null`" in rows["blade_depth"]
    for key in ("format", "description", "presets"):
        assert key in doc


def test_contributing_forbids_silhouette_data_and_copied_presets():
    doc = flat(text(CONTRIBUTING))
    assert "**your own test cuts on your own machine**" in doc
    assert "Never copy a row from Silhouette Studio" in doc
    for item in ("No code that reads Silhouette files", "No Silhouette data", "No firmware",
                 "No captures", "No personal data"):
        assert f"**{item}.**" in doc
    assert ("OpenCutter must not contain code or data that reads, decrypts or redistributes "
            "Silhouette Studio files, firmware or data") in doc
    assert "Never touch a real machine from a test" in doc
    assert "OPENCUTTER_STUDIO" not in doc
    # Studio's rows, column names and notation stay out; each profile value names its basis
    assert "no rows, column names or whole tables copied from them" in doc
    assert "no command syntax written in Studio's own placeholder notation" in doc
    assert ('each value with a short comment that gives its basis ("Studio 5.0.556 machine data", '
            '"firmware analysis", "measured on V5.05" or "public captures and reports")') in doc
    # the fourth basis: what others published, in our own words, never the capture itself
    assert "public captures and reports, described in our own words" in doc
    assert ("keep the capture itself, and any log or dump made from it, out of the repository"
            in doc)
    assert "**No captures.** No Bluetooth or USB captures (`.btsnoop`, `.pcap`, ...), your own " \
           "or published ones" in doc


# --- the bases of a profile value -----------------------------------------------------------------
# The four bases a value of a machine profile may have, as its comment names them. "measured on"
# is followed by what was measured ("measured on V5.05", "measured on a CAMEO 4 (V5.05)").
BASIS_LABELS = ("Studio 5.0.556 machine data", "firmware analysis", "measured on V5.05",
                "public captures and reports")
MACHINES = REPO / "src" / "opencutter" / "machines"


def basis_lists() -> dict[str, str]:
    """Each place that lists the bases, flattened: CONTRIBUTING.md, the first paragraph of
    PROTOCOL.md section 11, ARCHITECTURE.md, and the docstrings of registry.py and profile.py."""
    import ast
    docstrings = {name: ast.get_docstring(ast.parse(text(MACHINES / name)))
                  for name in ("registry.py", "profile.py")}
    out = {"CONTRIBUTING.md": text(CONTRIBUTING),
           "PROTOCOL.md section 11": section(text(PROTOCOL), "11").strip().split("\n\n")[0],
           "ARCHITECTURE.md": text(ARCHITECTURE), **docstrings}
    return {where: flat(doc) for where, doc in out.items()}


@pytest.mark.parametrize("where", ["CONTRIBUTING.md", "PROTOCOL.md section 11", "ARCHITECTURE.md",
                                   "registry.py", "profile.py"])
def test_every_list_of_bases_names_all_four(where):
    """Wherever the bases are listed, all four are, in quotes; the fourth with what it means:
    what others have published, described in our own words."""
    doc = basis_lists()[where]
    lists = [doc[max(0, m.start() - 150): m.end() + 150]
             for m in re.finditer(r'"firmware analysis"', doc)]
    assert lists, where
    for around in lists:
        for label in BASIS_LABELS:
            assert f'"{label}' in around, (where, label, around)
    assert "described in our own words" in doc, where


def registry_bases():
    """(where, basis) for every basis in the registry: each entry, firmware line and mat."""
    from opencutter.machines.profile import MachineProfile
    for entry in registry.entries():
        yield entry.id, entry.basis
        if isinstance(entry, MachineProfile):
            for line in entry.identity.lines:
                yield f"{entry.id} V{line.major}", line.basis
            for mat in entry.mats:
                yield f"{entry.id} {mat.key}", mat.basis


def test_every_basis_in_the_registry_is_one_of_the_four():
    labels = ("Studio 5.0.556 machine data", "firmware analysis", "measured on",
              "public captures and reports")
    found = list(registry_bases())
    assert len(found) > 40
    for where, basis in found:
        assert any(label in basis for label in labels), (where, basis)
    # the public sources are named, and named alike
    assert sum("public captures and reports" in basis for _, basis in found) >= 5


# Every mention of a public source in the profiles (comments, docstrings and bases) and in
# docs/PROTOCOL.md uses the fourth basis's own words.
PUBLIC_SOURCE = re.compile(r"\bpublic\b(?! captures and reports\b)", re.IGNORECASE)


def without_comment_marks(code: str) -> str:
    """Python source as one line of words: comment marks and string quotes between wrapped lines
    removed, so that a phrase wrapped over two comment or string lines reads as one."""
    return flat(re.sub(r'"\s*\n\s*"|\n\s*#', " ", code))


@pytest.mark.parametrize("path", sorted(MACHINES.glob("*.py")) + [PROTOCOL],
                         ids=lambda p: p.name)
def test_public_sources_are_named_as_public_captures_and_reports(path):
    doc = without_comment_marks(text(path)) if path.suffix == ".py" else flat(text(path))
    assert not [doc[max(0, m.start() - 60): m.end() + 60] for m in PUBLIC_SOURCE.finditer(doc)]


def test_the_public_source_check_really_matches():
    code = ('# commands close to the CAMEO 4 (public captures and\n'
            '#     reports: USB captures of V1.60)\n'
            'basis="Studio 5.0.556 machine data; public captures and "\n'
            '      "reports: one FG sample"\n')
    assert not PUBLIC_SOURCE.search(without_comment_marks(code))
    for bad in ("one public capture of Studio", "public reports of V1.20", "A public report",
                "(public sightings)", "the public drivers", "public captures of V1.60"):
        assert PUBLIC_SOURCE.search(bad), bad


# --- machine profiles: every model of the registry, its level and its values -------------------
UNTESTED = [p for p in registry.profiles() if p.support is Support.UNTESTED]
ALL_NAMES = [e.name for e in registry.entries()]
BASES = {"known": "**known**", "inferred": "**inferred**", "unknown": "**unknown**"}


def mentions(doc: str, name: str) -> bool:
    """True when `doc` names the model `name` itself, not only a longer model name that contains
    it ("CAMEO 5" in "CAMEO 5 Plus", "CC300" in "CC300L")."""
    for longer in sorted(ALL_NAMES, key=len, reverse=True):
        if longer != name and name in longer:
            doc = doc.replace(longer, " ")
    return re.search(re.escape(name) + r"(?![0-9A-Za-z])", doc) is not None


def range_cells(p) -> list[str]:
    """The cells of the model's row in PROTOCOL.md's range table, from speed to overcut."""
    r = p.ranges
    accel = "none" if r.acceleration is None else f"{r.acceleration.lo}..{r.acceleration.hi}"
    return [f"{r.speed.lo}..{r.speed.hi}", f"{r.force.lo}..{r.force.hi}", accel] + \
        [f"{x.lo}..{x.hi}" for x in (r.offset, r.blade_depth, r.overcut)]


def model_rows(table: str) -> dict[str, list[list[str]]]:
    """Model name -> the other cells of its rows, for the tables whose first cell names models
    ("CAMEO 5, CAMEO 5 Plus, CAMEO Pro MK-II"; a bracket after a name is left out)."""
    out: dict[str, list[list[str]]] = {}
    for row in table.splitlines():
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if row.startswith("| ") and len(cells) > 1:
            for name in re.sub(r" \(.*?\)", "", cells[0]).split(", "):
                out.setdefault(name, []).append(cells[1:])
    return out


def family_sections(doc: str) -> dict[str, tuple[str, list[str]]]:
    """Model name -> (the body of the model's own section, 12 to 16, and every model the section
    is for), from its heading: "## 14. CAMEO 5, CAMEO 5 Plus and CAMEO Pro MK-II"."""
    out = {}
    for m in re.finditer(r"^## 1[2-6]\. (.*?)$(.*?)(?=^## |\Z)", doc, re.M | re.S):
        names = re.split(r", | and ", m[1])
        for name in names:
            out[name] = (flat(m[2]), names)
    return out


def test_protocol_describes_every_model_of_the_registry():
    doc = text(PROTOCOL)
    overview = section(doc, "11")
    start = re.search(r"^## 11\. ", doc, re.M)
    assert start, "docs/PROTOCOL.md has no section 11"
    assert "TG9 FN0 TB50,0 FM1" in flat(doc[start.start():])
    assert len(registry.profiles()) >= 10
    families = family_sections(doc)
    for p in registry.profiles():
        row = next((r for r in overview.splitlines() if r.startswith(f"| {p.name} | ")), None)
        assert row, p.name
        cells = [c.strip() for c in row.split("|")]
        assert cells[2] == f"`{p.id}`", p.id
        assert all(f"`{t}`" in cells[4] for t in p.identity.fg_models), p.id
        level = cells[3]
        if p.support is Support.TESTED:            # "tested (V5), not yet tested (V1)"
            assert level.startswith("tested"), p.id
            for line in p.identity.lines:
                tag = "tested" if line.support is Support.TESTED else "not yet tested"
                assert f"{tag} (V{line.major})" in level, (p.id, line.major)
        else:
            assert level == "not yet tested", p.id
        assert cells[5] == ", ".join(f"V{line.major}" for line in p.identity.lines), p.id
        assert cells[6] == str(len(p.holders)), p.id
        # every mat, in the profile's order, and the first one is the mat offered first
        assert cells[7] == ", ".join(f"{m.label} ({m.tg})" for m in p.mats), p.id
        assert p.mats[0].key == p.default_mat, p.id
        assert range_cells(p) in model_rows(overview)[p.name], p.id
        own, names = families[p.name]
        for m in p.mats:
            a0, b0, a1, b1 = m.area_su
            assert f"\\{a0},{b0} Z{a1},{b1}" in own, (p.id, m.key)
            if m.expected_u is not None:
                assert f"{m.expected_u[0]},{m.expected_u[1]}" in own, (p.id, m.key)
        # the section names the basis of every mat of its models, and no other basis
        bases = {m.u_basis.value for q in registry.profiles() if q.name in names for m in q.mats}
        assert {b for b, bold in BASES.items() if bold in own} == bases, (p.id, sorted(bases))


def test_protocol_names_every_recognized_model_with_its_reason():
    """Section 17 has a row for every recognized model: the first cell names it, and the reason
    cell gives its own reason, or says it is that of another model with the same reason."""
    by_name = {m.name: m for m in registry.recognized_models()}
    rows = model_rows(section(text(PROTOCOL), "17"))
    rows.pop("Model", None)
    assert set(rows) == set(by_name)
    for name, model in by_name.items():
        (cells,) = rows[name]
        reason = cells[-1]
        same = re.fullmatch(r"as the (.+)", reason)
        if same:
            assert by_name[same[1]].reason == model.reason, name
        else:
            assert model.reason in reason, name


def test_protocol_states_the_rules_every_model_shares():
    doc = flat(text(PROTOCOL))
    assert "the smallest of 480, the profile's limit and MTU − 3" in doc
    assert "at least 32 bytes" in doc and "0.1, 0.2 and 0.4 s" in doc
    assert "TG9 FN0 TB50,0 FM1" in doc and "unchecked" in doc
    assert '{"op": "ping", "machine": true}' in doc and "`refused_reason`" in doc
    assert "The open reply carries `\"profile\"`" in doc
    for words in ("four spaces", "case-sensitively, never by prefix", "ESC 09 twice, 0.3 s apart"):
        assert words in doc, words


def test_protocol_fm_rule_names_the_models_of_the_code():
    """Section 5.2 names exactly the models the safety floor lets send FM1, and every model it
    never goes to."""
    from opencutter.machines import safety
    rule = flat(re.search(r"^### 5\.2 .*?$(.*?)(?=^##)", text(PROTOCOL), re.M | re.S)[1])
    allowed = [p.name for p in registry.profiles() if safety.expand_allowed(p)]
    assert allowed and all(mentions(rule, n) for n in allowed), allowed
    never = [registry.by_fg_model(t).name for t in sorted(safety.NO_EXPAND_MODELS)]
    sentence = re.search(r"never to an? (.*?), whatever a profile says", rule)
    assert sentence and all(mentions(sentence[1], n) for n in never), never
    assert not [n for n in allowed if mentions(sentence[1], n)]
    others = [p.name for p in registry.profiles() if p.name not in allowed]
    only = re.search(r"that is on a fixed list: (.*?);", rule)
    assert only and not [n for n in others if mentions(only[1], n)]


def supported_machines() -> tuple[str, str, str]:
    """README's "Supported machines": the tested part, the not yet tested part and the
    recognized part."""
    m = re.search(r"^## Supported machines$(.*?)(?=^### |^## )", text(README), re.M | re.S)
    assert m, "README has no section 'Supported machines'"
    doc = flat(m[1])
    assert doc.count("**Not yet tested.**") == 1
    assert doc.count("**Recognized, not supported yet.**") == 1
    tested, rest = doc.split("**Not yet tested.**")
    untested, recognized = rest.split("**Recognized, not supported yet.**")
    return tested, untested, recognized


def test_readme_lists_every_model_with_its_support_level():
    tested, untested, recognized = supported_machines()
    for p in registry.tested():
        for line in p.identity.lines:
            words = f"{p.name} with firmware V{line.major}.xx"
            assert words in (tested if line.support is Support.TESTED else untested), words
    for p in UNTESTED:
        assert mentions(untested, p.name) and not mentions(tested, p.name), p.id
        assert not mentions(recognized, p.name), p.id
    for model in registry.recognized_models():
        assert mentions(recognized, model.name), model.id
        assert not mentions(untested, model.name) and not mentions(tested, model.name), model.id
    machines = re.search(r"^\| `machines` \| (.*) \|$", readme_presets(), re.M)[1]
    assert all(f"`{p.id}`" in machines for p in registry.enabled())
    assert not [m.id for m in registry.recognized_models() if f"`{m.id}`" in machines]


def test_readme_preset_fields_give_every_models_own_range():
    """The presets table gives the CAMEO 4's ranges and every other model's where it differs."""
    rows = {m[1]: m[2] for m in re.finditer(r"^\| `(\w+)` \| (.*) \|$", readme_presets(), re.M)}
    cameo4 = registry.by_id("cameo4").ranges
    for p in registry.enabled():
        for field in ("speed", "force", "acceleration", "offset", "blade_depth", "overcut",
                      "passes"):
            r, base = getattr(p.ranges, field), getattr(cameo4, field)
            if r == base:
                continue
            if r is None:
                assert mentions(rows[field], p.name) and "has none" in rows[field], (p.id, field)
            else:
                assert f"`{r.lo}`..`{r.hi}`" in rows[field], (p.id, field)


# A claim of a hardware test ("tested on", "cut designs on", "verified on", "works with", ...),
# not after a negation: every model the claim names must be tested.
TESTED_CLAIM = re.compile(r"(?<![Nn]ot )(?<![Nn]ot yet )(?<![Nn]ever )(?<!not been )"
                          r"(?<!never been )(?<!not yet been )"
                          r"\b(?:[Tt]ested|[Cc]ut|[Vv]erified|[Ww]orks?|[Ww]orked)"
                          r"(?: [a-z]+){0,3}? (?:on|with) ")


def claimed_models(doc: str) -> set[str]:
    """The models that `doc` says it was tested on or with: each claim up to the end of its
    sentence or clause, or to a "not" ("tested on the CAMEO 4 and the CAMEO 5" names both)."""
    out = set()
    for m in TESTED_CLAIM.finditer(doc):
        claim = re.split(r"\. |\.$|;|\bnot\b|\bnever\b", doc[m.end():m.end() + 160])[0]
        for name in sorted(ALL_NAMES, key=len, reverse=True):
            if name in claim:
                out.add(name)
                claim = claim.replace(name, " ")
    return out


@pytest.mark.parametrize("path", [README, METAINFO, PACKAGE_INIT], ids=lambda p: p.name)
def test_claims_name_the_tested_model_and_no_more(path):
    """The README, the metainfo and the package docstring say that the app is tested on the
    CAMEO 4, name the other enabled models as not yet tested and the recognized ones as not
    supported yet, and claim no hardware test that did not happen."""
    doc = flat(text(path))
    assert "Works with Silhouette CAMEO 4" not in doc and "Only the CAMEO 4 is supported" not in doc
    assert re.search(r"[Tt]ested (on|with) the (Silhouette )?CAMEO 4", doc)
    assert "Not yet tested" in doc and "ecognized, not supported yet" in doc
    named = [p.name for p in UNTESTED if mentions(doc, p.name)]
    if path != PACKAGE_INIT:            # the docstring groups them ("the Portrait 3 and 4")
        assert named == [p.name for p in UNTESTED]
    for model in registry.recognized_models():
        if model.identity.ble_names:             # the ones a Bluetooth user may own
            assert mentions(doc, model.name), (path.name, model.name)
    claimed = claimed_models(doc)
    assert claimed and claimed <= {p.name for p in registry.tested()}, (path.name, claimed)


def test_claims_check_catches_an_overclaim():
    assert claimed_models(
        "Tested with the Silhouette CAMEO 4. It is tested on the CAMEO 5 Plus and the CC300; "
        "tested with a Portrait 3 V1.05, but it has not been tested on the CAMEO 5 Alpha. It "
        "has never been tested with the CURIO 2. Not yet tested: the CAMEO 4 Pro.") == \
        {"CAMEO 4", "CAMEO 5 Plus", "CC300", "Portrait 3"}
    # other claim verbs count too
    assert claimed_models("It has cut designs on one real CAMEO 4 and one real CAMEO 5.") == \
        {"CAMEO 4", "CAMEO 5"}
    assert claimed_models("Works with the Silhouette CAMEO 5.") == {"CAMEO 5"}
    assert claimed_models("Verified on a Portrait 4; worked with the CURIO 2.") == \
        {"Portrait 4", "CURIO 2"}
    assert claimed_models("It has not been verified on the CAMEO 5. Not cut on a Portrait 3; "
                          "never cut with the CAMEO 5 Alpha. Works with any SVG editor.") == set()


def metadata_claims() -> dict[str, str]:
    """The short claim surfaces outside the docs: pyproject's description and the desktop
    entry's Comment."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    desktop = re.search(r"^Comment=(.*)$", text(REPO / "data" / f"{APP_ID}.desktop"), re.M)
    assert desktop, "the desktop entry has no Comment"
    return {"pyproject.toml": project["description"], "desktop Comment": desktop[1]}


@pytest.mark.parametrize("where", ["pyproject.toml", "desktop Comment"])
def test_the_short_descriptions_claim_no_untested_model(where):
    claim = metadata_claims()[where]
    assert claimed_models(claim) <= {p.name for p in registry.tested()}, (where, claim)
    if where == "pyproject.toml":
        assert claimed_models(claim) == {"CAMEO 4"}, claim


# Studio's placeholder notation and file names (tests/studio_names.py; every other file of the
# repository is checked as well, by tests/test_repo_review_hygiene.py)
STUDIO_NOTATION = studio_names.STUDIO_NOTATION


@pytest.mark.parametrize("path", DOCS + [METAINFO, PACKAGE_INIT, REPO / "pyproject.toml"],
                         ids=lambda p: p.name)
def test_docs_hold_no_studio_notation(path):
    """No syntax in Studio's own placeholder notation and no Studio file names (CONTRIBUTING.md,
    "No Silhouette data")."""
    assert not STUDIO_NOTATION.findall(text(path)), path.name


# --- the stop, the machine's Pause key, the load step and the media (safety review) -------------
def readme_section(title: str) -> str:
    """The body of README's '## <title>' up to the next '## ' heading, flattened."""
    m = re.search(rf"^## {re.escape(title)}$(.*?)(?=^## |\Z)", text(README), re.M | re.S)
    assert m, f"README has no section {title!r}"
    return flat(m[1])


def test_readme_safety_says_not_to_resume_with_pause():
    """The machine's Pause key ends the job: the README says so, and says to press Unload (or
    switch the machine off), never Pause again, without claiming it was tried."""
    safety = readme_section("Safety")
    assert "**Do not press the machine's Pause key during a job.**" in safety
    bullet = safety.split("**Do not press the machine's Pause key during a job.**")[1]
    bullet = bullet.split(" - **")[0]                  # up to the next bullet
    assert "Do not press Pause again to continue" in bullet and "**Unload**" in bullet
    assert "has not been tried on a real machine yet" in bullet


def test_readme_safety_names_stop_machine_the_owed_stop_and_the_crash_limit():
    safety = readme_section("Safety")
    assert "**Stop Machine**" in safety and "`opencutter stop`" in safety
    # closing asks first, but a crash or kill relies on the service's stop during a step only
    assert "Closing the window during a job asks first" in safety
    assert "crashes or is killed" in safety
    assert "only when that happens while a step runs" in safety
    assert "between steps, for example at the outline check" in safety
    assert "nothing sends the stop: use the power button" in safety
    # after a lost link: the power button; the stop kept for the same machine is not tried yet
    assert "without the link no stop can reach the machine" in safety
    assert "as soon as the same machine is connected again" in safety
    assert "This has not been tried on a real machine yet" in safety


def test_the_docs_say_what_is_not_known_about_esc_09():
    """ESC 09: the firmware analysis shows that it clears the motion queue; whether commands
    already sent survive a stop is not known, and no doc may state the opposite."""
    readme, protocol_doc = flat(text(README)), flat(text(PROTOCOL))
    for doc in (readme, protocol_doc, flat(text(ARCHITECTURE))):
        assert "discards the queued motion" not in doc
        assert "leaves at most one batch queued" not in doc
    assert "What the stop command does is only partly known." in readme_section("Safety")
    stop = flat(section(text(PROTOCOL), "8"))
    assert "**What ESC 09 does is only partly known.**" in stop and "is not known" in stop
    assert "When the stop ended a paused state" in stop
    unknown = flat(section(text(PROTOCOL), "9"))
    assert "so whether queued motion survives a stop" in unknown
    assert "whether pressing Pause again after a stop would resume" in unknown


def test_no_doc_advises_pause_to_go_on():
    """No sentence of the docs tells the user to press Pause to continue or resume: every
    sentence that names Pause together with continuing or resuming is a negation, or an open
    question ("whether pressing Pause again ... would resume", section 9)."""
    for path in DOCS:
        for sentence in re.split(r"(?<=[.!?])\s+", flat(text(path))):
            if re.search(r"\b[Pp]ause\b", sentence) and re.search(r"continue|resum", sentence,
                                                                    re.I):
                assert re.search(r"\b(not|never|cannot|no|whether)\b", sentence, re.I), \
                    (path.name, sentence)


def test_the_stop_promises_match_the_code():
    """ARCHITECTURE's safety layer 8 and PROTOCOL's section 8 say when the stop goes out, what a
    lost link means (the owed stop) and that the owed stop is not tried on hardware."""
    arch = flat(text(ARCHITECTURE))
    assert "On any failure after motion started" not in arch
    layer = re.search(r"\n8\. The daemon sends the stop sequence.*?(?=\n\n)", text(ARCHITECTURE),
                      re.S)
    assert layer, "ARCHITECTURE.md has no safety layer 8"
    layer8 = flat(layer[0])
    for words in ("motion_possible", "Paused or Cancelling", "owed", "same machine",
                  "not tried on a real machine", "power button",
                  "A client that leaves between steps gets no stop"):
        assert words in layer8, words
    stop = flat(section(text(PROTOCOL), "8"))
    assert "when a step fails after motion started" not in stop
    for words in ("**After a lost link.**", "no stop can reach the machine",
                  "This has not been tried on a real machine", "**The stop without a job.**",
                  "`\"check\": true`", "A client that disconnects between steps gets no stop"):
        assert words in stop, words


def test_readme_using_it_says_where_the_sheet_goes_and_to_reload():
    using = readme_section("Using it")
    assert "top-left corner of the mat's grid" in using and "Media Size" in using
    assert "cannot keep the blade on a smaller sheet" in using
    assert "0.5 mm" in using
    assert "**Stop Machine**" in using and "**Send Again…**" in using
    assert "first show the Load sheet, even when the machine shows Ready" in using
    assert "\"Nothing was sent\"" in using


def test_readme_says_what_the_verbose_service_log_adds():
    doc = flat(text(README))
    assert "`opencutter serve -v` logs every status change" not in doc
    assert "every write and every reply" in doc
    assert "`[status change] 3 = Paused`" in doc and "`app.log`" in doc


def test_the_docs_claim_no_more_than_was_analysed_or_measured():
    """The second review of the stop docs: the ESC 09 effects come from the CAMEO 4's firmware
    only; the write time-outs and the idle stop are not presented as measured; the media check
    is about the design's points, not a promise about the blade; the sheet's place on the grid
    is the app's layout, not a measured fact; and the stop is owed only when the link was lost
    while motion was possible."""
    readme, protocol_doc = flat(text(README)), flat(text(PROTOCOL))
    assert "keeps every cut on the media" not in readme
    assert "a write is normally acknowledged" not in protocol_doc
    assert "On an idle machine the stop is harmless" not in protocol_doc
    safety = readme_section("Safety")
    assert "From an analysis of the CAMEO 4's firmware" in safety
    assert "From an analysis of the firmware," not in safety
    stop = flat(section(text(PROTOCOL), "8"))
    assert "From the analysis of the CAMEO 4's firmware" in stop
    using = readme_section("Using it")
    assert "has not been measured yet" in using
    assert "sends the machine's stop command again" not in using
    layer = re.search(r"\n8\. The daemon sends the stop sequence.*?(?=\n\n)", text(ARCHITECTURE),
                      re.S)
    assert layer and "when it was lost while motion was possible" in flat(layer[0])


# --- the README's Install section: the current release and the known limitations ----------------
WORKFLOW = REPO / ".github" / "workflows" / "flatpak.yml"
MANIFEST = REPO / "build-aux" / "flatpak" / f"{APP_ID}.yml"


def newest_stable_release() -> str:
    root = ET.parse(METAINFO).getroot()
    return next(r.get("version") for r in root.findall("releases/release")
                if r.get("type", "stable") == "stable")


def readme_install() -> str:
    m = re.search(r"^## Install$(.*?)(?=^## )", text(README), re.M | re.S)
    assert m, "README has no section 'Install'"
    return m[1]


def test_readme_install_names_the_newest_stable_release():
    """The download, check and install commands and the clone of a tag name the newest stable
    release of the metainfo, with the file names the release workflow attaches (the bundle
    opencutter-<version>.flatpak and SHA256SUMS) and the app id of the manifest."""
    version = newest_stable_release()
    install = readme_install()
    releases = "https://github.com/MortyNiners/opencutter/releases"
    bundle = f"opencutter-{version}.flatpak"
    for words in (f"[{version}]({releases}/tag/v{version})", f"`{bundle}`",
                  f"curl -LO {releases}/download/v{version}/{bundle}\n",
                  f"curl -LO {releases}/download/v{version}/SHA256SUMS\n",
                  f"sha256sum --check SHA256SUMS                       # prints: {bundle}: OK\n",
                  f"flatpak install --user ./{bundle}\n",
                  f"git clone --branch v{version} https://github.com/MortyNiners/opencutter.git\n",
                  f"flatpak run {APP_ID}\n"):
        assert words in install, words
    # no other version is named as the one to install
    named = set(re.findall(r"opencutter-([0-9][^\s`/]*?)\.flatpak", install))
    assert named <= {version, "0.2.0"}, named          # 0.2.0: the example of an update
    assert set(re.findall(r"--branch v(\S+)", install)) == {version}
    # what the workflow and the manifest make
    workflow = text(WORKFLOW)
    assert "bundle: opencutter-${{ steps.version.outputs.version }}.flatpak" in workflow
    assert 'sha256sum "$BUNDLE" > SHA256SUMS' in workflow
    assert '"dist/$BUNDLE" dist/SHA256SUMS' in workflow
    assert re.search(rf"^id: {re.escape(APP_ID)}$", text(MANIFEST), re.M)
    assert re.search(r"^command: opencutter$", text(MANIFEST), re.M)
    assert re.search(r"^runtime-version: '51'", text(MANIFEST), re.M) and "GNOME 51" in install


def test_readme_names_the_known_limitations():
    """Ending the service from outside the app, the unsigned bundle and the GNOME version a copy
    from source needs, under Install."""
    m = re.search(r"^### Known limitations$(.*?)(?=^##)", readme_install() + "\n## ", re.M | re.S)
    assert m, "README's Install section has no 'Known limitations'"
    known = flat(m[1])
    for words in ("Background Apps", "**Run in Background**", "`flatpak kill`",
                  "sends no stop to the machine", "does not release the Bluetooth link cleanly",
                  "**Disconnect**", "**The bundle is not signed.**", "`SHA256SUMS`",
                  "`sha256sum --check SHA256SUMS`",
                  "**From source, the app needs GNOME 49 or newer**"):
        assert words in known, words
