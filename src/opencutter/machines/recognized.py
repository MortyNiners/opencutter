# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Models OpenCutter names but cannot drive yet (RecognizedModel entries, refused).

They get ESC 05 and FG only; the link is released and the app names the model with the
reason in plain words: "OpenCutter recognizes this CAMEO 3 but cannot drive it yet: there is no
known way to stop it from the computer, and it does not report which tools are loaded." The
models: the CURIO 2 and the CURIO 1; the CAMEO 3, CAMEO 1 and CAMEO 2; the Portrait 1 and
Portrait 2; the Silhouette SD, CC300 and CC300L; the CC100 and the CC200.

Every entry is identity only. It has no ranges, mats, holders, status digits, queries or stop,
so nothing but ESC 05 and FG can reach one, and never FM. It lists no firmware line either:
every firmware of a recognized model is refused alike.

How each one is named
  * Its own FG model text, only where a reply has been seen or the firmware builds it: the
    CAMEO 3, the Portrait 2 and the CURIO 2. The text is matched exactly and case-sensitively
    with detect.py's parse, never by prefix or as part of a longer text: 'CURIO 2' is never
    read as a 'CURIO'.
  * A text that several models may report is shared (Identity.shared_fg_models) and names a
    model only together with its USB id. Alone it is UNKNOWN. 'CAMEO' is listed for the CAMEO 1;
    a Pro MK-II whose variant byte is 0 sends it too but does not claim it (cameo5.py), so with
    the MK-II's USB id the reply stays UNKNOWN. 'Silhouette' is listed for the Portrait 1 and,
    as a hypothesis, for the CC200.
  * The FG text of the CAMEO 2, the CURIO 1, the Silhouette SD, the CC300, the CC300L and the
    CC100 has never been seen, and none is guessed here. They carry their USB ids only;
    detect() never names a model from a USB id alone, so a future USB discovery is what will
    name them.
  * BLE name prefixes only for the models with Bluetooth. Each is the family word discovery
    looks for ('CAMEO', 'Portrait', 'CURIO'), with the exact advertised name in a comment. The
    CAMEO 3 and Portrait 2 adapters write the model without its space ('CAMEO3-',
    'Portrait2-'), unlike FG ('CAMEO 3', 'Portrait 2'). The names are discovery hints only; FG
    decides.
  * USB ids (vendor 0x0B4D, which Graphtec's own cutters use too) from Studio 5.0.556 machine
    data, with the firmware's own device descriptor where a firmware is bundled (the CURIO 2).
"""
from __future__ import annotations

from .profile import Identity, RecognizedModel

SILHOUETTE_USB = 0x0B4D          # Studio 5.0.556 machine data: the vendor id of every model here

# Why each one is refused: the part after "cannot drive it yet:", in plain words.
NO_STOP_NO_TOOL_CHECK = ("there is no known way to stop it from the computer, and it does not "
                         "report which tools are loaded")
# USB_ONLY is true only while OpenCutter has no USB connection: these models have no Bluetooth,
# so today the message cannot be reached. The USB package must reword it (the missing stop stays).
USB_ONLY = ("it needs a USB connection, which OpenCutter does not have, and there is no known way "
            "to stop it from the computer")
CURIO2_REASON = ("the work area of its trays, its tool height and the material thickness are not "
                 "known, and there is no way to confirm the work area before a cut")

# --- CURIO -----------------------------------------------------------------------------------
# A flatbed with trays and a Z axis. Studio selects no mat for it and TG does nothing on V1.16,
# so the U reply only echoes an earlier area and cannot confirm the work area. The tray sizes,
# the units of the tool height and pen-up height commands and how to use the thickness probe
# (which moves the head) are not settled (firmware analysis of V1.16; Studio 5.0.556 machine
# data). The firmware analysis does show a stop (ESC 09 once, with no latch); that alone is not
# enough to drive it.
CURIO2 = RecognizedModel(
    id="curio2",
    name="CURIO 2",
    identity=Identity(
        fg_models=frozenset({"CURIO 2"}),       # firmware analysis of V1.16: 'CURIO 2 V1.16'
        usb_ids=frozenset({(SILHOUETTE_USB, 0x113D)}),   # firmware analysis: USB descriptor
        ble_names=("CURIO",),                   # firmware analysis: named 'CURIO 2-<serial>'
    ),
    reason=CURIO2_REASON,
    basis="firmware analysis of V1.16 and Studio 5.0.556 machine data; never seen on a machine",
)

# USB only (Studio has no Bluetooth data for it). No firmware is bundled, Studio has no cancel
# for it, and Studio and the one published driver (public captures and reports) disagree on its
# commands.
CURIO1 = RecognizedModel(
    id="curio1",
    name="CURIO 1",
    identity=Identity(
        fg_models=frozenset(),                  # never seen; 'CURIO' is not guessed
        usb_ids=frozenset({(SILHOUETTE_USB, 0x112C)}),   # Studio 5.0.556 machine data
    ),
    reason=USB_ONLY,
    basis="Studio 5.0.556 machine data; public captures and reports: one published driver; no "
          "firmware; never seen",
)

# --- CAMEO 1, 2, 3 ---------------------------------------------------------------------------
# Two holders, with commands close to the CAMEO 4's (public captures and reports: USB captures
# of Studio driving one on V1.60). But Studio has no tool check (ESC 15) and no cancel for it,
# the published drivers send no stop (public captures and reports), and no firmware is bundled
# to show what ESC 09 does.
CAMEO3 = RecognizedModel(
    id="cameo3",
    name="CAMEO 3",
    identity=Identity(
        # public captures and reports: USB captures show 'CAMEO 3 V1.60' + 4 spaces
        fg_models=frozenset({"CAMEO 3"}),
        usb_ids=frozenset({(SILHOUETTE_USB, 0x112F)}),   # Studio 5.0.556 machine data
        # its adapter: 'CAMEO3-<serial>' (public captures and reports)
        ble_names=("CAMEO",),
    ),
    reason=NO_STOP_NO_TOOL_CHECK,
    basis="Studio 5.0.556 machine data; public captures and reports: USB captures of V1.60; no "
          "firmware bundled",
)

# One holder, USB only. No mat command, so OpenCutter's U check has no counterpart; no cancel
# in Studio's data and none in the published drivers (public captures and reports); no firmware
# is bundled.
CAMEO1 = RecognizedModel(
    id="cameo1",
    name="CAMEO 1",
    identity=Identity(
        fg_models=frozenset(),
        usb_ids=frozenset({(SILHOUETTE_USB, 0x1121)}),   # Studio 5.0.556 machine data
        # Public captures and reports: one sample, 'CAMEO V1.10' + 4 spaces (the unit is not
        # named). A Pro MK-II whose variant byte is 0 sends the same text (firmware analysis):
        # only this USB id names a CAMEO 1. The MK-II must not share the text: it would make
        # that reply drivable.
        shared_fg_models=frozenset({"CAMEO"}),
    ),
    reason=USB_ONLY,
    basis="Studio 5.0.556 machine data; public captures and reports: one FG sample; no firmware "
          "bundled",
)

# As the CAMEO 1. Its FG text has never been seen ('CAMEO 2' is only an analogy, not used).
CAMEO2 = RecognizedModel(
    id="cameo2",
    name="CAMEO 2",
    identity=Identity(
        fg_models=frozenset(),
        usb_ids=frozenset({(SILHOUETTE_USB, 0x112B)}),   # Studio 5.0.556 machine data
    ),
    reason=USB_ONLY,
    basis="Studio 5.0.556 machine data; no firmware bundled; never seen",
)

# --- Portrait 1, 2 ---------------------------------------------------------------------------
# One holder, USB only, no mat command, no cancel in Studio's data; no firmware is bundled.
PORTRAIT1 = RecognizedModel(
    id="portrait1",
    name="Portrait 1",
    identity=Identity(
        fg_models=frozenset(),
        usb_ids=frozenset({(SILHOUETTE_USB, 0x1123)}),   # Studio 5.0.556 machine data
        # Public captures and reports: one capture of an older Studio driving one, 'Silhouette
        # V1.10' + 4 spaces, not its model name. Studio also calls the CC200 'Silhouette': only
        # the USB id settles it.
        shared_fg_models=frozenset({"Silhouette"}),
    ),
    reason=USB_ONLY,
    basis="Studio 5.0.556 machine data; public captures and reports: one capture of an older "
          "Studio; no firmware bundled",
)

# One holder, a removable Bluetooth adapter. Studio has no tool check and no cancel for it, no
# firmware is bundled, and its mat windows and U replies are not known.
PORTRAIT2 = RecognizedModel(
    id="portrait2",
    name="Portrait 2",
    identity=Identity(
        # Public captures and reports: one report over Bluetooth, 'Portrait 2 V1.20' + 4 spaces.
        fg_models=frozenset({"Portrait 2"}),
        usb_ids=frozenset({(SILHOUETTE_USB, 0x1132)}),   # Studio 5.0.556 machine data
        # its adapter: 'Portrait2-<serial>' (public captures and reports)
        ble_names=("Portrait",),
    ),
    reason=NO_STOP_NO_TOOL_CHECK,
    basis="Studio 5.0.556 machine data; public captures and reports: Bluetooth reports of V1.20; "
          "no firmware bundled",
)

# --- Silhouette SD, CC300, CC300L, CC100, CC200 ----------------------------------------------
# One holder each, USB only, no mat command, no cancel in Studio's data, no firmware bundled,
# and no FG reply of any of them has been seen.
SILHOUETTE_SD = RecognizedModel(
    id="silhouette_sd",
    name="Silhouette SD",
    identity=Identity(
        fg_models=frozenset(),
        # Studio 5.0.556 machine data: two variants (CC330 and CC330L), both shown as this name.
        usb_ids=frozenset({(SILHOUETTE_USB, 0x111C), (SILHOUETTE_USB, 0x111D)}),
    ),
    reason=USB_ONLY,
    basis="Studio 5.0.556 machine data; no firmware bundled; never seen",
)

CC300 = RecognizedModel(
    id="cc300",
    name="CC300",
    identity=Identity(
        fg_models=frozenset(),
        usb_ids=frozenset({(SILHOUETTE_USB, 0x111A)}),   # Studio 5.0.556 machine data
    ),
    reason=USB_ONLY,
    basis="Studio 5.0.556 machine data; no firmware bundled; never seen",
)

CC300L = RecognizedModel(
    id="cc300l",
    name="CC300L",
    identity=Identity(
        fg_models=frozenset(),
        usb_ids=frozenset({(SILHOUETTE_USB, 0x111B)}),   # Studio 5.0.556 machine data
    ),
    reason=USB_ONLY,
    basis="Studio 5.0.556 machine data; no firmware bundled; never seen",
)

# Kept by Studio as a legacy entry only.
CC100 = RecognizedModel(
    id="cc100",
    name="CC100",
    identity=Identity(
        fg_models=frozenset(),
        # Studio 5.0.556 machine data. The same id belongs to a Graphtec cutter (CE5000-40) that
        # OpenCutter leaves out, so this id alone must never name a CC100.
        usb_ids=frozenset({(SILHOUETTE_USB, 0x1115)}),
    ),
    reason=USB_ONLY,
    basis="Studio 5.0.556 machine data (a legacy entry); no firmware bundled; never seen",
)

# Kept by Studio as a legacy entry only.
CC200 = RecognizedModel(
    id="cc200",
    name="CC200",
    identity=Identity(
        fg_models=frozenset(),
        usb_ids=frozenset({(SILHOUETTE_USB, 0x110A)}),   # Studio 5.0.556 machine data
        # Studio's own name for it is 'Silhouette', the text a Portrait 1 reports. Whether a CC200
        # answers FG with it is not known: it names a CC200 only together with this USB id.
        shared_fg_models=frozenset({"Silhouette"}),
    ),
    reason=USB_ONLY,
    basis="Studio 5.0.556 machine data (a legacy entry); no firmware bundled; never seen",
)

PROFILES: tuple = (CURIO2, CURIO1, CAMEO3, CAMEO1, CAMEO2, PORTRAIT1, PORTRAIT2, SILHOUETTE_SD,
                   CC300, CC300L, CC100, CC200)
