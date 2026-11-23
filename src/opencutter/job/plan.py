# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Step plan for one job: the writes, in order, with what the user should see at each step.

The daemon runs the steps one by one. It waits until the machine is ready before every step
and after every batch of writes, so each group gets its own ready wait after its setup (TG,
J and TF move the hardware) and after its cut.

Machine profiles. build_plan(spec, profile=None) builds with the profile the spec names
(JobSpec.machine) or with `profile`, the machine on the link; validate.job_profile has the
rules, and an unknown, recognized or mismatching machine is refused. The profile gives the
dialect of every write (builder), the write size (max_write), the texts (holder labels, tool
names and actions, the mat's feed and expected U reply) and the time estimate (speed_mm_s).
The plan also carries what the daemon needs to run it on that machine:
  profile_id         the profile it was built for: the daemon sends nothing unless it is the
                     connected machine's.
  units_expected     the U reply (a, b) the mat gives right after the header, compared in
                     order; None when it is not known.
  units_enforced     True when that reply is KNOWN or INFERRED: any other reply refuses the job
                     before the cut. False for an UNKNOWN reply, which only an UNTESTED profile
                     offers: the reply is logged and shown, not enforced, and the units step
                     and the summary say that the cut area could not be checked.
  batch_bytes        the most bytes queued before the next ready wait.
  slowest_su_per_s   the lowest speed in plotter units per second: bounds how long a batch runs.
  max_coordinate_su  the largest coordinate a move may have (check_plan_sequence reads it).
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from opencutter.i18n import N_
from opencutter.machines import CAMEO4, MachineProfile, Mat

from . import builder
from .builder import CutterPoint
from .spec import CutGroup, JobSpec, ToolSettings
from .validate import UnsafeJob, bind_job, check_plan_sequence

UNITS_QUERY = b"U\x03"
# Rough timing model for the estimate (seconds; not measured). Every move, tool up or down,
# is assumed to run at the group's Speed (cm/s).
HEADER_S = 5.0          # the TG feed and the FN0 return
SETUP_S = 2.0           # holder change (J)
AUTOBLADE_S = 10.0      # TF at the AutoBlade station
SEGMENT_S = 0.02        # acceleration and braking per line segment
TRAILER_S = 2.0
# The CAMEO 4's action words by tool code (an alias of Tool.action for older callers).
_ACTIONS = {t.code: t.action for h in CAMEO4.holders for t in h.tools if t.action != "Cut"}


@dataclass(frozen=True)
class PlanStep:
    """One step: a "write" step (GPGL writes, sent in order) or the "units" query step."""
    kind: str
    writes: list[bytes]
    title: str
    expect: str
    confirm: bool = False            # the user must confirm (after the outline preview)


@dataclass
class Plan:
    """A validated job: its steps, the tool each used holder must report (ESC 15), the
    byte-level validator bound to this job, a JSON-safe summary for the UI, and the media box
    (a_min, b_min, a_max, b_max in SU, `media_box_su` of its spec) that every D must start in
    (`check_plan_sequence`). The other fields come from the machine profile and the mat (see
    the module docstring); their defaults are the CAMEO 4's, with a unit check that cannot
    pass (nothing expected, but enforced)."""
    steps: list[PlanStep]
    required_tools: dict[int, int]
    validator: Callable[[bytes], None]
    summary: dict
    media_box: tuple[int, int, int, int]
    profile_id: str = CAMEO4.id
    units_expected: tuple[int, int] | None = None
    units_enforced: bool = True
    batch_bytes: int = CAMEO4.batch_bytes
    slowest_su_per_s: float = CAMEO4.slowest_su_per_s
    max_coordinate_su: int = CAMEO4.max_coordinate_su


def build_plan(spec: JobSpec, profile: MachineProfile | None = None) -> Plan:
    """Validate `spec` and build its steps: header, U check, then per group the setup and the
    cut (the outline preview after the first setup, if enabled), then the trailer. The
    machine is the profile `spec.machine` names, or `profile` (the machine on the link), which
    must have the same id. Raises UnsafeJob if the spec is not safe to send to it."""
    job = bind_job(spec, profile)                   # runs validate_spec first
    validator, profile, mat = job.validator, job.profile, job.mat
    work = [(g, cs) for g in spec.groups if (cs := builder.cutter_contours(g, profile))]
    if not work:
        raise UnsafeJob("the job has nothing to cut")
    box = _bbox(cs for _, cs in work)
    preview = spec.preview_outline

    steps = [_header_step(spec.mat, mat, profile), _units_step(mat, profile)]
    for i, (group, contours) in enumerate(work):
        steps.append(_setup_step(spec.mat, group.settings, i == 0, profile))
        if i == 0 and preview:
            steps.append(_preview_step(box, profile))
        steps.append(_cut_step(group, contours, i == 0 and preview, profile))
    steps.append(_trailer_step(profile))

    for step in steps:                              # the plan must pass its own validator
        if step.kind == "write":
            for w in step.writes:
                validator(w)
    plan = Plan(steps=steps,
                required_tools={g.settings.holder: g.settings.tool_code for g, _ in work},
                validator=validator,
                summary=_summary(spec, work, box, steps, mat, profile),
                media_box=job.media_box,
                profile_id=profile.id,
                units_expected=mat.expected_u,
                units_enforced=mat.enforce_u,
                batch_bytes=profile.batch_bytes,
                slowest_su_per_s=profile.slowest_su_per_s,
                max_coordinate_su=profile.max_coordinate_su)
    check_plan_sequence(plan)                       # every D starts from an in-media M/D
    return plan


def tool_name(s: ToolSettings, profile: MachineProfile = CAMEO4) -> str:
    """Studio's name of the tool the settings need."""
    return profile.tool_name(s.holder, s.tool_code)


def action_name(s: ToolSettings, profile: MachineProfile = CAMEO4) -> str:
    """Studio's action word for the tool: Cut, Sketch, Kraft Cut, ..."""
    tool = profile.tool(s.holder, s.tool_code)
    return tool.action if tool is not None else "Cut"


def blade_depth_mm(depth: int) -> str:
    """The AutoBlade's depth for a Blade Depth (TF) value, 0.1 mm per step: 1 -> '0.1 mm'."""
    return f"{depth / 10:.1f} mm"


def blade_depth_text(depth: int, mm: bool = False) -> str:
    """Studio's 'Blade Depth 1' (the TF value); with ``mm``, 'Blade Depth 1 (0.1 mm)'. The app
    shows the depth this way everywhere: the number, and the millimetres in brackets or in a
    tooltip where space is short."""
    text = f"Blade Depth {depth}"
    return f"{text} ({blade_depth_mm(depth)})" if mm else text


# --- steps ------------------------------------------------------------------------------------
# The step texts are made from templates marked with N_(): the window shows them in its own
# language (i18n.translate_text); the service and its log keep them in English.
def _header_step(key: str, mat: Mat, profile: MachineProfile) -> PlanStep:
    if mat.feed_mm is None:
        expect = N_("The mat may feed in and the carriage moves a little. The tool stays up.")
    else:
        expect = N_("The mat may feed in about {feed} mm and the carriage moves a little. The "
                    "tool stays up.").format(feed=f"{mat.feed_mm:g}")
    return PlanStep("write", builder.chunk(builder.mat_commands(key, profile),
                                           limit=profile.max_write),
                    N_("Header: {mat} cutting mat").format(mat=key), expect)


def _units_step(mat: Mat, profile: MachineProfile) -> PlanStep:
    if mat.enforce_u:
        a, b = mat.expected_u
        expect = N_("The machine should reply {a},{b}: {unit} mm per unit, mat command "
                    "accepted.").format(a=a, b=b, unit=f"{profile.unit_mm:g}")
    else:
        expect = N_("The reply is logged and shown but not checked: the {machine}'s reply with "
                    "the {mat} mat is not known, so the cut area could not be checked on this "
                    "model.").format(machine=profile.name, mat=mat.label)
    return PlanStep("units", [UNITS_QUERY], N_("Check units (no motion)"), expect)


def _setup_step(key: str, s: ToolSettings, first: bool, profile: MachineProfile) -> PlanStep:
    cmds = builder.area_commands(key, profile) if first else []
    cmds += builder.condition_block(s, profile)
    holder = profile.holder(s.holder)
    tool = tool_name(s, profile)
    if s.blade_depth is None:
        title = N_("{holder}: {tool}, Force {force}, Speed {speed}").format(
            holder=holder.label, tool=tool, force=s.force, speed=s.speed)
    else:
        title = N_("{holder}: {tool}, Blade Depth {depth} ({mm}), Force {force}, Speed "
                   "{speed}").format(holder=holder.label, tool=tool, depth=s.blade_depth,
                                     mm=blade_depth_mm(s.blade_depth), force=s.force,
                                     speed=s.speed)
    several = len(profile.holders) > 1
    if s.blade_depth is None and several:
        expect = N_("{holder} is selected; the carriage may move. Nothing touches the "
                    "media.").format(holder=holder.label)
    elif s.blade_depth is None:
        expect = N_("The tool settings are sent; the carriage may move. Nothing touches the "
                    "media.")
    elif several:                                   # holder 1 is the left one
        expect = N_("{holder} is selected; the carriage may move. Nothing touches the media. "
                    "Then the {tool} is set at its station on the left (about 10 s).").format(
            holder=holder.label, tool=tool)
    else:
        expect = N_("The tool settings are sent; the carriage may move. Nothing touches the "
                    "media. Then the {tool} is set at its station (about 10 s).").format(tool=tool)
    return PlanStep("write", builder.chunk(cmds, limit=profile.max_write), title, expect)


def _preview_step(box: tuple[int, int, int, int], profile: MachineProfile) -> PlanStep:
    a0, b0, a1, b1 = box
    corners = [(a0, b0), (a0, b1), (a1, b1), (a1, b0), (a0, b0)]
    return PlanStep("write", builder.chunk(builder.moves(corners), limit=profile.max_write),
                    N_("Preview outline (tool up)"),
                    N_("The tool stays up and traces the outer box of the design. Check that it "
                       "stays over the media."))


def _cut_step(group: CutGroup, contours: list[list[CutterPoint]], confirm: bool,
              profile: MachineProfile) -> PlanStep:
    s = group.settings
    paths = len(contours)
    length = f"{_cut_length_mm(s, contours, profile):.0f}"
    if paths == 1 and s.passes > 1:
        expect = N_("The tool goes down: 1 path, {length} mm, {passes} passes.")
    elif s.passes > 1:
        expect = N_("The tool goes down: {paths} paths, {length} mm, {passes} passes.")
    elif paths == 1:
        expect = N_("The tool goes down: 1 path, {length} mm.")
    else:
        expect = N_("The tool goes down: {paths} paths, {length} mm.")
    expect = expect.format(paths=paths, length=length, passes=s.passes)
    if len(profile.holders) > 1:
        title = N_("{action} with {holder} ({tool})").format(
            action=action_name(s, profile), holder=profile.holder(s.holder).label,
            tool=tool_name(s, profile))
    else:
        title = N_("{action} with the {tool}").format(action=action_name(s, profile),
                                                      tool=tool_name(s, profile))
    return PlanStep("write", builder.chunk(builder.path(contours, s.passes),
                                           limit=profile.max_write),
                    title, expect, confirm=confirm)


def _trailer_step(profile: MachineProfile) -> PlanStep:
    return PlanStep("write", builder.chunk(builder.trailer(profile), limit=profile.max_write),
                    N_("Return to Origin"),
                    N_("The tool moves up to the origin. The mat stays loaded; unload it on the "
                       "machine."))


# --- summary ----------------------------------------------------------------------------------
def _summary(spec: JobSpec, work: list[tuple[CutGroup, list[list[CutterPoint]]]],
             box: tuple[int, int, int, int], steps: list[PlanStep], mat: Mat,
             profile: MachineProfile) -> dict:
    """JSON-safe summary: lengths in mm, a rough time estimate, bbox [x, y, w, h] in
    document mm, the groups and the passes (the largest pass count of any group), the machine
    and whether the unit check is enforced (False: the cut area could not be checked)."""
    per_mm = profile.su_per_mm
    a0, b0, a1, b1 = box
    seconds = HEADER_S + TRAILER_S
    pos: CutterPoint = (0, 0)
    total_down = total_up = 0.0
    groups = []
    for i, (group, contours) in enumerate(work):
        s = group.settings
        mm_s = profile.speed_mm_s(s.speed)
        down = _cut_length_mm(s, contours, profile)
        up = 0.0
        for _ in range(s.passes):
            for c in contours:
                up += math.dist(pos, c[0]) / per_mm
                pos = c[-1]
        segments = sum(len(c) - 1 for c in contours) * s.passes
        seconds += SETUP_S + (AUTOBLADE_S if s.blade_depth is not None else 0.0)
        seconds += (down + up) / mm_s + segments * SEGMENT_S
        if i == 0 and spec.preview_outline:
            seconds += 2 * ((a1 - a0) + (b1 - b0)) / per_mm / mm_s
        total_down += down
        total_up += up
        groups.append({"holder": s.holder, "tool_code": s.tool_code,
                       "tool": tool_name(s, profile), "action": action_name(s, profile),
                       "settings": s.to_dict(), "paths": len(contours),
                       "path_length_mm": round(down, 1), "passes": s.passes})
    back = math.dist(pos, (0, 0)) / per_mm
    seconds += back / profile.speed_mm_s(work[-1][0].settings.speed)
    writes = [w for st in steps if st.kind == "write" for w in st.writes]
    return {
        "machine": profile.id,
        "mat": spec.mat,
        "media": list(spec.media),
        "bbox": [b0 / per_mm, a0 / per_mm, (b1 - b0) / per_mm, (a1 - a0) / per_mm],
        "path_length_mm": round(total_down, 1),
        "travel_length_mm": round(total_up + back, 1),
        "estimated_seconds": round(seconds),
        "passes": max(g["passes"] for g in groups),
        "groups": groups,
        "writes": len(writes),
        "bytes": sum(map(len, writes)),
        "units_enforced": mat.enforce_u,
    }


def _bbox(contour_sets: Iterable[list[list[CutterPoint]]]) -> tuple[int, int, int, int]:
    """(a_min, b_min, a_max, b_max) of every point, in SU."""
    pts = [p for cs in contour_sets for c in cs for p in c]
    a = [p[0] for p in pts]
    b = [p[1] for p in pts]
    return min(a), min(b), max(a), max(b)


def _cut_length_mm(s: ToolSettings, contours: Sequence[Sequence[CutterPoint]],
                   profile: MachineProfile) -> float:
    """Tool-down length of all passes, in mm."""
    su = sum(math.dist(p, q) for c in contours for p, q in zip(c, c[1:]))
    return su * s.passes / profile.su_per_mm
