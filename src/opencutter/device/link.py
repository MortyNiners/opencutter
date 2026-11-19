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
  * Every write has a time bound: a query write QUERY_WRITE_TIMEOUT (a reply is then owed for
    every copy, as after a reply time-out), a motion write MOTION_WRITE_TIMEOUT (TimeoutError:
    whether it arrived is unknown, so the daemon sends the stop), a stop write
    STOP_WRITE_TIMEOUT. A write that never gets its Write Response therefore cannot hold a step
    for longer than that. Cancelling a write here does not cancel it in the system's Bluetooth
    stack (BlueZ), so a stop sent after such a write may still wait behind it there.
  * Status changes (an indication, or an exact ESC 05 reply that differs from last_status) go
    to `on_status_change(old, new, source)`, for the daemon's log.
No bleak and no D-Bus import here.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..machines import CAMEO4, MachineProfile, safety
from ..machines.detect import Detection, detect
from .protocol import (ESC, ETX, PRE_GATE, REPLY_TIMEOUT, UnsafeCommand, check_safe, is_status,
                       stop_sequence)
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
_FG_QUERY = b"FG" + bytes([ETX])


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
        if self.verbose:
            debug(f"  <- reply  {bytes(data)!r}")
        self._buf += data
        while ETX in self._buf:
            i = self._buf.index(ETX)
            reply = bytes(self._buf[:i])
            del self._buf[: i + 1]
            if self._owed:           # late answer to a request that already timed out
                self._owed -= 1
                if self.verbose:
                    debug(f"  (dropped stale reply {reply!r})")
                continue
            self._replies.put_nowait(reply)

    def _on_status(self, data: bytes) -> None:
        old = self.last_status
        self.last_status = bytes(data).rstrip(b"\x03").decode("ascii", "replace")
        if self.on_status is not None:
            try:
                self.on_status(self.last_status)
            except Exception:  # noqa: BLE001 - a broken callback must not break the link
                pass
        self._status_changed(old, "indication")

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

    async def _request(self, data: bytes, timeout: float = REPLY_TIMEOUT) -> str:
        """Write one allowlisted query and return its reply, unstripped.

        Replies carry no id: late replies to timed-out requests are dropped by count in
        _on_reply, and anything already queued is skipped. (Assumes FIFO order.)"""
        check_safe(data, self._query_allowlist())
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
                await self._drop_duplicates()
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
                await asyncio.wait_for(self._raw_write(data), QUERY_WRITE_TIMEOUT)
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
                    await self._drop_duplicates()
                    raise
                if self.verbose:
                    debug(f"  ({e}; writing {data!r} again in {pause} s)")
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    async def _drop_duplicates(self) -> None:
        """After a query that was written more than once, both copies may have reached the
        machine: drop every reply that comes within DUPLICATE_WAIT."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DUPLICATE_WAIT
        while (left := deadline - loop.time()) > 0:
            try:
                extra = await asyncio.wait_for(self._replies.get(), left)
            except TimeoutError:
                return
            if self.verbose:
                debug(f"  (dropped the second reply {extra!r})")

    async def esc_query(self, code: int) -> str:
        """Allowlisted ESC query (ESC 05 before the gate; the profile's ESC queries, within 05,
        10, 15 and 16, after it); returns the exact reply. An exact ESC 05 status reply (the
        profile's; the CAMEO 4's before the gate) also updates last_status."""
        reply = await self._request(bytes([ESC, code]))
        if code == 0x05 and is_status(reply, self._profile or CAMEO4):
            old, self.last_status = self.last_status, reply
            self._status_changed(old, "ESC 05")
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
            debug(f"  -> MOTION {data!r}")
        try:
            await asyncio.wait_for(self._raw_write(data), MOTION_WRITE_TIMEOUT)
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
        so."""
        if self._stop_lock.locked():          # a stop is already being sent
            async with self._stop_lock:
                return list(self._last_stop_errors)
        async with self._stop_lock:
            try:
                profile = self._motion_profile()
                writes = stop_sequence(profile)
            except UnsafeCommand as e:
                self._last_stop_errors = [f"no stop sent: {e}"]
                return list(self._last_stop_errors)
            errors = []
            self.stop_sequences += 1
            # what a caller that joined this sequence gets if it is cancelled before its end
            # (never the result of an earlier sequence)
            self._last_stop_errors = ["the stop sequence was cut short"]
            for i, b in enumerate(writes):
                if i:
                    await asyncio.sleep(profile.stop.gap_s)
                try:
                    await asyncio.wait_for(self._raw_write(b), STOP_WRITE_TIMEOUT)
                except TimeoutError:
                    errors.append(f"{b.hex(' ')}: TimeoutError: no Write Response within "
                                  f"{STOP_WRITE_TIMEOUT:g} s")
                except Exception as e:
                    errors.append(f"{b.hex(' ')}: {type(e).__name__}: {e}")
            self._last_stop_errors = errors
            return list(errors)
