# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""build-aux/release-version.py, the version check of the release workflow: the three version
files must agree, and a release tag must be v + that version, a release (X.Y.Z) with a stable
newest <release>. Run on made-up copies of the three files; nothing is built or tagged."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from opencutter import APP_ID, __version__

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "build-aux" / "release-version.py"
_spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
release_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_version)


def make_repo(root: Path, pyproject="0.1.0", package="0.1.0", metainfo="0.1.0",
              release_type="stable") -> Path:
    """The three files the script reads, with the given versions."""
    (root / "src" / "opencutter").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "pyproject.toml").write_text(f'[project]\nname = "opencutter"\nversion = "{pyproject}"\n')
    (root / "src" / "opencutter" / "__init__.py").write_text(
        f'"""Doc."""\n__version__ = "{package}"\nAPP_ID = "{APP_ID}"\n')
    kind = f' type="{release_type}"' if release_type else ""
    (root / "data" / f"{APP_ID}.metainfo.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<component type="desktop-application">\n'
        f"  <id>{APP_ID}</id>\n  <releases>\n"
        f'    <release version="{metainfo}"{kind} date="2026-12-01"/>\n'
        '    <release version="0.0.9" type="stable" date="2026-11-01"/>\n'
        "  </releases>\n</component>\n")
    return root


def test_the_repository_itself_agrees_with_the_package():
    assert release_version.check(REPO) == __version__


def test_appstream_writes_dev_versions_with_a_tilde():
    assert release_version.appstream_version("0.1.0.dev0") == "0.1.0~dev0"
    assert release_version.appstream_version("1.2.3") == "1.2.3"


def test_a_release_tag_of_the_version_passes(tmp_path):
    repo = make_repo(tmp_path)
    assert release_version.check(repo) == "0.1.0"
    assert release_version.check(repo, tag="v0.1.0") == "0.1.0"
    # AppStream's default release type is stable
    assert release_version.check(make_repo(tmp_path / "b", release_type=None), "v0.1.0") == "0.1.0"


def test_a_development_version_builds_but_is_never_released(tmp_path):
    repo = make_repo(tmp_path, "0.2.0.dev0", "0.2.0.dev0", "0.2.0~dev0", "development")
    assert release_version.check(repo) == "0.2.0.dev0"
    with pytest.raises(release_version.VersionError, match="not a release version"):
        release_version.check(repo, tag="v0.2.0.dev0")


@pytest.mark.parametrize("files, match", [
    (dict(package="0.1.1"), "src/opencutter/__init__.py says 0.1.1"),
    (dict(pyproject="0.1.1"), "src/opencutter/__init__.py says 0.1.0"),
    (dict(metainfo="0.0.9"), "newest <release> of the metainfo is '0.0.9'"),
    (dict(metainfo="0.1.0.dev0"), "newest <release>"),
])
def test_the_three_files_must_agree(tmp_path, files, match):
    with pytest.raises(release_version.VersionError, match=match):
        release_version.check(make_repo(tmp_path, **files))


@pytest.mark.parametrize("tag", ["v0.1.1", "0.1.0", "v0.1.0 ", "v0.1", "V0.1.0", "v0.1.0-rc1",
                                 "refs/tags/v0.1.0"])
def test_a_tag_that_is_not_v_plus_the_version_is_refused(tmp_path, tag):
    with pytest.raises(release_version.VersionError, match="is not v0.1.0"):
        release_version.check(make_repo(tmp_path), tag=tag)


@pytest.mark.parametrize("version", ["0.1", "0.1.0rc1", "0.1.0.post1", "01.1.0", "0.1.0+local"])
def test_only_x_y_z_is_a_release(tmp_path, version):
    repo = make_repo(tmp_path, version, version, version)
    with pytest.raises(release_version.VersionError, match="not a release version"):
        release_version.check(repo, tag=f"v{version}")


def test_a_release_needs_a_stable_newest_release(tmp_path):
    repo = make_repo(tmp_path, release_type="development")
    assert release_version.check(repo) == "0.1.0"
    with pytest.raises(release_version.VersionError, match="type 'development'"):
        release_version.check(repo, tag="v0.1.0")


def test_the_command_prints_the_github_output_line_or_refuses(tmp_path):
    """The workflow appends stdout to $GITHUB_OUTPUT: exactly one line version=<version>."""
    repo = make_repo(tmp_path)
    run = [sys.executable, "-I", str(SCRIPT), "--repo", str(repo)]
    ok = subprocess.run(run + ["--tag", "v0.1.0"], capture_output=True, text=True, timeout=60)
    assert (ok.returncode, ok.stdout, ok.stderr) == (0, "version=0.1.0\n", "")
    bad = subprocess.run(run + ["--tag", "v0.2.0"], capture_output=True, text=True, timeout=60)
    assert bad.returncode == 1 and bad.stdout == ""
    assert bad.stderr.startswith("release-version: the tag v0.2.0 is not v0.1.0")
    missing = subprocess.run(run[:-1] + [str(tmp_path / "nowhere")], capture_output=True,
                             text=True, timeout=60)
    assert missing.returncode == 1 and missing.stdout == "" and "release-version:" in missing.stderr


def test_the_script_runs_with_the_standard_library_only():
    """The release job runs it with the runner's own python3, without installing anything."""
    import ast
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
                for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n")
