# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""XML pre-pass that rewrites the few constructs svgelements 1.9 renders differently from Inkscape.

- Inkscape 0.92 flowed text (`<flowRoot>`) becomes an empty `<text>`: it counts as ignored text
  and its frame rectangle is not cut.
- `<symbol>` is never drawn by itself. Every `<use>` of a symbol gets its own copy of it as a
  nested `<svg>`, so the symbol's viewBox and the use's width and height apply.
- A nested `<svg>` gets its x/y as a translate, x="0" y="0", and its width and height in user
  units. svgelements does not translate a nested viewport without a viewBox, resolves
  percentages against the last viewBox it saw, and leaks the parents' x, y, width and height
  into missing attributes.
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET

from svgelements import Length

_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_MAX_SYMBOL_DEPTH = 8  # nested symbols deeper than this (or cycles) are dropped
_DEFAULT_VIEWPORT = 1000.0  # what svgelements assumes for a percentage size on the root


def prepare(data: bytes) -> bytes:
    """Return `data`, rewritten if needed so svgelements renders it like Inkscape.

    Raises `xml.etree.ElementTree.ParseError` for malformed XML and `ValueError` when the root
    element is not `<svg>`.
    """
    root = ET.fromstring(data)
    if _local(root.tag) != "svg":
        raise ValueError("the root element is not <svg>")
    changed = _flowed_text(root)
    changed |= _symbols(root)
    changed |= _nested_viewports(root)
    return ET.tostring(root) if changed else data


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _renamed(tag: str, name: str) -> str:
    """`tag` with its local name replaced, keeping the namespace."""
    return tag[: len(tag) - len(_local(tag))] + name


def _flowed_text(root: ET.Element) -> bool:
    flows = [el for el in root.iter() if _local(el.tag) == "flowRoot"]
    for el in flows:
        el.tag = _renamed(el.tag, "text")
        el.text = None
        for child in list(el):
            el.remove(child)
    return bool(flows)


def _symbols(root: ET.Element) -> bool:
    symbols = [el for el in root.iter() if _local(el.tag) == "symbol"]
    if not symbols:
        return False
    parents = {child: parent for parent in root.iter() for child in parent}
    for sym in symbols:
        parents[sym].remove(sym)
    _instantiate(root, {sym.get("id"): sym for sym in symbols if sym.get("id")}, 0)
    return True


def _instantiate(tree: ET.Element, by_id: dict[str, ET.Element], depth: int) -> None:
    """Give every `<use>` in `tree` that points at a symbol its own copy as a nested `<svg>`."""
    uses = [el for el in tree.iter() if _local(el.tag) == "use"]
    for use in uses:
        href = use.get(_XLINK_HREF) or use.get("href") or ""
        sym = by_id.get(href[1:]) if href.startswith("#") else None
        if sym is None:
            continue
        use.attrib.pop(_XLINK_HREF, None)
        use.attrib.pop("href", None)
        if depth >= _MAX_SYMBOL_DEPTH:
            continue
        inst = copy.deepcopy(sym)
        inst.tag = _renamed(sym.tag, "svg")
        inst.attrib.pop("id", None)
        for attr in ("width", "height"):  # SVG 2: the use's size wins, then the symbol's, then 100%
            value = use.get(attr) or sym.get(attr)
            if value:
                inst.set(attr, value)
        use.append(inst)
        _instantiate(inst, by_id, depth + 1)


def _nested_viewports(root: ET.Element) -> bool:
    if not any(_local(el.tag) == "svg" for el in root.iter() if el is not root):
        return False
    width = _user_units(root.get("width", "100%"), _DEFAULT_VIEWPORT, _DEFAULT_VIEWPORT)
    height = _user_units(root.get("height", "100%"), _DEFAULT_VIEWPORT, _DEFAULT_VIEWPORT)
    stack = [(root, _viewbox_size(root) or (width, height))]
    while stack:
        el, size = stack.pop()
        for child in el:
            is_svg = _local(child.tag) == "svg"
            stack.append((child, _place_viewport(child, size) if is_svg else size))
    return True


def _place_viewport(el: ET.Element, size: tuple[float, float]) -> tuple[float, float]:
    """Resolve a nested `<svg>`'s position and size within the parent's `size`.

    Returns the size of the coordinate system it gives its children.
    """
    x = _user_units(el.get("x", "0"), size[0], 0.0)
    y = _user_units(el.get("y", "0"), size[1], 0.0)
    width = _user_units(el.get("width", "100%"), size[0], size[0])
    height = _user_units(el.get("height", "100%"), size[1], size[1])
    box = _viewbox_size(el)
    if width <= 0 or height <= 0 or (box is not None and min(box) <= 0):
        el.clear()  # an empty viewport shows nothing; svgelements would stop the whole parse
        return size
    if x or y:
        el.set("transform", f"{el.get('transform', '')} translate({x!r}, {y!r})".strip())
    el.set("x", "0")
    el.set("y", "0")
    el.set("width", repr(width))
    el.set("height", repr(height))
    return box or (width, height)


def _viewbox_size(el: ET.Element) -> tuple[float, float] | None:
    """The viewBox width and height, None when it is absent or unreadable."""
    numbers = el.get("viewBox", "").replace(",", " ").split()
    try:
        _, _, width, height = (float(v) for v in numbers)
    except ValueError:
        return None
    return width, height


def _user_units(value: str, relative: float, fallback: float) -> float:
    """A length in px, a percentage being of `relative`; `fallback` when it cannot be resolved."""
    px = Length(value).value(ppi=96.0, relative_length=relative)
    return float(px) if isinstance(px, (int, float)) else fallback
