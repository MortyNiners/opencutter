# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Asyncio client for the OpenCutter daemon, used by the GUI and the CLI.

It never touches Bluetooth and never imports bleak: everything goes through the daemon's Unix
socket as JSON lines. A Session is one connection; closing it while a step runs makes the
daemon stop the machine (ESC 09 twice).

It also manages the service itself: it starts one (`ensure_daemon`), asks one to end
(`shutdown_service`, the shutdown op) and ends an older one that has no shutdown op
(`terminate_older_daemon`, a guarded SIGTERM). None of these sends anything to the machine.

Inside a Flatpak sandbox the service is started through the Flatpak portal, in a sandbox of its
own (`service_command`): it outlives the window there too, and the window's sandbox can end
with the window. Its pid is then not visible to the app (another PID namespace), so nothing
here relies on it: the app knows its own service by the run id it was started with.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys

from .. import APP_ID
from ..identity import API_VERSION, BUILD, RUN_ID, STARTED_BY_ENV
from .protocol import (LEGACY_SOCKET_NAME, SOCKET_NAME, flatpak_app_id, in_flatpak, runtime_path,
                       service_path)

PING_TIMEOUT = 3.0
REQUEST_TIMEOUT = 10.0
STOP_TIMEOUT = 30.0       # > the daemon's two stop writes of up to 2 s each, plus a joined one,
                          # plus its status read afterwards (check=True: up to 15 s while the
                          # machine reads Paused, each read waiting up to 4 s for the query lock)
COMMAND_TIMEOUT = 25.0    # the resume and pause ops: the query lock (3 s), ESC 05 (3 s), the
                          # write (2 s), its quiet window and up to 5 s of reads afterwards
OPEN_TIMEOUT = 60.0       # planning a large job, three queries and the MTU read
SPAWN_WAIT = 15.0         # > the daemon's startup look at a link held elsewhere (4 x 2 s)
STREAM_LIMIT = 64 << 20   # a JobSpec line can be large
DAEMON_LOG = "daemon.log"
LOG_TAIL_BYTES = 4096     # how much of the daemon log is read back after a failed start
LOG_LINE_CHARS = 300
_TIMESTAMP = re.compile(r"^\d\d:\d\d:\d\d ")
SOCKET_ENV = "OPENCUTTER_SOCKET"   # development: talk to a daemon on another socket
IDLE_EXIT_SECONDS = 900   # the service the app starts ends 15 min after the last request
EXIT_WAIT = 10.0          # how long a service that was asked to end may take to do so
LOG_ROTATE_BYTES = 1 << 20  # at a spawn, a larger daemon.log becomes daemon.log.1
ENDING_WAIT = 25.0        # a service that is stopping: a stop during a step (up to 15 s), then
                          # its link release (up to 5 s)
FLATPAK_SPAWN = "flatpak-spawn"   # in the runtime: starts a command through the Flatpak portal
HELPER_REAP_WAIT = 2.0    # how long the flatpak-spawn helper may take to go after its SIGKILL


class DaemonError(ConnectionError):
    """The daemon could not be reached, closed the connection, or did not answer in time."""


def service_socket_path() -> str | None:
    """The service's socket: opencutter.sock in protocol.runtime_dir()
    ($XDG_RUNTIME_DIR/app/<app id>), or None without $XDG_RUNTIME_DIR. The one socket a
    service started by the app listens on."""
    return service_path(SOCKET_NAME)


def app_socket_path() -> str | None:
    """$XDG_RUNTIME_DIR/app/<opencutter.APP_ID>/opencutter.sock: the service socket of an
    installation outside Flatpak, whatever runtime_dir() says (`opencutter stop` always tries
    it), or None without $XDG_RUNTIME_DIR."""
    base = runtime_path("app")
    return os.path.join(base, APP_ID, SOCKET_NAME) if base else None


def older_socket_path() -> str | None:
    """$XDG_RUNTIME_DIR/opencutter.sock: where builds before 0.1.0 listened. Such a service
    reports no version and has no shutdown op (see terminate_older_daemon)."""
    return runtime_path(SOCKET_NAME)


def legacy_socket_path() -> str | None:
    """$XDG_RUNTIME_DIR/cameo4.sock: the socket of the legacy prototype daemon."""
    return runtime_path(LEGACY_SOCKET_NAME)


def default_socket_path() -> str | None:
    """$OPENCUTTER_SOCKET when set (e.g. a daemon on the simulated machine, see
    tests/fake_daemon.py), else service_socket_path(); None when neither is set.
    (`opencutter stop` does not rely on this alone: it stops through every socket a daemon may
    listen on, see __main__.stop_targets.)"""
    return os.environ.get(SOCKET_ENV) or service_socket_path()


def state_dir() -> str:
    """$XDG_STATE_HOME/opencutter (default ~/.local/state/opencutter).

    Inside a Flatpak sandbox Flatpak sets $XDG_STATE_HOME to the app's ~/.var/app/<id>/.local/state
    (from Flatpak 1.14 on). Without it (an older Flatpak), that same folder is used: the
    sandbox's own ~/.local/state is not kept when the sandbox ends."""
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        home, app_id = os.path.expanduser("~"), flatpak_app_id()
        base = (os.path.join(home, ".var", "app", app_id, ".local", "state") if app_id
                else os.path.join(home, ".local", "state"))
    return os.path.join(base, "opencutter")


async def _connect(path: str | None) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if not path:
        raise DaemonError("XDG_RUNTIME_DIR is not set, so there is no daemon socket")
    try:
        return await asyncio.open_unix_connection(path, limit=STREAM_LIMIT)
    except OSError as e:
        raise DaemonError(f"no OpenCutter daemon answers at {path} ({type(e).__name__})") from None


async def _send(writer: asyncio.StreamWriter, obj: dict) -> None:
    try:
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()
    except OSError as e:
        raise DaemonError(f"the daemon connection broke: {type(e).__name__}: {e}") from None


async def _receive(reader: asyncio.StreamReader, timeout: float | None) -> dict:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout)
    except TimeoutError:
        raise DaemonError(f"no answer from the daemon within {timeout:.0f} s") from None
    except (OSError, ValueError) as e:
        raise DaemonError(f"the daemon connection broke: {type(e).__name__}: {e}") from None
    if not line:
        raise DaemonError("the daemon closed the connection")
    try:
        reply = json.loads(line)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise DaemonError(f"unexpected answer from the daemon: {line[:80]!r}")
    return reply


def daemon_log_path() -> str:
    """$XDG_STATE_HOME/opencutter/daemon.log: what a daemon started by the app writes."""
    return os.path.join(state_dir(), DAEMON_LOG)


def _open_log(path: str, flags: int) -> int:
    """``open()``'s opener for the daemon log: never blocks (a FIFO there would hang the GUI's
    main loop) and never follows a symlink; only a regular file is used."""
    fd = os.open(path, flags | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "the daemon log is not a regular file", path)
        os.set_blocking(fd, True)          # the daemon writes to it as its stdout
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_spawn_log():
    """daemon.log for a new service, opened with ``_open_log``'s rules. When it has grown past
    LOG_ROTATE_BYTES, what was just opened (a regular file, not a link) is renamed to
    daemon.log.1 first, replacing an older one, and a new daemon.log is started."""
    path = daemon_log_path()
    log = open(path, "ab", opener=_open_log)
    try:
        if os.fstat(log.fileno()).st_size <= LOG_ROTATE_BYTES:
            return log
        os.replace(path, path + ".1")
    except OSError as e:
        logging.getLogger(__name__).warning("could not rotate the daemon log: %s", e)
        return log
    log.close()
    return open(path, "ab", opener=_open_log)


def _kill_spawn_helper(proc: subprocess.Popen) -> bool:
    """SIGKILL the flatpak-spawn helper (inside a Flatpak sandbox); False when it had ended.

    flatpak-spawn waits for the service it started, and while it waits it keeps the window's
    sandbox alive, up to 15 minutes after the window was closed. It is not needed any more once
    the service answers, or once the window closes: the service runs in a sandbox of its own,
    and without --watch-bus the portal keeps it running when the helper goes away. SIGKILL,
    never SIGTERM, SIGINT or SIGHUP: flatpak-spawn passes those on to the service, which would
    then end. Only this process's own child is signalled (``Popen.kill`` does nothing once it
    was reaped)."""
    if proc.poll() is not None:
        return False
    with contextlib.suppress(OSError):
        proc.kill()
    return True


async def _end_spawn_helper(proc: subprocess.Popen, wait: float = HELPER_REAP_WAIT) -> None:
    """End the flatpak-spawn helper (``_kill_spawn_helper``) and reap it without blocking the
    event loop: once the service answers."""
    if not _kill_spawn_helper(proc):
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while proc.poll() is None and loop.time() < deadline:
        await asyncio.sleep(0.02)


def service_command() -> tuple[list[str], dict[str, str] | None]:
    """The command that starts the service, and its environment (None: this process's).

    Outside Flatpak: `<python> -m opencutter serve --idle-exit 900`, with this run's id in
    $OPENCUTTER_STARTED_BY (identity.RUN_ID), so that the app knows the service it started.

    Inside a Flatpak sandbox the service is started through the Flatpak portal (flatpak-spawn),
    which runs it in a new sandbox of the same app, with the same permissions: never on the
    host (no --host) and never tied to the window (no --watch-bus). A child of the window would
    keep the window's sandbox alive, and it would end with it whenever that sandbox is torn
    down. The portal runs the same commit of the app as the window (no --latest-version):
    after an update, the open window and the service it starts stay one build, because Flatpak
    keeps an updated version that is still in use until its last sandbox ends. Should that
    version be gone all the same, flatpak-spawn ends at once with Flatpak's "not installed"
    line in daemon.log (gui.machine: APP_OUTDATED). The run id goes along with --env;
    flatpak-spawn passes its stdin, stdout and stderr on, so the service writes to the same
    daemon.log. It starts in / so that nothing in a working directory can shadow the
    package."""
    serve = ["serve", "--idle-exit", str(IDLE_EXIT_SECONDS)]
    if in_flatpak():
        return [FLATPAK_SPAWN, f"--env={STARTED_BY_ENV}={RUN_ID}", "--directory=/",
                "opencutter", *serve], None
    return [sys.executable, "-m", "opencutter", *serve], {**os.environ, STARTED_BY_ENV: RUN_ID}


def _spawn_daemon() -> tuple[subprocess.Popen, int | None]:
    """Start the service detached (``service_command``), with its output in the state dir log.
    Returns the process (inside a Flatpak sandbox: flatpak-spawn, which waits for the service
    in its own sandbox and exits with its status, see ``_end_spawn_helper``) and the log's size
    before it started (its own lines come after), or None when the log could not be opened
    safely: the daemon then starts without a log."""
    try:
        os.makedirs(state_dir(), mode=0o700, exist_ok=True)
        log = _open_spawn_log()
    except OSError as e:
        logging.getLogger(__name__).warning("starting the daemon without a log: %s", e)
        log = None
    command, env = service_command()
    with log if log is not None else contextlib.nullcontext():
        offset = os.fstat(log.fileno()).st_size if log is not None else None
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                stdout=log if log is not None else subprocess.DEVNULL,
                                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                                env=env)
    return proc, offset


def last_log_line(path: str, offset: int = 0) -> str | None:
    """The last non-empty line written to ``path`` at or after byte ``offset`` (without its
    time stamp), or None. Reads at most the last LOG_TAIL_BYTES, and only from a regular file
    opened without blocking (never a FIFO or a device), so it cannot hang the caller."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        start = offset if 0 <= offset <= info.st_size else 0      # truncated since: all of it
        start = max(start, info.st_size - LOG_TAIL_BYTES)
        data = os.pread(fd, LOG_TAIL_BYTES, start)
    except OSError:
        return None
    finally:
        os.close(fd)
    lines = [line.strip() for line in data.decode("utf-8", "replace").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return None
    return _TIMESTAMP.sub("", lines[-1])[:LOG_LINE_CHARS]


# --- managing the service: who listens on a socket, ending it --------------------------------------
_UCRED = struct.Struct("3i")          # Linux struct ucred: pid, uid, gid
_PYTHON = re.compile(r"python(\d+(\.\d+)*)?")
_PYTHON_LONG_VALUE_OPTIONS = frozenset({"--check-hash-based-pycs"})


def peer_credentials(sock) -> tuple[int, int]:
    """(pid, uid) of the process at the other end of a connected Unix socket (SO_PEERCRED): the
    process that listens on the socket's path, as a pid of this process's PID namespace. The pid
    is 0 when that process cannot be seen from here: from inside a Flatpak sandbox, a service
    on the host or in another sandbox (the app's service runs in a sandbox of its own). From
    the host, a service in a sandbox has its host pid."""
    pid, uid, _gid = _UCRED.unpack(
        sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size))
    return pid, uid


def process_cmdline(pid: int) -> list[str] | None:
    """The command line of process `pid` from /proc, or None when it cannot be read. (A process
    that has ended but was not reaped yet has an empty one.)"""
    try:
        fd = os.open(f"/proc/{int(pid)}/cmdline", os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0")[:-1]] if raw else []


def _is_serve(target: str, rest: list[str]) -> bool:
    return target in ("opencutter", "opencutter.__main__") and rest[:1] == ["serve"]


def is_service_cmdline(argv: list[str] | None) -> bool:
    """True for a Python process running OpenCutter's `serve`: `python [options] -m opencutter
    serve ...`, or `python [options] <dir>/opencutter serve ...` (the installed command).

    Short options are read the way Python reads them, also in clusters (`-Es`, `-Wignore`,
    `-mopencutter`): a `c` in a cluster is a command string (`-c<code>`), so the arguments after
    it are only that code's, and the process is something else."""
    if not argv or not _PYTHON.fullmatch(os.path.basename(argv[0])):
        return False
    args, i = argv[1:], 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "-":                          # the program comes from stdin: something else
            return False
        if arg == "--":                         # the end of the options: the script follows
            script = args[i] if i < len(args) else ""
            return _is_serve("opencutter" if os.path.basename(script) == "opencutter" else "",
                             args[i + 1:])
        if arg.startswith("--"):
            if arg in _PYTHON_LONG_VALUE_OPTIONS:
                i += 1                          # its value is the next argument
            continue
        if arg.startswith("-"):                 # one short option or a cluster of them
            for j, letter in enumerate(arg[1:], 1):
                if letter == "c":               # a command string: something else
                    return False
                if letter in "mWX":             # the rest of the argument, or the next one
                    value = arg[j + 1:]
                    if not value:
                        value = args[i] if i < len(args) else ""
                        i += 1
                    if letter == "m":
                        return _is_serve(value, args[i:])
                    break
            continue
        return _is_serve("opencutter" if os.path.basename(arg) == "opencutter" else "", args[i:])
    return False


class _Process:
    """One process, from its pid: a pidfd where the kernel has them, so that a signal can only
    reach that very process even if the pid were reused later."""

    def __init__(self, pid: int):
        self.pid = pid
        self.fd: int | None = None
        with contextlib.suppress(OSError, AttributeError):
            self.fd = os.pidfd_open(pid)

    def ended(self) -> bool:
        """True once the process has ended (a zombie counts as ended)."""
        if self.fd is not None:
            return bool(select.select([self.fd], [], [], 0)[0])
        try:
            with open(f"/proc/{self.pid}/stat", "rb") as f:
                state = f.read().rsplit(b")", 1)[1].split()[0]
        except (OSError, IndexError):
            return True
        return state in (b"Z", b"X")

    def terminate(self) -> None:
        """Send SIGTERM."""
        if self.fd is not None:
            signal.pidfd_send_signal(self.fd, signal.SIGTERM)
        else:
            os.kill(self.pid, signal.SIGTERM)

    async def wait(self, timeout: float) -> bool:
        """Wait (without blocking the event loop) until the process has ended; False on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.ended():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def close(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None


async def _wait_ended(process: _Process | None, path: str, timeout: float) -> bool:
    """Wait until a service that was asked to end has ended: its process when we can see it,
    else (another PID namespace, or this very process in the tests) until its socket file is
    gone, which a daemon of this build removes after it released the link."""
    if process is not None:
        return await process.wait(timeout)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while os.path.lexists(path):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def shutdown_service(path: str | None, wait: float = EXIT_WAIT) -> dict:
    """Ask the service on `path` to end (the shutdown op): it releases the machine at once and
    sends nothing to it. The service refuses while a program or job runs. Waits up to `wait`
    seconds until it has ended. Never raises.

    Returns {"ok": True, "pid"} once it ended, else {"ok": False, "error", "pid", "busy"}, where
    "busy" says the refusal was because a program or job runs."""
    if not path:
        return {"ok": False, "error": "XDG_RUNTIME_DIR is not set", "pid": None, "busy": False}
    try:
        reader, writer = await _connect(path)
    except DaemonError as e:
        return {"ok": False, "error": str(e), "pid": None, "busy": False}
    process = None
    try:
        try:
            pid, _uid = peer_credentials(writer.get_extra_info("socket"))
            if 0 < pid != os.getpid():
                process = _Process(pid)
            await _send(writer, {"op": "shutdown"})
            reply = await _receive(reader, REQUEST_TIMEOUT)
        except (DaemonError, OSError) as e:
            return {"ok": False, "error": str(e), "pid": None, "busy": False}
        finally:
            writer.close()
        if not reply.get("ok"):
            return {"ok": False, "error": str(reply.get("error") or "refused"), "pid": pid or None,
                    "busy": True}
        if not await _wait_ended(process, path, wait):
            return {"ok": False, "error": f"it did not end within {wait:.0f} s",
                    "pid": pid or None, "busy": False}
        return {"ok": True, "pid": pid or None}
    finally:
        if process is not None:
            process.close()


async def terminate_older_daemon(path: str | None, wait: float = EXIT_WAIT) -> dict:
    """End an OpenCutter service that has no shutdown op (a build before 0.1.0) with SIGTERM.

    The signal goes out only when every one of these holds: the pid comes from SO_PEERCRED on a
    connection to `path` itself; that process belongs to this user; /proc/<pid>/cmdline shows a
    Python process running OpenCutter's `serve`; and a ping on that same connection, right
    before the signal, says no program or job runs. Such a service handles SIGTERM like Ctrl+C:
    with no job running it sends nothing to the machine. Waits up to `wait` seconds until the
    process has ended. Never raises.

    Returns {"ok": True, "pid"} once it ended, else {"ok": False, "error", "pid", "busy"}."""
    def refused(why: str, pid: int | None = None, busy: bool = False) -> dict:
        return {"ok": False, "error": why, "pid": pid, "busy": busy}

    if not path:
        return refused("XDG_RUNTIME_DIR is not set")
    try:
        reader, writer = await _connect(path)
    except DaemonError as e:
        return refused(str(e))
    process = None
    try:
        try:
            pid, uid = peer_credentials(writer.get_extra_info("socket"))
            if uid != os.getuid():
                return refused(f"the process behind {path} belongs to another user (uid {uid})")
            if pid <= 0:
                return refused(f"the process behind {path} cannot be seen from here (it runs "
                               "in another sandbox, or outside this one)")
            if pid == os.getpid():
                return refused("that is this very process", pid)
            process = _Process(pid)
            argv = process_cmdline(pid)
            if not is_service_cmdline(argv):
                shown = " ".join(argv or [])[:120] or "unknown"
                return refused(f"process {pid} is not an OpenCutter background service "
                               f"(it runs: {shown})", pid)
            await _send(writer, {"op": "ping"})         # the same connection: the same process
            ping = await _receive(reader, PING_TIMEOUT)
            if ping.get("motion"):
                return refused(f"it is running {str(ping['motion'])[:40]}", pid, busy=True)
            if not ping.get("ok"):
                return refused(f"it did not answer the ping: {str(ping)[:120]}", pid)
            process.terminate()
        except (DaemonError, OSError) as e:
            return refused(f"{type(e).__name__}: {e}")
        finally:
            writer.close()
        if not await process.wait(wait):
            return refused(f"process {pid} did not end within {wait:.0f} s", pid)
        return {"ok": True, "pid": pid}
    finally:
        if process is not None:
            process.close()


async def ping_with_peer(path: str | None) -> tuple[dict | None, int | None]:
    """A ping of the service on `path` and the pid of the process that answered it (from
    SO_PEERCRED, not from the reply), or (None, None) when no service answers."""
    if not path:
        return None, None
    try:
        reader, writer = await _connect(path)
    except DaemonError:
        return None, None
    try:
        pid, _uid = peer_credentials(writer.get_extra_info("socket"))
        await _send(writer, {"op": "ping"})
        return await _receive(reader, PING_TIMEOUT), (pid or None)
    except (DaemonError, OSError):
        return None, None
    finally:
        writer.close()


class DaemonClient:
    """One-shot requests to the daemon, and sessions (programs or jobs)."""

    def __init__(self, socket_path: str | None = None):
        """Talk to the daemon at `socket_path`, by default `default_socket_path()`."""
        self.socket_path = socket_path or default_socket_path()
        # why the last daemon this client started did not come up: the last line it wrote to
        # daemon.log before it exited (e.g. "the legacy prototype daemon holds cameo4.lock;
        # stop it first ..."), or None
        self.spawn_error: str | None = None
        # the pid of the last service this client started (the app compares it with a ping);
        # None inside a Flatpak sandbox, where the service runs in a sandbox of its own and
        # the process this client started is flatpak-spawn
        self.spawned_pid: int | None = None
        # inside a Flatpak sandbox: the flatpak-spawn helper of the last start, until a service
        # answers (then it is ended, see _end_spawn_helper)
        self._helper: subprocess.Popen | None = None

    async def request(self, req: dict, timeout: float = REQUEST_TIMEOUT) -> dict:
        """Send one one-shot request (ping, esc, query, info, stop, resume, pause) and return
        the answer.
        Raises DaemonError when the daemon cannot be reached or does not answer."""
        reader, writer = await _connect(self.socket_path)
        try:
            await _send(writer, req)
            return await _receive(reader, timeout)
        finally:
            writer.close()

    async def ping(self) -> dict | None:
        """{"ok","connected","status","motion","device","reason","model","stopping","version",
        "build","api","pid","started_by"}, or None when no daemon answers. An older daemon's
        reply lacks "model" and the last six; "model" is what the machine reports itself as
        (FG; with the reason "wrong-model", the machine the service refused); "stopping" is
        true once the service is ending (it then answers until it has released the link). Once
        a service answers, a flatpak-spawn helper this client still waits for is ended (a
        service that came up after ensure_daemon gave up).

        The machine profile fields are not in this plain ping: a ping sent as
        {"op": "ping", "machine": true} (ping_machine) adds "profile", "support" and
        "refused_reason"."""
        try:
            reply = await self.request({"op": "ping"}, PING_TIMEOUT)
        except DaemonError:
            return None
        if self._helper is not None:
            await self._end_helper()
        return reply

    async def ping_machine(self, stop: bool = False) -> dict | None:
        """The ping with the machine profile fields, {"op": "ping", "machine": true}: every key
        of ping() plus "profile" (the connected machine's profile id, or a refused model's that
        OpenCutter recognizes), "support" ("tested" or "untested" while connected, "recognized"
        for that refused model) and "refused_reason" (why it is refused, in plain words); each
        None otherwise. A ping with "connected" false never names a usable profile. None when
        no daemon answers. With `stop`, also the stop fields of ping_stop().

        Ask it only of a service of this very build (identity.same_build of a plain ping
        first): a service from before machine profiles answers without the fields, and an older
        one must never get requests it does not know."""
        req = {"op": "ping", "machine": True, **({"stop": True} if stop else {})}
        try:
            reply = await self.request(req, PING_TIMEOUT)
        except DaemonError:
            return None
        return reply if isinstance(reply, dict) else None

    async def ping_stop(self) -> dict | None:
        """The ping with the stop fields, {"op": "ping", "stop": true} (also while no machine is
        connected): every key of ping() plus
          "stop_owed"        a stop could not get through (the link was lost while the machine
                             may have been moving, or the stop's writes failed); the service
                             sends it once that machine is linked again and passed the model
                             check. True until a stop gets through.
          "owed_stops_sent"  how many owed stops the service sent on a new link (tell the user
                             when it goes up)
          "motion_stopped"   the open session got a stop request (from this app or another
                             client, such as `opencutter stop`): its next step is refused
          "interrupted"      why the open session ends with the stop although nobody asked:
                             the Cancelling status digit it saw after its first write (a
                             cancel on the machine's panel), or "left-pause" (the machine left
                             Pause without going on, maybe a cancel whose Cancelling was
                             missed); None otherwise
          "load_seen"        how often the machine's status changed to the load digit (Load
                             Media) since the service started: an unload shows as a change of
                             this count, even between two pings
        and, from API 2 (OpenCutter 0.1.1) on:
          "paused"           the open session waits on a paused machine (its Pause key, or a
                             pause op): nothing more is sent until it goes on; the step in
                             progress, or the next one, waits with no time limit
          "resume_ok"        a resume op would be sent now (paused, no stop asked for, and a
                             machine whose Resume was tested: the CAMEO 4 on firmware V5)
          "pause_ok"         a pause op would be sent now (a session of ours wrote, it is not
                             paused or ending, the machine last read Ready or Busy, and its
                             Pause was tested)
          "no_reply_s"       seconds the query the open session waits for has been unanswered,
                             once that is 7 s or more ("Checking connection"; the service
                             keeps waiting and sends nothing new); None otherwise
          "indication_age_s" seconds since the machine's last status indication, or None: when
                             replies are late but indications still come, the machine still
                             reports its status ("status" is the last one)
          "stop_check"       the ESC 05 read-back after the last stop op with check, or None:
                             {"running", "status", "seconds", "still_busy", "still_paused",
                             "reason"} (see DaemonClient.stop)
        None when no daemon answers. Same build only, as for ping_machine(); a service without
        these fields (an older build) answers without them: a missing key means "not known",
        never "false"."""
        try:
            reply = await self.request({"op": "ping", "stop": True}, PING_TIMEOUT)
        except DaemonError:
            return None
        return reply if isinstance(reply, dict) else None

    async def _end_helper(self) -> None:
        helper, self._helper = self._helper, None
        if helper is not None:
            await _end_spawn_helper(helper)

    def end_helper(self) -> None:
        """End the flatpak-spawn helper of the last start at once, if it still waits for a
        service that has not answered yet (inside a Flatpak sandbox; otherwise there is none).
        The window calls this when it closes and the app when it quits, so that the window's
        sandbox ends with the app: the service itself starts and runs on as usual. Blocks at
        most HELPER_REAP_WAIT while the killed helper is reaped (it goes at once)."""
        helper, self._helper = self._helper, None
        if helper is not None and _kill_spawn_helper(helper):
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                helper.wait(HELPER_REAP_WAIT)

    async def info(self) -> dict:
        """{"ok","status","tools","firmware"}: ESC 05, ESC 15 and the machine's FG reply. From
        API 2 on the service never sends FG for it (the reply comes from when the machine was
        connected), and "tools" is None while the machine reads Paused or Cancelling (no ESC 15
        is sent then: keep the tools read last). Raises DaemonError."""
        return await self.request({"op": "info"})

    async def stop(self, check: bool = False) -> dict:
        """Ask the daemon to send ESC 09 twice (with or without a session: after a run too).
        Never raises: {"ok": False, "error": ...} on failure, in which case the machine's power
        button is the only stop left. {"ok", "errors"} otherwise.

        With `check`, a stop that got through is followed by ESC 05 reads (for up to about 3 s
        while the machine reads Busy or Cancelling, up to 15 s once it read Paused: a paused
        machine ends the job about 5 s after the stop), and the reply adds "status_after" (the
        last status digit read, or None) and "still_busy" (True when it still reads Busy: press
        the power button; None when nothing was read). A "status_after" of the Paused digit
        means that it still reported Paused at the end (protocol.STOP_CHECK_PAUSED s): press
        Unload on the machine. A Ready reply does not prove that the machine stopped for good;
        it is what the machine reports. An older service answers without these keys. The ping's
        "stop_check" (ping_stop) shows the read-back too, also while it runs."""
        req = {"op": "stop", **({"check": True} if check else {})}
        try:
            return await self.request(req, STOP_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def resume(self) -> dict:
        """The resume op (API 2): the service sends the machine's Resume (one ESC 08) for the
        user's explicit click or command, only while the open session waits on a paused
        machine whose Resume was tested (the CAMEO 4 on firmware V5), after a fresh ESC 05 read
        Paused; never repeated. pause(): the same with the Pause (one ESC 07), only while a
        session of ours has written and the machine reads Ready or Busy. The session then goes
        on, or waits, as for the machine's own Pause key. Never raises.

        {"ok", "sent", "status_before", "status_after", "paused", "errors"} and, unless "ok",
        "error" (why, in plain words). "sent" is False for a refusal (nothing was written),
        True when the write went out or may have (a write error: whether it arrived is
        unknown), None when the service did not answer. "status_after" is the last status the
        service read or was told within about 5 s; "paused" says whether the session waits on
        a paused machine now. An older service answers {"ok": False, "error": "unknown op"}."""
        return await self._command("resume")

    async def pause(self) -> dict:
        """The pause op (API 2): see resume()."""
        return await self._command("pause")

    async def _command(self, op: str) -> dict:
        try:
            reply = await self.request({"op": op}, COMMAND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return {"ok": False, "sent": None, "error": f"{type(e).__name__}: {e}"}
        return reply

    @property
    def manages_service(self) -> bool:
        """True when this client talks to the service socket (not to $OPENCUTTER_SOCKET or
        another path): only then does it start a service, replace one or look for an older one."""
        return bool(self.socket_path) and self.socket_path == service_socket_path()

    async def ping_older(self) -> dict | None:
        """The ping of an older service (a build before 0.1.0) on older_socket_path(), or None.
        Always None unless this client manages the service."""
        path = older_socket_path()
        if not self.manages_service or not path or path == self.socket_path:
            return None
        return await DaemonClient(path).ping()

    async def shutdown(self, wait: float = EXIT_WAIT) -> dict:
        """End the service on this client's socket: ``shutdown_service``."""
        return await shutdown_service(self.socket_path, wait)

    async def terminate_older(self, path: str | None = None, wait: float = EXIT_WAIT) -> dict:
        """End an older service without the shutdown op, on `path` (default:
        older_socket_path()): ``terminate_older_daemon``."""
        return await terminate_older_daemon(path or older_socket_path(), wait)

    async def ensure_daemon(self, timeout: float = SPAWN_WAIT) -> bool:
        """True if a daemon answers, starting `python -m opencutter serve --idle-exit 900`
        detached first if none does (inside a Flatpak sandbox through the portal, see
        ``service_command``). Never spawns for a socket other than the service socket (a
        daemon only listens there). A service that answers but is stopping (Disconnect,
        `opencutter service stop`, its idle time) is waited for (at most ENDING_WAIT) and then
        replaced by a new one. When the started daemon exits without answering, `spawn_error`
        says why. Inside a sandbox the process started is flatpak-spawn: it exits with the
        service's status (or at once, with its own error in daemon.log, when the portal or
        Flatpak refused), and once the service answers it is ended (``_end_spawn_helper``).
        Should it end with status 0 before the service answers, the service may still come
        up, so the wait goes on until `timeout`. When the wait is cancelled (the window closes
        while the service starts), the helper is ended too; when ``end_helper`` ended it from
        outside meanwhile, the wait ends (False, without ``spawn_error``)."""
        self.spawn_error = None
        ping = await self.ping()
        if ping is not None and ping.get("stopping") and self.manages_service:
            if not await self._until_gone(ENDING_WAIT):
                self.spawn_error = (f"the OpenCutter daemon that was stopping did not end within "
                                    f"{ENDING_WAIT:.0f} s")
                return False
            ping = None
        if ping is not None:
            return True
        if not self.manages_service:
            return False
        sandboxed = in_flatpak()
        await self._end_helper()                  # one from an earlier start that never answered
        proc, offset = _spawn_daemon()
        self.spawned_pid = None if sandboxed else proc.pid
        self._helper = proc if sandboxed else None       # ended by the first ping that answers
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while loop.time() < deadline:
                await asyncio.sleep(0.2)
                if await self.ping() is not None:
                    return True
                if sandboxed and self._helper is not proc:
                    return False              # end_helper: the window closed or the app quits
                status = proc.poll()
                if status is not None and not (sandboxed and status == 0):
                    # it exited (e.g. another daemon holds the lock, or the portal refused)
                    if await self.ping() is not None:
                        return True
                    line = (None if offset is None else
                            await asyncio.to_thread(last_log_line, daemon_log_path(), offset))
                    self.spawn_error = line or f"the daemon exited with status {status}"
                    return False
        except asyncio.CancelledError:
            if self._helper is proc:
                self.end_helper()             # the window closed while the service started
            raise
        return False

    def started_here(self, ping: dict | None) -> bool:
        """True when ``ping`` comes from a service this process started: it reports this run's
        id (identity.RUN_ID) as "started_by". A service that reports another run's id is not
        ours, whatever its pid: a service in another sandbox reports a pid of its own PID
        namespace, which may well equal one of ours. Without a run id (a service too old to
        report one, or started without it) its pid tells, and only outside a sandbox, where
        ``spawned_pid`` is set."""
        if not isinstance(ping, dict):
            return False
        started_by = ping.get("started_by")
        if isinstance(started_by, str):
            return started_by == RUN_ID
        pid = ping.get("pid")
        return self.spawned_pid is not None and type(pid) is int and pid == self.spawned_pid

    async def _until_gone(self, timeout: float) -> bool:
        """Wait (without blocking the event loop) until no service answers on this client's
        socket any more; False when one still does after `timeout` seconds. A service of this
        build stops answering only once it has released the link, right before it removes its
        socket and releases its locks."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while await self.ping() is not None:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.1)
        return True

    def session(self) -> Session:
        """A new session (async context manager) on its own connection."""
        return Session(self.socket_path)


class Session:
    """One program or job on one daemon connection.

    Use as `async with client.session() as s:`, then open_job() or open_program(), step(i) for
    i = 0, 1, ... and end(). Leaving the block closes the connection; if that happens while a
    step runs, the daemon stops the machine. Methods raise DaemonError when the connection
    fails; refusals by the daemon come back as {"ok": False, "error": ...}."""

    def __init__(self, socket_path: str | None):
        self._path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> Session:
        self._reader, self._writer = await _connect(self._path)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._reader = self._writer = None

    async def _call(self, req: dict, timeout: float | None) -> dict:
        if self._writer is None or self._reader is None:
            raise DaemonError("the session is not open")
        await _send(self._writer, req)
        return await _receive(self._reader, timeout)

    async def open_job(self, spec: dict) -> dict:
        """Open a job from JobSpec.to_dict(); returns the open reply (steps, summary, ...).
        The request carries this build: a service of another build refuses it."""
        return await self._call({"op": "job", "spec": spec, "build": BUILD, "api": API_VERSION},
                                OPEN_TIMEOUT)

    async def open_program(self, name: str, options: dict) -> dict:
        """Open a pinned program ("dry-run", "pen-test" or "cut-test"), with this build."""
        return await self._call({"op": "motion", "program": name, "options": options,
                                 "build": BUILD, "api": API_VERSION}, OPEN_TIMEOUT)

    async def step(self, index: int, timeout: float | None = 400) -> dict:
        """Run step `index` (in order) and wait for its result; None waits without limit.
        A long cut step can take longer than the default: pass a timeout from the plan. From
        API 2 on a step waits with no time limit while the machine is paused (until it goes
        on, or the user stops the job): pass None, and watch the service with ping_stop()
        instead ("paused", "no_reply_s"); a client that gives up on a step must send the stop
        (closing this connection while a step runs does that too)."""
        return await self._call({"op": "step", "index": index}, timeout)

    async def end(self) -> None:
        """End the session politely. Best effort: never raises."""
        with contextlib.suppress(DaemonError):
            await self._call({"op": "end"}, 5)
