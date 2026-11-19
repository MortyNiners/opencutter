# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Machine profiles: what OpenCutter knows about each cutter model, and which one is connected.

Pure Python, no I/O, and nothing else from opencutter is imported. profile.py holds the frozen
dataclasses (its docstring is the contract), safety.py the never-send rules and the profile
self-check, detect.py the FG parsing, registry.py every known model, and one module per model
family holds the values.

The package re-exports the names most callers need. `opencutter.machines.detect` is therefore
the function; import the module's other names from here or with
`from opencutter.machines.detect import ...`.
"""
from __future__ import annotations

from . import safety
from .cameo4 import CAMEO4
from .detect import FG_PADDING, Detection, Verdict, detect, fg_text
from .profile import (AccelForm, Dialect, Entry, FirmwareLine, HeaderExtra, Holder, HolderForm,
                      Identity, IntRange, MachineProfile, Mat, Queries, Ranges, RecognizedModel,
                      SpeedForm, StatusModel, StopSequence, Support, Tool, ToolReply, UBasis)
from .registry import (Index, RegistryError, ble_name_prefixes, by_fg_model, by_id, by_usb_id,
                       enabled, enabled_profile, entries, fg_candidates, profiles,
                       recognized_models, tested)

__all__ = [
    "AccelForm", "CAMEO4", "Detection", "Dialect", "Entry", "FG_PADDING", "FirmwareLine",
    "HeaderExtra", "Holder", "HolderForm", "Identity", "Index", "IntRange", "MachineProfile",
    "Mat", "Queries", "Ranges", "RecognizedModel", "RegistryError", "SpeedForm", "StatusModel",
    "StopSequence", "Support", "Tool", "ToolReply", "UBasis", "Verdict", "ble_name_prefixes",
    "by_fg_model", "by_id", "by_usb_id", "detect", "enabled", "enabled_profile", "entries",
    "fg_candidates", "fg_text", "profiles", "recognized_models", "safety", "tested",
]
