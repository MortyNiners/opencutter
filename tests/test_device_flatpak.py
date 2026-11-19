# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The background service inside a Flatpak sandbox.

The app starts the service through the Flatpak portal (a sandbox of its own, so that it
outlives the window), knows it by the run id it was started with rather than by its pid, and
copes with pids it cannot see. Nothing is started here: Popen is a fake, the services are fake
sockets in a private $XDG_RUNTIME_DIR, the sandbox is a stand-in for /.flatpak-info, and the
real daemon runs only against the simulated machine (tests/stubs.py)."""
import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

import stubs

stubs.install()
from opencutter import APP_ID, __main__ as cli  # noqa: E402
from opencutter.device import client as client_mod, protocol  # noqa: E402
from opencutter.device.client import DaemonClient  # noqa: E402
from opencutter.gui import machine  # noqa: E402
from opencutter.identity import API_VERSION, BUILD, RUN_ID, STARTED_BY_ENV, started_by  # noqa: E402
from stubs import harness  # noqa: E402,F401

HOST = [sys.executable, "-m", "opencutter", "serve", "--idle-exit", "900"]
PORTAL = ["flatpak-spawn", f"--env=OPENCUTTER_STARTED_BY={RUN_ID}", "--directory=/",
          "opencutter", "serve", "--idle-exit", "900"]


@pytest.fixture
def rundir(monkeypatch):
    """A private, short $XDG_RUNTIME_DIR (and $XDG_STATE_HOME) with the service's directory."""
    d = tempfile.mkdtemp(prefix="ocf-", dir="/tmp" if len(tempfile.gettempdir()) > 60 else None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    monkeypatch.setenv("XDG_STATE_HOME", d)
    monkeypatch.delenv(client_mod.SOCKET_ENV, raising=False)
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    os.makedirs(os.path.join(d, "app", APP_ID), mode=0o700)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """As inside the app's Flatpak sandbox: /.flatpak-info exists and $FLATPAK_ID is the app."""
    info = tmp_path / ".flatpak-info"
    info.write_text(f"[Application]\nname={APP_ID}\n")
    monkeypatch.setattr(protocol, "FLATPAK_INFO", str(info))
    monkeypatch.setenv("FLATPAK_ID", APP_ID)
    assert protocol.in_flatpak()
    return info


class FakePopen:
    """Records the spawn (and a kill of the started process) instead of running anything."""
    calls: list = []
    kills: list = []
    exit_code = None
    on_spawn = None
    pid = 2 ** 22 + 9                       # no such process

    def __init__(self, args, **kwargs):
        FakePopen.calls.append((args, kwargs))
        self.killed = False
        if FakePopen.on_spawn:
            FakePopen.on_spawn(args, kwargs)

    def poll(self):
        return -signal.SIGKILL if self.killed else FakePopen.exit_code

    def kill(self):
        FakePopen.kills.append(self.pid)
        self.killed = True

    def wait(self, timeout=None):
        return self.poll()


@pytest.fixture
def fake_popen(monkeypatch):
    monkeypatch.setattr(FakePopen, "calls", [])
    monkeypatch.setattr(FakePopen, "kills", [])
    monkeypatch.setattr(FakePopen, "exit_code", None)
    monkeypatch.setattr(FakePopen, "on_spawn", None)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return FakePopen


async def fake_service(path: str, reply: dict | None = None, log: list | None = None):
    """A Unix server answering every request with ``reply`` (a ping of this build by default)."""
    reply = reply or {"ok": True, "connected": False, "version": "x", "build": BUILD,
                      "api": API_VERSION, "pid": 2}

    async def handle(reader, writer):
        while line := await reader.readline():
            if log is not None:
                log.append(json.loads(line))
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        writer.close()
    return await asyncio.start_unix_server(handle, path=path)


# --- the run id ---------------------------------------------------------------------------------
def test_the_run_id_names_this_process():
    assert len(RUN_ID) == 16 and int(RUN_ID, 16) >= 0 and RUN_ID == RUN_ID.lower()
    assert STARTED_BY_ENV == "OPENCUTTER_STARTED_BY"
    assert started_by({STARTED_BY_ENV: RUN_ID}) == RUN_ID
    for bad in ("", "x", RUN_ID.upper(), RUN_ID + "0", "../../etc", " " + RUN_ID):
        assert started_by({STARTED_BY_ENV: bad}) is None, bad
    assert started_by({}) is None


def test_the_service_reports_who_started_it(harness, monkeypatch):
    monkeypatch.setenv(STARTED_BY_ENV, RUN_ID)

    async def scenario(h):
        assert (await h.ping())["started_by"] == RUN_ID
    harness.run(scenario)


def test_a_service_started_by_hand_reports_nobody(harness, monkeypatch):
    monkeypatch.setenv(STARTED_BY_ENV, "not a run id")

    async def scenario(h):
        assert (await h.ping())["started_by"] is None
    harness.run(scenario)


# --- starting the service -------------------------------------------------------------------------
def test_outside_a_sandbox_it_starts_python_with_the_run_id(rundir, fake_popen):
    async def scenario():
        c = DaemonClient()
        servers = []
        fake_popen.on_spawn = lambda *_: servers.append(
            asyncio.get_running_loop().create_task(fake_service(c.socket_path)))
        try:
            assert await c.ensure_daemon() is True
        finally:
            for t in servers:
                (await t).close()
        (args, kwargs), = fake_popen.calls
        assert args == HOST
        assert kwargs["env"][STARTED_BY_ENV] == RUN_ID
        assert kwargs["env"]["XDG_RUNTIME_DIR"] == rundir          # everything else as it is
        assert c.spawned_pid == FakePopen.pid
        assert fake_popen.kills == []               # the service itself: never touched
    asyncio.run(scenario())


def test_inside_a_sandbox_it_starts_through_the_portal(rundir, sandbox, fake_popen):
    """flatpak-spawn without --host (the service stays in the app's sandbox, with the app's
    permissions), without --watch-bus (it must outlive the window) and without --latest-version
    (the window's own commit, so window and service are one build); the log is its output.
    Once the service answers, the helper is ended (SIGKILL), so the window's sandbox can end
    with the window."""
    async def scenario():
        c = DaemonClient()
        assert c.socket_path == os.path.join(rundir, "app", APP_ID, "opencutter.sock")
        servers = []
        fake_popen.on_spawn = lambda *_: servers.append(
            asyncio.get_running_loop().create_task(fake_service(c.socket_path)))
        try:
            assert await c.ensure_daemon() is True
        finally:
            for t in servers:
                (await t).close()
        (args, kwargs), = fake_popen.calls
        assert args == PORTAL
        assert "--host" not in args and "--watch-bus" not in args and "--share-pids" not in args
        assert kwargs["env"] is None
        assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["stdout"].name == os.path.join(rundir, "opencutter", "daemon.log")
        assert c.spawned_pid is None                # flatpak-spawn's pid is not the service's
        assert fake_popen.kills == [FakePopen.pid]
        assert "--latest-version" not in args
    asyncio.run(scenario())


def test_a_helper_still_waiting_after_the_start_is_ended_once_a_service_answers(
        rundir, sandbox, fake_popen):
    """A service that comes up only after ensure_daemon gave up: the first ping it answers
    ends the helper (the polls ping every 2 s). Nothing is ended while nothing answers."""
    async def scenario():
        c = DaemonClient()
        assert await c.ensure_daemon(timeout=0.5) is False
        assert c.spawn_error is None and fake_popen.kills == []      # the helper still waits
        assert await c.ping() is None and fake_popen.kills == []
        server = await fake_service(c.socket_path)
        try:
            assert (await c.ping())["build"] == BUILD
            assert fake_popen.kills == [FakePopen.pid]
            assert await c.ping() is not None and fake_popen.kills == [FakePopen.pid]   # once
        finally:
            server.close()
    asyncio.run(scenario())


def test_the_helper_is_ended_with_sigkill_which_it_cannot_pass_on(tmp_path):
    """flatpak-spawn passes SIGTERM, SIGINT and SIGHUP on to the service (which would then end);
    SIGKILL it cannot. A stand-in that would note any of those three is killed, and reaped."""
    noted = tmp_path / "signals"
    ready = tmp_path / "ready"
    code = ("import signal, sys, time, pathlib\n"
            "noted, ready = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
            "for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):\n"
            "    signal.signal(s, lambda n, f: noted.write_text(str(n)))\n"
            "ready.write_text('1')\n"
            "while True: time.sleep(0.05)\n")

    async def scenario():
        proc = subprocess.Popen([sys.executable, "-I", "-c", code, str(noted), str(ready)],
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            for _ in range(200):
                if ready.exists():
                    break
                await asyncio.sleep(0.02)
            assert ready.exists()
            await client_mod._end_spawn_helper(proc)
            assert proc.returncode == -signal.SIGKILL and not noted.exists()
            await client_mod._end_spawn_helper(proc)          # reaped: nothing more is sent
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    asyncio.run(scenario())


async def _spawned(fake_popen) -> None:
    for _ in range(500):
        if fake_popen.calls:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("nothing was started")


def test_closing_the_window_while_the_service_starts_ends_the_helper(rundir, sandbox, fake_popen):
    """The window closes before the service answered (MainWindow cancels the monitor task):
    the helper is ended at once, so the window's sandbox can end with the window. The service
    it asked the portal for starts and runs on by itself (its own sandbox)."""
    async def scenario():
        c = DaemonClient()
        task = asyncio.ensure_future(c.ensure_daemon(timeout=30))
        await _spawned(fake_popen)
        await asyncio.sleep(0.3)
        assert fake_popen.kills == []                  # still starting: the helper waits
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_popen.kills == [FakePopen.pid] and c._helper is None
    asyncio.run(scenario())


def test_the_monitor_cancelled_during_the_start_ends_the_helper(rundir, sandbox, fake_popen):
    """The same through MachineMonitor.run(), the task MainWindow cancels when it closes."""
    async def scenario():
        c = DaemonClient()
        task = asyncio.ensure_future(machine.MachineMonitor(c).run())
        await _spawned(fake_popen)
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fake_popen.kills == [FakePopen.pid]
    asyncio.run(scenario())


def test_end_helper_ends_a_helper_left_by_a_start_that_gave_up(rundir, sandbox, fake_popen):
    """ensure_daemon gave up but the helper still waits (a later answer would end it): the
    window's close and the app's quit end it with end_helper, once."""
    async def scenario():
        c = DaemonClient()
        assert await c.ensure_daemon(timeout=0.5) is False
        assert fake_popen.kills == []
        c.end_helper()
        assert fake_popen.kills == [FakePopen.pid]
        c.end_helper()                                   # nothing left to end
        assert fake_popen.kills == [FakePopen.pid]
    asyncio.run(scenario())


def test_end_helper_during_the_wait_ends_the_start_quietly(rundir, sandbox, fake_popen):
    """The app quits while a Connect still waits for the service: the wait ends at once, with
    no start-up error to show."""
    async def scenario():
        c = DaemonClient()
        task = asyncio.ensure_future(c.ensure_daemon(timeout=30))
        await _spawned(fake_popen)
        c.end_helper()
        assert await asyncio.wait_for(task, 2) is False
        assert c.spawn_error is None and fake_popen.kills == [FakePopen.pid]
    asyncio.run(scenario())


def test_end_helper_outside_a_sandbox_never_touches_the_service(rundir, fake_popen):
    """On the host the started process is the service itself: never ended."""
    async def scenario():
        c = DaemonClient()
        assert await c.ensure_daemon(timeout=0.5) is False
        c.end_helper()
        assert fake_popen.kills == [] and c.spawned_pid == FakePopen.pid
    asyncio.run(scenario())


def test_end_helper_ends_a_real_helper_with_sigkill_and_reaps_it(tmp_path):
    """A stand-in that would note SIGTERM, SIGINT or SIGHUP (flatpak-spawn passes those on to
    the service): end_helper sends none of them, only SIGKILL, and reaps it."""
    noted = tmp_path / "signals"
    ready = tmp_path / "ready"
    code = ("import signal, sys, time, pathlib\n"
            "noted, ready = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])\n"
            "for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):\n"
            "    signal.signal(s, lambda n, f: noted.write_text(str(n)))\n"
            "ready.write_text('1')\n"
            "while True: time.sleep(0.05)\n")
    proc = subprocess.Popen([sys.executable, "-I", "-c", code, str(noted), str(ready)],
                            stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(250):
            if ready.exists():
                break
            time.sleep(0.02)
        assert ready.exists()
        c = DaemonClient("/nonexistent/opencutter.sock")
        c._helper = proc
        c.end_helper()
        assert proc.returncode == -signal.SIGKILL and not noted.exists() and c._helper is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_spawner_that_ends_with_status_0_is_not_a_failed_start(rundir, sandbox, fake_popen):
    """Should flatpak-spawn return at once, the service may still come up: wait for it."""
    fake_popen.exit_code = 0

    async def scenario():
        c = DaemonClient()
        loop = asyncio.get_running_loop()
        servers = []
        fake_popen.on_spawn = lambda *_: loop.call_later(0.8, lambda: servers.append(
            loop.create_task(fake_service(c.socket_path))))
        try:
            assert await c.ensure_daemon(timeout=5) is True and c.spawn_error is None
        finally:
            for t in servers:
                (await t).close()
    asyncio.run(scenario())


def test_a_refused_portal_call_is_a_failed_start_with_its_line(rundir, sandbox, fake_popen):
    line = ("Portal call failed: org.freedesktop.DBus.Error.ServiceUnknown: The name "
            "org.freedesktop.portal.Flatpak was not provided by any .service files")
    fake_popen.exit_code = 1

    def write(_args, kwargs):                    # what flatpak-spawn prints to its stderr
        kwargs["stdout"].write(line.encode() + b"\n")
        kwargs["stdout"].flush()
    fake_popen.on_spawn = write

    async def scenario():
        c = DaemonClient()
        assert await c.ensure_daemon(timeout=2) is False
        assert c.spawn_error == line
        assert machine.startup_problem(c.spawn_error) == machine.SERVICE_STOPPED.format(
            log=machine.log_hint())
    asyncio.run(scenario())


def test_the_windows_version_gone_is_a_failed_start_that_says_so(rundir, sandbox, fake_popen):
    """The portal asks Flatpak for the window's commit; should it be gone, `flatpak run` prints
    this line (as Flatpak 1.16.6 does) to the passed stderr, daemon.log, and fails."""
    line = ("error: app/io.github.mortyniners.OpenCutter/x86_64/master (commit "
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef) not installed")
    fake_popen.exit_code = 1

    def write(_args, kwargs):
        kwargs["stdout"].write(line.encode() + b"\n")
        kwargs["stdout"].flush()
    fake_popen.on_spawn = write

    async def scenario():
        c = DaemonClient()
        assert await c.ensure_daemon(timeout=2) is False
        assert c.spawn_error == line
        assert machine.startup_problem(c.spawn_error) == machine.APP_OUTDATED
        assert fake_popen.kills == []               # it ended by itself
    asyncio.run(scenario())


def test_started_here_by_run_id_and_on_the_host_by_pid(rundir):
    c = DaemonClient()
    assert not c.started_here(None) and not c.started_here({"pid": 2})
    assert c.started_here({"pid": 2, "started_by": RUN_ID})
    assert not c.started_here({"pid": 2, "started_by": "0123456789abcdef"})
    c.spawned_pid = 4321                     # the host: the pid tells, for a service too old to
    assert c.started_here({"pid": 4321}) and not c.started_here({"pid": 4322})   # report a run id
    assert not c.started_here({"pid": True})
    # another run's id is not ours, whatever the pid: a pid of another sandbox's PID namespace
    # may well equal the pid this process started; without a run id, the pid still tells
    assert not c.started_here({"pid": 4321, "started_by": "0123456789abcdef"})
    assert c.started_here({"pid": 4321, "started_by": None})
    c.spawned_pid = None                                       # in a sandbox: only the run id
    assert not c.started_here({"pid": 4321, "started_by": None})


# --- pids that cannot be seen -----------------------------------------------------------------------
def test_shutdown_of_a_service_in_another_sandbox_waits_for_its_socket(rundir, monkeypatch):
    """SO_PEERCRED gives pid 0 for a process in another PID namespace: the shutdown then waits
    until the service has removed its socket."""
    monkeypatch.setattr(client_mod, "peer_credentials", lambda sock: (0, os.getuid()))
    path = client_mod.service_socket_path()

    async def scenario():
        log = []
        server = await fake_service(path, {"ok": True}, log)
        loop = asyncio.get_running_loop()

        def gone():
            server.close()                     # (Python >= 3.13 removes the socket file too)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        loop.call_later(0.5, gone)
        start = loop.time()
        result = await client_mod.shutdown_service(path, wait=5)
        assert result == {"ok": True, "pid": None}
        assert loop.time() - start >= 0.4 and log == [{"op": "shutdown"}]
    asyncio.run(scenario())


def test_no_signal_to_a_pid_that_cannot_be_seen(rundir, monkeypatch):
    monkeypatch.setattr(client_mod, "peer_credentials", lambda sock: (0, os.getuid()))
    monkeypatch.setattr(client_mod.os, "kill", lambda *a: pytest.fail("signalled"))
    path = client_mod.older_socket_path()

    async def scenario():
        server = await fake_service(path, {"ok": True, "connected": False, "motion": None})
        try:
            result = await client_mod.terminate_older_daemon(path, wait=1)
        finally:
            server.close()
        assert result["ok"] is False and result["pid"] is None
        assert "cannot be seen from here" in result["error"]
    asyncio.run(scenario())


def test_service_status_never_shows_a_pid_of_another_sandbox(rundir, monkeypatch, capsys):
    ping = {"ok": True, "connected": False, "reason": "no-bluetooth", "motion": None,
            "version": "0.1.0.dev0", "build": BUILD, "api": API_VERSION, "pid": 2,
            "started_by": RUN_ID}

    async def with_peer(path):
        return dict(ping), None                    # SO_PEERCRED gave 0: not visible here
    monkeypatch.setattr(cli, "ping_with_peer", with_peer)
    assert asyncio.run(cli.service_status()) is True
    out = capsys.readouterr().out
    assert "  pid:     not visible from here (the service runs in another sandbox" in out
    assert "  pid:     2" not in out
    assert "  machine: not connected (no-bluetooth)" in out


def test_service_status_shows_the_pid_it_can_see(rundir, monkeypatch, capsys):
    async def with_peer(path):
        return {"ok": True, "connected": False, "build": BUILD, "api": API_VERSION,
                "pid": 2}, 54321                  # from the host: the host pid
    monkeypatch.setattr(cli, "ping_with_peer", with_peer)
    assert asyncio.run(cli.service_status()) is True
    assert "  pid:     54321\n" in capsys.readouterr().out


# --- the service log's folder -------------------------------------------------------------------
def test_the_log_stays_in_the_apps_folder_on_a_flatpak_without_xdg_state_home(
        rundir, sandbox, monkeypatch, tmp_path):
    """Flatpak sets $XDG_STATE_HOME to ~/.var/app/<id>/.local/state only from 1.14 on. Without
    it the log goes to that same folder: the sandbox's own ~/.local/state is not kept."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME")
    expected = tmp_path / ".var" / "app" / APP_ID / ".local" / "state" / "opencutter"
    assert client_mod.state_dir() == str(expected)
    assert client_mod.daemon_log_path() == str(expected / "daemon.log")
    assert machine.log_hint() == f"~/.var/app/{APP_ID}/.local/state/opencutter/daemon.log"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))      # Flatpak 1.14 and later
    assert client_mod.state_dir() == str(tmp_path / "state" / "opencutter")
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("FLATPAK_ID", "../../escape")                  # not an application id
    assert client_mod.state_dir() == str(tmp_path / ".local" / "state" / "opencutter")


def test_outside_a_sandbox_the_log_is_in_local_state(rundir, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("FLATPAK_ID", APP_ID)                 # leaked into a host command: ignored
    assert not protocol.in_flatpak() and protocol.flatpak_app_id() is None
    assert client_mod.state_dir() == str(tmp_path / ".local" / "state" / "opencutter")
