# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Which machine is on the link: the FG reply parsed to a Detection.

Pure Python, no I/O. The daemon sends ESC 05, then FG, on every new link; detect(reply) decides
from that reply alone (never from ESC 0x11, never from the BLE name). Every bundled firmware
builds the reply the same way: the model text, a space, 'V<major>.<minor, two digits>', then
four spaces (a space and a 3-byte field that is blank unless set), then ETX.

The parse:
  1. Strip ETX, NUL and ASCII whitespace at both ends (fg_text: device/protocol.py's cleanup,
     unchanged). Bytes must be ASCII; other replies (None, numbers, lists) name no model.
  2. Match the whole text against '<model> V<major>.<minor>' (major one or two digits with no
     leading zero, minor exactly two, ASCII digits only, case-sensitive 'V'). Whatever follows
     the version must be padding: a set 3-byte field, or anything else after the version, makes
     it UNKNOWN.
     The firmware's own tail is exactly four spaces (Detection.standard_tail: the reply is the
     model text, the version and those four spaces, with at most one final ETX and nothing
     before it). Only the tested machine reads any other padding, as the model gate always
     has: the gate's frozen test corpus requires 'CAMEO 4 V5.05' and 'CAMEO 4 V5.05\\r\\n' to
     pass. A reply that would be driven at the UNTESTED level (an UNTESTED profile, or the
     CAMEO 4 on its UNTESTED V1 line) must have the standard tail; any other is UNKNOWN.
  3. Look the model text up exactly, case-sensitively, never by prefix: 'CAMEO 4' is a prefix
     of 'CAMEO 4 PLUS', and each is its own entry. A text that several models share ('CAMEO')
     names one only when the USB id settles it. A USB id that the named model does not have
     makes the machine UNKNOWN ("USB id and model disagree").
  4. The verdict:
       USE         a MachineProfile, and a firmware line of it that may be driven (TESTED or
                   UNTESTED). Detection.support is the narrower of the two levels;
                   Detection.tested is True only when both are TESTED. Below TESTED the tail
                   must be the standard one (step 2).
       RECOGNIZED  a RecognizedModel, a RECOGNIZED firmware line, or a firmware line the
                   profile does not list. Refused: ESC 05 and FG only, reason "wrong-model".
                   Detection.message says why in plain words.
       UNKNOWN     a model no entry names, an ambiguous one, extra text, a tail other than the
                   four spaces where they are required, or not a model and version at all.
                   Refused, as today.
       NO_MODEL    nothing but padding, or not text. Refused, as today.

For the CAMEO 4, protocol.is_tested_model(r) is True exactly when detect(r) gives USE with the
CAMEO 4 profile and its TESTED V5 line. One intended change: 'CAMEO 4 V1.20    ' was refused by
the model gate and is now USE with the UNTESTED V1 line (jobs allowed, not tested); without the
four spaces it stays refused (UNKNOWN).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from . import registry
from .profile import (Entry, FirmwareLine, MachineProfile, RecognizedModel, Support,
                      recognized_message)

FG_PADDING = "\x03\x00 \t\n\r\x0b\x0c"        # ETX, NUL and ASCII whitespace
STANDARD_TAIL = "    "                         # what every firmware sends after the version
# "V%d.%02d": the major number has no leading zero, the minor always two digits.
_FG = re.compile(r"(?P<model>[ -~]+?) V(?P<major>[0-9]|[1-9][0-9])\.(?P<minor>[0-9]{2})"
                 r"(?P<tail>.*)", re.ASCII | re.DOTALL)


class Verdict(Enum):
    """What the daemon may do with the machine on the link."""
    USE = "use"                  # a profile and firmware line OpenCutter drives
    RECOGNIZED = "recognized"    # named, refused with a reason
    UNKNOWN = "unknown"          # refused: not a model OpenCutter knows
    NO_MODEL = "no-model"        # refused: the machine did not report a model


def fg_text(reply: object) -> str | None:
    """The FG reply as text with ETX, NUL and ASCII whitespace stripped at both ends, or None
    when it is neither text nor ASCII bytes."""
    if isinstance(reply, (bytes, bytearray)):
        try:
            reply = bytes(reply).decode("ascii")
        except UnicodeDecodeError:
            return None
    return reply.strip(FG_PADDING) if isinstance(reply, str) else None


@dataclass(frozen=True)
class Detection:
    """The result of detect(). `entry` and `line` name the machine when it is known."""
    verdict: Verdict
    reason: str = ""                     # plain words; empty for USE
    text: str | None = None              # the FG text after stripping (None: not text)
    model: str | None = None             # the model part of FG ("CAMEO 4")
    version: tuple[int, int] | None = None   # (major, minor)
    entry: Entry | None = None           # USE and RECOGNIZED: the registry entry
    line: FirmwareLine | None = None     # the firmware line, when the profile lists it
    standard_tail: bool = False          # four spaces after the version, as every firmware sends;
                                         # required below TESTED
    shared: bool = False                 # UNKNOWN: the model text is one several models report
                                         # ("CAMEO", "Silhouette"), and nothing tells which

    @property
    def profile(self) -> MachineProfile | None:
        """The profile the daemon uses: set only for USE."""
        return self.entry if self.verdict is Verdict.USE else None   # type: ignore[return-value]

    @property
    def support(self) -> Support | None:
        """USE: the narrower of the profile's and the line's level; RECOGNIZED: RECOGNIZED;
        otherwise None."""
        if self.verdict is Verdict.USE and self.line is not None:
            return self.entry.support.narrower(self.line.support)   # type: ignore[union-attr]
        return Support.RECOGNIZED if self.verdict is Verdict.RECOGNIZED else None

    @property
    def tested(self) -> bool:
        """True only for USE with a TESTED profile and a TESTED firmware line."""
        return self.support is Support.TESTED

    @property
    def name(self) -> str | None:
        """The model's name ("CAMEO 4") when it is known."""
        return self.entry.name if self.entry is not None else None

    @property
    def message(self) -> str:
        """Why nothing is sent, for people ('' for USE). For a recognized model: 'OpenCutter
        recognizes this <name> but cannot drive it yet: <reason>.'"""
        if self.verdict is Verdict.USE:
            return ""
        if self.verdict is Verdict.RECOGNIZED and self.entry is not None:
            return recognized_message(self.entry.name, self.reason)
        return self.reason[:1].upper() + self.reason[1:] + "."


def detect(reply: object, *, usb_id: tuple[int, int] | None = None,
           index: registry.Index | None = None) -> Detection:
    """Parse an FG reply (str, bytes or bytearray) to a Detection. `usb_id` (vendor, product)
    settles a shared FG text and must agree with the named model; `index` defaults to the
    registry of every known model (tests pass their own). Raises TypeError for a `usb_id` that
    is not two ints."""
    idx = registry.INDEX if index is None else index
    if usb_id is not None and not (type(usb_id) is tuple and len(usb_id) == 2
                                   and all(type(v) is int for v in usb_id)):
        raise TypeError(f"usb_id must be (vendor, product), not {usb_id!r}")
    if not isinstance(reply, (str, bytes, bytearray)):
        return Detection(Verdict.NO_MODEL, "the machine did not report its model")
    text = fg_text(reply)
    if text is None:
        return Detection(Verdict.UNKNOWN, "the machine's FG reply is not ASCII text")
    if not text:
        return Detection(Verdict.NO_MODEL, "the machine did not report its model", text)
    m = _FG.fullmatch(text)
    if m is None:
        return Detection(Verdict.UNKNOWN, "the FG reply is not a model and a firmware version",
                         text)
    model, version = m["model"], (int(m["major"]), int(m["minor"]))
    standard = _raw(reply) == f"{model} V{m['major']}.{m['minor']}{STANDARD_TAIL}"
    found = dict(text=text, model=model, version=version, standard_tail=standard)
    if m["tail"]:
        return Detection(Verdict.UNKNOWN, "the FG reply has more text after the firmware version",
                         **found)
    entry = idx.by_fg_model(model)
    if entry is None:
        shared = idx.fg_candidates(model)
        if not shared:
            return Detection(Verdict.UNKNOWN, "no machine OpenCutter knows reports this model",
                             **found)
        matches = [e for e in shared if usb_id is not None and usb_id in e.identity.usb_ids]
        if len(matches) != 1:
            names = " or ".join(e.name for e in shared)
            return Detection(Verdict.UNKNOWN, f"this FG reply could be a {names}, and no USB "
                             "id tells them apart", shared=True, **found)
        entry = matches[0]
    elif usb_id is not None and entry.identity.usb_ids and usb_id not in entry.identity.usb_ids:
        return Detection(Verdict.UNKNOWN, f"the USB id and the model ({entry.name}) disagree",
                         **found)
    if isinstance(entry, RecognizedModel):
        return Detection(Verdict.RECOGNIZED, entry.reason, entry=entry, **found)
    line = entry.identity.line(version[0])
    if line is None:
        return Detection(Verdict.RECOGNIZED, f"its firmware V{version[0]}.{version[1]:02} is not "
                         "a firmware line OpenCutter knows for it", entry=entry, **found)
    if not line.support.jobs_allowed:
        return Detection(Verdict.RECOGNIZED, line.reason, entry=entry, line=line, **found)
    if entry.support.narrower(line.support) is not Support.TESTED and not standard:
        return Detection(Verdict.UNKNOWN, f"the FG reply is not in the exact form the "
                         f"{entry.name}'s firmware sends (the version, then four spaces)",
                         **found)
    return Detection(Verdict.USE, entry=entry, line=line, **found)


def _raw(reply: str | bytes | bytearray) -> str | None:
    """The reply as text with one final ETX removed (for standard_tail), or None."""
    if isinstance(reply, (bytes, bytearray)):
        try:
            reply = bytes(reply).decode("ascii")
        except UnicodeDecodeError:
            return None
    return reply[:-1] if reply.endswith("\x03") else reply
