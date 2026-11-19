# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Run the real daemon against a simulated machine, to work on the app without a machine.

    .venv/bin/python tests/fake_daemon.py [--machine ID] [--tools " 2, 4"] [--u 6100,6100]
                                          [--move-seconds 0.6] [--fg TEXT]

It prints the socket path. Start the app against it in another terminal:

    OPENCUTTER_SOCKET=<that path> .venv/bin/python -m opencutter

bleak and dbus_fast are replaced by the stubs of tests/stubs.py, so nothing touches Bluetooth
or D-Bus, and the socket and the lock live in a private temporary directory. The simulated
machine accepts any job the daemon's validator lets through.

The machine is the tested CAMEO 4 (firmware V5.05) by default. `--machine cameo5` (any profile
id of opencutter.machines) simulates that model instead: its FG reply in the firmware's exact
form, an ESC 15 reply in its grammar with a usable tool in each holder, and the U reply each of
its mats is expected to give (where that is known). The daemon drives it as it would the real
one; for an UNTESTED model its ping says "untested" (with "machine": true). `--fg` sets the FG reply
itself: a model OpenCutter only recognizes (`--fg "CAMEO 3 V1.60    "`) or does not know
(`--fg "CAMEO 5 PRO V1.07"`) is refused by the daemon, and nothing more is sent to it.
Ctrl+C or SIGTERM ends it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "src")]   # when run from a checkout
import stubs  # noqa: E402
from opencutter.machines import MachineProfile, registry  # noqa: E402  (pure: no Bluetooth)


def tools_reply(profile: MachineProfile) -> str:
    """An ESC 15 reply in the profile's grammar: in each holder's slot the first usable blade
    tool (or the first usable tool), 0 in a slot no holder owns."""
    codes = [0, 0]
    for holder, slot in zip(profile.holders, profile.dialect.tool_slots):
        usable = [t for t in holder.tools if t.usable]
        blades = [t for t in usable if t.blade]
        codes[slot] = (blades or usable)[0].code if usable else 0
    return f" {codes[0]}, {codes[1]}"


def units_by_tg(profile: MachineProfile) -> dict[int, str]:
    """The U reply each mat of the profile is expected to give after its TG, where known."""
    return {m.tg: f"{m.expected_u[0]},{m.expected_u[1]}" for m in profile.mats
            if m.expected_u is not None}


def main() -> None:
    """Parse the options and serve until interrupted."""
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--machine", choices=sorted(p.id for p in registry.profiles()),
                    help="simulate this model (a profile id); default: the tested CAMEO 4")
    ap.add_argument("--tools", default=None,
                    help="the ESC 15 reply, e.g. ' 2, 4' = AutoBlade in Tool 1 (the CAMEO 4's "
                         "default)")
    ap.add_argument("--u", default=None,
                    help="the U reply after any TG (default: what the mat is expected to give, "
                         "6100,6100 on the CAMEO 4's 12x12 mat)")
    ap.add_argument("--move-seconds", type=float, default=stubs.FakeClient.move_s,
                    help="how long each motion write keeps the machine busy")
    ap.add_argument("--fg", default=None,
                    help="the FG reply (the model): the tested CAMEO 4's, or --machine's, by "
                         "default; a model OpenCutter does not drive is refused by the daemon")
    args = ap.parse_args()
    with pytest.MonkeyPatch.context() as mp:
        h = stubs.DaemonHarness(mp)
        mp.setattr(h.daemon, "_log", lambda msg: print(msg, flush=True))
        h.fake_cls.accept_any = True
        h.fake_cls.move_s = args.move_seconds
        if args.machine:
            profile = registry.enabled_profile(args.machine)
            h.simulate(profile, tools=tools_reply(profile), u_by_tg=units_by_tg(profile))
        if args.tools is not None:
            h.fake_cls.tools = args.tools
        if args.u is not None:
            h.fake_cls.u_reply = args.u
            h.fake_cls.u_by_tg = {}
        if args.fg is not None:
            h.fake_cls.fg_reply = args.fg

        async def until_stopped(h: stubs.DaemonHarness) -> None:
            print(h.socket, flush=True)
            await asyncio.wait({h.task})             # SIGTERM ends serve()
        try:
            h.run(until_stopped, timeout=None, until=lambda ping: ping.get("connected")
                  or ping.get("reason"))
        except KeyboardInterrupt:
            pass
        finally:
            h.close()


if __name__ == "__main__":
    main()
