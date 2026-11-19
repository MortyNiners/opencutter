# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The models OpenCutter recognizes but refuses (opencutter.machines.recognized).

Every model of the owner's RECOGNIZED list is named (its FG text where one is known, its USB
ids, its BLE name where it has Bluetooth) and refused with a reason in plain words. None of them
gets a profile: no ranges, mats, holders, status digits, queries or stop, so nothing but ESC 05
and FG reaches one, and never FM. A shared FG text ('CAMEO', 'Silhouette') names a model only
together with its USB id; a near miss names nothing. Pure functions only: nothing here imports
the device modules, and nothing reaches a machine.
"""
import ast
import dataclasses
import re
from pathlib import Path

import pytest

import stubs

stubs.install()
from opencutter.machines import (CAMEO4, Index, MachineProfile, RecognizedModel,  # noqa: E402
                                 Support, Verdict, by_fg_model, by_id, by_usb_id, detect, enabled,
                                 enabled_profile, fg_candidates, profiles, recognized_models,
                                 registry, safety)
from opencutter.machines import ble_name_prefixes  # noqa: E402
from opencutter.machines import recognized as rec  # noqa: E402
from opencutter.machines.safety import SafetyError, UnsafeProfile  # noqa: E402
from studio_names import (FIRMWARE_ADDRESS, FIRMWARE_IMAGE_ID, STUDIO_NOTATION,  # noqa: E402
                          studio_names)

SOURCE = Path(rec.__file__)
ALL = rec.PROFILES
INDEX = Index([CAMEO4, *ALL])            # the CAMEO 4 and this module alone: no other model
USB = 0x0B4D

# The models OpenCutter names but refuses (RECOGNIZED; README, "Supported machines"), by the
# name the app shows.
DECIDED = {"CURIO 2", "CURIO 1", "CAMEO 3", "CAMEO 1", "CAMEO 2", "Portrait 1", "Portrait 2",
           "Silhouette SD", "CC300", "CC300L", "CC100", "CC200"}

# USB product id -> entry (vendor 0x0B4D).
USB_IDS = {
    0x113D: rec.CURIO2, 0x112C: rec.CURIO1, 0x112F: rec.CAMEO3, 0x1121: rec.CAMEO1,
    0x112B: rec.CAMEO2, 0x1123: rec.PORTRAIT1, 0x1132: rec.PORTRAIT2,
    0x111C: rec.SILHOUETTE_SD, 0x111D: rec.SILHOUETTE_SD, 0x111A: rec.CC300, 0x111B: rec.CC300L,
    0x1115: rec.CC100, 0x110A: rec.CC200,
}
OWN_FG = {"CAMEO 3": rec.CAMEO3, "Portrait 2": rec.PORTRAIT2, "CURIO 2": rec.CURIO2}
SHARED_FG = {"CAMEO": (rec.CAMEO1,), "Silhouette": (rec.PORTRAIT1, rec.CC200)}
BLE = {rec.CAMEO3: ("CAMEO",), rec.PORTRAIT2: ("Portrait",), rec.CURIO2: ("CURIO",)}


def ids(e) -> str:
    return e.id


# --- the list -----------------------------------------------------------------------------------
def test_every_recognized_model_of_the_decisions_is_listed():
    assert {e.name for e in ALL} == DECIDED and len(ALL) == len(DECIDED)
    assert len({e.id for e in ALL}) == len(ALL)
    assert all(type(e) is RecognizedModel and e.support is Support.RECOGNIZED for e in ALL)
    assert [e.id for e in ALL] == ["curio2", "curio1", "cameo3", "cameo1", "cameo2", "portrait1",
                                   "portrait2", "silhouette_sd", "cc300", "cc300l", "cc100",
                                   "cc200"]


@pytest.mark.parametrize("entry", ALL, ids=ids)
def test_each_entry_passes_the_self_check(entry):
    safety.check_recognized(entry)
    safety.check_entry(entry)


@pytest.mark.parametrize("entry", ALL, ids=ids)
def test_a_recognized_model_has_no_programs(entry):
    for pinned in ([b"FG\x03"], [b"TG9\x03FM1\x03FN0\x03TB50,0\x03"], [b"\x1b\x09"]):
        with pytest.raises(UnsafeProfile):
            safety.check_entry(entry, pinned)


@pytest.mark.parametrize("entry", ALL, ids=ids)
def test_the_registry_names_it_and_never_enables_it(entry):
    assert by_id(entry.id) is entry and entry in recognized_models()
    assert enabled_profile(entry.id) is None
    assert entry not in profiles() and entry not in enabled()
    assert entry not in registry.tested()
    assert INDEX.by_id(entry.id) is entry and INDEX.enabled_profile(entry.id) is None


def test_the_cameo4_is_still_the_only_tested_profile():
    assert registry.tested() == (CAMEO4,) and INDEX.tested() == (CAMEO4,)
    for reply in ("CAMEO 4 V5.05    ", "CAMEO 4 V5.05    \x03", b"CAMEO 4 V5.05    \x03"):
        for d in (detect(reply), detect(reply, index=INDEX)):
            assert d.verdict is Verdict.USE and d.profile is CAMEO4 and d.tested


# --- identity only: no ranges, mats, holders, stop or status ------------------------------------
@pytest.mark.parametrize("entry", ALL, ids=ids)
def test_identity_only(entry):
    assert not isinstance(entry, MachineProfile)
    assert {f.name for f in dataclasses.fields(entry)} == {"id", "name", "identity", "reason",
                                                           "basis"}
    for field in ("ranges", "mats", "holders", "dialect", "status", "queries", "stop",
                  "default_mat", "programs", "max_write"):
        assert not hasattr(entry, field), field
    assert entry.identity.lines == ()        # every firmware is refused alike


@pytest.mark.parametrize("entry", ALL, ids=ids)
def test_nothing_but_esc05_and_fg_can_reach_it(entry):
    """No profile, so the only rule that applies is the one before the model gate: ESC 05 and
    FG. FM (expand mode), a mat, a tool check, a getter and the stop are all refused there."""
    assert INDEX.enabled_profile(entry.id) is None and enabled_profile(entry.id) is None
    safety.check_query(b"\x1b\x05")
    safety.check_query(b"FG\x03")
    for data in (b"FM1\x03", b"FM0\x03", b"TG9\x03", b"TG1\x03", b"U\x03", b"[\x03", b"TB71\x03",
                 b"\x1b\x15", b"\x1b\x10", b"\x1b\x16", b"\x1b\x11", b"\x1b\x09", b"FG",
                 b"FG\x03\x1b\x05", b"fg\x03"):
        with pytest.raises(SafetyError):
            safety.check_query(data)
    assert not safety.text_query_allowed(b"FM1")
    assert not safety.esc_query_allowed(0x15)


# --- reasons ------------------------------------------------------------------------------------
@pytest.mark.parametrize("entry", ALL, ids=ids)
def test_the_reason_is_plain_words(entry):
    assert entry.reason and entry.reason == entry.reason.strip()
    assert not entry.reason.endswith(".") and not entry.reason[0].isupper()
    assert not re.search(r"%|\(T\)|\\|ESC|[A-Z]{2}[0-9]", entry.reason), entry.reason
    assert entry.message == (f"OpenCutter recognizes this {entry.name} but cannot drive it yet: "
                             f"{entry.reason}.")
    assert entry.basis.strip() and re.search(r"Studio 5\.0\.556|firmware analysis", entry.basis)


def test_the_reasons_say_what_is_missing():
    assert rec.CAMEO3.message == ("OpenCutter recognizes this CAMEO 3 but cannot drive it yet: "
                                  "there is no known way to stop it from the computer, and it "
                                  "does not report which tools are loaded.")
    assert rec.PORTRAIT2.reason == rec.CAMEO3.reason
    assert rec.CC200.message == ("OpenCutter recognizes this CC200 but cannot drive it yet: it "
                                 "needs a USB connection, which OpenCutter does not have, and "
                                 "there is no known way to stop it from the computer.")
    usb_only = [e for e in ALL if not e.identity.ble_names]
    assert {e.name for e in usb_only} == DECIDED - {"CAMEO 3", "Portrait 2", "CURIO 2"}
    assert all(e.reason == rec.USB_ONLY for e in usb_only)
    assert {e for e in ALL if e.reason == rec.USB_ONLY} == set(usb_only)
    assert {e for e in ALL if e.reason == rec.NO_STOP_NO_TOOL_CHECK} == {rec.CAMEO3, rec.PORTRAIT2}
    assert rec.CURIO2.reason == rec.CURIO2_REASON
    for words in ("work area", "tool height", "thickness", "confirm"):
        assert words in rec.CURIO2.reason


@pytest.mark.parametrize("entry", [e for e in ALL if e.reason == rec.USB_ONLY], ids=ids)
def test_a_usb_only_model_can_be_named_only_with_its_usb_id(entry):
    """The USB_ONLY reason says OpenCutter has no USB connection. That is only shown truthfully
    if nothing but a USB id can name the model: no BLE name and no FG text of its own."""
    assert entry.identity.ble_names == () and entry.identity.fg_models == frozenset()
    for text in entry.identity.shared_fg_models:
        d = detect(f"{text} V1.10    \x03")
        assert d.verdict is Verdict.UNKNOWN and d.entry is None


# --- FG detection -------------------------------------------------------------------------------
@pytest.mark.parametrize("reply, entry", [
    ("CAMEO 3 V1.60    \x03", rec.CAMEO3), (b"CAMEO 3 V1.60    \x03", rec.CAMEO3),
    ("CAMEO 3 V1.60    ", rec.CAMEO3), ("CAMEO 3 V1.60", rec.CAMEO3),
    ("CAMEO 3 V2.10    ", rec.CAMEO3), ("CAMEO 3 V10.00", rec.CAMEO3),
    ("Portrait 2 V1.20    ", rec.PORTRAIT2), ("Portrait 2 V5.02", rec.PORTRAIT2),
    (bytearray(b"Portrait 2 V1.20    \x03"), rec.PORTRAIT2),
    ("CURIO 2 V1.16    \x03", rec.CURIO2), ("CURIO 2 V1.15", rec.CURIO2),
    ("CURIO 2 V1.16\x03", rec.CURIO2),     # the unpadded ESC 11 form; FG is padded
], ids=repr)
def test_an_own_fg_text_is_recognized(reply, entry):
    for d in (detect(reply), detect(reply, index=INDEX)):
        assert d.verdict is Verdict.RECOGNIZED and d.entry is entry and d.name == entry.name
        assert d.profile is None and d.line is None and not d.tested
        assert d.support is Support.RECOGNIZED
        assert d.reason == entry.reason and d.message == entry.message


def test_the_standard_tail_is_logged():
    assert detect("CAMEO 3 V1.60    \x03").standard_tail is True
    assert detect("Portrait 2 V1.20    ").standard_tail is True
    assert detect("CURIO 2 V1.16\x03").standard_tail is False


@pytest.mark.parametrize("text, entry", sorted(OWN_FG.items()))
def test_an_own_fg_text_agrees_with_its_usb_id(text, entry):
    ((pid, other),) = [(p, e) for p, e in USB_IDS.items() if e is entry]
    reply = f"{text} V1.20    \x03"
    d = detect(reply, usb_id=(USB, pid))
    assert d.verdict is Verdict.RECOGNIZED and d.entry is entry
    for wrong in [(USB, 0x1137), (USB, 0x1121), (0x3844, pid)] + [(USB, p) for p in USB_IDS
                                                                   if USB_IDS[p] is not entry]:
        d = detect(reply, usb_id=wrong)
        assert d.verdict is Verdict.UNKNOWN and d.entry is None and "disagree" in d.reason


@pytest.mark.parametrize("reply", [
    # CAMEO 3: its BLE name spelling, other words, case, spacing, version form, a set field
    "CAMEO3 V1.60", "CAMEO3-TEST01", "CAMEO 3 PLUS V1.60", "CAMEO 3 PRO V1.60", "cameo 3 V1.60",
    "Cameo 3 V1.60", "CAMEO 3V1.60", "CAMEO 3  V1.60", "CAMEO 3\tV1.60", "CAMEO  3 V1.60",
    "CAMEO 33 V1.60", "XCAMEO 3 V1.60", "CAMEO 3 V01.60", "CAMEO 3 V1.6", "CAMEO 3 V1.600",
    "CAMEO 3 v1.60", "CAMEO 3 V1.60 ABC", "CAMEO 3 V1.60 X1Z\x03", "CAMEO 3", "CAMEO 3 V",
    "CAMEO 3 V1.60\x1b", "CAMEO 3 V1.٦٠",
    # Portrait 2
    "Portrait2 V1.20", "Portrait2-TEST01", "PORTRAIT 2 V1.20", "portrait 2 V1.20",
    "Portrait 2 v1.20", "Portrait 2  V1.20", "Portrait V1.20", "Portrait 1 V1.10",
    "Portrait 2 V1.20 ABC",
    # CURIO 2, and the CURIO 1, whose text is not known (never by prefix)
    "CURIO V1.16", "CURIO 1 V1.00", "Curio 2 V1.16", "CURIO2 V1.16", "CURIO 2-0000001",
    "CURIO 2 V1.16 X1Z", "CURIO 2 PLUS V1.16", "CURIO 22 V1.16",
    # texts that are only guesses for the CAMEO 2 and the CC series: not listed
    "CAMEO 2 V1.30    ", "CAMEO 1 V1.10", "CC300 V1.00", "CC300L V1.00", "CC330 V1.00",
    "CC330L V1.00", "CC100 V1.00", "CC200 V1.00", "Silhouette SD V1.00", "SD V1.00",
    # near misses of the shared texts ('CAMEO  V1.10', two spaces, is a Pro MK-II on variant 1)
    "SILHOUETTE V1.10", "silhouette V1.10", "Silhouette 2 V1.10", "Silhouette  V1.10",
    "CAMEO  V1.10", "CAMEO  V1.10    \x03", "Cameo V1.10", "CAMEO V1.10 ABC",
    # a model text that stays UNKNOWN (a firmware variant no known product uses)
    "CAMEO 5 PRO V1.07    ",
], ids=repr)
def test_a_near_miss_names_nothing(reply):
    for d in (detect(reply), detect(reply, index=INDEX)):
        assert d.verdict is Verdict.UNKNOWN and d.entry is None and d.profile is None
        assert d.support is None and d.reason and d.message.endswith(".")


@pytest.mark.parametrize("entry", ALL, ids=ids)
def test_a_usb_id_alone_never_names_a_model(entry):
    """detect() decides from FG: a USB id only settles a shared text or cross-checks an own one.
    A model whose FG text is not known is not named from its USB id."""
    for pid in [p for p, e in USB_IDS.items() if e is entry]:
        for reply in ("CAMEO 2 V1.30    ", "CC300 V1.00", "CURIO V1.00", "X V1.00"):
            d = detect(reply, usb_id=(USB, pid), index=INDEX)
            assert d.verdict is Verdict.UNKNOWN and d.entry is None


# --- shared FG texts ----------------------------------------------------------------------------
def test_shared_texts_are_never_an_entrys_own():
    own = {t for e in ALL for t in e.identity.fg_models}
    shared = {t for e in ALL for t in e.identity.shared_fg_models}
    assert own == set(OWN_FG) and shared == set(SHARED_FG) and not own & shared
    for text, entries in SHARED_FG.items():
        assert by_fg_model(text) is None and INDEX.by_fg_model(text) is None
        assert INDEX.fg_candidates(text) == entries
        assert set(entries) <= set(fg_candidates(text))
    for text, entry in OWN_FG.items():
        assert by_fg_model(text) is entry and fg_candidates(text) == ()
    for text in ("CAMEO3", "Portrait2", "CURIO", "Portrait", "CAMEO 3 ", "Silhouette SD"):
        assert by_fg_model(text) is None and fg_candidates(text) == ()


@pytest.mark.parametrize("reply", ["CAMEO V1.10    ", "CAMEO V1.10    \x03", b"CAMEO V1.10",
                                   "CAMEO V2.00"], ids=repr)
def test_cameo_alone_is_unknown_and_its_usb_id_settles_it(reply):
    for idx in (INDEX, None):
        d = detect(reply, index=idx)
        assert d.verdict is Verdict.UNKNOWN and d.entry is None and "no USB id" in d.reason
        assert "CAMEO 1" in d.reason
        d = detect(reply, index=idx, usb_id=(USB, 0x1121))
        assert d.verdict is Verdict.RECOGNIZED and d.entry is rec.CAMEO1
        assert d.message == rec.CAMEO1.message
        # 0x1146: a Pro MK-II on variant 0 sends this text too. It must never become a drivable
        # MK-II (USE): its other variants take other mats, so the reply stays UNKNOWN.
        for wrong in (0x112B, 0x112F, 0x1123, 0x1137, 0x110A, 0x1146):
            d = detect(reply, index=idx, usb_id=(USB, wrong))
            assert d.verdict is Verdict.UNKNOWN and d.entry is None, hex(wrong)
            assert d.profile is None


def test_no_text_of_this_module_is_drivable_with_any_usb_id():
    """Every own or shared text, with no USB id or with any id the registry knows: never USE.
    RECOGNIZED only for a model of this module whose own text it is, or whose USB id it is."""
    usb_ids = [None, *sorted({u for e in registry.entries() for u in e.identity.usb_ids})]
    for text in [*OWN_FG, *SHARED_FG]:
        for usb in usb_ids:
            for idx in (INDEX, None):
                d = detect(f"{text} V1.60    \x03", usb_id=usb, index=idx)
                assert d.verdict in (Verdict.RECOGNIZED, Verdict.UNKNOWN), (text, usb)
                assert d.profile is None and not d.tested
                if d.verdict is Verdict.RECOGNIZED:
                    assert d.entry in ALL and d.support is Support.RECOGNIZED
                    if usb is not None:
                        assert usb in d.entry.identity.usb_ids, (text, usb)
                    if text in SHARED_FG:
                        assert usb is not None and d.entry in SHARED_FG[text], (text, usb)


@pytest.mark.parametrize("reply", ["Silhouette V1.10    ", "Silhouette V1.10    \x03",
                                   b"Silhouette V1.10", "Silhouette V2.00"], ids=repr)
def test_silhouette_alone_is_unknown_and_its_usb_id_settles_it(reply):
    for idx in (INDEX, None):
        d = detect(reply, index=idx)
        assert d.verdict is Verdict.UNKNOWN and d.entry is None and "no USB id" in d.reason
        assert "Portrait 1" in d.reason and "CC200" in d.reason
        for pid, entry in ((0x1123, rec.PORTRAIT1), (0x110A, rec.CC200)):
            d = detect(reply, index=idx, usb_id=(USB, pid))
            assert d.verdict is Verdict.RECOGNIZED and d.entry is entry
            assert d.message == entry.message
        for wrong in (0x1121, 0x1132, 0x112F, 0x1137, 0x111C):
            d = detect(reply, index=idx, usb_id=(USB, wrong))
            assert d.verdict is Verdict.UNKNOWN and d.entry is None, hex(wrong)


# --- USB ids and BLE names ----------------------------------------------------------------------
def test_usb_ids():
    assert {(USB, p) for p in USB_IDS} == {u for e in ALL for u in e.identity.usb_ids}
    for pid, entry in USB_IDS.items():
        assert by_usb_id((USB, pid)) == (entry,) and INDEX.by_usb_id((USB, pid)) == (entry,)
    assert rec.SILHOUETTE_SD.identity.usb_ids == {(USB, 0x111C), (USB, 0x111D)}
    for entry in ALL:
        assert entry.identity.usb_ids, entry.id                     # every one has an id
    # Out of scope for now: the feeder, the Mint, and the professional cutters
    # and the DLC1000 that share the vendor id. The CAMEO 4's own id is none of these entries.
    for pid in (0x113B, 0x113C, 0x112D, 0x1133, 0x112A, 0x1114, 0x1117, 0x1119, 0x1120, 0x1122,
                0x1126, 0x110F):
        assert by_usb_id((USB, pid)) == (), hex(pid)
    assert by_usb_id((USB, 0x1137)) == (CAMEO4,)
    # 0x1115 is the CC100 and also a professional cutter: it is listed, but it can only be a
    # discovery hint, since detect() never names a model from a USB id alone.
    assert by_usb_id((USB, 0x1115)) == (rec.CC100,) and not rec.CC100.identity.fg_models


@pytest.mark.parametrize("entry", ALL, ids=ids)
def test_ble_names_only_where_the_model_has_bluetooth(entry):
    assert entry.identity.ble_names == BLE.get(entry, ())


@pytest.mark.parametrize("name, entry", [("CAMEO3-TEST01", rec.CAMEO3),
                                         ("Portrait2-TEST01", rec.PORTRAIT2),
                                         ("PORTRAIT2-TEST01", rec.PORTRAIT2),
                                         ("CURIO 2-0000001", rec.CURIO2)])
def test_discovery_prefixes_cover_the_bluetooth_names(name, entry):
    def matches(prefixes):
        return any(name.upper().startswith(p.upper()) for p in prefixes)

    assert matches(entry.identity.ble_names) and matches(ble_name_prefixes())
    assert ble_name_prefixes()[0] == "CAMEO"                       # the CAMEO 4's comes first
    for other in ALL:
        if other is not entry:
            assert not matches(other.identity.ble_names), other.id
    d = detect(name)                                               # a BLE name is not FG
    assert d.verdict is Verdict.UNKNOWN and d.entry is None


def test_the_ble_prefixes_are_the_family_words_once():
    """Discovery looks for 'CAMEO', 'Portrait' and 'CURIO', and nothing wider."""
    assert INDEX.ble_name_prefixes() == ("CAMEO", "CURIO", "Portrait")
    assert set(ble_name_prefixes()) == {"CAMEO", "Portrait", "CURIO"}     # no wider than decided


# --- the module itself --------------------------------------------------------------------------
def test_the_module_imports_only_the_profile_types():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert [(n.level, n.module) for n in imports if isinstance(n, ast.ImportFrom)] == [
        (0, "__future__"), (1, "profile")]
    assert not [n for n in imports if isinstance(n, ast.Import)]


def test_the_module_holds_no_studio_notation():
    text = SOURCE.read_text(encoding="utf-8")
    for pattern in (STUDIO_NOTATION, FIRMWARE_ADDRESS, FIRMWARE_IMAGE_ID):
        assert not pattern.search(text), pattern.pattern
    assert not studio_names(text, profile_module=True)       # digests: tests/studio_names.py


def test_profiles_names_every_entry_of_the_module():
    declared = {v for k, v in vars(rec).items() if k.isupper() and isinstance(v, RecognizedModel)}
    assert declared == set(ALL)
