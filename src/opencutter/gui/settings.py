# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""GUI preferences, stored as JSON in the user's config directory.

Machine profiles (opencutter.machines)
  machine  the layout's machine, a profile id (default "cameo4"): the offline choice of Page
           Setup, and the machine the last layout was made for. An id OpenCutter does not
           drive (unknown, or a model it only recognizes) reads as "cameo4".
  mats     profile id -> the Cutting Mat chosen for that machine (a Mat.key). Entries of ids
           OpenCutter does not drive are dropped; a mat the profile does not offer reads as
           the profile's default_mat (mat_for).
  A file written before machine profiles has neither key and loads as before: the CAMEO 4
  with its 12 x 12 in mat. Builds before machine profiles ignore both keys.

language   the window's language (Preferences): "" follows the system's language, else a
           language code ("ro", "pt_BR"; opencutter.i18n). It takes effect at the next start
           (app.main). A file without it, or with a value that is not a language code, loads as
           "".
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from opencutter.i18n import valid_language
from opencutter.machines import CAMEO4, MachineProfile, enabled_profile

RECENT_LIMIT = 3
DEFAULT_MACHINE = CAMEO4.id


def default_path() -> Path:
    """``$XDG_CONFIG_HOME/opencutter/gui.json``."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "opencutter" / "gui.json"


@dataclass
class GuiSettings:
    """What the GUI remembers between runs. Unknown or broken values fall back to defaults."""
    units: str = "in"
    media: str = "letter"
    landscape: bool = False
    custom_media_mm: list[float] = field(default_factory=lambda: [254.0, 254.0])
    preview_outline: bool = True
    reveal_mat: bool = True
    first_run_done: bool = False
    material: str | None = None
    recent_materials: list[str] = field(default_factory=list)
    machine: str = DEFAULT_MACHINE
    mats: dict[str, str] = field(default_factory=dict)
    language: str = ""
    path: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path | None = None) -> "GuiSettings":
        """Read the settings file; a missing or unreadable file gives the defaults."""
        path = path or default_path()
        settings = cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return settings
        if not isinstance(data, dict):
            return settings
        for f in fields(cls):
            if f.name == "path" or f.name not in data:
                continue
            value, default = data[f.name], getattr(settings, f.name)
            expected = str if default is None else type(default)
            if isinstance(value, expected):
                setattr(settings, f.name, value)
        defaults = cls()
        if settings.units not in ("in", "mm"):
            settings.units = defaults.units
        size = settings.custom_media_mm
        if len(size) != 2 or not all(isinstance(v, (int, float)) and v > 0 for v in size):
            settings.custom_media_mm = defaults.custom_media_mm
        settings.recent_materials = [m for m in settings.recent_materials if isinstance(m, str)]
        if enabled_profile(settings.machine) is None:
            settings.machine = defaults.machine
        mats = {}
        for machine, key in settings.mats.items():
            profile = enabled_profile(machine)
            if profile is not None and isinstance(key, str):
                mats[machine] = key if _offers(profile, key) else profile.default_mat
        settings.mats = mats
        if not valid_language(settings.language):
            settings.language = defaults.language
        return settings

    def save(self) -> None:
        """Write the settings file (errors are ignored: preferences are a convenience)."""
        if self.path is None:
            return
        data = {k: v for k, v in asdict(self).items() if k != "path"}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    @property
    def profile(self) -> MachineProfile:
        """The profile of `machine` (the CAMEO 4 for an id OpenCutter does not drive)."""
        return enabled_profile(self.machine) or CAMEO4

    def set_machine(self, machine: str) -> None:
        """Make `machine` the layout's machine; an id OpenCutter does not drive gives the
        CAMEO 4."""
        self.machine = machine if enabled_profile(machine) is not None else DEFAULT_MACHINE

    def mat_for(self, profile: MachineProfile | None = None) -> str:
        """The Cutting Mat chosen for `profile` (default: the layout's machine), or its
        default_mat when none was chosen or the profile does not offer that mat."""
        profile = profile or self.profile
        key = self.mats.get(profile.id)
        return key if isinstance(key, str) and _offers(profile, key) else profile.default_mat

    def set_mat(self, key: str, profile: MachineProfile | None = None) -> None:
        """Remember `key` as the Cutting Mat of `profile` (default: the layout's machine); for
        a mat the profile does not offer, its default_mat is remembered instead."""
        profile = profile or self.profile
        self.mats[profile.id] = key if _offers(profile, key) else profile.default_mat

    def remember_material(self, material: str) -> None:
        """Make ``material`` the current one and put it first in the Recent list."""
        self.material = material
        recent = [m for m in self.recent_materials if m != material]
        self.recent_materials = [material, *recent][:RECENT_LIMIT]


def _offers(profile: MachineProfile, key: object) -> bool:
    """True when the profile offers the mat `key` at its own level (Mat.offered)."""
    return any(m.key == key for m in profile.offered_mats())
