# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""GUI logic for every machine profile, without a display: texts, machine state, mats, Send.

The CAMEO 4's texts and layout stay as they were; every other profile OpenCutter drives gets
its name, holders, mats and ranges from opencutter.machines. The app learns the connected
machine's profile from the machine ping ({"op": "ping", "machine": true}), asked only of a
service of this very build. A job is made for the layout's machine; when the service detects
another one, the layout follows it and Send waits for "Use {name}".

Nothing here touches Bluetooth: the service is FakeDaemonClient (tests/gui_fakes.py), or the
real daemon on the simulated machine of tests/stubs.py.
"""
import asyncio
import json
from dataclasses import replace

import pytest

import stubs
from stubs import harness, standard_fg  # noqa: F401

stubs.install()
from opencutter.device.protocol import (REASON_HELD_ELSEWHERE, REASON_NO_BLUETOOTH,  # noqa: E402
                                        REASON_NOT_PAIRED, REASON_UNREACHABLE,
                                        REASON_WRONG_MODEL)
from opencutter.gui import geometry, machine, runner, sending  # noqa: E402
from opencutter.gui.document import PlacedDocument  # noqa: E402
from opencutter.gui.machine import Link, MachineMonitor, MachineState  # noqa: E402
from opencutter.gui.runner import JobRunner, Outcome  # noqa: E402
from opencutter.gui.settings import GuiSettings  # noqa: E402
from opencutter.identity import identity  # noqa: E402
from opencutter.job.spec import JobSpec  # noqa: E402
from opencutter.machines import CAMEO4, Support, by_id, enabled, recognized_models  # noqa: E402
from opencutter.presets.model import (BASIC_PRESETS, PresetStore, basic_source,  # noqa: E402
                                      file_source)
from opencutter.svg.importer import load_svg  # noqa: E402
from gui_fakes import (CAMEO3_REASON, NO_MACHINE_FIELDS, OTHER_BUILD, READY_PING,  # noqa: E402
                       RECOGNIZED_FIELDS, RECOGNIZED_PING, UNITS_UNCHECKED, WRONG_MODEL_PING,
                       FakeDaemonClient)

CAMEO5 = by_id("cameo5")
CAMEO5_PLUS = by_id("cameo5_plus")
CAMEO4_PLUS = by_id("cameo4_plus")
PORTRAIT3 = by_id("portrait3")
PORTRAIT4 = by_id("portrait4")
DESIGN = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" viewBox="0 0 100 100"
  fill="none" stroke="#000"><rect x="20" y="30" width="40" height="20"/></svg>"""
VINYL = next(p for p in BASIC_PRESETS if p.action == "Cut")
PEN = next(p for p in BASIC_PRESETS if p.action == "Sketch")
LETTER = geometry.media_rect("letter", False, (0, 0))
WAITING = {"ok": True, "connected": False, "status": None, "motion": None, "device": None}


def connected(profile_id: str = "cameo4", support: str = "tested", tools=(2, 4),
              **kw) -> MachineState:
    return MachineState(link=Link.CONNECTED, status="0", tools=tools, profile=profile_id,
                        support=support, **kw)


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "design.svg"
    path.write_text(DESIGN)
    return PlacedDocument(load_svg(str(path)), LETTER)


# --- texts: the CAMEO 4's word for word, the others by name ---------------------------------------
NAMED_TEXTS = [
    (machine.another_program, "ANOTHER_PROGRAM", True),
    (machine.another_program_at_start, "ANOTHER_PROGRAM_AT_START", True),
    (machine.switch_off_on, "SWITCH_OFF_ON", True),
    (machine.not_paired, "NOT_PAIRED", False),
    (machine.another_version, "ANOTHER_VERSION", False),
    (machine.released, "RELEASED", False),
    (machine.disconnecting, "DISCONNECTING", False),
    (machine.not_disconnected_busy, "NOT_DISCONNECTED_BUSY", False),
]
# the constants as they read before machine profiles (tests/test_profile_characterization.py)
CAMEO4_WORDS = {
    "ANOTHER_PROGRAM": "Another program is using the CAMEO 4 over Bluetooth. Close it, then "
                       "switch the CAMEO 4 off and on.",
    "ANOTHER_PROGRAM_AT_START": "Another program is using the CAMEO 4 over Bluetooth. Close it, "
                                "switch the CAMEO 4 off and on, then press Try again.",
    "SWITCH_OFF_ON": "Switch the CAMEO 4 off and on, then press Try again.",
    "NOT_PAIRED": "No paired CAMEO 4 was found. Pair it in Settings → Bluetooth, then press Try "
                  "again.",
    "ANOTHER_VERSION": "Another OpenCutter version is using the CAMEO 4. Close it, then press Try "
                       "again.",
    "RELEASED": "The CAMEO 4 is free for other apps. Press Connect to use it here again.",
    "DISCONNECTING": "Releasing the CAMEO 4 for other apps…",
    "NOT_DISCONNECTED_BUSY": "Not disconnected: a job is running on the CAMEO 4. Press "
                             "Disconnect again when it has ended.",
}


@pytest.mark.parametrize("fn, const, power", NAMED_TEXTS, ids=[c for _, c, _ in NAMED_TEXTS])
def test_the_cameo4_texts_are_word_for_word_and_others_follow_the_name(fn, const, power):
    assert getattr(machine, const) == fn() == fn("CAMEO 4") == CAMEO4_WORDS[const]
    assert fn("CAMEO 5") == CAMEO4_WORDS[const].replace("CAMEO 4", "CAMEO 5")
    assert machine.MACHINE_NAME == "CAMEO 4" and "CAMEO 4" not in fn("Portrait 4")
    if power:
        # the off-and-on advice only for a machine that needs it (power_cycle_to_connect)
        assert fn("CAMEO 4", True) == CAMEO4_WORDS[const]
        quiet = fn("CAMEO 5", False)
        assert "off and on" not in quiet and "CAMEO 5" in quiet and "CAMEO 4" not in quiet


def test_texts_for_a_profile():
    assert machine.texts_for(CAMEO4) == {"name": "CAMEO 4", "power_cycle": True}
    assert machine.texts_for(CAMEO5) == {"name": "CAMEO 5", "power_cycle": True}
    assert machine.texts_for(PORTRAIT4) == {"name": "Portrait 4", "power_cycle": True}


def test_reason_and_startup_problems_name_the_machine():
    for reason, fn in ((REASON_HELD_ELSEWHERE, machine.another_program),
                       (REASON_UNREACHABLE, machine.switch_off_on)):
        assert machine.reason_problem(reason) == fn()
        assert machine.reason_problem(reason, name="CAMEO 5", power_cycle=False) == fn("CAMEO 5",
                                                                                       False)
    assert machine.reason_problem(REASON_NOT_PAIRED, name="Portrait 3") == machine.not_paired(
        "Portrait 3")
    assert machine.reason_problem(REASON_NO_BLUETOOTH, name="CAMEO 5") == machine.NO_BLUETOOTH
    assert machine.reason_problem("something new", name="CAMEO 5") is None
    # the refusal of a model does not follow the layout's machine
    assert machine.reason_problem(REASON_WRONG_MODEL, "X V1.00", name="CAMEO 5") == (
        machine.WRONG_MODEL.format(model="X V1.00"))
    # discovery still says "No paired CAMEO was found" (device/ble.py); every BLE prefix matches
    line = "No paired CAMEO was found. Pair it first in the system Bluetooth settings."
    assert machine.startup_problem(line) == machine.NOT_PAIRED
    assert machine.startup_problem(line, name="CAMEO 5", power_cycle=False) == (
        machine.not_paired("CAMEO 5"))
    assert machine.startup_problem("No paired Portrait was found.", name="Portrait 4") == (
        machine.not_paired("Portrait 4"))
    assert machine.startup_problem("the legacy prototype daemon holds cameo4.lock",
                                   name="CAMEO 5", power_cycle=False) == (
        machine.another_program_at_start("CAMEO 5", False))
    # the constant keeps every needle of before, in order, with the CAMEO 4's texts
    needles = [n for n, _ in machine.STARTUP_PROBLEMS]
    before = ["legacy prototype daemon", "already connected by another program",
              "another OpenCutter daemon is running", "an OpenCutter daemon is already running",
              "an older OpenCutter background service is running",
              "that was stopping did not end", "No paired CAMEO was found", ") not installed"]
    assert [n for n in needles if n in before] == before
    assert dict(machine.STARTUP_PROBLEMS)["No paired CAMEO was found"] == machine.NOT_PAIRED


def test_the_hint_for_an_unknown_machine_names_the_models_opencutter_drives():
    """Every model OpenCutter drives, by name and in registry order, with no word about which
    of them were tested; the CAMEO 4 with the firmware lines it is driven on."""
    hint = machine.OTHER_MACHINE_HINT
    assert hint == machine.other_machine_hint() == machine.drivable_hint()
    assert hint.startswith("OpenCutter drives these models: CAMEO 4 (firmware V1.xx or V5.xx), ")
    for p in enabled():
        assert p.name in hint
    for m in recognized_models():
        assert f" {m.name}," not in hint and f" {m.name}." not in hint
    assert "Try again" not in hint and "off and on" not in hint
    assert "tested" not in hint.lower() and "verified" not in hint and "works with" not in hint
    assert hint.endswith(machine.CONNECT_HINT_TAIL)
    names = machine.drivable_models()
    assert names[0] == "CAMEO 4 (firmware V1.xx or V5.xx)"
    assert names[1:] == [p.name for p in enabled()[1:]]     # one name each, no firmware lines


def test_a_recognized_machine_gets_its_name_and_plain_reason():
    state = MachineState(link=Link.FAILED, wrong_model=True, model="CAMEO 3 V1.60",
                         support="recognized", recognized="cameo3", refused_reason=CAMEO3_REASON)
    assert machine.other_machine_hint(state) == CAMEO3_REASON
    assert "CAMEO 3" in CAMEO3_REASON and "Try again" not in CAMEO3_REASON
    # without the reason: the registry's own words for that model
    assert machine.other_machine_hint(replace(state, refused_reason=None)) == (
        by_id("cameo3").message)
    # a known model on a firmware line OpenCutter does not know, without its reason
    line = replace(state, recognized="cameo4", refused_reason=None)
    assert machine.other_machine_hint(line) == machine.OTHER_MACHINE_HINT
    # an unknown model, and anything that is not a refusal
    assert machine.other_machine_hint(MachineState(link=Link.FAILED, wrong_model=True)) == (
        machine.OTHER_MACHINE_HINT)
    assert machine.other_machine_hint(connected()) == machine.OTHER_MACHINE_HINT
    assert machine.other_machine_hint(replace(state, wrong_model=False)) == (
        machine.OTHER_MACHINE_HINT)                          # only for a refused machine
    # the banner still says that nothing is sent, naming the model the machine reports
    assert machine.wrong_model_problem("CAMEO 3 V1.60") == machine.WRONG_MODEL.format(
        model="CAMEO 3 V1.60")


@pytest.mark.parametrize("model", ["CAMEO V1.10", "CAMEO V1.10    ", "Silhouette V1.00"])
def test_a_name_several_models_report_gets_its_own_reason(model):
    """'CAMEO V1.10' is what a CAMEO 1 reports, and a CAMEO Pro MK-II with some of its settings:
    the app says that it cannot tell which model it is, not just that it cannot drive it
    (while its hint names the CAMEO Pro MK-II among the models it drives)."""
    shown = model.strip()
    want = machine.SHARED_MODEL.format(model=shown)
    assert machine.wrong_model_problem(model) == want
    assert machine.reason_problem(REASON_WRONG_MODEL, model, name="CAMEO 5") == want
    assert "Try again" not in want and "off and on" not in want and "tested" not in want
    # a model no profile names gets the plain refusal
    assert machine.wrong_model_problem("CAMEO 5 PRO V1.07") == machine.WRONG_MODEL.format(
        model="CAMEO 5 PRO V1.07")

    async def scenario():
        client = FakeDaemonClient(ping={**WRONG_MODEL_PING, "model": shown})
        state = await MachineMonitor(client).refresh()
        assert state.wrong_model and state.problem == want and state.model == shown
    asyncio.run(scenario())


# --- the machine ping -------------------------------------------------------------------------
NONE = {"profile": None, "support": None, "refused_reason": None, "recognized": None}


@pytest.mark.parametrize("ping, fields", [
    ({"connected": True, "profile": "cameo5", "support": "untested"},
     {**NONE, "profile": "cameo5", "support": "untested"}),
    ({"connected": True, "profile": "cameo4", "support": "tested"},
     {**NONE, "profile": "cameo4", "support": "tested"}),
    # the CAMEO 4 on its untested firmware line
    ({"connected": True, "profile": "cameo4", "support": "untested"},
     {**NONE, "profile": "cameo4", "support": "untested"}),
    # never more than the profile's own level
    ({"connected": True, "profile": "cameo5", "support": "tested"},
     {**NONE, "profile": "cameo5", "support": "untested"}),
    # not a profile OpenCutter drives, or not a level a connected machine has
    ({"connected": True, "profile": "cameo3", "support": "untested"}, NONE),
    ({"connected": True, "profile": "cameo9", "support": "untested"}, NONE),
    ({"connected": True, "profile": "cameo5", "support": "recognized"}, NONE),
    ({"connected": True, "profile": "cameo5", "support": None}, NONE),
    ({"connected": "yes", "profile": "cameo5", "support": "untested"}, NONE),
    # a plain ping, or a service before machine profiles
    (READY_PING, NONE),
    # connected false is never a usable profile, whatever it names: an unknown firmware line of a
    # known model reports that model's id, recognized
    ({**WRONG_MODEL_PING, "profile": "cameo4", "support": "recognized",
      "refused_reason": "OpenCutter recognizes this CAMEO 4 but cannot drive it yet: its "
                        "firmware V9.99 is not a firmware line OpenCutter knows for it."},
     {**NONE, "support": "recognized", "recognized": "cameo4",
      "refused_reason": "OpenCutter recognizes this CAMEO 4 but cannot drive it yet: its "
                        "firmware V9.99 is not a firmware line OpenCutter knows for it."}),
    ({**RECOGNIZED_PING, **RECOGNIZED_FIELDS},
     {**NONE, "support": "recognized", "recognized": "cameo3", "refused_reason": CAMEO3_REASON}),
    ({**WAITING, "profile": "cameo5", "support": "untested"}, NONE),
    ({**WRONG_MODEL_PING, "profile": "cameo5", "support": "untested"}, NONE),
    ({**WRONG_MODEL_PING, **NO_MACHINE_FIELDS}, NONE),
    ({**WAITING, "reason": REASON_UNREACHABLE, "profile": "cameo3", "support": "recognized"},
     NONE),
    (None, NONE), ("pong", NONE), ([], NONE),
], ids=repr)
def test_machine_fields(ping, fields):
    assert machine.machine_fields(ping) == fields


def test_a_refused_reason_is_one_short_plain_line():
    ping = {**WRONG_MODEL_PING, "support": "recognized", "profile": "cameo3",
            "refused_reason": "a\x1b[31m\nb\tc  " + "x" * 400}
    reason = machine.machine_fields(ping)["refused_reason"]
    assert reason.startswith("a [31m b c x") and len(reason) == machine.REASON_CHARS
    assert reason.endswith("…") and all(c.isprintable() for c in reason)
    assert machine.machine_fields({**ping, "refused_reason": " \n "})["refused_reason"] is None
    assert machine.machine_fields({**ping, "refused_reason": 7})["refused_reason"] is None


def test_the_machine_ping_is_asked_only_of_this_build():
    async def scenario():
        client = FakeDaemonClient()
        monitor = MachineMonitor(client)
        state = await monitor.refresh()
        assert client.machine_pings == 1
        assert state.profile == "cameo4" and state.support == "tested" and state.tools == (2, 4)
        # another build's service: never the flag, whatever it runs
        client.identity = dict(OTHER_BUILD)
        client.manages_service = False
        await monitor.refresh()
        assert client.machine_pings == 1 and monitor.state.profile is None
        # an older build's service (no build at all): never the flag
        client.identity = {}
        await monitor.refresh()
        assert client.machine_pings == 1
        # this build again, but waiting for the machine: nothing to ask
        client.identity = identity()
        client.ping_reply = dict(WAITING)
        await monitor.refresh()
        assert client.machine_pings == 1 and monitor.state.profile is None
        # a refused machine: asked, for its reason
        client.ping_reply = dict(WRONG_MODEL_PING)
        await monitor.refresh()
        assert client.machine_pings == 2 and monitor.state.wrong_model
    asyncio.run(scenario())


def test_an_older_service_running_a_job_gets_no_machine_ping():
    async def scenario():
        client = FakeDaemonClient(ping=None)
        client.older_reply = {**READY_PING, "motion": "job"}
        monitor = MachineMonitor(client)
        await monitor.connect()
        assert monitor.state.blocker == machine.OTHER_VERSION_BUSY
        assert client.machine_pings == 0
    asyncio.run(scenario())


def test_a_machine_ping_that_fails_or_comes_from_elsewhere_leaves_the_plain_ping():
    class NoHelper(FakeDaemonClient):
        ping_machine = None                    # a client without the helper

    class Broken(FakeDaemonClient):
        async def ping_machine(self):
            raise ConnectionError("gone")

    class Elsewhere(FakeDaemonClient):
        async def ping_machine(self):
            return {**READY_PING, **OTHER_BUILD, "profile": "cameo5", "support": "untested"}

    class Stopping(FakeDaemonClient):
        async def ping_machine(self):
            return {**identity(), **READY_PING, "stopping": True, "profile": "cameo5",
                    "support": "untested"}

    class NotOk(FakeDaemonClient):
        async def ping_machine(self):
            return {**identity(), **READY_PING, "ok": False, "profile": "cameo5",
                    "support": "untested"}

    async def scenario(cls):
        monitor = MachineMonitor(cls())
        state = await monitor.refresh()
        assert state.connected and state.profile is None and state.support is None
        assert state.tools == (2, 4) and state.name == "CAMEO 4"    # read as before
    for cls in (NoHelper, Broken, Elsewhere, Stopping, NotOk):
        asyncio.run(scenario(cls))


def test_the_monitor_follows_the_profile_of_the_connected_machine():
    async def scenario():
        client = FakeDaemonClient()
        client.set_machine("cameo5", tools=" 2, 12")       # two-digit codes on the CAMEO 5
        monitor = MachineMonitor(client)
        state = await monitor.refresh()
        assert state.profile == "cameo5" and state.support == "untested"
        assert state.detected is CAMEO5 and state.name == "CAMEO 5"
        assert state.tools == (2, 12) and state.tool_text(1) == "AutoBlade (Detected)"
        assert state.firmware == "CAMEO 5 V1.07" and state.device_name == "CAMEO 5-TEST"
        # without its machine fields, the reply is read with the layout's machine: the CAMEO 4
        # grammar refuses two-digit codes (fail closed)
        client.machine_reply = dict(NO_MACHINE_FIELDS)
        await monitor.refresh()
        assert monitor.state.profile is None and monitor.state.tools is None
        # a one-holder model: one code, read from its own slot
        client.set_machine("portrait4", tools=" 7, 0")
        state = await monitor.refresh()
        assert state.tools == (7,) and state.holders == PORTRAIT4.holders
        assert state.holder_label(1) == "Tool" and state.tool_text(1) == "Sketch Pen (Detected)"
        assert state.tool_code(2) is None
        client.set_tools(" 7, 3")                          # its unused slot must read 0
        monitor_2 = MachineMonitor(client)
        assert (await monitor_2.refresh()).tools is None
        # the link drops: no profile, whatever the last one was
        client.ping_reply = {**client.ping_reply, "connected": False}
        state = await monitor.refresh()
        assert state.profile is None and state.support is None and state.detected is None
        assert state.name == "CAMEO 4"                     # the layout's machine
    asyncio.run(scenario())


def test_a_tool_reply_a_one_holder_model_cannot_read_is_kept_and_shown(doc):
    """On a one-holder model the unused ESC 15 slot must read 0: a well-formed reply with the
    tool there fails closed, and the app shows the raw reply instead of waiting for ever."""
    async def scenario():
        client = FakeDaemonClient()
        client.set_machine("portrait4", tools=" 0, 2")
        monitor = MachineMonitor(client)
        state = await monitor.refresh()
        assert state.tools is None and state.tools_reply == " 0, 2"
        assert state.tool_code(1) is None and state.tool_text(1) == "Unclear reply “0, 2”"
        assert state.tool_text(2) is None                  # no such holder on a Portrait 4
        problem = ("The Portrait 4 reported its tool as “0, 2”, which OpenCutter cannot read for "
                   "this model, so nothing is sent. Please report this reply.")
        assert state.tools_problem == problem
        settings = sending.starting_settings(PORTRAIT4, 1)
        layout = sending.LayoutMachine("portrait4")
        check = sending.tool_check(settings, state)
        assert sending.blocker_for_test_cut(state, check, False, layout=layout) == problem
        assert sending.send_blocker(doc, LETTER, state, check, False, layout=layout) == problem
        # a garbled read is not a reply: what was read last stays (nothing read yet here)
        client.set_tools("0")
        state = await monitor.refresh()
        assert state.tools is None and state.tools_reply == " 0, 2"
        client.set_tools(" 2, 0")                          # the tool in its own slot
        state = await monitor.refresh()
        assert state.tools == (2,) and state.tools_reply is None and state.tools_problem is None
        client.set_tools("0")
        assert (await monitor.refresh()).tools == (2,)     # as before: kept
        # the link drops: nothing of it is kept
        client.ping_reply = {**client.ping_reply, "connected": False}
        state = await monitor.refresh()
        assert state.tools is None and state.tools_reply is None
    asyncio.run(scenario())


def test_the_cameo4_never_has_an_unreadable_tool_reply():
    """Two holders own both slots: a reply in the CAMEO 4's form is always read, and one that
    is not keeps the last tools, as before."""
    async def scenario():
        client = FakeDaemonClient()
        monitor = MachineMonitor(client)
        assert (await monitor.refresh()).tools == (2, 4)
        for garbled in (" 10, 2", "0", ""):
            client.set_tools(garbled)
            state = await monitor.refresh()
            assert state.tools == (2, 4) and state.tools_reply is None, garbled
    asyncio.run(scenario())


def test_a_recognized_machine_in_the_monitor():
    async def scenario():
        client = FakeDaemonClient(ping=RECOGNIZED_PING)
        client.machine_reply = dict(RECOGNIZED_FIELDS)
        monitor = MachineMonitor(client)
        state = await monitor.refresh()
        assert state.link is Link.FAILED and state.wrong_model and not state.connected
        assert state.profile is None and state.detected is None
        assert state.support == "recognized" and state.recognized == "cameo3"
        assert state.refused_reason == CAMEO3_REASON
        assert machine.other_machine_hint(state) == CAMEO3_REASON
        assert state.problem == machine.wrong_model_problem("CAMEO 3 V1.60")
        # a known model on an unknown firmware line: recognized, still never its profile
        client.ping_reply = {**WRONG_MODEL_PING, "model": "CAMEO 4 V9.99"}
        client.machine_reply = {"profile": "cameo4", "support": "recognized",
                                "refused_reason": "OpenCutter recognizes this CAMEO 4 but "
                                                  "cannot drive it yet: its firmware V9.99 is "
                                                  "not a firmware line OpenCutter knows for it."}
        state = await monitor.refresh()
        assert state.profile is None and state.recognized == "cameo4"
        assert sending.LayoutMachine("cameo5").follow(state) is False
        assert "V9.99" in machine.other_machine_hint(state)
        # it goes away: the fields go with it
        client.ping_reply, client.machine_reply = dict(WAITING), None
        state = await monitor.refresh()
        assert (state.support, state.refused_reason, state.recognized) == (None, None, None)
    asyncio.run(scenario())


def test_the_monitor_never_keeps_machine_fields_of_a_machine_that_is_not_connected():
    """Every state the monitor publishes drops the profile unless connected, and the refusal
    fields unless the machine was refused for its model."""
    monitor = MachineMonitor(FakeDaemonClient())
    fields = {"profile": "cameo5", "support": "untested", "refused_reason": "why",
              "recognized": "cameo3"}
    monitor._set(MachineState(link=Link.CONNECTING, **fields))
    assert (monitor.state.profile, monitor.state.support, monitor.state.refused_reason,
            monitor.state.recognized) == (None, None, None, None)
    monitor._set(MachineState(link=Link.FAILED, wrong_model=True, **fields))
    assert monitor.state.profile is None and monitor.state.detected is None
    assert (monitor.state.support, monitor.state.refused_reason, monitor.state.recognized) == (
        "untested", "why", "cameo3")
    monitor._set(MachineState(link=Link.CONNECTED, **fields))
    assert monitor.state.profile == "cameo5"


def test_the_texts_of_the_monitor_follow_the_layouts_machine():
    async def scenario():
        client = FakeDaemonClient(ping={**WAITING, "reason": REASON_HELD_ELSEWHERE})
        monitor = MachineMonitor(client)
        seen = []
        monitor.add_listener(seen.append)
        await monitor.refresh()
        assert monitor.state.problem == machine.ANOTHER_PROGRAM
        monitor.set_choice("cameo5")
        assert seen[-1].choice == "cameo5" and seen[-1].name == "CAMEO 5"
        await monitor.refresh()
        assert monitor.state.problem == machine.another_program("CAMEO 5", True)
        assert "connects to it by itself" not in monitor.state.problem    # not seen on it
        monitor.set_choice("cameo3")                        # recognized: never a layout
        assert monitor.choice == "cameo4" and monitor.state.name == "CAMEO 4"
        monitor.set_choice("portrait4")
        client.ping_reply = {**WAITING, "reason": REASON_UNREACHABLE}
        await monitor.refresh()
        assert monitor.state.problem == machine.switch_off_on("Portrait 4", True)
        # Disconnect names the machine it releases (the connected one)
        client.set_machine("cameo5", tools=" 2, 0")
        await monitor.refresh()
        assert monitor.state.name == "CAMEO 5"
        assert await monitor.disconnect() is None
        assert monitor.state.note == machine.released("CAMEO 5") and monitor.state.released
        await monitor.refresh()                              # nothing answers now
        assert monitor.state.released and monitor.state.note == machine.released("CAMEO 5")
    asyncio.run(scenario())


def test_the_cameo4_disconnect_texts_are_as_before():
    async def scenario():
        client = FakeDaemonClient()
        monitor = MachineMonitor(client)
        await monitor.refresh()
        client.shutdown_reply = {"ok": False, "busy": True, "error": "a job runs"}
        assert await monitor.disconnect() == machine.NOT_DISCONNECTED_BUSY
        client.shutdown_reply = None
        assert await monitor.disconnect() is None
        assert monitor.state.note == machine.RELEASED and monitor.state.released
        assert monitor.state == MachineState(note=machine.RELEASED, released=True)
    asyncio.run(scenario())


# --- MachineState with N holders ----------------------------------------------------------------
def test_machine_state_holders_and_tools():
    cameo4 = MachineState(link=Link.CONNECTED, status="0", tools=(2, 4))   # no machine fields
    assert cameo4.name == "CAMEO 4" and cameo4.detected is None
    assert cameo4.machine_profile is CAMEO4 and [h.label for h in cameo4.holders] == [
        "Tool 1", "Tool 2"]
    assert cameo4.tool_text(1) == "AutoBlade (Detected)"
    assert cameo4.tool_text(2) == "Kraft Blade, 3mm (Detected)"
    assert cameo4.tool_code(3) is None and cameo4.holder_label(3) == "Tool 3"
    assert cameo4.tool_code(0) is None and cameo4.tool_code(-1) is None   # by holder number
    p4 = connected("portrait4", "untested", tools=(0,))
    assert p4.detected is PORTRAIT4 and len(p4.holders) == 1
    assert p4.tool_text(1) == "No Tool Detected" and p4.tool_code(2) is None
    assert p4.status_text == "Ready" and p4.ready
    short = connected("cameo5", "untested", tools=(2,))     # fewer codes than holders
    assert short.tool_code(2) is None
    # offline: the layout's machine names everything, nothing is reported
    offline = MachineState(choice="portrait3")
    assert offline.name == "Portrait 3" and offline.holder_label(1) == "Tool"
    assert offline.tool_code(1) is None and offline.detected is None
    assert MachineState(choice="cameo3").name == "CAMEO 4"     # recognized: never a layout
    assert MachineState(choice="nope").machine_profile is CAMEO4
    # a profile is the connected machine's only: a machine that is not connected has none
    assert MachineState(link=Link.FAILED, profile="cameo5").detected is None


def test_the_app_has_no_note_about_testing():
    """The app does not tell the models it drives apart by whether they were tested: no note,
    no label, no helper for it (the support level stays in MachineState and the registry)."""
    for name in ("untested_note", "UNTESTED_NOTE", "UNTESTED_FIRMWARE_NOTE", "NOT_YET_TESTED"):
        assert not hasattr(machine, name), name
    assert connected("cameo5", "untested").support == "untested"
    assert connected("cameo4", "untested").support == "untested"


# --- offline machine choices ------------------------------------------------------------------
def test_offline_machine_choices():
    choices = machine.machine_choices()
    assert [c.id for c in choices] == [p.id for p in enabled()]
    assert choices[0] == machine.MachineChoice("cameo4", "CAMEO 4")
    assert [c.name for c in choices] == [p.name for p in enabled()]     # plain names only
    assert not [c.name for c in choices if "tested" in c.name.lower()]
    ids = {c.id for c in choices}
    assert not ids & {m.id for m in recognized_models()}
    assert {"cameo5", "portrait3", "portrait4", "cameo4_pro"} <= ids
    assert all(by_id(c.id).support is not Support.RECOGNIZED for c in choices)
    assert machine.chosen_profile("cameo3") is CAMEO4
    assert machine.chosen_profile(None) is CAMEO4
    assert machine.chosen_profile("portrait4") is PORTRAIT4


# --- geometry: mats from the profile ------------------------------------------------------------
def test_the_cameo4_geometry_is_as_before():
    assert geometry.MAT_KEY == "12x12" and geometry.mat_label() == geometry.MAT_LABEL
    assert geometry.MAT_LABEL == "CAMEO 12 × 12 in"
    assert geometry.mat_size() == (geometry.MAT_SIZE_MM, geometry.MAT_SIZE_MM)
    assert geometry.media_sizes() == geometry.MEDIA_SIZES
    assert geometry.cut_area() == geometry.cut_area("12x12", CAMEO4) == (
        1.5, 1.5, pytest.approx(301.8), pytest.approx(301.8))
    assert geometry.media_rect("custom", False, (500, 100)) == (0.0, 0.0, geometry.MAT_SIZE_MM,
                                                               100)
    assert geometry.mat_choices() == (geometry.MatChoice("12x12", "CAMEO 12 × 12 in", True),)


def test_non_square_mats_clamp_the_media():
    # CAMEO 5, 12 x 24 in: 12 in across (x), 24 in along the feed (y)
    w, h = geometry.mat_size("12x24", CAMEO5)
    assert (w, h) == (12 * geometry.MM_PER_IN, 24 * geometry.MM_PER_IN)
    assert geometry.media_rect("custom", False, (500, 700), "12x24", CAMEO5) == (0.0, 0.0, w, h)
    assert geometry.media_rect("custom", True, (500, 400), "12x24", CAMEO5) == (0.0, 0.0, w, 500)
    assert geometry.media_rect("12x24", False, (0, 0), "12x24", CAMEO5) == (0.0, 0.0, w, h)
    assert geometry.cut_area("12x24", CAMEO5) == (1.5, 1.5, pytest.approx(301.8),
                                                  pytest.approx(606.6))
    tall = (20.0, 400.0, 50.0, 50.0)
    media = (0.0, 0.0, w, h)
    assert geometry.design_fits(tall, media, "12x24", CAMEO5)
    assert not geometry.design_fits(tall, media)                 # the CAMEO 4's 12 x 12 in
    assert geometry.usable_area(media, "12x24", CAMEO5)[3] == pytest.approx(606.6)
    # Portrait 3, 8 x 12 in: Letter is wider than the mat
    assert geometry.mat_size("8x12", PORTRAIT3) == (203.2, 12 * geometry.MM_PER_IN)
    assert geometry.media_rect("letter", False, (0, 0), "8x12", PORTRAIT3) == (
        0.0, 0.0, 203.2, pytest.approx(279.4))
    assert geometry.media_rect("letter", True, (0, 0), "8x12", PORTRAIT3) == (
        0.0, 0.0, 203.2, pytest.approx(215.9))
    assert geometry.media_rect("12x12", False, (0, 0), "8x12", PORTRAIT3)[2:] == (
        203.2, pytest.approx(304.8))
    a = geometry.cut_area("8x12", PORTRAIT3)
    assert a[2] < a[3] and a[0] + a[2] < 203.2
    assert geometry.test_cut_spot((0.0, 0.0, 203.2, 279.4), "8x12", PORTRAIT3) == (
        pytest.approx(a[0] + geometry.TEST_SPOT_INSET_MM),
        pytest.approx(a[1] + geometry.TEST_SPOT_INSET_MM))


def test_mat_labels_media_sizes_and_choices():
    assert geometry.mat_label("12x24", CAMEO5) == "CAMEO 12 × 24 in"
    assert geometry.mat_label("8x12", PORTRAIT3) == "Portrait 8 × 12 in"
    assert geometry.mat_label(None, CAMEO5_PLUS) == "CAMEO 15 × 15 in"     # its default mat
    assert [m.key for m in geometry.media_sizes("12x24", CAMEO5)] == ["letter", "a4", "12x24"]
    assert geometry.media_size("12x24") == geometry.MediaSize(
        "12x24", "12 × 24 in", 12 * geometry.MM_PER_IN, 24 * geometry.MM_PER_IN)
    assert geometry.media_size("custom") is None and geometry.media_size("12x12") == (
        geometry.MEDIA_SIZES[2])
    # every mat of the CAMEO 5 is offered on it; the Portrait's U reply is not known, so its
    # mat says that the cut area cannot be checked
    assert [(c.key, c.sensitive, c.note) for c in geometry.mat_choices(CAMEO5)] == [
        ("12x12", True, None), ("12x24", True, None)]
    assert geometry.mat_choices(PORTRAIT3) == (
        geometry.MatChoice("8x12", "Portrait 8 × 12 in", True, geometry.UNCHECKED_MAT),)
    # at a tested level, a mat whose U reply is not known cannot be chosen
    assert geometry.mat_choices(PORTRAIT3, "tested") == (
        geometry.MatChoice("8x12", "Portrait 8 × 12 in", False, geometry.NOT_MEASURED),)
    assert geometry.mat_choices(PORTRAIT3, Support.TESTED)[0].note == (
        "Not measured on this machine yet")
    assert not any(c.sensitive for c in geometry.mat_choices(CAMEO5, "recognized"))
    assert not any(c.sensitive for c in geometry.mat_choices(CAMEO5, "nonsense"))
    with pytest.raises(ValueError, match="no mat"):
        geometry.cut_area("12x24")                           # not a CAMEO 4 mat


# --- settings ---------------------------------------------------------------------------------
def test_settings_without_a_machine_load_as_before(tmp_path):
    path = tmp_path / "gui.json"
    old = {"units": "mm", "media": "a4", "landscape": True, "custom_media_mm": [100.0, 120.0],
           "preview_outline": False, "reveal_mat": False, "first_run_done": True,
           "material": "Vinyl", "recent_materials": ["Vinyl"]}
    path.write_text(json.dumps(old))
    settings = GuiSettings.load(path)
    assert settings.machine == "cameo4" and settings.mats == {}
    assert settings.profile is CAMEO4 and settings.mat_for() == "12x12"
    assert (settings.units, settings.media, settings.material) == ("mm", "a4", "Vinyl")
    settings.save()
    saved = json.loads(path.read_text())
    assert {k: saved[k] for k in old} == old                 # what an older build reads
    assert saved["machine"] == "cameo4" and saved["mats"] == {}
    assert GuiSettings.load(path) == settings


@pytest.mark.parametrize("value", ["cameo3", "curio2", "cameo9", "", 5, None, ["cameo5"]])
def test_a_machine_opencutter_does_not_drive_reads_as_the_cameo4(tmp_path, value):
    path = tmp_path / "gui.json"
    path.write_text(json.dumps({"machine": value, "mats": {"cameo4": "12x12"}}))
    settings = GuiSettings.load(path)
    assert settings.machine == "cameo4" and settings.mats == {"cameo4": "12x12"}


def test_mats_per_machine(tmp_path):
    path = tmp_path / "gui.json"
    path.write_text(json.dumps({"machine": "cameo5", "mats": {
        "cameo5": "12x24", "cameo5_plus": "99x99", "cameo3": "12x12", "nope": "12x12",
        "portrait3": 7, "cameo4": "12x24"}}))
    settings = GuiSettings.load(path)
    assert settings.machine == "cameo5" and settings.profile is CAMEO5
    assert settings.mats == {"cameo5": "12x24", "cameo5_plus": "15x15", "cameo4": "12x12"}
    assert settings.mat_for() == "12x24" and settings.mat_for(PORTRAIT3) == "8x12"
    settings.set_mat("12x12")
    assert settings.mat_for(CAMEO5) == "12x12"
    settings.set_mat("8x12", CAMEO5)                         # not a CAMEO 5 mat
    assert settings.mat_for(CAMEO5) == CAMEO5.default_mat
    settings.mats["cameo5"] = "8x12"                         # set behind its back
    assert settings.mat_for(CAMEO5) == CAMEO5.default_mat
    settings.set_mat("12x12", CAMEO5)
    settings.set_machine("cameo3")
    assert settings.machine == "cameo4"
    settings.set_machine("portrait4")
    settings.save()
    again = GuiSettings.load(path)
    assert again.machine == "portrait4" and again.mats == settings.mats
    path.write_text(json.dumps({"mats": ["cameo5", "12x24"]}))
    assert GuiSettings.load(path).mats == {}


# --- presets: the machine's own rows ------------------------------------------------------------
def _store_with_other_machines() -> PresetStore:
    felt = replace(VINYL, material="Felt", machines=("cameo5",), verified=False)
    vinyl5 = replace(VINYL, force=30, machines=("cameo5",), verified=False)
    both = replace(PEN, material="Card", machines=("cameo4", "cameo5"))
    return PresetStore([vinyl5, felt, VINYL, PEN, both], file_source("Shared presets"))


def test_the_gui_offers_each_machine_only_its_rows():
    store = _store_with_other_machines()
    cameo4 = sending.supported_presets(store)
    assert cameo4.materials() == ["Card", "Sketch Pen", "Vinyl"]
    assert all("cameo4" in p.machines for p in cameo4.presets)
    # the CAMEO 5 row comes first in the file, but the CAMEO 4 never gets it
    assert sending.choose_preset(cameo4, "Vinyl") == VINYL
    cameo5 = sending.supported_presets(store, CAMEO5)
    assert cameo5.materials() == ["Card", "Felt", "Vinyl"]
    assert sending.choose_preset(cameo5, "Vinyl").force == 30
    assert cameo4.source is store.source and cameo5.source is store.source
    # a machine without rows: no materials, and the hint points to Cut Settings
    portrait = sending.supported_presets(store, PORTRAIT3)
    assert portrait.presets == [] and sending.materials_note(portrait) == sending.NO_MATERIALS
    assert sending.NO_MATERIALS == "No materials for this machine yet"
    assert "Cut Settings" in sending.NO_MATERIALS_HINT
    assert sending.materials_note(cameo4) is None


def test_the_basic_set_and_a_schema_1_file_per_machine(tmp_path):
    basic = PresetStore(list(BASIC_PRESETS), basic_source())
    assert sending.supported_presets(basic).presets == BASIC_PRESETS
    plus = sending.supported_presets(basic, CAMEO4_PLUS)
    assert [p.material for p in plus.presets] == [p.material for p in BASIC_PRESETS]
    assert all(p.machines == ("cameo4_plus",) for p in plus.presets)
    assert sending.verified_preset(plus) is None             # nothing was cut on a Plus
    assert sending.supported_presets(basic, CAMEO5).presets == []
    # a schema 1 file holds CAMEO 4 rows only: a 4 Plus sees no materials from it
    path = tmp_path / "presets.json"
    path.write_text(json.dumps({"format": "opencutter-presets", "schema": 1,
                                "presets": [VINYL.to_dict()]}))
    store = PresetStore.load(path)
    assert sending.supported_presets(store).presets == [VINYL]
    assert sending.materials_note(sending.supported_presets(store, CAMEO4_PLUS)) == (
        sending.NO_MATERIALS)


def test_rotary_and_punch_rows_are_not_offered_on_other_machines():
    rotary = replace(VINYL, material="Felt", action="Rotary Cut", holder=2, tool="Rotary Blade",
                     tool_code=5, blade_depth=None, machines=("cameo5",))
    punch = replace(PEN, material="Felt", action="Punch", holder=2, tool="Punch Tool",
                    tool_code=6, machines=("cameo5",))
    store = PresetStore([rotary, punch], file_source())
    assert sending.supported_presets(store, CAMEO5).presets == []
    assert sending.UNSUPPORTED_TOOL_CODES == frozenset({5, 6})


# --- tool checks, cards and Cut Settings ranges ---------------------------------------------------
def test_tool_checks_per_holder_count(doc):
    s = VINYL.to_tool_settings()
    p4 = connected("portrait4", "untested", tools=(7,))
    check = sending.tool_check(s, p4)
    assert check.profile is PORTRAIT4 and check.exists and check.label == "Tool"
    assert check.required_name == "AutoBlade" and check.known and not check.matches
    assert check.mismatch_text == "Load the AutoBlade into the tool holder."
    layout = sending.LayoutMachine("portrait4")
    assert sending.blocker_for_test_cut(p4, check, False, layout=layout) == (
        "Load the AutoBlade into the tool holder to send.")
    second = sending.tool_check(replace(s, holder=2, blade_depth=None), p4)
    assert not second.exists and not second.known
    assert sending.blocker_for_test_cut(p4, second, False, layout=layout) == (
        "The Portrait 4 has no Tool 2.")
    # the CAMEO 4 reads as before
    cameo4 = sending.tool_check(s, connected(tools=(7, 4)))
    assert cameo4 == sending.ToolCheck(1, 2, 7)
    assert cameo4.mismatch_text == "Load the AutoBlade into Tool 1."
    assert sending.tool_check(s, MachineState()).required_name == "AutoBlade"


def test_tool_cards_and_slider_ranges():
    cards = sending.tool_cards()
    assert [(c.holder, c.label) for c in cards] == [(1, "Tool 1"), (2, "Tool 2")]
    codes = [code for code, _ in cards[0].tools]
    assert 0 not in codes and 5 not in codes and 6 not in codes and 2 in codes
    assert (8, "Foil Tool") in cards[1].tools and (8, "Foil Tool") not in cards[0].tools
    assert [(c.holder, c.label) for c in sending.tool_cards(PORTRAIT3)] == [(1, "Tool")]
    # the CAMEO 4's sliders: as the Send panel has them today
    assert sending.slider_ranges(CAMEO4, 1, 2) == {
        "blade_depth": (0, 10), "force": (1, 33), "speed": (1, 30), "acceleration": (1, 10),
        "passes": (1, 10), "offset": (0, 99), "overcut": (0, 9)}
    assert sending.slider_ranges() == sending.slider_ranges(CAMEO4, 1, 2)
    pen = sending.slider_ranges(CAMEO4, 1, 7)
    assert pen["blade_depth"] is None and pen["offset"] is None and pen["overcut"] is None
    assert sending.slider_ranges(CAMEO4, 2, 2)["blade_depth"] is None   # no depth in Tool 2
    p3 = sending.slider_ranges(PORTRAIT3, 1, 2)
    assert p3["acceleration"] is None and p3["speed"] == (1, 10) and p3["offset"] == (0, 45)
    assert sending.slider_ranges(PORTRAIT4, 1, 2)["acceleration"] == (1, 3)
    assert sending.slider_ranges(CAMEO5, 1, 2)["force"] == (1, 40)


def test_starting_settings_for_a_machine_without_materials():
    for profile in enabled():
        s = sending.starting_settings(profile)
        media = geometry.media_rect("letter", False, (0, 0), None, profile)   # its default mat
        spec = sending.spec_for_test_cut(media, s, profile.id)
        summary, error = sending.plan_summary(spec)
        assert error is None, (profile.id, error)
        assert summary["machine"] == profile.id and s.force == profile.ranges.force.lo
    p3 = sending.starting_settings(PORTRAIT3)
    assert p3.tool_code == 2 and p3.blade_depth == 1 and p3.speed == 5 and p3.offset == 18
    second = sending.starting_settings(CAMEO5, holder=2)
    assert second.holder == 2 and second.blade_depth is None and second.tool_code == 1
    pen = sending.starting_settings(CAMEO4, 1, 7)
    assert pen.offset == 0 and pen.overcut == (0, 0) and pen.blade_depth is None
    with pytest.raises(ValueError):
        sending.starting_settings(PORTRAIT3, holder=2)
    with pytest.raises(ValueError):
        sending.starting_settings(CAMEO4, 1, 5)                  # the Rotary Blade


# --- the JobSpec names the machine and its mat ----------------------------------------------------
def test_job_spec_carries_the_machine_and_the_mat(doc):
    s = VINYL.to_tool_settings()
    spec = sending.job_spec(doc, LETTER, s, True)
    assert spec.machine == "cameo4" and spec.mat == "12x12"
    assert spec.to_dict()["machine"] == "cameo4"
    media = geometry.media_rect("letter", False, (0, 0), "12x24", CAMEO5)
    spec = sending.job_spec(doc, media, s, True, machine="cameo5", mat="12x24")
    assert (spec.machine, spec.mat) == ("cameo5", "12x24")
    assert JobSpec.from_dict(spec.to_dict()) == spec
    summary, error = sending.plan_summary(spec)
    assert error is None and summary["machine"] == "cameo5" and summary["mat"] == "12x24"
    # the machine's default mat when none is given
    assert sending.job_spec(doc, LETTER, s, True, machine="cameo5_plus").mat == "15x15"
    narrow = geometry.media_rect("letter", False, (0, 0), "8x12", PORTRAIT3)
    test = sending.spec_for_test_cut(narrow, s, "portrait3")
    assert (test.machine, test.mat) == ("portrait3", "8x12")
    summary, error = sending.plan_summary(test)
    assert error is None and summary["units_enforced"] is False
    # a mat the machine does not have: refused before anything is sent
    summary, error = sending.plan_summary(sending.job_spec(doc, LETTER, s, True,
                                                           machine="cameo5", mat="8x12"))
    assert summary is None and "mat" in error


# --- Send waits for "Use {name}" ------------------------------------------------------------------
def test_send_is_blocked_on_another_machine_until_use(doc):
    s = VINYL.to_tool_settings()
    layout = sending.LayoutMachine()                         # the CAMEO 4, as saved
    state = connected("cameo5", "untested", tools=(2, 0))
    check = sending.tool_check(s, state, layout.profile)
    confirm = "Check the layout for the CAMEO 5, then press Use CAMEO 5."
    other = "The layout was made for the CAMEO 4, but the connected machine is a CAMEO 5."
    # not followed yet: the layout's machine differs from the detected one
    assert layout.blocker(state) == other
    assert sending.send_blocker(doc, LETTER, state, check, False, layout=layout) == other
    assert sending.blocker_for_test_cut(state, check, False, layout=layout) == other
    # the detected machine wins: the layout switches to it, Send still waits
    assert layout.follow(state) is True
    assert (layout.machine, layout.previous, layout.pending) == ("cameo5", "cameo4", "cameo5")
    assert layout.profile is CAMEO5 and layout.button == "Use CAMEO 5"
    assert layout.banner == (
        "The connected machine is a CAMEO 5. The layout was made for the CAMEO 4, so it now uses "
        "the CAMEO 5’s cutting mat; your design stays where it is. Check it, then press Use "
        "CAMEO 5.")
    assert layout.follow(state) is False                      # once
    check = sending.tool_check(s, state, layout.profile)
    assert sending.send_blocker(doc, LETTER, state, check, False, layout=layout) == confirm
    # what does not depend on the machine still comes first
    assert sending.send_blocker(None, LETTER, state, check, False, layout=layout) == (
        "Open a design first.")
    assert sending.send_blocker(doc, LETTER, state, check, False, False, layout=layout) == (
        sending.CHOOSE_MATERIAL)
    layout.use()
    assert layout.button is None and layout.banner is None and layout.blocker(state) is None
    assert sending.send_blocker(doc, LETTER, state, check, False, layout=layout,
                                mat="12x24") is None
    spec = sending.job_spec(doc, LETTER, s, True, machine=layout.machine)
    assert spec.machine == "cameo5"
    # a CAMEO 4 connects next: the layout follows it again, and waits again
    cameo4 = connected()
    assert layout.follow(cameo4) and layout.blocker(cameo4) == (
        "Check the layout for the CAMEO 4, then press Use CAMEO 4.")
    # the user's own offline choice needs no Use; a machine of that profile is not blocked
    layout.choose("cameo5")
    assert layout.pending is None and layout.blocker(state) is None
    # no profile reported: nothing to follow (the service still refuses a mismatch)
    unknown = MachineState(link=Link.CONNECTED, status="0", tools=(2, 4))
    assert not layout.follow(unknown) and layout.blocker(unknown) is None
    assert sending.LayoutMachine("cameo3").machine == "cameo4"


def test_without_a_layout_send_is_for_the_cameo4_only(doc):
    """A window that passes no layout makes its jobs for the CAMEO 4 (job_spec's default): a
    connected machine of another profile blocks Send and Test Cut, the CAMEO 4 reads as before."""
    s = VINYL.to_tool_settings()
    cameo5 = connected("cameo5", "untested", tools=(2, 0))
    other = "The layout was made for the CAMEO 4, but the connected machine is a CAMEO 5."
    check = sending.tool_check(s, cameo5, CAMEO4)
    assert sending.send_blocker(doc, LETTER, cameo5, check, False) == other
    assert sending.blocker_for_test_cut(cameo5, check, False) == other
    assert sending.job_spec(doc, LETTER, s, True).machine == "cameo4"
    cameo4 = connected()
    check = sending.tool_check(s, cameo4)
    assert sending.send_blocker(doc, LETTER, cameo4, check, False) is None
    assert sending.blocker_for_test_cut(cameo4, check, False) is None
    # a service that gave no machine fields: nothing to compare (the service is the authority)
    plain = MachineState(link=Link.CONNECTED, status="0", tools=(2, 4))
    assert sending.send_blocker(doc, LETTER, plain, sending.tool_check(s, plain), False) is None


def test_a_cutting_mat_the_machine_cannot_use_blocks_send(doc):
    """A mat key the layout's machine does not have (a stale one after a switch) or does not
    offer blocks Send with a plain text instead of failing."""
    s = VINYL.to_tool_settings()
    layout = sending.LayoutMachine("portrait3")
    state = connected("portrait3", "untested", tools=(2,))
    check = sending.tool_check(s, state, layout.profile)
    choose = "Choose a Cutting Mat for the Portrait 3 in Page Setup."
    assert sending.send_blocker(doc, LETTER, state, check, False, layout=layout,
                                mat="12x24") == choose
    assert sending.blocker_for_test_cut(state, check, False, layout=layout, mat="12x24") == choose
    assert sending.send_blocker(doc, LETTER, state, check, False, layout=layout,
                                mat="8x12") is None
    assert sending.send_blocker(doc, LETTER, state, check, False, layout=layout) is None
    # a mat whose U reply is not known is not offered on a tested model (the job validator's
    # rule), even on its untested firmware line
    unknown_u = PORTRAIT3.mat("8x12")
    tested = replace(CAMEO4, mats=(*CAMEO4.mats, unknown_u))
    assert sending.mat_offered(tested, "12x12") and not sending.mat_offered(tested, "8x12")
    assert not sending.mat_offered(tested, "nope") and not sending.mat_offered(tested, None)
    assert [(c.key, c.sensitive) for c in geometry.mat_choices(tested, "untested")] == [
        ("12x12", True), ("8x12", False)]
    assert geometry.mat_choices(tested, "untested")[1].note == geometry.NOT_MEASURED


def test_the_cut_area_of_the_layouts_mat_decides_whether_the_design_fits(doc):
    s = VINYL.to_tool_settings()
    layout = sending.LayoutMachine("cameo5")
    state = connected("cameo5", "untested", tools=(2, 0))
    check = sending.tool_check(s, state, layout.profile)
    media = geometry.media_rect("12x24", False, (0, 0), "12x24", CAMEO5)
    doc.move_to(20, 400)
    assert sending.send_blocker(doc, media, state, check, False, layout=layout,
                                mat="12x24") is None
    assert sending.send_blocker(doc, media, state, check, False, layout=layout) == (
        "Move the design inside the cut area.")              # its default mat: 12 x 12 in


def test_the_session_refuses_a_job_made_for_another_machine(doc):
    """The fake service, like the real one, refuses before anything else."""
    client = FakeDaemonClient()
    client.set_machine("cameo5", tools=" 2, 0")
    # no outline gate: a job that is not refused ends (DONE) instead of waiting for Cut
    spec = sending.job_spec(doc, LETTER, VINYL.to_tool_settings(), False).to_dict()
    run = JobRunner(client)
    assert asyncio.run(asyncio.wait_for(run.run_job(spec), 30)) is Outcome.FAILED
    assert run.error == ("refused: this job was made for the CAMEO 4, but the machine is the "
                         "CAMEO 5")
    assert client.steps_started == []


# --- the runner: an unchecked cut area ------------------------------------------------------------
def test_an_unchecked_unit_goes_on_with_a_note(doc):
    client = FakeDaemonClient()
    client.set_machine("portrait3", tools=" 2, 0")
    client.units_reply = dict(UNITS_UNCHECKED)
    media = geometry.media_rect("letter", False, (0, 0), "8x12", PORTRAIT3)
    spec = sending.job_spec(doc, media, VINYL.to_tool_settings(), False, machine="portrait3")
    run = JobRunner(client)
    assert asyncio.run(run.run_job(spec.to_dict())) is Outcome.DONE, run.error
    assert run.summary["units_enforced"] is False
    assert run.units_note == ("The cut area could not be checked on this model. The machine "
                              "answered “6100,4120”.")
    assert client.steps_done == list(range(len(run.steps)))


def test_unchecked_is_refused_where_the_unit_check_is_enforced(doc):
    client = FakeDaemonClient()
    client.units_reply = dict(UNITS_UNCHECKED)
    spec = sending.job_spec(doc, LETTER, VINYL.to_tool_settings(), False).to_dict()
    run = JobRunner(client)
    assert asyncio.run(run.run_job(spec)) is Outcome.FAILED
    assert run.units_note is None and runner.RELOAD_HINT in run.error
    units = next(i for i, s in enumerate(run.steps) if s.kind == "units")
    assert client.steps_started == list(range(units + 1))


@pytest.mark.parametrize("lie", [
    {"units_enforced": False},                                   # the CAMEO 4's 12 x 12 in
    {"units_enforced": False, "machine": "portrait3", "mat": "8x12"},   # not the JobSpec's
], ids=["not-enforced", "another-machine"])
def test_a_summary_that_drops_the_unit_check_is_not_believed(doc, monkeypatch, lie):
    """Defense in depth: the app's own profile and JobSpec decide whether 'unchecked' may pass,
    not only the service's summary."""
    import gui_fakes
    original = gui_fakes.FakeSession.open_job

    async def lying(self, spec):
        reply = await original(self, spec)
        return {**reply, "summary": {**reply["summary"], **lie}} if reply.get("ok") else reply
    monkeypatch.setattr(gui_fakes.FakeSession, "open_job", lying)
    client = FakeDaemonClient()
    client.units_reply = dict(UNITS_UNCHECKED)
    spec = sending.job_spec(doc, LETTER, VINYL.to_tool_settings(), False).to_dict()
    run = JobRunner(client)
    assert asyncio.run(run.run_job(spec)) is Outcome.FAILED
    assert run.summary["units_enforced"] is False and run.units_note is None
    units = next(i for i, s in enumerate(run.steps) if s.kind == "units")
    assert client.steps_started == list(range(units + 1))


def test_units_texts_and_reload_hints():
    assert runner.units_note({"reply": "12200,6100"}) == (
        "The cut area could not be checked on this model. The machine answered “12200,6100”.")
    assert runner.units_note({"reply": None}) == runner.UNITS_UNCHECKED
    assert runner.units_note({"reply": "\x1b<b>"}) == (
        "The cut area could not be checked on this model. The machine answered “?<b>”.")
    unchecked = {"verdict": "unchecked"}
    portrait = {"machine": "portrait3", "mat": "8x12"}          # its U reply is not known
    assert not runner.units_unchecked(unchecked, {})
    assert not runner.units_unchecked(unchecked, {**portrait, "units_enforced": True})
    assert not runner.units_unchecked(unchecked, {**portrait, "units_enforced": 0})
    assert runner.units_unchecked(unchecked, {**portrait, "units_enforced": False})
    assert not runner.units_unchecked({"verdict": "ok"}, {**portrait, "units_enforced": False})
    assert not runner.units_unchecked({"verdict": "mismatch"},
                                      {**portrait, "units_enforced": False})
    # the app's own profile must agree: a mat whose U reply is known is always enforced
    for summary in ({"machine": "cameo4", "mat": "12x12"}, {"machine": "cameo5", "mat": "12x24"},
                    {"machine": "portrait3", "mat": "12x12"}, {"machine": "cameo3", "mat": "8x12"},
                    {"mat": "8x12"}, {"machine": "portrait3"}):
        assert not runner.units_unchecked(unchecked, {**summary, "units_enforced": False})
    # and the summary is of the JobSpec the app sent
    assert runner.units_unchecked(unchecked, {**portrait, "units_enforced": False}, portrait)
    assert not runner.units_unchecked(unchecked, {**portrait, "units_enforced": False},
                                      {"machine": "portrait4", "mat": "8x12"})
    assert not runner.units_unchecked(unchecked, {**portrait, "units_enforced": False},
                                      {"mat": "8x12"})              # a CAMEO 4 JobSpec
    # the CAMEO 4 has one mat: the hint stays as it was
    assert runner.reload_hint() == runner.reload_hint(CAMEO4.mat("12x12")) == runner.RELOAD_HINT
    assert runner.summary_reload_hint({"machine": "cameo4", "mat": "12x12"}) == runner.RELOAD_HINT
    assert runner.summary_reload_hint({}) == runner.RELOAD_HINT
    assert runner.reload_hint(CAMEO5.mat("12x24"), CAMEO5) == (
        "Unload and load the 12 × 24 in mat on the machine, then send again.")
    assert runner.summary_reload_hint({"machine": "cameo5", "mat": "12x24"}) == (
        runner.reload_hint(CAMEO5.mat("12x24"), CAMEO5))
    assert runner.reload_hint(PORTRAIT3.mat("8x12"), PORTRAIT3) == runner.RELOAD_HINT


def test_an_unconfirmed_unit_names_the_mat_where_there_are_several(doc):
    client = FakeDaemonClient()
    client.set_machine("cameo5", tools=" 2, 0")
    client.units_reply = {"ok": True, "verdict": "unknown", "reply": None,
                          "message": "no reply to U; the unit is not confirmed"}
    media = geometry.media_rect("12x24", False, (0, 0), "12x24", CAMEO5)
    spec = sending.job_spec(doc, media, VINYL.to_tool_settings(), False, machine="cameo5",
                            mat="12x24")
    run = JobRunner(client)
    assert asyncio.run(run.run_job(spec.to_dict())) is Outcome.FAILED
    assert run.error.endswith("Unload and load the 12 × 24 in mat on the machine, then send "
                              "again.")


# --- the real service on the simulated machine ----------------------------------------------------
def test_the_monitor_reads_an_untested_machine_from_the_service(harness):
    harness.simulate(CAMEO5, tools=" 2, 12")

    async def scenario(h):
        state = await MachineMonitor(h.client()).refresh()
        assert state.connected and state.profile == "cameo5" and state.support == "untested"
        assert state.tools == (2, 12) and state.name == "CAMEO 5"
        layout = sending.LayoutMachine()
        assert layout.follow(state) and layout.machine == "cameo5"
    harness.run(scenario)


def test_the_monitor_reads_the_tested_cameo4_from_the_service(harness):
    async def scenario(h):
        state = await MachineMonitor(h.client()).refresh()
        assert (state.profile, state.support, state.tools) == ("cameo4", "tested", (2, 4))
    harness.run(scenario)


@pytest.mark.parametrize("fg, recognized, words", [
    (standard_fg("CAMEO 3", "V1.60"), "cameo3", "OpenCutter recognizes this CAMEO 3"),
    (standard_fg("CAMEO 4", "V9.99"), "cameo4", "its firmware V9.99"),
], ids=["cameo3", "cameo4-v9"])
def test_the_monitor_reads_a_recognized_machine_from_the_service(harness, fg, recognized,
                                                                 words):
    harness.fake_cls.fg_reply = fg

    async def scenario(h):
        state = await MachineMonitor(h.client()).refresh()
        assert state.wrong_model and not state.connected and state.profile is None
        assert state.support == "recognized" and state.recognized == recognized
        assert words in machine.other_machine_hint(state)
        assert h.fake.writes == [stubs.ESC05, stubs.FG]
    harness.run(scenario, until=lambda p: p.get("reason") == REASON_WRONG_MODEL)


def test_the_service_refuses_a_layout_for_another_machine(harness, doc):
    harness.simulate(CAMEO5, tools=" 2, 0")
    spec = sending.job_spec(doc, LETTER, VINYL.to_tool_settings(), False).to_dict()

    async def scenario(h):
        run = JobRunner(h.client())
        assert await asyncio.wait_for(run.run_job(spec), 30) is Outcome.FAILED
        assert "made for the CAMEO 4, but the machine is the CAMEO 5" in run.error
        assert not any(w.startswith(b"TG") for w in h.fake.writes)
    harness.run(scenario)


def test_an_unknown_u_reply_runs_with_the_note_through_the_service(harness, doc):
    harness.simulate(PORTRAIT4, tools=" 2, 0", u_reply="6100,4300", move_s=0.05)
    harness.fake_cls.accept_any = True
    media = geometry.media_rect("letter", False, (0, 0), "8.5x12", PORTRAIT4)
    spec = sending.job_spec(doc, media, VINYL.to_tool_settings(), False, machine="portrait4")

    async def scenario(h):
        run = JobRunner(h.client())
        assert await run.run_job(spec.to_dict()) is Outcome.DONE, run.error
        assert run.units_note == ("The cut area could not be checked on this model. The "
                                  "machine answered “6100,4300”.")
    harness.run(scenario)
