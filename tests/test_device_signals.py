# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Real signals sent to a daemon subprocess that drives the simulated machine mid-step.

The child process installs the stubs before importing the daemon, uses the fake machine and a
private socket and lock, so it cannot reach Bluetooth. It writes its write log on exit.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from stubs import ESC09, stops

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(TESTS), "src")
APP_ID = "io.github.mortyniners.OpenCutter"
CHILD = r"""
import asyncio, contextlib, json, os, sys
sys.path[:0] = [sys.argv[2], sys.argv[3]]
import stubs
stubs.install()
from opencutter.device import ble, daemon, programs
d = sys.argv[1]
assert daemon.SOCKET_PATH.startswith(d + os.sep)    # XDG_RUNTIME_DIR is the private d
Fake = type("Fake", (stubs.FakeClient,), {"accept": programs.PINNED_WRITES, "move_s": 5.0})
ble.BleakClient = Fake
ble.MessageBus = type("BlueZ", (stubs.FakeBlueZ,), {"devices": [stubs.bluez_device()]})

async def main():
    try:
        await daemon.serve(None, False)
    finally:
        with open(os.path.join(d, "writes.json"), "w") as f:
            json.dump([w.hex() for w in Fake.instance.writes], f)

with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
    asyncio.run(main())
"""


def _start_step(path: str) -> socket.socket:
    """Open a dry-run session and start its first step; the socket stays open."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect(path)
    sock.sendall(b'{"op":"motion","program":"dry-run","options":{}}\n')
    assert json.loads(sock.makefile("rb").readline())["ok"]
    sock.sendall(b'{"op":"step","index":0}\n')
    time.sleep(1.0)
    return sock


@pytest.mark.parametrize("signals", [[signal.SIGHUP], [signal.SIGTERM], [signal.SIGINT, signal.SIGINT],
                                     [signal.SIGTERM, signal.SIGTERM]],
                         ids=["HUP", "TERM", "INTx2", "TERMx2"])
def test_signal_mid_step_sends_exactly_one_stop(signals):
    d = tempfile.mkdtemp(prefix="ocs-", dir="/tmp" if len(tempfile.gettempdir()) > 60 else None)
    env = {k: v for k, v in os.environ.items() if k not in ("FLATPAK_ID", "OPENCUTTER_SOCKET")}
    proc = subprocess.Popen([sys.executable, "-c", CHILD, d, TESTS, SRC],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**env, "XDG_RUNTIME_DIR": d})
    path = os.path.join(d, "app", APP_ID, "opencutter.sock")
    try:
        for _ in range(100):                     # wait until the daemon reports the machine
            if os.path.exists(path):
                break
            time.sleep(0.05)
        time.sleep(0.8)
        sock = _start_step(path)
        for sig in signals:
            proc.send_signal(sig)
            time.sleep(0.05)
        log, _ = proc.communicate(timeout=20)
        sock.close()
        with open(os.path.join(d, "writes.json")) as f:
            writes = [bytes.fromhex(x) for x in json.load(f)]
        assert b"TG1\x03FN0\x03TB50,0\x03" in writes, log
        assert stops(writes) == 1 and writes[-2:] == [ESC09, ESC09], (writes, log)
        assert "daemon stopping during a step" in log and "client gone" not in log, log
        assert "Bluetooth link released" in log, log      # after the stop went through
        assert not os.path.exists(path)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        shutil.rmtree(d, ignore_errors=True)
