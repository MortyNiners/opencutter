# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""BLE protocol facts and the per-machine query, reply and stop rules.

Pure Python, no I/O. The transport rules behind these constants:
  * host -> machine: raw GPGL bytes written to CHAR_DATA (ATT Write Request, with response);
  * machine -> host: query replies as indications on CHAR_REPLY (ASCII, ends with ETX);
  * machine -> host: unsolicited status changes as indications on CHAR_STATUS (a status digit
    + ETX).
There is no framing, no CRC and no handshake. The GATT UUIDs are the same on every model.

What differs between models comes from the active machine profile (opencutter.machines): the
query allowlist, the ESC 05 replies, the ESC 15 reply grammar and holders, the commands that
lower a tool, the stop and the expected U reply of each mat. Every function that needs one takes
`profile` (default: the CAMEO 4), so the CAMEO 4 behaves exactly as before profiles existed:
  check_safe(data, queries=None)       one allowed read-only query. None: the CAMEO 4's
                                       allowlist (SAFE_*), as always; PRE_GATE before the model
                                       gate (ESC 05 and FG only); after it the profile's Queries
                                       (a MachineProfile is taken for its queries). The global
                                       safety rules of machines.safety hold for every set.
  is_status(reply, profile)            an exact ESC 05 reply of that model; status_text() names it,
                                       not_ready_text() says in plain words what to do when it
                                       is not the ready digit (nothing was sent).
  parse_tools(reply, profile)          the ESC 15 codes per holder, in profile.holders order,
                                       with the profile's grammar (one or two digits) and slot
                                       rule (on a one-holder model the unused slot must read 0).
  tool_name(), describe_tools()        Studio's tool names and the holder labels of the profile.
  lowers_tool(data, profile)           whether a write holds a command that can lower a tool,
                                       anywhere in it.
  stop_sequence(profile)               the stop writes (ESC 09 twice); the gap is
                                       profile.stop.gap_s.
  evaluate_units(reply, expected, enforced)
                                       the U check against the exact (a, b) pair of the mat;
                                       'unchecked' only for a U reply that is not enforced (the
                                       mat's U is UNKNOWN), never for a missing pair alone.
The CAMEO 4 names (STATUS_TEXT, TOOL_NAMES, STOP_SEQUENCE, MAX_DATA_WRITE, SAFE_*,
MAX_QUERY_WRITE) stay as aliases computed from the CAMEO 4 profile.

The model gate itself is machines.detect: is_tested_model(), model_text(), reported_model() and
wrong_model_message() are kept for their callers, built on it.
"""
from __future__ import annotations

import os
import re

from .. import APP_ID
from ..i18n import N_
from ..machines import CAMEO4, MachineProfile, Mat, Queries, Support, UBasis, safety, tested
from ..machines.detect import FG_PADDING, fg_text
from ..machines.detect import detect as _detect

SERVICE = "e2088282-4fde-42f9-bb22-6ec3c7ed8f91"
CHAR_DATA = "6d92661d-f429-4d67-929b-28e7a9780912"    # write: GPGL stream to the machine
CHAR_REPLY = "8dcf199a-30e7-4bd4-beb6-beb57dca866c"   # indicate: query replies
CHAR_STATUS = "61490654-b5b4-458c-a867-9e15bc1471e0"  # indicate: status changes

ETX = 0x03
ESC = 0x1B
REPLY_TIMEOUT = 3.0        # a reply to a query at session open or of a one-shot op
# Inside a session an ESC 05 (or ESC 15) query is never given up while the link is up: once its
# reply is this late the query is "overdue" (the app says "Checking connection"), the wait is
# logged and the query is asked once more (a reply can be lost over Bluetooth LE).
STATUS_OVERDUE = 7.0
# After the stop op of a client that asks for the read-back: ESC 05 is read for up to this many
# seconds once the machine read Paused (a paused machine ends the job about 5 s after the stop;
# firmware analysis). Still Paused then: the client tells the user to press Unload.
STOP_CHECK_PAUSED = 15.0

# The service's socket and lock, in runtime_dir(). Builds before 0.1.0 used these names directly
# in $XDG_RUNTIME_DIR: the daemon holds that old lock too, and `opencutter stop` also stops there.
SOCKET_NAME = "opencutter.sock"
LOCK_NAME = "opencutter.lock"
# The socket and the lock of the legacy prototype daemon, in $XDG_RUNTIME_DIR. The OpenCutter
# daemon holds that lock too, so the two daemons can never drive the machine together.
LEGACY_SOCKET_NAME = "cameo4.sock"
LEGACY_LOCK_NAME = "cameo4.lock"
# Why the daemon holds no link: the "reason" of its ping reply (None while connected or unknown).
REASON_HELD_ELSEWHERE = "held-elsewhere"   # another program holds the machine's link: not joining
REASON_NOT_PAIRED = "not-paired"           # BlueZ knows no paired CAMEO
REASON_UNREACHABLE = "unreachable"         # the machine does not accept a connection (power cycle it)
REASON_NO_BLUETOOTH = "no-bluetooth"       # BlueZ cannot be reached at all (Bluetooth off or not
                                           # running, or a Flatpak without the org.bluez permission)
REASON_WRONG_MODEL = "wrong-model"         # the machine's FG reply names no model and firmware
                                           # line OpenCutter drives: its link was released and it
                                           # is not used


class UnsafeCommand(ValueError):
    """A write was refused by a safety check; nothing was sent."""


# --- the stop ------------------------------------------------------------------------------
def stop_sequence(profile: MachineProfile = CAMEO4) -> tuple[bytes, ...]:
    """The profile's stop writes, each sent alone, profile.stop.gap_s seconds apart: ESC 09 (cancel)
    twice on every model. While busy or with GPGL bytes pending, ESC 09 runs the firmware's hard
    stop (tool up, step timers stopped, motion ring cleared); the second one covers a latch that
    makes the first one only clear itself. ESC 07 (pause) is never part of the stop: it stops
    nothing by itself and, consumed late, leaves the machine paused.

    Raises UnsafeCommand for a stop that is not exactly ESC 09 twice (the registry refuses such a
    profile at import; this holds for a profile built anywhere else too). A link calls it when
    its profile is set, so that a bad stop is refused before anything moves, never at the moment
    a stop is needed."""
    writes = profile.stop.writes
    if type(writes) is not tuple or len(writes) != safety.STOP_WRITES \
            or not all(safety.is_stop_write(w) for w in writes):
        raise UnsafeCommand(f"the stop of the {profile.name} is not ESC 09 twice")
    return writes


# The CAMEO 4's stop: ESC 09 twice, each alone.
STOP_SEQUENCE = stop_sequence(CAMEO4)

# Longest data write Studio sends to a CAMEO 4: 182 bytes. The firmware's hard cap is 480 bytes.
# Other models: profile.max_write.
MAX_DATA_WRITE = CAMEO4.max_write

# The CAMEO 4's ESC 05 replies and tool names (status_text() and tool_name() for any profile).
STATUS_TEXT: dict[str, str] = CAMEO4.status.as_dict()
TOOL_NAMES: dict[int, dict[int, str]] = CAMEO4.tool_names()


# --- read-only query allowlist ------------------------------------------------------------
# Only these may be written outside a motion session. Anything else (motion, settings, firmware
# download ESC 11/12/13, the FP diagnostic unlock, lowercase, padding tricks...) is refused.
# Before the model gate only ESC 05 and FG (PRE_GATE); after it the active profile's queries,
# always within the global rules of machines.safety (ESC 05/10/15/16 at most, each alone in a
# 2-byte write; text getters from safety.READ_ONLY_QUERIES only, never a never-send command,
# FM or a lowercase letter; at most safety.MAX_QUERY_WRITE bytes).
PRE_GATE = Queries(esc=safety.PRE_GATE_ESC, text=safety.PRE_GATE_TEXT,
                   max_write=safety.MAX_QUERY_WRITE)
# The CAMEO 4's allowlist: what check_safe(data) allows without a query set.
SAFE_ESC_QUERIES: frozenset[int] = CAMEO4.queries.esc        # answered by the M0; each alone
SAFE_TEXT_QUERIES: frozenset[bytes] = CAMEO4.queries.text    # read-only GPGL getters
MAX_QUERY_WRITE = CAMEO4.queries.max_write                   # every allowed query is tiny


def _query_set(queries: Queries | MachineProfile | None) -> Queries:
    """The Queries check_safe() applies; UnsafeCommand for anything it cannot read as one (a
    malformed set refuses every query instead of failing with another error)."""
    q = CAMEO4.queries if queries is None else queries
    if isinstance(q, MachineProfile):
        q = q.queries
    if not (isinstance(q, Queries) and type(q.esc) is frozenset and type(q.text) is frozenset
            and type(q.max_write) is int):
        raise UnsafeCommand(f"no query allowlist in {queries!r}")
    return q


def check_safe(data: bytes, queries: Queries | MachineProfile | None = None) -> None:
    """Raise UnsafeCommand unless `data` is exactly one allowlisted read-only query.

    `queries`: None keeps the CAMEO 4's allowlist (SAFE_ESC_QUERIES, SAFE_TEXT_QUERIES), as every
    caller had before machine profiles; PRE_GATE before the model gate (ESC 05 and FG only); after
    the gate the active profile's Queries, or the MachineProfile itself. Whatever the set says, a
    query also keeps the global rules of machines.safety: ESC only within 05/10/15/16, text only
    a read-only getter, never a never-send command, FM or a lowercase letter, and never longer
    than safety.MAX_QUERY_WRITE bytes."""
    q = _query_set(queries)
    if type(data) is not bytes:
        raise UnsafeCommand("a query must be bytes")
    if not data:
        raise UnsafeCommand("empty write")
    limit = min(q.max_write, safety.MAX_QUERY_WRITE)
    if len(data) > limit:
        raise UnsafeCommand(f"write of {len(data)} bytes exceeds {limit}")
    if data[0] == ESC:
        if len(data) == 2 and data[1] in q.esc and data[1] in safety.QUERY_ESC:
            return
        allowed = "/".join(f"{c:02x}" for c in sorted(q.esc & safety.QUERY_ESC))
        raise UnsafeCommand(f"only ESC {allowed}, alone in a 2-byte write, are allowed")
    if data[-1] != ETX or data.count(ETX) != 1:
        raise UnsafeCommand("exactly one ETX-terminated command per write")
    cmd = data[:-1]
    if cmd not in q.text or not _read_only(cmd):
        raise UnsafeCommand(f"{cmd!r} is not an allowlisted read-only query")


def _read_only(cmd: bytes) -> bool:
    """The global floor for a text query (machines.safety): a known read-only getter, never on
    the never-send list, never FM, no lowercase letter."""
    return (cmd in safety.READ_ONLY_QUERIES and not safety.is_never_send(cmd)
            and not safety.is_expand_command(cmd) and not any(0x61 <= b <= 0x7A for b in cmd))


# --- reply grammar (exact: replies are never stripped) ------------------------------------
def is_status(reply: object, profile: MachineProfile = CAMEO4) -> bool:
    """True only for an exact ESC 05 reply of the profile's model ('0'..'5' on the CAMEO 4),
    unpadded. A padded '    0' from some other query is not the ready state '0'; a digit the
    model is not known to send (a '6' on the CAMEO 4) is no status either, and aborts a step."""
    return profile.status.is_status(reply)


def status_text(reply: object, profile: MachineProfile = CAMEO4, default: str = "?") -> str:
    """Studio's term for an exact ESC 05 reply of the profile's model ("Ready"), or `default`."""
    text = profile.status.text(reply)
    return default if text is None else text


# Keyed on the term, not the digit: every profile today has '3' Paused and '4' Cancelling, and a
# test profile with other digits keeps its own. A Paused status during a session makes it wait
# (nothing more is sent until the machine goes on, or the user cancels); INTERRUPTED_TERMS end a
# job for good once it has started: a session that sees one of them stops.
PAUSED = "Paused"
CANCELLING = "Cancelling"
REJECTED = "Rejected"
INTERRUPTED_TERMS = frozenset({CANCELLING})
# The advice before a job, for a machine that is paused or ending a cancel when no job of
# OpenCutter runs: a paused job that is not ours is never continued.
# The texts for people are made from templates marked with N_(): the window shows them in its
# own language (i18n.translate_text); the service, its log and the command line keep English.
UNLOAD_ADVICE = N_("Do not press Pause to continue: press Unload on the machine (or switch it "
                   "off and on), check that the tool is up, then load the Cutting Mat right "
                   "before the job.")


def not_ready_text(status: object, profile: MachineProfile = CAMEO4) -> str:
    """Why a session (or a step before the first write) is refused for an ESC 05 reply that is
    not the profile's ready digit, in plain words; nothing was sent. Busy, Paused, Cancelling and
    Rejected have their own text; the load digit and every digit without one of those terms keep
    "The machine is not ready (status 'N' = term). Load the Cutting Mat on the machine right
    before the job." Never advises pressing Pause to continue."""
    states = profile.status
    term = status_text(status, profile, default="")
    shown = N_("status {status} = {term}").format(status=repr(status),
                                                    term=status_text(status, profile))
    if status == states.busy:
        text = N_("The machine is busy ({shown}): it is still working, or another connection "
                  "such as USB holds it. Nothing was sent. Wait until it shows Ready, then try "
                  "again.")
    elif term == PAUSED:
        text = N_("The machine is paused ({shown}). Nothing was sent. {advice}")
    elif term == CANCELLING:
        text = N_("The machine is still ending a cancel ({shown}). Nothing was sent. Wait until "
                  "it shows Ready or Load Media. {advice}")
    elif term == REJECTED:
        text = N_("The machine rejected a command ({shown}): another connection, such as USB, "
                  "held it. Nothing was sent. Close the other connection, then try again.")
    else:
        text = N_("The machine is not ready ({shown}). Load the Cutting Mat on the machine right "
                  "before the job.")
    return text.format(shown=shown, advice=UNLOAD_ADVICE)


def parse_tools(reply: object, profile: MachineProfile = CAMEO4) -> tuple[int, ...] | None:
    """The ESC 15 codes of the profile's holders, in profile.holders order, or None (fail closed).

    The reply is ' t1, t2' exactly: one-digit codes on the CAMEO 4, up to two digits (no leading
    zero) on the CAMEO 5 generation. Each holder reads its own slot (Dialect.tool_slots); a slot
    no holder owns, the second one on a one-holder model, must read 0, otherwise the reply is
    refused like a malformed one. The CAMEO 4 gets (t1, t2), as always."""
    codes = profile.holder_codes(reply)
    if codes is None:
        return None
    return tuple(codes[h.number] for h in profile.holders)


def tool_name(holder: object, code: object, profile: MachineProfile = CAMEO4) -> str:
    """Studio's name of the tool `code` reported in `holder` (1 or 2 on the CAMEO 4), or
    'unknown tool <code>'."""
    return profile.tool_name(holder, code)


def describe_tools(reply: object, profile: MachineProfile = CAMEO4) -> str:
    """Human-readable form of an ESC 15 reply: 'Tool 1: AutoBlade, Tool 2: Sketch Pen' (the
    profile's holder labels)."""
    codes = parse_tools(reply, profile)
    if codes is None:
        return f"unclear reply {reply!r}"
    return ", ".join(f"{h.label}: {profile.tool_name(h.number, code)}"
                     for h, code in zip(profile.holders, codes))


# --- the machine model gate -----------------------------------------------------------------
# machines.detect decides from the FG reply alone. A link is used only for a model and firmware
# line OpenCutter drives; every other reply is refused. The names below give the TESTED
# profiles and their TESTED firmware lines (the service's log line for a refused machine).
def _tested_lines() -> tuple[tuple[str, str], ...]:
    """(model name, firmware text 'V5.xx') of every TESTED firmware line of a TESTED profile."""
    return tuple((p.name, f"V{ln.major}.xx") for p in tested() for ln in p.identity.lines
                 if ln.support is Support.TESTED)


TESTED_FIRMWARE = next(fw for name, fw in _tested_lines() if name == CAMEO4.name)   # "V5.xx"
# the tested machines, for messages: "CAMEO 4 (firmware V5.xx)"
TESTED_MACHINE = " or ".join(f"{name} (firmware {fw})" for name, fw in _tested_lines())
MODEL_TEXT_MAX = 40                            # longest FG text shown to people
_FG_PADDING = FG_PADDING                       # ETX, NUL and ASCII whitespace (machines.detect)
_fg_text = fg_text                             # the FG cleanup, moved to machines.detect


def is_tested_model(reply: object) -> bool:
    """True only when the FG reply names a TESTED model on a TESTED firmware line
    (machines.detect: verdict USE and Detection.tested): today 'CAMEO 4 V5.' and two digits,
    exactly, once ETX, NUL and whitespace are stripped at both ends. Anything else is False:
    other models ('CAMEO 4 PLUS V5.05', 'CAMEO 5 V1.07', 'Portrait 3 V1.05'), other firmware
    lines ('CAMEO 4 V1.20'), anything added or missing, lowercase, control characters inside,
    an empty reply and None. The daemon drives an UNTESTED profile too: that is
    detect(reply).verdict is Verdict.USE, not this."""
    return _detect(reply).tested


def model_text(reply: object) -> str:
    """An FG reply as people may see it (logs, the ping's "model", messages): ETX, NUL and
    whitespace stripped at both ends, every character that is not printable ASCII shown as '?',
    runs of spaces collapsed, at most MODEL_TEXT_MAX characters. '?' when nothing is left. The
    GUI must still escape it before it uses markup: it comes from the machine."""
    if isinstance(reply, (bytes, bytearray)):
        reply = bytes(reply).decode("ascii", "replace")
    text = reply.strip(FG_PADDING) if isinstance(reply, str) else ""
    text = "".join(ch if " " <= ch <= "~" else "?" for ch in text)
    text = re.sub(" {2,}", " ", text).strip()
    if len(text) > MODEL_TEXT_MAX:
        text = text[:MODEL_TEXT_MAX - 3].rstrip() + "..."
    return text or "?"


def reported_model(reply: object) -> str | None:
    """What a machine reports itself as: model_text() of its FG reply, or None when the reply
    holds nothing but ETX, NUL and whitespace (an empty reply names no model) or is not text."""
    if isinstance(reply, (bytes, bytearray)):
        reply = bytes(reply).decode("ascii", "replace")
    if not isinstance(reply, str) or not reply.strip(FG_PADDING):
        return None
    return model_text(reply)


def wrong_model_message(model: object) -> str:
    """Why nothing is sent to a machine that failed the model gate (the daemon's refusals and
    the command line). `model` is shown through model_text(); without one (None, an empty
    reply) the text says that the machine did not report its model. A recognized model's own
    reason is machines.detect's Detection.message. A name that more than one model reports
    ("CAMEO V1.10": a CAMEO 1, and a CAMEO Pro MK-II with some of its settings) gets
    SHARED_MODEL_MESSAGE: the reason is that the model cannot be told apart."""
    if reported_model(model) is None:
        return N_("This machine did not report its model. OpenCutter cannot drive it and will "
                  "not send anything to it.")
    if _detect(model).shared:
        return SHARED_MODEL_MESSAGE.format(model=model_text(model))
    return N_('This machine reports itself as "{model}". OpenCutter cannot drive it and will not '
              "send anything to it.").format(model=model_text(model))


# a machine that reports a name several models report (wrong_model_message)
SHARED_MODEL_MESSAGE = N_('This machine reports itself as "{model}", a name that more than one '
                          "model uses, so OpenCutter cannot tell which model it is and will not "
                          "send anything to it.")


# --- motion writes ----------------------------------------------------------------------------
def lowers_tool(data: bytes, profile: MachineProfile = CAMEO4) -> bool:
    """True if a write can bring a tool down: one of the profile's lowering prefixes, D (tool
    down) and TF (AutoBlade depth setting, which cycles the blade down and up at the AutoBlade
    station) on every model, appears anywhere in it. The firmware skips filler bytes before a
    command, and a parser can start a new command at a command letter without an ETX before it
    (firmware analysis of the Portrait 4: FX10,TJ3 runs TJ3), so where a command starts is not
    trusted here. No other command OpenCutter writes holds a D or a TF, so every job and
    program write gets the same answer as from the commands' first letters."""
    return any(prefix in data for prefix in profile.lowering_prefixes)


# --- the units check after the header --------------------------------------------------------
# The U reply: two whole numbers (no sign, no leading zero, at most 9 digits), a comma between,
# padded with ASCII spaces ('  6100,  6100' on the CAMEO 4). Anything else is not a U reply.
_U_REPLY = re.compile(r"\s*(0|[1-9][0-9]{0,8})\s*,\s*(0|[1-9][0-9]{0,8})\s*", re.ASCII)
_ASCII_DIGIT = re.compile(r"[0-9]", re.ASCII)
_UNIT_SCALES = {0.5: "0.1 mm", 2.0: "0.025 mm", 5.0: "0.01 mm"}   # reply / expected -> unit
UNITS_VERDICTS = ("ok", "mismatch", "unknown", "unchecked")
DEFAULT_UNITS_MAT = CAMEO4.mat(CAMEO4.default_mat)                 # the 12 x 12 in mat, TG1


def evaluate_units(reply: object,
                   expected: Mat | tuple[int, int] | None = DEFAULT_UNITS_MAT,
                   enforced: bool | None = None) -> tuple[str, str]:
    """Judge the U reply right after the header (TG<n> on a freshly loaded mat).

    `expected` is the mat in use (its expected_u and u_basis decide), or what a plan carries:
    the exact (a, b) pair, or None when no pair is known, together with `enforced`
    (Plan.units_enforced: evaluate_units(reply, plan.units_expected, plan.units_enforced)).
    The pair is compared exactly and in order: a = feed, then b = carriage (the 12 x 12 in mat
    on a CAMEO 4: 6100,6100; a 12 x 24 in mat: 12200,6100).
    `enforced` left out: True for a pair or None, so None alone never passes (fail closed);
    for a Mat, Mat.enforce_u (KNOWN and INFERRED). False: the reply is not enforced, for a mat
    whose U reply is UNKNOWN. A Mat with a known reply is enforced whatever `enforced` says.
    Returns (verdict, message) with verdict
      'ok'         the reply is the expected pair: 0.05 mm per unit and the mat's window;
      'mismatch'   a different pair, numbers that are not a U reply, or an enforced check with
                   no pair to compare: refused before the cut;
      'unknown'    a reply without numbers (or not text): the unit is not confirmed;
      'unchecked'  a U reply that is not enforced (the mat's U reply is UNKNOWN): logged and
                   shown, never enforced, and the cut area could not be checked on this model.
    Whether such a mat may be used at all is the job validator's rule (Mat.offered). Raises
    ValueError for an `expected` or `enforced` of the wrong shape, and for a Mat whose U reply
    is enforced but not a pair of positive ints."""
    pair, label, tg, basis, enforce = _expectation(expected, enforced)
    if not isinstance(reply, str) or not _ASCII_DIGIT.search(reply):
        return "unknown", f"U reply without numbers: {reply!r}"
    shown = reply.strip()
    report = _report_hint(basis)
    m = _U_REPLY.fullmatch(reply)
    reply_text = repr(shown)
    if m is None:
        return "mismatch", N_("U = {reply} is not a unit reply (two numbers and a comma). "
                              "Stopping before anything is drawn.{report}").format(
            reply=reply_text, report=report)
    got = (int(m[1]), int(m[2]))
    if not enforce:
        return "unchecked", N_("U = {reply}: not checked. The reply the {mat} gives on this model "
                               "is not known yet, so the cut area could not be checked.").format(
            reply=reply_text, mat=label)
    if pair is None:
        return "mismatch", N_("U = {reply}: no reply is known for the {mat}, so the unit cannot "
                              "be confirmed. Stopping before anything is drawn.").format(
            reply=reply_text, mat=label)
    if got == pair:
        return "ok", N_("U = {reply}: 0.05 mm per unit, confirmed").format(reply=reply_text)
    ratios = {g / p for g, p in zip(got, pair)}
    size = _UNIT_SCALES.get(ratios.pop()) if len(ratios) == 1 else None
    if size:
        return "mismatch", N_("U = {reply}: the unit looks like {size}, not 0.05 mm. Stopping "
                              "before anything is drawn.{report}").format(
            reply=reply_text, size=size, report=report)
    tg_text = f"TG{tg}" if tg is not None else "TG"
    return "mismatch", N_("U = {reply}, not {a},{b}: the mat type ({tg}) probably did not take "
                          "effect because the {mat} was not loaded right before the job. Unload "
                          "and Load it on the machine, then retry.{report}").format(
        reply=reply_text, a=pair[0], b=pair[1], tg=tg_text, mat=label, report=report)


def _expectation(expected: object, enforced: object
                 ) -> tuple[tuple[int, int] | None, str, int | None, UBasis, bool]:
    """(pair or None, mat words, TG or None, basis, enforce) for evaluate_units. Raises
    ValueError for anything that is not a Mat, a pair of positive ints or None, for `enforced`
    other than None or a bool, and for an enforced Mat without a valid pair."""
    if enforced is not None and type(enforced) is not bool:
        raise ValueError(f"enforced must be a bool or None, not {enforced!r}")
    if isinstance(expected, Mat):
        enforce = expected.enforce_u or enforced is True
        pair = expected.expected_u if expected.enforce_u else None
        if expected.enforce_u and not _is_pair(pair):
            raise ValueError(f"the {expected.label} mat has no valid expected U: {pair!r}")
        return (pair, N_("{size} mat").format(size=expected.label), expected.tg,
                expected.u_basis, enforce)
    enforce = enforced is not False
    if expected is None:
        return None, N_("mat"), None, UBasis.UNKNOWN, enforce
    if _is_pair(expected):
        return expected, N_("mat"), None, UBasis.KNOWN, enforce   # type: ignore[return-value]
    raise ValueError(f"expected must be a Mat, an (a, b) pair or None, not {expected!r}")


def _is_pair(v: object) -> bool:
    return type(v) is tuple and len(v) == 2 and all(type(x) is int and x > 0 for x in v)


def _report_hint(basis: UBasis) -> str:
    if basis is UBasis.INFERRED:
        # Translators: added after a sentence: keep the space at the start.
        return N_(" The expected reply comes from the firmware analysis and was not measured on "
                  "this model yet: if the mat was loaded right before the job, please report "
                  "this reply.")
    # Translators: added after a sentence: keep the space at the start.
    return N_(" If this happens again with the mat loaded right before the job, please report "
              "it.")


# --- runtime paths ------------------------------------------------------------------------------
def runtime_path(name: str) -> str | None:
    """Path of `name` directly in $XDG_RUNTIME_DIR, or None when it is not set (never /tmp).
    Only the sockets and locks of older daemons live there; see runtime_dir()."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    return os.path.join(base, name) if base else None


_APP_ID = re.compile(r"[A-Za-z_][\w-]*(\.[A-Za-z_][\w-]*)+", re.ASCII)
FLATPAK_INFO = "/.flatpak-info"     # exists only inside a Flatpak sandbox


def in_flatpak() -> bool:
    """True inside a Flatpak sandbox (FLATPAK_INFO exists). There, the app starts the service
    through the Flatpak portal (client.service_command), and pids of other sandboxes are not
    visible."""
    return os.path.exists(FLATPAK_INFO)


def flatpak_app_id() -> str | None:
    """$FLATPAK_ID inside a Flatpak sandbox (/.flatpak-info exists) when it is a valid
    application id, else None (also for a $FLATPAK_ID that leaked into a host command)."""
    flatpak = (os.environ.get("FLATPAK_ID") or "") if in_flatpak() else ""
    return flatpak if _APP_ID.fullmatch(flatpak) else None


def runtime_dir() -> str | None:
    """The service's runtime directory: $XDG_RUNTIME_DIR/app/<id>, with <id> = $FLATPAK_ID
    inside a Flatpak sandbox (when /.flatpak-info exists and it is a valid application id),
    else opencutter.APP_ID. None without $XDG_RUNTIME_DIR (never /tmp).

    The instances of one Flatpak app share their $XDG_RUNTIME_DIR (Flatpak >= 1.11), but it is
    not the host's: only $XDG_RUNTIME_DIR/app/$FLATPAK_ID is the host's same path. So this one
    directory is the same for every instance of the app, for its command line and for an
    installation outside Flatpak: they all find one service. A $FLATPAK_ID outside a sandbox
    (one that leaked into a host command's environment) is ignored, so that every host process
    uses the same directory."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        return None
    return os.path.join(base, "app", flatpak_app_id() or APP_ID)


def service_path(name: str) -> str | None:
    """Path of `name` (SOCKET_NAME, LOCK_NAME) in runtime_dir(), or None."""
    base = runtime_dir()
    return os.path.join(base, name) if base else None
