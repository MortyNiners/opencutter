# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Review regressions for repository hygiene (hygiene-7).

- OpenCutter contains no code or data that reads, decrypts or redistributes Silhouette Studio
  files: no such module, import, function or dependency, and no text that describes one,
  anywhere in the repository (not only in src/ and tests/).
- No personal data in any file: no real machine name (any model of the registry, with its
  serial number), Bluetooth address or home directory. An image (the screenshots) holds only
  its picture: no text, time or Exif data that could carry a name or a path.
- .gitignore keeps Silhouette data, installers, firmware and captures out of commits, without
  hiding any file of the project itself.
"""
import ast
import os
import re
import shutil
import struct
import subprocess
import tomllib
import zlib
from pathlib import Path

import pytest

from opencutter.machines import ble_name_prefixes, registry
from studio_names import FIRMWARE_ADDRESS, STUDIO_NOTATION, studio_names

REPO = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist"}
# In a git worktree (or a submodule) ".git" is a file, not git's folder: one line pointing at
# the main checkout's git folder, which git never publishes. It is left out like the folder.
SKIP_FILES = {".git"}


def publishable_files(root: Path = REPO) -> list[Path]:
    """Every file under ``root`` that git could publish: the whole tree, with only the venv, git's
    own folder (or the ".git" file of a worktree) and the caches left out (stricter than
    .gitignore, so a new top-level folder or a non-Python file is checked too)."""
    found = []
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        found += [Path(top, f) for f in files
                  if not f.endswith(".pyc") and f not in SKIP_FILES]
    return sorted(found)


def text_of(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def rel(path: Path) -> str:
    return str(path.relative_to(REPO))


ALL_FILES = publishable_files()
PYTHON_FILES = [p for p in ALL_FILES if p.suffix == ".py"]
TEXT_FILES = [p for p in ALL_FILES if text_of(p) is not None]


def test_the_scan_covers_the_whole_repository():
    assert len(PYTHON_FILES) > 60 and len(TEXT_FILES) > 80
    names = {rel(p) for p in TEXT_FILES}
    assert {"README.md", "LICENSE", ".gitignore", "pyproject.toml", "docs/PROTOCOL.md",
            "src/opencutter/gui/style.css", "tests/data/paths.svg"} <= names
    assert not [n for n in names if n.startswith((".venv/", ".git/")) or n == ".git"]


# --- no code for Silhouette Studio files ---------------------------------------------------------
# identifiers and words that only code for Studio's installer or its resource cipher would use
FORBIDDEN_WORDS = re.compile(r"decrypt|encrypt|cipher|key_?table|pseudo|installer|py7zr|"
                             r"sevenzip|seven_zip", re.IGNORECASE)
RULE = "reads, decrypts or redistributes"          # the rule that forbids such code names it
WORD_CHECK_EXEMPT = {".gitignore"}                 # lists the file names it keeps out (see below)


def test_no_module_for_studio_files():
    stems = {p.stem for p in PYTHON_FILES}
    assert not [s for s in stems if FORBIDDEN_WORDS.search(s)], stems
    presets = sorted(p.name for p in (REPO / "src" / "opencutter" / "presets").glob("*.py"))
    assert presets == ["__init__.py", "model.py"]


def identifiers(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.update(node.name.split("."))
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.update(node.module.split("."))
    return names


@pytest.mark.parametrize("path", PYTHON_FILES, ids=rel)
def test_no_code_for_studio_files(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = sorted(n for n in identifiers(tree) if FORBIDDEN_WORDS.search(n))
    assert not bad, f"{path.name} names {bad}"


def without_rules(path: Path, text: str) -> str:
    """The text without the rules that forbid such code: they have to name what they forbid."""
    if path.name == "CONTRIBUTING.md":
        text = re.sub(r"^## What must never enter the repository$.*?(?=^## )", "", text,
                      flags=re.M | re.S)
    return re.sub(r"\s+", " ", text).replace(RULE, "")


@pytest.mark.parametrize("path", [p for p in TEXT_FILES if p.suffix != ".py"
                                  and p.name not in WORD_CHECK_EXEMPT], ids=rel)
def test_no_text_about_reading_studio_files(path):
    found = FORBIDDEN_WORDS.search(without_rules(path, path.read_text(encoding="utf-8")))
    assert not found, (rel(path), found and found[0])


def test_the_regression_checks_really_match(tmp_path):
    sample = ast.parse("import py7zr\nfrom x.installer import read\n"
                       "def find_key_table(pseudo): return decrypt(pseudo)\n")
    assert {"py7zr", "installer", "find_key_table", "pseudo", "decrypt"} <= \
        {n for n in identifiers(sample) if FORBIDDEN_WORDS.search(n)}
    assert not FORBIDDEN_WORDS.search("load_svg SvgImportError import_path presets_path")
    for name in ("tools/reader.py", "docs/HOWTO.md", "data/table.json", ".venv/lib/x.py",
                 "src/__pycache__/x.pyc", ".git", "vendor/sub/.git", "x/.git/config"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")      # ".git" as a file: a worktree's or a submodule's
    found = {str(p.relative_to(tmp_path)) for p in publishable_files(tmp_path)}
    assert found == {"tools/reader.py", "docs/HOWTO.md", "data/table.json"}
    assert FORBIDDEN_WORDS.search(without_rules(Path("HOWTO.md"), "use the installer's key"))
    contributing = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert FORBIDDEN_WORDS.search(contributing)            # only its rules section is left out
    assert not FORBIDDEN_WORDS.search(without_rules(REPO / "CONTRIBUTING.md", contributing))


def test_pyproject_has_no_archive_or_studio_dependency():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    deps = list(project["dependencies"])
    for extra in project.get("optional-dependencies", {}).values():
        deps += extra
    names = {re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0].strip().lower() for d in deps}
    assert names == {"bleak", "dbus-fast", "svgelements", "pytest"}
    assert "py7zr" not in (REPO / "pyproject.toml").read_text().lower()


# --- no personal data ----------------------------------------------------------------------------
# A real machine advertises its model, "-" and its serial number ("CAMEO 4-", "CAMEO 5 PLUS-",
# "Portrait 3-", "CAMEO3-", "CURIO 2-", ...): every BLE name prefix of the registry (the family
# words "CAMEO", "Portrait" and "CURIO") with the rest of a model name, in any case. The tests
# use made-up names ("CAMEO 4-fake"). A serial is digits and capitals ("12345A"); one that
# starts with up to two letters is caught too. The one serial that names no machine is the
# firmware's default, exactly 0000001 (tests use it as a placeholder); any other serial is a
# real one, also one that starts with zeros.
MACHINE_WORDS = ble_name_prefixes()
DEFAULT_SERIAL = "0000001"
REAL_MACHINE_NAME = re.compile(
    r"(?:" + "|".join(re.escape(w) for w in MACHINE_WORDS) + r")[ A-Z0-9]*-"
    r"(?!" + DEFAULT_SERIAL + r"(?![0-9A-Z]))[A-Z]{0,2}\d{4}",
    re.IGNORECASE)
MAC = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:_-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])")
FAKE_MACS = {"AA:BB:CC:DD:EE:FF"} | {f"00:00:00:00:00:{i:02X}" for i in range(256)}
HOME = re.compile(r"/home/[a-z_]|/run/media/[a-z_]|/Users/[A-Za-z]")


@pytest.mark.parametrize("path", TEXT_FILES, ids=rel)
def test_no_personal_data(path):
    text = path.read_text(encoding="utf-8")
    assert not REAL_MACHINE_NAME.search(text), rel(path)
    macs = {re.sub("[_-]", ":", m).upper() for m in MAC.findall(text)}
    assert macs <= FAKE_MACS, (rel(path), sorted(macs - FAKE_MACS))
    assert not HOME.search(text), rel(path)


def test_the_personal_data_checks_really_match():
    # the samples are put together at run time, so this file passes its own check
    assert REAL_MACHINE_NAME.search("ping names it 'CAMEO 4-" + "123456Z'")
    assert not REAL_MACHINE_NAME.search("CAMEO 4-fake CAMEO 4-TEST CAMEO 4-XXXXXXX "
                                        "CAMEO 4 · Ready")
    assert not REAL_MACHINE_NAME.search("CURIO 2-" + DEFAULT_SERIAL + " Portrait 3-<serial> "
                                        "CAMEO 5 V1.07 'cameo 4-" + DEFAULT_SERIAL + "'")
    serial = "-" + "20261118"
    for name in ("Cameo 4", "CAMEO 4 PLUS", "cameo 5 alpha plus", "CAMEO PRO MARK2", "CAMEO3",
                 "Portrait 4", "Portrait2", "CURIO 2"):
        assert REAL_MACHINE_NAME.search(f"seen as '{name}{serial}'"), name
    # only the exact default serial is exempt: a real serial may start with zeros
    for real in ("0000" + "1234", DEFAULT_SERIAL + "2", DEFAULT_SERIAL + "A", "0000" + "002",
                 "A" + "1234567", "AB" + "12345"):
        assert REAL_MACHINE_NAME.search(f"CURIO 2-{real}"), real
    octets = ["12", "34", "56", "78", "9a", "bc"]
    bluez, colon = "_".join(octets).upper(), ":".join(octets)
    assert MAC.findall(f"dev_{bluez} and {colon}") == [bluez, colon]
    assert not MAC.findall("0000fff0-0000-1000-8000-00805f9b34fb 12:34:56 AA:BB:CC:DD:EE:FF0")
    assert HOME.search("/" + "home/someone/x") and not HOME.search("/run/user/4242/x.sock")


def test_every_bluetooth_model_of_the_registry_is_a_real_machine_name():
    """A model with a Bluetooth name advertises "<its FG model text>-<serial>" (the CAMEO 3's and
    the Portrait 2's adapters without the space): each one is caught, whatever the registry
    adds."""
    serial = "-" + "4711" + "2026"
    models = [e for e in registry.entries() if e.identity.ble_names]
    assert len(models) >= 13 and set(MACHINE_WORDS) >= {"CAMEO", "Portrait", "CURIO"}
    for entry in models:
        for text in entry.identity.fg_models:
            for name in (text, text.replace(" ", ""), text.upper(), text.lower()):
                assert REAL_MACHINE_NAME.search(name + serial), (entry.id, name)


# --- images: the picture only --------------------------------------------------------------------
# A PNG may carry text (tEXt, zTXt, iTXt: a file name, a path, the tool and its user), a time
# (tIME) or Exif data (eXIf). Only the chunks that describe the picture itself are allowed.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"sBIT",
              b"bKGD", b"pHYs"}
IMAGES = [p for p in ALL_FILES if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp",
                                                         ".svg")]


def png_chunks(data: bytes) -> list[bytes]:
    """The chunk types of a PNG file, in order, after checking its signature and every CRC."""
    assert data.startswith(PNG_SIGNATURE), "not a PNG file"
    chunks, i = [], len(PNG_SIGNATURE)
    while i < len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        kind, body = data[i + 4:i + 8], data[i + 8:i + 8 + length]
        (crc,) = struct.unpack(">I", data[i + 8 + length:i + 12 + length])
        assert len(body) == length and zlib.crc32(kind + body) == crc, kind
        chunks.append(kind)
        i += 12 + length
    assert chunks and chunks[0] == b"IHDR" and chunks[-1] == b"IEND"
    return chunks


@pytest.mark.parametrize("path", [p for p in IMAGES if p.suffix.lower() == ".png"], ids=rel)
def test_png_images_hold_only_the_picture(path):
    chunks = png_chunks(path.read_bytes())
    assert set(chunks) <= PNG_CHUNKS, (rel(path), sorted(set(chunks) - PNG_CHUNKS))


def test_images_are_svg_icons_or_png_files():
    """The repository's images are the app's SVG icons, the test designs and the PNG
    screenshots: a JPEG, GIF or WebP file (which may carry Exif data) would not be checked."""
    assert {p.suffix.lower() for p in IMAGES} <= {".png", ".svg"}
    assert any(p.suffix == ".png" for p in IMAGES)


def test_the_png_check_really_matches():
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    pixel = chunk(b"IDAT", zlib.compress(b"\x00\x00"))
    clean = PNG_SIGNATURE + header + pixel + chunk(b"IEND", b"")
    assert png_chunks(clean) == [b"IHDR", b"IDAT", b"IEND"]
    tagged = PNG_SIGNATURE + header + chunk(b"tEXt", b"Comment\x00/" + b"home/someone") + pixel + \
        chunk(b"IEND", b"")
    assert not set(png_chunks(tagged)) <= PNG_CHUNKS


# --- .gitignore ----------------------------------------------------------------------------------
# Neutral sample names, one or more for each pattern of .gitignore.
NEVER_COMMIT = [
    "setup.exe", "setup.dmg", "setup.msi", "files.7z",                  # installers, archives
    "fw.S", "fw.bin", "fw.srec", "fw.s19", "fw.mot", "fw_mat_table_x.txt",          # firmware
    "c.btsnoop", "btsnoop_hci.log", "c.pcap", "c.pcapng", "captures/x.txt",          # captures
    "gpgl_x.txt", "decoded_x.txt",
    "pseudo", "x/pseudo", "materials", "x/materials", "materials.json", "materials.dec",
    "materials.txt", "lang", "x/lang", "lang.json", "lang.dec", "lang.txt", "SPEC_X/y",
    "presets.json", "x/presets.json", "a_presets_fw.json", "studio-presets.json",
    "studio-presets-1.json", "cameo4_materials.x", "cameo4_commands.x", "cameo4_params.x",
    "peripherals.json",
]


TOP_LEVEL = [".gitignore", "ARCHITECTURE.md", "CONTRIBUTING.md", "LICENSE", "README.md",
             "pyproject.toml"]
PROJECT_DIRS = [".github", "build-aux", "data", "docs", "src", "tests"]


def project_files() -> list[str]:
    """Every file of the project's own folders, as a relative path (caches left out)."""
    out = [f for f in TOP_LEVEL if (REPO / f).is_file()]
    for top in PROJECT_DIRS:
        for root, dirs, files in os.walk(REPO / top):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".pytest_cache")
                       and not d.endswith(".egg-info")]
            out += [os.path.relpath(os.path.join(root, f), REPO) for f in files
                    if not f.endswith(".pyc")]
    return sorted(out)


def ignored(paths: list[str], tmp_path: Path) -> set[str]:
    """The paths that the repository's .gitignore ignores, asked of git in a scratch repo."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    shutil.copy(REPO / ".gitignore", tmp_path / ".gitignore")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run([git, "init", "-q", str(tmp_path)], check=True, env=env)
    r = subprocess.run([git, "-C", str(tmp_path), "check-ignore", "--no-index", "--stdin"],
                       input="\n".join(paths) + "\n", capture_output=True, text=True, env=env)
    assert r.returncode in (0, 1), r.stderr
    return set(r.stdout.split("\n")) - {""}


def test_gitignore_keeps_silhouette_data_out(tmp_path):
    assert ignored(NEVER_COMMIT, tmp_path) == set(NEVER_COMMIT)


def test_gitignore_hides_no_project_file(tmp_path):
    files = project_files()
    assert "src/opencutter/gui/materials.py" in files and "docs/PROTOCOL.md" in files
    assert "build-aux/flatpak/io.github.mortyniners.OpenCutter.yml" in files
    assert ".github/workflows/flatpak.yml" in files and "build-aux/release-version.py" in files
    assert ignored(files, tmp_path) == set()


# --- Studio's notation, names and addresses, anywhere ----------------------------------------------
# Not only the docs: no file of the repository may hold Studio's placeholder notation, its file
# names, firmware addresses or Studio's own column, group and enum names (CONTRIBUTING.md, "No
# Silhouette data" and "No firmware"). The names are kept as digests (tests/studio_names.py), so
# this check does not publish them itself. The clock format of the service's log lines is the one
# percent sign with a capital letter that is not Studio's notation.
CLOCK_FORMAT = "%H:%M:%S"


@pytest.mark.parametrize("path", TEXT_FILES, ids=rel)
def test_no_studio_notation_names_or_firmware_addresses_anywhere(path):
    text = text_of(path).replace(CLOCK_FORMAT, "")
    assert not STUDIO_NOTATION.findall(text), rel(path)
    assert not FIRMWARE_ADDRESS.findall(text), rel(path)
    assert not studio_names(text), rel(path)


@pytest.mark.parametrize("path", sorted((REPO / "src" / "opencutter" / "machines").glob("*.py")),
                         ids=rel)
def test_the_machine_profiles_hold_none_of_studios_words(path):
    """The profile modules are hand-written in our own words: none of Studio's names, not even
    those that are ordinary words elsewhere."""
    assert not studio_names(text_of(path), profile_module=True), rel(path)


def test_the_studio_checks_really_match(monkeypatch):
    import studio_names as names
    for sample in ("FX" + "%" + "B,1", "TJ" + "%" + "A" + "(" + "T)",
                   "the cutters" + ".c" + "sv table", "c" + "sv:24", "J" + "%" + "COND"):
        assert STUDIO_NOTATION.search(sample), sample
    assert FIRMWARE_ADDRESS.search("at 0x" + "12ab34cd") and not FIRMWARE_ADDRESS.search("0x0B4D")
    for clean in ("TB50,0 FX10,1", "Speed 5", "10%", "100 % of it", "the (t) key"):
        assert not STUDIO_NOTATION.search(clean), clean
    # a made-up name stands in for Studio's: whole words, hyphenated words and prefixes
    monkeypatch.setattr(names, "NAMES", frozenset({names.digest("made_up_key"),
                                                   names.digest("xx100-20")}))
    monkeypatch.setattr(names, "PREFIXES", {6: frozenset({names.digest("madeup")})})
    assert names.studio_names("a made_up_key, and made_up_keys") == ["made_up_key"]
    assert names.studio_names("the xx100-20 model") == ["xx100-20"]
    assert names.studio_names("madeup_one madeupTwo made_up") == ["madeupTwo", "madeup_one"]
    assert names.studio_names("nothing here") == []
    monkeypatch.setattr(names, "PROFILE_NAMES", frozenset({names.digest("ordinary")}))
    assert names.studio_names("an ordinary word") == []
    assert names.studio_names("an ordinary word", profile_module=True) == ["ordinary"]


def test_the_studio_name_digests_are_kept():
    import studio_names as names
    assert len(names.NAMES) == 43 and len(names.PROFILE_NAMES) == 5
    assert sum(len(d) for d in names.PREFIXES.values()) == 5
    assert all(re.fullmatch(r"[0-9a-f]{20}", d) for d in names.NAMES | names.PROFILE_NAMES)


# --- private notes ---------------------------------------------------------------------------------
# Public contributors cannot read the project's private planning notes: code, tests and docs give
# the reason in plain words or point to the public docs (docs/PROTOCOL.md, ARCHITECTURE.md).
PRIVATE_NOTES = re.compile(r"\bDECISION[S]\b|\([a]udit\b|\b[a]udit [A-Z][0-9]|owner's decision[s]")


@pytest.mark.parametrize("path", TEXT_FILES, ids=rel)
def test_nothing_points_to_private_notes(path):
    assert not PRIVATE_NOTES.findall(text_of(path)), rel(path)


def test_the_private_notes_check_really_matches():
    for sample in ("see DECI" + "SIONS 14", "(a" + "udit G1)", "a" + "udit H3", "the owner's "
                   "decision" + "s"):
        assert PRIVATE_NOTES.search(sample), sample
    assert not PRIVATE_NOTES.search("the decisions of the user, an audit of the code")


# --- the Flatpak manifest ----------------------------------------------------------------------------
def test_the_flatpak_manifest_says_which_machines_the_service_finds():
    """Discovery takes every name prefix of the registry, not the CAMEO 4 alone."""
    manifest = (REPO / "build-aux" / "flatpak" / "io.github.mortyniners.OpenCutter.yml").read_text()
    comment = re.search(r"# The background service finds (.*?)BlueZ", manifest, re.S)
    assert comment, "the comment on the Bluetooth permission is gone"
    words = " ".join(line.strip("# ") for line in comment[1].splitlines())
    assert "CAMEO 4" not in words
    for prefix in ble_name_prefixes():
        assert prefix in words, prefix
