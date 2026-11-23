# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The background service's life: its runtime directory, its locks, what its ping says about
it, the shutdown op, the idle exit and its start line.

Runs the real daemon against the simulated machine (tests/stubs.py): stub bleak/dbus_fast, a
private $XDG_RUNTIME_DIR in a temp directory. Nothing reaches Bluetooth, and nothing is ever
sent to the (simulated) machine for a shutdown or an idle exit.
"""
import asyncio
import fcntl
import os
import re
import stat

import pytest

import stubs
from stubs import ESC09, harness, stops  # noqa: F401

stubs.install()
from opencutter import APP_ID, __version__  # noqa: E402
from opencutter.device import client as client_mod, daemon, protocol  # noqa: E402
from opencutter.gui import machine  # noqa: E402
from opencutter.identity import API_VERSION, BUILD  # noqa: E402


def flock_free(path: str) -> bool:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


async def _until(predicate, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def _session_over(h) -> None:
    """Wait until the daemon has let go of a session whose connection was closed."""
    for _ in range(200):
        if (await h.ping())["motion"] is None:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the session did not end")


# --- the runtime directory ---------------------------------------------------------------------
def test_runtime_dir_is_app_id_under_the_runtime_dir(monkeypatch, tmp_path):
    """One directory for the host and every Flatpak instance: $XDG_RUNTIME_DIR/app/<id>. (Pure
    path computations: nothing is created.)"""
    sandbox = tmp_path / ".flatpak-info"                       # stands for /.flatpak-info
    monkeypatch.setattr(protocol, "FLATPAK_INFO", str(sandbox))
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    assert protocol.runtime_dir() == f"/run/user/4242/app/{APP_ID}"
    assert protocol.service_path("opencutter.sock") == f"/run/user/4242/app/{APP_ID}/opencutter.sock"
    sandbox.write_text("[Application]\n")                      # inside a Flatpak sandbox
    monkeypatch.setenv("FLATPAK_ID", APP_ID)                    # the same directory
    assert protocol.runtime_dir() == f"/run/user/4242/app/{APP_ID}"
    monkeypatch.setenv("FLATPAK_ID", "org.example.Other")
    assert protocol.runtime_dir() == "/run/user/4242/app/org.example.Other"
    for bad in ("../../tmp/x", "a/b.c", "nodots", ".hidden.x", "x..y", "é.x"):
        monkeypatch.setenv("FLATPAK_ID", bad)                   # never a path out of app/
        assert protocol.runtime_dir() == f"/run/user/4242/app/{APP_ID}", bad
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert protocol.runtime_dir() is None and protocol.service_path("x") is None


def test_a_flatpak_id_outside_a_sandbox_is_ignored(monkeypatch, tmp_path):
    """A $FLATPAK_ID that leaked into a host command's environment (e.g. a terminal opened from
    a Flatpak editor) must not split the service: every host process uses app/<APP_ID>."""
    monkeypatch.setattr(protocol, "FLATPAK_INFO", str(tmp_path / "missing"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    for leaked in ("com.visualstudio.code", f"{APP_ID}.Devel"):
        monkeypatch.setenv("FLATPAK_ID", leaked)
        assert protocol.runtime_dir() == f"/run/user/4242/app/{APP_ID}", leaked


def test_the_daemon_creates_its_directory_private(harness):
    assert not os.path.exists(os.path.join(harness.dir, "app"))

    async def scenario(h):
        for d in (os.path.join(h.dir, "app"), h.service_dir):
            info = os.lstat(d)
            assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
            assert info.st_uid == os.getuid()
        assert os.path.dirname(h.socket) == h.service_dir == os.path.dirname(h.lock)
        assert stat.S_IMODE(os.stat(h.socket).st_mode) == 0o600
    harness.run(scenario)


@pytest.mark.parametrize("kind", ["symlink", "mode", "file", "owner"])
def test_a_directory_that_is_not_private_is_refused_and_left_alone(harness, monkeypatch, kind):
    os.makedirs(os.path.join(harness.dir, "app"), mode=0o700)
    elsewhere = os.path.join(harness.dir, "elsewhere")
    os.mkdir(elsewhere, 0o700)
    if kind == "symlink":
        os.symlink(elsewhere, harness.service_dir)
    elif kind == "mode":
        os.mkdir(harness.service_dir, 0o700)
        os.chmod(harness.service_dir, 0o755)
    elif kind == "file":
        open(harness.service_dir, "w").close()
    else:
        os.mkdir(harness.service_dir, 0o700)
    before = os.lstat(harness.service_dir)
    if kind == "owner":
        real_lstat, active = os.lstat, [True]

        def other_owner(path, *a, **k):              # as if another user had made it
            info = real_lstat(path, *a, **k)
            if not active[0] or os.path.abspath(path) != harness.service_dir:
                return info
            values = list(info)
            values[stat.ST_UID] = os.getuid() + 1
            return os.stat_result(values)
        monkeypatch.setattr(daemon.os, "lstat", other_owner)
    with pytest.raises(SystemExit) as refused:
        asyncio.run(daemon.serve(None, False))
    if kind == "owner":
        active[0] = False
    why = {"symlink": "is a symbolic link", "mode": "is open to other users (mode 755)",
           "file": "is not a directory", "owner": f"belongs to another user (uid {os.getuid() + 1})"}
    assert str(refused.value) == (f"the runtime directory {harness.service_dir} {why[kind]}; "
                                  "refusing to start (fix or remove it, then start again)")
    after = os.lstat(harness.service_dir)
    assert (after.st_mode, after.st_ino) == (before.st_mode, before.st_ino)   # not touched
    assert harness.fake is None and not os.listdir(elsewhere)
    assert flock_free(harness.old_lock) and flock_free(harness.legacy_lock)


# --- the locks ---------------------------------------------------------------------------------
def test_an_older_daemon_holding_the_old_lock_is_refused_in_plain_words(harness):
    """Builds before 0.1.0 lock $XDG_RUNTIME_DIR/opencutter.lock (the owner's running daemon
    holds it): the new daemon refuses to start next to it, with a line the app maps."""
    fd = os.open(harness.old_lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SystemExit) as refused:
            asyncio.run(daemon.serve(None, False))
    finally:
        os.close(fd)
    line = str(refused.value)
    assert line == ("an older OpenCutter background service is running; stop it with "
                    f"`opencutter service stop` (lock {harness.old_lock} is held)")
    assert machine.startup_problem(line) == machine.OLDER_SERVICE
    assert harness.fake is None and not os.path.exists(harness.socket)
    assert flock_free(harness.lock) and flock_free(harness.legacy_lock)   # its own: released
    assert not any("background service started" in x for x in harness.log)   # it did not start


def test_the_running_daemon_holds_all_three_locks(harness):
    async def scenario(h):
        for path in (h.lock, h.old_lock, h.legacy_lock):
            assert not flock_free(path), path         # no daemon of any generation can start
        started = sum("background service started" in x for x in h.log)
        with pytest.raises(SystemExit, match="another OpenCutter daemon is running"):
            await daemon.serve(None, False)
        assert sum("background service started" in x for x in h.log) == started   # none more
    harness.run(scenario)
    for path in (harness.lock, harness.old_lock, harness.legacy_lock):
        assert flock_free(path)


# --- what the ping says ------------------------------------------------------------------------
def test_ping_names_version_build_api_and_pid(harness):
    async def scenario(h):
        p = await h.ping()
        assert (p["version"], p["build"], p["api"], p["pid"]) == (__version__, BUILD, API_VERSION,
                                                                  os.getpid())
        assert API_VERSION == 2 and re.fullmatch(r"[0-9a-f]{12}", p["build"])
        assert p["connected"] and p["motion"] is None          # the old keys are all still there
        assert p["stopping"] is False
    harness.run(scenario)


def test_ping_says_stopping_while_the_link_is_released(harness, monkeypatch):
    """After an accepted shutdown the service still answers while it releases the link
    (bounded at 5 s): its ping says "stopping", so that nobody takes connected=false for a
    dropped link or uses it."""
    released = asyncio.Event()

    async def slow_disconnect(self):
        await released.wait()
        self.is_connected = False
    monkeypatch.setattr(harness.fake_cls, "disconnect", slow_disconnect)

    async def scenario(h):
        f = await h.fresh()
        assert (await h.raw([{"op": "shutdown"}]))[2][0] == {"ok": True}
        await _until(lambda: any("stopping on request" in x for x in h.log))
        p = await h.ping()
        assert p["stopping"] is True and p["ok"] is True
        released.set()
        await _until(lambda: h.task.done())
        assert f.writes == []
    harness.run(scenario)


def test_the_start_line_names_version_build_pid_and_date(harness):
    async def scenario(h):
        return list(h.log)
    log = harness.run(scenario)
    assert re.fullmatch(rf"OpenCutter {re.escape(__version__)} \(build {BUILD}\) background "
                        rf"service started, pid {os.getpid()}, \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\d"
                        r"[+-]\d\d:\d\d", log[0]), log[0]


def test_a_session_from_another_build_is_refused_and_nothing_is_sent(harness):
    async def scenario(h):
        f = await h.fresh()
        other = {"op": "motion", "program": "dry-run", "options": {}, "build": "0123456789ab",
                 "api": API_VERSION}
        reply = (await h.raw([other]))[2][0]
        assert reply["ok"] is False and "`opencutter service stop`" in reply["error"]
        assert f"OpenCutter {__version__} (build {BUILD})" in reply["error"]
        newer_api = {**other, "build": BUILD, "api": API_VERSION + 1}
        assert (await h.raw([newer_api]))[2][0]["ok"] is False
        assert f.writes == []
        # the same build, and an older client that sends no build, are served as before
        r, w, out = await h.raw([{**other, "build": BUILD}])
        assert out[0]["ok"], out
        w.close()
        await _session_over(h)
        r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}}])
        assert out[0]["ok"], out
        w.close()
        await _session_over(h)
    harness.run(scenario)


# --- the shutdown op ---------------------------------------------------------------------------
def test_shutdown_when_idle_ends_cleanly_and_sends_nothing(harness):
    async def scenario(h):
        f = await h.fresh()
        reply = await h.client().shutdown()
        assert reply == {"ok": True, "pid": os.getpid()}     # SO_PEERCRED: this very process
        assert h.task.done() and h.task.result() is None      # a clean exit, not a cancellation
        assert f.writes == []                                 # nothing was sent to the machine
        assert not f.is_connected                             # the link was released at once
        assert not os.path.exists(h.socket)
        for path in (h.lock, h.old_lock, h.legacy_lock):
            assert flock_free(path), path
        assert await h.client().ping() is None
        return list(h.log)
    log = harness.run(scenario)
    assert "stopping on request; the machine is released for other programs" in log
    assert "Bluetooth link released" in log
    assert log.index("Bluetooth link released") > log.index(
        "stopping on request; the machine is released for other programs")


def test_shutdown_is_refused_while_a_session_is_open(harness):
    async def scenario(h):
        f = await h.fresh()
        r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}}])
        assert out[0]["ok"]
        reply = (await h.raw([{"op": "shutdown"}]))[2][0]
        assert reply == {"ok": False, "error": daemon.SESSION_RUNNING}
        assert "`opencutter stop`" in reply["error"]
        assert (await h.ping())["motion"] == "dry-run" and not h.task.done()
        n = len(f.writes)
        w.close()                                            # the session ends (no step ran)
        await _session_over(h)
        assert stops(f.writes[n:]) == 0
        assert (await h.client().shutdown())["ok"] is True
        assert h.task.done()
    harness.run(scenario)


def test_once_stopping_no_session_is_opened(harness, monkeypatch):
    """A connection accepted before the shutdown and sending its session request after it is
    refused: from the accepted shutdown on, nothing more can start."""
    async def scenario(h):
        f = await h.fresh()
        early_r, early_w = await asyncio.open_unix_connection(h.socket)   # accepted, says nothing yet
        await asyncio.sleep(0.1)
        assert (await h.client().shutdown())["ok"] is True
        early_w.write(b'{"op":"motion","program":"dry-run","options":{}}\n')
        await early_w.drain()
        line = await asyncio.wait_for(early_r.readline(), 5)
        early_w.close()
        assert b"the background service is stopping" in line
        assert f.writes == []
    harness.run(scenario)


def test_shutdown_works_without_a_link_and_twice_is_harmless(harness):
    async def scenario(h):
        first = h.fake
        h.connected_prop = True                      # keep it from reconnecting
        first.is_connected = False
        first.cb_disc(first)
        await asyncio.sleep(0.3)
        assert (await h.ping())["connected"] is False
        assert (await h.client().shutdown())["ok"] is True
        assert (await h.client().shutdown())["ok"] is False     # nobody there any more
        assert first.writes[-1:] != [ESC09]
    harness.run(scenario)


def test_the_app_started_while_the_service_ends_starts_a_new_one(harness, monkeypatch):
    """The idle time ended and the service is still releasing the link when the app starts (or
    Connect is pressed): the app waits until that service has gone, then starts a new one,
    instead of staying Not Connected with none."""
    released = asyncio.Event()

    async def slow_disconnect(self):
        await released.wait()
        self.is_connected = False
    monkeypatch.setattr(harness.fake_cls, "disconnect", slow_disconnect)
    spawned = []

    class Started:                      # the new service (here a process that already exited)
        pid = 2 ** 22 + 11

        def poll(self):
            return 0

    def spawn():
        spawned.append((os.path.exists(harness.socket), flock_free(harness.lock)))
        return Started(), None
    monkeypatch.setattr(client_mod, "_spawn_daemon", spawn)

    async def main():
        await harness.start(idle_exit=0.5)
        try:
            await _until(lambda: any("no request for" in x for x in harness.log))
            client = client_mod.DaemonClient()
            assert client.manages_service and client.socket_path == harness.socket
            assert (await client.ping())["stopping"] is True
            monitor = machine.MachineMonitor(client)
            connect = asyncio.ensure_future(monitor.connect())
            await asyncio.sleep(0.5)
            assert spawned == [] and not connect.done()          # it waits for the old one
            released.set()
            await asyncio.wait_for(connect, 30)
            return client.spawned_pid
        finally:
            await harness.shutdown()
    assert asyncio.run(main()) == Started.pid
    assert spawned == [(False, True)]        # once the old one had gone and freed its lock


# --- the idle exit -----------------------------------------------------------------------------
async def _quiet(h, seconds: float) -> None:
    """Wait without any request (a ping would start the idle time again)."""
    await asyncio.sleep(seconds)


def test_idle_exit_after_the_time_without_requests(harness):
    async def scenario(h):
        f = await h.fresh()
        await _quiet(h, 0.5)
        assert not h.task.done()                     # 1 s not over yet
        await _until(lambda: h.task.done(), timeout=5)
        assert h.task.result() is None and f.writes == [] and not f.is_connected
        assert not os.path.exists(h.socket) and flock_free(h.lock)
        return list(h.log)

    async def main():
        await harness.start(idle_exit=1.0)
        try:
            return await asyncio.wait_for(scenario(harness), 30)
        finally:
            await harness.shutdown()
    log = asyncio.run(main())
    assert ("no request for 1 s and no job: the background service stops and releases the "
            "machine for other programs") in log
    assert "stops by itself after 1 s without requests and without a job" in log


def test_pings_keep_it_running(harness):
    async def scenario(h):
        for _ in range(8):                           # 1.6 s of pings every 0.2 s (the app: 2 s)
            await asyncio.sleep(0.2)
            assert (await h.ping())["ok"] and not h.task.done()

    async def main():
        await harness.start(idle_exit=0.6)
        try:
            await asyncio.wait_for(scenario(harness), 30)
            await _until(lambda: harness.task.done(), timeout=5)      # quiet: then it ends
        finally:
            await harness.shutdown()
    asyncio.run(main())


def test_no_idle_exit_while_a_session_is_open(harness):
    async def scenario(h):
        await h.fresh()
        r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}}])
        assert out[0]["ok"]
        await _quiet(h, 1.5)                         # three times the idle time, session open
        assert not h.task.done()
        w.close()                                    # the session ends: the idle time starts
        await asyncio.sleep(0.2)
        assert not h.task.done()
        await _until(lambda: h.task.done(), timeout=5)

    async def main():
        await harness.start(idle_exit=0.5)
        try:
            await asyncio.wait_for(scenario(harness), 30)
        finally:
            await harness.shutdown()
    asyncio.run(main())


def test_no_idle_exit_without_the_option(harness):
    async def scenario(h):
        await _quiet(h, 1.5)
        assert not h.task.done()
        return list(h.log)
    log = harness.run(scenario)                      # serve(None, False): idle_exit 0
    assert not [line for line in log if "stops by itself" in line or "no request" in line]
