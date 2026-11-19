# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Characterization lock: what OpenCutter sends to and shows for a CAMEO 4 today.

The machine profiles work moves every CAMEO 4 fact into a profile. The bytes a CAMEO 4 (firmware
V5.xx) receives must not change while that happens, nor the order of the writes, the step layout
or the texts people see. This file freezes them before the refactor:

  * a seeded corpus of 60 jobs: the SHA-256 of each plan's writes (with the step kinds, the
    confirmations, the tools each holder must report and the media box) and of its step titles
    and expectations. Each job goes in as a JobSpec dict, as the daemon receives it;
  * the read-only query allowlist (`check_safe`), the write validator of a reference job and the
    never-send commands, alone, behind filler bytes the firmware skips and in lowercase;
  * the model gate (`is_tested_model`) on the corpus of tests/test_device_model_gate.py and on
    the FG texts of other models;
  * STATUS_TEXT, TOOL_NAMES, RANGES, MATS and the other CAMEO 4 constants that stay as aliases;
  * the verdicts of the units check (`evaluate_units`), with the one planned change named in
    U_REPLY_6100_4120_VERDICT;
  * the JobSpec dict shape;
  * the texts of the app and the command line that name the CAMEO 4 (module constants only:
    the widgets build theirs from MACHINE_NAME at run time).

The pinned test programs are frozen byte for byte in tests/test_device_programs.py.

When a digest no longer matches, compare plan_lines() of that job on main and on the branch:
the first list is what reaches the machine, the second what people read.

The corpus draws its numbers from random.Random(SEED).random() only: Python keeps that sequence
the same across versions, which it does not promise for randint, choice or uniform. Coordinates
come from additions and multiplications only (no trigonometry), so every platform builds the
same jobs. Nothing here touches Bluetooth, D-Bus or a daemon.
"""
from __future__ import annotations

import functools
import hashlib
import importlib
import json
import random
from collections.abc import Sequence

import pytest

import stubs

stubs.install()
from opencutter import __main__ as cli  # noqa: E402
from opencutter.device import protocol  # noqa: E402
from opencutter.job import builder, validate  # noqa: E402
from opencutter.job.plan import Plan, build_plan  # noqa: E402
from opencutter.job.spec import CutGroup, JobSpec, SpecError, ToolSettings  # noqa: E402
from opencutter.job.validate import UnsafeJob, make_write_validator, media_box_su  # noqa: E402

ETX = b"\x03"
MAT = "12x12"
MAT_MM = 304.8
CUT_LO_MM, CUT_HI_MM = 1.6, 303.2    # inside the 12x12 cut area (30..6066 SU = 1.5..303.3 mm)
SEED = 20261118
RANDOM_JOBS = 48
# (holder, tool code) of every tool a job may use today: codes 0 (no tool), 5 (Rotary Blade) and
# 6 (Punch Tool) are refused, and the Foil Tool (8) exists only in Tool 2.
USABLE_TOOLS = ((1, 1), (1, 2), (1, 3), (1, 4), (1, 7),
                (2, 1), (2, 2), (2, 3), (2, 4), (2, 7), (2, 8))
BLADES = frozenset({1, 2, 3, 4})     # the usable tools that cut with a blade
AUTOBLADE = 2
ALLOW_CHOICES = ({}, {"2": True}, {"1": False}, {"1": True, "2": False}, {"1": True})


# --- the job corpus ------------------------------------------------------------------------------
class _Draw:
    """Numbers from random.Random(seed).random() alone, the one method whose sequence Python
    keeps across versions."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def real(self, lo: float, hi: float) -> float:
        """A number in lo..hi with 3 decimals."""
        return round(lo + (hi - lo) * self._rng.random(), 3)

    def int(self, lo: int, hi: int) -> int:
        """A whole number in lo..hi."""
        return lo + min(int(self._rng.random() * (hi - lo + 1)), hi - lo)

    def pick(self, items: Sequence):
        """One of ``items``."""
        return items[self.int(0, len(items) - 1)]

    def chance(self, p: float) -> bool:
        """True with probability ``p``."""
        return self._rng.random() < p


def _settings(holder: int, code: int, speed: int, force: int, accel: int, offset: int = 0,
              fe: bool = False, overcut: tuple[int, int] = (0, 0), depth: int | None = None,
              passes: int | None = None) -> dict:
    """One ToolSettings dict; without ``passes`` the key is left out (the default, 1)."""
    s = {"holder": holder, "tool_code": code, "speed": speed, "force": force,
         "acceleration": accel, "offset": offset, "line_segment_overcut": fe,
         "overcut": list(overcut), "blade_depth": depth}
    if passes is not None:
        s["passes"] = passes
    return s


def _job(media: tuple[float, float, float, float], *groups: tuple[dict, list],
         preview: bool | None = True, allow: dict | None = None) -> dict:
    """One JobSpec dict on the 12x12 mat; ``groups`` are (settings, paths). Without ``preview``
    or ``allow`` those keys are left out (their defaults)."""
    d: dict = {"mat": MAT, "media": list(media),
               "groups": [{"settings": s, "paths": [[list(p) for p in path] for path in paths]}
                          for s, paths in groups]}
    if preview is not None:
        d["preview_outline"] = preview
    if allow is not None:
        d["allow_unrecognized_tool"] = allow
    return d


def _circle(cx: float, cy: float, r: float, n: int) -> list[tuple[float, float]]:
    """A closed polygon of 2n points on a circle, from the rational parametrization (no
    trigonometry): the right half from the bottom up, the left half turned by 180 degrees."""
    half = []
    for k in range(n):
        t = -1 + 2 * k / n
        half.append(((1 - t * t) / (1 + t * t), 2 * t / (1 + t * t)))
    ring = half + [(-x, -y) for x, y in half]
    ring.append(ring[0])
    return [(round(cx + r * x, 3), round(cy + r * y, 3)) for x, y in ring]


SQUARE = [(30.0, 30.0), (60.0, 30.0), (60.0, 60.0), (30.0, 60.0), (30.0, 30.0)]
OPEN = [(40.0, 40.0), (90.0, 80.0), (110.0, 30.0)]
TRIANGLE = [(70.0, 70.0), (100.0, 75.0), (80.0, 95.0)]
VINYL = _settings(1, AUTOBLADE, 5, 10, 3, offset=18, overcut=(1, 1), depth=1)
PEN = _settings(2, 7, 10, 7, 3)
# The reference job: AutoBlade with Blade Depth 1 in Tool 1 and a Sketch Pen in Tool 2, on a
# media rectangle x 20..120 mm, y 20..100 mm.
REFERENCE = _job((20.0, 20.0, 100.0, 80.0), (VINYL, [SQUARE]), (PEN, [OPEN]))

HAND_JOBS: dict[str, dict] = {
    "h01-reference": REFERENCE,
    "h02-pen-no-preview": _job((20.0, 20.0, 100.0, 80.0),
                               (_settings(1, 7, 10, 7, 3), [OPEN, SQUARE]), preview=False),
    "h03-lowest-values": _job((10.0, 10.0, 150.0, 120.0),
                              (_settings(1, 1, 1, 1, 1, passes=1), [SQUARE]),
                              (_settings(2, 8, 1, 1, 1), [OPEN]),
                              allow={"1": True, "2": True}),
    "h04-highest-values": _job((10.0, 10.0, 150.0, 120.0),
                               (_settings(1, AUTOBLADE, 30, 33, 10, offset=99, fe=True,
                                          overcut=(9, 9), depth=10, passes=3), [SQUARE]),
                               (_settings(2, 3, 30, 33, 10, offset=99, fe=True, overcut=(9, 9),
                                          passes=3), [TRIANGLE + TRIANGLE[:1], OPEN])),
    "h05-depth-zero": _job((60.0, 60.0, 60.0, 50.0),
                           (_settings(1, AUTOBLADE, 3, 12, 2, offset=20, depth=0, passes=2),
                            [TRIANGLE + TRIANGLE[:1]])),
    "h06-autoblade-in-tool-2": _job((20.0, 20.0, 100.0, 80.0),
                                    (_settings(2, AUTOBLADE, 8, 14, 5, offset=25, fe=True),
                                     [SQUARE]),
                                    (_settings(1, 4, 6, 20, 1, offset=68, overcut=(2, 0)),
                                     [OPEN])),
    "h07-whole-cut-area": _job((0.0, 0.0, MAT_MM, MAT_MM),
                               (_settings(1, 1, 4, 9, 2, offset=7, overcut=(3, 4)),
                                [[(1.5, 1.5), (303.3, 1.5), (303.3, 303.3), (1.5, 303.3),
                                  (1.5, 1.5)]])),
    "h08-media-tolerance": _job((50.0, 60.0, 100.0, 100.0),
                                (_settings(1, 7, 12, 5, 4),
                                 [[(49.5, 59.5), (150.5, 59.5), (150.5, 160.5), (49.5, 160.5)]]),
                                preview=None),
    "h09-collapsed-and-repeated": _job((20.0, 20.0, 100.0, 80.0),
                                       (_settings(1, 7, 10, 7, 3),
                                        [[(40.0, 40.0), (40.0, 40.0), (60.0, 40.0), (60.0, 40.0),
                                          (60.0, 60.0)], [(70.0, 70.0), (70.01, 70.01)]]),
                                       (_settings(2, 4, 6, 8, 1, offset=68, fe=True,
                                                  overcut=(1, 1)),
                                        [[(80.0, 80.0), (80.02, 80.0)]])),
    "h10-long-circle": _job((60.0, 60.0, 180.0, 180.0),
                            (_settings(1, AUTOBLADE, 5, 10, 3, offset=18, overcut=(1, 1),
                                       depth=2), [_circle(150.0, 150.0, 80.0, 200)])),
    "h11-one-holder-three-groups": _job((20.0, 20.0, 100.0, 80.0),
                                        (_settings(1, 7, 10, 7, 3), [SQUARE]),
                                        (_settings(1, 7, 20, 15, 6, passes=2), [OPEN]),
                                        (_settings(1, 7, 3, 1, 9), [TRIANGLE]),
                                        preview=None, allow={"1": True}),
    "h12-open-and-closed-twice": _job((50.0, 50.0, 80.0, 80.0),
                                      (_settings(1, 1, 7, 18, 4, offset=30, fe=True,
                                                 overcut=(0, 5), passes=2),
                                       [TRIANGLE, TRIANGLE + TRIANGLE[:1]]),
                                      allow={"2": False}),
}


def _point(d: _Draw, region: tuple[float, float, float, float]) -> list[float]:
    x0, y0, x1, y1 = region
    return [d.real(x0, x1), d.real(y0, y1)]


def _random_path(d: _Draw, region: tuple[float, float, float, float]) -> list[list[float]]:
    """An open polyline, a closed polygon or a closed rectangle inside ``region``."""
    kind = d.pick(("open", "closed", "rect"))
    if kind == "open":
        return [_point(d, region) for _ in range(d.int(2, 9))]
    if kind == "closed":
        pts = [_point(d, region) for _ in range(d.int(3, 8))]
        return pts + [list(pts[0])]
    (xa, ya), (xb, yb) = _point(d, region), _point(d, region)
    return [[xa, ya], [xb, ya], [xb, yb], [xa, yb], [xa, ya]]


def _comb(d: _Draw, region: tuple[float, float, float, float]) -> list[list[float]]:
    """A long zigzag across ``region`` (80..200 points, so its cut spans many writes), closed
    half of the time."""
    x0, y0, x1, y1 = region
    n = d.int(80, 200)
    ya, yb = d.real(y0, y1), d.real(y0, y1)
    pts = [[round(x0 + (x1 - x0) * k / (n - 1), 3), ya if k % 2 == 0 else yb] for k in range(n)]
    if d.chance(0.5):
        pts.append(list(pts[0]))
    return pts


def _random_settings(d: _Draw, holder: int, code: int, passes: int) -> dict:
    blade = code in BLADES
    depth = d.int(0, 10) if holder == 1 and code == AUTOBLADE and d.chance(0.75) else None
    s = _settings(holder, code, d.int(1, 30), d.int(1, 33), d.int(1, 10),
                  offset=d.int(0, 99) if blade else 0,
                  fe=d.chance(0.5) if blade else False,
                  overcut=(d.int(0, 9), d.int(0, 9)) if blade else (0, 0), depth=depth)
    if passes != 1 or d.chance(0.5):
        s["passes"] = passes
    return s


def _random_group(d: _Draw, holder: int, code: int, passes: int,
                  region: tuple[float, float, float, float], long: bool) -> dict:
    settings = _random_settings(d, holder, code, passes)
    paths = [_random_path(d, region) for _ in range(d.int(1, 3))]
    if long:
        paths.append(_comb(d, region))
    return {"settings": settings, "paths": paths}


def _random_job(d: _Draw, i: int) -> dict:
    """Job ``i``: its first group uses USABLE_TOOLS[i % 11] with i % 3 + 1 passes, so that every
    tool meets every pass count and both preview settings; up to two more groups, a random media
    rectangle on the mat, and a long zigzag in every fourth job."""
    holder, code = USABLE_TOOLS[i % len(USABLE_TOOLS)]
    passes = i % 3 + 1
    preview = (i // len(USABLE_TOOLS)) % 2 == 0
    x, y = d.int(0, 1800) / 10, d.int(0, 1800) / 10
    w = d.int(400, int((MAT_MM - x) * 10) - 10) / 10
    h = d.int(400, int((MAT_MM - y) * 10) - 10) / 10
    region = (max(x, CUT_LO_MM), max(y, CUT_LO_MM), min(x + w, CUT_HI_MM), min(y + h, CUT_HI_MM))
    tools = {holder: code}
    groups = [_random_group(d, holder, code, passes, region, long=i % 4 == 3)]
    for _ in range(d.int(0, 2)):
        other = d.int(1, 2)
        pick = d.pick([c for hh, c in USABLE_TOOLS if hh == other])
        groups.append(_random_group(d, other, tools.setdefault(other, pick), d.int(1, 3), region,
                                    long=False))
    allow = dict(d.pick(ALLOW_CHOICES))
    if allow.get("1") and any(g["settings"]["blade_depth"] is not None for g in groups):
        allow["1"] = False           # Blade Depth needs the AutoBlade detected in Tool 1
    return {"mat": MAT, "media": [x, y, w, h], "groups": groups, "preview_outline": preview,
            "allow_unrecognized_tool": allow}


def _corpus() -> dict[str, dict]:
    d = _Draw(SEED)
    jobs = dict(HAND_JOBS)
    for i in range(RANDOM_JOBS):
        jobs[f"r{i:02d}"] = _random_job(d, i)
    return jobs


CORPUS = _corpus()
# SHA-256 of the corpus as JSON: when it changes, the jobs changed, not the code under test.
CORPUS_SHA = "1feb955f598063ef1b9e16fbbfc8a09ba1919c77fc5c9ef2fd943e181aa453f4"


def plan_lines(plan: Plan) -> tuple[list[str], list[str]]:
    """A plan in two canonical text forms: what reaches the machine (the tools each holder must
    report, the media box, then per step its kind, whether it waits for a confirmation, and its
    writes) and what people read (each step's title and expectation)."""
    machine = [f"tools {sorted(plan.required_tools.items())}", f"box {tuple(plan.media_box)}"]
    texts = []
    for step in plan.steps:
        machine.append(f"step {step.kind} confirm={step.confirm} writes={len(step.writes)}")
        machine += [repr(w) for w in step.writes]
        texts += [repr(step.title), repr(step.expect)]
    return machine, texts


def digest(lines: list[str]) -> str:
    """SHA-256 of the lines, one per line."""
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


@functools.cache
def _plan(name: str) -> Plan:
    return build_plan(JobSpec.from_dict(CORPUS[name]))


# name -> (SHA-256 of the machine lines, SHA-256 of the text lines) of plan_lines()
PLAN_DIGESTS: dict[str, tuple[str, str]] = {
    "h01-reference": ("8201bde62397a9c4d125b34dcbacc98ff0edf3c3805b77e32e0fb20a4be541f3",
                      "5c04aeef7f889e421d3a8ab334e1a46c0a5998e2537f708fa3ca0ed6e70d7bad"),
    "h02-pen-no-preview": ("b839fd6f744b8b85627b07c8d38212ef715c256b5ce1fd4352eff6406c40c295",
                           "d80d7e10713fc37a000c1447732f40b3fc34f47e8eebf98e30c47a62aa2647d9"),
    "h03-lowest-values": ("3252c921c0db184f8b7b66795f8c7403b04f7e2f830a064c05fbf732912cf2bd",
                          "d9e0aa2d693f1ebd6daf7ec413cb38b78da47caa899f6a3c0c841031aac61cee"),
    "h04-highest-values": ("ef7a62c72adb2d8332888e2c8cbef605bd3df993eec30333a1ccb21c22212adc",
                           "579f9488e4e77d69fae92e5e14b51f10320dd6a2ea444402275b96e53c84d57a"),
    "h05-depth-zero": ("1175ff9f14a77979440df5bb80ee1ebe112fe9433134018e0b4a8e02c72fd903",
                       "27721c1b0dcf505550672dff7f055f671ca54af3eaa1b941d54e81b88747e376"),
    "h06-autoblade-in-tool-2": ("dd9250c528fff6a6c67a16b20e179f380613f3e435c957197b3c61d027846ddf",
                                "0b53e74a963a4a25b57617902264224f5b911482605a821db629ad31375bc0a5"),
    "h07-whole-cut-area": ("c36c4b06d04f873215d6c910724a1caebbc372c4e7c1b08ee24d337e094f2c3b",
                           "81d5944884f542c18a494e8c76db163aa143b513f448149591a1758ec5c24cf3"),
    "h08-media-tolerance": ("120118fc153a95a939b87f5512ea128219427fbceea5b1cb3b4a67b01b19fd58",
                            "ce1a34865367fc74dd78a19d3bda6cca1aa7fe46baf3dfec54f666db7d0be9bc"),
    "h09-collapsed-and-repeated": (
        "b636004b5c09920a6816b724b578d7c01d503f02e972576ae00384ce7ef21797",
        "71a3ee8663a2a976b606765667f2478cd5021ad88a6f2198cf7d7153ae998e44"),
    "h10-long-circle": ("0be5ceb320f5bb7f5dc9cd6e69b65e7084f01f70db15afd11d061b95ab2b6c89",
                        "e315a7d6f0659851d15c501f04eb9cdf54b2db54845d88f1d389127d0a9fc66d"),
    "h11-one-holder-three-groups": (
        "d4618db3e8b87072768c9c6e672c5efc886317622d6ecd153c9108aed14bfbcb",
        "76e1966c75c4b08896e0cc5d8e322f1ab1e1de77c1fb0f0f933502af7ae785c1"),
    "h12-open-and-closed-twice": (
        "3915b84af34973d01944dd0b6ae613f8265841ad73c40bd686820030c9f0bd44",
        "fd4f688ef88ed2d6761967345a046b423b9027187a25f22a96d5b4c56d47def6"),
    "r00": ("db7d0c43fef023a3bf3e5a4b5c780cf648a4b2d4c5f070ce5fab278447a7183d",
            "2a4e02eb28037aece7d3268748224913c63cc616c334aeeaf6ddb746d04ce3bb"),
    "r01": ("64f761891805273e7a932f028ff4b1bda7a9b3403e54a32fc69573e7ff640a63",
            "c12060eb9e7e2206d9f1c6cf1080429c8d5b957153a5c2ffe6fae00f34dbaa95"),
    "r02": ("847b190d6725589218fc93df98fff2a93a1c20dcb1ca0cc351bf7296543e144f",
            "eb44d8cdbd1bab9cc9561669b2101be43a2b43b937edca32cc6503d6677adead"),
    "r03": ("3745d8b60800a739d5d500410be04d2e88e63c40da3e5ab57272077024af3516",
            "62f25d886c0ff134e63fc16f0c101f4d903b822e4a6baf95591fea8f0741a0d8"),
    "r04": ("082d4b2e6aa52f8942ee4ffcb1f1b085d05d572cb50efc8fa6b51800771be0cc",
            "0b52b81250a3fb61f00225a2f4bc2f0a44319cc7da7f3c6b8269ff2a54e9a5e1"),
    "r05": ("697e3cde6323340603d99945d6b157caf9a66d491fca55ad143c4d33cc2b7f60",
            "a18ec7aedb311e287a954514caef335f05cabab1986c7747b2ded760162f4deb"),
    "r06": ("9b4424225ec439704feed89cd2508d09d3019a852356602d460791400937d2b3",
            "7adf9869f8e535476e05a003b42157bd4907bf2a69215d6862f72c073ee7b744"),
    "r07": ("e7c4d6cb5d2062f6163f906b565bb5a0767abba3984352c3fe1e9f52aa27d28a",
            "c5706299ea6a98aa793c26dde9eceaaee508f2aee4b4adcec70d3632f767ae6f"),
    "r08": ("27c5085c892a57b97fa4062c41ace1262325df23fd04671e4e69d93b5c4b386c",
            "62d37202142156234551355dbca0d11f933d4288b4ef2ea0666f80fdb3422170"),
    "r09": ("9acded60b8565e2e966a44611442719f04d29d052343077129f30135e7c1e8aa",
            "739d1c1a528811d75f424bf34850e33ebfcd1353cc42c399f6f5694e66481c68"),
    "r10": ("d225f087feeb86b38a3341d4358283e19c415ff07d38f4bb9fc0d4b2632907a3",
            "622bd475263c47da2f83dd5160f57aeb345eb3ce7a901db1a2db6faa2a6259d3"),
    "r11": ("f1a679356009b64437e3c9b48caee41009bad83a2ff792f546a9b4bc3db83f8b",
            "fea289cf08de2e49aaab3e9285f50a0a23a197b69b825c1f251a04e74f2a16b1"),
    "r12": ("4b378f1a7a792b3725ccad815e9857ac44830edd39af8f930c0aaccd6593ed52",
            "d9140963055ec9479f96ce4afe790b45daa7a735d234f4bd3f4ac60dca7e5b8d"),
    "r13": ("cf4a6e0aa97f27bb6ee0a2e9f0b6bc3bce9fe351166dd596af5100c22e29de26",
            "6c370a57744161351947e8cd1b504df43236c4daa7898038adcace4aef79c0ef"),
    "r14": ("c7d44ab84e544e0c23daf5407b49caac44e5bcea6391a9650bc5e5d3ae45956c",
            "c60c856c2faf4881211ee2ffb3f4d722b52389055b6952fc7409897289e6a922"),
    "r15": ("ed0abb729f274de7c536dee2ed1d187bce673732035c13af1d9b2ba1c0a02a27",
            "9a769b0c7937d2febe5159b3c53ad7d305e77e6af16e2040c4360af6695e6d0d"),
    "r16": ("887538539246432482be40c5b40ec7b6386a93c1bf57733acac2b92f2ee4300b",
            "bd19ffddfc005bf25b658f96e0940c0b4156a99d4f946c524ed2ecefcdcdcbee"),
    "r17": ("b7aa07277d61c272b56a15199acb5de16d6401aa3ffe21e3262c1118c5ecda09",
            "a3ce8e6e27a68690dee435fe2996247242d5fe730225e9e1278e8d92d2cf08de"),
    "r18": ("236c9168bf9490de2108936071b21c73a3dcf14dedcb40c395639b8fa40bbf8d",
            "bccd655d0296060adc34a292f55c1600042eab2cc7c47fa313186523c4998da3"),
    "r19": ("46e736a778bd373cf2348e861b628c1541a8795b701aa133d816715d5365817e",
            "8356ebf4d508a641d6a6772f15bda44d560982b1f87cf57172f742d62b59fcf0"),
    "r20": ("dbb21466d1ae63b56e28676c1713995d0bba5a6964940ab73332fb3bbd501d19",
            "12961e2f66a884fa602e64282741410272e959372d018f8f5878ab41b2e4cf45"),
    "r21": ("e6151736fc624db07e290e0b93240888e0ecfd363d53102434803af061d384f0",
            "6b6767ed4b8b69d2a4a201fe3e87c013f2cfc9dc750ea66fb5c4f57583777a9b"),
    "r22": ("08895cab886537fe1014e16474404a2745c72f045c10534a7ade5b479b6407e1",
            "cd8025af201e623a44b986d12bd6fe92db9201617b2c5b2c68ee7eb5c14eb41a"),
    "r23": ("568340ee11111a92e1e03a70a39487be99760f69e53e5fb63a0c5c4692873e81",
            "98717d7aefa92ab9805df293c6212598411ee0d8e7b139ba882ed89e93018711"),
    "r24": ("32d8e680691a5dbebe0fc1bf2683d85958345d3ee0440d5a184d705e4c957d01",
            "ee435760c97874a32cc867370b47cdae4c5f8af4fdf0bdcd8adc53b9cbae6a02"),
    "r25": ("ccea1d5e50938549fb89622e726274130a2ba4395c7af985d1db40008108a57e",
            "8af9cac487e1d7afce96b1a4c89039dbbfa9480a2a3951d2dda7e0636aec587d"),
    "r26": ("2684105a939cc0379a356ae0b0c7be6591936d745d1bb8b917904e3ce07e55c0",
            "b969c603bb7875169081755dcebb4273f0302fc2668c033129770a96db16f626"),
    "r27": ("c1c08015488ebfe175cce652ad34e44ce6b1b96b103cf4a58ae2f97517b14e23",
            "133e10f2c19a82657fd8597d3f04787d9fb55d403d00d677584cdd4d5d320af5"),
    "r28": ("f553420d21c8cb20d4d10053dc758373c1d00d7d35baad3c049dcf292fa9d0ce",
            "c7309feadd106c79d9fba7acb516ac252a25028d2e454655bb1eb38446f94fe3"),
    "r29": ("e0e21a549c0e5d816c1a355d29ddb589f3ad02d0bc24cb79738b5920b1e217e8",
            "c530b9741f88b505302c856f9baa4df5d425c429b3f7afd84162616557a282b4"),
    "r30": ("9bc20d2a0066331082e4598968d60817a6dd0fb46480f68d37c886c1361a426e",
            "06fcf4eb54cb89562a28d6d1d9da5dd3887ee8dc9f78e97924c724b3a0fc4f37"),
    "r31": ("8ff53350fee2605d9a624c1d59e374e6c9de0c9761f896ffd41f69f08a89a046",
            "266ea5cbca766e77d3878618c97e608fc1cbac337ee329542b76fa983242d8a8"),
    "r32": ("702a24a4d2678fe0001f9646b55002138ff7e3759e86dd8825d6ce23eaccb45f",
            "33b862a2ce2948d83b868c81ac427fa0276739d51a8cebfb9ddb72eed0c0517c"),
    "r33": ("762371d38e2349c130706fd184a432558b7d53ff24fe93eddcc7df2ddc72fccf",
            "0d93155b2d949bd1b047db3ccf09c858e74f1f61cff2ff4699694baf4e0d71fc"),
    "r34": ("d5512a7ef5868a47291b95342923a2657826b0a3f0f2f525067a2a880534ebce",
            "965de31713f9bacb7ef1f0da011c9085c3d5d8b5f95e74ed26b137109e278708"),
    "r35": ("f6552654e59c14cd7767169e1ff71ceaaa0a821be8249c8652598cecf57a91ec",
            "ace5b2fb7b30ac4bbfb72b368ef742521c812a73423f14984902a0b3f5218296"),
    "r36": ("8935bb328e0adf34ee9f2ec484abb852207bb5dfc66e3cea4f50dd4aa6aa84e6",
            "954f7cda438f21127ab428515f5bcabbe90d4035206b4d9a8b0bd8ca76da864f"),
    "r37": ("dc98a268ec252d0c7bb38d8eba9236ae7675fc72eb7586ded0f88c39a2ee5a38",
            "c2ad309b7bd11fd803ba9c1f9128df3b33ad2c1d4734c5cbf2d2101e04a430e9"),
    "r38": ("40da4c76cb0058b151a249a64099fdae2c8e3fffe16d761d7fdfd2cbfe718552",
            "eabc708c4588611b3ab5950db2c2afa77f407acfdcfc90d335b8dbadb58445e2"),
    "r39": ("81e1a38cd1ba7facb366ccb0d8abbc40d2b15c9943f9b0463d6cb7c80ac3f179",
            "3356a7cac8159869e073696f661757c7a19077465e0947ae401509bbaea0e5de"),
    "r40": ("e562911f83e4f0a84ccdcaee4165c3d18fe57c3241e84ba32a8ac19641997c6d",
            "93b4de7b40b1f11aa0a284e5796b5ffe833fa1931c6e24f9edc864834647ee85"),
    "r41": ("5b9e373beae4b68f1cc09829d92f2782ee2e974c4df543216e726cdf2c0d7b25",
            "ab5492f01b997d7af6d951703ea788fc61f8b212c465ebe5fa9cffde8e237656"),
    "r42": ("7e8ebe1cbea1b6b8fa80f3b2df8c86a9ea1ec155725800f139b3d3e72f2bfdf0",
            "c6dee87963cb22ce509d99486532a37cc12d37ec6d05947581ecd08df05d4f2a"),
    "r43": ("d1df5262db53195df4abe7f26af453450b0eaecaa958dfc6f16ded26ba312dd4",
            "70c64f3b799318dd3da2bfb3050d6e9d75f01aa771bea44cb2f868bf01af2a87"),
    "r44": ("61ed9e3a499ab7b4deeecfc617b9f4618d48b0253c9b9bac3d61da3857c630a5",
            "e6e8545ee1a9e969d599fa64d37f7667c5de33a8d7726deb0ec2689a43e84b61"),
    "r45": ("4daa4e524a4033b01152235f7cbfcfe41e682f9265f88444f61037757fcea44a",
            "99e44c44b1932f45f5df41e46748e4183b0fca1daf6b01763208ef554b7d3b13"),
    "r46": ("8b051ad0c33959adbaf6f4876e8097a92646695aafd2e52e108da47a1da4f2a4",
            "a7c9293bf74d09564d2104b9b1a510c96dd8ea1fe3e64305849801b61c0cb764"),
    "r47": ("4797addb197f3e7dd0df6f5d1cb759dcb226ff7f79ba9c28004a5dc99164dc51",
            "928c41d45e1ceef28a1c0d8212d5692acdfb022f7765de4e6cea897aa1046e36"),
}


def test_the_corpus_is_the_frozen_one():
    text = json.dumps(CORPUS, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(text.encode()).hexdigest() == CORPUS_SHA
    assert len(CORPUS) == 60 and set(PLAN_DIGESTS) == set(CORPUS)


def test_the_corpus_covers_what_it_claims():
    groups = [g for job in CORPUS.values() for g in job["groups"]]
    assert {(g["settings"]["holder"], g["settings"]["tool_code"]) for g in groups} == \
        set(USABLE_TOOLS)
    assert {g["settings"].get("passes", 1) for g in groups} == {1, 2, 3}
    assert {job.get("preview_outline", True) for job in CORPUS.values()} == {True, False}
    assert {None, 0, 10} <= {g["settings"]["blade_depth"] for g in groups}
    paths = [p for g in groups for p in g["paths"]]
    assert any(len(p) >= 4 and p[0] == p[-1] for p in paths)
    assert any(p[0] != p[-1] for p in paths)
    writes = {name: max(len(s.writes) for s in _plan(name).steps) for name in CORPUS}
    assert writes["h10-long-circle"] >= 20 and sum(n >= 4 for n in writes.values()) >= 10
    work = [len(_plan(name).summary["groups"]) for name in CORPUS]
    assert sum(work) < len(groups)                  # h09: a group whose paths all collapse


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_the_plan_of_every_corpus_job_is_unchanged(name):
    plan = _plan(name)
    machine, texts = plan_lines(plan)
    want = PLAN_DIGESTS[name]
    assert digest(machine) == want[0], f"{name}: the writes, steps, tools or media box changed"
    assert digest(texts) == want[1], f"{name}: a step title or expectation changed"
    # the same job after a trip through JSON, as the daemon receives it
    spec = JobSpec.from_dict(CORPUS[name])
    again = build_plan(JobSpec.from_dict(json.loads(json.dumps(spec.to_dict()))))
    assert plan_lines(again) == (machine, texts)
    for command in NEVER_SEND:
        with pytest.raises(UnsafeJob):
            plan.validator(command + ETX)


# --- never-send commands -------------------------------------------------------------------------
# Never sent to any machine: calibration and persistent settings, the FP diagnostic prefix (FPG
# too) and lowercase commands, alone, with arguments and behind filler bytes the firmware skips
# (control characters, space, digits, + , - . and DEL). The write validator calls each of them a
# never-send command.
NEVER_SEND_GLOBAL = (
    b"FB", b"FB0", b"TB72", b"TB72,0,0", b"TB70", b"TB70,1", b"ZF", b"ZF1", b"TA", b"TH", b"FY",
    b"FU", b"TD", b"TE", b"FD", b"TN", b"SC", b"FP", b"FPG", b"FP1",
    b"0TH", b" FP", b"\x00FPG", b"+FB", b"-TB72,0,0", b",ZF", b".SC", b"9TN", b"\x7fFD",
    b"\tTA", b"\r\nFY", b"00FU",
    b"fpg", b"fp", b"th", b"tb72", b"fb", b" fpg", b"0fp", b"m500,500", b"d500,500",
)
# Refused as well; only the naming of the refusal may change (FM becomes a rule of the profiles
# with a 24-inch mat, and never applies to the CAMEO 4).
NEVER_SEND_OTHER = (b"FM", b"FM0", b"FM1", b"  FM1", b"PW", b"S0", b"S1", b"S2", b"S3", b"S7",
                    b"S8", b"S9")
NEVER_SEND = NEVER_SEND_GLOBAL + NEVER_SEND_OTHER


# --- the read-only query allowlist ---------------------------------------------------------------
CHECK_SAFE_ACCEPTS = (
    b"\x1b\x05", b"\x1b\x10", b"\x1b\x15", b"\x1b\x16",
    b"FG\x03", b"TB71\x03", b"FA\x03", b"FQ0\x03", b"FQ2\x03", b"TI\x03", b"TO\x03", b"TC\x03",
    b"G\x03", b"[\x03", b"U\x03",
)
CHECK_SAFE_REFUSES = (
    b"", b"\x03", b"\x1b", b"\x1b\x00", b"\x1b\x07", b"\x1b\x09", b"\x1b\x11", b"\x1b\x12",
    b"\x1b\x13", b"\x1b\x18", b"\x1b\x05\x1b\x05", b"\x1b\x05\x03", b"\x1b\x15 ", b"\x1b\x1b",
    b"FG", b"U", b"FG\x03FG\x03", b"U\x03FG\x03", b"F\x03G\x03", b"FG\x03\x03",
    b"fg\x03", b"u\x03", b"Fg\x03", b" FG\x03", b"FG \x03", b"0FG\x03", b"\x00U\x03",
    b"FG1\x03", b"TB71,0\x03", b"TB7\x03", b"FQ1\x03", b"FQ3\x03", b"FQ\x03", b"TB50,0\x03",
    b"U" + b" " * 30 + b"\x03", b"TB71" + b"\x00" * 16 + b"\x03",
    b"M200,200\x03", b"D1,1\x03", b"TG1\x03", b"FN0\x03", b"J1\x03", b"!5,1\x03", b"FX10,1\x03",
    b"TJ3\x03", b"FC18,1,1\x03", b"FE0,1\x03", b"FF1,1,1\x03", b"TF1,1\x03", b"\\0,0\x03",
    b"Z6066,6066\x03", b"H\x03",
) + tuple(c + ETX for c in NEVER_SEND)


@pytest.mark.parametrize("data", CHECK_SAFE_ACCEPTS)
def test_check_safe_accepts_the_queries(data):
    protocol.check_safe(data)


@pytest.mark.parametrize("data", CHECK_SAFE_REFUSES)
def test_check_safe_refuses_everything_else(data):
    with pytest.raises(protocol.UnsafeCommand):
        protocol.check_safe(data)


# --- the write validator of the reference job ----------------------------------------------------
REFERENCE_BOX = (390, 390, 2010, 2410)    # media_box_su: a = feed (y), b = carriage (x)
J182 = b"J1\x03J2\x03M1000,1000\x03" + b"D1000,1001\x03" * 15      # exactly 182 bytes
WRITES_ACCEPTED = (
    b"TG1\x03FN0\x03TB50,0\x03",
    b"\\30,30\x03Z6066,6066\x03J1\x03!5,1\x03FX10,1\x03TJ3\x03FC18,1,1\x03FE0,1\x03FF1,1,1\x03"
    b"TF1,1\x03",
    b"J2\x03!10,2\x03FX7,2\x03TJ3\x03FC0,1,2\x03FE0,2\x03FF0,0,2\x03",
    b"\\0,0\x03M0,0\x03FN0\x03TB50,0\x03",
    b"M390,390\x03D2010,2410\x03D390,2410\x03D2010,390\x03",
    b"D1000,1000\x03", b"D1000,1000\x03M500,500\x03D600,600\x03",
    b"M0,0\x03M500,500\x03D600,600\x03", b"TG1\x03", b"TF1,1\x03", b"J2\x03", b"M0,0\x03", J182,
)
WRITES_REFUSED = (
    b"", b"FN0", b"\x03", b"FN0\x03\x03", b"J1\x03" + b"M1000,1000\x03" * 16 + b"FN0\x03",
    b"\x1b\x09", b"\x1b\x05", b"FN0\x03\x1b\x05", b"M500,500\x1b\x03", b"\x1bFN0\x03",
    b"M389,390\x03", b"M390,389\x03", b"M2011,500\x03", b"M500,2411\x03", b"D2010,2411\x03",
    b"M1,1\x03", b"M0,1\x03", b"M1,0\x03", b"D0,0\x03", b"M6066,6066\x03",
    b"FN0\x03D1000,1000\x03", b"M0,0\x03D1000,1000\x03", b"TF1,1\x03D1000,1000\x03",
    b"J2\x03D1000,1000\x03", b"TG1\x03D1000,1000\x03", b"\\0,0\x03D1000,1000\x03",
    b"TG2\x03", b"TG0\x03", b"TG3\x03", b"TG8\x03", b"TG9\x03", b"FN1\x03", b"TB50,1\x03",
    b"J3\x03", b"J0\x03", b"!6,1\x03", b"!5,2\x03", b"!31,1\x03", b"!0,1\x03", b"!5.5,1\x03",
    b"FX11,1\x03", b"FX34,1\x03", b"FX10,2\x03", b"TJ4\x03", b"TJ0\x03", b"TJ11\x03",
    b"FC19,1,1\x03", b"FC100,1,1\x03", b"FC18,0,1\x03", b"FE1,1\x03", b"FE2,1\x03",
    b"FF1,2,1\x03", b"FF10,1,1\x03", b"TF2,1\x03", b"TF1,2\x03", b"TF11,1\x03", b"TF1\x03",
    b"\\31,30\x03", b"Z6067,6066\x03", b"Z12162,6066\x03", b"Z6066,4034\x03",
    b"!5\x03", b"FX10\x03", b"FC18\x03", b"FE0,0\x03", b"FF1,1,0\x03",
    b"M0500,500\x03", b"M500,0500\x03", b" M500,500\x03", b"M500,500 \x03", b"M500, 500\x03",
    b"M+500,500\x03", b"M-500,500\x03", b"M500.0,500\x03", b"M100000,500\x03",
    b"M500,500,500\x03", b"M500\x03", b"MD500,500\x03", b"FX010,1\x03", b"TJ03\x03",
    b"U\x03", b"FG\x03", b"TB71\x03", b"FQ0\x03", b"j1\x03", b"tg1\x03", b"fn0\x03",
) + tuple(c + ETX for c in NEVER_SEND)


@functools.cache
def _reference_check():
    return make_write_validator(JobSpec.from_dict(REFERENCE))


def test_the_reference_job():
    assert media_box_su(JobSpec.from_dict(REFERENCE)) == REFERENCE_BOX
    assert len(J182) == builder.MAX_WRITE == 182


@pytest.mark.parametrize("data", WRITES_ACCEPTED)
def test_the_write_validator_accepts_the_jobs_own_writes(data):
    _reference_check()(data)


@pytest.mark.parametrize("data", WRITES_REFUSED)
def test_the_write_validator_refuses_everything_else(data):
    with pytest.raises(UnsafeJob):
        _reference_check()(data)


@pytest.mark.parametrize("data", [bytearray(b"FN0\x03"), "FN0\x03", memoryview(b"FN0\x03"), None])
def test_the_write_validator_takes_bytes_only(data):
    with pytest.raises(UnsafeJob, match="must be bytes"):
        _reference_check()(data)


@pytest.mark.parametrize("command", NEVER_SEND_GLOBAL)
def test_the_write_validator_names_the_never_send_commands(command):
    with pytest.raises(UnsafeJob, match="never-send"):
        _reference_check()(command + ETX)


# --- the model gate ------------------------------------------------------------------------------
# The corpus of tests/test_device_model_gate.py, then the FG texts of other models as their
# firmware writes them (padded with 4 spaces, sometimes with the final ETX).
TESTED_MODEL_ACCEPTS = (
    "CAMEO 4 V5.05    ", "CAMEO 4 V5.05", "CAMEO 4 V5.00", "CAMEO 4 V5.99", "  CAMEO 4 V5.05\x03",
    "\x00CAMEO 4 V5.05\x00", "CAMEO 4 V5.05\r\n", b"CAMEO 4 V5.05    ", bytearray(b"CAMEO 4 V5.10"),
    "CAMEO 4 V5.05    \x03", b"CAMEO 4 V5.05    \x03",
)
TESTED_MODEL_REFUSES = (
    "CAMEO 4 PLUS V5.05", "CAMEO 4 PRO V5.05", "CAMEO 5 V1.07", "CAMEO 5 PLUS V1.07",
    "CAMEO 5 ALPHA V1.00", "CAMEO 4 V1.20", "PORTRAIT 3 V1.20", "PORTRAIT 4 V1.00",
    "CURIO 2 V1.00", "CAMEO 3 V3.00", "", "   ", "\x03", "CAMEO 4", "CAMEO 4 V5", "CAMEO 4 V5.",
    "CAMEO 4 V5.5", "CAMEO 4 V5.055", "CAMEO 4 V5.05 X", "X CAMEO 4 V5.05", "cameo 4 v5.05",
    "CAMEO 4 v5.05", "CAMEO  4 V5.05", "CAMEO 4 V6.05", "CAMEO 4 V15.05", "CAMEO 4\tV5.05",
    "CAMEO 4 V5.0\x005", "CAMEO 4\x00V5.05", "CAMEO 4 V5.05\x1b", "CAMEO 4 V5.05\x7f",
    "CAMEO 4 V5.\u0660\u0665", "CAMEO 4 V5.05\xa0", "CAMEO\xa04 V5.05", "1", " 2, 4",
    None, 405, b"CAMEO 4 V5.05\xff", b"\xffCAMEO 4 V5.05", ["CAMEO 4 V5.05"],
    "CAMEO 4 PLUS V5.05    ", "CAMEO 4 PRO V5.05    ", "CAMEO 4 V1.20    ", "CAMEO 4 V5.05 ABC",
    "CAMEO 5 V1.07    ", "CAMEO 5 PLUS V1.07    ", "CAMEO 5 PRO V1.07    ",
    "CAMEO 5 ALPHA V1.04    ", "CAMEO 5 ALPHA PLUS V1.04    ", b"CAMEO 5 ALPHA V1.02    \x03",
    "CAMEO PRO MARK2 V1.10    ", "CAMEO  V1.10    ", "CAMEO V1.10    ", "Portrait 3 V1.05    ",
    "Portrait 3 V5.02    ", "Portrait 4 V1.05    ", "CURIO 2 V1.16    ", "CURIO 2 V1.16\x03",
    "CAMEO 3 V1.60    ", "Portrait 2 V1.20    ", "Silhouette V1.10    ", b"CAMEO 4\x03",
)


@pytest.mark.parametrize("reply", TESTED_MODEL_ACCEPTS, ids=ascii)
def test_the_model_gate_passes_the_tested_machine(reply):
    assert protocol.is_tested_model(reply) is True


@pytest.mark.parametrize("reply", TESTED_MODEL_REFUSES, ids=ascii)
def test_the_model_gate_refuses_everything_else(reply):
    assert protocol.is_tested_model(reply) is False


# --- the CAMEO 4 tables --------------------------------------------------------------------------
def test_the_cameo4_tables():
    assert protocol.STATUS_TEXT == {"0": "Ready", "1": "Busy", "2": "Load Media", "3": "Paused",
                                    "4": "Cancelling", "5": "Rejected"}
    common = {0: "No Tool Detected", 1: "Ratchet Blade", 2: "AutoBlade", 3: "Deep-Cut Blade",
              5: "Rotary Blade", 6: "Punch Tool", 7: "Sketch Pen"}
    assert protocol.TOOL_NAMES == {1: {**common, 4: "Kraft Blade, 2mm"},
                                   2: {**common, 4: "Kraft Blade, 3mm", 8: "Foil Tool"}}
    assert validate.RANGES == {"speed": (1, 30), "force": (1, 33), "acceleration": (1, 10),
                               "offset": (0, 99), "blade_depth": (0, 10), "passes": (1, 10)}
    assert validate.UNSUPPORTED_TOOLS == frozenset({5, 6})
    # Other mats may join MATS later; the 12x12 mat stays exactly this.
    assert builder.MATS[MAT] == {"tg": 1, "area_su": (30, 30, 6066, 6066),
                                 "size_mm": (304.8, 304.8)}


def test_the_cameo4_link_constants():
    assert protocol.STOP_SEQUENCE == (b"\x1b\x09", b"\x1b\x09")
    assert protocol.MAX_DATA_WRITE == builder.MAX_WRITE == 182
    assert protocol.SAFE_ESC_QUERIES == frozenset({0x05, 0x10, 0x15, 0x16})
    assert protocol.SAFE_TEXT_QUERIES == frozenset({
        b"FG", b"TB71", b"FA", b"FQ0", b"FQ2", b"TI", b"TO", b"TC", b"G", b"[", b"U"})


@pytest.mark.parametrize("key", ["", "12X12", " 12x12", "12x12 ", "12 x 12", "TG1", "1"])
def test_unknown_mat_keys_are_refused(key):
    spec = JobSpec.from_dict({**REFERENCE, "mat": key})
    with pytest.raises(UnsafeJob):
        validate.validate_spec(spec)


# --- the units check after TG1 -------------------------------------------------------------------
# The one verdict the machine profiles work changes on purpose. Today the check takes the larger
# number of the reply, so the reply the 8x12 mat gives ("6100,4120") passes as the 12x12 mat. Once
# every mat has its own expected pair, compared in order, this becomes "mismatch": the package
# that makes that change sets it here, and changes nothing else in this file.
U_REPLY_6100_4120_VERDICT = "mismatch"
# Replies whose verdict stays: the reply the machine gives, mismatches of every kind, and
# replies without numbers.
UNITS_VERDICTS = {
    "6100,6100": "ok", "  6100,  6100": "ok",
    "3050,3050": "mismatch", "  3050,  3050": "mismatch", "  6066,  6066": "mismatch",
    "12200,12200": "mismatch", "  12200,  6100": "mismatch", "30500,30500": "mismatch",
    "0,0": "mismatch", "4120,4120": "mismatch", "6101,6101": "mismatch",
    "": "unknown", "?": "unknown", "   ": "unknown", "OK": "unknown", ",": "unknown",
}


@pytest.mark.parametrize("reply", sorted(UNITS_VERDICTS), ids=ascii)
def test_the_units_verdicts(reply):
    assert protocol.evaluate_units(reply)[0] == UNITS_VERDICTS[reply]


def test_the_units_verdict_that_is_planned_to_change():
    assert protocol.evaluate_units("6100,4120")[0] == U_REPLY_6100_4120_VERDICT


# --- the JobSpec dict ----------------------------------------------------------------------------
JOBSPEC_KEYS = frozenset({"mat", "media", "groups", "preview_outline", "allow_unrecognized_tool"})
# The one key the profiles add to the dict: "machine", the profile id ("cameo4" when left out).
JOBSPEC_KEYS_TO_COME = frozenset({"machine"})
CUTGROUP_KEYS = frozenset({"settings", "paths"})
TOOLSETTINGS_KEYS = frozenset({"holder", "tool_code", "speed", "force", "acceleration", "offset",
                               "line_segment_overcut", "overcut", "blade_depth", "passes"})
REFERENCE_DICT = {
    "mat": "12x12", "media": [20.0, 20.0, 100.0, 80.0],
    "groups": [
        {"settings": {"holder": 1, "tool_code": 2, "speed": 5, "force": 10, "acceleration": 3,
                      "offset": 18, "line_segment_overcut": False, "overcut": [1, 1],
                      "blade_depth": 1, "passes": 1},
         "paths": [[[30.0, 30.0], [60.0, 30.0], [60.0, 60.0], [30.0, 60.0], [30.0, 30.0]]]},
        {"settings": {"holder": 2, "tool_code": 7, "speed": 10, "force": 7, "acceleration": 3,
                      "offset": 0, "line_segment_overcut": False, "overcut": [0, 0],
                      "blade_depth": None, "passes": 1},
         "paths": [[[40.0, 40.0], [90.0, 80.0], [110.0, 30.0]]]},
    ],
    "preview_outline": True, "allow_unrecognized_tool": {},
}


def test_the_jobspec_dict_keeps_its_shape():
    spec = JobSpec.from_dict(REFERENCE)
    d = spec.to_dict()
    assert JOBSPEC_KEYS <= set(d) and set(d) - JOBSPEC_KEYS <= JOBSPEC_KEYS_TO_COME
    assert {k: d[k] for k in JOBSPEC_KEYS} == REFERENCE_DICT
    assert all(set(g) == CUTGROUP_KEYS and set(g["settings"]) == TOOLSETTINGS_KEYS
               for g in d["groups"])
    assert JobSpec.from_dict({k: d[k] for k in JOBSPEC_KEYS}) == spec     # today's clients
    allow = {"1": False, "2": True}
    spec = JobSpec.from_dict({**REFERENCE_DICT, "allow_unrecognized_tool": allow})
    assert spec.allow_unrecognized_tool == {1: False, 2: True}
    assert spec.to_dict()["allow_unrecognized_tool"] == allow


def test_the_jobspec_dict_keeps_its_required_and_optional_keys():
    minimal = {"mat": MAT, "media": [20, 20, 100, 80], "groups": REFERENCE_DICT["groups"]}
    spec = JobSpec.from_dict(minimal)
    assert spec.preview_outline is True and spec.allow_unrecognized_tool == {}
    for key in ("mat", "media", "groups"):
        with pytest.raises(SpecError):
            JobSpec.from_dict({k: v for k, v in minimal.items() if k != key})
    for bad in ({**minimal, "extra": 1}, {**minimal, "allow_unrecognized_tool": {"3": True}},
                {**minimal, "allow_unrecognized_tool": {"0": True}}):
        with pytest.raises(SpecError):
            JobSpec.from_dict(bad)
    group = REFERENCE_DICT["groups"][0]
    with pytest.raises(SpecError):
        CutGroup.from_dict({**group, "extra": 1})
    settings = group["settings"]
    assert ToolSettings.from_dict({k: v for k, v in settings.items() if k != "passes"}).passes == 1
    for key in TOOLSETTINGS_KEYS - {"passes"}:
        with pytest.raises(SpecError):
            ToolSettings.from_dict({k: v for k, v in settings.items() if k != key})
    with pytest.raises(SpecError):
        ToolSettings.from_dict({**settings, "extra": 1})


# --- the texts that name the CAMEO 4 -------------------------------------------------------------
# (module, name) -> the module constant. These keep their words for the CAMEO 4.
CAMEO4_TEXTS = {
    ("opencutter.gui.machine", "MACHINE_NAME"): "CAMEO 4",
    ("opencutter.gui.machine", "ANOTHER_PROGRAM"):
        "Another program is using the CAMEO 4 over Bluetooth. Close it, then switch the CAMEO 4 "
        "off and on.",
    ("opencutter.gui.machine", "ANOTHER_PROGRAM_AT_START"):
        "Another program is using the CAMEO 4 over Bluetooth. Close it, switch the CAMEO 4 off "
        "and on, then press Try again.",
    ("opencutter.gui.machine", "SWITCH_OFF_ON"):
        "Switch the CAMEO 4 off and on, then press Try again.",
    ("opencutter.gui.machine", "NOT_PAIRED"):
        "No paired CAMEO 4 was found. Pair it in Settings → Bluetooth, then press Try again.",
    ("opencutter.gui.machine", "ANOTHER_VERSION"):
        "Another OpenCutter version is using the CAMEO 4. Close it, then press Try again.",
    ("opencutter.gui.machine", "RELEASED"):
        "The CAMEO 4 is free for other apps. Press Connect to use it here again.",
    ("opencutter.gui.machine", "DISCONNECTING"): "Releasing the CAMEO 4 for other apps…",
    ("opencutter.gui.machine", "NOT_DISCONNECTED_BUSY"):
        "Not disconnected: a job is running on the CAMEO 4. Press Disconnect again when it has "
        "ended.",
    # the refusal of a machine no profile drives names no machine (reworded when the app stopped
    # naming the tested ones)
    ("opencutter.gui.machine", "WRONG_MODEL"):
        "This machine reports itself as “{model}”. OpenCutter cannot drive it and will not send "
        "jobs to it.",
    ("opencutter.gui.machine", "WRONG_MODEL_UNKNOWN"):
        "This machine did not report its model. OpenCutter cannot drive it and will not send "
        "jobs to it.",
    ("opencutter.gui.connection", "POWER_ON_HINT"):
        "The CAMEO 4 accepts a Bluetooth connection only right after it is switched on. If the "
        "app can’t connect, switch the machine off and on.",
    ("opencutter.gui.connection", "CONNECT_HINT"):
        "Make sure the CAMEO 4 is switched on and paired. If it still fails, switch it off and on "
        "again.",
    ("opencutter.gui.connection", "COULD_NOT_CONNECT"):
        "<b>Could not Connect.</b> Make sure the CAMEO 4 is switched on and paired. If it still "
        "fails, switch it off and on again.",
    ("opencutter.gui.connection", "RELEASE_HINT"):
        "Releases the CAMEO 4 so other apps can connect to it.",
    ("opencutter.gui.window", "JOB_INHIBIT_REASON"): "A job is running on the CAMEO 4",
    ("opencutter.device.portal", "BACKGROUND_STATUS"):
        "Holds the CAMEO 4 link. Use Disconnect in OpenCutter to release it; do not close it here.",
    ("opencutter.device.protocol", "TESTED_FIRMWARE"): "V5.xx",
    ("opencutter.device.protocol", "TESTED_MACHINE"): "CAMEO 4 (firmware V5.xx)",
}
# The hint under a refused machine names every model OpenCutter drives, with no word about
# testing (reworded when the other models were added, and when the app stopped naming the
# tested ones)
ONLY_CAMEO4_TEXTS = {
    ("opencutter.gui.machine", "OTHER_MACHINE_HINT"):
        "OpenCutter drives these models: CAMEO 4 (firmware V1.xx or V5.xx), CAMEO 4 Plus, CAMEO 4 "
        "Pro, CAMEO 5, CAMEO 5 Plus, CAMEO Pro MK-II, CAMEO 5 Alpha, CAMEO 5 Alpha Plus, "
        "Portrait 3 and Portrait 4. To use one, pair it in Settings → Bluetooth and switch it on: "
        "OpenCutter connects to it by itself.",
}
# the sentences of `opencutter --help` that name the machines (whitespace collapsed): only the
# CAMEO 4 on firmware V5 is tested on real hardware; the others are driven but not tested
# (reworded when the other models were added)
ONLY_CAMEO4_HELP = ("Machines: OpenCutter has been tested on real hardware only with the CAMEO 4 "
                    "(firmware V5.xx). It also drives these models, which have not been tested "
                    "on real hardware yet: CAMEO 4 (firmware V1.xx), CAMEO 4 Plus, CAMEO 4 Pro, "
                    "CAMEO 5, CAMEO 5 Plus, CAMEO Pro MK-II, CAMEO 5 Alpha, CAMEO 5 Alpha Plus, "
                    "Portrait 3 and Portrait 4.")
UNKNOWN_MODEL = "CAMEO 5 PRO V1.07"       # a model no profile names: refused, now and later


@pytest.mark.parametrize("where", sorted(CAMEO4_TEXTS | ONLY_CAMEO4_TEXTS), ids=".".join)
def test_the_texts_that_name_the_cameo4(where):
    module, name = where
    want = CAMEO4_TEXTS.get(where, ONLY_CAMEO4_TEXTS.get(where))
    assert getattr(importlib.import_module(module), name) == want


def test_the_refusal_messages_say_opencutter_cannot_drive_the_machine():
    from opencutter.gui import machine
    assert protocol.wrong_model_message(UNKNOWN_MODEL) == (
        'This machine reports itself as "CAMEO 5 PRO V1.07". OpenCutter cannot drive it and '
        "will not send anything to it.")
    assert protocol.wrong_model_message(None) == (
        "This machine did not report its model. OpenCutter cannot drive it and will not send "
        "anything to it.")
    assert machine.wrong_model_problem(UNKNOWN_MODEL) == (
        "This machine reports itself as “CAMEO 5 PRO V1.07”. OpenCutter cannot drive it and "
        "will not send jobs to it.")
    refused = {"connected": False, "reason": protocol.REASON_WRONG_MODEL, "model": UNKNOWN_MODEL}
    assert cli._model_line(refused) == (
        "CAMEO 5 PRO V1.07, refused: OpenCutter cannot drive this model")


def test_the_command_line_help_names_the_cameo4(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert ONLY_CAMEO4_HELP in " ".join(capsys.readouterr().out.split())
