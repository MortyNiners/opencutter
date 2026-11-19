# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""JobSpec serialization tests: JSON round trip and strict types."""
import copy
import json

import pytest

from opencutter.job.spec import CutGroup, JobSpec, SpecError, ToolSettings

PEN = ToolSettings(holder=1, tool_code=7, speed=5, force=7, acceleration=3, offset=0,
                   line_segment_overcut=False, overcut=(0, 0), blade_depth=None)
KRAFT = ToolSettings(holder=2, tool_code=4, speed=6, force=8, acceleration=1, offset=68,
                     line_segment_overcut=True, overcut=(1, 1), blade_depth=None, passes=2)
SPEC = JobSpec(mat="12x12", media=(10.0, 20.0, 200.0, 150.5),
               groups=[CutGroup(PEN, [[(11.0, 21.5), (40.25, 21.5)]]),
                       CutGroup(KRAFT, [[(50.0, 50.0), (60.0, 50.0), (60.0, 60.0), (50.0, 50.0)]])],
               preview_outline=False, allow_unrecognized_tool={1: True})


def test_round_trip():
    assert JobSpec.from_dict(SPEC.to_dict()) == SPEC


def test_json_round_trip():
    text = json.dumps(SPEC.to_dict())
    assert JobSpec.from_dict(json.loads(text)) == SPEC
    assert json.loads(text)["allow_unrecognized_tool"] == {"1": True}


def test_types_after_from_dict():
    d = SPEC.to_dict()
    d["groups"][0]["paths"][0][0] = [11, 21]            # JSON integers are fine for mm
    spec = JobSpec.from_dict(d)
    assert spec.groups[0].paths[0][0] == (11.0, 21.0)
    assert all(type(v) is float for v in spec.groups[0].paths[0][0])
    assert type(spec.media) is tuple and type(spec.groups[1].settings.overcut) is tuple


def test_defaults():
    d = SPEC.to_dict()
    del d["preview_outline"], d["allow_unrecognized_tool"], d["groups"][0]["settings"]["passes"]
    spec = JobSpec.from_dict(d)
    assert spec.preview_outline is True and spec.allow_unrecognized_tool == {}
    assert spec.groups[0].settings.passes == 1


def _mutate(path, value):
    d = copy.deepcopy(SPEC.to_dict())
    *keys, last = path
    target = d
    for k in keys:
        target = target[k]
    if value is KeyError:
        del target[last]
    else:
        target[last] = value
    return d


@pytest.mark.parametrize("path, value", [
    (("groups", 0, "settings", "speed"), True),
    (("groups", 0, "settings", "speed"), 5.0),
    (("groups", 0, "settings", "speed"), "5"),
    (("groups", 0, "settings", "speed"), None),
    (("groups", 0, "settings", "blade_depth"), 1.0),
    (("groups", 0, "settings", "line_segment_overcut"), 0),
    (("groups", 0, "settings", "overcut"), [0, 0, 0]),
    (("groups", 0, "settings", "overcut"), "00"),
    (("groups", 0, "settings", "holder"), KeyError),
    (("groups", 0, "settings", "extra"), 1),
    (("groups", 0, "paths", 0, 0), [1.0, 2.0, 3.0]),
    (("groups", 0, "paths", 0, 0), ["1", 2.0]),
    (("groups", 0, "paths", 0, 0), [True, 2.0]),
    (("groups", 0, "paths", 0), "path"),
    (("groups", 0, "paths"), {}),
    (("groups", 0, "extra"), []),
    (("groups", 0), []),
    (("groups",), {}),
    (("media",), [0.0, 0.0, 10.0]),
    (("media",), [0.0, 0.0, 10.0, None]),
    (("mat",), 12),
    (("mat",), KeyError),
    (("preview_outline",), "yes"),
    (("allow_unrecognized_tool",), {"3": True}),
    (("allow_unrecognized_tool",), {"1": 1}),
    (("allow_unrecognized_tool",), [1]),
    (("bytes",), "TG1"),
])
def test_strict_types(path, value):
    with pytest.raises(SpecError):
        JobSpec.from_dict(_mutate(path, value))


def test_not_a_dict():
    for bad in (None, [], "spec", 1):
        with pytest.raises(SpecError):
            JobSpec.from_dict(bad)
