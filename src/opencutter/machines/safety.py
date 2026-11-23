# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The never-send rules that hold for every machine, and the self-check every profile passes.

Pure Python, no I/O. One global list, the same for every profile; a profile can never remove an
entry (docs/PROTOCOL.md section 6):
  * the FP prefix, which also covers FPG (the diagnostic unlocks), and every command that
    starts with a lowercase letter (the gated diagnostic commands);
  * the firmware download path: PW and the S-record lines; ESC 0x11, 0x12 and 0x13;
  * the calibration and persistent writes FB, TB72, TB70, ZF, TA, TH, FY, FU, TD, TE, FD, TN
    and SC;
  * commands OpenCutter jobs never need, refused everywhere because stricter is allowed
    (STRICTER_PREFIXES): the tool-up speed APS, the cross cutter ACT, the door or actuator
    command AOC, the heated or powered tool TS, the electrostatic mat TR, the curve commands
    BZ, BZK and TZ, and the scans TB24, TB124, TB125 and TB1023.
The firmware skips leading filler bytes before a command, so the prefixes are checked after
those are stripped. Its parser also starts a new command at a command letter without an ETX
before it (firmware analysis: FX10,TJ3 runs FX10, then TJ3), so the rules are applied from
every ASCII letter of a command too (command_starts): FX10,TB72 or FN0FP is refused like TB72 or
FP alone. Every command OpenCutter writes has its letters only at its start.

FM (expand mode) is not on the global list: it is a per-profile rule. It may be sent only as
FM1, right after the TB50,0 that follows TG9 in the job header (TG9 FN0 TB50,0 FM1, all in one
write, the header Studio sends with its 24 x 24 in mat), for a 24-inch mat whose header_extra
is EXPAND, on a profile whose machine data defines expand mode and whose FG model texts are all
in EXPAND_MODELS (the CAMEO 4 Pro and the CAMEO Pro MK-II), and never to a CAMEO 4, 4 Plus, 5
or 5 Plus (NO_EXPAND_MODELS). Firmware analysis: after TG9, FM1 opens the 610 mm window; with a
12-inch mat it does nothing, and after TG0 the opposite command (FM0) narrows the carriage
window.

ESC: before the model gate only ESC 05 (and the text query FG) may be sent. After the gate the
profile's queries, always within ESC 05, 10, 15 and 16, each alone in a 2-byte write. The stop
is ESC 09, twice, each alone. ESC 07 (pause) and ESC 08 (resume) are never a query, never part
of a motion write and never the stop: each may go out only alone, once, as the Pause or Resume
the user asked for, on a profile that lists exactly that write (MachineProfile.pause, resume;
the device link sends it only on a TESTED profile and firmware line). Every other ESC code is
refused: 0x11, 0x12 and 0x13 (firmware download), 0x00 (nudge) and all the rest. ESC 0x11 is
never used to identify a machine: FG decides.

Text queries: a profile's read-only getters are always within READ_ONLY_QUERIES, the getters
that only report. A motion or setting command (M, D, TG, TF, H, ...) can never become a query.

check_profile() and check_recognized() refuse a profile that breaks any of this (a query that
is ESC 11, lowercase or not a known getter, a stop that is not ESC 09 twice, FP in a pinned
write, FM outside its place, a range wider than any firmware takes, a malformed shape, ...).
The registry runs them on every entry at import and raises (not assert, so the checks hold
under python -O too). Pinned program writes live in device/programs.py: that module passes
them to check_profile(profile, pinned=...) in its own import-time check.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator
from operator import attrgetter

from .profile import (AccelForm, Dialect, FirmwareLine, HeaderExtra, Holder, HolderForm, Identity,
                      IntRange, MachineProfile, Mat, Queries, Ranges, RecognizedModel, SpeedForm,
                      StatusModel, StopSequence, Support, Tool, ToolReply, UBasis)

ETX = 0x03
ESC = 0x1B
# Commands no OpenCutter job needs, on the global list as well (stricter is allowed): the
# tool-up speed (APS; '!' resets it, firmware analysis), the cross cutter (ACT), the door or
# actuator command (AOC), heated or powered tools (TS), the electrostatic mat (TR), the curve
# commands and their subdivision mode (BZ, BZK, TZ: they would bypass the bounds of M and D)
# and the registration mark and mode scans that move the machine (TB24, TB124, TB125, TB1023).
STRICTER_PREFIXES: tuple[bytes, ...] = (
    b"APS", b"ACT", b"AOC", b"TS", b"TR", b"BZ", b"BZK", b"TZ", b"TB24", b"TB124", b"TB125",
    b"TB1023")
# Never sent to any machine (docs/PROTOCOL.md section 6): the firmware download path, the
# diagnostic unlocks (FP, so FPG too), the calibration and persistent writes, and the
# commands above. FM is a per-profile rule (below).
NEVER_SEND_PREFIXES: tuple[bytes, ...] = (
    b"FB", b"TB72", b"TB70", b"TH", b"TD", b"TE", b"ZF", b"TA", b"SC", b"TN", b"FD", b"FP",
    b"FY", b"FU", b"PW", b"S0", b"S1", b"S2", b"S3", b"S7", b"S8", b"S9") + STRICTER_PREFIXES
FILLER = bytes(range(0x00, 0x21)) + b"+,-.0123456789\x7f"   # skipped by the firmware's parser

FM_PREFIX = b"FM"
EXPAND_COMMAND = b"FM1"
EXPAND_TG = 9                    # the 24 x 24 in mat: the only TG FM1 may follow
# What stands between TG9 and FM1 in the header, in the same write: the only 24 x 24 in job
# in public captures and reports (a CAMEO 4 Pro run by Studio) sends TG9 FN0 TB50,0 FM1.
EXPAND_AFTER: tuple[bytes, ...] = (b"FN0", b"TB50,0")
TWELVE_INCH_MM = 304.8           # FM1 never goes with a mat this wide or narrower
# Models that never get FM, whatever a profile says: their machine data defines no expand mode,
# and the firmware analysis shows FM can only shift their carriage frame.
NO_EXPAND_MODELS = frozenset({"CAMEO 4", "CAMEO 4 PLUS", "CAMEO 5", "CAMEO 5 PLUS"})
# The only models that may get FM1 (an allowlist, so a new profile cannot take expand mode by
# itself): the 24-inch models whose machine data defines expand mode and whose firmware opens
# the 610 mm window with TG9 then FM1. The CAMEO 5 Alpha models stay out: neither takes TG9.
EXPAND_MODELS = frozenset({"CAMEO 4 PRO", "CAMEO PRO MARK2"})

QUERY_ESC = frozenset({0x05, 0x10, 0x15, 0x16})   # the most a profile may allow after the gate
REQUIRED_ESC = frozenset({0x05, 0x15})            # status and the tool check
STOP_ESC = 0x09
STOP_WRITE = bytes([ESC, STOP_ESC])
STOP_WRITES = 2                  # ESC 09 twice: the second covers the firmware's latch
NEVER_ESC = frozenset({0x00, 0x11, 0x12, 0x13})   # named in refusals; every code outside
                                                  # the rules is refused too
# The user's explicit Pause and Resume: each one write alone, never a query, never in a motion
# write, never the stop, never sent by OpenCutter on its own (device.link.SafeLink.pause/resume)
PAUSE_ESC = 0x07
RESUME_ESC = 0x08
PAUSE_WRITE = bytes([ESC, PAUSE_ESC])
RESUME_WRITE = bytes([ESC, RESUME_ESC])
PRE_GATE_ESC = frozenset({0x05})
PRE_GATE_TEXT = frozenset({b"FG"})
# The text queries any profile may allow after the gate: getters that only report and move
# nothing (firmware analysis of V5.05; the CAMEO 4's allowlist in device/protocol.py). A profile
# lists the subset its own firmware answers that way.
READ_ONLY_QUERIES = frozenset({b"FG", b"TB71", b"FA", b"FQ0", b"FQ2", b"TI", b"TO", b"TC", b"G",
                               b"[", b"U"})
REQUIRED_TEXT = frozenset({b"U"})                 # the unit check after the header
MAX_QUERY_WRITE = 20
FIRMWARE_WRITE_CAP = 480         # the BLE write cap of every bundled firmware
UNIT_MM = 0.05                   # the plotter unit of every known model
# The widest value any bundled firmware takes (firmware analysis): speed 30 cm/s (CAMEO 4 and 5;
# Portrait 10), FX 40 (CAMEO 5 generation; 33 elsewhere), TJ 10 (Portrait 4: 3); passes is the
# UI's limit. A profile's range must lie inside these.
SPEED_MAX = 30
FORCE_MAX = 40
ACCELERATION_MAX = 10
PASSES_MAX = 10

_TG = re.compile(rb"TG(0|[1-9][0-9]?)")
_ID = re.compile(r"[a-z][a-z0-9_]*", re.ASCII)
_FG_MODEL = re.compile(r"[!-~]+(?: [!-~]+)*", re.ASCII)
_PRINTABLE = re.compile(r"[ -~]+", re.ASCII)
_REPLY = re.compile(r"0|[1-9][0-9]?", re.ASCII)


class SafetyError(ValueError):
    """A write or a value that must never reach a machine."""


class UnsafeProfile(SafetyError):
    """A profile that breaks a global safety rule. The registry refuses it at import."""


# --- commands ----------------------------------------------------------------------------------
def bare(command: bytes) -> bytes:
    """The command as the firmware reads it: leading filler bytes stripped."""
    return command.lstrip(FILLER)


def command_starts(command: bytes) -> list[bytes]:
    """Every place the firmware may start a command in `command`: after the leading filler
    bytes, and at every later ASCII letter, since its parser starts a new command at a command
    letter even without an ETX before it (FX10,TJ3 runs TJ3). Each item runs to the end."""
    c = bare(command)
    return [c] + [c[i:] for i in range(1, len(c)) if c[i:i + 1].isalpha()]


def is_never_send(command: bytes) -> bool:
    """True for a command on the global never-send list or one that starts with a lowercase
    letter, after filler bytes are stripped, also where it follows another command's letters or
    values without an ETX (command_starts: FX10,TB72, FN0FP, M100,100fb). FM is not decided
    here (is_expand_command)."""
    return any(c.startswith(NEVER_SEND_PREFIXES) or c[:1].islower()
               for c in command_starts(command))


def is_expand_command(command: bytes) -> bool:
    """True for any FM command (after filler bytes are stripped), also one that follows another
    command without an ETX (FX10,FM1)."""
    return any(c.startswith(FM_PREFIX) for c in command_starts(command))


def has_tg(command: bytes) -> bool:
    """True when a TG command starts anywhere the firmware may start one (command_starts)."""
    return any(c.startswith(b"TG") for c in command_starts(command))


def expand_allowed(profile: MachineProfile) -> bool:
    """True when the profile may send FM1 at all: its data defines expand mode, and every FG
    model text it has (its own and shared ones) is in EXPAND_MODELS, none in NO_EXPAND_MODELS."""
    texts = profile.identity.fg_models | profile.identity.shared_fg_models
    return (profile.dialect.expand_mode is True and bool(texts) and texts <= EXPAND_MODELS
            and not texts & NO_EXPAND_MODELS)


def tg_value(command: bytes) -> int | None:
    """n of an exact 'TG<n>' command, or None."""
    m = _TG.fullmatch(command)
    return int(m[1]) if m else None


def check_write(data: bytes, profile: MachineProfile) -> None:
    """Raise SafetyError unless `data` is one motion write that keeps the global rules for
    `profile`: bytes, 1..profile.max_write long, ETX-terminated printable ASCII commands, no
    ESC, no empty command, nothing on the never-send list, TG only with a value of one of the
    profile's mats, and FM only as FM1 right after the TG9 FN0 TB50,0 of a mat with EXPAND,
    in the same write, on a profile that may send it. Each rule also holds where the firmware
    could start a command without an ETX before it (command_starts: FX10,TB72 is refused). It
    is a floor, not an allowlist: job and program validators are exact."""
    if type(data) is not bytes or not 0 < len(data) <= profile.max_write:
        raise SafetyError(f"a write must be 1..{profile.max_write} bytes")
    if bytes([ESC]) in data:
        raise SafetyError("ESC inside a write")
    if data[-1] != ETX:
        raise SafetyError("a write must end with ETX")
    tgs = {m.tg for m in profile.mats}
    expand_tgs = ({m.tg for m in profile.mats
                   if m.header_extra is HeaderExtra.EXPAND and m.tg == EXPAND_TG}
                  if expand_allowed(profile) else set())
    lead = len(EXPAND_AFTER) + 1                 # TG9, then EXPAND_AFTER, then FM1
    commands = data[:-1].split(bytes([ETX]))
    for i, c in enumerate(commands):
        if not c or not _PRINTABLE.fullmatch(c.decode("latin-1")):
            raise SafetyError(f"command {c!r} is empty or not printable ASCII")
        if is_never_send(c):
            raise SafetyError(f"never-send command {c!r}")
        if is_expand_command(c):
            before = commands[i - lead:i] if i >= lead else []
            if not (c == EXPAND_COMMAND and len(before) == lead
                    and tg_value(before[0]) in expand_tgs
                    and tuple(before[1:]) == EXPAND_AFTER):
                raise SafetyError(f"{c!r}: FM is sent only as FM1 right after TG9 FN0 TB50,0, "
                                  "for a mat with expand mode on a model whose data defines it")
        if has_tg(c) and tg_value(c) not in tgs:
            raise SafetyError(f"{c!r} is not the TG of a {profile.name} mat")


# --- queries and the stop ----------------------------------------------------------------------
def esc_query_allowed(code: object, profile: MachineProfile | None = None) -> bool:
    """True when ESC `code` may be sent as a query: before the gate (profile None) only ESC 05;
    after it the profile's ESC queries (always within QUERY_ESC)."""
    if type(code) is not int:
        return False
    if profile is None:
        return code in PRE_GATE_ESC
    return code in QUERY_ESC and code in profile.queries.esc


def text_query_allowed(text: object, profile: MachineProfile | None = None) -> bool:
    """True when the getter `text` (without ETX) may be sent: before the gate only FG; after it
    the profile's text queries, always within READ_ONLY_QUERIES. Never a never-send command, FM
    or a lowercase letter."""
    if type(text) is not bytes or not text or is_never_send(text) or is_expand_command(text):
        return False
    if any(0x61 <= b <= 0x7A for b in text) or text not in READ_ONLY_QUERIES:
        return False
    return text in (PRE_GATE_TEXT if profile is None else profile.queries.text)


def check_query(data: bytes, profile: MachineProfile | None = None) -> None:
    """Raise SafetyError unless `data` is exactly one allowed read-only query: ESC <code> alone
    in a 2-byte write, or one getter followed by its single ETX. Before the gate (profile None)
    that is ESC 05 or FG only."""
    limit = MAX_QUERY_WRITE if profile is None else profile.queries.max_write
    if type(data) is not bytes or not data:
        raise SafetyError("empty write")
    if len(data) > limit:
        raise SafetyError(f"write of {len(data)} bytes exceeds {limit}")
    if data[0] == ESC:
        if len(data) == 2 and esc_query_allowed(data[1], profile):
            return
        raise SafetyError(f"ESC {data[1:2].hex() or '?'} is not an allowed query here")
    if data[-1] != ETX or data.count(ETX) != 1:
        raise SafetyError("exactly one ETX-terminated command per write")
    if not text_query_allowed(data[:-1], profile):
        raise SafetyError(f"{data[:-1]!r} is not an allowed read-only query here")


def is_stop_write(data: object) -> bool:
    """True only for ESC 09, alone."""
    return type(data) is bytes and data == STOP_WRITE


def is_pause_write(data: object) -> bool:
    """True only for ESC 07, alone: the user's explicit Pause."""
    return type(data) is bytes and data == PAUSE_WRITE


def is_resume_write(data: object) -> bool:
    """True only for ESC 08, alone: the user's explicit Resume."""
    return type(data) is bytes and data == RESUME_WRITE


# --- the profile self-check --------------------------------------------------------------------
def check_entry(entry: object, pinned: Iterable[bytes] = ()) -> None:
    """check_profile() for a MachineProfile, check_recognized() for a RecognizedModel; refuse
    anything else."""
    if isinstance(entry, MachineProfile):
        check_profile(entry, pinned)
    elif isinstance(entry, RecognizedModel):
        if tuple(pinned):
            raise UnsafeProfile(f"{entry.id!r} is only recognized: it has no programs")
        check_recognized(entry)
    else:
        raise UnsafeProfile(f"{entry!r} is neither a MachineProfile nor a RecognizedModel")


def check_profile(profile: MachineProfile, pinned: Iterable[bytes] = ()) -> None:
    """Raise UnsafeProfile listing every rule `profile` breaks. `pinned`: the profile's pinned
    program writes, each checked with check_write()."""
    if not isinstance(profile, MachineProfile):
        raise UnsafeProfile(f"{profile!r} is not a MachineProfile")
    problems = _collect(_profile_problems(profile, tuple(pinned)))
    if problems:
        raise UnsafeProfile(f"profile {profile.id!r}: " + "; ".join(problems))


def check_recognized(entry: RecognizedModel) -> None:
    """Raise UnsafeProfile listing every rule a recognition-only entry breaks."""
    if not isinstance(entry, RecognizedModel):
        raise UnsafeProfile(f"{entry!r} is not a RecognizedModel")
    problems = _collect(_common_problems(entry))
    problems += _collect(_identity_problems(entry.identity, enabled=False))
    problems += _collect(_reason_problems(entry.reason, "reason"))
    if problems:
        raise UnsafeProfile(f"recognized model {entry.id!r}: " + "; ".join(problems))


def _collect(problems: Iterator[str]) -> list[str]:
    """The problems found; a shape the checks cannot even read is one more problem (refused,
    never a crash with another exception)."""
    out: list[str] = []
    try:
        for problem in problems:
            out.append(problem)
    except (AttributeError, TypeError, ValueError, KeyError, IndexError) as e:
        out.append(f"malformed ({type(e).__name__}: {e})")
    return out


def _common_problems(entry: MachineProfile | RecognizedModel) -> Iterator[str]:
    if type(entry.id) is not str or not _ID.fullmatch(entry.id):
        yield "id must be lower case letters, digits and '_', starting with a letter"
    if type(entry.name) is not str or not _PRINTABLE.fullmatch(entry.name) \
            or entry.name != entry.name.strip():
        yield "name must be printable ASCII"
    if type(entry.basis) is not str or not entry.basis.strip():
        yield "basis must say where the values come from"


def _reason_problems(reason: object, what: str) -> Iterator[str]:
    if type(reason) is not str or not reason.strip():
        yield f"{what} must say in plain words why it cannot be driven"
    elif reason.endswith(".") or reason != reason.strip():
        yield f"{what} is the part after 'cannot drive it yet:', without a final period"


def _identity_shape_ok(identity: object) -> bool:
    return (isinstance(identity, Identity) and type(identity.fg_models) is frozenset
            and type(identity.shared_fg_models) is frozenset
            and type(identity.usb_ids) is frozenset and type(identity.lines) is tuple
            and all(isinstance(ln, FirmwareLine) for ln in identity.lines))


def _identity_problems(identity: object, enabled: bool) -> Iterator[str]:
    if not isinstance(identity, Identity):
        yield "identity is not an Identity"
        return
    if not _identity_shape_ok(identity):
        yield ("identity: FG texts, shared texts and USB ids must be frozensets, firmware lines "
               "a tuple of FirmwareLine")
        return
    texts = identity.fg_models | identity.shared_fg_models
    if not texts and not identity.usb_ids:
        yield "nothing identifies it"
    for text in sorted(texts, key=repr):
        if type(text) is not str or not _FG_MODEL.fullmatch(text):
            yield f"FG model text {text!r} must be printable ASCII words with single spaces"
    if identity.fg_models & identity.shared_fg_models:
        yield "an FG model text is both its own and shared"
    for usb in identity.usb_ids:
        if not (type(usb) is tuple and len(usb) == 2
                and all(type(v) is int and 0 <= v <= 0xFFFF for v in usb)):
            yield f"USB id {usb!r} must be (vendor, product), each 0..0xffff"
    if type(identity.ble_names) is not tuple or not all(
            type(n) is str and _PRINTABLE.fullmatch(n) and n == n.strip()
            for n in identity.ble_names):
        yield "BLE names must be a tuple of printable prefixes"
    majors = [ln.major for ln in identity.lines]
    if len(set(majors)) != len(majors):
        yield "two firmware lines have the same major number"
    if not enabled:
        if identity.lines:
            yield "a recognized model has no firmware lines: every firmware is refused alike"
        return
    if not identity.fg_models:
        yield "a profile needs at least one FG model text of its own"
    if not any(ln.support.jobs_allowed for ln in identity.lines):
        yield "a profile needs a firmware line that may be driven"
    for ln in identity.lines:
        if type(ln.major) is not int or not 0 <= ln.major <= 99:
            yield f"firmware line {ln.major!r}: the major number must be 0..99"
        if not isinstance(ln.support, Support):
            yield f"firmware line V{ln.major}: support must be a Support"
            continue
        if type(ln.basis) is not str or not ln.basis.strip():
            yield f"firmware line V{ln.major} has no basis"
        if ln.support is Support.RECOGNIZED:
            yield from _reason_problems(ln.reason, f"firmware line V{ln.major} reason")
        elif ln.reason:
            yield f"firmware line V{ln.major}: only a RECOGNIZED line has a reason"


def _range_ok(r: object, lo: int | None = None, hi: int | None = None) -> bool:
    if not isinstance(r, IntRange) or type(r.lo) is not int or type(r.hi) is not int:
        return False
    return r.lo <= r.hi and (lo is None or r.lo >= lo) and (hi is None or r.hi <= hi)


def _profile_problems(p: MachineProfile, pinned: tuple[bytes, ...]) -> Iterator[str]:
    yield from _common_problems(p)
    if p.support not in (Support.TESTED, Support.UNTESTED):
        yield "a MachineProfile is TESTED or UNTESTED; a refused model is a RecognizedModel"
        return
    yield from _identity_problems(p.identity, enabled=True)
    shape = list(_shape_problems(p))
    yield from shape
    if shape or not _identity_shape_ok(p.identity):
        return                                   # the checks below read these shapes
    for ln in p.identity.lines:
        if isinstance(ln.support, Support) and ln.support.rank > p.support.rank:
            yield f"firmware line V{ln.major} is {ln.support.value}, wider than the profile"
    yield from _dialect_problems(p)
    yield from _holder_problems(p)
    yield from _range_problems(p)
    yield from _mat_problems(p)
    yield from _status_problems(p)
    yield from _query_problems(p)
    if p.stop.writes != (STOP_WRITE,) * STOP_WRITES:
        yield "the stop must be ESC 09 twice, each alone"
    gap = p.stop.gap_s
    if not (type(gap) in (int, float) and math.isfinite(gap) and 0 < gap <= 2):
        yield "the stop gap must be 0..2 s"
    if p.pause is not None and not is_pause_write(p.pause):
        yield "the pause must be None or ESC 07 alone"
    if p.resume is not None and not is_resume_write(p.resume):
        yield "the resume must be None or ESC 08 alone"
    if p.unit_mm != UNIT_MM:
        yield f"the plotter unit must be {UNIT_MM} mm"
    if type(p.max_write) is not int or not 20 <= p.max_write <= FIRMWARE_WRITE_CAP:
        yield f"max_write must be 20..{FIRMWARE_WRITE_CAP}"
    elif type(p.batch_bytes) is not int or p.batch_bytes < p.max_write:
        yield "batch_bytes must be at least max_write"
    if type(p.max_coordinate_su) is not int or not 1 <= p.max_coordinate_su <= 999_999:
        yield "max_coordinate_su must be 1..999999"
    if (type(p.lowering_prefixes) is not tuple or b"D" not in p.lowering_prefixes
            or not all(type(x) is bytes and x for x in p.lowering_prefixes)):
        yield "lowering_prefixes must list D (and TF where a tool takes a Blade Depth)"
    elif any(t.depth for h in p.holders for t in h.tools) and b"TF" not in p.lowering_prefixes:
        yield "TF lowers the AutoBlade: it must be in lowering_prefixes"
    if type(p.programs) is not frozenset or not all(type(x) is str for x in p.programs):
        yield "programs must be a frozenset of names"
    for flag in ("fg_claims_session", "power_cycle_to_connect"):
        if type(getattr(p, flag)) is not bool:
            yield f"{flag} must be a bool"
    yield from _rendering_problems(p)
    for data in pinned:
        try:
            check_write(data, p)
        except SafetyError as e:
            yield f"pinned write {data!r}: {e}"


def _shape_problems(p: MachineProfile) -> Iterator[str]:
    """The parts the other checks read have the right types (frozen tuples, never lists that
    could change after the check)."""
    parts = {"ranges": (p.ranges, Ranges), "dialect": (p.dialect, Dialect),
             "status": (p.status, StatusModel), "queries": (p.queries, Queries),
             "stop": (p.stop, StopSequence)}
    for name, (value, kind) in parts.items():
        if not isinstance(value, kind):
            yield f"{name} is not a {kind.__name__}"
    if type(p.holders) is not tuple or not all(
            isinstance(h, Holder) and type(h.tools) is tuple
            and all(isinstance(t, Tool) for t in h.tools) for h in p.holders):
        yield "holders must be a tuple of Holder, each with a tuple of Tool"
    if type(p.mats) is not tuple or not all(isinstance(m, Mat) for m in p.mats):
        yield "mats must be a tuple of Mat"
    if isinstance(p.stop, StopSequence) and type(p.stop.writes) is not tuple:
        yield "the stop writes must be a tuple"


def _dialect_problems(p: MachineProfile) -> Iterator[str]:
    d = p.dialect
    if not isinstance(d.holders, HolderForm) or not isinstance(d.accel, AccelForm) \
            or not isinstance(d.tool_reply, ToolReply):
        yield "the dialect's forms must be HolderForm, AccelForm and ToolReply values"
    if d.speed is not SpeedForm.WHOLE:
        yield "speed is written as whole numbers only"
    texts = p.identity.fg_models | p.identity.shared_fg_models
    if type(d.expand_mode) is not bool:
        yield "expand_mode must be a bool"
    elif d.expand_mode and texts & NO_EXPAND_MODELS:
        yield "expand mode (FM) is never sent to a CAMEO 4, 4 Plus, 5 or 5 Plus"
    elif d.expand_mode and not texts <= EXPAND_MODELS:
        yield ("expand mode (FM) is allowed only for the CAMEO 4 Pro and the CAMEO Pro MK-II "
               "(EXPAND_MODELS)")


def _holder_problems(p: MachineProfile) -> Iterator[str]:
    d = p.dialect
    numbers = [h.number for h in p.holders]
    if not numbers:
        yield "no tool holder"
        return
    if numbers != sorted(set(numbers)) or not all(type(n) is int and 1 <= n <= 8 for n in numbers):
        yield "holder numbers must be distinct, ascending and 1..8"
    if d.holders is HolderForm.SINGLE and numbers != [1]:
        yield "a single-holder model has exactly holder 1"
    slots = d.tool_slots
    if (type(slots) is not tuple or len(slots) != len(numbers) or len(set(slots)) != len(slots)
            or not all(s in (0, 1) and type(s) is int for s in slots)):
        yield "tool_slots needs one distinct ESC 15 reply slot (0 or 1) per holder"
    top = 9 if d.tool_reply is ToolReply.ONE_DIGIT else 99
    for h in p.holders:
        codes = [t.code for t in h.tools]
        if type(h.label) is not str or not h.label.strip():
            yield f"holder {h.number} needs a label"
        if len(set(codes)) != len(codes) or not all(type(c) is int and 0 <= c <= top
                                                    for c in codes):
            yield (f"holder {h.number}: tool codes must be distinct and 0..{top} for the ESC 15 "
                   "reply grammar")
        none = h.tool(0)
        if none is None or none.usable:
            yield f"holder {h.number}: code 0 (no tool detected) must be listed and not usable"
        for t in h.tools:
            if type(t.name) is not str or not t.name.strip() or not t.action.strip():
                yield f"holder {h.number}, code {t.code}: name and action are needed"
            if t.depth and (h.number != 1 or not t.blade):
                yield (f"holder {h.number}, code {t.code}: only a blade in holder 1 takes a "
                       "Blade Depth (TF acts on holder 1 only)")


def _range_problems(p: MachineProfile) -> Iterator[str]:
    r = p.ranges
    checks = {"speed": _range_ok(r.speed, 1, SPEED_MAX),
              "force": _range_ok(r.force, 1, FORCE_MAX),
              "offset": _range_ok(r.offset, 0, 99), "blade_depth": _range_ok(r.blade_depth, 0, 10),
              "overcut": _range_ok(r.overcut, 0, 9), "passes": _range_ok(r.passes, 1, PASSES_MAX)}
    for name, ok in checks.items():
        if not ok:
            yield f"range {name} is out of the firmware's bounds or malformed"
    if (r.acceleration is None) != (p.dialect.accel is AccelForm.NONE):
        yield "acceleration has a range exactly when the dialect writes TJ"
    elif r.acceleration is not None and not _range_ok(r.acceleration, 1, ACCELERATION_MAX):
        yield (f"range acceleration must lie within 1..{ACCELERATION_MAX} (TJ0 is ignored, and no "
               "firmware takes more)")


def _mat_problems(p: MachineProfile) -> Iterator[str]:
    keys = [m.key for m in p.mats]
    if not keys:
        yield "no mat"
        return
    if len(set(keys)) != len(keys):
        yield "two mats share a key"
    default = p.mat(p.default_mat)
    if default is None:
        yield f"default mat {p.default_mat!r} is not one of the mats"
    elif not default.offered(p.support):
        yield "the default mat must be offered at the profile's own level"
    expand_ok = expand_allowed(p)
    for m in p.mats:
        where = f"mat {m.key!r}"
        if (type(m.key) is not str or not _PRINTABLE.fullmatch(m.key)
                or type(m.label) is not str or not m.label.strip()):
            yield f"{where}: key and label must be printable text"
        if type(m.tg) is not int or not 1 <= m.tg <= 13:
            yield f"{where}: TG must be 1..13 (TG0 is no mat)"
        size = m.size_mm
        if not (type(size) is tuple and len(size) == 2 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                and v > 0 for v in size)):
            yield f"{where}: size_mm must be two positive numbers"
            continue
        area = m.area_su
        if not (type(area) is tuple and len(area) == 4 and all(type(v) is int for v in area)):
            yield f"{where}: area_su must be four ints"
            continue
        a0, b0, a1, b1 = area
        if not (0 <= a0 < a1 <= p.max_coordinate_su and 0 <= b0 < b1 <= p.max_coordinate_su):
            yield f"{where}: the cut area needs 0 <= lower < upper on both axes"
        if a1 > round(size[1] / p.unit_mm) or b1 > round(size[0] / p.unit_mm):
            yield f"{where}: the cut area is larger than the mat"
        if not isinstance(m.u_basis, UBasis) or (m.expected_u is None) != (
                m.u_basis is UBasis.UNKNOWN):
            yield f"{where}: an expected U is given exactly when its basis is KNOWN or INFERRED"
        elif m.expected_u is not None and not (
                type(m.expected_u) is tuple and len(m.expected_u) == 2
                and all(type(v) is int and v > 0 for v in m.expected_u)):
            yield f"{where}: expected U must be two positive ints"
        elif m.expected_u is not None and (a1 > m.expected_u[0] or b1 > m.expected_u[1]):
            yield (f"{where}: the cut area reaches beyond the window its U reply confirms "
                   "(a = feed first, as in the area)")
        if type(m.basis) is not str or not m.basis.strip():
            yield f"{where}: basis must say where the TG, area and U come from"
        if m.header_extra is not None:
            if m.header_extra is not HeaderExtra.EXPAND:
                yield f"{where}: unknown header extra"
            elif m.tg != EXPAND_TG or not expand_ok:
                yield (f"{where}: FM1 is sent only after TG{EXPAND_TG} FN0 TB50,0, on a profile "
                       "whose data defines expand mode and whose model is one of EXPAND_MODELS "
                       "(never a CAMEO 4, 4 Plus, 5 or 5 Plus)")
            elif size[0] <= TWELVE_INCH_MM:
                yield f"{where}: FM1 is never sent with a 12-inch mat"
        if m.grid_in is not None and not (type(m.grid_in) is tuple and len(m.grid_in) == 2
                                          and all(type(v) is int and v > 0 for v in m.grid_in)):
            yield f"{where}: grid_in must be two positive ints"
        if m.feed_mm is not None and not (isinstance(m.feed_mm, (int, float))
                                          and m.feed_mm >= 0):
            yield f"{where}: feed_mm must be a length"


def _status_problems(p: MachineProfile) -> Iterator[str]:
    s = p.status
    replies = [r for r, _ in s.texts] if type(s.texts) is tuple else []
    if not replies or len(set(replies)) != len(replies) or not all(
            type(r) is str and _REPLY.fullmatch(r) for r, _ in s.texts):
        yield "status texts need distinct replies of one or two digits"
    if not all(type(t) is str and t.strip() for _, t in s.texts):
        yield "every status reply needs a text"
    if len({s.ready, s.busy, s.load}) != 3 or not {s.ready, s.busy, s.load} <= set(replies):
        yield "ready, busy and load must be three distinct listed replies"
    if type(s.unloaded_after_header) is not bool:
        yield "unloaded_after_header must be a bool"


def _query_problems(p: MachineProfile) -> Iterator[str]:
    q = p.queries
    if (type(q.esc) is not frozenset or not all(type(c) is int for c in q.esc)
            or not q.esc <= QUERY_ESC):
        yield "ESC queries must be within ESC 05, 10, 15 and 16 (never 11, 12, 13, 07 or 00)"
    if not REQUIRED_ESC <= q.esc:
        yield "ESC 05 (status) and ESC 15 (tool check) are needed"
    if type(q.max_write) is not int or not 2 <= q.max_write <= MAX_QUERY_WRITE:
        yield f"the query write limit must be 2..{MAX_QUERY_WRITE}"
    if type(q.text) is not frozenset:
        yield "text queries must be a frozenset of bytes"
        return
    if not REQUIRED_TEXT <= q.text:
        yield "the U query (unit check) is needed"
    for t in sorted(q.text, key=repr):
        if type(t) is not bytes or not t or not _PRINTABLE.fullmatch(t.decode("latin-1")):
            yield f"text query {t!r} must be printable ASCII"
        elif is_never_send(t) or is_expand_command(t):
            yield f"text query {t!r} is on the never-send list"
        elif any(0x61 <= b <= 0x7A for b in t):
            yield f"text query {t!r} has a lowercase letter"
        elif t not in READ_ONLY_QUERIES:
            yield f"text query {t!r} is not a read-only getter (READ_ONLY_QUERIES)"
        elif len(t) + 1 > q.max_write:
            yield f"text query {t!r} is longer than the query write limit"


def _rendering_problems(p: MachineProfile) -> Iterator[str]:
    """The profile's own header, trailer and condition blocks keep the rules and fit its
    command grammar; the grammar refuses every never-send command."""
    try:
        grammar = p.setting_grammar()
        writes = [p.mat_texts(m.key) for m in p.mats] + [p.area_texts(m.key) for m in p.mats]
        writes.append(p.trailer_texts())
        r = p.ranges
        for h in p.holders:
            depth = r.blade_depth.lo if any(t.depth for t in h.tools) else None
            for top in (False, True):             # every range at its low end, then its high end
                pick = attrgetter("hi" if top else "lo")
                writes.append(p.condition_texts(
                    holder=h.number, speed=pick(r.speed), force=pick(r.force),
                    acceleration=None if r.acceleration is None else pick(r.acceleration),
                    offset=pick(r.offset), line_segment_overcut=top,
                    overcut=(pick(r.overcut), pick(r.overcut)), blade_depth=depth))
    except (ValueError, TypeError, AttributeError) as e:
        yield f"its commands cannot be written: {e}"
        return
    for texts in writes:
        data = b"".join(t.encode("ascii") + bytes([ETX]) for t in texts)
        try:
            check_write(data, p)
        except SafetyError as e:
            yield f"its own commands {texts!r} break a rule: {e}"
        for t in texts:
            if t != "M0,0" and not grammar.fullmatch(t.encode("ascii")):
                yield f"its command grammar does not accept its own {t!r}"
    expand = any(m.header_extra is HeaderExtra.EXPAND for m in p.mats)
    for c in _never_send_samples():
        if grammar.fullmatch(c) and not (expand and c == EXPAND_COMMAND):
            yield f"its command grammar accepts the never-send command {c!r}"


def _never_send_samples() -> Iterator[bytes]:
    for prefix in NEVER_SEND_PREFIXES + (FM_PREFIX,):
        for tail in (b"", b"0", b"1", b"1,1", b"0,0,0", b"G", b"GRFCC1"):
            yield prefix + tail
    yield from (b"tg1", b"j1", b"fx1,1", b"fn0", b"z1,1", b"m1,1", b"d1,1", b"tf1,1")
