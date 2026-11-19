# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""What the background service tells the desktop about itself, inside a Flatpak sandbox.

Once the window is closed, the service's sandbox is an instance of the app without a window, and
GNOME lists the app under Background Apps (quick settings). Its close button there ends every
instance at once (`flatpak kill`, SIGKILL): the service then cannot release the Bluetooth link,
and the machine may need to be switched off and on. So the service sets the line that GNOME
shows under the app's name (the Background portal's ``SetStatus``), which says what it does and
how to release it. The line names the machine: the CAMEO 4 when the service starts
(BACKGROUND_STATUS), and the connected machine's model once another one is connected
(background_status).

This talks to the session bus only (the portal), never to the system bus, and sends nothing to
the machine. It is best effort: without a session bus, or with a portal that has no
``SetStatus`` (xdg-desktop-portal before 1.17), the service logs one line and runs as usual.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
BACKGROUND = "org.freedesktop.portal.Background"
# the line under "OpenCutter" in GNOME's Background Apps; the portal refuses more than 96
# characters
BACKGROUND_STATUS = ("Holds the CAMEO 4 link. Use Disconnect in OpenCutter to release it; do not "
                     "close it here.")
# the same line for another machine, and the line for a model whose name makes it too long
BACKGROUND_STATUS_FOR = ("Holds the {name} link. Use Disconnect in OpenCutter to release it; do "
                         "not close it here.")
BACKGROUND_STATUS_ANY = ("Holds the link to the cutter. Use Disconnect in OpenCutter to release "
                         "it; do not close it here.")
MAX_STATUS = 96
CALL_TIMEOUT = 10.0


async def _session_bus():
    """A connected dbus-fast MessageBus on the session bus (never the system bus)."""
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus
    return await MessageBus(bus_type=BusType.SESSION).connect()


def background_status(name: str) -> str:
    """The status line of a service that holds the link to the machine called `name`
    (BACKGROUND_STATUS for the CAMEO 4), or BACKGROUND_STATUS_ANY when the line would be longer
    than the portal takes (MAX_STATUS) or `name` is not plain printable text."""
    if not (isinstance(name, str) and name.strip() and name.isprintable() and name.isascii()):
        return BACKGROUND_STATUS_ANY
    line = BACKGROUND_STATUS_FOR.format(name=name.strip())
    return line if len(line) <= MAX_STATUS else BACKGROUND_STATUS_ANY


def _set_status_message(message: str):
    """The SetStatus call of the Background portal with ``message``."""
    from dbus_fast import Message, Variant
    return Message(destination=PORTAL_NAME, path=PORTAL_PATH, interface=BACKGROUND,
                   member="SetStatus", signature="a{sv}",
                   body=[{"message": Variant("s", message)}])


async def announce_background(log: Callable[[str], None],
                              message: str = BACKGROUND_STATUS,
                              connect: Callable[[], Awaitable] = _session_bus,
                              timeout: float = CALL_TIMEOUT) -> bool:
    """Set the Background portal's status line of this app to ``message``; True when the portal
    accepted it. The session bus connection stays open until the task is cancelled (when the
    service ends), so the line lasts as long as the service. A failure is logged with one line
    and never raised; only CancelledError is (and ValueError for a ``message`` the portal would
    refuse: empty or longer than MAX_STATUS)."""
    if not 0 < len(message) <= MAX_STATUS:
        raise ValueError(f"the status line must have 1 to {MAX_STATUS} characters")
    bus = None
    try:
        bus = await asyncio.wait_for(connect(), timeout)
        reply = await asyncio.wait_for(bus.call(_set_status_message(message)), timeout)
        error = getattr(reply, "error_name", None)
        if error:
            body = getattr(reply, "body", None) or [""]
            log(f"the desktop was not told what the background service does ({error}: "
                f"{str(body[0])[:120]})")
            return False
        log("told the desktop what the background service does (Background Apps)")
        with contextlib.suppress(Exception):
            await bus.wait_for_disconnect()       # the session ended, or cancelled at the end
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - best effort: the service runs on without it
        log(f"the desktop was not told what the background service does "
            f"({type(e).__name__}: {str(e)[:120]})")
        return False
    finally:
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.disconnect()
