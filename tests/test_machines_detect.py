# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""FG detection and the registry.

detect() must agree with the model gate on the gate's frozen corpus
(tests/test_device_model_gate.py, read from its parametrize marks, never copied): the gate
accepts a reply exactly when detect() names the CAMEO 4 on its TESTED V5 line. The one
intended difference: 'CAMEO 4 V1.20    ' is now USE on the UNTESTED V1 line. Below TESTED, the
firmware's four-space tail is required (anything else is UNKNOWN). Pure functions only:
the gate's module imports the daemon on the stub Bluetooth of tests/stubs.py, and nothing
reaches a machine.
"""
import dataclasses

import pytest

import stubs

stubs.install()
import test_device_model_gate as gate  # noqa: E402  (the frozen corpus)
from opencutter.device import protocol  # noqa: E402
from opencutter.machines import (CAMEO4, FG_PADDING, Detection, FirmwareLine,  # noqa: E402
                                 Identity, Index, RecognizedModel, RegistryError, Support,
                                 Verdict, by_fg_model, by_id, by_usb_id, detect, enabled,
                                 enabled_profile, entries, fg_candidates, fg_text, profiles,
                                 safety)
from opencutter.machines import ble_name_prefixes, registry  # noqa: E402
from opencutter.machines import cameo4 as c4  # noqa: E402


def frozen_corpus(test) -> list:
    (mark,) = [m for m in test.pytestmark if m.name == "parametrize"]
    return list(mark.args[1])


PASSING = frozen_corpus(gate.test_the_tested_machine_passes)
REFUSED = frozen_corpus(gate.test_everything_else_is_refused)
MORE = [
    "CAMEO 4 V5.05    \x03", b"CAMEO 4 V5.05    \x03", "CAMEO 4 V5.05 ABC", "CAMEO 4 V5.05\x00\x00",
    "\tCAMEO 4 V5.05", "CAMEO 4 V5.05\x03\x03", "CAMEO 4 V5.05\x03X", "CAMEO 4 V05.05",
    "CAMEO 4 V5.05.05", "CAMEO 4 V 5.05", "CAMEO 4 PRO V5.05    ", "CAMEO 4 PLUS V5.05    ",
    "CAMEO 4 V1.20    ", "CAMEO 4 V0.05", "CAMEO 4 V9.99", "CAMEO V1.10    ", "CAMEO  V1.10",
    "Silhouette V1.10    ", "Portrait 3 V1.05    ", "CURIO 2 V1.16", "CAMEO 5 PRO V1.07",
    "CAMEO 4 V5.05　", "CAMEO 4 V5.05\x85", " CAMEO 4 V5.05", "ＣAMEO 4 V5.05",
    memoryview(b"CAMEO 4 V5.05"), 5.05, b"", bytearray(), b"\x03\x00", "V5.05", " V5.05",
    "CAMEO 4 V5.05\n\n", "CAMEO 4 V5.05\x0b\x0c", "CAMEO 4 V5.05\x1c",
]


def is_cameo4_v5(d: Detection) -> bool:
    """detect()'s side of the equivalence: the CAMEO 4 profile on its TESTED V5 line."""
    return (d.verdict is Verdict.USE and d.profile is CAMEO4 and d.line is c4.V5 and d.tested
            and d.support is Support.TESTED)


def test_the_frozen_corpus_was_found():
    assert len(PASSING) >= 9 and len(REFUSED) >= 40
    assert "CAMEO 4 V1.20" in REFUSED and None in REFUSED


@pytest.mark.parametrize("reply", PASSING + REFUSED + MORE, ids=repr)
def test_detect_equals_the_model_gate(reply):
    assert is_cameo4_v5(detect(reply)) == protocol.is_tested_model(reply)


@pytest.mark.parametrize("reply", PASSING + REFUSED + MORE, ids=repr)
def test_fg_text_moved_unchanged(reply):
    assert fg_text(reply) == protocol._fg_text(reply)


def test_fg_padding_moved_unchanged():
    assert FG_PADDING == protocol._FG_PADDING


def test_v1_20_is_now_untested_and_usable():
    """The intended change: the gate refused the CAMEO 4's V1 line; it is now UNTESTED (jobs
    allowed, not tested), with the firmware's four-space tail only."""
    for reply in ("CAMEO 4 V1.20    ", "CAMEO 4 V1.20    \x03", b"CAMEO 4 V1.02    \x03"):
        d = detect(reply)
        assert protocol.is_tested_model(reply) is False
        assert d.verdict is Verdict.USE and d.profile is CAMEO4 and d.line is c4.V1
        assert d.support is Support.UNTESTED and not d.tested and d.message == ""
        assert d.standard_tail is True
    for reply in ("CAMEO 4 V1.20", "CAMEO 4 V1.20\r\n", " CAMEO 4 V1.20    ", "CAMEO 4 V1.20   ",
                  "CAMEO 4 V1.20     ", "\x00CAMEO 4 V1.20    \x03", "CAMEO 4 V1.20    \x03\x03"):
        d = detect(reply)
        assert protocol.is_tested_model(reply) is False
        assert d.verdict is Verdict.UNKNOWN and d.profile is None and d.entry is None, reply
        assert d.model == "CAMEO 4" and d.version == (1, 20) and not d.standard_tail
        assert "four spaces" in d.reason and d.message.endswith(".")


# --- the FG tail: four spaces below TESTED (docs/PROTOCOL.md section 3.4) ----------------------
NON_STANDARD_TAILS = ["", "   ", "     ", "\r\n", "    \r\n", "\t", "    \x00", "    \x03\x03",
                      " \x03"]


@pytest.mark.parametrize("tail", NON_STANDARD_TAILS, ids=repr)
@pytest.mark.parametrize("model, version", [("CAMEO 4 PLUS", "V5.05"), ("CAMEO 5", "V1.07"),
                                            ("Portrait 3", "V1.05"), ("CAMEO 4", "V1.20")])
def test_an_untested_profile_needs_the_four_space_tail(model, version, tail):
    good = detect(f"{model} {version}    ")
    assert good.verdict is Verdict.USE and good.support is Support.UNTESTED
    assert good.standard_tail is True
    for reply in (f"{model} {version}{tail}", f"{model} {version}{tail}".encode()):
        d = detect(reply)
        assert d.verdict is Verdict.UNKNOWN and d.profile is None and d.entry is None, reply
        assert d.support is None and not d.tested and not d.standard_tail
        assert d.model == model and good.name in d.reason


@pytest.mark.parametrize("reply", ["\x00CAMEO 4 PLUS V5.05    ", " CAMEO 5 V1.07    \x03",
                                   "\nPortrait 4 V1.05    "], ids=repr)
def test_leading_padding_is_not_the_standard_form(reply):
    d = detect(reply)
    assert d.verdict is Verdict.UNKNOWN and d.profile is None and not d.standard_tail


@pytest.mark.parametrize("tail", NON_STANDARD_TAILS + ["    "], ids=repr)
def test_the_tested_machine_keeps_its_lenient_tail(tail):
    """The CAMEO 4 on its TESTED V5 line reads any padding, as the frozen model gate does."""
    reply = f"CAMEO 4 V5.05{tail}"
    assert is_cameo4_v5(detect(reply)) == protocol.is_tested_model(reply)
    if tail.strip(FG_PADDING) == "":
        assert is_cameo4_v5(detect(reply)), reply
        assert detect(reply).standard_tail is (tail == "    ")


def test_the_tail_rule_follows_the_level_that_counts():
    """A synthetic TESTED profile on a TESTED line reads other padding too; the same profile on
    an UNTESTED line needs the standard tail. Refusals keep naming the model whatever the tail."""
    tested = profile("test_tested", "TEST TESTED", lines=(5,), support=Support.TESTED)
    mixed = dataclasses.replace(tested, identity=dataclasses.replace(
        tested.identity, lines=(FirmwareLine(5, Support.TESTED, "t"),
                                FirmwareLine(1, Support.UNTESTED, "t"))))
    idx = Index([mixed])
    assert detect("TEST TESTED V5.01", index=idx).profile is mixed
    assert detect("TEST TESTED V1.01", index=idx).verdict is Verdict.UNKNOWN
    assert detect("TEST TESTED V1.01    ", index=idx).profile is mixed
    assert detect("TEST TESTED V7.01", index=idx).verdict is Verdict.RECOGNIZED
    for reply in ("CAMEO 4 PLUS V6.05", "CAMEO 4 V6.05", "CAMEO 4 V6.05    "):
        assert detect(reply).verdict is Verdict.RECOGNIZED, reply


def test_the_tested_machine():
    d = detect("CAMEO 4 V5.05    ")
    assert d.verdict is Verdict.USE and d.profile is CAMEO4 and d.name == "CAMEO 4"
    assert (d.text, d.model, d.version, d.standard_tail) == ("CAMEO 4 V5.05", "CAMEO 4", (5, 5),
                                                              True)
    assert d.reason == "" and d.message == ""


@pytest.mark.parametrize("reply", [None, "", "   ", "\x03", "\x00\x03  ", 405, 5.05,
                                   ["CAMEO 4 V5.05"], memoryview(b"CAMEO 4 V5.05"), b"",
                                   bytearray(b" \x03")])
def test_no_model(reply):
    d = detect(reply)
    assert d.verdict is Verdict.NO_MODEL and d.profile is None and d.entry is None
    assert d.support is None and not d.tested
    assert d.message == "The machine did not report its model."


@pytest.mark.parametrize("reply", [
    "cameo 4 v5.05", "CAMEO 4 v5.05", "CAMEO  4 V5.05", "CAMEO 4\tV5.05", "X CAMEO 4 V5.05",
    "CAMEO 4 V5.05 X", "CAMEO 4 V5.055", "CAMEO 4 V5.5", "CAMEO 4 V5", "CAMEO 4", "1", " 2, 4",
    "CAMEO 4 V5.٠٥", "CAMEO 4 V5.05\xa0", "CAMEO 4\xa0V5.05", b"CAMEO 4 V5.05\xff",
    b"\xffCAMEO 4 V5.05", "CAMEO 4 V5.05\x1b", "CAMEO 5 PRO V1.07", "CAMEO 4 V5.05 \x03ABC",
])
def test_unknown(reply):
    d = detect(reply)
    assert d.verdict is Verdict.UNKNOWN and d.profile is None and d.entry is None
    assert d.reason and d.message.endswith(".")


def test_a_set_firmware_field_is_unknown():
    """The 3-byte field after the version is blank on every known machine; a set one fails
    closed."""
    d = detect("CAMEO 4 V5.05 X1Z\x03")
    assert d.verdict is Verdict.UNKNOWN and d.model == "CAMEO 4" and d.version == (5, 5)
    assert "after the firmware version" in d.reason


@pytest.mark.parametrize("reply", ["CAMEO 4 V6.05", "CAMEO 4 V15.05", "CAMEO 4 V0.99    "])
def test_an_unknown_firmware_line_is_recognized(reply):
    d = detect(reply)
    assert d.verdict is Verdict.RECOGNIZED and d.entry is CAMEO4 and d.line is None
    assert d.profile is None and d.support is Support.RECOGNIZED and not d.tested
    assert d.message.startswith("OpenCutter recognizes this CAMEO 4 but cannot drive it yet: ")
    assert f"V{d.version[0]}.{d.version[1]:02}" in d.reason


def test_standard_tail():
    assert detect("CAMEO 4 V5.05    ").standard_tail is True
    assert detect("CAMEO 4 V5.05    \x03").standard_tail is True
    assert detect(b"CAMEO 4 V5.05    \x03").standard_tail is True
    for reply in ("CAMEO 4 V5.05", "CAMEO 4 V5.05   ", "CAMEO 4 V5.05     ", " CAMEO 4 V5.05    ",
                  "CAMEO 4 V5.05\r\n"):
        d = detect(reply)
        assert d.verdict is Verdict.USE and d.standard_tail is False, reply


# --- exact matching, shared texts, USB ids (synthetic entries) ---------------------------------
def profile(entry_id: str, fg: str, usb=(), shared=(), lines=(5,), support=Support.UNTESTED):
    return dataclasses.replace(
        CAMEO4, id=entry_id, name=fg.title(), support=support,
        identity=Identity(frozenset({fg}) if fg else frozenset(),
                          tuple(FirmwareLine(n, support, "test") for n in lines),
                          frozenset(usb), ("TEST",), frozenset(shared)))


def recognized(entry_id: str, name: str, fg=(), shared=(), usb=(),
               reason="there is no known way to stop it from the computer"):
    return RecognizedModel(entry_id, name, Identity(frozenset(fg), (), frozenset(usb), (),
                                                    frozenset(shared)), reason, "test")


PLUS = profile("test_plus", "CAMEO 4 PLUS", usb={(0x0B4D, 0x1138)})


@pytest.mark.parametrize("order", [(CAMEO4, PLUS), (PLUS, CAMEO4)])
def test_matching_is_exact_never_by_prefix(order):
    idx = Index(order)
    assert detect("CAMEO 4 PLUS V5.05    ", index=idx).profile is PLUS
    assert detect("CAMEO 4 V5.05    ", index=idx).profile is CAMEO4
    for reply in ("CAMEO 4 PLU V5.05", "CAMEO 4 PLUS EXTRA V5.05", "CAMEO 4 PLUSV5.05",
                  "CAMEO 4 Plus V5.05", "CAMEO V5.05"):
        assert detect(reply, index=idx).verdict is Verdict.UNKNOWN, reply


def test_a_longer_model_text_never_falls_back_to_a_shorter_one():
    idx = Index([CAMEO4])
    assert detect("CAMEO 4 PLUS V5.05    ", index=idx).verdict is Verdict.UNKNOWN
    assert detect("CAMEO 4 PRO V5.05    ", index=idx).verdict is Verdict.UNKNOWN
    for reply in ("CAMEO 4 PLUS V5.05    ", "CAMEO 4 PRO V5.05    "):     # the real registry
        assert detect(reply).entry is not CAMEO4


@pytest.mark.parametrize("reply, shared", [
    ("CAMEO V1.10    ", True), ("CAMEO V1.10", True), ("Silhouette V1.00    ", True),
    ("CAMEO 5 PRO V1.07    ", False), ("CAMEO 3 V1.60    ", False), ("CAMEO 4 V5.05", False),
    ("CAMEO V1.10 ABC", False), ("", False), (None, False)], ids=repr)
def test_only_a_text_several_models_report_is_shared(reply, shared):
    """Detection.shared tells a name several models report (the app says so) from a model no
    profile names and from one it recognizes."""
    assert detect(reply).shared is shared


def test_a_shared_text_needs_a_usb_id():
    mk2 = profile("test_mk2", "CAMEO PRO MARK2", usb={(0x0B4D, 0x1146)}, shared={"CAMEO"},
                  lines=(1,))
    cameo1 = recognized("test_cameo1", "CAMEO", shared={"CAMEO"}, usb={(0x0B4D, 0x1121)})
    idx = Index([CAMEO4, mk2, cameo1])
    assert idx.fg_candidates("CAMEO") == (mk2, cameo1) and idx.by_fg_model("CAMEO") is None
    d = detect("CAMEO V1.10    ", index=idx)
    assert d.verdict is Verdict.UNKNOWN and "no USB id" in d.reason and d.shared
    d = detect("CAMEO V1.10    ", index=idx, usb_id=(0x0B4D, 0x1121))
    assert d.verdict is Verdict.RECOGNIZED and d.entry is cameo1 and not d.shared
    d = detect("CAMEO V1.10    ", index=idx, usb_id=(0x0B4D, 0x1146))
    assert d.verdict is Verdict.USE and d.profile is mk2 and d.support is Support.UNTESTED
    assert detect("CAMEO V1.10", index=idx, usb_id=(0x0B4D, 0x1137)).verdict is Verdict.UNKNOWN
    assert detect("CAMEO PRO MARK2 V1.10    ", index=idx).profile is mk2
    assert detect("CAMEO PRO MARK2 V1.10", index=idx).verdict is Verdict.UNKNOWN


def test_the_usb_id_must_agree_with_the_model():
    assert detect("CAMEO 4 V5.05", usb_id=(0x0B4D, 0x1137)).profile is CAMEO4
    d = detect("CAMEO 4 V5.05", usb_id=(0x0B4D, 0x1138))
    assert d.verdict is Verdict.UNKNOWN and "disagree" in d.reason
    no_usb = profile("test_no_usb", "TEST NO USB")
    assert detect("TEST NO USB V5.01    ", index=Index([no_usb]),
                  usb_id=(1, 2)).profile is no_usb         # no USB id known: FG decides


@pytest.mark.parametrize("usb_id", [[0x0B4D, 0x1137], (0x0B4D,), (0x0B4D, "1137"), (True, 1),
                                    (0x0B4D << 16) | 0x1137])
def test_a_malformed_usb_id_is_a_programming_error(usb_id):
    with pytest.raises(TypeError):
        detect("CAMEO 4 V5.05", usb_id=usb_id)


def test_a_recognized_model_is_named_and_refused():
    cameo3 = recognized("test_cameo3", "CAMEO 3", fg={"CAMEO 3"})
    d = detect("CAMEO 3 V1.60    \x03", index=Index([CAMEO4, cameo3]))
    assert d.verdict is Verdict.RECOGNIZED and d.entry is cameo3 and d.profile is None
    assert d.support is Support.RECOGNIZED and d.line is None and d.name == "CAMEO 3"
    assert d.message == cameo3.message == ("OpenCutter recognizes this CAMEO 3 but cannot drive "
                                           "it yet: there is no known way to stop it from the "
                                           "computer.")


def test_a_recognized_firmware_line_is_refused_with_its_reason():
    line = FirmwareLine(1, Support.RECOGNIZED, "test", "its V1 firmware has no known stop")
    p = dataclasses.replace(PLUS, identity=dataclasses.replace(
        PLUS.identity, lines=(FirmwareLine(5, Support.UNTESTED, "test"), line)))
    safety.check_profile(p)
    d = detect("CAMEO 4 PLUS V1.02", index=Index([p]))
    assert d.verdict is Verdict.RECOGNIZED and d.line is line and d.profile is None
    assert d.message == ("OpenCutter recognizes this Cameo 4 Plus but cannot drive it yet: its V1 "
                         "firmware has no known stop.")
    assert detect("CAMEO 4 PLUS V5.02    ", index=Index([p])).profile is p


def test_a_tested_profile_on_an_untested_line_is_not_tested():
    d = detect("CAMEO 4 V1.20    ")
    assert CAMEO4.support is Support.TESTED and d.line.support is Support.UNTESTED
    assert d.support is Support.UNTESTED and d.tested is False


def test_detection_is_frozen():
    d = detect("CAMEO 4 V5.05")
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.verdict = Verdict.UNKNOWN


# --- the index and the registry ------------------------------------------------------------------
@pytest.mark.parametrize("entries_", [
    [CAMEO4, CAMEO4],                                                      # the same id twice
    [CAMEO4, profile("test_twin", "CAMEO 4")],                             # one FG text twice
    [CAMEO4, profile("test_shared", "TEST", shared={"CAMEO 4"})],          # own and shared
    [profile("test_shared", "TEST", shared={"CAMEO 4"}), CAMEO4],
    [CAMEO4, "CAMEO 4"],                                                   # not an entry
    [CAMEO4, profile("test_usb", "TEST", usb={(0x0B4D, 0x1137)})],         # one USB id twice
    [recognized("test_a", "A", usb={(0x0B4D, 0x1121)}),
     recognized("test_b", "B", shared={"CAMEO"}, usb={(0x0B4D, 0x1121)})],
])
def test_the_index_refuses_an_inconsistent_set(entries_):
    with pytest.raises(RegistryError):
        Index(entries_)


def test_registry_lookups():
    assert entries()[0] is CAMEO4 and CAMEO4 in profiles() and CAMEO4 in enabled()
    assert registry.tested() == (CAMEO4,)       # only the CAMEO 4 has cut on real hardware
    assert by_id("cameo4") is CAMEO4 and enabled_profile("cameo4") is CAMEO4
    for bad in ("CAMEO4", "cameo 4", "", None, 4, "cameo5_pro"):
        assert by_id(bad) is None and enabled_profile(bad) is None
    assert by_fg_model("CAMEO 4") is CAMEO4
    for bad in ("cameo 4", "CAMEO 4 ", "CAMEO", "CAMEO 4 V5.05", None):
        assert by_fg_model(bad) is not CAMEO4
    assert fg_candidates("CAMEO 4") == ()
    assert by_usb_id((0x0B4D, 0x1137)) == (CAMEO4,)
    for bad in ((0x0B4D, 0x9999), [0x0B4D, 0x1137], ([1], 2), None):
        assert by_usb_id(bad) == ()
    assert ble_name_prefixes()[0] == "CAMEO"
    for p in enabled():
        assert p.support.jobs_allowed
    assert set(registry.recognized_models()) | set(profiles()) == set(entries())


def test_enabled_profile_is_never_a_recognized_model():
    cameo3 = recognized("test_cameo3", "CAMEO 3", fg={"CAMEO 3"})
    idx = Index([CAMEO4, cameo3])
    assert idx.by_id("test_cameo3") is cameo3 and idx.enabled_profile("test_cameo3") is None
    assert idx.recognized_models() == (cameo3,) and idx.enabled() == (CAMEO4,)


def test_ble_prefixes_have_no_duplicates():
    a = dataclasses.replace(PLUS, identity=dataclasses.replace(PLUS.identity,
                                                               ble_names=("CAMEO", "Portrait")))
    b = recognized("test_curio", "CURIO 2", fg={"CURIO 2"})
    b = dataclasses.replace(b, identity=dataclasses.replace(b.identity, ble_names=("cameo",
                                                                                   "CURIO")))
    assert Index([CAMEO4, a, b]).ble_name_prefixes() == ("CAMEO", "Portrait", "CURIO")


@pytest.mark.parametrize("entry", entries(), ids=lambda e: e.id)
def test_every_registry_entry_passes_the_self_check(entry):
    safety.check_entry(entry)


def test_the_registry_names_each_fg_text_and_usb_id_once():
    """Across every model module: one entry per own FG text and per USB id, and no own FG text
    that another entry shares."""
    own = [t for e in entries() for t in e.identity.fg_models]
    usb = [u for e in entries() for u in e.identity.usb_ids]
    shared = {t for e in entries() for t in e.identity.shared_fg_models}
    assert len(own) == len(set(own)) and len(usb) == len(set(usb))
    assert not shared & set(own)
    for u in usb:
        assert len(by_usb_id(u)) == 1, u
    for text in own:
        assert by_fg_model(text).identity.fg_models >= {text}
