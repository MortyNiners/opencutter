# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The sidebar: Page Setup and Send (Material, Action and Tool) panels, Studio style.

Machine profiles: both panels are for the layout's machine (``profile``, a MachineProfile).
  Page Setup  The Machine row shows the connected machine with "(Detected)"; with no machine
              connected it is a choice of every machine OpenCutter drives, by its plain name
              (machine.machine_choices), which emits ``machine-chosen``. Cutting Mat lists the
              machine's mats (geometry.mat_choices): a mat that may not be used at the
              machine's level is listed but cannot be chosen ("Not measured on this machine
              yet"), and one whose cut area cannot be checked on this model says so under the
              list. Media Size offers Letter, A4 and the mat's own size, and the media is
              clamped to the mat.
  Send        One Tool card per holder of the machine, with its label ('Tool 1', or 'Tool' on a
              one-holder model). The Cut Settings sliders take the machine's ranges; the
              Acceleration row is hidden on a machine without one, Blade Depth shows only for a
              tool that takes it. A machine without materials shows "No materials for this
              machine yet" with Use Cut Settings: the user then picks the tool and sets the
              values in Cut Settings, which start light (sending.starting_settings).

Translations: the panels keep the English keys (materials, actions, tools, holders: the presets'
and the profiles' words) and show them in the window's language (i18n's labels); the reasons
Send waits come in English and are translated where they are shown.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from gi.repository import Adw, GLib, GObject, Gtk, Pango

from opencutter.device.protocol import tool_name
from opencutter.i18n import (MANUAL_MATERIAL, N_, _, action_label, holder_label, material_label,
                             pgettext, tool_label, translate_text)
from opencutter.job.plan import blade_depth_mm
from opencutter.job.spec import ToolSettings
from opencutter.machines import MachineProfile, Mat
from opencutter.presets.model import Preset, PresetStore

from .geometry import (CUSTOM_MEDIA, MatChoice, Rect, format_size, mat_choices, media_rect,
                       media_size, media_sizes, profile_mat)
from .geometry import media_label as media_name
from .machine import MachineState, machine_choices
from .sending import (NO_MATERIALS, NO_MATERIALS_HINT, ToolCheck, choose_preset, depth_note,
                      materials_note, preset_line, settings_summary, slider_ranges,
                      starting_settings, store_description, tool_cards, tool_check,
                      verified_preset)
from .settings import GuiSettings
from .materials import USE_CUT_SETTINGS, MaterialPicker
from .widgets import InlineNote, LengthEntry, StatusDot, field, icon_label, label, relate

DEFAULT_OVERCUT = (1, 1)
CHOOSE_MATERIAL_PLACEHOLDER = N_("Choose a material")
# Studio forces FE0 and FF0,0 for tools without a blade (pens, foil): these edits exist
# only for blade tools (PROTOCOL.md section 5)
BLADE_ONLY_EDITS = frozenset({"line_segment_overcut", "overcut"})
DETECTED = N_("(Detected)")
NOT_DETECTED = N_("Not detected")         # a machine the service refused: it never will be
NOT_DETECTED_YET = N_("Not detected yet")
# what the Material field says once the user chose to set everything in Cut Settings (a
# machine without materials)
# MANUAL_MATERIAL (opencutter.i18n, "Cut Settings"): the material of the manual mode
MANUAL_SOURCE = N_("Your own settings")
MANUAL_TOOL = N_("The tool in the chosen holder")
# the Cut Settings rows: (key, name)
SLIDERS = (("blade_depth", N_("Blade Depth")), ("force", N_("Force")), ("speed", N_("Speed")),
           ("acceleration", N_("Acceleration")))


def _factory(describe: Callable[[str], tuple[bool, str | None]] | None = None,
             button: bool = False) -> Gtk.SignalListItemFactory:
    """A DropDown factory: each item's string in a label (ellipsized in the button). In the
    list, a check mark shows the chosen item, and ``describe(string)`` gives (sensitive,
    tooltip): an item that is not sensitive is shown dimmed and cannot be chosen."""
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, item) -> None:
        box = Gtk.Box(spacing=8)
        text = Gtk.Label(xalign=0.0, hexpand=True)
        if button:
            text.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(text)
        if not button:
            check = Gtk.Image(icon_name="object-select-symbolic")
            box.append(check)
            item.connect("notify::selected",
                         lambda it, _p: check.set_opacity(1.0 if it.get_selected() else 0.0))
        item.set_child(box)

    def bind(_factory, item) -> None:
        box = item.get_child()
        string = item.get_item().get_string()
        box.get_first_child().set_text(string)
        sensitive, tooltip = describe(string) if describe else (True, None)
        box.set_sensitive(sensitive)
        box.set_tooltip_text(tooltip)
        if not button:
            item.set_selectable(sensitive)
            item.set_activatable(sensitive)
            box.get_last_child().set_opacity(1.0 if item.get_selected() else 0.0)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


# --- Page Setup ------------------------------------------------------------------------
def _named_size(name: str, w_mm: float, h_mm: float, units: str) -> str:
    """'Letter · 8.5 × 11 in', or the size alone when the name already says it ('12 × 12 in')."""
    dims = format_size(w_mm, h_mm, units)
    return name if name == dims else f"{name} · {dims}"


class PageSetupPanel(Gtk.Box):
    """Machine, Cutting Mat, Media Size, Orientation and Reveal Cutting Mat."""

    __gtype_name__ = "OpenCutterPageSetupPanel"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
                    # the user chose the layout's machine (a profile id; no machine connected)
                    "machine-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, settings: GuiSettings, profile: MachineProfile | None = None):
        """Build the panel from the saved settings, for the layout's machine ``profile``
        (default: the settings' machine)."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.add_css_class("sidebar-panel")
        self.settings = settings
        self.profile = profile or settings.profile
        self.machine = MachineState(choice=self.profile.id)
        self._updating = False

        # the machine: the detected one, or a choice while none is connected
        self._choices = machine_choices()
        machine = Gtk.Box(spacing=8)
        self._machine_list = Gtk.StringList.new([c.name for c in self._choices])
        self._machine = Gtk.DropDown(model=self._machine_list, hexpand=True)
        self._machine.set_factory(_factory(button=True))
        self._machine.set_list_factory(_factory())
        self._machine.add_css_class("app-field")
        self._machine.update_property([Gtk.AccessibleProperty.LABEL], [_("Machine")])
        self._machine_name = label(self.profile.name)
        self._machine_name.set_hexpand(True)
        self._machine_name.set_ellipsize(Pango.EllipsizeMode.END)
        machine.append(self._machine)
        machine.append(self._machine_name)
        self._detected = label(_(NOT_DETECTED_YET), "dim-label", "caption")
        self._detected.set_xalign(1.0)
        machine.append(self._detected)
        self.append(field(_("Machine"), machine, self._machine))

        # the machine's mats
        self._mat_choices: tuple[MatChoice, ...] = ()
        self._mat_key = settings.mat_for(self.profile)
        self._mat_list = Gtk.StringList()
        self._mat = Gtk.DropDown(model=self._mat_list)
        self._mat.set_factory(_factory(self._describe_mat, button=True))
        self._mat.set_list_factory(_factory(self._describe_mat))
        self._mat.add_css_class("app-field")
        self._mat_note = label("", "caption", "dim-label", wrap=True)
        self._mat_note.set_visible(False)
        mat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        mat_box.append(self._mat)
        mat_box.append(self._mat_note)
        self.append(field(_("Cutting Mat"), mat_box, self._mat))

        self._media_keys: list[str] = []
        self._media_list = Gtk.StringList()
        self._media = Gtk.DropDown(model=self._media_list)
        self._media.add_css_class("app-field")
        custom = Gtk.Box(spacing=8)
        self._custom_w = LengthEntry(_("Custom width"), settings.units, positive=True)
        self._custom_h = LengthEntry(_("Custom height"), settings.units, positive=True)
        self._custom_unit = label(pgettext("unit", settings.units), "dim-label")
        # Translators: W and H are the short forms of Width and Height.
        for widget in (label(pgettext("width", "W"), "dim-label"), self._custom_w,
                       label("×", "dim-label"), label(pgettext("height", "H"), "dim-label"),
                       self._custom_h, self._custom_unit):
            custom.append(widget)
        self._custom = Gtk.Revealer(child=custom)
        media_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        media_box.append(self._media)
        media_box.append(self._custom)
        self.append(field(_("Media Size"), media_box, self._media))

        self._orientation = Adw.ToggleGroup()
        # Translators: the page orientation (not the Portrait machines).
        self._orientation.add(Adw.Toggle(name="portrait", label=pgettext("orientation",
                                                                         "Portrait")))
        self._orientation.add(Adw.Toggle(name="landscape", label=pgettext("orientation",
                                                                          "Landscape")))
        self._orientation.add_css_class("segmented")
        self.append(field(_("Orientation"), self._orientation))

        self.append(Gtk.Separator())
        reveal = Gtk.Box(spacing=12)
        reveal_label = label(_("Reveal Cutting Mat"))
        reveal_label.set_hexpand(True)
        self.reveal_switch = Gtk.Switch(valign=Gtk.Align.CENTER, active=settings.reveal_mat)
        relate(self.reveal_switch, Gtk.AccessibleRelation.LABELLED_BY, reveal_label)
        reveal.append(reveal_label)
        reveal.append(self.reveal_switch)
        self.append(reveal)

        self._show_machine()
        self._fill_mats()
        self._fill_media_list()
        self._orientation.set_active_name("landscape" if settings.landscape else "portrait")
        self._custom_w.set_mm(settings.custom_media_mm[0])
        self._custom_h.set_mm(settings.custom_media_mm[1])
        self._custom.set_reveal_child(self.media_key == CUSTOM_MEDIA)

        self._machine.connect("notify::selected", self._on_machine_chosen)
        self._mat.connect("notify::selected", self._on_mat)
        self._media.connect("notify::selected", self._on_media)
        self._orientation.connect("notify::active-name", self._on_orientation)
        self._custom_w.connect("committed", self._on_custom)
        self._custom_h.connect("committed", self._on_custom)
        self.reveal_switch.connect("notify::active", self._on_reveal)

    # --- reading ---------------------------------------------------------------------
    @property
    def media_key(self) -> str:
        """The selected media size key ('letter', 'a4', the mat's size such as '12x12', or
        'custom')."""
        index = self._media.get_selected()
        return self._media_keys[index] if index < len(self._media_keys) else self._media_keys[0]

    @property
    def mat_key(self) -> str:
        """The Cutting Mat of the layout (a Mat.key of the machine)."""
        return self._mat_key

    @property
    def mat(self) -> Mat:
        """The Cutting Mat of the layout."""
        return profile_mat(self._mat_key, self.profile)

    @property
    def mat_choices(self) -> tuple[MatChoice, ...]:
        """The Cutting Mat list, in order (sensitive or not, with its note)."""
        return self._mat_choices

    @property
    def machine_choice_ids(self) -> list[str]:
        """The ids the Machine list offers while no machine is connected, in order."""
        return [c.id for c in self._choices]

    def media_rect(self) -> Rect:
        """The media rectangle on the mat, in document mm."""
        return media_rect(self.media_key, self.settings.landscape,
                          tuple(self.settings.custom_media_mm), self._mat_key, self.profile)

    def media_label(self) -> str:
        """Caption drawn on the media, e.g. 'Letter · 8.5 × 11 in', or '12 × 12 in' alone when
        the name already is the size."""
        size = media_size(self.media_key, self._mat_key, self.profile)
        _x, _y, w, h = self.media_rect()
        return _named_size(media_name(size.label) if size else _("Custom"), w, h,
                           self.settings.units)

    # --- changing --------------------------------------------------------------------
    def commit_pending(self) -> None:
        """Commit a custom width or height that was typed but not confirmed with Enter yet."""
        for entry in (self._custom_w, self._custom_h):
            entry.commit()

    def select_media(self, key: str) -> None:
        """Select a media size by key (e.g. after Preferences changed the default)."""
        if key in self._media_keys:
            self._media.set_selected(self._media_keys.index(key))
        elif media_size(key) is not None:          # another machine's size: listed, clamped
            self.settings.media = key
            self._fill_media_list()
            self._changed()

    def select_mat(self, key: str) -> None:
        """Select the Cutting Mat by key (e.g. after Preferences changed the default); a mat
        that cannot be chosen is not."""
        keys = [c.key for c in self._mat_choices]
        if key in keys and self._mat_choices[keys.index(key)].sensitive:
            self._mat.set_selected(keys.index(key))

    def set_profile(self, profile: MachineProfile) -> None:
        """Make the panel the layout's for ``profile``: its name, its mats (the one chosen for
        it before, else its default) and the media sizes of that mat. Emits nothing: the
        window re-reads the media itself."""
        self.profile = profile
        self._mat_key = self.settings.mat_for(profile)
        self.machine = replace(self.machine, choice=profile.id)
        self._show_machine()
        self._fill_mats()
        self._fill_media_list()
        self._custom.set_reveal_child(self.media_key == CUSTOM_MEDIA)

    def set_machine(self, state: MachineState) -> None:
        """Show whether the machine was detected. A machine the service refused for its model
        is never detected: no "yet"."""
        self.machine = state
        self._show_machine()
        self._fill_mats()

    def set_units(self, units: str) -> None:
        """Switch the custom size fields and media captions to 'in' or 'mm'."""
        for entry in (self._custom_w, self._custom_h):
            entry.units = units
        self._custom_w.set_mm(self.settings.custom_media_mm[0])
        self._custom_h.set_mm(self.settings.custom_media_mm[1])
        self._custom_unit.set_text(pgettext("unit", units))
        self._fill_media_list()

    # --- internals -------------------------------------------------------------------
    def _show_machine(self) -> None:
        state = self.machine
        connected = state.connected
        self._machine.set_visible(not connected)
        self._machine_name.set_visible(connected)
        # connected: the profile the service detected; when it did not say which, what the
        # machine reports itself as (never the layout's machine, which was not detected)
        detected = state.detected
        shown = detected.name if detected is not None else (state.model or state.name)
        self._machine_name.set_text(shown if connected else self.profile.name)
        ids = self.machine_choice_ids
        self._updating = True
        self._machine.set_selected(ids.index(self.profile.id) if self.profile.id in ids
                                   else Gtk.INVALID_LIST_POSITION)
        self._updating = False
        self._detected.set_text(_(DETECTED if connected else
                                  NOT_DETECTED if state.wrong_model else NOT_DETECTED_YET))

    def _support(self) -> str | None:
        """The detected machine's level when it is the layout's machine, else None (the
        profile's own level)."""
        detected = self.machine.detected
        return self.machine.support if detected is not None and detected.id == self.profile.id else None

    def _describe_mat(self, text: str) -> tuple[bool, str | None]:
        choice = next((c for c in self._mat_choices if c.label == text), None)
        if choice is None:
            return True, None
        return choice.sensitive, _(choice.note) if choice.note else None

    def _fill_mats(self) -> None:
        choices = mat_choices(self.profile, self._support())
        keys = [c.key for c in choices]
        if self._mat_key not in keys:
            self._mat_key = self.settings.mat_for(self.profile)
        if choices != self._mat_choices:
            self._mat_choices = choices
            self._updating = True
            self._mat_list.splice(0, self._mat_list.get_n_items(), [c.label for c in choices])
            self._updating = False
        self._updating = True
        self._mat.set_selected(keys.index(self._mat_key))
        self._updating = False
        note = self._mat_choices[keys.index(self._mat_key)].note
        self._mat_note.set_text(_(note) if note else "")
        self._mat_note.set_visible(bool(note))

    def _fill_media_list(self) -> None:
        units = self.settings.units
        sizes = list(media_sizes(self._mat_key, self.profile))
        current = self.settings.media
        if current not in [m.key for m in sizes] and current != CUSTOM_MEDIA:
            other = media_size(current)          # a size chosen for another mat: still listed
            if other is not None:
                sizes.append(other)
        self._media_keys = [m.key for m in sizes] + [CUSTOM_MEDIA]
        names = [_named_size(media_name(m.label), m.width_mm, m.height_mm, units)
                 for m in sizes] + [_("Custom…")]
        self._updating = True
        self._media_list.splice(0, self._media_list.get_n_items(), names)
        self._media.set_selected(self._media_keys.index(current) if current in self._media_keys
                                 else 0)
        self._updating = False

    def _on_machine_chosen(self, *_args) -> None:
        if self._updating:
            return
        index = self._machine.get_selected()
        if index < len(self._choices) and self._choices[index].id != self.profile.id:
            self.emit("machine-chosen", self._choices[index].id)

    def _on_mat(self, *_args) -> None:
        if self._updating:
            return
        index = self._mat.get_selected()
        if index >= len(self._mat_choices):
            return
        choice = self._mat_choices[index]
        if not choice.sensitive:                 # listed, but never chosen
            self._fill_mats()
            return
        self._mat_key = choice.key
        self.settings.set_mat(choice.key, self.profile)
        self._fill_mats()
        self._fill_media_list()
        self._custom.set_reveal_child(self.media_key == CUSTOM_MEDIA)
        self._changed()

    def _on_media(self, *_args) -> None:
        if self._updating:
            return
        self.settings.media = self.media_key
        self._custom.set_reveal_child(self.media_key == CUSTOM_MEDIA)
        self._changed()

    def _on_orientation(self, *_args) -> None:
        self.settings.landscape = self._orientation.get_active_name() == "landscape"
        self._changed()

    def _on_custom(self, *_args) -> None:
        self.settings.custom_media_mm = [self._custom_w.mm, self._custom_h.mm]
        self._changed()

    def _on_reveal(self, *_args) -> None:
        self.settings.reveal_mat = self.reveal_switch.get_active()
        self._changed()

    def _changed(self) -> None:
        self.settings.save()
        self.emit("changed")


# --- Tool cards and setting rows -------------------------------------------------------
class ToolCard(Gtk.ToggleButton):
    """One holder: 'Tool 1 · AutoBlade' with what was detected; selectable like a radio."""

    __gtype_name__ = "OpenCutterToolCard"

    def __init__(self, holder: int, title: str | None = None):
        """Create the card for a holder (its number) titled with its label ('Tool 1', or
        'Tool' on a one-holder model)."""
        super().__init__()
        self.holder = holder
        self.title = title or f"Tool {holder}"
        self.add_css_class("tool-card")
        grid = Gtk.Grid(column_spacing=12)
        self._title = label(holder_label(self.title), "heading")
        self._desc = label("", "caption", "dim-label")
        self._icon = Gtk.Image()
        self._title.set_hexpand(True)
        grid.attach(self._title, 0, 0, 1, 1)
        grid.attach(self._desc, 0, 1, 1, 1)
        grid.attach(self._icon, 1, 0, 1, 2)
        self.set_child(grid)

    def show_state(self, tool: str | None, description: str, icon: str | None, warn: bool) -> None:
        """Set the title tool name, the description line and the status icon."""
        title = holder_label(self.title) + (f" · {tool}" if tool else "")
        self._title.set_text(title)
        self._desc.set_text(description)
        self._icon.set_from_icon_name(icon)
        self._icon.set_visible(icon is not None)
        if warn:
            self.add_css_class("warning")
        else:
            self.remove_css_class("warning")
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{title}. {description}"])


class _SliderRow:
    """Name (with a changed dot), slider and value, on one grid row."""

    def __init__(self, grid: Gtk.Grid, row: int, name: str, low: int, high: int, on_change):
        self.name_box = Gtk.Box(spacing=6)
        self.name_box.append(label(name, "setting-name"))
        self.dot = StatusDot("accent")
        self.dot.set_tooltip_text(_("Changed"))
        self.name_box.append(self.dot)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, 1)
        self.scale.set_draw_value(False)
        self.scale.set_round_digits(0)
        self.scale.set_hexpand(True)
        self.scale.update_property([Gtk.AccessibleProperty.LABEL], [name])
        self.value = label("", "setting-value", xalign=1.0)
        self.value.set_width_chars(3)
        self.scale.connect("value-changed", lambda s: (self.value.set_text(str(int(s.get_value()))),
                                                       on_change(int(s.get_value()))))
        self.widgets = (self.name_box, self.scale, self.value)
        for col, widget in enumerate(self.widgets):
            grid.attach(widget, col, row, 1, 1)

    @property
    def range(self) -> tuple[int, int]:
        """(lowest, highest) the slider allows."""
        adjustment = self.scale.get_adjustment()
        return int(adjustment.get_lower()), int(adjustment.get_upper())

    def set_range(self, low: int, high: int) -> None:
        """The machine's range for this setting."""
        if self.range != (low, high):
            self.scale.set_range(low, high)

    def show(self, value: int, changed: bool, visible: bool = True) -> None:
        self.scale.set_value(value)
        self.value.set_text(str(value))
        self.dot.set_visible(changed)
        for widget in self.widgets:
            widget.set_visible(visible)


# --- Send ------------------------------------------------------------------------------
class SendPanel(Gtk.Box):
    """Material, Action and Tool, Cut Settings, Test Cut and the big Send button."""

    __gtype_name__ = "OpenCutterSendPanel"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "send": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "test-cut": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, store: PresetStore, settings: GuiSettings,
                 profile: MachineProfile | None = None):
        """Build the panel for the layout's machine ``profile`` (default: the settings'
        machine) from ``store``, that machine's presets; the material comes from the settings
        or a verified preset (else none)."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.store, self.settings = store, settings
        self.profile = profile or settings.profile
        self.machine = MachineState(choice=self.profile.id)
        self.material: str | None = None
        self.action: str | None = None
        self.holder = self.profile.holders[0].number
        self.manual = False                  # the user sets everything in Cut Settings
        self._manual_tools: dict[int, int] = {}     # holder -> the tool chosen there (manual)
        self._manual_codes: list[int] = []
        self._manual_shown: list[tuple[int, str]] | None = None   # (code, name) in the list
        self._edits: dict = {}
        self._updating = False

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        body.add_css_class("sidebar-panel")
        body.append(self._build_material())
        self._actions = Gtk.StringList()
        self._action = Gtk.DropDown(model=self._actions)
        self._action.add_css_class("app-field")
        self._action.connect("notify::selected", self._on_action)
        self._action_keys: list[str] = []      # the actions the list shows, in English
        body.append(field(_("Action"), self._action))
        body.append(self._build_tools())
        body.append(self._build_settings())
        # no focus on click: after a Test Cut a stray Return or Space must not start another
        self.test_cut = Gtk.Button(label=_("Test Cut"), halign=Gtk.Align.START, focus_on_click=False)
        self.test_cut.add_css_class("pill-sm")
        self.test_cut.connect("clicked", lambda *_: self.emit("test-cut"))
        body.append(self.test_cut)
        scroller = Gtk.ScrolledWindow(child=body, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(scroller)
        self.append(self._build_footer())
        self.set_store(store)

    # --- building --------------------------------------------------------------------
    def _build_material(self) -> Gtk.Widget:
        self._picker = MaterialPicker()
        self._picker.connect("picked", lambda _p, m: self._on_picked(m))
        self._picker.connect("use-cut-settings", lambda *_: self.use_cut_settings())
        content = Gtk.Box(spacing=8)
        content.append(Gtk.Image.new_from_icon_name("system-search-symbolic"))
        self._material_label = label("")
        self._material_label.set_hexpand(True)
        self._material_label.set_ellipsize(Pango.EllipsizeMode.END)
        content.append(self._material_label)
        self._material_dot = StatusDot("accent")
        self._material_dot.set_tooltip_text(_("Modified"))
        content.append(self._material_dot)
        content.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
        self._material_button = Gtk.MenuButton(child=content, popover=self._picker)
        self._material_button.add_css_class("app-field")
        self._material_button.connect("notify::active", self._on_picker_open)
        source = Gtk.Box(spacing=8)
        self._source = label("", "caption", "dim-label")
        self._source.set_hexpand(True)
        self._source.set_ellipsize(Pango.EllipsizeMode.END)
        source.append(self._source)
        self._reset = Gtk.Button(label=_("Reset to preset"))
        self._reset.add_css_class("link-button")
        self._reset.add_css_class("caption")
        self._reset.connect("clicked", lambda *_: self.reset_to_preset())
        source.append(self._reset)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self._material_button)
        box.append(source)
        # a machine without materials: say so and point to Cut Settings
        self._empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._empty_note = InlineNote(_(NO_MATERIALS_HINT))
        self._empty.append(self._empty_note)
        self._use_cut_settings = Gtk.Button(label=_(USE_CUT_SETTINGS), halign=Gtk.Align.START)
        self._use_cut_settings.add_css_class("pill-sm")
        self._use_cut_settings.connect("clicked", lambda *_: self.use_cut_settings())
        self._empty.append(self._use_cut_settings)
        self._empty.set_visible(False)
        box.append(self._empty)
        return field(_("Material"), box, self._material_button)

    def _build_tools(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._mismatch = Gtk.Box(spacing=12)
        self._mismatch.add_css_class("inline-alert")
        self._mismatch.add_css_class("warning")
        self._mismatch.append(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._mismatch_title = label("", wrap=True, markup=True)
        self._mismatch_detail = label("", "caption", wrap=True)
        texts.append(self._mismatch_title)
        texts.append(self._mismatch_detail)
        self._mismatch.append(texts)
        self._mismatch.update_property([Gtk.AccessibleProperty.LABEL], [_("Tool mismatch")])
        box.append(self._mismatch)
        self._cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self._cards_box)
        self._cards: dict[int, ToolCard] = {}
        self._build_cards()
        # with the settings from Cut Settings: which tool is in the chosen holder
        self._manual_tool_list = Gtk.StringList()
        self._manual_tool = Gtk.DropDown(model=self._manual_tool_list, visible=False,
                                         tooltip_text=_(MANUAL_TOOL))
        self._manual_tool.add_css_class("app-field")
        self._manual_tool.update_property([Gtk.AccessibleProperty.LABEL], [_(MANUAL_TOOL)])
        self._manual_tool.connect("notify::selected", self._on_manual_tool)
        box.append(self._manual_tool)
        return field(_("Tool"), box, self._cards_box)

    def _build_cards(self) -> None:
        """One Tool card per holder of the machine."""
        while (child := self._cards_box.get_first_child()) is not None:
            self._cards_box.remove(child)
        self._cards = {}
        first = None
        for holder in tool_cards(self.profile):
            card = ToolCard(holder.holder, holder.label)
            if first is None:
                first = card
            else:
                card.set_group(first)
            card.connect("toggled", self._on_card)
            self._cards_box.append(card)
            self._cards[holder.holder] = card

    def _build_settings(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("cut-settings")
        header = Gtk.Box(spacing=8)
        self._chevron = Gtk.Image.new_from_icon_name("pan-end-symbolic")
        header.append(self._chevron)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        titles.append(label(_("Cut Settings"), "heading"))
        self._summary = label("", "caption", "dim-label", "numeric")
        # two lines when one is too short (a longer translation): the passes stay in sight
        self._summary.set_ellipsize(Pango.EllipsizeMode.END)
        self._summary.set_wrap(True)
        self._summary.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._summary.set_natural_wrap_mode(Gtk.NaturalWrapMode.NONE)
        self._summary.set_lines(2)
        titles.append(self._summary)
        header.append(titles)
        self._edit_label = label(_("Edit"), "accent")
        header.append(self._edit_label)
        self._settings_toggle = Gtk.ToggleButton(child=header)
        self._settings_toggle.add_css_class("flat")
        self._settings_toggle.add_css_class("expander-header")
        self._settings_toggle.connect("toggled", self._on_settings_toggle)
        outer.append(self._settings_toggle)

        grid = Gtk.Grid(column_spacing=12, row_spacing=14)
        grid.add_css_class("settings-grid")
        ranges = slider_ranges(self.profile)
        self._sliders = {}
        for row, (key, name) in enumerate(SLIDERS):
            low, high = ranges[key] or (0, 10)
            self._sliders[key] = _SliderRow(grid, row, _(name), low, high,
                                            lambda v, key=key: self._edit(key, v))
        self._passes = Gtk.SpinButton.new_with_range(*ranges["passes"], 1)
        self._passes.set_halign(Gtk.Align.START)
        self._passes.connect("value-changed", lambda s: self._edit("passes", int(s.get_value())))
        self._passes_dot = StatusDot("accent")
        self._lso = Gtk.Switch(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self._lso.connect("notify::active", lambda s, _p: self._edit("line_segment_overcut", s.get_active()))
        self._overcut = Gtk.Switch(halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self._overcut.connect("notify::active", lambda s, _p: self._on_overcut(s.get_active()))
        self._blade_rows: list[Gtk.Widget] = []      # shown only for blade tools
        for row, (name, widget) in enumerate(((N_("Passes"), self._passes),
                                              (N_("Line Segment Overcut"), self._lso),
                                              (N_("Overcut"), self._overcut)), start=4):
            name_label = label(_(name), "setting-name")
            widget.update_property([Gtk.AccessibleProperty.LABEL], [_(name)])
            if widget is self._passes:
                name_box = Gtk.Box(spacing=6)
                name_box.append(name_label)
                name_box.append(self._passes_dot)
                grid.attach(name_box, 0, row, 1, 1)
                grid.attach(widget, 1, row, 2, 1)
            else:
                grid.attach(name_label, 0, row, 2, 1)
                grid.attach(widget, 2, row, 1, 1)
                self._blade_rows += [name_label, widget]
        self._settings_revealer = Gtk.Revealer(child=grid)
        outer.append(self._settings_revealer)
        return outer

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        footer.add_css_class("send-footer")
        self.send_button = Gtk.Button(child=icon_label("media-playback-start-symbolic", _("Send")))
        self.send_button.add_css_class("suggested-action")
        self.send_button.add_css_class("send-big")
        self.send_button.update_property([Gtk.AccessibleProperty.LABEL], [_("Send")])
        self.send_button.connect("clicked", lambda *_: self.emit("send"))
        footer.append(self.send_button)
        self._why = label("", "caption", "why-disabled", wrap=True, xalign=0.5)
        footer.append(self._why)
        self.preview = Gtk.CheckButton(label=_("Preview outline before cutting"),
                                       active=self.settings.preview_outline)
        self.preview.add_css_class("caption")
        self.preview.connect("toggled", self._on_preview)
        footer.append(self.preview)
        relate(self.send_button, Gtk.AccessibleRelation.DESCRIBED_BY, self._why)
        return footer

    # --- public ----------------------------------------------------------------------
    def set_profile(self, profile: MachineProfile, store: PresetStore) -> None:
        """Make the panel the layout's for ``profile`` with ``store``, that machine's presets:
        its Tool cards, its ranges and its materials (set_store). Settings from Cut Settings
        are dropped: they were for the other machine."""
        self.profile = profile
        self.manual = False
        self._manual_tools.clear()
        self._manual_shown = None                # the tool list is built anew for it
        self._edits.clear()
        self._picker.popdown()                   # its list was the other machine's
        self._build_cards()
        if profile.holder(self.holder) is None:
            self.holder = profile.holders[0].number
        self.set_store(store)

    def set_store(self, store: PresetStore) -> None:
        """Use a (new) preset store: keep the material the user picked (the remembered one) if
        the store has it, else start on a preset verified on a real machine, else choose
        nothing. A preset nobody verified is never chosen for the user: with no material, Send
        and Test Cut stay blocked until one is picked. So a material the app chose itself (the
        verified preset) is not kept for another machine where its row is not verified (the
        CAMEO 4's Basic set on a CAMEO 4 Plus). An empty store keeps settings the user took
        from Cut Settings."""
        self.store = store
        if self.manual and not store.presets:
            self._refresh()
            return
        if self.manual:
            self.manual = False
            self.material = self.action = None
        materials = store.materials()
        picked = self.settings.material          # what the user picked last (select_material)
        wanted = picked if picked and picked in materials else None
        if wanted is None:
            verified = verified_preset(store)
            if verified is not None:            # its own action and holder, not the material's first
                wanted, self.action, self.holder = verified.material, verified.action, verified.holder
        self.select_material(wanted, remember=False)

    def select_material(self, material: str | None, remember: bool = True) -> None:
        """Choose a material; its actions fill the Action list (``sending.choose_preset``)."""
        self.manual = False
        self.material = material
        if material and remember:
            self.settings.remember_material(material)
            self.settings.save()
        presets = self.store.presets_for(material) if material else []
        actions = list(dict.fromkeys(p.action for p in presets))
        chosen = choose_preset(self.store, material, self.action, self.holder)
        self._fill_actions(actions, chosen.action if chosen else None)
        self._set_action(chosen.action if chosen else None, chosen.holder if chosen else None)

    def use_cut_settings(self) -> None:
        """Use Cut Settings (a machine without materials only): the user picks the tool and
        sets its values in Cut Settings, which open, starting light (the lowest Force, Speed 5:
        sending.starting_settings). Taking them counts as choosing the material."""
        if self.store.presets:
            return
        if self.manual:                          # already: keep the user's values
            self._settings_toggle.set_active(True)
            return
        self.manual = True
        self.material = MANUAL_MATERIAL
        if self.profile.holder(self.holder) is None:
            self.holder = self.profile.holders[0].number
        self._manual_mode_action()
        self._edits.clear()
        self._refresh()
        self._settings_toggle.set_active(True)
        self.emit("changed")

    def set_machine(self, state: MachineState) -> None:
        """Show the detected tools and re-check the required one."""
        self.machine = state
        self._refresh()

    def set_blockers(self, send: str | None, test_cut: str | None) -> None:
        """Enable or disable Send and Test Cut, showing why Send is disabled."""
        self.send_button.set_sensitive(send is None)
        self.send_button.set_tooltip_text(translate_text(send))
        self._why.set_text(translate_text(send) or "")
        self._why.set_visible(send is not None)
        self.test_cut.set_sensitive(test_cut is None)
        self.test_cut.set_tooltip_text(translate_text(test_cut))

    def set_send_primary(self, primary: bool) -> None:
        """Only one cyan action per view: the big Send is primary while this panel shows."""
        if primary:
            self.send_button.add_css_class("suggested-action")
        else:
            self.send_button.remove_css_class("suggested-action")

    def preset(self) -> Preset | None:
        """The preset for the chosen material, action and holder (None with the settings from
        Cut Settings)."""
        if self.manual or not self.material or not self.action:
            return None
        return next((p for p in self.store.presets_for(self.material)
                     if p.action == self.action and p.holder == self.holder), None)

    def _base(self) -> ToolSettings | None:
        """The settings before the user's edits: the preset's, or Cut Settings' first values."""
        if self.manual:
            return starting_settings(self.profile, self.holder, self._manual_tool_code(self.holder))
        preset = self.preset()
        return None if preset is None else preset.to_tool_settings()

    def _is_blade(self, settings: ToolSettings | None) -> bool:
        tool = None if settings is None else self.profile.tool(settings.holder, settings.tool_code)
        return tool is not None and tool.blade

    def tool_settings(self) -> ToolSettings | None:
        """The preset's settings with the user's edits (overcut edits only for blade tools)."""
        base = self._base()
        if base is None:
            return None
        blade = self._is_blade(base)
        edits = {k: v for k, v in self._edits.items() if blade or k not in BLADE_ONLY_EDITS}
        return replace(base, **edits)

    @property
    def blade_tool(self) -> bool:
        """True when the chosen tool cuts with a blade (overcut settings apply)."""
        return self._is_blade(self._base())

    def tool_check(self) -> ToolCheck | None:
        """The required tool against the detected one."""
        settings = self.tool_settings()
        return None if settings is None else tool_check(settings, self.machine, self.profile)

    @property
    def modified(self) -> bool:
        """True when a setting differs from the preset (or from Cut Settings' first values)."""
        base = self._base()
        return base is not None and self.tool_settings() != base

    def summary_text(self, with_passes: bool = True) -> str:
        """'Blade Depth 1 · Force 10 · Speed 5 · 1 pass' ('' with no material); without passes
        it ends in the acceleration, which a machine without one leaves out."""
        s = self.tool_settings()
        if s is None:
            return ""
        text = settings_summary(s, with_passes)
        if not with_passes and self.profile.ranges.acceleration is None:
            text = text.rsplit(" · ", 1)[0]
        return text

    def picker_line(self, material: str) -> tuple[str, str]:
        """(line, tooltip) the material list shows for ``material``: the settings of the preset
        that picking it selects now, from the same rule as ``select_material``."""
        preset = choose_preset(self.store, material, self.action, self.holder)
        if preset is None:
            return "", ""
        line = preset_line(preset, self.profile)
        note = depth_note(preset.to_tool_settings())
        return line, line + (f"\n{note}" if note else "")

    def reset_to_preset(self) -> None:
        """Drop the user's edits."""
        self._edits.clear()
        self._refresh()
        self.emit("changed")

    # --- internals -------------------------------------------------------------------
    def _fill_actions(self, actions: list[str], chosen: str | None) -> None:
        updating, self._updating = self._updating, True
        self._action_keys = list(actions)
        self._actions.splice(0, self._actions.get_n_items(), [action_label(a) for a in actions])
        self._action.set_selected(actions.index(chosen) if chosen in actions
                                  else Gtk.INVALID_LIST_POSITION)
        self._action.set_sensitive(bool(actions) and not self.manual)
        self._updating = updating

    def _manual_tool_code(self, holder: int) -> int:
        """The tool chosen for ``holder`` with the settings from Cut Settings: the user's
        choice, else the holder's first one (its depth tool, the AutoBlade)."""
        code = self._manual_tools.get(holder)
        return code if code is not None else starting_settings(self.profile, holder).tool_code

    def _manual_mode_action(self) -> None:
        """The action is the chosen tool's (Cut, Sketch, ...)."""
        tool = self.profile.tool(self.holder, self._manual_tool_code(self.holder))
        self.action = tool.action if tool is not None else None
        self._fill_actions([self.action] if self.action else [], self.action)

    def _set_action(self, action: str | None, holder: int | None = None) -> None:
        self.action = action
        if holder is None:
            chosen = choose_preset(self.store, self.material, action, self.holder)
            holder = chosen.holder if chosen is not None else None
        if holder is not None:
            self.holder = holder
        self._edits.clear()
        self._refresh()
        self.emit("changed")

    def _holders(self) -> list[int]:
        if self.manual:
            return [h.holder for h in tool_cards(self.profile) if h.tools]
        if not self.material or not self.action:
            return []
        return sorted({p.holder for p in self.store.presets_for(self.material) if p.action == self.action})

    def _required(self, holder: int) -> int | None:
        if self.manual:
            return self._manual_tool_code(holder) if holder in self._holders() else None
        return next((p.tool_code for p in self.store.presets_for(self.material or "")
                     if p.action == self.action and p.holder == holder), None)

    def _refresh(self) -> None:
        self._updating = True
        preset = self.preset()
        empty = materials_note(self.store) is not None
        shown = (_(MANUAL_MATERIAL) if self.manual else material_label(self.material)) \
            if self.material else None
        self._material_label.set_text(shown or (_(CHOOSE_MATERIAL_PLACEHOLDER)
                                                if not empty else _(NO_MATERIALS)))
        self._material_label.set_tooltip_text(_(NO_MATERIALS) if empty and not self.material
                                              else None)
        if self.material:
            self._material_label.remove_css_class("dim-label")
        else:
            self._material_label.add_css_class("dim-label")
        origin = store_description(self.store)  # "Basic set", or the presets file's own text
        if self.manual:
            text = _(MANUAL_SOURCE)
        elif empty:                              # none of its rows are for this machine
            text = ""
        else:
            text = f"{material_label(preset.material_type)} · {origin}" if preset else origin
        self._source.set_text(text)
        self._source.set_tooltip_text(text)
        self._empty.set_visible(empty and not self.manual)
        self._material_dot.set_visible(self.modified)
        self._reset.set_label(_("Reset") if self.manual else _("Reset to preset"))
        self._reset.set_visible(self.modified)
        self._refresh_tools()
        settings = self.tool_settings()
        if settings is not None:
            base = self._base()
            ranges = slider_ranges(self.profile, settings.holder, settings.tool_code)
            for key, row in self._sliders.items():
                value = getattr(settings, key)
                if ranges[key] is not None:
                    row.set_range(*ranges[key])
                row.show(value if value is not None else 0, value != getattr(base, key),
                         visible=value is not None and ranges[key] is not None)
            depth = self._sliders["blade_depth"]
            depth.value.set_tooltip_text(None if settings.blade_depth is None
                                         else blade_depth_mm(settings.blade_depth))
            low, high = ranges["passes"]
            self._passes.set_range(low, high)
            self._passes.set_value(settings.passes)
            self._passes_dot.set_visible(settings.passes != base.passes)
            self._lso.set_active(settings.line_segment_overcut)
            self._overcut.set_active(any(settings.overcut))
        blade = self.blade_tool
        for widget in self._blade_rows:
            widget.set_visible(blade)
            widget.set_sensitive(blade)
        self._summary.set_text(self.summary_text())
        self._summary.set_tooltip_text(depth_note(settings))
        if settings is None:                     # nothing to edit: keep the editor closed
            self._settings_toggle.set_active(False)
        self._settings_toggle.set_sensitive(settings is not None)
        self._updating = False

    def _refresh_tools(self) -> None:
        holders = self._holders()
        reported = self.machine.machine_profile          # the tools the machine reports
        for holder, card in self._cards.items():
            required = self._required(holder)
            detected = self.machine.tool_code(holder)
            selected = holder == self.holder and holder in holders
            card.set_sensitive(required is not None)
            card.set_active(selected)
            if detected is not None:
                tool = tool_label(tool_name(holder, detected, reported))
            else:
                tool = (tool_label(tool_name(holder, required, self.profile))
                        if selected and required is not None else None)
            if required is None and not self.material:
                desc, icon, warn = _("No material chosen"), None, False
            elif required is None:
                desc = (_("Not used for {action}").format(action=action_label(self.action))
                        if self.action else _("Not used for this action"))
                icon, warn = None, False
            elif detected is None and self.machine.tools_problem:   # reported, unreadable
                desc = _("Unclear reply “{reply}”").format(
                    reply=(self.machine.tools_reply or "").strip())
                icon, warn = "dialog-warning-symbolic", True
            elif detected is None:      # never, for a machine refused for its model
                desc = _(NOT_DETECTED) if self.machine.wrong_model else _(NOT_DETECTED_YET)
                icon, warn = None, False
            elif not selected:
                desc, icon, warn = _("Detected") if detected else _("Not used"), None, False
            elif detected == required:
                desc = _("Detected · required for {action}").format(
                    action=action_label(self.action))
                icon, warn = "object-select-symbolic", False
            else:
                desc = _("Detected · {tool} required").format(
                    tool=tool_label(tool_name(holder, required, self.profile)))
                icon, warn = "dialog-warning-symbolic", True
            card.show_state(tool, desc, icon, warn)
        check = self.tool_check()
        mismatch = check is not None and check.known and not check.matches
        self._mismatch.set_visible(mismatch)
        if mismatch:
            tool = GLib.markup_escape_text(tool_label(check.required_name))
            if len(self.profile.holders) == 1:
                title = _("Load the <b>{tool}</b> into <b>the tool holder</b>.").format(tool=tool)
            else:
                title = _("Load the <b>{tool}</b> into <b>{holder}</b>.").format(
                    tool=tool, holder=GLib.markup_escape_text(holder_label(check.label)))
            self._mismatch_title.set_markup(title)
            current = tool_label(tool_name(check.holder, check.detected_code, reported))
            self._mismatch_detail.set_text(
                (_("Currently: {tool} detected.") if check.detected_code
                 else _("Currently: {tool}.")).format(tool=current))
        self._refresh_manual_tool()

    def _refresh_manual_tool(self) -> None:
        """The tool list of the chosen holder, with the settings from Cut Settings only."""
        self._manual_tool.set_visible(self.manual)
        if not self.manual:
            return
        card = next((c for c in tool_cards(self.profile) if c.holder == self.holder), None)
        tools = list(card.tools) if card is not None else []
        codes = [code for code, _name in tools]
        if tools != self._manual_shown:          # the names too: a code may differ per holder
            self._manual_shown, self._manual_codes = tools, codes
            self._manual_tool_list.splice(0, self._manual_tool_list.get_n_items(),
                                          [tool_label(name) for _code, name in tools])
        code = self._manual_tool_code(self.holder)
        self._manual_tool.set_selected(codes.index(code) if code in codes
                                       else Gtk.INVALID_LIST_POSITION)

    def _edit(self, key: str, value) -> None:
        if self._updating or (key in BLADE_ONLY_EDITS and not self.blade_tool):
            return
        self._edits[key] = value
        self._refresh()
        self.emit("changed")

    def _on_overcut(self, active: bool) -> None:
        settings = self._base()
        base = tuple(settings.overcut) if settings else DEFAULT_OVERCUT
        self._edit("overcut", (base if any(base) else DEFAULT_OVERCUT) if active else (0, 0))

    def _on_action(self, *_args) -> None:
        if self._updating:
            return
        index = self._action.get_selected()
        action = (self._action_keys[index] if index != Gtk.INVALID_LIST_POSITION
                  and index < len(self._action_keys) else None)
        if action != self.action:
            self._set_action(action)

    def _on_card(self, card: ToolCard) -> None:
        if self._updating:
            return
        if not card.get_active():
            if len(self._cards) == 1 and card.holder == self.holder and card.get_sensitive():
                self._updating = True          # a lone card stays chosen, as a radio would
                card.set_active(True)
                self._updating = False
            return
        if card.holder == self.holder:
            return
        self.holder = card.holder
        self._edits.clear()
        if self.manual:
            self._manual_mode_action()
        self._refresh()
        self.emit("changed")

    def _on_manual_tool(self, *_args) -> None:
        if self._updating or not self.manual:
            return
        index = self._manual_tool.get_selected()
        if index >= len(self._manual_codes):
            return
        code = self._manual_codes[index]
        if code == self._manual_tool_code(self.holder):
            return
        self._manual_tools[self.holder] = code
        self._edits.clear()
        self._manual_mode_action()
        self._refresh()
        self.emit("changed")

    def _on_picked(self, material: str) -> None:
        """A material picked in the list: only one of the machine's presets (a list shown before
        the machine changed may name one it does not have)."""
        if material in self.store.materials():
            self.select_material(material)

    def _on_picker_open(self, button: Gtk.MenuButton, _pspec) -> None:
        if button.get_active():
            self._picker.set_data(self.store, self.settings.recent_materials, self.material,
                                  self.picker_line)

    def _on_settings_toggle(self, toggle: Gtk.ToggleButton) -> None:
        open_ = toggle.get_active()
        self._settings_revealer.set_reveal_child(open_)
        self._chevron.set_from_icon_name("pan-down-symbolic" if open_ else "pan-end-symbolic")
        self._edit_label.set_text(_("Done") if open_ else _("Edit"))

    def _on_preview(self, check: Gtk.CheckButton) -> None:
        self.settings.preview_outline = check.get_active()
        self.settings.save()


# --- the sidebar with its two tabs -----------------------------------------------------
class Sidebar(Gtk.Box):
    """The 'Page Setup | Send' tabs over the two panels."""

    __gtype_name__ = "OpenCutterSidebar"
    __gsignals__ = {"tab-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, page_setup: PageSetupPanel, send: SendPanel, start: str = "send"):
        """Put the panels under underline-style tabs."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        tabs = Gtk.Box(spacing=32)
        tabs.add_css_class("tabs")
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        # Page Setup scrolls like the Send panel's settings: its height must not set the
        # window's minimum height
        scroller = Gtk.ScrolledWindow(child=page_setup, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._stack.add_named(scroller, "page-setup")
        self._stack.add_named(send, "send")
        self._tabs: dict[str, Gtk.ToggleButton] = {}
        for name, title in (("page-setup", N_("Page Setup")), ("send", N_("Send"))):
            button = Gtk.ToggleButton(label=_(title))
            if self._tabs:
                button.set_group(next(iter(self._tabs.values())))
            button.connect("toggled", self._on_tab, name)
            self._tabs[name] = button
            tabs.append(button)
        self.append(tabs)
        self.append(self._stack)
        self.show_tab(start)

    @property
    def tab(self) -> str:
        """'page-setup' or 'send'."""
        return self._stack.get_visible_child_name()

    def show_tab(self, name: str) -> None:
        """Switch to a tab."""
        self._tabs[name].set_active(True)
        self._stack.set_visible_child_name(name)

    def _on_tab(self, button: Gtk.ToggleButton, name: str) -> None:
        if button.get_active():
            self._stack.set_visible_child_name(name)
            self.emit("tab-changed", name)
