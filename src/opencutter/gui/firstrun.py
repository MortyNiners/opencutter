# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""First run: Welcome, then Connect.

Before any machine was connected the app does not know which one the user has: the texts say
"your machine" (or name the profile given), and follow the machine once one is detected. The
tip to switch it off and on is part of the steps for every machine whose profile asks for it
(MachineProfile.power_cycle_to_connect), and while the machine is not known yet; only for the
tested model, where it was seen, does it say that the machine accepts a connection only right
after it is switched on (machine.power_cycle_seen).
"""
from __future__ import annotations

from gi.repository import Adw, GObject, Gtk, Pango

from opencutter import APP_ID
from opencutter.machines import MachineProfile

from .connection import DEVICE_NAME_CHARS, PAIR_HINT, device_line, shown_problem
from .machine import OTHER_MACHINE_HINT, Link, MachineState, power_cycle_seen
from .widgets import StatusDot, label

STEPS = 2
YOUR_MACHINE = "your machine"           # the machine, before one is known


def welcome_text(profile: MachineProfile | None = None) -> str:
    """'Cut your designs on a Silhouette CAMEO 4 from Linux.', or on 'your machine'."""
    if profile is None:
        return f"Cut your designs on {YOUR_MACHINE} from Linux."
    return f"Cut your designs on a Silhouette {profile.name} from Linux."


def switch_on_step(profile: MachineProfile | None = None) -> str:
    """The second step: switch the machine on (and off and on when it needs that)."""
    if profile is None:
        return ("Switch your machine on. Some machines accept a Bluetooth connection only right "
                "after they are switched on: if the app can’t connect, switch it off and on.")
    if power_cycle_seen(profile):
        return (f"Switch the {profile.name} on. It accepts a Bluetooth connection only right "
                "after it is switched on: if the app can’t connect, switch it off and on.")
    if profile.power_cycle_to_connect:        # not seen on this model: advice only
        return f"Switch the {profile.name} on. If the app can’t connect, switch it off and on."
    return f"Switch the {profile.name} on. OpenCutter then connects to it by itself."


class FirstRunDialog(Adw.Dialog):
    """The two-step welcome: what the app does, then how to connect the machine."""

    __gtype_name__ = "OpenCutterFirstRunDialog"
    __gsignals__ = {
        "connect": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, profile: MachineProfile | None = None):
        """Build the pages for ``profile`` (None: the machine is not known yet); the window
        feeds machine states with ``update_machine``."""
        super().__init__(title="Welcome", content_width=560, content_height=560)
        self.profile = profile
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
                                vexpand=True)
        self._stack.add_named(self._welcome_page(), "welcome")
        self._stack.add_named(self._connect_page(), "connect")
        header = Adw.HeaderBar(show_title=False)
        header.add_css_class("flat")
        view = Adw.ToolbarView(content=self._stack)
        view.add_top_bar(header)
        self.set_child(view)

    # --- pages -----------------------------------------------------------------------
    def _page(self, step: int, body: Gtk.Widget, *buttons: Gtk.Widget):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.add_css_class("first-run-page")
        body.set_vexpand(True)
        page.append(body)
        footer = Gtk.Box(spacing=8)
        footer.add_css_class("sheet-footer")
        dots = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
        dots.update_property([Gtk.AccessibleProperty.LABEL], [f"Step {step} of {STEPS}"])
        for i in range(1, STEPS + 1):
            dots.append(StatusDot("accent" if i == step else "neutral"))
        footer.append(dots)
        for button in buttons:
            footer.append(button)
        page.append(footer)
        return page

    def _welcome_page(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        body.add_css_class("welcome")
        body.append(Gtk.Image(icon_name=APP_ID, pixel_size=128))
        body.append(label("OpenCutter", "title-1", xalign=0.5))
        self._welcome = label(welcome_text(self.profile), "title-4", wrap=True, xalign=0.5)
        body.append(self._welcome)
        body.append(label("Draw in Inkscape, place the design on the mat here, choose Material, "
                          "Action and Tool, and send it over Bluetooth.", "dim-label", wrap=True,
                          xalign=0.5))
        start = Gtk.Button(label="Get started")
        start.add_css_class("suggested-action")
        start.add_css_class("pill-md")
        start.connect("clicked", lambda *_: self._stack.set_visible_child_name("connect"))
        return self._page(1, body, start)

    def _connect_page(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        body.append(label("Optional", "field-label"))
        body.append(label("Connect your machine", "title-2"))
        self._steps = []        # hidden for a machine refused for its model: they cannot help
        self._step_texts = []
        for number, text in (("1", PAIR_HINT), ("2", switch_on_step(self.profile))):
            row = Gtk.Box(spacing=12)
            badge = label(number, "step-number", xalign=0.5)
            badge.set_valign(Gtk.Align.START)
            row.append(badge)
            step = label(text, wrap=True, markup=True)
            row.append(step)
            body.append(row)
            self._steps.append(row)
            self._step_texts.append(step)
        device = Gtk.Box(spacing=12)
        device.add_css_class("device-row")
        device.append(Gtk.Image(icon_name="bluetooth-symbolic", pixel_size=20))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self._device_name = label(device_line(MachineState())[0], "heading")
        self._device_name.set_ellipsize(Pango.EllipsizeMode.END)
        self._device_name.set_max_width_chars(DEVICE_NAME_CHARS)
        state = Gtk.Box(spacing=6)
        self._device_dot = StatusDot()
        self._device_state = label("Not Connected", "caption", "dim-label")
        state.append(self._device_dot)
        state.append(self._device_state)
        names.append(self._device_name)
        names.append(state)
        self._problem = label("", "caption", wrap=True)      # why it could not connect
        self._problem.set_visible(False)
        names.append(self._problem)
        # a machine refused for its model: which machines OpenCutter drives (plain text)
        self._hint = label(OTHER_MACHINE_HINT, "caption", "dim-label", wrap=True)
        self._hint.set_visible(False)
        names.append(self._hint)
        device.append(names)
        self._connect = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
        self._connect.add_css_class("suggested-action")
        self._connect.add_css_class("pill-sm")
        self._connect.connect("clicked", lambda *_: self.emit("connect"))
        device.append(self._connect)
        body.append(device)
        self._note = label("Nothing moves when you connect. The machine moves only after you "
                           "press Send, Test Cut or Cut.", "caption", "dim-label", wrap=True)
        body.append(self._note)
        back = Gtk.Button(label="Back")
        back.add_css_class("flat")
        back.add_css_class("pill-md")
        back.connect("clicked", lambda *_: self._stack.set_visible_child_name("welcome"))
        self._finish = Gtk.Button(label="Skip for now")
        self._finish.add_css_class("pill-md")
        self._finish.connect("clicked", lambda *_: self.close())
        return self._page(2, body, back, self._finish)

    # --- behaviour -------------------------------------------------------------------
    def set_profile(self, profile: MachineProfile | None) -> None:
        """Name ``profile`` in the welcome text and the steps (None: 'your machine')."""
        self.profile = profile
        self._welcome.set_text(welcome_text(profile))
        self._step_texts[1].set_text(switch_on_step(profile))

    @property
    def step_texts(self) -> list[str]:
        """The texts of the steps, in order."""
        return [step.get_text() for step in self._step_texts]

    def update_machine(self, state: MachineState) -> None:
        """Show the live connection on the Connect page; a detected machine is named from then
        on."""
        detected = state.detected
        if detected is not None and detected != self.profile:
            self.set_profile(detected)
        name, text, tone = device_line(state)
        if state.link is Link.FAILED and not state.wrong_model:
            text = "Could not Connect"
        self._device_name.set_text(name)        # plain text: it may come from the machine
        self._device_name.set_tooltip_text(name if len(name) > DEVICE_NAME_CHARS else None)
        self._device_state.set_text(text)
        self._device_dot.set_tone(tone, state.link is Link.CONNECTING)
        # a machine of another model: Try again cannot change it
        self._connect.set_visible(state.link in (Link.NO_DAEMON, Link.FAILED)
                                  and not state.wrong_model)
        self._connect.set_label("Try again" if state.link is Link.FAILED else "Connect")
        line = shown_problem(state) if state.link is Link.FAILED else state.note
        self._problem.set_text(line or "")
        self._problem.set_visible(bool(line))
        # a machine of another model: pairing it or switching it off and on does not help, and
        # it never moves; say which machines OpenCutter drives, as the connection popover does
        self._hint.set_visible(state.wrong_model)
        self._note.set_visible(not state.wrong_model)
        for row in self._steps:
            row.set_visible(not state.wrong_model)
        self._finish.set_label("Done" if state.connected else "Skip for now")
        if state.connected:
            self._finish.add_css_class("suggested-action")
        else:
            self._finish.remove_css_class("suggested-action")
