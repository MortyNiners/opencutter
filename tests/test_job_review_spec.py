# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Review follow-up: an integer beyond the float range in a JobSpec is a SpecError (a clean
refusal with a message), not an OverflowError escaping from float()."""
import json

import pytest

from opencutter.job.spec import CutGroup, JobSpec, SpecError, ToolSettings

VINYL = ToolSettings(holder=1, tool_code=2, speed=5, force=10, acceleration=3, offset=18,
                     line_segment_overcut=False, overcut=(1, 1), blade_depth=1, passes=1)


def spec_dict() -> dict:
    spec = JobSpec(mat="12x12", media=(0.0, 0.0, 100.0, 100.0),
                   groups=[CutGroup(settings=VINYL, paths=[[(10.0, 10.0), (20.0, 10.0)]])])
    return json.loads(json.dumps(spec.to_dict()))


@pytest.mark.parametrize("where", ["point", "media"])
def test_a_huge_integer_is_a_spec_error(where):
    d = spec_dict()
    if where == "point":
        d["groups"][0]["paths"][0][1] = [10**400, 20]
    else:
        d["media"][2] = 10**400
    with pytest.raises(SpecError, match="too large"):
        JobSpec.from_dict(d)


def test_ordinary_numbers_still_round_trip():
    d = spec_dict()
    assert JobSpec.from_dict(d).to_dict() == d
