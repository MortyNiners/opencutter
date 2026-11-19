# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Small widgets shared by the panels and dialogs (InlineNote: a non-blocking note, such as
the unchecked cut area of a mat)."""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import GObject, Gtk, Pango

from .geometry import format_length, parse_length

TONES = ("neutral", "accent", "info", "positive", "warning", "negative")


class StatusDot(Gtk.Box):
    """A small coloured dot. Colour is never the only signal: it always sits next to text."""

    __gtype_name__ = "OpenCutterStatusDot"

    def __init__(self, tone: str = "neutral"):
        """Create a dot with the given tone."""
        super().__init__(valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.add_css_class("status-dot")
        self.set_tone(tone)

    def set_tone(self, tone: str, pulse: bool = False) -> None:
        """Change the colour (one of ``TONES``) and whether it pulses."""
        for name in TONES:
            self.remove_css_class(name)
        self.add_css_class(tone)
        if pulse:
            self.add_css_class("pulse")
        else:
            self.remove_css_class("pulse")


def relate(widget: Gtk.Widget, relation: Gtk.AccessibleRelation, *others: Gtk.Widget) -> None:
    """Set an accessible relation, e.g. ``relate(switch, LABELLED_BY, its_label)``."""
    widget.update_relation([relation], [Gtk.AccessibleList.new_from_list(list(others))])


def field(label: str, child: Gtk.Widget, mnemonic_target: Gtk.Widget | None = None) -> Gtk.Box:
    """A labelled field: a small muted label above the control."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    title = Gtk.Label(label=label, xalign=0)
    title.add_css_class("field-label")
    box.append(title)
    box.append(child)
    target = mnemonic_target or child
    relate(target, Gtk.AccessibleRelation.LABELLED_BY, title)
    return box


def label(text: str = "", *css: str, wrap: bool = False, xalign: float = 0.0,
          markup: bool = False) -> Gtk.Label:
    """A label (left-aligned unless ``xalign`` says otherwise) with optional CSS classes."""
    widget = Gtk.Label(xalign=xalign, wrap=wrap)
    if wrap:
        widget.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
        if xalign == 0.5:
            widget.set_justify(Gtk.Justification.CENTER)
    if markup:
        widget.set_markup(text)
    else:
        widget.set_text(text)
    for name in css:
        widget.add_css_class(name)
    return widget


def icon_label(icon: str, text: str) -> Gtk.Box:
    """Icon followed by text, for button contents."""
    box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
    box.append(Gtk.Image.new_from_icon_name(icon))
    box.append(Gtk.Label(label=text))
    return box


class PropertyList(Gtk.Grid):
    """Two columns of label / value rows."""

    __gtype_name__ = "OpenCutterPropertyList"

    def __init__(self):
        """Create an empty list."""
        super().__init__(column_spacing=16, row_spacing=8)
        self.add_css_class("property-list")
        self._rows = 0

    def add(self, name: str, value: str) -> Gtk.Label:
        """Append a row and return its value label."""
        key = label(name, "dim-label")
        val = label(value)
        val.set_hexpand(True)
        val.set_ellipsize(Pango.EllipsizeMode.END)
        self.attach(key, 0, self._rows, 1, 1)
        self.attach(val, 1, self._rows, 1, 1)
        self._rows += 1
        return val

    def clear(self) -> None:
        """Remove every row (to add them again, e.g. for a machine with other tool holders)."""
        while (child := self.get_first_child()) is not None:
            self.remove(child)
        self._rows = 0


class InlineNote(Gtk.Box):
    """A short note in a tinted box with an icon: 'info' (a note that blocks nothing),
    'warning' or 'error'. Plain text, never markup: it may hold what the machine reports."""

    __gtype_name__ = "OpenCutterInlineNote"

    def __init__(self, text: str = "", tone: str = "info",
                 icon: str = "dialog-information-symbolic"):
        """Create the note."""
        super().__init__(spacing=8)
        self.add_css_class("inline-alert")
        self.add_css_class(tone)
        self.append(Gtk.Image(icon_name=icon, valign=Gtk.Align.START))
        self._text = label(text, "caption", wrap=True)
        self._text.set_hexpand(True)
        self.append(self._text)
        self.set_visible(bool(text))

    @property
    def text(self) -> str:
        """The note's text."""
        return self._text.get_text()

    def show_text(self, text: str | None) -> None:
        """Show ``text``, or hide the note for None or ''."""
        self._text.set_text(text or "")
        self.set_visible(bool(text))
        self.update_property([Gtk.AccessibleProperty.LABEL], [text or ""])


class LengthEntry(Gtk.Entry):
    """A short entry for a length in the user's units; emits ``committed`` with mm."""

    __gtype_name__ = "OpenCutterLengthEntry"
    __gsignals__ = {"committed": (GObject.SignalFlags.RUN_FIRST, None, (float,))}

    def __init__(self, name: str, units: str = "in", positive: bool = False):
        """Create an entry labelled ``name``; ``positive`` refuses zero and negative values."""
        super().__init__(width_chars=5, max_width_chars=6, xalign=1.0, valign=Gtk.Align.CENTER,
                         input_purpose=Gtk.InputPurpose.NUMBER)
        self.add_css_class("length-entry")
        self.units = units
        self.positive = positive
        self._mm = 0.0
        self.update_property([Gtk.AccessibleProperty.LABEL], [name])
        self.connect("activate", lambda *_: self.commit())
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self.commit())
        self.add_controller(focus)

    @property
    def mm(self) -> float:
        """The last committed value in mm."""
        return self._mm

    def set_mm(self, mm: float) -> None:
        """Show ``mm`` in the current units (does not emit)."""
        self._mm = mm
        self.set_text(format_length(mm, self.units))

    def commit(self) -> None:
        """Take the typed text as the value (as Enter or leaving the field does).

        Emits ``committed`` when the value changed; text that is not a valid length is
        replaced by the last committed value. Call it before reading a value that the user
        may still be typing, e.g. right before a job spec is built."""
        mm = parse_length(self.get_text(), self.units)
        if mm is None or (self.positive and mm <= 0):
            self.set_mm(self._mm)
            return
        if abs(mm - self._mm) > 1e-6 and self.get_text() != format_length(self._mm, self.units):
            self._mm = mm
            self.emit("committed", mm)


class Painting(Gtk.Widget):
    """A fixed-size widget drawn by ``paint(widget, snapshot, width, height)``."""

    __gtype_name__ = "OpenCutterPainting"

    def __init__(self, paint: Callable[[Gtk.Widget, Gtk.Snapshot, int, int], None],
                 width: int, height: int, description: str | None = None):
        """Create the widget; ``description`` becomes its accessible label."""
        super().__init__(valign=Gtk.Align.CENTER)
        self._paint = paint
        self.set_size_request(width, height)
        if description:
            self.update_property([Gtk.AccessibleProperty.LABEL], [description])

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        """Delegate to the paint function."""
        self._paint(self, snapshot, self.get_width(), self.get_height())
