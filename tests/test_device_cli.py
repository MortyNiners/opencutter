# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The command line: argument parsing, the app entry point and the operator prompt."""
import asyncio
import os
import pty
import shutil
import sys
import tempfile
import types

import pytest

import stubs

stubs.install()
from opencutter import __main__ as cli  # noqa: E402
from opencutter.device import daemon  # noqa: E402


@pytest.fixture
def rundir(monkeypatch):
    """An empty private $XDG_RUNTIME_DIR: no daemon can answer there."""
    d = tempfile.mkdtemp(prefix="occ-", dir="/tmp" if len(tempfile.gettempdir()) > 60 else None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    monkeypatch.setenv("XDG_STATE_HOME", d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_no_arguments_start_the_app(monkeypatch):
    calls = []
    fake_app = types.ModuleType("opencutter.gui.app")
    fake_app.main = lambda: calls.append("gui") or 0
    monkeypatch.setitem(sys.modules, "opencutter.gui.app", fake_app)
    assert cli.main([]) == 0 and calls == ["gui"]


def test_ctrl_c_in_the_terminal_ends_the_app_without_a_traceback(monkeypatch):
    """PyGObject quits the app on SIGINT and then raises KeyboardInterrupt from app.run():
    `opencutter` must end with 130, like the command-line tools, not with a traceback."""
    fake_app = types.ModuleType("opencutter.gui.app")

    def interrupted():
        raise KeyboardInterrupt
    fake_app.main = interrupted
    monkeypatch.setitem(sys.modules, "opencutter.gui.app", fake_app)
    try:
        status = cli.main([])
    except KeyboardInterrupt:            # caught here: pytest would stop the whole run on it
        pytest.fail("Ctrl+C reached the caller as a KeyboardInterrupt")
    assert status == 130


def test_serve_subcommand_passes_its_options(rundir, monkeypatch):
    seen = []

    async def fake_serve(address, verbose, force=False, idle_exit=0):
        seen.append((address, verbose, force, idle_exit))
    monkeypatch.setattr(daemon, "serve", fake_serve)
    assert cli.main(["serve", "--force", "-v", "--address", "AA:BB"]) == 0
    assert cli.main(["serve"]) == 0
    assert cli.main(["serve", "--idle-exit", "900"]) == 0
    assert seen == [("AA:BB", True, True, 0), (None, False, False, 0), (None, False, False, 900)]


@pytest.mark.parametrize("argv", [["status"], ["info"], ["dry-run"]])
def test_commands_need_a_daemon(rundir, monkeypatch, argv):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert "daemon" in str(e.value.code)


def test_stop_without_a_daemon_fails_loudly(rundir, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["stop"])
    assert e.value.code == 1
    assert "POWER BUTTON" in capsys.readouterr().err


def test_programs_need_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    with pytest.raises(SystemExit, match="terminal"):
        asyncio.run(cli.run_program(cli.DaemonClient("/nonexistent/x.sock"), "dry-run", {}))


@pytest.mark.parametrize("argv", [["cut"], ["pen-test", "--pen-force", "33"], ["serve", "--nope"],
                                  ["serve", "--idle-exit", "-1"], ["serve", "--idle-exit", "x"],
                                  ["service"], ["service", "restart"]])
def test_parser_refuses(argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2


def test_texts_cover_every_program():
    assert set(cli.INTRO) == set(cli.OUTRO) == {"dry-run", "pen-test", "cut-test"}
    assert "{force}" in cli.INTRO["pen-test"]


def test_ainput_discards_type_ahead(monkeypatch):
    """Enter presses typed before the prompt are discarded (real pty)."""
    master, slave = pty.openpty()
    monkeypatch.setattr(sys, "stdin", os.fdopen(slave, "r", closefd=False))

    async def scenario():
        os.write(master, b"\n\n\n")             # typed ahead while a step was moving
        await asyncio.sleep(0.1)
        asyncio.get_running_loop().call_later(0.3, os.write, master, b"x\n")
        return await asyncio.wait_for(cli.ainput(""), 3)
    try:
        assert asyncio.run(scenario()).strip() == "x"
    finally:
        os.close(master)
        os.close(slave)
