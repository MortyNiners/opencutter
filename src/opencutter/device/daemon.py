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
  * once the session's first write went out, a Paused status (an indication or an ESC 05 reply;
    '3' on every model today) makes the session wait, as Silhouette Studio does: nothing more is
    written but ESC 05 (every PAUSED_POLL s, and at once on every status indication), with no
    time limit, until the machine goes on ('1'; or '0' when the pause came between batches, or
    within RESUME_WINDOW s of the user's Resume), the user stops the job, or the link is lost.
    The machine's own Pause key, or the user's Resume (one ESC 08, _command_op), goes on;
    nothing is ever sent by itself to make it go on. A '0' after a pause that came in the middle
    of a batch, with no '1' or Cancelling seen, may be a cancel on the panel whose Cancelling
    was missed: the job ends with the stop (LEFT_PAUSE). A Cancelling status (not after our
    own stop) ends the job for good, with the stop, even when a later ESC 05 reads ready;
  * the user's Pause (one ESC 07, _command_op) only while a session of ours runs and the machine
    reads Ready or Busy, and Resume (one ESC 08) only while that session waits on a paused
    machine; both only on a TESTED profile and firmware line that has them (the CAMEO 4),
    never repeated, never sent by OpenCutter on its own;
  * no abort on silence: inside a session ESC 05 and ESC 15 are asked patiently
    (link.SafeLink.patient_esc: one query at a time, overdue after STATUS_OVERDUE s, then asked
    once more and logged, the ping's no_reply_s); a step ends only on ready, a status that ends
    it, the stop, a lost link or READY_TIMEOUT counted over Busy replies only. A ready reply
    ends a wait only when it answers the one query outstanding, and only once no earlier query
    is still unanswered (a late reply must never end the wait for the next batch): the earlier
    replies are waited for first, sending nothing, and the reply to the last query decides;
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
    SIGHUP, SIGINT; further signals are ignored while that stop goes out. No query is written
    from the stop's first byte until 0.3 s after its last (link.STOP_QUIET); after the stop op
    ESC 05 is read back (STOP_CHECK s while Busy or Cancelling, STOP_CHECK_PAUSED s while
    Paused: a paused job ends about 5 s after the stop), and nothing else is sent: no second
    pair by itself, no FG, no U, no ESC 15;
  * the info op answers the FG reply read at the model gate (it never sends FG), and text
    queries (and ESC 10/15/16) are refused while the machine reads Paused or Cancelling;
    text queries also for QUERY_HOLD_AFTER_STOP s after a stop.
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
from ..machines import (CAMEO4, Detection, MachineProfile, Mat, Support, Verdict, detect,
                        registry)
from .ble import Cameo4, PlotterUnreachable, bluetooth_off_error, find_bonded_cameo
from .link import MIN_SESSION_WRITE_LIMIT, REPLY_LOST_AFTER, RESUME_QUIET, QueryAbandoned
from .portal import announce_background, background_status
from .programs import build_program
from .protocol import (CANCELLING, ETX, INTERRUPTED_TERMS, LEGACY_LOCK_NAME, LOCK_NAME, PAUSED,
                       REASON_HELD_ELSEWHERE, REASON_NO_BLUETOOTH, REASON_NOT_PAIRED,
                       REASON_UNREACHABLE, REASON_WRONG_MODEL, REJECTED, REPLY_TIMEOUT,
                       SOCKET_NAME, STOP_CHECK_PAUSED, TESTED_MACHINE, UnsafeCommand,
                       evaluate_units, in_flatpak, is_status, lowers_tool, model_text,
                       not_ready_text, reported_model, runtime_path, service_path, status_text,
                       tool_name, wrong_model_message)

SOCKET_PATH = service_path(SOCKET_NAME)   # $XDG_RUNTIME_DIR/app/<app id>/opencutter.sock
LOCK_PATH = service_path(LOCK_NAME)
OLD_LOCK_PATH = runtime_path(LOCK_NAME)   # where builds before 0.1.0 locked: held as well

# base wait for ready after a write (Graphtec's safe_write uses 120 s), counted over Busy
# replies only: neither a pause nor a reply that is late counts
READY_TIMEOUT = 120.0
PRE_STEP_WAIT = 10.0       # a previous step may still report '1' for a moment (Busy time only)
FIRST_POLL = 0.5           # the first ESC 05 read after a batch: never a stale '0'
BUSY_POLL = 0.4            # between ESC 05 reads while the machine reads Busy
PAUSED_POLL = 2.5          # between ESC 05 reads while it is paused (Studio's Bluetooth rate)
# The most one Busy reply adds to the Busy time: a poll cycle with a normal reply time, so that
# a reply that comes late (or a pause) does not use up READY_TIMEOUT or PRE_STEP_WAIT
BUSY_CREDIT_MAX = BUSY_POLL + 1.0
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
# The machine left Pause with '0' although the pause came in the middle of a batch, and neither
# '1' (it went on) nor Cancelling was seen: maybe a cancel on its panel whose Cancelling status
# was missed (it is shown only for a moment). The job ends with the stop.
LEFT_PAUSE = ("The machine left Pause without going on with the cut, so it may have been "
              "cancelled on its panel. OpenCutter ended the job with the stop and sends nothing "
              "more. Press Unload on the machine, check that the tool is up, then load the mat "
              "and send the job again.")
LEFT_PAUSE_PROGRAM = ("The machine left Pause without going on with the program, so it may have "
                      "been cancelled on its panel. OpenCutter ended the program with the stop "
                      "and sends nothing more. Press Unload on the machine, check that the tool "
                      "is up, then load the mat and run the program again.")
# what MotionSession.interrupted (and the ping's "interrupted") holds for that case
LEFT_PAUSE_MARK = "left-pause"
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
#   interrupted      why the open session ends with the stop although nobody asked for one: the
#                    Cancelling status digit it saw after its first write (a cancel on the
#                    panel), or LEFT_PAUSE_MARK ("left-pause": the machine left Pause without
#                    going on); None otherwise
#   load_seen        how often the machine's status changed to the load digit ('2', Load Media,
#                    on every model today) since the service started: an unload on the panel
#                    shows as a change of this count, even between two pings
#   paused           the open session waits on a paused machine (Paused seen after its first
#                    write, and it has not gone on yet): nothing more is sent until it goes on
#   resume_ok        a resume op would be sent now: paused, no stop asked for, and a machine
#                    whose Resume was tested (the CAMEO 4 on firmware V5)
#   pause_ok         a pause op would be sent now: a session of ours wrote, it is not paused or
#                    ending, the machine last read Ready or Busy, and its Pause was tested
#   no_reply_s       how long the query the open session waits for has been unanswered, once
#                    that is STATUS_OVERDUE (7) s or more ("Checking connection"); else None
#   indication_age_s seconds since the machine's last status indication, or None: indications
#                    that still come while replies do not tell a stalled reply from a dead link
#   stop_check       the read-back after the last stop op that asked for it, or None: {"running"
#                    (still reading), "status" (the last ESC 05 digit read, or None), "seconds"
#                    (how long it read), "still_busy", "still_paused" (None when nothing was
#                    read), "reason" (why nothing could be read, or None)}
PING_STOP_KEYS = ("stop_owed", "owed_stops_sent", "motion_stopped", "interrupted", "load_seen",
                  "paused", "resume_ok", "pause_ok", "no_reply_s", "indication_age_s",
                  "stop_check")
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
                           # while it reads Busy or Cancelling
# ... and STOP_CHECK_PAUSED (protocol) once it read Paused (a paused job ends about 5 s after it)
STOP_CHECK_POLL = 0.4      # between those reads
STOP_LOCK_WAIT = 4.0       # how long each of those reads waits for the query lock (more than
                           # one reply time-out)
QUERY_HOLD_AFTER_STOP = 10.0  # text queries are refused this long after a stop
RESUME_LOCK_WAIT = 3.0     # the resume and pause ops: how long they wait for the query lock
RESUME_CHECK = 5.0         # ... how long they watch for the machine to leave (or reach) Paused
RESUME_WINDOW = 5.0        # a '0' this soon after our Resume means it went on with nothing left
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
    # A Cancelling status (its digit) seen after the first write, without a stop of ours before
    # it (the machine was cancelled on its panel), or LEFT_PAUSE_MARK (it left Pause without
    # going on). The job ends for good, with the stop.
    interrupted: str | None = None
    # From each motion write until the machine read ready at the end of a step and again right
    # before the next one: the machine may still be moving (the loss, exit and refusal stops).
    motion_possible: bool = False
    link: Any = None             # the link the session runs on
    # called with (link, errors) after each stop sequence the session sent or joined
    on_stop: Any = None
    # The pause (_note_status): the Paused digit while the machine is paused after the first
    # write (None otherwise), since when (time.monotonic()), whether it came while a batch was
    # open, every status seen since, and how many pauses there were in this session
    paused: str | None = None
    paused_at: float | None = None
    paused_total: float = 0.0    # seconds paused so far
    paused_mid_batch: bool = False
    seen_since_pause: set = field(default_factory=set)
    pauses: int = 0
    resume_sent_at: float | None = None   # time.monotonic() of the user's last Resume (ESC 08)
    pause_sent_at: float | None = None    # ... and of the user's last Pause (ESC 07)
    # from a batch's first write until the ready read after it: a pause now leaves bytes of
    # ours in the machine
    batch_open: bool = False
    step_title: str | None = None   # the step that runs, for the log
    batch: tuple[int, int] = (0, 0)   # (this batch, batches) of that step, for the log


def _what(sess: MotionSession) -> tuple[str, str]:
    """('job', 'send the job again') for a job, ('program', 'run the program again') else."""
    if sess.name == "job":
        return "job", "send the job again"
    return "program", "run the program again"


def _now() -> float:
    return time.monotonic()


def _note_status(sess: MotionSession | None, status: str | None) -> None:
    """Record a status seen during a session (an indication or an exact ESC 05 reply).

    A '2' (Load Media, the profile's load digit) after the header went out means the mat was
    unloaded (StatusModel.unloaded_after_header, on every model OpenCutter drives); after a
    reload the origin no longer matches what TG1 set (PROTOCOL.md 7.2: a panel load sets
    a := 0, TG1 feeds a further 16.5 mm and sets a := 0 again, and TG1 is not repeated), so
    every later step is refused. A '2' before the header is harmless: the header's TG1 then
    follows a fresh load.

    A Paused status (the profile's term; '3' on every model today) after the header, while no
    stop of ours was asked for, starts a pause (sess.paused): the session sends nothing more
    but ESC 05 until it ends. Every status seen meanwhile is kept; the pause ends with the
    first of them that says how it went on:
      Busy               it goes on (the Pause key again, or our Resume);
      Ready              it went on with nothing left when the pause came between batches (no
                         batch was open) or within RESUME_WINDOW s of our Resume; otherwise,
                         with no Busy and no Cancelling seen, it may be a cancel on the panel
                         whose Cancelling was missed: latched as LEFT_PAUSE_MARK (the job ends
                         with the stop);
      Cancelling, load   as below.
    A Cancelling status (the profile's INTERRUPTED_TERMS; '4' on every model today) after the
    header, while no stop of ours was asked for, is latched in sess.interrupted: the machine was
    cancelled on its panel, so the step in progress, or the next one, ends with the stop even
    when a later ESC 05 reads ready. A Cancelling or Paused after our own stop changes nothing.
    'Rejected' and other digits are not latched (the waits judge them)."""
    if sess is None or not sess.wrote or sess.media_changed:
        return
    states = sess.profile.status
    if status == states.load and states.unloaded_after_header:
        sess.media_changed = True
        _clear_pause(sess)
        _log(f"{sess.name}: the mat was unloaded (status {status}) during the session; "
             "nothing more will be sent")
        return
    if sess.stop.is_set():
        return
    term = status_text(status, sess.profile, default="")
    if term == PAUSED:
        if sess.paused is None and sess.interrupted is None:
            _begin_pause(sess, status)
        return
    if sess.paused is not None:
        _leave_pause(sess, status, term)
    if term in INTERRUPTED_TERMS and sess.interrupted is None:
        sess.interrupted = status
        what, _ = _what(sess)
        _log(f"{sess.name}: the machine reported {status} = {term} during the session: it was "
             f"cancelled on its panel, and the {what} ends with the stop")


def _begin_pause(sess: MotionSession, status: str) -> None:
    """The machine paused during the session: from now on nothing but ESC 05 is sent."""
    sess.paused = status
    sess.paused_at = _now()
    sess.paused_mid_batch = sess.batch_open
    sess.seen_since_pause = set()
    sess.pauses += 1
    where = f" during {sess.step_title!r}" if sess.step_title else " between steps"
    b, n = sess.batch
    if n and sess.batch_open:
        where += f", batch {b}/{n}"
    elif n:
        where += f", after batch {b}/{n}"
    _log(f"{sess.name}: the machine paused ({status}){where}; waiting: nothing more is sent "
         "until it goes on")


def _clear_pause(sess: MotionSession) -> None:
    if sess.paused is not None and sess.paused_at is not None:
        sess.paused_total += max(0.0, _now() - sess.paused_at)
    sess.paused = None
    sess.paused_at = None


def _leave_pause(sess: MotionSession, status: str | None, term: str) -> None:
    """A status other than Paused while the session is paused (see _note_status)."""
    states = sess.profile.status
    sess.seen_since_pause.add(status)
    waited = _now() - (sess.paused_at or _now())
    if term in INTERRUPTED_TERMS:
        _clear_pause(sess)                 # latched by the caller
    elif status == states.busy:
        _log(f"{sess.name}: the machine goes on ({status}) after {waited:.0f} s")
        _clear_pause(sess)
    elif status == states.ready:
        ours = (sess.resume_sent_at is not None and sess.paused_at is not None
                and sess.resume_sent_at >= sess.paused_at
                and _now() - sess.resume_sent_at <= RESUME_WINDOW)
        if not sess.paused_mid_batch or ours:
            _log(f"{sess.name}: the machine goes on ({status}: nothing was left to run) after "
                 f"{waited:.0f} s")
            _clear_pause(sess)
        else:
            _log(f"{sess.name}: the machine left Pause without going on ({sess.paused} -> "
                 f"{status}, no {states.busy} and no Cancelling seen): ended")
            _clear_pause(sess)
            sess.interrupted = LEFT_PAUSE_MARK


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
    term says what happened (Cancelling, Rejected, Busy; LEFT_PAUSE_MARK: LEFT_PAUSE).
    `before_step`: the step was refused before its first write. A digit without such a term
    keeps "unexpected status 'N' = term" (during a step). Never mentions the Pause key, except
    LEFT_PAUSE, which says that the machine left Pause."""
    p = sess.profile
    what, again = _what(sess)
    if status == LEFT_PAUSE_MARK:
        return LEFT_PAUSE if sess.name == "job" else LEFT_PAUSE_PROGRAM
    term = status_text(status, p, default="")
    shown = f"status {status!r} = {status_text(status, p)}"
    nothing = " before this step; nothing of this step was sent" if before_step else ""
    unload = (f"press Unload on the machine (or switch it off), check that the tool is up, then "
              f"load the mat and {again}.")
    if term == PAUSED:                     # a session waits instead; kept for safety
        return (f"The machine was paused ({shown}){nothing}. OpenCutter ended the {what} with "
                f"the stop and sends nothing more: {unload}")
    if term == CANCELLING:
        return (f"The machine cancelled the {what} ({shown}){nothing}. OpenCutter ended the "
                f"{what} with the stop and sends nothing more. Press Unload on the machine, "
                f"check that the tool is up, then load the mat and {again}.")
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
    an unloaded mat, a Cancelling status (or LEFT_PAUSE_MARK) seen since the first write, or
    (with `link`) a lost link. A pause is not a reason: the session waits. The ready waits leave
    an unloaded mat to the status they read (a mat unloaded and loaded again during a step's
    last batch refuses the next step, without a stop)."""
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
    """Take a Cancelling status the link saw last (an indication the session's own callback
    may have missed) into the session's latch, and a Paused one into its pause
    (_note_status)."""
    last = getattr(c, "last_status", None)
    term = status_text(last, sess.profile, default="")
    if term in INTERRUPTED_TERMS or (term == PAUSED and sess.paused is None):
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


async def _nap(c, sess: MotionSession, seconds: float) -> None:
    """Wait `seconds`, or less: until the session's stop or the link's next status indication
    (a test double without next_indication: the stop only)."""
    if seconds <= 0:
        return
    nxt = getattr(c, "next_indication", None)
    if nxt is None:
        await _sleep_or_stop(sess.stop, seconds)
        return
    indication = nxt()
    stop = asyncio.ensure_future(sess.stop.wait())
    try:
        await asyncio.wait({indication, stop}, timeout=seconds,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        indication.cancel()
        stop.cancel()


def _reporter(sess: MotionSession):
    """The log lines of a patient query (link.SafeLink.patient_esc), with the session's name."""
    return lambda text: _log(f"{sess.name}: {text}")


async def _session_query(c, lock: asyncio.Lock, sess: MotionSession, code: int,
                         hold=None) -> tuple[str | None, bool]:
    """An ESC query inside a session, asked patiently (link.SafeLink.patient_esc: never given
    up while the link is up, one query at a time under the query lock). Returns (reply, fresh).
    With `hold`: (None, False) without writing anything when hold() is true once the lock is
    held. A query that stops waiting for the session's stop or a lost link raises MotionAborted
    (_check_session). Once the stop was asked for (and for a test double without patient_esc)
    it is the one-shot read (_esc_retry)."""
    patient = getattr(c, "patient_esc", None)
    if sess.stop.is_set() and code != 0x05:        # after a stop: nothing but ESC 05
        raise MotionAborted(STOPPED_IN_STEP)
    if patient is None or sess.stop.is_set():     # after a stop request: one plain read
        if hold is not None and hold():
            return None, False
        return await _esc_retry(c, lock, code, sess.profile), True
    try:
        async with lock:
            if hold is not None and hold():
                return None, False
            if sess.stop.is_set() and code != 0x05:    # the stop came while the lock was busy
                raise MotionAborted(STOPPED_IN_STEP)
            return await patient(code, stop=sess.stop, report=_reporter(sess))
    except QueryAbandoned:
        _check_session(c, sess, media=False)
        raise MotionAborted(LINK_LOST) from None


async def _session_status(c, lock: asyncio.Lock, sess: MotionSession) -> tuple[str, bool]:
    """ESC 05 inside a session (_session_query) once the session wrote, the one-shot read with
    one retry (_esc_retry) before that (nothing has moved yet). A reply that is not a status is
    read once more (it is then not fresh). Returns (reply, fresh)."""
    if not sess.wrote:
        return await _esc_retry(c, lock, 0x05, sess.profile), True
    s, fresh = await _session_query(c, lock, sess, 0x05)
    if not is_status(s, sess.profile):
        s, _ = await _session_query(c, lock, sess, 0x05)
        fresh = False
    return s, fresh


def _replies_pending(c) -> int:
    """How many queries on the link still wait for their reply (SafeLink.replies_pending; 0
    for a test double without it)."""
    pending = getattr(c, "replies_pending", None)
    n = pending() if callable(pending) else 0
    return n if type(n) is int else 0


async def _ready_status(c, lock: asyncio.Lock, sess: MotionSession) -> tuple[str, bool]:
    """_session_status for the reads that can end at ready (wait_ready, _pre_step_status): the
    next motion write follows a ready reply. A ready reply that came while an earlier query was
    still unanswered may be the late answer to that one (replies carry no id), so it does not
    count yet: the replies still due are waited for under the query lock, sending nothing
    (link.SafeLink.drain_replies), and then the reply to the last query is the status, fresh.
    When they do not come (taken as lost after link.REPLY_LOST_AFTER s) the ready reply is not
    fresh, and the caller reads again. MotionAborted for a stop request or a lost link meanwhile
    (_check_session)."""
    s, fresh = await _session_status(c, lock, sess)
    pending = _replies_pending(c)
    if s != sess.profile.status.ready or not pending:
        return s, fresh
    _log(f"{sess.name}: a ready reply came while {pending} earlier "
         f"quer{'y was' if pending == 1 else 'ies were'} still unanswered; waiting for "
         f"{'its reply' if pending == 1 else 'their replies'} before it counts (nothing is sent)")
    try:
        async with lock:
            answered = await c.drain_replies(sess.stop)
    except QueryAbandoned:
        _check_session(c, sess, media=False)
        raise MotionAborted(LINK_LOST) from None
    latest = getattr(c, "last_reply_text", None)
    if answered and is_status(latest, sess.profile):
        _log(f"{sess.name}: the replies came; the last one reads {latest}")
        return latest, True
    _log(f"{sess.name}: no reply for {REPLY_LOST_AFTER:g} s: taken as lost; ESC 05 is read again")
    return s, False


async def _tool_check(c, lock: asyncio.Lock, sess: MotionSession) -> str | None:
    """ESC 15 right before a write that can lower a tool: patient once the session wrote, the
    one-shot read before that. None when the machine is paused (nothing was asked: ESC 15 is
    never written while it is paused)."""
    if not sess.wrote:
        return await _esc_retry(c, lock, 0x15, sess.profile)
    reply, _ = await _session_query(c, lock, sess, 0x15, hold=lambda: _is_paused(sess))
    return reply


def _pause_over(sess: MotionSession) -> bool:
    """True when the session's wait on a paused machine is over, whatever the reason."""
    return (sess.paused is None or sess.stop.is_set() or sess.interrupted is not None
            or sess.media_changed)


def _is_paused(sess: MotionSession) -> bool:
    """The session waits on a paused machine (and nothing has ended that wait yet)."""
    return not _pause_over(sess)


async def _wait_while_paused(c, lock: asyncio.Lock, sess: MotionSession) -> str | None:
    """While the machine is paused (sess.paused): no time limit, nothing but ESC 05, every
    PAUSED_POLL s (every BUSY_POLL s for RESUME_WINDOW s after the user's Pause or Resume), and
    at once on every status indication (_note_status ends the pause). Returns once the pause is
    over (it went on, it was cancelled or unloaded, it left Pause, the stop was asked for) with
    the last status read (None when none was read). A reply that is neither a status nor one
    _note_status judges (Rejected, an unknown digit) ends the pause too: it is returned, and
    the caller refuses it. Raises MotionAborted for a lost link (and for a stop that came while
    a query was out)."""
    states = sess.profile.status
    last = None
    while not _pause_over(sess):
        if not _link_up(c):
            raise MotionAborted(LINK_LOST)
        sent = [t for t in (sess.resume_sent_at, sess.pause_sent_at) if t is not None]
        soon = bool(sent) and _now() - max(sent) < RESUME_WINDOW
        await _nap(c, sess, BUSY_POLL if soon else PAUSED_POLL)
        if _pause_over(sess):
            break
        s, _ = await _session_status(c, lock, sess)
        last = s
        _note_status(sess, s)
        term = status_text(s, sess.profile, default="")
        if not _pause_over(sess) and term not in (PAUSED, CANCELLING) and s not in (
                states.ready, states.busy, states.load):
            _log(f"{sess.name}: while paused the machine reported {s!r}; the pause ends there")
            _clear_pause(sess)
    return last


async def wait_ready(c: Cameo4, lock: asyncio.Lock, sess: MotionSession,
                     timeout: float = READY_TIMEOUT) -> str:
    """Poll ESC 05 until the profile's ready digit ('0'), patiently (_session_status: a query
    that is late is never given up while the link is up). The first read comes FIRST_POLL s
    after the batch, then one every BUSY_POLL s while it reads Busy, woken by every status
    indication. A ready reply ends the wait only when it is fresh (it answers the one query
    outstanding) and no earlier query is still unanswered (_ready_status waits for those
    replies first); otherwise ESC 05 is read once more. While the machine is paused the wait goes
    on with no limit (_wait_while_paused). `timeout` is counted over Busy replies only (each one
    adds at most BUSY_CREDIT_MAX s): neither a pause nor silence uses it up.

    Raise MotionAborted for anything but ready, busy or paused (the load digit with the plain
    words of an unloaded mat), for a stop request, a Cancelling status or LEFT_PAUSE_MARK
    (_note_status) seen before a poll or while it was out, even when its reply is ready; also
    for a lost link and for the timeout."""
    states = sess.profile.status
    busy_s = 0.0
    await _sleep_or_stop(sess.stop, FIRST_POLL)    # do not read a stale '0' before motion starts
    mark = _now()
    while True:
        _check_session(c, sess, media=False)
        if sess.paused is not None:
            last = await _wait_while_paused(c, lock, sess)
            _check_session(c, sess, media=False)
            if sess.media_changed:
                raise MotionAborted(_media_changed_step_error(sess))
            if last is not None and sess.paused is None and last not in (
                    states.ready, states.busy) and status_text(
                    last, sess.profile, default="") != PAUSED:
                raise MotionAborted(_status_stop_text(last, sess))
            mark = _now()
            continue                       # a fresh read after the pause
        s, fresh = await _ready_status(c, lock, sess)
        _note_status(sess, s)
        _check_session(c, sess, link=False, media=False)   # what came while the poll was out
        if sess.paused is not None:
            continue
        if s == states.ready:
            if fresh:
                sess.batch_open = False
                return s
            continue                       # read once more: this reply may be a late one
        if s != states.busy:
            if sess.media_changed:
                raise MotionAborted(_media_changed_step_error(sess))
            raise MotionAborted(_status_stop_text(s, sess))
        now = _now()
        busy_s += min(now - mark, BUSY_CREDIT_MAX)
        mark = now
        if busy_s > timeout:
            raise MotionAborted(f"still Busy after {timeout:.0f} s")
        await _nap(c, sess, BUSY_POLL)


async def _pre_step_status(c: Cameo4, lock: asyncio.Lock, sess: MotionSession) -> str:
    """Status right before a step; a brief busy ('1') left over from the previous step is
    waited out for up to PRE_STEP_WAIT s of Busy replies (not after a stop request, an unload
    or a cancel), and a paused machine is waited for with no limit (_wait_while_paused),
    then read again. A ready reply must be fresh, with no earlier query unanswered
    (_ready_status). A ready reply here is the second one since the previous step's motion
    (its last ready wait was the first): motion is no longer possible until the next write
    (sess.motion_possible), unless a pause came meanwhile.
    A stop request ends the wait (the caller refuses the step); a lost link raises
    MotionAborted."""
    states = sess.profile.status
    pauses = sess.pauses if sess.paused is None else -1     # a pause before or during it
    s = getattr(c, "last_status", None)

    def over() -> bool:
        return sess.stop.is_set() or sess.interrupted is not None or sess.media_changed

    try:
        s, fresh = await _ready_status(c, lock, sess)
        _note_status(sess, s)
        busy_s, mark = 0.0, _now()
        while not over():
            if sess.paused is not None:
                last = await _wait_while_paused(c, lock, sess)
                if over():
                    break
                if sess.paused is None and last is not None and last not in (
                        states.ready, states.busy) and status_text(
                        last, sess.profile, default="") != PAUSED:
                    s = last                   # refused by the caller
                    break
                s, fresh = await _ready_status(c, lock, sess)   # a fresh read after it
                _note_status(sess, s)
                mark = _now()
                continue
            if s == states.ready and not fresh:
                s, fresh = await _ready_status(c, lock, sess)
                _note_status(sess, s)
                continue
            if s != states.busy or busy_s >= PRE_STEP_WAIT:
                break
            await _nap(c, sess, BUSY_POLL)
            s, fresh = await _ready_status(c, lock, sess)
            _note_status(sess, s)
            if s == states.busy:
                now = _now()
                busy_s += min(now - mark, BUSY_CREDIT_MAX)
                mark = now
    except MotionAborted:
        if sess.stop.is_set() and _link_up(c):
            return s                       # the step is refused for the stop request
        raise
    if (s == states.ready and sess.interrupted is None and not sess.stop.is_set()
            and sess.paused is None and sess.pauses == pauses):
        sess.motion_possible = False
    return s


def _stop_pairs(c) -> int:
    """How many stop sequences the link sent (SafeLink.stop_sequences)."""
    n = getattr(c, "stop_sequences", 0)
    return n if type(n) is int else 0


def _stop_line(c, errors: list[str]) -> str:
    """The log line of a stop sequence this link just sent: 'STOP (ESC 09 x2) sent, pair 1 on
    this link', or for a failed one what each write did ('STOP (ESC 09 x2) FAILED, pair 1 on
    this link: write 1 acknowledged after 0.03 s; write 2 no Write Response within 2 s')."""
    if not errors:
        return f"STOP (ESC 09 x2) sent, pair {_stop_pairs(c)} on this link"
    report = getattr(c, "last_stop_report", "")
    detail = report if isinstance(report, str) and report else f"{errors}"
    return f"STOP (ESC 09 x2) FAILED, pair {_stop_pairs(c)} on this link: {detail}"


async def _stop_session(c: Cameo4, sess: MotionSession, force: bool = False) -> list[str]:
    """Stop the session's motion. Automatic paths skip it if a stop already went out
    successfully; an explicit stop (force) always sends (or joins one in flight). The log says
    which: a pair sent (and its number on this link), a pair in flight joined, or none sent
    because one already got through. sess.on_stop hears the result of every pair sent or
    joined."""
    sess.stop.set()
    _clear_pause(sess)                   # the job ends: it no longer waits on the machine
    if sess.stop_sent and not force:
        _log(f"{sess.name}: STOP already sent (pair {_stop_pairs(c)} on this link); not sent "
             "again")
        return []
    joined = bool(getattr(c, "stop_in_flight", False))
    errors = await c.emergency_stop()
    if not errors:
        sess.stop_sent = True            # a failed sequence stays retryable
    if joined:
        result = f"FAILED: {errors}" if errors else "sent"
        _log(f"{sess.name}: joined the STOP in flight (pair {_stop_pairs(c)} on this link): "
             f"{result}")
    else:
        _log(f"{sess.name}: {_stop_line(c, errors)}")
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

    Nothing but U is sent here (and ESC 05 while the machine is paused). U is never written
    while the machine is paused (it would not be answered): the step waits until it goes on
    (_wait_while_paused); a U that times out while a pause came is asked once more after it.
    A Cancelling status (or LEFT_PAUSE_MARK) seen before or during it, or a stop request,
    refuses the step with StepRefused(stop=True) (the caller sends the stop, which an idle
    machine takes harmlessly); a lost link raises MotionAborted. Neither is named a mat
    problem."""
    expected, enforced = _units_expectation(sess.plan, sess.profile)

    def refusal() -> dict | None:
        if not _link_up(c):
            raise MotionAborted(LINK_LOST)
        if sess.stop.is_set():
            raise StepRefused(STOPPED_IN_UNITS.format(again=_what(sess)[1]), stop=True)
        _latch_from_link(c, sess)
        if sess.interrupted is not None:
            raise StepRefused(_status_stop_text(sess.interrupted, sess), stop=True)
        return None

    reply = None
    verdict, msg = "unknown", "no reply to U; the unit is not confirmed"
    asked = 0
    while True:
        try:
            while _is_paused(sess):
                await _wait_while_paused(c, lock, sess)
        except MotionAborted:
            if not sess.stop.is_set():
                raise
        refusal()
        if sess.media_changed:
            break
        pauses = sess.pauses
        try:
            async with lock:
                refusal()                      # a stop or a cancel while the lock was busy
                if sess.media_changed:
                    break
                if _is_paused(sess):
                    continue                   # paused meanwhile: wait first, U not written
                asked += 1
                reply = await c.query("U")
        except TimeoutError:
            verdict, reply, msg = "unknown", None, "no reply to U; the unit is not confirmed"
            if asked == 1 and (sess.pauses != pauses or _is_paused(sess)):
                _log(f"{sess.name}: U was not answered while the machine paused; asked again "
                     "once it goes on")
                continue
        else:
            verdict, msg = evaluate_units(reply, expected, enforced)
        break
    _log(f"{sess.name}: U -> {reply!r} ({verdict})")
    refusal()
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
                       status: str) -> str:
    """Write one chunk of the current step; returns the status it went out on (`status`, or a
    newer one read after a pause).

    Before the step's first write, a refusal raises StepRefused (nothing of the step was sent).
    After it, problems raise MotionAborted so that the caller sends the stop. Nothing is written
    while the machine is paused (sess.paused, checked again under the query lock right before
    the write): the chunk waits until it goes on (_wait_while_paused); before the step's first
    write the pre-step reads run again, and after a pause between batches a fresh ready is read
    first. Every motion write sets sess.motion_possible."""
    p = sess.profile
    while True:
        if _is_paused(sess):
            if not sess.running:
                status = await _pre_step_status(c, lock, sess)
            else:
                last = await _wait_while_paused(c, lock, sess)
                _check_session(c, sess)
                if sess.paused is None and last is not None and last not in (
                        p.status.ready, p.status.busy) and status_text(
                        last, p, default="") != PAUSED:
                    raise MotionAborted(_status_stop_text(last, sess))
                if not sess.batch_open:        # it paused between batches: ready again first
                    status = await wait_ready(c, lock, sess)
        # what can be refused without a query is refused first: no ESC 15 after a stop request
        if not sess.running:
            _refuse_before_first_write(c, sess, status, None)
        else:
            _check_session(c, sess)
        tools = await _tool_check(c, lock, sess) if lowers_tool(chunk, p) else None
        if tools is None and lowers_tool(chunk, p):
            continue                           # paused before the tool check: wait first
        async with lock:
            if _is_paused(sess):
                continue                       # paused meanwhile: nothing is written
            if not sess.running:
                _refuse_before_first_write(c, sess, status, tools)
            else:
                _check_session(c, sess)
                if getattr(c, "profile", p) is not p:
                    raise MotionAborted("the machine on the link is not the one the session "
                                        "was opened for")
                problem = (tool_problem(sess.plan.required_tools, sess.allow_unrecognized, tools,
                                        p) if tools is not None else None)
                if problem:
                    raise MotionAborted(problem)
            was_running, sess.running = sess.running, True
            was_possible, sess.motion_possible = sess.motion_possible, True
            was_open, sess.batch_open = sess.batch_open, True
            sess.wrote = True              # from here on a '2' status means the mat was unloaded
            try:
                await c.write_validated(chunk, sess.plan.validator)
            except ValueError:                 # refused by a check: this chunk never went out
                sess.running = was_running
                sess.motion_possible = was_possible
                sess.batch_open = was_open
                raise
        break
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
    return status


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
        sess.step_title, sess.batch = step.title, (0, 0)
        if step.kind == "units":
            return {**await _units_step(c, lock, sess), "seconds": round(loop.time() - t0, 1)}
        _log(f"{sess.name}: {step.title!r}: {len(step.writes)} write(s), "
             f"{sum(map(len, step.writes))} B")
        s = await _pre_step_status(c, lock, sess)
        pos = (0, 0)
        batches = _batches(step.writes, sess.profile.batch_bytes)
        for number, batch in enumerate(batches, 1):
            sess.batch = (number, len(batches))
            for chunk in batch:
                s = await _write_chunk(c, lock, sess, chunk, s)
            travel, pos = _travel(batch, pos)
            s = await wait_ready(c, lock, sess,
                                 READY_TIMEOUT + travel / sess.profile.slowest_su_per_s)
        _check_session(c, sess, link=False, media=False)   # a stop or cancel since: not ok
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
        sess.batch_open = False
        sess.step_title, sess.batch = None, (0, 0)


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


LOGGED_OPS = ("info", "esc", "query", "resume", "pause")


def _note_op(state: dict, op: object, resp: dict | None, failed: bool = False) -> None:
    """Log the failures of the one-shot ops in LOGGED_OPS, rate-limited: the first failure of
    an op, then nothing more until it works again (one line with how many failed). A failure is
    an error on the way to the machine (`failed`: the op raised, e.g. a reply time-out) or a
    resume or pause write that failed; a refusal (no link, a job runs, a query held back) is
    not one: its reply says why."""
    if op not in LOGGED_OPS or not isinstance(resp, dict):
        return
    failing = state.setdefault("op_failures", {})
    if resp.get("ok"):
        n = failing.pop(op, 0)
        if n:
            _log(f"the {op} op works again (after {n} failure{'s' if n != 1 else ''})")
        return
    if not failed and not (op in ("resume", "pause") and resp.get("sent")):
        return
    n = failing.get(op, 0)
    failing[op] = n + 1
    if n == 0:
        _log(f"the {op} op failed: {str(resp.get('error'))[:200]}")


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
    c = state.get("c")
    no_reply = age = None
    if c is not None and callable(getattr(c, "no_reply_seconds", None)):
        no_reply = c.no_reply_seconds()
    if c is not None and callable(getattr(c, "indication_age", None)):
        age = c.indication_age()
    return {"stop_owed": bool(state.get("stop_owed")),
            "owed_stops_sent": int(state.get("owed_stops_sent") or 0),
            "motion_stopped": bool(sess is not None and sess.stop.is_set()),
            "interrupted": sess.interrupted if sess is not None else None,
            "load_seen": int(state.get("load_seen") or 0),
            "paused": bool(sess is not None and _is_paused(sess)),
            "resume_ok": _command_refusal(c, state, "resume") is None,
            "pause_ok": _command_refusal(c, state, "pause") is None,
            "no_reply_s": round(no_reply, 1) if no_reply is not None else None,
            "indication_age_s": round(age, 1) if age is not None else None,
            "stop_check": dict(state["stop_check"]) if state.get("stop_check") else None}


async def _nap_link(c, seconds: float) -> None:
    """Wait `seconds`, or until the link's next status indication."""
    nxt = getattr(c, "next_indication", None)
    if nxt is None:
        await asyncio.sleep(seconds)
        return
    indication = nxt()
    try:
        await asyncio.wait({indication}, timeout=seconds)
    finally:
        indication.cancel()


async def _status_after_stop(c: Cameo4, lock: asyncio.Lock,
                             info: dict | None = None) -> str | None:
    """What ESC 05 reads after a stop: polled every STOP_CHECK_POLL s (sooner on a status
    indication), once the stop's quiet window is over (the link holds the first read until
    then), for up to STOP_CHECK s while it reads Busy or Cancelling, and up to
    STOP_CHECK_PAUSED s once it read Paused (a paused machine ends the job about 5 s after the
    stop, and Cancelling may not be shown at all). Done at any other status (Ready, Load Media).
    Each read waits at most STOP_LOCK_WAIT s for the query lock. Returns the last exact status
    read, or None. Nothing but ESC 05 is sent. A ready reply does not prove that the machine
    stopped for good (motion still queued might resume), only that it reports Ready now. Never
    raises for a read that fails (a lost link, a busy write): the stop already got through, so
    the reading just ends there. `info` gets "seconds" (how long it read), "seen" (each
    different status read, with the seconds since the start) and "reason" (why the last read
    gave no status, or None)."""
    profile = c.profile
    loop = asyncio.get_running_loop()
    start = loop.time()
    last = None
    reason = None
    paused_seen = False
    seen: list[tuple[str, float]] = []
    while True:
        if not _link_up(c):
            reason = "the link was lost"
            break
        try:
            await asyncio.wait_for(lock.acquire(), STOP_LOCK_WAIT)
        except TimeoutError:
            reason = f"the query lock was busy for {STOP_LOCK_WAIT:g} s"
            break
        try:
            reply = await c.esc_query(0x05)
        except TimeoutError:
            reply = None
            reason = f"no reply to ESC 05 within {REPLY_TIMEOUT:g} s"
        except Exception as e:  # noqa: BLE001 - the stop's reply must still say it got through
            _log(f"after the stop: ESC 05 failed ({type(e).__name__}: {e})")
            reason = f"ESC 05 failed ({type(e).__name__}: {e})"
            break
        finally:
            lock.release()
        if is_status(reply, profile):
            reason = None
            if reply != last:
                seen.append((reply, loop.time() - start))
            last = reply
        elif reply is not None:
            reason = f"the reply {reply!r} is not a status"
        term = status_text(last, profile, default="")
        paused_seen = paused_seen or term == PAUSED
        waiting = last is None or last == profile.status.busy or term in (CANCELLING, PAUSED)
        limit = STOP_CHECK_PAUSED if paused_seen else STOP_CHECK
        if not waiting or loop.time() + STOP_CHECK_POLL > start + limit:
            break
        await _nap_link(c, STOP_CHECK_POLL)
    if info is not None:
        info.update(seconds=round(loop.time() - start, 1), seen=seen,
                    reason=reason if last is None else None)
    return last


def _seen_words(seen: list[tuple[str, float]], paused: str) -> str:
    """'3 for 5.3 s, then 4, then 0' from _status_after_stop's "seen"."""
    parts = []
    for i, (status, at) in enumerate(seen):
        if status == paused and i + 1 < len(seen):
            parts.append(f"{status} for {seen[i + 1][1] - at:.1f} s")
        else:
            parts.append(status)
    return ", then ".join(parts)


async def _stop_op(req: dict, c: Cameo4, lock: asyncio.Lock, state: dict) -> dict:
    """The stop op through a usable link: with a session open its stop (forced: always sent, or
    joined while one is in flight), else the profile's stop on its own (a machine after a job,
    or one owed a stop). {"ok", "errors"}; with "check": true and a stop that got through also
    "status_after" (_status_after_stop: the last ESC 05 reply, or None; the paused digit means
    it still reported Paused STOP_CHECK_PAUSED s after the stop) and "still_busy" (it still
    reads the busy digit; None when nothing was read). The read-back is also the ping's
    "stop_check"."""
    sess = state["motion"]
    if sess is not None:
        errors = await _stop_session(c, sess, force=True)
    else:
        joined = bool(getattr(c, "stop_in_flight", False))
        errors = await c.emergency_stop()
        result = f"FAILED: {errors}" if errors else "sent"
        _log(f"joined the STOP in flight (pair {_stop_pairs(c)} on this link): {result}"
             if joined else _stop_line(c, errors))
        _stop_result(state, c, errors)
    reply = {"ok": not errors, "errors": errors}
    if req.get("check") is True:
        profile = c.profile
        info: dict = {}
        if not errors:
            state["stop_check"] = {"running": True, "status": None, "seconds": 0.0,
                                   "still_busy": None, "still_paused": None, "reason": None}
            status = await _status_after_stop(c, lock, info)
        else:
            status = None
            info["reason"] = "the stop did not get through"
        busy = None if status is None else status == profile.status.busy
        paused = None if status is None else status_text(status, profile, default="") == PAUSED
        reply.update(status_after=status, still_busy=busy)
        state["stop_check"] = {"running": False, "status": status,
                               "seconds": info.get("seconds", 0.0), "still_busy": busy,
                               "still_paused": paused, "reason": info.get("reason")}
        seen = info.get("seen") or []
        who = f"{sess.name}: " if sess is not None else ""
        paused_digit = next((st for st, _ in seen
                             if status_text(st, profile, default="") == PAUSED), None)
        if paused_digit is not None:
            _log(f"{who}cancel while paused: the machine read {_seen_words(seen, paused_digit)}")
        if status is None:
            _log(f"after the stop: the status could not be read ({info.get('reason')})")
        elif paused:
            _log(f"after the stop the machine still reports {status} = Paused "
                 f"{info.get('seconds', 0.0):.0f} s later: press Unload on the machine; if "
                 "anything moves, press its power button")
        else:
            _log(f"after the stop the machine reports {status} = {status_text(status, profile)}"
                 + (f" {STOP_CHECK:g} s later: if it moves, press its power button"
                    if busy else ""))
    return reply


# --- the user's Pause and Resume --------------------------------------------------------------
def _command_refusal(c, state: dict, kind: str) -> str | None:
    """Why the user's `kind` ("resume" or "pause") would not be sent now, in plain words, or
    None. Resume: only while the open session waits on a paused machine. Pause: only while a
    session of ours has written and is neither paused nor ending, and the machine last read
    Ready or Busy. Both: no stop asked for, the session's machine on a usable link, and only a
    machine whose command was tested (its profile has it, and the profile and firmware line are
    TESTED: the CAMEO 4 on firmware V5)."""
    sess = state.get("motion")
    if sess is None:
        return "no job is running"
    if not _usable(c) or sess.link is not c:
        return "the machine is not answering (the link is down)"
    if sess.stop.is_set():
        return "a stop was asked for: the job is ending"
    if sess.interrupted is not None or sess.media_changed:
        return "the job is ending"
    p = sess.profile
    command = p.resume if kind == "resume" else p.pause
    if command is None or p.support is not Support.TESTED \
            or state.get("support") != Support.TESTED.value:
        return f"the {p.name} has not been tested with {kind}: press its Pause key"
    last = getattr(c, "last_status", None)
    shown = f"status {last!r} = {status_text(last, p)}"
    if kind == "resume":
        if not _is_paused(sess):
            return f"the machine is not paused ({shown})"
        return None
    if not sess.wrote:
        return "the job has not started yet"
    if _is_paused(sess):
        return "the machine is already paused"
    if last not in (p.status.ready, p.status.busy):
        return f"the machine is neither ready nor busy ({shown})"
    return None


async def _command_op(kind: str, c: Cameo4, lock: asyncio.Lock, state: dict) -> dict:
    """The resume op (one ESC 08) or the pause op (one ESC 07), for the user's explicit click or
    command; never sent by OpenCutter on its own, never repeated. Refused (nothing sent) as
    _command_refusal says, and also when the query lock is not free within RESUME_LOCK_WAIT s
    or a fresh ESC 05, read under that lock, does not read Paused (resume) or Ready or Busy
    (pause). After the write the lock is held for its quiet window (link.RESUME_QUIET), then
    ESC 05 is read (every STOP_CHECK_POLL s, at most RESUME_CHECK s) until the machine left
    Paused (resume) or reached it (pause). The session then goes on, or waits, as for the
    machine's own Pause key.

    {"ok", "sent" (the write went out, or may have: a write error), "status_before",
    "status_after" (the last status read or indicated), "paused" (the session waits on a paused
    machine now), "errors" (the write's), "error" (why it was refused or failed)}."""
    def answer(ok: bool, sent: bool, error: str | None = None, errors=()) -> dict:
        reply = {"ok": ok, "sent": sent, "status_before": before,
                 "status_after": getattr(c, "last_status", None),
                 "paused": sess is not None and _is_paused(sess), "errors": list(errors)}
        if error is not None:
            reply["error"] = error
        return reply

    sess = state.get("motion")
    before = None
    refused = _command_refusal(c, state, kind)
    if refused is not None:
        return answer(False, False, refused)
    p = sess.profile
    esc = "ESC 08" if kind == "resume" else "ESC 07"
    try:
        await asyncio.wait_for(lock.acquire(), RESUME_LOCK_WAIT)
    except TimeoutError:
        return answer(False, False, "the machine is not answering (its status could not be read "
                                    f"within {RESUME_LOCK_WAIT:g} s)")
    try:
        try:
            before = await c.esc_query(0x05)
        except TimeoutError:
            return answer(False, False, "the machine is not answering (no reply to ESC 05)")
        if _replies_pending(c):                # that reply may be an earlier query's
            return answer(False, False, "the machine's status could not be read for sure (an "
                                        "earlier reply is still due); try again")
        _note_status(sess, before)
        refused = _command_refusal(c, state, kind)
        shown = f"status {before!r} = {status_text(before, p)}"
        if refused is None and kind == "resume" and status_text(before, p, default="") != PAUSED:
            refused = f"the machine is not paused ({shown})"
        if refused is None and kind == "pause" and before not in (p.status.ready,
                                                                  p.status.busy):
            refused = f"the machine is neither ready nor busy ({shown})"
        if refused is not None:
            return answer(False, False, refused)
        field_name = "resume_sent_at" if kind == "resume" else "pause_sent_at"
        earlier = getattr(sess, field_name)
        setattr(sess, field_name, _now())     # before the write: a '0' right after it counts
        try:
            await (c.resume() if kind == "resume" else c.pause())
        except UnsafeCommand as e:            # nothing was written
            setattr(sess, field_name, earlier)
            return answer(False, False, f"refused: {e}")
        except Exception as e:  # noqa: BLE001 - whether it arrived is unknown: never again
            error = f"{type(e).__name__}: {e}"
            _log(f"{sess.name}: {kind} asked: {esc} FAILED ({error}); it is not sent again")
            return answer(False, True, f"the {kind} write failed ({error}); whether it arrived "
                                       "is unknown, and it is not sent again", [error])
        _log(f"{sess.name}: {kind} asked: {esc} sent (the machine read {before})")
        await asyncio.sleep(RESUME_QUIET)      # no query in its quiet window
    finally:
        lock.release()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RESUME_CHECK
    while state.get("motion") is sess and not sess.stop.is_set() and loop.time() < deadline:
        if (kind == "resume") != _is_paused(sess):
            break                              # resumed / paused: the job's state says so
        try:
            await asyncio.wait_for(lock.acquire(), RESUME_LOCK_WAIT)
        except TimeoutError:
            break
        try:
            _note_status(sess, await c.esc_query(0x05))
        except TimeoutError:
            pass
        finally:
            lock.release()
        if (kind == "resume") != _is_paused(sess):
            break
        await _nap_link(c, STOP_CHECK_POLL)
    return answer(True, True)


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
def _query_hold(c: Cameo4, state: dict, text: bool) -> str | None:
    """Why a query other than ESC 05 is not sent now, or None: while the machine last read
    Paused or Cancelling (a paused machine answers no command; one that arrives during a cancel
    makes the cancel wait longer), and for a text query (`text`) also for
    QUERY_HOLD_AFTER_STOP s after a stop."""
    last = getattr(c, "last_status", None)
    term = status_text(last, c.profile, default="")
    if term in (PAUSED, CANCELLING):
        return (f"the machine reports {last!r} = {term}: nothing but ESC 05 is sent to it until "
                "it shows Ready or Load Media")
    stopped = state.get("last_stop")
    if text and stopped is not None and time.monotonic() - stopped < QUERY_HOLD_AFTER_STOP:
        return (f"a stop went out less than {QUERY_HOLD_AFTER_STOP:g} s ago: nothing but ESC 05 "
                "is sent to the machine until then")
    return None


async def _one_shot(req: dict, c: Cameo4 | None, lock: asyncio.Lock, state: dict) -> dict:
    """Answer ping, stop, resume, pause, esc, query and info."""
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
            _clear_pause(sess)
            if sess.motion_possible and not sess.stop_sent:
                _owe_stop(state, sess.link, "a stop was asked for while the link was down")
        if op in ("resume", "pause"):
            return {"ok": False, "sent": False, "error": "the machine is not connected"}
        return {"ok": False, "error": _no_link_error(state)}
    if op == "stop":                       # never waits for the query lock; always sends
        return await _stop_op(req, c, lock, state)
    if op in ("resume", "pause"):          # the user's click or command; never automatic
        return await _command_op(op, c, lock, state)
    if sess is not None:
        return {"ok": False, "error": "a program or job is running (use stop to stop it)"}
    if op == "esc":
        code = int(req["code"])
        hold = _query_hold(c, state, text=False) if code != 0x05 else None
        if hold:
            return {"ok": False, "error": f"refused: {hold}"}
        async with lock:
            return {"ok": True, "reply": await c.esc_query(code)}
    if op == "query":
        hold = _query_hold(c, state, text=True)
        if hold:
            return {"ok": False, "error": f"refused: {hold}"}
        async with lock:
            return {"ok": True, "reply": await c.query(str(req["cmd"]))}
    if op == "info":
        # FG is never sent here: its reply from the model gate (FG claims the machine's session,
        # and a paused machine answers no command); no ESC 15 while it is paused or cancelling
        async with lock:
            status = await c.esc_query(0x05)
            tools = None if _query_hold(c, state, text=False) else await c.esc_query(0x15)
        firmware = getattr(c, "fg_reply", None) or getattr(c, "machine", None) or ""
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
def _log_slow_write(data: bytes, seconds: float) -> None:
    """A write whose Write Response took link.SLOW_WRITE_LOG s or more."""
    shown = data.hex(" ") if data[:1] == b"\x1b" else f"a {len(data)}-byte motion write"
    _log(f"slow Write Response: {shown} after {seconds:.1f} s")


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
    # (monotonic), for the status log (STOP_WATCH) and the query hold; stop_check: the last
    # stop read-back (the ping's); op_failures: the one-shot ops failing now (_note_op)
    state: dict = {"c": None, "motion": None, "reason": None, "refused_model": None,
                   "support": None, "refused_profile": None, "refused_support": None,
                   "refused_reason": None, "sessions": 0, "stopping": False, "exit": None,
                   "last_request": time.monotonic(), "started_by": started_by(),
                   "stop_owed": set(), "owed_stops_sent": 0, "load_seen": 0, "last_stop": None,
                   "stop_check": None, "op_failures": {}}
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
        link.on_slow_write = _log_slow_write
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
                _note_op(state, req.get("op"), resp)
        except UnsafeCommand as e:
            resp = {"ok": False, "error": f"refused: {e}"}
        except Exception as e:  # report to the client, keep serving
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if isinstance(req, dict):
                _note_op(state, req.get("op"), resp, failed=True)
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
