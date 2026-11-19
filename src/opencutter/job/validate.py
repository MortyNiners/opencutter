# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Safety boundary for user jobs.

`validate_spec` checks a JobSpec's values before anything is built. `make_write_validator`
returns the byte-level check the daemon applies to every write of that job: it accepts only
the exact commands the builder emits for that spec, and tool moves (M/D) inside the spec's
media and the mat's cut area. Everything else is refused, including ESC, the never-send list
of PROTOCOL.md section 6, and any setting or position from another spec.

Machine profiles. Every check uses the profile the job names (`JobSpec.machine`, looked up in
opencutter.machines.registry), or the profile the caller passes (the daemon: the machine FG
detected). The spec must name that profile's id, and the profile must be one jobs may use
(TESTED or UNTESTED): an unknown id, a model that is only recognized and a mismatching machine
are refused before anything is built. A profile that is not the registry's own entry for its
id must pass the global self-check (machines.safety.check_profile). From the profile come the
mats (only those offered at the profile's level, Mat.offered: a mat with an unknown U reply
only on an UNTESTED profile), the holders and tools (Tool.usable, Tool.blade, Tool.depth), the
ranges, the command grammar (MachineProfile.setting_grammar) and the limits (max_write,
max_coordinate_su). The global never-send rules (machines.safety.check_write, the same for
every model) are a floor under every write; FM1 passes only where the profile's mat has expand
mode, right after the TG9 FN0 TB50,0 of the header. The module names UNSUPPORTED_TOOLS, RANGES
and the private grammar are the CAMEO 4's, kept for older callers.

A D cuts from wherever the tool is, which one write alone cannot always tell (a cut may be
split over several writes). The write validator refuses a D that directly follows any other
command than an in-media M or D in the same write; `check_plan_sequence` checks the whole plan
once, before anything is sent, so that every D starts from an M or D inside the media.

The plan's "units" step (`U`) is a read-only query sent through the device's query
allowlist, not a job write, so the write validator does not accept it.
"""
from __future__ import annotations

import functools
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from opencutter.machines import CAMEO4, MachineProfile, Mat, RecognizedModel, registry, safety

from . import builder
from .builder import ETX, doc_to_cutter
from .spec import NO_TOOL, CutGroup, JobSpec, ToolSettings

# The CAMEO 4's refused tools: Rotary Blade (5) and Punch Tool (6), whose motion encoding was
# never checked (Tool.usable; an alias for older callers).
UNSUPPORTED_TOOLS = CAMEO4.unsupported_codes()
MAX_POINTS = 500_000
MAX_PASSES = CAMEO4.ranges.passes.hi
MEDIA_TOLERANCE_MM = 0.5
# The CAMEO 4's firmware ranges (PROTOCOL.md section 5); passes from the UI. An alias of
# CAMEO4.ranges.table() for older callers; other models: MachineProfile.ranges.
RANGES: dict[str, tuple[int, int]] = CAMEO4.ranges.table()
ESC = 0x1B
ORIGIN_MOVE = b"M0,0"                       # the trailer's tool-up return to the origin


@functools.cache
def _move_pattern(max_coordinate_su: int) -> re.Pattern[bytes]:
    """M or D with two whole numbers of at most as many digits as `max_coordinate_su`, no
    leading zeros (the bounds come from the media box)."""
    more = len(str(max_coordinate_su)) - 1
    return re.compile(rb"[MD](0|[1-9][0-9]{0,%d}),(0|[1-9][0-9]{0,%d})" % (more, more))


_MOVE = _move_pattern(CAMEO4.max_coordinate_su)
# The CAMEO 4's command grammar before machine profiles, kept for comparison: the validator
# uses MachineProfile.setting_grammar(), which for the CAMEO 4 accepts the same commands except
# TG2..TG7 (mats it does not offer).
_N1_10 = rb"(?:[1-9]|10)"
_SETTING = re.compile(
    rb"TG[1-7]|FN0|TB50,0|\\(?:0|[1-9][0-9]{0,4}),(?:0|[1-9][0-9]{0,4})"
    rb"|Z[1-9][0-9]{0,4},[1-9][0-9]{0,4}|J[12]"
    rb"|!(?:[1-9]|[12][0-9]|30),[12]|FX(?:[1-9]|[12][0-9]|3[0-3]),[12]|TJ" + _N1_10 +
    rb"|FC(?:[0-9]|[1-9][0-9]),1,[12]|FE[01],[12]|FF[0-9],[0-9],[12]|TF(?:[0-9]|10),1")
# The never-send commands PROTOCOL.md section 6 names: the global list of every model
# (machines.safety.NEVER_SEND_PREFIXES, with the commands no job needs), plus FM. FM is a rule
# of the profiles with expand mode: a job that does not emit FM1 itself refuses it by name. The
# firmware skips leading filler bytes, so the check strips them, and it starts a command at
# any letter, so the check reads one from every letter (_is_never_send); the exact-match
# allowlist is what really protects.
_NEVER_SEND = safety.NEVER_SEND_PREFIXES + (safety.FM_PREFIX,)
_FILLER = safety.FILLER


class UnsafeJob(ValueError):
    """A job or a write that must not reach the machine."""


def job_profile(spec: JobSpec, profile: MachineProfile | None = None) -> MachineProfile:
    """The machine profile `spec` is checked and built with. Raises UnsafeJob unless the spec
    names a profile jobs may use (JobSpec.machine: a TESTED or UNTESTED MachineProfile of the
    registry) and, when the caller passes `profile` (the machine on the link), that profile has
    the same id. A passed profile that is not the registry's own entry for its id must pass the
    global self-check (machines.safety.check_profile). A recognized model, named by the spec or
    passed as the machine, is refused with its plain-words reason."""
    machine = getattr(spec, "machine", None)
    if type(machine) is not str:
        raise UnsafeJob("the job does not name a machine")
    entry = registry.by_id(machine)
    if isinstance(entry, RecognizedModel):
        raise UnsafeJob(f"this job was made for the {entry.name}. {entry.message}")
    if profile is None:
        if entry is None:
            raise UnsafeJob(f"unknown machine {machine!r}")
        profile = entry
    elif isinstance(profile, RecognizedModel):
        raise UnsafeJob(profile.message)        # the machine itself is one OpenCutter refuses
    elif not isinstance(profile, MachineProfile):
        raise UnsafeJob("not a machine profile")
    elif profile.id != machine:
        made_for = entry.name if entry is not None else f"unknown machine {machine!r}"
        raise UnsafeJob(f"this job was made for the {made_for}, but the machine is the "
                        f"{profile.name}")
    elif entry is not profile:
        try:
            safety.check_profile(profile)
        except safety.UnsafeProfile as e:
            raise UnsafeJob(str(e)) from None
    if not profile.support.jobs_allowed:
        raise UnsafeJob(f"OpenCutter cannot drive the {profile.name} yet")
    return profile


def job_mat(spec: JobSpec, profile: MachineProfile) -> Mat:
    """The profile's mat `spec.mat`, if it is offered at the profile's level (Mat.offered);
    otherwise UnsafeJob."""
    mat = profile.mat(spec.mat) if type(spec.mat) is str else None
    if mat is None:
        raise UnsafeJob(f"unknown cutting mat {spec.mat!r} for the {profile.name}")
    if not mat.offered(profile.support):
        raise UnsafeJob(f"the {mat.label} cutting mat is not offered on the {profile.name}: "
                        "its reply to the unit check (U) is not known")
    return mat


def validate_spec(spec: JobSpec, profile: MachineProfile | None = None) -> None:
    """Raise UnsafeJob unless `spec` is for the machine (`job_profile`: the profile it names,
    and `profile` when given), its mat is one of that machine's offered mats, every value is
    in the profile's ranges and every point lies inside the media (with `MEDIA_TOLERANCE_MM`)
    and inside the mat's cut area. Tools the profile marks unusable (on the CAMEO 4 the Rotary
    Blade and the Punch Tool, `UNSUPPORTED_TOOLS`) are refused; tools that do not cut with a
    blade (Sketch Pen, Foil Tool) need Offset 0, no Line Segment Overcut and Overcut (0, 0)."""
    _validate(spec, profile)


def _validate(spec: JobSpec, profile: MachineProfile | None) -> tuple[MachineProfile, Mat]:
    """validate_spec(); returns the profile and the mat it checked against."""
    if type(spec) is not JobSpec:
        raise UnsafeJob("not a JobSpec")
    profile = job_profile(spec, profile)
    mat = job_mat(spec, profile)
    _check_media(spec, mat)
    if type(spec.groups) is not list or not spec.groups:
        raise UnsafeJob("the job has nothing to cut")
    tools: dict[int, int] = {}
    points = 0
    x, y, w, h = spec.media
    tol = MEDIA_TOLERANCE_MM
    media = (x - tol, y - tol, x + w + tol, y + h + tol)
    for i, group in enumerate(spec.groups, 1):
        if type(group) is not CutGroup:
            raise UnsafeJob(f"group {i} is not a CutGroup")
        s = group.settings
        _check_settings(s, i, profile)
        if tools.setdefault(s.holder, s.tool_code) != s.tool_code:
            if len(profile.holders) > 1:
                raise UnsafeJob(f"{profile.holder(s.holder).label} is used with two different "
                                "tools")
            raise UnsafeJob(f"the {profile.name} has one tool holder, and the job uses two "
                            "different tools")
        points += _check_paths(group, i, media, mat.area_su)
        if points > MAX_POINTS:
            raise UnsafeJob(f"more than {MAX_POINTS} points")
    if type(spec.preview_outline) is not bool:
        raise UnsafeJob("preview_outline must be a bool")
    _check_allow_unrecognized(spec, profile)
    return profile, mat


def media_box_su(spec: JobSpec,
                 profile: MachineProfile | None = None) -> tuple[int, int, int, int]:
    """(a_min, b_min, a_max, b_max) in SU: the media rect plus the tolerance, intersected
    with the mat's cut area. Every M/D of the job lies inside it (the origin return aside)."""
    return _media_box(spec, job_mat(spec, job_profile(spec, profile)))


def _media_box(spec: JobSpec, mat: Mat) -> tuple[int, int, int, int]:
    x, y, w, h = spec.media
    a0, b0, a1, b1 = mat.area_su
    tol = MEDIA_TOLERANCE_MM
    return (max(a0, builder.su(y - tol)), max(b0, builder.su(x - tol)),
            min(a1, builder.su(y + h + tol)), min(b1, builder.su(x + w + tol)))


def make_write_validator(spec: JobSpec,
                         profile: MachineProfile | None = None) -> Callable[[bytes], None]:
    """Validate `spec` (for `profile` when given), then return check(data) that raises
    UnsafeJob unless `data` is one write of this spec's job: ETX-terminated whole commands, at
    most the profile's `max_write` bytes, no ESC, each command either one the builder emits for
    this spec (header, condition blocks, trailer) or an M/D inside `media_box_su(spec)`, and
    no D directly after any command other than an M or D inside that box. A D that opens a
    write is checked by `check_plan_sequence`, which knows the previous write. Every write
    must also keep the global rules (machines.safety.check_write): nothing on the never-send
    list, and FM1 only right after TG9 FN0 TB50,0 in the same write."""
    return bind_job(spec, profile).validator


@dataclass(frozen=True)
class BoundJob:
    """A validated spec bound to its machine (bind_job)."""
    validator: Callable[[bytes], None]       # make_write_validator's check
    profile: MachineProfile                  # the machine (job_profile)
    mat: Mat                                 # the profile's mat spec.mat (job_mat)
    media_box: tuple[int, int, int, int]     # media_box_su


def bind_job(spec: JobSpec, profile: MachineProfile | None = None) -> BoundJob:
    """Validate `spec` (validate_spec, for `profile` when given) and bind it to its machine:
    the write validator (make_write_validator), the profile, the mat and the media box, with
    the profile resolved once. Raises UnsafeJob."""
    profile, mat = _validate(spec, profile)
    try:
        emitted = builder.header(spec.mat, profile) + builder.trailer(profile)
        for group in spec.groups:
            emitted += builder.condition_block(group.settings, profile)
    except ValueError as e:               # the dialect cannot write a value: fail closed
        raise UnsafeJob(str(e)) from None
    allowed = {c[:-1] for c in emitted}
    grammar = profile.setting_grammar()
    for c in allowed:                     # the builder's own output must pass the grammar
        if not (grammar.fullmatch(c) or c == ORIGIN_MOVE):
            raise UnsafeJob(f"builder emitted {c!r}, outside the command grammar")
    box = _media_box(spec, mat)
    a_min, b_min, a_max, b_max = box
    limit = profile.max_write
    move = _move_pattern(profile.max_coordinate_su)

    def check(data: bytes) -> None:
        if type(data) is not bytes:
            raise UnsafeJob("a write must be bytes")
        if not 0 < len(data) <= limit:
            raise UnsafeJob(f"write of {len(data)} bytes (allowed 1..{limit})")
        if ESC in data:
            raise UnsafeJob("ESC inside a job write")
        if not data.endswith(ETX):
            raise UnsafeJob("a write must end with ETX")
        after: bytes | None = None      # the previous command, unless it was an in-media M/D
        for c in data[:-1].split(ETX):
            m = move.fullmatch(c)
            inside = m is not None and a_min <= int(m[1]) <= a_max and b_min <= int(m[2]) <= b_max
            if c in allowed and not inside:   # header, condition block or trailer (M0,0 too,
                after = c                     # unless the media reaches the origin)
                continue
            if m is None:
                raise UnsafeJob(f"never-send command {c!r}" if _is_never_send(c)
                                else f"command {c!r} is not part of this job")
            if not inside:
                raise UnsafeJob(f"{c!r} lies outside the media")
            if c[:1] == b"D" and after is not None:
                raise UnsafeJob(f"{c!r} would cut from an unknown point: it follows {after!r}, "
                                "not an M or D inside the media")
            after = None
        try:
            safety.check_write(data, profile)     # the global floor, the same for every model
        except safety.SafetyError as e:
            raise UnsafeJob(str(e)) from None

    return BoundJob(check, profile, mat, box)


def check_plan_sequence(plan: Any) -> None:
    """Raise UnsafeJob unless every tool-down move (D) of `plan` starts from a known point
    inside the job's bounds. Pure: no I/O; run it once, before anything is sent.

    Walks every command of every write step in plan order ("units" steps do not move; any
    other kind, and a write step without writes, is refused). A D must directly follow an M
    or D that lies inside `plan.media_box` (a_min, b_min, a_max, b_max in SU), in the same
    write step: each step runs only after the user confirmed it, so a step never cuts on from
    where the previous one left the tool. This refuses a D after the header, J, TF, FN0, \\,
    Z, the trailer's M0,0, or at the start of a step, and any malformed D. An object without
    `media_box` (a pinned program, whose validator bounds every M/D itself) gets the
    predecessor rule alone. A move may have as many digits as `plan.max_coordinate_su` (the
    profile's; the CAMEO 4's 99 999 for an object without it)."""
    box = getattr(plan, "media_box", None)
    if box is not None and (type(box) is not tuple or len(box) != 4
                            or not all(_is_int(v) for v in box)):
        raise UnsafeJob("the plan's media box must be four integers")
    top = getattr(plan, "max_coordinate_su", CAMEO4.max_coordinate_su)
    if not _is_int(top) or not 1 <= top <= 999_999:
        raise UnsafeJob("the plan's largest coordinate must be an integer 1..999999")
    move = _move_pattern(top)
    steps = getattr(plan, "steps", None)
    if not isinstance(steps, (list, tuple)) or not steps:
        raise UnsafeJob("the plan has no steps")
    for step in steps:
        kind, writes = getattr(step, "kind", None), getattr(step, "writes", None)
        if kind == "units":
            continue
        if kind != "write" or not isinstance(writes, (list, tuple)) or not writes:
            raise UnsafeJob(f"bad step {getattr(step, 'title', step)!r}")
        prev = "the start of the step"      # what the next D would start from
        known = False                       # prev is an M/D inside the box
        for data in writes:
            if type(data) is not bytes or not data.endswith(ETX):
                raise UnsafeJob(f"step {step.title!r} has a write that is not ETX-terminated "
                                "bytes")
            for c in data[:-1].split(ETX):
                m = move.fullmatch(c)
                if c.lstrip(_FILLER)[:1] in (b"D", b"d"):   # the firmware skips filler bytes
                    if m is None:
                        raise UnsafeJob(f"step {step.title!r}: malformed tool-down move {c!r}")
                    if not known:
                        raise UnsafeJob(f"step {step.title!r}: {c!r} would cut from an unknown "
                                        f"point (after {prev}, not an M or D inside the media)")
                prev = repr(c)
                known = m is not None and (box is None or (box[0] <= int(m[1]) <= box[2]
                                                           and box[1] <= int(m[2]) <= box[3]))


def _check_media(spec: JobSpec, mat: Mat) -> None:
    media = spec.media
    if type(media) is not tuple or len(media) != 4 or not all(_is_real(v) for v in media):
        raise UnsafeJob("media must be four finite numbers (x, y, w, h)")
    x, y, w, h = media
    width, height = mat.size_mm
    if w <= 0 or h <= 0:
        raise UnsafeJob("the media size must be positive")
    if x < 0 or y < 0 or x + w > width or y + h > height:
        raise UnsafeJob(f"the media does not fit on the {spec.mat} cutting mat")


def _check_settings(s: ToolSettings, i: int, profile: MachineProfile) -> None:
    if type(s) is not ToolSettings:
        raise UnsafeJob(f"group {i}: settings is not ToolSettings")
    holder = profile.holder(s.holder)
    if holder is None:
        raise UnsafeJob(f"group {i}: the tool holder must be {_holder_numbers(profile)}")
    tool = holder.tool(s.tool_code)
    if tool is None or tool.code == NO_TOOL:
        raise UnsafeJob(f"group {i}: tool code {s.tool_code!r} is not valid in {holder.label}")
    if not tool.usable:
        raise UnsafeJob(f"group {i}: the {tool.name} (tool code {s.tool_code}) is not supported")
    for name, (lo, hi) in profile.ranges.table().items():
        value = getattr(s, name)
        if name == "blade_depth" and value is None:
            continue
        if not _is_int(value) or not lo <= value <= hi:
            raise UnsafeJob(f"group {i}: {name} {value!r} outside {lo}..{hi}")
    if profile.ranges.acceleration is None and not _is_int(s.acceleration):
        # A model without TJ: the value is not sent, but it must still be a whole number.
        raise UnsafeJob(f"group {i}: acceleration {s.acceleration!r} is not a whole number")
    if type(s.line_segment_overcut) is not bool:
        raise UnsafeJob(f"group {i}: line_segment_overcut must be a bool")
    oc, (lo, hi) = s.overcut, profile.ranges.overcut.as_tuple()
    if type(oc) is not tuple or len(oc) != 2 or not all(_is_int(v) and lo <= v <= hi for v in oc):
        raise UnsafeJob(f"group {i}: overcut must be two integers {lo}..{hi}")
    if not tool.blade and (s.offset != 0 or s.line_segment_overcut or oc != (0, 0)):
        # Studio forces FC0, FE0 and FF0,0 for pens and foil (PROTOCOL.md section 5).
        raise UnsafeJob(f"group {i}: the {tool.name} is not a blade: "
                        "Offset, Line Segment Overcut and Overcut must be 0 or off")
    if s.blade_depth is not None and not tool.depth:
        raise UnsafeJob(f"group {i}: Blade Depth (TF) works only for {_depth_tools(profile)}")


def _check_paths(group: CutGroup, i: int, media: tuple[float, float, float, float],
                 area: tuple[int, int, int, int]) -> int:
    """Check every point of the group against the media bounds (x0, y0, x1, y1 in mm, with
    the tolerance) and the cut area (SU); return the number of points."""
    x0, y0, x1, y1 = media
    a0, b0, a1, b1 = area
    if type(group.paths) is not list or not group.paths:
        raise UnsafeJob(f"group {i} has no paths")
    count = 0
    for path in group.paths:
        if type(path) is not list or len(path) < 2:
            raise UnsafeJob(f"group {i}: every path needs at least 2 points")
        for pt in path:
            if type(pt) is not tuple or len(pt) != 2 or not (_is_real(pt[0]) and _is_real(pt[1])):
                raise UnsafeJob(f"group {i}: a point must be two finite numbers")
            x, y = pt
            if not (x0 <= x <= x1 and y0 <= y <= y1):
                raise UnsafeJob(f"group {i}: point ({x:.2f}, {y:.2f}) mm lies outside the media")
            a, b = doc_to_cutter(x, y)
            if not (a0 <= a <= a1 and b0 <= b <= b1):
                raise UnsafeJob(f"group {i}: point ({x:.2f}, {y:.2f}) mm lies outside the cut area")
        count += len(path)
    return count


def _check_allow_unrecognized(spec: JobSpec, profile: MachineProfile) -> None:
    allow = spec.allow_unrecognized_tool
    if type(allow) is not dict or not all(_is_int(h) and profile.holder(h) is not None
                                          and type(v) is bool for h, v in allow.items()):
        raise UnsafeJob(f"allow_unrecognized_tool must map holder {_holder_numbers(profile)} "
                        "to a bool")
    for g in spec.groups:
        s = g.settings
        if s.blade_depth is not None and allow.get(s.holder):
            holder = profile.holder(s.holder)
            raise UnsafeJob(f"Blade Depth (TF) needs the {holder.tool(s.tool_code).name} "
                            f"detected in {holder.label}")


def _holder_numbers(profile: MachineProfile) -> str:
    """'1 or 2' on the CAMEO 4, '1' on a one-holder model."""
    numbers = [str(h.number) for h in profile.holders]
    return ", ".join(numbers[:-1]) + " or " + numbers[-1] if len(numbers) > 1 else numbers[0]


def _depth_tools(profile: MachineProfile) -> str:
    """The tools that take a Blade Depth: 'the AutoBlade in Tool 1' on the CAMEO 4, 'the
    AutoBlade' on a one-holder model."""
    names = [f"the {t.name}" + (f" in {h.label}" if len(profile.holders) > 1 else "")
             for h in profile.holders for t in h.tools if t.depth]
    return " or ".join(names) if names else f"no tool of the {profile.name}"


def _is_never_send(c: bytes) -> bool:
    """True for a command the refusal names as never-send: the global list and lowercase
    (machines.safety, which covers `_NEVER_SEND` but FM), and FM, refused by name wherever the
    job does not emit FM1 itself. Like the firmware, it reads a command from every letter, so
    FX10,TB72 is named never-send too (safety.command_starts)."""
    return safety.is_never_send(c) or safety.is_expand_command(c)


def _is_int(v: object) -> bool:
    return type(v) is int


def _is_real(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
