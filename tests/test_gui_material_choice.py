# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Which preset the Send panel chooses, how its settings read, and the material blocker.

No display needed. Synthetic presets only (tests/gui_fakes.imported_store and BASIC_PRESETS).
"""
from dataclasses import replace

import pytest

import stubs

stubs.install()
from opencutter.gui import sending  # noqa: E402
from opencutter.gui.machine import Link, MachineState  # noqa: E402
from opencutter.job.plan import blade_depth_mm, blade_depth_text, build_plan  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec  # noqa: E402
from opencutter.presets.model import BASIC_PRESETS, PresetStore, basic_source  # noqa: E402
from gui_fakes import imported_store  # noqa: E402

BASIC = PresetStore(list(BASIC_PRESETS), basic_source())
VINYL = BASIC_PRESETS[0]
READY = MachineState(link=Link.CONNECTED, status="0", tools=(2, 4))


def test_the_basic_sets_vinyl_is_the_verified_preset():
    assert sending.verified_preset(BASIC) == VINYL and VINYL.verified
    assert (VINYL.speed, VINYL.force, VINYL.blade_depth) == (5, 10, 1)


def test_an_imported_store_without_verified_rows_has_none():
    store = imported_store()
    assert len(store.materials()) > 10 and not any(p.verified for p in store.presets)
    assert sending.verified_preset(store) is None
    # the material that used to be chosen by default: the first one, alphabetically
    assert store.materials()[0] == "Test Acetate, Thick"


def test_choose_preset_keeps_the_action_and_holder_else_takes_the_first():
    store = imported_store()
    card = "Test Card, Smooth"                            # Cut (Tool 1), Score (Tool 2), Sketch (Tool 1)
    assert [p.action for p in store.presets_for(card)] == ["Cut", "Sketch", "Score"]
    assert sending.choose_preset(store, card).action == "Cut"
    assert sending.choose_preset(store, card, "Score").holder == 2
    assert sending.choose_preset(store, card, "Sketch", holder=1).tool_code == 7
    vinyl = "Test Vinyl, Glossy"                          # Cut only: another action falls back
    assert sending.choose_preset(store, vinyl, "Score").action == "Cut"
    assert sending.choose_preset(store, None) is None
    assert sending.choose_preset(store, "No Such Material") is None


def test_choose_preset_keeps_the_chosen_tool_whatever_the_machine_reports():
    """The rule has no machine input, so the material list's line cannot depend on the tools
    loaded: the Tool cards say when another tool is needed."""
    pen1 = replace(BASIC_PRESETS[1], material="Two Pens", holder=1)
    pen2 = replace(BASIC_PRESETS[1], material="Two Pens", holder=2)
    store = PresetStore([pen1, pen2], basic_source())
    assert sending.choose_preset(store, "Two Pens").holder == 1
    assert sending.choose_preset(store, "Two Pens", holder=2).holder == 2
    assert sending.choose_preset(store, "Two Pens", holder=1).holder == 1
    only2 = PresetStore([pen2], basic_source())
    assert sending.choose_preset(only2, "Two Pens", holder=1).holder == 2


def test_blade_depth_is_the_studio_number_with_the_millimetres_beside_it():
    assert blade_depth_text(1) == "Blade Depth 1"
    assert blade_depth_text(1, mm=True) == "Blade Depth 1 (0.1 mm)"
    assert blade_depth_text(10, mm=True) == "Blade Depth 10 (1.0 mm)"
    assert blade_depth_mm(0) == "0.0 mm"


def test_settings_summary_and_depth_note():
    s = VINYL.to_tool_settings()
    assert sending.settings_summary(s) == "Blade Depth 1 · Force 10 · Speed 5 · 1 pass"
    assert sending.settings_summary(s, with_passes=False) == "Blade Depth 1 · Force 10 · Speed 5 · Accel. 3"
    assert sending.settings_summary(replace(s, passes=3)).endswith("· 3 passes")
    assert sending.depth_note(s) == "Blade Depth 1 = 0.1 mm"
    pen = BASIC_PRESETS[1].to_tool_settings()
    assert sending.settings_summary(pen) == "Force 7 · Speed 5 · 1 pass"      # no depth for a pen
    assert sending.depth_note(pen) is None and sending.depth_note(None) is None


def test_preset_lines_show_only_what_the_preset_carries():
    assert sending.preset_line(VINYL) == "Cut · AutoBlade · Blade Depth 1 · Force 10 · Speed 5"
    assert sending.preset_line(BASIC_PRESETS[1]) == "Sketch · Sketch Pen · Force 7 · Speed 5"
    store = imported_store()
    ratchet = store.presets_for("Test Felt")[0]
    assert ratchet.tool_code == 1 and ratchet.blade_depth is None
    assert "Blade Depth" not in sending.preset_line(ratchet)               # set by hand: no value
    kraft = next(p for p in store.presets if p.tool_code == 4)
    assert sending.preset_line(kraft).startswith(f"{kraft.action} · Kraft Blade, 3mm · Force ")
    two = replace(VINYL, passes=2)
    assert sending.preset_line(two).endswith("· Speed 5 · 2 passes")


def test_the_job_step_title_shows_the_same_blade_depth():
    spec = JobSpec(mat="12x12", media=(0.0, 0.0, 215.9, 279.4), preview_outline=False,
                   groups=[CutGroup(VINYL.to_tool_settings(), [[(20.0, 20.0), (40.0, 20.0)]])])
    titles = [step.title for step in build_plan(spec).steps]
    assert "Tool 1: AutoBlade, Blade Depth 1 (0.1 mm), Force 10, Speed 5" in titles
    pen = JobSpec(mat="12x12", media=(0.0, 0.0, 215.9, 279.4), preview_outline=False,
                  groups=[CutGroup(BASIC_PRESETS[1].to_tool_settings(), [[(20.0, 20.0), (40.0, 20.0)]])])
    assert "Tool 1: Sketch Pen, Force 7, Speed 5" in [step.title for step in build_plan(pen).steps]


def test_no_material_blocks_send_and_test_cut_first():
    check = sending.tool_check(VINYL.to_tool_settings(), READY)
    assert sending.blocker_for_test_cut(READY, None, False, material_chosen=False) == \
        "Choose a material first."
    offline = MachineState()
    assert sending.blocker_for_test_cut(offline, None, False, False) == sending.CHOOSE_MATERIAL
    assert sending.blocker_for_test_cut(READY, None, True, False) == "A job is already running."
    assert sending.blocker_for_test_cut(READY, check, False, True) is None
    assert sending.blocker_for_test_cut(READY, None, False, True) == "Choose a material and action."


def test_no_material_blocks_send_with_a_design_open(tmp_path):
    from opencutter.gui.document import PlacedDocument
    from opencutter.svg.importer import load_svg
    path = tmp_path / "d.svg"
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" '
                    'viewBox="0 0 100 100" fill="none" stroke="#000"><rect x="20" y="30" '
                    'width="40" height="20"/></svg>')
    media = (0.0, 0.0, 215.9, 279.4)
    doc = PlacedDocument(load_svg(str(path)), media)
    # with no design open either, the visible reason under Send is still the material
    assert sending.send_blocker(None, media, READY, None, False, False) == "Choose a material first."
    assert sending.send_blocker(None, media, READY, None, True, False) == "A job is already running."
    assert sending.send_blocker(None, media, READY, None, False, True) == "Open a design first."
    assert sending.send_blocker(doc, media, READY, None, False, False) == "Choose a material first."
    check = sending.tool_check(VINYL.to_tool_settings(), READY)
    assert sending.send_blocker(doc, media, READY, check, False, True) is None


@pytest.mark.parametrize("material", sorted(imported_store().materials()))
def test_every_imported_preset_line_is_its_choose_preset(material):
    store = imported_store()
    preset = sending.choose_preset(store, material)
    line = sending.preset_line(preset)
    assert line.startswith(f"{preset.action} · ")
    assert ("Blade Depth" in line) == (preset.blade_depth is not None)
