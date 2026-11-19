# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Regression tests for review finding daemon-5: `opencutter stop` must reach the daemon on the
default runtime socket even when $OPENCUTTER_SOCKET is still exported (e.g. from development
against tests/fake_daemon.py), and say which socket and machine each stop went to. It also
reaches older daemons: $XDG_RUNTIME_DIR/opencutter.sock (builds before 0.1.0) and the legacy
prototype daemon's socket, and reports those only when one answers.

The "default" socket is always in a private temp $XDG_RUNTIME_DIR; nothing reaches Bluetooth,
and nothing connects to the user's real sockets.
"""
import asyncio
import json
import os
import shutil
import socketserver
import tempfile
import threading

import pytest

import stubs
from stubs import harness, stops  # noqa: F401

stubs.install()
from opencutter import APP_ID, __main__ as cli  # noqa: E402
from opencutter.device import protocol  # noqa: E402
from opencutter.device.client import SOCKET_ENV  # noqa: E402


def _short_tmpdir(prefix: str) -> str:
    base = tempfile.gettempdir()
    return tempfile.mkdtemp(prefix=prefix, dir=base if len(base) < 60 else "/tmp")


def _service_socket(runtime: str) -> str:
    """The service socket in a runtime dir (made, as the daemon would)."""
    os.makedirs(os.path.join(runtime, "app", APP_ID), mode=0o700, exist_ok=True)
    return os.path.join(runtime, "app", APP_ID, "opencutter.sock")


@pytest.fixture
def rundir(monkeypatch):
    """A private, empty $XDG_RUNTIME_DIR (the user's session in these tests)."""
    d = _short_tmpdir("ocr-")
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def recording_daemon(path: str, name: str):
    """A stand-in daemon that acknowledges stop and answers ping with `name`."""
    seen: list[str] = []

    async def handle(reader, writer):
        op = json.loads(await reader.readline())["op"]
        seen.append(op)
        resp = {"ok": True, "errors": []} if op == "stop" else {
            "ok": True, "connected": True, "status": "0", "motion": None,
            "device": {"name": name, "address": "AA:BB:CC:DD:EE:FF"}}
        writer.write((json.dumps(resp) + "\n").encode())
        await writer.drain()
        writer.close()
    return await asyncio.start_unix_server(handle, path=path), seen


def test_stop_reaches_the_default_daemon_although_the_variable_points_elsewhere(
        harness, monkeypatch, capsys):
    """The reviewer's scenario: $OPENCUTTER_SOCKET still points at the simulated machine; the
    app's daemon for the real machine listens on the default runtime socket."""
    session_dir = _short_tmpdir("ocu-")
    default = _service_socket(session_dir)

    async def scenario(h):
        f = await h.fresh()
        server, seen = await recording_daemon(default, "CAMEO 4-real")
        monkeypatch.setenv("XDG_RUNTIME_DIR", session_dir)
        monkeypatch.setenv(SOCKET_ENV, h.socket)
        assert cli.stop_targets() == [default, h.socket, os.path.join(session_dir, "opencutter.sock"),
                                      os.path.join(session_dir, "cameo4.sock")]
        try:
            ok = await cli.stop_everywhere()
        finally:
            server.close()
        return ok, seen, stops(f.writes)
    try:
        ok, seen, simulated_stops = harness.run(scenario)
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)
    out, err = capsys.readouterr()
    assert ok is True
    assert seen == ["stop", "ping"]                  # the stop first, then the label
    assert simulated_stops == 1
    assert f"STOP sent (ESC 09 twice) to CAMEO 4-real [AA:BB:CC:DD:EE:FF] via {default}" in out
    assert f"to CAMEO 4-fake [00:00:00:00:00:01] via {harness.socket}" in out
    assert out.count("STOP sent") == 2
    assert f"${SOCKET_ENV} is set ({harness.socket})" in err


def test_stop_without_the_variable_goes_to_the_default_socket_only(harness, capsys):
    async def scenario(h):
        f = await h.fresh()
        # harness: XDG_RUNTIME_DIR = its dir; then the sockets of older daemons there
        assert cli.stop_targets() == [h.socket, h.old_socket, os.path.join(h.dir, "cameo4.sock")]
        return await cli.stop_everywhere(), stops(f.writes)
    ok, n = harness.run(scenario)
    out, err = capsys.readouterr()
    assert ok and n == 1
    assert out.splitlines()[0] == (f"STOP sent (ESC 09 twice) to CAMEO 4-fake "
                                   f"[00:00:00:00:00:01] via {harness.socket}")
    assert SOCKET_ENV not in err
    assert harness.old_socket not in out + err and "cameo4.sock" not in out + err   # none there


def test_variable_equal_to_the_default_is_stopped_once(harness, monkeypatch, capsys):
    monkeypatch.setenv(SOCKET_ENV, harness.socket)

    async def scenario(h):
        f = await h.fresh()
        assert cli.stop_targets() == [h.socket, h.old_socket, os.path.join(h.dir, "cameo4.sock")]
        return await cli.stop_everywhere(), stops(f.writes)
    ok, n = harness.run(scenario)
    out, err = capsys.readouterr()
    assert ok and n == 1 and out.count("STOP sent") == 1
    assert f"${SOCKET_ENV} is set" in err


def test_one_target_down_is_reported_and_the_other_still_stops(harness, monkeypatch, capsys):
    gone = os.path.join(harness.dir, "gone.sock")
    monkeypatch.setenv(SOCKET_ENV, gone)

    async def scenario(h):
        f = await h.fresh()
        return await cli.stop_everywhere(), stops(f.writes)
    ok, n = harness.run(scenario)
    out, err = capsys.readouterr()
    assert ok and n == 1
    assert f"via {harness.socket}" in out
    assert f"!! STOP did NOT get through via {gone} (no daemon answers)" in err
    assert "PRESS THE POWER BUTTON" not in err      # one daemon acknowledged


def test_a_daemon_without_the_machine_does_not_count(harness, capsys):
    async def scenario(h):
        first = h.fake
        h.connected_prop = True                    # keep it from reconnecting
        first.is_connected = False
        first.cb_disc(first)
        await asyncio.sleep(0.3)
        return await cli.stop_everywhere()
    assert harness.run(scenario) is False
    out, err = capsys.readouterr()
    assert f"via {harness.socket} (no machine connected)" in err
    assert "PRESS THE POWER BUTTON" in err and "STOP sent" not in out


def test_no_daemon_anywhere_fails_loudly(rundir, monkeypatch, capsys):
    other = os.path.join(rundir, "dev.sock")
    monkeypatch.setenv(SOCKET_ENV, other)
    assert asyncio.run(cli.stop_everywhere()) is False
    out, err = capsys.readouterr()
    assert f"via {_service_socket(rundir)}" in err and f"via {other}" in err
    assert f"via {os.path.join(rundir, 'opencutter.sock')}" not in err   # no older daemon there
    assert "PRESS THE POWER BUTTON" in err and out == ""
    with pytest.raises(SystemExit) as e:
        cli.main(["stop"])
    assert e.value.code == 1


def test_no_runtime_dir_and_no_variable(monkeypatch, capsys):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    assert cli.stop_targets() == []
    assert asyncio.run(cli.stop_everywhere()) is False
    assert "XDG_RUNTIME_DIR is not set" in capsys.readouterr().err


class _Ack(socketserver.StreamRequestHandler):
    def handle(self):
        op = json.loads(self.rfile.readline())["op"]
        self.server.seen.append(op)
        resp = {"ok": True, "errors": []} if op == "stop" else {
            "ok": True, "connected": True, "device": {"name": "CAMEO 4-dev", "address": "x"}}
        self.wfile.write((json.dumps(resp) + "\n").encode())


def test_cli_exit_status_follows_the_acknowledgement(rundir, monkeypatch, capsys):
    """`opencutter stop` exits 0 when one daemon acknowledged (here only the one named by the
    variable; the default socket has none), and 1 when none did."""
    path = os.path.join(rundir, "dev.sock")
    server = socketserver.UnixStreamServer(path, _Ack)
    server.seen = []
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setenv(SOCKET_ENV, path)
        assert cli.main(["stop"]) == 0
        assert server.seen == ["stop", "ping"]
    finally:
        server.shutdown()
        server.server_close()
    out, err = capsys.readouterr()
    assert f"to CAMEO 4-dev [x] via {path}" in out
    assert f"via {_service_socket(rundir)}" in err
    with pytest.raises(SystemExit) as e:
        cli.main(["stop"])
    assert e.value.code == 1


def test_stop_reaches_the_new_the_older_and_the_legacy_socket(rundir, capsys):
    """Whichever daemon holds the machine, `opencutter stop` reaches it: the service socket,
    $XDG_RUNTIME_DIR/opencutter.sock of a build before 0.1.0, and the legacy prototype
    daemon's cameo4.sock (stand-in daemons here, in the private runtime dir)."""
    paths = {"new": _service_socket(rundir), "older": os.path.join(rundir, "opencutter.sock"),
             "legacy": os.path.join(rundir, "cameo4.sock")}

    async def scenario():
        servers = {k: await recording_daemon(p, f"CAMEO 4-{k}") for k, p in paths.items()}
        try:
            assert cli.stop_targets() == list(paths.values())
            return await cli.stop_everywhere(), {k: seen for k, (_s, seen) in servers.items()}
        finally:
            for server, _seen in servers.values():
                server.close()
    ok, seen = asyncio.run(scenario())
    out, err = capsys.readouterr()
    assert ok and seen == {k: ["stop", "ping"] for k in paths}
    for k, p in paths.items():
        assert f"STOP sent (ESC 09 twice) to CAMEO 4-{k} [AA:BB:CC:DD:EE:FF] via {p}" in out
    assert err == ""


def test_a_leaked_flatpak_id_does_not_move_the_stop(rundir, monkeypatch, tmp_path):
    """Outside a Flatpak sandbox a $FLATPAK_ID (leaked from a Flatpak editor's terminal, say) is
    ignored: the stop goes to the service of app/<APP_ID>, as without it."""
    monkeypatch.setattr(protocol, "FLATPAK_INFO", str(tmp_path / "missing"))
    monkeypatch.setenv("FLATPAK_ID", "com.visualstudio.code")
    assert cli.stop_targets() == [_service_socket(rundir), os.path.join(rundir, "opencutter.sock"),
                                  os.path.join(rundir, "cameo4.sock")]
    assert cli.service_targets() == [_service_socket(rundir)]


def test_stop_reaches_app_id_inside_a_sandbox_of_another_id(rundir, monkeypatch, tmp_path,
                                                             capsys):
    """Inside a Flatpak sandbox of another id (a .Devel build) the service socket is
    app/<that id>; the machine stop still tries app/<APP_ID> too, and reports it only when a
    daemon answers there."""
    sandbox = tmp_path / ".flatpak-info"
    sandbox.write_text("[Application]\n")
    monkeypatch.setattr(protocol, "FLATPAK_INFO", str(sandbox))
    monkeypatch.setenv("FLATPAK_ID", f"{APP_ID}.Devel")
    devel = os.path.join(rundir, "app", f"{APP_ID}.Devel", "opencutter.sock")
    main = _service_socket(rundir)
    assert cli.stop_targets() == [devel, main, os.path.join(rundir, "opencutter.sock"),
                                  os.path.join(rundir, "cameo4.sock")]

    async def scenario(answer: bool):
        server = seen = None
        if answer:
            server, seen = await recording_daemon(main, "CAMEO 4-host")
        try:
            return await cli.stop_everywhere(), seen
        finally:
            if server is not None:
                server.close()
    ok, seen = asyncio.run(scenario(True))
    out, err = capsys.readouterr()
    assert ok and seen == ["stop", "ping"]
    assert f"STOP sent (ESC 09 twice) to CAMEO 4-host [AA:BB:CC:DD:EE:FF] via {main}" in out
    assert f"via {devel} (no daemon answers)" in err
    ok, _ = asyncio.run(scenario(False))
    out, err = capsys.readouterr()
    assert not ok and main not in out + err                          # nothing there: not reported
