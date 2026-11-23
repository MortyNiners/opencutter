# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Machine profiles: what OpenCutter knows about one cutter model, as frozen typed values.

Pure data and pure helpers: no I/O, and nothing else from opencutter is imported. Every value is
written by hand, in our own words, with a short comment that gives its basis: "measured on
V5.05", "firmware analysis", "Studio 5.0.556 machine data" or "public captures and reports"
(what others have published, described in our own words). No Studio rows, column names, syntax
strings or preset tables belong here (CONTRIBUTING.md).

Support levels (Support)
  TESTED      the profile has cut on real hardware (today only the CAMEO 4, firmware line V5).
  UNTESTED    ported from Studio's machine data and the firmware analysis, with a few values
              from public captures and reports. Jobs are allowed; the README lists the model as
              not yet tested, and the app shows no note about it.
  RECOGNIZED  the model is named (FG, USB id or BLE name), but a fact needed to move it safely
              is missing. It gets ESC 05 and FG only, its link is released and its address
              skipped (reason "wrong-model"), and the app says why in plain words.
A MachineProfile is TESTED or UNTESTED; a RECOGNIZED model is a RecognizedModel (below), a
recognition-only shape with no ranges, mats or commands. A firmware line (FirmwareLine) can only
narrow its profile's level: the level that counts is the narrower of the two (Support.narrower).

MachineProfile: one model OpenCutter may drive (TESTED or UNTESTED)
  id                   stable id: JobSpec.machine, Program.profile_id, presets, settings
                       ("cameo4"); lower case letters, digits and "_".
  name                 the model's name in every user-visible text ("CAMEO 4").
  support              TESTED or UNTESTED; the firmware lines may narrow it.
  identity             Identity: how FG, USB and BLE name the model.
  holders              Holder per tool position, ascending numbers (1 = left).
  ranges               Ranges: firmware ranges in firmware units (Studio's range intersected
                       with the range the firmware accepts).
  dialect              Dialect: how condition blocks and the ESC 15 reply are written.
  mats                 Mat per cutting mat the model can use, each with its TG value, cut area
                       and expected U reply.
  default_mat          key of the mat offered first; it must be offered at `support`.
  status               StatusModel: the ESC 05 digits; any other digit aborts the step.
  queries              Queries: the read-only queries allowed after the model gate.
  stop                 StopSequence: always ESC 09 twice, each in its own write.
  basis                own words: where the profile comes from.
  unit_mm              plotter unit, 0.05 mm on every known model (the self-check requires it).
  max_write            longest motion write in bytes (182 on the CAMEO 4; never above 480).
  batch_bytes          most bytes queued before the next ready wait.
  max_coordinate_su    largest M/D/area coordinate in plotter units.
  lowering_prefixes    commands that can bring a tool down (D, and TF where the AutoBlade
                       cycles); writes holding one need the tool check first.
  fg_claims_session    FG claims the machine's session, so the status is read again after it.
  power_cycle_to_connect  the texts tell to switch the machine off and on when the app cannot
                       connect (texts only). Observed on the CAMEO 4, which accepts a new link
                       only right after power-on; the safe advice for every model where that
                       was not observed. False only for a model seen to take a link at any time.
  programs             names of the pinned test programs this profile has.
  pause, resume        the single write an explicit Pause (ESC 07) or Resume (ESC 08) asked for
                       by the user sends, or None (the default: never sent). Only the exact
                       write is allowed (machines.safety), and only a TESTED profile on a TESTED
                       firmware line ever sends it (device.link); today the CAMEO 4 only.
Derived helpers (pure): holder(), tool(), tool_name(), tool_names(), blade_codes(),
unsupported_codes(), mat(), offered_mats(), speed_text(), speed_mm_s(), su_per_mm,
slowest_su_per_s, mat_texts(), area_texts(), condition_texts(), trailer_texts(),
setting_grammar(), tools_pattern(), parse_tools(), holder_codes().

Identity: how a model names itself
  fg_models            exact FG model texts, the part before " V<major>.<minor>" ("CAMEO 4").
                       Matching is exact and case-sensitive, never by prefix: "CAMEO 4" is a
                       prefix of "CAMEO 4 PLUS".
  lines                FirmwareLine per known firmware line (the <major> of "V5.05").
  usb_ids              (vendor id, product id) pairs: a discovery hint and a cross-check.
  ble_names            prefixes of the BlueZ device name, compared without regard to case
                       ("CAMEO", "Portrait", "CURIO"): a discovery hint only; FG decides.
  shared_fg_models     FG texts this model may report that another model reports as well
                       ("CAMEO" for a CAMEO 1 or a Pro MK-II). They name the model only when a
                       USB id settles it; otherwise the machine is UNKNOWN.

FirmwareLine: major (N of "VN.dd"), support, basis (own words), reason (RECOGNIZED lines only:
plain words, the part after "cannot drive it yet:", without a final period).

Holder: number (the J argument and the ",n" suffix), label ("Tool 1", or "Tool" on a
one-holder model), tools (Tool per ESC 15 code; code 0 "No Tool Detected" is always there and
never usable).
Tool: code (ESC 15 code), name (Studio's term shown to people), blade (cuts with a blade:
closed contours get the overlap; Offset and Overcut apply), depth (takes Blade Depth, TF; only
in holder 1, the AutoBlade), usable (False: refused for jobs), action (Studio's action word:
"Cut", "Sketch", "Kraft Cut", ...).

Ranges: speed (whole cm/s), force, acceleration (None: the model has no TJ), offset,
blade_depth, overcut (each of the two FF values), passes (a UI rule, not firmware).

Dialect: typed choices, never syntax strings
  holders      DUAL: J<n> first, then the per-holder forms !s,n FXf,n FCo,1,n FEx,n FFs,e,n.
               SINGLE: no J; the plain forms !s FXf FCo FEx,0 FFs,e,0, which set every
               condition of the firmware, so the active one does not matter.
  accel        SHARED: TJa once, with no holder argument (it applies to every condition).
               NONE: the model has no TJ.
  tool_reply   the ESC 15 reply ' t1, t2' with ONE_DIGIT or TWO_DIGITS codes; anything else
               fails closed.
  tool_slots   for each holder, in order, which of the two reply codes is its own. A
               one-holder model whose slot is not established uses the first one, (0,). A
               reply slot no holder owns must read 0 (no tool); any other code there fails
               the tool check closed (holder_codes gives None, and the raw reply is shown).
  speed        WHOLE: whole numbers only ("!5"). Decimal speeds and the tool-up speed are
               never sent.
  expand_mode  the model's machine data defines expand mode (FM). Even then FM is sent only as
               FM1 right after the TB50,0 that follows TG9 (TG9 FN0 TB50,0 FM1, the header
               Studio sends), on a 24-inch mat whose header_extra is EXPAND, only for the models
               of safety.EXPAND_MODELS (the CAMEO 4 Pro and the CAMEO Pro MK-II), and never to a
               CAMEO 4, 4 Plus, 5 or 5 Plus (safety.NO_EXPAND_MODELS).
The job header is TG<n> FN0 TB50,0 [FM1], then \\a0,b0 Za1,b1; a condition block is as above,
then TF<d>,1 for a depth tool; the trailer is \\0,0 M0,0 FN0 TB50,0 (mat_texts, area_texts,
condition_texts, trailer_texts).

Mat: key ("12x12": JobSpec.mat, settings), label, tg (1..13; TG0, no mat, is not a mat),
size_mm (width x = carriage, length y = feed), area_su (\\a0,b0 Za1,b1 with a = feed),
expected_u (the U reply (a, b) right after TG on a fresh load; None when unknown), u_basis
(KNOWN: measured; INFERRED: from the firmware's window tables or a stated analogy; UNKNOWN),
basis (own words), header_extra (EXPAND: FM1 right after the TG9 FN0 TB50,0 of the header),
grid_in (printed inch grid, drawing only), feed_mm (how far TG feeds the mat after a panel load,
texts only).
KNOWN and INFERRED U replies are enforced: a mismatch refuses the job before the cut. With an
UNKNOWN U the mat is offered only where the support level that counts is UNTESTED, and the U
reply is logged and shown but not enforced (Mat.offered).

StatusModel: texts (ESC 05 reply -> Studio's term; an exact, unpadded reply; any other reply
aborts the step), ready, busy, load (the digits the session machinery waits on),
unloaded_after_header (a load digit after the header means the mat was unloaded: the session
stops).
Queries: esc (ESC codes allowed as queries after the gate, a subset of 05, 10, 15, 16, with 05
and 15 always), text (read-only getters, each written alone with ETX; U always; always within
safety.READ_ONLY_QUERIES), max_write.
StopSequence: writes (each exactly ESC 09; two of them), gap_s (seconds between them).

RecognizedModel: id, name, identity (no firmware lines: every firmware is refused alike),
reason (plain words, the part after "cannot drive it yet:", without a final period), basis.
Its message reads "OpenCutter recognizes this CAMEO 3 but cannot drive it yet: there is no
known way to stop it from the computer."
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


def N_(message: str) -> str:
    """Marks a text for the translations of the window (the same marker as opencutter.i18n.N_,
    which this package may not import): the window translates it where it shows it."""
    return message


class Support(Enum):
    """How far OpenCutter may go with a model or a firmware line."""
    TESTED = "tested"            # cut on real hardware
    UNTESTED = "untested"        # ported from the machine data and the firmware; jobs allowed
    RECOGNIZED = "recognized"    # named, but refused: ESC 05 and FG only

    @property
    def jobs_allowed(self) -> bool:
        """True for TESTED and UNTESTED."""
        return self is not Support.RECOGNIZED

    @property
    def rank(self) -> int:
        """0 RECOGNIZED, 1 UNTESTED, 2 TESTED."""
        return _SUPPORT_ORDER.index(self)

    def narrower(self, other: Support) -> Support:
        """The narrower of two levels: a firmware line can only narrow its profile's level."""
        return self if self.rank <= other.rank else other


_SUPPORT_ORDER = (Support.RECOGNIZED, Support.UNTESTED, Support.TESTED)


@dataclass(frozen=True)
class IntRange:
    """An inclusive range of whole numbers. `v in r` is True only for an int (never a bool)."""
    lo: int
    hi: int

    def __contains__(self, v: object) -> bool:
        return type(v) is int and self.lo <= v <= self.hi

    def as_tuple(self) -> tuple[int, int]:
        """(lo, hi)."""
        return (self.lo, self.hi)

    def values(self) -> range:
        """Every value, lo..hi."""
        return range(self.lo, self.hi + 1)


@dataclass(frozen=True)
class FirmwareLine:
    """One firmware line of a model: the N of 'VN.dd' in its FG reply."""
    major: int
    support: Support
    basis: str                   # own words: why this level
    reason: str = ""             # RECOGNIZED only: why it cannot be driven, in plain words


@dataclass(frozen=True)
class Identity:
    """How a model names itself: FG model texts, firmware lines, USB ids and BLE names."""
    fg_models: frozenset[str]
    lines: tuple[FirmwareLine, ...] = ()
    usb_ids: frozenset[tuple[int, int]] = frozenset()
    ble_names: tuple[str, ...] = ()
    shared_fg_models: frozenset[str] = frozenset()

    def line(self, major: int) -> FirmwareLine | None:
        """The firmware line with this major number, or None."""
        return next((ln for ln in self.lines if ln.major == major), None)


@dataclass(frozen=True)
class Tool:
    """One tool an ESC 15 code stands for in one holder."""
    code: int
    name: str
    blade: bool = False          # cuts with a blade: overlap on closed contours, FC/FE/FF apply
    depth: bool = False          # takes Blade Depth (TF): the AutoBlade in holder 1
    usable: bool = True          # False: refused for jobs
    action: str = "Cut"          # Studio's action word


@dataclass(frozen=True)
class Holder:
    """One tool position."""
    number: int
    label: str
    tools: tuple[Tool, ...]

    def tool(self, code: object) -> Tool | None:
        """The tool for ESC 15 `code`, or None (also for a bool or a non-int)."""
        if type(code) is not int:
            return None
        return next((t for t in self.tools if t.code == code), None)

    def names(self) -> dict[int, str]:
        """ESC 15 code -> tool name."""
        return {t.code: t.name for t in self.tools}


@dataclass(frozen=True)
class Ranges:
    """Firmware ranges in firmware units: Studio's range intersected with the firmware's."""
    speed: IntRange                  # whole cm/s
    force: IntRange
    acceleration: IntRange | None    # None: the model has no TJ
    offset: IntRange
    blade_depth: IntRange            # 0.1 mm per step
    overcut: IntRange                # each of the two FF values
    passes: IntRange = IntRange(1, 10)   # a UI rule, not firmware

    def table(self) -> dict[str, tuple[int, int]]:
        """name -> (lo, hi) for the ToolSettings fields checked by range, in the shape of
        job.validate.RANGES (no acceleration entry without TJ; overcut is checked per value)."""
        out = {"speed": self.speed.as_tuple(), "force": self.force.as_tuple()}
        if self.acceleration is not None:
            out["acceleration"] = self.acceleration.as_tuple()
        out.update(offset=self.offset.as_tuple(), blade_depth=self.blade_depth.as_tuple(),
                   passes=self.passes.as_tuple())
        return out


class HolderForm(Enum):
    """How a condition block names its holder."""
    DUAL = "dual"        # J<n> first; per-holder forms !s,n FXf,n FCo,1,n FEx,n FFs,e,n
    SINGLE = "single"    # no J; plain forms !s FXf FCo FEx,0 FFs,e,0 (every condition)


class AccelForm(Enum):
    """How acceleration is written."""
    SHARED = "shared"    # TJa once, no holder argument
    NONE = "none"        # no TJ on this model


class SpeedForm(Enum):
    """How speed is written."""
    WHOLE = "whole"      # whole cm/s: "!5"


class ToolReply(Enum):
    """The ESC 15 reply grammar: ' t1, t2' with codes of one or of up to two digits."""
    ONE_DIGIT = "one-digit"
    TWO_DIGITS = "two-digits"


@dataclass(frozen=True)
class Dialect:
    """How the profile's commands are written (typed choices, never syntax strings)."""
    holders: HolderForm
    accel: AccelForm
    tool_reply: ToolReply
    tool_slots: tuple[int, ...]      # per holder, in order: index of its code in the reply
    speed: SpeedForm = SpeedForm.WHOLE
    expand_mode: bool = False        # the machine data defines expand mode (FM)


class UBasis(Enum):
    """Where a mat's expected U reply comes from."""
    KNOWN = "known"          # measured on a real machine
    INFERRED = "inferred"    # from the firmware's window tables or a stated analogy
    UNKNOWN = "unknown"      # not known: logged and shown, never enforced


class HeaderExtra(Enum):
    """A command the job header adds for one mat."""
    EXPAND = "expand"        # FM1 after TG9 FN0 TB50,0: opens the 610 mm window


@dataclass(frozen=True)
class Mat:
    """One cutting mat: its TG value, size, cut area and expected U reply."""
    key: str
    label: str
    tg: int
    size_mm: tuple[float, float]             # (width x = carriage, length y = feed)
    area_su: tuple[int, int, int, int]       # \a0,b0 Za1,b1 with a = feed
    expected_u: tuple[int, int] | None       # (a, b) right after TG on a fresh load
    u_basis: UBasis
    basis: str
    header_extra: HeaderExtra | None = None
    grid_in: tuple[int, int] | None = None   # printed inch grid (columns, rows): drawing only
    feed_mm: float | None = None             # TG feed after a panel load: texts only

    @property
    def enforce_u(self) -> bool:
        """True when a U mismatch refuses the job (KNOWN and INFERRED)."""
        return self.u_basis is not UBasis.UNKNOWN

    def offered(self, support: Support) -> bool:
        """True when the mat may be used at this support level: never when RECOGNIZED; with an
        UNKNOWN U only when UNTESTED."""
        if not support.jobs_allowed:
            return False
        return self.u_basis is not UBasis.UNKNOWN or support is Support.UNTESTED


@dataclass(frozen=True)
class StatusModel:
    """The ESC 05 replies of one model."""
    texts: tuple[tuple[str, str], ...]   # (reply, Studio's term) for every reply it may send
    ready: str = "0"
    busy: str = "1"
    load: str = "2"
    unloaded_after_header: bool = True   # load after the header: the mat was unloaded

    def as_dict(self) -> dict[str, str]:
        """reply -> term."""
        return dict(self.texts)

    def text(self, reply: object) -> str | None:
        """Studio's term for an exact reply, or None."""
        return dict(self.texts).get(reply) if isinstance(reply, str) else None

    def is_status(self, reply: object) -> bool:
        """True only for an exact, unpadded ESC 05 reply of this model."""
        return self.text(reply) is not None


@dataclass(frozen=True)
class Queries:
    """Read-only queries allowed after the model gate."""
    esc: frozenset[int]          # ESC codes, each alone in a 2-byte write
    text: frozenset[bytes]       # getters without ETX, each alone with ETX
    max_write: int = 20


@dataclass(frozen=True)
class StopSequence:
    """The stop: each write alone, `gap_s` seconds apart."""
    writes: tuple[bytes, ...]
    gap_s: float


def _int(v: object, what: str) -> int:
    if type(v) is not int:
        raise ValueError(f"{what} must be an int, not {v!r}")
    return v


def _alt(values: range | list[int]) -> bytes:
    """A regex alternation of exact whole numbers."""
    return b"(?:" + b"|".join(str(v).encode("ascii") for v in sorted(values, reverse=True)) + b")"


@dataclass(frozen=True)
class MachineProfile:
    """One model OpenCutter may drive (TESTED or UNTESTED). See the module docstring."""
    id: str
    name: str
    support: Support
    identity: Identity
    holders: tuple[Holder, ...]
    ranges: Ranges
    dialect: Dialect
    mats: tuple[Mat, ...]
    default_mat: str
    status: StatusModel
    queries: Queries
    stop: StopSequence
    basis: str
    unit_mm: float = 0.05
    max_write: int = 182
    batch_bytes: int = 1024
    max_coordinate_su: int = 99_999
    lowering_prefixes: tuple[bytes, ...] = (b"D", b"TF")
    fg_claims_session: bool = False
    power_cycle_to_connect: bool = False
    programs: frozenset[str] = frozenset()
    # The user's explicit Pause and Resume (one ESC 07, one ESC 08), or None: never sent.
    pause: bytes | None = None
    resume: bytes | None = None

    # --- holders and tools ------------------------------------------------------------------
    def holder(self, number: object) -> Holder | None:
        """The holder with this number, or None."""
        if type(number) is not int:
            return None
        return next((h for h in self.holders if h.number == number), None)

    def tool(self, holder: object, code: object) -> Tool | None:
        """The tool ESC 15 `code` stands for in `holder`, or None."""
        h = self.holder(holder)
        return h.tool(code) if h is not None else None

    def tool_name(self, holder: object, code: object) -> str:
        """Studio's name of the tool, or 'unknown tool <code>'."""
        t = self.tool(holder, code)
        return t.name if t is not None else f"unknown tool {code}"

    def tool_names(self) -> dict[int, dict[int, str]]:
        """holder -> code -> name, in the shape of device.protocol.TOOL_NAMES."""
        return {h.number: h.names() for h in self.holders}

    def blade_codes(self) -> frozenset[int]:
        """Codes of the blade tools in any holder."""
        return frozenset(t.code for h in self.holders for t in h.tools if t.blade)

    def unsupported_codes(self) -> frozenset[int]:
        """Codes of real tools that are refused for jobs (code 0, no tool, left out)."""
        return frozenset(t.code for h in self.holders for t in h.tools
                         if not t.usable and t.code != 0)

    # --- mats -------------------------------------------------------------------------------
    def mat(self, key: object) -> Mat | None:
        """The mat with this key, or None."""
        return next((m for m in self.mats if m.key == key), None)

    def offered_mats(self, support: Support | None = None) -> tuple[Mat, ...]:
        """The mats offered at `support` (default: the profile's own level; pass the narrower
        level of the profile and the detected firmware line)."""
        level = self.support if support is None else support
        return tuple(m for m in self.mats if m.offered(level))

    # --- speed ------------------------------------------------------------------------------
    def speed_text(self, speed: object) -> str:
        """The speed as written in '!': whole numbers only."""
        return str(_int(speed, "speed"))

    def speed_mm_s(self, speed: int) -> float:
        """Speed in mm/s: the firmware's speed is in cm/s."""
        return _int(speed, "speed") * 10.0

    @property
    def su_per_mm(self) -> int:
        """Plotter units per mm (20 at 0.05 mm)."""
        return round(1 / self.unit_mm)

    @property
    def slowest_su_per_s(self) -> float:
        """The lowest speed in plotter units per second: bounds how long a batch may run."""
        return self.speed_mm_s(self.ranges.speed.lo) * self.su_per_mm

    # --- command texts (without ETX) for job/builder.py -------------------------------------
    def mat_texts(self, key: str) -> tuple[str, ...]:
        """The first header write: TG<n>, FN0, TB50,0, then FM1 for an EXPAND mat (as Studio
        writes its 24 x 24 in header: TG9 FN0 TB50,0 FM1)."""
        m = self.mat(key)
        if m is None:
            raise ValueError(f"{self.name} has no mat {key!r}")
        out = (f"TG{m.tg}", "FN0", "TB50,0")
        return (*out, "FM1") if m.header_extra is HeaderExtra.EXPAND else out

    def area_texts(self, key: str) -> tuple[str, ...]:
        """The cut area: \\a0,b0 then Za1,b1."""
        m = self.mat(key)
        if m is None:
            raise ValueError(f"{self.name} has no mat {key!r}")
        a0, b0, a1, b1 = m.area_su
        return (f"\\{a0},{b0}", f"Z{a1},{b1}")

    def condition_texts(self, *, holder: int, speed: int, force: int,
                        acceleration: int | None, offset: int, line_segment_overcut: bool,
                        overcut: tuple[int, int], blade_depth: int | None) -> tuple[str, ...]:
        """One condition block in this dialect: [J] ! FX [TJ] FC FE FF [TF].

        Writes the values as given; job/validate.py checks the ranges. Raises ValueError for an
        unknown holder, a non-int value, an acceleration on a model without TJ (or none on one
        with it) and a Blade Depth for a holder whose tools take none."""
        h = self.holder(holder)
        if h is None:
            raise ValueError(f"{self.name} has no holder {holder!r}")
        if type(line_segment_overcut) is not bool:
            raise ValueError("line_segment_overcut must be a bool")
        n = h.number
        sp, f, o = self.speed_text(speed), _int(force, "force"), _int(offset, "offset")
        s, e = (_int(v, "overcut") for v in overcut)
        lso = int(line_segment_overcut)
        if self.dialect.holders is HolderForm.DUAL:
            out = [f"J{n}", f"!{sp},{n}", f"FX{f},{n}"]
            rest = [f"FC{o},1,{n}", f"FE{lso},{n}", f"FF{s},{e},{n}"]
        else:
            out = [f"!{sp}", f"FX{f}"]
            rest = [f"FC{o}", f"FE{lso},0", f"FF{s},{e},0"]
        if self.dialect.accel is AccelForm.SHARED:
            out.append(f"TJ{_int(acceleration, 'acceleration')}")
        elif acceleration is not None:
            raise ValueError(f"the {self.name} has no acceleration setting (TJ)")
        out += rest
        if blade_depth is not None:
            if not any(t.depth for t in h.tools):
                raise ValueError(f"no tool in {h.label} takes a Blade Depth")
            out.append(f"TF{_int(blade_depth, 'blade_depth')},1")
        return tuple(out)

    def trailer_texts(self) -> tuple[str, ...]:
        """Return to the origin: \\0,0 (so the area does not clip the move) M0,0 FN0 TB50,0."""
        return ("\\0,0", "M0,0", "FN0", "TB50,0")

    def setting_grammar(self) -> re.Pattern[bytes]:
        """Every non-move command the builder may emit for this profile (header, condition
        blocks, trailer apart from M0,0), with the profile's ranges and no leading zeros.

        TG takes only the profile's mats, and FM1 appears only when a mat has EXPAND (where it
        may stand, after TG9 FN0 TB50,0, is checked by safety.check_write). For the CAMEO 4
        this accepts exactly what job/validate.py's command grammar accepts, except
        TG2..TG7."""
        r, digits = self.ranges, len(str(self.max_coordinate_su)) - 1
        alts = [b"TG" + _alt(sorted({m.tg for m in self.mats}))]
        if any(m.header_extra is HeaderExtra.EXPAND for m in self.mats):
            alts.append(b"FM1")
        num = b"(?:0|[1-9][0-9]{0,%d})" % digits
        pos = b"[1-9][0-9]{0,%d}" % digits
        alts += [b"FN0", b"TB50,0", b"\\\\" + num + b"," + num, b"Z" + pos + b"," + pos]
        speed = _alt([v for v in r.speed.values()])
        oc = _alt(r.overcut.values())
        if self.dialect.holders is HolderForm.DUAL:
            n = _alt([h.number for h in self.holders])
            alts += [b"J" + n, b"!" + speed + b"," + n, b"FX" + _alt(r.force.values()) + b"," + n,
                     b"FC" + _alt(r.offset.values()) + b",1," + n, b"FE[01]," + n,
                     b"FF" + oc + b"," + oc + b"," + n]
        else:
            alts += [b"!" + speed, b"FX" + _alt(r.force.values()), b"FC" + _alt(r.offset.values()),
                     b"FE[01],0", b"FF" + oc + b"," + oc + b",0"]
        if self.dialect.accel is AccelForm.SHARED and r.acceleration is not None:
            alts.append(b"TJ" + _alt(r.acceleration.values()))
        if any(t.depth for h in self.holders for t in h.tools):
            alts.append(b"TF" + _alt(r.blade_depth.values()) + b",1")
        return re.compile(b"|".join(b"(?:" + a + b")" for a in alts))

    # --- ESC 15 -----------------------------------------------------------------------------
    def tools_pattern(self) -> re.Pattern[str]:
        """The exact ESC 15 reply ' t1, t2' (replies are never stripped)."""
        code = "([0-9])" if self.dialect.tool_reply is ToolReply.ONE_DIGIT else "([0-9]|[1-9][0-9])"
        return re.compile(f" {code}, {code}", re.ASCII)

    def parse_tools(self, reply: object) -> tuple[int, int] | None:
        """The two codes of an exact ESC 15 reply, or None (fail closed). Only the grammar: the
        tool check reads holder_codes(), which also applies the slot rule."""
        m = self.tools_pattern().fullmatch(reply) if isinstance(reply, str) else None
        return (int(m[1]), int(m[2])) if m else None

    def holder_codes(self, reply: object) -> dict[int, int] | None:
        """holder -> the code in its reply slot (Dialect.tool_slots), or None (fail closed; the
        caller shows the raw reply). A reply slot no holder owns, the second one on a one-holder
        model, must read 0: any other code there gives None."""
        codes = self.parse_tools(reply)
        if codes is None:
            return None
        slots = self.dialect.tool_slots
        if any(code != 0 for slot, code in enumerate(codes) if slot not in slots):
            return None
        return {h.number: codes[slot] for h, slot in zip(self.holders, slots)}


@dataclass(frozen=True)
class RecognizedModel:
    """A model OpenCutter names but refuses: ESC 05 and FG only. See the module docstring."""
    id: str
    name: str
    identity: Identity
    reason: str                  # plain words, the part after "cannot drive it yet:"
    basis: str

    @property
    def support(self) -> Support:
        """Always RECOGNIZED."""
        return Support.RECOGNIZED

    @property
    def message(self) -> str:
        """Why nothing is sent to it, for people."""
        return recognized_message(self.name, self.reason)


def recognized_message(name: str, reason: str) -> str:
    """'OpenCutter recognizes this <name> but cannot drive it yet: <reason>.'"""
    return N_("OpenCutter recognizes this {name} but cannot drive it yet: {reason}.").format(
        name=name, reason=reason)


# A registry entry: a model OpenCutter may drive, or one it only names.
Entry = MachineProfile | RecognizedModel
