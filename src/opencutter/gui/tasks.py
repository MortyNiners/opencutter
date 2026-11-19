# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Start asyncio work from GTK callbacks (the app runs asyncio on the GLib main loop)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

from gi.repository import Gio

log = logging.getLogger(__name__)


def spawn(coro: Coroutine) -> asyncio.Task:
    """Run ``coro`` as a task; the application stays alive until it ends, errors are logged."""
    app = Gio.Application.get_default()
    if app is not None and hasattr(app, "create_asyncio_task"):
        task = app.create_asyncio_task(coro)
    else:
        task = asyncio.get_running_loop().create_task(coro)
    task.add_done_callback(_log_failure)
    return task


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("background task failed", exc_info=task.exception())
