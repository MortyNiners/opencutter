# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""SafeLink: every check between OpenCutter and one machine, over a dumb RawTransport.

The transport (transport.RawTransport; ble.BleTransport for BlueZ) only moves bytes. Everything
that decides what may leave the host is here, the same for every transport:
  * Before the model gate (profile None) only ESC 05 and FG may be sent (protocol.PRE_GATE): no
    other query, no motion write and no stop. The FG reply that comes before the gate is kept
    (fg_reply, detection). Which profile the link then uses is the daemon's decision, from that
    reply (machines.detect): it sets `profile`, and setting it to None closes the gate again. A
    new connection always starts before the gate.
  * After the gate, the profile's read-only queries (protocol.check_safe with the profile),
    always within the global rules of machines.safety.
  * A motion write goes out only through write_validated(): the caller's validator, then no ESC,
    ETX at the end, the global floor of machines.safety.check_write for the profile, and at most
    min(480, profile.max_write, the transport's limit) bytes. While the transport's limit (BLE:
    MTU - 3) is below MIN_SESSION_WRITE_LIMIT, no motion write goes out at all: a session needs
    every query reply to fit in one indication.
  * The stop is the profile's: ESC 09 twice, each alone, profile.stop.gap_s apart. A caller
    that arrives while it is being sent shares it and gets its result.
  * Replies are framed by ETX. Replies carry no id: one that comes after its request timed out
    is dropped by count, and after two timeouts in a row the count is reset (a reply was
    probably lost). Their grammar is checked by the callers (protocol.is_status, parse_tools,
    evaluate_units), never stripped here.
  * A query whose write the transport reports busy (TransportBusy; BLE: ATT error 0x0E) is
    written again, at most len(BUSY_RETRY_DELAYS) times after growing pauses: a query only
    reads, so a second copy is harmless, and a second reply is dropped (when the query times
    out, a late reply is owed for every copy). A motion write or a stop
    write is never repeated: whether a busy write reached the machine is unknown, so it is an
    error for the caller (the daemon then sends the stop).
  * Every write of a one-shot query, a motion write and the stop has a time bound: a query write
    QUERY_WRITE_TIMEOUT (a reply is then owed for every copy, as after a reply time-out), a
    motion write MOTION_WRITE_TIMEOUT (TimeoutError: whether it arrived is unknown, so the daemon
    sends the stop), a stop write STOP_WRITE_TIMEOUT. Cancelling a write here does not cancel it
    in the system's Bluetooth stack (BlueZ), so a stop sent after such a write may still wait
    behind it there. Every Write Response is timed (on_slow_write hears the slow ones).
  * Inside a session the daemon asks ESC 05 and ESC 15 patiently (patient_esc): one query at a
    time, never given up while the link is up. A write without its Write Response is waited for
    (reported once after QUERY_WRITE_TIMEOUT, never written again meanwhile: a new write would
    only queue behind it); a reply that is STATUS_OVERDUE late is reported and the query asked
    once more (a reply can be lost). The wait ends only on its reply, the session's stop or a
    lost link (QueryAbandoned). no_reply_seconds() says how long it has been overdue. A patient
    query is never written once the session's stop was asked for.
  * Every query written is counted until a reply comes for it (replies_pending(); replies come
    in the order of the queries). A reply that came while earlier ones were still due may be
    the late answer to one of them, so before a ready reply may count (and the next motion
    write go out) the daemon waits for them with drain_replies(): nothing is written
    meanwhile, and the last reply is then the answer to the last query. Replies still due
    REPLY_LOST_AFTER s after the last query or reply are taken as lost.
  * Quiet windows: no query write from the stop's first byte until STOP_QUIET after its last,
    and for RESUME_QUIET after a Pause or Resume write, so that the radio core passes the
    command on before it answers anything (firmware analysis).
  * The user's explicit Pause and Resume (pause(), resume()): the profile's one ESC 07 or ESC 08
    write, alone, only on a TESTED profile on a TESTED firmware line, never repeated (a write
    error reaches the caller), never while a stop is being sent or once one went out during
    its wait for a quiet window.
  * Status changes (an indication, or an exact ESC 05 reply that differs from last_status) go
    to `on_status_change(old, new, source)`, for the daemon's log. Every status indication is
    counted and timed (last_indication_at, indication_count) and wakes next_indication().
No bleak and no D-Bus import here.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable

from ..machines import CAMEO4, MachineProfile, Support, safety
from ..machines.detect import Detection, detect
from .protocol import (ESC, ETX, PRE_GATE, REPLY_TIMEOUT, STATUS_OVERDUE, UnsafeCommand,
                       check_safe, is_status, stop_sequence)
from .transport import MIN_WRITE_LIMIT, RawTransport, TransportBusy, debug

# MTU - 3 >= 32 before any motion write, so that the reply of every allowlisted query fits
# in one indication (firmware analysis: the CAMEO 4 and Portrait 3 firmware send no reply
# longer than 32 bytes, and no allowlisted reply of the other models is longer).
MIN_SESSION_WRITE_LIMIT = 32
BUSY_RETRY_DELAYS = (0.1, 0.2, 0.4)   # pauses before a busy query write is written again
DUPLICATE_WAIT = 0.5          # after a repeated query: how long a second reply is waited for
CONNECT_TIMEOUT = 45.0
SUBSCRIBE_SETTLE = 0.5        # pause after subscribing, before the first query
STOP_WRITE_TIMEOUT = 2.0      # each stop write
# Generous bounds, not measured: a write is normally acknowledged within tens of milliseconds.
QUERY_WRITE_TIMEOUT = 5.0     # each query write's Write Response
MOTION_WRITE_TIMEOUT = 10.0   # each motion write's Write Response
MAX_STOP_GAP_S = 2.0          # a profile's gap between the stop writes is 0 < gap <= this
COMMAND_WRITE_TIMEOUT = 2.0   # the Write Response of the user's Pause or Resume write
STOP_QUIET = 0.3              # no query write from the stop's first byte until this long after
RESUME_QUIET = 0.3            # its last; and for this long after a Pause or Resume write
SLOW_WRITE_LOG = 1.0          # a Write Response slower than this goes to on_slow_write
REASK_LOG_EVERY = 60.0        # an overdue query: after the first report, one this often
# drain_replies: a reply still due this long after the last query written and the last reply
# received is taken as lost. Generous on purpose: a reply that is merely late must keep the
# drain waiting (one that came later than this could still answer a later query), while a lost
# one only costs this long a wait, with nothing sent.
REPLY_LOST_AFTER = 30.0
_FG_QUERY = b"FG" + bytes([ETX])


class QueryAbandoned(RuntimeError):
    """A patient query stopped waiting: the session's stop was asked for, or the link was lost.
    Whatever was written may still reach the machine and be answered: its reply is owed."""


def _stamp() -> str:
    """The wall clock with milliseconds, for the verbose lines ('14:19:21.204')."""
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"


def _query_name(data: bytes) -> str:
    """'ESC 05' for an ESC query, 'U' for a text query."""
    if data[:1] == bytes([ESC]):
        return f"ESC {data[1:2].hex()}"
    return data.rstrip(bytes([ETX])).decode("ascii", "replace")


class SafeLink:
    """One link to a machine over a RawTransport: queries, validated motion writes and the stop,
    each one checked here (see the module docstring). `on_status` gets every status indication
    as text."""

    # Class-level defaults, so that the checks below also hold for an object made without
    # __init__ (tests call the write guards that way): before the gate, quiet, smallest limit.
    verbose = False
    machine: str | None = None
    fg_reply: str | None = None
    _profile: MachineProfile | None = None
    _write_limit = MIN_WRITE_LIMIT
    on_status_change: Callable[[str | None, str, str], None] | None = None
    stop_sequences = 0            # stop sequences this link sent (each one ESC 09 twice)
    pause_writes = 0              # the user's Pause writes (ESC 07) this link sent
    resume_writes = 0             # the user's Resume writes (ESC 08) this link sent
    # what each write of the last stop sequence did ('write 1 acknowledged after 0.03 s; ...')
    last_stop_report = ""
    # called with (data, seconds) for a write whose Write Response took SLOW_WRITE_LOG or longer
    on_slow_write: Callable[[bytes, float], None] | None = None
    # time.monotonic() of the last status indication, reply and Write Response (None: none yet)
    last_indication_at: float | None = None
    last_reply_at: float | None = None
    last_write_ack_at: float | None = None
    indication_count = 0          # status indications on this link
    dropped_replies = 0           # late replies dropped by count (_owed)
    last_reply_text: str | None = None    # the last reply received (as text), dropped or not
    # time.monotonic() of each query written whose reply has not come yet, oldest first
    _asked: deque | None = None
    _quiet_until = 0.0            # loop time until which no query is written (the quiet windows)
    _waiting_since: float | None = None   # time.monotonic(): a patient query unanswered since

    def __init__(self, transport: RawTransport, verbose: bool = False,
                 on_status: Callable[[str], None] | None = None,
                 on_status_change: Callable[[str | None, str, str], None] | None = None):
        self.transport = transport
        self.device = transport.ref           # .name and .address, for logs and the ping
        self.verbose = verbose
        self.on_status = on_status
        # called with (old, new, source) when last_status changes; source is "indication" or
        # "ESC 05"
        self.on_status_change = on_status_change
        self.stop_sequences = 0
        self.last_status: str | None = None   # from status indications and ESC 05 replies
        self._profile = None
        # The FG reply this link received before the gate (as query() returned it), or None.
        self.fg_reply = None
        # The machine's model as people may see it (protocol.model_text of its FG reply), set by
        # the daemon only once this link passed the model gate. None: not checked, not usable.
        self.machine = None
        self._subscribed = False
        self._replies: asyncio.Queue[bytes] = asyncio.Queue()
        self._buf = bytearray()
        self._owed = 0            # replies still due to earlier requests that timed out
        self._timeouts_in_row = 0
        self._stop_lock = asyncio.Lock()
        self._last_stop_errors: list[str] = []
        self._write_limit = MIN_WRITE_LIMIT
        self.pause_writes = self.resume_writes = 0
        self.last_stop_report = ""
        self.on_slow_write = None
        self.last_indication_at = self.last_reply_at = self.last_write_ack_at = None
        self.indication_count = 0
        self.dropped_replies = 0
        self._quiet_until = 0.0
        self._waiting_since = None
        self._query_sent_at: float | None = None
        self._indication_waiters: set[asyncio.Future] = set()
        self._asked = deque()
        self.last_reply_text = None

    # the model gate -----------------------------------------------------------------------
    @property
    def profile(self) -> MachineProfile | None:
        """The machine profile this link uses, set by the daemon once the FG reply on this link
        named a model it drives; None before the gate (only ESC 05 and FG may be sent)."""
        return self._profile

    @profile.setter
    def profile(self, profile: MachineProfile | None) -> None:
        """Open the gate with `profile`, or close it with None. Raises UnsafeCommand, and the
        link stays as it was, when no FG reply came on this link, or for anything but a
        MachineProfile that passes machines.safety.check_profile (the registry's self-check,
        so a profile made anywhere else is held to the same rules), that jobs may use, and
        whose stop is ESC 09 twice with a gap of at most MAX_STOP_GAP_S (so that a bad stop is
        refused before anything moves)."""
        if profile is not None:
            if not isinstance(profile, MachineProfile):
                raise UnsafeCommand(f"not a machine profile: {profile!r}")
            if self.fg_reply is None:
                raise UnsafeCommand("the model gate did not run on this link: no FG reply came")
            try:
                safety.check_profile(profile)
            except safety.SafetyError as e:
                raise UnsafeCommand(str(e)) from None
            if not profile.support.jobs_allowed:
                raise UnsafeCommand(f"OpenCutter does not drive the {profile.name}")
            stop_sequence(profile)
            gap = profile.stop.gap_s
            if not (type(gap) in (int, float) and 0 < gap <= MAX_STOP_GAP_S):
                raise UnsafeCommand(f"the stop gap of the {profile.name} is not 0..2 s: {gap!r}")
        self._profile = profile

    @property
    def detection(self) -> Detection | None:
        """machines.detect of this link's FG reply, or None before one came."""
        return None if self.fg_reply is None else detect(self.fg_reply)

    def use_detected_profile(self) -> MachineProfile:
        """Open the gate with the profile that this link's FG reply names (machines.detect:
        verdict USE, TESTED or UNTESTED) and return it. Raises UnsafeCommand when it names none."""
        det = self.detection
        if det is None or det.profile is None:
            raise UnsafeCommand("the FG reply on this link names no model OpenCutter drives")
        self.profile = det.profile
        return det.profile

    def _query_allowlist(self):
        """What check_safe() applies now: PRE_GATE before the gate, then the profile."""
        return PRE_GATE if self._profile is None else self._profile

    def _motion_profile(self) -> MachineProfile:
        """The profile a motion write or the stop is checked with; UnsafeCommand before the gate."""
        if self._profile is None:
            raise UnsafeCommand("nothing but ESC 05 and FG is sent before the model gate")
        return self._profile

    # connection ---------------------------------------------------------------------------
    def _new_connection(self) -> None:
        """A new connection starts before the gate, with a reply stream of its own."""
        self._profile = None
        self.fg_reply = None
        self.machine = None
        self._subscribed = False
        self._buf.clear()
        while not self._replies.empty():
            self._replies.get_nowait()
        self._owed = 0
        self._timeouts_in_row = 0
        self._quiet_until = 0.0
        self._waiting_since = None
        if self._asked is not None:
            self._asked.clear()
        self.last_reply_text = None

    async def open(self) -> SafeLink:
        """Idempotent: connect if needed, subscribe to replies and status if needed."""
        t = self.transport
        if not t.is_up():
            self._new_connection()
            await t.connect(CONNECT_TIMEOUT)
        if not self._subscribed:
            await t.subscribe(self._on_reply, self._on_status if t.has_status_channel else None)
            self._subscribed = True
            await asyncio.sleep(SUBSCRIBE_SETTLE)
        return self

    async def close(self) -> None:
        """Unsubscribe; leaves the connection to the system (BLE: bluetoothd drops it ~2 s after
        we exit)."""
        try:
            await self.transport.unsubscribe()
        except Exception:  # noqa: BLE001 - closing never fails
            pass
        self._subscribed = False

    async def release(self) -> None:
        """Free the resources of a connection that is already gone. Never drops a live one."""
        await self.transport.release()

    async def disconnect(self) -> None:
        """Drop the connection on purpose (the daemon is exiting), so that the machine is free
        for other programs at once. Nothing is written to the machine: no command, no stop."""
        self._subscribed = False
        await self.transport.disconnect()

    async def __aenter__(self) -> SafeLink:
        return await self.open()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    @property
    def disconnected(self) -> asyncio.Event:
        """Set when the connection is lost."""
        return self.transport.disconnected

    @property
    def is_connected(self) -> bool:
        """True while the connection is up."""
        return self.transport.is_up() and not self.transport.disconnected.is_set()

    def transport_up(self) -> bool:
        """True while the transport reports the connection up (also during the gate)."""
        return self.transport.is_up()

    # indications --------------------------------------------------------------------------
    def _on_reply(self, data: bytes) -> None:
        self.last_reply_at = time.monotonic()
        if self.verbose:
            sent = getattr(self, "_query_sent_at", None)
            age = f", {self.last_reply_at - sent:.2f} s after the last query" if sent else ""
            debug(f"{_stamp()}   <- reply  {bytes(data)!r}{age}")
        self._buf += data
        while ETX in self._buf:
            i = self._buf.index(ETX)
            reply = bytes(self._buf[:i])
            del self._buf[: i + 1]
            self.last_reply_text = reply.decode("ascii", "replace")
            if self._asked:          # it answers the oldest query still due
                self._asked.popleft()
            if self._owed:           # late answer to a request that already timed out
                self._owed -= 1
                self.dropped_replies += 1
                if self.verbose:
                    debug(f"{_stamp()}   (dropped late reply {reply!r})")
                continue
            self._replies.put_nowait(reply)

    def _on_status(self, data: bytes) -> None:
        old = self.last_status
        self.last_status = bytes(data).rstrip(b"\x03").decode("ascii", "replace")
        self.last_indication_at = time.monotonic()
        self.indication_count += 1
        if self.verbose:
            debug(f"{_stamp()}   <- status {self.last_status!r}")
        if self.on_status is not None:
            try:
                self.on_status(self.last_status)
            except Exception:  # noqa: BLE001 - a broken callback must not break the link
                pass
        self._status_changed(old, "indication")
        waiters = getattr(self, "_indication_waiters", None)
        if waiters:
            for fut in list(waiters):
                if not fut.done():
                    fut.set_result(self.last_status)
            waiters.clear()

    def next_indication(self) -> asyncio.Future:
        """A future that gets the next status indication (its text). The caller cancels it when
        it stops waiting."""
        fut = asyncio.get_running_loop().create_future()
        self._indication_waiters.add(fut)
        fut.add_done_callback(self._indication_waiters.discard)
        return fut

    def indications_since(self, mark: int, now: float | None = None) -> str:
        """'3 status indications since, last '1' 1.2 s ago' (or 'no status indication since')
        for the indications after indication_count was `mark`: whether the machine still
        reports its status tells a stalled reply apart from a dead link."""
        n = self.indication_count - mark
        if n <= 0 or self.last_indication_at is None:
            return "no status indication since"
        age = (time.monotonic() if now is None else now) - self.last_indication_at
        return (f"{n} status indication{'s' if n != 1 else ''} since, last "
                f"{self.last_status!r} {age:.1f} s ago")

    def indication_age(self) -> float | None:
        """Seconds since the last status indication on this link, or None."""
        if self.last_indication_at is None:
            return None
        return max(0.0, time.monotonic() - self.last_indication_at)

    def no_reply_seconds(self) -> float | None:
        """How long the query a session waits for has been unanswered, once that is
        STATUS_OVERDUE s or more (the app's "Checking connection"); None otherwise."""
        since = self._waiting_since
        if since is None:
            return None
        waited = time.monotonic() - since
        return waited if waited >= STATUS_OVERDUE else None

    def _status_changed(self, old: str | None, source: str) -> None:
        """Tell on_status_change when last_status differs from `old`. Never raises."""
        new = self.last_status
        if new == old or new is None or self.on_status_change is None:
            return
        try:
            self.on_status_change(old, new, source)
        except Exception:  # noqa: BLE001 - a broken callback must not break the link
            pass

    # queries ------------------------------------------------------------------------------
    async def _raw_write(self, data: bytes) -> None:
        """One write through the transport (Cameo4 writes through its bleak client)."""
        await self.transport.write(data)

    async def _timed_write(self, data: bytes) -> float:
        """_raw_write, timed: returns the seconds until the Write Response. A slow one
        (SLOW_WRITE_LOG or more) goes to on_slow_write; with `verbose` every one is printed."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await self._raw_write(data)
        took = loop.time() - t0
        self.last_write_ack_at = time.monotonic()
        if self.verbose:
            debug(f"{_stamp()}   (Write Response to {data[:24]!r} after {took:.3f} s)")
        if took >= SLOW_WRITE_LOG and self.on_slow_write is not None:
            try:
                self.on_slow_write(data, took)
            except Exception:  # noqa: BLE001 - a broken callback must not break the link
                pass
        return took

    async def _wait_quiet(self, stop: asyncio.Event | None = None) -> None:
        """Wait until no quiet window holds query writes (the stop's, a Pause's or a
        Resume's). With `stop`: QueryAbandoned once it is set (also when no window holds)."""
        loop = asyncio.get_running_loop()
        while True:
            if stop is not None and stop.is_set():
                raise QueryAbandoned("a stop was asked for")
            left = self._quiet_until - loop.time()
            if left <= 0:
                return
            await asyncio.sleep(min(left, 0.05))

    def _note_asked(self) -> None:
        """A query is being written: its reply is due (replies_pending)."""
        if self._asked is None:
            self._asked = deque()
        self._asked.append(time.monotonic())

    def _forget_asked(self, n: int) -> None:
        """The last `n` queries written will not be answered (copies of a busy write whose
        second reply did not come): they are no longer due."""
        while n > 0 and self._asked:
            self._asked.pop()
            n -= 1

    def replies_pending(self) -> int:
        """How many queries written on this connection still wait for their reply (replies come
        in the order of the queries; drain_replies forgets the ones taken as lost)."""
        return len(self._asked) if self._asked else 0

    async def drain_replies(self, stop: asyncio.Event | None = None) -> bool:
        """Wait, writing nothing, until every query written has been answered; the replies
        that come meanwhile, and any left in the queue, are dropped (last_reply_text keeps the
        last one). True when they all came: the last reply is then the answer to the last query
        written. False when the replies still due were taken as lost (none came for
        REPLY_LOST_AFTER s since the last query or reply). Either way nothing is owed or queued
        afterwards. QueryAbandoned when `stop` is set or the link is lost first. The caller
        holds the query lock."""
        answered = True
        mine = self._waiting_since is None
        if mine:
            self._waiting_since = time.monotonic()
        try:
            while self._asked:
                last = max(self._asked[-1], self.last_reply_at or 0.0)
                left = REPLY_LOST_AFTER - (time.monotonic() - last)
                if left <= 0:
                    self._asked.clear()
                    answered = False
                    break
                get = asyncio.ensure_future(self._replies.get())
                try:
                    done = await self._until(get, stop, min(left, 0.1))
                except QueryAbandoned:
                    get.cancel()
                    raise
                if not done:
                    get.cancel()
                    continue
                self.dropped_replies += 1
                if self.verbose:
                    debug(f"{_stamp()}   (dropped reply {get.result()!r}: an earlier query's)")
            while not self._replies.empty():
                self._replies.get_nowait()
                self.dropped_replies += 1
            self._owed = 0
            self._timeouts_in_row = 0
            return answered
        finally:
            if mine:
                self._waiting_since = None

    async def _query_write(self, data: bytes, stop: asyncio.Event | None = None,
                           begun: list | None = None) -> float:
        """_timed_write of a query, counted as due (replies_pending) once it begins. With
        `stop` (a patient query, written from a task of its own): nothing is written once it
        is set, nor during a quiet window (waited out first); QueryAbandoned instead.
        `begun` gets True appended when the write begins."""
        if stop is not None:
            await self._wait_quiet(stop)
            if self.stop_in_flight:
                raise QueryAbandoned("a stop is being sent")
        self._note_asked()
        if begun is not None:
            begun.append(True)
        return await self._timed_write(data)

    async def _request(self, data: bytes, timeout: float = REPLY_TIMEOUT) -> str:
        """Write one allowlisted query and return its reply, unstripped (the one-shot path:
        `timeout` for the reply, QUERY_WRITE_TIMEOUT for the write, then TimeoutError).

        Replies carry no id: late replies to timed-out requests are dropped by count in
        _on_reply, and anything already queued is skipped. (Assumes FIFO order.)"""
        check_safe(data, self._query_allowlist())
        await self._wait_quiet()
        stale = self._replies.qsize()
        if self.verbose:
            debug(f"  -> write  {data!r}" + (f"  (skipping {stale} stale)" if stale else ""))
        copies = await self._write_query(data)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                reply = await asyncio.wait_for(self._replies.get(), max(0.0, deadline - loop.time()))
            except TimeoutError:
                # every copy written may still be answered (a busy write may have reached the
                # machine): owing too many only costs a timeout, owing too few lets a late
                # reply answer a later query
                self._owed += copies
                self._timeouts_in_row += 1
                if self._timeouts_in_row >= 2:   # a reply was probably lost: resync
                    self._owed = 0
                raise TimeoutError(f"no reply to {data!r} within {timeout:.0f} s") from None
            if stale:
                stale -= 1
                continue
            self._timeouts_in_row = 0
            if copies > 1:
                await self._drop_duplicates(copies - 1)
            return reply.decode("ascii", "replace")

    async def _write_query(self, data: bytes) -> int:
        """Write a checked query; on TransportBusy write it again after each pause of
        BUSY_RETRY_DELAYS. Returns how many times it was written (each copy may be answered).
        When it stays busy, the replies that still come are dropped and TransportBusy is
        raised. A write without a Write Response within QUERY_WRITE_TIMEOUT raises
        TimeoutError; a reply is then owed for every copy written (it may still reach the
        machine and be answered), as after a reply time-out."""
        for attempt, pause in enumerate((*BUSY_RETRY_DELAYS, None)):
            try:
                self._query_sent_at = time.monotonic()
                await asyncio.wait_for(self._query_write(data), QUERY_WRITE_TIMEOUT)
                return attempt + 1
            except TimeoutError:
                self._owed += attempt + 1
                self._timeouts_in_row += 1
                if self._timeouts_in_row >= 2:   # as in _request: resync
                    self._owed = 0
                raise TimeoutError(f"no Write Response to {data!r} within "
                                   f"{QUERY_WRITE_TIMEOUT:g} s") from None
            except TransportBusy as e:
                if pause is None:
                    await self._drop_duplicates(attempt + 1)
                    raise
                if self.verbose:
                    debug(f"  ({e}; writing {data!r} again in {pause} s)")
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    async def _drop_duplicates(self, extra: int = 0) -> None:
        """After a query that was written more than once, both copies may have reached the
        machine: drop every reply that comes within DUPLICATE_WAIT. Of the `extra` copies
        whose reply may still come, those that got none by then are no longer due."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DUPLICATE_WAIT
        while (left := deadline - loop.time()) > 0:
            try:
                dup = await asyncio.wait_for(self._replies.get(), left)
            except TimeoutError:
                break
            extra -= 1
            if self.verbose:
                debug(f"  (dropped the second reply {dup!r})")
        self._forget_asked(extra)

    async def esc_query(self, code: int) -> str:
        """Allowlisted ESC query (ESC 05 before the gate; the profile's ESC queries, within 05,
        10, 15 and 16, after it); returns the exact reply. An exact ESC 05 status reply (the
        profile's; the CAMEO 4's before the gate) also updates last_status."""
        reply = await self._request(bytes([ESC, code]))
        self._note_esc_reply(code, reply)
        return reply

    def _note_esc_reply(self, code: int, reply: str) -> None:
        if code == 0x05 and is_status(reply, self._profile or CAMEO4):
            old, self.last_status = self.last_status, reply
            self._status_changed(old, "ESC 05")

    async def patient_esc(self, code: int, stop: asyncio.Event | None = None,
                          report: Callable[[str], None] | None = None) -> tuple[str, bool]:
        """An allowlisted ESC query (ESC 05 or ESC 15 in a session) asked patiently: never given
        up while the link is up. Returns (exact reply, fresh). `fresh` is True when the reply
        answers this very query and nothing else could have been outstanding: the query was
        written once and no earlier reply was owed or lost. A ready reply that is not fresh is
        read again by the caller (a reply that is very late could answer a later query).

        The write is waited for until its Write Response comes, however long that takes: after
        QUERY_WRITE_TIMEOUT `report` hears it once, and it is never written again meanwhile. Its
        reply is waited for until STATUS_OVERDUE s after the write (at least REPLY_TIMEOUT after
        the Write Response); then `report` hears it (once, then every REASK_LOG_EVERY s), the
        reply is owed (it may still come) and the query is asked once more. TransportBusy is
        handled as in the one-shot path. Raises QueryAbandoned when `stop` is set (then nothing
        more is written, checked again right before each write) or the link is lost, and a
        write error as it is. An exact ESC 05 status reply also updates last_status."""
        data = bytes([ESC, code])
        check_safe(data, self._query_allowlist())
        loop = asyncio.get_running_loop()
        name = _query_name(data)
        fresh = self._owed == 0 and self._timeouts_in_row == 0
        began = loop.time()
        mark = self.indication_count
        dropped = self.dropped_replies
        queries = 0
        reported_at: float | None = None
        try:
            while True:
                await self._wait_quiet(stop)
                stale = self._replies.qsize()
                started = loop.time()
                if self._waiting_since is None:
                    self._waiting_since = time.monotonic()
                queries += 1
                copies, ack, stalled = await self._patient_write(data, stop, report, name, mark)
                if stalled:
                    reported_at = reported_at or loop.time()
                deadline = max(started + STATUS_OVERDUE, loop.time() + REPLY_TIMEOUT)
                try:
                    reply = await self._patient_reply(deadline, stale, stop)
                except QueryAbandoned:
                    self._owe(copies)
                    raise
                if reply is not None:
                    self._timeouts_in_row = 0
                    if copies > 1:
                        await self._drop_duplicates(copies - 1)
                    if reported_at is not None and report is not None:
                        n = self.dropped_replies - dropped
                        report(f"{name} answered again after {loop.time() - began:.1f} s "
                               f"({queries} quer{'y' if queries == 1 else 'ies'}, {n} late "
                               f"repl{'y' if n == 1 else 'ies'} dropped)")
                    text = reply.decode("ascii", "replace")
                    self._note_esc_reply(code, text)
                    return text, fresh and queries == 1
                self._owe(copies)              # overdue: its reply may still come
                fresh = False
                now = loop.time()
                if report is not None and (reported_at is None
                                           or now - reported_at >= REASK_LOG_EVERY):
                    report(f"{name} not answered for {now - began:.0f} s (write acknowledged "
                           f"after {ack:.2f} s; {self.indications_since(mark)}); asking again, "
                           "nothing new is sent")
                    reported_at = now
        finally:
            self._waiting_since = None

    def _owe(self, copies: int) -> None:
        """A request whose reply did not come in time: owe a reply for every copy written (as
        in _request); after two such in a row a reply was probably lost: resync."""
        self._owed += copies
        self._timeouts_in_row += 1
        if self._timeouts_in_row >= 2:
            self._owed = 0

    async def _until(self, task: asyncio.Future, stop: asyncio.Event | None,
                     timeout: float | None) -> bool:
        """Wait for `task`: True once it is done, False after `timeout` s (None: no limit).
        QueryAbandoned when `stop` is set or the link is lost first."""
        extra = [asyncio.ensure_future(self.transport.disconnected.wait())]
        if stop is not None:
            extra.append(asyncio.ensure_future(stop.wait()))
        try:
            done, _ = await asyncio.wait({task, *extra}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in extra:
                t.cancel()
        if task in done:
            return True
        if stop is not None and stop.is_set():
            raise QueryAbandoned("a stop was asked for")
        if self.transport.disconnected.is_set() or not self.transport.is_up():
            raise QueryAbandoned("the link was lost")
        return False

    async def _patient_write(self, data: bytes, stop: asyncio.Event | None,
                             report: Callable[[str], None] | None, name: str,
                             mark: int) -> tuple[int, float, bool]:
        """Write a query once and wait for its Write Response for as long as it takes
        (reported once after QUERY_WRITE_TIMEOUT); on TransportBusy write it again after each
        pause of BUSY_RETRY_DELAYS. Returns (copies written, seconds to the Write Response,
        whether the wait was reported). QueryAbandoned (a reply is then owed for every copy)
        or the write's own error."""
        loop = asyncio.get_running_loop()
        for attempt, pause in enumerate((*BUSY_RETRY_DELAYS, None)):
            t0 = loop.time()
            self._query_sent_at = time.monotonic()
            begun: list = []
            # a task of its own: the stop and the quiet window are checked again in it, right
            # before the write (QueryAbandoned without writing)
            task = asyncio.ensure_future(self._query_write(data, stop, begun))
            reported = False
            try:
                while not await self._until(task, stop, None if reported else
                                            max(0.0, QUERY_WRITE_TIMEOUT - (loop.time() - t0))):
                    if report is not None:
                        report(f"{name} write not acknowledged after {QUERY_WRITE_TIMEOUT:g} s "
                               f"(Write Response missing; {self.indications_since(mark)}); "
                               "waiting for it")
                    reported = True
            except QueryAbandoned:
                task.cancel()
                if attempt + len(begun):       # every copy written may still be answered
                    self._owe(attempt + len(begun))
                raise
            try:
                task.result()
                return attempt + 1, loop.time() - t0, reported
            except QueryAbandoned:
                if attempt:                    # this copy was not written; earlier ones were
                    self._owe(attempt)
                raise
            except TransportBusy as e:
                if pause is None:
                    await self._drop_duplicates(attempt + 1)
                    raise
                if self.verbose:
                    debug(f"  ({e}; writing {data!r} again in {pause} s)")
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    async def _patient_reply(self, deadline: float, stale: int,
                             stop: asyncio.Event | None) -> bytes | None:
        """The next reply that is not one of `stale` (queued before the write), or None at
        `deadline` (loop time). QueryAbandoned on a stop or a lost link."""
        loop = asyncio.get_running_loop()
        while True:
            left = deadline - loop.time()
            if left <= 0:
                return None
            get = asyncio.ensure_future(self._replies.get())
            try:
                done = await self._until(get, stop, left)
            except QueryAbandoned:
                get.cancel()
                raise
            if not done:
                get.cancel()
                return None
            reply = get.result()
            if stale:
                stale -= 1
                continue
            return reply

    async def query(self, cmd: str, timeout: float = REPLY_TIMEOUT) -> str:
        """Allowlisted text query such as 'FG'; ETX is appended. Before the gate the FG reply is
        kept in fg_reply. `timeout`: how long the reply is waited for."""
        data = cmd.encode("ascii", "strict") + bytes([ETX])
        reply = await self._request(data, timeout)
        if data == _FG_QUERY and self._profile is None:
            self.fg_reply = reply
        return reply

    # motion -------------------------------------------------------------------------------
    def write_limit(self) -> int:
        """The transport's limit as last measured by refresh_write_limit() (BLE: MTU - 3).
        Anything longer becomes a prepared/long write, which the machine forwards wrongly, so it
        is refused. Fails closed at MIN_WRITE_LIMIT until measured."""
        return self._write_limit

    async def refresh_write_limit(self) -> int:
        """Measure the transport's limit (transport.max_write()) and return it; any failure
        keeps MIN_WRITE_LIMIT. Nothing is sent to the machine."""
        limit = MIN_WRITE_LIMIT
        try:
            measured = await self.transport.max_write()
            if type(measured) is int and measured > 0:
                limit = measured
        except Exception as e:  # noqa: BLE001 - a failed measurement keeps the safe minimum
            if self.verbose:
                debug(f"  (write limit: {type(e).__name__}: {e}; limit {limit})")
        self._write_limit = limit
        return limit

    def motion_limit(self) -> int:
        """The longest motion write this link sends now: min(480, profile.max_write,
        write_limit()); 0 before the gate and while write_limit() is below
        MIN_SESSION_WRITE_LIMIT."""
        link = self.write_limit()
        if self._profile is None or link < MIN_SESSION_WRITE_LIMIT:
            return 0
        return min(safety.FIRMWARE_WRITE_CAP, self._profile.max_write, link)

    async def write_validated(self, data: bytes, validator: Callable[[bytes], None]) -> None:
        """Write one motion chunk (no reply expected) after `validator(data)` accepted it.

        The validator raises on anything unsafe. Independently of it, the link refuses (with
        UnsafeCommand: nothing was sent) a write before the gate, ESC bytes, empty writes,
        writes without ETX at the end, anything machines.safety.check_write refuses for the
        profile, and writes longer than min(480, profile.max_write, write_limit()); and every
        motion write while write_limit() is below MIN_SESSION_WRITE_LIMIT. The write is never
        repeated: TransportBusy reaches the caller, and so does TimeoutError when the write got
        no Write Response within MOTION_WRITE_TIMEOUT (whether it arrived is then unknown)."""
        validator(data)
        if type(data) is not bytes or not data or bytes([ESC]) in data or data[-1] != ETX:
            raise UnsafeCommand("a motion write must be ETX-terminated GPGL without ESC")
        profile = self._motion_profile()
        link = self.write_limit()
        if link < MIN_SESSION_WRITE_LIMIT:
            raise UnsafeCommand(f"the write limit {link} (MTU - 3) is below the "
                                f"{MIN_SESSION_WRITE_LIMIT} bytes a session needs")
        limit = min(safety.FIRMWARE_WRITE_CAP, profile.max_write, link)
        if len(data) > limit:
            raise UnsafeCommand(f"motion write of {len(data)} bytes exceeds the write limit "
                                f"{limit}")
        try:
            safety.check_write(data, profile)
        except safety.SafetyError as e:
            raise UnsafeCommand(str(e)) from None
        if self.verbose:
            debug(f"{_stamp()}   -> MOTION {data!r}")
        try:
            await asyncio.wait_for(self._timed_write(data), MOTION_WRITE_TIMEOUT)
        except TimeoutError:
            raise TimeoutError(f"the machine did not acknowledge a write within "
                               f"{MOTION_WRITE_TIMEOUT:g} s; whether it arrived is "
                               "unknown") from None

    @property
    def stop_in_flight(self) -> bool:
        """True while a stop sequence is being sent: an emergency_stop() call now joins it."""
        lock = getattr(self, "_stop_lock", None)
        return lock is not None and lock.locked()

    async def emergency_stop(self) -> list[str]:
        """Send the profile's stop (protocol.stop_sequence: ESC 09 twice, each alone), with
        profile.stop.gap_s seconds between the two writes (0.3 s on every model today).
        Returns the write errors ([] = every byte was acknowledged).

        Takes no query lock: it must not wait for a query in flight. A caller arriving while a
        sequence is being sent shares it and gets its result (stop_in_flight says beforehand
        whether a call would). stop_sequences counts the sequences this link sent. Before the
        gate nothing is sent (nothing that moves went through this link), and the result says
        so. No query is written from the first stop write until STOP_QUIET s after the last
        (the quiet window); last_stop_report says what each write did, with its time."""
        if self._stop_lock.locked():          # a stop is already being sent
            async with self._stop_lock:
                return list(self._last_stop_errors)
        async with self._stop_lock:
            try:
                profile = self._motion_profile()
                writes = stop_sequence(profile)
            except UnsafeCommand as e:
                self._last_stop_errors = [f"no stop sent: {e}"]
                self.last_stop_report = "nothing written"
                return list(self._last_stop_errors)
            errors = []
            report = []
            self.stop_sequences += 1
            # what a caller that joined this sequence gets if it is cancelled before its end
            # (never the result of an earlier sequence)
            self._last_stop_errors = ["the stop sequence was cut short"]
            self.last_stop_report = "cut short"
            loop = asyncio.get_running_loop()
            self._quiet_until = math.inf           # no query write until the quiet window ends
            try:
                for i, b in enumerate(writes):
                    if i:
                        await asyncio.sleep(profile.stop.gap_s)
                    t0 = loop.time()
                    try:
                        await asyncio.wait_for(self._timed_write(b), STOP_WRITE_TIMEOUT)
                        report.append(f"write {i + 1} acknowledged after {loop.time() - t0:.2f} s")
                    except TimeoutError:
                        errors.append(f"{b.hex(' ')}: TimeoutError: no Write Response within "
                                      f"{STOP_WRITE_TIMEOUT:g} s")
                        report.append(f"write {i + 1} no Write Response within "
                                      f"{STOP_WRITE_TIMEOUT:g} s")
                    except Exception as e:
                        errors.append(f"{b.hex(' ')}: {type(e).__name__}: {e}")
                        report.append(f"write {i + 1} failed after {loop.time() - t0:.2f} s "
                                      f"({type(e).__name__}: {e})")
            finally:
                self._quiet_until = loop.time() + STOP_QUIET
            self._last_stop_errors = errors
            self.last_stop_report = "; ".join(report)
            return list(errors)

    # the user's Pause and Resume ----------------------------------------------------------
    async def pause(self) -> float:
        """Write the profile's Pause (ESC 07) once, for the user's explicit Pause. Returns the
        seconds until its Write Response. See _command for what is refused."""
        return await self._command("pause")

    async def resume(self) -> float:
        """Write the profile's Resume (ESC 08) once, for the user's explicit Resume. Returns
        the seconds until its Write Response. See _command for what is refused."""
        return await self._command("resume")

    async def _command(self, kind: str) -> float:
        """One write of the profile's `kind` ("pause" or "resume"), alone, never repeated.

        Refused with UnsafeCommand (nothing was sent): before the gate, on a profile without
        that exact write (MachineProfile.pause, resume: ESC 07 or ESC 08 alone), on a profile
        or firmware line that is not TESTED (the link's FG reply, machines.detect), while a
        stop is being sent, and when one was sent while a quiet window was waited out.
        TimeoutError when no Write Response came within
        COMMAND_WRITE_TIMEOUT, and TransportBusy or any other write error as it is: whether
        it arrived is then unknown, and it is not written again. No query is written for
        RESUME_QUIET s after it (the quiet window)."""
        profile = self._motion_profile()
        data = profile.pause if kind == "pause" else profile.resume
        allowed = safety.is_pause_write if kind == "pause" else safety.is_resume_write
        if data is None or not allowed(data):
            raise UnsafeCommand(f"the {profile.name} has no {kind} command")
        det = self.detection
        if profile.support is not Support.TESTED or det is None or not det.tested:
            raise UnsafeCommand(f"the {kind} command has not been tested on this machine")
        if self.stop_in_flight:
            raise UnsafeCommand(f"a stop is being sent; no {kind} is sent")
        sequences = self.stop_sequences
        await self._wait_quiet()
        if self.stop_in_flight or self.stop_sequences != sequences:
            raise UnsafeCommand(f"a stop was sent meanwhile; no {kind} is sent")
        loop = asyncio.get_running_loop()
        if kind == "pause":
            self.pause_writes += 1
        else:
            self.resume_writes += 1
        if self.verbose:
            debug(f"{_stamp()}   -> {kind.upper()} {data!r}")
        try:
            return await asyncio.wait_for(self._timed_write(data), COMMAND_WRITE_TIMEOUT)
        except TimeoutError:
            raise TimeoutError(f"no Write Response within {COMMAND_WRITE_TIMEOUT:g} s; whether "
                               "it arrived is unknown") from None
        finally:
            self._quiet_until = max(self._quiet_until, loop.time() + RESUME_QUIET)
