# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Which OpenCutter this process runs: its version, a build hash of its sources and the version
of the contract between the app (or the command line) and the background service.

The background service reports these in its ping reply. The app and the command line compare
them with their own before they use a service: a service started before an update keeps
running the code it started with, and the build hash also catches a change without a version
bump. ``BUILD`` is computed once, when this module is first imported.

``RUN_ID`` names this process: a service the app starts gets it in its environment and reports
it, so the app knows the service it started itself, also when that service runs in another
Flatpak sandbox, where its pid means nothing to the app.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets

from . import __version__

# The client/daemon contract (ARCHITECTURE.md, "Socket protocol"). Raise it when a change means
# that an older app or command line must not use the service, or the other way round.
API_VERSION = 1
BUILD_CHARS = 12


def _sources(root: str, suffix: str) -> list[str]:
    """The regular files ending in ``suffix`` under ``root``. Hidden names (an editor's lock
    file such as Emacs's '.#window.py', a dangling symlink) and anything that is not a regular
    file are not part of the build."""
    found = []
    for top, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__" and not d.startswith(".")]
        for name in names:
            path = os.path.join(top, name)
            if name.endswith(suffix) and not name.startswith(".") and os.path.isfile(path):
                found.append(path)
    return found


def build_hash(root: str) -> str:
    """A short hash of this version (``__version__``) and of the ``.py`` files under ``root``
    as they are on disk: their sorted relative paths and their bytes. Caches (``__pycache__``)
    and other files do not count. An installation without sources (only ``.pyc`` files) hashes
    those instead; where no file can be read at all (a zip or a frozen bundle), the version
    still tells two releases apart."""
    files = _sources(root, ".py") or _sources(root, ".pyc")
    entries = sorted((os.path.relpath(p, root).replace(os.sep, "/"), p) for p in files)
    digest = hashlib.sha256(f"opencutter {__version__}\0".encode("utf-8", "surrogateescape"))
    for rel, path in entries:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            data = b"\0unreadable"
        digest.update(rel.encode("utf-8", "surrogateescape") + b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()[:BUILD_CHARS]


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD = build_hash(PACKAGE_DIR)


def build_on_disk() -> str:
    """The build of the package's files as they are on disk now. It differs from ``BUILD`` once
    OpenCutter was updated (or edited) after this process started: a service started now would
    report it, not ``BUILD``. Inside a Flatpak sandbox it never differs: the sandbox keeps the
    files it started with, and the service the app starts through the portal runs those too."""
    return build_hash(PACKAGE_DIR)


# A random id of this process (this run of the app or the command line). The app starts the
# service with it in $OPENCUTTER_STARTED_BY; the service reports it in its ping as "started_by".
RUN_ID = secrets.token_hex(8)
STARTED_BY_ENV = "OPENCUTTER_STARTED_BY"
_RUN_ID = re.compile(r"[0-9a-f]{16}")


def started_by(environ=None) -> str | None:
    """The run id of the process that started this service ($OPENCUTTER_STARTED_BY in
    ``environ``, default os.environ), or None when it is unset or not a run id."""
    value = (os.environ if environ is None else environ).get(STARTED_BY_ENV) or ""
    return value if _RUN_ID.fullmatch(value) else None


def identity() -> dict:
    """This build as the service's ping reports it: {"version", "build", "api"}."""
    return {"version": __version__, "build": BUILD, "api": API_VERSION}


def same_build(ping: object) -> bool:
    """True when a ping reply comes from a service of this very build and contract. An older
    service's reply has neither field, so it is never the same build."""
    if not isinstance(ping, dict):
        return False
    api = ping.get("api")
    return ping.get("build") == BUILD and type(api) is int and api == API_VERSION


def describe(ping: dict | None) -> str:
    """'OpenCutter 0.1.0 (build 1a2b3c4d5e6f)' for a service's ping, or the words for an older
    service whose ping has no version."""
    version, build = (ping or {}).get("version"), (ping or {}).get("build")
    if not isinstance(version, str) or not isinstance(build, str):
        return "an older OpenCutter background service (it reports no version)"
    return f"OpenCutter {version[:40]} (build {build[:BUILD_CHARS]})"


def this_build() -> str:
    """'OpenCutter 0.1.0 (build 1a2b3c4d5e6f)' for this process."""
    return describe(identity())
