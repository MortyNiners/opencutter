# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Managing the background service from the client side: the guarded SIGTERM for an older
service without the shutdown op, the shutdown op, `opencutter service stop|status`,
`opencutter --version`, the build check of the CLI's motion commands, the daemon log at a
spawn, and the app replacing an older service end to end.

Every service here is a stand-in that the test itself starts in a private temporary
$XDG_RUNTIME_DIR (tests/service_stubs.py), or the real daemon on the simulated machine
(tests/stubs.py). Nothing reaches Bluetooth, D-Bus or the user's own service.
"""
import ast
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import types

import pytest

import service_stubs as ss
import stubs
from stubs import harness, stops  # noqa: F401

stubs.install()
from opencutter import APP_ID, __main__ as cli, __version__  # noqa: E402
from opencutter.device import client as client_mod  # noqa: E402
from opencutter.device.client import (DaemonClient, is_service_cmdline,  # noqa: E402
                                      process_cmdline, terminate_older_daemon)
from opencutter.gui.machine import APP_OUTDATED, Link, MachineMonitor  # noqa: E402
from opencutter.identity import API_VERSION, BUILD  # noqa: E402

OLDER_PING = {"ok": True, "connected": True, "status": "0", "motion": None,
              "device": {"name": "CAMEO 4-older", "address": "00:00:00:00:00:02"}, "reason": None}


@pytest.fixture
def rundir(monkeypatch):
    """A private, short $XDG_RUNTIME_DIR (and state dir); no Flatpak id, no socket override."""
    d = tempfile.mkdtemp(prefix="ocv-", dir="/tmp" if len(tempfile.gettempdir()) > 60 else None)
    os.chmod(d, 0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    monkeypatch.setenv("XDG_STATE_HOME", d)
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.delenv(client_mod.SOCKET_ENV, raising=False)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def procs():
    """The test's own child processes: ended (killed if need be) at teardown."""
    started: list[subprocess.Popen] = []
    yield started
    ss.finish(*started)


def _older(rundir, procs, reply=OLDER_PING, name="opencutter", path=None, lock=True,
           glued_c=False):
    path = path or os.path.join(rundir, "opencutter.sock")
    proc, record = ss.start_older(rundir, path, reply, name=name,
                                  lock=os.path.join(rundir, "opencutter.lock") if lock else None,
                                  glued_c=glued_c)
    procs.append(proc)
    return proc, record, path


# --- which processes count as a service ------------------------------------------------------
@pytest.mark.parametrize("argv, ok", [
    (["/usr/bin/python3", "-m", "opencutter", "serve"], True),
    (["/x/.venv/bin/python", "-m", "opencutter", "serve"], True),
    (["python3.14", "-I", "-X", "dev", "-m", "opencutter", "serve", "--idle-exit", "900"], True),
    (["/usr/bin/python3", "-mopencutter", "serve"], True),
    (["/usr/bin/python3", "-m", "opencutter.__main__", "serve", "-v"], True),
    (["/usr/bin/python3", "/app/bin/opencutter", "serve"], True),          # the installed command
    (["/usr/bin/python3", "-m", "opencutter"], False),                      # the app itself
    (["/usr/bin/python3", "-m", "opencutter", "stop"], False),
    (["/usr/bin/python3", "-m", "opencutter", "--force", "serve"], False),
    (["/usr/bin/python3", "-m", "opencutterx", "serve"], False),
    (["/usr/bin/python3", "/tmp/notopencutter", "serve"], False),
    (["/usr/bin/python3", "-c", "import opencutter", "opencutter", "serve"], False),
    # a command string glued to -c, alone or in a cluster: what follows is only its argv
    (["/usr/bin/python3", "-cimport time; time.sleep(9)", "opencutter", "serve"], False),
    (["/usr/bin/python3", "-Ecimport x", "opencutter", "serve"], False),
    (["/usr/bin/python3", "-Ic", "import x", "opencutter", "serve"], False),
    (["/usr/bin/python3", "-", "opencutter", "serve"], False),             # a program on stdin
    (["/usr/bin/python3", "-Es", "-mopencutter", "serve"], True),           # clusters of flags
    (["/usr/bin/python3", "-Xdev", "-Wignore", "-Im", "opencutter", "serve"], True),
    (["/usr/bin/python3", "-W", "-c", "-m", "opencutter", "serve"], True),  # -c is -W's value
    (["/usr/bin/python3", "-X", "-mopencutter", "serve"], False),           # -m.. is -X's value
    (["/usr/bin/python3", "--check-hash-based-pycs", "never", "-m", "opencutter", "serve"], True),
    (["/usr/bin/python3", "-m"], False),
    (["/usr/bin/bash", "-m", "opencutter", "serve"], False),                # not Python
    (["/usr/bin/opencutter", "serve"], False),
    ([], False), (None, False),
])
def test_service_command_lines(argv, ok):
    assert is_service_cmdline(argv) is ok


def test_process_cmdline_reads_proc():
    argv = process_cmdline(os.getpid())
    assert argv and os.path.basename(argv[0]).startswith("python") and "pytest" in " ".join(argv)
    assert process_cmdline(2 ** 22 + 7) is None


# --- the guarded SIGTERM ---------------------------------------------------------------------
def test_an_older_service_is_terminated_and_its_lock_freed(rundir, procs):
    proc, record, path = _older(rundir, procs)
    result = asyncio.run(terminate_older_daemon(path))
    assert result == {"ok": True, "pid": proc.pid}
    assert proc.wait(5) == 0 and ss.ops(record) == ["ping", "SIGTERM"]    # a ping right before
    assert not os.path.exists(path)


def test_the_pid_comes_from_the_socket_not_from_the_reply(rundir, procs):
    """Two stand-ins; the one asked about claims the other's pid in its reply: only the process
    that really listens on that socket (SO_PEERCRED) gets the signal."""
    other, other_record, _ = _older(rundir, procs, path=os.path.join(rundir, "b.sock"), lock=False)
    proc, record, path = _older(rundir, procs, reply={**OLDER_PING, "pid": other.pid}, lock=False)
    result = asyncio.run(terminate_older_daemon(path))
    assert result["ok"] and result["pid"] == proc.pid
    assert proc.wait(5) == 0 and other.poll() is None and "SIGTERM" not in ss.ops(other_record)


def test_never_while_it_runs_a_job(rundir, procs):
    proc, record, path = _older(rundir, procs, reply={**OLDER_PING, "motion": "job"})
    result = asyncio.run(terminate_older_daemon(path))
    assert result == {"ok": False, "error": "it is running job", "pid": proc.pid, "busy": True}
    assert proc.poll() is None and ss.ops(record) == ["ping"]


def test_never_a_python_command_string(rundir, procs):
    """`python -c<code> opencutter serve` answers like an older service, but the words after
    the code are only that code's arguments: it is not OpenCutter, and gets no signal."""
    proc, record, path = _older(rundir, procs, glued_c=True)
    assert process_cmdline(proc.pid)[1].startswith("-c")
    result = asyncio.run(terminate_older_daemon(path))
    assert not result["ok"] and result["pid"] == proc.pid and not result["busy"]
    assert result["error"].startswith(f"process {proc.pid} is not an OpenCutter background service")
    assert proc.poll() is None and "SIGTERM" not in ss.ops(record)


def test_never_a_process_that_is_not_a_service(rundir, procs):
    proc, record, path = _older(rundir, procs, name="notopencutter")
    result = asyncio.run(terminate_older_daemon(path))
    assert not result["ok"] and result["pid"] == proc.pid and not result["busy"]
    assert result["error"].startswith(f"process {proc.pid} is not an OpenCutter background service")
    assert proc.poll() is None and ss.ops(record) == []                    # not even pinged


def test_never_a_process_of_another_user(rundir, procs, monkeypatch):
    proc, record, path = _older(rundir, procs)
    real = client_mod.peer_credentials
    monkeypatch.setattr(client_mod, "peer_credentials",
                        lambda sock: (real(sock)[0], os.getuid() + 1))
    result = asyncio.run(terminate_older_daemon(path))
    assert not result["ok"] and "belongs to another user" in result["error"]
    assert proc.poll() is None and ss.ops(record) == []


@pytest.mark.parametrize("cmdline", [None, [], ["/usr/bin/python3", "-m", "opencutter"]])
def test_never_when_proc_does_not_show_a_service(rundir, procs, monkeypatch, cmdline):
    """/proc read stubbed: unreadable, empty (a zombie), or the app instead of `serve`."""
    proc, record, path = _older(rundir, procs)
    monkeypatch.setattr(client_mod, "process_cmdline", lambda pid: cmdline)
    result = asyncio.run(terminate_older_daemon(path))
    assert not result["ok"] and "is not an OpenCutter background service" in result["error"]
    assert proc.poll() is None


def test_never_an_invisible_process_or_this_one(rundir, procs, monkeypatch):
    proc, record, path = _older(rundir, procs)
    monkeypatch.setattr(client_mod, "peer_credentials", lambda sock: (0, os.getuid()))
    assert "cannot be seen" in asyncio.run(terminate_older_daemon(path))["error"]
    monkeypatch.setattr(client_mod, "peer_credentials", lambda sock: (os.getpid(), os.getuid()))
    assert "this very process" in asyncio.run(terminate_older_daemon(path))["error"]
    assert proc.poll() is None


def test_nothing_there(rundir):
    result = asyncio.run(terminate_older_daemon(os.path.join(rundir, "opencutter.sock")))
    assert not result["ok"] and "no OpenCutter daemon answers" in result["error"]
    assert asyncio.run(terminate_older_daemon(None))["ok"] is False


# --- opencutter service stop | status, --version ----------------------------------------------
def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out == f"opencutter {__version__} (build {BUILD}, api {API_VERSION})\n"


def test_service_status_and_stop_of_this_build(harness, capsys):
    async def scenario(h):
        f = await h.fresh()
        assert await cli.service_status() is True
        status = capsys.readouterr().out
        assert await cli.service_stop() is True
        assert h.task.done() and f.writes == [] and not f.is_connected
        return status
    status = harness.run(scenario)
    out, err = capsys.readouterr()
    assert status.splitlines() == [
        f"OpenCutter background service at {harness.socket}",
        f"  version: {__version__}",
        f"  build:   {BUILD} (the same as this command)",
        f"  api:     {API_VERSION}",
        f"  pid:     {os.getpid()}",
        "  machine: CAMEO 4-fake [00:00:00:00:00:01], connected",
        "  model:   CAMEO 4 V5.05",
        "  job:     none"]
    assert out == (f"Stopped OpenCutter {__version__} (build {BUILD}) (pid {os.getpid()}) at "
                   f"{harness.socket}; the machine is free for other programs.\n")
    assert err == ""


def test_service_stop_refuses_while_a_job_runs(harness, capsys):
    async def scenario(h):
        f = await h.fresh()
        r, w, out = await h.raw([{"op": "motion", "program": "dry-run", "options": {}}])
        assert out[0]["ok"]
        stopped = await cli.service_stop()
        alive = not h.task.done()
        w.close()
        return stopped, alive, list(f.writes)
    stopped, alive, writes = harness.run(scenario)
    out, err = capsys.readouterr()
    assert stopped is False and alive and stops(writes) == 0
    assert "!! Not stopped:" in err and "is running dry-run" in err
    assert "Cancel it in the app, or stop the machine with `opencutter stop`" in err


def test_service_stop_and_status_of_an_older_service(rundir, procs, capsys):
    os.makedirs(os.path.join(rundir, "app", APP_ID), mode=0o700)
    proc, record, path = _older(rundir, procs)
    assert asyncio.run(cli.service_status()) is True
    status = capsys.readouterr().out
    assert f"OpenCutter background service: not running ({cli.service_targets()[0]})" in status
    assert f"OpenCutter background service at {path}" in status
    assert "  version: unknown (an older service)" in status
    assert "  build:   none (an older service): run `opencutter service stop`" in status
    assert f"  pid:     {proc.pid}" in status                     # from SO_PEERCRED
    assert asyncio.run(cli.service_stop()) is True
    out = capsys.readouterr().out
    assert (f"Stopped an older OpenCutter background service (it reports no version) "
            f"(pid {proc.pid}) at {path}; the machine is free for other programs.") in out
    assert proc.wait(5) == 0 and ss.ops(record)[-1] == "SIGTERM"


def test_service_stop_with_nothing_running(rundir, capsys):
    assert cli.main(["service", "stop"]) == 0
    assert capsys.readouterr().out == (f"OpenCutter background service: not running "
                                       f"({client_mod.service_socket_path()})\n")
    with pytest.raises(SystemExit) as e:
        cli.main(["service", "status"])
    assert e.value.code == 1


def test_service_stop_exits_1_while_something_runs(rundir, procs, capsys):
    proc, record, path = _older(rundir, procs, reply={**OLDER_PING, "motion": "job"})
    with pytest.raises(SystemExit) as e:
        cli.main(["service", "stop"])
    assert e.value.code == 1 and proc.poll() is None
    assert "is running a job from the app" in capsys.readouterr().err


def test_the_machine_stop_says_what_it_does():
    help_text = cli._parser().format_help()
    assert "stop the MACHINE now (ESC 09 twice)" in help_text
    assert "the service keeps running" in help_text or "service keeps running" in help_text
    assert "service stop" in help_text and "service status" in help_text


# --- the CLI never runs motion on another build ------------------------------------------------
@pytest.mark.parametrize("reply", [{**OLDER_PING, "version": "0.0.9", "build": "0123456789ab",
                                    "api": API_VERSION}, OLDER_PING])
def test_motion_commands_refuse_another_build(rundir, monkeypatch, reply):
    path = os.path.join(rundir, "dev.sock")
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    seen = []

    async def scenario():
        async def handle(reader, writer):
            seen.append(json.loads(await reader.readline())["op"])
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            writer.close()
        server = await asyncio.start_unix_server(handle, path=path)
        try:
            await cli.run_program(DaemonClient(path), "dry-run", {})
        finally:
            server.close()
    with pytest.raises(SystemExit) as refused:
        asyncio.run(scenario())
    assert "Run `opencutter service stop` first" in str(refused.value)
    assert f"this command is OpenCutter {__version__} (build {BUILD})" in str(refused.value)
    assert seen == ["ping"]                                    # no session was opened


def test_commands_name_an_older_service_on_the_old_socket(rundir, procs, monkeypatch):
    os.makedirs(os.path.join(rundir, "app", APP_ID), mode=0o700)
    _older(rundir, procs)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    for argv in (["status"], ["dry-run"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert "An older OpenCutter background service is running" in str(e.value.code)
        assert "`opencutter service stop`" in str(e.value.code)


# --- the daemon log at a spawn -----------------------------------------------------------------
class _NoProcess:
    calls: list = []
    pid = 2 ** 22 + 9                       # no such process

    def __init__(self, args, **kwargs):
        _NoProcess.calls.append((args, kwargs))

    def poll(self):
        return 1


def test_a_large_log_is_rotated_at_a_spawn(rundir, monkeypatch):
    monkeypatch.setattr(_NoProcess, "calls", [])
    monkeypatch.setattr(subprocess, "Popen", _NoProcess)
    log = os.path.join(rundir, "opencutter", "daemon.log")
    os.makedirs(os.path.dirname(log), mode=0o700)
    big = b"09:00:00 an old line\n" * (client_mod.LOG_ROTATE_BYTES // 20)
    with open(log, "wb") as f:
        f.write(big)
    with open(log + ".1", "wb") as f:
        f.write(b"the one before\n")
    asyncio.run(DaemonClient().ensure_daemon(timeout=1))
    (args, kwargs), = _NoProcess.calls
    assert args == [sys.executable, "-m", "opencutter", "serve", "--idle-exit", "900"]
    assert kwargs["stdout"].name == log
    with open(log + ".1", "rb") as f:
        assert f.read() == big                                  # the old log, whole
    assert os.path.getsize(log) == 0                             # a fresh one for this start
    asyncio.run(DaemonClient().ensure_daemon(timeout=1))
    with open(log + ".1", "rb") as f:
        assert f.read() == big                                   # small logs are not rotated


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_rotation_never_follows_a_link_or_a_fifo(rundir, monkeypatch, kind):
    monkeypatch.setattr(_NoProcess, "calls", [])
    monkeypatch.setattr(subprocess, "Popen", _NoProcess)
    log = os.path.join(rundir, "opencutter", "daemon.log")
    os.makedirs(os.path.dirname(log), mode=0o700)
    target = os.path.join(rundir, "elsewhere.log")
    with open(target, "wb") as f:
        f.write(b"x" * (client_mod.LOG_ROTATE_BYTES + 10))
    if kind == "symlink":
        os.symlink(target, log)
    else:
        os.mkfifo(log)
    asyncio.run(DaemonClient().ensure_daemon(timeout=1))
    (_args, kwargs), = _NoProcess.calls
    assert kwargs["stdout"] == subprocess.DEVNULL                # started without a log
    assert not os.path.lexists(log + ".1") and os.path.getsize(target) == client_mod.LOG_ROTATE_BYTES + 10


def test_the_start_line_and_last_log_line_after_a_spawn(rundir, procs):
    """A real daemon (stubbed machine) started in the private dir writes the start line first;
    last_log_line still reads only what this start wrote."""
    log = os.path.join(rundir, "daemon.log")
    with open(log, "w") as f:
        f.write("09:00:00 yesterday's last line\n")
    offset = os.path.getsize(log)
    proc = ss.spawn_stub_daemon(rundir, os.path.join(rundir, "w.json"), idle_exit=900, log=log)
    procs.append(proc)
    path = os.path.join(rundir, "app", APP_ID, "opencutter.sock")
    ss.wait_for(lambda: os.path.exists(path))
    assert asyncio.run(DaemonClient(path).shutdown())["ok"]
    assert proc.wait(10) == 0
    with open(log) as f:
        lines = f.read().splitlines()
    assert re.fullmatch(rf"\d\d:\d\d:\d\d OpenCutter {re.escape(__version__)} \(build {BUILD}\) "
                        rf"background service started, pid {proc.pid}, \d{{4}}-.*", lines[1])
    assert client_mod.last_log_line(log, offset) == "Bluetooth link released"
    with open(os.path.join(rundir, "w.json")) as f:
        machine = json.load(f)
    assert machine["connected"] is False and all(
        bytes.fromhex(w) in (b"\x1b\x05",) for w in machine["writes"])   # only the status query


# --- the app replaces an older service, end to end ----------------------------------------------
def test_the_app_replaces_an_older_service_end_to_end(rundir, procs, monkeypatch):
    """The owner's case: a service of a build before 0.1.0 runs on the old socket and holds the
    old lock. The app (MachineMonitor with the real DaemonClient) stops it with the guarded
    SIGTERM once it runs no job, starts its own (the real daemon on the simulated machine) and
    connects. Nothing is sent to the machine on the way."""
    older, record, old_path = _older(rundir, procs)
    writes = os.path.join(rundir, "w.json")
    spawned = []

    def spawn():
        proc = ss.spawn_stub_daemon(rundir, writes, idle_exit=900,
                                    log=os.path.join(rundir, "daemon.log"))
        procs.append(proc)
        spawned.append(proc)
        return proc, None
    monkeypatch.setattr(client_mod, "_spawn_daemon", spawn)
    client = DaemonClient()
    assert client.manages_service

    async def scenario():
        monitor = MachineMonitor(client)
        seen = []
        monitor.add_listener(lambda state: seen.append((state.link, state.note)))
        await monitor.connect()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while not monitor.state.connected:
            assert loop.time() < deadline, monitor.state
            await asyncio.sleep(0.2)
            await monitor.refresh()
        state = monitor.state
        ping = await client.ping()
        stopped = await client.shutdown()
        return seen, state, ping, stopped
    seen, state, ping, stopped = asyncio.run(scenario())
    assert (Link.CONNECTING, "Updating OpenCutter’s background service…") in seen
    # looked at twice by the app, then the guarded SIGTERM's own ping right before the signal
    assert older.wait(5) == 0 and ss.ops(record) == ["ping", "ping", "ping", "SIGTERM"]
    assert not os.path.exists(old_path)
    assert len(spawned) == 1 and state.connected and state.firmware == "CAMEO 4 V5.05"
    assert ping["build"] == BUILD and ping["pid"] == spawned[0].pid
    assert stopped["ok"] and spawned[0].wait(10) == 0
    with open(writes) as f:
        machine = json.load(f)
    assert {bytes.fromhex(w) for w in machine["writes"]} <= {b"\x1b\x05", b"\x1b\x15", b"FG\x03"}


def test_an_app_older_than_its_files_keeps_the_service_it_started(rundir, procs, monkeypatch,
                                                                 tmp_path):
    """OpenCutter's files changed while the app was open: the service it starts now (the real
    daemon from an edited copy of the package, on the simulated machine) reports another build.
    The app is the old side: it neither ends that service nor starts another (that would be the
    same build again, and every Try again would cost a Bluetooth reconnect); it says to restart."""
    copy = tmp_path / "src"
    shutil.copytree(ss.SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    with open(copy / "opencutter" / "svg" / "__init__.py", "a") as f:
        f.write("\n# edited after the app started\n")
    writes = os.path.join(rundir, "w.json")
    spawned = []

    def spawn():
        proc = ss.spawn_stub_daemon(rundir, writes, idle_exit=900, src=str(copy))
        procs.append(proc)
        spawned.append(proc)
        return proc, None
    monkeypatch.setattr(client_mod, "_spawn_daemon", spawn)
    client = DaemonClient()
    assert client.manages_service

    async def scenario():
        monitor = MachineMonitor(client)
        await monitor.connect()
        for _ in range(5):
            await asyncio.sleep(0.2)
            await monitor.refresh()
        first = monitor.state
        await monitor.connect()                                      # Try again
        return first, monitor.state, await client.ping(), await client.shutdown()
    first, again, ping, stopped = asyncio.run(scenario())
    assert first.link is Link.FAILED and first.problem == APP_OUTDATED
    assert again.link is Link.FAILED and again.problem == APP_OUTDATED
    assert len(spawned) == 1 and ping["pid"] == spawned[0].pid and ping["build"] != BUILD
    assert stopped["ok"] and spawned[0].wait(10) == 0            # it ran until the test ended it


def test_signal_constants_used_are_sigterm_only():
    """The service is only ever asked to end: SIGTERM (it stops a running step itself), never
    SIGKILL. The one kill in the client is of the flatpak-spawn helper this process started
    itself (``Popen.kill``, in ``_kill_spawn_helper``), which is not the service."""
    source = open(client_mod.__file__).read()
    tree = ast.parse(source)
    assert "signal.SIGKILL" not in source and source.count("signal.SIGTERM") == 2
    sends = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr in ("kill", "pidfd_send_signal", "killpg", "send_signal")]
    assert sorted(ast.unparse(n) for n in sends) == sorted([
        "os.kill(self.pid, signal.SIGTERM)", "signal.pidfd_send_signal(self.fd, signal.SIGTERM)",
        "proc.kill()"])
    [helper] = [f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                and f.name == "_kill_spawn_helper"]
    assert any(n is sends_kill for n in ast.walk(helper)
               for sends_kill in sends if ast.unparse(sends_kill) == "proc.kill()")
    assert ast.unparse(helper.args.args[0].annotation) == "subprocess.Popen"
    assert signal.SIGTERM
