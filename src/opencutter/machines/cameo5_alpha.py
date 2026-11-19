# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The CAMEO 5 Alpha and the CAMEO 5 Alpha Plus.

Both UNTESTED (jobs allowed): no Alpha has been run with OpenCutter. The values come from the
firmware analysis of the V1.04 image both models run and from Studio 5.0.556 machine data. The
two models are one firmware image with a stored model variant. The variant changes the FG model
text ('CAMEO 5 ALPHA', 'CAMEO 5 ALPHA PLUS') and the TG values the machine accepts; the USB
product id differs too (their USB vendor id is not Silhouette's usual one).

They are CAMEO 5 generation machines: OpenCutter writes them exactly the CAMEO 4's job (the same
header, condition blocks, trailer and stop), and the firmware reads every one of those commands
with the same meaning. What differs from the CAMEO 4:
  * Force goes up to 40 (the firmware clamps FX to 1..40; Studio's range is the same).
  * The ESC 15 reply can carry two-digit tool codes, and holder 1 can report the Foil Tool.
  * Some text queries answer differently, so fewer of them are allowed (QUERIES).
  * TG3 (8 x 12 in) is not a mat here: the firmware turns it into TG0, the roll with no mat.
    So does TG9 (24 x 24 in) on both models and TG8 (15 x 15 in) on the Alpha.

Mats: 12 x 12 in (TG1) and 12 x 24 in (TG2) on both models. Their firmware window entries are
the CAMEO 4's, so their U replies are INFERRED (enforced). Left out until their cut areas are
worked out: 15 x 15 in (TG8, Plus only), the electrostatic mats, 8.5 x 12 in and PixScan. The
6 x 8 in mat is left out because its TG value is unknown.

Never sent, though Studio's data has them for these models:
  * FM (expand mode): it is only ever sent in the header of a TG9 mat (TG9 FN0 TB50,0 FM1,
    as Studio sends it), and neither model accepts the 24 x 24 in mat; on the mats offered
    here the firmware ignores FM anyway. So the profiles do not take expand mode
    (expand_mode=False) and FM is never sent.
  * BZK: it only changes how the firmware divides BZ and TZ curves; OpenCutter sends straight
    moves (M, D) only, so jobs do not need it.
  * AOC (Alpha Plus): it drives an actuator; OpenCutter jobs do not need it.
  * APS (tool-up speed): every speed command resets it to the firmware's default (30, the top
    speed), and OpenCutter keeps that default.
  * ACT (the cross cutter), TR, TS and the registration-mark and scan modes of TB.
None of these is in the profile's command grammar, so the job validator refuses them.
"""
from __future__ import annotations

from dataclasses import replace

from .profile import (AccelForm, Dialect, FirmwareLine, Holder, HolderForm, Identity, IntRange,
                      MachineProfile, Mat, Queries, Ranges, StatusModel, StopSequence, Support,
                      Tool, ToolReply, UBasis)

# Firmware line: the N of "VN.dd" in FG. The bundled image is V1.04; public captures and reports
# show units on V1.02 and V1.03 with the same FG format.
V1 = FirmwareLine(1, Support.UNTESTED, "firmware analysis of V1.04; V1.02 and V1.03 units report "
                                       "the same FG format (public captures and reports); no "
                                       "Alpha has been run")

USB_VENDOR = 0x3844                  # firmware analysis: both USB device descriptors use it

IDENTITY_ALPHA = Identity(
    # firmware analysis; FG replies in public captures and reports agree
    fg_models=frozenset({"CAMEO 5 ALPHA"}),
    lines=(V1,),
    usb_ids=frozenset({(USB_VENDOR, 0x0001)}),     # firmware analysis: first USB descriptor
    ble_names=("CAMEO",),            # its BLE name starts 'CAMEO 5 ALPHA-' (firmware analysis)
)

IDENTITY_ALPHA_PLUS = Identity(
    # firmware analysis; an FG reply in public captures and reports agrees
    fg_models=frozenset({"CAMEO 5 ALPHA PLUS"}),
    lines=(V1,),
    # firmware analysis; a USB listing in public captures and reports agrees
    usb_ids=frozenset({(USB_VENDOR, 0x0002)}),
    ble_names=("CAMEO",),            # its BLE name starts 'CAMEO 5 ALPHA PLUS-'
)

# ESC 15 codes (Studio 5.0.556 machine data for the CAMEO 5 generation; the reply format is the
# CAMEO 4's, firmware analysis). Codes 0..7 mean what they mean on the CAMEO 4, and code 8 is
# the Foil Tool in either holder; the names are the ones OpenCutter shows for the same tools on
# the CAMEO 4. Codes 9 and above are left out, so they fail closed as unknown tools: 11, 17
# and 18 (both holders) and 15 (holder 1 only) stand for newer tools whose names and motion
# were never checked, and the others are reserved (the same table as the CAMEO 5's). The
# Rotary Blade and the Punch Tool are refused, as on the CAMEO 4.
_SHARED_TOOLS = (
    Tool(0, "No Tool Detected", usable=False),
    Tool(1, "Ratchet Blade", blade=True),
    Tool(3, "Deep-Cut Blade", blade=True),
    Tool(5, "Rotary Blade", blade=True, usable=False, action="Rotary Cut"),
    Tool(6, "Punch Tool", usable=False, action="Punch"),
    Tool(7, "Sketch Pen", action="Sketch"),
    Tool(8, "Foil Tool", action="Foil"),
)


def _sorted(*tools: Tool) -> tuple[Tool, ...]:
    return tuple(sorted(tools, key=lambda t: t.code))


HOLDERS = (
    # Tool 1, the left holder: the AutoBlade sets its depth (TF) here, with the CAMEO 4's rule
    # (firmware analysis).
    Holder(1, "Tool 1", _sorted(*_SHARED_TOOLS,
                                Tool(2, "AutoBlade", blade=True, depth=True),
                                Tool(4, "Kraft Blade, 2mm", blade=True, action="Kraft Cut"))),
    # Tool 2, the right holder: TF does not act here.
    Holder(2, "Tool 2", _sorted(*_SHARED_TOOLS,
                                Tool(2, "AutoBlade", blade=True),
                                Tool(4, "Kraft Blade, 3mm", blade=True, action="Kraft Cut"))),
)

# Studio 5.0.556 machine data intersected with the firmware analysis of V1.04. Passes is a UI
# rule. Whether one force step presses as hard as on the CAMEO 4 is not known, so CAMEO 4
# settings are not reused for these models.
RANGES = Ranges(
    speed=IntRange(1, 30),           # cm/s, whole numbers; the firmware also takes tenths
    force=IntRange(1, 40),           # the firmware clamps FX to 1..40; Studio says 1..40
    acceleration=IntRange(1, 10),    # TJ 1..10; the firmware ignores TJ0
    offset=IntRange(0, 99),          # FC ignores an offset of 100 or more
    blade_depth=IntRange(0, 10),     # 0.1 mm per step; the CAMEO 4's TF rule
    overcut=IntRange(0, 9),          # the CAMEO 4's FF handler: 10 or more drops the command
    passes=IntRange(1, 10),
)

# Two holders: J<n> first, per-holder forms, one TJ for both (it sets every condition),
# two-digit ESC 15 codes in the order holder 1, holder 2 (firmware analysis). expand_mode is
# False on purpose: Studio's data lists expand mode for both models, but FM belongs only after
# TG9, which neither accepts (see the module docstring), so FM can never be sent.
DIALECT = Dialect(holders=HolderForm.DUAL, accel=AccelForm.SHARED,
                  tool_reply=ToolReply.TWO_DIGITS, tool_slots=(0, 1), expand_mode=False)

MAT_12X12 = Mat(
    key="12x12", label="12 × 12 in", tg=1,
    size_mm=(304.8, 304.8),          # the 12-inch mat; the firmware window is 305 mm
    area_su=(30, 30, 6066, 6066),    # the CAMEO 4's area: the mat minus 30 units on each side
    expected_u=(6100, 6100),         # the TG1 window, 305 x 305 mm
    u_basis=UBasis.INFERRED,
    basis="firmware analysis: the TG1 window entry and the U handler are the CAMEO 4's, which "
          "answers 6100,6100 after TG1 (measured on V5.05)",
    grid_in=(12, 12),
    feed_mm=16.5,                    # firmware analysis: the TG1 entry feeds 16.5 mm
)

MAT_12X24 = Mat(
    key="12x24", label="12 × 24 in", tg=2,
    size_mm=(304.8, 609.6),
    area_su=(30, 30, 12162, 6066),   # the same rule: the mat minus 30 units on each side
    expected_u=(12200, 6100),        # the TG2 window, 610 mm of feed by 305 mm
    u_basis=UBasis.INFERRED,
    basis="firmware analysis: the TG2 window entry is the CAMEO 4's; the area follows the "
          "12 x 12 in rule; never measured on any machine",
    grid_in=(12, 24),
    feed_mm=16.5,                    # firmware analysis: the TG2 entry feeds 16.5 mm
)

# ESC 05 replies (firmware analysis: the builder is the CAMEO 4's, instruction for
# instruction). '5' is the firmware's own: an earlier write was refused. Studio's list also has
# '6' (heating) and values of its own that this firmware does not send; any unlisted reply
# aborts the step.
STATUS = StatusModel(texts=(("0", "Ready"), ("1", "Busy"), ("2", "Load Media"), ("3", "Paused"),
                            ("4", "Cancelling"), ("5", "Rejected")))

# Read-only queries (firmware analysis). ESC 05 and 15 answer as on the CAMEO 4 (ESC 15 with
# codes of up to two digits); ESC 10 and 16 report other bits here, which OpenCutter only shows.
# FA, G, [ and U have the CAMEO 4's handlers and FQ2 reports the same value; TO is a getter
# Studio reads on this generation (its reply was never seen). Left out: FQ0, which answers in
# tenths here, and TB71, TI and TC, whose handlers differ and whose replies were never seen.
QUERIES = Queries(
    esc=frozenset({0x05, 0x10, 0x15, 0x16}),
    text=frozenset({b"FG", b"FA", b"FQ2", b"TO", b"G", b"[", b"U"}),
    max_write=20,
)

# ESC 09 twice, 0.3 s apart: the firmware has the CAMEO 4's stop handling and its latch, which
# can make the first ESC 09 only clear itself (firmware analysis).
STOP = StopSequence(writes=(b"\x1b\x09", b"\x1b\x09"), gap_s=0.3)

CAMEO5_ALPHA = MachineProfile(
    id="cameo5_alpha",
    name="CAMEO 5 Alpha",
    support=Support.UNTESTED,
    identity=IDENTITY_ALPHA,
    holders=HOLDERS,
    ranges=RANGES,
    dialect=DIALECT,
    mats=(MAT_12X12, MAT_12X24),     # both variants accept TG1 and TG2
    default_mat="12x12",             # Studio's default mat for the Alpha
    status=STATUS,
    queries=QUERIES,
    stop=STOP,
    basis="firmware analysis of the V1.04 image and Studio 5.0.556 machine data; not yet run",
    unit_mm=0.05,                    # Studio's plotter unit; the TG1 window is 6100 units
    max_write=182,                   # the longest write Studio sends; the firmware's cap is 480
    batch_bytes=1024,                # the CAMEO 4's value: the BLE data path is the same code
                                     # (firmware analysis); the input queue was not measured
    max_coordinate_su=99_999,
    lowering_prefixes=(b"D", b"TF"),  # TF cycles the AutoBlade down and up at its station
    fg_claims_session=True,          # the BLE data path that claims the session is the CAMEO 4's
                                     # code; at worst the status is read once more
    power_cycle_to_connect=True,     # not observed on these models: the safe advice to switch
                                     # it off and on (texts only)
    programs=frozenset(),            # no pinned test programs yet
)

# The Alpha Plus differs only in how it names itself. Its own mat, 15 x 15 in (TG8, Studio's
# default for it), is not offered yet, so its default is the 12 x 12 in mat too.
CAMEO5_ALPHA_PLUS = replace(CAMEO5_ALPHA, id="cameo5_alpha_plus", name="CAMEO 5 Alpha Plus",
                            identity=IDENTITY_ALPHA_PLUS)

PROFILES: tuple = (CAMEO5_ALPHA, CAMEO5_ALPHA_PLUS)
