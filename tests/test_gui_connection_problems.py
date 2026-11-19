# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Why the app could not connect, in plain words: from the daemon's ping reason and from the
last line a daemon that exited at start-up wrote. No display, no daemon, no Bluetooth: the
start-up messages are the daemon's own, raised against private temp paths."""
import asyncio
import fcntl
import os

import pytest

import stubs

stubs.install()
from opencutter.device import daemon  # noqa: E402
from opencutter.device.protocol import (REASON_HELD_ELSEWHERE, REASON_NO_BLUETOOTH,  # noqa: E402
                                        REASON_NOT_PAIRED, REASON_UNREACHABLE)
from opencutter.gui import machine  # noqa: E402
from opencutter.gui.connection import COULD_NOT_CONNECT, failure_markup, failure_text  # noqa: E402
from opencutter.gui.machine import (ANOTHER_PROGRAM, ANOTHER_PROGRAM_AT_START,  # noqa: E402
                                    CONNECT_GRACE_SECONDS, NO_BLUETOOTH, NOT_PAIRED, SERVICE_BUSY,
                                    SERVICE_NOT_STARTED, SWITCH_OFF_ON, Link, MachineMonitor, pill)
from gui_fakes import READY_PING, FakeDaemonClient  # noqa: E402
from stubs import harness  # noqa: E402,F401

WAITING = {"ok": True, "connected": False, "status": None, "motion": None, "device": None}


def test_the_ping_reasons_in_plain_words():
    assert machine.reason_problem(REASON_HELD_ELSEWHERE) == ANOTHER_PROGRAM
    assert machine.reason_problem(REASON_NOT_PAIRED) == NOT_PAIRED
    assert machine.reason_problem(REASON_UNREACHABLE) == SWITCH_OFF_ON
    assert machine.reason_problem(REASON_NO_BLUETOOTH) == NO_BLUETOOTH
    assert NO_BLUETOOTH == ("Bluetooth is off or not available. Turn it on in Settings → "
                            "Bluetooth, then press Try again.")
    assert machine.reason_problem(None) is None and machine.reason_problem("new-reason") is None
    assert machine.reason_problem(["held-elsewhere"]) is None
    assert ANOTHER_PROGRAM == ("Another program is using the CAMEO 4 over Bluetooth. Close it, "
                               "then switch the CAMEO 4 off and on.")
    assert SWITCH_OFF_ON == "Switch the CAMEO 4 off and on, then press Try again."
    # a daemon that refused to start retries nothing by itself: the text says to press Try again
    assert ANOTHER_PROGRAM_AT_START == ("Another program is using the CAMEO 4 over Bluetooth. "
                                        "Close it, switch the CAMEO 4 off and on, then press "
                                        "Try again.")


def test_the_legacy_daemons_lock_is_another_program(harness):
    fd = os.open(harness.legacy_lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)          # what the legacy daemon holds
    try:
        with pytest.raises(SystemExit) as refused:
            asyncio.run(daemon.serve(None, False))
    finally:
        os.close(fd)
    line = str(refused.value)                    # Python prints this as the daemon's last line
    assert line.startswith("the legacy prototype daemon holds cameo4.lock; stop it first")
    assert machine.startup_problem(line) == ANOTHER_PROGRAM_AT_START


def test_a_link_held_elsewhere_at_start_up_is_another_program(monkeypatch):
    async def connected(_address=None):
        device = __import__("sys").modules["bleak.backends.device"]
        return device.BLEDevice("00:00:00:00:00:01", "CAMEO 4-fake",
                                {"path": "/fake", "props": {"Connected": True}})

    async def no_wait(_seconds):
        return None
    monkeypatch.setattr(daemon, "find_bonded_cameo", connected)
    monkeypatch.setattr(daemon.asyncio, "sleep", no_wait)
    with pytest.raises(SystemExit) as refused:
        asyncio.run(daemon._refuse_if_held_elsewhere(None, False))
    assert machine.startup_problem(str(refused.value)) == ANOTHER_PROGRAM_AT_START


def test_other_start_up_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "LOCK_PATH", str(tmp_path / "opencutter.lock"))
    fd = os.open(daemon.LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)          # another OpenCutter daemon
    try:
        with pytest.raises(SystemExit) as refused:
            daemon._take_daemon_lock()
    finally:
        os.close(fd)
    assert machine.startup_problem(str(refused.value)) == SERVICE_BUSY
    assert machine.startup_problem("PlotterUnreachable: No paired CAMEO was found. Pair it first "
                                   "in the system Bluetooth settings.") == NOT_PAIRED
    assert machine.startup_problem(None) is None and machine.startup_problem("") is None


@pytest.mark.parametrize("line", ["ModuleNotFoundError: No module named 'bleak'",
                                  "the daemon exited with status -9",
                                  "dbus_fast.errors.DBusError: org.bluez.Error.NotReady"])
def test_unknown_start_up_lines_are_not_shown_but_the_log_is_named(line, monkeypatch, tmp_path):
    """A traceback line or an exit status is not plain words: the banner says what to do and
    where the details are, with the home folder written '~'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local" / "state"))
    problem = machine.startup_problem(line)
    assert problem == ("OpenCutter’s background service stopped unexpectedly. Press Try again; "
                       "if it keeps stopping, see ~/.local/state/opencutter/daemon.log.")
    assert "status" not in problem and "Error" not in problem and str(tmp_path) not in problem
    monkeypatch.setenv("XDG_STATE_HOME", "/elsewhere")
    assert machine.log_hint() == "/elsewhere/opencutter/daemon.log"


def test_failure_texts_fall_back_to_the_generic_hint():
    assert failure_text(None) == ("Could not Connect. Make sure the CAMEO 4 is switched on and "
                                  "paired. If it still fails, switch it off and on again.")
    assert failure_markup(None) == COULD_NOT_CONNECT
    assert failure_text(ANOTHER_PROGRAM) == f"Could not Connect. {ANOTHER_PROGRAM}"
    assert failure_markup("a < b & c") == "<b>Could not Connect.</b> a &lt; b &amp; c"


# --- the monitor ----------------------------------------------------------------------------
def test_a_daemon_that_could_not_start_says_why_until_one_answers():
    async def scenario():
        client = FakeDaemonClient(ping=None)
        client.spawn_error = ("the legacy prototype daemon holds cameo4.lock; stop it first "
                              "(lock /tmp/x/cameo4.lock is held)")
        monitor = MachineMonitor(client)
        await monitor.connect()
        state = monitor.state
        assert state.link is Link.FAILED and state.problem == ANOTHER_PROGRAM_AT_START
        assert pill(state) == ("Could not Connect", "negative", False)
        await monitor.refresh()                         # still no daemon: still why
        assert monitor.state.problem == ANOTHER_PROGRAM_AT_START
        client.ping_reply = dict(READY_PING)            # the user closed the other program
        await monitor.refresh()
        assert monitor.state.connected and monitor.state.problem is None
        client.ping_reply = None
        await monitor.refresh()                         # gone again later: no stale reason
        assert monitor.state.link is Link.NO_DAEMON and monitor.state.problem is None
    asyncio.run(scenario())


def test_no_reason_keeps_the_old_states():
    async def scenario():
        client = FakeDaemonClient(ping=None)            # e.g. OPENCUTTER_SOCKET: nothing spawned
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert monitor.state.link is Link.NO_DAEMON and monitor.state.problem is None
    asyncio.run(scenario())


def test_a_failing_start_is_reported_in_plain_words():
    """Nothing was started, so the service did not 'stop'; no errno, no path in the banner."""
    class Broken(FakeDaemonClient):
        async def ensure_daemon(self):
            raise PermissionError(13, "Permission denied", "/" + "home/someone/.local/state/x")

    async def scenario():
        monitor = MachineMonitor(Broken(ping=None))
        await monitor.connect()
        assert monitor.state.link is Link.FAILED
        assert monitor.state.problem == SERVICE_NOT_STARTED == (
            "OpenCutter could not start its background service. Press Try again; if it keeps "
            "failing, start OpenCutter from a terminal to see why.")
    asyncio.run(scenario())


def test_a_cleared_reason_gives_a_fresh_grace_time():
    """The other program let go (or the machine was just paired) and the daemon is connecting:
    Connecting…, not Could not Connect with the generic text."""
    async def scenario():
        clock = [0.0]
        client = FakeDaemonClient(ping={**WAITING, "reason": REASON_HELD_ELSEWHERE})
        monitor = MachineMonitor(client, clock=lambda: clock[0])
        await monitor.connect()
        assert monitor.state.link is Link.FAILED and monitor.state.problem == ANOTHER_PROGRAM
        clock[0] += 3 * CONNECT_GRACE_SECONDS           # the user takes a while
        await monitor.refresh()
        client.ping_reply = {**WAITING, "reason": None}  # the daemon found it free: connecting
        await monitor.refresh()
        assert monitor.state.link is Link.CONNECTING and monitor.state.problem is None
        clock[0] += CONNECT_GRACE_SECONDS + 1          # still nothing after the grace time
        await monitor.refresh()
        assert monitor.state.link is Link.FAILED and monitor.state.problem is None
    asyncio.run(scenario())


@pytest.mark.parametrize("reason, problem", [(REASON_HELD_ELSEWHERE, ANOTHER_PROGRAM),
                                             (REASON_NOT_PAIRED, NOT_PAIRED),
                                             (REASON_NO_BLUETOOTH, NO_BLUETOOTH)])
def test_reasons_that_waiting_cannot_fix_fail_at_once(reason, problem):
    async def scenario():
        client = FakeDaemonClient(ping={**WAITING, "reason": reason})
        monitor = MachineMonitor(client, clock=lambda: 0.0)
        await monitor.connect()
        assert monitor.state.link is Link.FAILED and monitor.state.problem == problem
    asyncio.run(scenario())


def test_an_unreachable_machine_gets_the_grace_time_then_the_hint():
    async def scenario():
        clock = [0.0]
        client = FakeDaemonClient(ping={**WAITING, "reason": REASON_UNREACHABLE})
        monitor = MachineMonitor(client, clock=lambda: clock[0])
        await monitor.connect()
        assert monitor.state.link is Link.CONNECTING
        clock[0] += CONNECT_GRACE_SECONDS + 1
        await monitor.refresh()
        assert monitor.state.link is Link.FAILED and monitor.state.problem == SWITCH_OFF_ON
        client.ping_reply = dict(WAITING)               # an older daemon: no reason field
        await monitor.refresh()
        assert monitor.state.link is Link.FAILED and monitor.state.problem is None
        client.ping_reply = dict(READY_PING)
        await monitor.refresh()
        assert monitor.state.connected and monitor.state.problem is None
    asyncio.run(scenario())
