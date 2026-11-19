# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The main window: header bar, mat canvas, sidebar (Page Setup / Send or the job), status bar.

It owns the flow that can move the machine. Nothing moves without an explicit click on Send
(after the Send sheet), Test Cut, or Cut at the outline gate; Cancel Job is one click.

Machine profiles: the layout is made for one machine (``layout``, sending.LayoutMachine, kept in
step with GuiSettings.machine and MachineMonitor.set_choice) and one of its mats (Page Setup).
Every JobSpec names that machine and mat. With no machine connected, Page Setup chooses the
machine. The detected machine always wins: when the service reports another one, the layout
switches to it (its mat, the media clamped to it, the design where it was) and a banner offers
"Use {name}"; Send and Test Cut stay blocked until it is pressed, also inside the Send sheet.
The texts, the Tool cards, the status bar and the connection popover follow the machine.

Stopping and the next job (safety):
- Cancel Job's stop may answer late: until it does, Send, Test Cut, Open and the finished page's
  buttons wait, closing the window waits for the reply (and keeps the window with the
  power-button alert when the stop failed), and a failed stop is always shown (a toast that stays
  and the job view), whenever its reply comes.
- Once a run has ended, Stop Machine (on the finished page, and in the header after anything
  but a completed run; on the toast of a completed Test Cut, which returns to the panels at
  once) sends the service's stop op without a session: the stop and nothing else.
- After a run that went to the machine, or after the link to the machine dropped, the next Send,
  Cut Again, Send Again… or Test Cut first shows the Load sheet, which waits until the machine
  reported the mat unloaded and then Ready: the machine applies the mat setting only right after
  a load, and the next job's header moves the carriage before its unit check.
- A stop that the service could not send and owes the machine is shown, and so is the moment it
  was sent on a new link. A stop from another client at the outline gate ends the run there.
- Esc cancels a running job through the window's own shortcut (bubble phase): a dialog or a
  popover that is open takes Esc first (the "A job is running" dialog then keeps the job).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import replace

from gi.repository import Adw, GLib, GObject, Gdk, Gio, Gsk, Gtk, Pango

from opencutter import APP_ID
from opencutter.device.protocol import INTERRUPTED_TERMS
from opencutter.machines import MachineProfile
from opencutter.presets.model import PresetStore
from opencutter.svg.importer import SvgImportError, load_svg

from . import dialogs
from .canvas import MatCanvas, Scene, line_style, palette
from .connection import (ConnectionPopover, StatusPill, failure_text, failure_title,
                         refused_name, shown_problem, state_hint)
from .document import PlacedDocument
from .firstrun import FirstRunDialog
from .geometry import (ALIGNMENTS, UNCHECKED_MAT, align_rect, design_fits, format_duration,
                       format_length, mat_choices, media_covers_cut_area, placement_problem,
                       test_cut_spot, usable_area)
from .jobview import STOP_MACHINE, JobInfo, JobView
from .machine import MACHINE_NAME, Link, MachineMonitor, MachineState, chosen_profile, pill
from .runner import JobRunner, Outcome, StepState, activity, cut_steps, estimated_seconds
from .sending import (LayoutMachine, blocker_for_test_cut, depth_note, job_spec, plan_summary,
                      send_blocker, spec_for_test_cut, supported_presets, tool_check)
from .settings import GuiSettings
from .sheets import (RELOAD_AFTER_DONE, RELOAD_AFTER_DROP, RELOAD_AFTER_STOP, LoadMediaDialog,
                     SendDialog, SendSummary, mat_loaded_text)
from .sidebar import PageSetupPanel, SendPanel, Sidebar
from .tasks import spawn
from .widgets import LengthEntry, Painting, StatusDot, icon_label, label, relate

log = logging.getLogger(__name__)

ACTIVITY = {"Sketch": "Sketching", "Score": "Scoring"}
SENDING = "A send is already in progress."
TRY_AGAIN = "Try again"
ALIGN_ICON = f"{APP_ID}-align-symbolic"      # gui/icons/hicolor/symbolic/apps (app.ICONS)
CENTER_TO_PAGE_ACCEL = "<Control><Shift>c"   # app.ACCELS["win.align::center-page"]
CONFIRM_ROUNDS = 3          # the Send sheet is shown again at most this often when the job changed
# while a job or Test Cut runs (MainWindow._inhibit_session); GNOME shows the reason when the
# user logs out or shuts down meanwhile
JOB_INHIBIT_FLAGS = Gtk.ApplicationInhibitFlags.LOGOUT | Gtk.ApplicationInhibitFlags.SUSPEND


def job_inhibit_reason(name: str = MACHINE_NAME) -> str:
    """Why the desktop must not log out or suspend while a job runs on the machine ``name``."""
    return f"A job is running on the {name}"


JOB_INHIBIT_REASON = job_inhibit_reason()
# the layout switched to the detected machine, or Send waits for "Use {name}" (whatever it
# was for): the Send sheet and a Test Cut stop short of the machine
MACHINE_CHANGED = ("The connected machine changed", "Nothing was sent. Check the layout for "
                   "the connected machine, then send again.")
# the area banner: the design leaves the cut area, or only the media (geometry.placement_problem)
OUTSIDE_CUT_AREA = "Part of the design is outside the cut area."
DESIGN_PLACED = "Design on the media and inside the cut area"     # the Send sheet's checklist
OFF_MEDIA = "Part of the design is off the media ({media})."
# Media Size covers the whole cut area of the mat: a note on the Send sheet (blocks nothing)
MEDIA_IS_MAT = ("Media Size is the whole mat ({media}), so OpenCutter cannot keep the blade on a "
                "smaller sheet. If your sheet is smaller, set Media Size in Page Setup.")
STOP_FAILED_TOAST = "<b>The stop could not be sent.</b> Press the machine's power button."
STOP_SENT = "Stop sent. If the machine keeps moving, press its power button."
STOP_UNAVAILABLE = "Not connected: press the machine's power button."
WAITING_FOR_STOP = "Waiting for the machine to confirm the stop…"
OWED_STOP = ("A stop could not get through to the {name}. OpenCutter sends it as soon as the "
             "{name} is connected again. If it moves, press its power button.")
OWED_STOP_SENT = ("The stop that could not get through was sent to the {name} when it connected "
                  "again.")
STILL_BUSY_SECONDS = 4.0    # Busy for this long after a run: the finished page says so
TEST_TOAST_SECONDS = 15     # the completed Test Cut's toast (with Stop Machine) stays this long
STOP_WAIT = 25.0            # longest wait for Cancel's stop reply (> DaemonClient's STOP_TIMEOUT)
QUIT_WAIT = 5.0             # after Cancel Job and quit: how long the run may take to end
JOB_LOST_HEADING = "Connection lost during the job"
CUT_LOST_HEADING = "Connection lost while cutting"
LOST_BODY = ("OpenCutter cannot stop the machine without the connection. If it is still moving, "
             "press its power button. Then switch it on again to reconnect.")
LOST_BACK_BODY = ("The connection came back, but this job has ended and does not go on. If the "
                  "machine moves, press Stop Machine or its power button.")


class MainWindow(Adw.ApplicationWindow):
    """The OpenCutter main window."""

    __gtype_name__ = "OpenCutterMainWindow"

    def __init__(self, application: Adw.Application | None, client, store: PresetStore,
                 settings: GuiSettings):
        """``client`` is a ``DaemonClient``; ``store`` holds the presets the app offers."""
        super().__init__(application=application, title="OpenCutter",
                         default_width=1280, default_height=720)
        self.set_size_request(760, 560)
        self.client, self.settings = client, settings
        self.presets = store                    # every row; ``store`` is the machine's view
        self.layout = LayoutMachine(settings.machine)
        self.store = supported_presets(store, self.layout.profile)
        self.monitor = MachineMonitor(client)
        self.monitor.set_choice(self.layout.machine)
        self.monitor.add_listener(self._on_machine)
        self._switching = False                 # the layout is switching machines
        self.doc: PlacedDocument | None = None
        self.runner: JobRunner | None = None
        self._monitor_task: asyncio.Task | None = None
        self._live: set = set()                 # open sheets that follow the machine state
        self._closing = False
        self._closed = False                    # the window was closed: start nothing more
        self._sending = False                   # a send_flow is between its click and run_job
        self._disconnecting = False             # Disconnect waits for the service to end
        self._quit_confirmed = False            # "Cancel Job and quit" was answered, the stop sent
        self._stop_alerted = False              # this runner's failed stop was shown already
        self._link_dropped_in_job = False       # the service lost the link during this run
        self._machine_stopping = False          # Stop Machine waits for the service's reply
        # the next job first needs Unload and Load (a run went to the machine, or the link
        # dropped): cleared once the machine reported the mat unloaded (its load status)
        self._reload_needed = False
        self._reload_text = RELOAD_AFTER_STOP   # why: the Load sheet's RELOAD_AFTER_* text
        self._reload_base: int | None = None    # the service's load_seen when it was set
        self._reload_runner: JobRunner | None = None   # the run that set it last
        self._was_connected = False
        self._busy_since: float | None = None   # the machine reports Busy after a run, since
        self._owed_alerted = False              # the owed stop was told
        self._owed_sent_seen: int | None = None   # the service's owed_stops_sent, last seen
        self._build()
        self._install_actions()
        self.connect("close-request", self._on_close_request)
        self._on_page_setup()
        self._on_machine(self.monitor.state)

    # --- building --------------------------------------------------------------------
    def _build(self) -> None:
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.RAISED_BORDER,
                               bottom_bar_style=Adw.ToolbarStyle.RAISED_BORDER)
        view.add_top_bar(self._build_header())
        self.banner = Adw.Banner(title=failure_text(None), button_label=TRY_AGAIN, use_markup=False)
        self.banner.add_css_class("error")
        self.banner.connect("button-clicked", lambda *_: spawn(self.monitor.connect()))
        view.add_top_bar(self.banner)
        # the layout switched to the detected machine: Send waits for "Use {name}"
        self.machine_banner = Adw.Banner(use_markup=False)
        self.machine_banner.connect("button-clicked", lambda *_: self.use_machine())
        view.add_top_bar(self.machine_banner)

        profile = self.layout.profile
        self.page_setup = PageSetupPanel(self.settings, profile)
        self.page_setup.connect("changed", lambda *_: self._on_page_setup())
        self.page_setup.connect("machine-chosen", lambda _p, machine: self.choose_machine(machine))
        self.send_panel = SendPanel(self.store, self.settings, profile)
        self.send_panel.connect("changed", lambda *_: self._on_send_settings())
        self.send_panel.connect("send", lambda *_: self.activate_action("win.send"))
        self.send_panel.connect("test-cut", lambda *_: self.activate_action("win.test-cut"))
        self.sidebar = Sidebar(self.page_setup, self.send_panel)
        self.sidebar.connect("tab-changed", lambda *_: self._on_tab())
        self.job_view = JobView()
        self.job_view.connect("cancel", lambda *_: self.cancel_job())
        self.job_view.connect("done", lambda *_: self._leave_job())
        self.job_view.connect("cut-again", lambda *_: self._cut_again())
        self.job_view.connect("tick", lambda *_: self._update_pill())    # the pill's % every second
        self.side_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.side_stack.add_css_class("sidebar-pane")
        self.side_stack.add_named(self.sidebar, "panels")
        self.side_stack.add_named(self.job_view, "job")

        self.split = Adw.OverlaySplitView(sidebar_position=Gtk.PackType.END, min_sidebar_width=340,
                                          max_sidebar_width=400, sidebar_width_fraction=0.3,
                                          content=self._build_main(), sidebar=self.side_stack)
        self.split.bind_property("show-sidebar", self.sidebar_toggle, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.split.connect("notify::show-sidebar", lambda *_: self._on_show_sidebar())
        view.set_content(self.split)
        view.add_bottom_bar(self._build_statusbar())
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 980sp"))
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self.sidebar_toggle, "visible", True)
        self.add_breakpoint(narrow)

    def _build_header(self) -> Gtk.Widget:
        header = Adw.HeaderBar(show_title=False)
        menu = Gio.Menu()
        files = Gio.Menu()
        files.append("Open…", "win.open")
        menu.append_section(None, files)
        app_items = Gio.Menu()
        app_items.append("Preferences", "app.preferences")
        app_items.append("Keyboard Shortcuts", "win.shortcuts")
        app_items.append("About OpenCutter", "app.about")
        menu.append_section(None, app_items)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                     tooltip_text="Main menu", primary=True)
        header.pack_start(menu_button)
        open_button = Gtk.Button(child=icon_label("document-open-symbolic", "Open"),
                                 action_name="win.open", tooltip_text="Open an SVG design (Ctrl+O)")
        open_button.add_css_class("pill-sm")
        header.pack_start(open_button)
        title = Gtk.Box(spacing=8, margin_start=8)
        self.doc_label = label("No design open", "doc-name", "dim-label")
        # shrinks first when the header is short of room (a long file name, "Could not Connect")
        self.doc_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.modified_dot = StatusDot("neutral")
        self.modified_dot.set_tooltip_text("Moved since opening")
        self.modified_dot.set_visible(False)
        title.append(self.doc_label)
        title.append(self.modified_dot)
        header.pack_start(title)

        self.sidebar_toggle = Gtk.ToggleButton(icon_name="sidebar-show-right-symbolic",
                                               tooltip_text="Show sidebar", visible=False)
        header.pack_end(self.sidebar_toggle)
        self.header_send = Gtk.Button(child=icon_label("media-playback-start-symbolic", "Send"),
                                      action_name="win.send")
        self.header_send.add_css_class("pill-sm")
        self.header_send.update_property([Gtk.AccessibleProperty.LABEL], ["Send"])
        header.pack_end(self.header_send)
        # in the Send button's place while a job runs: Cancel Job stays on screen even when
        # the sidebar (with the big Cancel Job) is collapsed away
        self.header_cancel = Gtk.Button(child=icon_label("media-playback-stop-symbolic", "Cancel Job"),
                                        action_name="win.cancel-job", tooltip_text="Cancel Job (Esc)",
                                        visible=False)
        self.header_cancel.add_css_class("pill-sm")
        self.header_cancel.add_css_class("destructive-action")
        self.header_cancel.update_property([Gtk.AccessibleProperty.LABEL], ["Cancel Job"])
        self.header_cancel.update_property([Gtk.AccessibleProperty.KEY_SHORTCUTS], ["Escape"])
        header.pack_end(self.header_cancel)
        # once a run has ended (but a completed one): Stop Machine stays on screen even when
        # the sidebar with the job view is collapsed away
        self.header_stop = Gtk.Button(child=icon_label("media-playback-stop-symbolic", STOP_MACHINE),
                                      action_name="win.stop-machine", visible=False)
        self.header_stop.add_css_class("pill-sm")
        self.header_stop.add_css_class("destructive-action")
        self.header_stop.update_property([Gtk.AccessibleProperty.LABEL], [STOP_MACHINE])
        header.pack_end(self.header_stop)
        self.connection = ConnectionPopover()
        self.connection.connect("connect", lambda *_: spawn(self.monitor.connect()))
        self.connection.connect("disconnect", lambda *_: spawn(self.disconnect_machine()))
        self.pill = StatusPill(self.connection)
        header.pack_end(self.pill)
        return header

    def _build_main(self) -> Gtk.Widget:
        self.scene = Scene(media=self.page_setup.media_rect(), reveal_mat=self.settings.reveal_mat,
                           mat=self.page_setup.mat, profile=self.layout.profile)
        self.canvas = MatCanvas(self.scene)
        self.canvas.connect("placement-changed", lambda *_: self._sync_placement())
        self.canvas.connect("cursor-moved", self._on_cursor)
        self.canvas.connect("zoom-changed", lambda *_: self.zoom_label.set_text(
            f"{round(self.canvas.zoom * 100)}%"))

        design = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        design.append(self._build_placement_bar())
        self.area_banner = Adw.Banner(title=OUTSIDE_CUT_AREA, button_label="Fit to Mat")
        self.area_banner.add_css_class("warning")
        self.area_banner.connect("button-clicked", lambda *_: self.fit_design())
        design.append(self.area_banner)
        design.append(self.canvas)
        design.append(self._build_canvas_footer())

        self.main_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.main_stack.add_named(self._build_empty(), "empty")
        self.main_stack.add_named(design, "design")
        self.main_stack.add_css_class("main-area")
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        drop.connect("enter", self._on_drop_enter)
        drop.connect("leave", lambda *_: self.main_stack.remove_css_class("drop-hover"))
        self.main_stack.add_controller(drop)
        return self.main_stack

    def _build_empty(self) -> Gtk.Widget:
        zone = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, halign=Gtk.Align.CENTER,
                       valign=Gtk.Align.CENTER)
        zone.add_css_class("drop-zone")
        zone.set_size_request(520, 340)
        circle = Gtk.Box(halign=Gtk.Align.CENTER)
        circle.add_css_class("drop-icon")
        circle.append(Gtk.Image(icon_name="document-send-symbolic", pixel_size=32))
        zone.append(circle)
        zone.append(label("Open an SVG design", "title-1", xalign=0.5))
        zone.append(label("Drag a file here, or choose one from your computer.", "dim-label", xalign=0.5))
        button = Gtk.Button(label="Open design…", action_name="win.open", halign=Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.add_css_class("pill-md")
        zone.append(button)
        zone.append(label("Design in Inkscape, then open the SVG here.", "caption", "dim-label",
                          xalign=0.5))
        page = Gtk.Box(hexpand=True, vexpand=True)
        page.add_css_class("empty-page")
        zone.set_hexpand(True)
        page.append(zone)
        # scrolls rather than setting the window's minimum height (the Could not Connect banner
        # takes up to three lines at the minimum width)
        return Gtk.ScrolledWindow(child=page, hscrollbar_policy=Gtk.PolicyType.NEVER)

    def _build_placement_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=12)
        bar.add_css_class("canvas-bar")
        bar.add_css_class("top")
        self.entries: dict[str, LengthEntry] = {}
        for name in ("X", "Y", "W", "H"):
            entry = LengthEntry(name, self.settings.units, positive=name in "WH")
            entry.connect("committed", self._on_entry, name)
            pair = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
            pair.append(label(name, "dim-label", "caption"))
            pair.append(entry)
            bar.append(pair)
            self.entries[name] = entry
        self.unit_label = label(self.settings.units, "dim-label", "caption")
        bar.append(self.unit_label)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        rotate = Gtk.Button(icon_name="object-rotate-right-symbolic", action_name="win.rotate",
                            tooltip_text="Rotate 90°", valign=Gtk.Align.CENTER)
        rotate.add_css_class("flat")
        bar.append(rotate)
        bar.append(self._build_align_button())
        self.placement_info = label("", "dim-label", "caption", xalign=1.0)
        self.placement_info.set_hexpand(True)
        self.placement_info.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(self.placement_info)
        self.placement_bar = bar
        return bar

    def _build_align_button(self) -> Gtk.Widget:
        """'Align': Center to Page, then the horizontal and the vertical alignments."""
        menu = Gio.Menu()
        for group in (ALIGNMENTS[:1], ALIGNMENTS[1:4], ALIGNMENTS[4:]):
            section = Gio.Menu()
            for key, title in group:
                section.append(title, f"win.align::{key}")
            menu.append_section(None, section)
        self.align_button = Gtk.MenuButton(icon_name=ALIGN_ICON, menu_model=menu, tooltip_text="Align",
                                           always_show_arrow=True, valign=Gtk.Align.CENTER)
        self.align_button.add_css_class("flat")
        self.align_button.update_property([Gtk.AccessibleProperty.LABEL], ["Align"])
        return self.align_button

    def _build_canvas_footer(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=8)
        bar.add_css_class("canvas-bar")
        bar.add_css_class("bottom")
        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic", action_name="win.zoom-out",
                              tooltip_text="Zoom out")
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic", action_name="win.zoom-in",
                             tooltip_text="Zoom in")
        self.zoom_label = label("100%", "numeric", "caption", xalign=0.5)
        self.zoom_label.set_width_chars(5)
        fit = Gtk.Button(label="Fit", action_name="win.fit", tooltip_text="Fit (Ctrl+0)")
        for widget in (zoom_out, self.zoom_label, zoom_in, fit):
            if isinstance(widget, Gtk.Button):
                widget.add_css_class("flat")
            bar.append(widget)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        reveal = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.page_setup.reveal_switch.bind_property(
            "active", reveal, "active", GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        reveal_label = label("Reveal Cutting Mat", "caption")
        relate(reveal, Gtk.AccessibleRelation.LABELLED_BY, reveal_label)
        bar.append(reveal)
        bar.append(reveal_label)
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        for action in ("Cut", "Score", "Sketch"):
            bar.append(_legend(action))
        return bar

    def _build_statusbar(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=12)
        bar.add_css_class("statusbar")
        self.status_label = label("", "caption")
        bar.append(self.status_label)
        self._tool_bar = Gtk.Box(spacing=12)     # '· Tool 1: … · Tool 2: …', one per holder
        self.tool_labels: dict[int, Gtk.Label] = {}
        self._show_tool_labels(self.monitor.state)
        bar.append(self._tool_bar)
        self.cursor_label = label("x — · y —", "caption", "numeric", "dim-label", xalign=1.0)
        self.cursor_label.set_hexpand(True)
        bar.append(self.cursor_label)
        return bar

    def _show_tool_labels(self, state: MachineState) -> None:
        """One status bar label per holder of the machine: 'Tool 1: AutoBlade (Detected)'."""
        holders = [h.number for h in state.holders]
        if list(self.tool_labels) != holders:
            while (child := self._tool_bar.get_first_child()) is not None:
                self._tool_bar.remove(child)
            self.tool_labels = {}
            for holder in holders:
                self._tool_bar.append(label("·", "dim-label"))
                self.tool_labels[holder] = label("", "caption")
                self._tool_bar.append(self.tool_labels[holder])
        for holder, widget in self.tool_labels.items():
            widget.set_text(f"{state.holder_label(holder)}: {state.tool_text(holder) or '—'}")

    def _install_actions(self) -> None:
        actions = {
            "open": lambda *_: spawn(self.choose_file()),
            "send": lambda *_: spawn(self.send_flow()),
            "test-cut": lambda *_: spawn(self.send_flow(test_cut=True)),
            "cancel-job": lambda *_: self.cancel_job(),
            "stop-machine": lambda *_: spawn(self.stop_machine()),
            "fit": lambda *_: self.canvas.fit(),
            "zoom-in": lambda *_: self.canvas.zoom_by(1.25),
            "zoom-out": lambda *_: self.canvas.zoom_by(0.8),
            "rotate": lambda *_: self._rotate(),
            "shortcuts": lambda *_: _shortcuts_dialog().present(self),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        align = Gio.SimpleAction.new("align", GLib.VariantType.new("s"))   # win.align::center-page
        align.connect("activate", lambda _a, how: self.align_design(how.get_string()))
        self.add_action(align)
        self._enable("stop-machine", False)
        # Esc cancels a running job: the window's own shortcut in the bubble phase, so that an
        # open dialog or popover takes Esc first (an application accelerator would run before
        # them, and cancel the job behind "A job is running" whose Esc means Keep cutting)
        keys = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.BUBBLE,
                                      scope=Gtk.ShortcutScope.LOCAL)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                       action=Gtk.NamedAction.new("win.cancel-job")))
        self.add_controller(keys)

    def _enable(self, name: str, enabled: bool) -> None:
        action = self.lookup_action(name)
        if action is not None:            # None once the window is being destroyed
            action.set_enabled(enabled)

    # --- startup ---------------------------------------------------------------------
    def start(self) -> None:
        """Start the daemon if needed and begin polling the machine."""
        if self._monitor_task is None:
            self._monitor_task = spawn(self.monitor.run())

    def show_first_run(self) -> None:
        """Welcome → Connect; marks the first run as done when closed. Which machine the user
        has is not known yet: the dialog says "your machine" until one is detected."""
        dialog = FirstRunDialog()
        self._attach_live(dialog, dialog.update_machine)
        dialog.connect("connect", lambda *_: spawn(self.monitor.connect()))
        dialog.connect("closed", lambda *_: self._first_run_done())
        dialog.present(self)

    def _first_run_done(self) -> None:
        self.settings.first_run_done = True
        self.settings.save()

    def _attach_live(self, dialog: Adw.Dialog, update) -> None:
        """Feed ``dialog`` every machine state until it closes."""
        self._live.add(update)
        update(self.monitor.state)
        dialog.connect("closed", lambda *_: self._live.discard(update))

    # --- files -----------------------------------------------------------------------
    async def choose_file(self) -> None:
        """Ask for an SVG file and open it."""
        if self._busy():
            return
        dialog = Gtk.FileDialog(title="Open an SVG design", modal=True)
        svg = Gtk.FileFilter(name="SVG designs")
        svg.add_mime_type("image/svg+xml")
        svg.add_suffix("svg")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(svg)
        dialog.set_filters(filters)
        try:
            file = await dialog.open(self)
        except GLib.Error:
            return
        if file is not None and file.get_path():
            await self.open_path(file.get_path())

    async def open_path(self, path: str) -> bool:
        """Load an SVG and place it on the media; returns False (with a message) on failure."""
        try:
            design = await asyncio.to_thread(load_svg, path)
        except SvgImportError as e:
            dialogs.show_message(self, "Could not open the design", str(e))
            return False
        except OSError as e:
            dialogs.show_message(self, "Could not open the design", e.strerror or str(e))
            return False
        if self._busy():                  # a send started while the file was loading
            self.toast("The design was not opened: a job is being sent.")
            return False
        self.doc = PlacedDocument(design, self.page_setup.media_rect())
        self.scene.doc = self.doc
        self.doc_label.set_text(self.doc.source_name)
        self.doc_label.set_tooltip_text(self.doc.source_name)
        self.doc_label.remove_css_class("dim-label")
        self.set_title(f"{self.doc.source_name} — OpenCutter")
        self.main_stack.set_visible_child_name("design")
        self.canvas.fit()
        self._sync_placement()
        if self.doc.warnings:
            toast = Adw.Toast(title=GLib.markup_escape_text(" ".join(self.doc.warnings)),
                              button_label="Details", timeout=8)
            toast.connect("button-clicked", lambda *_: dialogs.show_message(
                self, "Some objects were ignored", "\n".join(self.doc.warnings)))
            self.toasts.add_toast(toast)
        return True

    def _on_drop_enter(self, *_args) -> Gdk.DragAction:
        self.main_stack.add_css_class("drop-hover")
        return Gdk.DragAction.COPY

    def _on_drop(self, _target, value, _x, _y) -> bool:
        self.main_stack.remove_css_class("drop-hover")
        if not isinstance(value, Gio.File) or not value.get_path() or self._busy():
            return False
        spawn(self.open_path(value.get_path()))
        return True

    # --- placement -------------------------------------------------------------------
    def _on_entry(self, _entry, mm: float, name: str) -> None:
        doc = self.doc
        if doc is None or self._job_running():
            return
        if name == "X":
            doc.move_to(mm, doc.y)
        elif name == "Y":
            doc.move_to(doc.x, mm)
        elif name == "W":
            doc.set_width(mm)
        else:
            doc.set_height(mm)
        self.canvas.queue_draw()
        self._sync_placement()

    def _rotate(self) -> None:
        if self.doc is not None and not self._job_running():
            self.doc.rotate_quarter()
            self.canvas.queue_draw()
            self._sync_placement()

    def align_design(self, how: str) -> None:
        """Align the design like typing X/Y: centred on the media (Center to Page, Align
        Center, Align Middle) or against an edge of the usable area (the media inside the cut
        area), and always inside the usable area when it fits there.

        ``how`` is a key of ``geometry.ALIGNMENTS``, e.g. 'center-page'. A size or a custom
        media size that was typed but not confirmed yet counts, as it would for Send."""
        if self.doc is None or self._job_running() or how not in dict(ALIGNMENTS):
            return
        for entry in self.entries.values():
            entry.commit()
        self.page_setup.commit_pending()
        doc = self.doc
        media = self.page_setup.media_rect()
        x, y = align_rect(doc.bbox(), usable_area(media, *self._mat()), how, page=media)
        doc.move_to(x, y)
        self.canvas.queue_draw()
        self._sync_placement()

    def fit_design(self) -> None:
        """'Fit to Mat' (or 'Fit to Media'): shrink if needed and move the design inside the
        usable area (the media inside the cut area)."""
        if self.doc is not None and not self._job_running():
            self.doc.fit_into(usable_area(self.page_setup.media_rect(), *self._mat()))
            self.canvas.queue_draw()
            self._sync_placement()

    def _sync_placement(self) -> None:
        doc = self.doc
        locked = self._job_running()
        self.placement_bar.set_sensitive(doc is not None and not locked)
        self._enable("rotate", doc is not None and not locked)
        self._enable("align", doc is not None and not locked)
        if doc is not None:
            x, y, w, h = doc.bbox()
            for name, value in zip("XYWH", (x, y, w, h)):
                self.entries[name].set_mm(value)
            self.modified_dot.set_visible(doc.modified)
            problem = placement_problem(doc.bbox(), self.page_setup.media_rect(), *self._mat())
            if problem == "media":       # inside the cut area, but off the media
                self.area_banner.set_title(GLib.markup_escape_text(
                    OFF_MEDIA.format(media=self.page_setup.media_label())))
                self.area_banner.set_button_label("Fit to Media")
            else:
                self.area_banner.set_title(OUTSIDE_CUT_AREA)
                self.area_banner.set_button_label("Fit to Mat")
            self.area_banner.set_revealed(problem is not None and not locked)
            paths = f"{doc.path_count} path{'s' if doc.path_count != 1 else ''}"
            panel = self.send_panel
            what = panel.action or ("No material" if panel.material is None else "No action")
            self.placement_info.set_text("Placement is locked while the machine is moving" if locked
                                         else f"{paths} · {what}")
        self._update_send_state()
        self._update_live()               # e.g. the Send sheet's DESIGN_PLACED row

    def _on_cursor(self, _canvas, x: float, y: float) -> None:
        units = self.settings.units
        if math.isnan(x):
            self.cursor_label.set_text("x — · y —")
        else:
            self.cursor_label.set_text(f"x {format_length(x, units)} {units} · "
                                       f"y {format_length(y, units)} {units}")

    # --- panels ----------------------------------------------------------------------
    def _mat(self) -> tuple[str, MachineProfile]:
        """(mat key, profile) of the layout: what the cut area and the media depend on."""
        return self.page_setup.mat_key, self.layout.profile

    def _on_page_setup(self) -> None:
        self.scene.media = self.page_setup.media_rect()
        self.scene.media_label = self.page_setup.media_label()
        mat, profile = self.page_setup.mat, self.layout.profile
        refit = (mat, profile) != (self.scene.mat, self.scene.profile)
        self.scene.mat, self.scene.profile = mat, profile
        reveal = self.page_setup.reveal_switch.get_active()
        if reveal != self.scene.reveal_mat:
            self.scene.reveal_mat = reveal
            refit = True
        if refit:
            self.canvas.fit()
        self._on_tab()

    def _on_show_sidebar(self) -> None:
        if self._job_running() and not self.split.get_show_sidebar():
            # the big Cancel Job lives in the sidebar: it stays shown while the job runs
            self.split.set_show_sidebar(True)
            return
        self._on_tab()

    def _on_tab(self) -> None:
        on_send = self.sidebar.tab == "send"
        self.scene.test_spot = test_cut_spot(self.scene.media, *self._mat()) if on_send else None
        # one cyan action per view: the panel's big Send when the Send tab shows
        if on_send and self.split.get_show_sidebar():
            self.header_send.remove_css_class("suggested-action")
        else:
            self.header_send.add_css_class("suggested-action")
        self.send_panel.set_send_primary(on_send)
        self.canvas.queue_draw()
        self._sync_placement()

    def _on_send_settings(self) -> None:
        self.scene.action = self.send_panel.action or "Cut"
        self.canvas.queue_draw()
        self._sync_placement()

    def apply_settings(self) -> None:
        """Re-read units and defaults after Preferences changed them."""
        units = self.settings.units
        for entry in self.entries.values():
            entry.units = units
        self.unit_label.set_text(units)
        self.page_setup.set_units(units)
        self.page_setup.select_mat(self.settings.mat_for(self.layout.profile))
        self.page_setup.select_media(self.settings.media)
        self.send_panel.preview.set_active(self.settings.preview_outline)
        self._on_page_setup()

    # --- machine ---------------------------------------------------------------------
    def _on_machine(self, state: MachineState) -> None:
        if self._switching:                  # set_choice while the layout switches: done after
            return
        self._follow_safety(state)
        if self.layout.follow(state):        # another machine was detected: it always wins
            self._switch_layout()
        if state.choice != self.layout.machine:      # the texts name the layout's machine
            state = replace(state, choice=self.layout.machine)
        self.connection.update(state)
        # why, when the daemon said so (plain text: use_markup is off, the model is the
        # machine's own text). A machine of another model gets no Try again: it would never
        # help, and the service connects to a machine it drives by itself once one is on
        self.banner.set_title(failure_text(shown_problem(state), failure_title(state),
                                           state_hint(state)))
        self.banner.set_button_label(None if state.wrong_model else TRY_AGAIN)
        self.banner.set_revealed(state.link is Link.FAILED and not self._job_running())
        self.page_setup.set_machine(state)
        self.send_panel.set_machine(state)
        self._show_tool_labels(state)
        self._update_live()
        self._update_pill()
        self._update_send_state()

    def _follow_safety(self, state: MachineState) -> None:
        """What the machine state means for stopping and for the next job: a link that dropped
        (during a run, and for the next job's Load sheet), the mat unloaded since the last run,
        a stop from another client at the outline gate, a stop the service owes the machine,
        and a machine that keeps reporting Busy after a run."""
        dropped = (self._was_connected and not state.connected
                   and state.link in (Link.FAILED, Link.CONNECTING))
        self._was_connected = state.connected
        if dropped:
            self._need_reload(state, RELOAD_AFTER_DROP)
            if self._job_running():
                self._link_dropped_in_job = True
                self.job_view.show_link_lost()
        self._check_reload(state)
        runner = self.runner
        if (runner is not None and runner.outcome is Outcome.RUNNING and runner.holding
                and state.connected and state.motion_stopped):
            runner.stopped_elsewhere()            # `opencutter stop` at the outline gate
        self._follow_owed_stop(state)
        self._watch_busy(state)
        self.job_view.set_machine(state.name, state.connected)

    def _need_reload(self, state: MachineState, why: str) -> None:
        """The next job first needs Unload and Load (see _check_reload); `why` is the Load
        sheet's instruction (sheets.RELOAD_AFTER_DONE, RELOAD_AFTER_STOP or RELOAD_AFTER_DROP)."""
        self._reload_needed = True
        self._reload_text = why
        self._reload_base = state.load_seen if state.connected else None

    def _check_reload(self, state: MachineState) -> None:
        """The mat was unloaded since the reload became needed: the machine reports its load
        status now, or the service counted a change to it since (load_seen: an Unload and Load
        between two polls). A quick Unload and Load that neither saw only asks again."""
        if not self._reload_needed or not state.connected:
            return
        load = state.machine_profile.status.load
        seen = state.load_seen
        if self._reload_base is None:
            self._reload_base = seen
        elif seen is not None and seen > self._reload_base:
            self._reload_needed = False
        if state.status == load:
            self._reload_needed = False

    def _follow_owed_stop(self, state: MachineState) -> None:
        """Tell the user when the service owes the machine a stop (it could not get through),
        and when it sent such a stop on a new link."""
        name = state.name
        if state.stop_owed and not self._owed_alerted:
            self._owed_alerted = True
            self._toast_stays(GLib.markup_escape_text(OWED_STOP.format(name=name)))
        elif not state.stop_owed:
            self._owed_alerted = False
        sent = state.owed_stops_sent
        if sent is not None:
            if self._owed_sent_seen is not None and sent > self._owed_sent_seen:
                self.toast(GLib.markup_escape_text(OWED_STOP_SENT.format(name=name)))
            self._owed_sent_seen = sent
        self.job_view.show_stop_owed(state.stop_owed)

    def _watch_busy(self, state: MachineState) -> None:
        """After a run: a machine that keeps reporting Busy for STILL_BUSY_SECONDS gets a note
        on the finished page (Stop Machine, or its power button)."""
        busy = (self.job_view.finished and self.side_stack.get_visible_child_name() == "job"
                and state.connected and state.status == state.machine_profile.status.busy)
        if not busy:
            self._busy_since = None
            self.job_view.show_still_busy(False)
            return
        if self._busy_since is None:
            self._busy_since = time.monotonic()
            since = self._busy_since
            GLib.timeout_add(int(STILL_BUSY_SECONDS * 1000) + 50, self._still_busy, since)

    def _still_busy(self, since: float) -> bool:
        if self._busy_since == since and time.monotonic() - since >= STILL_BUSY_SECONDS:
            self.job_view.show_still_busy(True)
        return GLib.SOURCE_REMOVE

    def choose_machine(self, machine: str) -> None:
        """The layout's machine, chosen in Page Setup while no machine is connected (an id
        OpenCutter does not drive gives the CAMEO 4). Not while a job is being sent, and never
        while a machine is connected: the detected one decides."""
        if (not self._busy() and not self.monitor.state.connected
                and chosen_profile(machine).id != self.layout.machine):
            self.layout.choose(machine)
            self._switch_layout()
        else:
            self.page_setup.set_profile(self.layout.profile)
        self._on_machine(self.monitor.state)

    def use_machine(self) -> None:
        """"Use {name}": the user accepts the layout for the detected machine; Send may go."""
        self.layout.use()
        self._show_machine_banner()
        self._update_send_state()
        self._update_live()

    def _switch_layout(self) -> None:
        """Make everything the layout's for ``layout.machine``: the settings, the monitor's
        texts, Page Setup (the machine's mat, the media clamped to it), the Send panel (its
        Tool cards, ranges and materials), the canvas and the banner. The design stays where
        it is."""
        profile = self.layout.profile
        self._switching = True
        try:
            self.settings.set_machine(profile.id)
            self.settings.save()
            self.monitor.set_choice(profile.id)
        finally:
            self._switching = False
        self.store = supported_presets(self.presets, profile)
        self.page_setup.set_profile(profile)
        self.send_panel.set_profile(profile, self.store)
        self._show_machine_banner()
        self._on_page_setup()

    def _show_machine_banner(self) -> None:
        banner = self.layout.banner
        if banner:
            self.machine_banner.set_title(banner)
            self.machine_banner.set_button_label(self.layout.button)
        self.machine_banner.set_revealed(bool(banner))

    async def disconnect_machine(self) -> bool:
        """Disconnect: end the background service so that the machine is free for other apps at
        once. Not while a job runs. Nothing starts again until Connect (or Try again)."""
        if self._busy() or self.monitor.state.motion or self._disconnecting:
            return False
        self._disconnecting = True
        try:
            error = await self.monitor.disconnect()
        finally:
            self._disconnecting = False
        if error is not None:
            self.toast(GLib.markup_escape_text(error))      # plain words (machine.NOT_DISCONNECTED*)
            return False
        return True

    def _update_live(self) -> None:
        """Feed the open sheets the current machine state (their checks re-run)."""
        for update in list(self._live):
            update(self.monitor.state)

    def _update_pill(self) -> None:
        runner, info = self.runner, self.job_view.info
        state = self.monitor.state
        if runner is not None and runner.outcome is Outcome.RUNNING:
            text, tone, pulse, status = self._job_pill(runner, info, state)
            refused = None
        else:
            text, tone, pulse = pill(state)
            status = state.status_text
            refused = refused_name(state)
        self.pill.set_state(text, tone, pulse, refused, name=state.name)
        self.status_label.set_markup(f"Status: <span weight='500'>{GLib.markup_escape_text(status)}</span>")
        # a refused machine: why, in plain words (its name and reason for one OpenCutter knows)
        self.status_label.set_tooltip_text(shown_problem(state) if state.wrong_model else None)

    def _job_pill(self, runner: JobRunner, info: JobInfo,
                  state: MachineState) -> tuple[str, str, bool, str]:
        """(pill text, tone, pulsing, status bar text) while a job runs: what the current step
        does (runner.activity: 'Preparing', 'Previewing outline', 'Cutting 40%', 'Returning to
        origin'), 'Check the position' at the outline gate, the machine's own Paused or
        Cancelling, or 'Connection lost'. The status bar adds what the machine reports."""
        verb = ACTIVITY.get(info.action, "Cutting")
        if self._link_dropped_in_job:
            return "Connection lost", "negative", False, "Connection lost"
        status = state.machine_profile.status
        term = (status.text(state.interrupted) or status.text(state.status)) if state.connected else None
        reported = f" · machine: {state.status_text}" if state.connected else ""
        if term in INTERRUPTED_TERMS:            # never resumed from here: the step ends
            return term, "warning", False, f"{term}{reported}"
        if runner.holding:
            return ("Check the position", "warning", False,
                    f"Waiting for Cut (outline check){reported}")
        doing, cutting = activity(runner, verb)
        if info.test_cut:
            return "Test in Progress", "info", True, f"{doing}{reported}"
        if cutting:
            return f"{verb} {round(runner.progress() * 100)}%", "info", True, f"{verb}{reported}"
        return doing, "info", True, f"{doing}{reported}"

    def _update_send_state(self) -> None:
        running = self._job_running()
        media = self.page_setup.media_rect()
        check = self.send_panel.tool_check()
        chosen = self.send_panel.material is not None
        mat = self.page_setup.mat_key
        send = send_blocker(self.doc, media, self.monitor.state, check, running, chosen,
                            layout=self.layout, mat=mat)
        test = blocker_for_test_cut(self.monitor.state, check, running, chosen,
                                    layout=self.layout, mat=mat)
        if self._sending and not running:          # one send at a time, from click to run_job
            send = test = SENDING
        if self._stop_pending() and not running:   # Cancel's stop has no reply yet
            send = test = WAITING_FOR_STOP
        self.send_panel.set_blockers(send, test)
        self._enable("send", send is None)
        self._enable("test-cut", test is None)
        self._enable("cancel-job", running)
        self._enable("open", not self._busy())
        self.connection.set_busy(self._busy() or bool(self.monitor.state.motion))
        self.header_send.set_tooltip_text(send or "Send (Ctrl+Enter)")
        self.header_send.set_visible(not running)
        self.header_cancel.set_visible(running)
        # Stop Machine: the service's stop op without a session, once a run has ended
        connected = self.monitor.state.connected
        self._enable("stop-machine", connected and not running and not self._machine_stopping)
        tip = None if connected else STOP_UNAVAILABLE
        self.header_stop.set_tooltip_text(tip)
        self.job_view.stop_button.set_tooltip_text(tip)
        finished = (not running and self.job_view.finished
                    and self.side_stack.get_visible_child_name() == "job")
        self.header_stop.set_visible(finished and self.runner.outcome is not Outcome.DONE)

    # --- sending ---------------------------------------------------------------------
    def _job_running(self) -> bool:
        return self.runner is not None and self.runner.outcome is Outcome.RUNNING

    def _stop_pending(self) -> bool:
        """Cancel sent the stop, and its reply has not come yet."""
        return self.runner is not None and self.runner.stop_pending

    def _busy(self) -> bool:
        """True from a Send or Test Cut click until its job has ended, and until the reply of
        Cancel's stop came."""
        return self._sending or self._job_running() or self._stop_pending()

    async def send_flow(self, test_cut: bool = False) -> None:
        """Send (or Test Cut): plan locally, Load Media if needed, confirm, then run.

        Only one flow runs at a time: a second click (a double click, a repeated Ctrl+Enter)
        returns at once, before anything is awaited."""
        if self._busy():
            return
        self._sending = True
        self._update_send_state()
        try:
            await self._send_flow(test_cut)
        finally:
            self._sending = False
            self._update_send_state()

    async def _send_flow(self, test_cut: bool) -> None:
        for _round in range(CONFIRM_ROUNDS):
            prepared = self._prepare(test_cut)
            if prepared is None:
                return
            spec, settings, doc = prepared
            summary, error = plan_summary(spec)
            if error:
                dialogs.show_message(self, "This job cannot be sent", error)
                return
            await self.monitor.refresh()
            if self._closed:
                return
            # after a run (or a dropped link) the mat must be unloaded and loaded first, even
            # when the machine reports Ready: never skipped silently
            if self._reload_needed or not self.monitor.state.ready:
                if not await self._load_sheet():
                    return
                if self._closed:
                    return
            # never for a layout the user has not accepted for the connected machine: the
            # machine may have changed while Load Media was open (then the Send sheet would show
            # a job made for the other one), and again while the Send sheet was open
            if self._machine_changed():
                return
            if not test_cut and not await self._confirm_send(settings, summary, doc):
                return
            if self._closed or self._machine_changed():
                return
            # what is on screen now must be what the user confirmed
            current = self._prepare(test_cut)
            if current is not None and current[0].to_dict() == spec.to_dict():
                await self.run_job(spec.to_dict(), test_cut)
                return
            if test_cut:
                dialogs.show_message(self, "The settings changed",
                                     "Nothing was sent. Press Test Cut again to cut with the new settings.")
                return
            self.toast("The job changed before it was sent. Check it again.")
        dialogs.show_message(self, "The job kept changing",
                             "Nothing was sent. Check the design and the settings, then send again.")

    async def _load_sheet(self) -> bool:
        """The Load Cutting Mat sheet: True once the user pressed Continue (the machine is
        Ready, and when a reload was needed the mat was unloaded meanwhile)."""
        reload = None
        if self._reload_needed:
            reload = self._reload_text
        sheet = LoadMediaDialog(self._mat_name(), self.page_setup.media_label(), reload=reload)
        self._attach_live(sheet, lambda state: sheet.update(state, self._reload_needed))
        return await sheet.ask(self)

    def _prepare(self, test_cut: bool):
        """(spec, settings, design snapshot) from what is on screen now, or None.

        Typed but unconfirmed lengths (X, Y, W, H, the custom media size) are committed first,
        exactly as Enter would, so the job never differs from what the window shows."""
        for entry in self.entries.values():
            entry.commit()
        self.page_setup.commit_pending()
        settings = self.send_panel.tool_settings()
        media = self.page_setup.media_rect()
        if self.send_panel.material is None or settings is None or (not test_cut and self.doc is None):
            return None                   # never a job without a material the user chose
        machine, mat = self.layout.machine, self.page_setup.mat_key
        if test_cut:
            return spec_for_test_cut(media, settings, machine=machine, mat=mat), settings, None
        doc = self.doc.snapshot()
        return job_spec(doc, media, settings, self.send_panel.preview.get_active(),
                        machine=machine, mat=mat), settings, doc

    def _machine_changed(self) -> bool:
        """True when the layout is not accepted for the connected machine (another machine, or
        "Use {name}" not pressed yet): says so (MACHINE_CHANGED), and nothing is sent."""
        if self.layout.blocker(self.monitor.state) is None:
            return False
        dialogs.show_message(self, *MACHINE_CHANGED)
        return True

    def _mat_name(self) -> str | None:
        """The layout's mat ('12 × 24 in') on a machine with more than one, else None."""
        profile = self.layout.profile
        return self.page_setup.mat.label if len(profile.mats) > 1 else None

    def _holder_label(self, holder: int) -> str:
        found = self.layout.profile.holder(holder)
        return found.label if found is not None else f"Tool {holder}"

    def _notes(self) -> list[str]:
        """What the Send sheet notes without blocking: a mat whose cut area cannot be checked
        on the layout's machine, and a Media Size that covers the whole cut area (the media
        check cannot keep the blade on a smaller sheet)."""
        choice = next((c for c in mat_choices(self.layout.profile)
                       if c.key == self.page_setup.mat_key), None)
        notes = [UNCHECKED_MAT] if choice is not None and choice.note == UNCHECKED_MAT else []
        if media_covers_cut_area(self.page_setup.media_rect(), *self._mat()):
            notes.append(MEDIA_IS_MAT.format(media=self.page_setup.media_label()))
        return notes

    async def _confirm_send(self, settings, summary: dict, doc: PlacedDocument) -> bool:
        panel = self.send_panel
        profile = self.layout.profile
        required = tool_check(settings, MachineState(choice=profile.id), profile)
        tool = f"{required.label} · {required.required_name}"
        loaded = mat_loaded_text(self._mat_name())

        def checks(state: MachineState) -> list[tuple[str, bool]]:
            check = tool_check(settings, state, profile)
            current = self.doc        # live: the window feeds the sheet on every change
            fits = current is not None and design_fits(current.bbox(), self.page_setup.media_rect(),
                                                       *self._mat())
            rows = [(f"{check.label}: {check.required_name} detected", check.matches),
                    (loaded, state.ready and not self._reload_needed),
                    (DESIGN_PLACED, fits)]
            why = self.layout.blocker(state)       # another machine, or "Use {name}" not pressed
            return rows + [(why, False)] if why else rows

        sheet = SendDialog(SendSummary(document=doc.source_name, material=panel.material or "",
                                       action=panel.action or "", tool=tool,
                                       settings=panel.summary_text(with_passes=False),
                                       settings_tooltip=depth_note(settings),
                                       passes=settings.passes,
                                       preview_outline=panel.preview.get_active(),
                                       estimated_seconds=estimated_seconds(summary),
                                       paths=doc.path_count, machine=profile.name,
                                       notes=tuple(self._notes()),
                                       media=self.page_setup.media_label()),
                           replace(self.scene, doc=doc, test_spot=None), checks)
        self._attach_live(sheet, sheet.update)
        return await sheet.ask(self)

    async def run_job(self, spec: dict, test_cut: bool = False) -> Outcome:
        """Run a job spec through the daemon, showing the job view; handles a lost link.

        Never starts while another job runs, or while the last one's stop still waits for its
        reply: the running job's runner (and with it Cancel Job, Esc and the close-window stop)
        must stay the one the window tracks."""
        panel = self.send_panel
        settings = panel.tool_settings()
        if self._job_running() or self._stop_pending() or panel.material is None or settings is None:
            return Outcome.FAILED
        runner = JobRunner(self.client, on_update=self._on_runner_update)
        self.runner = runner
        self._stop_alerted = False
        self._link_dropped_in_job = False
        self._busy_since = None
        self.monitor.quiet = True
        action = panel.action     # the job's own: the panel follows another machine meanwhile
        subtitle = " · ".join(filter(None, [None if test_cut else self.doc.source_name, panel.material,
                                            panel.action, self._holder_label(settings.holder)]))
        self.job_view.start(runner, JobInfo(test_cut=test_cut, subtitle=subtitle,
                                            material=panel.material or "", action=panel.action or "",
                                            passes=settings.passes))
        self.job_view.set_machine(self.monitor.state.name, self.monitor.state.connected)
        self.side_stack.set_visible_child_name("job")
        # Cancel Job must stay on screen: pin the sidebar so a narrow window does not hide it,
        # and lock the sidebar toggle until the job has ended
        pinned = self.split.get_pin_sidebar()
        self.split.set_pin_sidebar(True)
        self.split.set_show_sidebar(True)
        self.sidebar_toggle.set_sensitive(False)
        self.scene.locked = True
        self._sync_placement()
        inhibit = self._inhibit_session()
        try:
            outcome = await runner.run_job(spec)
            if runner.stop_pending:             # Cancel's stop: its reply decides what is shown
                await runner.wait_stop(STOP_WAIT)
        finally:
            self._uninhibit_session(inhibit)
            self.monitor.quiet = False
            self.scene.locked = False
            self.split.set_pin_sidebar(pinned)
            self.sidebar_toggle.set_sensitive(True)
            self._sync_placement()
            self.canvas.queue_draw()
        await self.monitor.refresh()
        if self._closed:                         # closed meanwhile (after Cancel Job and quit)
            return outcome
        self._on_machine(self.monitor.state)     # the banner, the pill and the buttons again
        if self._lost_in_run(runner, outcome):
            if await dialogs.connection_lost(self):
                await self.monitor.connect()
        elif outcome is Outcome.DONE:
            if test_cut:
                # the panels come back at once: the toast carries Stop Machine instead of the
                # finished page
                self._toast_with_stop("<b>Test Complete.</b> Check that the square peels off "
                                      "cleanly.")
                self._leave_job()
            elif not self.is_active():
                self.toast(f"<b>{action} complete</b> · {format_duration(runner.elapsed())}")
        return outcome

    def _lost_in_run(self, runner: JobRunner, outcome: Outcome) -> bool:
        """True when the run ended because the link to the machine (or the service) was lost:
        decided from the run itself, not only from the state after it (a fast reconnect)."""
        if outcome is Outcome.LOST or runner.link_lost:
            return True
        if outcome not in (Outcome.FAILED, Outcome.CANCELLED) or runner.refused_before_start:
            return False
        return self._link_dropped_in_job or not self.monitor.state.connected

    def connection_lost_texts(self) -> tuple[str, str, bool]:
        """(heading, body, offer Reconnect) of the 'connection lost' dialog for the last run:
        'while cutting' only when a cut step had started; when the link is back already, the
        body says so and Reconnect is not offered."""
        runner = self.runner
        heading = JOB_LOST_HEADING
        if runner is not None:
            cuts = cut_steps(runner.steps, runner.summary)
            failed = [i for i, s in enumerate(runner.steps)
                      if s.started_at is not None and s.state is not StepState.DONE]
            if any(i in cuts for i in failed):
                heading = CUT_LOST_HEADING
        if self.monitor.state.connected:
            return heading, LOST_BACK_BODY, False
        return heading, LOST_BODY, True

    def _inhibit_session(self) -> tuple[Gtk.Application, int] | None:
        """While a job or Test Cut runs: ask the desktop not to log out or suspend (GNOME suspends
        an idle computer on battery after 15 minutes; a logout would end the background service
        in the middle of a cut). Inside the Flatpak GTK asks through the Inhibit portal. Returns
        what ``_uninhibit_session`` needs, or None when the desktop did not agree."""
        app = self.get_application()
        if app is None:
            return None
        try:
            cookie = app.inhibit(self, JOB_INHIBIT_FLAGS,
                                 job_inhibit_reason(self.layout.profile.name))
        except Exception:  # noqa: BLE001 - the job runs all the same
            log.exception("could not ask the desktop not to log out or suspend")
            return None
        return (app, cookie) if cookie else None

    @staticmethod
    def _uninhibit_session(inhibit: tuple[Gtk.Application, int] | None) -> None:
        if inhibit is not None:
            app, cookie = inhibit
            try:
                app.uninhibit(cookie)
            except Exception:  # noqa: BLE001 - nothing more to do about it
                log.exception("could not end the logout and suspend inhibition")

    def _on_runner_update(self, runner: JobRunner) -> None:
        self.job_view.update(runner)
        self._update_pill()
        if runner is not self.runner:
            return
        if runner.stop_failed and not self._stop_alerted:
            # whenever the reply comes, whatever page is shown: a toast that stays
            self._stop_alerted = True
            self._toast_stays(STOP_FAILED_TOAST)
        if runner.outcome is not Outcome.RUNNING:
            if runner.started and self._reload_runner is not runner:
                # the machine may have moved: the next job first needs Unload and Load (an
                # Unload from now on counts, also while the stop's reply is awaited)
                self._reload_runner = runner
                self._need_reload(self.monitor.state, RELOAD_AFTER_DONE if runner.outcome is
                                  Outcome.DONE else RELOAD_AFTER_STOP)
                self._check_reload(self.monitor.state)
            self._update_send_state()            # Stopping… ends: Send and the rest come back

    def cancel_job(self) -> None:
        """Cancel Job: one click, no confirmation."""
        if self._job_running():
            spawn(self.runner.cancel())

    async def stop_machine(self) -> dict | None:
        """Stop Machine, once a run has ended: the service's stop op without a session (ESC 09
        twice through its link, nothing else), with the status read after it. Says what came
        back: sent (and whether the machine still reports Busy), or not sent (the power button)."""
        if self._machine_stopping or self._job_running():
            return None
        self._machine_stopping = True
        self._update_send_state()
        try:
            reply = await self.client.stop(check=True)
        except Exception as e:  # noqa: BLE001 - DaemonClient.stop never raises; shown as not sent
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            self._machine_stopping = False
            self._update_send_state()
        if not isinstance(reply, dict) or not reply.get("ok"):
            await dialogs.stop_not_sent(self)
        elif reply.get("still_busy"):
            await dialogs.still_busy_after_stop(self)
        else:
            self.toast(GLib.markup_escape_text(STOP_SENT))
        return reply

    def _cut_again(self) -> None:
        if self._busy():
            return
        self._leave_job()
        spawn(self.send_flow())

    def _leave_job(self) -> None:
        if self._job_running() or self._stop_pending():
            return
        self.side_stack.set_visible_child_name("panels")
        # Gtk.Stack gives the focus back to the panel's last focused widget (often Test Cut
        # or Send): a stray Return or Space must not start another job, so move it away. The
        # canvas keeps it, disarmed: an arrow key does not move the design until it is clicked
        if not (self.canvas.get_mapped() and self.canvas.park_focus()):
            self.set_focus(None)
        self._busy_since = None
        self.job_view.show_still_busy(False)
        self._on_machine(self.monitor.state)     # the banner, hidden during the job, may show

    def toast(self, markup: str) -> None:
        """Show a short message (Pango markup)."""
        self.toasts.add_toast(Adw.Toast(title=markup, use_markup=True))

    def _toast_with_stop(self, markup: str) -> None:
        """A message (Pango markup) with a Stop Machine button (win.stop-machine, insensitive
        while not connected): after a completed Test Cut, whose finished page is not shown."""
        self.toasts.add_toast(Adw.Toast(title=markup, use_markup=True, button_label=STOP_MACHINE,
                                        action_name="win.stop-machine", timeout=TEST_TOAST_SECONDS))

    def _toast_stays(self, markup: str) -> None:
        """A message that stays until it is dismissed (Pango markup)."""
        self.toasts.add_toast(Adw.Toast(title=markup, use_markup=True, timeout=0,
                                        priority=Adw.ToastPriority.HIGH))

    # --- closing ---------------------------------------------------------------------
    def _on_close_request(self, _window) -> bool:
        if not self._quit_confirmed:
            if self._job_running():
                if not self._closing:
                    spawn(self._close_during_job())
                return True
            if self._stop_pending():              # Cancel's stop has no reply yet: wait for it
                if not self._closing:
                    spawn(self._close_after_stop())
                return True
        self._closed = True               # a send still between its click and run_job stops here
        if self._monitor_task is not None:
            self._monitor_task.cancel()
        end_helper = getattr(self.client, "end_helper", None)
        if end_helper is not None:
            # in the Flatpak: a flatpak-spawn helper still waiting for a starting service would
            # keep this window's sandbox running after the window (the service runs on)
            end_helper()
        return False

    async def _close_during_job(self) -> None:
        self._closing = True
        runner = self.runner
        try:
            if not await dialogs.confirm_quit_during_job(self):
                return
            self._stop_alerted = True             # shown here, by the blocking alert
            reply = await runner.cancel()
            if (runner.stop_failed or runner.outcome is Outcome.LOST
                    or (not reply.get("ok") and not runner.link_lost)):
                # the stop did not get through (Cancel's, or the service's own when the run
                # failed while this dialog was open; none at all when the session broke
                # meanwhile): keep the window (and the job view's power-button text) and say so
                # in a blocking alert instead of disappearing
                await dialogs.stop_failed(self)
                return
            self._quit_confirmed = True
            await runner.wait_ended(QUIT_WAIT)    # never asks again while the session closes
        finally:
            self._closing = False
        self.close()

    async def _close_after_stop(self) -> None:
        """The window was closed while Cancel's stop waits for its reply: close once it came
        and got through; otherwise keep the window with the power-button alert."""
        self._closing = True
        runner = self.runner
        try:
            self.toast(GLib.markup_escape_text(WAITING_FOR_STOP))
            self._stop_alerted = True             # a failure is shown here, by the alert
            reply = await runner.wait_stop(STOP_WAIT)
            if runner.stop_failed or ((reply is None or not reply.get("ok"))
                                      and not runner.link_lost):
                await dialogs.stop_failed(self)
                return
            self._quit_confirmed = True
        finally:
            self._closing = False
        self.close()


def _paint_legend_line(action: str):
    def paint(_widget: Gtk.Widget, snapshot: Gtk.Snapshot, width: int, height: int) -> None:
        key, dashed = line_style(action)
        builder = Gsk.PathBuilder.new()
        builder.move_to(0, height / 2)
        builder.line_to(width, height / 2)
        stroke = Gsk.Stroke.new(2)
        if dashed:
            stroke.set_dash([4, 2])
        snapshot.append_stroke(builder.to_path(), stroke, palette()[key])
    return paint


def _legend(action: str) -> Gtk.Widget:
    box = Gtk.Box(spacing=6)
    box.append(Painting(_paint_legend_line(action), 16, 10))
    box.append(label(action, "caption", "dim-label"))
    return box


def _shortcuts_section() -> Adw.ShortcutsSection:
    section = Adw.ShortcutsSection(title="General")
    for title, accel in (("Open", "<Control>o"), ("Send", "<Control>Return"),
                         ("Cancel Job", "Escape"), ("Fit", "<Control>0"),
                         ("Zoom in", "<Control>plus"), ("Zoom out", "<Control>minus"),
                         ("Nudge the design by 0.1 in (Shift: 1 in)", "Left Right Up Down"),
                         ("Center to Page", CENTER_TO_PAGE_ACCEL),
                         ("Preferences", "<Control>comma")):
        section.add(Adw.ShortcutsItem(title=title, accelerator=accel))
    return section


def _shortcuts_dialog() -> Adw.ShortcutsDialog:
    dialog = Adw.ShortcutsDialog()
    dialog.add(_shortcuts_section())
    return dialog
