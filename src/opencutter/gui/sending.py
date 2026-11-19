# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""From the placed design and the chosen preset to a JobSpec, and the rules for Send.

Pure Python: the GUI widgets call these, and the tests check them without a display.

Machine profiles (opencutter.machines)
  The layout is made for one machine (GuiSettings.machine) and one of its mats. Every function
  that needs the machine takes ``profile`` (a MachineProfile) or ``machine`` (its id), and the
  default is the CAMEO 4 with its 12 x 12 in mat, so the CAMEO 4 path is exactly as before.
    job_spec(..., machine, mat), spec_for_test_cut(..., machine, mat)
                         JobSpec.machine is the layout's machine id and JobSpec.mat the chosen
                         mat (default: the machine's default_mat). The service refuses a job
                         made for another machine than the connected one; the app blocks Send
                         first (LayoutMachine).
    supported_presets(store, profile)
                         the rows the machine may use (PresetStore.for_profile), without the
                         Rotary Cut and Punch rows. Empty for a machine without materials:
                         materials_note() gives NO_MATERIALS and NO_MATERIALS_HINT, which point
                         to Cut Settings (starting_settings() gives their first values).
    tool_check(settings, machine_state, profile)
                         per holder of that profile; a holder the profile does not have is
                         never "known" and blocks Send (ToolCheck.exists).
    LayoutMachine        the layout's machine against the detected one: when a machine of
                         another profile is detected, the layout switches to it (follow), and
                         Send and Test Cut stay blocked until the user presses "Use {name}"
                         (use). The offline Page Setup choice is choose().
    send_blocker(..., layout=, mat=), blocker_for_test_cut(..., layout=, mat=)
                         as before, plus that block, a Cutting Mat the machine may use
                         (mat_offered, CHOOSE_MAT) and the cut area of that mat. Without
                         `layout` the layout is the CAMEO 4's, the machine job_spec() defaults
                         to: a connected machine of another profile blocks Send (LAYOUT_MISMATCH).

Pure helpers for the widgets
  tool_cards(profile)    one HolderCard (what a Tool card shows) per holder: its number, its
                         label ('Tool 1', or 'Tool' on a one-holder model) and the tools jobs
                         may use there.
  slider_ranges(profile, holder, tool_code)
                         the Cut Settings slider ranges for that tool: Blade Depth only for a
                         tool that takes one, Acceleration None on a model without it (the row
                         is hidden), Offset and Overcut only for blade tools.
"""
from __future__ import annotations

from dataclasses import dataclass

from opencutter.device.protocol import tool_name
from opencutter.job import builder
from opencutter.job.plan import blade_depth_mm, blade_depth_text, build_plan
from opencutter.job.spec import DEFAULT_MACHINE, CutGroup, JobSpec, ToolSettings
from opencutter.machines import CAMEO4, MachineProfile, Tool
from opencutter.presets.model import Preset, PresetStore

from .document import PlacedDocument
from .geometry import Rect, placement_problem, test_cut_spot
from .machine import NOT_SUPPORTED, MachineState, chosen_profile


# Out of scope for now (ARCHITECTURE.md): the Rotary Blade and the Punch Tool need their own
# job handling, so their presets are not offered. The codes are the CAMEO 4's unusable tools
# (MachineProfile.unsupported_codes(); the same on every model OpenCutter drives).
UNSUPPORTED_TOOL_CODES = CAMEO4.unsupported_codes()
UNSUPPORTED_ACTIONS = frozenset({"Rotary Cut", "Punch"})
CHOOSE_MATERIAL = "Choose a material first."
# a machine whose view of the presets is empty (no Basic set, no rows in the presets file)
NO_MATERIALS = "No materials for this machine yet"
NO_MATERIALS_HINT = ("Set the tool, Force and Speed in Cut Settings, then make a Test Cut. A "
                     "presets file can add materials for this machine.")
# the detected machine is another than the layout's: the layout switched to it, and nothing is
# sent until the user accepts it
USE_MACHINE = "Use {name}"
MACHINE_SWITCHED = ("The connected machine is a {name}. The layout was made for the {old}, so "
                    "it now uses the {name}’s cutting mat; your design stays where it is. Check "
                    "it, then press Use {name}.")
CONFIRM_MACHINE = "Check the layout for the {name}, then press Use {name}."
# the detected machine is another than the layout's and the layout did not switch to it (a
# window that does not follow the detected machine: its jobs are made for the CAMEO 4)
LAYOUT_MISMATCH = "The layout was made for the {layout}, but the connected machine is a {name}."
NO_SUCH_HOLDER = "The {name} has no {label}."
# the layout's Cutting Mat is not one the machine may use (Mat.offered at its own level)
CHOOSE_MAT = "Choose a Cutting Mat for the {name} in Page Setup."
# why Send waits for the design's place (geometry.placement_problem): the cut area comes first
OUTSIDE_CUT_AREA = "Move the design inside the cut area."
OFF_MEDIA = "Move the design onto the media, or set Media Size to your sheet in Page Setup."
PLACEMENT_BLOCKERS = {"cut-area": OUTSIDE_CUT_AREA, "media": OFF_MEDIA}


def is_supported(preset: Preset, profile: MachineProfile = CAMEO4) -> bool:
    """True if the app can send this preset (not a Rotary Cut or Punch preset)."""
    return (preset.tool_code not in profile.unsupported_codes()
            and preset.action not in UNSUPPORTED_ACTIONS)


def supported_presets(store: PresetStore, profile: MachineProfile = CAMEO4) -> PresetStore:
    """The presets the machine `profile` may use (PresetStore.for_profile: the rows that name
    it) that the app can send; the source is kept. For a store of CAMEO 4 rows and the CAMEO 4
    this is the whole store but the Rotary Cut and Punch rows, as before."""
    rows = store.for_profile(profile).presets
    return PresetStore([p for p in rows if is_supported(p, profile)], store.source)


def materials_note(store: PresetStore) -> str | None:
    """NO_MATERIALS for a machine's view of the presets that is empty, else None. The Send panel
    then shows NO_MATERIALS_HINT, which points to Cut Settings."""
    return None if store.presets else NO_MATERIALS


@dataclass(frozen=True)
class ToolCheck:
    """The tool the chosen Material and Action need, against what the machine reports."""
    holder: int
    required_code: int
    detected_code: int | None      # None while the machine has not reported its tools
    profile: MachineProfile = CAMEO4

    @property
    def exists(self) -> bool:
        """True when the machine's profile has this holder."""
        return self.profile.holder(self.holder) is not None

    @property
    def label(self) -> str:
        """'Tool 1', 'Tool 2', or 'Tool' on a one-holder model."""
        h = self.profile.holder(self.holder)
        return h.label if h is not None else f"Tool {self.holder}"

    @property
    def required_name(self) -> str:
        """Studio name of the required tool."""
        return tool_name(self.holder, self.required_code, self.profile)

    @property
    def known(self) -> bool:
        """True once the machine reported the tool in this holder."""
        return self.detected_code is not None

    @property
    def matches(self) -> bool:
        """True when the required tool is detected in its holder."""
        return self.detected_code == self.required_code

    @property
    def mismatch_text(self) -> str:
        """The instruction to fix a mismatch, e.g. 'Load the AutoBlade into Tool 1.' ('... into
        the tool holder.' on a one-holder model)."""
        if len(self.profile.holders) == 1:
            return f"Load the {self.required_name} into the tool holder."
        return f"Load the {self.required_name} into {self.label}."


def tool_check(settings: ToolSettings, machine: MachineState,
               profile: MachineProfile | None = None) -> ToolCheck:
    """Compare the settings' tool with the machine's report. `profile`: the layout's machine
    (default: the machine the state names, MachineState.machine_profile)."""
    profile = profile or machine.machine_profile
    return ToolCheck(settings.holder, settings.tool_code, machine.tool_code(settings.holder),
                     profile)


# --- the material's preset and how its settings read ------------------------------------
def verified_preset(store: PresetStore) -> Preset | None:
    """The first preset verified on a real machine (the Basic set's vinyl), or None. With no
    material remembered, the Send panel starts on this one or on none: an unverified preset is
    never chosen for the user."""
    return next((p for p in store.presets if p.verified), None)


def choose_preset(store: PresetStore, material: str | None, action: str | None = None,
                  holder: int | None = None) -> Preset | None:
    """The preset the Send panel selects for ``material``.

    The action stays ``action`` when the material has it, else it is the material's first one;
    the holder stays ``holder`` when that action uses it, else it is that action's first
    holder. The machine's tools play no part: the material list shows the settings of this
    preset, so what it shows is what picking the material applies, and the Tool cards then
    say if another tool must be loaded. ``store`` is the machine's view (supported_presets)."""
    presets = store.presets_for(material) if material else []
    actions = list(dict.fromkeys(p.action for p in presets))
    if not actions:
        return None
    if action not in actions:
        action = actions[0]
    rows = [p for p in presets if p.action == action]
    holders = sorted({p.holder for p in rows})
    if holder not in holders:
        holder = holders[0]
    return next(p for p in rows if p.holder == holder)


def settings_summary(s: ToolSettings, with_passes: bool = True) -> str:
    """'Blade Depth 1 · Force 10 · Speed 5 · 1 pass' (or '... · Accel. 3' without passes).
    Blade Depth only for the AutoBlade: no other tool carries a depth value."""
    parts = [] if s.blade_depth is None else [blade_depth_text(s.blade_depth)]
    parts += [f"Force {s.force}", f"Speed {s.speed}"]
    if with_passes:
        parts.append(f"{s.passes} pass" + ("" if s.passes == 1 else "es"))
    else:
        parts.append(f"Accel. {s.acceleration}")
    return " · ".join(parts)


def depth_note(s: ToolSettings | None) -> str | None:
    """'Blade Depth 1 = 0.1 mm' for a tooltip, or None when the tool has no depth value."""
    if s is None or s.blade_depth is None:
        return None
    return f"{blade_depth_text(s.blade_depth)} = {blade_depth_mm(s.blade_depth)}"


def preset_line(preset: Preset, profile: MachineProfile = CAMEO4) -> str:
    """'Cut · AutoBlade · Blade Depth 1 · Force 10 · Speed 5': the settings a preset applies,
    for the material list. Blade Depth only when the tool has one; passes only when > 1."""
    s = preset.to_tool_settings()
    parts = [preset.action, tool_name(preset.holder, preset.tool_code, profile)]
    if s.blade_depth is not None:
        parts.append(blade_depth_text(s.blade_depth))
    parts += [f"Force {s.force}", f"Speed {s.speed}"]
    if s.passes > 1:
        parts.append(f"{s.passes} passes")
    return " · ".join(parts)


# --- tool cards and Cut Settings -------------------------------------------------------
@dataclass(frozen=True)
class HolderCard:
    """What one Tool card shows: a holder of the machine and the tools jobs may use there."""
    holder: int
    label: str                             # 'Tool 1', 'Tool 2', or 'Tool'
    tools: tuple[tuple[int, str], ...]     # (ESC 15 code, Studio name), usable tools only


def _usable(tool: Tool) -> bool:
    return tool.usable and tool.code != 0


def tool_cards(profile: MachineProfile = CAMEO4) -> tuple[HolderCard, ...]:
    """One Tool card per holder of `profile`, in order (two on the CAMEO 4, one labelled
    'Tool' on a one-holder model)."""
    return tuple(HolderCard(h.number, h.label,
                            tuple((t.code, t.name) for t in h.tools if _usable(t)))
                 for h in profile.holders)


def slider_ranges(profile: MachineProfile = CAMEO4, holder: int = 1,
                  tool_code: int | None = None) -> dict[str, tuple[int, int] | None]:
    """The Cut Settings ranges (lo, hi) for a tool of `profile`: blade_depth (None unless the
    tool takes a Blade Depth), force, speed, acceleration (None: the model has none, hide the
    row), passes, and offset and overcut (None for a tool that is not a blade). With no
    `tool_code`, the holder's depth tool counts (the AutoBlade in Tool 1)."""
    r = profile.ranges
    h = profile.holder(holder)
    tool = h.tool(tool_code) if h is not None and tool_code is not None else None
    if tool is None and h is not None and tool_code is None:
        tool = next((t for t in h.tools if t.depth), None)
    blade = tool is not None and tool.blade
    return {"blade_depth": r.blade_depth.as_tuple() if tool is not None and tool.depth else None,
            "force": r.force.as_tuple(),
            "speed": r.speed.as_tuple(),
            "acceleration": r.acceleration.as_tuple() if r.acceleration is not None else None,
            "passes": r.passes.as_tuple(),
            "offset": r.offset.as_tuple() if blade else None,
            "overcut": r.overcut.as_tuple() if blade else None}


def _within(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def starting_settings(profile: MachineProfile = CAMEO4, holder: int | None = None,
                      tool_code: int | None = None) -> ToolSettings:
    """First Cut Settings for a machine without materials, to start a Test Cut from: the
    holder's depth tool (else its first usable blade, else its first usable tool), the lowest
    Force, Speed 5 and Acceleration 3 (within the machine's ranges, as in the Basic set), Blade
    Depth 1, the CAMEO 4's blade Offset 18 and Overcut 1, 1 for a blade (0 otherwise), 1 pass.
    A light start: the user raises the Force after the Test Cut."""
    h = profile.holder(holder) if holder is not None else profile.holders[0]
    if h is None:
        raise ValueError(f"the {profile.name} has no holder {holder!r}")
    usable = [t for t in h.tools if _usable(t)]
    if tool_code is not None:
        tool = h.tool(tool_code)
    else:
        tool = (next((t for t in usable if t.depth), None)
                or next((t for t in usable if t.blade), None) or (usable[0] if usable else None))
    if tool is None or not _usable(tool):
        raise ValueError(f"{h.label} of the {profile.name} has no usable tool {tool_code!r}")
    r = profile.ranges
    accel = _within(3, *r.acceleration.as_tuple()) if r.acceleration is not None else 1
    return ToolSettings(holder=h.number, tool_code=tool.code,
                        speed=_within(5, *r.speed.as_tuple()), force=r.force.lo,
                        acceleration=accel,
                        offset=_within(18, *r.offset.as_tuple()) if tool.blade else 0,
                        line_segment_overcut=False,
                        overcut=(_within(1, *r.overcut.as_tuple()),) * 2 if tool.blade else (0, 0),
                        blade_depth=_within(1, *r.blade_depth.as_tuple()) if tool.depth else None,
                        passes=1)


# --- the job ---------------------------------------------------------------------------
def _mat(machine: str, mat: str | None) -> str:
    """The mat key of a JobSpec: `mat`, or the machine's default_mat for None."""
    return mat if mat is not None else chosen_profile(machine).default_mat


def job_spec(doc: PlacedDocument, media: Rect, settings: ToolSettings,
             preview_outline: bool, machine: str = DEFAULT_MACHINE,
             mat: str | None = None) -> JobSpec:
    """The JobSpec for the placed design, cut with one condition block (Action by: Simple),
    made for the layout's machine (a profile id) and its chosen mat (default: its
    default_mat)."""
    return JobSpec(mat=_mat(machine, mat), media=tuple(media),
                   groups=[CutGroup(settings=settings, paths=doc.polylines())],
                   preview_outline=preview_outline, machine=machine)


def spec_for_test_cut(media: Rect, settings: ToolSettings, machine: str = DEFAULT_MACHINE,
                      mat: str | None = None) -> JobSpec:
    """The JobSpec for a Test Cut at the default spot of the media, with the given settings,
    made for the layout's machine and mat."""
    key = _mat(machine, mat)
    profile = chosen_profile(machine)
    x, y = test_cut_spot(media, key if profile.mat(key) is not None else None, profile)
    return JobSpec(mat=key, media=tuple(media),
                   groups=[CutGroup(settings=settings, paths=builder.test_cut_contours(x, y))],
                   preview_outline=False, machine=machine)


def plan_summary(spec: JobSpec) -> tuple[dict | None, str | None]:
    """Validate and plan locally, for the machine the spec names: (summary, None) or (None,
    the user-facing reason)."""
    try:
        return build_plan(spec).summary, None
    except ValueError as e:              # UnsafeJob is a ValueError
        return None, str(e)


# --- the layout's machine against the detected one -------------------------------------
class LayoutMachine:
    """Which machine the layout is made for (GuiSettings.machine), against the one the service
    detected. The detected machine always wins: when it is another profile, the
    layout switches to it (the window then re-clamps the media to its mat and keeps the
    design), shows MACHINE_SWITCHED with the button USE_MACHINE, and Send and Test Cut stay
    blocked (CONFIRM_MACHINE) until the user presses it (use()); before follow() they are
    blocked as well (LAYOUT_MISMATCH). A machine whose profile the service did not report
    changes nothing; the service still refuses a job made for another machine. The window
    keeps GuiSettings.machine (set_machine) and MachineMonitor.set_choice in step with
    `machine`, and makes its JobSpecs for it (job_spec(..., machine=...))."""

    def __init__(self, machine: str = DEFAULT_MACHINE):
        """`machine`: the layout's machine id (an unknown or recognized id gives the CAMEO 4)."""
        self.machine = chosen_profile(machine).id
        self.pending: str | None = None    # switched to by itself, not yet accepted (Use)
        self.previous: str | None = None   # what the layout was made for before that switch

    @property
    def profile(self) -> MachineProfile:
        """The layout's machine profile."""
        return chosen_profile(self.machine)

    def choose(self, machine: str) -> None:
        """The user picked the layout's machine (Page Setup, offline): nothing to accept."""
        self.machine = chosen_profile(machine).id
        self.pending = self.previous = None

    def follow(self, state: MachineState) -> bool:
        """Follow the detected machine. True when the layout switched to it (re-clamp the media,
        show the banner); then Send waits for use()."""
        detected = state.detected
        if detected is None or detected.id == self.machine:
            return False
        self.previous, self.machine = self.machine, detected.id
        self.pending = detected.id
        return True

    def use(self) -> None:
        """The user pressed "Use {name}": the layout's machine is accepted."""
        self.pending = self.previous = None

    @property
    def button(self) -> str | None:
        """'Use CAMEO 5' while a switch waits for the user, else None."""
        return USE_MACHINE.format(name=self.profile.name) if self.pending else None

    @property
    def banner(self) -> str | None:
        """MACHINE_SWITCHED while a switch waits for the user, else None."""
        if not self.pending:
            return None
        old = chosen_profile(self.previous).name if self.previous else self.profile.name
        return MACHINE_SWITCHED.format(name=self.profile.name, old=old)

    def blocker(self, state: MachineState) -> str | None:
        """Why nothing may be sent for this layout to the connected machine, or None: the
        detected machine differs from the layout's (LAYOUT_MISMATCH: follow() was not called),
        or a switch to it is not accepted yet (CONFIRM_MACHINE)."""
        detected = state.detected
        if detected is None:
            return None
        if detected.id != self.machine:
            return LAYOUT_MISMATCH.format(layout=self.profile.name, name=detected.name)
        if self.pending == detected.id:
            return CONFIRM_MACHINE.format(name=detected.name)
        return None


def mat_offered(profile: MachineProfile, mat: object) -> bool:
    """True when `mat` is a key of a mat `profile` may use: one of its mats, offered at the
    profile's own level (Mat.offered; the job validator's rule)."""
    m = profile.mat(mat) if isinstance(mat, str) else None
    return m is not None and m.offered(profile.support)


def send_blocker(doc: PlacedDocument | None, media: Rect, machine: MachineState,
                 check: ToolCheck | None, job_running: bool,
                 material_chosen: bool = True, *, layout: LayoutMachine | None = None,
                 mat: str | None = None) -> str | None:
    """Why Send is disabled right now, or None when it may be pressed. A machine the service
    refused for its model comes first: nothing on screen can change that. A missing material
    comes before a missing design: it is the one choice the app never makes for the user.
    `layout` is the layout's machine (default: the CAMEO 4, the machine job_spec() makes a job
    for by default) and `mat` its mat (default: that machine's default_mat): the design must
    lie in that mat's cut area."""
    if job_running:
        return "A job is already running."
    if machine.wrong_model:
        return NOT_SUPPORTED
    if not material_chosen:
        return CHOOSE_MATERIAL
    if doc is None:
        return "Open a design first."
    if doc.is_empty:
        return "The design has no paths to cut."
    profile = layout.profile if layout is not None else CAMEO4
    key = mat if mat is not None else profile.default_mat
    why = blocker_for_test_cut(machine, check, False, material_chosen, layout=layout, mat=key)
    return why or PLACEMENT_BLOCKERS.get(placement_problem(doc.bbox(), media, key, profile))


def blocker_for_test_cut(machine: MachineState, check: ToolCheck | None, job_running: bool,
                         material_chosen: bool = True, *, layout: LayoutMachine | None = None,
                         mat: str | None = None) -> str | None:
    """Why Test Cut is disabled right now, or None when it may be pressed. `layout` is the
    layout's machine (default: the CAMEO 4, as job_spec() and spec_for_test_cut() default to):
    nothing is sent while the detected machine is another one. `mat` is the layout's Cutting
    Mat (default: that machine's default_mat): one the machine may use (CHOOSE_MAT)."""
    if job_running:
        return "A job is already running."
    if machine.wrong_model:              # the service refused the machine for its model
        return NOT_SUPPORTED
    if not material_chosen:
        return CHOOSE_MATERIAL
    if machine.blocker:                  # e.g. a background service of another version
        return machine.blocker
    if not machine.connected:
        return "Connect the machine first."
    layout = layout if layout is not None else LayoutMachine()
    if why := layout.blocker(machine):
        return why
    if mat is not None and not mat_offered(layout.profile, mat):
        return CHOOSE_MAT.format(name=layout.profile.name)
    if check is None:
        return "Choose a material and action."
    if not check.exists:
        return NO_SUCH_HOLDER.format(name=check.profile.name, label=f"Tool {check.holder}")
    if not check.known:                  # a reply it cannot read for this model says so
        return machine.tools_problem or "Waiting for the machine to report its tools."
    if not check.matches:
        return f"{check.mismatch_text[:-1]} to send."
    return None
