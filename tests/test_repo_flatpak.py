# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The Flatpak manifest (build-aux/flatpak): the app id, the GNOME runtime, the least
permissions that work (no network, no files, only BlueZ on the system bus), Python modules
pinned to PyPI release files and installed offline, and the files the app module installs. No
build, no network: the manifest is only read."""
import re
import tomllib
from pathlib import Path

import pytest

from opencutter import APP_ID

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO / "build-aux" / "flatpak" / f"{APP_ID}.yml"
MANIFEST = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
MODULES = {m["name"]: m for m in MANIFEST["modules"]}
PYPI = re.compile(r"https://files\.pythonhosted\.org/packages/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{60}/"
                  r"(?P<file>[A-Za-z0-9_.-]+\.(tar\.gz|whl))")
PINNED = {"poetry-core": "2.5.0", "dbus-fast": "5.2.0", "bleak": "3.1.0", "svgelements": "1.9.6"}


def test_app_id_runtime_and_command():
    assert MANIFEST["id"] == APP_ID and MANIFEST["command"] == "opencutter"
    assert MANIFEST["runtime"] == "org.gnome.Platform" and MANIFEST["sdk"] == "org.gnome.Sdk"
    assert MANIFEST["runtime-version"] == "51"


def test_the_permissions_are_the_window_the_gpu_and_bluez_only():
    assert sorted(MANIFEST["finish-args"]) == sorted([
        "--share=ipc", "--socket=wayland", "--socket=fallback-x11", "--device=dri",
        "--system-talk-name=org.bluez"])
    args = MANIFEST["finish-args"]
    assert len(args) == len(set(args))
    # no network, no files of the host, nothing on the session bus, no other system bus name
    assert not [a for a in args if a.startswith(("--share=network", "--filesystem", "--persist",
                                                  "--talk-name", "--own-name", "--socket=session-bus",
                                                  "--socket=system-bus", "--device=all"))]
    assert [a for a in args if "talk-name" in a] == ["--system-talk-name=org.bluez"]
    assert "*" not in "".join(args)
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    for arg in MANIFEST["finish-args"]:                     # each one is explained
        before = text.split(f"  - {arg}\n", 1)[0].rstrip().splitlines()[-1].strip()
        assert before.startswith(("#", "- --")), arg


def test_python_modules_are_pinned_pypi_files_installed_offline():
    names = [m["name"] for m in MANIFEST["modules"]]
    assert names == ["python3-poetry-core", "python3-dbus-fast", "python3-bleak",
                     "python3-svgelements", "opencutter"]
    for project, version in PINNED.items():
        module = MODULES[f"python3-{project}"]
        [source] = module["sources"]
        assert source["type"] == "file" and re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
        found = PYPI.fullmatch(source["url"])
        assert found, source["url"]
        assert found["file"].replace("_", "-").startswith(f"{project}-{version}"), found["file"]
        [install] = [c for c in module["build-commands"] if "pip3 install" in c]
        for flag in ("--no-index", '--find-links="file://${PWD}"', "--no-build-isolation",
                     '--prefix="${FLATPAK_DEST}"', f"{project}=={version}"):
            assert flag in install, (project, flag)
    assert MODULES["python3-poetry-core"]["cleanup"] == ["*"]          # build-only
    # svgelements' own tests would land in site-packages as a package named "test"
    assert MODULES["python3-svgelements"]["cleanup"] == ["/lib/python3.14/site-packages/test"]
    # the wheel is the only exception to "from source", and it is pure Python
    assert MODULES["python3-bleak"]["sources"][0]["url"].endswith("-py3-none-any.whl")
    # dbus-fast must have its compiled extension, never the silent pure-Python fallback
    check = MODULES["python3-dbus-fast"]["build-commands"][1]
    assert "REQUIRE_CYTHON=1" in MODULES["python3-dbus-fast"]["build-commands"][0]
    assert 'endswith(".so")' in check and "sys.exit(" in check


def test_the_pins_satisfy_the_package_requirements():
    version = pytest.importorskip("packaging.version")
    requirements = pytest.importorskip("packaging.requirements")
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    for dep in project["dependencies"]:
        req = requirements.Requirement(dep)
        assert version.Version(PINNED[req.name]) in req.specifier, dep


def test_the_app_module_builds_this_repository_and_installs_its_files():
    app = MODULES["opencutter"]
    [source] = app["sources"]
    assert source["type"] == "dir"
    assert (MANIFEST_PATH.parent / source["path"]).resolve() == REPO
    assert {".git", ".venv", "tests"} <= set(source["skip"])
    # the build folders of the release workflow (in its checkout) and of a local build, and
    # what `pip install .` leaves in a working copy (pip would reuse a stale build/lib)
    assert {"build-dir", "flatpak-repo", ".flatpak-builder", "build", "dist"} <= set(source["skip"])
    commands = app["build-commands"]
    assert commands[0] == 'pip3 install --no-index --no-build-isolation --prefix="${FLATPAK_DEST}" .'
    installed = [c.split()[-1].replace("${FLATPAK_ID}", APP_ID) for c in commands
                 if c.startswith("install ")]
    assert installed == [f"data/{APP_ID}.desktop", f"data/{APP_ID}.metainfo.xml",
                         f"src/opencutter/gui/icons/hicolor/scalable/apps/{APP_ID}.svg",
                         f"src/opencutter/gui/icons/hicolor/symbolic/apps/{APP_ID}-symbolic.svg"]
    for path in installed:
        assert (REPO / path).is_file(), path


def test_the_app_module_checks_what_it_installed():
    """A smoke test in every build (the release workflow's too): the command runs, every module
    imports (the GUI, the service and their dependencies), and the style sheet and the three
    icons are package data. It runs before anything else is installed."""
    commands = MODULES["opencutter"]["build-commands"]
    assert commands[1] == "${FLATPAK_DEST}/bin/opencutter --version"
    check = commands[2]
    assert check.startswith("python3 -I -c '") and check.endswith("'")
    code = check[len("python3 -I -c '"):-1]
    compile(code, "smoke", "exec")
    assert "import os, opencutter.device.daemon, opencutter.gui.app as app;" in code
    assert "app.STYLE" in code and "raise SystemExit(" in code
    icons = REPO / "src" / "opencutter" / "gui" / "icons" / "hicolor"
    for name in ('"scalable/apps" / (a + ".svg")', '"symbolic/apps" / (a + "-symbolic.svg")',
                 '"symbolic/apps" / (a + "-align-symbolic.svg")'):
        assert name in code, name
    assert sorted(p.relative_to(icons).as_posix() for p in icons.rglob("*.svg")) == [
        f"scalable/apps/{APP_ID}.svg", f"symbolic/apps/{APP_ID}-align-symbolic.svg",
        f"symbolic/apps/{APP_ID}-symbolic.svg"]
    assert all(c.startswith("install ") for c in commands[3:])
