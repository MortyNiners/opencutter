# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Review regression hygiene-8: every source file carries the same license and copyright
notice, and the README and the About dialog name the copyright holder. No display needed."""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LICENSE_ID = "SPDX-License-Identifier: GPL-3.0-or-later"
COPYRIGHT_ID = "SPDX-FileCopyrightText: 2026 OpenCutter contributors"
HOLDER = "Copyright (C) 2026 OpenCutter contributors"


def sources() -> list[Path]:
    found = [p for top in ("src", "tests") for p in (REPO / top).rglob("*")
             if p.suffix in (".py", ".css") and "__pycache__" not in p.parts]
    assert len(found) > 60
    return sorted(found)


@pytest.mark.parametrize("path", sources(), ids=lambda p: str(p.relative_to(REPO)))
def test_every_source_file_starts_with_the_spdx_header(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    if path.suffix == ".css":
        assert lines[:2] == [f"/* {LICENSE_ID} */", f"/* {COPYRIGHT_ID} */"]
    else:
        assert lines[:2] == [f"# {LICENSE_ID}", f"# {COPYRIGHT_ID}"]
    assert sum(line.startswith(("# SPDX-", "/* SPDX-")) for line in lines) == 2   # no duplicates


def packaging_files() -> list[Path]:
    """The release tooling: the Flatpak manifest, the version script and the workflows."""
    found = [p for top in ("build-aux", ".github") for p in (REPO / top).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts]
    names = {str(p.relative_to(REPO)) for p in found}
    assert {"build-aux/flatpak/io.github.mortyniners.OpenCutter.yml",
            "build-aux/release-version.py", ".github/workflows/flatpak.yml"} <= names
    return sorted(found)


@pytest.mark.parametrize("path", packaging_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_every_packaging_file_starts_with_the_spdx_header(path):
    assert path.suffix in (".py", ".yml", ".yaml"), path    # all of them take '#' comments
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    assert lines[:2] == [f"# {LICENSE_ID}", f"# {COPYRIGHT_ID}"]
    assert sum(line.startswith("# SPDX-") for line in lines) == 2


def test_readme_license_section_names_the_copyright_holder():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    section = readme.split("## License", 1)[1]
    assert HOLDER in section and "GPL-3.0-or-later" in section


def test_about_dialog_sets_the_copyright():
    src = (REPO / "src" / "opencutter" / "gui" / "dialogs.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert consts.get("COPYRIGHT") == HOLDER
    about = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "AboutDialog"]
    assert len(about) == 1
    kw = {k.arg: k.value for k in about[0].keywords}
    assert isinstance(kw.get("copyright"), ast.Name) and kw["copyright"].id == "COPYRIGHT"
    assert ast.unparse(kw["license_type"]) == "Gtk.License.GPL_3_0"
