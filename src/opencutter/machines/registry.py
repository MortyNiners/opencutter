# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Every machine OpenCutter knows, and how to look one up.

Pure Python, no I/O. The registry is built once, at import, from the model modules in MODULES
(cameo4, cameo4_plus_pro, cameo5, cameo5_alpha, portrait, recognized). Every entry passes
safety.check_entry() first and the whole set must be consistent (Index), otherwise the import
raises: a wrong profile never reaches a machine.

How a model module declares its models (the contract for machines/<model>.py)
  * A module-level tuple PROFILES holding MachineProfile and RecognizedModel values (profile.py
    documents every field). An empty tuple is allowed.
  * MachineProfile for a model OpenCutter may drive: support TESTED or UNTESTED, at least one
    firmware line that may be driven, ranges, mats, dialect and queries as the field notes in
    profile.py say. RecognizedModel for a model that is only named and refused, with a
    plain-words reason. A firmware line can be RECOGNIZED on its own, with its own reason.
  * Values are typed and in our own words, each with a short basis comment ("measured on
    V5.05", "firmware analysis", "Studio 5.0.556 machine data" or "public captures and
    reports": what others have published, described in our own words, never a capture copied
    into the repository). No Studio rows, column names, syntax strings, firmware bytes or preset
    tables.
  * The module imports from .profile and the standard library, and it may import shared values
    from another model module (cameo4_plus_pro reuses cameo4's holders, ranges, status, queries
    and stop). It never imports .safety, .detect or .registry: a model module only declares
    values, and the registry checks them. It names its values in upper case (CAMEO4, V5, ...)
    so tests and other model modules can reach them.
  * Ids are unique over the registry, and so is every FG model text: an exact FG text belongs to
    one entry (Identity.fg_models), and a text several models may report goes in each one's
    Identity.shared_fg_models instead. Every USB id belongs to one entry too, so that a USB id
    can settle a shared FG text.
  * Pinned program bytes stay in device/programs.py, which checks them with
    safety.check_profile(profile, pinned=...).

Lookups (module functions, over INDEX)
  entries()             every entry, in MODULES order.
  profiles()            every MachineProfile (TESTED and UNTESTED).
  recognized_models()   every RecognizedModel.
  tested(), enabled()   the TESTED profiles; every profile jobs may use (TESTED and UNTESTED).
  by_id(id)             the entry with this id, or None.
  enabled_profile(id)   the MachineProfile with this id, or None (also for a recognized id):
                        what JobSpec.machine and Program.profile_id must name.
  by_fg_model(text)     the entry whose own FG model text is exactly `text`, or None.
  fg_candidates(text)   the entries that share the FG text `text` (settled by a USB id).
  by_usb_id((vid, pid)) the entries with this USB id (a discovery hint; FG still decides).
  ble_name_prefixes()   the BLE name prefixes discovery accepts, without regard to case.

Detection API (detect.py; re-exported by the package)
  detect(reply, *, usb_id=None, index=None) -> Detection, with
    verdict    Verdict.USE (a profile and a firmware line OpenCutter drives, TESTED or
               UNTESTED; below TESTED only with the firmware's four-space tail), RECOGNIZED
               (named, refused, with a reason), UNKNOWN or NO_MODEL;
    profile    the MachineProfile for USE, else None; entry, line: who it is when known;
    support    the narrower of the profile's and the line's level (USE), RECOGNIZED, or None;
    tested     True only for a TESTED profile on a TESTED line;
    name, reason, message, text, model, version, standard_tail.
  The daemon uses a link only for USE; anything else gets ESC 05 and FG only and is refused
  with the reason "wrong-model", as today.
"""
from __future__ import annotations

from collections.abc import Iterable

from . import cameo4, cameo4_plus_pro, cameo5, cameo5_alpha, portrait, recognized
from .profile import Entry, MachineProfile, RecognizedModel, Support
from .safety import check_entry

MODULES = (cameo4, cameo4_plus_pro, cameo5, cameo5_alpha, portrait, recognized)


class RegistryError(ValueError):
    """The entries are inconsistent (a duplicate id, FG text or USB id, a malformed PROFILES)."""


class Index:
    """Lookups over a fixed set of entries. Raises RegistryError for a duplicate id, a
    duplicate FG model text, an FG text that is both one entry's own and shared, or a USB id
    that two entries claim."""

    def __init__(self, entries: Iterable[Entry]):
        self.entries: tuple[Entry, ...] = tuple(entries)
        self._ids: dict[str, Entry] = {}
        self._fg: dict[str, Entry] = {}
        self._shared: dict[str, list[Entry]] = {}
        self._usb: dict[tuple[int, int], list[Entry]] = {}
        for e in self.entries:
            if not isinstance(e, (MachineProfile, RecognizedModel)):
                raise RegistryError(f"{e!r} is neither a MachineProfile nor a RecognizedModel")
            if e.id in self._ids:
                raise RegistryError(f"two entries have the id {e.id!r}")
            self._ids[e.id] = e
            for text in e.identity.fg_models:
                if text in self._fg or text in self._shared:
                    raise RegistryError(f"FG model text {text!r} is claimed twice")
                self._fg[text] = e
            for text in e.identity.shared_fg_models:
                if text in self._fg:
                    raise RegistryError(f"FG model text {text!r} is both {self._fg[text].id!r}'s "
                                        f"own and shared by {e.id!r}")
                self._shared.setdefault(text, []).append(e)
            for usb in e.identity.usb_ids:
                if usb in self._usb:
                    raise RegistryError(f"USB id {usb!r} is claimed by {self._usb[usb][0].id!r} "
                                        f"and {e.id!r}")
                self._usb.setdefault(usb, []).append(e)

    def by_id(self, entry_id: object) -> Entry | None:
        """The entry with this id, or None."""
        return self._ids.get(entry_id) if isinstance(entry_id, str) else None

    def enabled_profile(self, entry_id: object) -> MachineProfile | None:
        """The MachineProfile with this id, or None (also for a recognized model)."""
        e = self.by_id(entry_id)
        return e if isinstance(e, MachineProfile) else None

    def by_fg_model(self, text: object) -> Entry | None:
        """The entry whose own FG model text is exactly `text` (case-sensitive), or None."""
        return self._fg.get(text) if isinstance(text, str) else None

    def fg_candidates(self, text: object) -> tuple[Entry, ...]:
        """The entries that may report the shared FG model text `text`."""
        return tuple(self._shared.get(text, ())) if isinstance(text, str) else ()

    def by_usb_id(self, usb_id: object) -> tuple[Entry, ...]:
        """The entries with this (vendor, product) USB id."""
        try:
            return tuple(self._usb.get(usb_id, ())) if isinstance(usb_id, tuple) else ()
        except TypeError:                 # an unhashable tuple names no USB id
            return ()

    def profiles(self) -> tuple[MachineProfile, ...]:
        """Every MachineProfile."""
        return tuple(e for e in self.entries if isinstance(e, MachineProfile))

    def recognized_models(self) -> tuple[RecognizedModel, ...]:
        """Every RecognizedModel."""
        return tuple(e for e in self.entries if isinstance(e, RecognizedModel))

    def tested(self) -> tuple[MachineProfile, ...]:
        """The TESTED profiles."""
        return tuple(p for p in self.profiles() if p.support is Support.TESTED)

    def enabled(self) -> tuple[MachineProfile, ...]:
        """The profiles jobs may use: TESTED and UNTESTED."""
        return tuple(p for p in self.profiles() if p.support.jobs_allowed)

    def ble_name_prefixes(self) -> tuple[str, ...]:
        """Every entry's BLE name prefixes, in registry order, without duplicates (ignoring
        case). Discovery compares them with the start of the device name, ignoring case."""
        seen: dict[str, str] = {}
        for e in self.entries:
            for prefix in e.identity.ble_names:
                seen.setdefault(prefix.upper(), prefix)
        return tuple(seen.values())


def _load() -> Index:
    entries: list[Entry] = []
    for module in MODULES:
        declared = getattr(module, "PROFILES", None)
        if type(declared) is not tuple:
            raise RegistryError(f"{module.__name__}.PROFILES must be a tuple")
        entries += declared
    for e in entries:
        check_entry(e)
    return Index(entries)


INDEX = _load()


def entries() -> tuple[Entry, ...]:
    """Every entry, in MODULES order."""
    return INDEX.entries


def profiles() -> tuple[MachineProfile, ...]:
    """Every MachineProfile (TESTED and UNTESTED)."""
    return INDEX.profiles()


def recognized_models() -> tuple[RecognizedModel, ...]:
    """Every RecognizedModel."""
    return INDEX.recognized_models()


def tested() -> tuple[MachineProfile, ...]:
    """The TESTED profiles."""
    return INDEX.tested()


def enabled() -> tuple[MachineProfile, ...]:
    """The profiles jobs may use: TESTED and UNTESTED."""
    return INDEX.enabled()


def by_id(entry_id: object) -> Entry | None:
    """The entry with this id, or None."""
    return INDEX.by_id(entry_id)


def enabled_profile(entry_id: object) -> MachineProfile | None:
    """The MachineProfile with this id, or None (also for a recognized model)."""
    return INDEX.enabled_profile(entry_id)


def by_fg_model(text: object) -> Entry | None:
    """The entry whose own FG model text is exactly `text`, or None."""
    return INDEX.by_fg_model(text)


def fg_candidates(text: object) -> tuple[Entry, ...]:
    """The entries that may report the shared FG model text `text`."""
    return INDEX.fg_candidates(text)


def by_usb_id(usb_id: object) -> tuple[Entry, ...]:
    """The entries with this (vendor, product) USB id."""
    return INDEX.by_usb_id(usb_id)


def ble_name_prefixes() -> tuple[str, ...]:
    """The BLE name prefixes discovery accepts, compared without regard to case."""
    return INDEX.ble_name_prefixes()
