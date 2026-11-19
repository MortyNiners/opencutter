# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The opened design and its placement on the mat (pure Python, no GTK).

The design is kept as a *base*: rotated by the chosen quarter turns, unscaled, with its
bounding box at (0, 0). The placement adds a uniform scale and the position of the bounding
box's top-left corner, in document mm. Only move, uniform scale and 90° rotation exist: the
app is not a drawing program.
"""
from __future__ import annotations

import copy

from opencutter.svg.importer import Design, transform

from .geometry import Rect, clamp

MIN_SCALE = 0.01
MAX_SCALE = 100.0
FIT_MARGIN_MM = 1.0


def _at_origin(design: Design) -> Design:
    """The design moved so its bounding box starts at (0, 0)."""
    x, y = design.bbox[0], design.bbox[1]
    return transform(design, dx=-x, dy=-y) if (x or y) else design


class PlacedDocument:
    """A loaded design plus its placement (position, uniform scale, quarter turns)."""

    def __init__(self, design: Design, media: Rect):
        """Place ``design`` where it sits on its SVG page, the page being the media."""
        self.source_name = design.source_name
        self.warnings = list(design.warnings)
        self._unrotated = _at_origin(design)
        self.base = self._unrotated
        self.quarter_turns = 0
        self.scale = 1.0
        self.x = media[0] + design.bbox[0]
        self.y = media[1] + design.bbox[1]
        self._initial = self._placement()

    # --- reading ---------------------------------------------------------------------
    @property
    def path_count(self) -> int:
        """Number of paths in the design."""
        return len(self.base.paths)

    @property
    def is_empty(self) -> bool:
        """True if there is nothing to cut."""
        return not any(len(p.points) >= 2 for p in self.base.paths)

    @property
    def size(self) -> tuple[float, float]:
        """Placed width and height in mm."""
        return self.base.bbox[2] * self.scale, self.base.bbox[3] * self.scale

    def bbox(self) -> Rect:
        """Placed bounding box in document mm."""
        w, h = self.size
        return (self.x, self.y, w, h)

    @property
    def modified(self) -> bool:
        """True once the design was moved, scaled or rotated since it was opened."""
        return self._placement() != self._initial

    def placed(self) -> Design:
        """The design with the placement applied, in document mm (for the job spec)."""
        # transform() scales about the bbox top-left, which is (0, 0) for the base
        return transform(self.base, scale=self.scale, dx=self.x, dy=self.y)

    def polylines(self) -> list[list[tuple[float, float]]]:
        """Placed paths as polylines; closed paths end on their first point."""
        lines = []
        for path in self.placed().paths:
            points = [(float(x), float(y)) for x, y in path.points]
            if len(points) < 2:
                continue
            if path.closed and points[0] != points[-1]:
                points.append(points[0])
            lines.append(points)
        return lines

    def snapshot(self) -> "PlacedDocument":
        """An independent copy of the current placement (e.g. for the Send sheet).

        Placing never changes a Design in place (``transform`` is pure; the methods below
        only rebind attributes), so a shallow copy is enough and stays cheap for big designs."""
        return copy.copy(self)

    # --- placement -------------------------------------------------------------------
    def move_to(self, x: float, y: float) -> None:
        """Put the bounding box's top-left corner at (x, y)."""
        self.x, self.y = x, y

    def nudge(self, dx: float, dy: float) -> None:
        """Move by (dx, dy) mm."""
        self.x += dx
        self.y += dy

    def set_scale(self, scale: float) -> None:
        """Set the uniform scale (the top-left corner stays put)."""
        self.scale = clamp(scale, MIN_SCALE, MAX_SCALE)

    def set_width(self, width: float) -> None:
        """Scale uniformly so the placed width becomes ``width`` mm."""
        if self.base.bbox[2] > 0 and width > 0:
            self.set_scale(width / self.base.bbox[2])

    def set_height(self, height: float) -> None:
        """Scale uniformly so the placed height becomes ``height`` mm."""
        if self.base.bbox[3] > 0 and height > 0:
            self.set_scale(height / self.base.bbox[3])

    def rotate_quarter(self) -> None:
        """Rotate by 90° about the centre of the bounding box."""
        w, h = self.size
        cx, cy = self.x + w / 2, self.y + h / 2
        self.quarter_turns = (self.quarter_turns + 1) % 4
        # transform() rotates clockwise and keeps the bbox top-left, (0, 0), in place
        self.base = transform(self._unrotated, rotate90=self.quarter_turns)
        w, h = self.size
        self.x, self.y = cx - w / 2, cy - h / 2

    def fit_into(self, area: Rect, margin: float = FIT_MARGIN_MM) -> None:
        """Shrink the design if it is too big for ``area``, then move it inside."""
        room_w, room_h = area[2] - 2 * margin, area[3] - 2 * margin
        w, h = self.size
        if w > room_w or h > room_h:
            shrink = min(room_w / w if w else 1.0, room_h / h if h else 1.0)
            self.set_scale(self.scale * shrink)
            w, h = self.size
        self.x = clamp(self.x, area[0] + margin, max(area[0] + margin, area[0] + area[2] - margin - w))
        self.y = clamp(self.y, area[1] + margin, max(area[1] + margin, area[1] + area[3] - margin - h))

    def _placement(self) -> tuple[float, float, float, int]:
        return (round(self.x, 4), round(self.y, 4), round(self.scale, 6), self.quarter_turns)
