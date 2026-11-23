# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""OpenCutter entry point.

  opencutter                 start the app
  opencutter serve           hold the Bluetooth link (the app starts this by itself)
  opencutter status          the machine, its model and its status (ESC 05)
  opencutter info            status, tools, sensor, firmware, machine profile and test programs
  opencutter stop            stop the MACHINE now (ESC 09 twice), from any terminal, through
                             every OpenCutter service that runs; the service keeps running
  opencutter pause           pause the job or test program that runs now (ESC 07), as the
                             machine's Pause key does; it then waits for the machine
  opencutter resume          go on with that job once it is paused (ESC 08), from where it
                             stopped, as pressing the machine's Pause key again does
  opencutter service stop    stop the background service (not the machine): it releases the
                             Bluetooth link, so other programs can use the machine
  opencutter service status  which background service runs: version, build, pid, machine,
                             model
  opencutter dry-run         motion test with the tool always UP (nothing touches the mat)
  opencutter pen-test        20 mm square with a Sketch Pen in Tool 1
  opencutter cut-test        cut one 1-inch grid square in adhesive vinyl (AutoBlade, Tool 1)

The machine must already be paired in the system Bluetooth settings. The daemon reads the model
a machine reports (FG) and drives it only when OpenCutter knows how (see "Machines" in
`opencutter --help`). The test programs exist for the CAMEO 4 only. They run only through the
daemon, one step per Enter; Ctrl+C stops the machine. A step waits as long as the machine is
paused (its Pause key): press it again, or run `opencutter resume`, to go on. The machine's
power button is the only stop that does not depend on its firmware.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import os
import sys
import textwrap
import time

from . import __version__
from .device.client import (PING_TIMEOUT, SOCKET_ENV, DaemonClient, DaemonError, Session,
                            app_socket_path, legacy_socket_path, older_socket_path,
                            ping_with_peer, service_socket_path, shutdown_service,
                            terminate_older_daemon)
from .device.programs import PINNED_PROGRAMS
from .device.protocol import (PAUSED, REASON_WRONG_MODEL, STOP_CHECK_PAUSED, describe_tools,
                              model_text, status_text, wrong_model_message)
from .identity import API_VERSION, BUILD, describe, same_build, this_build
from .machines import CAMEO4, MachineProfile, Support, ble_name_prefixes, detect, registry

# A step has no time limit (a paused machine waits as long as it takes): while it runs, the
# service is pinged every WATCH_EVERY s, and changes are printed (paused, going on, no answer).
WATCH_EVERY = 2.0
WATCH_MISSES = 3           # pings in a row without an answer: the stop is sent
POWER = "PRESS THE POWER BUTTON if it is still moving"
NO_DAEMON = "No OpenCutter daemon is running. Start it with `opencutter serve` or open the app."
NOT_CONNECTED = ("The daemon is running but the machine is not connected yet "
                 "(switch the machine off and on).")
OLDER_RUNNING = ("An older OpenCutter background service is running ({path}). Stop it with "
                 "`opencutter service stop`, then open the app or run `opencutter serve`.")
OTHER_BUILD = ("The background service is {service}, but this command is {mine}. Run "
               "`opencutter service stop` first, then try again; nothing was sent.")
# The ping's "support" in words (the profile line of `status` and `info`).
SUPPORT_WORDS = {"tested": "tested on real hardware",
                 "untested": "not yet tested on this model",
                 "recognized": "recognized, but not driven"}
# "untested" for a tested model on a firmware line that is not (the CAMEO 4 on firmware V1.xx)
UNTESTED_FIRMWARE = "not yet tested with this firmware"
# After a recognized model's plain-words reason (the ping's "refused_reason"); the service's
# own refusals say the same (daemon.RECOGNIZED_REFUSAL).
RECOGNIZED_REFUSAL = "Nothing more is sent to it."
NO_PROGRAMS = "There are no test programs for the {name}; nothing was sent."
# What the service adds to a ping that asks for it ({"op": "ping", "machine": true}); the
# service's own list is daemon.PING_MACHINE_KEYS.
PING_MACHINE_KEYS = ("profile", "support", "refused_reason")

INTRO = {
    "dry-run": """\
DRY RUN: the tool stays UP the whole time; nothing touches the mat.
It lets you check the axis order, the direction of the carriage and (with a ruler) the 0.05 mm unit.

Before you start:
  [ ] the 12x12 Cutting Mat (with or without a sheet) was loaded on the machine JUST NOW
      (choose the mat option if the panel asks), with no other job since
  [ ] free space behind the machine (the mat goes up to about 8 cm further in)
  [ ] hands away from the carriage and the rollers; a finger ready on the power button
Each step starts only when you press Enter. Ctrl+C stops (ESC 09 twice).
If another motor moves than described, it moves much further, a tool goes down or something
heads for a collision: Ctrl+C, then the power button.
If only the carriage direction or the exact position differs: not a fault, note it and go on.""",
    "pen-test": """\
PEN TEST: a 20 mm square and a 5 mm tick, Speed 1, Force {force}.

Before you start:
  [ ] a Sketch Pen is in Tool 1 (AutoBlade taken out); Tool 2 is not used
  [ ] paper on the 12x12 Cutting Mat, in the top-left corner of the grid (at least 4 x 4 cm)
  [ ] the mat was loaded on the machine JUST NOW (choose the mat option if the panel asks);
      after a dry run, unload it and load it again
  [ ] hands away from the carriage and the rollers; a finger ready on the power button
Each step starts only when you press Enter. Ctrl+C stops (ESC 09 twice).""",
    "cut-test": """\
CUT TEST: cuts one grid square (25.4 mm) in the middle of an adhesive vinyl sheet with the
AutoBlade at Blade Depth 1 (0.1 mm: cuts the vinyl, not the backing), Speed 5, Force 10.

Before you start:
  [ ] AutoBlade in Tool 1; Tool 2 is not used
  [ ] the vinyl sheet on the mat: full width, about 5 squares down from the top of the grid
  [ ] the mat was loaded on the machine JUST NOW (choose the mat option), with no other job since
  [ ] hands away from the carriage and the rollers; a finger ready on the power button
Steps 4 and 5 show two opposite corners of the square with the blade UP. If the blade is not
above the sheet: q + Enter, and nothing is cut.
Each step starts only when you press Enter. Ctrl+C stops (ESC 09 twice).""",
}
OUTRO = {
    "dry-run": "Done. Note what moved in steps 5 and 6 (rollers or carriage, which way, how far).",
    "pen-test": "Done. Measure the side of the square (20.0 mm?), the distance from the top-left "
                "corner of the grid to the corner of the square (10 mm + ? in both directions) and "
                "where the tick is.",
    "cut-test": "Done. Measure the side of the cut square (25.4 mm = exactly one grid square?), its "
                "distance from the top edge of the sheet (about 50.8 mm) and from the left edge of "
                "the grid (about 139.7 mm). Peel the square: did it cut the vinyl but not the backing?",
}


async def ainput(prompt: str) -> str:
    """input() that does not block the event loop (Ctrl+C still cancels).

    Anything typed before the prompt appears (e.g. extra Enter presses while a step was moving)
    is discarded, so every step needs a fresh Enter after its text is on screen. Reads the fd
    directly (no Python-side buffering of extra lines). Returns '\\x04' on EOF."""
    import termios
    fd = sys.stdin.fileno()
    with contextlib.suppress(termios.error, OSError):
        termios.tcflush(fd, termios.TCIFLUSH)
    print(prompt, end="", flush=True)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def ready() -> None:
        if not fut.done():
            try:
                fut.set_result(os.read(fd, 4096).decode("utf-8", "replace"))
            except OSError as e:
                fut.set_exception(e)
    loop.add_reader(fd, ready)
    try:
        line = await fut
    finally:
        loop.remove_reader(fd)
    return line if line else "\x04"          # EOF -> treated as "quit"


def after_stop_line(resp: dict) -> tuple[str, bool] | None:
    """What a stop reply says about the machine afterwards (its "status_after", from a stop
    with check=True), as (line, alarm), or None when it says nothing (an older service). The
    words never claim a proven stop: a machine that reads Ready may still have motion queued."""
    if "status_after" not in resp:
        return None
    status = resp.get("status_after")
    if status is None:
        return "  the status after the stop could not be read: watch the machine.", False
    if resp.get("still_busy"):
        return (f"!! the machine still reports Busy (status {status!r}) a few seconds after the "
                f"stop: {POWER}", True)
    if status_text(status, CAMEO4, default="") == PAUSED:
        return (f"  the machine still reports Paused {STOP_CHECK_PAUSED:g} s after the stop: "
                f"press Unload on the machine; {POWER}", True)
    return (f"  the machine now reports status {status!r} = {status_text(status, CAMEO4)}.", False)


def _print_after_stop(resp: dict) -> None:
    line = after_stop_line(resp)
    if line is not None:
        print(line[0], file=sys.stderr if line[1] else sys.stdout)


async def send_stop(client: DaemonClient) -> bool:
    """Ask the daemon to stop and tell the operator what happened. Never raises; True if the
    stop got through."""
    resp = await client.stop(check=True)
    if not resp.get("ok"):
        print(f"!! STOP did NOT get through ({resp.get('error') or resp.get('errors')}): {POWER}",
              file=sys.stderr)
        return False
    print("STOP sent (ESC 09 twice). If it is still moving: the power button.")
    _print_after_stop(resp)
    return True


def stop_targets() -> list[str]:
    """The sockets `opencutter stop` stops through: always the service socket (the daemon the
    app starts for the real machine), $OPENCUTTER_SOCKET as well when it is set and different
    (e.g. still exported from development against the simulated machine), and the sockets of
    older daemons: $XDG_RUNTIME_DIR/opencutter.sock (builds before 0.1.0) and cameo4.sock (the
    legacy prototype daemon). The service socket of an installation outside Flatpak
    (app/<opencutter.APP_ID>) is tried too, whatever the environment says, when it is not the
    same one. A stop must reach whichever of them holds the machine."""
    paths = [service_socket_path(), app_socket_path(), os.environ.get(SOCKET_ENV) or None,
             older_socket_path(), legacy_socket_path()]
    return list(dict.fromkeys(p for p in paths if p))


def _older_targets() -> set[str]:
    """The sockets reported by `opencutter stop` only when a daemon answers there: those of
    older daemons, and app/<APP_ID> when it is not the service socket."""
    return {p for p in (app_socket_path(), older_socket_path(), legacy_socket_path()) if p} - {
        service_socket_path(), os.environ.get(SOCKET_ENV)}


def _machine(ping: dict | None) -> str | None:
    """The machine behind a daemon as its ping names it ('CAMEO 4-XXXXXXX [AA:BB:CC:DD:EE:FF]'),
    or None."""
    dev = (ping or {}).get("device") or {}
    return f"{dev['name']} [{dev.get('address')}]" if dev.get("name") else None


async def _stop_one(path: str) -> tuple[str, dict, dict | None]:
    """Stop through one daemon first, then ask it which machine it holds (for the report)."""
    client = DaemonClient(path)
    resp = await client.stop(check=True)
    return path, resp, await client.ping()


async def stop_everywhere() -> bool:
    """`opencutter stop`: ESC 09 twice through every daemon in stop_targets(), concurrently.
    Prints one line per target with its socket path and machine. Never raises; True if at
    least one daemon acknowledged the stop."""
    env = os.environ.get(SOCKET_ENV)
    if env:
        print(f"note: ${SOCKET_ENV} is set ({env}); the stop also goes to the default socket.",
              file=sys.stderr)
    targets = stop_targets()
    if not targets:
        print(f"!! STOP did NOT get through: XDG_RUNTIME_DIR is not set, so there is no daemon "
              f"socket. {POWER}", file=sys.stderr)
        return False
    results = await asyncio.gather(*(_stop_one(p) for p in targets))
    acknowledged = False
    older = _older_targets()
    for path, resp, ping in results:
        machine = _machine(ping)
        if path in older and ping is None and not resp.get("ok"):
            continue                       # no older daemon there: nothing to report
        if resp.get("ok"):
            acknowledged = True
            print(f"STOP sent (ESC 09 twice) to {machine or 'the machine'} via {path}")
            _print_after_stop(resp)
        else:
            where = machine or ("no daemon answers" if ping is None else "no machine connected")
            why = resp.get("error") or "; ".join(resp.get("errors") or []) or "no answer"
            print(f"!! STOP did NOT get through via {path} ({where}): {why}", file=sys.stderr)
    if acknowledged:
        print("If it is still moving: the power button.")
    else:
        print(f"!! No daemon acknowledged the stop: {POWER}", file=sys.stderr)
    return acknowledged


# --- the background service ---------------------------------------------------------------------
def service_targets() -> list[str]:
    """The sockets `opencutter service stop|status` act on with the shutdown op: the service
    socket, and $OPENCUTTER_SOCKET as well when it is set and different."""
    paths = [service_socket_path(), os.environ.get(SOCKET_ENV) or None]
    return list(dict.fromkeys(p for p in paths if p))


def _older_service_path(targets: list[str]) -> str | None:
    path = older_socket_path()
    return path if path and path not in targets else None


def motion_words(name: object) -> str:
    """What a ping's "motion" names, in words: 'a job from the app' for "job" (the app's
    sessions), else '<name> (a test program)'."""
    shown = str(name)[:40]
    return "a job from the app" if shown == "job" else f"{shown} (a test program)"


async def _end_service(path: str, ping: dict) -> tuple[bool, str]:
    """End the service on `path` (the shutdown op, or for an older one without it the guarded
    SIGTERM). Returns (ended, the line to print)."""
    who = describe(ping)
    if ping.get("motion"):
        return False, (f"!! Not stopped: {who} at {path} is running "
                       f"{motion_words(ping['motion'])}. Cancel it in the app, or stop the "
                       "machine with `opencutter stop`, then run `opencutter service stop` "
                       "again.")
    if ping.get("build") is None:
        result = await terminate_older_daemon(path)
    else:
        result = await shutdown_service(path)
    pid = result.get("pid")
    at = f"{who}{f' (pid {pid})' if pid else ''} at {path}"
    if result.get("ok"):
        return True, f"Stopped {at}; the machine is free for other programs."
    if result.get("busy"):
        return False, (f"!! Not stopped: {at} is running a job ({result.get('error')}). Cancel "
                       "it in the app, or stop the machine with `opencutter stop`, then run "
                       "`opencutter service stop` again.")
    return False, f"!! Not stopped: {at}: {result.get('error')}"


async def service_stop() -> bool:
    """`opencutter service stop`: end every OpenCutter background service, one line each. Never
    sends anything to the machine, and refuses while a program or job runs. True when none
    runs any more."""
    targets = service_targets()
    if not targets:
        print("!! XDG_RUNTIME_DIR is not set, so there is no background service socket.",
              file=sys.stderr)
        return False
    if os.environ.get(SOCKET_ENV):
        print(f"note: ${SOCKET_ENV} is set ({os.environ[SOCKET_ENV]}); the service on the default "
              "socket is stopped too.", file=sys.stderr)
    all_ended = True
    older = _older_service_path(targets)
    for path in targets + ([older] if older else []):
        ping = await DaemonClient(path).ping()
        if ping is None:
            if path != older:
                print(f"OpenCutter background service: not running ({path})")
            continue
        ended, line = await _end_service(path, ping)
        all_ended = all_ended and ended
        print(line, file=sys.stdout if ended else sys.stderr)
    return all_ended


def _pid_line(pid: int | None, ping: dict) -> str:
    """The service's pid as this process sees it (SO_PEERCRED). None when it cannot see it:
    from inside a Flatpak sandbox, the service runs in a sandbox of its own (or on the host),
    and the pid its ping reports belongs to that sandbox, not to this one."""
    if pid:
        return str(pid)
    if isinstance(ping.get("pid"), int):
        return ("not visible from here (the service runs in another sandbox, or outside this "
                "one; `flatpak ps` on the host lists the app's sandboxes)")
    return "unknown"


async def service_status() -> bool:
    """`opencutter service status`: what runs on each service socket. True if a service runs."""
    targets = service_targets()
    if not targets:
        print("!! XDG_RUNTIME_DIR is not set, so there is no background service socket.",
              file=sys.stderr)
        return False
    found = False
    older = _older_service_path(targets)
    for path in targets + ([older] if older else []):
        ping, pid = await ping_with_peer(path)
        if ping is None:
            if path != older:
                print(f"OpenCutter background service: not running ({path})")
            continue
        found = True
        ping = await with_machine(DaemonClient(path), ping)
        version, build, api = ping.get("version"), ping.get("build"), ping.get("api")
        if build is None:
            build_line = "none (an older service): run `opencutter service stop`"
        elif same_build(ping):
            build_line = f"{build} (the same as this command)"
        else:
            build_line = (f"{build}, but this command is build {BUILD} (api {API_VERSION}): run "
                          "`opencutter service stop`, then open the app")
        dev = ping.get("device") or {}
        if ping.get("connected"):
            machine = f"{dev.get('name') or 'connected'} [{dev.get('address')}], connected"
        else:
            reason = ping.get("reason")
            machine = "not connected" + (f" ({reason})" if reason else "")
        print(f"OpenCutter background service at {path}")
        print(f"  version: {version if version is not None else 'unknown (an older service)'}")
        print(f"  build:   {build_line}")
        print(f"  api:     {api if api is not None else 'none'}")
        print(f"  pid:     {_pid_line(pid, ping)}")
        print(f"  machine: {machine}")
        model = _model_line(ping)
        if model:
            print(f"  model:   {model}")
        running = motion_words(ping["motion"]) + " is running" if ping.get("motion") else "none"
        print(f"  job:     {running}")
    return found


def _model_line(ping: dict) -> str | None:
    """What the machine reports itself as (the ping's "model"), and whether the service refused
    it (for a model OpenCutter recognizes, with its reason); for a connected machine that is
    not tested ("support"), "not yet tested on this model" (or "with this firmware":
    _support_words) and its profile id. None when the
    ping names no model and refused none (no machine yet, or an older service)."""
    model = ping.get("model")
    if not ping.get("connected") and ping.get("reason") == REASON_WRONG_MODEL:
        shown = model_text(model) if model else "not reported"
        reason = _refused_reason(ping)
        if reason:
            return f"{shown}, refused: {reason.rstrip('.')}"
        if model and detect(model).shared:
            return (f"{shown}, refused: more than one model reports this name, and OpenCutter "
                    "cannot tell which one it is")
        return f"{shown}, refused: OpenCutter cannot drive this model"
    if not model:
        return None
    support = ping.get("support")
    if ping.get("connected") and support not in (None, Support.TESTED.value):
        return (f"{model_text(model)}, {_support_words(ping)} "
                f"(profile {model_text(ping.get('profile'))})")
    return model_text(model)


def _refused_reason(ping: dict) -> str | None:
    """The plain-words reason of a refused model OpenCutter recognizes (the ping's
    "refused_reason"), or None."""
    reason = ping.get("refused_reason")
    return reason if isinstance(reason, str) and reason.strip() else None


def _not_connected(ping: dict) -> str:
    """Why a command cannot use the machine: the model message when the service refused the
    machine for its model (for a recognized model its reason), else NOT_CONNECTED."""
    if ping.get("reason") == REASON_WRONG_MODEL:
        reason = _refused_reason(ping)
        if reason:
            return f"{reason} {RECOGNIZED_REFUSAL}"
        return wrong_model_message(ping.get("model"))
    return NOT_CONNECTED


def ping_profile(ping: dict) -> MachineProfile | None:
    """The machine profile the service uses for its machine (the ping's "profile"): the CAMEO 4
    for a service from before machine profiles (it drove nothing else), None for a profile
    this command does not know."""
    if "profile" not in ping:
        return CAMEO4
    return registry.enabled_profile(ping.get("profile"))


def _profile_words(ping: dict) -> str:
    """'CAMEO 4 (cameo4), tested on real hardware': the ping's profile and support level."""
    pid = ping.get("profile")
    entry = registry.by_id(pid)
    name = entry.name if entry is not None else model_text(pid)
    return f"{name} ({model_text(pid)}), {_support_words(ping)}"


def _support_words(ping: dict) -> str:
    """The ping's "support" in words (SUPPORT_WORDS). An untested firmware line of a model that
    is tested (the CAMEO 4 on V1.xx) is UNTESTED_FIRMWARE: the model itself was tested."""
    support = ping.get("support")
    entry = registry.by_id(ping.get("profile"))
    if (support == Support.UNTESTED.value and entry is not None
            and entry.support is Support.TESTED):
        return UNTESTED_FIRMWARE
    return SUPPORT_WORDS.get(support, model_text(support))


def program_names(profile_id: object) -> tuple[str, ...]:
    """The test programs the profile with this id has (dry-run, pen-test, cut-test on the
    CAMEO 4), in their order; () for every other profile."""
    pinned = PINNED_PROGRAMS.get(profile_id) if isinstance(profile_id, str) else None
    return tuple(dict.fromkeys(name for name, _ in pinned.programs)) if pinned else ()


def _no_program(ping: dict, name: str) -> str | None:
    """Why the connected machine has no test program `name`, or None (also when the ping names
    no profile, as from a service before machine profiles: the service refuses by itself)."""
    pid = ping.get("profile")
    if not pid or name in program_names(pid):
        return None
    entry = registry.by_id(pid)
    return NO_PROGRAMS.format(name=entry.name if entry is not None else model_text(pid))


def _status_words(reply: object, profile: MachineProfile | None) -> str:
    """The profile's term for an ESC 05 reply ("Ready"), or '?'."""
    return status_text(reply, profile) if profile is not None else "?"


def _tools_words(reply: object, profile: MachineProfile | None) -> str:
    """The ESC 15 reply with the profile's holder labels and tool names."""
    if profile is None:
        return f"unclear reply {reply!r}"
    return describe_tools(reply, profile)


async def _ping_service(client: DaemonClient) -> dict | None:
    """The ping of the daemon a command will use, or None when none answers; refuses
    (SystemExit) when only an older one (a build before 0.1.0) answers."""
    ping = await client.ping()
    if ping is None:
        older = await client.ping_older()
        if older is not None:
            raise SystemExit(OLDER_RUNNING.format(path=older_socket_path()))
        return None
    return ping


async def with_machine(client: DaemonClient, ping: dict) -> dict:
    """`ping` with the machine profile fields (PING_MACHINE_KEYS: the profile, its support level
    and why a recognized model is refused) of a second look, {"op": "ping", "machine": true}.
    Not asked of a service from before 0.1.0 (its ping has no build); a service before machine
    profiles answers without them, and a failed look leaves `ping` as it is."""
    if ping.get("build") is None:
        return ping
    try:
        detail = await client.request({"op": "ping", "machine": True}, PING_TIMEOUT)
    except (DaemonError, OSError, ValueError):
        return ping
    if isinstance(detail, dict):
        ping.update({key: detail[key] for key in PING_MACHINE_KEYS if key in detail})
    return ping


async def _connected(client: DaemonClient) -> dict:
    ping = await _ping_service(client)
    if ping is None:
        raise SystemExit(NO_DAEMON)
    ping = await with_machine(client, ping)
    if not ping.get("connected"):
        raise SystemExit(_not_connected(ping))
    return ping


async def show_status(client: DaemonClient) -> None:
    """Print the daemon state and the machine status (ESC 05)."""
    ping = await _connected(client)
    profile = ping_profile(ping)
    dev = ping.get("device") or {}
    print(f"machine: {dev.get('name')} [{dev.get('address')}]")
    if ping.get("model"):
        print(f"model:   {model_text(ping['model'])}")
    if ping.get("profile"):
        print(f"profile: {_profile_words(ping)}")
    if ping.get("motion"):
        status = ping.get("status")
        print(f"running: {motion_words(ping['motion'])} (last reported status {status!r} = "
              f"{_status_words(status, profile)})")
        stop = await client.ping_stop() if ping.get("build") is not None else None
        if stop and stop.get("paused"):
            print("         Paused (the open job waits for the machine): press its Pause key or "
                  "run `opencutter resume` to go on, or `opencutter stop`")
        elif stop and isinstance(stop.get("no_reply_s"), (int, float)):
            print(f"         no answer from the machine for {stop['no_reply_s']:.0f} s; the "
                  "service keeps waiting and sends nothing new")
        return
    r = await client.request({"op": "esc", "code": 0x05})
    if not r.get("ok"):
        raise SystemExit(f"status: {r.get('error')}")
    print(f"Status (ESC 05): {r['reply']!r} = {_status_words(r['reply'], profile)}")


async def show_info(client: DaemonClient) -> None:
    """Print status, tools, sensor and firmware, then the machine profile and its test
    programs."""
    ping = await _connected(client)
    profile = ping_profile(ping)
    r = await client.info()
    if not r.get("ok"):
        raise SystemExit(f"info: {r.get('error')}")
    print(f"Status   (ESC 05): {r['status']!r} = {_status_words(r['status'], profile)}")
    print(f"Tools    (ESC 15): {r['tools']!r} = {_tools_words(r['tools'], profile)}")
    sensor = await client.request({"op": "esc", "code": 0x16})
    if sensor.get("ok"):
        print(f"Sensor   (ESC 16): {sensor['reply']!r}")
    print(f"Firmware (FG):     {r['firmware']}")
    if ping.get("profile"):
        print(f"Profile:           {_profile_words(ping)}")
        names = program_names(ping.get("profile"))
        print(f"Test programs:     {', '.join(names) if names else 'none for this machine yet'}")


PAUSED_LINE = ("      .. the machine is paused (status {status!r}). Press its Pause key to go "
               "on, run 'opencutter resume' in another terminal, or press Ctrl+C to stop.")
PAUSED_KEY_LINE = ("      .. the machine is paused (status {status!r}). Press its Pause key to "
                   "go on, or press Ctrl+C to stop.")
GOES_ON_LINE = "      .. the machine goes on (status {status!r})."
NO_ANSWER_LINE = ("      .. no answer from the machine for {seconds:.0f} s; still waiting. Ctrl+C "
                  "stops.")


def watch_lines(ping: dict, seen: dict) -> list[str]:
    """What changed since the last ping of a running step (`seen` keeps what was printed):
    the machine paused, it goes on, or no answer from it for 7 s or more (once per episode)."""
    lines = []
    paused = bool(ping.get("paused"))
    status = ping.get("status")
    if paused and not seen.get("paused"):
        lines.append((PAUSED_LINE if ping.get("resume_ok") else PAUSED_KEY_LINE)
                     .format(status=status))
    elif not paused and seen.get("paused") and not ping.get("motion_stopped") \
            and ping.get("interrupted") is None:
        lines.append(GOES_ON_LINE.format(status=status))
    seen["paused"] = paused
    silent = ping.get("no_reply_s")
    if isinstance(silent, (int, float)) and not seen.get("silent"):
        lines.append(NO_ANSWER_LINE.format(seconds=silent))
    seen["silent"] = isinstance(silent, (int, float))
    return lines


async def run_step(client: DaemonClient, s: Session, index: int) -> dict:
    """Run one step without a time limit (it waits as long as the machine is paused), pinging
    the service every WATCH_EVERY s and printing what changes (watch_lines). Raises DaemonError
    when the step's connection fails or WATCH_MISSES pings in a row get no answer."""
    step = asyncio.ensure_future(s.step(index, None))
    seen: dict = {}
    misses = 0
    try:
        while True:
            done, _ = await asyncio.wait({step}, timeout=WATCH_EVERY)
            if step in done:
                return step.result()
            ping = await client.ping_stop()
            if ping is None:
                misses += 1
                if misses >= WATCH_MISSES:
                    raise DaemonError(f"{WATCH_MISSES} pings in a row got no answer")
                continue
            misses = 0
            for line in watch_lines(ping, seen):
                print(line, flush=True)
    finally:
        if not step.done():
            step.cancel()
            with contextlib.suppress(BaseException):
                await step


async def _run_steps(client: DaemonClient, s: Session, name: str, opened: dict) -> None:
    steps = opened["steps"]
    for i, st in enumerate(steps):
        print(f"\n[{i + 1}/{len(steps)}] {st['title']}\n      Expected: {st['expect']}")
        if (await ainput("      Enter = run this step, q + Enter = stop here: ")).strip():
            print("Stopped before the step; nothing more was sent.")
            return
        try:
            r = await run_step(client, s, i)
        except asyncio.CancelledError:
            print("\nCtrl+C: sending STOP...")
            await send_stop(client)
            raise
        except DaemonError as e:
            print(f"\n!! no answer from the service ({e}): sending STOP...")
            await send_stop(client)
            return
        if not r.get("ok"):
            print(f"      !! STOPPED: {r.get('error')}")
            if r.get("stopped") and r.get("stop_errors"):
                print(f"      !! STOP did NOT get through ({'; '.join(r['stop_errors'])}): {POWER}")
            elif r.get("stopped"):
                print("      STOP sent. If it is still moving: the power button.")
            return
        if st["kind"] == "units":
            print(f"      done, step {i + 1}/{len(steps)}: {r.get('message')}")
            if r.get("verdict") != "ok":
                answer = await ainput('      Continue without the unit confirmed? Type "yes": ')
                if answer.strip().lower() != "yes":
                    print("Stopped; nothing more was sent.")
                    return
        else:
            print(f"      done, step {i + 1}/{len(steps)} in {r.get('seconds')} s "
                  f"(status {r.get('status')!r})")
    print("\n" + OUTRO[name])


async def run_program(client: DaemonClient, name: str, options: dict) -> None:
    """Run a pinned test program interactively, one step per Enter."""
    if not sys.stdin.isatty():
        raise SystemExit("The test programs need an interactive terminal (each step = one Enter).")
    ping = await _ping_service(client)
    if ping is None:
        raise SystemExit("Motion only runs through the daemon: start `opencutter serve` first.")
    if not same_build(ping):
        raise SystemExit(OTHER_BUILD.format(service=describe(ping), mine=this_build()))
    ping = await with_machine(client, ping)
    if not ping.get("connected"):
        raise SystemExit(_not_connected(ping))
    problem = _no_program(ping, name)
    if problem:
        raise SystemExit(problem)
    profile = ping_profile(ping)
    print(INTRO[name].format(force=options.get("pen_force", 1)))
    if (await ainput('\nType "yes" and Enter to start: ')).strip().lower() != "yes":
        print("Cancelled; nothing was sent.")
        return
    async with client.session() as s:
        opened = await s.open_program(name, options)
        if not opened.get("ok"):
            raise SystemExit(f"Refused, nothing was sent: {opened.get('error')}")
        print(f"\nstatus {opened['status']!r} = {_status_words(opened['status'], profile)}; "
              f"tools {opened['tools']!r} = {_tools_words(opened['tools'], profile)}")
        await _run_steps(client, s, name, opened)
        await s.end()


async def command(client: DaemonClient, kind: str) -> bool:
    """`opencutter resume` / `opencutter pause`: one resume op (ESC 08) or pause op (ESC 07)
    through the service of this very build, for the session that runs now. Prints what was sent
    and what the machine reports afterwards, or why nothing was sent. True when it was sent."""
    ping = await _ping_service(client)
    if ping is None:
        raise SystemExit(NO_DAEMON)
    if not same_build(ping):
        raise SystemExit(OTHER_BUILD.format(service=describe(ping), mine=this_build()))
    ping = await with_machine(client, ping)
    if not ping.get("connected"):
        raise SystemExit(_not_connected(ping))
    profile = ping_profile(ping)
    r = await (client.resume() if kind == "resume" else client.pause())
    word, esc = ("Resume", "ESC 08") if kind == "resume" else ("Pause", "ESC 07")
    after = r.get("status_after")
    if r.get("ok"):
        print(f"{word} sent ({esc}); the machine now reports {after} = "
              f"{_status_words(after, profile)}.")
        if kind == "resume" and r.get("paused"):
            print("  It still reports Paused: press its Pause key to go on, or stop the job "
                  "(Cancel Job in the app, Ctrl+C in its terminal, or `opencutter stop`).")
        elif kind == "pause" and not r.get("paused"):
            print("  It does not report Paused yet: a paused job waits until the moves it has "
                  "already taken in are done. Watch the machine.")
        return True
    if r.get("sent") is False:
        print(f"Not sent: {r.get('error')}", file=sys.stderr)
    else:
        print(f"!! {word} ({esc}) may have reached the machine: {r.get('error')}. It is not "
              "sent again; watch the machine.", file=sys.stderr)
    return False


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = -1.0
    if not 0 <= value < 1e9:
        raise argparse.ArgumentTypeError(f"not a number of seconds >= 0: {text!r}")
    return value


_KEEP = "\x00"          # a space inside a machine's name, while the paragraph is wrapped


def _join(names: list[str], last: str = "and") -> str:
    """'A, B and C' ('none' for no names)."""
    if len(names) < 2:
        return names[0] if names else "none"
    return f"{', '.join(names[:-1])} {last} {names[-1]}"


def _unbroken(name: str) -> str:
    """`name` with its spaces kept together while the paragraph is wrapped."""
    return name.replace(" ", _KEEP)


def machines_help() -> str:
    """The paragraph at the end of `opencutter --help` that says which machines OpenCutter
    drives, from the registry (opencutter.machines): the TESTED firmware lines, the UNTESTED
    models and lines (driven, never tried on real hardware), and the models it recognizes but
    does not drive."""
    tested: list[str] = []
    untested: list[str] = []
    for p in registry.enabled():
        for line in p.identity.lines:
            level = p.support.narrower(line.support)
            if not level.jobs_allowed:
                continue
            if p.support is Support.TESTED:         # name its lines: some may be untested
                name = _unbroken(f"{p.name} (firmware V{line.major}.xx)")
                (tested if level is Support.TESTED else untested).append(name)
            elif _unbroken(p.name) not in untested:
                untested.append(_unbroken(p.name))
    known = [_unbroken(m.name) for m in registry.recognized_models()]
    text = [f"Machines: OpenCutter has been tested on real hardware only with the "
            f"{_join(tested)}." if tested else
            "Machines: OpenCutter has not been tested on real hardware with any machine yet."]
    if untested:
        text.append(f"It also drives these models, which have not been tested on real hardware "
                    f"yet: {_join(untested)}.")
    if known:
        text.append(f"It knows these models but cannot drive them yet: {_join(known)}.")
    text.append("The daemon reads the model a machine reports (FG) and sends nothing more to a "
                "machine it does not drive.")
    return textwrap.fill(" ".join(text), width=95, break_on_hyphens=False,
                         break_long_words=False).replace(_KEEP, " ")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="opencutter", description=f"{__doc__}\n{machines_help()}",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version",
                    version=f"opencutter {__version__} (build {BUILD}, api {API_VERSION})")
    sub = ap.add_subparsers(dest="action", required=True)
    serve = sub.add_parser("serve", help="hold the Bluetooth link and serve clients")
    serve.add_argument("--address", help="Bluetooth address (default: the first paired machine "
                                          "whose name starts with "
                                          f"{_join(list(ble_name_prefixes()), 'or')})")
    serve.add_argument("--force", action="store_true",
                       help="join a link that BlueZ already reports connected")
    serve.add_argument("-v", "--verbose", action="store_true", help="log every write and reply")
    serve.add_argument("--idle-exit", type=_seconds, default=0.0, metavar="SECONDS",
                       help="end after SECONDS without any request while no job runs, releasing "
                            "the machine (default 0: never; the app starts it with 900)")
    only = f"; {_join(_program_machines())} only"
    paused_only = f"; {_join(_pause_machines())} only"
    for name, text in (("status", "the machine, its model and its status (ESC 05)"),
                       ("info", "status, tools, sensor, firmware, machine profile and test "
                                "programs"),
                       ("stop", "stop the MACHINE now (ESC 09 twice); the background service "
                                "keeps running (see `service stop`)"),
                       ("pause", "pause the job that runs now (ESC 07), as the machine's Pause "
                                 f"key does{paused_only}"),
                       ("resume", "go on with the paused job that runs now (ESC 08), from where "
                                  f"it stopped{paused_only}"),
                       ("dry-run", f"motion test with the tool always up{only}"),
                       ("cut-test", f"cut one 1-inch square in adhesive vinyl{only}")):
        sub.add_parser(name, help=text)
    service = sub.add_parser("service", help="the background service: stop it, or show it")
    service_sub = service.add_subparsers(dest="service_action", required=True)
    service_sub.add_parser("stop", help="stop the background service (not the machine), which "
                                        "releases the Bluetooth link; refused while a job runs; "
                                        "exit status 1 if a service still runs")
    service_sub.add_parser("status", help="version, build, pid, socket, machine and model of "
                                          "the background service; exit status 1 if none runs")
    pen = sub.add_parser("pen-test", help=f"draw a 20 mm square with a Sketch Pen in Tool 1{only}")
    pen.add_argument("--pen-force", type=int, choices=[1, 7], default=1,
                     help="Force: 1 (lowest, default) or 7 (a usual sketch setting)")
    pen.add_argument("--pen-unrecognized", action="store_true",
                     help="accept Tool 1 reporting code 0 (pen seen in the holder by eye)")
    return ap


def _program_machines() -> list[str]:
    """The names of the machines that have test programs (the CAMEO 4)."""
    return [p.profile.name for p in PINNED_PROGRAMS.values()]


def _pause_machines() -> list[str]:
    """The names of the machines whose Pause and Resume OpenCutter sends (the CAMEO 4): a
    TESTED profile that has them."""
    return [p.name for p in registry.enabled() if p.support is Support.TESTED
            and p.pause is not None and p.resume is not None]


async def _run(args: argparse.Namespace) -> None:
    if args.action == "serve":
        from .device.daemon import serve      # imports bleak: only for the daemon
        try:
            await serve(args.address, args.verbose, args.force, args.idle_exit)
        except SystemExit as e:
            if not isinstance(e.code, str):
                raise
            # a refusal at start-up (another service holds the lock, ...): dated in the log,
            # with the refusal itself as the last line (the app reads it back without the time)
            stamp = time.strftime("%H:%M:%S")
            when = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            with contextlib.suppress(OSError, ValueError):
                print(f"{stamp} the background service did not start, {when}:", file=sys.stderr,
                      flush=True)
                print(f"{stamp} {e.code}", file=sys.stderr, flush=True)
            raise SystemExit(1) from None
        return
    if args.action == "stop":
        if not await stop_everywhere():
            raise SystemExit(1)
        return
    if args.action == "service":
        done = await (service_stop() if args.service_action == "stop" else service_status())
        if not done:
            raise SystemExit(1)
        return
    client = DaemonClient()
    try:
        if args.action in ("resume", "pause"):
            if not await command(client, args.action):
                raise SystemExit(1)
            return
        if args.action == "status":
            await show_status(client)
        elif args.action == "info":
            await show_info(client)
        elif args.action == "pen-test":
            await run_program(client, "pen-test", {"pen_force": args.pen_force,
                                                   "allow_unrecognized_pen": args.pen_unrecognized})
        else:
            await run_program(client, args.action, {})
    except DaemonError as e:
        raise SystemExit(f"daemon: {e}") from None


def main(argv: list[str] | None = None) -> int:
    """`opencutter` with no arguments starts the app; otherwise run a CLI subcommand."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .gui.app import main as gui_main
        try:
            return gui_main() or 0
        except KeyboardInterrupt:      # Ctrl+C in the terminal that started the app: the app
            return 130                 # has already quit (PyGObject re-raises it after that)
    args = _parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
