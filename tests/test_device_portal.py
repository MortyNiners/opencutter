# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""What the background service tells the desktop inside a Flatpak sandbox (device.portal).

GNOME lists the windowless service under Background Apps; its close button there would end the
service with SIGKILL, so the service sets the line shown under the app's name (the Background
portal's SetStatus). The session bus is a fake here: nothing connects to any bus, and dbus-fast
is the stub of tests/stubs.py, whose Message and Variant are replaced by recorders."""
import asyncio
import sys
import types

import pytest

import stubs

stubs.install()
from opencutter.device import daemon, portal  # noqa: E402
from stubs import harness  # noqa: E402,F401


class Recorded:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class FakeBus:
    """A connected session bus that answers SetStatus with ``reply``."""

    def __init__(self, reply=None, hang: bool = False):
        self.reply = reply or types.SimpleNamespace(error_name=None, body=[])
        self.calls: list = []
        self.disconnected = False
        self.hang = hang
        self._gone = asyncio.Event()

    async def call(self, message):
        self.calls.append(message)
        if self.hang:
            await asyncio.sleep(3600)
        return self.reply

    async def wait_for_disconnect(self):
        await self._gone.wait()

    def disconnect(self):
        self.disconnected = True
        self._gone.set()


@pytest.fixture
def recorders(monkeypatch):
    """dbus_fast.Message and dbus_fast.Variant record how they were made."""
    monkeypatch.setattr(sys.modules["dbus_fast"], "Message", Recorded, raising=False)
    monkeypatch.setattr(sys.modules["dbus_fast"], "Variant", Recorded, raising=False)


def test_the_status_line_is_short_plain_and_says_how_to_release_the_machine():
    line = portal.BACKGROUND_STATUS
    assert 0 < len(line) <= portal.MAX_STATUS == 96
    assert "CAMEO 4" in line and "Disconnect" in line and "do not close it here" in line
    assert line.isascii()


def test_the_status_is_set_on_the_background_portal_and_kept_until_the_service_ends(recorders):
    bus = FakeBus()
    lines = []

    async def scenario():
        async def connect():
            return bus
        task = asyncio.ensure_future(portal.announce_background(lines.append, connect=connect))
        for _ in range(100):
            if lines:
                break
            await asyncio.sleep(0.01)
        assert not task.done() and not bus.disconnected       # kept while the service runs
        task.cancel()                                          # the service ends
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    [message] = bus.calls
    assert message.kwargs == {
        "destination": "org.freedesktop.portal.Desktop", "path": "/org/freedesktop/portal/desktop",
        "interface": "org.freedesktop.portal.Background", "member": "SetStatus",
        "signature": "a{sv}", "body": [{"message": message.kwargs["body"][0]["message"]}]}
    variant = message.kwargs["body"][0]["message"]
    assert variant.args == ("s", portal.BACKGROUND_STATUS)
    assert lines == ["told the desktop what the background service does (Background Apps)"]
    assert bus.disconnected


def test_a_portal_without_set_status_is_one_line_in_the_log(recorders):
    error = types.SimpleNamespace(error_name="org.freedesktop.DBus.Error.UnknownMethod",
                                  body=["No such method “SetStatus”"])
    bus = FakeBus(reply=error)
    lines = []

    async def connect():
        return bus
    assert asyncio.run(portal.announce_background(lines.append, connect=connect)) is False
    assert lines == ["the desktop was not told what the background service does "
                     "(org.freedesktop.DBus.Error.UnknownMethod: No such method “SetStatus”)"]
    assert bus.disconnected


@pytest.mark.parametrize("failure", ["no-bus", "hangs"])
def test_no_session_bus_or_no_answer_never_stops_the_service(recorders, failure):
    lines = []
    bus = FakeBus(hang=True)

    async def connect():
        if failure == "no-bus":
            raise FileNotFoundError("no session bus")
        return bus
    result = asyncio.run(portal.announce_background(lines.append, connect=connect, timeout=0.2))
    assert result is False and len(lines) == 1
    assert lines[0].startswith("the desktop was not told what the background service does (")
    assert ("FileNotFoundError" if failure == "no-bus" else "TimeoutError") in lines[0]
    assert bus.disconnected is (failure == "hangs")


def test_the_session_bus_is_never_the_system_bus(monkeypatch):
    """The one bus this module connects to is the session bus (the portal)."""
    made = []

    class Bus:
        def __init__(self, **kwargs):
            made.append(kwargs)

        async def connect(self):
            return self
    monkeypatch.setattr(sys.modules["dbus_fast"], "BusType",
                        types.SimpleNamespace(SESSION="session", SYSTEM="system"))
    monkeypatch.setattr(sys.modules["dbus_fast.aio"], "MessageBus", Bus)
    asyncio.run(portal._session_bus())
    assert made == [{"bus_type": "session"}]
    source = open(portal.__file__).read()
    assert "SYSTEM" not in source and "system_bus" not in source


def test_a_status_line_the_portal_would_refuse_is_never_sent():
    for bad in ("", "x" * 97):
        with pytest.raises(ValueError):
            asyncio.run(portal.announce_background(print, message=bad))


@pytest.mark.parametrize("sandboxed", [True, False])
def test_the_service_announces_itself_only_inside_flatpak(harness, monkeypatch, sandboxed):
    """Inside a sandbox the service starts the announcement once it listens and cancels it when
    it ends; outside one it never does."""
    started, ended = [], []

    async def announce(log):
        started.append(log)
        try:
            await asyncio.sleep(3600)
        finally:
            ended.append(True)
    monkeypatch.setattr(daemon, "announce_background", announce)
    monkeypatch.setattr(daemon, "in_flatpak", lambda: sandboxed)

    async def scenario(h):
        assert (await h.ping())["ok"]
        await asyncio.sleep(0.05)
        assert len(started) == (1 if sandboxed else 0)
    harness.run(scenario)
    assert started == ([daemon._log] if sandboxed else [])
    assert ended == ([True] if sandboxed else [])
