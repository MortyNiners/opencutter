# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Checks that keep Silhouette Studio's own names out of the repository, without spelling them.

CONTRIBUTING.md keeps Studio's rows, file and column names, its machine group and class names,
its enum keys and its placeholder notation out of the repository. A test that wrote those names
out as a deny-list would publish them itself, so this module holds only a short SHA-256 digest
of each (the first 20 hex digits of the digest of the exact word) and the generic patterns,
written so that no pattern matches its own source line.

  studio_names(text)                       the words of `text` that are Studio's own names
  studio_names(text, profile_module=True)  also the ones that are ordinary words elsewhere
                                           (for the machine profile modules)
  STUDIO_NOTATION, FIRMWARE_ADDRESS, FIRMWARE_IMAGE_ID   the generic patterns
"""
from __future__ import annotations

import hashlib
import re

# Studio's placeholder notation (a percent sign and a capital letter; a capital T in brackets)
# and its file names (the comma-separated tables). The character classes keep each pattern from
# matching its own source.
STUDIO_NOTATION = re.compile(r"%[A-Z]|\(T\)|\.[c]sv\b|\b[c]sv:")
# A firmware or Studio code address: "0x" and six or more hex digits.
FIRMWARE_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")
# A firmware image id (shaped like a UUID; the Bluetooth UUIDs are public and live elsewhere).
FIRMWARE_IMAGE_ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-")

# Whole words: Studio's column names, its machine group and class names and its enum keys.
NAMES = frozenset({
    "0413311da18e585ee70a", "05f7da9de64d714e2f8e", "08365a3ef86e1e638672",
    "088bfd20c0b49669330c", "0c82900eef2ecc209328", "10bdabe89bed5d929912",
    "113071ed5e3609e047d7", "13f438743d4926b7b4a9", "1a26dcb0443e82d91106",
    "21d9e5b16582534e43ed", "28f707d9db7323cc4eba", "2c88c07062d3d9ee75cc",
    "349b8112bb182f682804", "36fd70c5a2edac6a4337", "3757a807f864e4eb42c4",
    "391484accd676559e96a", "393e40bef933403b2833", "411d3a7f0d57af41d618",
    "4674cf588e6a12369d95", "4adf73d767b96e6df52f", "52ea127f68888f5341ab",
    "573b8be6001cf045938c", "621b20d3f8157f2ccdf0", "7b43c51782d40ca9dea5",
    "804a69f507715535ec80", "80b5f632e93ffccde87f", "8392c8ce3e3f4c0152fe",
    "892c7444b973f4892bfe", "8a71b0072c2fc79d0f06", "8b04b01de10687b85432",
    "96f27569670742f0890a", "a53f5fa5770624f7b58a", "a989ae26d1ed7846b247",
    "b4661f4c5bfac1ee4f6b", "baf68f2d4ca481d55251", "bf36df7264b6d164a2ed",
    "c4dba78b4576ffa39ad3", "c6e63457b2c024136793", "cad4710ffb451a22b321",
    "cea4b4df181a969c4eba", "d24fc6694384ed4bd161", "de1246761feb9bc5e7f3",
    "e4acba6ced2e905bc962",
})
# Names that begin a word (a family of keys), by their length.
PREFIXES = {
    7: frozenset({"ffdabdcd784c2d3b7260"}),
    9: frozenset({"39fd7ffc9a6c7af77f18", "754cace5f31c63f34b85", "e42c85a768dd24169946"}),
    14: frozenset({"68be749ee4d02d082cca"}),
}
# Names that are also ordinary words, or OpenCutter's own (a function, part of a tool's name):
# checked only in the machine profile modules, where none of them belongs.
PROFILE_NAMES = frozenset({
    "335d6238f0674d2fc59a", "63bc6a09a8a3daf2a825", "7da6940c6ef99d18fbf4",
    "9686e554a5877f076b89", "b756f563d3281b3962ad",
})
PROFILE_PREFIXES = {4: frozenset({"6818ba3f2b908e68b8b9"})}

_WORD = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*")
_PART = re.compile(r"[A-Za-z0-9_]+")


def digest(word: str) -> str:
    """The short digest a name is kept as here."""
    return hashlib.sha256(word.encode("utf-8")).hexdigest()[:20]


def studio_names(text: str, profile_module: bool = False) -> list[str]:
    """The words of `text` (also hyphenated ones) that are Studio's own names, sorted.
    `profile_module`: also the names that are ordinary words elsewhere."""
    names = NAMES | PROFILE_NAMES if profile_module else NAMES
    prefixes: dict[int, frozenset[str]] = dict(PREFIXES)
    if profile_module:
        for size, digests in PROFILE_PREFIXES.items():
            prefixes[size] = prefixes.get(size, frozenset()) | digests
    found = set()
    for word in set(_WORD.findall(text)) | set(_PART.findall(text)):
        if digest(word) in names or any(len(word) >= size and digest(word[:size]) in digests
                                        for size, digests in prefixes.items()):
            found.add(word)
    return sorted(found)
