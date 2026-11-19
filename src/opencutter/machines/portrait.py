# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The Portrait 3 and the Portrait 4.

Both are UNTESTED: ported from Studio 5.0.556's machine data and checked against the firmware
Studio bundles for them (V1.05 and V5.02 for the Portrait 3, V1.05 for the Portrait 4). Neither
has been run with OpenCutter. Jobs are allowed; the README lists both as not yet tested, and the
app shows no note about it.

How they differ from the CAMEO 4:
  * One tool holder, labelled "Tool". Studio writes the plain single-holder forms with no J
    (HolderForm.SINGLE): !s FXf FCo FEx,0 FFs,e,0, then TFd,1 for the AutoBlade. Without a
    holder argument the firmware sets all of its conditions, so the active one does not matter;
    J itself is read and ignored (firmware analysis).
  * The Portrait 3 has no acceleration: its firmware has no TJ (AccelForm.NONE). The Portrait 4
    takes TJ 1..3 and ignores anything else. Studio sends its force and acceleration as one
    command, which the firmware reads as FX followed by TJ, so OpenCutter writes the two as
    separate commands; the firmware ends in the same state (firmware analysis).
  * Speed 1..10 cm/s in whole numbers. The Portrait 4 also takes tenths and has a separate
    tool-up speed; OpenCutter sends neither (every accepted speed command resets the tool-up
    speed to its maximum).
  * Offset 0..45: Studio's range, inside the 0..99 the firmware accepts.
  * Their own TG values. The 8 x 12 in mat is TG3 on the Portrait 3. The 8.5 x 12 in mat is
    TG11 on the Portrait 4, whose firmware turns every TG other than 0, 7, 11 and 13 into TG0
    (a roll with no mat feed). TG1, the CAMEO 4's 12 x 12 in mat, is never sent to a Portrait.
  * No TO or TC query: neither firmware has them.
  * ESC 15 answers with two codes, as on the CAMEO 4, and which one belongs to the Portrait's
    holder is not established. The first is used, and the second must read 0: any other code
    there fails the tool check closed and the raw reply is shown, so a tool reported in the
    second slot is never guessed.

The U reply of each mat is UNKNOWN. The firmware's window tables give 6100,4120 (Portrait 3,
TG3) and 6100,4320 (Portrait 4, TG11), but only if the firmware's step factor is the CAMEO 4's,
and that was not checked on either Portrait. So U is logged and shown but not enforced, which
is allowed because both profiles are UNTESTED (Mat.offered).

Left out until their facts are known: the 6 x 8 in mat (no TG value is known for it), the
PixScan mat (no cut area; on the Portrait 4 its TG value turns into TG0) and the Portrait 4's
electrostatic mat (TG13, no cut area). The Portrait 1 and the Portrait 2 are only recognized;
they belong in recognized.py.
"""
from __future__ import annotations

from .profile import (AccelForm, Dialect, FirmwareLine, Holder, HolderForm, Identity, IntRange,
                      MachineProfile, Mat, Queries, Ranges, StatusModel, StopSequence, Support,
                      Tool, ToolReply, UBasis)

# --- firmware lines: the N of "VN.dd" in FG ----------------------------------------------------
P3_V1 = FirmwareLine(1, Support.UNTESTED, "firmware analysis of V1.05; never run")
P3_V5 = FirmwareLine(5, Support.UNTESTED, "firmware analysis of V5.02: the same command set, TG "
                                          "table, speed limit and FX handling as V1.05 (only the "
                                          "addresses differ); never run")
P4_V1 = FirmwareLine(1, Support.UNTESTED, "firmware analysis of V1.05; never run")

P3_IDENTITY = Identity(
    fg_models=frozenset({"Portrait 3"}),       # firmware analysis: FG 'Portrait 3 V1.05' + 4 spaces
    lines=(P3_V1, P3_V5),
    usb_ids=frozenset({(0x0B4D, 0x113A)}),     # firmware analysis (USB descriptor); Studio agrees
    ble_names=("Portrait",),                   # firmware analysis: named 'Portrait 3-' + serial
)
P4_IDENTITY = Identity(
    fg_models=frozenset({"Portrait 4"}),       # firmware analysis: FG 'Portrait 4 V1.05' + 4 spaces
    lines=(P4_V1,),
    usb_ids=frozenset({(0x0B4D, 0x113F)}),     # firmware analysis (USB descriptor); Studio agrees
    ble_names=("Portrait",),                   # firmware analysis: named 'Portrait 4-' + serial
)

# --- the one holder ----------------------------------------------------------------------------
# ESC 15 codes of the reply's first slot (Studio 5.0.556 machine data: the Portrait 3 shares the
# CAMEO 4's Tool 1 list). Codes 8..10 are reserved: no tool, so they fail closed. The Rotary Blade
# and the Punch Tool are refused, as on the CAMEO 4: their motion was never checked.
_TOOLS = (
    Tool(0, "No Tool Detected", usable=False),
    Tool(1, "Ratchet Blade", blade=True),
    Tool(2, "AutoBlade", blade=True, depth=True),   # TF sets its depth at a station
    Tool(3, "Deep-Cut Blade", blade=True),
    Tool(4, "Kraft Blade, 2mm", blade=True, action="Kraft Cut"),
    Tool(5, "Rotary Blade", blade=True, usable=False, action="Rotary Cut"),
    Tool(6, "Punch Tool", usable=False, action="Punch"),
    Tool(7, "Sketch Pen", action="Sketch"),
)
P3_HOLDERS = (Holder(1, "Tool", _TOOLS),)
# The Portrait 4's own list adds the Foil Tool as code 8; 9 and 10 are reserved (Studio 5.0.556
# machine data). Its AutoBlade keeps the last depth and skips a TF that repeats it (firmware
# analysis).
P4_HOLDERS = (Holder(1, "Tool", _TOOLS + (Tool(8, "Foil Tool", action="Foil"),)),)

# --- ranges: Studio's range intersected with the firmware's ------------------------------------
P3_RANGES = Ranges(
    speed=IntRange(1, 10),           # cm/s, whole: both agree; the firmware ignores values outside
    force=IntRange(1, 33),           # both agree; the firmware clamps to 1..33
    acceleration=None,               # no TJ in the firmware
    offset=IntRange(0, 45),          # Studio's range; the firmware accepts 0..99
    blade_depth=IntRange(0, 10),     # 0.1 mm per step; both agree
    overcut=IntRange(0, 9),          # each FF value; both agree (10 or more drops the command)
    passes=IntRange(1, 10),          # a UI rule
)
P4_RANGES = Ranges(
    speed=IntRange(1, 10),           # cm/s, whole: both agree; the tenths the firmware also takes
                                     # (0.1..10.0) are never sent
    force=IntRange(1, 33),           # both agree; the firmware clamps to 1..33
    acceleration=IntRange(1, 3),     # the firmware ignores TJ outside 1..3; Studio offers 1..10
    offset=IntRange(0, 45),          # Studio's range; the firmware accepts 0..99
    blade_depth=IntRange(0, 10),     # 0.1 mm per step; both agree
    overcut=IntRange(0, 9),          # each FF value; both agree (10 or more drops the command)
    passes=IntRange(1, 10),          # a UI rule
)

# --- dialect -----------------------------------------------------------------------------------
# One holder: no J, the plain forms. One-digit ESC 15 codes (the CAMEO 4's reply, firmware
# analysis) in the first slot, the rule for a one-holder model whose slot is not established;
# the unused second slot must read 0 (MachineProfile.holder_codes).
P3_DIALECT = Dialect(holders=HolderForm.SINGLE, accel=AccelForm.NONE,
                     tool_reply=ToolReply.ONE_DIGIT, tool_slots=(0,))
P4_DIALECT = Dialect(holders=HolderForm.SINGLE, accel=AccelForm.SHARED,
                     tool_reply=ToolReply.ONE_DIGIT, tool_slots=(0,))

# --- mats --------------------------------------------------------------------------------------
MAT_8X12 = Mat(
    key="8x12", label="8 × 12 in", tg=3,    # Studio's TG value; TG1..3 open one window
    size_mm=(203.2, 304.8),
    area_su=(30, 30, 6066, 4034),    # the mat minus 30 units on each side, Studio's rule
    expected_u=None,                 # the window reads 6100,4120 only at the CAMEO 4's step factor
    u_basis=UBasis.UNKNOWN,
    basis="Studio 5.0.556 machine data (TG value, mat size) and firmware analysis of V1.05 and "
          "V5.02 (TG3 opens a 305 x 206 mm window); cut area by Studio's rule, 30 units in from "
          "each edge of the mat; the step factor, and so U, is not checked",
    feed_mm=13.5,                    # firmware analysis: the TG table, read as on the CAMEO 4
)
MAT_8_5X12 = Mat(
    key="8.5x12", label="8.5 × 12 in", tg=11,   # Studio's TG value; the firmware keeps 11
    size_mm=(215.9, 304.8),
    area_su=(30, 30, 6066, 4288),    # the mat minus 30 units on each side, Studio's rule
    expected_u=None,                 # the window reads 6100,4320 only at the CAMEO 4's step factor
    u_basis=UBasis.UNKNOWN,
    basis="Studio 5.0.556 machine data (TG value, mat size) and firmware analysis of V1.05 "
          "(TG11 opens a 305 x 216 mm window); cut area by Studio's rule, 30 units in from each "
          "edge of the mat; the step factor, and so U, is not checked",
    feed_mm=16.0,                    # firmware analysis: the TG table, read as on the CAMEO 4
)

# --- status, queries, stop ---------------------------------------------------------------------
# ESC 05 replies (firmware analysis): the same reply code as the CAMEO 4's. Studio's list adds 6
# (heating, the Portrait 4's heat pen), which that code never sends: a 6 aborts the step.
STATUS = StatusModel(texts=(("0", "Ready"), ("1", "Busy"), ("2", "Load Media"), ("3", "Paused"),
                            ("4", "Cancelling"), ("5", "Rejected")))

# Read-only queries (firmware analysis): ESC 05/10/15/16 are answered by the same code as on the
# CAMEO 4; the getters are the CAMEO 4's without TO and TC, which neither firmware has.
QUERIES = Queries(
    esc=frozenset({0x05, 0x10, 0x15, 0x16}),
    text=frozenset({b"FG", b"TB71", b"FA", b"FQ0", b"FQ2", b"TI", b"G", b"[", b"U"}),
    max_write=20,
)

# ESC 09 twice, 0.3 s apart, as on the CAMEO 4: both firmwares have the latch that makes a lone
# ESC 09 only clear itself (firmware analysis). Not run on a Portrait.
STOP = StopSequence(writes=(b"\x1b\x09", b"\x1b\x09"), gap_s=0.3)

# --- the profiles ------------------------------------------------------------------------------
# Shared by both (firmware analysis: the same Bluetooth core and command handlers as the CAMEO 4):
#   max_write 182        the longest write Studio sends; the firmware's cap is 480
#   batch_bytes 1024     the CAMEO 4's input queue, by analogy: the Bluetooth data path is the
#                        same code, but the queue size was not checked on a Portrait
#   max_coordinate_su    the M and D handlers are the CAMEO 4's
#   fg_claims_session    the session claim was traced on the CAMEO 4 and the same code is here;
#                        reading the status again after FG is harmless either way
#   power_cycle_to_connect  texts only: not observed on a Portrait, so the safe advice to switch
#                        it off and on, as for every model where it was not observed
PORTRAIT3 = MachineProfile(
    id="portrait3",
    name="Portrait 3",
    support=Support.UNTESTED,
    identity=P3_IDENTITY,
    holders=P3_HOLDERS,
    ranges=P3_RANGES,
    dialect=P3_DIALECT,
    mats=(MAT_8X12,),
    default_mat="8x12",              # Studio's default mat for this model
    status=STATUS,
    queries=QUERIES,
    stop=STOP,
    basis="Studio 5.0.556 machine data; firmware analysis of V1.05 and V5.02; never run",
    unit_mm=0.05,                    # Studio's plotter unit for this model
    max_write=182,
    batch_bytes=1024,
    max_coordinate_su=99_999,
    lowering_prefixes=(b"D", b"TF"),  # TF cycles the AutoBlade at its station (firmware analysis)
    fg_claims_session=True,
    power_cycle_to_connect=True,
    programs=frozenset(),            # no pinned test programs yet
)

PORTRAIT4 = MachineProfile(
    id="portrait4",
    name="Portrait 4",
    support=Support.UNTESTED,
    identity=P4_IDENTITY,
    holders=P4_HOLDERS,
    ranges=P4_RANGES,
    dialect=P4_DIALECT,
    mats=(MAT_8_5X12,),
    default_mat="8.5x12",            # Studio's default mat for this model
    status=STATUS,
    queries=QUERIES,
    stop=STOP,
    basis="Studio 5.0.556 machine data; firmware analysis of V1.05; never run",
    unit_mm=0.05,                    # Studio's plotter unit for this model
    max_write=182,
    batch_bytes=1024,
    max_coordinate_su=99_999,
    lowering_prefixes=(b"D", b"TF"),  # TF moves the AutoBlade to a station (firmware analysis)
    fg_claims_session=True,
    power_cycle_to_connect=True,
    programs=frozenset(),            # no pinned test programs yet
)

PROFILES: tuple = (PORTRAIT3, PORTRAIT4)
