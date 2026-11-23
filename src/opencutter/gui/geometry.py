# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Mat, media and unit geometry for the GUI (pure Python, no GTK).

All lengths are in document millimetres: x to the right (carriage), y down (feed), from the
top-left corner of the mat grid. Rectangles are ``(x, y, w, h)``.

Machine profiles (opencutter.machines)
  The mat comes from the layout's machine: every function that needs it takes ``mat`` (a
  Mat.key of the profile, or the Mat) and ``profile`` (a MachineProfile), defaulting to the
  CAMEO 4's 12 x 12 in mat (MAT_KEY), so the CAMEO 4 layout is exactly as before.
    mat_size(mat, profile)        (width x, length y) of the mat. A size of whole quarter
                                  inches is computed in inches (n x MM_PER_IN), as the media
                                  sizes are: the 12 x 12 in mat is exactly MAT_SIZE_MM.
    cut_area(mat, profile)        where the tool can reach: the mat's area_su in mm.
    usable_area(media, mat, profile), design_fits(bbox, media, mat, profile),
    test_cut_spot(media, mat, profile)  as before, on that mat.
    media_rect(key, landscape, custom_mm, mat, profile)  the media clamped to the mat's
                                  (w, h), which need not be square (12 x 24 in, 8 x 12 in).
    media_sizes(mat, profile)     the Media Size choices for a mat: Letter, A4 and the mat's own
                                  size (for the CAMEO 4, MEDIA_SIZES).
    mat_label(mat, profile)       'CAMEO 12 × 12 in', 'Portrait 8 × 12 in' (MAT_LABEL for the
                                  CAMEO 4).
    mat_choices(profile, support) the Cutting Mat dropdown: every mat of the profile, sensitive
                                  when Mat.offered at that support level (the detected machine's,
                                  else the profile's own) and at the profile's own level (as the
                                  job validator requires); an insensitive one carries
                                  NOT_MEASURED, one whose U reply is not known UNCHECKED_MAT.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from opencutter.i18n import N_, NC_, _, pgettext
from opencutter.job.validate import MEDIA_TOLERANCE_MM
from opencutter.machines import CAMEO4, MachineProfile, Mat, Support, UBasis, enabled

Rect = tuple[float, float, float, float]

MM_PER_IN = 25.4
MAT_KEY = "12x12"                  # the CAMEO 4's mat, and its default
MAT_LABEL = "CAMEO 12 × 12 in"
MAT_SIZE_MM = 12 * MM_PER_IN       # the 12 x 12 in grid
TEST_SPOT_INSET_MM = 0.25 * MM_PER_IN
# a mat listed for the profile but not offered at the machine's level (its U reply is not known
# on a tested machine): shown, but cannot be chosen
NOT_MEASURED = N_("Not measured on this machine yet")
# a mat offered on an untested machine although its U reply is not known: the reply is shown
# but not checked
UNCHECKED_MAT = N_("The cut area cannot be checked with this mat on this model yet.")


@dataclass(frozen=True)
class MediaSize:
    """A named media size, given in portrait orientation."""
    key: str
    label: str
    width_mm: float
    height_mm: float


MEDIA_SIZES: tuple[MediaSize, ...] = (
    MediaSize("letter", NC_("paper size", "Letter"), 8.5 * MM_PER_IN, 11 * MM_PER_IN),
    MediaSize("a4", "A4", 210.0, 297.0),
    MediaSize("12x12", "12 × 12 in", MAT_SIZE_MM, MAT_SIZE_MM),
)
CUSTOM_MEDIA = "custom"


# --- the mat ------------------------------------------------------------------------------------
def profile_mat(mat: str | Mat | None = MAT_KEY, profile: MachineProfile = CAMEO4) -> Mat:
    """The profile's mat: `mat` itself, the mat with this key, or the default mat for None.
    Raises ValueError for a key the profile does not have."""
    if isinstance(mat, Mat):
        return mat
    found = profile.mat(profile.default_mat if mat is None else mat)
    if found is None:
        raise ValueError(f"the {profile.name} has no mat {mat!r}")
    return found


def _inches(mm: float) -> float:
    """`mm` as n x MM_PER_IN when it is a whole number of quarter inches, else as it is."""
    quarters = round(mm / MM_PER_IN * 4)
    return quarters / 4 * MM_PER_IN if abs(quarters / 4 * MM_PER_IN - mm) < 1e-6 else mm


def mat_size(mat: str | Mat | None = MAT_KEY,
             profile: MachineProfile = CAMEO4) -> tuple[float, float]:
    """(width x = carriage, length y = feed) of the mat in mm."""
    w, h = profile_mat(mat, profile).size_mm
    return _inches(w), _inches(h)


def mat_family(profile: MachineProfile = CAMEO4) -> str:
    """The family word mats are named by: 'CAMEO', 'Portrait'."""
    return profile.name.split()[0]


def mat_label(mat: str | Mat | None = MAT_KEY, profile: MachineProfile = CAMEO4) -> str:
    """'CAMEO 12 × 12 in': the family and the mat's size (MAT_LABEL for the CAMEO 4)."""
    return f"{mat_family(profile)} {profile_mat(mat, profile).label}"


@dataclass(frozen=True)
class MatChoice:
    """One entry of the Cutting Mat dropdown."""
    key: str
    label: str
    sensitive: bool                # False: listed, but it cannot be chosen
    note: str | None = None        # tooltip: NOT_MEASURED or UNCHECKED_MAT


def _level(support: Support | str | None, profile: MachineProfile) -> Support:
    if isinstance(support, Support):
        return support
    if isinstance(support, str):
        try:
            return Support(support)
        except ValueError:
            return Support.RECOGNIZED          # an unknown level offers nothing
    return profile.support


def mat_choices(profile: MachineProfile = CAMEO4,
                support: Support | str | None = None) -> tuple[MatChoice, ...]:
    """The Cutting Mat dropdown for `profile`, in its order. `support` is the level that counts
    (the detected machine's "support"; None: the profile's own level). A mat is sensitive when
    Mat.offered at that level and at the profile's own level (the job validator's rule, so a
    tested model on an untested firmware line is offered no more mats than its jobs may use);
    a mat that is not is listed insensitive with NOT_MEASURED, and an offered mat whose U reply
    is not known carries UNCHECKED_MAT."""
    level = _level(support, profile)
    out = []
    for m in profile.mats:
        offered = m.offered(level) and m.offered(profile.support)
        note = (None if offered and m.u_basis is not UBasis.UNKNOWN
                else UNCHECKED_MAT if offered else NOT_MEASURED)
        out.append(MatChoice(m.key, mat_label(m, profile), offered, note))
    return tuple(out)


# --- media --------------------------------------------------------------------------------------
def media_sizes(mat: str | Mat | None = MAT_KEY,
                profile: MachineProfile = CAMEO4) -> tuple[MediaSize, ...]:
    """The Media Size choices for a mat: Letter, A4 and the mat's own size (MEDIA_SIZES on the
    CAMEO 4's 12 x 12 in mat)."""
    m = profile_mat(mat, profile)
    return (*MEDIA_SIZES[:2], MediaSize(m.key, m.label, *mat_size(m, profile)))


def media_size(key: str, mat: str | Mat | None = None,
               profile: MachineProfile = CAMEO4) -> MediaSize | None:
    """Return the named media size, or None for an unknown key or Custom. A mat-sized key
    ('12x24') is found among the mat's choices (with `mat`), MEDIA_SIZES and the mats of every
    machine OpenCutter drives, so a size chosen for one machine still reads on another."""
    sizes = media_sizes(mat, profile) if mat is not None else ()
    found = next((m for m in (*sizes, *MEDIA_SIZES) if m.key == key), None)
    if found is not None:
        return found
    for p in enabled():
        m = p.mat(key)
        if m is not None:
            return MediaSize(m.key, m.label, *mat_size(m, p))
    return None


def media_rect(key: str, landscape: bool, custom_mm: tuple[float, float],
               mat: str | Mat | None = MAT_KEY, profile: MachineProfile = CAMEO4) -> Rect:
    """The media rectangle on the mat: placed at the origin, clamped to the mat's width (x)
    and length (y)."""
    size = media_size(key, mat, profile)
    w, h = (size.width_mm, size.height_mm) if size else custom_mm
    if landscape:
        w, h = h, w
    mat_w, mat_h = mat_size(mat, profile)
    return (0.0, 0.0, clamp(w, 1.0, mat_w), clamp(h, 1.0, mat_h))


def cut_area(mat: str | Mat | None = MAT_KEY, profile: MachineProfile = CAMEO4) -> Rect:
    """The area the tool can reach on the mat (from the profile's mat: area_su)."""
    a0, b0, a1, b1 = profile_mat(mat, profile).area_su
    per_mm = profile.su_per_mm
    # a is the feed (document y), b the carriage (document x)
    return (b0 / per_mm, a0 / per_mm, (b1 - b0) / per_mm, (a1 - a0) / per_mm)


def usable_area(media: Rect, mat: str | Mat | None = MAT_KEY,
                profile: MachineProfile = CAMEO4) -> Rect:
    """Where a design may lie: inside both the media and the cut area."""
    return intersect(media, cut_area(mat, profile))


def test_cut_spot(media: Rect, mat: str | Mat | None = MAT_KEY,
                  profile: MachineProfile = CAMEO4) -> tuple[float, float]:
    """Default Test Cut position: a little inside the top-left corner of the media."""
    area = usable_area(media, mat, profile)
    return (max(media[0], area[0]) + TEST_SPOT_INSET_MM, max(media[1], area[1]) + TEST_SPOT_INSET_MM)


def intersect(a: Rect, b: Rect) -> Rect:
    """The overlap of two rectangles (zero size when they do not overlap)."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def contains(outer: Rect, inner: Rect, tolerance: float = 0.0) -> bool:
    """True if ``inner`` lies within ``outer`` (grown by ``tolerance`` on every side)."""
    return (inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance
            and inner[0] + inner[2] <= outer[0] + outer[2] + tolerance
            and inner[1] + inner[3] <= outer[1] + outer[3] + tolerance)


def design_fits(bbox: Rect, media: Rect, mat: str | Mat | None = MAT_KEY,
                profile: MachineProfile = CAMEO4) -> bool:
    """The validator's rule on the bounding box: inside the media (with tolerance) and the cut area."""
    return contains(media, bbox, MEDIA_TOLERANCE_MM) and contains(cut_area(mat, profile), bbox)


def placement_problem(bbox: Rect, media: Rect, mat: str | Mat | None = MAT_KEY,
                      profile: MachineProfile = CAMEO4) -> str | None:
    """Why design_fits() fails, for the texts: 'cut-area' when part of the design is outside
    the mat's cut area (that comes first), 'media' when it is inside the cut area but off the
    media, None when it fits."""
    if not contains(cut_area(mat, profile), bbox):
        return "cut-area"
    if not contains(media, bbox, MEDIA_TOLERANCE_MM):
        return "media"
    return None


def media_covers_cut_area(media: Rect, mat: str | Mat | None = MAT_KEY,
                          profile: MachineProfile = CAMEO4) -> bool:
    """True when the media rectangle holds the whole cut area (a mat-sized Media Size): the
    media check then cannot keep the blade on a smaller sheet."""
    return contains(media, cut_area(mat, profile))


def clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


# Studio's alignment commands, in menu order: (key, label). The edges are those of an area (the
# window passes usable_area(media)) and the centres those of the page (the media), kept inside
# that area, so an aligned design that fits always lies inside the cut area.
ALIGNMENTS: tuple[tuple[str, str], ...] = (
    ("center-page", N_("Center to Page")),
    ("left", N_("Align Left")), ("center", N_("Align Center")), ("right", N_("Align Right")),
    ("top", N_("Align Top")), ("middle", N_("Align Middle")), ("bottom", N_("Align Bottom")),
)


def align_rect(bbox: Rect, area: Rect, how: str, page: Rect | None = None) -> tuple[float, float]:
    """The new top-left corner (x, y) of ``bbox`` aligned in ``area``.

    ``how`` is a key of ``ALIGNMENTS``: 'center-page' centres on both axes, 'left', 'center'
    and 'right' move only x, 'top', 'middle' and 'bottom' only y. The centres are those of
    ``page`` (default ``area``), e.g. the media sheet whose edge lies inside the cut area's
    margin; a box that fits in ``area`` is then moved just enough to lie inside it. The size
    never changes: a box larger than the area is still centred, or put against the chosen
    edge. The result depends only on the box's size, the area and the page, so aligning twice
    gives the same position."""
    x, y, w, h = bbox
    ax, ay, aw, ah = area
    px, py, pw, ph = area if page is None else page
    horizontal = {"center-page": px + (pw - w) / 2, "left": ax, "center": px + (pw - w) / 2,
                  "right": ax + aw - w}
    vertical = {"center-page": py + (ph - h) / 2, "top": ay, "middle": py + (ph - h) / 2,
                "bottom": ay + ah - h}
    if how not in horizontal and how not in vertical:
        raise ValueError(f"unknown alignment {how!r}")
    if how in horizontal:
        x = _within(horizontal[how], w, ax, aw)
    if how in vertical:
        y = _within(vertical[how], h, ay, ah)
    return x, y


def _within(start: float, size: float, low: float, length: float) -> float:
    """``start`` moved into [low, low + length - size] when the box fits, then by the last
    float step(s) needed for [start, start + size] to lie in [low, low + length]: (a + b) - b
    can exceed a by 1e-14 mm, and the Send check (``design_fits``) compares exactly against
    the cut area."""
    if size > length:
        return start
    start = max(min(start, low + length - size), low)
    while start + size > low + length and start > low:
        start = math.nextafter(start, -math.inf)
    return start


def to_units(mm: float, units: str) -> float:
    """Convert millimetres to the user's units ('in' or 'mm')."""
    return mm / MM_PER_IN if units == "in" else mm


def from_units(value: float, units: str) -> float:
    """Convert a value in the user's units ('in' or 'mm') to millimetres."""
    return value * MM_PER_IN if units == "in" else value


def format_length(mm: float, units: str) -> str:
    """A length as text in the user's units, without the unit name."""
    return f"{to_units(mm, units):.2f}" if units == "in" else f"{mm:.1f}"


def parse_length(text: str, units: str) -> float | None:
    """Parse a typed length in the user's units; None when it is not a number."""
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    return from_units(value, units)


def format_size(w_mm: float, h_mm: float, units: str) -> str:
    """A size like '8.5 × 11 in' or '216 × 279 mm'."""
    if units == "in":
        return f"{_trim(w_mm / MM_PER_IN)} × {_trim(h_mm / MM_PER_IN)} in"
    return f"{w_mm:.0f} × {h_mm:.0f} mm"


def format_duration(seconds: float) -> str:
    """A duration like '45 s', '1 min 12 s' or '2 min'."""
    seconds = max(0, round(seconds))
    minutes, secs = divmod(seconds, 60)
    if not minutes:
        return _("{seconds} s").format(seconds=secs)
    if secs:
        return _("{minutes} min {seconds} s").format(minutes=minutes, seconds=secs)
    return _("{minutes} min").format(minutes=minutes)


def media_label(label: str) -> str:
    """A Media Size's name in the window's language ('Letter'; a size such as '12 × 12 in' or
    'A4' stays as it is)."""
    return pgettext("paper size", label) if label else label


def format_clock(seconds: float) -> str:
    """A duration like '0:48' for progress text."""
    minutes, secs = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def _trim(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"
