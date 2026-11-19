# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The CAMEO 5, the CAMEO 5 Plus and the CAMEO Pro MK-II.

All three are UNTESTED (jobs allowed): ported from Studio 5.0.556's machine data and the
firmware analysis (the cut areas of the 15 x 15 and 24 x 24 in mats: public captures and
reports), never run. The CAMEO 5 and 5 Plus share one firmware image (V1.07), and a
variant setting in the machine picks the model; the Pro MK-II has its own image (V1.10). Every
command OpenCutter sends to a CAMEO 4 has a handler with the same meaning in both images (some
handlers were compared only at their top level), so the three profiles keep the CAMEO 4's
command forms. What differs:

  * FG names the model exactly: 'CAMEO 5', 'CAMEO 5 PLUS' or 'CAMEO PRO MARK2', then the
    version. 'CAMEO 5 PRO' (a variant the V1.07 image has but no known product uses) matches
    no entry and stays UNKNOWN. An MK-II only says 'CAMEO PRO MARK2' with the variant setting
    that also accepts its 24 x 24 in mat; other settings make it report a bare 'CAMEO', which a
    CAMEO 1 reports too, so the MK-II does not claim that text.
  * The ESC 15 reply has codes of up to two digits.
  * FX takes 1..40 (the CAMEO 4: 1..33).
  * Speeds are whole numbers. Both images also take tenths, which OpenCutter never sends, and
    the tool-up speed (APS) is never sent: '!' resets it to 30 cm/s.
  * Each model takes its own TG values; a value it does not take silently selects the roll
    window, so only those mats are listed. A mat is listed only when its TG value, window and
    cut area are known: the electrostatic mats, the 8.5 x 12 in mat and the scanning mat are
    left out until their cut areas are known.
  * FM1 for the MK-II's 24 x 24 in mat only, in the header Studio sends with that mat
    (TG9 FN0 TB50,0 FM1): it widens the carriage window from 590 to 610 mm. The CAMEO 5 and
    5 Plus never get FM (safety.NO_EXPAND_MODELS).

No U reply has been measured on these models: every expected U is inferred from the firmware's
window tables, and enforced, so a reply that differs refuses the job before the cut.
"""
from __future__ import annotations

from .profile import (AccelForm, Dialect, FirmwareLine, HeaderExtra, Holder, HolderForm,
                      Identity, IntRange, MachineProfile, Mat, Queries, Ranges, StatusModel,
                      StopSequence, Support, Tool, ToolReply, UBasis)

# Firmware lines: the N of "VN.dd" in FG. One line per image; an unlisted major number is
# RECOGNIZED (refused with its reason).
C5_V1 = FirmwareLine(1, Support.UNTESTED, "firmware analysis of V1.07, the image the CAMEO 5 and "
                                          "5 Plus share; no machine has been run")
MK2_V1 = FirmwareLine(1, Support.UNTESTED, "firmware analysis of V1.10, the Pro MK-II's own "
                                           "image; no machine has been run")

# Identity (firmware analysis: the FG, USB descriptor and BLE name builders of each image). FG
# is the model text, a space, 'V<major>.<minor>' and four spaces. The BLE name is the FG model
# text, '-' and the serial; discovery only filters on "CAMEO", FG decides.
IDENTITY_CAMEO5 = Identity(
    fg_models=frozenset({"CAMEO 5"}),          # firmware analysis: no variant suffix
    lines=(C5_V1,),
    usb_ids=frozenset({(0x0B4D, 0x1140)}),     # firmware analysis: first USB descriptor
    ble_names=("CAMEO",),
)
IDENTITY_CAMEO5_PLUS = Identity(
    fg_models=frozenset({"CAMEO 5 PLUS"}),     # firmware analysis: suffix ' PLUS', upper case
    lines=(C5_V1,),
    usb_ids=frozenset({(0x0B4D, 0x1141)}),     # firmware analysis: second USB descriptor
    ble_names=("CAMEO",),
)
IDENTITY_PRO_MK2 = Identity(
    fg_models=frozenset({"CAMEO PRO MARK2"}),  # firmware analysis: 'CAMEO' + ' PRO MARK2'
    lines=(MK2_V1,),
    usb_ids=frozenset({(0x0B4D, 0x1146)}),     # firmware analysis: the image's only descriptor
    ble_names=("CAMEO",),
)

# ESC 15 codes (Studio 5.0.556 machine data, the same table for all three models; the names are
# the ones OpenCutter shows for the same tools on the CAMEO 4). Holder 1 reports the Foil Tool
# too on this generation. Codes 9 and above are left out, so they fail closed as unknown tools:
# 11, 17 and 18 (both holders) and 15 (holder 1 only) name newer tools that OpenCutter has never
# seen report, and the others are reserved or not listed for that holder. The Rotary Blade and
# the Punch Tool are refused, as on the CAMEO 4.
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
    # Tool 1, the left holder: TF sets the AutoBlade's depth here, only with ',1' and 0..10
    # (firmware analysis of both images).
    Holder(1, "Tool 1", _sorted(*_SHARED_TOOLS,
                                Tool(2, "AutoBlade", blade=True, depth=True),
                                Tool(4, "Kraft Blade, 2mm", blade=True, action="Kraft Cut"))),
    # Tool 2, the right holder: no Blade Depth, as on the CAMEO 4.
    Holder(2, "Tool 2", _sorted(*_SHARED_TOOLS,
                                Tool(2, "AutoBlade", blade=True),
                                Tool(4, "Kraft Blade, 3mm", blade=True, action="Kraft Cut"))),
)

# Ranges: Studio 5.0.556's range intersected with what both images accept (firmware analysis of
# V1.07 and V1.10). Passes is a UI rule.
RANGES = Ranges(
    speed=IntRange(1, 30),           # cm/s, whole numbers; both sides also take tenths (unused)
    force=IntRange(1, 40),           # Studio's range; the firmware clamps FX to 1..40
    acceleration=IntRange(1, 10),    # both sides 1..10; TJ0 is ignored
    offset=IntRange(0, 99),          # FC: both sides 0..99
    blade_depth=IntRange(0, 10),     # TF, 0.1 mm per step: both sides 0..10
    overcut=IntRange(0, 9),          # FF: 10 or more drops the whole command (firmware)
    passes=IntRange(1, 10),
)

# Two holders: J<n> first, per-holder forms, one TJ for both, two-digit ESC 15 codes in the
# order holder 1, holder 2 (firmware analysis: both images print the two codes as plain decimal
# numbers, so codes from 10 up take two digits). Speed is written as a whole number and APS is
# never sent: '!' then resets the tool-up speed to 30 cm/s (on V1.07 to 10 cm/s, with the speed
# capped at 10, while a state of unknown meaning is set; this only slows the machine down).
DIALECT = Dialect(holders=HolderForm.DUAL, accel=AccelForm.SHARED,
                  tool_reply=ToolReply.TWO_DIGITS, tool_slots=(0, 1))
# The Pro MK-II's machine data defines expand mode (FM); the CAMEO 5 and 5 Plus have none, even
# though V1.07 has the handler.
DIALECT_EXPAND = Dialect(holders=HolderForm.DUAL, accel=AccelForm.SHARED,
                         tool_reply=ToolReply.TWO_DIGITS, tool_slots=(0, 1), expand_mode=True)

# Mats. The TG windows are the firmware's tables (feed x carriage; the same entries in both
# images and on the CAMEO 4). The expected U is that window in plotter units, as the CAMEO 4's
# TG1 window gave U 6100,6100 on V5.05: INFERRED, never measured on these models. Unless noted,
# the cut area keeps 30 units inside the mat on each side, as Studio's own jobs do.
MAT_12X12 = Mat(
    key="12x12", label="12 × 12 in", tg=1,
    size_mm=(304.8, 304.8),
    area_su=(30, 30, 6066, 6066),    # the area cut on the CAMEO 4, whose TG1 window is the same
    expected_u=(6100, 6100),         # window 305 x 305 mm
    u_basis=UBasis.INFERRED,
    basis="firmware analysis: the TG1 window equals the CAMEO 4's (305 x 305 mm), which "
          "measured U 6100,6100 on V5.05",
    grid_in=(12, 12),
)
MAT_12X24 = Mat(
    key="12x24", label="12 × 24 in", tg=2,
    size_mm=(304.8, 609.6),
    area_su=(30, 30, 12162, 6066),
    expected_u=(12200, 6100),        # window 610 x 305 mm
    u_basis=UBasis.INFERRED,
    basis="firmware analysis: TG2 window 610 x 305 mm; never measured",
    grid_in=(12, 24),
)
MAT_15X15 = Mat(
    key="15x15", label="15 × 15 in", tg=8,
    size_mm=(381.0, 381.0),
    area_su=(30, 9, 7590, 7320),     # as Studio sent it for this mat (public captures and
                                     # reports: a CAMEO 4 Pro, whose TG8 window is the same)
    expected_u=(7620, 7420),         # window 381 x 371 mm, with no carriage offset
    u_basis=UBasis.INFERRED,
    basis="firmware analysis: TG8 window 381 x 371 mm; public captures and reports: the area "
          "Studio sent with this mat; never measured",
    grid_in=(15, 15),
)
MAT_8X12 = Mat(
    key="8x12", label="8 × 12 in", tg=3,
    size_mm=(203.2, 304.8),
    area_su=(30, 30, 6066, 4034),
    expected_u=(6100, 4120),         # window 305 x 206 mm, with no carriage offset
    u_basis=UBasis.INFERRED,
    basis="firmware analysis: TG3 window 305 x 206 mm; never measured",
    grid_in=(8, 12),
)
MAT_24X24 = Mat(
    key="24x24", label="24 × 24 in", tg=9,
    size_mm=(609.6, 609.6),
    area_su=(30, 30, 12162, 12162),  # as Studio sent it after TG9 and FM1 (public captures and
                                     # reports: a CAMEO 4 Pro, the same TG9 and FM handlers)
    expected_u=(12200, 12200),       # window 610 x 610 mm once FM1 has widened it
    u_basis=UBasis.INFERRED,
    basis="firmware analysis: TG9 opens 610 x 590 mm and FM1 (after TG9 FN0 TB50,0, as Studio "
          "sends it) widens the carriage window to 610 mm; whether U reports the window after "
          "FM1 was not traced",
    header_extra=HeaderExtra.EXPAND,
    grid_in=(24, 24),
)

# ESC 05 replies (firmware analysis: the status builder is the CAMEO 4's, instruction for
# instruction). Studio's list adds '6' (heating); no image was shown to send it, so it is left
# out and aborts the step like any other unknown reply.
STATUS = StatusModel(texts=(("0", "Ready"), ("1", "Busy"), ("2", "Load Media"), ("3", "Paused"),
                            ("4", "Cancelling"), ("5", "Rejected")))

# Read-only queries: the CAMEO 4's set. Each is one of Studio's getters for this generation
# (ESC 10 is answered by the BLE core, as on the CAMEO 4), and every handler is present in both
# images (firmware analysis). FQ0 reports the speed in tenths on V1.07.
QUERIES = Queries(
    esc=frozenset({0x05, 0x10, 0x15, 0x16}),
    text=frozenset({b"FG", b"TB71", b"FA", b"FQ0", b"FQ2", b"TI", b"TO", b"TC", b"G", b"[",
                    b"U"}),
    max_write=20,
)

# ESC 09 twice, 0.3 s apart: both images have the CAMEO 4's latch, which makes the first ESC 09
# only clear itself (firmware analysis).
STOP = StopSequence(writes=(b"\x1b\x09", b"\x1b\x09"), gap_s=0.3)

_BASIS = "Studio 5.0.556 machine data and firmware analysis of "


def _profile(*, profile_id: str, name: str, identity: Identity, dialect: Dialect,
             mats: tuple[Mat, ...], default_mat: str, basis: str) -> MachineProfile:
    """One model of this generation: everything but the identity, dialect and mats is shared."""
    return MachineProfile(
        id=profile_id,
        name=name,
        support=Support.UNTESTED,
        identity=identity,
        holders=HOLDERS,
        ranges=RANGES,
        dialect=dialect,
        mats=mats,
        default_mat=default_mat,     # Studio 5.0.556 machine data: the model's default mat
        status=STATUS,
        queries=QUERIES,
        stop=STOP,
        basis=basis,
        unit_mm=0.05,                # Studio 5.0.556 machine data; the TG tables agree
        max_write=182,               # the write size Studio uses; the firmware's cap is 480
        batch_bytes=1024,            # the CAMEO 4's pacing: the BLE data path is the same code
        max_coordinate_su=99_999,
        lowering_prefixes=(b"D", b"TF"),   # TF cycles the AutoBlade down and up at its station
        fg_claims_session=True,      # firmware analysis: the BLE data path is the CAMEO 4's code,
                                     # where the first data write claims the session
        power_cycle_to_connect=True,       # not observed on these models: the safe advice to
                                           # switch it off and on (texts only)
        programs=frozenset(),        # no pinned programs yet
    )


CAMEO5 = _profile(
    profile_id="cameo5", name="CAMEO 5", identity=IDENTITY_CAMEO5, dialect=DIALECT,
    # TG values this model takes: 0, 1, 2, 4-7 and 11-13 (firmware analysis); TG3 (8 x 12 in)
    # selects the roll window, and Studio offers no 8 x 12 in mat for it either.
    mats=(MAT_12X12, MAT_12X24),
    default_mat="12x12",
    basis=_BASIS + "V1.07; never run on this model",
)
CAMEO5_PLUS = _profile(
    profile_id="cameo5_plus", name="CAMEO 5 Plus", identity=IDENTITY_CAMEO5_PLUS, dialect=DIALECT,
    # TG values this model takes: those of the CAMEO 5 and TG8 (firmware analysis).
    mats=(MAT_15X15, MAT_12X12, MAT_12X24),
    default_mat="15x15",
    basis=_BASIS + "V1.07; never run on this model",
)
CAMEO_PRO_MK2 = _profile(
    profile_id="cameo_pro_mk2", name="CAMEO Pro MK-II", identity=IDENTITY_PRO_MK2,
    dialect=DIALECT_EXPAND,
    # TG values this model takes: 0-9 and 11 (firmware analysis, with the variant setting that
    # makes FG report 'CAMEO PRO MARK2'); it has no electrostatic mats.
    mats=(MAT_24X24, MAT_15X15, MAT_12X12, MAT_12X24, MAT_8X12),
    default_mat="24x24",
    basis=_BASIS + "V1.10; never run on this model",
)

PROFILES: tuple = (CAMEO5, CAMEO5_PLUS, CAMEO_PRO_MK2)
