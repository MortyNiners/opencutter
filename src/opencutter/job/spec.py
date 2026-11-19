# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Job model: what to cut, where, and with which tool settings.

Pure data. A `JobSpec` travels from the GUI to the daemon as JSON (`to_dict` / `from_dict`);
the daemon never receives raw bytes from a client. `from_dict` checks only the shape and the
types; `opencutter.job.validate.validate_spec` checks the values against the machine profile
the job names (`JobSpec.machine`, an id of opencutter.machines; "cameo4" when left out, so
older clients keep working).

Coordinates are document millimetres on the mat: x to the right, y down, from the mat origin
(top-left). A closed contour repeats its first point at the end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opencutter.machines import CAMEO4

Point = tuple[float, float]

DEFAULT_MACHINE = CAMEO4.id               # the profile of a JobSpec that names none
NO_TOOL = 0
AUTOBLADE = 2                              # the CAMEO 4's AutoBlade code (other models: Tool.depth)
# The CAMEO 4's tools that cut with a blade (they get the contour overlap); other models say it
# per tool (Tool.blade).
BLADE_TOOLS = CAMEO4.blade_codes()


class SpecError(ValueError):
    """A job dict that does not have the JobSpec shape or types."""


@dataclass
class ToolSettings:
    """One condition block (PROTOCOL.md section 5), in firmware units. The ranges are the
    machine profile's (Ranges); the CAMEO 4's are in the comments."""
    holder: int                      # Holder.number: 1 (left) or 2 (right); 1 on a one-holder model
    tool_code: int                   # ESC 15 code required in that holder
    speed: int                       # ! 1..30
    force: int                       # FX 1..33
    acceleration: int                # TJ 1..10; not sent to a model without TJ
    offset: int                      # FC 0..99 (blade offset; 0 for pens)
    line_segment_overcut: bool       # FE
    overcut: tuple[int, int]         # FF start,end 0..9
    blade_depth: int | None          # TF 0..10, only a tool that takes it (Tool.depth); else None
    passes: int = 1                  # 1..10 (path repeated)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict."""
        return {"holder": self.holder, "tool_code": self.tool_code, "speed": self.speed,
                "force": self.force, "acceleration": self.acceleration, "offset": self.offset,
                "line_segment_overcut": self.line_segment_overcut,
                "overcut": list(self.overcut), "blade_depth": self.blade_depth,
                "passes": self.passes}

    @staticmethod
    def from_dict(d: Any) -> ToolSettings:
        """Inverse of `to_dict`. Raises SpecError on unknown or missing keys and wrong types."""
        d = _fields(d, "settings", required=("holder", "tool_code", "speed", "force",
                                             "acceleration", "offset", "line_segment_overcut",
                                             "overcut", "blade_depth"), optional=("passes",))
        overcut = _seq(d["overcut"], "settings.overcut", 2)
        depth = d["blade_depth"]
        return ToolSettings(
            holder=_int(d["holder"], "settings.holder"),
            tool_code=_int(d["tool_code"], "settings.tool_code"),
            speed=_int(d["speed"], "settings.speed"),
            force=_int(d["force"], "settings.force"),
            acceleration=_int(d["acceleration"], "settings.acceleration"),
            offset=_int(d["offset"], "settings.offset"),
            line_segment_overcut=_bool(d["line_segment_overcut"], "settings.line_segment_overcut"),
            overcut=(_int(overcut[0], "settings.overcut"), _int(overcut[1], "settings.overcut")),
            blade_depth=None if depth is None else _int(depth, "settings.blade_depth"),
            passes=_int(d.get("passes", 1), "settings.passes"))


@dataclass
class CutGroup:
    """Paths cut with one tool and one set of settings."""
    settings: ToolSettings
    paths: list[list[Point]]         # polylines in document mm

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict."""
        return {"settings": self.settings.to_dict(),
                "paths": [[[x, y] for x, y in p] for p in self.paths]}

    @staticmethod
    def from_dict(d: Any) -> CutGroup:
        """Inverse of `to_dict`. Raises SpecError on unknown or missing keys and wrong types."""
        d = _fields(d, "group", required=("settings", "paths"))
        paths = []
        for path in _seq(d["paths"], "group.paths"):
            points = []
            for pt in _seq(path, "group.paths[]"):
                x, y = _seq(pt, "point", 2)
                points.append((_num(x, "point.x"), _num(y, "point.y")))
            paths.append(points)
        return CutGroup(settings=ToolSettings.from_dict(d["settings"]), paths=paths)


@dataclass
class JobSpec:
    """A whole job: the machine, the mat, the media rectangle on it, and the groups in cutting
    order."""
    mat: str                                          # Mat.key of the machine's mat: "12x12"
    media: tuple[float, float, float, float]          # x, y, w, h in document mm on the mat
    groups: list[CutGroup]
    preview_outline: bool = True                      # tool-up trace of the bbox before cutting
    allow_unrecognized_tool: dict[int, bool] = field(default_factory=dict)
    machine: str = DEFAULT_MACHINE                    # MachineProfile.id the job was made for

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (holder keys become the strings "1" / "2"). "machine" is always
        there."""
        return {"machine": self.machine, "mat": self.mat, "media": list(self.media),
                "groups": [g.to_dict() for g in self.groups],
                "preview_outline": self.preview_outline,
                "allow_unrecognized_tool": {str(h): v
                                            for h, v in self.allow_unrecognized_tool.items()}}

    @staticmethod
    def from_dict(d: Any) -> JobSpec:
        """Inverse of `to_dict`. Raises SpecError on unknown or missing keys and wrong types.
        "machine" is optional (DEFAULT_MACHINE, the CAMEO 4), as clients before machine
        profiles leave it out."""
        d = _fields(d, "spec", required=("mat", "media", "groups"),
                    optional=("machine", "preview_outline", "allow_unrecognized_tool"))
        machine = d.get("machine", DEFAULT_MACHINE)
        if type(machine) is not str:
            raise SpecError("spec.machine must be a string")
        if type(d["mat"]) is not str:
            raise SpecError("spec.mat must be a string")
        media = _seq(d["media"], "spec.media", 4)
        allow = d.get("allow_unrecognized_tool", {})
        if type(allow) is not dict:
            raise SpecError("spec.allow_unrecognized_tool must be an object")
        allowed: dict[int, bool] = {}
        for key, value in allow.items():
            if key not in ("1", "2"):
                raise SpecError(f"spec.allow_unrecognized_tool: unknown holder {key!r}")
            allowed[int(key)] = _bool(value, "spec.allow_unrecognized_tool")
        return JobSpec(
            mat=d["mat"],
            media=(_num(media[0], "spec.media"), _num(media[1], "spec.media"),
                   _num(media[2], "spec.media"), _num(media[3], "spec.media")),
            groups=[CutGroup.from_dict(g) for g in _seq(d["groups"], "spec.groups")],
            preview_outline=_bool(d.get("preview_outline", True), "spec.preview_outline"),
            allow_unrecognized_tool=allowed,
            machine=machine)


# --- strict type helpers ---------------------------------------------------------------------
def _fields(d: Any, what: str, required: tuple[str, ...],
            optional: tuple[str, ...] = ()) -> dict:
    if type(d) is not dict:
        raise SpecError(f"{what} must be an object")
    unknown = set(d) - set(required) - set(optional)
    if unknown:
        raise SpecError(f"{what}: unknown keys {sorted(map(str, unknown))}")
    missing = set(required) - set(d)
    if missing:
        raise SpecError(f"{what}: missing keys {sorted(missing)}")
    return d


def _seq(v: Any, what: str, length: int | None = None) -> list | tuple:
    if type(v) not in (list, tuple):
        raise SpecError(f"{what} must be a list")
    if length is not None and len(v) != length:
        raise SpecError(f"{what} must have {length} items")
    return v


def _int(v: Any, what: str) -> int:
    if type(v) is not int:            # bool is rejected too
        raise SpecError(f"{what} must be an integer")
    return v


def _bool(v: Any, what: str) -> bool:
    if type(v) is not bool:
        raise SpecError(f"{what} must be true or false")
    return v


def _num(v: Any, what: str) -> float:
    if type(v) not in (int, float):   # bool and numeric strings are rejected
        raise SpecError(f"{what} must be a number")
    try:
        return float(v)
    except OverflowError:             # an int beyond the float range (e.g. 10**400)
        raise SpecError(f"{what} is too large") from None
