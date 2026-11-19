# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The OpenCutter daemon: holds the BLE link and runs one session at a time.

Run it once (`opencutter serve`, or started by the app) and leave it running: it holds the
link and reconnects by itself after a power cycle. Clients talk to it over a Unix socket with
JSON lines (see ARCHITECTURE.md, "Socket protocol"). Clients never send bytes: a session is a
pinned program name or a JobSpec, which the daemon turns into a plan and validates itself.

It ends on SIGTERM, SIGHUP or SIGINT, on the shutdown op (`opencutter service stop`, Disconnect in
the app) and, with `--idle-exit SECONDS` (the app starts it with 900), after that long without
any request while no session is open. A clean exit releases the Bluetooth link at once; nothing
is ever sent to the machine for it (only a step that is still running gets the stop sequence).

Safety measures (each one exists because of a real problem, see ARCHITECTURE.md):
  * one daemon only: an exclusive flock on opencutter.lock in the private runtime directory
    $XDG_RUNTIME_DIR/app/<app id> (protocol.runtime_dir), one on $XDG_RUNTIME_DIR/opencutter.lock
    (where builds before 0.1.0 locked), and one on $XDG_RUNTIME_DIR/cameo4.lock, the lock of the
    legacy prototype daemon, so that no two of them can run together;
  * the runtime directory must be a real directory of this user that nobody else may enter;
  * the socket is created with mode 0600 (umask, no chmod window);
  * never join a link that BlueZ reports already connected (held elsewhere): not at startup,
    and not when reconnecting after a drop (unless `serve --force`);
  * the model gate: a new link is used only after FG, sent right after the first ESC 05, names
    a model and firmware line OpenCutter drives (machines.detect, verdict USE: the TESTED
    CAMEO 4 on firmware V5, or an UNTESTED profile whose FG reply has the firmware's exact
    form). From then on the link checks everything with that machine profile (link.profile),
    and a job or program runs only when it was made for it (JobSpec.machine, Program.profile_id);
    otherwise nothing is sent. Any other machine (a model OpenCutter only recognizes, an unknown
    one, an empty reply) gets nothing more: its link is released and it is skipped for the life
    of the service. `--force` does not skip the gate;
  * exact reply grammar, per profile: replies are never stripped, only the profile's exact
    ready digit ('0') means ready, and a status digit the profile does not list aborts a step;
    the ESC 15 reply is read with the profile's grammar and holders, and fails closed;
  * every step waits for ready first; every write that can lower a tool (D, or TF where a tool
    takes a Blade Depth) re-reads the tools;
  * a job goes on after the U check only when the unit is confirmed: where the mat's U reply is
    known (KNOWN or INFERRED) only that exact reply passes; where it is not known (UNKNOWN,
    offered on UNTESTED profiles only) the reply is logged and shown but not enforced. Pinned
    programs leave an unconfirmed unit to the operator at the terminal;
  * once a session's first write went out, a '2' (Load Media) status, as an indication or as
    an ESC 05 reply, means the mat was unloaded: TG1 is not repeated, so the origin no longer
    matches, and every later step is refused without sending anything;
  * once the session's first write went out, a Paused or Cancelling status (an indication or an
    ESC 05 reply; '3' and '4' on every model today) ends the job for good, even when a later
    ESC 05 reads ready: OpenCutter never resumes a paused machine (it never sends ESC 08), so
    the step in progress, or the next one before anything of it is sent, ends with the stop;
  * long steps go out in batches of at most the profile's batch size (1024 bytes on the
    CAMEO 4) with a ready wait after each batch, and the stop flag is checked between every
    chunk, in every wait (the ready waits, the waits before a step, the U check), and before a
    step reports ok;
  * "motion possible" (MotionSession.motion_possible) is set by every motion write and cleared
    only when the machine read ready at the end of a step and again right before the next one.
    While it is set, a failure (a lost link included), the end of the service and a refusal
    of the next step for the machine's status send the stop, not only while a write is in
    flight;
  * a stop that could not get through (the link was lost while motion was possible, or its
    writes failed) is owed to that machine: when the service links to the SAME address again
    and the model gate passed, it sends the stop once, before anything else may use the link;
  * the profile's stop, ESC 09 twice, on: client EOF or any byte mid-step, the stop op, SIGTERM,
    SIGHUP, SIGINT; further signals are ignored while that stop goes out.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import fcntl
import json
import math
import os
import re
import signal
import stat
import time
from dataclasses import dataclass, field
from typing import Any

from .. import __version__
from ..identity import API_VERSION, BUILD, identity, started_by
from ..machines import CAMEO4, Detection, MachineProfile, Mat, Verdict, detect, registry
from .ble import Cameo4, PlotterUnreachable, bluetooth_off_error, find_bonded_cameo
from .link import MIN_SESSION_WRITE_LIMIT
from .portal import announce_background, background_status
from .programs import build_program
from .protocol import (CANCELLING, ETX, INTERRUPTED_TERMS, LEGACY_LOCK_NAME, LOCK_NAME, PAUSED,
                       REASON_HELD_ELSEWHERE, REASON_NO_BLUETOOTH, REASON_NOT_PAIRED,
                       REASON_UNREACHABLE, REASON_WRONG_MODEL, REJECTED, SOCKET_NAME,
                       TESTED_MACHINE, UnsafeCommand, evaluate_units, in_flatpak, is_status,
                       lowers_tool, model_text, not_ready_text, reported_model, runtime_path,
                       service_path, status_text, tool_name, wrong_model_message)

SOCKET_PATH = service_path(SOCKET_NAME)   # $XDG_RUNTIME_DIR/app/<app id>/opencutter.sock
LOCK_PATH = service_path(LOCK_NAME)
OLD_LOCK_PATH = runtime_path(LOCK_NAME)   # where builds before 0.1.0 locked: held as well

READY_TIMEOUT = 120.0      # base wait for ready after a write (Graphtec's safe_write uses 120 s)
PRE_STEP_WAIT = 10.0       # a previous step may still report '1' for a moment
# The CAMEO 4's pacing; a session uses its machine profile's (MachineProfile.batch_bytes and
# slowest_su_per_s), the same values on the CAMEO 4.
BATCH_BYTES = CAMEO4.batch_bytes            # 1024: at most this much is queued before a ready wait
SLOWEST_SU_PER_S = CAMEO4.slowest_su_per_s  # Speed 1 = 1 cm/s = 200 SU/s: bounds a batch's run
MAX_REQUEST = 64 << 20     # one JSON line; a JobSpec with 500 000 points fits easily
NOTHING_SENT = "nothing was sent"
NOT_CONNECTED = ("the machine is not connected yet: switch it off and on; "
                 "the daemon reconnects by itself")
MEDIA_CHANGED = ("The mat was unloaded during the job; nothing more was sent. "
                 "Load it and send the job again.")
MEDIA_CHANGED_PROGRAM = ("The mat was unloaded during the program; nothing more was sent. "
                         "Load it and run the program again.")
# the same during a step: that step ends with the stop
MEDIA_CHANGED_STEP = ("The mat was unloaded during the job, so OpenCutter ended it with the stop "
                      "and sends nothing more. Load the mat, then send the job again.")
MEDIA_CHANGED_STEP_PROGRAM = ("The mat was unloaded during the program, so OpenCutter ended it "
                              "with the stop and sends nothing more. Load the mat, then run the "
                              "program again.")
UNITS_UNCONFIRMED = ("The machine did not confirm the unit (U). Unload and load the mat, "
                     "then send again.")
LINK_LOST = "the Bluetooth link was lost"
# a stop request (the stop op, a signal, the client going away) during a step
STOPPED_IN_STEP = ("stopped on request (Cancel in the app, or `opencutter stop`); nothing more "
                   "was sent")
# ... and before a step: what to do next
STOPPED_BEFORE_STEP = ("A stop request (Cancel in the app, or `opencutter stop` from a terminal) "
                       "stopped the machine before this step; nothing more was sent. Press "
                       "Unload on the machine, check that the tool is up, then load the mat and "
                       "{again}.")
STOPPED_IN_UNITS = ("A stop request (Cancel in the app, or `opencutter stop` from a terminal) "
                    "came during the unit check (U); nothing more was sent. Press Unload on the "
                    "machine, then load the mat and {again}.")
# After the plain-words reason of a model OpenCutter recognizes but does not drive (the ping's
# "refused_reason", machines.detect's Detection.message): what that means for the machine.
RECOGNIZED_REFUSAL = "Nothing more is sent to it."
# What a ping with "machine": true adds (_machine_fields). The plain ping keeps its keys, which
# clients and tests compare as a whole; a service from before machine profiles ignores the flag.
PING_MACHINE_KEYS = ("profile", "support", "refused_reason")
# What a ping with "stop": true adds (_stop_fields), with or without "machine": true:
#   stop_owed        a stop could not get through to a machine (its link was lost while motion
#                    was possible, or the stop's writes failed); it is sent once that machine is
#                    linked again and passed the model gate. True until a stop gets through.
#   owed_stops_sent  how many owed stops this service sent on a new link (a client may tell the
#                    user when it goes up)
#   motion_stopped   the open session got a stop request (Cancel, `opencutter stop`, a signal):
#                    its next step is refused
#   interrupted      the Paused or Cancelling status the open session saw after its first
#                    write (a status digit), or None: the session ends with the stop
#   load_seen        how often the machine's status changed to the load digit ('2', Load Media,
#                    on every model today) since the service started: an unload on the panel
#                    shows as a change of this count, even between two pings
PING_STOP_KEYS = ("stop_owed", "owed_stops_sent", "motion_stopped", "interrupted", "load_seen")
LEGACY_DAEMON = "the legacy prototype daemon holds cameo4.lock; stop it first"
OLDER_DAEMON = ("an older OpenCutter background service is running; stop it with "
                "`opencutter service stop`")
STOPPING = "the background service is stopping"
SESSION_RUNNING = ("a program or job is running; cancel it first (in the app, or with "
                   "`opencutter stop`)")
OTHER_BUILD = ("this background service is {service}, but the app or command is {client}: "
               "nothing was sent. Run `opencutter service stop`, then try again")
HELD_ELSEWHERE_WAIT = 3.0  # seconds between looks at a link that another program holds
RETRY_WAIT = 3.0           # seconds before the next look after a failed look, connect or gate
WRONG_MODEL_WAIT = 60.0    # `serve --address` of a machine the model gate refused: just wait
LINK_RELEASE_WAIT = 5.0    # how long a clean exit waits for BlueZ to drop the link
STOP_CHECK = 3.0           # after the stop op's pair (when asked): how long ESC 05 is read back
STOP_CHECK_POLL = 0.4      # between those reads
STOP_LOCK_WAIT = 2.0       # how long each of those reads waits for the query lock
STOP_WATCH = 30.0          # after a stop, Ready and Busy changes are logged too, this long
FG_TIMEOUT_OWED = 10.0     # FG's reply time-out on a new link that is owed a stop (see serve)
STILL_WAITING_EVERY = 600.0  # while no link comes up: a 'still waiting' line this often
_MOVE = re.compile(rb"[MD](\d+),(\d+)")
_log_day: list[str] = []   # the day of the last log line (a dated line starts each new day)


def _log(msg: str) -> None:
    """Timestamped log line. Never raises: a hung-up terminal (EIO) must not stop a stop. When
    the day changes, a line with the new date comes first."""
    with contextlib.suppress(OSError, ValueError):
        day = datetime.date.today().isoformat()
        if _log_day[-1:] != [day]:
            if _log_day:                   # not before the very first line
                print(f"{time.strftime('%H:%M:%S')} --- {day} ---", flush=True)
            _log_day[:] = [day]
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class MotionAborted(RuntimeError):
    """A step had to end after motion started; the caller sends the stop sequence."""


class StepRefused(Exception):
    """A step is refused before its first write: nothing of it was sent. With `stop`, the
    refusal is about the machine's state after the session wrote (a stop request, Paused,
    Cancelling, Rejected, still Busy, an unknown status): the caller sends the stop as well
    (not forced: never a second pair after one got through)."""

    def __init__(self, text: str, stop: bool = False):
        super().__init__(text)
        self.text = text
        self.stop = stop


@dataclass(eq=False)
class MotionSession:
    """State of the one running program or job.

    plan is an opencutter.job.plan.Plan or a programs.Program: steps (kind, writes, title,
    expect, confirm), required_tools, validator and summary. profile is the machine profile of
    the link it runs on, the one the plan was made for: its status digits, tool grammar, batch
    size and pacing apply to every step."""
    name: str
    plan: Any
    allow_unrecognized: dict[int, bool]
    profile: MachineProfile = CAMEO4
    stop: asyncio.Event = field(default_factory=asyncio.Event)  # stop op, signal or lost client
    stop_sent: bool = False      # a stop sequence already went out in this session
    running: bool = False        # a motion write is in flight or being waited on
    wrote: bool = False          # the session's first motion write (the header) went out
    media_changed: bool = False  # '2' (Load Media) seen after that: nothing more may be sent
    # A Paused or Cancelling status (its digit) seen after the first write, without a stop of
    # ours before it: the machine was paused or cancelled on its panel. The job ends for good.
    interrupted: str | None = None
    # From each motion write until the machine read ready at the end of a step and again right
    # before the next one: the machine may still be moving (the loss, exit and refusal stops).
    motion_possible: bool = False
    link: Any = None             # the link the session runs on
    # called with (link, errors) after each stop sequence the session sent or joined
    on_stop: Any = None


def _what(sess: MotionSession) -> tuple[str, str]:
    """('job', 'send the job again') for a job, ('program', 'run the program again') else."""
    if sess.name == "job":
        return "job", "send the job again"
    return "program", "run the program again"


def _note_status(sess: MotionSession | None, status: str | None) -> None:
    """Record a status seen during a session (an indication or an exact ESC 05 reply).

    A '2' (Load Media, the profile's load digit) after the header went out means the mat was
    unloaded (StatusModel.unloaded_after_header, on every model OpenCutter drives); after a
    reload the origin no longer matches what TG1 set (PROTOCOL.md 7.2: a panel load sets
    a := 0, TG1 feeds a further 16.5 mm and sets a := 0 again, and TG1 is not repeated), so
    every later step is refused. A '2' before the header is harmless: the header's TG1 then
    follows a fresh load.

    A Paused or Cancelling status (the profile's terms; '3' and '4' on every model today)
    after the header, while no stop of ours was asked for, is latched in sess.interrupted:
    OpenCutter cannot resume a job, so the step in progress, or the next one, ends with the
    stop even when a later ESC 05 reads ready. A Cancelling after our own stop changes nothing.
    'Rejected' and other digits are not latched (the ready waits judge them)."""
    if sess is None or not sess.wrote or sess.media_changed:
        return
    states = sess.profile.status
    if status == states.load and states.unloaded_after_header:
        sess.media_changed = True
        _log(f"{sess.name}: the mat was unloaded (status {status}) during the session; "
             "nothing more will be sent")
        return
    term = status_text(status, sess.profile, default="")
    if term in INTERRUPTED_TERMS and sess.interrupted is None and not sess.stop.is_set():
        sess.interrupted = status
        what, _ = _what(sess)
        _log(f"{sess.name}: the machine reported {status} = {term} during the session; "
             f"OpenCutter cannot resume a {what}: it ends with the stop")


def _media_changed_error(sess: MotionSession) -> str:
    """The refusal for every step after the mat was unloaded mid-session."""
    if sess.name == "job":
        return MEDIA_CHANGED
    return MEDIA_CHANGED_PROGRAM


def _media_changed_step_error(sess: MotionSession) -> str:
    """Why a step ended with the stop when the mat was unloaded during it."""
    if sess.name == "job":
        return MEDIA_CHANGED_STEP
    return MEDIA_CHANGED_STEP_PROGRAM


def _status_stop_text(status: object, sess: MotionSession, before_step: bool = False) -> str:
    """Why the session ends with the stop for an ESC 05 status (or a latched indication) that
    is neither ready nor busy, or still busy before a step; in plain words where the profile's
    term says what happened (Paused, Cancelling, Rejected, Busy). `before_step`: the step was
    refused before its first write. A digit without such a term keeps "unexpected status 'N' =
    term" (during a step). Never advises pressing Pause to continue."""
    p = sess.profile
    what, again = _what(sess)
    term = status_text(status, p, default="")
    shown = f"status {status!r} = {status_text(status, p)}"
    nothing = " before this step; nothing of this step was sent" if before_step else ""
    unload = (f"press Unload on the machine (or switch it off), check that the tool is up, then "
              f"load the mat and {again}.")
    if term == PAUSED:
        return (f"The machine was paused ({shown}){nothing}. OpenCutter cannot resume a {what}, "
                f"so it ended the {what} with the stop. Do not press Pause to continue: "
                f"{unload}")
    if term == CANCELLING:
        return (f"The machine cancelled the {what} ({shown}){nothing}. OpenCutter cannot resume "
                f"a {what}, so it ended the {what} with the stop. Do not press Pause to "
                f"continue: {unload}")
    if term == REJECTED:
        return (f"The machine rejected a command ({shown}){nothing}: another connection, such "
                f"as USB, may hold it. OpenCutter ended the {what} with the stop. Close the "
                f"other connection, then {unload}")
    if status == p.status.busy:
        return (f"The machine still reported Busy {PRE_STEP_WAIT:g} s after the previous step "
                f"({shown}){nothing}. OpenCutter ended the {what} with the stop; if the machine "
                f"keeps moving, press its power button. Then {unload}")
    if before_step:
        return (f"unexpected status {status!r} = {status_text(status, p)} before this step; "
                f"nothing of this step was sent, and OpenCutter ended the {what} with the stop")
    return f"unexpected status {status!r} = {status_text(status, p)}"


def _check_session(c, sess: MotionSession, link: bool = True, media: bool = True) -> None:
    """Raise MotionAborted when the step in progress must end: a stop request, (with `media`)
    an unloaded mat, a Paused or Cancelling status seen since the first write, or (with `link`)
    a lost link. The ready waits leave an unloaded mat to the status they read (a mat unloaded
    and loaded again during a step's last batch refuses the next step, without a stop)."""
    if sess.stop.is_set():
        raise MotionAborted(STOPPED_IN_STEP)
    if media and sess.media_changed:
        raise MotionAborted(_media_changed_step_error(sess))
    if sess.interrupted is not None:
        raise MotionAborted(_status_stop_text(sess.interrupted, sess))
    if link and not _link_up(c):
        raise MotionAborted(LINK_LOST)


def _link_up(c) -> bool:
    """The link's is_connected (True for a test double without one)."""
    return bool(getattr(c, "is_connected", True))


def _latch_from_link(c, sess: MotionSession) -> None:
    """Take a Paused or Cancelling status the link saw last (an indication the session's own
    callback may have missed) into the session's latch (_note_status)."""
    last = getattr(c, "last_status", None)
    if status_text(last, sess.profile, default="") in INTERRUPTED_TERMS:
        _note_status(sess, last)


# --- plans -----------------------------------------------------------------------------------
def build_job(spec: dict) -> tuple[Any, dict[int, bool]]:
    """Validate a JobSpec dict and build its plan for the machine it names (JobSpec.machine);
    returns (plan, allow_unrecognized_tool). The caller checks that this is the connected
    machine (_open_request).

    Raises on anything invalid (the job package validates every range and every point)."""
    from opencutter.job.plan import build_plan
    from opencutter.job.spec import JobSpec
    job = JobSpec.from_dict(spec)
    allow = {int(h): ok is True for h, ok in job.allow_unrecognized_tool.items()}
    return build_plan(job), allow


def _open_request(req: dict, profile: MachineProfile) -> MotionSession:
    """Resolve a session request (a program name or a JobSpec) to a session on the machine with
    `profile` (the link's, from its FG reply). Raises if refused, before anything is sent:
    a program must be pinned for that profile (programs.build_program; Program.profile_id), and
    a job must have been made for it (JobSpec.machine, checked before the plan is built, and
    the plan's profile_id after)."""
    if req.get("op") == "motion":
        prog = build_program(req.get("program"), req.get("options") or {}, profile)
        _check_machine(prog.profile_id, profile, "program")
        return MotionSession(prog.name, prog, prog.allow_unrecognized, profile=profile)
    spec = req.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec must be a JSON object")
    from opencutter.job.spec import DEFAULT_MACHINE
    _check_machine(spec.get("machine", DEFAULT_MACHINE), profile, "job")
    plan, allow = build_job(spec)
    # a plan object without profile_id (made before machine profiles) is a CAMEO 4 plan, as the
    # default of job.plan.Plan says
    _check_machine(getattr(plan, "profile_id", CAMEO4.id), profile, "job")
    return MotionSession("job", plan, allow, profile=profile)


def _check_machine(machine: object, profile: MachineProfile, what: str) -> None:
    """Raise ValueError unless `machine` (a JobSpec's machine, a plan's or a program's
    profile_id) is the id of `profile`, the connected machine's."""
    if type(machine) is not str:
        raise ValueError(f"the {what} does not name a machine")
    if machine != profile.id:
        entry = registry.by_id(machine)
        made_for = (f"the {entry.name}" if entry is not None
                    else f"an unknown machine {machine[:40]!r}")
        raise ValueError(f"this {what} was made for {made_for}, but the machine is the "
                         f"{profile.name}")


def _motion_writes(plan) -> list[bytes]:
    return [w for s in plan.steps if s.kind == "write" for w in s.writes]


def check_plan(plan) -> None:
    """Refuse a malformed plan, or one with any write its own validator rejects, before
    anything is sent. Then the job package's whole-plan sequence check: every D must start
    from an M or D in the same step, inside the job's media box (a pinned program, which has
    no media box, gets that predecessor rule alone). The per-write validator cannot check this
    because it sees one write at a time."""
    if not plan.steps:
        raise ValueError("the plan has no steps")
    for step in plan.steps:
        if step.kind == "units":
            continue
        if step.kind != "write" or not step.writes:
            raise ValueError(f"bad step {step.title!r}")
        for w in step.writes:
            if not isinstance(w, bytes):
                raise ValueError(f"step {step.title!r} has a write that is not bytes")
            plan.validator(w)
    from opencutter.job import validate
    validate.check_plan_sequence(plan)     # always: a missing check fails closed (refused)


def tool_problem(required: dict[int, int], allow: dict[int, bool], tools: str | None,
                 profile: MachineProfile = CAMEO4) -> str | None:
    """Why no tool may be lowered now, or None. tools = the exact ESC 15 reply ' t1, t2', read
    with the profile's grammar and reply slots (MachineProfile.holder_codes: one-digit codes on
    the CAMEO 4, up to two on the CAMEO 5 generation; on a one-holder model the unused slot must
    read 0). Anything else fails closed, and the message shows the raw reply.

    Each holder in `required` must report its code; code 0 passes only where `allow` says so."""
    codes = profile.holder_codes(tools)
    if codes is None:
        return f"unclear tool reply (ESC 15): {tools!r}"
    if not required:
        return "this program declares no tool, so nothing may be lowered"
    for holder, need in sorted(required.items()):
        h = profile.holder(holder)
        if h is None:
            return f"unknown tool holder {holder!r}"
        have = codes[h.number]
        if have == need or (have == 0 and allow.get(holder) is True):
            continue
        hint = (" If the tool is in place but not detected (code 0), allow an undetected tool."
                if have == 0 else "")
        where = f" in {h.label}" if len(profile.holders) > 1 else ""
        return (f"{h.label}: {tool_name(holder, have, profile)} (code {have}); this needs "
                f"{tool_name(holder, need, profile)} (code {need}){where}.{hint}")
    return None


def precheck(plan, allow: dict[int, bool], status: str, tools: str,
             profile: MachineProfile = CAMEO4) -> str | None:
    """Why the session must not start, or None. status is the exact ESC 05 reply, which must be
    the profile's ready digit (protocol.not_ready_text says in plain words what to do for any
    other). A plan with any write that can lower a tool (the profile's lowering commands) needs
    its declared tools."""
    if status != profile.status.ready:
        return not_ready_text(status, profile)
    if any(lowers_tool(w, profile) for w in _motion_writes(plan)):
        return tool_problem(plan.required_tools, allow, tools, profile)
    return None


def _batches(writes, limit: int = BATCH_BYTES) -> list[list[bytes]]:
    """Group consecutive writes into batches of at most `limit` bytes."""
    out: list[list[bytes]] = []
    cur: list[bytes] = []
    size = 0
    for w in writes:
        if cur and size + len(w) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(w)
        size += len(w)
    if cur:
        out.append(cur)
    return out


def _travel(writes, start: tuple[int, int]) -> tuple[float, tuple[int, int]]:
    """Length in SU of the M/D moves in `writes` from `start`, and the end point."""
    pos, total = start, 0.0
    for w in writes:
        for cmd in w.split(bytes([ETX])):
            m = _MOVE.fullmatch(cmd)
            if m:
                nxt = (int(m[1]), int(m[2]))
                total += math.dist(pos, nxt)
                pos = nxt
    return total, pos


# --- step machinery ---------------------------------------------------------------------------
async def _esc_retry(c: Cameo4, lock: asyncio.Lock, code: int,
                     profile: MachineProfile = CAMEO4) -> str:
    """Read-only ESC query, retried once on a timeout or, for ESC 05, on a reply that is not
    exactly one of the profile's status replies. After a query whose reply never came (e.g. a
    silent U) the reply bookkeeping can drop or misplace one reply before it resyncs. Replies
    are never stripped."""
    reply = ""
    for attempt in (1, 2):
        try:
            async with lock:
                reply = await c.esc_query(code)
        except TimeoutError:
            if attempt == 2:
                raise
            continue
        if code != 0x05 or is_status(reply, profile):
            return reply
    return reply              # still not a status digit: the caller refuses it


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


async def wait_ready(c: Cameo4, lock: asyncio.Lock, sess: MotionSession,
                     timeout: float = READY_TIMEOUT) -> str:
    """Poll ESC 05 until the profile's ready digit ('0'). Raise MotionAborted for anything but
    ready or busy ('1') (the load digit with the plain words of an unloaded mat), and for a stop
    request or a Paused or Cancelling status (_note_status) seen before a poll or while it was
    out, even when its reply is ready; also for a lost link, three silent polls in a row or the
    timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    misses = 0
    stop = sess.stop
    states = sess.profile.status
    await _sleep_or_stop(stop, 0.5)          # do not read a stale '0' before motion starts
    while True:
        _check_session(c, sess, media=False)
        try:
            async with lock:
                s = await c.esc_query(0x05)       # exact: only ready and busy are accepted below
            misses = 0
        except TimeoutError:
            misses += 1
            if misses >= 3:
                raise MotionAborted("the machine no longer answers ESC 05") from None
            continue
        _note_status(sess, s)
        _check_session(c, sess, link=False, media=False)   # what came while the poll was out
        if s == states.ready:
            return s
        if s != states.busy:
            if sess.media_changed:
                raise MotionAborted(_media_changed_step_error(sess))
            raise MotionAborted(_status_stop_text(s, sess))
        if loop.time() > deadline:
            raise MotionAborted(f"still Busy after {timeout:.0f} s")
        await _sleep_or_stop(stop, 0.4)


async def _pre_step_status(c: Cameo4, lock: asyncio.Lock, sess: MotionSession) -> str:
    """Status right before a step; a brief busy ('1') left over from the previous step is
    waited out (not after a stop request, an unload or a pause). A ready reply here is the
    second one since the previous step's motion (its last ready wait was the first): motion is
    no longer possible until the next write (sess.motion_possible)."""
    loop = asyncio.get_running_loop()
    s = await _esc_retry(c, lock, 0x05, sess.profile)
    _note_status(sess, s)
    settle = loop.time() + PRE_STEP_WAIT
    while (s == sess.profile.status.busy and loop.time() < settle and not sess.stop.is_set()
           and sess.interrupted is None and not sess.media_changed):
        await _sleep_or_stop(sess.stop, 0.4)
        s = await _esc_retry(c, lock, 0x05, sess.profile)
        _note_status(sess, s)
    if s == sess.profile.status.ready and sess.interrupted is None and not sess.stop.is_set():
        sess.motion_possible = False
    return s


def _stop_pairs(c) -> int:
    """How many stop sequences the link sent (SafeLink.stop_sequences)."""
    n = getattr(c, "stop_sequences", 0)
    return n if type(n) is int else 0


async def _stop_session(c: Cameo4, sess: MotionSession, force: bool = False) -> list[str]:
    """Stop the session's motion. Automatic paths skip it if a stop already went out
    successfully; an explicit stop (force) always sends (or joins one in flight). The log says
    which: a pair sent (and its number on this link), a pair in flight joined, or none sent
    because one already got through. sess.on_stop hears the result of every pair sent or
    joined."""
    sess.stop.set()
    if sess.stop_sent and not force:
        _log(f"{sess.name}: STOP already sent (pair {_stop_pairs(c)} on this link); not sent "
             "again")
        return []
    joined = bool(getattr(c, "stop_in_flight", False))
    errors = await c.emergency_stop()
    if not errors:
        sess.stop_sent = True            # a failed sequence stays retryable
    result = f"FAILED: {errors}" if errors else "sent"
    if joined:
        _log(f"{sess.name}: joined the STOP in flight (pair {_stop_pairs(c)} on this link): "
             f"{result}")
    else:
        _log(f"{sess.name}: STOP (ESC 09 x2) {result}, pair {_stop_pairs(c)} on this link")
    if sess.on_stop is not None:
        with contextlib.suppress(Exception):
            sess.on_stop(c, errors)
    return errors


def _units_expectation(plan, profile: MachineProfile) -> tuple[Mat | tuple[int, int] | None,
                                                               bool | None]:
    """What the U reply is judged against (evaluate_units' `expected` and `enforced`): the
    plan's expected pair and whether it is enforced (Plan.units_expected, units_enforced). When
    exactly one of the profile's mats has that reply and basis (the plan's own mat, when its
    summary names one), the Mat itself, so that the messages name its TG and say where the
    expected reply comes from. Only such a mat, offered at the profile's level (Mat.offered: a
    mat with an UNKNOWN U reply only on an UNTESTED profile, as the job validator says), can
    make the reply not enforced: without one the pair is enforced (fail closed). A plan object
    without units_expected (made before machine profiles) gets the profile's default mat."""
    if not hasattr(plan, "units_expected"):
        return profile.mat(profile.default_mat), None
    pair, enforced = plan.units_expected, getattr(plan, "units_enforced", True)
    summary = getattr(plan, "summary", None)
    named = summary.get("mat") if isinstance(summary, dict) else None
    mats = [m for m in profile.mats if (named is None or m.key == named)
            and m.expected_u == pair and m.enforce_u is enforced
            and m.offered(profile.support)]
    if len(mats) == 1:
        return mats[0], None
    return pair, True


async def _units_step(c: Cameo4, lock: asyncio.Lock, sess: MotionSession) -> dict:
    """The U check after the header, against the mat's expected reply (_units_expectation). A
    mismatch is always a refusal. An unconfirmed unit (no reply, or a reply without numbers) is
    a refusal for a job, which no operator at a terminal confirms; a pinned program returns it
    as ok with verdict 'unknown', and the CLI asks the operator to type "yes" before it goes on.
    A mat whose U reply is not known (UNKNOWN, on an UNTESTED profile) gives the verdict
    'unchecked' for any U-shaped reply: logged, returned with the message that the cut area could
    not be checked on this model, and never a refusal.

    Nothing but U is sent here. A Paused or Cancelling status seen before or during it, or a
    stop request during it, refuses the step with StepRefused(stop=True) (the caller sends the
    stop, which an idle machine takes harmlessly); a lost link raises MotionAborted. Neither is
    named a mat problem."""
    expected, enforced = _units_expectation(sess.plan, sess.profile)
    try:
        async with lock:
            reply = await c.query("U")
    except TimeoutError:
        verdict, reply, msg = "unknown", None, "no reply to U; the unit is not confirmed"
    else:
        verdict, msg = evaluate_units(reply, expected, enforced)
    _log(f"{sess.name}: U -> {reply!r} ({verdict})")
    if not _link_up(c):
        raise MotionAborted(LINK_LOST)
    if sess.stop.is_set():
        raise StepRefused(STOPPED_IN_UNITS.format(again=_what(sess)[1]), stop=True)
    _latch_from_link(c, sess)
    if sess.interrupted is not None:
        raise StepRefused(_status_stop_text(sess.interrupted, sess), stop=True)
    if sess.media_changed:
        return {"ok": False, "verdict": verdict, "reply": reply,
                "error": _media_changed_error(sess)}
    if verdict == "mismatch":
        return {"ok": False, "verdict": verdict, "reply": reply, "error": msg}
    if verdict not in ("ok", "unchecked") and sess.name == "job":
        _log(f"{sess.name}: refused after the U check: {msg}")
        return {"ok": False, "verdict": verdict, "reply": reply, "error": UNITS_UNCONFIRMED}
    return {"ok": True, "verdict": verdict, "reply": reply, "message": msg}


def _nothing_sent(text: str) -> str:
    """`text`, saying that nothing was sent when it does not say so already."""
    if NOTHING_SENT in text.lower():
        return text
    return f"{text} Nothing was sent."


def _refuse_before_first_write(c: Cameo4, sess: MotionSession, status: str,
                               tools: str | None) -> None:
    """Raise StepRefused when the step must not start (nothing of it is sent). Refusals about
    the machine's state after the session wrote (a stop request, Paused, Cancelling, Rejected,
    still Busy, an unknown status) ask for the stop too; an unloaded mat, a tool refusal and
    any refusal before the header do not."""
    p = sess.profile
    if sess.stop.is_set():
        raise StepRefused(STOPPED_BEFORE_STEP.format(again=_what(sess)[1]), stop=sess.wrote)
    if sess.media_changed:
        raise StepRefused(_media_changed_error(sess))
    if getattr(c, "profile", p) is not p:
        raise StepRefused("the machine on the link is not the one the session was opened for; "
                          f"{NOTHING_SENT}")
    if sess.interrupted is not None:
        raise StepRefused(_status_stop_text(sess.interrupted, sess, before_step=True), stop=True)
    if status != p.status.ready:
        if sess.wrote:
            raise StepRefused(_status_stop_text(status, sess, before_step=True), stop=True)
        raise StepRefused(_nothing_sent(not_ready_text(status, p)))
    if tools is not None:
        problem = tool_problem(sess.plan.required_tools, sess.allow_unrecognized, tools, p)
        if problem:
            raise StepRefused(f"{problem}; {NOTHING_SENT}")


async def _write_chunk(c: Cameo4, lock: asyncio.Lock, sess: MotionSession, chunk: bytes,
                       status: str) -> None:
    """Write one chunk of the current step.

    Before the step's first write, a refusal raises StepRefused (nothing of the step was sent).
    After it, problems raise MotionAborted so that the caller sends the stop. Every motion write
    sets sess.motion_possible."""
    p = sess.profile
    tools = await _esc_retry(c, lock, 0x15, p) if lowers_tool(chunk, p) else None
    async with lock:
        if not sess.running:
            _refuse_before_first_write(c, sess, status, tools)
        else:
            _check_session(c, sess)
            if getattr(c, "profile", p) is not p:
                raise MotionAborted("the machine on the link is not the one the session was "
                                    "opened for")
            problem = (tool_problem(sess.plan.required_tools, sess.allow_unrecognized, tools, p)
                       if tools is not None else None)
            if problem:
                raise MotionAborted(problem)
        was_running, sess.running = sess.running, True
        was_possible, sess.motion_possible = sess.motion_possible, True
        sess.wrote = True                  # from here on a '2' status means the mat was unloaded
        try:
            await c.write_validated(chunk, sess.plan.validator)
        except ValueError:                 # refused by a check: this chunk never went out
            sess.running = was_running
            sess.motion_possible = was_possible
            raise
    if sess.stop.is_set():
        # A stop arrived while this write was in flight. On BlueZ it cannot have overtaken the
        # write: nothing yields between the stop check above and bleak queueing the WriteValue
        # on D-Bus, and BlueZ runs one WriteValue per characteristic at a time (InProgress,
        # which bleak retries every 10 ms). So the stop's ESC 09 pair goes out after this
        # chunk's Write Response, and this call normally joins that sequence (no extra pair).
        # It sends a pair of its own only when no sequence is in flight any more, e.g. when
        # that one failed or with a transport that lets a write overtake another.
        await _stop_session(c, sess, force=True)
        raise MotionAborted(STOPPED_IN_STEP)


async def _run_step(c: Cameo4, lock: asyncio.Lock, sess: MotionSession, step) -> dict:
    """Run one step; never raises, and never reports ok once a stop was asked for. A failure
    while motion is possible (sess.motion_possible: a lost link included) sends the stop
    sequence; so does a refusal for the machine's state after the session wrote. Every refusal
    is logged."""
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        if sess.stop.is_set():
            raise StepRefused(STOPPED_BEFORE_STEP.format(again=_what(sess)[1]), stop=sess.wrote)
        if sess.media_changed:             # checked before any query: nothing more is sent
            raise StepRefused(_media_changed_error(sess))
        if not _link_up(c):
            raise MotionAborted(LINK_LOST)
        _latch_from_link(c, sess)
        if sess.interrupted is not None:   # paused or cancelled since: before any query
            raise StepRefused(_status_stop_text(sess.interrupted, sess, before_step=True),
                              stop=True)
        if step.kind == "units":
            return {**await _units_step(c, lock, sess), "seconds": round(loop.time() - t0, 1)}
        _log(f"{sess.name}: {step.title!r}: {len(step.writes)} write(s), "
             f"{sum(map(len, step.writes))} B")
        s = await _pre_step_status(c, lock, sess)
        pos = (0, 0)
        for batch in _batches(step.writes, sess.profile.batch_bytes):
            for chunk in batch:
                await _write_chunk(c, lock, sess, chunk, s)
            travel, pos = _travel(batch, pos)
            s = await wait_ready(c, lock, sess,
                                 READY_TIMEOUT + travel / sess.profile.slowest_su_per_s)
        _check_session(c, sess, link=False, media=False)   # a stop or pause since: not ok
        return {"ok": True, "status": s, "seconds": round(loop.time() - t0, 1)}
    except asyncio.CancelledError:
        if sess.running or sess.motion_possible:
            await asyncio.shield(_stop_session(c, sess))
        raise
    except StepRefused as r:
        _log(f"{sess.name}: {step.title!r} refused before its first write: {r.text}")
        if r.stop and sess.wrote:
            errors = await _stop_session(c, sess)
            return {"ok": False, "error": r.text, "stopped": True, "stop_errors": errors}
        return {"ok": False, "error": r.text}
    except Exception as e:
        link_down = not _link_up(c)
        if link_down:                      # whatever the error text (bleak's varies)
            msg = LINK_LOST
        elif isinstance(e, MotionAborted):
            msg = str(e)
        elif isinstance(e, ValueError):
            msg = f"refused: {e}"
        elif isinstance(e, TimeoutError) and str(e):
            msg = str(e)
        else:
            msg = f"{type(e).__name__}: {e}"
        stopped = sess.running or sess.motion_possible
        idle = "" if sess.running or not stopped else " (between writes: the machine may be idle)"
        errors = await _stop_session(c, sess) if stopped else []     # the stop before any print
        _log(f"{sess.name}: step aborted: {msg}{idle}")
        return {"ok": False, "error": msg, "stopped": stopped, "stop_errors": errors}
    finally:
        sess.running = False


async def _session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, req: dict,
                   c: Cameo4, lock: asyncio.Lock, state: dict) -> None:
    """One program or job on one connection: open, then {"op":"step","index":i} in order, then
    {"op":"end"}. If the connection closes or sends anything while a step runs, stop."""
    async def send(obj: dict) -> None:
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()

    async def refuse(error: str, **more) -> None:
        """Refuse the session at open (nothing was sent), with a line in the log."""
        _log(f"{what}: refused at open: {error}")
        await send({"ok": False, "error": error, **more})

    what = str(req.get("program") or "job")[:40] if req.get("op") == "motion" else "job"
    if state.get("stopping"):
        await refuse(STOPPING)
        return
    if not _usable(c):                     # handle() checks it too: never a refused machine
        await refuse(_no_link_error(state))
        return
    if state.get("motion") is not None:
        await refuse("another program or job is already running")
        return
    problem = _client_build_problem(req)
    if problem:
        await refuse(problem)
        return
    profile = c.profile                    # the connected machine's, from its FG reply
    try:
        sess = _open_request(req, profile)
        check_plan(sess.plan)
    except Exception as e:
        await refuse(f"refused: {e}")
        return
    sess.link = c
    sess.on_stop = lambda link, errors: _stop_result(state, link, errors)
    state["motion"] = sess
    steps = sess.plan.steps
    try:
        status = await _esc_retry(c, lock, 0x05, profile)
        tools = await _esc_retry(c, lock, 0x15, profile)
        again = await _esc_retry(c, lock, 0x05, profile)   # the reply stream must be consistent
        problem = precheck(sess.plan, sess.allow_unrecognized, status, tools, profile)
        if problem is None and again != status:
            problem = f"inconsistent ESC 05 replies ({status!r}, then {again!r}); retry"
        longest = max(map(len, _motion_writes(sess.plan)), default=0)
        limit = await c.refresh_write_limit()
        # what the link sends: min(480, profile.max_write, limit), and nothing while the limit
        # (MTU - 3) is below MIN_SESSION_WRITE_LIMIT: refused here, before the first write
        motion = c.motion_limit()
        if problem is None and limit < MIN_SESSION_WRITE_LIMIT:
            problem = (f"Bluetooth MTU too small: the link takes writes of at most {limit} "
                       f"bytes, and a session needs {MIN_SESSION_WRITE_LIMIT}")
        elif problem is None and longest > motion and motion == limit:
            problem = (f"Bluetooth MTU too small: a write has {longest} bytes but the limit is "
                       f"{motion}; long writes are never sent")
        elif problem is None and longest > motion:    # the profile's (or the firmware's) cap
            problem = (f"a write has {longest} bytes, but the {profile.name} takes writes of at "
                       f"most {motion}; long writes are never sent")
        if problem:
            await refuse(problem, status=status, tools=tools)
            return
        _log(f"{sess.name}: opened (status {status}, tools {tools!r}, write limit {limit} B)")
        # tool_down: the step holds a write that can lower a tool (the profile's lowering
        # commands), for the client's words ("Cutting" only while a tool can be down)
        await send({"ok": True, "status": status, "tools": tools, "write_limit": limit,
                    "profile": profile.id,
                    "steps": [{"kind": s.kind, "title": s.title, "expect": s.expect,
                               "confirm": bool(s.confirm), "writes": len(s.writes),
                               "tool_down": s.kind == "write"
                               and any(lowers_tool(w, profile) for w in s.writes)}
                              for s in steps],
                    "summary": sess.plan.summary})
        index = 0
        while True:
            line = await reader.readline()
            _touch(state)
            if not line:
                _log(f"{sess.name}: client left after {index}/{len(steps)} steps")
                return
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if not isinstance(msg, dict):
                await send({"ok": False, "error": "bad request"})
                return
            if msg.get("op") == "end":
                _log(f"{sess.name}: ended after {index}/{len(steps)} steps")
                await send({"ok": True})
                return
            if msg.get("op") != "step" or msg.get("index") != index or index >= len(steps):
                await send({"ok": False, "error": f"expected step {index}; {NOTHING_SENT}"})
                return
            step_task = asyncio.create_task(_run_step(c, lock, sess, steps[index]))
            watch = asyncio.create_task(reader.read(1))
            try:
                done, _ = await asyncio.wait({step_task, watch}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:      # daemon shutting down mid-step
                step_task.cancel()             # _run_step sends the stop if motion was in flight
                with contextlib.suppress(BaseException):
                    await step_task
                raise
            finally:
                if not watch.done():
                    watch.cancel()
                    # let the cancelled read release the StreamReader before the next readline();
                    # asyncio.wait never raises watch's CancelledError but still propagates a
                    # cancel aimed at this task (daemon shutdown)
                    await asyncio.wait({watch})
            if step_task not in done:          # client vanished or broke protocol mid-step
                idle = (" (the unit check: the machine is idle after the header)"
                        if steps[index].kind == "units" else "")
                _log(f"{sess.name}: client gone during step {index}{idle} -> stop")
                await _stop_session(c, sess)
                with contextlib.suppress(Exception):
                    await step_task
                return
            result = step_task.result()
            if not result.get("ok"):
                result = {"stopped": False, "stop_errors": [], **result}
            await send(result)
            if not result.get("ok"):
                return
            index += 1
    finally:
        state["motion"] = None


def _touch(state: dict) -> None:
    """A client request arrived: the idle time starts again."""
    state["last_request"] = time.monotonic()


def _usable(c: Cameo4 | None) -> bool:
    """True for a link that is up and passed the model gate (Cameo4.machine is set, and so is
    its machine profile). Nothing is ever written to a machine through any other link."""
    return (c is not None and c.is_connected and bool(c.machine)
            and getattr(c, "profile", None) is not None)


def refused_message(model: object, reason: object = None) -> str:
    """Why nothing is sent to a machine the model gate refused: for a model OpenCutter
    recognizes but does not drive, its plain-words reason (Detection.message: "OpenCutter
    recognizes this CAMEO 3 but cannot drive it yet: ...") and RECOGNIZED_REFUSAL; for any other
    machine protocol.wrong_model_message(model), as before."""
    if isinstance(reason, str) and reason:
        return f"{reason} {RECOGNIZED_REFUSAL}"
    return wrong_model_message(model)


def _machine_fields(c: Cameo4 | None, state: dict, refused: bool) -> dict:
    """The ping's PING_MACHINE_KEYS. `c`: the usable link, or None.
      profile         the machine profile's id: the connected machine's, or a refused model's
                      that OpenCutter recognizes; None otherwise (also for a refused model it
                      does not know).
      support         "tested" or "untested" for the connected machine (the narrower of its
                      profile's and its firmware line's level), "recognized" for a refused
                      recognized model, None otherwise.
      refused_reason  why a recognized model is refused, in plain words (machines.detect's
                      Detection.message); None otherwise (the texts built from "model" apply)."""
    if c is not None:
        support = state.get("support") or c.profile.support.value
        return {"profile": c.profile.id, "support": support, "refused_reason": None}
    if refused:
        return {"profile": state.get("refused_profile"), "support": state.get("refused_support"),
                "refused_reason": state.get("refused_reason")}
    return dict.fromkeys(PING_MACHINE_KEYS)


def _address(c) -> str | None:
    """The link's machine address in upper case, or None."""
    address = getattr(getattr(c, "device", None), "address", None)
    return address.upper() if isinstance(address, str) else None


def _owe_stop(state: dict, c, why: str) -> None:
    """Remember that a stop could not get through to the machine of link `c`: serve() sends it
    once that same machine (address) is linked again and passed the model gate. Not when a newer
    usable link to it is already in use (it got any stop owed when it passed the gate). Each
    machine's owed stop is kept on its own (state["stop_owed"] is a set of addresses), so a stop
    owed to another machine never replaces it."""
    address = _address(c)
    if address is None:
        return
    current = state.get("c")
    if current is not None and current is not c and _address(current) == address:
        _log(f"the stop through the lost link to [{address}] did not get through, but the "
             "machine is linked again: if it moves, use Stop Machine or `opencutter stop`")
        return
    owed = state.setdefault("stop_owed", set())
    if address not in owed:
        name = getattr(getattr(c, "device", None), "name", None) or "the machine"
        _log(f"a stop is owed to {name} [{address}] ({why}); it is sent when the same machine "
             "is linked again and its FG check passed")
    owed.add(address)


def _stop_result(state: dict, c, errors: list[str]) -> None:
    """After a stop sequence through link `c`: a failed one is owed (_owe_stop); one that got
    through pays a stop owed to that machine. Either way the status changes are logged for a
    while (STOP_WATCH)."""
    state["last_stop"] = time.monotonic()
    address = _address(c)
    if errors:
        _owe_stop(state, c, "its stop did not get through")
    elif address is not None and address in state.get("stop_owed", ()):
        state["stop_owed"].discard(address)
        _log(f"the stop owed to [{address}] got through")


def _stop_fields(state: dict) -> dict:
    """The ping's PING_STOP_KEYS (see there)."""
    sess = state.get("motion")
    return {"stop_owed": bool(state.get("stop_owed")),
            "owed_stops_sent": int(state.get("owed_stops_sent") or 0),
            "motion_stopped": bool(sess is not None and sess.stop.is_set()),
            "interrupted": sess.interrupted if sess is not None else None,
            "load_seen": int(state.get("load_seen") or 0)}


async def _status_after_stop(c: Cameo4, lock: asyncio.Lock) -> str | None:
    """What ESC 05 reads after a stop: polled every STOP_CHECK_POLL s for up to STOP_CHECK s
    while it reads Busy or Cancelling (each read waits at most STOP_LOCK_WAIT s for the query
    lock). Returns the last exact status read, or None. Nothing but ESC 05 is sent. A ready
    reply does not prove that the machine stopped for good (motion still queued might resume),
    only that it reports Ready now. Never raises for a read that fails (a lost link, a busy
    write): the stop already got through, so the reading just ends there."""
    profile = c.profile
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STOP_CHECK
    last = None
    while True:
        if not _link_up(c):
            return last
        try:
            await asyncio.wait_for(lock.acquire(), STOP_LOCK_WAIT)
        except TimeoutError:
            return last
        try:
            reply = await c.esc_query(0x05)
        except TimeoutError:
            reply = None
        except Exception as e:  # noqa: BLE001 - the stop's reply must still say it got through
            _log(f"after the stop: ESC 05 failed ({type(e).__name__}: {e})")
            return last
        finally:
            lock.release()
        if is_status(reply, profile):
            last = reply
        busy = last is None or last == profile.status.busy or status_text(
            last, profile, default="") == CANCELLING
        if not busy or loop.time() + STOP_CHECK_POLL > deadline:
            return last
        await asyncio.sleep(STOP_CHECK_POLL)


async def _stop_op(req: dict, c: Cameo4, lock: asyncio.Lock, state: dict) -> dict:
    """The stop op through a usable link: with a session open its stop (forced: always sent, or
    joined while one is in flight), else the profile's stop on its own (a machine after a job,
    or one owed a stop). {"ok", "errors"}; with "check": true and a stop that got through also
    "status_after" (_status_after_stop: the last ESC 05 reply, or None) and "still_busy" (it
    still reads the busy digit; None when nothing was read)."""
    sess = state["motion"]
    if sess is not None:
        errors = await _stop_session(c, sess, force=True)
    else:
        joined = bool(getattr(c, "stop_in_flight", False))
        errors = await c.emergency_stop()
        result = f"FAILED: {errors}" if errors else "sent"
        _log(f"joined the STOP in flight (pair {_stop_pairs(c)} on this link): {result}"
             if joined else
             f"STOP (ESC 09 x2) {result}, pair {_stop_pairs(c)} on this link")
        _stop_result(state, c, errors)
    reply = {"ok": not errors, "errors": errors}
    if req.get("check") is True:
        status = None if errors else await _status_after_stop(c, lock)
        busy = None if status is None else status == c.profile.status.busy
        reply.update(status_after=status, still_busy=busy)
        if status is None:
            _log("after the stop: the status could not be read")
        else:
            _log(f"after the stop the machine reports {status} = {status_text(status, c.profile)}"
                 + (f" {STOP_CHECK:g} s later: if it moves, press its power button"
                    if busy else ""))
    return reply


def _no_link_error(state: dict) -> str:
    """The refusal of a session or an op without a usable link: the model message when the
    model gate refused the machine (refused_message), else NOT_CONNECTED."""
    if state.get("reason") == REASON_WRONG_MODEL:
        return refused_message(state.get("refused_model"), state.get("refused_reason"))
    return NOT_CONNECTED


def _client_build_problem(req: dict) -> str | None:
    """Why a session request from a client of another build is refused, or None. Clients send
    their "build" and "api" with every session request (an older client sends neither and is
    not checked here: the app and the command line check the ping before they open one)."""
    if "build" not in req and "api" not in req:
        return None
    if req.get("build") == BUILD and type(req.get("api")) is int and req.get("api") == API_VERSION:
        return None
    client = (f"build {str(req.get('build'))[:20]}" if req.get("api") == API_VERSION
              else f"build {str(req.get('build'))[:20]}, api {str(req.get('api'))[:10]}")
    return OTHER_BUILD.format(service=f"OpenCutter {__version__} (build {BUILD})", client=client)


# --- one-shot ops -----------------------------------------------------------------------------
async def _one_shot(req: dict, c: Cameo4 | None, lock: asyncio.Lock, state: dict) -> dict:
    """Answer ping, stop, esc, query and info."""
    op = req.get("op")
    sess = state["motion"]
    if op == "ping":
        connected = _usable(c)
        reason = None if connected else state.get("reason")
        refused = reason == REASON_WRONG_MODEL
        reply = {"ok": True, "connected": connected,
                 "status": c.last_status if c else None,
                 "motion": sess.name if sess else None,
                 "device": {"name": c.device.name, "address": c.device.address} if c else None,
                 "reason": reason,
                 # what the machine reports itself as (FG, protocol.model_text): the connected
                 # machine, or the one the model gate refused last; None otherwise
                 "model": (c.machine if connected else state.get("refused_model")
                           if refused else None),
                 "stopping": bool(state.get("stopping")),     # it is ending: do not use it
                 # pid: in this process's own PID namespace (its own sandbox, in a Flatpak);
                 # started_by: the run id of the app that started it (identity.RUN_ID), or None
                 **identity(), "pid": os.getpid(), "started_by": state.get("started_by")}
        if req.get("machine") is True:     # {"op": "ping", "machine": true}: PING_MACHINE_KEYS
            reply.update(_machine_fields(c if connected else None, state, refused))
        if req.get("stop") is True:        # {"op": "ping", "stop": true}: PING_STOP_KEYS
            reply.update(_stop_fields(state))
        return reply
    if not _usable(c):                     # no link, or not one that passed the model gate
        if op == "stop" and sess is not None:
            # nothing can be sent now: the session takes no more steps, and while motion was
            # possible its machine is owed the stop (sent when it is linked again)
            sess.stop.set()
            if sess.motion_possible and not sess.stop_sent:
                _owe_stop(state, sess.link, "a stop was asked for while the link was down")
        return {"ok": False, "error": _no_link_error(state)}
    if op == "stop":                       # never waits for the query lock; always sends
        return await _stop_op(req, c, lock, state)
    if sess is not None:
        return {"ok": False, "error": "a program or job is running (use stop to stop it)"}
    if op == "esc":
        async with lock:
            return {"ok": True, "reply": await c.esc_query(int(req["code"]))}
    if op == "query":
        async with lock:
            return {"ok": True, "reply": await c.query(str(req["cmd"]))}
    if op == "info":
        async with lock:
            status = await c.esc_query(0x05)
            tools = await c.esc_query(0x15)
            firmware = await c.query("FG")
        return {"ok": True, "status": status, "tools": tools, "firmware": firmware.strip()}
    return {"ok": False, "error": f"unknown op {op!r}"}


# --- startup guards ---------------------------------------------------------------------------
async def _socket_alive(path: str) -> bool:
    try:
        _r, w = await asyncio.open_unix_connection(path)
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    w.close()
    return True


def _private_dir(path: str) -> None:
    """Create the service's runtime directory (and its parent, $XDG_RUNTIME_DIR/app) with mode
    0700 if missing, then refuse to start unless it is a real directory (not a symbolic link)
    of this user that nobody else may enter. An existing path is never changed or removed."""
    for d in (os.path.dirname(path), path):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        except OSError as e:
            raise SystemExit(f"cannot create the runtime directory {d}: {e.strerror}") from None
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        problem = "is a symbolic link"
    elif not stat.S_ISDIR(info.st_mode):
        problem = "is not a directory"
    elif info.st_uid != os.getuid():
        problem = f"belongs to another user (uid {info.st_uid})"
    elif info.st_mode & 0o077:
        problem = f"is open to other users (mode {stat.S_IMODE(info.st_mode):o})"
    else:
        return
    raise SystemExit(f"the runtime directory {path} {problem}; refusing to start (fix or remove "
                     "it, then start again)")


def _take_lock(path: str, refusal: str) -> int:
    """An exclusive flock on `path` for the daemon's lifetime, or SystemExit with `refusal`."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit(f"{refusal} (lock {path} is held)") from None
    return fd


def _take_daemon_lock() -> int:
    """Exclusive lock held for the daemon's lifetime, independent of SOCKET_PATH, so that a
    second daemon (another socket path, a test harness) can never drive the machine next to
    this one."""
    return _take_lock(LOCK_PATH, "another OpenCutter daemon is running")


def _take_old_lock() -> int | None:
    """Hold $XDG_RUNTIME_DIR/opencutter.lock too, the lock of the builds before 0.1.0, which
    put their socket and lock directly in $XDG_RUNTIME_DIR: refuse to start while such a
    daemon runs (OLDER_DAEMON), and make it refuse to start while this one runs. None when it
    is the same file as LOCK_PATH (nothing more to hold)."""
    if not OLD_LOCK_PATH or os.path.abspath(OLD_LOCK_PATH) == os.path.abspath(LOCK_PATH):
        return None
    return _take_lock(OLD_LOCK_PATH, OLDER_DAEMON)


def legacy_lock_path() -> str:
    """The lock of the legacy prototype daemon (it locks $XDG_RUNTIME_DIR/cameo4.lock). It lies
    next to OLD_LOCK_PATH, so a private OLD_LOCK_PATH (a test harness) also gets a private
    legacy lock and never touches the user's."""
    return os.path.join(os.path.dirname(OLD_LOCK_PATH), LEGACY_LOCK_NAME)


def _take_legacy_lock() -> int:
    """Hold the legacy prototype daemon's lock for the daemon's lifetime: refuse to start while
    that daemon runs, and make it refuse to start while this one runs. Each of them locks only
    its own name otherwise, so both could end up driving the machine over one BLE link."""
    return _take_lock(legacy_lock_path(), LEGACY_DAEMON)


def _preload_job_package() -> None:
    """Import the job package (plan builder, validator) when the service starts, so that it
    runs one build from start to end: an update on disk later cannot mix newer modules into a
    running service (its ping reports the build it started with)."""
    from opencutter.job import plan, spec, validate  # noqa: F401


def _close_all(fds: list[int]) -> None:
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


def _connected_elsewhere(dev) -> bool:
    """True when BlueZ reports the device Connected. Whenever we hold no link of our own, that
    link belongs to another program (an older daemon, a test, another app)."""
    props = (getattr(dev, "details", None) or {}).get("props") or {}
    return bool(props.get("Connected"))


async def _refuse_if_held_elsewhere(address: str | None, force: bool) -> None:
    """At startup we hold no link, so a machine that BlueZ reports Connected is held by someone
    else (an older daemon, a test, another app): joining it would let two programs drive it.
    bluetoothd drops a dead client's link ~2 s after it exits, so wait a little first."""
    if force:
        return
    for _ in range(4):
        try:
            dev = await find_bonded_cameo(address)
        except PlotterUnreachable:
            return
        except Exception:  # noqa: BLE001 - BlueZ cannot be reached (yet): nothing to join now.
            return         # The main loop reports it and never joins a link held elsewhere.
        if not _connected_elsewhere(dev):
            return
        await asyncio.sleep(2)
    raise SystemExit("The machine is already connected by another program (an older daemon, a "
                     "test, the system settings?). Close it, or switch the machine off and on, "
                     "then retry. `serve --force` joins the existing link anyway.")


# --- daemon -----------------------------------------------------------------------------------
def _duration(seconds: float) -> str:
    """'15 minutes' for 900, '1 minute' for 60, '0.5 s' for 0.5."""
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} s"


async def _watch_idle(state: dict, seconds: float, finish) -> None:
    """End the service (`finish`) once no client request of any kind (a ping included) arrived
    for `seconds` and no session is open. The app pings every 2 s, so this happens only after
    the app was closed; the machine is then free for other programs."""
    while True:
        busy = state["sessions"] > 0 or state["motion"] is not None
        left = state["last_request"] + seconds - time.monotonic()
        if not busy and left <= 0:
            finish(f"no request for {_duration(seconds)} and no job: the background service "
                   "stops and releases the machine for other programs")
            return
        await asyncio.sleep(min(seconds, 5.0) if busy else left)


async def _release_link(c: Cameo4) -> None:
    """Drop our Bluetooth link on a clean exit, so the machine is free at once (bluetoothd would
    drop it about 2 s after this process ends anyway). Bounded; nothing is sent to the machine."""
    release = asyncio.ensure_future(c.disconnect())
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait({release}, timeout=LINK_RELEASE_WAIT)
    if not release.done():
        release.cancel()
        _log("the Bluetooth link was not released in time; the system drops it when this "
             "service has ended")
    elif release.exception() is not None:
        _log(f"releasing the Bluetooth link: {type(release.exception()).__name__}: "
             f"{release.exception()}")
    else:
        _log("Bluetooth link released")


async def _drop_link(c: Cameo4) -> None:
    """Release a link that is not used while the service runs on (the model gate refused the
    machine, or FG or the status read after it failed): BlueZ drops it at once. Nothing is
    written to the machine. Bounded; unlike _release_link, a cancel (shutdown, idle time,
    signal) goes through, and serve's own exit releases the link again."""
    try:
        await asyncio.wait_for(c.disconnect(), LINK_RELEASE_WAIT)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - the system drops it when this service ends anyway
        _log(f"releasing the Bluetooth link: {type(e).__name__}: {e}")


async def _next_machine(address: str | None, refused: set[str], tried: set[str]):
    """The paired machine to try next: never one the model gate refused, and one not tried in this
    round when there is one. When every paired one was tried (or there is only one), the round
    starts again. Only reads BlueZ's objects."""
    if tried:
        try:
            return await find_bonded_cameo(address, skip=refused | tried)
        except PlotterUnreachable:
            tried.clear()
    return await find_bonded_cameo(address, skip=refused)


def _late_status(reply: object) -> bool:
    """True when an FG reply is an exact ESC 05 reply of a model OpenCutter drives: the late
    answer of an earlier ESC 05, not a model."""
    return any(p.status.is_status(reply) for p in registry.profiles())


def _note_refusal(state: dict, det: Detection | None) -> None:
    """Record what the model gate refused, for the ping: for a model OpenCutter recognizes, its
    id, "recognized" and its plain-words reason (Detection.message); None for anything else."""
    recognized = det is not None and det.verdict is Verdict.RECOGNIZED and det.entry is not None
    state["refused_profile"] = det.entry.id if recognized else None
    state["refused_support"] = det.support.value if recognized else None
    state["refused_reason"] = det.message if recognized else None


async def _status_after_fg(c: Cameo4) -> str | None:
    """The status read right after a passing FG (the caller holds the query lock): FG claims
    the session (status 1 becomes 2 on the CAMEO 4; MachineProfile.fg_claims_session), so the
    first status clients see is right.

    This read is not part of the model check, so a timeout does not cost the link: the tested
    CAMEO 4 accepts a new link only after a power cycle. FG answered in time, so Cameo4's count
    drops the late reply of this read; the status indications keep last_status right."""
    try:
        return await c.esc_query(0x05)
    except TimeoutError as e:
        _log(f"no reply to the status read after FG ({e}); the status comes from the machine's "
             "status indications")
        return c.last_status


async def serve(address: str | None, verbose: bool, force: bool = False,
                idle_exit: float = 0) -> None:
    """Hold the BLE link and answer clients on SOCKET_PATH until cancelled or signalled, until a
    client asks it to shut down, or (`idle_exit` > 0) until no request came for that many
    seconds while no session is open. A requested or idle exit returns normally."""
    if not SOCKET_PATH or not LOCK_PATH or not OLD_LOCK_PATH:
        raise SystemExit("XDG_RUNTIME_DIR is not set; refusing to put the control socket in /tmp")
    for directory in dict.fromkeys(os.path.dirname(p) for p in (SOCKET_PATH, LOCK_PATH)):
        _private_dir(directory)
    _preload_job_package()
    lock = asyncio.Lock()
    # c: the link in use, only once it passed the model gate; reason: why there is no link;
    # refused_model: what the last machine the model gate refused reports itself as
    # (protocol.reported_model; None when it named no model); sessions: connections inside a
    # session (open or opening); stopping: the shutdown op was accepted or the idle time is
    # over; exit: why we end by ourselves
    # support: the connected machine's support level (Detection.support, "tested" or
    # "untested"); refused_profile, refused_support and refused_reason: the id, the level
    # ("recognized") and the plain-words reason of the last refused model when OpenCutter
    # recognizes it (None for a model it does not know)
    # stop_owed: the addresses (upper case) of the machines owed a stop (_owe_stop);
    # owed_stops_sent: how many of those went out on a new link; load_seen: how often the
    # status changed to the load digit; last_stop: when the last stop sequence went out
    # (monotonic), for the status log (STOP_WATCH)
    state: dict = {"c": None, "motion": None, "reason": None, "refused_model": None,
                   "support": None, "refused_profile": None, "refused_support": None,
                   "refused_reason": None, "sessions": 0, "stopping": False, "exit": None,
                   "last_request": time.monotonic(), "started_by": started_by(),
                   "stop_owed": set(), "owed_stops_sent": 0, "load_seen": 0, "last_stop": None}
    me = asyncio.current_task()

    def finish(why: str) -> None:
        """End the service through the same path as SIGTERM (the shutdown op, the idle time)."""
        if state["exit"] is None:
            state["exit"] = why
            state["stopping"] = True
            _log(why)
            me.cancel()

    def on_status(s: str) -> None:
        """Every status indication (repeated ones too): the open session's bookkeeping."""
        _note_status(state["motion"], s)

    def watching() -> bool:
        """Ready and Busy changes are logged while a session is open and for STOP_WATCH s after
        a stop, so that the log shows what a stop did; otherwise only with -v."""
        last = state.get("last_stop")
        return (verbose or state["motion"] is not None
                or (last is not None and time.monotonic() - last < STOP_WATCH))

    def on_status_change(link: Cameo4, old: str | None, new: str, source: str) -> None:
        """A real change of the link's last status (an indication, or an exact ESC 05 reply):
        one log line ("[status change] 4 = Cancelling", "... (ESC 05)" for a reply), Ready and
        Busy only while watching(); the statuses a new link reads before its model gate passed
        are in its "connected to" line instead. Counts the changes to the load digit (the
        ping's load_seen), on every link."""
        # the texts of the machine on that link (before the gate the CAMEO 4's: words only)
        profile = getattr(link, "profile", None) or CAMEO4
        if new == profile.status.load:
            state["load_seen"] += 1
        if old is None or not link.machine or (new in (profile.status.ready,
                                                       profile.status.busy)
                                               and not watching()):
            return
        via = "" if source == "indication" else f" ({source})"
        _log(f"[status change] {new} = {status_text(new, profile)}{via}")

    def new_link(dev) -> Cameo4:
        link = Cameo4(dev, verbose=verbose, on_status=on_status)
        link.on_status_change = (lambda old, new, source:
                                 on_status_change(link, old, new, source))
        return link

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        resp = None
        shutdown = False
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if line:
                _touch(state)
            req = json.loads(line)
            if not isinstance(req, dict):
                resp = {"ok": False, "error": "bad request"}
            elif req.get("op") in ("motion", "job"):
                if state["stopping"]:
                    resp = {"ok": False, "error": STOPPING}
                elif not _usable(state["c"]):
                    resp = {"ok": False, "error": _no_link_error(state)}
                else:
                    state["sessions"] += 1
                    try:
                        await _session(reader, writer, req, state["c"], lock, state)
                    finally:
                        state["sessions"] -= 1
                        _touch(state)
            elif req.get("op") == "shutdown":
                # never with a session open or opening; nothing is sent to the machine
                if state["sessions"] or state["motion"] is not None:
                    resp = {"ok": False, "error": SESSION_RUNNING}
                else:
                    state["stopping"] = True        # from now on every session is refused
                    resp, shutdown = {"ok": True}, True
            else:
                resp = await _one_shot(req, state["c"], lock, state)
        except UnsafeCommand as e:
            resp = {"ok": False, "error": f"refused: {e}"}
        except Exception as e:  # report to the client, keep serving
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            with contextlib.suppress(Exception):
                if resp is not None:
                    writer.write((json.dumps(resp) + "\n").encode())
                    await writer.drain()
                writer.close()
            if shutdown:
                finish("stopping on request; the machine is released for other programs")

    lock_fds = [_take_daemon_lock()]
    try:
        old = _take_old_lock()
        if old is not None:
            lock_fds.append(old)
        lock_fds.append(_take_legacy_lock())
        if await _socket_alive(SOCKET_PATH):
            raise SystemExit(f"an OpenCutter daemon is already running ({SOCKET_PATH})")
        # the first line of a service that got its directory and its locks (one that refused
        # before writes only its refusal)
        _log(f"OpenCutter {__version__} (build {BUILD}) background service started, pid "
             f"{os.getpid()}, {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}")
        await _refuse_if_held_elsewhere(address, force)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(SOCKET_PATH)
        old_umask = os.umask(0o177)          # socket is created 0600, no chmod window
        try:
            server = await asyncio.start_unix_server(handle, path=SOCKET_PATH, limit=MAX_REQUEST)
        finally:
            os.umask(old_umask)
    except BaseException:
        _close_all(lock_fds)
        raise

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):     # SIGHUP: the serve terminal was closed
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, me.cancel)

    _log(f"listening on {SOCKET_PATH}")
    # inside the Flatpak: the line GNOME shows under the app in Background Apps once the window
    # is closed (portal.announce_background; the session bus only)
    background = asyncio.create_task(announce_background(_log)) if in_flatpak() else None
    announced = CAMEO4.name                  # the machine the background status line names
    idle = None
    if idle_exit and idle_exit > 0:
        _touch(state)
        idle = asyncio.create_task(_watch_idle(state, idle_exit, finish))
        _log(f"stops by itself after {_duration(idle_exit)} without requests and without a job")
    _log("connecting to the machine... (if nothing happens, switch it off and on)")
    held_logged = False
    waiting_logged: tuple | None = None      # the reason (and its words) last logged while waiting
    # while no link comes up: since when (time.monotonic(), and the clock as HH:MM), how many
    # tries, and when the last 'still waiting' line was written (STILL_WAITING_EVERY)
    waiting_since: tuple[float, str] | None = None
    tries = 0
    still_logged = 0.0
    # the addresses (upper case) of the machines the model gate refused: never used again while
    # this service runs; discovery skips them, so another paired machine can be found
    refused: set[str] = set()
    # the addresses (upper case) whose connect or model check failed in this round: discovery
    # tries the other paired machines first, so one that is switched off or does not answer FG
    # cannot hide another one. When every one was tried, the next round starts.
    tried: set[str] = set()
    c: Cameo4 | None = None
    try:
        while True:
            gate = False                     # FG (or the status read after it) is under way
            det: Detection | None = None     # what FG on the new link names (machines.detect)
            try:
                if c is None:
                    if address and address.upper() in refused:
                        # --address names a machine the model gate refused: nothing else to
                        # try. Wait; the shutdown op, the idle time and signals still end us.
                        state["reason"] = REASON_WRONG_MODEL
                        await asyncio.sleep(WRONG_MODEL_WAIT)
                        continue
                    dev = await _next_machine(address, refused, tried)
                    if not force and _connected_elsewhere(dev):
                        # We hold no link, so another program does (e.g. an older daemon that
                        # reconnected first after a power cycle): never join it.
                        if not held_logged:
                            _log("the machine is connected by another program; not joining its "
                                 "link (close that program, or switch the machine off and on)")
                            held_logged = True
                        else:       # seen twice in a row: not just our own link going away
                            state["reason"] = REASON_HELD_ELSEWHERE
                        await asyncio.sleep(HELD_ELSEWHERE_WAIT)
                        continue
                    held_logged = False
                    # found, and nobody else holds it: those reasons are no longer true while we
                    # connect (that can take a minute); 'unreachable' stays until it works
                    if state["reason"] in (REASON_HELD_ELSEWHERE, REASON_NOT_PAIRED,
                                           REASON_NO_BLUETOOTH):
                        state["reason"] = None
                    c = new_link(dev)
                await c.open()
                _log(f"Bluetooth link to {c.device.name} [{c.device.address}] is up; reading its "
                     "FG reply")
                async with lock:
                    s = await c.esc_query(0x05)
                # The model gate, on every new link (after a link loss too; --force does not
                # skip it): FG right after ESC 05, under the lock, and nothing else is sent
                # before it passed. FG picks the machine profile (machines.detect): only a USE
                # verdict (TESTED, or UNTESTED with the firmware's exact reply) opens the gate on
                # this link (link.profile). Where FG claims the session (status 1 -> 2 on the
                # CAMEO 4: MachineProfile.fg_claims_session), the status is read again after it:
                # the first status clients see is right (a timeout of that read does not cost
                # the link: _status_after_fg).
                # A machine owed a stop (its link was lost while it may have been moving) may
                # still be busy with queued moves and answer FG late: its FG reply is waited for
                # longer, so that the gate does not drop the link that can stop it. Before the
                # gate, nothing but ESC 05 and FG may be sent (no stop either).
                owed = c.device.address.upper() in state["stop_owed"]
                gate = True
                async with lock:
                    fg = await (c.query("FG", timeout=FG_TIMEOUT_OWED) if owed
                                else c.query("FG"))
                    if _late_status(fg):
                        # Not a model: the late answer of an earlier ESC 05 on this link (two
                        # timeouts in a row reset Cameo4's count of late replies). Never refuse
                        # the machine for it: a failed probe, tried again on a new link.
                        raise RuntimeError(f"FG was answered with {fg!r}, a late status reply")
                    det = detect(fg)
                    if det.profile is not None:
                        c.profile = det.profile       # the gate opens on this link only
                        if det.profile.fg_claims_session:
                            s = await _status_after_fg(c)
                gate = False
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let one failure kill the session holder
                off = bluetooth_off_error(e)
                if off:     # the computer's Bluetooth is off (adapter not powered, or none)
                    state["reason"] = REASON_NO_BLUETOOTH
                elif c is not None:
                    state["reason"] = REASON_UNREACHABLE
                elif isinstance(e, PlotterUnreachable):
                    # nothing (else) is paired; after a refusal the refused machine is the news
                    state["reason"] = REASON_WRONG_MODEL if refused else REASON_NOT_PAIRED
                else:       # the look itself failed: BlueZ cannot be reached on the system bus
                    state["reason"] = REASON_NO_BLUETOOTH
                what = ("Bluetooth (it is off, or there is no adapter)" if off else
                        "Bluetooth (BlueZ cannot be reached)"
                        if state["reason"] == REASON_NO_BLUETOOTH
                        else "the machine (switch it off and on)")
                tries += 1
                now = time.monotonic()
                if waiting_since is None:
                    waiting_since = (now, time.strftime("%H:%M:%S")[:5])    # HH:MM
                if waiting_logged != (state["reason"], what):
                    _log(f"waiting for {what}: {type(e).__name__}: {e}")
                    waiting_logged = (state["reason"], what)
                    still_logged = now
                elif now - still_logged >= STILL_WAITING_EVERY:
                    _log(f"still waiting for {what} ({tries} tries since {waiting_since[1]}; "
                         f"last: {type(e).__name__}: {e})")
                    still_logged = now
                if c is not None and gate:
                    # every failed check on a link that came up (rare), whatever was logged
                    _log(f"the FG check on {c.device.name} [{c.device.address}] did not pass: "
                         f"{type(e).__name__}: {e}; this link is not used")
                if c is not None and gate and c.transport_up():
                    # FG (or the status read after it) failed, or FG timed out or got a late
                    # status reply. Its reply may still come, and replies carry no id: it must
                    # never answer a later query. So this link is not used: it is released, and
                    # the next try starts on a new link with a reply stream of its own.
                    tried.add(c.device.address.upper())
                    await _drop_link(c)
                    c = None
                elif c is not None and not c.transport_up():
                    tried.add(c.device.address.upper())
                    await c.release()
                    c = None
                await asyncio.sleep(RETRY_WAIT)
                continue
            if det is None or det.profile is None:
                # Not a machine OpenCutter drives (RECOGNIZED, UNKNOWN or no model): nothing
                # more is sent to it. Release its link, and skip it from now on, so that another
                # paired machine can be found.
                model = state["refused_model"] = reported_model(fg)
                _note_refusal(state, det)
                state["reason"] = REASON_WRONG_MODEL
                refused.add(c.device.address.upper())
                waiting_logged = (REASON_WRONG_MODEL, None)
                said = (f"reports itself as \"{model}\"" if model is not None
                        else "did not report its model (an empty FG reply)")
                why = (state["refused_reason"].rstrip(".") if state["refused_reason"] else
                       f"OpenCutter is tested only with the {TESTED_MACHINE} and does not use it")
                _log(f"the machine {c.device.name} [{c.device.address}] {said}; {why}; "
                     "releasing the link")
                await _drop_link(c)
                c = None
                continue
            profile = c.profile
            c.machine = model_text(fg)
            waiting_logged = None
            waiting_since, tries = None, 0
            tried.clear()
            if c.device.address.upper() in state["stop_owed"]:
                # The link to the machine owed a stop is back and passed the gate: the stop goes
                # out once, before anything else may use this link (state["c"] is not set yet, so
                # no session or query can come between). Only ESC 09 twice, the profile's stop;
                # on a machine that was switched off and on it is an idle stop. When it fails,
                # it stays owed for the next link. (A machine still busy that answers FG too late
                # even for FG_TIMEOUT_OWED is dropped by the gate: then only its power button
                # stops it.)
                errors = await c.emergency_stop()
                state["last_stop"] = time.monotonic()
                if errors:
                    _log(f"link back: sending the stop owed from the lost session: FAILED: "
                         f"{errors}; it is tried again on the next link")
                else:
                    state["stop_owed"].discard(c.device.address.upper())
                    state["owed_stops_sent"] += 1
                    _log("link back: sending the stop owed from the lost session: sent (ESC 09 "
                         f"x2, pair {_stop_pairs(c)} on this link)")
            state["reason"] = None
            state["support"] = det.support.value
            state["c"] = c                    # only now may anything be sent through it
            level = "" if det.tested else f" ({det.support.value})"
            _log(f"connected to {c.device.name} [{c.device.address}], {c.machine}{level}, "
                 f"status {s} = {status_text(s, profile)}")
            if background is not None and profile.name != announced:
                # the line under the app in Background Apps names the machine this service holds
                background.cancel()
                announced = profile.name
                background = asyncio.create_task(
                    announce_background(_log, background_status(profile.name)))
            await c.disconnected.wait()
            state["c"] = None
            state["reason"] = None
            state["support"] = None
            _log("link lost; waiting for the machine (switch it off and on)")
            sess = state["motion"]
            if (sess is not None and sess.link is c and sess.motion_possible
                    and not sess.stop_sent):
                _owe_stop(state, c, "the link was lost while it may have been moving")
            await c.release()
            c = None
    except asyncio.CancelledError:
        if state["exit"] is None:
            raise                          # a signal, or the caller cancelled serve()
        me.uncancel()                      # our own clean exit (shutdown op, idle time)
    finally:
        # Ignore further signals while finishing: a second Ctrl+C / SIGTERM must not cut the stop
        # short. (Not remove_signal_handler: that restores SIG_DFL, which would kill us.)
        with contextlib.suppress(Exception):
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with contextlib.suppress(Exception):
                loop.add_signal_handler(sig, lambda: None)
        state["stopping"] = True
        if idle is not None:
            idle.cancel()
        if background is not None:
            background.cancel()
        sess = state["motion"]
        release = True
        # Only through a link that passed the model gate, to the session's machine. After a link
        # loss, c may already be a new link whose gate is still under way (maybe to another
        # model): it gets nothing. While motion is possible (a step runs, or the machine was not
        # yet read ready twice since its last write), the stop goes out; on an idle machine that
        # is harmless.
        if (sess is not None and (sess.running or sess.motion_possible) and _usable(c)
                and _address(c) == _address(sess.link or c)):
            stop = asyncio.ensure_future(_stop_session(c, sess))   # start it before any print
            _log("daemon stopping during a step -> STOP" if sess.running else
                 "daemon stopping between steps of a session (the machine may be idle) -> STOP")
            deadline = loop.time() + 15        # > 2 writes x 2 s + gap, plus a joined sequence
            while not stop.done() and loop.time() < deadline:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait({stop}, timeout=max(0.0, deadline - loop.time()))
            # a stop that did not get through is tried again when the session's task ends:
            # keep the link for that (it drops when this process ends)
            release = sess.stop_sent
        state["c"] = None                     # meanwhile: sessions refused, nothing to query
        if c is not None and release:
            await _release_link(c)            # before the socket and the locks go: whoever sees
                                              # them gone (a new daemon) never meets our link
        server.close()                        # (Python >= 3.13 also removes the socket file)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(SOCKET_PATH)
        _close_all(lock_fds)
