#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The version of OpenCutter, read and checked for the Flatpak workflow
(.github/workflows/flatpak.yml) and for a release.

    python3 build-aux/release-version.py               # prints version=0.1.0
    python3 build-aux/release-version.py --tag v0.1.0  # prints version=0.1.0, or refuses

It checks that pyproject.toml, src/opencutter/__init__.py and the newest <release> of the
AppStream metainfo (the first one in data/<app id>.metainfo.xml) name the same version. AppStream
writes a PEP 440 ".devN" as "~devN". With --tag it also checks that the tag is "v" followed by
that version, and that the version is a release: X.Y.Z, three whole numbers, with a "stable"
newest <release>. On success it prints "version=<version>", the line the workflow appends to
$GITHUB_OUTPUT; otherwise it prints the reason on stderr and exits with status 1.

Standard library only, Python 3.11 or newer (tomllib).
"""
import argparse
import ast
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RELEASE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


class VersionError(Exception):
    """The versions do not match, or the tag is not a release of this version."""


def appstream_version(version: str) -> str:
    """How the metainfo writes a package version: "0.1.0.dev0" is "0.1.0~dev0"."""
    return version.replace(".dev", "~dev")


def module_constants(path: Path) -> dict[str, str]:
    """The string constants assigned at the top level of a Python file (read, never run)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}


def versions(repo: Path = REPO) -> dict[str, str]:
    """The version as each file names it, and the type of the newest metainfo release."""
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    package = module_constants(repo / "src" / "opencutter" / "__init__.py")
    metainfo = repo / "data" / f"{package['APP_ID']}.metainfo.xml"
    newest = ET.parse(metainfo).getroot().find("releases/release")
    if newest is None:
        raise VersionError(f"{metainfo.relative_to(repo)} has no <release>")
    return {"pyproject.toml": pyproject["project"]["version"],
            "src/opencutter/__init__.py": package["__version__"],
            "metainfo": newest.get("version", ""),
            "metainfo type": newest.get("type", "stable")}


def check(repo: Path = REPO, tag: str | None = None) -> str:
    """The package version, after the checks above; raises VersionError."""
    found = versions(repo)
    version = found["pyproject.toml"]
    if found["src/opencutter/__init__.py"] != version:
        raise VersionError(f"pyproject.toml says {version}, src/opencutter/__init__.py says "
                           f"{found['src/opencutter/__init__.py']}")
    if found["metainfo"] != appstream_version(version):
        raise VersionError(f"the newest <release> of the metainfo is {found['metainfo']!r}, "
                           f"not {appstream_version(version)!r} (version {version})")
    if tag is not None:
        if tag != f"v{version}":
            raise VersionError(f"the tag {tag} is not v{version}, the version in "
                               "pyproject.toml, src/opencutter/__init__.py and the metainfo")
        if not RELEASE.fullmatch(version):
            raise VersionError(f"{version} is not a release version (X.Y.Z): bump the version "
                               "before tagging")
        if found["metainfo type"] != "stable":
            raise VersionError(f"the newest <release> of the metainfo has type "
                               f"{found['metainfo type']!r}, not 'stable'")
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", help="the release tag, vX.Y.Z")
    parser.add_argument("--repo", type=Path, default=REPO, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        version = check(args.repo, args.tag)
    except (VersionError, OSError, KeyError, ET.ParseError, tomllib.TOMLDecodeError) as e:
        print(f"release-version: {e}", file=sys.stderr)
        return 1
    print(f"version={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
