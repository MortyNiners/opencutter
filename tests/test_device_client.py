# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""DaemonClient and Session against fake daemon sockets (no daemon, no Bluetooth, no spawning)."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from opencutter import APP_ID
from opencutter.device import client as client_mod
from opencutter.device.client import DaemonClient, DaemonError
from opencutter.identity import API_VERSION, BUILD

SERVE = [sys.executable, "-m", "opencutter", "serve", "--idle-exit", "900"]


@pytest.fixture
def rundir(monkeypatch):
    """A private, short $XDG_RUNTIME_DIR (and $XDG_STATE_HOME) for one test, with the service's
    directory app/<app id> in it (the daemon creates it; the fake daemons here do not)."""
    d = tempfile.mkdtemp(prefix="occ-", dir="/tmp" if len(tempfile.gettempdir()) > 60 else None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    monkeypatch.setenv("XDG_STATE_HOME", d)
    monkeypatch.delenv(client_mod.SOCKET_ENV, raising=False)
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    os.makedirs(os.path.join(d, "app", APP_ID), mode=0o700)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def no_spawn(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("TEST BUG: tried to start a process")
    monkeypatch.setattr(subprocess, "Popen", refuse)


async def fake_daemon(path, answer=lambda req: {"ok": True, "connected": False}, log=None):
    """A Unix server that answers each JSON line with answer(request)."""
    async def handle(reader, writer):
        while line := await reader.readline():
            req = json.loads(line)
            if log is not None:
                log.append(req)
            reply = answer(req)
            if reply is None:
                break
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        writer.close()
    return await asyncio.start_unix_server(handle, path=path)


def test_default_socket_is_in_the_runtime_dir(rundir):
    assert DaemonClient().socket_path == os.path.join(rundir, "app", APP_ID, "opencutter.sock")
    assert DaemonClient().manages_service
    assert client_mod.older_socket_path() == os.path.join(rundir, "opencutter.sock")
    assert client_mod.legacy_socket_path() == os.path.join(rundir, "cameo4.sock")


def test_no_daemon(rundir, no_spawn):
    async def scenario():
        c = DaemonClient()
        assert await c.ping() is None
        with pytest.raises(DaemonError):
            await c.info()
        stop = await c.stop()
        assert stop["ok"] is False and "no OpenCutter daemon" in stop["error"]
        with pytest.raises(DaemonError):
            async with c.session():
                pass
    asyncio.run(scenario())


def test_socket_env_overrides_the_default_and_never_spawns(rundir, monkeypatch, no_spawn):
    other = os.path.join(rundir, "fake.sock")
    monkeypatch.setenv("OPENCUTTER_SOCKET", other)
    assert DaemonClient().socket_path == other
    assert DaemonClient(os.path.join(rundir, "given.sock")).socket_path.endswith("given.sock")
    assert asyncio.run(DaemonClient().ensure_daemon()) is False


def test_no_runtime_dir(monkeypatch, no_spawn):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv(client_mod.SOCKET_ENV, raising=False)

    async def scenario():
        c = DaemonClient()
        assert c.socket_path is None
        assert await c.ping() is None
        assert (await c.stop())["ok"] is False
        assert await c.ensure_daemon() is False
    asyncio.run(scenario())


def test_ensure_daemon_does_not_spawn_when_a_daemon_answers(rundir, no_spawn):
    async def scenario():
        c = DaemonClient()
        server = await fake_daemon(c.socket_path)
        try:
            assert await c.ensure_daemon() is True
            assert await c.ping() == {"ok": True, "connected": False}
        finally:
            server.close()
    asyncio.run(scenario())


def test_ensure_daemon_waits_for_a_stopping_service_that_does_not_end(rundir, no_spawn,
                                                                     monkeypatch):
    """A service that keeps answering 'stopping' is not started next to (its locks are still
    held): after the wait, `spawn_error` says so, and the app maps it to plain words."""
    monkeypatch.setattr(client_mod, "ENDING_WAIT", 0.5)

    async def scenario():
        c = DaemonClient()
        server = await fake_daemon(c.socket_path, lambda req: {"ok": True, "connected": False,
                                                               "stopping": True})
        try:
            started = asyncio.get_running_loop().time()
            assert await c.ensure_daemon() is False
            assert asyncio.get_running_loop().time() - started >= 0.5
            assert c.spawn_error.startswith("the OpenCutter daemon that was stopping did not "
                                            "end within")
            assert c.spawned_pid is None                         # nothing was started
        finally:
            server.close()
    asyncio.run(scenario())
    from opencutter.gui import machine
    assert machine.startup_problem("the OpenCutter daemon that was stopping did not end within "
                                   "25 s") == machine.SERVICE_ENDING


def test_ensure_daemon_never_spawns_for_another_socket(rundir, no_spawn):
    async def scenario():
        assert await DaemonClient(os.path.join(rundir, "other.sock")).ensure_daemon() is False
    asyncio.run(scenario())


class FakePopen:
    """Records the spawn instead of running anything; can start a fake daemon in its place."""
    calls: list = []
    exit_code = None
    on_spawn = None
    pid = 2 ** 22 + 8                       # no such process

    def __init__(self, args, **kwargs):
        FakePopen.calls.append((args, kwargs))
        if FakePopen.on_spawn:
            FakePopen.on_spawn()

    def poll(self):
        return FakePopen.exit_code


@pytest.fixture
def fake_popen(monkeypatch):
    monkeypatch.setattr(FakePopen, "calls", [])
    monkeypatch.setattr(FakePopen, "exit_code", None)
    monkeypatch.setattr(FakePopen, "on_spawn", None)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return FakePopen


def test_ensure_daemon_spawns_serve_detached(rundir, fake_popen):
    async def scenario():
        c = DaemonClient()
        servers = []
        fake_popen.on_spawn = lambda: servers.append(
            asyncio.get_running_loop().create_task(fake_daemon(c.socket_path)))
        try:
            assert await c.ensure_daemon() is True
        finally:
            for t in servers:
                (await t).close()
        (args, kwargs), = fake_popen.calls
        assert args == SERVE                         # it ends 15 minutes after the app
        assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["stdout"].name == os.path.join(rundir, "opencutter", "daemon.log")
    asyncio.run(scenario())


def test_ensure_daemon_reports_a_daemon_that_exits(rundir, fake_popen):
    fake_popen.exit_code = 1

    async def scenario():
        assert await DaemonClient().ensure_daemon(timeout=2) is False
        assert len(fake_popen.calls) == 1
    asyncio.run(scenario())


def test_stop_never_raises_on_a_broken_daemon(rundir):
    async def scenario():
        c = DaemonClient()
        server = await fake_daemon(c.socket_path, answer=lambda req: None)   # hangs up
        try:
            stop = await c.stop()
        finally:
            server.close()
        assert stop["ok"] is False and "closed the connection" in stop["error"]
    asyncio.run(scenario())


def test_session_protocol(rundir):
    log = []

    def answer(req):
        if req["op"] == "job":
            return {"ok": True, "steps": [{"kind": "write"}]}
        if req["op"] == "step":
            return {"ok": True, "status": "0", "seconds": 0.1}
        if req["op"] == "motion":
            return {"ok": False, "error": "refused: test"}
        return None                      # "end": hang up, end() must not raise

    async def scenario():
        c = DaemonClient()
        server = await fake_daemon(c.socket_path, answer, log)
        try:
            async with c.session() as s:
                assert (await s.open_job({"mat": "12x12"}))["ok"]
                assert (await s.step(0))["seconds"] == 0.1
                await s.end()
                with pytest.raises(DaemonError):
                    await s.step(1)
            async with c.session() as s:
                assert (await s.open_program("dry-run", {}))["ok"] is False
        finally:
            server.close()
        me = {"build": BUILD, "api": API_VERSION}       # a service of another build refuses
        assert log == [{"op": "job", "spec": {"mat": "12x12"}, **me}, {"op": "step", "index": 0},
                       {"op": "end"}, {"op": "motion", "program": "dry-run", "options": {}, **me}]
    asyncio.run(scenario())


def test_session_step_timeout_raises(rundir):
    async def scenario():
        c = DaemonClient()

        async def silent(reader, writer):
            await reader.read()
        server = await asyncio.start_unix_server(silent, path=c.socket_path)
        try:
            async with c.session() as s:
                with pytest.raises(DaemonError, match="no answer"):
                    await s.step(0, timeout=0.3)
        finally:
            server.close()
    asyncio.run(scenario())


def test_client_never_imports_bleak():
    code = ("import sys; import opencutter.device.client, opencutter.__main__; "
            "sys.exit(any(m.split('.')[0] in ('bleak', 'dbus_fast') for m in sys.modules))")
    src = os.path.dirname(os.path.dirname(client_mod.__file__))
    env = {**os.environ, "PYTHONPATH": os.path.dirname(src)}
    assert subprocess.run([sys.executable, "-c", code], env=env, timeout=60).returncode == 0


# --- why a started daemon did not come up ----------------------------------------------------
LEGACY = ("the legacy prototype daemon holds cameo4.lock; stop it first "
          "(lock /tmp/occ-x/cameo4.lock is held)")


def test_ensure_daemon_reports_the_last_line_of_a_daemon_that_exits(rundir, fake_popen):
    log = os.path.join(rundir, "opencutter", "daemon.log")
    os.makedirs(os.path.dirname(log), mode=0o700)
    with open(log, "w") as f:                       # an earlier run's lines are not this start's
        f.write("09:00:00 listening on /tmp/old.sock\nan old error from yesterday\n")
    fake_popen.exit_code = 1

    def daemon_writes():
        _args, kwargs = fake_popen.calls[-1]
        kwargs["stdout"].write(LEGACY.encode() + b"\n")
        kwargs["stdout"].flush()
    fake_popen.on_spawn = daemon_writes

    async def scenario():
        c = DaemonClient()
        assert await c.ensure_daemon(timeout=2) is False
        assert c.spawn_error == LEGACY
    asyncio.run(scenario())


def test_ensure_daemon_ignores_older_lines_when_the_daemon_wrote_none(rundir, fake_popen):
    log = os.path.join(rundir, "opencutter", "daemon.log")
    os.makedirs(os.path.dirname(log), mode=0o700)
    with open(log, "w") as f:
        f.write(LEGACY + "\n")                      # yesterday's refusal
    fake_popen.exit_code = 3

    async def scenario():
        c = DaemonClient()
        assert await c.ensure_daemon(timeout=2) is False
        assert c.spawn_error == "the daemon exited with status 3"
        server = await fake_daemon(c.socket_path)
        try:
            assert await c.ensure_daemon() is True and c.spawn_error is None
        finally:
            server.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["fifo", "symlink", "unwritable"])
def test_ensure_daemon_never_blocks_on_or_follows_a_planted_log(rundir, fake_popen, kind):
    """ensure_daemon runs on the GUI's main loop: a FIFO as daemon.log must not hang it, a
    symlink must not redirect the daemon's output, and a log it cannot write must not keep the
    daemon from starting (it then starts without a log)."""
    import threading
    log = os.path.join(rundir, "opencutter", "daemon.log")
    os.makedirs(os.path.dirname(log), mode=0o700)
    target = os.path.join(rundir, "elsewhere.log")
    with open(target, "w") as f:
        f.write(LEGACY + "\n")
    if kind == "fifo":
        os.mkfifo(log)
    elif kind == "symlink":
        os.symlink(target, log)
    else:
        open(log, "w").close()
        os.chmod(log, 0)
    fake_popen.exit_code = 1
    done, rescued = threading.Event(), []

    def rescue():                                   # a blocked open of the FIFO: let it go
        if kind == "fifo" and not done.wait(5):
            rescued.append(True)
            os.close(os.open(log, os.O_RDONLY | os.O_NONBLOCK))
    threading.Thread(target=rescue, daemon=True).start()

    async def scenario():
        c = DaemonClient()
        try:
            assert await c.ensure_daemon(timeout=2) is False
        finally:
            done.set()
        return c
    c = asyncio.run(scenario())
    assert not rescued, "opening daemon.log blocked the event loop"
    (args, kwargs), = fake_popen.calls
    assert args == SERVE
    assert kwargs["stdout"] == subprocess.DEVNULL     # started anyway, without a log
    assert c.spawn_error == "the daemon exited with status 1"    # nothing read through a link
    with open(target) as f:
        assert f.read() == LEGACY + "\n"


def test_last_log_line_reads_only_the_tail_after_the_offset(tmp_path):
    path = tmp_path / "daemon.log"
    path.write_bytes(b"old line\n" + b"12:34:56 listening on /x\n\n" + b"y" * 9000 + b"\n"
                     + b"12:34:59 waiting for the machine (switch it off and on): Error: x\n\n")
    line = client_mod.last_log_line(str(path), 9)
    assert line == "waiting for the machine (switch it off and on): Error: x"   # no time stamp
    size = path.stat().st_size
    assert client_mod.last_log_line(str(path), size) is None                 # nothing new
    assert client_mod.last_log_line(str(path), size + 100) is not None       # truncated: all
    assert client_mod.last_log_line(str(tmp_path / "missing.log"), 0) is None
    path.write_bytes(b"\xff\xfe broken \xc3\n")
    assert client_mod.last_log_line(str(path), 0) == "�� broken �"
    path.write_bytes(b"z" * 5000)
    assert client_mod.last_log_line(str(path), 0) == "z" * client_mod.LOG_LINE_CHARS


def test_last_log_line_never_blocks_on_a_fifo(tmp_path):
    fifo = tmp_path / "daemon.log"
    os.mkfifo(fifo)                                 # opening it for reading would wait forever
    assert client_mod.last_log_line(str(fifo), 0) is None
    link = tmp_path / "link.log"
    link.symlink_to(tmp_path / "elsewhere.log")
    (tmp_path / "elsewhere.log").write_text("x\n")
    assert client_mod.last_log_line(str(link), 0) is None
