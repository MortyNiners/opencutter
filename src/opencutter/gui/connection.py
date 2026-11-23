# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The machine status pill in the header bar and its connection popover.

Machine profiles: the texts name the machine of ``MachineState.machine_profile`` (the detected
one, else the layout's), and its tool holders give the popover one row each. The module
constants name the CAMEO 4, word for word as before; power_on_hint(), connect_hint() and
release_hint() give them for another machine. The tip to switch the machine off and on shows
for every machine whose profile asks for it (MachineProfile.power_cycle_to_connect); it says
that the machine accepts a connection only right after it is switched on only where that was
seen (machine.power_cycle_seen, the tested model), and is advice only for the others. The
popover shows every machine OpenCutter drives alike, whatever its support level; a machine the
service refused although OpenCutter recognizes it gets its name and plain reason
(shown_problem).

Translations: the texts this module makes are translated where they are made; the machine
state's texts (problem, note, hints of gui.machine) stay English there and are translated here,
where they are shown (i18n.translate_text).
"""
from __future__ import annotations

from gi.repository import GLib, GObject, Gtk, Pango

from opencutter.i18n import N_, _, holder_label, status_label, translate_text
from opencutter.machines import Holder, Support

from .machine import (MACHINE_NAME, OTHER_MACHINE_HINT, RELEASED, UNSUPPORTED, Link, MachineState,
                      drivable_hint, other_machine_hint, power_cycle_seen, texts_for)
from .widgets import PropertyList, StatusDot, label

PAIR_HINT = N_("Pair the machine once in <b>Settings → Bluetooth</b>.")
POPOVER_CHARS = 46


def power_on_hint(name: str = MACHINE_NAME, seen: bool = True) -> str:
    """The tip to switch the machine off and on. `seen`: the machine was seen to accept a
    connection only right after it is switched on (machine.power_cycle_seen); otherwise the tip
    is advice only and claims nothing about the model."""
    if seen:
        return _("The {name} accepts a Bluetooth connection only right after it is switched on. "
                 "If the app can’t connect, switch the machine off and on.").format(name=name)
    return _("If the app can’t connect to the {name}, switch the machine off and on.").format(
        name=name)


def connect_hint(name: str = MACHINE_NAME, power_cycle: bool = True) -> str:
    """What to check when nothing more is known about a failed connection."""
    if power_cycle:
        return _("Make sure the {name} is switched on and paired. If it still fails, switch it "
                 "off and on again.").format(name=name)
    return _("Make sure the {name} is switched on and paired.").format(name=name)


def release_hint(name: str = MACHINE_NAME) -> str:
    """What Disconnect does."""
    return _("Releases the {name} so other apps can connect to it.").format(name=name)


POWER_ON_HINT = power_on_hint()
CONNECT_HINT = connect_hint()
COULD_NOT_CONNECT = f"<b>Could not Connect.</b> {CONNECT_HINT}"
RELEASE_HINT = release_hint()
DISCONNECT_BUSY = N_("Available when the job has ended.")
SCANNING = N_("Scanning for devices")
COULD_NOT_CONNECT_TITLE = N_("Could not Connect")
NOT_SUPPORTED_STATE = N_("Not Supported")  # the device row under the refused machine's model
UNKNOWN_MACHINE = N_("Unknown machine")    # a refused machine that did not report its model
DEVICE_NAME_CHARS = 24                     # a machine's name or model is ellipsized beyond this


def failure_title(state: MachineState) -> str:
    """The first words of the banner and the popover's alert: 'Could not Connect', or
    'Unsupported Machine' for a machine the service refused for its model."""
    return UNSUPPORTED if state.wrong_model else COULD_NOT_CONNECT_TITLE


def failure_text(problem: str | None, title: str = COULD_NOT_CONNECT_TITLE,
                 hint: str = CONNECT_HINT) -> str:
    """'Could not Connect.' (or ``title``) and why (``MachineState.problem``), or the generic
    ``hint``. Plain text, not escaped: only for widgets without markup, such as the banner."""
    # Translators: the first words of a banner ("Could not Connect"), then why.
    return _("{title}. {problem}").format(title=_(title),
                                          problem=translate_text(problem or hint))


def failure_markup(problem: str | None, title: str = COULD_NOT_CONNECT_TITLE,
                   hint: str = CONNECT_HINT) -> str:
    """``failure_text`` as Pango markup with the first words in bold. The problem is escaped:
    it may name what the machine reports itself as."""
    return _("<b>{title}.</b> {problem}").format(
        title=GLib.markup_escape_text(_(title)),
        problem=GLib.markup_escape_text(translate_text(problem or hint)))


def shown_problem(state: MachineState) -> str | None:
    """Why it could not connect, as the banner, the popover and the first run show it: for a
    machine the service refused although OpenCutter recognizes it, its name and plain reason
    ("OpenCutter recognizes this CAMEO 3 but cannot drive it yet: ..."); otherwise
    ``MachineState.problem``."""
    if state.wrong_model and state.support == Support.RECOGNIZED.value:
        reason = other_machine_hint(state)
        if reason != OTHER_MACHINE_HINT:
            return reason
    return state.problem


def state_hint(state: MachineState) -> str:
    """The connect hint for the machine the state names (connect_hint)."""
    return connect_hint(**texts_for(state.machine_profile))


def refused_name(state: MachineState) -> str | None:
    """What the pill's tooltip calls a machine the service refused for its model (its model, or
    'Unknown machine'); None for every other state, where the pill names the machine."""
    if not state.wrong_model:
        return None
    return state.model or _(UNKNOWN_MACHINE)


def device_line(state: MachineState) -> tuple[str, str, str]:
    """The device row (name, state, tone) of the connection popover and the first run. A
    machine refused for its model is shown by what it reports itself as, 'Not Supported'."""
    if state.wrong_model:
        return state.model or _(UNKNOWN_MACHINE), _(NOT_SUPPORTED_STATE), "negative"
    texts = {Link.NO_DAEMON: (_("Not Connected"), "neutral"),
             Link.CONNECTING: (_("Connecting…"), "info"),
             Link.FAILED: (_("Not Connected"), "negative"),
             Link.CONNECTED: (_("Connected"), "positive")}
    return (state.device_name or state.name, *texts[state.link])


class StatusPill(Gtk.MenuButton):
    """'● CAMEO 4 · Ready': a coloured dot plus text; opens the connection popover."""

    __gtype_name__ = "OpenCutterStatusPill"

    def __init__(self, popover: Gtk.Popover):
        """Create the pill around the given popover."""
        super().__init__(popover=popover)
        self.add_css_class("status-pill")
        box = Gtk.Box(spacing=8)
        self._dot = StatusDot()
        self._text = label("", "status-text")
        self._name = label(MACHINE_NAME, "status-name")
        box.append(self._dot)
        box.append(self._name)
        box.append(self._text)
        box.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
        self.set_child(box)
        self.set_state(_("Not Connected"), "neutral", False)

    def set_state(self, text: str, tone: str, pulse: bool, refused: str | None = None,
                  name: str = MACHINE_NAME) -> None:
        """Show a state, e.g. ('Cutting 62%', 'info', True), for the machine ``name``. For a
        machine the service refused for its model, ``refused`` is what to call it
        (``refused_name``): the pill then shows the state alone, not the name, and the tooltip
        names that machine (plain text)."""
        self._dot.set_tone(tone, pulse)
        self._name.set_text(name)
        self._name.set_visible(refused is None)
        self._text.set_text(text if refused is not None else f"· {text}")
        self.set_tooltip_text(f"{refused or name}: {text}")
        self.update_property([Gtk.AccessibleProperty.LABEL],
                             [_("Machine status: {status}. Open connection").format(status=text)])

    @property
    def text(self) -> str:
        """The state text shown, without the machine name."""
        return self._text.get_text().removeprefix("· ")


def _help_row(text: str) -> tuple[Gtk.Box, Gtk.Label]:
    row = Gtk.Box(spacing=8)
    row.append(Gtk.Image(icon_name="dialog-information-symbolic", valign=Gtk.Align.START))
    hint = label(text, "caption", wrap=True, markup=True)
    hint.set_max_width_chars(POPOVER_CHARS)
    row.append(hint)
    return row, hint


class ConnectionPopover(Gtk.Popover):
    """Not Connected, Connecting (scanning), Connected (firmware, tools, Disconnect), Could not
    Connect, or Unsupported Machine (the service refused the machine for its model)."""

    __gtype_name__ = "OpenCutterConnectionPopover"
    __gsignals__ = {"connect": (GObject.SignalFlags.RUN_FIRST, None, ()),
                    "disconnect": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        """Build every state; ``update`` picks the one to show."""
        super().__init__()
        self.add_css_class("connection-popover")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_size_request(340, -1)

        self._alert = Gtk.Box(spacing=12)
        self._alert.add_css_class("inline-alert")
        self._alert.add_css_class("error")
        self._alert.append(Gtk.Image(icon_name="dialog-error-symbolic", valign=Gtk.Align.START))
        self._alert_text = label(failure_markup(None), "caption", wrap=True, markup=True)
        self._alert_text.set_max_width_chars(POPOVER_CHARS)
        self._alert.append(self._alert_text)
        box.append(self._alert)

        self._scanning = Gtk.Box(spacing=8)
        self._scanning.append(Gtk.Spinner(spinning=True, valign=Gtk.Align.START))
        self._scanning_text = label(_(SCANNING), "heading", wrap=True)
        self._scanning_text.set_max_width_chars(POPOVER_CHARS)
        self._scanning.append(self._scanning_text)
        box.append(self._scanning)

        device = Gtk.Box(spacing=12)
        device.add_css_class("device-row")
        device.append(Gtk.Image(icon_name="bluetooth-symbolic", pixel_size=20))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self._device_name = label(MACHINE_NAME, "heading")
        self._device_name.set_ellipsize(Pango.EllipsizeMode.END)
        self._device_name.set_max_width_chars(DEVICE_NAME_CHARS)
        state_line = Gtk.Box(spacing=6)
        self._device_dot = StatusDot()
        self._device_state = label("", "caption", "dim-label")
        state_line.append(self._device_dot)
        state_line.append(self._device_state)
        names.append(self._device_name)
        names.append(state_line)
        device.append(names)
        self._connect = Gtk.Button(label=_("Connect"), valign=Gtk.Align.CENTER)
        self._connect.add_css_class("suggested-action")
        self._connect.add_css_class("pill-sm")
        self._connect.connect("clicked", self._on_connect)
        device.append(self._connect)
        self._disconnect = Gtk.Button(label=_("Disconnect"), valign=Gtk.Align.CENTER)
        self._disconnect.add_css_class("pill-sm")
        self._disconnect.connect("clicked", self._on_disconnect)
        device.append(self._disconnect)
        box.append(device)
        # under the device row: what Disconnect does, or (after it) that the machine is free
        self._hint = label(release_hint(), "caption", "dim-label", wrap=True)
        self._hint.set_max_width_chars(POPOVER_CHARS)
        box.append(self._hint)
        self._busy = False

        self._props = PropertyList()
        self._holders: tuple[tuple[int, str], ...] = ()
        self._tools: dict[int, Gtk.Label] = {}
        self._show_holders(MachineState().holders)
        box.append(self._props)

        self._help = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._help.add_css_class("help-list")
        pair, _hint = _help_row(_(PAIR_HINT))
        self._power_row, self._power_hint = _help_row(power_on_hint())
        self._help.append(pair)
        self._help.append(self._power_row)
        box.append(self._help)
        self._note = label(_("Nothing moves when you connect. The machine moves only after you "
                             "press Send, Test Cut or Cut."), "caption", "dim-label", wrap=True)
        self._note.set_max_width_chars(POPOVER_CHARS)
        box.append(self._note)
        self.set_child(box)
        self.update(MachineState())

    def _show_holders(self, holders: tuple[Holder, ...]) -> None:
        """One Tool row per holder of the machine, between Firmware and Status."""
        key = tuple((h.number, h.label) for h in holders)
        if key == self._holders:
            return
        self._holders = key
        self._props.clear()
        self._firmware = self._props.add(_("Firmware"), "—")
        self._tools = {number: self._props.add(holder_label(name), "—") for number, name in key}
        self._status = self._props.add(_("Status"), "—")

    def tool_rows(self) -> list[tuple[str, str]]:
        """(holder label, shown tool) of every Tool row, in order."""
        return [(name, self._tools[number].get_text()) for number, name in self._holders]

    def update(self, state: MachineState) -> None:
        """Show the given machine state."""
        link = state.link
        profile = state.machine_profile
        self._alert.set_visible(link is Link.FAILED)
        self._alert_text.set_markup(failure_markup(shown_problem(state), failure_title(state),
                                                   state_hint(state)))
        self._scanning.set_visible(link is Link.CONNECTING)
        self._scanning_text.set_text(translate_text(state.note) or _(SCANNING))
        name, text, tone = device_line(state)
        self._device_name.set_text(name)        # plain text: it may come from the machine
        self._device_name.set_tooltip_text(name if len(name) > DEVICE_NAME_CHARS else None)
        self._device_state.set_text(text)
        self._device_dot.set_tone(tone, link is Link.CONNECTING)
        # a machine of another model: Try again cannot change it (the service connects to a
        # machine OpenCutter drives by itself once one is paired and switched on)
        self._connect.set_visible(link in (Link.NO_DAEMON, Link.FAILED) and not state.wrong_model)
        self._connect.set_label(_("Try again") if link is Link.FAILED else _("Connect"))
        self._disconnect.set_visible(state.connected)
        if state.connected:
            hint = translate_text(release_hint(state.name))
        elif state.wrong_model:
            hint = drivable_hint(shown=True)     # OTHER_MACHINE_HINT, in the window's language
        else:
            hint = translate_text(state.note) if link is Link.NO_DAEMON else None
        self._hint.set_text(hint or "")
        self._hint.set_visible(bool(hint))
        self._props.set_visible(state.connected)
        # the pairing and switch-off-and-on tips help when connecting fails or has not happened
        # yet, not right after the user released the machine on purpose (Disconnect), and not
        # for a machine of another model (pairing it again or switching it off and on does not
        # change its model)
        self._help.set_visible(not state.connected and state.note != RELEASED
                               and not state.released and not state.wrong_model)
        self._power_hint.set_markup(GLib.markup_escape_text(
            power_on_hint(profile.name, power_cycle_seen(profile))))
        self._power_row.set_visible(profile.power_cycle_to_connect)
        # "the machine moves only after you press Send": never true for a refused machine
        self._note.set_visible(not state.wrong_model)
        self._show_holders(state.holders)
        if state.connected:
            # what the machine reports itself as (FG): the model gate's text, else info's
            self._firmware.set_text(state.model or state.firmware or "—")
            for number, widget in self._tools.items():
                widget.set_text(state.shown_tool_text(number) or "—")
            self._status.set_text(status_label(state.status_text))

    def set_busy(self, busy: bool) -> None:
        """While a job runs (here or in another app), Disconnect is not available."""
        self._busy = busy
        self._disconnect.set_sensitive(not busy)
        self._disconnect.set_tooltip_text(_(DISCONNECT_BUSY) if busy else None)

    def _on_connect(self, *_args) -> None:
        self.emit("connect")

    def _on_disconnect(self, *_args) -> None:
        if not self._busy:
            self.emit("disconnect")         # the popover stays open and shows the result
