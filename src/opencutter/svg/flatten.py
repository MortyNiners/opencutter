# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Flatten svgelements path segments into polylines within a maximum deviation.

The segment counts come from closed-form bounds, so no recursion and no sampling is needed:
- Bezier curves use Wang's formula: n >= sqrt(d(d-1)/8 * M / tol), where d is the degree and M
  the largest second difference of the control points.
- Elliptical arcs use the chord sagitta r(1 - cos(step/2)) <= tol, with r the largest semi-axis.
A segment never gets more than MAX_STEPS pieces, so absurd coordinates cannot stall the import.
"""
from __future__ import annotations

from collections.abc import Iterable
from math import acos, atan2, ceil, cos, hypot, pi, sin, sqrt

from svgelements import Arc, Close, CubicBezier, Line, Move, PathSegment, QuadraticBezier

Point = tuple[float, float]
MAX_STEPS = 10_000  # far above what any curve that fits on a cutting mat needs


def flatten(segments: Iterable[PathSegment], tolerance: float) -> list[tuple[list[Point], bool]]:
    """Split `segments` into subpaths and flatten every curve.

    Returns one `(points, closed)` pair per subpath, in the segments' own coordinates. `closed`
    is True when the subpath ends with a close command; its points then end with the first point
    again. The polyline stays within `tolerance` of the true curve.
    """
    subpaths: list[tuple[list[Point], bool]] = []
    points: list[Point] = []
    for seg in segments:
        if isinstance(seg, Move):
            if points:
                subpaths.append((points, False))
            points = [(seg.end.x, seg.end.y)]
            continue
        if not points:  # drawing right after a close, or a path without a leading move
            points = [(seg.start.x, seg.start.y)]
        if isinstance(seg, Close):
            points.append(points[0])
            subpaths.append((points, True))
            points = []
        elif isinstance(seg, Line):
            points.append((seg.end.x, seg.end.y))
        elif isinstance(seg, CubicBezier):
            points.extend(_cubic(seg, tolerance))
        elif isinstance(seg, QuadraticBezier):
            points.extend(_quadratic(seg, tolerance))
        elif isinstance(seg, Arc):
            points.extend(_arc(seg, tolerance))
    if points:
        subpaths.append((points, False))
    return subpaths


def _cubic(seg: CubicBezier, tol: float) -> list[Point]:
    """Points after the start of a cubic Bezier, ending exactly at its end."""
    x0, y0 = seg.start.x, seg.start.y
    x1, y1 = seg.control1.x, seg.control1.y
    x2, y2 = seg.control2.x, seg.control2.y
    x3, y3 = seg.end.x, seg.end.y
    m = max(hypot(x0 - 2 * x1 + x2, y0 - 2 * y1 + y2), hypot(x1 - 2 * x2 + x3, y1 - 2 * y2 + y3))
    n = _steps(sqrt(0.75 * m / tol))
    out = []
    for i in range(1, n):
        t = i / n
        s = 1 - t
        a, b, c, d = s * s * s, 3 * s * s * t, 3 * s * t * t, t * t * t
        out.append((a * x0 + b * x1 + c * x2 + d * x3, a * y0 + b * y1 + c * y2 + d * y3))
    out.append((x3, y3))
    return out


def _quadratic(seg: QuadraticBezier, tol: float) -> list[Point]:
    """Points after the start of a quadratic Bezier, ending exactly at its end."""
    x0, y0 = seg.start.x, seg.start.y
    x1, y1 = seg.control.x, seg.control.y
    x2, y2 = seg.end.x, seg.end.y
    m = hypot(x0 - 2 * x1 + x2, y0 - 2 * y1 + y2)
    n = _steps(sqrt(0.25 * m / tol))
    out = []
    for i in range(1, n):
        t = i / n
        s = 1 - t
        a, b, c = s * s, 2 * s * t, t * t
        out.append((a * x0 + b * x1 + c * x2, a * y0 + b * y1 + c * y2))
    out.append((x2, y2))
    return out


def _arc(seg: Arc, tol: float) -> list[Point]:
    """Points after the start of an elliptical arc, ending exactly at its end.

    The arc is c + u*cos(t) + v*sin(t) with u = prx - c and v = pry - c. This form stays exact
    under any affine transform (skew included), unlike svgelements' rx/ry/rotation view.
    """
    end = (seg.end.x, seg.end.y)
    cx, cy = seg.center.x, seg.center.y
    ux, uy = seg.prx.x - cx, seg.prx.y - cy
    vx, vy = seg.pry.x - cx, seg.pry.y - cy
    det = ux * vy - uy * vx
    if det == 0 or not seg.sweep:
        return [end]
    sx, sy = seg.start.x - cx, seg.start.y - cy
    # Solve start - c = u*cos(t0) + v*sin(t0).
    t0 = atan2((ux * sy - uy * sx) / det, (sx * vy - sy * vx) / det)
    # svgelements negates the sweep when a mirroring transform turns the u, v basis left-handed;
    # in this parametrization the direction of t does not change, so undo that.
    sweep = seg.sweep if det > 0 else -seg.sweep
    r = largest_stretch(ux, uy, vx, vy)  # the largest semi-axis
    step = 2 * acos(1 - tol / r) if tol < 2 * r else 2 * pi
    n = _steps(abs(sweep) / step) if step > 0 else MAX_STEPS  # step is 0 when r dwarfs tol
    out = []
    for i in range(1, n):
        t = t0 + sweep * i / n
        c, s = cos(t), sin(t)
        out.append((cx + ux * c + vx * s, cy + uy * c + vy * s))
    out.append(end)
    return out


def _steps(exact: float) -> int:
    """ceil(exact) clamped to 1..MAX_STEPS; inf and nan give MAX_STEPS."""
    if exact <= 1:
        return 1
    return ceil(exact) if exact < MAX_STEPS else MAX_STEPS


def largest_stretch(ux: float, uy: float, vx: float, vy: float) -> float:
    """Largest singular value of the 2x2 matrix with columns (ux, uy) and (vx, vy).

    It is the most any length can grow under that linear map.
    """
    f = ux * ux + uy * uy + vx * vx + vy * vy
    det = ux * vy - uy * vx
    return sqrt((f + sqrt(max(f * f - 4 * det * det, 0.0))) / 2)
