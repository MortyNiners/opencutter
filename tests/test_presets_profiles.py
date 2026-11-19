# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Presets and machine profiles: schema 2 with "machines", the checks per profile, for_profile,
the Basic set per profile, and why a build from before machine profiles never applies another
model's rows. Synthetic data only; nothing here touches a machine."""
import ast
import json
import logging
from dataclasses import fields, replace
from pathlib import Path

import pytest

import stubs

stubs.install()
from opencutter.machines import (CAMEO4, AccelForm, Dialect, Holder, HolderForm,  # noqa: E402
                                 IntRange, Ranges, Tool, ToolReply, by_id, enabled,
                                 enabled_profile, recognized_models)
from opencutter.presets import model  # noqa: E402
from opencutter.presets.model import (BASIC_MACHINES, BASIC_PRESETS,  # noqa: E402
                                      BASIC_PRESETS_BY_PROFILE, DEFAULT_MACHINES, FILE_KEYS,
                                      FILE_SCHEMA, FILE_SCHEMAS, Preset, PresetStore,
                                      basic_presets, basic_source, check_values, file_source,
                                      machine_profiles, presets_path)

CAMEO4_PLUS = enabled_profile("cameo4_plus")
CAMEO4_PRO = enabled_profile("cameo4_pro")
CAMEO5 = enabled_profile("cameo5")
PORTRAIT3 = enabled_profile("portrait3")
PORTRAIT4 = enabled_profile("portrait4")
FAMILY = {"cameo4", "cameo4_plus", "cameo4_pro"}


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    """Every test gets its own empty XDG_DATA_HOME, so no real presets file is read or written."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path / "data"


def blade(material="Test Vinyl", machines=None, **kw) -> dict:
    """An AutoBlade cut row in Tool 1, as a presets file holds it."""
    d = {"material": material, "material_type": "Vinyl", "action": "Cut", "holder": 1,
         "tool": "AutoBlade", "tool_code": 2, "speed": 5, "force": 10, "acceleration": 3,
         "offset": 18, "line_segment_overcut": False, "overcut": [1, 1], "blade_depth": 1,
         "passes": 1, **kw}
    if machines is not None:
        d["machines"] = machines
    return d


def pen(material="Test Paper", machines=None, **kw) -> dict:
    """A Sketch Pen row in Tool 1."""
    return blade(material, machines, **{"material_type": "Paper", "action": "Sketch",
                                        "tool": "Sketch Pen", "tool_code": 7, "offset": 0,
                                        "overcut": [0, 0], "blade_depth": None, **kw})


def write(rows, schema=2, **top) -> dict:
    """Write a presets file as a person or their own tool would; return what was written."""
    body = {"format": "opencutter-presets", "schema": schema, "presets": rows, **top}
    presets_path().parent.mkdir(parents=True, exist_ok=True)
    presets_path().write_text(json.dumps(body), encoding="utf-8")
    return body


def broken(caplog) -> PresetStore:
    """Load a file that must fall back to the Basic set, with a warning."""
    with caplog.at_level(logging.WARNING, logger="opencutter.presets.model"):
        s = PresetStore.load()
    assert s.presets == BASIC_PRESETS
    assert s.source["kind"] == "basic" and s.description == "Basic set"
    assert "presets.json" in s.source["file_error"]
    assert "using the Basic set" in caplog.text
    return s


def materials(store: PresetStore, profile) -> list[tuple[str, str, int]]:
    return [(p.material, p.action, p.holder) for p in store.for_profile(profile).presets]


# --- the schemas ----------------------------------------------------------------------------
def test_the_schema_constants():
    assert FILE_SCHEMA == 2 and FILE_SCHEMAS == (1, 2)
    assert DEFAULT_MACHINES == ("cameo4",) == (CAMEO4.id,)
    assert Preset(**{**blade(), "overcut": (1, 1)}).machines == ("cameo4",)


def test_a_schema_1_file_holds_cameo4_rows_only():
    write([blade(), pen()], schema=1, description="Old file")
    s = PresetStore.load()
    assert s.source == {"kind": "file", "description": "Old file"}
    assert [p.machines for p in s.presets] == [("cameo4",), ("cameo4",)]
    assert s.for_profile(CAMEO4).presets == s.presets
    for other in (CAMEO4_PLUS, CAMEO4_PRO, CAMEO5, PORTRAIT3, PORTRAIT4):
        view = s.for_profile(other)
        assert view.presets == [] and view.source == s.source       # the file replaces Basic


def test_schema_1_still_ignores_row_keys_it_does_not_know():
    write([{**blade(), "note": "from an older writer"}], schema=1)
    assert PresetStore.load().presets == [Preset.from_dict(blade())]


@pytest.mark.parametrize("machines", [["cameo4"], ["cameo5"], ["cameo4", "cameo5"], []])
def test_a_schema_1_file_cannot_name_machines(machines, caplog):
    """A build that reads schema 1 drops "machines" and would take the row as a CAMEO 4 row."""
    write([blade(), pen(machines=machines)], schema=1)
    assert "needs schema 2" in broken(caplog).source["file_error"]


@pytest.mark.parametrize("schema", [1, 2])
def test_machines_at_the_top_of_the_file_is_an_error(schema, caplog):
    write([blade()], schema=schema, machines=["cameo5"])
    assert "belongs in each preset" in broken(caplog).source["file_error"]


@pytest.mark.parametrize("key", ["machine", "Machines", "models", "notice"])
def test_schema_2_refuses_a_top_level_key_it_does_not_have(key, caplog):
    """A misspelt file-wide "machines" must not leave every row a CAMEO 4 row either."""
    write([blade()], schema=2, **{key: ["cameo5"]})
    assert f"unknown top-level key(s) ['{key}']" in broken(caplog).source["file_error"]


def test_schema_1_still_ignores_top_level_keys_it_does_not_use():
    assert FILE_KEYS == {"format", "schema", "description", "presets"}
    write([blade()], schema=1, notice="Do not share.", models=["x"])
    assert PresetStore.load().presets == [Preset.from_dict(blade())]
    write([blade()], schema=2, description="Mine")
    assert PresetStore.load().source == {"kind": "file", "description": "Mine"}


@pytest.mark.parametrize("schema", [True, 1.0, 2.0, "2", 0, 3, None, [2]])
def test_only_schema_1_and_2_are_read(schema, caplog):
    write([blade()], schema=schema)
    assert "of schema 1 or 2" in broken(caplog).source["file_error"]


def test_a_schema_2_file_names_machines_per_row():
    write([
        blade(machines=["cameo4"]),
        blade(machines=["cameo5"], force=40),                      # the CAMEO 5 takes FX 1..40
        pen(machines=["cameo4", "cameo4_plus", "cameo4_pro", "cameo5"]),
        blade("Test Card", machines=["portrait3"], material_type="Card", speed=8, offset=30),
    ], description="Several machines")
    s = PresetStore.load()
    assert s.source == {"kind": "file", "description": "Several machines"}
    assert [p.machines for p in s.presets] == [
        ("cameo4",), ("cameo5",), ("cameo4", "cameo4_plus", "cameo4_pro", "cameo5"),
        ("portrait3",)]
    vinyl, paper = ("Test Vinyl", "Cut", 1), ("Test Paper", "Sketch", 1)
    card = ("Test Card", "Cut", 1)
    assert materials(s, CAMEO4) == [vinyl, paper]
    assert materials(s, CAMEO5) == [vinyl, paper]
    assert [p.force for p in s.for_profile(CAMEO5).presets] == [40, 10]
    assert materials(s, CAMEO4_PLUS) == materials(s, CAMEO4_PRO) == [paper]
    assert materials(s, PORTRAIT3) == [card]
    assert materials(s, PORTRAIT4) == materials(s, "cameo5_plus") == []
    assert materials(s, "cameo5") == materials(s, CAMEO5)              # a profile id works too
    assert materials(s, by_id("cameo3")) == materials(s, "nothing") == []
    view = s.for_profile(CAMEO5)
    assert view.source == s.source and view.source is not s.source
    assert view.materials() == ["Test Paper", "Test Vinyl"]


def test_a_schema_2_row_without_machines_is_a_cameo4_row():
    write([blade(), pen(machines=["cameo5"])])
    s = PresetStore.load()
    assert [p.machines for p in s.presets] == [("cameo4",), ("cameo5",)]
    assert materials(s, CAMEO4) == [("Test Vinyl", "Cut", 1)]


def test_schema_2_refuses_a_field_it_does_not_have(caplog):
    """A misspelt "machines" must not leave a row of another model as a CAMEO 4 row."""
    write([blade(machine=["cameo5"], force=20)])
    assert "unknown field(s) ['machine']" in broken(caplog).source["file_error"]


@pytest.mark.parametrize("machines, reason", [
    (["cameo9"], "'cameo9' is not a machine OpenCutter knows"),
    (["CAMEO4"], "'CAMEO4' is not a machine OpenCutter knows"),
    (["cameo4", "cameo3"], "'cameo3' is the CAMEO 3, which OpenCutter cannot drive yet"),
    (["curio2"], "'curio2' is the CURIO 2, which OpenCutter cannot drive yet"),
    ([], "machines must be a non-empty list"),
    (["cameo4", "cameo4"], "list of different machine ids"),
    ([4], "list of different machine ids"),
    ([None], "list of different machine ids"),
    ("cameo4", "machines must be a list of machine ids"),
    ({"cameo4": True}, "machines must be a list of machine ids"),
    (None, "machines must be a list of machine ids"),
])
def test_a_wrong_machine_makes_the_file_broken(machines, reason, caplog):
    write([pen(), {**blade(), "machines": machines}])
    assert reason in broken(caplog).source["file_error"]


@pytest.mark.parametrize("model", recognized_models(), ids=lambda m: m.id)
def test_no_row_may_name_a_model_opencutter_only_recognizes(model, caplog):
    write([pen(machines=[model.id])])
    assert f"is the {model.name}, which OpenCutter cannot drive yet" in \
        broken(caplog).source["file_error"]


@pytest.mark.parametrize("profile", enabled(), ids=lambda p: p.id)
def test_every_model_opencutter_drives_can_have_rows(profile):
    """A Sketch Pen row at the low end of each profile's ranges loads and is offered to it."""
    r = profile.ranges
    accel = r.acceleration.lo if r.acceleration is not None else 1
    write([pen(machines=[profile.id], speed=r.speed.lo, force=r.force.lo, acceleration=accel)])
    s = PresetStore.load()
    assert s.source["kind"] == "file"
    assert materials(s, profile) == [("Test Paper", "Sketch", 1)]


# --- the values, per profile ----------------------------------------------------------------
@pytest.mark.parametrize("rows, reason", [
    ([blade(machines=["cameo4", "cameo5"], force=40)],
     "for the CAMEO 4: force 40 is outside 1..33"),
    ([blade(machines=["cameo5"], force=41)], "for the CAMEO 5: force 41 is outside 1..40"),
    ([blade(machines=["portrait3"], offset=46)], "for the Portrait 3: offset 46 is outside 0..45"),
    ([blade(machines=["portrait3"], speed=11)], "for the Portrait 3: speed 11 is outside 1..10"),
    ([blade(machines=["portrait3"], holder=2, blade_depth=None)], "holder 2 is not 1"),
    ([blade(machines=["portrait4"], acceleration=4)],
     "for the Portrait 4: acceleration 4 is outside 1..3"),
    ([pen(machines=["portrait3"], tool="Foil Tool", tool_code=8)],
     "tool_code 8 is not a tool the machine detects in Tool"),
    ([blade(machines=["cameo4"], holder=3)], "holder 3 is not 1 or 2"),
    ([pen(machines=["portrait4"], blade_depth=2)],
     "blade_depth works only for the AutoBlade; use null"),
])
def test_a_row_must_suit_every_machine_it_names(rows, reason, caplog):
    write(rows)
    assert reason in broken(caplog).source["file_error"]


def test_a_model_without_tj_does_not_use_the_acceleration():
    write([blade(machines=["portrait3"], acceleration=9, offset=18),
           pen(machines=["portrait4"], tool="Foil Tool", tool_code=8, acceleration=3)])
    s = PresetStore.load()
    assert s.source["kind"] == "file"
    assert [p.acceleration for p in s.for_profile(PORTRAIT3).presets] == [9]
    assert [p.tool_code for p in s.for_profile(PORTRAIT4).presets] == [8]


# A one-holder cutter that is not in the registry: narrow ranges, no TJ, four tools.
ONE_HOLDER = replace(
    CAMEO4, id="test_one_holder", name="Test Cutter",
    holders=(Holder(1, "Tool", (Tool(0, "No Tool Detected", usable=False),
                                Tool(1, "Ratchet Blade", blade=True),
                                Tool(2, "AutoBlade", blade=True, depth=True),
                                Tool(7, "Sketch Pen", action="Sketch"))),),
    ranges=Ranges(speed=IntRange(1, 8), force=IntRange(1, 20), acceleration=None,
                  offset=IntRange(0, 45), blade_depth=IntRange(0, 10), overcut=IntRange(0, 5),
                  passes=IntRange(1, 3)),
    dialect=Dialect(holders=HolderForm.SINGLE, accel=AccelForm.NONE,
                    tool_reply=ToolReply.ONE_DIGIT, tool_slots=(0,)))
GOOD = Preset.from_dict(blade(offset=30, overcut=[5, 5], blade_depth=3, passes=3, speed=8))


def test_check_values_takes_the_rules_from_the_profile():
    check_values(GOOD, ONE_HOLDER)
    check_values(replace(GOOD, acceleration=99), ONE_HOLDER)       # no TJ: not used
    check_values(Preset.from_dict(pen()), ONE_HOLDER)
    check_values(replace(GOOD, tool="Ratchet Blade", tool_code=1, blade_depth=None), ONE_HOLDER)


@pytest.mark.parametrize("changes, reason", [
    ({"holder": 2}, "holder 2 is not 1"),
    ({"tool_code": 0}, "tool_code 0 is not a tool the machine detects in Tool"),
    ({"tool_code": 3, "blade_depth": None},
     "tool_code 3 is not a tool the machine detects in Tool"),
    ({"speed": 9}, "speed 9 is outside 1..8"),
    ({"force": 21}, "force 21 is outside 1..20"),
    ({"offset": 46}, "offset 46 is outside 0..45"),
    ({"passes": 4}, "passes 4 is outside 1..3"),
    ({"blade_depth": 11}, "blade_depth 11 is outside 0..10"),
    ({"overcut": (6, 0)}, "overcut [6, 0] is outside 0..5"),
    ({"tool_code": 1}, "blade_depth works only for the AutoBlade; use null"),
    ({"tool_code": 7, "blade_depth": None}, "the Sketch Pen is not a blade"),
])
def test_check_values_refuses_what_the_profile_does_not_accept(changes, reason):
    with pytest.raises(ValueError, match=reason.replace("[", r"\[").replace("]", r"\]")) as e:
        check_values(replace(GOOD, **changes), ONE_HOLDER)
    assert "preset 'Test Vinyl' / 'Cut' for the Test Cutter: " in str(e.value)


@pytest.mark.parametrize("value", [None, 2.5, "3", True])
def test_without_tj_the_acceleration_is_still_a_whole_number(value):
    """Not sent to a model without TJ, but a whole number, as job.validate requires."""
    with pytest.raises(ValueError, match="acceleration .* is not a whole number"):
        check_values(replace(GOOD, acceleration=value), ONE_HOLDER)


@pytest.mark.parametrize("overcut", [[1, 1], (1,), (1, 1, 1), 1])
def test_check_values_refuses_an_overcut_that_is_not_a_pair(overcut):
    with pytest.raises(ValueError, match="overcut"):
        check_values(replace(GOOD, overcut=overcut), ONE_HOLDER)


def test_check_values_with_a_profile_checks_only_the_values():
    """GOOD names the CAMEO 4 alone; its values still suit the test cutter."""
    assert GOOD.machines == ("cameo4",)
    check_values(GOOD, ONE_HOLDER)


def test_presets_take_every_rule_from_the_profile():
    """No CAMEO 4 constants from the device or job layers: only job.spec's shapes
    and the machine profiles."""
    tree = ast.parse(Path(model.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    ours = {m for m in imported if m and m.startswith("opencutter")}
    assert ours == {"opencutter.job.spec", "opencutter.machines"}


def test_a_file_cannot_name_a_profile_outside_the_registry():
    with pytest.raises(ValueError, match="'test_one_holder' is not a machine OpenCutter knows"):
        Preset.from_dict(blade(machines=[ONE_HOLDER.id]))


def test_without_a_profile_check_values_checks_every_machine_the_row_names():
    strong = replace(BASIC_PRESETS[0], force=40, machines=("cameo5",))
    check_values(strong)
    assert machine_profiles(strong) == (CAMEO5,)
    both = replace(strong, machines=("cameo5", "cameo4"))
    assert machine_profiles(both) == (CAMEO5, CAMEO4)
    with pytest.raises(ValueError, match="for the CAMEO 4: force 40 is outside 1..33"):
        check_values(both)
    check_values(both, CAMEO5)
    with pytest.raises(ValueError, match="'cameo3' is the CAMEO 3"):
        machine_profiles(replace(strong, machines=("cameo3",)))


@pytest.mark.parametrize("machines", [(), "cameo4", ["cameo4"], ("cameo4", "cameo4")])
def test_a_row_built_in_code_with_malformed_machines_fails_closed(machines):
    """A row that names no machine is never accepted, and a plain string never matches a
    longer id ("cameo4" is part of "cameo4_plus")."""
    row = replace(BASIC_PRESETS[0], machines=machines)
    with pytest.raises(ValueError, match="machines must be a non-empty list"):
        check_values(row)
    store = PresetStore([replace(BASIC_PRESETS[0], machines="cameo4_plus")],
                        {"kind": "file", "description": "Built in code"})
    assert store.for_profile(CAMEO4).presets == []


# --- rows -----------------------------------------------------------------------------------
def test_rows_that_differ_only_in_their_machines_are_different():
    assert replace(BASIC_PRESETS[0], machines=("cameo5",)) != BASIC_PRESETS[0]
    assert replace(BASIC_PRESETS[0], machines=("cameo4",)) == BASIC_PRESETS[0]


def test_to_dict_names_machines_only_for_a_row_beyond_the_cameo4():
    assert "machines" not in BASIC_PRESETS[0].to_dict()
    for machines in (("cameo5",), ("cameo4", "cameo4_plus"), ("portrait4", "cameo4")):
        p = replace(BASIC_PRESETS[1], machines=machines, acceleration=2)
        d = p.to_dict()
        assert d["machines"] == list(machines)
        assert Preset.from_dict(d) == p
        assert Preset.from_dict(json.loads(json.dumps(d))) == p
    assert Preset.from_dict(BASIC_PRESETS[0].to_dict()) == BASIC_PRESETS[0]


def test_a_schema_2_file_round_trip():
    rows = [blade(machines=["cameo5", "cameo5_plus"], force=38), pen(), pen(machines=["portrait4"])]
    write(rows)
    loaded = PresetStore.load().presets
    write([p.to_dict() for p in loaded])
    assert PresetStore.load().presets == loaded
    assert [p.machines for p in loaded] == [("cameo5", "cameo5_plus"), ("cameo4",), ("portrait4",)]


@pytest.mark.parametrize("rows", [
    [blade(machines=["cameo5"]), blade(machines=["cameo5"], force=12)],
    [blade(machines=["cameo4", "cameo5"]), blade(machines=["cameo5"])],
    [blade(), blade(machines=["cameo4"])],                       # no "machines" is the CAMEO 4
])
def test_a_material_action_and_holder_appear_once_per_machine(rows, caplog):
    write(rows)
    assert "in Tool 1 appears twice for the CAMEO" in broken(caplog).source["file_error"]


def test_the_same_material_may_have_a_row_for_each_machine():
    write([blade(), blade(machines=["cameo5"], force=30), blade(machines=["cameo4_plus"])])
    s = PresetStore.load()
    assert [p.force for p in s.presets_for("Test Vinyl")] == [10, 30, 10]
    for profile, force in ((CAMEO4, 10), (CAMEO5, 30), (CAMEO4_PLUS, 10)):
        assert [p.force for p in s.for_profile(profile).presets_for("Test Vinyl")] == [force]


# --- the Basic set ------------------------------------------------------------------------
def test_basic_presets_is_the_cameo4_set():
    assert BASIC_PRESETS is BASIC_PRESETS_BY_PROFILE["cameo4"]
    assert BASIC_PRESETS == BASIC_PRESETS_BY_PROFILE["cameo4"]
    assert all(p.machines == ("cameo4",) for p in BASIC_PRESETS)
    assert basic_presets(CAMEO4) == basic_presets("cameo4") == BASIC_PRESETS
    assert basic_presets(CAMEO4) is not BASIC_PRESETS                    # a new list


def test_the_basic_set_applies_to_the_cameo4_family_only():
    assert set(BASIC_PRESETS_BY_PROFILE) == set(BASIC_MACHINES) == FAMILY
    for machine, rows in BASIC_PRESETS_BY_PROFILE.items():
        profile = enabled_profile(machine)
        assert profile is not None, machine
        assert [replace(p, machines=("cameo4",), verified=False) for p in rows] == \
            [replace(p, verified=False) for p in BASIC_PRESETS]             # the same values
        assert all(p.machines == (machine,) for p in rows)
        for p in rows:
            check_values(p)
            check_values(p, profile)
    for machine in FAMILY - {"cameo4"}:                       # cut on a CAMEO 4 only
        assert not any(p.verified for p in BASIC_PRESETS_BY_PROFILE[machine])
    for profile in enabled():
        if profile.id not in FAMILY:
            assert basic_presets(profile) == basic_presets(profile.id) == []
    for model in recognized_models():
        assert basic_presets(model) == []


def test_without_a_file_each_machine_gets_its_basic_set():
    s = PresetStore.load()
    assert s.presets == BASIC_PRESETS and s.source == basic_source()
    assert s.for_profile(CAMEO4).presets == BASIC_PRESETS
    assert s.for_profile(CAMEO4_PLUS).presets == BASIC_PRESETS_BY_PROFILE["cameo4_plus"]
    assert s.for_profile("cameo4_pro").presets == BASIC_PRESETS_BY_PROFILE["cameo4_pro"]
    for other in (CAMEO5, PORTRAIT3, PORTRAIT4, "cameo_pro_mk2", by_id("cameo3")):
        view = s.for_profile(other)
        assert view.presets == [] and view.source == basic_source()
    view = s.for_profile(CAMEO4)
    view.presets.clear()
    assert len(BASIC_PRESETS) == 2 and s.for_profile(CAMEO4).presets == BASIC_PRESETS


def test_the_cameo4_view_of_cameo4_rows_is_the_whole_store_whatever_its_source():
    """The GUI tests build Basic-set stores of their own rows (tests/test_gui_logic.py,
    tests/test_gui_material_choice.py): filtering for the CAMEO 4 must not swap them for the
    built-in rows, or the CAMEO 4's material list would change."""
    felt = replace(BASIC_PRESETS[0], material="Felt", action="Kraft Cut", holder=2,
                   tool="Kraft Blade, 3mm", tool_code=4, blade_depth=None)
    pens = [replace(BASIC_PRESETS[1], material="Two Pens", holder=h) for h in (1, 2)]
    for rows in ([*BASIC_PRESETS, felt], pens, [pens[1]]):
        for source in (basic_source(), file_source("Mine")):
            view = PresetStore(list(rows), source).for_profile(CAMEO4)
            assert view.presets == rows and view.source == source


def test_a_basic_store_gives_the_plus_and_the_pro_its_own_cameo4_rows():
    felt = replace(BASIC_PRESETS[0], material="Felt", verified=True)
    s = PresetStore([felt, BASIC_PRESETS[1]], basic_source())
    for machine in ("cameo4_plus", "cameo4_pro"):
        view = s.for_profile(machine)
        assert view.presets == [replace(felt, machines=(machine,), verified=False),
                                replace(BASIC_PRESETS[1], machines=(machine,))]
        assert view.for_profile(machine).presets == view.presets          # filtering again
        assert view.for_profile(CAMEO4).presets == []
        assert s.for_profile(CAMEO4).for_profile(machine).presets == view.presets
    assert s.for_profile(CAMEO5).presets == s.for_profile(PORTRAIT4).presets == []
    # a file's CAMEO 4 rows are the file's: they are never given to the Plus
    assert PresetStore(list(s.presets), file_source()).for_profile(CAMEO4_PLUS).presets == []


def test_a_basic_store_never_doubles_a_row_the_plus_has_already():
    own = replace(BASIC_PRESETS[0], machines=("cameo4_plus",), force=12)
    s = PresetStore([*BASIC_PRESETS, own], basic_source())
    assert [(p.material, p.force) for p in s.for_profile(CAMEO4_PLUS).presets] == [
        ("Vinyl", 12), ("Sketch Pen", 7)]


def test_the_plus_and_the_pro_get_only_basic_rows_their_profile_accepts(monkeypatch):
    """Should the Plus's ranges ever differ from the CAMEO 4's, a Basic row it would refuse is
    left out, not given to it."""
    narrow = replace(CAMEO4_PLUS, ranges=replace(CAMEO4_PLUS.ranges, force=IntRange(1, 8)))
    monkeypatch.setattr(model, "enabled_profile",
                        lambda i: narrow if i == "cameo4_plus" else enabled_profile(i))
    view = PresetStore(list(BASIC_PRESETS), basic_source()).for_profile("cameo4_plus")
    assert [p.material for p in view.presets] == ["Sketch Pen"]                # force 10 > 8
    assert [p.material for p in model._for_family(BASIC_PRESETS, "cameo4_pro")] == \
        ["Vinyl", "Sketch Pen"]


@pytest.mark.parametrize("profile", [None, 4, ("cameo4",), b"cameo4"])
def test_for_profile_needs_a_profile_or_its_id(profile):
    with pytest.raises(TypeError, match="a machine profile or its id"):
        PresetStore(list(BASIC_PRESETS), basic_source()).for_profile(profile)
    with pytest.raises(TypeError, match="a machine profile or its id"):
        basic_presets(profile)


def test_a_broken_file_gives_each_family_machine_the_basic_set_and_its_error(caplog):
    write([blade(machines=["cameo3"])])
    s = broken(caplog)
    plus = s.for_profile(CAMEO4_PLUS)
    assert plus.presets == BASIC_PRESETS_BY_PROFILE["cameo4_plus"]
    assert plus.source["file_error"] == s.source["file_error"]
    assert s.for_profile(CAMEO5).presets == []


# --- builds from before machine profiles --------------------------------------------------
def old_build_reads(data: object) -> bool:
    """The check a build from before machine profiles (OpenCutter 0.1.0.dev0) makes in
    PresetStore.load before it reads any row: the format, and a schema equal to 1. It then
    drops the keys it does not know, "machines" included, and takes every row as a CAMEO 4
    row."""
    return (isinstance(data, dict) and data.get("format") == "opencutter-presets"
            and data.get("schema") == 1)


# The row fields of that build. A new Preset field is one more key it ignores: check what that
# means for it before adding the field here as well.
OLD_FIELDS = ("material", "material_type", "action", "holder", "tool", "tool_code", "speed",
              "force", "acceleration", "offset", "line_segment_overcut", "overcut", "blade_depth",
              "passes", "verified")


def old_build_rows(data: dict) -> list[dict]:
    """The rows as that build takes them: its own fields only, "verified" false when left out."""
    return [{k: d.get(k, False) if k == "verified" else d[k] for k in OLD_FIELDS}
            for d in data["presets"]]


FILES = [
    dict(rows=[blade(), pen()], schema=1),
    dict(rows=[blade(), pen(machines=["cameo5"])], schema=1),
    dict(rows=[blade(), pen(machines=["cameo4"])], schema=1),
    dict(rows=[blade(), pen()], schema=1, machines=["cameo5"]),
    dict(rows=[blade(), {**pen(), "Machines": ["cameo5"], "note": "x"}], schema=1),
    dict(rows=[blade(), pen()], schema=1, machine=["cameo5"], notice="x"),
    dict(rows=[blade(), pen(machines=["cameo5"])], schema=True),
    dict(rows=[blade(), pen(machines=["cameo5"])], schema=1.0),
    dict(rows=[blade(), pen()], schema=2),
    dict(rows=[blade(machines=["cameo5"], force=40), pen(machines=["portrait3"])], schema=2),
    dict(rows=[blade(machines=["cameo4", "cameo5"]), pen()], schema=2),
    dict(rows=[blade(), pen(machines=["cameo4"])], schema=2),
    dict(rows=[blade(), {**pen(), "machine": ["cameo5"]}], schema=2),
    dict(rows=[blade(), pen()], schema=2, machine=["cameo5"]),
]


@pytest.mark.parametrize("file", FILES, ids=range(len(FILES)))
def test_a_file_both_builds_read_holds_cameo4_rows_for_both(file):
    """What an older build reads (it ignores "machines") is what this build reads, and a row of
    another model reaches only this build: an older build refuses its file."""
    assert set(OLD_FIELDS) == {f.name for f in fields(Preset)} - {"machines"}
    body = write(file["rows"], file["schema"], **{k: v for k, v in file.items()
                                                  if k not in ("rows", "schema")})
    s = PresetStore.load()
    if s.source["kind"] != "file":
        return                                                     # refused here: Basic set
    if old_build_reads(body):
        assert all(p.machines == DEFAULT_MACHINES for p in s.presets)
        assert [{k: v for k, v in p.to_dict().items() if k != "machines"}
                for p in s.presets] == old_build_rows(body)
    if any(p.machines != DEFAULT_MACHINES for p in s.presets):
        assert not old_build_reads(body)


def test_an_older_build_refuses_every_schema_2_file():
    body = write([blade(machines=["cameo5"], force=40)])
    assert PresetStore.load().source["kind"] == "file"
    assert body["schema"] == FILE_SCHEMA == 2 and not old_build_reads(body)
