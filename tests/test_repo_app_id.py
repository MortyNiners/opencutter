# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The application id is the same everywhere: APP_ID, the data file names, the desktop file,
the AppStream metainfo (id, launchable, icon, developer, URLs, release, screenshots), the icon
file names, the code that uses it (application id, icons, program name, About links) and the
project links in the docs. No display needed."""
import ast
import configparser
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from opencutter import APP_ID

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
PACKAGE = REPO / "src" / "opencutter"
ICONS = PACKAGE / "gui" / "icons"
PROJECT = "https://github.com/MortyNiners/opencutter"
RAW = "https://raw.githubusercontent.com/MortyNiners/opencutter"     # files of a tag of the project
SCREENSHOTS = DATA / "screenshots"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_app_id_is_reverse_dns_on_the_owners_github_pages_domain():
    assert APP_ID == "io.github.mortyniners.OpenCutter"
    assert re.fullmatch(r"io\.github\.[a-z0-9-]+\.OpenCutter", APP_ID)


def test_data_files_are_named_after_the_app_id():
    """The desktop file and the metainfo, and the screenshots the metainfo shows (and nothing
    else: a screenshot no metainfo names is a file nobody sees)."""
    names = sorted(str(p.relative_to(DATA)) for p in DATA.rglob("*") if p.is_file())
    shots = [f"screenshots/{name}" for name, _, _ in metainfo_screenshots()]
    assert shots and names == sorted([f"{APP_ID}.desktop", f"{APP_ID}.metainfo.xml", *shots])


def test_the_icons_are_package_data_named_after_the_app_id():
    """The hicolor icon tree is part of the package (gui/icons), so an installed copy has the
    in-app icons too; the Flatpak manifest installs the app icons from there for the desktop."""
    names = sorted(str(p.relative_to(ICONS)) for p in ICONS.rglob("*") if p.is_file())
    assert names == sorted([
        f"hicolor/scalable/apps/{APP_ID}.svg",
        f"hicolor/symbolic/apps/{APP_ID}-symbolic.svg",
        f"hicolor/symbolic/apps/{APP_ID}-align-symbolic.svg",     # the Align menu button
    ])
    # setuptools ships what [tool.setuptools.package-data] names (globs relative to the package)
    patterns = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["setuptools"][
        "package-data"]["opencutter"]
    shipped = {p for pattern in patterns for p in PACKAGE.glob(pattern) if p.is_file()}
    assert {p for p in ICONS.rglob("*") if p.is_file()} <= shipped
    assert PACKAGE / "gui" / "style.css" in shipped
    # and the app looks for them next to its own module, not in a checkout's data folder
    [icons] = [n.value for n in gui_module("app.py").body if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "ICONS" for t in n.targets)]
    assert ast.unparse(icons) == "Path(__file__).with_name('icons')"


def test_desktop_file_uses_the_app_id_icon():
    entry = configparser.ConfigParser(interpolation=None)
    entry.optionxform = str
    entry.read(DATA / f"{APP_ID}.desktop", encoding="utf-8")
    desktop = entry["Desktop Entry"]
    assert desktop["Icon"] == APP_ID
    assert desktop["Exec"] == "opencutter" and desktop["Name"] == "OpenCutter"


def test_metainfo_ids_and_urls():
    root = ET.parse(DATA / f"{APP_ID}.metainfo.xml").getroot()
    assert root.findtext("id") == APP_ID
    launchable = root.find("launchable")
    assert launchable.get("type") == "desktop-id" and launchable.text == f"{APP_ID}.desktop"
    assert root.findtext("icon") == APP_ID
    developer = root.find("developer")
    assert developer.get("id") == "io.github.mortyniners"
    assert developer.findtext("name") == "OpenCutter contributors"
    urls = {u.get("type"): u.text for u in root.findall("url")}
    assert urls == {"homepage": PROJECT, "bugtracker": f"{PROJECT}/issues", "vcs-browser": PROJECT}


def metainfo_screenshots() -> list[tuple[str, str, ET.Element]]:
    """(file name, tag, <screenshot>) for each screenshot of the metainfo, in order; each image
    URL must be a file of data/screenshots/ at a tag of the project."""
    root = ET.parse(DATA / f"{APP_ID}.metainfo.xml").getroot()
    out = []
    for shot in root.findall("screenshots/screenshot"):
        [image] = shot.findall("image")
        m = re.fullmatch(re.escape(RAW) + r"/(v[0-9][^/]*)/data/screenshots/([a-z0-9-]+\.png)",
                         image.text or "")
        assert m, image.text
        out.append((m[2], m[1], shot))
    return out


def test_the_metainfo_shows_the_screenshots_of_a_release():
    """The screenshots are the PNG files in data/screenshots/, at their raw.githubusercontent.com
    URL under the tag of a stable release in the metainfo (a tag that exists once that release
    is out; a github.com/.../raw/... URL would be a redirect). The first one is the default, and
    every one has a short caption."""
    root = ET.parse(DATA / f"{APP_ID}.metainfo.xml").getroot()
    stable = {f"v{r.get('version')}" for r in root.findall("releases/release")
              if r.get("type", "stable") == "stable"}
    shots = metainfo_screenshots()
    assert len(shots) >= 2
    assert [s.get("type") for _, _, s in shots] == ["default"] + [None] * (len(shots) - 1)
    assert len({name for name, _, _ in shots}) == len(shots)
    for name, tag, shot in shots:
        assert tag in stable, (name, tag, sorted(stable))
        assert (SCREENSHOTS / name).read_bytes().startswith(PNG_SIGNATURE), name
        [caption] = shot.findall("caption")
        text = (caption.text or "").strip()
        assert text and len(text) <= 100 and not text.endswith("."), (name, text)


def test_the_readme_shows_the_default_screenshot_near_the_top():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    top = readme.split("\n## ", 1)[0]
    default = metainfo_screenshots()[0][0]
    assert re.search(rf"!\[[^\]]+\]\(data/screenshots/{re.escape(default)}\)", top), default


def test_the_newest_release_is_the_package_version():
    """AppStream lists the version that exists: a PEP 440 ".devN" is written "~devN" there, which
    AppStream orders before the final release. Bump all three together when tagging."""
    from opencutter import __version__
    root = ET.parse(DATA / f"{APP_ID}.metainfo.xml").getroot()
    newest = root.find("releases/release")
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert newest.get("version").replace("~dev", ".dev") == __version__ == pyproject
    assert newest.get("type") == ("development" if ".dev" in __version__ else "stable")


def gui_module(name: str) -> ast.Module:
    tree = ast.parse((REPO / "src" / "opencutter" / "gui" / name).read_text(encoding="utf-8"))
    imported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom)
                and n.module == "opencutter" for a in n.names}
    assert "APP_ID" in imported, name                  # APP_ID is opencutter.APP_ID, not a copy
    return tree


def calls(tree: ast.AST, func: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func) == func]


def argument(call: ast.Call, name: str | int) -> str | None:
    """The source of a keyword argument (by name) or a positional one (by index)."""
    if isinstance(name, int):
        return ast.unparse(call.args[name]) if len(call.args) > name else None
    return next((ast.unparse(k.value) for k in call.keywords if k.arg == name), None)


def test_about_dialog_links_to_the_project():
    tree = gui_module("dialogs.py")
    consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert consts["WEBSITE"] == PROJECT
    [about] = calls(tree, "Adw.AboutDialog")
    assert argument(about, "application_icon") == "APP_ID"
    assert argument(about, "website") == "WEBSITE"
    assert argument(about, "issue_url") == "f'{WEBSITE}/issues'"


def test_the_app_registers_and_shows_itself_under_the_app_id():
    app = gui_module("app.py")
    [init] = calls(app, "super().__init__")
    assert argument(init, "application_id") == "APP_ID"
    [icon] = calls(app, "Gtk.Window.set_default_icon_name")
    assert argument(icon, 0) == "APP_ID"
    # the program name (X11 WM_CLASS) is the app id, set before the application runs
    main = next(n for n in app.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    [prgname] = calls(main, "GLib.set_prgname")
    [run] = calls(main, "app.run")
    assert argument(prgname, 0) == "APP_ID" and prgname.lineno < run.lineno
    images = [c for c in calls(gui_module("firstrun.py"), "Gtk.Image")
              if argument(c, "pixel_size") == "128"]
    assert [argument(c, "icon_name") for c in images] == ["APP_ID"]


@pytest.mark.parametrize("name", ["README.md", "CONTRIBUTING.md", "ARCHITECTURE.md",
                                  f"data/{APP_ID}.metainfo.xml"])
def test_every_github_link_points_to_the_project(name):
    """github.com links: the project, its issues, its releases, the page of one release and the
    two files of a release; raw.githubusercontent.com links: the project's files."""
    text = (REPO / name).read_text(encoding="utf-8")
    links = re.findall(r"https?://(?:www\.)?github\.com[^\s)>\"<`']*", text, re.IGNORECASE)
    version = r"[0-9]+\.[0-9]+\.[0-9]+"
    allowed = (re.escape(PROJECT) + rf"(\.git|/issues|/releases(/tag/v{version}|/download/"
               rf"v(?P<tag>{version})/(opencutter-(?P=tag)\.flatpak|SHA256SUMS))?)?")
    bad = [u for u in links if not re.fullmatch(allowed, u)]
    assert not bad, (name, bad)
    if name != "ARCHITECTURE.md":
        assert links, name
    raw = re.findall(r"https?://[\w.-]*githubusercontent\.com[^\s)>\"<`']*", text, re.IGNORECASE)
    assert not [u for u in raw if not u.startswith(RAW + "/")], (name, raw)


def test_no_old_or_placeholder_ids_are_left():
    pattern = re.compile(r"io\.github\.opencutter|github\.com/opencutter|opencutter\.github",
                         re.IGNORECASE)
    for top in ("src", "data", "tests", "docs"):
        for path in (REPO / top).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path != Path(__file__):
                # binary files (the screenshots) too: an id could hide in a text chunk
                text = path.read_bytes().decode("utf-8", errors="replace")
                assert not pattern.search(text), path
    for name in ("README.md", "ARCHITECTURE.md", "CONTRIBUTING.md", "pyproject.toml"):
        assert not pattern.search((REPO / name).read_text(encoding="utf-8")), name
