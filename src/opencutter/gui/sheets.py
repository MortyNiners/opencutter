# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The Load Media sheet and the Send confirmation sheet.

Machine profiles: every mat of a machine profile is a cutting mat, so both sheets speak of the
cutting mat; on a machine with more than one mat they name the one the layout uses
(load_text, mat_loaded_text). The Send sheet is titled for the layout's machine and shows the
notes that block nothing, such as that the cut area of the mat cannot be checked on this model.

The Load sheet says where the sheet goes (its top-left corner on the top-left corner of the mat's
grid, where OpenCutter lays the design out) and that Media Size must match it; the Send sheet
shows the Media Size in use. For a machine that is busy, paused, cancelling or held by another
connection the Load sheet says what to do instead of loading (never: press Pause to continue).
After a run or a dropped link (``reload``), it asks to press Unload and load the mat again
(saying which of the two it was), and Continue waits until the machine reported the mat unloaded
and then Ready.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass

from gi.repository import Adw, GLib, Graphene, Gsk, Gtk

from opencutter.device.protocol import CANCELLING, PAUSED, REJECTED

from .canvas import MatThumbnail, Scene, palette
from .geometry import format_duration
from .machine import MACHINE_NAME, MachineState
from .widgets import InlineNote, Painting, PropertyList, icon_label, label

LOAD_TEXT = ("Place the media on the mat, line the mat up with the guide, then press the "
             "<b>Load</b> button on the machine. Load the mat right before every job.")
# where the sheet goes: the canvas lays the design out from the grid's top-left corner
PLACE_TEXT = ("Put the top-left corner of the sheet on the top-left corner of the mat's grid: "
              "OpenCutter lays the design out from there. Media Size in Page Setup must match "
              "the sheet{media}.")
MAT_LOADED = "Cutting mat loaded"
# after a run, or after the link dropped (the window's reload): Unload, then Load (never: press
# Pause to continue)
RELOAD_AFTER_DONE = ("Press <b>Unload</b> on the machine. With the media for the next job on the "
                     "mat, press <b>Load</b>: the machine applies the mat setting only right "
                     "after a load.")
RELOAD_AFTER_STOP = ("Press <b>Unload</b> on the machine and check that the tool is up. Then put "
                     "the media on the mat and press <b>Load</b>.")
RELOAD_AFTER_DROP = ("The connection to the machine dropped since the mat was loaded. Press "
                     "<b>Unload</b> on the machine and check that the tool is up. Then put the "
                     "media on the mat and press <b>Load</b>.")
# the state line while the machine reads Ready but the mat was not loaded again yet
RELOAD_STATES = {RELOAD_AFTER_DONE: "Loaded before the last job",
                 RELOAD_AFTER_STOP: "Loaded before the last job",
                 RELOAD_AFTER_DROP: "Loaded before the connection dropped"}
LOAD_TITLE = "Load Cutting Mat"
# what the machine reports instead of Load Media or Ready: (heading, state line, instruction)
BUSY_TEXT = ("Machine Busy", "Busy", "The machine is still busy. Wait until it stops; if it keeps "
                                     "moving, press its power button. Then load the mat.")
PAUSED_TEXT = ("Machine Paused", "Paused",
               "The machine is paused. OpenCutter does not resume it: press <b>Unload</b> on the "
               "machine (or switch it off and on), then load the mat. Do not press Pause to "
               "continue.")
CANCELLING_TEXT = ("Machine Cancelling", "Cancelling",
                   "The machine is ending a cancel. Wait until it has ended, then press "
                   "<b>Unload</b> and load the mat. Do not press Pause to continue.")
REJECTED_TEXT = ("Machine Held Elsewhere", "Rejected",
                 "Another connection (for example USB) holds the machine. Close it, then load "
                 "the mat.")


def load_text(mat_label: str | None = None, media_label: str | None = None) -> str:
    """The Load Media sheet's instruction (markup); ``mat_label`` names the mat to use ('12 ×
    24 in') on a machine with more than one. It goes on with where the sheet goes and that
    Media Size must match it, naming the Media Size in use (``media_label``) when given."""
    text = LOAD_TEXT
    if mat_label:
        text = LOAD_TEXT.replace("on the mat,", f"on the {mat_label} mat,", 1)
    return f"{text} {place_text(media_label)}"


def place_text(media_label: str | None = None) -> str:
    """PLACE_TEXT (markup), naming the Media Size in use: '... match the sheet (now Letter · 8.5
    × 11 in).'"""
    media = f" (now {GLib.markup_escape_text(media_label)})" if media_label else ""
    return PLACE_TEXT.format(media=media)


def machine_not_loadable(state: MachineState) -> tuple[str, str, str] | None:
    """(heading, state line, instruction markup) when the machine is busy, paused, cancelling
    or rejecting commands: it cannot take the mat now, and the sheet does not tell to load it
    first. None for every other state."""
    if not state.connected:
        return None
    status = state.machine_profile.status
    if state.status == status.busy:
        return BUSY_TEXT
    term = status.text(state.status)
    return {PAUSED: PAUSED_TEXT, CANCELLING: CANCELLING_TEXT, REJECTED: REJECTED_TEXT}.get(term)


def mat_loaded_text(mat_label: str | None = None) -> str:
    """The checklist line: 'Cutting mat loaded', or 'Cutting mat loaded (12 × 24 in)'."""
    return f"{MAT_LOADED} ({mat_label})" if mat_label else MAT_LOADED


class _AsyncDialog(Adw.Dialog):
    """An Adw.Dialog that can be awaited: ``await dialog.ask(parent)`` gives True or False."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._future: asyncio.Future | None = None
        # closing by Escape or the close button answers False; never close() again from here:
        # closing a dialog that is already closing aborts libadwaita (adw-dialog-host)
        self.connect("closed", lambda *_: self._resolve(False))

    async def ask(self, parent: Gtk.Widget) -> bool:
        """Present the dialog and wait for the user's answer."""
        self._future = asyncio.get_running_loop().create_future()
        self.present(parent)
        return await self._future

    def _resolve(self, value: bool) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result(value)

    def _answer(self, value: bool) -> None:
        self._resolve(value)
        self.close()


def _footer(*buttons: Gtk.Widget, note: str = "") -> Gtk.Box:
    footer = Gtk.Box(spacing=8)
    footer.add_css_class("sheet-footer")
    hint = label(note, "caption", "dim-label", wrap=True)
    hint.set_hexpand(True)
    footer.append(hint)
    for button in buttons:
        footer.append(button)
    return footer


def _button(text: str, primary: bool = False, icon: str | None = None) -> Gtk.Button:
    button = Gtk.Button(child=icon_label(icon, text)) if icon else Gtk.Button(label=text)
    button.add_css_class("pill-md")
    if primary:
        button.add_css_class("suggested-action")
    if icon:
        button.update_property([Gtk.AccessibleProperty.LABEL], [text])
    return button


def _paint_loading(_widget: Gtk.Widget, snapshot: Gtk.Snapshot, width: int, height: int) -> None:
    """Original line drawing: a cutting mat sliding up into the machine's rollers."""
    pal = palette()
    cx = width / 2

    def stroke(color: str, width_px: float, *lines: tuple[float, float, float, float],
               rects: tuple = ()) -> None:
        builder = Gsk.PathBuilder.new()
        for x0, y0, x1, y1 in lines:
            builder.move_to(cx + x0, y0)
            builder.line_to(cx + x1, y1)
        for x, y, w, h in rects:
            builder.add_rect(Graphene.Rect().init(cx + x, y, w, h))
        line = Gsk.Stroke.new(width_px)
        line.set_line_cap(Gsk.LineCap.ROUND)
        line.set_line_join(Gsk.LineJoin.ROUND)
        snapshot.append_stroke(builder.to_path(), line, pal[color])

    stroke("ink_muted", 2, (-90, 52, 90, 52), rects=((-110, 18, 220, 34),))        # machine, slot
    stroke("accent", 2, rects=((-70, 58, 140, 70),))                                # mat
    stroke("accent", 1, *[(-70 + i * 140 / 6, 58, -70 + i * 140 / 6, 128) for i in range(1, 6)],
           *[(-70, 58 + i * 70 / 3, 70, 58 + i * 70 / 3) for i in range(1, 3)])   # mat grid
    stroke("ink", 2, *[seg for x in (-100, 100)                                     # arrows up
                       for seg in ((x, 120, x, 70), (x - 7, 78, x, 70), (x, 70, x + 7, 78))])


class LoadMediaDialog(_AsyncDialog):
    """'Load Cutting Mat': waits, with a live state, until the machine reports Ready (and, after
    a run, until it reported the mat unloaded first)."""

    __gtype_name__ = "OpenCutterLoadMediaDialog"

    def __init__(self, mat_label: str | None = None, media_label: str | None = None,
                 reload: str | None = None):
        """Build the sheet; feed it machine states with ``update``. ``mat_label`` names the
        mat on a machine with more than one, ``media_label`` the Media Size in use. ``reload``
        (RELOAD_AFTER_DONE, RELOAD_AFTER_STOP or RELOAD_AFTER_DROP) is the instruction after a
        run or a dropped link: the mat must be unloaded and loaded again before Continue."""
        super().__init__(title=LOAD_TITLE, content_width=460)
        self._load = load_text(mat_label, media_label)
        self._reload = f"{reload} {place_text(media_label)}" if reload else None
        self._reload_state = RELOAD_STATES.get(reload, "Not loaded again yet")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.add_css_class("sheet")
        box.append(Painting(_paint_loading, 300, 140, "A cutting mat entering the machine"))
        self._heading = label(LOAD_TITLE, "title-2")
        box.append(self._heading)
        self._text = label(self._reload or self._load, wrap=True, markup=True)
        box.append(self._text)
        status = Gtk.Box(spacing=12)
        status.add_css_class("live-status")
        status.update_property([Gtk.AccessibleProperty.LABEL], ["Machine state"])
        self._spinner = Gtk.Spinner(spinning=True)
        self._ok = Gtk.Image(icon_name="object-select-symbolic")
        self._ok.add_css_class("success")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._state = label("Waiting for media…", "heading")
        self._detail = label("Changes to Ready by itself", "caption", "dim-label")
        texts.append(self._state)
        texts.append(self._detail)
        for widget in (self._spinner, self._ok, texts):
            status.append(widget)
        box.append(status)
        cancel = _button("Cancel")
        cancel.connect("clicked", lambda *_: self._answer(False))
        self._continue = _button("Continue", primary=True)
        self._continue.connect("clicked", lambda *_: self._answer(True))
        box.append(_footer(cancel, self._continue))
        self.set_child(box)
        self.update(MachineState(), reload is not None)

    def update(self, state: MachineState, reload_pending: bool = False) -> None:
        """Show the live machine state; Continue is enabled only when Ready, and with
        ``reload_pending`` (the mat was not unloaded since the last run) not even then."""
        ready = state.ready and not reload_pending
        self._spinner.set_visible(not ready)
        self._ok.set_visible(ready)
        stuck = machine_not_loadable(state)
        heading = stuck[0] if stuck is not None else LOAD_TITLE
        self._heading.set_text(heading)
        self.set_title(heading)
        if stuck is not None:
            self._text.set_markup(stuck[2])
        else:
            self._text.set_markup(self._reload if reload_pending and self._reload else self._load)
        if stuck is not None:
            self._state.set_text(stuck[1])
            self._detail.set_text("Waiting for the machine")
        elif ready:
            self._state.set_text("Ready — the mat is loaded")
            self._detail.set_text("If you loaded it before the last job, press Unload, then Load.")
        elif reload_pending and state.ready:
            self._state.set_text(self._reload_state)
            self._detail.set_text("Press Unload, then Load.")
        elif state.connected and state.status == state.machine_profile.status.load:
            self._state.set_text("Waiting for media…")
            self._detail.set_text("Changes to Ready by itself")
        else:
            self._state.set_text(state.status_text)
            self._detail.set_text("Waiting for the machine")
        self._continue.set_sensitive(ready)


@dataclass
class SendSummary:
    """What the Send sheet shows about the job."""
    document: str
    material: str
    action: str
    tool: str                  # 'Tool 1 · AutoBlade'
    settings: str              # 'Blade Depth 1 · Force 10 · Speed 5 · Accel. 3'
    passes: int
    preview_outline: bool
    estimated_seconds: float | None
    paths: int
    settings_tooltip: str | None = None     # 'Blade Depth 1 = 0.1 mm'
    machine: str = MACHINE_NAME             # the machine the job is made for
    notes: tuple[str, ...] = ()             # notes that block nothing (geometry.UNCHECKED_MAT)
    media: str = ""                         # the Media Size in use: 'Letter · 8.5 × 11 in'


class SendDialog(_AsyncDialog):
    """'Send to CAMEO 4': summary, thumbnail and a live checklist; Send only when all pass."""

    __gtype_name__ = "OpenCutterSendDialog"

    def __init__(self, summary: SendSummary, scene: Scene,
                 checks: Callable[[MachineState], list[tuple[str, bool]]]):
        """``checks(state)`` returns the checklist lines and whether each passes. The sheet is
        titled for ``summary.machine`` and shows ``summary.notes``, which block nothing."""
        super().__init__(title=f"Send to {summary.machine}", content_width=620)
        self._checks = checks
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.add_css_class("sheet")
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head.append(label(summary.document, "caption", "dim-label"))
        head.append(label(f"Send to {summary.machine}", "title-2"))
        box.append(head)

        card = Gtk.Box(spacing=20)
        card.add_css_class("summary-card")
        card.append(MatThumbnail(scene))
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, hexpand=True)
        props = PropertyList()
        props.add("Material", summary.material)
        props.add("Action", summary.action)
        if summary.media:
            props.add("Media", summary.media)
        props.add("Tool", summary.tool)
        settings = props.add("Settings", summary.settings)
        settings.set_tooltip_text("\n".join(filter(None, [summary.settings, summary.settings_tooltip])))
        props.add("Passes", str(summary.passes))
        props.add("Preview", "Outline first, tool up" if summary.preview_outline else "Off")
        right.append(props)
        kpis = Gtk.Box(spacing=32)
        kpis.append(_kpi("Estimated time", _estimate_text(summary.estimated_seconds)))
        kpis.append(_kpi("Paths", str(summary.paths)))
        right.append(kpis)
        card.append(right)
        box.append(card)

        box.append(label("Before the machine moves", "heading"))
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._list.add_css_class("checklist")
        box.append(self._list)
        self._notes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for text in summary.notes:
            self._notes.append(InlineNote(text))
        self._notes.set_visible(bool(summary.notes))
        box.append(self._notes)
        cancel = _button("Cancel")
        cancel.connect("clicked", lambda *_: self._answer(False))
        self._send = _button("Send", primary=True, icon="media-playback-start-symbolic")
        self._send.connect("clicked", lambda *_: self._answer(True))
        box.append(_footer(cancel, self._send, note="Send stays disabled while any check fails."))
        self.set_child(box)              # no default widget: a stray Enter must not start a job

    @property
    def notes(self) -> list[str]:
        """The notes shown (they never disable Send)."""
        out, child = [], self._notes.get_first_child()
        while child is not None:
            out.append(child.text)
            child = child.get_next_sibling()
        return out

    def update(self, state: MachineState) -> None:
        """Re-run the checklist against the latest machine state."""
        while (child := self._list.get_first_child()) is not None:
            self._list.remove(child)
        results = self._checks(state)
        for text, ok in results:
            row = Gtk.Box(spacing=10)
            icon = Gtk.Image(icon_name="object-select-symbolic" if ok else "dialog-error-symbolic")
            icon.add_css_class("success" if ok else "error")
            row.append(icon)
            row.append(label(text))
            row.update_property([Gtk.AccessibleProperty.LABEL], [f"{text}: {'passed' if ok else 'failed'}"])
            self._list.append(row)
        self._send.set_sensitive(all(ok for _text, ok in results))


def _kpi(name: str, value: str) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.append(label(name, "field-label"))
    box.append(label(value, "kpi-value", "numeric"))
    return box


def _estimate_text(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"about {format_duration(math.ceil(seconds))}"
