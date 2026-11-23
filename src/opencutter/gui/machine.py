# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Machine state for the GUI: polls the daemon (ping, info) and derives what to show.

The GUI never talks to the machine itself; it only reads state through ``DaemonClient``. It
also keeps the background service current: a service of another build (started before an
update) is replaced once per run, never while it runs a job, and Disconnect ends the service so
that the machine is free for other programs.

Machine profiles (opencutter.machines)
  The service is the authority: it reads the model from FG and drives only a profile it may
  drive. The app learns which one from the ping's machine fields (PING_MACHINE_KEYS), which a
  plain ping leaves out: ``DaemonClient.ping_machine()`` asks for them with
  {"op": "ping", "machine": true}, and the monitor asks only after a plain ping has shown a
  service of this very build (never an older or another build's service), and only while a
  machine is connected or refused for its model. machine_fields() reads them: a ping with
  "connected" false is never a usable profile, whatever its "profile" says (an unknown firmware
  line of a known model reports that model's id with the support "recognized").
  MachineState then carries
    profile         the connected machine's profile id, None unless connected and known;
    support         "tested" or "untested" for it (the narrower of its model's and its firmware
                    line's level), or "recognized" for a refused model OpenCutter names;
    refused_reason  why such a recognized model is refused, in plain words;
    recognized      the registry id of that recognized model;
    choice          the layout's machine (the offline choice, GuiSettings.machine). Until a
                    machine is detected, every text names it (MachineState.name), and its
                    holders label the tools.
  tools is one ESC 15 code per holder of that profile, in its holder order (protocol.parse_tools
  with the profile). A well-formed reply the profile's holders cannot read (on a one-holder
  model, a code in the slot no holder owns) fails closed: tools stays None, tools_reply keeps
  the reply, and the Tool rows, the Tool card and the reason Send waits show it
  (MachineState.tools_problem).

Texts
  The module constants name the CAMEO 4, word for word as before; each comes from a function of
  the machine's name (another_program(), switch_off_on(), not_paired(), released(), ...). The
  texts that tell to switch the machine off and on take ``power_cycle`` (the profile's
  power_cycle_to_connect). texts_for(profile) gives the keyword arguments for a profile.
  WRONG_MODEL and WRONG_MODEL_UNKNOWN name only the reported model, not the layout's machine,
  and say that OpenCutter cannot drive it; a refused machine that reports a name several models
  share gets SHARED_MODEL instead.
  OTHER_MACHINE_HINT names the models OpenCutter drives (from the registry, drivable_models()),
  without telling them apart by their support level; other_machine_hint() gives a refused
  machine that OpenCutter recognizes its name and plain reason instead.

Stopping (the service's stop fields, PING_STOP_KEYS)
  The monitor asks for them with every ping of a service of this very build: with the machine
  ping (``ping_machine(stop=True)``) while a machine is connected or refused, else with
  ``ping_stop()``; a client without them gives none. MachineState then carries stop_owed (a stop
  could not get through; the service sends it when that machine is linked again),
  owed_stops_sent (how many such stops it sent), motion_stopped (the open session got a stop
  request, e.g. from `opencutter stop`), interrupted (why the session ends with the stop
  although nobody asked: the Cancelling digit it saw, or LEFT_PAUSE_MARK when the machine left
  Pause without going on) and load_seen (how often the machine reported its load status: an
  Unload shows as a change, even between two polls).

The machine's Pause during a job (the same stop fields)
  paused     the open session waits on a paused machine (its Pause key, or the app's Pause):
             the service sends it nothing but ESC 05 until it goes on, and the step waits with
             no time limit. MachineState.shows_paused also counts a machine whose status reads
             Paused while the session has not said so yet.
  resume_ok  the service would send the machine's Resume now (one ESC 08: paused, no stop asked
             for, a machine whose Resume was tested, the CAMEO 4 on firmware V5); pause_ok the
             same for the app's Pause (one ESC 07: a job of ours runs and the machine reads Ready
             or Busy). The app offers Resume only with paused and resume_ok, and Pause only on
             such a machine (MachineState.offers_pause); each is sent only for a click, through
             the service's op, never by itself.
  no_reply_s the query the open session waits for has been unanswered this many seconds (7 or
             more: "Checking connection"; the service keeps waiting and sends nothing new);
             indication_age_s (kept only then) says whether the machine still reports its
             status by itself (checking_note()).
  stop_check the service's read-back after its last checked stop (StopCheck), or None.
  paused_note() and checking_note() are the job view's notes for them. A missing key (an older
  service) means "not known": nothing is offered.

  Outside a job the info poll stays: from API 2 on the service answers it without FG (the reply
  read when the machine was connected) and, while the machine reads Paused or Cancelling,
  without ESC 15 (its "tools" is then None and the tools read last are kept), so that a paused
  machine gets nothing but ESC 05.

Link drops
  After the link to a machine dropped (from Connected to the service answering without one),
  until it is connected again: with no reason from the service yet, the problem says that the
  connection was lost and to switch the machine off and on (link_dropped()); a held-elsewhere
  reason gets a softer text (held_after_drop()), since the system may have reconnected the
  machine by itself. Nothing joins or disconnects such a link.

Translations
  The texts here stay English (the logic and the tests compare them): every template is marked
  with N_(), and the widgets translate a text where they show it (i18n.translate_text, or
  MachineState.shown_tool_text for the Tool rows).

Pure helpers for the widgets
  machine_choices()    the Page Setup machine dropdown before a machine is connected: every
                       profile jobs may use (TESTED and UNTESTED), by its plain name; never a
                       RECOGNIZED model.
  chosen_profile(id)   the profile of a layout's machine id: an unknown or recognized id gives
                       the CAMEO 4.
  other_machine_hint(state)  the hint for a machine the service refused (see Texts).
  MachineState.holders, .holder_label(n), .tool_code(n), .tool_text(n), .name, .detected,
  .machine_profile, .released.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum

from opencutter.device.client import daemon_log_path
from opencutter.device.protocol import (INTERRUPTED_TERMS, PAUSED, REASON_HELD_ELSEWHERE,
                                        REASON_NO_BLUETOOTH, REASON_NOT_PAIRED, REASON_UNREACHABLE,
                                        REASON_WRONG_MODEL, STATUS_OVERDUE, model_text,
                                        parse_tools, tool_name)
from opencutter.i18n import N_, _, pgettext, tool_label
from opencutter.identity import BUILD, build_on_disk, same_build
from opencutter.machines import (CAMEO4, Holder, MachineProfile, RecognizedModel, Support,
                                 ble_name_prefixes, by_id, detect, enabled, enabled_profile)

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
CONNECT_GRACE_SECONDS = 20.0
MACHINE_NAME = CAMEO4.name            # "CAMEO 4": the module constants below name it
DEFAULT_PROFILE = CAMEO4.id           # the layout's machine when none was chosen
# What a ping with "machine": true adds (the service's daemon.PING_MACHINE_KEYS)
PING_MACHINE_KEYS = ("profile", "support", "refused_reason")
# What a ping with "stop": true adds (the service's daemon.PING_STOP_KEYS)
PING_STOP_KEYS = ("stop_owed", "owed_stops_sent", "motion_stopped", "interrupted", "load_seen",
                  "paused", "resume_ok", "pause_ok", "no_reply_s", "indication_age_s",
                  "stop_check")
# the ping's "interrupted" when the machine left Pause without going on (daemon.LEFT_PAUSE_MARK;
# the GUI does not import the service)
LEFT_PAUSE_MARK = "left-pause"
SUPPORT_LEVELS = (Support.TESTED.value, Support.UNTESTED.value)   # a connected machine's
REASON_CHARS = 300                    # the most of a refused_reason that is shown

STATUS_TONE = {"0": "positive", "1": "info", "2": "warning", "3": "warning", "4": "warning",
               "5": "negative"}


# Why the app could not connect, in plain words (MachineState.problem), as functions of the
# machine's name. The generic text of the banner and the connection popover stays the fallback
# when nothing more is known. `power_cycle`: the machine accepts a new link only right after it
# is switched on (MachineProfile.power_cycle_to_connect), so the text says to switch it off and on.
def another_program(name: str = MACHINE_NAME, power_cycle: bool = True) -> str:
    """Another program holds the machine's link; the running daemon joins it by itself once the
    other program let go."""
    if power_cycle:
        return N_("Another program is using the {name} over Bluetooth. Close it, then switch the "
                  "{name} off and on.").format(name=name)
    return N_("Another program is using the {name} over Bluetooth. Close it: OpenCutter then "
              "connects to it by itself.").format(name=name)


def another_program_at_start(name: str = MACHINE_NAME, power_cycle: bool = True) -> str:
    """The same for a daemon that refused to start: it is not running, nothing retries until
    Try again."""
    if power_cycle:
        return N_("Another program is using the {name} over Bluetooth. Close it, switch the "
                  "{name} off and on, then press Try again.").format(name=name)
    return N_("Another program is using the {name} over Bluetooth. Close it, then press Try "
              "again.").format(name=name)


def switch_off_on(name: str = MACHINE_NAME, power_cycle: bool = True) -> str:
    """The machine does not accept a connection."""
    if power_cycle:
        return N_("Switch the {name} off and on, then press Try again.").format(name=name)
    return N_("Make sure the {name} is switched on, then press Try again.").format(name=name)


def not_paired(name: str = MACHINE_NAME) -> str:
    """No paired machine was found."""
    return N_("No paired {name} was found. Pair it in Settings → Bluetooth, then press Try "
              "again.").format(name=name)


def another_version(name: str = MACHINE_NAME) -> str:
    """Another OpenCutter version holds the machine and is not ours to replace."""
    return N_("Another OpenCutter version is using the {name}. Close it, then press Try "
              "again.").format(name=name)


def released(name: str = MACHINE_NAME) -> str:
    """After Disconnect: nothing starts by itself until Connect."""
    return N_("The {name} is free for other apps. Press Connect to use it here again.").format(
        name=name)


def disconnecting(name: str = MACHINE_NAME) -> str:
    """While Disconnect waits for the service to end: nothing may be sent."""
    return N_("Releasing the {name} for other apps…").format(name=name)


def not_disconnected_busy(name: str = MACHINE_NAME) -> str:
    """Why Disconnect did not happen: a job runs."""
    return N_("Not disconnected: a job is running on the {name}. Press Disconnect again when it "
              "has ended.").format(name=name)


def link_dropped(name: str = MACHINE_NAME, power_cycle: bool = True) -> str:
    """The link to the machine dropped, and the service gave no reason yet."""
    if power_cycle:
        return N_("The connection to the {name} was lost. Switch the {name} off and on; "
                  "OpenCutter reconnects by itself.").format(name=name)
    return N_("The connection to the {name} was lost. Make sure the {name} is switched on; "
              "OpenCutter reconnects by itself.").format(name=name)


def held_after_drop(name: str = MACHINE_NAME, power_cycle: bool = True) -> str:
    """Right after our own link dropped, the system reports the machine connected: it may have
    reconnected it by itself, or another program uses it."""
    if power_cycle:
        return N_("The {name} is connected to this computer, but not to OpenCutter: the system "
                  "may have reconnected it by itself, or another program uses it. Switch the "
                  "{name} off and on.").format(name=name)
    return N_("The {name} is connected to this computer, but not to OpenCutter: the system may "
              "have reconnected it by itself, or another program uses it. Close the other "
              "program.").format(name=name)


def texts_for(profile: MachineProfile) -> dict:
    """The keyword arguments of the text functions for `profile`: its name, and power_cycle
    for those that take it."""
    return {"name": profile.name, "power_cycle": profile.power_cycle_to_connect}


def power_cycle_seen(profile: MachineProfile) -> bool:
    """True when the texts may say as a fact that the machine accepts a Bluetooth connection
    only right after it is switched on: seen on the tested model. Every other model whose
    profile asks for the off-and-on advice gets it as advice only."""
    return profile.power_cycle_to_connect and profile.support is Support.TESTED


ANOTHER_PROGRAM = another_program()
ANOTHER_PROGRAM_AT_START = another_program_at_start()
SWITCH_OFF_ON = switch_off_on()
NOT_PAIRED = not_paired()
# the service cannot reach the computer's Bluetooth at all (off, not running, or no permission)
NO_BLUETOOTH = N_("Bluetooth is off or not available. Turn it on in Settings → Bluetooth, then "
                  "press Try again.")
SERVICE_BUSY = N_("OpenCutter’s background service is already running but does not answer. Wait "
                  "a moment, then press Try again.")
# a start-up line we do not know, or none (the details are in its log, not in the banner)
SERVICE_STOPPED = N_("OpenCutter’s background service stopped unexpectedly. Press Try again; if "
                     "it keeps stopping, see {log}.")
# ensure_daemon itself failed (nothing was started); the details go to the app's log
SERVICE_NOT_STARTED = N_("OpenCutter could not start its background service. Press Try again; "
                         "if it keeps failing, start OpenCutter from a terminal to see why.")
# an older build's service holds the old lock and did not answer, so it could not be replaced
OLDER_SERVICE = N_("An older OpenCutter background service is still running. Press Try again; "
                   "if it keeps failing, restart the computer.")
# the background service is of another build (started before an update, or another copy)
UPDATING_SERVICE = N_("Updating OpenCutter’s background service…")
OTHER_VERSION_BUSY = N_("OpenCutter’s background service is from another version; it will be "
                        "updated when its current job ends.")
ANOTHER_VERSION = another_version()
SERVICE_NOT_REPLACED = N_("OpenCutter’s background service from another version did not stop. "
                          "Wait a moment, then press Try again.")
# the guarded SIGTERM refused to signal it (not our process, not ours to see, ...)
SERVICE_NOT_STOPPED = N_("OpenCutter’s background service from another version could not be "
                         "stopped. Restart the computer, then open OpenCutter again.")
# OpenCutter was updated (or edited) while the app was open: every service it starts now is of
# another build than the app itself, so it replaces none. In the Flatpak the portal starts the
# window's own version, which Flatpak keeps while it is in use; should it be gone all the same,
# the start fails with Flatpak's "... (commit ...) not installed" (STARTUP_PROBLEMS)
APP_OUTDATED = N_("OpenCutter was updated while it was open. Close it and open it again.")
# a service that was stopping when the app wanted to start one did not end in time
SERVICE_ENDING = N_("OpenCutter’s background service is still stopping. Wait a moment, then "
                    "press Try again.")
OTHER_VERSION_REFUSED = N_("OpenCutter’s background service is from another version, so nothing "
                           "was sent. Send again once it has been updated.")
# after Disconnect: nothing starts by itself until Connect
RELEASED = released()
# while Disconnect waits for the service to end: nothing may be sent (MachineState.blocker)
DISCONNECTING = disconnecting()
# why Disconnect did not happen (a toast; the details go to the app's log)
NOT_DISCONNECTED_BUSY = not_disconnected_busy()
NOT_DISCONNECTED = N_("Not disconnected: OpenCutter’s background service did not stop. Try "
                      "again in a moment.")
NOT_DISCONNECTED_UPDATING = N_("Not disconnected: OpenCutter’s background service is being "
                               "updated.")
# The service's model gate refused the machine (the ping's reason wrong-model): it reported a
# model OpenCutter does not drive (the ping's "model"), its link was released and nothing is
# sent to it. Try again cannot change that: the service skips it and connects by itself to
# another paired machine once one is switched on, so these texts offer no Try again. They name
# only the reported model (not the layout's machine) and say nothing about testing.
WRONG_MODEL = N_("This machine reports itself as “{model}”. OpenCutter cannot drive it and "
                 "will not send jobs to it.")
WRONG_MODEL_UNKNOWN = N_("This machine did not report its model. OpenCutter cannot drive it and "
                         "will not send jobs to it.")   # no model
# A machine that reports a name more than one model reports ("CAMEO V1.10": a CAMEO 1, and a
# CAMEO Pro MK-II with some of its settings): which model it is cannot be told, so the reason
# is that (machines.detect: Detection.shared)
SHARED_MODEL = N_("This machine reports itself as “{model}”, a name that more than one model "
                  "uses, so OpenCutter cannot tell which model it is and will not send jobs to "
                  "it.")
UNSUPPORTED = N_("Unsupported Machine")  # the pill, the status bar and the headings
NOT_SUPPORTED = N_("This machine is not supported.")        # why Send and Test Cut are disabled
CONNECT_HINT_TAIL = N_("To use one, pair it in Settings → Bluetooth and switch it on: OpenCutter "
                       "connects to it by itself.")
# The job view's notes while the machine is paused during a job: Resume offered (the service
# would send it: paused and resume_ok), or only the machine's own Pause key
PAUSED_NOTE = N_("The machine is paused. OpenCutter sends nothing more until it goes on. To go "
                 "on from where it stopped, press Resume here or the Pause key on the machine. "
                 "To end the job, press Cancel Job.")
PAUSED_NOTE_KEY = N_("The machine is paused. OpenCutter sends nothing more until it goes on. To "
                     "go on from where it stopped, press the Pause key on the machine. To end the "
                     "job, press Cancel Job.")
# ... and while the query the job waits for has no answer for STATUS_OVERDUE s or more
CHECKING = N_("Checking connection")       # the pill
CHECKING_NOTE = N_("No answer from the machine for {n} s. OpenCutter keeps waiting and sends "
                   "nothing new. If it moves when it should not, press Cancel Job or its power "
                   "button.")
STILL_REPORTS = " It still reports its status ({term})."   # indications came in the last 7 s
# CHECKING_NOTE + STILL_REPORTS, as one text for the translators
CHECKING_NOTE_REPORTS = N_("No answer from the machine for {n} s. OpenCutter keeps waiting and "
                           "sends nothing new. If it moves when it should not, press Cancel Job "
                           "or its power button. It still reports its status ({term}).")


def _join(items: list[str], shown: bool = False) -> str:
    """'A', 'A and B', 'A, B and C'; with `shown`, in the window's language (join_shown)."""
    if len(items) == 1:
        return items[0]
    if shown:
        # Translators: the end of a list of machine names: "CAMEO 4, CAMEO 5 and Portrait 3".
        return _("{items} and {last}").format(
            items=pgettext("list separator", ", ").join(items[:-1]), last=items[-1])
    return N_("{items} and {last}").format(items=", ".join(items[:-1]), last=items[-1])


def join_shown(items: list[str]) -> str:
    """'A', 'A and B', 'A, B and C' in the window's language."""
    return _join(items, shown=True)


def drivable_models(shown: bool = False) -> list[str]:
    """The models OpenCutter drives, from the registry, in registry order, by their names
    alone: the GUI does not tell them apart by their support level. A model whose firmware
    lines are driven at different levels is named with those lines, lowest first ("CAMEO 4
    (firmware V1.xx or V5.xx)"), so that a refused machine of that model on another line sees
    which lines are driven. With `shown`, those words are in the window's language."""
    names: list[str] = []
    for p in enabled():
        driven = sorted((line for line in p.identity.lines
                         if p.support.narrower(line.support).jobs_allowed),
                        key=lambda line: line.major)
        if not driven:
            continue
        if len({p.support.narrower(line.support) for line in driven}) > 1:
            words = [f"V{line.major}.xx" for line in driven]
            if shown:
                # Translators: between two firmware lines: "V1.xx or V5.xx".
                lines = pgettext("list separator", " or ").join(words)
                names.append(_("{model} (firmware {lines})").format(model=p.name, lines=lines))
            else:
                names.append(N_("{model} (firmware {lines})").format(model=p.name,
                                                                     lines=" or ".join(words)))
        else:
            names.append(p.name)
    return names


def drivable_hint(shown: bool = False) -> str:
    """Which models OpenCutter drives and how to use one: the hint for a machine the service
    refused that OpenCutter does not know (OTHER_MACHINE_HINT). With `shown`, in the window's
    language, made when it is shown (the connection popover, the first run)."""
    if shown:
        return _("OpenCutter drives these models: {models}. {hint}").format(
            models=join_shown(drivable_models(shown=True)), hint=_(CONNECT_HINT_TAIL))
    return N_("OpenCutter drives these models: {models}. {hint}").format(
        models=_join(drivable_models()), hint=CONNECT_HINT_TAIL)


# The hint under a machine the service refused for its model: which models OpenCutter drives
# (and so which firmware lines of the CAMEO 4). other_machine_hint() gives a recognized model
# its own reason instead.
OTHER_MACHINE_HINT = drivable_hint()
REASONS = {REASON_HELD_ELSEWHERE: ANOTHER_PROGRAM, REASON_NOT_PAIRED: NOT_PAIRED,
           REASON_UNREACHABLE: SWITCH_OFF_ON, REASON_NO_BLUETOOTH: NO_BLUETOOTH}
# waiting does not help with these (the daemon never joins a held link, needs a pairing, needs
# Bluetooth, and never uses a machine of another model): Could not Connect (or Unsupported
# Machine) at once instead of after the grace time
FAIL_AT_ONCE = frozenset({REASON_HELD_ELSEWHERE, REASON_NOT_PAIRED, REASON_NO_BLUETOOTH,
                          REASON_WRONG_MODEL})
# Discovery's "nothing paired" line (device/ble.py): "No paired CAMEO was found", and the same
# for each name prefix discovery accepts (registry.ble_name_prefixes)
_NOT_PAIRED_NEEDLES = tuple(dict.fromkeys(("No paired CAMEO was found",
                                           *(f"No paired {p} was found"
                                             for p in ble_name_prefixes()))))
# daemon start-up refusals (its last line in daemon.log) -> plain words; first match wins. The
# texts as functions of the machine's name (startup_problem); STARTUP_PROBLEMS names the CAMEO 4
_STARTUP = (
    ("legacy prototype daemon", another_program_at_start),
    ("already connected by another program", another_program_at_start),
    ("another OpenCutter daemon is running", SERVICE_BUSY),
    ("an OpenCutter daemon is already running", SERVICE_BUSY),
    ("an older OpenCutter background service is running", OLDER_SERVICE),
    ("that was stopping did not end", SERVICE_ENDING),
    *((needle, not_paired) for needle in _NOT_PAIRED_NEEDLES),
    # Flatpak, through the portal: the window's version (its commit) is no longer installed
    (") not installed", APP_OUTDATED),
)


def _text(problem, name: str, power_cycle: bool) -> str:
    """A text of _STARTUP: a constant, or a function of the machine's name."""
    if isinstance(problem, str):
        return problem
    if problem is not_paired:
        return problem(name)
    return problem(name, power_cycle)


STARTUP_PROBLEMS = tuple((needle, _text(p, MACHINE_NAME, True)) for needle, p in _STARTUP)


def reason_problem(reason: object, model: object = None, *, name: str = MACHINE_NAME,
                   power_cycle: bool = True) -> str | None:
    """The plain-words problem for the ``reason`` of a daemon's ping reply, or None. For
    ``wrong-model``, ``model`` is the ping's "model": what the refused machine reports itself
    as. The other texts name the machine ``name`` (the layout's; texts_for(profile))."""
    if reason == REASON_WRONG_MODEL:
        return wrong_model_problem(model)
    if not isinstance(reason, str):
        return None
    texts = {REASON_HELD_ELSEWHERE: another_program(name, power_cycle),
             REASON_NOT_PAIRED: not_paired(name),
             REASON_UNREACHABLE: switch_off_on(name, power_cycle),
             REASON_NO_BLUETOOTH: NO_BLUETOOTH}
    return texts.get(reason)


def shown_model(value: object) -> str | None:
    """A model the service reported (the ping's "model", an FG reply) as people may see it:
    ``protocol.model_text`` (printable ASCII, at most 40 characters), or None without one. The
    text still comes from the machine: escape it before it goes into markup."""
    if isinstance(value, str) and value.strip():
        return model_text(value)
    return None


def wrong_model_problem(model: object) -> str:
    """Why nothing is sent to a machine the service refused for its model, in plain words: for
    a name several models report, that OpenCutter cannot tell which one it is (SHARED_MODEL)."""
    shown = shown_model(model)
    if not shown:
        return WRONG_MODEL_UNKNOWN
    if detect(model).shared:                 # a str: shown_model took it
        return SHARED_MODEL.format(model=shown)
    return WRONG_MODEL.format(model=shown)


def startup_problem(line: str | None, *, name: str = MACHINE_NAME,
                    power_cycle: bool = True) -> str | None:
    """The plain-words problem for the last line a daemon that exited at start-up wrote (or
    ``client.spawn_error``'s "the daemon exited with status N"), or None without one. A line
    we do not know is not shown (it may be a traceback): the text points to the log. The texts
    name the machine ``name`` (texts_for(profile))."""
    if not line:
        return None
    for needle, problem in _STARTUP:
        if needle in line:
            return _text(problem, name, power_cycle)
    return SERVICE_STOPPED.format(log=log_hint())


def log_hint() -> str:
    """The daemon log's path for the user, with the home folder written '~'."""
    path, home = daemon_log_path(), os.path.expanduser("~")
    if home.rstrip(os.sep) and path.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + path[len(home.rstrip(os.sep)):]
    return path


# --- machine profiles ---------------------------------------------------------------------------
def chosen_profile(profile_id: object) -> MachineProfile:
    """The profile of a layout's machine id (GuiSettings.machine, the offline choice): an
    unknown id, or one of a model OpenCutter only recognizes, gives the CAMEO 4."""
    return enabled_profile(profile_id) or CAMEO4


@dataclass(frozen=True)
class MachineChoice:
    """One entry of the offline machine dropdown: the profile id and its plain name, which the
    dropdown shows."""
    id: str
    name: str


def machine_choices() -> tuple[MachineChoice, ...]:
    """The Page Setup machine dropdown before a machine is connected: every profile jobs may
    use (TESTED and UNTESTED), in registry order. Never a model OpenCutter only recognizes."""
    return tuple(MachineChoice(p.id, p.name) for p in enabled())


def _plain(value: object) -> str | None:
    """Text from the service as one plain line: control characters as spaces, runs of spaces
    collapsed, at most REASON_CHARS characters; None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    if len(text) > REASON_CHARS:
        text = text[:REASON_CHARS - 1].rstrip() + "…"
    return text or None


def service_text(value: object) -> str | None:
    """Plain words from the service (a refusal, a reason) as one plain line of at most
    REASON_CHARS characters, or None: control characters as spaces, runs of spaces collapsed.
    Still text from outside the app: escape it before it goes into markup."""
    return _plain(value)


def machine_fields(ping: object) -> dict:
    """The machine fields of a ping (PING_MACHINE_KEYS, from {"op": "ping", "machine": true}),
    as MachineState takes them: profile, support, refused_reason and recognized.

    Connected ("connected" true): the profile id when it names a profile OpenCutter drives and
    the support is "tested" or "untested" (the narrower of that and the profile's own level),
    else nothing. Not connected: never a profile, whatever "profile" says; a machine the service
    refused for its model (reason wrong-model) with the support "recognized" gives that support,
    its plain reason and the id of the entry it names. A ping without the fields (a plain ping,
    a service before machine profiles) gives None for all of them."""
    out = dict.fromkeys(("profile", "support", "refused_reason", "recognized"))
    if not isinstance(ping, dict):
        return out
    pid, support = ping.get("profile"), ping.get("support")
    if ping.get("connected") is True:
        profile = enabled_profile(pid)
        if profile is not None and support in SUPPORT_LEVELS:
            level = Support(support).narrower(profile.support)
            out.update(profile=profile.id, support=level.value)
        return out
    if ping.get("reason") == REASON_WRONG_MODEL and support == Support.RECOGNIZED.value:
        entry = by_id(pid)
        out.update(support=support, refused_reason=_plain(ping.get("refused_reason")),
                   recognized=entry.id if entry is not None else None)
    return out


@dataclass(frozen=True)
class StopCheck:
    """The service's ESC 05 read-back after its last stop op with check (the ping's
    "stop_check"): still reading, the last status digit read (None: none), how many seconds it
    read, whether it still read Busy or Paused at the end (None: nothing read), and why nothing
    could be read (plain words from the service, or None)."""
    running: bool = False
    status: str | None = None
    seconds: float = 0.0
    still_busy: bool | None = None
    still_paused: bool | None = None
    reason: str | None = None


def _seconds(value: object) -> float | None:
    """A number of seconds the service sent (not a bool, finite, not negative), or None."""
    if type(value) not in (int, float) or not 0 <= value < float("inf"):
        return None
    return float(value)


def _stop_check(value: object) -> StopCheck | None:
    """The ping's "stop_check" as a StopCheck, or None for anything that is not a dict."""
    if not isinstance(value, dict):
        return None
    status = value.get("status")

    def flag(key: str) -> bool | None:
        return value.get(key) if isinstance(value.get(key), bool) else None
    return StopCheck(running=value.get("running") is True,
                     status=status if isinstance(status, str) and len(status) == 1 else None,
                     seconds=_seconds(value.get("seconds")) or 0.0,
                     still_busy=flag("still_busy"), still_paused=flag("still_paused"),
                     reason=_plain(value.get("reason")))


def stop_fields(ping: object) -> dict:
    """The stop fields of a ping (PING_STOP_KEYS, from {"op": "ping", "stop": true}), as
    MachineState takes them; the defaults for a ping without them (an older service: nothing
    paused, nothing offered). "indication_age_s" is kept only while "no_reply_s" is set: it
    matters only then (checking_note()), and a value that changes on every ping would change
    the state on every poll."""
    out = {"stop_owed": False, "owed_stops_sent": None, "motion_stopped": False,
           "interrupted": None, "load_seen": None, "paused": False, "resume_ok": False,
           "pause_ok": False, "no_reply_s": None, "indication_age_s": None, "stop_check": None}
    if not isinstance(ping, dict):
        return out
    for key in ("stop_owed", "motion_stopped", "paused", "resume_ok", "pause_ok"):
        out[key] = ping.get(key) is True
    for key in ("owed_stops_sent", "load_seen"):
        value = ping.get(key)
        out[key] = value if type(value) is int and value >= 0 else None
    interrupted = ping.get("interrupted")
    if isinstance(interrupted, str) and (len(interrupted) == 1
                                         or interrupted == LEFT_PAUSE_MARK):
        out["interrupted"] = interrupted
    out["no_reply_s"] = _seconds(ping.get("no_reply_s"))
    if out["no_reply_s"] is not None:
        out["indication_age_s"] = _seconds(ping.get("indication_age_s"))
    out["stop_check"] = _stop_check(ping.get("stop_check"))
    return out


def paused_note(state: MachineState) -> str | None:
    """The job view's note while the machine is paused (MachineState.shows_paused): PAUSED_NOTE
    when Resume is offered (the service would send it now), else PAUSED_NOTE_KEY (only the
    machine's own Pause key goes on); None otherwise."""
    if not state.shows_paused:
        return None
    return PAUSED_NOTE if state.offers_resume else PAUSED_NOTE_KEY


def checking_note(state: MachineState) -> str | None:
    """The job view's note while the query the job waits for has had no answer for 7 s or more
    (CHECKING_NOTE with the whole seconds), adding STILL_REPORTS with the last status when the
    machine reported it by itself in the last 7 s; None otherwise."""
    if not state.checking:
        return None
    term = state.machine_profile.status.text(state.status)
    age = state.indication_age_s
    if term and age is not None and age < STATUS_OVERDUE:
        return CHECKING_NOTE_REPORTS.format(n=int(state.no_reply_s), term=term)
    return CHECKING_NOTE.format(n=int(state.no_reply_s))


def _takes(function, name: str) -> bool:
    """True when `function` takes a keyword argument `name` (a client's ping_machine)."""
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def other_machine_hint(state: MachineState | None = None) -> str:
    """The hint for a machine the service refused for its model: for a model OpenCutter
    recognizes, its name and plain reason ("OpenCutter recognizes this CAMEO 3 but cannot drive
    it yet: ..."), from the ping's refused_reason or the registry; otherwise which models
    OpenCutter drives and how to use one (OTHER_MACHINE_HINT). Never Try again."""
    if state is not None and state.wrong_model and state.support == Support.RECOGNIZED.value:
        if state.refused_reason:
            return state.refused_reason
        entry = by_id(state.recognized)
        if isinstance(entry, RecognizedModel):
            return entry.message
    return OTHER_MACHINE_HINT


class Link(Enum):
    """How far the GUI is from the machine."""
    NO_DAEMON = "no-daemon"     # no background service answers
    CONNECTING = "connecting"   # the service runs and is waiting for the machine
    FAILED = "failed"           # no link after the grace time, or the link dropped
    CONNECTED = "connected"


@dataclass(frozen=True)
class MachineState:
    """One snapshot of what the daemon reports."""
    link: Link = Link.NO_DAEMON
    status: str | None = None              # ESC 05 digit '0'..'5'
    # ESC 15 code per holder of the machine's profile, in its holder order (Tool 1, Tool 2 on
    # the CAMEO 4; one code on a one-holder model)
    tools: tuple[int, ...] | None = None
    # the raw ESC 15 reply when it is well formed but cannot be read for this model (a code in
    # the slot no holder of a one-holder model owns): tools is then None, nothing is sent, and
    # the reply is shown (tool_text, tools_problem)
    tools_reply: str | None = None
    firmware: str | None = None
    device_name: str | None = None
    motion: str | None = None              # name of the program the daemon is running
    problem: str | None = None             # why it could not connect, in plain words (FAILED)
    note: str | None = None                # a plain line while Connecting or Not Connected
    blocker: str | None = None             # why nothing may be sent although it is connected
    # what the machine reports itself as (FG, through shown_model): the connected machine's, or
    # while wrong_model the refused machine's. Text from the machine: never markup unescaped
    model: str | None = None
    wrong_model: bool = False              # FAILED: the service refused the machine's model
    # the machine fields (machine_fields): the connected machine's profile id, never one of a
    # machine that is not connected; its support level ("tested"/"untested"), or "recognized"
    # for a refused model OpenCutter names, with its plain reason and its registry id
    profile: str | None = None
    support: str | None = None
    refused_reason: str | None = None
    recognized: str | None = None
    choice: str = DEFAULT_PROFILE          # the layout's machine: names the texts until detected
    released: bool = False                 # Disconnect released the machine (note: released())
    # the stop fields (stop_fields): a stop owed to the machine, how many owed stops were sent,
    # a stop request in the open session, the Paused/Cancelling digit it saw, load status count
    stop_owed: bool = False
    owed_stops_sent: int | None = None
    motion_stopped: bool = False
    interrupted: str | None = None
    load_seen: int | None = None
    # ... and the pause fields: the open session waits on a paused machine, Resume and Pause
    # would be sent now, how long the session's query has had no answer (7 s or more), seconds
    # since the last status indication (only with no_reply_s), the last checked stop's read-back
    paused: bool = False
    resume_ok: bool = False
    pause_ok: bool = False
    no_reply_s: float | None = None
    indication_age_s: float | None = None
    stop_check: StopCheck | None = None

    @property
    def connected(self) -> bool:
        """True while the daemon holds a link to the machine."""
        return self.link is Link.CONNECTED

    @property
    def detected(self) -> MachineProfile | None:
        """The connected machine's profile, as the service reports it; None while not
        connected or not known (a service that did not say)."""
        return enabled_profile(self.profile) if self.connected else None

    @property
    def machine_profile(self) -> MachineProfile:
        """The profile the texts and holders follow: the detected one, else the layout's
        machine (chosen_profile of `choice`)."""
        return self.detected or chosen_profile(self.choice)

    @property
    def name(self) -> str:
        """The machine's name in texts: the detected profile's, else the layout's machine."""
        return self.machine_profile.name

    @property
    def holders(self) -> tuple[Holder, ...]:
        """The tool holders of machine_profile, in order."""
        return self.machine_profile.holders

    def holder_label(self, holder: int) -> str:
        """'Tool 1', 'Tool 2', or 'Tool' on a one-holder model; 'Tool <n>' for a holder the
        profile does not have."""
        h = self.machine_profile.holder(holder)
        return h.label if h is not None else f"Tool {holder}"

    @property
    def ready(self) -> bool:
        """True when the machine is connected, idle and has media loaded."""
        return self.connected and self.status == self.machine_profile.status.ready

    @property
    def status_text(self) -> str:
        """The machine state in Studio terms."""
        if self.link is Link.NO_DAEMON:
            return N_("Not Connected")
        if self.link is Link.CONNECTING:
            return N_("Connecting…")
        if self.link is Link.FAILED:
            return UNSUPPORTED if self.wrong_model else N_("Could not Connect")
        return self.machine_profile.status.text(self.status) or N_("Connected")

    def tool_code(self, holder: int) -> int | None:
        """The tool code reported for ``holder`` (its number: 1 or 2 on the CAMEO 4), or None
        when unknown or when the profile has no such holder."""
        if not self.connected or self.tools is None:
            return None
        numbers = [h.number for h in self.machine_profile.holders]
        if holder not in numbers:
            return None
        index = numbers.index(holder)
        return self.tools[index] if index < len(self.tools) else None

    def tool_text(self, holder: int) -> str | None:
        """'AutoBlade (Detected)', 'No Tool Detected', 'Unclear reply “0, 2”' for a reply that
        cannot be read for this model (tools_reply), or None when unknown."""
        code = self.tool_code(holder)
        if code is None:
            if (self.connected and self.tools_reply is not None
                    and self.machine_profile.holder(holder) is not None):
                return unclear_reply(self.tools_reply)
            return None
        name = tool_name(holder, code, self.machine_profile)
        return name if code == 0 else N_("{tool} (Detected)").format(tool=name)

    def shown_tool_text(self, holder: int) -> str | None:
        """tool_text() in the window's language."""
        code = self.tool_code(holder)
        if code is None:
            text = self.tool_text(holder)
            if text is None:
                return None
            return _("Unclear reply “{reply}”").format(reply=self.tools_reply.strip())
        name = tool_label(tool_name(holder, code, self.machine_profile))
        return name if code == 0 else _("{tool} (Detected)").format(tool=name)

    @property
    def reports_paused(self) -> bool:
        """True while the connected machine's status reads Paused."""
        return self.connected and self.machine_profile.status.text(self.status) == PAUSED

    @property
    def shows_paused(self) -> bool:
        """True while a job is to be shown paused: the open session waits on a paused machine,
        or the machine's status reads Paused (before the session said so, or an older
        service)."""
        return self.connected and (self.paused or self.reports_paused)

    @property
    def offers_resume(self) -> bool:
        """True when Resume may be offered: the session waits on a paused machine and the
        service would send its Resume now."""
        return self.connected and self.paused and self.resume_ok

    @property
    def ends_job(self) -> bool:
        """True while the open session is ending without a Cancel of ours: it saw a cancel on
        the machine or LEFT_PAUSE_MARK (interrupted), it got a stop request (motion_stopped),
        or the machine reads Cancelling. Neither Resume nor Pause is offered then."""
        if not self.connected:
            return False
        return (self.interrupted is not None or self.motion_stopped
                or self.machine_profile.status.text(self.status) in INTERRUPTED_TERMS)

    @property
    def offers_pause(self) -> bool:
        """True for a machine whose Pause the app offers during a job: the detected profile has
        one, and it is a tested model on a tested firmware line (the CAMEO 4 on V5). Whether it
        is sent now is pause_ok."""
        detected = self.detected
        return (detected is not None and detected.pause is not None
                and self.support == Support.TESTED.value)

    @property
    def checking(self) -> bool:
        """True while the query the open session waits for has had no answer for 7 s or more
        ("Checking connection")."""
        return self.connected and self.no_reply_s is not None

    @property
    def tools_problem(self) -> str | None:
        """Why the tools are not known although the machine reported them (tools_reply), in
        plain words with the reply; None otherwise."""
        if not self.connected or self.tools is not None or self.tools_reply is None:
            return None
        return tools_unclear(self.name, self.tools_reply, len(self.holders))


def pill(state: MachineState) -> tuple[str, str, bool]:
    """Status pill (text, tone, pulsing) for a machine state outside a job."""
    if state.link is Link.NO_DAEMON:
        return N_("Not Connected"), "neutral", False
    if state.link is Link.CONNECTING:
        return N_("Connecting…"), "info", True
    if state.link is Link.FAILED:
        return state.status_text, "negative", False
    return state.status_text, STATUS_TONE.get(state.status or "", "positive"), False


def _parse_tools(value: object, profile: MachineProfile = CAMEO4) -> tuple[int, ...] | None:
    """info's "tools" (the raw ESC 15 reply) as one code per holder of `profile`."""
    if isinstance(value, str):
        return parse_tools(value, profile)
    if (isinstance(value, (list, tuple)) and len(value) == len(profile.holders)
            and all(isinstance(v, int) for v in value)):
        return tuple(value)
    return None


def _unclear_tools(value: object, profile: MachineProfile) -> str | None:
    """info's "tools" when it is a well-formed ESC 15 reply for `profile` that its holders
    cannot read (a code in the slot no holder of a one-holder model owns: fail closed, and the
    reply is shown), else None. A reply that is not even well formed (a garbled read) is not
    this: the last tools read are kept, as before."""
    if (isinstance(value, str) and profile.parse_tools(value) is not None
            and profile.holder_codes(value) is None):
        return value
    return None


def unclear_reply(reply: str) -> str:
    """'Unclear reply “0, 2”': a tool reply OpenCutter cannot read for the model."""
    return N_("Unclear reply “{reply}”").format(reply=reply.strip())


def tools_unclear(name: str, reply: str, holders: int = 1) -> str:
    """Why Send and Test Cut wait when the machine reported its tools in a form OpenCutter
    cannot read for its model."""
    if holders == 1:
        return N_("The {name} reported its tool as “{reply}”, which OpenCutter cannot read for "
                  "this model, so nothing is sent. Please report this reply.").format(
            name=name, reply=reply.strip())
    return N_("The {name} reported its tools as “{reply}”, which OpenCutter cannot read for this "
              "model, so nothing is sent. Please report this reply.").format(
        name=name, reply=reply.strip())


class MachineMonitor:
    """Polls the daemon every couple of seconds and tells listeners when the state changes.

    While a machine is connected or refused for its model, a ping of a service of this very
    build is followed by the machine ping (``client.ping_machine()``, when the client has it),
    whose reply then stands for the ping: it holds every key of the plain one. ``choice`` is
    the layout's machine (set_choice): the texts name it until a machine is detected."""

    def __init__(self, client, interval: float = POLL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        """``client`` is a ``DaemonClient`` (or a fake with the same methods)."""
        self.client = client
        self.interval = interval
        self.choice = DEFAULT_PROFILE  # the layout's machine (GuiSettings.machine)
        self.state = MachineState()
        self.quiet = False            # True during a job: ping only, no info queries
        self._clock = clock
        self._waiting_since: float | None = None
        self._start_problem: str | None = None      # the daemon could not be started: why
        self._failed_at_once = False                # the last ping gave a FAIL_AT_ONCE reason
        self._listeners: list[Callable[[MachineState], None]] = []
        self._replaces_left = 1       # a service of another build is replaced once (per Try again)
        self._replacing = False       # a replace runs: it sets the states itself
        self._older = False           # an older build's service (old socket) runs a job: follow it
        self._released = False       # Disconnect: nothing starts by itself until connect()
        self._released_name = MACHINE_NAME    # the machine Disconnect released (its note)
        self._disconnecting = False  # Disconnect waits for the service to end: polls keep out
        self._replace_problem: str | None = None    # this run's replace failed: why (until Try again)
        self._outdated = False       # the app is older than its files on disk: it replaces nothing
        self._dropped = False        # the link dropped from Connected: until connected again

    def add_listener(self, callback: Callable[[MachineState], None]) -> None:
        """Call ``callback(state)`` after every change."""
        self._listeners.append(callback)

    def set_choice(self, profile_id: object) -> None:
        """The layout's machine (the offline choice): the texts and holders follow it until a
        machine is detected. An unknown or recognized id gives the CAMEO 4 (chosen_profile).
        Listeners hear of it when the state changes."""
        self.choice = chosen_profile(profile_id).id
        self._set(self.state)

    def _texts(self) -> dict:
        """texts_for() the layout's machine: what the texts of a machine that is not connected
        name (MachineState.name)."""
        return texts_for(chosen_profile(self.choice))

    async def run(self) -> None:
        """Start the daemon if needed, then poll until cancelled."""
        await self.connect()
        while True:
            await asyncio.sleep(self.interval)
            await self.refresh()

    async def connect(self) -> None:
        """Start the daemon if none answers, restart the grace time, and poll once (the app's
        start, Connect and Try again). A service of another build may be replaced once more.

        When the daemon could not be started and said why (``client.spawn_error``), the state
        is Could not Connect with that reason until a daemon answers. An older build's service
        on the old socket is not started next to: the poll replaces it (or waits for its job).
        A service that is stopping is waited for, then a new one is started (ensure_daemon)."""
        if self._replacing or self._disconnecting:
            return
        self._released = False
        self._replaces_left = 1
        self._replace_problem = None
        self._start_problem = None
        self._older = False
        try:
            older = None
            if await self.client.ping() is None:
                older = await self.client.ping_older()
            if older is not None:
                self._older = True
            else:
                await self._start()
        except Exception:  # noqa: BLE001 - shown as Could not Connect, the details in the log
            log.exception("could not start the daemon")
            self._start_problem = SERVICE_NOT_STARTED
        self._waiting_since = None
        await self.refresh()

    async def _start(self) -> None:
        """Start the daemon if none answers; remember why it could not start."""
        if not await self.client.ensure_daemon():
            self._start_problem = startup_problem(getattr(self.client, "spawn_error", None),
                                                  **self._texts())

    async def disconnect(self) -> str | None:
        """Disconnect: end the background service (the shutdown op), which releases the machine
        at once for other programs. Nothing starts again by itself until ``connect()``. While it
        waits for the service to end, the polls leave the state alone and nothing may be sent
        (``DISCONNECTING``). Returns why it did not end, in plain words for a toast, or None."""
        if self._replacing:
            return NOT_DISCONNECTED_UPDATING
        if self._disconnecting:
            return None                       # the Disconnect under way reports for itself
        self._disconnecting = True
        name = self.state.name                # the machine being released
        self._set(replace(self.state, blocker=disconnecting(name)))
        try:
            result = await self.client.shutdown()
        except Exception as e:  # noqa: BLE001 - reported like a refusal
            log.exception("shutdown failed")
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            self._disconnecting = False
        if not result.get("ok"):
            log.warning("Disconnect: the background service did not stop: %s", result.get("error"))
            await self.refresh()
            return not_disconnected_busy(name) if result.get("busy") else NOT_DISCONNECTED
        self._released = True
        self._released_name = name
        self._older = False
        self._dropped = False
        self._start_problem = None
        self._set(MachineState(note=released(name), released=True))
        return None

    async def refresh(self) -> MachineState:
        """Poll once (ping, then info when idle) and notify listeners on a change. A service of
        another build is never used: while it runs a job, Send is blocked; otherwise it is
        replaced (once), or the state says that another version holds the machine. A service
        that is stopping counts as gone: it is not a dropped link, and it is not used."""
        if self._replacing or self._disconnecting:
            return self.state
        ping = await self._ping()
        if ping and ping.get("stopping"):
            ping = None
        if ping and (self._older or not same_build(ping)):
            new = await self._other_build(ping)
            if new is None:                      # replaced: poll the new service
                return await self.refresh()
            self._set(new)
            return new
        if ping:
            self._replace_problem = None         # our own build answers
            ping = await self._with_machine(ping)
        old = self.state
        stops = stop_fields(ping)
        failed_at_once, self._failed_at_once = self._failed_at_once, False
        if not ping:
            if self._start_problem:
                new = MachineState(link=Link.FAILED, problem=self._start_problem)
            else:
                new = MachineState(note=released(self._released_name) if self._released
                                   else None, released=self._released)
        elif not ping.get("connected"):
            self._start_problem = None
            reason = ping.get("reason")
            at_once = isinstance(reason, str) and reason in FAIL_AT_ONCE
            if failed_at_once and not at_once:
                # that reason went away (the other program let go, the machine was paired):
                # the daemon is connecting now, so it gets a fresh grace time
                self._waiting_since = None
            self._failed_at_once = at_once
            if old.link is Link.CONNECTED:
                self._dropped = True             # our own link dropped (until connected again)
            link = self._waiting_link(old)
            wrong = reason == REASON_WRONG_MODEL
            fields = machine_fields(ping) if wrong else {}
            problem = reason_problem(reason, ping.get("model"), **self._texts())
            if self._dropped and reason is None:
                problem = link_dropped(**self._texts())
            elif self._dropped and reason == REASON_HELD_ELSEWHERE:
                problem = held_after_drop(**self._texts())
            new = MachineState(link=Link.FAILED if at_once else link, problem=problem,
                               model=shown_model(ping.get("model")) if wrong else None,
                               wrong_model=wrong, **fields, **stops)
        else:
            self._start_problem = None
            self._waiting_since = None
            self._released = False
            self._dropped = False
            device = ping.get("device") or {}
            fields = machine_fields(ping)
            if fields["profile"] != old.profile:  # another machine: its tools are read anew
                old = replace(old, tools=None, tools_reply=None)
            new = replace(old, link=Link.CONNECTED, status=ping.get("status") or old.status,
                          motion=ping.get("motion"), device_name=device.get("name"), problem=None,
                          note=None, blocker=None, model=shown_model(ping.get("model")),
                          wrong_model=False, released=False, **fields, **stops)
            if not self.quiet and not new.motion:
                new = await self._with_info(new)
        self._set(new)
        return new

    async def _with_machine(self, ping: dict) -> dict:
        """The machine ping ({"op": "ping", "machine": true}) in place of `ping`, a ping of a
        service of this very build, while a machine is connected or refused for its model. The
        flag is never sent to another build's or an older service (refresh checks the plain
        ping first). `ping` stays as it is when the client cannot ask, the answer is not an ok
        reply of this build, the service is stopping, or the look fails: then MachineState has no
        machine fields, and the service still refuses a job made for another machine."""
        if not (ping.get("connected") or ping.get("reason") == REASON_WRONG_MODEL):
            return await self._with_stop(ping)
        ask = getattr(self.client, "ping_machine", None)
        if ask is None:
            return ping
        try:
            detail = await (ask(stop=True) if _takes(ask, "stop") else ask())
        except Exception:  # noqa: BLE001 - the plain ping still holds
            log.exception("machine ping failed")
            return ping
        if (not isinstance(detail, dict) or detail.get("ok") is not True
                or not same_build(detail) or detail.get("stopping")):
            return ping
        return detail

    async def _with_stop(self, ping: dict) -> dict:
        """The stop ping ({"op": "ping", "stop": true}) in place of `ping`, a ping of a service
        of this very build without a machine: a stop it owes a machine shows while that machine
        is not connected. `ping` stays as it is when the client cannot ask or the answer is not
        an ok reply of this build."""
        ask = getattr(self.client, "ping_stop", None)
        if ask is None:
            return ping
        try:
            detail = await ask()
        except Exception:  # noqa: BLE001 - the plain ping still holds
            log.exception("stop ping failed")
            return ping
        if (not isinstance(detail, dict) or detail.get("ok") is not True
                or not same_build(detail) or detail.get("stopping")
                or detail.get("connected") != ping.get("connected")):
            return ping
        return detail

    async def _ping(self) -> dict | None:
        """The ping of the service in use: normally the one on the client's socket; while an
        older build's service runs a job, that one (when it has ended, ours is started)."""
        try:
            if self._older:
                older = await self.client.ping_older()
                if older is not None:
                    return older
                self._older = False          # it ended by itself: start ours, as connect() would
                await self._start()
            return await self.client.ping()
        except Exception:  # noqa: BLE001 - a broken daemon looks like no daemon
            log.exception("ping failed")
            return None

    async def _other_build(self, ping: dict) -> MachineState | None:
        """The state for a service of another build (or an older one), or None after it was
        replaced by ours."""
        self._start_problem = None
        self._waiting_since = None
        device = ping.get("device") or {}
        busy = MachineState(link=Link.CONNECTED if ping.get("connected") else Link.CONNECTING,
                            status=ping.get("status"), motion=ping.get("motion"),
                            device_name=device.get("name"), blocker=OTHER_VERSION_BUSY)
        if ping.get("motion"):
            return busy     # never touched while it runs a job: Send is blocked; next poll again
        if await self._app_outdated(ping):
            return MachineState(link=Link.FAILED, problem=APP_OUTDATED)    # the app is the old one
        if self._replaces_left <= 0 and self._replace_problem:
            return MachineState(link=Link.FAILED, problem=self._replace_problem)
        if (self._released or self._replaces_left <= 0
                or not getattr(self.client, "manages_service", False)):
            return MachineState(link=Link.FAILED,                       # do not fight
                                problem=another_version(self._texts()["name"]))
        if not await self._replace(ping):
            return busy     # a job started there meanwhile: wait for it to end
        return None

    async def _app_outdated(self, ping: dict) -> bool:
        """True when this app is older than OpenCutter's files on disk (it was updated or edited
        while open): the service it started itself reports another build
        (``client.started_here``: this run's id, or its pid outside a sandbox), or the package
        on disk is no longer the build this app runs. Every service it could start would be of
        another build, so it must not replace any. Stays True for this run.

        In the Flatpak neither happens after an update: an open window keeps the files it
        started with (its /app does not change), and the portal starts the service from those
        same files. A service of the new version that another run started is replaced as any
        other build."""
        if not self._outdated:
            started_here = getattr(self.client, "started_here", None)
            if started_here is not None and started_here(ping):
                self._outdated = True
            else:
                try:
                    self._outdated = await asyncio.to_thread(build_on_disk) != BUILD
                except Exception:  # noqa: BLE001 - unknown: treat the service as the old one
                    log.exception("could not read the build on disk")
            if self._outdated:
                log.warning("OpenCutter was updated while the app was open (app build %s, service "
                            "%s): not replacing the service", BUILD, ping.get("build"))
        return self._outdated

    async def _replace(self, ping: dict) -> bool:
        """End the service of another build (no job runs there) and start ours: the shutdown op
        for a service that reports a build, the guarded SIGTERM for an older one. False when it
        refused because a job had started there (the replace is then still to come)."""
        self._replaces_left -= 1
        self._replacing = True
        self._set(MachineState(link=Link.CONNECTING, note=UPDATING_SERVICE))
        try:
            if self._older:
                result = await self.client.terminate_older()
            elif ping.get("build") is None:
                result = await self.client.terminate_older(self.client.socket_path)
            else:
                result = await self.client.shutdown()
            if not result.get("ok"):
                log.warning("could not stop the background service of another version: %s",
                            result.get("error"))
                if result.get("busy"):
                    self._replaces_left += 1          # a job started meanwhile: wait for it
                    return False
                if "did not end" in str(result.get("error")):
                    self._start_problem = self._replace_problem = SERVICE_NOT_REPLACED
                else:                                 # the guard refused, or it went away
                    self._replace_problem = SERVICE_NOT_STOPPED
                return True
            log.info("stopped the background service of another version (pid %s)",
                     result.get("pid"))
            self._older = False
            await self._start()
        except Exception:  # noqa: BLE001 - shown as Could not Connect, the details in the log
            log.exception("could not replace the background service")
            self._start_problem = SERVICE_NOT_STARTED
        finally:
            self._replacing = False
            self._waiting_since = None
        return True

    def _waiting_link(self, old: MachineState) -> Link:
        now = self._clock()
        if old.link is Link.CONNECTED:
            self._waiting_since = now - CONNECT_GRACE_SECONDS      # the link dropped
        elif self._waiting_since is None:
            self._waiting_since = now
        waited = now - self._waiting_since
        return Link.CONNECTING if waited < CONNECT_GRACE_SECONDS else Link.FAILED

    async def _with_info(self, state: MachineState) -> MachineState:
        try:
            info = await self.client.info()
        except Exception:  # noqa: BLE001 - keep what ping said
            log.exception("info failed")
            return state
        if not info or not info.get("ok"):
            return state
        profile = state.machine_profile
        tools = _parse_tools(info.get("tools"), profile)
        unclear = _unclear_tools(info.get("tools"), profile) if tools is None else None
        if tools is not None or unclear is not None:     # read, or reported but unreadable
            tools_reply = unclear
        else:                                  # nothing readable: keep what was read last
            tools, tools_reply = state.tools, state.tools_reply
        firmware = info.get("firmware")
        return replace(state, status=info.get("status") or state.status, tools=tools,
                       tools_reply=tools_reply, firmware=shown_model(firmware) or state.firmware)

    def _set(self, new: MachineState) -> None:
        new = replace(new, choice=self.choice)
        if not new.connected:
            wrong = new.wrong_model
            new = replace(new, tools=None, tools_reply=None, status=None, firmware=None,
                          motion=None, profile=None, paused=False, resume_ok=False,
                          pause_ok=False, no_reply_s=None, indication_age_s=None,
                          model=new.model if wrong else None,
                          support=new.support if wrong else None,
                          refused_reason=new.refused_reason if wrong else None,
                          recognized=new.recognized if wrong else None)
        if new == self.state:
            return
        self.state = new
        for callback in list(self._listeners):
            try:
                callback(new)
            except Exception:  # noqa: BLE001 - one broken view must not stop the others
                log.exception("machine state listener failed")
