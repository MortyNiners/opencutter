# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""Alert dialogs, Preferences and About.

About names the machines from the registry: the models OpenCutter drives, and apart from them
the ones it only recognizes, as not supported yet. It says nothing about which of them were
tested. Preferences shows the Default Cutting Mat of the layout's machine (GuiSettings.mats, one
per machine) and the model's name, with "Recognized, not supported yet" or "Not supported" for a
machine the service refused. Its Language row stores the window's language (GuiSettings.language:
"System default", English, or a language with a catalog); it takes effect at the next start.
"""
from __future__ import annotations

import asyncio

from gi.repository import Adw, GObject, Gtk

from opencutter import APP_ID, __version__
from opencutter import i18n
from opencutter.i18n import SOURCE_LANGUAGE, N_, _, available_languages, language_name, pgettext
from opencutter.machines import RecognizedModel, by_id, recognized_models
from opencutter.presets.model import PresetStore

from .geometry import format_size, mat_choices, mat_label, media_label, media_size, media_sizes
from .machine import MachineState, drivable_models, join_shown
from .sending import store_description
from .settings import GuiSettings

WEBSITE = "https://github.com/MortyNiners/opencutter"
COPYRIGHT = "Copyright (C) 2026 OpenCutter contributors"
NOT_AFFILIATED = N_("Not affiliated with or endorsed by Silhouette America, Inc. Silhouette, CAMEO, "
                    "Portrait and CURIO are trademarks of their respective owners.")
NOT_SUPPORTED_YET = N_("Recognized, not supported yet")
NOT_SUPPORTED = N_("Not supported")
RESTART_FOR_LANGUAGE = N_("Restart OpenCutter to use the new language")


def model_name(machine: MachineState) -> str:
    """The model the Machine group names: the refused machine's (its registry name, else what
    it reports itself as), else the machine the state names."""
    if machine.wrong_model:
        entry = by_id(machine.recognized)
        return entry.name if entry is not None else machine.model or _("Unknown machine")
    return machine.name


def machines_text() -> str:
    """Which machines OpenCutter drives, and which it recognizes but does not drive, from the
    registry."""
    recognized = list(dict.fromkeys(m.name for m in recognized_models()))
    drivable = drivable_models(shown=True)               # "CAMEO 4 (firmware ...)" translated
    parts = [_("Drives the {models}.").format(models=join_shown(drivable))]
    if recognized:
        parts.append(_("{not_supported_yet}: {models}.").format(
            not_supported_yet=_(NOT_SUPPORTED_YET), models=join_shown(recognized)))
    return " ".join(parts)


def about_comments() -> str:
    """The About dialog's comments: what the app does, the machines and the notice."""
    return (_("Send SVG designs to a Silhouette cutting machine over Bluetooth. {machines}")
            .format(machines=machines_text()) + f"\n\n{_(NOT_AFFILIATED)}")


def support_text(machine: MachineState) -> str | None:
    """For a machine the service refused: 'Recognized, not supported yet' (a model OpenCutter
    recognizes) or 'Not supported'. None for a machine OpenCutter drives: the Model row then
    names it alone."""
    if not machine.wrong_model:
        return None
    recognized = isinstance(by_id(machine.recognized), RecognizedModel)
    return _(NOT_SUPPORTED_YET) if recognized else _(NOT_SUPPORTED)


def model_text(machine: MachineState) -> str:
    """The Model row's subtitle: 'CAMEO 5', or 'CAMEO 3 · Recognized, not supported yet' for a
    machine the service refused."""
    return " · ".join(t for t in (model_name(machine), support_text(machine)) if t)


async def _choose(dialog: Adw.AlertDialog, parent: Gtk.Widget) -> str:
    future = asyncio.get_running_loop().create_future()
    dialog.connect("response", lambda _d, response: future.done() or future.set_result(response))
    dialog.present(parent)
    return await future


async def confirm_quit_during_job(parent: Gtk.Widget) -> bool:
    """'A job is running': True when the user chose to cancel the job and quit."""
    dialog = Adw.AlertDialog(heading=_("A job is running"),
                             body=_("Cancel the job and quit? The machine stops right away."))
    dialog.add_response("keep", _("Keep cutting"))
    dialog.add_response("quit", _("Cancel Job and quit"))
    dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("keep")
    dialog.set_close_response("keep")
    return await _choose(dialog, parent) == "quit"


CONNECTION_LOST_HEADING = N_("Connection lost during the job")
CONNECTION_LOST_BODY = N_("OpenCutter cannot stop the machine without the connection. If it is "
                          "still moving, press its power button. Then switch it on again to "
                          "reconnect.")


async def connection_lost(parent: Gtk.Widget) -> bool:
    """Blocking 'Connection lost' dialog: True when the user chose Reconnect. The heading and
    the body come from ``parent.connection_lost_texts()`` when it has it (the main window:
    'while cutting' only when a cut step had started; a link that is back already gets no
    Reconnect), else CONNECTION_LOST_HEADING and CONNECTION_LOST_BODY."""
    texts = getattr(parent, "connection_lost_texts", None)
    heading, body, reconnect = (texts() if callable(texts) else
                                (_(CONNECTION_LOST_HEADING), _(CONNECTION_LOST_BODY), True))
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("close", _("Close"))
    if reconnect:
        dialog.add_response("reconnect", _("Reconnect"))
        dialog.set_response_appearance("reconnect", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_close_response("close")
    return await _choose(dialog, parent) == "reconnect"


async def stop_failed(parent: Gtk.Widget) -> None:
    """Blocking 'The stop could not be sent' alert with the power-button instruction.

    Shown when closing during a job (or right after Cancel) could not stop the machine: the
    window stays open."""
    dialog = Adw.AlertDialog(heading=_("The stop could not be sent"),
                             body=_("Press the machine's power button now. The window stays open; "
                                    "close it again once the machine has stopped."))
    dialog.add_response("ok", _("OK"))
    dialog.set_close_response("ok")
    await _choose(dialog, parent)


async def stop_not_sent(parent: Gtk.Widget) -> None:
    """Blocking alert after Stop Machine: the service could not send the stop."""
    dialog = Adw.AlertDialog(heading=_("The stop could not be sent"),
                             body=_("OpenCutter could not send the stop to the machine. If it "
                                    "moves, press its power button now."))
    dialog.add_response("ok", _("OK"))
    dialog.set_close_response("ok")
    await _choose(dialog, parent)


async def still_busy_after_stop(parent: Gtk.Widget) -> None:
    """Blocking alert after Stop Machine: the stop went out, but the machine still reports
    Busy a few seconds later. What it reports does not prove that it moves, or that it stopped."""
    dialog = Adw.AlertDialog(heading=_("The machine still reports Busy"),
                             body=_("The stop was sent, but the machine still reports Busy. If it "
                                    "moves, press its power button now."))
    dialog.add_response("ok", _("OK"))
    dialog.set_close_response("ok")
    await _choose(dialog, parent)


async def still_paused_after_stop(parent: Gtk.Widget, body: str) -> None:
    """Blocking alert after Stop Machine: the stop went out, but the machine still reports
    Paused at the end of the service's read-back (a paused machine ends the job about 5 s after
    the stop). `body` is jobview.still_paused_text(): STILL_PAUSED with the seconds, already in
    the window's language."""
    dialog = Adw.AlertDialog(heading=_("The machine still reports Paused"), body=body)
    dialog.add_response("ok", _("OK"))
    dialog.set_close_response("ok")
    await _choose(dialog, parent)


def show_message(parent: Gtk.Widget, heading: str, body: str) -> None:
    """A simple informational dialog with an OK button."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("ok", _("OK"))
    dialog.present(parent)


def show_about(parent: Gtk.Widget) -> None:
    """The About dialog: GPL-3.0 and the not-affiliated notice."""
    about = Adw.AboutDialog(
        application_name="OpenCutter", application_icon=APP_ID, version=__version__,
        developer_name="OpenCutter contributors", license_type=Gtk.License.GPL_3_0,
        copyright=COPYRIGHT, website=WEBSITE, issue_url=f"{WEBSITE}/issues",
        comments=about_comments())
    about.present(parent)


class PreferencesDialog(Adw.PreferencesDialog):
    """General (units, defaults), Presets (source and count) and Machine."""

    __gtype_name__ = "OpenCutterPreferencesDialog"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, settings: GuiSettings, store: PresetStore, machine: MachineState):
        """Build the pages from the current settings, presets and machine. The mat and media
        rows are those of the layout's machine (``settings.profile``)."""
        super().__init__(title=_("Preferences"))
        self.settings = settings
        profile = settings.profile
        self.profile = profile
        page = Adw.PreferencesPage(title=_("Preferences"), icon_name="preferences-system-symbolic")

        general = Adw.PreferencesGroup(title=_("General"))
        units = Adw.ToggleGroup(valign=Gtk.Align.CENTER)
        units.add(Adw.Toggle(name="in", label=pgettext("unit", "in")))
        units.add(Adw.Toggle(name="mm", label=pgettext("unit", "mm")))
        units.set_active_name(settings.units)
        units.connect("notify::active-name", self._on_units)
        units_row = Adw.ActionRow(title=_("Units"), subtitle=_("For positions and sizes"))
        units_row.add_suffix(units)
        general.add(units_row)
        general.add(self._mat_row(profile))
        sizes = list(media_sizes(settings.mat_for(profile), profile))
        if settings.media not in [m.key for m in sizes]:
            other = media_size(settings.media)   # chosen for another machine's mat: still the
            if other is not None:                # one in use, as Page Setup lists it
                sizes.append(other)
        media = Adw.ComboRow(title=_("Default Media Size"))
        media.set_model(Gtk.StringList.new(
            [f"{media_label(m.label)} · {format_size(m.width_mm, m.height_mm, settings.units)}"
             for m in sizes]))
        keys = [m.key for m in sizes]
        media.set_selected(keys.index(settings.media) if settings.media in keys else 0)
        media.connect("notify::selected", lambda row, _p: self._set("media", keys[row.get_selected()]))
        self.media_row = media
        general.add(media)
        preview = Adw.SwitchRow(title=_("Preview outline before cutting"),
                                subtitle=_("Trace the outer box with the tool up first"),
                                active=settings.preview_outline)
        preview.connect("notify::active", lambda row, _p: self._set("preview_outline", row.get_active()))
        general.add(preview)
        general.add(self._language_row())
        page.add(general)

        presets = Adw.PreferencesGroup(title=_("Presets"))
        presets.add(Adw.ActionRow(title=_("Source"), subtitle=store_description(store)))
        presets.add(Adw.ActionRow(title=_("Materials"), subtitle=str(len(store.materials()))))
        page.add(presets)

        machine_group = Adw.PreferencesGroup(title=_("Machine"))
        # plain text: the names come from the machine
        device = Adw.ActionRow(title=machine.device_name or machine.name, use_markup=False,
                               subtitle=_("Connected") if machine.connected else _("Pair it in Settings → Bluetooth"))
        device.add_prefix(Gtk.Image(icon_name="bluetooth-symbolic"))
        machine_group.add(device)
        self.support_row = Adw.ActionRow(title=_("Model"), use_markup=False,
                                         subtitle=model_text(machine))
        machine_group.add(self.support_row)
        page.add(machine_group)
        self.add(page)

    def _mat_row(self, profile) -> Adw.ActionRow:
        """'Default Cutting Mat' of the layout's machine: its only mat, or a choice of the mats
        it may use (the others are listed as not measured on this machine)."""
        choices = mat_choices(profile)
        current = self.settings.mat_for(profile)
        if len(choices) == 1:
            return Adw.ActionRow(title=_("Default Cutting Mat"), subtitle=mat_label(current, profile))
        offered = [c for c in choices if c.sensitive]
        row = Adw.ComboRow(title=_("Default Cutting Mat"),
                           subtitle=_("For the {machine}").format(machine=profile.name))
        row.set_model(Gtk.StringList.new([c.label for c in offered]))
        keys = [c.key for c in offered]
        row.set_selected(keys.index(current) if current in keys else 0)
        row.connect("notify::selected", lambda r, _p: self._set_mat(keys[r.get_selected()]))
        self.mat_row = row
        return row

    def _language_row(self) -> Adw.ComboRow:
        """'Language': System default, English and every language with a catalog, by its own
        name. The choice is saved at once and used from the next start."""
        codes = ["", SOURCE_LANGUAGE] + [c for c in available_languages() if c != SOURCE_LANGUAGE]
        chosen = self.settings.language
        if chosen and chosen not in codes:
            codes.append(chosen)                 # chosen before its catalog went away
        names = [_("System default")] + [language_name(c) for c in codes[1:]]
        row = Adw.ComboRow(title=_("Language"))
        row.set_model(Gtk.StringList.new(names))
        row.set_selected(codes.index(chosen) if chosen in codes else 0)
        self.language_codes = codes
        self.language_row = row
        # the choice this run started with (app.setup_language), not the one saved since: a
        # dialog opened again after a change still says to restart
        if i18n.started_choice is None:         # no setup_language in this process
            i18n.started_choice = chosen
        self._started_choice = i18n.started_choice
        if chosen != self._started_choice:
            row.set_subtitle(_(RESTART_FOR_LANGUAGE))

        def changed(r, _p):
            code = codes[r.get_selected()]
            self._set_language(code)
        row.connect("notify::selected", changed)
        return row

    def _set_language(self, code: str) -> None:
        """Save the window's language; the row says to restart when it differs from the one
        this run started with."""
        self.settings.language = code
        self.settings.save()
        self.language_row.set_subtitle(_(RESTART_FOR_LANGUAGE)
                                       if code != self._started_choice else "")

    def _set_mat(self, key: str) -> None:
        self.settings.set_mat(key, self.profile)
        self.settings.save()
        self.emit("changed")

    def _on_units(self, toggles: Adw.ToggleGroup, _pspec) -> None:
        self._set("units", toggles.get_active_name())

    def _set(self, name: str, value) -> None:
        setattr(self.settings, name, value)
        self.settings.save()
        self.emit("changed")
