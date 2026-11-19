# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Pinned test programs: dry-run, pen-test and cut-test, per machine profile.

Every motion byte string here is fixed and was checked against the V5.05 firmware and on the
real machine. The daemon writes nothing else for a program: the program's validator refuses
any bytes not in that profile's pinned writes, and each pinned write is checked at import
against a strict grammar of exactly the commands these programs use, and against the global
safety rules for its profile (machines.safety.check_profile(profile, pinned=...)).

Only the CAMEO 4 has pinned programs. They are keyed by profile id: PINNED_PROGRAMS (a
PinnedPrograms per profile: programs, required tools, mat, grammar, test area, validator) and
PROGRAMS_BY_PROFILE (the programs alone). The CAMEO 4 names (PROGRAMS, DRY_RUN, CUT_TEST,
REQUIRED_TOOLS, PINNED_WRITES, TEST_AREA, validate_pinned) stay as aliases with today's bytes.
A profile without programs has none: build_program(..., profile) refuses it, and a profile may
list a program name (MachineProfile.programs) only when its bytes are pinned here. Pinned bytes
of another model are written here as literals after review, never generated at run time.

A Program has the same shape as opencutter.job.plan.Plan (steps, required_tools, validator,
summary, profile_id, units_expected, units_enforced, batch_bytes, slowest_su_per_s,
max_coordinate_su), so the daemon runs both through one session machinery.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from ..machines import CAMEO4, MachineProfile, Mat, profiles, safety
from .protocol import ESC, ETX, UnsafeCommand, lowers_tool

# The CAMEO 4's pinned grammar: exactly the commands its programs use, and the test area every
# pinned M/D stays in.
_PINNED_CMD = re.compile(
    rb"TG1|FN0|TB50,0|\\(0|30),(0|30)|Z6066,6066|J1"
    rb"|!(1|5),1|FX(1|7|10),1|TJ(1|3)|FC(0|18),1,1|FE0,1|FF(0,0|1,1),1|TF1,1"
    rb"|[MD][0-9]{1,4},[0-9]{1,4}")
TEST_AREA = (1600, 3400)   # max a, b (SU) of any pinned M/D: 80 mm x 170 mm from the origin
_PINNED_MOVE = re.compile(rb"[MD](0|[1-9][0-9]{0,4}),(0|[1-9][0-9]{0,4})")   # whatever the grammar


@dataclass(frozen=True)
class Step:
    """One operator-confirmed step; same fields as opencutter.job.plan.PlanStep."""
    kind: str                   # "write" (pinned GPGL bytes) or "units" (U query, no motion)
    writes: tuple[bytes, ...]
    title: str
    expect: str
    confirm: bool = False


@dataclass(frozen=True)
class Program:
    """A resolved pinned program; quacks like opencutter.job.plan.Plan."""
    name: str
    options: dict
    steps: tuple[Step, ...]
    required_tools: dict[int, int]           # holder -> ESC 15 code needed before D or TF
    allow_unrecognized: dict[int, bool] = field(default_factory=dict)   # accept code 0 there
    profile_id: str = CAMEO4.id              # the profile the bytes are pinned for; the daemon
                                             # runs it only when it equals the detected one

    def _pinned(self) -> PinnedPrograms | None:
        """The pinned programs of profile_id, or None (no profile or no programs)."""
        return PINNED_PROGRAMS.get(self.profile_id) if isinstance(self.profile_id, str) else None

    def _profile(self) -> MachineProfile:
        """The profile the bytes are pinned for; the CAMEO 4 (Plan's defaults) without one."""
        pinned = self._pinned()
        return pinned.profile if pinned is not None else CAMEO4

    @property
    def validator(self) -> Callable[[bytes], None]:
        """Byte-level validator for this program's writes: only its profile's pinned writes."""
        pinned = self._pinned()
        return pinned.validator if pinned is not None else _refuse_all

    @property
    def units_expected(self) -> tuple[int, int] | None:
        """The U reply expected right after the header: the pinned mat's (a, b); None for a
        profile without pinned programs."""
        pinned = self._pinned()
        return pinned.units_mat.expected_u if pinned is not None else None

    @property
    def units_enforced(self) -> bool:
        """True: a pinned program's mat always has an enforced U reply (the self-check requires
        it). A profile without pinned programs has no pair either, so no reply can pass."""
        pinned = self._pinned()
        return pinned.units_mat.enforce_u if pinned is not None else True

    @property
    def batch_bytes(self) -> int:
        """Most bytes queued before the next ready wait (the profile's; Plan's default else)."""
        return self._profile().batch_bytes

    @property
    def slowest_su_per_s(self) -> float:
        """The profile's lowest speed in plotter units per second."""
        return self._profile().slowest_su_per_s

    @property
    def max_coordinate_su(self) -> int:
        """The profile's largest move coordinate."""
        return self._profile().max_coordinate_su

    @property
    def summary(self) -> dict:
        """What the open reply reports about this program."""
        return {"program": self.name, "options": dict(self.options)}


def _refuse_all(data: bytes) -> None:
    raise UnsafeCommand("no pinned writes for this machine")


def _gpgl(*cmds: str) -> bytes:
    return b"".join(c.encode("ascii") + bytes([ETX]) for c in cmds)


def _check_grammar(data: bytes, grammar: re.Pattern[bytes] = _PINNED_CMD,
                   test_area: tuple[int, int] = TEST_AREA,
                   max_write: int = CAMEO4.max_write) -> None:
    """Raise UnsafeCommand unless `data` uses only the pinned command grammar and test area (the
    CAMEO 4's by default) and fits one write of the profile."""
    if type(data) is not bytes or not 0 < len(data) <= max_write or bytes([ESC]) in data \
            or data[-1] != ETX:
        raise UnsafeCommand(f"bad pinned write {data!r}")
    for cmd in data[:-1].split(bytes([ETX])):
        if not grammar.fullmatch(cmd):
            raise UnsafeCommand(f"command {cmd!r} not allowed in a pinned write")
        if cmd[:1] in (b"M", b"D"):
            move = _PINNED_MOVE.fullmatch(cmd)
            if move is None or not (int(move[1]) <= test_area[0] and int(move[2]) <= test_area[1]):
                raise UnsafeCommand(f"{cmd!r} outside the test area")


def _write(data: bytes, title: str, expect: str, confirm: bool = False) -> Step:
    return Step("write", (data,), title, expect, confirm)


_HEADER = _write(
    _gpgl("TG1", "FN0", "TB50,0"),
    "Header: 12x12 Cutting Mat (TG1), normal orientation (FN0 TB50,0)",
    "The mat may feed in about 16.5 mm and the carriage moves a little to the right (about "
    "5 mm), tool up. Nothing touches the mat. TG1 only works right after the mat was loaded "
    "on the machine; otherwise step 2 stops the program.")
_UNITS = Step(
    "units", (b"U\x03",),
    "Unit check (U query, no motion)",
    "Reply 6100,6100 means 0.05 mm per unit, as the job format assumes.")
_TRAILER = _write(
    _gpgl("\\0,0", "M0,0", "FN0", "TB50,0"),
    "Return to Origin (\\0,0 M0,0 FN0 TB50,0)",
    "Tool-up move back to (0,0), near the top-left corner of the grid (the exact offset is "
    "still to be measured). The mat stays loaded; unload it on the machine afterwards.")

DRY_RUN = (
    _HEADER,
    _UNITS,
    _write(_gpgl("\\30,30", "Z6066,6066", "J1", "!1,1"),
           "Cutting area, Tool 1, lowest Speed (\\30,30 Z6066,6066 J1 !1,1)",
           "The carriage may move a little to select Tool 1. No tool goes down."),
    _write(_gpgl("M200,200"),
           "Tool-up move to a=10 mm, b=10 mm (M200,200)",
           "Rollers and carriage move a little. Tool 1 ends up above a point about 10 mm below "
           "and 10 mm right of the top-left corner of the grid (the exact offset is still to be "
           "measured; if it is elsewhere, just note it)."),
    _write(_gpgl("M1200,200"),
           "+50 mm on the FIRST axis, a (M1200,200)",
           "ONLY the rollers turn: the mat goes 50 mm further in, away from you. The carriage "
           "stays put. To measure, first mark the mat edge with a pencil next to the front plate."),
    _write(_gpgl("M1200,1200"),
           "+50 mm on the SECOND axis, b (M1200,1200)",
           "ONLY the carriage moves 50 mm, expected to your RIGHT. The rollers stay put. If it "
           "goes left, that is not a fault: just note it."),
    _write(_gpgl("M200,1200"),
           "-50 mm on a (M200,1200)",
           "ONLY the rollers: the mat comes 50 mm back toward you."),
    _write(_gpgl("M200,200"),
           "-50 mm on b (M200,200)",
           "ONLY the carriage: 50 mm back, opposite to step 6."),
    _TRAILER,
)


def _pen_test(force: int) -> tuple[Step, ...]:
    return (
        _HEADER,
        _UNITS,
        _write(_gpgl("\\30,30", "Z6066,6066", "J1", "!1,1", f"FX{force},1", "TJ1",
                     "FC0,1,1", "FE0,1", "FF0,0,1"),
               f"Cutting area, Tool 1, Sketch Pen: Speed 1, Force {force}, no Offset or Overcut",
               "Tool 1 is selected (the carriage may move). The pen does NOT go down yet."),
        _write(_gpgl("M200,200", "D600,200", "D600,600", "D200,600", "D200,200",
                     "M200,100", "D300,100"),
               "Draw a 20 mm square and a 5 mm tick at 1 cm/s (about 15 s)",
               "Side 1: ONLY the rollers turn (the paper goes 20 mm in). Side 2: ONLY the "
               "carriage, 20 mm to the right. Sides 3 and 4 go back. Last, a 5 mm tick 5 mm left "
               "of the square, level with the first 5 mm of its left side."),
        _TRAILER,
    )


# Settings that cut adhesive vinyl on its backing cleanly on our machine: AutoBlade in Tool 1,
# Blade Depth 1 (0.1 mm: cuts the vinyl, not the backing), Speed 5, Force 10.
VINYL_CUT = ("J1", "!5,1", "FX10,1", "TJ3", "FC18,1,1", "FE0,1", "FF1,1,1", "TF1,1")
# A 1-inch square (508 SU = one mat grid square) centred on a sheet that covers the full width
# and 5 grid squares down from the top of the 12x12 grid: centre a = 2.5 in, b = 6 in.
SQ_A0, SQ_A1, SQ_B0, SQ_B1 = 1016, 1524, 2794, 3302
CUT_TEST = (
    _HEADER,
    _UNITS,
    _write(_gpgl("\\30,30", "Z6066,6066", *VINYL_CUT),
           "Vinyl settings: Tool 1: AutoBlade, Blade Depth 1 (0.1 mm), Force 10, Speed 5",
           "The carriage goes LEFT to the AutoBlade station (outside the sheet) and sets the "
           "blade: a few clicks, up to about 10 s. The blade does not touch the sheet."),
    _write(_gpgl(f"M{SQ_A0},{SQ_B0}"),
           "Preview, blade UP: top-left corner of the square",
           "The blade stays UP above a point about 2 squares (5 cm) below the top edge of the "
           "sheet and about 5.5 squares (14 cm) from the left edge of the grid. Is it above the "
           "sheet? If not, stop here."),
    _write(_gpgl(f"M{SQ_A1},{SQ_B1}"),
           "Preview, blade UP: bottom-right corner of the square",
           "The blade, still UP, moves diagonally one square down and one square right (about "
           "3 squares below the top edge of the sheet, 6.5 from the left). Still above the "
           "sheet? If not, stop here."),
    _write(_gpgl(f"M{SQ_A0},{SQ_B0 + 20}", f"D{SQ_A0},{SQ_B0}", f"D{SQ_A1},{SQ_B0}",
                 f"D{SQ_A1},{SQ_B1}", f"D{SQ_A0},{SQ_B1}", f"D{SQ_A0},{SQ_B0}",
                 f"D{SQ_A0 + 20},{SQ_B0}"),
           "CUT: one grid square (25.4 mm) in the middle of the sheet, 1 mm overlap at the start",
           "The blade goes DOWN and cuts: rollers only (the sheet goes 25 mm in), carriage only "
           "to the right, rollers back, carriage back to the left. A few seconds. Stop at once "
           "if it cuts outside the sheet.",
           confirm=True),
    _TRAILER,
)

PROGRAMS: dict[tuple[str, int], tuple[Step, ...]] = {
    ("dry-run", 0): DRY_RUN,
    ("pen-test", 1): _pen_test(1),
    ("pen-test", 7): _pen_test(7),
    ("cut-test", 0): CUT_TEST,
}
# ESC 15 code each holder must report before any write that can lower a tool (lowers_tool).
REQUIRED_TOOLS: dict[str, dict[int, int]] = {"dry-run": {}, "pen-test": {1: 7}, "cut-test": {1: 2}}
PINNED_WRITES = frozenset(w for p in PROGRAMS.values() for s in p if s.kind == "write"
                          for w in s.writes)


def validate_pinned(data: bytes) -> None:
    """The CAMEO 4 programs' validator: raise UnsafeCommand unless `data` is one of its pinned
    writes."""
    if data not in PINNED_WRITES:
        raise UnsafeCommand("not a pinned motion write")


@dataclass(frozen=True)
class PinnedPrograms:
    """The pinned programs of one profile, with the grammar and test area their writes keep."""
    profile: MachineProfile
    mat: str                                         # the mat key the programs' header selects
    grammar: re.Pattern[bytes]                       # every command of every pinned write
    test_area: tuple[int, int]                       # max a, b (SU) of any pinned M/D
    programs: Mapping[tuple[str, int], tuple[Step, ...]]   # (name, variant) -> steps
    required_tools: Mapping[str, Mapping[int, int]]  # name -> holder -> ESC 15 code
    validator: Callable[[bytes], None]               # refuses every write that is not pinned here

    @property
    def writes(self) -> frozenset[bytes]:
        """Every pinned motion write of these programs."""
        return frozenset(w for p in self.programs.values() for s in p if s.kind == "write"
                         for w in s.writes)

    @property
    def names(self) -> frozenset[str]:
        """The program names."""
        return frozenset(name for name, _ in self.programs)

    @property
    def units_mat(self) -> Mat | None:
        """The profile's Mat the header selects (its expected U is checked after it)."""
        return self.profile.mat(self.mat)


# Every profile's pinned programs, by profile id. Only the CAMEO 4 has any.
PINNED_PROGRAMS: dict[str, PinnedPrograms] = {
    CAMEO4.id: PinnedPrograms(CAMEO4, "12x12", _PINNED_CMD, TEST_AREA, PROGRAMS, REQUIRED_TOOLS,
                              validate_pinned),
}
# The programs keyed by profile id: PROGRAMS_BY_PROFILE["cameo4"] is PROGRAMS.
PROGRAMS_BY_PROFILE: dict[str, Mapping[tuple[str, int], tuple[Step, ...]]] = {
    pid: pinned.programs for pid, pinned in PINNED_PROGRAMS.items()}


def _self_check(pinned: Mapping[str, PinnedPrograms],
                registered: Iterable[MachineProfile]) -> None:
    """Import-time checks; raise (not assert) so they hold under python -O too.

    For each profile with pinned programs: it is the registry's own profile, its `programs`
    names are exactly the pinned ones, every write keeps the pinned grammar and a test area
    inside its mat's cut area and passes machines.safety.check_profile(profile, pinned=...), the
    validator accepts every pinned write and refuses writes that are not pinned, every program
    starts with the header of its mat and the U check, every step is one write or the U check,
    the variants are the ones build_program() reads (0, or the pen forces of the pen test), a
    program that can lower a tool declares a usable tool for it, and the dry run never lowers
    one. A registered profile that lists programs must have them pinned here."""
    by_id = {p.id: p for p in registered}
    for pid, s in pinned.items():
        p = s.profile
        if not isinstance(p, MachineProfile) or p.id != pid or by_id.get(pid) != p:
            raise RuntimeError(f"pinned programs for {pid!r} are not for the registry's profile")
        if s.names != p.programs or set(s.required_tools) != s.names:
            raise RuntimeError(f"{pid}: pinned programs {sorted(s.names)} and required tools "
                               f"{sorted(s.required_tools)} differ from the profile's "
                               f"{sorted(p.programs)}")
        mat = s.units_mat
        if mat is None or not mat.enforce_u or not mat.offered(p.support):
            raise RuntimeError(f"{pid}: the programs need a mat whose U reply is checked")
        _a0, _b0, a1, b1 = mat.area_su
        if not (type(s.test_area) is tuple and len(s.test_area) == 2
                and all(type(v) is int for v in s.test_area)
                and 0 < s.test_area[0] <= a1 and 0 < s.test_area[1] <= b1):
            raise RuntimeError(f"{pid}: the test area {s.test_area!r} must lie inside the cut "
                               f"area of the {mat.label} mat")
        header = _gpgl(*p.mat_texts(mat.key))
        writes = s.writes
        for data in writes:
            _check_grammar(data, s.grammar, s.test_area, p.max_write)
        safety.check_profile(p, pinned=sorted(writes))
        _check_validator(pid, s.validator, writes)
        for (name, variant), steps in s.programs.items():
            if type(variant) is not int or (variant < 1 if name == "pen-test" else variant != 0):
                raise RuntimeError(f"{pid} {name}: variant {variant!r} is not one build_program "
                                   "reads (0, or a pen force of the pen test)")
            if (len(steps) < 2 or steps[0].kind != "write" or steps[0].writes != (header,)
                    or steps[1].kind != "units"):
                raise RuntimeError(f"{pid} {name}: a program starts with the header of its mat "
                                   "and the U check")
            if any(st.kind not in ("write", "units") or len(st.writes) != 1
                   or (st.kind == "units" and st.writes != (b"U\x03",)) for st in steps):
                raise RuntimeError(f"{pid} {name}: every step is one write or the U check")
            lowering = any(lowers_tool(w, p) for st in steps if st.kind == "write"
                           for w in st.writes)
            if lowering and name == "dry-run":
                raise RuntimeError("dry-run must never lower the tool")
            if lowering and not s.required_tools[name]:
                raise RuntimeError(f"{name} lowers a tool but declares no required tool")
            for holder, code in s.required_tools[name].items():
                tool = p.tool(holder, code)
                if tool is None or not tool.usable:
                    raise RuntimeError(f"{pid} {name}: code {code} in holder {holder} is not a "
                                       "usable tool")
    for pid, p in by_id.items():
        if p.programs and pid not in pinned:
            raise RuntimeError(f"{pid} lists the programs {sorted(p.programs)}, but none are "
                               "pinned")


def _check_validator(pid: str, validator: Callable[[bytes], None],
                     writes: frozenset[bytes]) -> None:
    """The validator accepts each pinned write and refuses (ValueError, as UnsafeCommand) writes
    that are not pinned: each pinned write twice over or without its ETX, and a few others."""
    for data in sorted(writes):
        try:
            validator(data)
        except ValueError as e:
            raise RuntimeError(f"{pid}: the validator refuses the pinned write {data!r}") from e
    others ={b"", b"\x03", b"D0,0\x03", b"M0,0\x03", b"FG\x03", b"\x1b\x09", b"FP\x03",
              b"TB72\x03", *(w + w for w in writes), *(w[:-1] for w in writes)}
    for data in sorted(others - writes):
        try:
            validator(data)
        except ValueError:
            continue
        raise RuntimeError(f"{pid}: the validator accepts {data!r}, which is not pinned")


_self_check(PINNED_PROGRAMS, profiles())


def build_program(name, options, profile: MachineProfile = CAMEO4) -> Program:
    """Resolve a client request to a pinned program of `profile` (the detected machine). Clients
    never send bytes, only names.

    Raises ValueError for a profile without pinned programs (every model but the CAMEO 4), an
    unknown program or unknown options."""
    pinned = PINNED_PROGRAMS.get(profile.id) if isinstance(profile, MachineProfile) else None
    if pinned is None or pinned.profile != profile:
        what = f"the {profile.name}" if isinstance(profile, MachineProfile) else "this machine"
        raise ValueError(f"there are no test programs for {what}")
    if not isinstance(options, dict) or set(options) - {"pen_force", "allow_unrecognized_pen"}:
        raise ValueError("unknown options")
    if not isinstance(name, str) or name not in pinned.names:
        raise ValueError(f"unknown program {name!r}")
    required = dict(pinned.required_tools[name])
    variants = sorted(v for n, v in pinned.programs if n == name)
    if name != "pen-test":
        if options:
            raise ValueError(f"{name} takes no options")
        return Program(name, {}, pinned.programs[(name, 0)], required, profile_id=profile.id)
    force = options.get("pen_force", variants[0])
    allow = options.get("allow_unrecognized_pen", False)
    if type(force) is not int or force not in variants or not isinstance(allow, bool):
        choices = " or ".join(str(v) for v in variants)
        raise ValueError(f"pen_force must be {choices}, allow_unrecognized_pen a bool")
    return Program(name, {"pen_force": force, "allow_unrecognized_pen": allow},
                   pinned.programs[(name, force)], required,
                   {holder: allow for holder in required}, profile_id=profile.id)
