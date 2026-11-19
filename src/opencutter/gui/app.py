# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Application entry point: ``opencutter`` with no arguments runs ``main()``.

asyncio runs on the GLib main loop (``gi.events.GLibEventLoopPolicy``), so the GUI awaits
``DaemonClient`` calls directly.

The app's own log: ``main()`` writes the 'opencutter' loggers (INFO and above) and every
uncaught exception (GTK callbacks, threads, asyncio tasks) to ``app.log`` in the state folder
($XDG_STATE_HOME/opencutter, next to the service's daemon.log), with daemon.log's rules: a
regular file only (never a link or a FIFO), mode 0600, and a log past 1 MiB is kept as
app.log.1 when the app starts. Messages still go to stderr as before.

Esc is not an application accelerator: the main window cancels a running job with its own
shortcut, after an open dialog or popover had its turn (window.MainWindow).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import warnings
from pathlib import Path

from gi.events import GLibEventLoopPolicy
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk

from opencutter import APP_ID
from opencutter.device.client import LOG_ROTATE_BYTES, DaemonClient, _open_log, state_dir
from opencutter.presets.model import PresetStore

from . import dialogs
from .settings import GuiSettings
from .window import CENTER_TO_PAGE_ACCEL, MainWindow

STYLE = Path(__file__).with_name("style.css")
# The app's own icons (hicolor tree: the app icon, its symbolic variant and the Align icon) are
# package data next to this file, so they are found wherever the package is installed (a
# checkout, pip, pipx, the Flatpak). The desktop gets the app icon from share/icons instead.
ICONS = Path(__file__).with_name("icons")

ACCELS = {
    "win.open": ["<Control>o"],
    "win.send": ["<Control>Return"],
    "win.fit": ["<Control>0"],
    "win.align::center-page": [CENTER_TO_PAGE_ACCEL],
    "win.zoom-in": ["<Control>plus", "<Control>equal", "<Control>KP_Add"],
    "win.zoom-out": ["<Control>minus", "<Control>KP_Subtract"],
    "win.shortcuts": ["<Control>question"],
    "app.preferences": ["<Control>comma"],
    "app.quit": ["<Control>q"],
}


APP_LOG = "app.log"
log = logging.getLogger("opencutter")


def app_log_path() -> str:
    """$XDG_STATE_HOME/opencutter/app.log: the app's own log, next to daemon.log."""
    return os.path.join(state_dir(), APP_LOG)


def _open_app_log():
    """app.log for appending (text), with daemon.log's rules (client._open_log: never a link,
    a FIFO or anything but a regular file; 0600); one past LOG_ROTATE_BYTES becomes app.log.1
    first."""
    path = app_log_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    stream = open(path, "a", encoding="utf-8", opener=_open_log)
    try:
        if os.fstat(stream.fileno()).st_size <= LOG_ROTATE_BYTES:
            return stream
        os.replace(path, path + ".1")
    except OSError as e:
        print(f"opencutter: could not rotate {path}: {e}", file=sys.stderr)
        return stream
    stream.close()
    return open(path, "a", encoding="utf-8", opener=_open_log)


def setup_logging() -> logging.Handler | None:
    """Write the 'opencutter' loggers (INFO and above) and every uncaught exception to app.log
    (stderr keeps WARNING and above, as before). Returns the file handler, or None when the log
    could not be opened (the app runs all the same)."""
    try:
        stream = _open_app_log()
    except OSError as e:
        print(f"opencutter: no app log ({e})", file=sys.stderr)
        return None
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    if not any(getattr(h, "_opencutter_stderr", False) for h in log.handlers):
        errors = logging.StreamHandler(sys.stderr)       # what reached stderr before
        errors.setLevel(logging.WARNING)
        errors._opencutter_stderr = True
        log.addHandler(errors)

    previous = sys.excepthook

    def excepthook(kind, value, tb):
        log.critical("uncaught exception", exc_info=(kind, value, tb))
        if previous not in (None, sys.__excepthook__):
            previous(kind, value, tb)

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            log.critical("uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    log.info("OpenCutter app started (pid %d)", os.getpid())
    return handler


def _asyncio_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """The asyncio loop's exception handler: the app log, then asyncio's own report."""
    error = context.get("exception")
    log.error("asyncio: %s", context.get("message") or "error",
              exc_info=(type(error), error, error.__traceback__) if error is not None else None)
    loop.default_exception_handler(context)


def load_style(display: Gdk.Display) -> Gtk.CssProvider:
    """Load style.css, letting its ``prefers-color-scheme`` media query follow the style."""
    provider = Gtk.CssProvider()
    Gtk.Settings.get_for_display(display).bind_property(
        "gtk-interface-color-scheme", provider, "prefers-color-scheme",
        GObject.BindingFlags.SYNC_CREATE)
    provider.load_from_path(str(STYLE))
    Gtk.StyleContext.add_provider_for_display(display, provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return provider


class OpenCutterApp(Adw.Application):
    """The libadwaita application: one main window, dark by default."""

    def __init__(self):
        """Register the application id and its actions."""
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        self._css: Gtk.CssProvider | None = None
        for name, callback in (("preferences", self._on_preferences), ("about", self._on_about),
                               ("quit", self._on_quit)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, accels)

    def do_startup(self) -> None:
        """Dark by default (following a light system style), load the CSS and icons."""
        Adw.Application.do_startup(self)
        self.get_style_manager().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        display = Gdk.Display.get_default()
        self._css = load_style(display)
        if ICONS.is_dir():
            Gtk.IconTheme.get_for_display(display).add_search_path(str(ICONS))
        Gtk.Window.set_default_icon_name(APP_ID)
        try:                    # errors of asyncio tasks go to the app log too
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)   # policies, as in main()
                loop = asyncio.get_event_loop_policy().get_event_loop()
            loop.set_exception_handler(_asyncio_exception)
        except Exception:  # noqa: BLE001 - the app runs all the same
            log.exception("could not set the asyncio exception handler")

    def do_activate(self) -> None:
        """Create (or raise) the main window; show the first run once."""
        if self.window is None:
            settings = GuiSettings.load()
            self.window = MainWindow(self, DaemonClient(), PresetStore.load(), settings)
            self.window.present()
            self.window.start()
            if not settings.first_run_done:
                self.window.show_first_run()
        else:
            self.window.present()

    def do_shutdown(self) -> None:
        """The app quits: end a flatpak-spawn helper that still waits for a starting service
        (``DaemonClient.end_helper``; in the Flatpak it would keep this sandbox running after
        the app). The service itself is left alone: it outlives the app by design."""
        end_helper = getattr(getattr(self.window, "client", None), "end_helper", None)
        if end_helper is not None:
            end_helper()
        Adw.Application.do_shutdown(self)

    def _on_preferences(self, *_args) -> None:
        win = self.window
        prefs = dialogs.PreferencesDialog(win.settings, win.store, win.monitor.state)
        prefs.connect("changed", lambda *_: win.apply_settings())
        prefs.present(win)

    def _on_about(self, *_args) -> None:
        dialogs.show_about(self.window)

    def _on_quit(self, *_args) -> None:
        if self.window is not None:
            self.window.close()            # asks first while a job runs


def main(argv: list[str] | None = None) -> int:
    """Run the GUI and return its exit status."""
    # The program name is the window's X11 WM_CLASS: make it the desktop file's id however the
    # app was started (`opencutter`, `python -m opencutter`), so desktops match the window to it.
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("OpenCutter")
    Gtk.init_check()           # may report success without a display: check the display itself
    if Gdk.Display.get_default() is None:
        print("opencutter: cannot open a display; the app needs a graphical session "
              "(the command-line tools work without one: opencutter --help)", file=sys.stderr)
        return 1
    setup_logging()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)   # policies are deprecated in 3.14
        asyncio.set_event_loop_policy(GLibEventLoopPolicy())
    app = OpenCutterApp()
    return app.run(sys.argv[:1] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
