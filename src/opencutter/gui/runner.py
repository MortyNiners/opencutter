# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Runs one job (or Test Cut) through a daemon session, one step at a time.

The runner never builds bytes: it sends the daemon a JobSpec and then asks for the steps the
daemon's plan returned, in order. A step marked ``confirm`` waits at the outline gate until
the user presses Cut. Cancel sends ``DaemonClient.stop()`` at once and drops the session
(closing the session mid-step also stops the machine).

The unit check (the plan's ``units`` step) must come back with the verdict ``ok``: anything
else ends the run before the next step, whatever the daemon decided (defense in depth). The one
exception is the verdict ``unchecked``, and only for a plan whose summary says the unit check is
not enforced ("units_enforced" false: a mat whose U reply is not known, offered on an untested
machine only) when the app's own profile of that machine agrees and the summary names the
machine and mat of the JobSpec the app sent (units_unchecked()). The run then goes on, and
``units_note`` says that the cut area could not be checked on this model, with the machine's
reply (units_note()).

Reload hints name the mat when the machine has more than one (reload_hint()): every mat of a
machine profile is a cutting mat, so the wording is the same for all of them.

Stopping. Cancel ends the run at once (Outcome.CANCELLED), but the stop's reply may come later
(up to the client's STOP_TIMEOUT): ``stop_pending`` is True until it arrives, then
``stop_failed`` says whether it got through, and the runner notifies again. A cancel at the
outline gate whose stop could not be sent because the service has no link to the machine is
not a failed stop: the machine waited there with the tool up after the preview had ended, and
nothing can be sent without the link (``link_lost``). A step that failed with the service's
link-loss text sets ``link_lost`` too. ``stopped_elsewhere()`` ends a run that waits at the
gate when another client (``opencutter stop``) stopped the session. ``refused_before_start``:
the session was refused before any step, so nothing was sent.

Activity. ``activity(runner, verb)`` names what the current step does for the pill and the
status bar: 'Preparing' (header, unit check, tool setup), 'Previewing outline', the verb only on
the cut steps (the plan's steps titled with a group's action), 'Returning to origin' after the
last one. A step whose ``tool_down`` (from the service) is False is never called cutting.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from opencutter.identity import same_build
from opencutter.job.spec import DEFAULT_MACHINE
from opencutter.machines import CAMEO4, MachineProfile, Mat, enabled_profile

from .machine import OTHER_VERSION_REFUSED, shown_model

log = logging.getLogger(__name__)

UNITS_KIND = "units"
RELOAD_HINT = "Unload and load the mat on the machine, then send again."
RELOAD_MAT_HINT = "Unload and load the {label} mat on the machine, then send again."
UNITS_UNCHECKED = "The cut area could not be checked on this model."
# the service's text for every step that failed because the Bluetooth link went down
# (device.daemon.LINK_LOST; the GUI does not import the service)
LINK_LOST = "the Bluetooth link was lost"
STOPPED_OUTSIDE = ("Stopped from outside the app (for example with the opencutter stop "
                   "command). Nothing was cut.")
PREPARING = "Preparing"
PREVIEWING = "Previewing outline"
RETURNING = "Returning to origin"


class StepState(Enum):
    """Where a step is in the run."""
    TODO = "todo"
    ACTIVE = "active"
    HOLD = "hold"          # waiting at the outline gate for the user's Cut
    DONE = "done"
    FAILED = "failed"


class Outcome(Enum):
    """How the run ended (RUNNING while it has not)."""
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"      # the daemon refused or aborted a step
    LOST = "lost"          # the session with the daemon broke


@dataclass
class RunStep:
    """One step of the daemon's plan, as the GUI shows it."""
    title: str
    expect: str = ""
    confirm: bool = False
    writes: int = 1
    kind: str = ""                 # the plan's step kind: 'write' or 'units'
    tool_down: bool | None = None  # the step holds a write that can lower a tool (the service)
    state: StepState = StepState.TODO
    started_at: float | None = None
    seconds: float | None = None


def reload_hint(mat: Mat | None = None, profile: MachineProfile = CAMEO4) -> str:
    """What to do after an unconfirmed unit: RELOAD_HINT, naming the mat when the machine has
    more than one ('Unload and load the 12 × 24 in mat ...')."""
    if mat is None or len(profile.mats) < 2:
        return RELOAD_HINT
    return RELOAD_MAT_HINT.format(label=mat.label)


def summary_reload_hint(summary: dict) -> str:
    """reload_hint() for the machine and mat a plan summary names ("machine", "mat")."""
    profile = enabled_profile(summary.get("machine")) or CAMEO4
    return reload_hint(profile.mat(summary.get("mat")), profile)


def units_error(result: dict, hint: str = RELOAD_HINT) -> str:
    """The message for a unit check (U) that did not confirm the unit."""
    detail = result.get("message") or result.get("error") or "no answer"
    return f"The machine did not confirm the unit ({detail}). {hint}"


def units_note(result: dict) -> str:
    """UNITS_UNCHECKED with the machine's reply ('... The machine answered “12200,6100”.'):
    the job view shows it for a unit check that is not enforced. The reply comes from the
    machine: short printable text, never markup unescaped."""
    reply = shown_model(result.get("reply"))
    return f"{UNITS_UNCHECKED} The machine answered “{reply}”." if reply else UNITS_UNCHECKED


def units_unchecked(result: dict, summary: dict, spec: dict | None = None) -> bool:
    """True for the verdict 'unchecked' of a plan whose unit check is not enforced: the
    summary's "units_enforced" is False, and the app's own profile of the summary's machine
    agrees (its mat of that key has a U reply that is not known: Mat.enforce_u is False). With
    `spec`, the JobSpec dict the app sent, its machine and mat must be the summary's too. Every
    other verdict but 'ok' ends the run (fail closed)."""
    if result.get("verdict") != "unchecked" or summary.get("units_enforced") is not False:
        return False
    machine, mat = summary.get("machine"), summary.get("mat")
    if spec is not None and (spec.get("machine", DEFAULT_MACHINE), spec.get("mat")) != (
            machine, mat):
        return False
    profile = enabled_profile(machine)
    known = profile.mat(mat) if profile is not None else None
    return known is not None and not known.enforce_u


def estimated_seconds(summary: dict) -> float | None:
    """The plan's time estimate in seconds, if it has one."""
    value = summary.get("estimated_seconds")
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _flag(value: object) -> bool | None:
    """A boolean of the service's reply, or None for anything else (a key it did not send)."""
    return value if isinstance(value, bool) else None


def cut_steps(steps: list[RunStep], summary: dict) -> list[int]:
    """The indices of the steps that cut (score, sketch): the plan titles them with a group's
    action ('Cut with the AutoBlade'), and the service never marks them tool_down False. Without
    such titles, the steps the service marks tool_down; without that either, every step (as
    before the service said)."""
    actions = {g.get("action") for g in summary.get("groups") or [] if isinstance(g, dict)}
    actions = {a for a in actions if isinstance(a, str) and a}
    titled = [i for i, s in enumerate(steps)
              if s.kind == "write" and s.tool_down is not False
              and any(s.title.startswith(f"{a} with") for a in actions)]
    if titled:
        return titled
    if any(s.tool_down is not None for s in steps):
        return [i for i, s in enumerate(steps) if s.tool_down]
    return list(range(len(steps)))


def activity(runner: "JobRunner", verb: str) -> tuple[str, bool]:
    """(what the current step does, whether it cuts): `verb` ('Cutting') on a cut step,
    PREVIEWING on the outline preview, RETURNING after the last cut step, PREPARING otherwise
    (header, unit check, tool setup)."""
    current = runner.current
    if current is None:
        return PREPARING, False
    index = next(i for i, s in enumerate(runner.steps) if s is current)
    cuts = cut_steps(runner.steps, runner.summary)
    if index in cuts:
        return verb, True
    if cuts and index > cuts[-1]:
        return RETURNING, False
    if current.kind == "write" and current.tool_down is not True and current.title.startswith(
            "Preview outline"):
        return PREVIEWING, False
    return PREPARING, False


class JobRunner:
    """Drives one session; the GUI reads its fields after each ``on_update`` call."""

    def __init__(self, client, on_update: Callable[["JobRunner"], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        """``client`` is a ``DaemonClient`` (or a fake with the same methods)."""
        self.client = client
        self.steps: list[RunStep] = []
        self.summary: dict = {}
        self.spec: dict | None = None   # the JobSpec dict of run_job (units_unchecked)
        self.outcome = Outcome.RUNNING
        self.error: str | None = None
        self.stop_failed = False       # the stop request could not be delivered
        self.stop_pending = False      # Cancel sent the stop; its reply has not come yet
        self.stop_reply: dict | None = None   # the reply of Cancel's stop, once it came
        self.cancelled_at_gate = False  # Cancel came while the run waited at the outline gate
        self.link_lost = False         # the run ended because the service lost the link
        self.stopped_outside = False   # another client stopped the session at the gate
        self.refused_before_start = False     # refused before any step: nothing was sent
        self.units_note: str | None = None   # the cut area could not be checked (units_note())
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._on_update = on_update or (lambda runner: None)
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._gate: asyncio.Future | None = None
        self._cancelling = False
        self._stop: asyncio.Future | None = None       # Cancel's stop (see cancel())
        self._ended = asyncio.Event()                  # set once the run has an outcome

    # --- running ---------------------------------------------------------------------
    async def run_job(self, spec: dict) -> Outcome:
        """Open a job session with a ``JobSpec.to_dict()`` and run it to the end."""
        self.spec = spec
        return await self._run(lambda session: session.open_job(spec))

    async def _run(self, opener: Callable[[object], Awaitable[dict]]) -> Outcome:
        self._task = asyncio.current_task()
        self.started_at = self._clock()
        try:
            # never to a background service of another build (the service checks it too)
            ping = await self.client.ping()
            if ping is not None and not same_build(ping):
                self.refused_before_start = True
                return self._finish(Outcome.FAILED, OTHER_VERSION_REFUSED)
            async with self.client.session() as session:
                reply = await opener(session)
                if not reply.get("ok"):
                    self.refused_before_start = True
                    return self._finish(Outcome.FAILED, reply.get("error") or "The machine refused the job.")
                self.summary = reply.get("summary") or {}
                self.steps = [RunStep(title=s.get("title") or s.get("kind") or "Step",
                                      expect=s.get("expect") or "", confirm=bool(s.get("confirm")),
                                      writes=max(1, int(s.get("writes") or 1)),
                                      kind=str(s.get("kind") or ""),
                                      tool_down=_flag(s.get("tool_down")))
                              for s in reply.get("steps", [])]
                self._notify()
                for index, step in enumerate(self.steps):
                    if step.confirm and not await self._hold(step):
                        return self._finish(Outcome.CANCELLED,
                                            STOPPED_OUTSIDE if self.stopped_outside else None)
                    step.state, step.started_at = StepState.ACTIVE, self._clock()
                    self._notify()
                    # no client-side timeout: a long cut must never be cut short by the GUI
                    # (dropping the session would stop the machine); Cancel Job stays available
                    result = await session.step(index, timeout=None)
                    if not result.get("ok"):
                        step.state = StepState.FAILED
                        if result.get("stop_errors"):     # the daemon's own stop did not get through
                            self.stop_failed = True
                        if result.get("error") == LINK_LOST:
                            self.link_lost = True
                        return self._finish(Outcome.FAILED, result.get("error") or "The step failed.")
                    if step.kind == UNITS_KIND and units_unchecked(result, self.summary, self.spec):
                        self.units_note = units_note(result)    # shown, not enforced
                    elif step.kind == UNITS_KIND and result.get("verdict") != "ok":
                        # an unconfirmed unit means TG may not have acted: nothing more is sent
                        step.state = StepState.FAILED
                        return self._finish(Outcome.FAILED,
                                            units_error(result, summary_reload_hint(self.summary)))
                    step.state, step.seconds = StepState.DONE, result.get("seconds")
                    self._notify()
                await session.end()
                return self._finish(Outcome.DONE)
        except asyncio.CancelledError:
            if not self._cancelling:
                raise
            asyncio.current_task().uncancel()
            return self._finish(Outcome.CANCELLED)
        except Exception as e:  # noqa: BLE001 - any break of the session means contact is lost
            log.exception("job session broke")
            if self._cancelling:
                return self._finish(Outcome.CANCELLED)
            return self._finish(Outcome.LOST, str(e) or type(e).__name__)

    async def _hold(self, step: RunStep) -> bool:
        step.state = StepState.HOLD
        self._gate = asyncio.get_running_loop().create_future()
        self._notify()
        try:
            return await self._gate
        finally:
            self._gate = None

    def _finish(self, outcome: Outcome, error: str | None = None) -> Outcome:
        self.outcome, self.error = outcome, error
        self.finished_at = self._clock()
        for step in self.steps:
            if step.state in (StepState.ACTIVE, StepState.HOLD):
                step.state = StepState.FAILED if outcome is not Outcome.DONE else StepState.DONE
        self._ended.set()
        self._notify()
        return outcome

    def _notify(self) -> None:
        try:
            self._on_update(self)
        except Exception:  # noqa: BLE001 - a view bug must never break the run
            log.exception("job view update failed")

    # --- user actions ----------------------------------------------------------------
    @property
    def holding(self) -> bool:
        """True while the run waits at the outline gate."""
        return self._gate is not None and not self._gate.done()

    def confirm_cut(self) -> None:
        """The user pressed Cut at the outline gate."""
        if self.holding:
            self._gate.set_result(True)

    def stopped_elsewhere(self) -> None:
        """Another client stopped the session (``opencutter stop``) while the run waits at the
        outline gate: end the run there (CANCELLED, STOPPED_OUTSIDE). Nothing is sent: the stop
        already went out, and the session takes no more steps."""
        if self.holding:
            self.stopped_outside = True
            self._gate.set_result(False)

    async def cancel(self) -> dict:
        """Cancel Job: send the stop right away and drop the session. Returns the stop reply
        (the same one to every caller while it is pending, and after it came); {"ok": True}
        once the run had ended without a Cancel."""
        if self._stop is not None:
            return await asyncio.shield(self._stop)
        if self.outcome is not Outcome.RUNNING:
            return {"ok": True}
        self._cancelling = True
        self.cancelled_at_gate = self.holding
        self.stop_pending = True
        self._stop = asyncio.ensure_future(self._send_stop())
        if self._task is not None and not self._task.done():
            self._task.cancel()
        return await asyncio.shield(self._stop)

    async def _send_stop(self) -> dict:
        try:
            reply = await self.client.stop()
        except Exception as e:  # noqa: BLE001 - DaemonClient.stop never raises; a fake might
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not isinstance(reply, dict):
            reply = {"ok": False, "error": "no answer"}
        if not reply.get("ok"):
            if self.cancelled_at_gate and await self._link_down():
                # at the gate the preview had ended with Ready and nothing was sent since:
                # without a link nothing can reach the machine (not a failed stop)
                self.link_lost = True
            else:
                self.stop_failed = True
        self.stop_reply = reply
        self.stop_pending = False
        self._notify()
        return reply

    async def _link_down(self) -> bool:
        """True when the service answers that it has no link to the machine (or no service
        answers at all: then no session can send anything either)."""
        try:
            ping = await self.client.ping()
        except Exception:  # noqa: BLE001 - no answer: no link either
            return True
        return ping is None or ping.get("connected") is False

    async def wait_stop(self, timeout: float | None = None) -> dict | None:
        """The reply of Cancel's stop, waiting at most `timeout` s for it; None when there was
        no Cancel, or no reply in time."""
        if self._stop is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(self._stop), timeout)
        except TimeoutError:
            return None

    async def wait_ended(self, timeout: float | None = None) -> bool:
        """Wait (at most `timeout` s) until the run has an outcome; True when it has."""
        try:
            await asyncio.wait_for(self._ended.wait(), timeout)
        except TimeoutError:
            pass
        return self.outcome is not Outcome.RUNNING

    @property
    def started(self) -> bool:
        """True once a step went to the machine (the machine may have moved)."""
        return any(s.state is not StepState.TODO or s.started_at is not None for s in self.steps)

    # --- reading ---------------------------------------------------------------------
    @property
    def current(self) -> RunStep | None:
        """The active or holding step."""
        return next((s for s in self.steps if s.state in (StepState.ACTIVE, StepState.HOLD)), None)

    def elapsed(self) -> float:
        """Seconds since the run started (frozen once it ended)."""
        if self.started_at is None:
            return 0.0
        return (self.finished_at or self._clock()) - self.started_at

    def step_progress(self, step: RunStep) -> float | None:
        """Estimated fraction done of an active step, from the plan's time estimate."""
        total = estimated_seconds(self.summary)
        if step.state is not StepState.ACTIVE or step.started_at is None or not total:
            return None
        share = total * step.writes / max(1, sum(s.writes for s in self.steps))
        return min(0.95, (self._clock() - step.started_at) / share) if share > 0 else None

    def progress(self) -> float:
        """Overall fraction done, weighted by each step's number of writes."""
        if self.outcome is Outcome.DONE:
            return 1.0
        total = sum(s.writes for s in self.steps)
        if not total:
            return 0.0
        done = sum(s.writes for s in self.steps if s.state is StepState.DONE)
        active = self.current
        if active is not None:
            done += active.writes * (self.step_progress(active) or 0.0)
        return min(1.0, done / total)

    def seconds_left(self) -> float | None:
        """Rough time left, from the plan's estimate and the progress so far."""
        total = estimated_seconds(self.summary)
        if not total or self.outcome is not Outcome.RUNNING:
            return None
        return max(0.0, total * (1.0 - self.progress()))
