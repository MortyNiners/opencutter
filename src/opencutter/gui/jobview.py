# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The job progress view that replaces the sidebar while the machine works.

It lists the daemon's steps, highlights the current one, holds at the outline gate
('Is the outline in the right place? Cut / Cancel') and always shows a large Cancel Job.

The machine's Pause (show_machine, from the window's machine state): while the machine is
paused during the job, the current step shows a pause icon instead of the spinner, the note
says that OpenCutter sends nothing more until it goes on and how to go on or end the job
(machine.paused_note), and Resume (suggested style, above Cancel Job) is shown only while the
service would send it (MachineState.offers_resume). A click sends the service's resume op once
through the window ('Resuming…' until its answer); a machine still paused RESUME_SECONDS later
gets NOT_RESUMED, and nothing is sent again by itself. On a machine whose Pause the app offers
(the CAMEO 4 on firmware V5: MachineState.offers_pause), Pause is shown while a step runs,
not at the outline check and not while the job ends (MachineState.ends_job), and is sensitive
only while the service would send it: a click sends the pause op once ('Pausing…'), and the
paused state then comes from the service, never from the click. Both are insensitive for a
moment after an answer (hold_commands: the window's double-click guard, so that a second click
never seems to be taken and then ignored). While the query the job waits for has had no answer
for 7 s or more, the note says so (machine.checking_note). No text tells to press the
machine's Pause key after a stop: the finished pages never advise it (STILL_PAUSED only says
that the machine still reports Paused, and to press Unload).

When the run has ended, the finished page keeps a Stop Machine button (the service's stop op,
also without a session: the window sends it), says 'Stopping…' until Cancel's stop has its
reply, and then 'Job cancelled' or 'Stop not confirmed'. After a Cancel while the machine was
paused it says CANCELLING_PAUSED while the stop's reply waits for the machine to end the job,
and STILL_PAUSED when the machine still reported Paused at the end. A stop that could not be
sent is shown in red above both pages, whenever its reply comes. After a cancelled, stopped or
failed run the hint says to check that the tool is up, press Unload and load the mat before the
next job (after a completed one, to unload and load the mat again); 'Send Again…' (never 'Try
Again', which reconnects elsewhere) opens the Send flow, and is not offered while the stop is
not confirmed, after a lost link, or while the machine is not connected. A run the service
refused before any step says 'Nothing was sent'. A link lost during the run shows in the view,
and at the outline gate Cut is then disabled for the rest of the run (the session cannot use a
new link).

Every mat of a machine profile is a cutting mat, so the texts speak of the mat on every
machine. When the machine's reply to the unit check could not be checked on this model (a mat
whose reply is not known, on an untested machine: JobRunner.units_note), the view says so while
the job runs and when it has ended; the job goes on.
"""
from __future__ import annotations

from dataclasses import dataclass

from gi.repository import Adw, GLib, GObject, Gtk

from .geometry import format_clock, format_duration
from .machine import MachineState, checking_note, paused_note
from .runner import JobRunner, Outcome, RunStep, StepState
from .widgets import InlineNote, PropertyList, icon_label, label

POWER_HINT = "If the machine keeps moving, press its power button."
STOP_FAILED_TEXT = "The stop could not be sent. Press the machine's power button now."
STOPPING = "Stopping…"
STOPPING_HINT = "Waiting for the machine to confirm the stop. " + POWER_HINT
NOTHING_SENT = "Nothing was sent"
# before the next job, after a run that went to the machine (never: press Pause to continue)
RELOAD_AFTER_STOP = ("Before the next job: check that the tool is up, press <b>Unload</b>, then "
                     "load the mat.")
RELOAD_AFTER_STOP_FAILED = ("Once the machine has stopped, check that the tool is up and load the "
                            "mat again before the next job.")
DONE_HINT = ("Press <b>Unload</b> on the machine to remove the mat. Load the mat again right "
             "before the next job.")
GATE_LINK_LOST = ("The connection to the {name} was lost while the job waited at the outline "
                  "check, so the stop could not be sent. The machine was waiting there with the "
                  "tool up; nothing more was sent. If it moves, press its power button.")
LINK_LOST_NOTE = "The connection to the {name} was lost. Nothing more is sent."
GATE_CUT_OFF = ("The connection was lost while the job waited here, so Cut cannot be sent in "
                "this job: press Cancel.")
STILL_BUSY = ("The machine still reports Busy. If it moves, press Stop Machine or its power "
              "button.")
STOP_OWED = ("The stop has not reached the {name} yet: OpenCutter sends it as soon as the {name} "
             "is connected again. If it moves, press its power button.")
STOP_MACHINE = "Stop Machine"
SEND_AGAIN = "Send Again…"
CUT_AGAIN = "Cut Again"
# the machine's Pause during the job (NOT_RESUMED and the other notes of a click: runner)
RESUME = "Resume"
RESUMING = "Resuming…"
PAUSE = "Pause"
PAUSING = "Pausing…"
PAUSED_ICON = "media-playback-pause-symbolic"
# after Cancel while the machine was paused: its stop's reply waits for the machine to end the job
CANCELLING = "Cancelling…"                 # the pill meanwhile
CANCELLING_PAUSED = ("Cancelling… A paused machine ends the job about 5 seconds after the "
                     "stop.")
STILL_PAUSED = ("The machine still reports Paused {n} s after the stop. Press Unload on the "
                "machine; if anything moves, press its power button.")


def still_paused_text(seconds: float | None) -> str:
    """STILL_PAUSED with the whole seconds since the stop."""
    return STILL_PAUSED.format(n=max(0, round(seconds or 0)))


@dataclass
class JobInfo:
    """What the view says about the job besides the steps."""
    test_cut: bool
    subtitle: str              # 'my_design.svg · Vinyl, Glossy · Cut · Tool 1'
    material: str
    action: str
    passes: int


class _StepRow(Gtk.Box):
    """One step: state icon, title, and for the current step progress or the gate."""

    def __init__(self, step: RunStep, on_cut, on_cancel):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("job-step")
        top = Gtk.Box(spacing=12)
        self._icon = Gtk.Image(pixel_size=16)
        self._spinner = Adw.Spinner(visible=False)
        self._title = label(step.title, wrap=True)
        self._title.set_hexpand(True)
        self._expect = label(step.expect, "caption", "dim-label", wrap=True)
        self._meta = label("", "caption", "dim-label", "numeric")
        for widget in (self._icon, self._spinner, self._title, self._meta):
            top.append(widget)
        self.append(top)
        self.append(self._expect)

        self._bar = Gtk.ProgressBar(visible=False)
        self._bar.update_property([Gtk.AccessibleProperty.LABEL], [f"{step.title} progress"])
        self.append(self._bar)
        times = Gtk.Box()
        self._elapsed = label("", "caption", "dim-label", "numeric")
        self._elapsed.set_hexpand(True)
        self._left = label("", "caption", "dim-label", "numeric", xalign=1.0)
        times.append(self._elapsed)
        times.append(self._left)
        self._times = times
        self.append(times)

        gate = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        gate.add_css_class("gate")
        gate.append(label("Is the outline in the right place?", "heading", wrap=True))
        gate.append(label("The tool traced the outer box of the design with the tool up. "
                          "Nothing was cut yet. To adjust the material, press Cancel; do not "
                          "unload the mat.", "caption", wrap=True))
        buttons = Gtk.Box(spacing=8)
        self.cut_button = Gtk.Button(child=icon_label("media-playback-start-symbolic", "Cut"))
        self.cut_button.add_css_class("suggested-action")
        self.cut_button.add_css_class("pill-sm")
        self.cut_button.update_property([Gtk.AccessibleProperty.LABEL], ["Cut"])
        self.cut_button.connect("clicked", lambda *_: on_cut())
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("pill-sm")
        cancel.connect("clicked", lambda *_: on_cancel())
        buttons.append(self.cut_button)
        buttons.append(cancel)
        gate.append(buttons)
        # the link was lost while waiting here: Cut can never succeed in this run
        self._gate_note = InlineNote(GATE_CUT_OFF, "warning", "dialog-warning-symbolic")
        self._gate_note.set_visible(False)
        gate.append(self._gate_note)
        self._gate = Gtk.Revealer(child=gate)
        self.append(self._gate)
        self._bar.set_tooltip_text("Estimated from the plan, not reported by the machine")
        self.link_lost = False

    def set_link_lost(self, lost: bool) -> None:
        """The link to the machine was lost during this run: Cut is disabled for good."""
        self.link_lost = lost
        self.cut_button.set_sensitive(not lost)
        self._gate_note.set_visible(lost)

    def show_step(self, step: RunStep, runner: JobRunner) -> None:
        state = step.state
        for css in ("current", "done", "todo", "failed"):
            self.remove_css_class(css)
        self.add_css_class({StepState.ACTIVE: "current", StepState.HOLD: "current",
                            StepState.DONE: "done", StepState.FAILED: "failed"}.get(state, "todo"))
        icons = {StepState.DONE: "object-select-symbolic", StepState.HOLD: "find-location-symbolic",
                 StepState.FAILED: "dialog-error-symbolic", StepState.TODO: "radio-symbolic"}
        # a paused machine: the pause icon instead of the spinner (nothing is moving)
        paused = state is StepState.ACTIVE and runner.machine_paused
        self._spinner.set_visible(state is StepState.ACTIVE and not paused)
        self._icon.set_visible(state is not StepState.ACTIVE or paused)
        if paused:
            self._icon.set_from_icon_name(PAUSED_ICON)
        elif state in icons:
            self._icon.set_from_icon_name(icons[state])
        fraction = runner.step_progress(step)
        self._meta.set_text(f"{round(fraction * 100)}%" if fraction is not None else "")
        self._bar.set_visible(fraction is not None)
        self._times.set_visible(fraction is not None)
        if fraction is not None:
            self._bar.set_fraction(fraction)
            self._elapsed.set_text(f"{format_clock(runner.elapsed())} elapsed")
            left = runner.seconds_left()
            self._left.set_text(f"about {format_clock(left)} left" if left is not None else "")
        self._expect.set_visible(state is StepState.ACTIVE and bool(step.expect))
        # no focus is moved to Cut: a stray Enter must never lower the blade
        self._gate.set_reveal_child(state is StepState.HOLD)


class JobView(Gtk.Box):
    """Progress, outline gate, Cancel Job, then the finished state with Cut Again / Done."""

    __gtype_name__ = "OpenCutterJobView"
    __gsignals__ = {
        "cancel": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "resume": (GObject.SignalFlags.RUN_FIRST, None, ()),    # the machine's Resume (a click)
        "pause": (GObject.SignalFlags.RUN_FIRST, None, ()),     # the machine's Pause (a click)
        "cut-again": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "done": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "tick": (GObject.SignalFlags.RUN_FIRST, None, ()),    # every second while the job runs
    }

    def __init__(self):
        """Create the view; ``start`` binds it to a runner."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("job-view")
        self.runner: JobRunner | None = None
        self.info: JobInfo | None = None
        self._rows: list[_StepRow] = []
        self._tick = 0
        self._name = "machine"          # the machine's name in texts (set_machine)
        self._state = MachineState()    # the machine state (show_machine)
        self._held = 0                  # Resume and Pause take no click while set (hold_commands)
        self._connected = True
        self._link_lost = False
        self._focus_done = False       # move the focus to Done once it can take it
        # the stop could not be sent: above both pages, so it shows whenever the reply comes
        self._error = InlineNote("", "error", "dialog-error-symbolic")
        self._error.set_margin_start(24)
        self._error.set_margin_end(24)
        self._error.set_margin_top(16)
        self.append(self._error)
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self._stack.add_named(self._build_running(), "running")
        self._stack.add_named(self._build_finished(), "finished")
        self.append(self._stack)

    # --- building --------------------------------------------------------------------
    def _build_running(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        body.add_css_class("sidebar-panel")
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._eyebrow = label("Job in progress", "field-label")
        self._title = label("", "title-2", wrap=True)
        self._subtitle = label("", "caption", "dim-label", wrap=True)
        for widget in (self._eyebrow, self._title, self._subtitle):
            head.append(widget)
        body.append(head)
        self._steps = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._steps.update_property([Gtk.AccessibleProperty.LABEL], ["Job steps"])
        body.append(self._steps)
        # the link was lost during the run: nothing more is sent
        self._lost = InlineNote("", "warning", "dialog-warning-symbolic")
        body.append(self._lost)
        # the cut area could not be checked on this model: a note, the job goes on
        self._units = InlineNote()
        body.append(self._units)
        # no answer from the machine for 7 s or more; the machine is paused; what came of the
        # last Resume or Pause
        self._checking = InlineNote("", "warning", "dialog-warning-symbolic")
        self._paused = InlineNote("", "warning", PAUSED_ICON)
        self._command = InlineNote("", "warning", "dialog-warning-symbolic")
        for note in (self._checking, self._paused, self._command):
            body.append(note)
        page.append(Gtk.ScrolledWindow(child=body, vexpand=True,
                                       hscrollbar_policy=Gtk.PolicyType.NEVER))

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        footer.add_css_class("send-footer")
        # Resume (while paused) or Pause (while a step runs), above Cancel Job, in the same
        # place. Neither takes the focus by a click or from the view: a stray Enter or Space
        # must never start the machine (keyboard users still reach them with Tab)
        self._commands = Gtk.Box(spacing=8, homogeneous=True, visible=False)
        self.resume_button = Gtk.Button(child=icon_label("media-playback-start-symbolic", RESUME),
                                        hexpand=True, visible=False, focus_on_click=False)
        self.resume_button.add_css_class("suggested-action")
        self.resume_button.add_css_class("pill-md")
        self.resume_button.update_property([Gtk.AccessibleProperty.LABEL], [RESUME])
        self.resume_button.connect("clicked", lambda *_: self.emit("resume"))
        self.pause_button = Gtk.Button(child=icon_label(PAUSED_ICON, PAUSE), hexpand=True,
                                       visible=False, focus_on_click=False)
        self.pause_button.add_css_class("pill-md")
        self.pause_button.update_property([Gtk.AccessibleProperty.LABEL], [PAUSE])
        self.pause_button.set_tooltip_text("Pause the machine: it finishes the few moves it has "
                                           "planned, lifts the tool and waits")
        self.pause_button.connect("clicked", lambda *_: self.emit("pause"))
        self._commands.append(self.resume_button)
        self._commands.append(self.pause_button)
        footer.append(self._commands)
        content = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        content.append(Gtk.Image.new_from_icon_name("media-playback-stop-symbolic"))
        content.append(Gtk.Label(label="Cancel Job"))
        kbd = Gtk.Label(label="Esc", valign=Gtk.Align.CENTER)
        kbd.add_css_class("kbd")
        content.append(kbd)
        self.cancel_button = Gtk.Button(child=content)
        self.cancel_button.add_css_class("cancel-job")
        self.cancel_button.update_property([Gtk.AccessibleProperty.LABEL], ["Cancel Job"])
        self.cancel_button.update_property([Gtk.AccessibleProperty.KEY_SHORTCUTS], ["Escape"])
        self.cancel_button.connect("clicked", lambda *_: self.emit("cancel"))
        footer.append(self.cancel_button)
        footer.append(label(POWER_HINT, "caption", "dim-label", wrap=True, xalign=0.5))
        page.append(footer)
        return page

    def _build_finished(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, valign=Gtk.Align.CENTER)
        page.add_css_class("sidebar-panel")
        page.add_css_class("finished")
        self._done_icon = Gtk.Image(pixel_size=44)
        self._done_title = label("", "title-2", xalign=0.5)
        self._done_time = label("", "dim-label", "numeric", xalign=0.5)
        self._done_props = Gtk.Box(halign=Gtk.Align.CENTER)
        self._done_hint = label("", wrap=True, markup=True, xalign=0.5)
        self._done_hint.set_justify(Gtk.Justification.CENTER)
        self._done_units = InlineNote()
        # after the run: the machine still reports Busy, or Paused after the stop of a paused
        # job, or a stop is still owed to it
        self._still_busy = InlineNote(STILL_BUSY, "warning", "dialog-warning-symbolic")
        self._still_busy.set_visible(False)
        self._still_paused = InlineNote("", "warning", "dialog-warning-symbolic")
        self._owed = InlineNote("", "warning", "dialog-warning-symbolic")
        for widget in (self._done_icon, self._done_title, self._done_time, self._done_props,
                       self._done_hint, self._done_units, self._still_busy, self._still_paused,
                       self._owed):
            page.append(widget)
        # the service's stop op, also without a session (the window sends it)
        self.stop_button = Gtk.Button(child=icon_label("media-playback-stop-symbolic", STOP_MACHINE),
                                      halign=Gtk.Align.CENTER, action_name="win.stop-machine")
        self.stop_button.add_css_class("pill-sm")
        self.stop_button.update_property([Gtk.AccessibleProperty.LABEL], [STOP_MACHINE])
        page.append(self.stop_button)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self._again = Gtk.Button(label=CUT_AGAIN)
        self._again.add_css_class("pill-sm")
        self._again.connect("clicked", lambda *_: self.emit("cut-again"))
        done = Gtk.Button(label="Done")
        done.add_css_class("suggested-action")
        done.add_css_class("pill-sm")
        done.connect("clicked", lambda *_: self.emit("done"))
        self._done_button = done
        buttons.append(self._again)
        buttons.append(done)
        page.append(buttons)
        return page

    # --- binding ---------------------------------------------------------------------
    def start(self, runner: JobRunner, info: JobInfo) -> None:
        """Show a new run (steps arrive with the runner's first update)."""
        self.runner, self.info = runner, info
        self._rows = []
        self._link_lost = False
        self._focus_done = False
        while (child := self._steps.get_first_child()) is not None:
            self._steps.remove(child)
        self._eyebrow.set_text("Test Cut" if info.test_cut else "Job in progress")
        self._title.set_text("Test in Progress" if info.test_cut else "Preparing")
        self._subtitle.set_text(info.subtitle)
        self._error.show_text(None)
        self._lost.show_text(None)
        self._units.show_text(None)
        self._still_busy.set_visible(False)
        self._still_paused.show_text(None)
        self._owed.show_text(None)
        self.cancel_button.set_sensitive(True)
        self._render_machine()
        self._stack.set_visible_child_name("running")
        if not self._tick:
            self._tick = GLib.timeout_add_seconds(1, self._on_tick)

    def set_machine(self, name: str, connected: bool) -> None:
        """The machine's name for the texts, and whether it is connected now (Send Again… is
        offered only then)."""
        changed = (name, connected) != (self._name, self._connected)
        self._name, self._connected = name, connected
        if changed and self.finished:
            self._render_finished(self.runner)

    def show_machine(self, state: MachineState) -> None:
        """The machine state for the running page: paused (note, Resume), Pause, and no answer
        for 7 s or more (Checking connection)."""
        self._state = state
        self._render_machine()

    def _render_machine(self) -> None:
        """The pause and connection notes and the Resume and Pause buttons, from the machine
        state and the runner (its pending Resume or Pause, and the note of the last one)."""
        runner, state = self.runner, self._state
        running = runner is not None and runner.outcome is Outcome.RUNNING
        paused = running and state.shows_paused
        pending = runner.command_pending if running else None
        self._checking.show_text(checking_note(state) if running else None)
        self._paused.show_text(paused_note(state) if paused else None)
        self._command.show_text(runner.command_note if running else None)
        free = pending is None and not self._held     # no op runs, and not right after one
        resume = running and paused and (state.offers_resume or pending == "resume")
        self.resume_button.set_visible(resume)
        self.resume_button.set_sensitive(free)
        self._button_text(self.resume_button, RESUMING if pending == "resume" else RESUME)
        pause = (running and not paused and not state.ends_job and state.offers_pause
                 and not runner.holding and runner.current is not None)
        self.pause_button.set_visible(pause)
        self.pause_button.set_sensitive(free and state.pause_ok)
        self._button_text(self.pause_button, PAUSING if pending == "pause" else PAUSE)
        self._commands.set_visible(resume or pause)

    def hold_commands(self, seconds: float) -> None:
        """Resume and Pause take no click for `seconds` (the window's guard after the answer to
        one of them): the second click of a double click lands on the other button, which
        shows that it is not clickable yet instead of ignoring the click."""
        if seconds <= 0:
            return
        self._held += 1
        self._render_machine()
        GLib.timeout_add(int(seconds * 1000) + 20, self._release_commands)

    def _release_commands(self) -> bool:
        self._held = max(0, self._held - 1)
        self._render_machine()
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _button_text(button: Gtk.Button, text: str) -> None:
        """The text of an icon_label button (and its accessible label)."""
        box = button.get_child()
        child = box.get_last_child() if box is not None else None
        if isinstance(child, Gtk.Label) and child.get_text() != text:
            child.set_text(text)
            button.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def show_link_lost(self) -> None:
        """The service lost the link during this run: say so, and disable Cut at the gate."""
        self._link_lost = True
        self._lost.show_text(LINK_LOST_NOTE.format(name=self._name))
        for row in self._rows:
            row.set_link_lost(True)

    def show_still_busy(self, busy: bool) -> None:
        """After the run: the machine kept reporting Busy for a while."""
        self._still_busy.set_visible(busy and self.finished)

    def show_stop_owed(self, owed: bool) -> None:
        """After the run: a stop is owed to the machine (the service sends it on the next link)."""
        self._owed.show_text(STOP_OWED.format(name=self._name) if owed and self.finished else None)

    @property
    def finished(self) -> bool:
        """True while the finished page shows."""
        return self.runner is not None and self.runner.outcome is not Outcome.RUNNING

    def update(self, runner: JobRunner) -> None:
        """Redraw from the runner's state (called on every runner update)."""
        if runner is not self.runner:
            return
        if len(self._rows) != len(runner.steps):
            for step in runner.steps[len(self._rows):]:
                row = _StepRow(step, runner.confirm_cut, lambda: self.emit("cancel"))
                row.set_link_lost(self._link_lost)
                self._rows.append(row)
                self._steps.append(row)
        for row, step in zip(self._rows, runner.steps):
            row.show_step(step, runner)
        current = runner.current
        if current is not None and not self.info.test_cut:
            self._title.set_text("Check the position" if runner.holding else current.title)
        self._error.show_text(STOP_FAILED_TEXT if runner.stop_failed else None)
        self._units.show_text(runner.units_note)
        self._render_machine()
        if runner.outcome is not Outcome.RUNNING:
            self._finish(runner)

    def _on_tick(self) -> bool:
        if self.runner is None or self.runner.outcome is not Outcome.RUNNING:
            self._tick = 0
            return GLib.SOURCE_REMOVE
        self.update(self.runner)
        self.emit("tick")
        return GLib.SOURCE_CONTINUE

    def _finish(self, runner: JobRunner) -> None:
        if self._stack.get_visible_child_name() != "finished":
            self._focus_done = True
        self.cancel_button.set_sensitive(False)
        self._render_finished(runner)
        self._stack.set_visible_child_name("finished")
        if self._focus_done and self._done_button.get_sensitive():
            self._focus_done = False
            self._done_button.grab_focus()

    def _title_text(self, runner: JobRunner) -> str:
        info, outcome = self.info, runner.outcome
        verb = "Test" if info.test_cut else info.action
        if runner.stop_pending:
            return STOPPING
        if runner.refused_before_start:
            return NOTHING_SENT
        if runner.stop_failed and outcome in (Outcome.FAILED, Outcome.CANCELLED):
            return "Stop not confirmed"
        titles = {Outcome.DONE: "Test Complete" if info.test_cut else f"{info.action or 'Job'} complete",
                  Outcome.CANCELLED: "Job cancelled", Outcome.FAILED: f"{verb} stopped",
                  Outcome.LOST: "Connection lost"}
        return titles[outcome]

    def _hint_markup(self, runner: JobRunner) -> str:
        outcome = runner.outcome
        reason = GLib.markup_escape_text(runner.error or "")
        lead = f"{reason}\n" if reason else ""
        if runner.stop_pending:
            # a paused machine ends the job only some seconds after the stop
            return GLib.markup_escape_text(CANCELLING_PAUSED if runner.paused_cancel
                                           else STOPPING_HINT)
        if runner.refused_before_start:
            return reason
        if outcome is Outcome.DONE:
            return DONE_HINT
        if runner.stop_failed:
            return (lead + f"<b>{GLib.markup_escape_text(STOP_FAILED_TEXT)}</b>\n"
                    + RELOAD_AFTER_STOP_FAILED)
        if outcome is Outcome.CANCELLED and runner.cancelled_at_gate and runner.link_lost:
            return (GLib.markup_escape_text(GATE_LINK_LOST.format(name=self._name)) + "\n"
                    + RELOAD_AFTER_STOP)
        return lead + GLib.markup_escape_text(POWER_HINT) + "\n" + RELOAD_AFTER_STOP

    def _render_finished(self, runner: JobRunner) -> None:
        """The finished page for the runner's current state (again on every later update: the
        stop's reply, the machine's state)."""
        info = self.info
        outcome = runner.outcome
        icons = {Outcome.DONE: "object-select-symbolic", Outcome.CANCELLED: "media-playback-stop-symbolic"}
        self._done_icon.set_from_icon_name(icons.get(outcome, "dialog-error-symbolic"))
        for css in ("success", "error"):
            self._done_icon.remove_css_class(css)
        self._done_icon.add_css_class("success" if outcome is Outcome.DONE else "error")
        self._done_title.set_text(self._title_text(runner))
        self._done_time.set_text(format_duration(runner.elapsed()))
        props = PropertyList()
        props.add("Material", info.material)
        props.add("Action", info.action)
        props.add("Passes", str(info.passes))
        self._done_props.set_visible(outcome is Outcome.DONE)
        if (old := self._done_props.get_first_child()) is not None:
            self._done_props.remove(old)
        self._done_props.append(props)
        self._done_hint.set_markup(self._hint_markup(runner))
        self._done_units.show_text(runner.units_note)
        self._still_paused.show_text(still_paused_text(runner.stop_seconds)
                                     if runner.still_paused and not runner.stop_pending else None)
        # Stop Machine: prominent after anything but a completed run
        if outcome is Outcome.DONE:
            self.stop_button.remove_css_class("destructive-action")
            self.stop_button.add_css_class("flat")
        else:
            self.stop_button.remove_css_class("flat")
            self.stop_button.add_css_class("destructive-action")
        self._again.set_label(CUT_AGAIN if outcome is Outcome.DONE else SEND_AGAIN)
        lost = outcome is Outcome.LOST or runner.link_lost or self._link_lost
        self._again.set_visible(not info.test_cut and not runner.stop_failed and not lost
                                and self._connected)
        pending = runner.stop_pending
        self._again.set_sensitive(not pending)
        self._done_button.set_sensitive(not pending)
