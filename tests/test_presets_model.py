# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Preset model and store: lookups, ToolSettings, the presets file. Synthetic data only."""
import json
import logging
from dataclasses import replace

import pytest

from opencutter.job.spec import CutGroup, JobSpec, ToolSettings
from opencutter.job.validate import validate_spec
from opencutter.presets.model import (BASIC_PRESETS, Preset, PresetStore, basic_source,
                                      check_values, file_source, presets_path)


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    """Every test gets its own empty XDG_DATA_HOME, so no real presets file is read or written."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path / "data"


def preset(material, material_type="Paper", action="Cut", holder=1, **kw):
    base = Preset(material=material, material_type=material_type, action=action, holder=holder,
                  tool="AutoBlade", tool_code=2, speed=4, force=12, acceleration=2, offset=18,
                  line_segment_overcut=True, overcut=(2, 3), blade_depth=4, passes=2)
    return replace(base, **kw)


@pytest.fixture
def store():
    return PresetStore([
        preset("Test Vinyl, Matte", "Vinyl"),
        preset("Test Vinyl, Glossy", "Vinyl"),
        preset("Test Card, Thick", "Card", action="Score"),
        preset("Test Card, Thick", "Card", action="Cut"),
        preset("Test Card, Thick", "Card", action="Kraft Cut", holder=2, tool_code=4,
               blade_depth=None),
        preset("test paper", "Paper", action="Sketch", tool="Sketch Pen", tool_code=7,
               offset=0, line_segment_overcut=False, overcut=(0, 0), blade_depth=None),
    ], file_source("Test presets"))


def test_materials_and_types_are_sorted_case_insensitively(store):
    assert store.materials() == ["Test Card, Thick", "test paper", "Test Vinyl, Glossy",
                                 "Test Vinyl, Matte"]
    assert store.material_types() == ["Card", "Paper", "Vinyl"]
    assert store.materials("Vinyl") == ["Test Vinyl, Glossy", "Test Vinyl, Matte"]
    assert store.materials("Nothing") == []


def test_presets_for_orders_by_holder_then_action(store):
    rows = store.presets_for("Test Card, Thick")
    assert [(p.holder, p.action) for p in rows] == [(1, "Cut"), (1, "Score"), (2, "Kraft Cut")]
    assert store.presets_for("Unknown") == []


def test_search_matches_all_words_in_name_or_type(store):
    assert store.search("") == store.materials()
    assert store.search("VINYL") == ["Test Vinyl, Glossy", "Test Vinyl, Matte"]
    assert store.search("matte vinyl") == ["Test Vinyl, Matte"]
    assert store.search("card") == ["Test Card, Thick"]              # by name
    assert store.search("paper") == ["test paper"]
    assert store.search("thick", material_type="Vinyl") == []
    assert store.search("test", material_type="Card") == ["Test Card, Thick"]
    assert store.search("felt") == []


def test_to_tool_settings_copies_every_firmware_field():
    p = preset("Test", blade_depth=4)
    assert p.to_tool_settings() == ToolSettings(
        holder=1, tool_code=2, speed=4, force=12, acceleration=2, offset=18,
        line_segment_overcut=True, overcut=(2, 3), blade_depth=4, passes=2)


@pytest.mark.parametrize("basic", BASIC_PRESETS, ids=lambda p: p.material)
def test_basic_presets_pass_the_job_validator(basic):
    square = [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0), (10.0, 10.0)]
    spec = JobSpec(mat="12x12", media=(0.0, 0.0, 100.0, 100.0),
                   groups=[CutGroup(settings=basic.to_tool_settings(), paths=[square])])
    validate_spec(spec)


def test_basic_set_is_small_and_marks_only_the_vinyl_as_verified():
    vinyl = next(p for p in BASIC_PRESETS if p.verified)
    assert [p.verified for p in BASIC_PRESETS].count(True) == 1
    assert (vinyl.action, vinyl.tool, vinyl.tool_code, vinyl.holder) == ("Cut", "AutoBlade", 2, 1)
    assert (vinyl.speed, vinyl.force, vinyl.acceleration, vinyl.offset) == (5, 10, 3, 18)
    assert (vinyl.overcut, vinyl.blade_depth, vinyl.line_segment_overcut) == ((1, 1), 1, False)
    pen = next(p for p in BASIC_PRESETS if p.action == "Sketch")
    assert (pen.tool, pen.tool_code, pen.offset, pen.overcut, pen.blade_depth) == \
        ("Sketch Pen", 7, 0, (0, 0), None)


def test_without_a_presets_file_the_store_is_the_basic_set():
    s = PresetStore.load()
    assert s.presets == BASIC_PRESETS
    assert s.source == basic_source() == {"kind": "basic", "description": "Basic set"}
    assert s.description == "Basic set"


def test_presets_path_follows_xdg_data_home(data_home, monkeypatch, tmp_path):
    assert presets_path() == data_home / "opencutter" / "presets.json"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", "relative/dir")             # ignored, as the spec says
    assert presets_path() == tmp_path / "home" / ".local" / "share" / "opencutter" / "presets.json"


def write_file(presets=BASIC_PRESETS, **data):
    """Write a presets file as a person or their own tool would (the app never writes it)."""
    presets_path().parent.mkdir(parents=True, exist_ok=True)
    body = {"format": "opencutter-presets", "schema": 1,
            "presets": [p.to_dict() for p in presets], **data}
    presets_path().write_text(json.dumps(body), encoding="utf-8")


def test_presets_file_round_trip(store, data_home):
    write_file(store.presets, description="Test presets")
    assert presets_path() == data_home / "opencutter" / "presets.json"
    loaded = PresetStore.load()
    assert loaded.presets == store.presets
    assert loaded.source == store.source == {"kind": "file", "description": "Test presets"}
    assert all(isinstance(p.overcut, tuple) for p in loaded.presets)


def test_the_app_never_writes_the_presets_file():
    assert not [name for name in vars(PresetStore) if "save" in name or "write" in name]


@pytest.mark.parametrize("description, shown", [
    (None, "Presets file"), ("", "Presets file"), ("   ", "Presets file"), (42, "Presets file"),
    ("My vinyl list", "My vinyl list"), ("  First line\nsecond line", "First line"),
    ("x" * 200, "x" * 79 + "…"),
])
def test_the_description_comes_from_the_file(description, shown):
    if description is None:
        write_file()
    else:
        write_file(description=description)
    s = PresetStore.load()
    assert s.source == {"kind": "file", "description": shown} and s.description == shown


def test_a_file_from_an_older_writer_still_loads():
    """Extra keys such as "source" and "notice" (written by earlier versions) are ignored."""
    write_file(source={"kind": "something", "count": 2}, notice="Do not share.")
    s = PresetStore.load()
    assert s.presets == BASIC_PRESETS and s.description == "Presets file"


def wrong(**changes) -> dict:
    return replace(BASIC_PRESETS[0], **changes).to_dict()


@pytest.mark.parametrize("content", [
    "{not json",
    "[]",
    json.dumps({"format": "something-else", "schema": 1, "presets": []}),
    json.dumps({"format": "opencutter-presets", "schema": 99, "presets": []}),
    json.dumps({"format": "opencutter-presets", "schema": 1}),
    json.dumps({"format": "opencutter-presets", "schema": 1, "presets": []}),
    json.dumps({"format": "opencutter-presets", "schema": 1, "presets": {"a": 1}}),
    json.dumps({"format": "opencutter-presets", "schema": 1, "presets": [{"material": "x"}]}),
    json.dumps({"format": "opencutter-presets", "schema": 1, "presets": [wrong(speed=31)]}),
    json.dumps({"format": "opencutter-presets", "schema": 1,
                "presets": [BASIC_PRESETS[0].to_dict()] * 2}),
])
def test_unreadable_presets_file_falls_back_to_the_basic_set(content, caplog):
    presets_path().parent.mkdir(parents=True)
    presets_path().write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="opencutter.presets.model"):
        s = PresetStore.load()
    assert s.presets == BASIC_PRESETS
    assert s.source["kind"] == "basic" and s.description == "Basic set"
    assert "presets.json" in s.source["file_error"]
    assert "using the Basic set" in caplog.text


@pytest.mark.parametrize("changes, reason", [
    ({"holder": 3}, "holder 3"),
    ({"tool_code": 0}, "tool_code 0"),
    ({"tool_code": 8}, "tool_code 8 is not a tool the machine detects in Tool 1"),
    ({"speed": 0}, "speed 0 is outside 1..30"),
    ({"speed": 31}, "speed 31"),
    ({"force": 34}, "force 34 is outside 1..33"),
    ({"acceleration": 11}, "acceleration 11"),
    ({"offset": 100}, "offset 100"),
    ({"passes": 0}, "passes 0"),
    ({"blade_depth": 11}, "blade_depth 11"),
    ({"overcut": (0, 10)}, "overcut [0, 10]"),
    ({"holder": 2, "blade_depth": 1}, "only for the AutoBlade in Tool 1"),
    ({"tool_code": 1, "blade_depth": 1}, "only for the AutoBlade in Tool 1"),
    ({"tool_code": 7, "blade_depth": None}, "is not a blade"),
])
def test_values_the_machine_does_not_accept_are_refused(changes, reason):
    p = replace(BASIC_PRESETS[0], **changes)
    with pytest.raises(ValueError, match=reason.replace("[", r"\[").replace("]", r"\]")):
        check_values(p)
    with pytest.raises(ValueError):
        Preset.from_dict(p.to_dict())


def test_rotary_and_punch_rows_are_accepted_in_a_file():
    """The app hides them (gui.sending.is_supported); they are not errors in the file."""
    rotary = replace(BASIC_PRESETS[0], action="Rotary Cut", holder=2, tool="Rotary Blade",
                     tool_code=5, offset=40, blade_depth=None)
    punch = replace(BASIC_PRESETS[0], action="Punch", holder=2, tool="Punch Tool", tool_code=6,
                    offset=0, overcut=(0, 0), blade_depth=None)
    for p in (rotary, punch):
        check_values(p)
        assert Preset.from_dict(p.to_dict()) == p


@pytest.mark.parametrize("field, value", [
    ("speed", "5"), ("speed", True), ("blade_depth", 1.5), ("overcut", [1]),
    ("overcut", 3), ("line_segment_overcut", 0), ("material", None),
])
def test_from_dict_rejects_wrong_types(field, value):
    d = preset("Test").to_dict()
    d[field] = value
    with pytest.raises(ValueError):
        Preset.from_dict(d)


def test_from_dict_round_trip_and_default_verified():
    d = preset("Test").to_dict()
    assert Preset.from_dict(d) == preset("Test")
    del d["verified"]
    assert Preset.from_dict(d).verified is False
    del d["speed"]
    with pytest.raises(ValueError):
        Preset.from_dict(d)
