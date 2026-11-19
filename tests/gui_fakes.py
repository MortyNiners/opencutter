# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Fakes shared by the GUI tests (this module holds no tests).

FakeDaemonClient has the DaemonClient interface but no socket: it answers ping/info from
attributes, records stop() calls, and its sessions plan the job with the real
opencutter.job.plan (as the daemon does) and run steps instantly or until released.
The unit check (a 'units' step) answers like the daemon does: with ``units_reply`` (the
verdict 'ok' by default).

A connected ping names the model the machine reports (the tested CAMEO 4's); ``WRONG_MODEL_PING``
is the ping of a service whose model gate refused the machine (a model no profile names), and
``RECOGNIZED_PING`` that of one it refused although OpenCutter recognizes it (a CAMEO 3).

The machine ping (``ping_machine()``, {"op": "ping", "machine": true}) is the ping plus the
machine fields: ``machine_reply`` when set, else those of ``machine_fields_for(ping)`` (the
CAMEO 4, tested, while connected; nothing otherwise). ``set_machine(id)`` makes the fake a
connected machine of another profile: its ping, its machine fields, and its tools in that
profile's grammar. ``machine_pings`` counts the machine pings. A job session refuses a JobSpec
made for another machine than the connected one, as the service does, before anything else.

The stop fields ({"op": "ping", "stop": true}: ``ping_stop()``, or ``ping_machine(stop=True)``)
come from ``stop_owed``, ``owed_stops_sent``, ``motion_stopped``, ``interrupted`` and
``load_seen``; ``set_status`` to the load digit '2' counts in ``load_seen`` as the service does.
``stop(check=True)`` adds "status_after" and "still_busy" from the status, like the service's
checked stop; ``stop_gate`` (an asyncio.Event) holds every stop reply until it is set, and
``stop_checks`` records the check flag of every stop. A job session's steps carry "tool_down"
as the service computes it.

Its ping is that of a service of this very build (``identity`` is merged into every reply; a
"build" in ``ping_reply`` wins, ``identity = {}`` gives an older service's reply). An older
service on the old socket is ``older_reply``; ``shutdown()`` and ``terminate_older()`` end the
fake services and record the calls; ``spawn_ping`` is what a daemon started by
``ensure_daemon`` answers (of this very build, or of ``spawn_identity``), and its "pid" is
``spawned_pid``, unless ``sandboxed`` (the app runs in a Flatpak sandbox: the service it starts
runs in a sandbox of its own, and ``spawned_pid`` stays None). ``started_here`` is
DaemonClient's: this run's id in "started_by", or the pid it started.
"""
from __future__ import annotations

import asyncio

from opencutter.device.protocol import lowers_tool
from opencutter.identity import RUN_ID, identity
from opencutter.job.plan import build_plan
from opencutter.job.spec import DEFAULT_MACHINE, JobSpec
from opencutter.machines import by_id, enabled_profile
from opencutter.presets.model import Preset, PresetStore, file_source

OTHER_BUILD = {"version": "0.0.9", "build": "0123456789ab", "api": 1}   # another version

READY_PING = {"ok": True, "connected": True, "status": "0", "motion": None,
              "device": {"name": "CAMEO 4-TEST", "address": "00:00:00:00:00:00"},
              "model": "CAMEO 4 V5.05"}
# what the service answers once its model gate refused the machine (nothing is sent to it): a
# model no profile names
WRONG_MODEL_PING = {"ok": True, "connected": False, "status": None, "motion": None,
                    "device": None, "reason": "wrong-model", "model": "CAMEO 5 PRO V1.07"}
CAMEO3_REASON = ("OpenCutter recognizes this CAMEO 3 but cannot drive it yet: there is no known "
                 "way to stop it from the computer, and it does not report which tools are "
                 "loaded.")
# a model OpenCutter recognizes but does not drive, refused the same way (its machine fields
# name it, with the reason)
RECOGNIZED_PING = {**WRONG_MODEL_PING, "model": "CAMEO 3 V1.60"}
RECOGNIZED_FIELDS = {"profile": "cameo3", "support": "recognized", "refused_reason": CAMEO3_REASON}
NO_MACHINE_FIELDS = {"profile": None, "support": None, "refused_reason": None}
INFO = {"ok": True, "status": "0", "tools": " 2, 4", "firmware": "CAMEO 4 V5.05    "}
UNITS_OK = {"ok": True, "verdict": "ok", "reply": "6100,6100",
            "message": "U = '6100,6100': 0.05 mm per unit, confirmed"}
UNITS_UNKNOWN = {"ok": True, "verdict": "unknown", "reply": None,
                 "message": "no reply to U; the unit is not confirmed"}
# a mat whose U reply is not known, on an untested machine: shown, not enforced
UNITS_UNCHECKED = {"ok": True, "verdict": "unchecked", "reply": "6100,4120",
                   "message": "U = '6100,4120': not checked. The reply the 8 x 12 in mat gives "
                              "on this model is not known yet, so the cut area could not be "
                              "checked."}


def machine_fields_for(ping: dict | None) -> dict:
    """The machine fields a service gives with this ping: the tested CAMEO 4 while connected,
    nothing otherwise (a model no profile names)."""
    if ping and ping.get("connected"):
        return {"profile": DEFAULT_MACHINE, "support": "tested", "refused_reason": None}
    return dict(NO_MACHINE_FIELDS)


def connected_ping(profile_id: str, version: str = "V1.07") -> dict:
    """READY_PING for a machine of another profile: its BLE-like name and its FG model."""
    profile = enabled_profile(profile_id)
    model = sorted(profile.identity.fg_models)[0]
    return {**READY_PING, "device": {"name": f"{profile.name}-TEST",
                                     "address": "00:00:00:00:00:00"},
            "model": f"{model} {version}"}


class FakeSession:
    """One fake daemon session."""

    def __init__(self, client: "FakeDaemonClient"):
        self.client = client
        self.closed = False
        self.kinds: list[str] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc) -> None:
        self.closed = True
        self.client.sessions_closed += 1

    async def open_job(self, spec: dict) -> dict:
        self.client.opened.append(spec)
        if self.client.open_reply is not None:
            return self.client.open_reply
        connected = self.client.connected_profile()
        machine = spec.get("machine", DEFAULT_MACHINE)
        if connected is not None and machine != connected.id:
            entry = by_id(machine)
            made_for = f"the {entry.name}" if entry else f"an unknown machine {machine!r}"
            return {"ok": False, "error": f"refused: this job was made for {made_for}, but the "
                                          f"machine is the {connected.name}"}
        job = JobSpec.from_dict(spec)
        plan = build_plan(job)
        profile = enabled_profile(job.machine)
        self.kinds = [s.kind for s in plan.steps]
        return {"ok": True, "status": "0", "tools": " 2, 4", "write_limit": 182,
                "steps": [{"kind": s.kind, "title": s.title, "expect": s.expect,
                           "confirm": s.confirm, "writes": len(s.writes),
                           "tool_down": s.kind == "write"
                           and any(lowers_tool(w, profile) for w in s.writes)}
                          for s in plan.steps],
                "summary": plan.summary}

    async def step(self, index: int, timeout: float | None = 400) -> dict:
        self.client.steps_started.append(index)
        hold = self.client.hold_step
        if hold is not None and index == hold[0]:
            await hold[1].wait()
        if isinstance(self.client.fail, Exception) and index == self.client.fail_at:
            raise self.client.fail
        if index == self.client.fail_at:
            return {"ok": False, "error": self.client.fail, "stopped": True,
                    "stop_errors": list(self.client.stop_errors)}
        self.client.steps_done.append(index)
        if index < len(self.kinds) and self.kinds[index] == "units":
            return {**self.client.units_reply, "seconds": 0.1}
        return {"ok": True, "status": "0", "seconds": 0.1}

    async def end(self) -> None:
        self.client.ended += 1


class FakeDaemonClient:
    """DaemonClient stand-in for the GUI tests."""

    def __init__(self, ping: dict | None = READY_PING, info: dict | None = INFO):
        self.ping_reply = dict(ping) if ping else None
        self.info_reply = dict(info) if info else None
        self.stops = 0
        self.stop_reply = {"ok": True, "errors": []}
        self.stop_gate: asyncio.Event | None = None   # stop() waits for it when set
        self.stop_checks: list[bool] = []          # the check flag of every stop
        self.stop_pings = 0                        # ping_stop() calls
        self.stop_owed = False                     # the stop fields of the stop ping
        self.owed_stops_sent = 0
        self.motion_stopped = False
        self.interrupted: str | None = None
        self.load_seen = 0
        self.opened: list[dict] = []
        self.open_reply: dict | None = None
        self.steps_started: list[int] = []
        self.steps_done: list[int] = []
        self.hold_step: tuple[int, asyncio.Event] | None = None   # (index, released when set)
        self.fail_at: int | None = None
        self.fail: str | Exception = "simulated failure"
        self.stop_errors: list[str] = []          # what a failed step reports as its stop errors
        self.units_reply: dict = dict(UNITS_OK)
        self.machine_reply: dict | None = None    # the machine fields; None: machine_fields_for
        self.machine_pings = 0
        self.ended = 0
        self.sessions_closed = 0
        self.daemon_started = 0
        self.spawn_error: str | None = None       # what DaemonClient reports for a failed start
        self.identity: dict = identity()          # merged into every ping: this very build
        self.manages_service = True               # the default socket: may start and replace
        self.older_reply: dict | None = None      # an older service on the old socket
        self.spawn_ping: dict | None = None       # what a daemon started by ensure_daemon answers
        self.spawn_identity: dict | None = None   # its build (None: this very build)
        self.spawned_pid: int | None = None       # DaemonClient: the pid of the last one started
        self.sandboxed = False                    # in a Flatpak: the started pid is not known
        self.shutdown_reply: dict | None = None   # None: the service ends
        self.terminate_reply: dict | None = None  # None: the older service ends
        self.shutdown_gate: asyncio.Event | None = None   # shutdown() waits for it when set
        self.shutdowns = 0
        self.terminated: list[str | None] = []    # the paths terminate_older() was called with
        self.helpers_ended = 0                    # end_helper() calls (window closed, app quit)

    def set_tools(self, reply: str) -> None:
        """Change what ESC 15 reports, e.g. ' 7, 4' for a Sketch Pen in Tool 1."""
        self.info_reply = {**self.info_reply, "tools": reply}

    def set_machine(self, profile_id: str, support: str = "untested",
                    tools: str = " 2, 0", version: str = "V1.07") -> None:
        """Make the fake a connected machine of another profile (untested, by default)."""
        self.ping_reply = connected_ping(profile_id, version)
        self.machine_reply = {"profile": profile_id, "support": support, "refused_reason": None}
        self.info_reply = {**(self.info_reply or INFO), "tools": tools,
                           "firmware": f"{self.ping_reply['model']}    "}

    def connected_profile(self):
        """The profile of the connected machine as its machine fields name it, or None."""
        if not (self.ping_reply and self.ping_reply.get("connected")):
            return None
        fields = self.machine_reply if self.machine_reply is not None else (
            machine_fields_for(self.ping_reply))
        return enabled_profile(fields.get("profile"))

    def set_status(self, status: str) -> None:
        """Change the ESC 05 status ('0' Ready, '2' Load Media, ...); a change to '2' counts in
        load_seen, as the service counts it."""
        if status == "2" and (self.ping_reply or {}).get("status") != "2":
            self.load_seen += 1
        self.ping_reply = {**self.ping_reply, "status": status}
        self.info_reply = {**self.info_reply, "status": status}

    def stop_fields(self) -> dict:
        """What {"op": "ping", "stop": true} adds (daemon.PING_STOP_KEYS)."""
        return {"stop_owed": self.stop_owed, "owed_stops_sent": self.owed_stops_sent,
                "motion_stopped": self.motion_stopped, "interrupted": self.interrupted,
                "load_seen": self.load_seen}

    @property
    def socket_path(self) -> str:
        return "/fake/opencutter.sock"

    async def ping(self) -> dict | None:
        if self.ping_reply is None:
            return None
        return {**self.identity, **self.ping_reply}

    async def ping_machine(self, stop: bool = False) -> dict | None:
        """DaemonClient.ping_machine: the ping plus the machine fields (and with `stop`, the
        stop fields)."""
        self.machine_pings += 1
        ping = await self.ping()
        if ping is None:
            return None
        fields = self.machine_reply if self.machine_reply is not None else (
            machine_fields_for(self.ping_reply))
        return {**ping, **fields, **(self.stop_fields() if stop else {})}

    async def ping_stop(self) -> dict | None:
        """DaemonClient.ping_stop: the ping plus the stop fields."""
        self.stop_pings += 1
        ping = await self.ping()
        if ping is None:
            return None
        return {**ping, **self.stop_fields()}

    async def ping_older(self) -> dict | None:
        return dict(self.older_reply) if self.older_reply is not None else None

    async def info(self) -> dict:
        return self.info_reply or {"ok": False, "error": "no info"}

    async def stop(self, check: bool = False) -> dict:
        self.stops += 1
        self.stop_checks.append(check)
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        reply = self.stop_reply
        if check and reply.get("ok"):
            status = (self.ping_reply or {}).get("status")
            reply = {**reply, "status_after": status,
                     "still_busy": None if status is None else status == "1"}
        return reply

    async def shutdown(self, wait: float = 10) -> dict:
        self.shutdowns += 1
        if self.shutdown_gate is not None:
            await self.shutdown_gate.wait()
        if self.shutdown_reply is not None:
            return self.shutdown_reply
        self.ping_reply = None
        return {"ok": True, "pid": 4242}

    async def terminate_older(self, path: str | None = None, wait: float = 10) -> dict:
        self.terminated.append(path)
        if self.terminate_reply is not None:
            return self.terminate_reply
        if path is None:
            self.older_reply = None
        else:
            self.ping_reply = None
        return {"ok": True, "pid": 4243}

    async def ensure_daemon(self) -> bool:
        self.daemon_started += 1
        if self.ping_reply is None and self.spawn_ping is not None:
            self.ping_reply = dict(self.spawn_ping)
            self.identity = dict(self.spawn_identity or identity())   # this very build, by default
            self.spawned_pid = None if self.sandboxed else self.ping_reply.get("pid")
        return self.ping_reply is not None

    def end_helper(self) -> None:
        self.helpers_ended += 1

    def started_here(self, ping: dict | None) -> bool:
        """DaemonClient.started_here: this run's id; another run's id is not ours; without a
        run id (outside a sandbox) the started pid."""
        if not isinstance(ping, dict):
            return False
        started_by = ping.get("started_by")
        if isinstance(started_by, str):
            return started_by == RUN_ID
        pid = ping.get("pid")
        return self.spawned_pid is not None and type(pid) is int and pid == self.spawned_pid

    def session(self) -> FakeSession:
        return FakeSession(self)


# --- a presets-file-like store: many materials, nothing verified -------------------------------
# Made-up names and values (synthetic test data), shaped like a large presets file: several
# material types, materials with more than one action, AutoBlade rows with a Blade Depth,
# Ratchet Blade, Kraft Blade and Sketch Pen rows without one. No row is verified.
IMPORTED_TYPES = {
    "Acetate": ["Test Acetate, Thin", "Test Acetate, Thick"],
    "Cardstock": ["Test Card, Smooth", "Test Card, Textured", "Test Card, Glitter"],
    "Fabric": ["Test Felt", "Test Canvas"],
    "Paper": ["Test Copy Paper", "Test Sticker Paper", "Test Vellum"],
    "Specialty": ["Test Chipboard", "Test Magnet Sheet", "Test Craft Foam"],
    "Vinyl": ["Test Vinyl, Glossy", "Test Vinyl, Matte", "Test Vinyl, Metallic", "Test Vinyl, Flock"],
}


def _row(material, material_type, action, holder, tool, code, i, depth=True) -> Preset:
    blade = code in (1, 2, 3, 4)
    return Preset(material=material, material_type=material_type, action=action, holder=holder,
                  tool=tool, tool_code=code, speed=1 + i % 8, force=1 + (i * 7) % 33,
                  acceleration=1 + i % 10, offset=18 if blade else 0,
                  line_segment_overcut=False, overcut=(1, 1) if blade else (0, 0),
                  blade_depth=(1 + i % 10) if depth and code == 2 and holder == 1 else None,
                  passes=1 + (i % 3 == 0))


def imported_store() -> PresetStore:
    """A store like an imported presets file: 17 materials in 6 types, none verified."""
    rows = []
    i = 0
    for material_type, names in IMPORTED_TYPES.items():
        for name in names:
            i += 1
            if material_type == "Specialty" and i % 2:
                rows.append(_row(name, material_type, "Kraft Cut", 2, "Kraft Blade, 3mm", 4, i))
                continue
            if material_type == "Fabric":
                rows.append(_row(name, material_type, "Cut", 1, "Ratchet Blade", 1, i))
            else:
                rows.append(_row(name, material_type, "Cut", 1, "AutoBlade", 2, i))
            if material_type in ("Cardstock", "Paper"):
                rows.append(_row(name, material_type, "Sketch", 1, "Sketch Pen", 7, i + 3))
                rows.append(_row(name, material_type, "Score", 2, "Kraft Blade, 3mm", 4, i + 5))
    return PresetStore(rows, file_source("Imported presets"))
