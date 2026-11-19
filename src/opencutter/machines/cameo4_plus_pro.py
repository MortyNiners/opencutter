# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The CAMEO 4 Plus and the CAMEO 4 Pro: the CAMEO 4's firmware with another model variant.

Both UNTESTED (jobs allowed): no Plus or Pro has run OpenCutter yet. They run the CAMEO 4's
firmware image, on both of its lines (V5 and V1). A model variant stored in the machine changes
only the FG model text ('CAMEO 4 PLUS', 'CAMEO 4 PRO'), the BLE name, the USB product id, the
TG values the machine accepts (the Plus up to TG8, the Pro up to TG9; any other value silently
becomes TG0, no mat), the carriage reach and, on the Pro only, where the carriage homes and
where the AutoBlade sets its depth, both 3 mm further out (firmware analysis; the bytes sent
for TF stay the same). The command handlers, the ESC 15 reply, the ESC 05 digits and the stop
are the same code as on the CAMEO 4, and Studio 5.0.556's machine data gives the three models
the same commands and ranges, except expand mode (FM), which only the Pro has. So the holders,
tools, ranges, status, queries and stop below are the CAMEO 4's own values, imported from
cameo4.py, and a 12 x 12 in job sends the CAMEO 4's bytes.

Mats
  Plus  15 x 15 in (TG8, the default) and 12 x 12 in (TG1).
  Pro   24 x 24 in (TG9, the default) and 12 x 12 in (TG1).
Every expected U is INFERRED, so it is enforced: a U reply that differs refuses the job before
the cut. TG1's was measured on a CAMEO 4, whose TG1 window is the same; TG8's and TG9's are the
firmware's window for that mat, the feed axis first as on the CAMEO 4.

Expand mode (FM): the Pro's 24 x 24 in header is TG9 FN0 TB50,0 FM1, the order of Studio's own
24 x 24 in job in the one capture of it (public captures and reports: a CAMEO 4 Pro). After TG9
the carriage window is 590 mm; FM1 widens it to 610 mm, which the cut area needs, and moves the
frame 10 mm toward the carriage home (firmware analysis). The Plus never gets FM
(safety.NO_EXPAND_MODELS), and neither does a 12 x 12 in mat.

Left out (not offered) until someone measures them on a Plus or a Pro:
  * the 12 x 24 in (TG2) and 8 x 12 in (TG3) mats, as on the CAMEO 4, and the Pro's
    15 x 15 in mat (TG8);
  * the 6 x 8 in mat: which TG it takes is not known;
  * the PixScan mat (TG4): its cut area comes from a scan, which was not traced;
  * media without a mat (TG0): a profile cannot describe it yet. Without a mat the Plus
    reaches 371 mm across and the Pro 600 mm (firmware analysis).
"""
from __future__ import annotations

import dataclasses

from . import cameo4
from .profile import (Dialect, FirmwareLine, HeaderExtra, Identity, MachineProfile, Mat, Support,
                      UBasis)

# Firmware lines: the N of "VN.dd" in FG. The same two lines as the CAMEO 4, neither run here.
V5 = FirmwareLine(5, Support.UNTESTED, "firmware analysis: the CAMEO 4's V5.05 image, one copy "
                                        "of code for every model variant; not run on a Plus or "
                                        "a Pro")
V1 = FirmwareLine(1, Support.UNTESTED, "firmware analysis: the CAMEO 4's V1.20 image, the same "
                                        "command handlers as V5.05; not run on a Plus or a Pro")

PLUS_IDENTITY = Identity(
    fg_models=frozenset({"CAMEO 4 PLUS"}),     # firmware analysis: 'CAMEO 4 PLUS V5.05' + 4 spaces
    lines=(V5, V1),
    usb_ids=frozenset({(0x0B4D, 0x1138)}),     # firmware analysis and Studio 5.0.556 machine data
    ble_names=("CAMEO",),                      # firmware analysis: named 'CAMEO 4 PLUS-<serial>'
)

PRO_IDENTITY = Identity(
    fg_models=frozenset({"CAMEO 4 PRO"}),      # firmware analysis: 'CAMEO 4 PRO V5.05' + 4 spaces
    lines=(V5, V1),
    usb_ids=frozenset({(0x0B4D, 0x1139)}),     # firmware analysis and Studio 5.0.556 machine data
    ble_names=("CAMEO",),                      # firmware analysis: named 'CAMEO 4 PRO-<serial>'
)

# The Plus writes exactly the CAMEO 4's dialect: its machine data defines no expand mode.
PLUS_DIALECT = cameo4.DIALECT
# The Pro adds expand mode: Studio 5.0.556's machine data defines it for the Pro alone.
PRO_DIALECT: Dialect = dataclasses.replace(cameo4.DIALECT, expand_mode=True)

# 12 x 12 in: the CAMEO 4's mat, values unchanged. TG1 has the same window, feed and offset on
# every variant (firmware analysis), so the U measured on a CAMEO 4 is inferred here. Where the
# mat sits against the wider machines' guides was not observed.
MAT_12X12: Mat = dataclasses.replace(
    cameo4.MAT_12X12,
    u_basis=UBasis.INFERRED,
    basis="measured on a CAMEO 4 (V5.05): U 6100,6100 after TG1, and cuts in place; firmware "
          "analysis: the Plus and the Pro have the same TG1 window",
)

# 15 x 15 in, the Plus's own mat (TG8: the CAMEO 4 turns it into TG0).
MAT_15X15 = Mat(
    key="15x15", label="15 × 15 in", tg=8,
    size_mm=(381.0, 381.0),
    area_su=(30, 9, 7590, 7320),     # the area Studio sent with this mat (public captures and
                                     # reports: Studio 4.4 on a CAMEO 4 Pro); inside the TG8
                                     # window
    expected_u=(7620, 7420),         # firmware analysis: the TG8 window, 381 mm along the feed by
                                     # 371 mm across, with no side offset
    u_basis=UBasis.INFERRED,
    basis="firmware analysis: TG8 window 7620 x 7420 units, the same code on the Plus and the "
          "Pro; cut area as Studio sent it with this mat (public captures and reports)",
    grid_in=(15, 15),                # drawing only: the mat's printed inch grid
)

# 24 x 24 in, the Pro's own mat (TG9: the CAMEO 4 and the Plus turn it into TG0). FM1, after
# TG9 FN0 TB50,0 as Studio sends it, opens the 610 mm window the cut area needs.
MAT_24X24 = Mat(
    key="24x24", label="24 × 24 in", tg=9,
    size_mm=(609.6, 609.6),
    area_su=(30, 30, 12162, 12162),  # the mat minus 30 units on each side, as Studio sent it with
                                     # FM1 (public captures and reports: Studio 4.4 on a
                                     # CAMEO 4 Pro)
    expected_u=(12200, 12200),       # firmware analysis: after TG9 and FM1 the window is 610 mm
                                     # each way (12200,11800 after TG9 alone)
    u_basis=UBasis.INFERRED,
    basis="firmware analysis: TG9 window 12200 x 11800 units, widened to 12200 x 12200 by FM1; "
          "cut area as Studio sent it with this mat (public captures and reports)",
    header_extra=HeaderExtra.EXPAND,
    grid_in=(24, 24),                # drawing only: the mat's printed inch grid
)

CAMEO4_PLUS = MachineProfile(
    id="cameo4_plus",
    name="CAMEO 4 Plus",
    support=Support.UNTESTED,
    identity=PLUS_IDENTITY,
    holders=cameo4.HOLDERS,          # firmware analysis: same ESC 15 codes and tool handling
    ranges=cameo4.RANGES,            # firmware analysis; Studio 5.0.556 machine data agrees
    dialect=PLUS_DIALECT,
    mats=(MAT_15X15, MAT_12X12),
    default_mat="15x15",             # Studio 5.0.556 machine data: the Plus's default mat
    status=cameo4.STATUS,            # firmware analysis: the same ESC 05 replies
    queries=cameo4.QUERIES,          # firmware analysis: the same read-only queries
    stop=cameo4.STOP,                # firmware analysis: the same latch, so ESC 09 twice
    basis="firmware analysis of the CAMEO 4's V5.05 and V1.20 images, which the Plus runs as "
          "its model variant; Studio 5.0.556 machine data; not run on a Plus",
    unit_mm=0.05,                    # the CAMEO 4's: the same firmware step factor
    max_write=182,                   # the CAMEO 4's: the same Bluetooth firmware
    batch_bytes=1024,                # the CAMEO 4's: the same input queue
    max_coordinate_su=99_999,        # the CAMEO 4's: five digits at most
    lowering_prefixes=(b"D", b"TF"),  # TF cycles the AutoBlade down and up at its station
    fg_claims_session=True,          # the CAMEO 4's: FG is the same code
    power_cycle_to_connect=True,     # the CAMEO 4's: the same Bluetooth firmware (texts only)
    programs=frozenset(),            # no pinned test programs for this model yet
)

CAMEO4_PRO = MachineProfile(
    id="cameo4_pro",
    name="CAMEO 4 Pro",
    support=Support.UNTESTED,
    identity=PRO_IDENTITY,
    holders=cameo4.HOLDERS,          # firmware analysis: same ESC 15 codes and tool handling;
                                     # only the AutoBlade station sits 3 mm further out
    ranges=cameo4.RANGES,            # firmware analysis; Studio 5.0.556 machine data agrees
    dialect=PRO_DIALECT,
    mats=(MAT_24X24, MAT_12X12),
    default_mat="24x24",             # Studio 5.0.556 machine data: the Pro's default mat
    status=cameo4.STATUS,            # firmware analysis: the same ESC 05 replies
    queries=cameo4.QUERIES,          # firmware analysis: the same read-only queries
    stop=cameo4.STOP,                # firmware analysis: the same latch, so ESC 09 twice
    basis="firmware analysis of the CAMEO 4's V5.05 and V1.20 images, which the Pro runs as "
          "its model variant; Studio 5.0.556 machine data; not run on a Pro",
    unit_mm=0.05,                    # the CAMEO 4's: the same firmware step factor
    max_write=182,                   # the CAMEO 4's: the same Bluetooth firmware
    batch_bytes=1024,                # the CAMEO 4's: the same input queue
    max_coordinate_su=99_999,        # the CAMEO 4's: five digits at most
    lowering_prefixes=(b"D", b"TF"),  # TF cycles the AutoBlade down and up at its station
    fg_claims_session=True,          # the CAMEO 4's: FG is the same code
    power_cycle_to_connect=True,     # the CAMEO 4's: the same Bluetooth firmware (texts only)
    programs=frozenset(),            # no pinned test programs for this model yet
)

PROFILES: tuple = (CAMEO4_PLUS, CAMEO4_PRO)
