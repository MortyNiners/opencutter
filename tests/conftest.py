# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Settings for every test.

The suite also runs inside OpenCutter's own Flatpak sandbox (to try the runtime's Python and
GTK), where /.flatpak-info exists. Every test runs as outside a sandbox, wherever the suite
runs: a test of the Flatpak code points protocol.FLATPAK_INFO at a stand-in file of its own
(tests/test_device_flatpak.py). Otherwise a test that starts a service would start it through
the Flatpak portal, outside the test's private directories.

The suite also runs without PyGObject (the test job of .github/workflows/flatpak.yml installs
none): then every test that needs GTK is skipped and all the others run. The package
opencutter.gui only selects the GTK and libadwaita versions (gi.require_version); its logic
modules (machine, runner, sending, geometry, document, settings) import nothing of GTK. So a
stand-in package gi takes that version selection, and importing anything from it (gi.repository,
gi.events) skips the test module, or the test, that does it, but only when the import comes from
the tests or from opencutter.gui. Code of OpenCutter outside opencutter.gui (the service, the
command line, the machine profiles) never needs GTK: if it imports gi, directly or through a GUI
module, the import fails as it would without PyGObject, so the CI run fails instead of quietly
skipping the tests of that code. With PyGObject installed, the stand-in is never made."""
import importlib.abc
import importlib.util
import os
import sys
import types

import pytest

from opencutter.device import protocol      # no Bluetooth: protocol imports neither bleak nor dbus_fast

HAVE_PYGOBJECT = importlib.util.find_spec("gi") is not None
NO_PYGOBJECT = "needs GTK 4, and PyGObject is not installed"
# tests/test_gui_review_headless.py checks, in a child pytest, that the GTK window modules are
# skipped for want of a display. Without PyGObject they are skipped before that question.
NEEDS_PYGOBJECT = {"test_gui_review_headless.py"}


def _inside(path: str, folder: str) -> bool:
    return path.startswith(folder + os.sep)


class _SkipWithoutPyGObject(importlib.abc.MetaPathFinder):
    """Turns an import of gi.<anything> made for a test into a skip (of the module while it is
    collected, of the test while it runs), as pytest.importorskip would. The reason names the
    line of the test module that led to the import.

    The calls that led to the import are read from the innermost out, up to the first frame of
    a test module (or a helper of the tests): every frame of OpenCutter's own code on the way
    must be in opencutter.gui. Otherwise, or when no test led to the import (another thread, a
    plugin), it is left to fail: No module named 'gi.repository'."""

    CONFTEST = os.path.realpath(__file__)
    TESTS = os.path.dirname(CONFTEST)
    PACKAGE = os.path.dirname(os.path.dirname(os.path.realpath(protocol.__file__)))  # opencutter
    GUI = os.path.join(PACKAGE, "gui")

    def find_spec(self, name, path=None, target=None):
        if not name.startswith("gi."):
            return None
        frame = sys._getframe(1)
        while frame is not None:
            file = frame.f_code.co_filename
            if not file.startswith("<"):                    # not <frozen importlib._bootstrap>
                file = os.path.realpath(file)
                if _inside(file, self.TESTS) and file != self.CONFTEST:
                    where = os.path.relpath(file, os.path.dirname(self.TESTS))
                    pytest.skip(f"{NO_PYGOBJECT} ({where}:{frame.f_lineno})",
                                allow_module_level=True)
                if _inside(file, self.PACKAGE) and not _inside(file, self.GUI):
                    return None                             # GTK outside the GUI: an error
            frame = frame.f_back
        return None


if not HAVE_PYGOBJECT:
    _gi = types.ModuleType("gi", "Stand-in for PyGObject, not installed (tests/conftest.py)")
    _gi.__path__ = []
    _gi.require_version = lambda namespace, version: None
    sys.modules["gi"] = _gi
    sys.meta_path.insert(0, _SkipWithoutPyGObject())


def pytest_collection_modifyitems(items):
    if HAVE_PYGOBJECT:
        return
    for item in items:
        if item.path.name in NEEDS_PYGOBJECT:
            item.add_marker(pytest.mark.skip(reason=NO_PYGOBJECT))


@pytest.fixture(autouse=True)
def outside_a_flatpak_sandbox(monkeypatch):
    monkeypatch.setattr(protocol, "FLATPAK_INFO", os.path.join(os.sep, "nonexistent", ".flatpak-info"))
