# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""transform(): pure placement by move, uniform scale and quarter turns."""
from __future__ import annotations

import copy

import pytest

from opencutter.svg.importer import Design, DesignPath, transform


def make_design() -> Design:
    """A 30 x 10 mm rectangle at (10, 20) plus an open line, as load_svg would return them."""
    rect = DesignPath([(10, 20), (40, 20), (40, 30), (10, 30), (10, 20)], True, "#ff0000", None, "Cut")
    line = DesignPath([(10, 20), (25, 25)], False, None, "#000000", None)
    return Design([rect, line], (10, 20, 30, 10), ["1 image ignored"], "box.svg")


def recomputed_bbox(design: Design) -> tuple[float, float, float, float]:
    xs = [x for p in design.paths for x, _ in p.points]
    ys = [y for p in design.paths for _, y in p.points]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def test_identity_returns_an_equal_copy():
    design = make_design()
    moved = transform(design)
    assert moved == design
    assert moved is not design
    assert moved.paths[0] is not design.paths[0]
    assert moved.warnings is not design.warnings


def test_input_is_not_changed():
    design = make_design()
    before = copy.deepcopy(design)
    transform(design, dx=5, dy=-3, scale=2, rotate90=1)
    assert design == before


def test_move():
    moved = transform(make_design(), dx=5, dy=-20)
    assert moved.bbox == pytest.approx((15, 0, 30, 10))
    expected = [(15, 0), (45, 0), (45, 10), (15, 10), (15, 0)]
    assert moved.paths[0].points == [pytest.approx(p) for p in expected]


def test_scale_about_the_top_left():
    scaled = transform(make_design(), scale=2)
    assert scaled.bbox == pytest.approx((10, 20, 60, 20))
    assert scaled.paths[1].points == [pytest.approx((10, 20)), pytest.approx((40, 30))]


@pytest.mark.parametrize(
    "turns, bbox, corner",
    [
        # where the rectangle's top-right corner (40, 20) ends up
        (1, (10, 20, 10, 30), (20, 50)),  # clockwise: top edge becomes the right edge
        (2, (10, 20, 30, 10), (10, 30)),
        (3, (10, 20, 10, 30), (10, 20)),  # counter-clockwise: top edge becomes the left edge
        (-1, (10, 20, 10, 30), (10, 20)),
        (4, (10, 20, 30, 10), (40, 20)),
    ],
)
def test_quarter_turns_keep_the_top_left(turns, bbox, corner):
    turned = transform(make_design(), rotate90=turns)
    assert turned.bbox == pytest.approx(bbox)
    assert recomputed_bbox(turned) == pytest.approx(bbox)
    assert turned.paths[0].points[1] == pytest.approx(corner)


def test_scale_turn_and_move_together():
    placed = transform(make_design(), dx=-10, dy=5, scale=0.5, rotate90=1)
    assert placed.bbox == pytest.approx((0, 25, 5, 15))
    assert recomputed_bbox(placed) == pytest.approx(placed.bbox)


def test_attributes_are_kept():
    turned = transform(make_design(), rotate90=3, scale=3)
    assert [(p.closed, p.stroke, p.fill, p.layer) for p in turned.paths] == [
        (True, "#ff0000", None, "Cut"),
        (False, None, "#000000", None),
    ]
    assert turned.warnings == ["1 image ignored"]
    assert turned.source_name == "box.svg"


@pytest.mark.parametrize("scale", [0, -1, float("nan"), float("inf")])
def test_bad_scale(scale):
    with pytest.raises(ValueError):
        transform(make_design(), scale=scale)


def test_rotation_must_be_whole_quarter_turns():
    with pytest.raises(TypeError):
        transform(make_design(), rotate90=0.5)
