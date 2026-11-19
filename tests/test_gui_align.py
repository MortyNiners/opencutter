# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Align: Center to Page and the edge alignments as pure geometry (no display needed).

The edges are those of the usable area (the media inside the mat's cut area), the centres those
of the page (the media) kept inside that area, so an aligned design that fits always passes the
Send check ``design_fits``, to the last float step.
"""
import random

import pytest

import stubs

stubs.install()
from opencutter.gui.document import PlacedDocument  # noqa: E402
from opencutter.gui.geometry import (ALIGNMENTS, align_rect, cut_area, design_fits,  # noqa: E402
                                     media_rect, usable_area)
from opencutter.svg.importer import load_svg  # noqa: E402

IN = 25.4
MAT = media_rect("12x12", False, (0, 0))
SHEET = media_rect("custom", False, (12 * IN, 5 * IN))          # the user's 12 x 5 in vinyl sheet
LETTER_LANDSCAPE = media_rect("letter", True, (0, 0))
DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 100 100"
  fill="none" stroke="#000"><rect x="20" y="30" width="40" height="20"/><circle cx="80" cy="40" r="5"/></svg>"""


def centre(rect):
    x, y, w, h = rect
    return x + w / 2, y + h / 2


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "design.svg"
    path.write_text(DESIGN)
    return PlacedDocument(load_svg(str(path)), MAT)


def test_menu_terms_and_order():
    assert [label for _key, label in ALIGNMENTS] == [
        "Center to Page", "Align Left", "Align Center", "Align Right",
        "Align Top", "Align Middle", "Align Bottom"]
    assert len({key for key, _label in ALIGNMENTS}) == 7


def test_center_to_page_on_the_default_mat():
    area = usable_area(MAT)
    assert area == cut_area()                     # the 12 x 12 in media reaches past the cut area
    x, y = align_rect((10.0, 20.0, 50.8, 25.4), area, "center-page")
    assert centre((x, y, 50.8, 25.4)) == pytest.approx((6 * IN, 6 * IN))
    assert design_fits((x, y, 50.8, 25.4), MAT)


@pytest.mark.parametrize("how, expected", [
    ("center-page", ((12 * IN - 40) / 2, (5 * IN - 20) / 2)),      # the sheet's own centre
    ("left", (1.5, 80.0)),
    ("center", ((12 * IN - 40) / 2, 80.0)),
    ("right", (303.3 - 40, 80.0)),
    ("top", (100.0, 1.5)),
    ("middle", (100.0, (5 * IN - 20) / 2)),
    ("bottom", (100.0, 5 * IN - 20)),
])
def test_each_alignment_on_a_custom_12_by_5_in_sheet(how, expected):
    area = usable_area(SHEET)
    assert area == pytest.approx((1.5, 1.5, 301.8, 5 * IN - 1.5))
    x, y = align_rect((100.0, 80.0, 40.0, 20.0), area, how, page=SHEET)
    assert (x, y) == pytest.approx(expected)
    assert design_fits((x, y, 40.0, 20.0), SHEET)


@pytest.mark.parametrize("media", [media_rect("letter", False, (0, 0)), media_rect("a4", False, (0, 0)),
                                   SHEET, LETTER_LANDSCAPE, MAT])
def test_center_to_page_centres_on_the_sheet_not_on_its_part_in_the_cut_area(media):
    """The sheet's edge lies inside the cut area's 1.5 mm margin: Studio's Center to Page is the
    page centre, not 0.75 mm right of and below it."""
    box = (0.0, 0.0, 100.0, 30.0)
    x, y = align_rect(box, usable_area(media), "center-page", page=media)
    assert centre((x, y, 100.0, 30.0)) == pytest.approx(centre(media))
    assert design_fits((x, y, 100.0, 30.0), media)


def test_a_centred_box_is_moved_just_inside_the_cut_area():
    """Centred on Letter, a box 214 mm wide would start 0.95 mm in, inside the cut area's margin:
    it moves to 1.5 mm, the nearest place where Send accepts it."""
    letter = media_rect("letter", False, (0, 0))
    area = usable_area(letter)                                  # x 1.5 .. 215.9 mm
    x, _y = align_rect((50.0, 50.0, 214.0, 30.0), area, "center", page=letter)
    assert x == 1.5 and design_fits((x, 50.0, 214.0, 30.0), letter)
    big = (0.0, 0.0, 300.0, 400.0)                              # larger than the area: centred
    assert centre((*align_rect(big, area, "center-page", page=letter), 300.0, 400.0)) == \
        pytest.approx(centre(letter))


def test_landscape_media():
    area = usable_area(LETTER_LANDSCAPE)              # 11 x 8.5 in, inside the cut area
    assert area == pytest.approx((1.5, 1.5, 11 * IN - 1.5, 8.5 * IN - 1.5))
    x, y = align_rect((200.0, 150.0, 60.0, 30.0), area, "center-page")
    assert centre((x, y, 60.0, 30.0)) == pytest.approx(centre(area))
    assert align_rect((200.0, 150.0, 60.0, 30.0), area, "right") == pytest.approx((11 * IN - 60, 150.0))
    assert align_rect((200.0, 150.0, 60.0, 30.0), area, "bottom") == pytest.approx((200.0, 8.5 * IN - 30))


def test_a_rotated_and_scaled_design_uses_its_placed_box(doc):
    doc.set_width(130)                         # 130 x 40 mm
    doc.rotate_quarter()                       # 40 x 130 mm
    assert doc.size == pytest.approx((40, 130))
    area = usable_area(SHEET)                  # 125.5 mm high: the rotated design is too tall
    x, y = align_rect(doc.bbox(), area, "center-page")
    doc.move_to(x, y)
    assert centre(doc.bbox()) == pytest.approx(centre(area))
    assert centre(doc.placed().bbox) == pytest.approx(centre(area))     # the paths moved with it
    assert not design_fits(doc.bbox(), SHEET)
    doc.set_height(100)                        # scaled down: it fits; Right moves x only
    doc.move_to(*align_rect(doc.bbox(), area, "middle"))
    doc.move_to(*align_rect(doc.bbox(), area, "right"))
    assert doc.bbox()[0] + doc.bbox()[2] == pytest.approx(303.3)
    assert design_fits(doc.bbox(), SHEET)


def test_a_design_larger_than_the_area_is_still_centred_and_put_against_the_edge():
    area = usable_area(SHEET)
    big = (7.0, 9.0, 400.0, 200.0)
    x, y = align_rect(big, area, "center-page")
    assert centre((x, y, 400.0, 200.0)) == pytest.approx(centre(area))
    assert align_rect(big, area, "left") == (1.5, 9.0)
    assert align_rect(big, area, "right") == pytest.approx((303.3 - 400, 9.0))
    assert align_rect(big, area, "top") == (7.0, 1.5)
    assert align_rect(big, area, "bottom") == pytest.approx((7.0, 5 * IN - 200))
    assert align_rect(big, area, "middle")[1] == pytest.approx(centre(area)[1] - 100)


def test_aligning_twice_gives_exactly_the_same_place():
    area = usable_area(SHEET)
    box = (12.345678, 98.7654321, 0.1 + 0.2, 123.456789 / 3)
    for how, _label in ALIGNMENTS:
        x, y = align_rect(box, area, how)
        assert align_rect((x, y, box[2], box[3]), area, how) == (x, y), how
    x, y = align_rect(box, area, "center-page")
    again = align_rect((x, y, box[2], box[3]), area, "center-page")
    assert again == (x, y)                     # no drift, not even in the last digit
    for how, _label in ALIGNMENTS:             # centred on the sheet: the same
        x, y = align_rect(box, area, how, page=SHEET)
        assert align_rect((x, y, box[2], box[3]), area, how, page=SHEET) == (x, y), how


def test_aligned_designs_that_fit_always_pass_the_send_check():
    """(a + b) - b can exceed a by one float step; the Send check compares exactly."""
    rng = random.Random(4)
    medias = [MAT, SHEET, LETTER_LANDSCAPE, media_rect("a4", False, (0, 0)),
              media_rect("custom", False, (123.456, 78.9))]
    for _ in range(400):
        media = rng.choice(medias)
        area = usable_area(media)
        w = rng.uniform(0.001, 1.0) * area[2]
        h = rng.uniform(0.001, 1.0) * area[3]
        if rng.random() < 0.1:
            w, h = area[2], area[3]            # exactly the area's size
        page = media if rng.random() < 0.5 else None          # as the window does, or the area
        x, y = align_rect((rng.uniform(-50, 400), rng.uniform(-50, 400), w, h), area, "center-page",
                          page=page)
        assert design_fits((x, y, w, h), media), (media, w, h)
        for how, _label in ALIGNMENTS:          # from there, every alignment keeps it inside
            moved = align_rect((x, y, w, h), area, how, page=page)
            assert design_fits((*moved, w, h), media), (media, w, h, how)
            assert moved[0] >= area[0] and moved[0] + w <= area[0] + area[2]
            assert moved[1] >= area[1] and moved[1] + h <= area[1] + area[3]


def test_unknown_alignment_is_refused():
    with pytest.raises(ValueError, match="unknown alignment"):
        align_rect((0, 0, 1, 1), usable_area(MAT), "diagonal")
