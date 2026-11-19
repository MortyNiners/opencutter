# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Stand-in background services in separate processes, for the tests of stopping and replacing
a service (this module holds no tests).

Every process here is started by the test itself, in a private temporary $XDG_RUNTIME_DIR,
and is stopped (and if need be killed) by the test. Nothing reaches Bluetooth or D-Bus:

- ``start_older`` runs a small script named ``opencutter`` (as the installed command is)
  with the argument ``serve``: it answers every ping with a fixed reply (an older build's reply
  has no version), records the ops it gets, holds an optional flock (the old lock of builds
  before 0.1.0) and ends on SIGTERM, removing its socket. It has no shutdown op.
- ``spawn_stub_daemon`` runs the real daemon (``serve``) with the stub bleak/dbus_fast of
  tests/stubs.py, the stub BlueZ (one paired machine) and the simulated machine, whose FG
  answers ``fg_reply`` (the tested CAMEO 4's by default; stubs.standard_fg builds another
  model's) and whose other FakeClient settings (``tools``, ``u_reply``, ``u_by_tg``) a test may
  set, and writes the machine's write log on exit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(TESTS), "src")
TESTED_FG = "CAMEO 4 V5.05    "     # stubs.TESTED_FG (stubs installs itself only in the child)

OLDER_SCRIPT = r'''
import asyncio, fcntl, json, os, signal, sys
args = sys.argv[sys.argv.index("serve") + 1:]
sock_path, reply, record = args[0], args[1], args[2]
lock = args[3] if len(args) > 3 else ""


def note(text):
    with open(record, "a") as f:
        f.write(text + "\n")


async def main():
    if lock:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def handle(r, w):
        line = await r.readline()
        if line:
            op = json.loads(line).get("op")
            note(str(op))
            answer = json.loads(reply) if op == "ping" else {"ok": False, "error": f"unknown op {op!r}"}
            w.write((json.dumps(answer) + "\n").encode())
            await w.drain()
        w.close()
    server = await asyncio.start_unix_server(handle, path=sock_path)
    done = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, done.set)
    print("ready", flush=True)
    await done.wait()
    server.close()                     # (Python >= 3.13 removes the socket file itself)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    note("SIGTERM")

asyncio.run(main())
'''

DAEMON_CHILD = r'''
import asyncio, contextlib, json, os, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import stubs
stubs.install()
from opencutter.device import ble, daemon, programs
writes_file, idle, fg = sys.argv[3], float(sys.argv[4]), json.loads(sys.argv[5])
settings = json.loads(sys.argv[6]) if len(sys.argv) > 6 else {}
if "u_by_tg" in settings:                     # JSON object keys are text: TG values are ints
    settings["u_by_tg"] = {int(tg): u for tg, u in settings["u_by_tg"].items()}
assert daemon.SOCKET_PATH.startswith(os.environ["XDG_RUNTIME_DIR"] + os.sep)
Fake = type("Fake", (stubs.FakeClient,), {"accept": programs.PINNED_WRITES, "fg_reply": fg,
                                          **settings})
ble.BleakClient = Fake
ble.MessageBus = type("BlueZ", (stubs.FakeBlueZ,), {"devices": [stubs.bluez_device()]})


async def main():
    try:
        await daemon.serve(None, False, idle_exit=idle)
    finally:
        f = Fake.instance
        with open(writes_file, "w") as out:
            json.dump({"writes": [w.hex() for w in (f.writes if f else [])],
                       "connected": bool(f and f.is_connected)}, out)

with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
    asyncio.run(main())
'''


def private_env(runtime: str) -> dict:
    """The environment for a child: the private runtime dir, no Flatpak id, no socket override,
    no D-Bus."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("FLATPAK_ID", "OPENCUTTER_SOCKET", "DBUS_SESSION_BUS_ADDRESS")}
    env.update(XDG_RUNTIME_DIR=runtime, XDG_STATE_HOME=runtime,
               DBUS_SYSTEM_BUS_ADDRESS="unix:path=/nonexistent", PYTHONDONTWRITEBYTECODE="1")
    return env


def start_older(workdir: str, socket_path: str, reply: dict, *, name: str = "opencutter",
                lock: str | None = None, glued_c: bool = False) -> tuple[subprocess.Popen, str]:
    """Start a stand-in older service on `socket_path` (see the module doc). Returns the process
    and the file that records the ops it got (and "SIGTERM" when it ended on one). With
    `glued_c`, the same code runs as `python -c<code> opencutter serve ...` instead: a command
    string, not the installed command."""
    record = os.path.join(workdir, f"{name}-{os.path.basename(socket_path)}.ops")
    if glued_c:
        args = [sys.executable, "-c" + OLDER_SCRIPT, "opencutter", "serve"]
    else:
        script = os.path.join(workdir, name)
        with open(script, "w") as f:
            f.write(OLDER_SCRIPT)
        args = [sys.executable, script, "serve"]
    args += [socket_path, json.dumps(reply), record]
    if lock:
        args.append(lock)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=private_env(os.path.dirname(socket_path)), cwd=workdir)
    line = proc.stdout.readline()
    assert line.strip() == "ready", line
    return proc, record


def spawn_stub_daemon(runtime: str, writes_file: str, idle_exit: float = 900,
                      log: str | None = None, src: str = SRC,
                      fg_reply: str | None = TESTED_FG,
                      fake: dict | None = None) -> subprocess.Popen:
    """Start the real daemon on the simulated machine in `runtime` (its own $XDG_RUNTIME_DIR),
    from the package in `src` (e.g. an edited copy: another build). The machine answers FG
    with `fg_reply` (None: no answer); `fake` holds more FakeClient class settings (JSON
    values: tools, u_reply, u_by_tg, ...)."""
    out = open(log, "ab") if log else subprocess.DEVNULL
    try:
        return subprocess.Popen([sys.executable, "-c", DAEMON_CHILD, TESTS, src, writes_file,
                                 str(idle_exit), json.dumps(fg_reply), json.dumps(fake or {})],
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                env=private_env(runtime))
    finally:
        if log:
            out.close()


def ops(record: str) -> list[str]:
    """The ops a stand-in older service recorded."""
    try:
        with open(record) as f:
            return f.read().split()
    except FileNotFoundError:
        return []


def finish(*procs: subprocess.Popen) -> None:
    """Make sure the test's own child processes have ended (kill them if they have not)."""
    for proc in procs:
        if proc is not None and proc.poll() is None:
            proc.kill()
        if proc is not None:
            proc.wait(10)


def wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)
