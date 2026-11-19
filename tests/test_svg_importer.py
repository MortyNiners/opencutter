# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""load_svg: units, viewBox, transforms, use, shapes, visibility, warnings, layers, colors."""
from __future__ import annotations

import gzip
import math
import random
import time
from pathlib import Path

import pytest

from opencutter.svg import importer
from opencutter.svg.importer import Design, DesignPath, SvgImportError, load_svg

DATA = Path(__file__).parent / "data"
SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'


def load_text(tmp_path: Path, text: str, name: str = "design.svg") -> Design:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return load_svg(str(path))


def extent(path: DesignPath) -> tuple[float, float, float, float]:
    xs = [x for x, _ in path.points]
    ys = [y for _, y in path.points]
    return min(xs), min(ys), max(xs), max(ys)


def assert_close(actual, expected, tol: float = 1e-4) -> None:
    assert len(actual) == len(expected)
    for a, e in zip(actual, expected):
        assert a == pytest.approx(e, abs=tol), (actual, expected)


# --- units and viewBox ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, bbox",
    [
        ("square_mm.svg", (10, 5, 25.4, 25.4)),
        ("square_px.svg", (12.7, 25.4, 25.4, 25.4)),
        ("square_in.svg", (12.7, 12.7, 25.4, 25.4)),
    ],
)
def test_one_inch_square_in_any_unit(name, bbox):
    design = load_svg(str(DATA / name))
    assert design.source_name == name
    assert design.warnings == []
    assert len(design.paths) == 1
    square = design.paths[0]
    assert square.closed
    assert len(square.points) == 5 and square.points[0] == square.points[-1]
    assert_close(design.bbox, bbox)


@pytest.mark.parametrize("size", ["1in", "25.4mm", "2.54cm", "72pt", "6pc", "96px", "96"])
def test_root_size_units(tmp_path, size):
    text = f'<svg {SVG_NS} width="{size}" height="{size}" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'
    assert_close(load_text(tmp_path, text).bbox, (0, 0, 25.4, 25.4))


def test_lengths_with_units_inside_the_document(tmp_path):
    text = f'<svg {SVG_NS} width="200" height="200"><rect x="10mm" y="0.5in" width="72pt" height="2.54cm"/></svg>'
    assert_close(load_text(tmp_path, text).bbox, (10, 12.7, 25.4, 25.4))


def test_missing_size_uses_the_viewbox_as_px(tmp_path):
    text = f'<svg {SVG_NS} viewBox="0 0 192 192"><rect x="96" y="0" width="96" height="96"/></svg>'
    assert_close(load_text(tmp_path, text).bbox, (25.4, 0, 25.4, 25.4))


def test_viewbox_origin_is_the_document_top_left(tmp_path):
    text = (
        f'<svg {SVG_NS} width="20mm" height="20mm" viewBox="-10 -10 20 20">'
        '<rect x="-10" y="-10" width="5" height="5"/></svg>'
    )
    assert_close(load_text(tmp_path, text).bbox, (0, 0, 5, 5))


@pytest.mark.parametrize(
    "aspect, bbox",
    [
        (None, (15, 0, 30, 30)),  # default: xMidYMid meet
        ("xMinYMin meet", (0, 0, 30, 30)),
        ("xMaxYMax", (30, 0, 30, 30)),
        ("none", (0, 0, 60, 30)),
        ("xMidYMid slice", (0, -15, 60, 60)),
    ],
)
def test_preserve_aspect_ratio(tmp_path, aspect, bbox):
    attr = f' preserveAspectRatio="{aspect}"' if aspect else ""
    text = (
        f'<svg {SVG_NS} width="60mm" height="30mm" viewBox="0 0 10 10"{attr}>'
        '<rect width="10" height="10"/></svg>'
    )
    assert_close(load_text(tmp_path, text).bbox, bbox)


# --- transforms, use, nested viewports ----------------------------------------------------


def test_nested_transforms():
    rect, line, path = load_svg(str(DATA / "transforms.svg")).paths
    assert_close(extent(rect), (12, 22, 22, 32))
    assert_close(line.points[0] + line.points[1], (10, 20, 10, 30))
    assert_close(path.points[0] + path.points[1], (40, 20, 50, 20))


def test_use_clones_and_symbols():
    paths = load_svg(str(DATA / "use.svg")).paths
    assert [p.closed for p in paths] == [True] * 6
    expected = [
        (5, 5, 15, 15),
        (52, 2, 72, 22),
        (20, 60, 40, 80),
        (0, 0, 100, 100),
        (80, 80, 90, 90),
        (80, 70, 90, 80),
    ]
    for path, box in zip(paths, expected, strict=True):
        assert_close(extent(path), box)
    for x, y in paths[2].points:  # the symbol's circle, scaled by its viewBox
        assert ((x - 30) ** 2 + (y - 70) ** 2) ** 0.5 == pytest.approx(10, abs=1e-3)


def test_nested_svg_viewports(tmp_path):
    text = (
        f'<svg {SVG_NS} width="100mm" height="100mm" viewBox="0 0 100 100">'
        '<svg x="50" y="50" width="10" height="10" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>'
        '<svg x="70" y="60"><rect width="3" height="3"/></svg>'
        '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'  # fills the page
        '<svg width="0"><rect width="10" height="10"/></svg>'  # empty viewports show nothing
        '<svg viewBox="0 0 0 10"><rect width="10" height="10"/></svg>'
        "</svg>"
    )
    first, second, third = load_text(tmp_path, text).paths
    assert_close(extent(first), (50, 50, 60, 60))
    assert_close(extent(second), (70, 60, 73, 63))
    assert_close(extent(third), (0, 0, 100, 100))


# --- shapes and paths ---------------------------------------------------------------------


def test_basic_shapes():
    design = load_svg(str(DATA / "shapes.svg"))
    rect, circle, ellipse, line, polyline, polygon, curves, arc = design.paths
    assert [p.closed for p in design.paths] == [True, True, True, False, False, True, True, False]
    assert_close(extent(rect), (10, 10, 40, 30))
    assert (10.0, 10.0) not in rect.points  # rounded corners
    assert_close(extent(circle), (55, 10, 85, 40), tol=0.05)
    assert_close(extent(ellipse), (10, 52, 40, 68), tol=0.05)
    assert_close(line.points[0] + line.points[1], (50, 50, 90, 50))
    assert_close([c for p in polyline.points for c in p], (10, 80, 20, 90, 30, 80))
    assert_close([c for p in polygon.points for c in p], (50, 70, 70, 70, 60, 90, 50, 70))
    assert len(curves.points) > 20
    assert_close(arc.points[0] + arc.points[-1], (100, 10, 115, 25))


def test_closed_detection_and_subpaths():
    paths = load_svg(str(DATA / "paths.svg")).paths
    assert [(p.closed, len(p.points)) for p in paths] == [
        (True, 4),
        (True, 4),
        (False, 3),
        (True, 5),
        (False, 2),
        (True, 4),
        (False, 2),
    ]
    for path in paths:
        if path.closed:
            assert path.points[0] == path.points[-1]
    assert_close(paths[-1].points[0] + paths[-1].points[1], (0, 20, 5, 25))


def test_degenerate_paths_are_skipped():
    paths = load_svg(str(DATA / "degenerate.svg")).paths
    assert len(paths) == 1
    assert_close(paths[0].points[0] + paths[0].points[1], (0, 0, 10, 0))


# --- visibility and ignored content -------------------------------------------------------


def test_hidden_elements_are_skipped():
    design = load_svg(str(DATA / "hidden.svg"))
    assert [extent(p)[:2] for p in design.paths] == [(0, 0), pytest.approx((40, 20), abs=1e-4)]
    assert design.warnings == []  # hidden text and images are not reported


def test_text_images_and_clips_give_warnings():
    design = load_svg(str(DATA / "ignored.svg"))
    assert len(design.paths) == 2  # the line and the clipped rect, not the flowed text frame
    assert design.warnings == [
        "3 text objects ignored: convert text to paths in Inkscape (Path > Object to Path).",
        "2 images ignored: trace images in Inkscape (Path > Trace Bitmap) to cut them.",
        "1 clipped or masked object: clips and masks are ignored, so the whole object is cut.",
    ]


def test_singular_warnings(tmp_path):
    text = (
        f'<svg {SVG_NS} xmlns:xlink="http://www.w3.org/1999/xlink" width="10mm" height="10mm">'
        '<path d="M0 0 H5"/><text>A</text><image width="1" height="1" xlink:href="a.png"/></svg>'
    )
    assert load_text(tmp_path, text).warnings == [
        "1 text object ignored: convert text to paths in Inkscape (Path > Object to Path).",
        "1 image ignored: trace images in Inkscape (Path > Trace Bitmap) to cut them.",
    ]


# --- layers and colors --------------------------------------------------------------------


def test_inkscape_layers():
    paths = load_svg(str(DATA / "layers.svg")).paths
    assert [p.layer for p in paths] == [None, "Cut", "Cut", "Score", "Cut", "layer3"]


def test_colors():
    paths = load_svg(str(DATA / "colors.svg")).paths
    assert [(p.stroke, p.fill) for p in paths] == [
        ("#0000ff", "#000000"),  # inherited stroke, default black fill
        ("#aabbcc", None),
        ("#ff8000", None),  # gradient fill
        (None, None),  # fully transparent fill
        ("#123456", "#ff0000"),  # currentColor; opacity does not change the color
        (None, "#00ff00"),
    ]


# --- files and errors ---------------------------------------------------------------------


def test_svgz(tmp_path):
    path = tmp_path / "square.svgz"
    path.write_bytes(gzip.compress((DATA / "square_mm.svg").read_bytes()))
    design = load_svg(path)
    assert design.source_name == "square.svgz"
    assert_close(design.bbox, (10, 5, 25.4, 25.4))


def test_missing_file(tmp_path):
    with pytest.raises(SvgImportError, match=r"^Could not open nope\.svg: No such file"):
        load_svg(str(tmp_path / "nope.svg"))


@pytest.mark.parametrize(
    "content, message",
    [
        ("this is not xml", r"^Could not read design\.svg: the file is damaged or not an SVG"),
        ("<svg " + SVG_NS + "><rect", r"^Could not read design\.svg: the file is damaged or not an SVG"),
        ("<html><body/></html>", r"^Could not read design\.svg: it is not an SVG file\.$"),
        ("<svg " + SVG_NS + "/>", r"^Nothing to cut in design\.svg: it has no visible paths or shapes\.$"),
        ("<svg " + SVG_NS + ' style="display:none"><rect width="5" height="5"/></svg>', r"^Nothing to cut"),
        (
            "<svg " + SVG_NS + "><text>Hi</text></svg>",
            r"no visible paths or shapes\. 1 text object ignored: convert text to paths",
        ),
    ],
)
def test_import_errors(tmp_path, content, message):
    with pytest.raises(SvgImportError, match=message):
        load_text(tmp_path, content)


def test_damaged_svgz(tmp_path):
    path = tmp_path / "broken.svgz"
    path.write_bytes(b"\x1f\x8b" + b"not a gzip stream")
    with pytest.raises(SvgImportError, match=r"^Could not read broken\.svgz: the compressed file is damaged\.$"):
        load_svg(path)


def test_svgz_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "_MAX_SVGZ_BYTES", 100)
    path = tmp_path / "bomb.svgz"
    path.write_bytes(gzip.compress(b"<svg>" + b" " * 1000 + b"</svg>"))
    with pytest.raises(SvgImportError, match=r"^Could not read bomb\.svgz: the file is too large\.$"):
        load_svg(path)


def test_large_file_loads_in_a_few_seconds(tmp_path):
    rnd = random.Random(7)
    parts = [f'<svg {SVG_NS} width="300mm" height="300mm" viewBox="0 0 300 300" fill="none" stroke="red">']
    size = 0
    while size < 5_000_000:
        parts.append(f'<g transform="translate({rnd.uniform(0, 5):.2f},0) rotate({rnd.randint(0, 9)})">')
        for _ in range(50):
            curves = " ".join(
                "c" + " ".join(f"{rnd.uniform(-5, 5):.3f},{rnd.uniform(-5, 5):.3f}" for _ in range(3))
                for _ in range(8)
            )
            item = f'<path d="M{rnd.uniform(10, 280):.3f},{rnd.uniform(10, 280):.3f} {curves} z"/>'
            parts.append(item)
            size += len(item)
        parts.append("</g>")
    parts.append("</svg>")
    path = tmp_path / "big.svg"
    path.write_text("\n".join(parts))

    start = time.perf_counter()
    design = load_svg(str(path))
    elapsed = time.perf_counter() - start
    assert len(design.paths) > 10_000
    assert elapsed < 10, f"5 MB took {elapsed:.1f} s"


def test_absurd_coordinates_neither_stall_nor_leak(tmp_path):
    text = (
        f'<svg {SVG_NS} width="100mm" height="100mm" viewBox="0 0 100 100">'
        '<path d="M 0 0 L 1e999 0"/>'  # infinite: skipped
        '<path d="M 0 0 C 1e15 0 0 1e15 10 10"/>'  # huge but finite: bounded point count
        '<path d="M 0 0 A 1e300 1e300 0 0 1 10 0"/>'
        '<g transform="scale(1e999)"><path d="M 0 0 L 1 1"/></g>'
        '<path d="M 5 5 L 6 6"/></svg>'
    )
    start = time.perf_counter()
    design = load_text(tmp_path, text)
    assert time.perf_counter() - start < 2
    assert all(len(p.points) <= 10_001 for p in design.paths)
    assert all(math.isfinite(v) for p in design.paths for xy in p.points for v in xy)
    assert design.paths[-1].points == [pytest.approx((5, 5)), pytest.approx((6, 6))]
