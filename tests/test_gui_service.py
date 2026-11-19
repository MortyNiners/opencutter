# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The app and its background service: a service of another build is replaced once (never
while it runs a job, and never fought over), nothing is sent to it, and Disconnect ends the
service until Connect. MachineMonitor and JobRunner with the fake client: no display, no
socket, no process."""
import asyncio

import pytest

from opencutter.gui import machine, sending
from opencutter.gui.machine import (ANOTHER_VERSION, APP_OUTDATED, DISCONNECTING,
                                    NOT_DISCONNECTED, NOT_DISCONNECTED_BUSY, OTHER_VERSION_BUSY,
                                    OTHER_VERSION_REFUSED, RELEASED, SERVICE_NOT_REPLACED,
                                    SERVICE_NOT_STOPPED, UPDATING_SERVICE, Link, MachineMonitor,
                                    MachineState, pill)
from opencutter.gui.runner import JobRunner, Outcome
from opencutter.gui.sending import blocker_for_test_cut, send_blocker, tool_check
from opencutter.identity import RUN_ID, identity
from opencutter.presets.model import BASIC_PRESETS
from gui_fakes import OTHER_BUILD, READY_PING, FakeDaemonClient

VINYL = BASIC_PRESETS[0].to_tool_settings()
MEDIA = (0.0, 0.0, 304.8, 304.8)


def _other(**ping) -> FakeDaemonClient:
    """A client whose service is of another build (it would answer `ping`)."""
    client = FakeDaemonClient(ping={**READY_PING, **ping})
    client.identity = dict(OTHER_BUILD)
    client.spawn_ping = dict(READY_PING)              # ours, once it is started
    return client


def _watch(monitor: MachineMonitor) -> list[MachineState]:
    seen: list[MachineState] = []
    monitor.add_listener(seen.append)
    return seen


def test_the_texts():
    assert UPDATING_SERVICE == "Updating OpenCutter’s background service…"
    assert OTHER_VERSION_BUSY == ("OpenCutter’s background service is from another version; it "
                                  "will be updated when its current job ends.")
    assert ANOTHER_VERSION == ("Another OpenCutter version is using the CAMEO 4. Close it, then "
                               "press Try again.")
    assert RELEASED == "The CAMEO 4 is free for other apps. Press Connect to use it here again."
    assert APP_OUTDATED == "OpenCutter was updated while it was open. Close it and open it again."
    assert SERVICE_NOT_STOPPED == ("OpenCutter’s background service from another version could "
                                   "not be stopped. Restart the computer, then open OpenCutter "
                                   "again.")
    assert NOT_DISCONNECTED_BUSY == ("Not disconnected: a job is running on the CAMEO 4. Press "
                                     "Disconnect again when it has ended.")
    for text in (APP_OUTDATED, SERVICE_NOT_STOPPED, NOT_DISCONNECTED, NOT_DISCONNECTED_BUSY,
                 DISCONNECTING):
        assert "daemon" not in text and "`" not in text and "/" not in text   # plain words


# --- replacing a service of another build -----------------------------------------------------
def test_a_service_of_another_build_is_replaced_by_the_shutdown_op():
    async def scenario():
        client = _other()
        monitor = MachineMonitor(client)
        seen = _watch(monitor)
        await monitor.connect()
        assert client.shutdowns == 1 and client.terminated == [] and client.daemon_started == 2
        assert monitor.state.connected and monitor.state.blocker is None
        assert any(s.link is Link.CONNECTING and s.note == UPDATING_SERVICE for s in seen)
        assert pill(next(s for s in seen if s.note == UPDATING_SERVICE)) == ("Connecting…", "info",
                                                                            True)
        assert client.opened == [] and client.stops == 0          # nothing was sent anywhere
    asyncio.run(scenario())


def test_an_older_service_without_a_build_gets_the_guarded_sigterm():
    async def scenario():
        client = _other()
        client.identity = {}                                       # an older reply: no build
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert client.terminated == [client.socket_path] and client.shutdowns == 0
        assert monitor.state.connected
    asyncio.run(scenario())


def test_an_older_service_on_the_old_socket_is_replaced():
    async def scenario():
        client = FakeDaemonClient(ping=None)
        client.older_reply = {k: v for k, v in READY_PING.items()}   # no build: an older one
        client.spawn_ping = dict(READY_PING)
        monitor = MachineMonitor(client)
        seen = _watch(monitor)
        await monitor.connect()
        assert client.terminated == [None]                          # the old socket's service
        assert client.daemon_started == 1 and monitor.state.connected
        assert any(s.note == UPDATING_SERVICE for s in seen)
    asyncio.run(scenario())


def test_replace_once_then_do_not_fight_then_try_again_replaces_once_more():
    async def scenario():
        client = _other()
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert client.shutdowns == 1 and monitor.state.connected
        # another copy of another version takes over (e.g. a Flatpak and a pip install)
        client.identity = dict(OTHER_BUILD)
        for _ in range(3):
            await monitor.refresh()
            assert monitor.state.link is Link.FAILED and monitor.state.problem == ANOTHER_VERSION
        assert client.shutdowns == 1                               # not fought over
        assert pill(monitor.state) == ("Could not Connect", "negative", False)
        await monitor.connect()                                    # Try again
        assert client.shutdowns == 2 and monitor.state.connected
    asyncio.run(scenario())


def test_never_while_its_job_runs_and_send_is_blocked():
    async def scenario():
        client = _other(motion="job", status="1")
        monitor = MachineMonitor(client)
        await monitor.connect()
        state = monitor.state
        assert client.shutdowns == 0 and state.blocker == OTHER_VERSION_BUSY
        assert state.connected and state.motion == "job" and state.status_text == "Busy"
        check = tool_check(VINYL, state)
        assert blocker_for_test_cut(state, check, False) == OTHER_VERSION_BUSY
        assert send_blocker(None, MEDIA, state, check, False) == "Open a design first."
        for _ in range(3):
            await monitor.refresh()
        assert client.shutdowns == 0
        client.ping_reply = {**client.ping_reply, "motion": None, "status": "0"}   # it ended
        await monitor.refresh()
        assert client.shutdowns == 1 and monitor.state.connected and monitor.state.blocker is None
        assert blocker_for_test_cut(monitor.state, tool_check(VINYL, monitor.state), False) is None
    asyncio.run(scenario())


def test_an_older_service_running_a_job_is_waited_for():
    async def scenario():
        client = FakeDaemonClient(ping=None)
        client.older_reply = {**READY_PING, "motion": "cut-test", "status": "1"}
        client.spawn_ping = dict(READY_PING)
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert client.daemon_started == 0 and client.terminated == []   # nothing next to it
        assert monitor.state.blocker == OTHER_VERSION_BUSY
        client.older_reply = {**READY_PING, "motion": None}
        await monitor.refresh()
        assert client.terminated == [None] and monitor.state.connected
    asyncio.run(scenario())


def test_a_job_that_starts_meanwhile_keeps_the_replace_for_later():
    async def scenario():
        client = _other()
        client.shutdown_reply = {"ok": False, "error": "a program or job is running", "pid": 7,
                                 "busy": True}
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert client.shutdowns == 1 and monitor.state.blocker == OTHER_VERSION_BUSY
        client.shutdown_reply = None                               # that job ended
        await monitor.refresh()
        assert client.shutdowns == 2 and monitor.state.connected
    asyncio.run(scenario())


@pytest.mark.parametrize("result", [
    {"ok": False, "error": "process 9 is not an OpenCutter background service", "pid": 9,
     "busy": False},
    {"ok": False, "error": "the process behind x belongs to another user (uid 1)", "pid": None,
     "busy": False},
])
def test_a_refused_guard_is_not_fought(result):
    """The guarded SIGTERM refused: the old service is the app's own (no other copy to close),
    so the state says it could not be stopped, also on later polls, and Try again tries once
    more."""
    async def scenario():
        client = _other()
        client.identity = {}
        client.terminate_reply = result
        monitor = MachineMonitor(client)
        await monitor.connect()
        for _ in range(3):
            assert monitor.state.link is Link.FAILED
            assert monitor.state.problem == SERVICE_NOT_STOPPED
            await monitor.refresh()
        assert client.daemon_started == 1                          # nothing started next to it
        assert client.terminated == [client.socket_path]           # not fought over
        await monitor.connect()                                    # Try again
        assert client.terminated == [client.socket_path] * 2
        assert monitor.state.problem == SERVICE_NOT_STOPPED
    asyncio.run(scenario())


@pytest.mark.parametrize("older", [False, True])
def test_a_service_that_did_not_end_but_still_answers(older):
    """It got the shutdown op (or, an older one, SIGTERM) but still answers after the wait:
    'did not stop, wait, Try again', not 'another version' (there is no other copy)."""
    async def scenario():
        if older:
            client = FakeDaemonClient(ping=None)
            client.older_reply = dict(READY_PING)
            client.terminate_reply = {"ok": False, "error": "process 5 did not end within 10 s",
                                      "pid": 5, "busy": False}
        else:
            client = _other()
            client.shutdown_reply = {"ok": False, "error": "it did not end within 10 s", "pid": 5,
                                     "busy": False}
        client.spawn_ping = dict(READY_PING)
        monitor = MachineMonitor(client)
        await monitor.connect()
        for _ in range(3):
            assert monitor.state.link is Link.FAILED
            assert monitor.state.problem == SERVICE_NOT_REPLACED
            await monitor.refresh()
        assert client.shutdowns + len(client.terminated) == 1
        client.shutdown_reply = client.terminate_reply = None      # it ends this time
        await monitor.connect()                                    # Try again
        assert monitor.state.connected and monitor.state.problem is None
    asyncio.run(scenario())


def test_a_service_that_does_not_end_in_time():
    class Lingering(FakeDaemonClient):
        async def shutdown(self, wait=10):
            self.shutdowns += 1
            self.ping_reply = None                    # it stopped answering but did not end
            return {"ok": False, "error": "it did not end within 10 s", "pid": 5, "busy": False}

    async def scenario():
        client = Lingering(ping=dict(READY_PING))
        client.identity = dict(OTHER_BUILD)
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert monitor.state.link is Link.FAILED and monitor.state.problem == SERVICE_NOT_REPLACED
    asyncio.run(scenario())


# --- the app itself is the old one -------------------------------------------------------------
def test_an_app_older_than_its_files_replaces_nothing(monkeypatch):
    """OpenCutter was updated (or edited) while the app was open; its service ended, and the one
    it starts now is of the files on disk: the app is the old side. It must not end that service
    (it would only start the same build again), and says to restart the app."""
    async def scenario():
        client = FakeDaemonClient(ping=None)
        client.spawn_ping = dict(READY_PING)
        client.spawn_identity = {**OTHER_BUILD, "build": "fedcba987654"}   # the files on disk
        monitor = MachineMonitor(client)
        await monitor.connect()
        for _ in range(3):
            assert monitor.state.link is Link.FAILED and monitor.state.problem == APP_OUTDATED
            await monitor.refresh()
        await monitor.connect()                                    # Try again: the same
        assert monitor.state.problem == APP_OUTDATED
        assert client.shutdowns == 0 and client.terminated == [] and client.daemon_started == 2
        assert client.opened == [] and client.stops == 0
    monkeypatch.setattr(machine, "build_on_disk", lambda: "fedcba987654")
    asyncio.run(scenario())


def test_its_own_new_service_of_another_build_is_never_replaced():
    """Even where the files on disk cannot tell (the build on disk looks like the app's), the
    service the app started itself (its pid) reporting another build means the app is old."""
    async def scenario():
        client = FakeDaemonClient(ping=None)
        client.spawn_ping = {**READY_PING, "pid": 4321}
        client.spawn_identity = dict(OTHER_BUILD)
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert client.spawned_pid == 4321
        assert monitor.state.problem == APP_OUTDATED and client.shutdowns == 0
    asyncio.run(scenario())


def test_in_the_flatpak_its_own_service_is_known_by_the_run_id():
    """In the Flatpak the service runs in a sandbox of its own: its pid (here 2, as small as
    the app's own pids) means nothing to the app. The run id it was started with tells: should
    the service this run started report another build, the app is the old side."""
    async def scenario():
        client = FakeDaemonClient(ping=None)
        client.sandboxed = True
        client.spawn_ping = {**READY_PING, "pid": 2, "started_by": RUN_ID}
        client.spawn_identity = dict(OTHER_BUILD)
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert client.spawned_pid is None
        assert monitor.state.problem == APP_OUTDATED and client.shutdowns == 0
    asyncio.run(scenario())


def test_a_flatpak_update_while_open_keeps_window_and_service_one_build():
    """After a Flatpak update the open window keeps its files, and the portal starts the service
    from those same files (the window's commit). A service of the new version that another run
    started (here: by hand) is replaced once, as any other build; the window's own service then
    answers, and the window works as before."""
    async def scenario():
        client = _other(pid=2, started_by=None)         # the new version, not started here
        client.sandboxed = True
        client.spawn_ping = {**READY_PING, "pid": 2, "started_by": RUN_ID}   # this very build
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert client.shutdowns == 1 and client.daemon_started == 2
        assert monitor.state.connected and monitor.state.problem is None
        for _ in range(3):
            await monitor.refresh()
        assert monitor.state.connected and client.shutdowns == 1
    asyncio.run(scenario())


def test_a_flatpak_window_whose_version_is_gone_says_so_without_a_loop():
    """Should the window's version be gone all the same (the portal asks Flatpak for the
    window's commit), flatpak-spawn ends at once with Flatpak's line in daemon.log. The window
    says to close and open it again; the polls start nothing, Try again tries once more."""
    gone = ("error: app/io.github.mortyniners.OpenCutter/x86_64/master (commit "
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef) not installed")
    assert machine.startup_problem(gone) == APP_OUTDATED

    async def scenario():
        client = FakeDaemonClient(ping=None)
        client.sandboxed = True
        client.spawn_error = gone
        monitor = MachineMonitor(client)
        await monitor.connect()
        for _ in range(3):
            assert monitor.state.link is Link.FAILED and monitor.state.problem == APP_OUTDATED
            await monitor.refresh()
        assert client.daemon_started == 1
        await monitor.connect()                         # Try again: once more, the same
        assert monitor.state.problem == APP_OUTDATED and client.daemon_started == 2
        assert client.shutdowns == 0 and client.opened == [] and client.stops == 0
    asyncio.run(scenario())


def test_another_runs_service_with_a_pid_like_ours_is_not_taken_for_ours():
    """A service of another build started by another window (another run id): replaced once,
    as before, whatever its pid."""
    async def scenario():
        client = _other(pid=2, started_by="0123456789abcdef")
        client.sandboxed = True
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert client.shutdowns == 1 and monitor.state.connected
    asyncio.run(scenario())


def test_an_app_older_than_its_files_leaves_a_job_alone(monkeypatch):
    async def scenario():
        client = _other(motion="job", status="1")
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert monitor.state.blocker == OTHER_VERSION_BUSY and client.shutdowns == 0
        client.ping_reply = {**client.ping_reply, "motion": None, "status": "0"}   # it ended
        await monitor.refresh()
        assert monitor.state.problem == APP_OUTDATED and client.shutdowns == 0
    monkeypatch.setattr(machine, "build_on_disk", lambda: OTHER_BUILD["build"])
    asyncio.run(scenario())


def test_another_socket_is_never_replaced():
    async def scenario():
        client = _other()
        client.manages_service = False                             # $OPENCUTTER_SOCKET
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert monitor.state.problem == ANOTHER_VERSION and client.shutdowns == 0
    asyncio.run(scenario())


def test_the_monitor_waits_while_replacing():
    async def scenario():
        client = _other()
        client.shutdown_gate = asyncio.Event()
        monitor = MachineMonitor(client)
        task = asyncio.ensure_future(monitor.connect())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if monitor.state.note == UPDATING_SERVICE:
                break
        assert monitor.state.link is Link.CONNECTING and monitor.state.note == UPDATING_SERVICE
        assert await monitor.refresh() is monitor.state           # polls do not interfere
        await monitor.connect()                                   # nor does Try again
        assert await monitor.disconnect() is not None
        assert client.shutdowns == 1
        client.shutdown_gate.set()
        await task
        assert monitor.state.connected and client.shutdowns == 1
    asyncio.run(scenario())


# --- nothing is sent to another build ----------------------------------------------------------
def test_the_runner_never_opens_a_job_on_another_build():
    async def scenario():
        client = FakeDaemonClient()
        client.identity = dict(OTHER_BUILD)
        runner = JobRunner(client)
        outcome = await runner.run_job({"mat": "12x12"})
        assert outcome is Outcome.FAILED and runner.error == OTHER_VERSION_REFUSED
        assert client.opened == [] and client.sessions_closed == 0
        client.identity = identity()
        client.ping_reply = None                       # no service: the session itself fails
        assert await JobRunner(client).run_job({"mat": "x"}) is not Outcome.DONE
    asyncio.run(scenario())


def test_blocker_comes_before_connect_first():
    state = MachineState(link=Link.CONNECTING, blocker=OTHER_VERSION_BUSY)
    assert blocker_for_test_cut(state, None, False) == OTHER_VERSION_BUSY
    assert blocker_for_test_cut(state, None, True) == "A job is already running."
    assert blocker_for_test_cut(state, None, False, material_chosen=False) == sending.CHOOSE_MATERIAL


# --- Disconnect ------------------------------------------------------------------------------
def test_disconnect_then_nothing_starts_by_itself_then_connect():
    async def scenario():
        client = FakeDaemonClient()
        client.spawn_ping = dict(READY_PING)
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert monitor.state.connected and client.daemon_started == 1
        assert await monitor.disconnect() is None
        assert client.shutdowns == 1 and client.stops == 0
        state = monitor.state
        assert state.link is Link.NO_DAEMON and state.note == RELEASED
        assert pill(state) == ("Not Connected", "neutral", False)
        for _ in range(3):
            await monitor.refresh()
        assert client.daemon_started == 1 and monitor.state.note == RELEASED   # no respawn
        await monitor.connect()                                    # Connect
        assert client.daemon_started == 2 and monitor.state.connected
        assert monitor.state.note is None
    asyncio.run(scenario())


def test_disconnect_is_refused_during_a_job():
    async def scenario():
        client = FakeDaemonClient(ping={**READY_PING, "motion": "job", "status": "1"})
        client.shutdown_reply = {"ok": False, "error": "a program or job is running; cancel it "
                                 "first (in the app, or with `opencutter stop`)", "pid": 3,
                                 "busy": True}
        monitor = MachineMonitor(client)
        await monitor.refresh()
        error = await monitor.disconnect()
        assert error == NOT_DISCONNECTED_BUSY                      # plain words, no command
        assert monitor.state.connected and monitor.state.motion == "job"
        assert monitor.state.blocker is None
    asyncio.run(scenario())


@pytest.mark.parametrize("result", [
    {"ok": False, "error": "it did not end within 10 s", "pid": 3, "busy": False},
    {"ok": False, "error": "no OpenCutter daemon answers at /x/opencutter.sock "
                           "(FileNotFoundError)", "pid": None, "busy": False},
])
def test_a_failed_disconnect_says_so_in_plain_words(result):
    async def scenario():
        client = FakeDaemonClient()
        client.shutdown_reply = result
        monitor = MachineMonitor(client)
        await monitor.refresh()
        assert await monitor.disconnect() == NOT_DISCONNECTED
    asyncio.run(scenario())


def test_no_failure_is_shown_while_disconnect_waits_for_the_service():
    """While the service releases the link it still answers, with connected false: that is not
    a dropped link. The polls (and Connect) leave the state alone meanwhile, and nothing may be
    sent."""
    async def scenario():
        client = FakeDaemonClient()
        client.spawn_ping = dict(READY_PING)
        client.shutdown_gate = asyncio.Event()
        monitor = MachineMonitor(client)
        seen = _watch(monitor)
        await monitor.connect()
        task = asyncio.ensure_future(monitor.disconnect())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if monitor.state.blocker == DISCONNECTING:
                break
        state = monitor.state
        assert state.blocker == DISCONNECTING
        assert blocker_for_test_cut(state, tool_check(VINYL, state), False) == DISCONNECTING
        client.ping_reply = {**READY_PING, "connected": False, "status": None, "device": None,
                             "stopping": True}                   # releasing the link
        for _ in range(3):
            await monitor.refresh()
        await monitor.connect()
        assert client.daemon_started == 1
        client.shutdown_gate.set()
        assert await task is None
        assert not any(s.link is Link.FAILED for s in seen)
        assert monitor.state.note == RELEASED and client.shutdowns == 1
    asyncio.run(scenario())


def test_a_service_that_is_stopping_is_not_a_dropped_link():
    """`opencutter service stop` from a terminal: the service answers 'stopping' while it
    releases the link; the app shows Not Connected, not Could not Connect."""
    async def scenario():
        client = FakeDaemonClient()
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert monitor.state.connected
        client.ping_reply = {**READY_PING, "connected": False, "status": None, "device": None,
                             "stopping": True}
        for _ in range(3):
            await monitor.refresh()
            assert monitor.state.link is Link.NO_DAEMON and monitor.state.problem is None
        client.identity = dict(OTHER_BUILD)                      # nor is it replaced
        await monitor.refresh()
        assert monitor.state.link is Link.NO_DAEMON and client.shutdowns == 0
    asyncio.run(scenario())


def test_after_disconnect_another_build_is_left_alone():
    async def scenario():
        client = FakeDaemonClient()
        monitor = MachineMonitor(client)
        await monitor.connect()
        await monitor.disconnect()
        client.ping_reply = dict(READY_PING)                       # someone else started one
        client.identity = dict(OTHER_BUILD)
        await monitor.refresh()
        assert monitor.state.problem == ANOTHER_VERSION and client.shutdowns == 1
    asyncio.run(scenario())


def test_an_older_service_that_did_not_answer_at_start_up():
    line = ("an older OpenCutter background service is running; stop it with `opencutter "
            "service stop` (lock /tmp/x/opencutter.lock is held)")
    assert machine.startup_problem(line) == machine.OLDER_SERVICE
