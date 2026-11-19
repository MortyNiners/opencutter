# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""hygiene-2: without a display the GUI window test modules are skipped, not crashed.

Gtk.init_check() can report success on a headless box while Gdk.Display.get_default() is None;
building a window then kills the interpreter (SIGSEGV), which also stops every module that
sorts after it. This runs the display modules in a child pytest with no display at all: no
DISPLAY / WAYLAND_DISPLAY / BROADWAY_DISPLAY / GDK_BACKEND, no session bus, and an empty
private XDG_RUNTIME_DIR, so the child cannot reach any display server or the real daemon.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DISPLAY_MODULES = ["tests/test_gui_window.py", "tests/test_gui_review_window.py"]
NO_DISPLAY = ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "BROADWAY_DISPLAY", "GDK_BACKEND",
              "DBUS_SESSION_BUS_ADDRESS", "OPENCUTTER_SOCKET")


def test_display_modules_skip_cleanly_without_a_display(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items() if k not in NO_DISPLAY}
    env.update(XDG_RUNTIME_DIR=str(runtime), NO_AT_BRIDGE="1", GTK_A11Y="none")
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs",
                           *DISPLAY_MODULES], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=120)
    out = done.stdout + done.stderr
    # module-level skips leave no test to run: pytest's "no tests collected" (5); a crash is < 0
    assert done.returncode in (0, 5), out
    assert f"{len(DISPLAY_MODULES)} skipped" in out and "GTK cannot open a display" in out, out
