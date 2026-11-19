# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The cutting mat canvas: mat grid, media, cut area and the design, with placement by mouse.

``render()`` draws a ``Scene`` with ``Gtk.Snapshot`` and GSK paths (no cairo bindings needed)
and is shared by the main canvas and the thumbnail in the Send sheet. ``MatCanvas`` adds
zoom, pan, drag to move, corner handles for uniform scale and arrow-key nudging. The mat's
top edge is the one that goes into the machine.

The mat is the scene's (``Scene.mat`` of ``Scene.profile``, the CAMEO 4's 12 x 12 in mat by
default): its size, which need not be square (12 x 24 in), its printed inch grid
(``Mat.grid_in``, columns x rows; the whole inches of its size when it has none), its cut area
and the caption "CUTTING MAT <label>".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from gi.repository import Adw, Gdk, GObject, Graphene, Gsk, Gtk, Pango

from opencutter.machines import CAMEO4, MachineProfile, Mat
from opencutter.svg.importer import Design

from .document import PlacedDocument
from .geometry import MM_PER_IN, Rect, cut_area, design_fits, mat_size, profile_mat

# Drawing colours mirror the design tokens of style.css (a snapshot cannot read CSS variables).
_DARK = {"surface": "#0c0c0e", "raised": "#151518", "overlay": "#1c1c20", "line": "#26262b",
         "line_strong": "#3a3a41", "line_control": "#72727b", "ink": "#f4f4f5",
         "ink_muted": "#a6a6ae", "ink_subtle": "#86868f", "accent": "#00c2df",
         "negative": "#ff6b86", "warning": "#f5c04a", "shadow": "#000000"}
_LIGHT = {"surface": "#ffffff", "raised": "#f7f8fa", "overlay": "#ffffff", "line": "#e3e5e9",
          "line_strong": "#cfd3d9", "line_control": "#80868f", "ink": "#0c0d10",
          "ink_muted": "#4f545c", "ink_subtle": "#646973", "accent": "#00a3bc",
          "negative": "#c8203f", "warning": "#8f5b00", "shadow": "#101828"}

# Space around the grid, in pixels: rulers on the top and left, the caption below.
PAD_LEFT, PAD_TOP, PAD_RIGHT, PAD_BOTTOM = 26.0, 22.0, 14.0, 26.0
FEED_LABEL_SPACE = 26.0
FIT_MARGIN = 20.0
HANDLE = 8.0
SELECTION_PAD = 6.0
MIN_ZOOM, MAX_ZOOM = 0.25, 16.0
NUDGE_MM, NUDGE_BIG_MM = 0.1 * MM_PER_IN, MM_PER_IN
TEST_CUT_SIZE_MM = 10.0


def palette() -> dict[str, Gdk.RGBA]:
    """Colours for the current light or dark style."""
    colors = _DARK if Adw.StyleManager.get_default().get_dark() else _LIGHT
    result = {}
    for key, value in colors.items():
        rgba = Gdk.RGBA()
        rgba.parse(value)
        result[key] = rgba
    return result


def line_style(action: str) -> tuple[str, bool]:
    """(palette colour, dashed) for the design lines of an action, as in Studio."""
    if action == "Score":
        return "ink", True
    if action == "Sketch":
        return "ink_muted", False
    if action == "No Cut":
        return "line_strong", False
    return "negative", False


@dataclass
class Scene:
    """Everything the canvas shows."""
    media: Rect
    media_label: str = ""
    reveal_mat: bool = True
    doc: PlacedDocument | None = None
    action: str = "Cut"
    test_spot: tuple[float, float] | None = None
    locked: bool = False            # placement is locked while the machine moves
    mat: Mat = profile_mat()        # the layout's mat (the CAMEO 4's 12 x 12 in mat by default)
    profile: MachineProfile = CAMEO4    # the machine the mat belongs to

    @property
    def mat_size(self) -> tuple[float, float]:
        """(width x, length y) of the mat in mm."""
        return mat_size(self.mat, self.profile)

    @property
    def cut_area(self) -> Rect:
        """Where the tool can reach on the mat."""
        return cut_area(self.mat, self.profile)

    def fits(self, bbox: Rect) -> bool:
        """The validator's rule: inside the media and the mat's cut area."""
        return design_fits(bbox, self.media, self.mat, self.profile)


def mat_grid(mat: Mat, size_mm: tuple[float, float]) -> tuple[int, int]:
    """The printed inch grid (columns, rows): Mat.grid_in, or the whole inches of the mat."""
    if mat.grid_in is not None:
        return mat.grid_in
    return int(size_mm[0] / MM_PER_IN + 1e-6), int(size_mm[1] / MM_PER_IN + 1e-6)


def half_inch_lines(mat: Mat, size_mm: tuple[float, float]) -> tuple[int, int]:
    """How many half-inch grid lines are drawn across and down the mat (23 and 23 on the 12 x
    12 in mat): those of its printed grid (Mat.grid_in), else every one inside the mat, never
    at or past its edge."""
    inside = tuple(max(0, math.ceil(v / (MM_PER_IN / 2) - 1e-6) - 1) for v in size_mm)
    if mat.grid_in is None:
        return inside
    return (min(2 * mat.grid_in[0] - 1, inside[0]), min(2 * mat.grid_in[1] - 1, inside[1]))


@dataclass
class View:
    """Screen mapping: ``scale`` pixels per mm, (ox, oy) is the grid origin in pixels."""
    scale: float = 1.0
    ox: float = 0.0
    oy: float = 0.0

    def to_px(self, x: float, y: float) -> tuple[float, float]:
        """Document mm to widget pixels."""
        return self.ox + x * self.scale, self.oy + y * self.scale

    def to_mm(self, px: float, py: float) -> tuple[float, float]:
        """Widget pixels to document mm."""
        return (px - self.ox) / self.scale, (py - self.oy) / self.scale


def fit_view(width: float, height: float, scene: Scene, labels: bool = True) -> View:
    """The view that shows the whole mat (or only the media) centred in the area."""
    if scene.reveal_mat:
        x0_mm = y0_mm = 0.0
        w_mm, h_mm = scene.mat_size
        pad_l, pad_r = (PAD_LEFT, PAD_RIGHT) if labels else (4.0, 4.0)
        pad_t = PAD_TOP + FEED_LABEL_SPACE if labels else 4.0
        pad_b = PAD_BOTTOM if labels else 4.0
    else:
        x0_mm, y0_mm, w_mm, h_mm = scene.media
        pad_l = pad_r = pad_b = 8.0
        pad_t = 8.0 + (FEED_LABEL_SPACE if labels else 0.0)
    margin = FIT_MARGIN if labels else 4.0
    scale = max(0.05, min((width - 2 * margin - pad_l - pad_r) / w_mm,
                          (height - 2 * margin - pad_t - pad_b) / h_mm))
    used_w = w_mm * scale + pad_l + pad_r
    used_h = h_mm * scale + pad_t + pad_b
    return View(scale, (width - used_w) / 2 + pad_l - x0_mm * scale,
                (height - used_h) / 2 + pad_t - y0_mm * scale)


# --- painting helpers -----------------------------------------------------------------
def _rect(x: float, y: float, w: float, h: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, w, h)


def _with_alpha(color: Gdk.RGBA, alpha: float) -> Gdk.RGBA:
    out = color.copy()
    out.alpha = alpha
    return out


def _stroke(width: float, dash: list[float] | None = None, round_caps: bool = False) -> Gsk.Stroke:
    stroke = Gsk.Stroke.new(width)
    stroke.set_line_join(Gsk.LineJoin.ROUND)
    if round_caps:
        stroke.set_line_cap(Gsk.LineCap.ROUND)
    if dash:
        stroke.set_dash(dash)
    return stroke


def _rect_path(x: float, y: float, w: float, h: float) -> Gsk.Path:
    builder = Gsk.PathBuilder.new()
    builder.add_rect(_rect(x, y, w, h))
    return builder.to_path()


def _lines_path(segments: list[tuple[float, float, float, float]]) -> Gsk.Path:
    builder = Gsk.PathBuilder.new()
    for x0, y0, x1, y1 in segments:
        builder.move_to(x0, y0)
        builder.line_to(x1, y1)
    return builder.to_path()


def text(snapshot: Gtk.Snapshot, widget: Gtk.Widget, value: str, x: float, y: float,
         color: Gdk.RGBA, size: float = 11.0, bold: bool = False, align: float = 0.0,
         spacing: float = 0.0) -> None:
    """Draw ``value`` with its top at ``y``; ``align`` 0 = starts at x, 0.5 centred, 1 ends at x."""
    layout = widget.create_pango_layout(value)
    desc = Pango.FontDescription.from_string("Public Sans, sans-serif")
    desc.set_absolute_size(size * Pango.SCALE)
    if bold:
        desc.set_weight(Pango.Weight.SEMIBOLD)
    layout.set_font_description(desc)
    if spacing:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_letter_spacing_new(int(spacing * Pango.SCALE)))
        layout.set_attributes(attrs)
    width = layout.get_pixel_size()[0]
    snapshot.save()
    snapshot.translate(Graphene.Point().init(round(x - width * align), round(y)))
    snapshot.append_layout(layout, color)
    snapshot.restore()


_PATHS: dict[int, tuple[Design, Gsk.Path]] = {}


def design_path(design: Design) -> Gsk.Path:
    """The design's polylines as one GSK path in its own mm units (cached per design)."""
    cached = _PATHS.get(id(design))
    if cached is not None and cached[0] is design:
        return cached[1]
    builder = Gsk.PathBuilder.new()
    for path in design.paths:
        points = path.points
        if len(points) < 2:
            continue
        builder.move_to(*points[0])
        for x, y in points[1:]:
            builder.line_to(x, y)
        if path.closed:
            builder.close()
    result = builder.to_path()
    if len(_PATHS) > 8:
        _PATHS.clear()
    _PATHS[id(design)] = (design, result)
    return result


# --- drawing ---------------------------------------------------------------------------
def render(snapshot: Gtk.Snapshot, widget: Gtk.Widget, scene: Scene, view: View,
           labels: bool = True, selected: bool = True) -> None:
    """Draw the scene. ``labels`` adds rulers and captions; ``selected`` the selection box."""
    pal = palette()
    if scene.reveal_mat:
        _draw_mat(snapshot, widget, scene, view, pal, labels)
    _draw_media(snapshot, widget, scene, view, pal, labels)
    if scene.reveal_mat:
        _draw_cut_area(snapshot, widget, scene, view, pal, labels)
    if labels:
        _draw_origin(snapshot, widget, view, pal)
        _draw_feed_label(snapshot, widget, scene, view, pal)
    if scene.test_spot is not None and not scene.locked:
        x, y = view.to_px(*scene.test_spot)
        side = TEST_CUT_SIZE_MM * view.scale
        snapshot.append_stroke(_rect_path(x, y, side, side), _stroke(1, [2, 2]), pal["ink_subtle"])
    if scene.doc is not None:
        _draw_design(snapshot, scene, view, pal)
        if selected and not scene.locked:
            _draw_selection(snapshot, scene, view, pal)


def mat_caption(mat: Mat) -> str:
    """'CUTTING MAT 12 × 12 in': under the mat."""
    return f"CUTTING MAT {mat.label}"


def _draw_mat(snapshot: Gtk.Snapshot, widget: Gtk.Widget, scene: Scene, view: View, pal: dict,
              labels: bool) -> None:
    x0, y0 = view.to_px(0, 0)
    w_mm, h_mm = scene.mat_size
    width, height = w_mm * view.scale, h_mm * view.scale
    columns, rows = mat_grid(scene.mat, (w_mm, h_mm))
    pads = (PAD_LEFT, PAD_TOP, PAD_RIGHT, PAD_BOTTOM) if labels else (4.0, 4.0, 4.0, 4.0)
    panel = Gsk.RoundedRect()
    panel.init_from_rect(_rect(x0 - pads[0], y0 - pads[1], width + pads[0] + pads[2],
                               height + pads[1] + pads[3]), 6)
    snapshot.push_rounded_clip(panel)
    snapshot.append_color(pal["raised"], panel.bounds)
    snapshot.pop()
    snapshot.append_border(panel, [1, 1, 1, 1], [pal["line_strong"]] * 4)
    snapshot.append_color(pal["surface"], _rect(x0, y0, width, height))
    half = MM_PER_IN / 2 * view.scale
    across, down = half_inch_lines(scene.mat, (w_mm, h_mm))
    for key, odd in (("line", True), ("line_strong", False)):
        segments = []
        for i in range(1, max(across, down) + 1):
            if (i % 2 == 1) != odd:
                continue
            pos = round(i * half) + 0.5
            if i <= across:
                segments.append((x0 + pos, y0, x0 + pos, y0 + height))
            if i <= down:
                segments.append((x0, y0 + pos, x0 + width, y0 + pos))
        snapshot.append_stroke(_lines_path(segments), _stroke(1), pal[key])
    snapshot.append_stroke(_rect_path(x0 + 0.5, y0 + 0.5, width - 1, height - 1), _stroke(1),
                           pal["line_strong"])
    if not labels:
        return
    inch = MM_PER_IN * view.scale
    step = 1 if inch >= 18 else 2 if inch >= 9 else 4
    for i in range(0, columns + 1, step):
        text(snapshot, widget, str(i), x0 + i * inch, y0 - 18, pal["ink_muted"], align=0.5 if i else 0.0)
    for i in range(step, rows + 1, step):
        text(snapshot, widget, str(i), x0 - 6, y0 + i * inch - 8, pal["ink_muted"], align=1.0)
    text(snapshot, widget, mat_caption(scene.mat), x0 + width / 2, y0 + height + 6,
         pal["ink_subtle"], align=0.5, spacing=0.9)


def _draw_media(snapshot: Gtk.Snapshot, widget: Gtk.Widget, scene: Scene, view: View, pal: dict,
                labels: bool) -> None:
    x, y = view.to_px(scene.media[0], scene.media[1])
    w, h = scene.media[2] * view.scale, scene.media[3] * view.scale
    sheet = Gsk.RoundedRect()
    sheet.init_from_rect(_rect(x, y, w, h), 0)
    snapshot.append_outset_shadow(sheet, _with_alpha(pal["shadow"], 0.35), 0, 4, 0, 16)
    snapshot.append_color(pal["overlay"], sheet.bounds)
    snapshot.append_border(sheet, [1, 1, 1, 1], [pal["line_strong"]] * 4)
    if labels and scene.media_label and w > 120:
        top = y + h - 20
        if scene.reveal_mat:        # the 'Cut area' caption sits in the same corner: one line up
            ax, ay, aw, ah = scene.cut_area
            right, bottom = view.to_px(ax + aw, ay + ah)
            if abs(y + h - bottom) < 18 and x + w > right - 80:
                top -= 16
        text(snapshot, widget, scene.media_label, x + w - 8, top, pal["ink_subtle"], align=1.0)


def _draw_cut_area(snapshot: Gtk.Snapshot, widget: Gtk.Widget, scene: Scene, view: View,
                   pal: dict, labels: bool) -> None:
    ax, ay, aw, ah = scene.cut_area
    x, y = view.to_px(ax, ay)
    w, h = round(aw * view.scale) - 1, round(ah * view.scale) - 1
    snapshot.append_stroke(_rect_path(round(x) + 0.5, round(y) + 0.5, w, h), _stroke(1, [4, 3]),
                           pal["line_control"])
    if labels:
        text(snapshot, widget, "Cut area", x + w - 6, y + h - 20, pal["ink_subtle"], align=1.0)


def _draw_origin(snapshot: Gtk.Snapshot, widget: Gtk.Widget, view: View, pal: dict) -> None:
    x, y = view.to_px(0, 0)
    builder = Gsk.PathBuilder.new()
    builder.move_to(x, y)
    builder.line_to(x + 12, y)
    builder.line_to(x, y + 12)
    builder.close()
    snapshot.append_fill(builder.to_path(), Gsk.FillRule.WINDING, pal["ink"])
    text(snapshot, widget, "Origin", x + 14, y + 1, pal["ink"], bold=True)


def _draw_feed_label(snapshot: Gtk.Snapshot, widget: Gtk.Widget, scene: Scene, view: View,
                     pal: dict) -> None:
    if scene.reveal_mat:
        cx = view.to_px(scene.mat_size[0] / 2, 0)[0]
        top = view.to_px(0, 0)[1] - PAD_TOP - FEED_LABEL_SPACE + 4
    else:
        cx = view.to_px(scene.media[0] + scene.media[2] / 2, 0)[0]
        top = view.to_px(0, scene.media[1])[1] - 8 - FEED_LABEL_SPACE + 4
    ax = cx - 59
    arrow = _lines_path([(ax + 5, top + 15, ax + 5, top + 3), (ax + 1, top + 7, ax + 5, top + 3),
                         (ax + 5, top + 3, ax + 9, top + 7)])
    snapshot.append_stroke(arrow, _stroke(1.5, round_caps=True), pal["ink_muted"])
    text(snapshot, widget, "Feeds into machine", ax + 15, top + 1, pal["ink_muted"], size=12, bold=True)


def _draw_design(snapshot: Gtk.Snapshot, scene: Scene, view: View, pal: dict) -> None:
    doc = scene.doc
    key, dashed = line_style(scene.action)
    zoom = doc.scale * view.scale              # pixels per base-design mm
    snapshot.save()
    snapshot.translate(Graphene.Point().init(*view.to_px(doc.x, doc.y)))
    snapshot.scale(zoom, zoom)
    stroke = _stroke(1.5 / zoom, [5 / zoom, 3 / zoom] if dashed else None)
    snapshot.append_stroke(design_path(doc.base), stroke, pal[key])
    snapshot.restore()


def _draw_selection(snapshot: Gtk.Snapshot, scene: Scene, view: View, pal: dict) -> None:
    x, y, w, h = _selection_px(scene.doc, view)
    color = pal["accent"] if scene.fits(scene.doc.bbox()) else pal["warning"]
    snapshot.append_stroke(_rect_path(round(x) + 0.5, round(y) + 0.5, round(w), round(h)), _stroke(1), color)
    for hx, hy in _corners(x, y, w, h).values():
        handle = _rect(round(hx - HANDLE / 2) + 0.5, round(hy - HANDLE / 2) + 0.5, HANDLE, HANDLE)
        snapshot.append_color(pal["surface"], handle)
        snapshot.append_stroke(_rect_path(handle.get_x(), handle.get_y(), HANDLE, HANDLE), _stroke(1), color)


def _selection_px(doc: PlacedDocument, view: View) -> Rect:
    x, y, w, h = doc.bbox()
    px, py = view.to_px(x, y)
    return (px - SELECTION_PAD, py - SELECTION_PAD, w * view.scale + 2 * SELECTION_PAD,
            h * view.scale + 2 * SELECTION_PAD)


def _corners(x: float, y: float, w: float, h: float) -> dict[str, tuple[float, float]]:
    return {"nw": (x, y), "ne": (x + w, y), "sw": (x, y + h), "se": (x + w, y + h)}


# --- widgets ---------------------------------------------------------------------------
class MatThumbnail(Gtk.Widget):
    """A small, non-interactive picture of a scene (for the Send sheet)."""

    __gtype_name__ = "OpenCutterMatThumbnail"

    def __init__(self, scene: Scene, size: int = 180):
        """Show ``scene`` in a ``size`` × ``size`` square."""
        super().__init__(valign=Gtk.Align.START)
        self.scene = scene
        self.set_size_request(size, size)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["The design on the mat"])

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        """Draw the scene without labels or selection."""
        view = fit_view(self.get_width(), self.get_height(), self.scene, labels=False)
        render(snapshot, self, self.scene, view, labels=False, selected=False)


class MatCanvas(Gtk.Widget):
    """The interactive mat. Emits ``placement-changed`` while the design is moved or scaled,
    ``cursor-moved`` with the pointer in mm (NaN when it leaves) and ``zoom-changed``.

    The arrow keys nudge the design while the canvas has the focus, except after
    ``park_focus()`` (the window parks the focus here after a job, so that a stray Return or
    Space reaches no button): then they do nothing until the canvas is clicked or the focus
    left it and came back."""

    __gtype_name__ = "OpenCutterMatCanvas"
    __gsignals__ = {
        "placement-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "cursor-moved": (GObject.SignalFlags.RUN_FIRST, None, (float, float)),
        "zoom-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, scene: Scene):
        """Create a canvas showing ``scene`` (the window keeps it up to date)."""
        super().__init__(hexpand=True, vexpand=True, focusable=True, overflow=Gtk.Overflow.HIDDEN)
        self.scene = scene
        self.view = View()
        self.zoom = 1.0
        self._fit = True                # follow the widget size until the user zooms or pans
        self._drag: tuple | None = None
        self._pinch_from = 1.0
        self.armed = True               # the arrow keys may nudge the design (park_focus)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Cutting mat"])
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.queue_draw())

        drag = Gtk.GestureDrag(button=0)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *_: self.emit("cursor-moved", math.nan, math.nan))
        self.add_controller(motion)
        pinch = Gtk.GestureZoom()
        pinch.connect("begin", self._on_pinch_begin)
        pinch.connect("scale-changed", self._on_pinch)
        self.add_controller(pinch)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: setattr(self, "armed", True))
        self.add_controller(focus)

    def park_focus(self) -> bool:
        """Take the keyboard focus without arming the arrow keys (they do nothing until the
        canvas is clicked, or the focus left it and came back). True when it took the focus."""
        self.armed = False
        return self.grab_focus()

    # --- zoom ------------------------------------------------------------------------
    def fit(self) -> None:
        """Zoom to show the whole mat (or the media when the mat is hidden)."""
        self._fit = True
        self.zoom = 1.0
        self.queue_draw()
        self.emit("zoom-changed")

    def zoom_by(self, factor: float, around: tuple[float, float] | None = None) -> None:
        """Zoom in (factor > 1) or out around a widget point (the centre by default)."""
        base = fit_view(self.get_width(), self.get_height(), self.scene)
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom * factor))
        px, py = around or (self.get_width() / 2, self.get_height() / 2)
        mx, my = self.view.to_mm(px, py)
        scale = base.scale * zoom
        self.view = View(scale, px - mx * scale, py - my * scale)
        self.zoom, self._fit = zoom, False
        self.queue_draw()
        self.emit("zoom-changed")

    # --- drawing ---------------------------------------------------------------------
    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        """Draw the scene (refitting first while the canvas follows its size)."""
        if self._fit:
            self.view = fit_view(self.get_width(), self.get_height(), self.scene)
        render(snapshot, self, self.scene, self.view)

    # --- pointer ---------------------------------------------------------------------
    def _hit(self, px: float, py: float) -> str | None:
        """'nw'/'ne'/'sw'/'se' for a corner handle, 'move' inside the design, else None."""
        doc = self.scene.doc
        if doc is None or self.scene.locked:
            return None
        x, y, w, h = _selection_px(doc, self.view)
        for name, (hx, hy) in _corners(x, y, w, h).items():
            if abs(px - hx) <= HANDLE and abs(py - hy) <= HANDLE:
                return name
        return "move" if x <= px <= x + w and y <= py <= y + h else None

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, px: float, py: float) -> None:
        self.armed = True
        self.grab_focus()
        hit = self._hit(px, py) if gesture.get_current_button() == 1 else None
        doc = self.scene.doc
        if hit == "move":
            self._drag = ("move", doc.x, doc.y)
        elif hit is not None:
            x, y, w, h = doc.bbox()
            anchor = {"nw": (x + w, y + h), "ne": (x, y + h), "sw": (x + w, y), "se": (x, y)}[hit]
            self._drag = ("scale", hit, anchor, w, h, doc.scale)
        else:
            self._drag = ("pan", self.view.ox, self.view.oy)
        self.set_cursor_from_name("grabbing" if hit in (None, "move") else f"{hit}-resize")

    def _on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._drag is None:
            return
        kind, doc, s = self._drag[0], self.scene.doc, self.view.scale
        if kind == "pan":
            self.view = View(s, self._drag[1] + dx, self._drag[2] + dy)
            self._fit = False
        elif kind == "move":
            doc.move_to(self._drag[1] + dx / s, self._drag[2] + dy / s)
            self.emit("placement-changed")
        else:
            _kind, corner, (ax, ay), w0, h0, scale0 = self._drag
            _ok, sx, sy = gesture.get_start_point()
            mx, my = self.view.to_mm(sx + dx, sy + dy)
            factor = max(abs(mx - ax) / w0 if w0 else 0.0, abs(my - ay) / h0 if h0 else 0.0)
            doc.set_scale(scale0 * max(factor, 0.01))
            w, h = doc.size
            doc.move_to(ax - w if corner in ("nw", "sw") else ax, ay - h if corner in ("nw", "ne") else ay)
            self.emit("placement-changed")
        self.queue_draw()

    def _on_drag_end(self, *_args) -> None:
        self._drag = None
        self.set_cursor(None)

    def _on_motion(self, _ctl, px: float, py: float) -> None:
        mx, my = self.view.to_mm(px, py)
        self.emit("cursor-moved", mx, my)
        if self._drag is None:
            hit = self._hit(px, py)
            self.set_cursor_from_name(None if hit is None else "move" if hit == "move" else f"{hit}-resize")

    def _on_scroll(self, ctl: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        event = ctl.get_current_event()
        ok, px, py = event.get_position() if event else (False, 0, 0)
        if dy:
            self.zoom_by(1.15 ** (-dy), (px, py) if ok else None)
        return True

    def _on_pinch_begin(self, *_args) -> None:
        self._pinch_from = self.zoom

    def _on_pinch(self, gesture: Gtk.GestureZoom, scale: float) -> None:
        ok, px, py = gesture.get_bounding_box_center()
        self.zoom_by(self._pinch_from * scale / self.zoom, (px, py) if ok else None)

    def _on_key(self, _ctl, keyval: int, _code: int, state: Gdk.ModifierType) -> bool:
        doc = self.scene.doc
        steps = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0), Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1)}
        if keyval not in steps:
            return False
        if not self.armed:
            # parked here after a job: nothing moves, and the key does not move the focus to
            # a button either (where a stray Return would press it), with or without a design
            return True
        if doc is None or self.scene.locked:
            return False
        step = NUDGE_BIG_MM if state & Gdk.ModifierType.SHIFT_MASK else NUDGE_MM
        dx, dy = steps[keyval]
        doc.nudge(dx * step, dy * step)
        self.emit("placement-changed")
        self.queue_draw()
        return True
