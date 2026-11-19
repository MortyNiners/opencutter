# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""GPGL byte builder for jobs (docs/PROTOCOL.md sections 4, 5 and 7), in the dialect of a
machine profile (opencutter.machines).

Pure functions, no I/O. Every command is ASCII text ending in ETX (0x03); a write is one or
more whole commands, at most the profile's `max_write` bytes (`MAX_WRITE`, 182, on the
CAMEO 4), never containing ESC (0x1b).

Every function that writes a command takes the profile as `profile` (default: the CAMEO 4, so
every caller written before machine profiles gets the same bytes). The profile writes the texts
(MachineProfile.mat_texts, area_texts, condition_texts, trailer_texts); this module adds ETX:
  * the header: TG<n> FN0 TB50,0, then FM1 for a mat with expand mode (TG9 FN0 TB50,0 FM1, as
    Studio sends its 24 x 24 in header), then the cut area \\a0,b0 Za1,b1;
  * a condition block: on a two-holder model J<n> ! FX [TJ] FC FE FF with the holder number;
    on a one-holder model the plain forms with no J; TJ only where the model has it; TF<d>,1
    last for a tool that takes a Blade Depth;
  * the trailer \\0,0 M0,0 FN0 TB50,0.
M and D are the same on every model.

Coordinates: document mm (x right, y down from the mat origin) map to cutter units with
a = su(y) (feed, rollers) and b = su(x) (carriage); 1 SU = 0.05 mm (confirmed on the machine;
the plotter unit of every known model, which the profile self-check requires).

Closed contours cut with a blade (Tool.blade) get Studio's overlap: they start `OVERLAP_SU`
before the start point on the closing edge and end `OVERLAP_SU` past it on the first edge, so
the cut closes cleanly (PROTOCOL.md 7.1, step 6). Pens, punch and foil tools draw the contour
as given.
"""
from __future__ import annotations

import math
import struct
from collections.abc import Iterable, Sequence

from opencutter.machines import CAMEO4, AccelForm, MachineProfile

from .spec import CutGroup, Point, ToolSettings

ETX = b"\x03"
SU_PER_MM = CAMEO4.su_per_mm          # 20: 1 SU = 0.05 mm on every known model
_STUDIO_UNIT = struct.unpack("f", struct.pack("f", 0.05))[0]   # Studio divides by float32 0.05
MAX_WRITE = CAMEO4.max_write          # 182: the CAMEO 4's longest data write (PROTOCOL.md 2.3)
OVERLAP_SU = 20                       # 1 mm closed-contour overlap for blades
# The CAMEO 4's mats: TG value, Studio-style cut area (\a0,b0 Za1,b1 in SU, a = feed) and size
# (width, length) in mm (PROTOCOL.md section 4). An alias of CAMEO4.mats for older callers;
# other models: MachineProfile.mats.
MATS: dict[str, dict] = {m.key: {"tg": m.tg, "area_su": m.area_su, "size_mm": m.size_mm}
                         for m in CAMEO4.mats}

CutterPoint = tuple[int, int]


def round_away(x: float) -> int:
    """Round half away from zero, as Studio does."""
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def su(mm: float) -> int:
    """mm -> integer SU exactly as Studio does: 3 decimals, divide by float32 0.05, round."""
    return round_away(round(mm, 3) / _STUDIO_UNIT)


def doc_to_cutter(x_mm: float, y_mm: float) -> CutterPoint:
    """Document point -> (a, b): a = su(y) is the feed, b = su(x) is the carriage."""
    return su(y_mm), su(x_mm)


def cmd(text: str) -> bytes:
    """One GPGL command: ASCII text plus ETX."""
    return text.encode("ascii") + ETX


def mat_commands(mat: str, profile: MachineProfile = CAMEO4) -> list[bytes]:
    """Mat type and normal orientation: TG<n> FN0 TB50,0 (they reset the cut area), then FM1
    for a mat with expand mode. Raises ValueError for a mat the profile does not have."""
    return [cmd(t) for t in profile.mat_texts(mat)]


def area_commands(mat: str, profile: MachineProfile = CAMEO4) -> list[bytes]:
    """Cut area \\a0,b0 Za1,b1. Must follow the mat commands; LL is strictly below UR."""
    return [cmd(t) for t in profile.area_texts(mat)]


def header(mat: str, profile: MachineProfile = CAMEO4) -> list[bytes]:
    """The job header: TG<n> FN0 TB50,0 [FM1] \\a0,b0 Za1,b1."""
    return mat_commands(mat, profile) + area_commands(mat, profile)


def condition_block(s: ToolSettings, profile: MachineProfile = CAMEO4) -> list[bytes]:
    """The condition block in the profile's dialect: J ! FX TJ FC FE FF [TF] on the CAMEO 4.
    TF comes last: it drives the carriage to the AutoBlade station. A model without TJ gets
    none (its acceleration setting is not sent). Raises ValueError for what the dialect cannot
    write (job/validate.py checks the values first)."""
    accel = None if profile.dialect.accel is AccelForm.NONE else s.acceleration
    return [cmd(t) for t in profile.condition_texts(
        holder=s.holder, speed=s.speed, force=s.force, acceleration=accel, offset=s.offset,
        line_segment_overcut=s.line_segment_overcut, overcut=s.overcut,
        blade_depth=s.blade_depth)]


def path(contours_su: Sequence[Sequence[CutterPoint]], passes: int = 1) -> list[bytes]:
    """M to each contour start, D through its points; the whole set repeated `passes` times."""
    out = []
    for _ in range(passes):
        for pts in contours_su:
            (a, b), rest = pts[0], pts[1:]
            out.append(cmd(f"M{a},{b}"))
            out += [cmd(f"D{a},{b}") for a, b in rest]
    return out


def moves(points: Iterable[CutterPoint]) -> list[bytes]:
    """Tool-up moves through `points`, skipping repeated points (the outline preview)."""
    return [cmd(f"M{a},{b}") for a, b in _dedupe(points)]


def trailer(profile: MachineProfile = CAMEO4) -> list[bytes]:
    """Return to origin: \\0,0 (so LL does not clip the move) M0,0 FN0 TB50,0."""
    return [cmd(t) for t in profile.trailer_texts()]


def chunk(cmds: Iterable[bytes], limit: int = MAX_WRITE) -> list[bytes]:
    """Pack whole commands into writes of at most `limit` bytes (split only at ETX); pass the
    profile's `max_write` for another model."""
    writes, cur = [], b""
    for c in cmds:
        if len(c) > limit:
            raise ValueError(f"command {c!r} is longer than one write")
        if len(cur) + len(c) > limit:
            writes.append(cur)
            cur = b""
        cur += c
    if cur:
        writes.append(cur)
    return writes


def test_cut_contours(x_mm: float, y_mm: float) -> list[list[Point]]:
    """Studio-style Test Cut: a 10 mm square with a triangle inside, top-left at (x, y) in
    document mm. The triangle comes first so the inner piece is cut before the outer one."""
    triangle = [(x_mm + 5, y_mm + 2.5), (x_mm + 8, y_mm + 7.5), (x_mm + 2, y_mm + 7.5),
                (x_mm + 5, y_mm + 2.5)]
    square = [(x_mm, y_mm), (x_mm + 10, y_mm), (x_mm + 10, y_mm + 10), (x_mm, y_mm + 10),
              (x_mm, y_mm)]
    return [triangle, square]


test_cut_contours.__test__ = False      # not a pytest test despite its name


def cutter_contours(group: CutGroup, profile: MachineProfile = CAMEO4) -> list[list[CutterPoint]]:
    """The group's paths in SU, without repeated points, with the blade overlap applied where
    the profile's tool cuts with a blade. Paths that collapse to a single point after rounding
    are dropped."""
    blade = is_blade(group.settings, profile)
    out = []
    for p in group.paths:
        pts = _dedupe(doc_to_cutter(x, y) for x, y in p)
        if len(pts) < 2:
            continue
        if blade and is_closed(pts):
            pts = with_overlap(pts)
        out.append(pts)
    return out


def is_blade(s: ToolSettings, profile: MachineProfile = CAMEO4) -> bool:
    """True when the tool of `s` cuts with a blade (Tool.blade); for a code the holder does not
    list, whether that code is a blade in any holder of the profile."""
    tool = profile.tool(s.holder, s.tool_code)
    return tool.blade if tool is not None else s.tool_code in profile.blade_codes()


def is_closed(pts: Sequence[CutterPoint]) -> bool:
    """A contour with at least three distinct points that ends where it starts."""
    return len(pts) >= 4 and pts[0] == pts[-1]


def with_overlap(pts: Sequence[CutterPoint], length: int = OVERLAP_SU) -> list[CutterPoint]:
    """Closed contour -> start `length` SU back along the closing edge, run the contour, then
    continue `length` SU along the first edge (Studio's closed-contour overlap)."""
    lead_in = _walk(pts[::-1], length)[::-1]
    lead_out = _walk(pts, length)
    return lead_in + list(pts) + lead_out


def _walk(pts: Sequence[CutterPoint], length: float) -> list[CutterPoint]:
    """Points reached walking `length` SU along `pts` from pts[0] (pts[0] excluded)."""
    out: list[CutterPoint] = []
    left = float(length)
    for (a0, b0), (a1, b1) in zip(pts, pts[1:]):
        seg = math.hypot(a1 - a0, b1 - b0)
        if seg >= left:
            t = left / seg
            end = (round_away(a0 + (a1 - a0) * t), round_away(b0 + (b1 - b0) * t))
            if end != (a0, b0):
                out.append(end)
            return out
        out.append((a1, b1))
        left -= seg
    return out


def _dedupe(pts: Iterable[CutterPoint]) -> list[CutterPoint]:
    out: list[CutterPoint] = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    return out
