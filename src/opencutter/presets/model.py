# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Material presets: one row per material + action + tool holder, and the store that holds them.

The store is the presets file ``$XDG_DATA_HOME/opencutter/presets.json`` when there is one, else
the small built-in Basic set below. OpenCutter never writes that file itself: people write it by
hand or with their own tools. Its format is documented in README.md ("Material presets"). A file
that cannot be read, or that holds a value a machine it names would not accept, gives the Basic
set and a warning.

Machines (opencutter.machines)
  Each row names the machine profiles it is for (Preset.machines: profile ids such as "cameo4").
  Its values must suit every one of them (check_values): that profile's holders and tools, its
  ranges, Blade Depth only for a tool that takes one, and no Offset or Overcut for a tool that
  is not a blade. An id that is unknown, or that names a model OpenCutter only recognizes and
  cannot drive, is an error, and an error makes the whole file fall back to the Basic set. A
  machine is offered only the rows that name it (PresetStore.for_profile).

File schemas (FILE_SCHEMAS)
  1  Every row is a CAMEO 4 row. A "machines" key anywhere in the file is an error; other keys
     a schema 1 file does not use are ignored, as they always were.
  2  A row may name its machines: "machines": ["cameo4", "cameo5"]. A row without the key is a
     CAMEO 4 row, as in schema 1. A key that schema 2 does not have, in a row or at the top of
     the file (FILE_KEYS), is an error, so that a misspelt "machines" cannot turn a row of
     another model into a CAMEO 4 row.
  Machines are named per row only: a "machines" key at the top of the file is an error.

Builds from before machine profiles (OpenCutter 0.1.0.dev0)
  Such a build takes every row as a CAMEO 4 row and ignores keys it does not know, "machines"
  included. But its PresetStore.load reads only files whose "schema" is 1: it refuses any other
  file as unreadable and uses its Basic set instead. A schema 2 file therefore never gives it a
  row of another model. That is why rows of other models need schema 2, and why this build
  refuses "machines" in a schema 1 file: a schema 1 file means the same to both builds.

Basic set
  Ours, made on a CAMEO 4. It applies to the CAMEO 4 family only: the CAMEO 4, the CAMEO 4
  Plus and the CAMEO 4 Pro (BASIC_PRESETS_BY_PROFILE), which share the CAMEO 4's holders, tools,
  ranges and firmware. It has not been cut on a Plus or a Pro, so their rows are not verified,
  and a row their profile would refuse is left out. Other models get no Basic presets: they use
  Cut Settings or a presets file. A store of the Basic set holds the CAMEO 4's rows;
  PresetStore.for_profile gives the Plus and the Pro the same rows, named for them.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from opencutter.job.spec import NO_TOOL, ToolSettings
from opencutter.machines import CAMEO4, MachineProfile, RecognizedModel, by_id, enabled_profile

log = logging.getLogger(__name__)

FILE_FORMAT = "opencutter-presets"
FILE_SCHEMA = 2                        # the newest schema; this build reads FILE_SCHEMAS
FILE_SCHEMAS = (1, 2)
MACHINES_KEY = "machines"              # the row key that names the machines (schema 2 only)
# The keys at the top of a schema 2 file; any other one is an error there (schema 1 ignores it).
FILE_KEYS = frozenset({"format", "schema", "description", "presets"})
BASIC_DESCRIPTION = "Basic set"
FILE_DESCRIPTION = "Presets file"     # shown for a presets file without its own description
MAX_DESCRIPTION = 80                   # characters of a file's description that are shown

# The machines of a row that names none: every schema 1 row is a CAMEO 4 row.
DEFAULT_MACHINES: tuple[str, ...] = (CAMEO4.id,)
# The machines the Basic set applies to: the CAMEO 4 family.
BASIC_MACHINES: tuple[str, ...] = (CAMEO4.id, "cameo4_plus", "cameo4_pro")


@dataclass
class Preset:
    """One material + action + tool holder row, in the units the cutter uses. Its values must
    suit every machine it names; the ranges are the profile's (Ranges), the CAMEO 4's are in the
    comments."""
    material: str                # name shown in the material list
    material_type: str           # broad group used for filtering, e.g. "Vinyl"
    action: str                  # Studio term: "Cut", "Sketch", "Score", "Kraft Cut", ...
    holder: int                  # Holder.number: 1 (left) or 2 (right); 1 on a one-holder model
    tool: str                    # Studio tool name, e.g. "AutoBlade"
    tool_code: int               # ESC 15 code expected in that holder (0 = not detectable)
    speed: int                   # ! 1..30 (whole cm/s)
    force: int                   # FX 1..33
    acceleration: int            # TJ 1..10; a whole number, not sent to a model without TJ
    offset: int                  # FC blade offset 0..99; 0 for pens and the Punch Tool
    line_segment_overcut: bool   # FE
    overcut: tuple[int, int]     # FF start, end 0..9 (= round(mm * 9))
    blade_depth: int | None      # TF 0..10, only for a tool that takes it (the AutoBlade in
                                 # Tool 1); None for every other tool
    passes: int                  # 1..10
    verified: bool = False       # True only when tested by us on a real machine of each model
                                 # the row names
    machines: tuple[str, ...] = DEFAULT_MACHINES   # profile ids (opencutter.machines)

    def to_tool_settings(self) -> ToolSettings:
        """The condition block settings the job builder needs for this preset. The job names
        the machine itself (JobSpec.machine)."""
        return ToolSettings(
            holder=self.holder, tool_code=self.tool_code, speed=self.speed, force=self.force,
            acceleration=self.acceleration, offset=self.offset,
            line_segment_overcut=self.line_segment_overcut, overcut=self.overcut,
            blade_depth=self.blade_depth, passes=self.passes)

    def to_dict(self) -> dict:
        """JSON-safe dict: one row of the presets file. "machines" is left out for a row of the
        CAMEO 4 alone, which is what a row without it means, so such a row fits a file of either
        schema; a row that names other machines needs a schema 2 file."""
        d = asdict(self)
        d["overcut"] = list(self.overcut)
        machines = d.pop(MACHINES_KEY)
        if tuple(machines) != DEFAULT_MACHINES:
            d[MACHINES_KEY] = list(machines)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Preset:
        """Inverse of :meth:`to_dict`. Raises ValueError on a missing field, a wrong type, a
        machine id that is unknown or names a model OpenCutter cannot drive, or a value one of
        the row's machines does not accept (:func:`check_values`). Without "machines" the row
        is a CAMEO 4 row. Keys that are not fields are ignored here; PresetStore.load refuses
        them in a schema 2 file."""
        names = {f.name for f in fields(cls)}
        required = names - {"verified", MACHINES_KEY}
        if not isinstance(d, dict) or not required <= d.keys():
            raise ValueError(f"preset needs the fields {sorted(required)}")
        values = {k: v for k, v in d.items() if k in names}
        if not isinstance(values["overcut"], (list, tuple)):
            raise ValueError("overcut must be a pair")
        values["overcut"] = tuple(values["overcut"])
        if MACHINES_KEY in values:
            if not isinstance(values[MACHINES_KEY], (list, tuple)):
                raise ValueError(f"{_where_dict(d)}: machines must be a list of machine ids")
            values[MACHINES_KEY] = tuple(values[MACHINES_KEY])
        p = cls(**values)
        _check_types(p)
        check_values(p)
        return p


def _where(p: Preset) -> str:
    return f"preset {p.material!r} / {p.action!r}"


def _where_dict(d: dict) -> str:
    return f"preset {d.get('material')!r} / {d.get('action')!r}"


def _check_types(p: Preset) -> None:
    def is_int(v: object) -> bool:
        return isinstance(v, int) and not isinstance(v, bool)

    ints = (p.holder, p.tool_code, p.speed, p.force, p.acceleration, p.offset, p.passes)
    ok = (all(isinstance(s, str) for s in (p.material, p.material_type, p.action, p.tool))
          and all(is_int(v) for v in ints)
          and isinstance(p.line_segment_overcut, bool) and isinstance(p.verified, bool)
          and len(p.overcut) == 2 and all(is_int(v) for v in p.overcut)
          and (p.blade_depth is None or is_int(p.blade_depth)))
    if not ok:
        raise ValueError(f"{_where(p)} has a field of the wrong type")
    _check_machine_ids(p)


def _check_machine_ids(p: Preset) -> None:
    m = p.machines
    if not (isinstance(m, tuple) and m and all(isinstance(i, str) for i in m)
            and len(set(m)) == len(m)):
        raise ValueError(f"{_where(p)}: machines must be a non-empty list of different "
                         "machine ids")


def machine_profiles(p: Preset) -> tuple[MachineProfile, ...]:
    """The profiles the row names, in its order. Raises ValueError for a row that names none, an
    id that is unknown, or one that names a model OpenCutter recognizes but cannot drive (a
    RecognizedModel)."""
    _check_machine_ids(p)
    out = []
    for machine in p.machines:
        profile = enabled_profile(machine)
        if profile is None:
            entry = by_id(machine)
            if entry is None:
                raise ValueError(f"{_where(p)}: {machine!r} is not a machine OpenCutter knows")
            raise ValueError(f"{_where(p)}: {machine!r} is the {entry.name}, which OpenCutter "
                             "cannot drive yet")
        out.append(profile)
    return tuple(out)


def check_values(p: Preset, profile: MachineProfile | None = None) -> None:
    """Raise ValueError unless every value of ``p`` is one the machine accepts: ``profile``, or
    without it every machine the row names (:func:`machine_profiles`, which also refuses an
    unknown id). With ``profile`` only the values are checked, not whether the row names it.

    The same rules as ``job.validate`` for one condition block (docs/PROTOCOL.md section 5),
    taken from the profile: a holder it has, a tool it detects in that holder, its ranges,
    Blade Depth only for a tool that takes one (Tool.depth: the AutoBlade in Tool 1 on the
    CAMEO 4), and Offset 0, no Line Segment Overcut and Overcut 0, 0 for tools that are not
    blades. A model without TJ does not use the acceleration, which stays a whole number. Tools
    that jobs refuse (the Rotary Blade and the Punch Tool) are accepted here; the app hides
    them.
    """
    for target in (profile,) if profile is not None else machine_profiles(p):
        _check_for(p, target)


def _check_for(p: Preset, profile: MachineProfile) -> None:
    where = f"{_where(p)} for the {profile.name}"
    holder = profile.holder(p.holder)
    if holder is None:
        raise ValueError(f"{where}: holder {p.holder} is not {_holder_numbers(profile)}")
    tool = holder.tool(p.tool_code)
    if tool is None or tool.code == NO_TOOL:
        raise ValueError(f"{where}: tool_code {p.tool_code} is not a tool the machine detects "
                         f"in {holder.label}")
    for name, (lo, hi) in profile.ranges.table().items():
        value = getattr(p, name)
        if value is None and name == "blade_depth":
            continue
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError(f"{where}: {name} {value} is outside {lo}..{hi}")
    if type(p.acceleration) is not int:     # without TJ it is not sent, but stays a whole number
        raise ValueError(f"{where}: acceleration {p.acceleration!r} is not a whole number")
    if not (isinstance(p.overcut, tuple) and len(p.overcut) == 2):
        raise ValueError(f"{where}: overcut {p.overcut!r} is not a pair")
    lo, hi = profile.ranges.overcut.as_tuple()
    if not all(type(v) is int and lo <= v <= hi for v in p.overcut):
        raise ValueError(f"{where}: overcut {list(p.overcut)} is outside {lo}..{hi}")
    if p.blade_depth is not None and not tool.depth:
        raise ValueError(f"{where}: blade_depth works only for {_depth_tools(profile)}; "
                         "use null")
    if not tool.blade and (p.offset != 0 or p.line_segment_overcut or p.overcut != (0, 0)):
        raise ValueError(f"{where}: the {tool.name} is not a blade: offset must be 0, "
                         "line_segment_overcut false and overcut [0, 0]")


def _holder_numbers(profile: MachineProfile) -> str:
    """'1 or 2' on the CAMEO 4, '1' on a one-holder model."""
    numbers = [str(h.number) for h in profile.holders]
    return ", ".join(numbers[:-1]) + " or " + numbers[-1] if len(numbers) > 1 else numbers[0]


def _depth_tools(profile: MachineProfile) -> str:
    """'the AutoBlade in Tool 1' on the CAMEO 4, 'the AutoBlade' on a one-holder model."""
    names = [f"the {t.name}" + (f" in {h.label}" if len(profile.holders) > 1 else "")
             for h in profile.holders for t in h.tools if t.depth]
    return " or ".join(names) if names else f"no tool of the {profile.name}"


# Our own small set, used when there is no presets file: the CAMEO 4's rows.
BASIC_PRESETS: list[Preset] = [
    # Verified 2026-11-16 on a CAMEO 4 (firmware V5.05, Bluetooth LE): Oracal 651 adhesive vinyl
    # on its backing, on a 12x12 in mat. A 25.4 mm test square cut cleanly and matched the mat grid.
    Preset(material="Vinyl", material_type="Vinyl", action="Cut", holder=1,
           tool="AutoBlade", tool_code=2, speed=5, force=10, acceleration=3, offset=18,
           line_segment_overcut=False, overcut=(1, 1), blade_depth=1, passes=1, verified=True),
    # Not tested yet: a light starting point for a Sketch Pen in Tool Holder 1 on paper.
    Preset(material="Sketch Pen", material_type="Paper", action="Sketch", holder=1,
           tool="Sketch Pen", tool_code=7, speed=5, force=7, acceleration=3, offset=0,
           line_segment_overcut=False, overcut=(0, 0), blade_depth=None, passes=1),
]


def _suits(p: Preset) -> bool:
    """True when every machine the row names accepts its values (check_values)."""
    try:
        check_values(p)
    except ValueError:
        return False
    return True


def _for_family(rows: Iterable[Preset], machine: str) -> list[Preset]:
    """The CAMEO 4 rows among ``rows`` as rows of ``machine``, another model of the CAMEO 4
    family: the same values, not verified there (nothing was cut on it), and only the rows its
    profile accepts."""
    out = []
    for p in rows:
        if p.machines == DEFAULT_MACHINES:
            q = replace(p, machines=(machine,), verified=False)
            if _suits(q):
                out.append(q)
    return out


def _basic_rows(machine: str) -> list[Preset]:
    """The Basic set named for one machine of the CAMEO 4 family: the CAMEO 4's own rows, or
    the same values for the Plus or the Pro (_for_family)."""
    if machine == CAMEO4.id:
        return BASIC_PRESETS
    return _for_family(BASIC_PRESETS, machine)


# machine id -> its Basic set. Models outside the CAMEO 4 family have none.
BASIC_PRESETS_BY_PROFILE: dict[str, list[Preset]] = {m: _basic_rows(m) for m in BASIC_MACHINES}


def _machine_id(profile: MachineProfile | RecognizedModel | str) -> str:
    """The id of a profile, a recognized model or an id; anything else is a TypeError."""
    if isinstance(profile, str):
        return profile
    if isinstance(profile, (MachineProfile, RecognizedModel)):
        return profile.id
    raise TypeError(f"a machine profile or its id is needed, not {profile!r}")


def _names(p: Preset, machine: str) -> bool:
    """True when the row names ``machine`` (a tuple only: never a substring of a plain string,
    as "cameo4" is of "cameo4_plus")."""
    return isinstance(p.machines, tuple) and machine in p.machines


def _key(p: Preset) -> tuple[str, str, int]:
    return (p.material, p.action, p.holder)


def basic_presets(profile: MachineProfile | RecognizedModel | str) -> list[Preset]:
    """The Basic set for one machine (a profile or its id), as a new list: empty for a model
    outside the CAMEO 4 family."""
    return list(BASIC_PRESETS_BY_PROFILE.get(_machine_id(profile), ()))


def basic_source() -> dict:
    """The ``source`` dict of the built-in set."""
    return {"kind": "basic", "description": BASIC_DESCRIPTION}


def file_source(description: object = None) -> dict:
    """The ``source`` dict of a presets file with this ``description`` (its first line, at most
    ``MAX_DESCRIPTION`` characters; "Presets file" when it has none)."""
    lines = description.strip().splitlines() if isinstance(description, str) else []
    text = lines[0].strip() if lines else ""
    if len(text) > MAX_DESCRIPTION:
        text = text[:MAX_DESCRIPTION - 1].rstrip() + "…"
    return {"kind": "file", "description": text or FILE_DESCRIPTION}


def presets_path() -> Path:
    """``$XDG_DATA_HOME/opencutter/presets.json`` (default ``~/.local/share/...``)."""
    base = os.environ.get("XDG_DATA_HOME", "")
    root = Path(base) if os.path.isabs(base) else Path.home() / ".local" / "share"
    return root / "opencutter" / "presets.json"


class PresetStore:
    """All presets of one source, with lookups for the material list.

    A presets file can hold rows for several machines, so ``presets`` may name machines other
    than the connected one: :meth:`for_profile` gives the rows one machine may use.
    """

    def __init__(self, presets: list[Preset], source: dict):
        self.presets = presets
        self.source = source      # {"kind": "basic"|"file", "description": str}; a file that
                                  # could not be read adds "file_error" (and kind is "basic")

    @property
    def description(self) -> str:
        """What the app shows as the source: "Basic set", or the presets file's description."""
        return self.source.get("description") or (
            BASIC_DESCRIPTION if self.source.get("kind") == "basic" else FILE_DESCRIPTION)

    @staticmethod
    def load(path: str | os.PathLike | None = None) -> PresetStore:
        """Load the presets file, or the built-in set if there is none.

        A file that cannot be read, or that has a wrong value, also gives the built-in set; the
        reason is logged and kept in ``source["file_error"]``. The rules for each schema are in
        the module docstring.
        """
        path = Path(path) if path else presets_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return PresetStore(list(BASIC_PRESETS), basic_source())
        except (OSError, ValueError) as e:
            return _broken_file(path, e)
        try:
            schema = data.get("schema")
            if (data.get("format") != FILE_FORMAT or type(schema) is not int
                    or schema not in FILE_SCHEMAS):
                raise ValueError(f"not an {FILE_FORMAT} file of schema "
                                 + " or ".join(str(s) for s in FILE_SCHEMAS))
            rows = data["presets"]
            if not isinstance(rows, list) or not rows:
                raise ValueError("it has no presets")
            _check_rows(data, rows, schema)
            presets = [Preset.from_dict(d) for d in rows]
            _check_unique(presets)
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            return _broken_file(path, e)
        return PresetStore(presets, file_source(data.get("description")))

    def for_profile(self, profile: MachineProfile | RecognizedModel | str) -> PresetStore:
        """The presets one machine (a profile or its id) may use, as a store with the same
        source, a "file_error" included: the store's rows that name it, in their order. A store
        of the Basic set (also the one a broken file gives) also gives the CAMEO 4 Plus and the
        CAMEO 4 Pro its CAMEO 4 rows, named for them and not verified (BASIC_MACHINES), unless
        it has a row of that material, action and holder for them already. Empty for a machine
        that has no rows, such as a model OpenCutter cannot drive.

        For the CAMEO 4 the view of a store of CAMEO 4 rows holds all of them, whatever its
        source, so the CAMEO 4's material list does not change. Raises TypeError for anything
        that is not a profile, a recognized model or an id."""
        machine = _machine_id(profile)
        rows = [p for p in self.presets if _names(p, machine)]
        if (self.source.get("kind") == "basic" and machine in BASIC_MACHINES
                and machine != CAMEO4.id):
            have = {_key(p) for p in rows}
            rows += [q for q in _for_family(self.presets, machine) if _key(q) not in have]
        return PresetStore(rows, dict(self.source))

    def materials(self, material_type: str | None = None) -> list[str]:
        """Material names, sorted, optionally only those of one material type."""
        names = {p.material for p in self.presets
                 if material_type is None or p.material_type == material_type}
        return sorted(names, key=str.casefold)

    def material_types(self) -> list[str]:
        """The material types present, sorted."""
        return sorted({p.material_type for p in self.presets if p.material_type},
                      key=str.casefold)

    def presets_for(self, material: str) -> list[Preset]:
        """All presets of one material, ordered by holder and action."""
        return sorted((p for p in self.presets if p.material == material),
                      key=lambda p: (p.holder, p.action.casefold()))

    def search(self, text: str, material_type: str | None = None) -> list[str]:
        """Material names that contain every word of ``text`` (case-insensitive).

        The material type counts as part of the name, so "vinyl" also finds vinyl types.
        """
        words = text.casefold().split()
        found = {p.material for p in self.presets
                 if (material_type is None or p.material_type == material_type)
                 and all(w in f"{p.material} {p.material_type}".casefold() for w in words)}
        return sorted(found, key=str.casefold)


_ROW_KEYS = frozenset(f.name for f in fields(Preset))


def _check_rows(data: dict, rows: list, schema: int) -> None:
    """The schema rules for "machines", for top-level keys and for row keys (module
    docstring)."""
    if MACHINES_KEY in data:
        raise ValueError('"machines" belongs in each preset, not at the top of the file')
    unknown_top = sorted(str(k) for k in data.keys() - FILE_KEYS)
    if schema >= 2 and unknown_top:
        raise ValueError(f"unknown top-level key(s) {unknown_top}")
    for d in rows:
        if not isinstance(d, dict):
            continue                      # Preset.from_dict refuses it
        if schema == 1 and MACHINES_KEY in d:
            raise ValueError(f'{_where_dict(d)}: "machines" needs schema 2 (a build that reads '
                             'schema 1 ignores it and takes every preset as a CAMEO 4 preset)')
        unknown = sorted(str(k) for k in d.keys() - _ROW_KEYS)
        if schema >= 2 and unknown:
            raise ValueError(f"{_where_dict(d)}: unknown field(s) {unknown}")


def _check_unique(presets: list[Preset]) -> None:
    seen: set[tuple[str, str, str, int]] = set()
    for p in presets:
        for machine in p.machines:
            key = (machine, p.material, p.action, p.holder)
            if key in seen:
                profile = enabled_profile(machine)
                holder = profile.holder(p.holder) if profile is not None else None
                label = holder.label if holder is not None else f"Tool {p.holder}"
                name = profile.name if profile is not None else machine
                raise ValueError(f"{_where(p)} in {label} appears twice for the {name}")
            seen.add(key)


def _broken_file(path: Path, error: Exception) -> PresetStore:
    message = f"Could not read {path}: {error}"
    log.warning("%s; using the Basic set", message)
    source = basic_source()
    source["file_error"] = message
    return PresetStore(list(BASIC_PRESETS), source)
