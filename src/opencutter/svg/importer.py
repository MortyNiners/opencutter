# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Load an SVG design as polylines in millimetres, and place it on the mat.

Document coordinates: x to the right, y down, origin at the SVG's top-left corner, in mm.
User units follow the SVG rules (1 px = 1/96 in) through width, height and viewBox.
"""
from __future__ import annotations

import io
import operator
import os
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, replace
from math import hypot, inf, isfinite
from xml.etree.ElementTree import ParseError

from svgelements import SVG, SVG_STRUCT_ATTRIB, Color, Group, Image, Shape, SVGElement, Text, Use

from .flatten import flatten, largest_stretch
from .prepare import prepare

__all__ = ["Design", "DesignPath", "SvgImportError", "TOLERANCE_MM", "load_svg", "transform"]

TOLERANCE_MM = 0.05  # the most a flattened curve may deviate from the true curve
_PX_PER_IN = 96.0
_MM_PER_PX = 25.4 / _PX_PER_IN
_SAME_MM = 1e-6  # consecutive points closer than this are merged
_CLOSE_MM = 1e-3  # an open path that ends this close to its start is closed
_MAX_SVGZ_BYTES = 256 * 1024 * 1024
_INKSCAPE = "{http://www.inkscape.org/namespaces/inkscape}"

Point = tuple[float, float]


class SvgImportError(Exception):
    """The file cannot be imported. The message is written for the user."""


@dataclass
class DesignPath:
    """One polyline in document mm. Curves are flattened to within `TOLERANCE_MM`.

    A closed path repeats its first point at the end.
    """

    points: list[Point]
    closed: bool
    stroke: str | None  # '#rrggbb', or None for no paint (none, transparent, gradient, pattern)
    fill: str | None
    layer: str | None  # label of the innermost Inkscape layer, if any


@dataclass
class Design:
    """An imported design: its paths, their bounding box (x, y, w, h in mm) and warnings."""

    paths: list[DesignPath]
    bbox: tuple[float, float, float, float]
    warnings: list[str]
    source_name: str


def load_svg(path: str | os.PathLike[str]) -> Design:
    """Read an SVG or SVGZ file.

    Hidden elements and degenerate paths are skipped. Text, images and clips cannot be cut as
    drawn, so they produce warnings. Raises `SvgImportError`, with a message for the user, when
    the file cannot be read or has nothing to cut.
    """
    name = os.path.basename(path)
    try:
        data = prepare(_read(path, name))
    except ParseError as exc:
        message = f"Could not read {name}: the file is damaged or not an SVG ({exc})."
        raise SvgImportError(message) from exc
    except ValueError as exc:
        raise SvgImportError(f"Could not read {name}: it is not an SVG file.") from exc
    try:
        doc = SVG.parse(io.BytesIO(data), reify=False, ppi=_PX_PER_IN)
    except Exception as exc:  # svgelements has no error type of its own: any failure is bad input
        raise SvgImportError(f"Could not read {name}: {exc}") from exc

    found = _Collector()
    if isinstance(doc, SVG):  # None when the whole document is display:none
        found.walk(doc)
    warnings = found.warnings()
    if not found.paths:
        message = f"Nothing to cut in {name}: it has no visible paths or shapes."
        raise SvgImportError(" ".join([message, *warnings]))
    return Design(found.paths, _bbox(found.paths), warnings, name)


def transform(
    design: Design, *, dx: float = 0.0, dy: float = 0.0, scale: float = 1.0, rotate90: int = 0
) -> Design:
    """Return a scaled, rotated and moved copy of `design`. The input is not changed.

    `scale` is uniform about the bbox top-left. `rotate90` turns the design in 90 degree steps,
    clockwise on screen for positive values, keeping the bbox top-left where it was. Then
    everything moves by (`dx`, `dy`) mm.
    """
    if not (isfinite(scale) and scale > 0):
        raise ValueError("scale must be a positive number")
    turns = operator.index(rotate90) % 4
    x0, y0, w, h = design.bbox
    s, sw, sh = scale, w * scale, h * scale
    ox, oy = x0 + dx, y0 + dy
    # x' = a*x + b*y + c, y' = d*x + e*y + f; with u = s*(x - x0) and v = s*(y - y0) the four turns
    # give (u, v), (sh - v, u), (sw - u, sh - v) and (v, sw - u) from the new top-left (ox, oy).
    a, b, c, d, e, f = (
        (s, 0.0, ox - s * x0, 0.0, s, oy - s * y0),
        (0.0, -s, ox + sh + s * y0, s, 0.0, oy - s * x0),
        (-s, 0.0, ox + sw + s * x0, 0.0, -s, oy + sh + s * y0),
        (0.0, s, ox - s * y0, -s, 0.0, oy + sw + s * x0),
    )[turns]
    paths = [
        replace(p, points=[(a * x + b * y + c, d * x + e * y + f) for x, y in p.points])
        for p in design.paths
    ]
    size = (sw, sh) if turns % 2 == 0 else (sh, sw)
    return Design(paths, (ox, oy, *size), list(design.warnings), design.source_name)


class _Collector:
    """Walks the parsed tree in document order and gathers paths and what was ignored."""

    def __init__(self) -> None:
        self.paths: list[DesignPath] = []
        self.texts = 0
        self.images = 0
        self.clipped = 0

    def walk(self, doc: SVG) -> None:
        stack: list[tuple[Iterator[SVGElement], str | None]] = [(iter(doc), None)]
        while stack:
            children, layer = stack[-1]
            el = next(children, None)
            if el is None:
                stack.pop()
            elif isinstance(el, (Group, Use)):
                self._count_clip(el)
                stack.append((iter(el), _layer_label(el) or layer))
            elif _hidden(el):
                pass
            elif isinstance(el, Shape):
                self._count_clip(el)
                self._add_shape(el, layer)
            elif isinstance(el, Text):
                if el.values.get("tag") == "text":  # its <tspan> parts are Text objects too
                    self.texts += 1
            elif isinstance(el, Image):
                self.images += 1

    def _count_clip(self, el: SVGElement) -> None:
        attrs = el.values.get(SVG_STRUCT_ATTRIB, {})
        if any(attrs.get(key, "none") != "none" for key in ("clip-path", "mask")):
            self.clipped += 1

    def _add_shape(self, el: Shape, layer: str | None) -> None:
        m = el.transform
        a, b, c, d, e, f = (_MM_PER_PX * v for v in (m.a, m.b, m.c, m.d, m.e, m.f))
        stretch = largest_stretch(a, b, c, d)
        if stretch == 0 or not isfinite(stretch):
            return  # collapsed to nothing, or scaled beyond the float range
        stroke = _paint(el.stroke, el.values.get("stroke"))
        fill = _paint(el.fill, el.values.get("fill"))
        # Flatten in the shape's own units with a tolerance scaled so that it holds in mm.
        for points, closed in flatten(el.segments(transformed=False), TOLERANCE_MM / stretch):
            mm = [(a * x + c * y + e, b * x + d * y + f) for x, y in points]
            if not isfinite(sum(map(sum, mm))):
                continue  # coordinates beyond the float range: nothing sensible to cut
            cleaned = _clean(mm, closed)
            if cleaned is not None:
                self.paths.append(DesignPath(cleaned[0], cleaned[1], stroke, fill, layer))

    def warnings(self) -> list[str]:
        out = []
        if self.texts:
            out.append(
                f"{_count(self.texts, 'text object')} ignored: "
                "convert text to paths in Inkscape (Path > Object to Path)."
            )
        if self.images:
            out.append(
                f"{_count(self.images, 'image')} ignored: "
                "trace images in Inkscape (Path > Trace Bitmap) to cut them."
            )
        if self.clipped:
            out.append(
                f"{_count(self.clipped, 'clipped or masked object')}: "
                "clips and masks are ignored, so the whole object is cut."
            )
        return out


def _read(path: str | os.PathLike[str], name: str) -> bytes:
    """The file's bytes, unpacked when it is gzip-compressed (.svgz)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise SvgImportError(f"Could not open {name}: {exc.strerror or exc}.") from exc
    if data[:2] != b"\x1f\x8b":
        return data
    unzip = zlib.decompressobj(wbits=31)
    try:
        data = unzip.decompress(data, _MAX_SVGZ_BYTES)
    except zlib.error as exc:
        raise SvgImportError(f"Could not read {name}: the compressed file is damaged.") from exc
    if unzip.unconsumed_tail:
        raise SvgImportError(f"Could not read {name}: the file is too large.")
    return data


def _hidden(el: SVGElement) -> bool:
    """True for visibility hidden or collapse; `values` carries the inherited value."""
    return el.values.get("visibility") in ("hidden", "collapse")


def _layer_label(el: SVGElement) -> str | None:
    attrs = el.values.get(SVG_STRUCT_ATTRIB, {})
    if attrs.get(_INKSCAPE + "groupmode") != "layer":
        return None
    return attrs.get(_INKSCAPE + "label") or attrs.get("id")


def _paint(color: Color | None, raw: str | None) -> str | None:
    """'#rrggbb' for a solid, visible paint; None otherwise."""
    if color is None or color.value is None or color.alpha == 0:
        return None
    if raw is not None and raw.lstrip().startswith("url("):
        return None  # gradient or pattern: svgelements would report it as black
    return f"#{color.red:02x}{color.green:02x}{color.blue:02x}"


def _clean(points: list[Point], closed: bool) -> tuple[list[Point], bool] | None:
    """Merge repeated points and detect closed outlines. None when nothing is left to cut."""
    out = [points[0]]
    for p in points[1:]:
        q = out[-1]
        if abs(p[0] - q[0]) > _SAME_MM or abs(p[1] - q[1]) > _SAME_MM:
            out.append(p)
    if len(out) < 2:
        return None
    if not closed and len(out) > 2:
        closed = hypot(out[-1][0] - out[0][0], out[-1][1] - out[0][1]) <= _CLOSE_MM
    if closed:
        out[-1] = out[0]
    return out, closed


def _bbox(paths: list[DesignPath]) -> tuple[float, float, float, float]:
    x0 = y0 = inf
    x1 = y1 = -inf
    for path in paths:
        xs, ys = zip(*path.points)
        x0, y0 = min(x0, min(xs)), min(y0, min(ys))
        x1, y1 = max(x1, max(xs)), max(y1, max(ys))
    return x0, y0, x1 - x0, y1 - y0


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"
