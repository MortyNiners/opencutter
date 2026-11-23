# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The Silhouette CAMEO 4: the machine OpenCutter was built and tested on.

Every value equals what OpenCutter sent before machine profiles existed (device/protocol.py,
job/builder.py, job/validate.py, device/daemon.py), so the bytes sent to a CAMEO 4 stay the
same. Firmware line V5 is TESTED: dry runs and an AutoBlade cut on V5.05. Line V1 runs the
same command handlers (firmware analysis of V1.20) but was never run, so it is UNTESTED.

Only the 12 x 12 in mat is listed. The 12 x 24 in (TG2) and 8 x 12 in (TG3) mats exist on this
model, but their U replies are only inferred from the firmware's window tables; they are added
once measured on a CAMEO 4.
"""
from __future__ import annotations

from .profile import (AccelForm, Dialect, FirmwareLine, Holder, HolderForm, Identity, IntRange,
                      MachineProfile, Mat, Queries, Ranges, StatusModel, StopSequence, Support,
                      Tool, ToolReply, UBasis)

# Firmware lines: the N of "VN.dd" in FG.
V5 = FirmwareLine(5, Support.TESTED, "measured on V5.05: tool-up dry runs and an AutoBlade cut")
V1 = FirmwareLine(1, Support.UNTESTED, "firmware analysis: V1.20 has the same command handlers "
                                        "as V5.05; no V1 machine has been run")

IDENTITY = Identity(
    fg_models=frozenset({"CAMEO 4"}),          # measured on V5.05: FG 'CAMEO 4 V5.05' + 4 spaces
    lines=(V5, V1),
    usb_ids=frozenset({(0x0B4D, 0x1137)}),     # firmware analysis: USB device descriptor
    ble_names=("CAMEO",),                      # the name filter OpenCutter has always used
)

# ESC 15 codes (Studio 5.0.556 machine data; firmware analysis for the reply). Codes 8..10 of
# holder 1 and 9..10 of holder 2 are reserved: no tool, so they fail closed. The Rotary Blade
# and the Punch Tool are refused: their motion was never checked.
_SHARED_TOOLS = (
    Tool(0, "No Tool Detected", usable=False),
    Tool(1, "Ratchet Blade", blade=True),
    Tool(3, "Deep-Cut Blade", blade=True),
    Tool(5, "Rotary Blade", blade=True, usable=False, action="Rotary Cut"),
    Tool(6, "Punch Tool", usable=False, action="Punch"),
    Tool(7, "Sketch Pen", action="Sketch"),
)


def _sorted(*tools: Tool) -> tuple[Tool, ...]:
    return tuple(sorted(tools, key=lambda t: t.code))


HOLDERS = (
    # Tool 1, the left holder: the AutoBlade sets its depth (TF) at its station here.
    Holder(1, "Tool 1", _sorted(*_SHARED_TOOLS,
                                Tool(2, "AutoBlade", blade=True, depth=True),
                                Tool(4, "Kraft Blade, 2mm", blade=True, action="Kraft Cut"))),
    # Tool 2, the right holder: TF does not act here (firmware analysis).
    Holder(2, "Tool 2", _sorted(*_SHARED_TOOLS,
                                Tool(2, "AutoBlade", blade=True),
                                Tool(4, "Kraft Blade, 3mm", blade=True, action="Kraft Cut"),
                                Tool(8, "Foil Tool", action="Foil"))),
)

# Firmware ranges (firmware analysis of V5.05; Studio 5.0.556 machine data agrees except TJ,
# which Studio starts at 0 and the firmware ignores). Passes is a UI rule.
RANGES = Ranges(
    speed=IntRange(1, 30),           # cm/s, whole numbers
    force=IntRange(1, 33),
    acceleration=IntRange(1, 10),
    offset=IntRange(0, 99),
    blade_depth=IntRange(0, 10),     # 0.1 mm per step
    overcut=IntRange(0, 9),          # a value of 10 or more drops the whole FF command
    passes=IntRange(1, 10),
)

# Two holders: J<n> first, per-holder forms, one TJ for both, one-digit ESC 15 codes in the
# order holder 1, holder 2. No FM: the CAMEO 4's machine data has no expand mode.
DIALECT = Dialect(holders=HolderForm.DUAL, accel=AccelForm.SHARED,
                  tool_reply=ToolReply.ONE_DIGIT, tool_slots=(0, 1))

MAT_12X12 = Mat(
    key="12x12", label="12 × 12 in", tg=1,
    size_mm=(304.8, 304.8),
    area_su=(30, 30, 6066, 6066),    # the mat minus 30 units on each side; cut on V5.05
    expected_u=(6100, 6100),         # measured on V5.05 after TG1 on a fresh load
    u_basis=UBasis.KNOWN,
    basis="measured on V5.05: U 6100,6100 after TG1, and cuts in place",
    grid_in=(12, 12),
    feed_mm=16.5,                    # firmware analysis: TG1 feeds 16.5 mm after a panel load
)

# ESC 05 replies (firmware analysis). '5' is the firmware's own: an earlier write was refused
# because another interface held the session; Studio's list does not have it.
STATUS = StatusModel(texts=(("0", "Ready"), ("1", "Busy"), ("2", "Load Media"), ("3", "Paused"),
                            ("4", "Cancelling"), ("5", "Rejected")))

# Read-only queries: ESC 05/10/15/16 answer without moving anything; the getters only report.
QUERIES = Queries(
    esc=frozenset({0x05, 0x10, 0x15, 0x16}),
    text=frozenset({b"FG", b"TB71", b"FA", b"FQ0", b"FQ2", b"TI", b"TO", b"TC", b"G", b"[",
                    b"U"}),
    max_write=20,
)

# ESC 09 twice, 0.3 s apart: the second one covers a latch that makes the first one only clear
# itself (firmware analysis); stopping an idle or paused machine was measured on V5.05.
STOP = StopSequence(writes=(b"\x1b\x09", b"\x1b\x09"), gap_s=0.3)

CAMEO4 = MachineProfile(
    id="cameo4",
    name="CAMEO 4",
    support=Support.TESTED,
    identity=IDENTITY,
    holders=HOLDERS,
    ranges=RANGES,
    dialect=DIALECT,
    mats=(MAT_12X12,),
    default_mat="12x12",
    status=STATUS,
    queries=QUERIES,
    stop=STOP,
    basis="measured on V5.05 over Bluetooth LE; firmware analysis of V5.05 and V1.20",
    unit_mm=0.05,                    # measured: U 6100 after TG1, a 508-unit square is 25.4 mm
    max_write=182,                   # the longest write Studio sends; the firmware's cap is 480
    batch_bytes=1024,                # firmware analysis: its input queue takes 1024 bytes
    max_coordinate_su=99_999,
    lowering_prefixes=(b"D", b"TF"),  # TF cycles the AutoBlade down and up at its station
    fg_claims_session=True,          # V5.05: FG claims the session, status 1 becomes 2
    power_cycle_to_connect=True,     # observed: a new link only right after power-on
    programs=frozenset({"dry-run", "pen-test", "cut-test"}),
    # firmware analysis of V5.05: ESC 07 pauses between commands while busy or idle (ignored when
    # paused); ESC 08 goes on from where it stopped, only while paused (no effect otherwise)
    pause=b"\x1b\x07",
    resume=b"\x1b\x08",
)

PROFILES: tuple = (CAMEO4,)
