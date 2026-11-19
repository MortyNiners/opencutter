# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Curve flattening: every polyline stays within 0.05 mm of the true curve."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from svgelements import Matrix, Path as SvgPath

from opencutter.svg.flatten import flatten, largest_stretch
from opencutter.svg.importer import TOLERANCE_MM, load_svg

SVG_HEAD = '<svg xmlns="http://www.w3.org/2000/svg" width="200mm" height="200mm" viewBox="0 0 200 200">'
EPS = 2e-4  # svgelements converts mm with 0.0393701 in/mm: 0.54 ppm too large


def load_points(tmp_path: Path, body: str) -> list[tuple[float, float]]:
    path = tmp_path / "curve.svg"
    path.write_text(SVG_HEAD + body + "</svg>")
    (only,) = load_svg(str(path)).paths
    return only.points


def segment_distance(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def max_deviation(curve, points) -> float:
    """Largest distance from points sampled on the true curve to the polyline."""
    segments = list(zip(points, points[1:]))
    return max(min(segment_distance(p, a, b) for a, b in segments) for p in curve)


def sample(fn, n: int = 1500):
    return [fn(i / n) for i in range(n + 1)]


def perimeter(points) -> float:
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def test_circle_circumference_within_a_tenth_of_a_percent(tmp_path):
    r = 25.4
    points = load_points(tmp_path, f'<circle cx="100" cy="100" r="{r}"/>')
    assert perimeter(points) == pytest.approx(2 * math.pi * r, rel=1e-3)


@pytest.mark.parametrize("r", [0.5, 2, 12.7, 90])
def test_circle_deviation(tmp_path, r):
    points = load_points(tmp_path, f'<circle cx="100" cy="100" r="{r}"/>')
    for x, y in points:  # vertices lie on the circle
        assert math.hypot(x - 100, y - 100) == pytest.approx(r, abs=EPS)
    for a, b in zip(points, points[1:]):  # chords bulge in by at most the tolerance
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        assert r - math.hypot(mid[0] - 100, mid[1] - 100) <= TOLERANCE_MM + EPS


def test_half_circle_arc(tmp_path):
    points = load_points(tmp_path, '<path d="M 10 100 A 40 40 0 0 1 90 100"/>')
    assert points[0] == pytest.approx((10, 100), abs=EPS)
    assert points[-1] == pytest.approx((90, 100), abs=EPS)
    assert max(y for _, y in points) == pytest.approx(100, abs=EPS)  # sweep 1 passes above
    assert min(y for _, y in points) == pytest.approx(60, abs=TOLERANCE_MM)
    assert perimeter(points) == pytest.approx(math.pi * 40, rel=1e-3)


def test_rotated_ellipse(tmp_path):
    cx, cy, rx, ry, angle = 100, 100, 60, 20, math.radians(30)
    body = f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" transform="rotate(30 {cx} {cy})"/>'
    points = load_points(tmp_path, body)

    def ellipse(t):
        x, y = rx * math.cos(2 * math.pi * t), ry * math.sin(2 * math.pi * t)
        c, s = math.cos(angle), math.sin(angle)
        return (cx + x * c - y * s, cy + x * s + y * c)

    assert max_deviation(sample(ellipse), points) <= TOLERANCE_MM + EPS


def test_skewed_circle(tmp_path):
    points = load_points(tmp_path, '<g transform="skewX(30)"><circle cx="50" cy="100" r="20"/></g>')
    k = math.tan(math.radians(30))

    def skewed(t):
        x, y = 50 + 20 * math.cos(2 * math.pi * t), 100 + 20 * math.sin(2 * math.pi * t)
        return (x + k * y, y)

    assert max_deviation(sample(skewed), points) <= TOLERANCE_MM + EPS
    assert min(y for _, y in points) == pytest.approx(80, abs=TOLERANCE_MM)


def test_mirrored_arc_keeps_its_side(tmp_path):
    arc = '<path d="M -10 0 A 10 10 0 0 1 10 0"/>'  # passes above the chord before mirroring
    points = load_points(tmp_path, f'<g transform="translate(100 100) scale(1 -1)">{arc}</g>')
    assert min(y for _, y in points) == pytest.approx(100, abs=EPS)
    assert max(y for _, y in points) == pytest.approx(110, abs=TOLERANCE_MM)


def test_cubic_bezier(tmp_path):
    p0, p1, p2, p3 = (10, 10), (10, 180), (190, -60), (190, 100)
    points = load_points(tmp_path, '<path d="M 10 10 C 10 180 190 -60 190 100"/>')

    def cubic(t):
        s = 1 - t
        w = (s**3, 3 * s * s * t, 3 * s * t * t, t**3)
        return tuple(sum(wi * p[i] for wi, p in zip(w, (p0, p1, p2, p3))) for i in (0, 1))

    assert points[0] == pytest.approx(p0, abs=EPS) and points[-1] == pytest.approx(p3, abs=EPS)
    assert max_deviation(sample(cubic), points) <= TOLERANCE_MM + EPS


def test_quadratic_bezier(tmp_path):
    p0, p1, p2 = (10, 10), (100, 190), (190, 10)
    points = load_points(tmp_path, '<path d="M 10 10 Q 100 190 190 10"/>')

    def quadratic(t):
        s = 1 - t
        w = (s * s, 2 * s * t, t * t)
        return tuple(sum(wi * p[i] for wi, p in zip(w, (p0, p1, p2))) for i in (0, 1))

    assert max_deviation(sample(quadratic), points) <= TOLERANCE_MM + EPS


def test_scaled_curves_keep_the_tolerance_in_mm(tmp_path):
    # A tiny circle scaled up 50 times must be flattened finely enough for its final size.
    points = load_points(tmp_path, '<g transform="translate(100 100) scale(50)"><circle r="1"/></g>')
    for a, b in zip(points, points[1:]):
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        assert 50 - math.hypot(mid[0] - 100, mid[1] - 100) <= TOLERANCE_MM + EPS


def test_flatten_arcs_already_mirrored_by_svgelements():
    # Transformed segments carry a negated sweep with a left-handed basis; the arc must not flip.
    segments = (SvgPath("M -10 0 A 10 10 0 0 1 10 0") * Matrix("scale(1, -1)")).segments()
    ((points, closed),) = flatten(segments, 0.01)
    assert not closed
    assert points[0] == pytest.approx((-10, 0)) and points[-1] == pytest.approx((10, 0))
    assert max(y for _, y in points) == pytest.approx(10, abs=0.01)
    assert min(y for _, y in points) == pytest.approx(0)


@pytest.mark.parametrize(
    "u, v, expected",
    [
        ((1, 0), (0, 1), 1),
        ((math.cos(1), math.sin(1)), (-math.sin(1), math.cos(1)), 1),
        ((2, 0), (0, -3), 3),
        ((1, 0), (1, 1), math.sqrt((3 + math.sqrt(5)) / 2)),
    ],
)
def test_largest_stretch(u, v, expected):
    assert largest_stretch(*u, *v) == pytest.approx(expected)
