# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The Material list: searchable, grouped by Material Type, with Recent on top.

Each row shows, on a dim second line, the settings of the preset that picking the material
selects (the Send panel computes the line with the rule it then applies). The Basic set's note
says what it holds; on a machine where none of its rows was cut (the CAMEO 4 Plus and Pro get
the CAMEO 4's rows, not verified on them) it says where the rows come from and to start light
instead (BASIC_NOTE_UNTESTED), and nothing about testing.

A machine whose view of the presets is empty (no Basic set for it, no rows for it in the
presets file) gets the empty state instead: "No materials for this machine yet", which points to
Cut Settings, and the button Use Cut Settings (the signal ``use-cut-settings``): the Send panel
then takes its settings from Cut Settings, which the user sets, starting light."""
from __future__ import annotations

from collections.abc import Callable

from gi.repository import GObject, Gtk, Pango

from opencutter.presets.model import PresetStore

from .sending import NO_MATERIALS, NO_MATERIALS_HINT
from .widgets import label

USE_CUT_SETTINGS = "Use Cut Settings"

BASIC_NOTE = ("Basic set: one vinyl cut setting and a Sketch Pen starting point. For other "
              "materials, start from the closest one and adjust Speed, Force and Blade Depth in "
              "Cut Settings, then make a Test Cut.")
# The Basic set on a machine where none of its settings was cut (the CAMEO 4 Plus and Pro get
# the CAMEO 4's rows, not verified on them): where they come from, and to start light
BASIC_NOTE_UNTESTED = ("Basic set from the CAMEO 4. Start light: adjust Speed, Force and Blade "
                       "Depth in Cut Settings, then make a Test Cut.")


class MaterialPicker(Gtk.Popover):
    """Searchable material list grouped by Material Type, with Recent on top."""

    __gtype_name__ = "OpenCutterMaterialPicker"
    __gsignals__ = {
        "picked": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "use-cut-settings": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        """Create the (empty) popover; ``set_data`` fills it."""
        super().__init__(has_arrow=False)
        self.add_css_class("material-picker")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_size_request(320, -1)
        self._search = Gtk.SearchEntry(placeholder_text="Search materials")
        self._search.update_property([Gtk.AccessibleProperty.LABEL], ["Search materials"])
        self._search.connect("search-changed", self._on_search)
        self._search.connect("activate", self._on_search_activate)
        box.append(self._search)

        self._basic = Gtk.Box(spacing=8)
        self._basic.add_css_class("inline-alert")
        self._basic.add_css_class("info")
        self._basic.append(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
        note = label(BASIC_NOTE, "caption", wrap=True)
        note.set_max_width_chars(40)      # wrap inside the 320 px list, not widen the popover
        note.set_hexpand(True)
        self._basic.append(note)
        self._basic_note = note           # BASIC_NOTE_UNTESTED where no row was cut (set_data)
        box.append(self._basic)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("material-list")
        self._list.set_header_func(self._header)
        self._list.set_filter_func(self._filter)
        self._list.connect("row-activated", lambda _l, row: self._pick(row.material))
        self._scroller = Gtk.ScrolledWindow(child=self._list,
                                            hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            propagate_natural_height=True, max_content_height=420)
        box.append(self._scroller)

        # no materials for this machine: say so, and point to Cut Settings
        self._empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._empty.append(label(NO_MATERIALS, "heading", wrap=True))
        hint = label(NO_MATERIALS_HINT, "caption", "dim-label", wrap=True)
        hint.set_max_width_chars(40)
        self._empty.append(hint)
        self._use = Gtk.Button(label=USE_CUT_SETTINGS, halign=Gtk.Align.START)
        self._use.add_css_class("pill-sm")
        self._use.connect("clicked", lambda *_: self._use_cut_settings())
        self._empty.append(self._use)
        self._empty.set_visible(False)
        box.append(self._empty)
        self.set_child(box)
        self._matches: set[str] | None = None      # None: no search, show everything
        self._store: PresetStore | None = None
        self._describe: Callable[[str], tuple[str, str]] | None = None
        self.connect("show", lambda *_: (self._search.set_text(""), self._search.grab_focus()))

    def set_data(self, store: PresetStore, recent: list[str], current: str | None,
                 describe: Callable[[str], tuple[str, str]] | None = None) -> None:
        """Fill the list from the store; ``recent`` materials come first. ``describe(material)``
        gives each row's settings line and its tooltip ('' for none)."""
        self._store = store
        self._describe = describe
        empty = not store.presets
        self._empty.set_visible(empty)
        self._search.set_visible(not empty)
        self._scroller.set_visible(not empty)
        self._basic.set_visible(store.source.get("kind") == "basic" and not empty)
        verified = any(p.verified for p in store.presets)
        self._basic_note.set_text(BASIC_NOTE if verified else BASIC_NOTE_UNTESTED)
        while (row := self._list.get_row_at_index(0)) is not None:
            self._list.remove(row)
        known = set(store.materials())
        groups = [("Recent", m) for m in recent if m in known]
        for material in store.materials():
            presets = store.presets_for(material)
            groups.append((presets[0].material_type if presets else "Other", material))
        order = {t: i for i, t in enumerate(store.material_types())}
        groups.sort(key=lambda g: -1 if g[0] == "Recent" else order.get(g[0], len(order)))
        for group, material in groups:
            self._list.append(_MaterialRow(group, material, material == current))
        self.refresh_lines()

    def refresh_lines(self) -> None:
        """Recompute every row's settings line (e.g. after the machine reported other tools)."""
        index = 0
        while (row := self._list.get_row_at_index(index)) is not None:
            row.set_line(*(self._describe(row.material) if self._describe else ("", "")))
            index += 1

    def line(self, material: str) -> str:
        """The settings line shown for ``material`` (its first row)."""
        index = 0
        while (row := self._list.get_row_at_index(index)) is not None:
            if row.material == material:
                return row.line
            index += 1
        raise KeyError(material)

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        text = entry.get_text().strip()
        self._matches = set(self._store.search(text)) if text and self._store else None
        self._list.invalidate_filter()

    def _filter(self, row: "_MaterialRow") -> bool:
        if self._matches is None:
            return True
        return row.group != "Recent" and row.material in self._matches

    def _header(self, row: "_MaterialRow", before: "_MaterialRow | None") -> None:
        if before is not None and before.group == row.group:
            row.set_header(None)
            return
        header = label(row.group, "material-group")
        row.set_header(header)

    def _on_search_activate(self, entry: Gtk.SearchEntry) -> None:
        """Enter picks the first match of a search. With nothing typed it picks nothing: the
        field has the focus as the list opens, and a stray Enter must never choose a material
        (the first row would be an untested preset)."""
        if not entry.get_text().strip():
            return
        self._on_search(entry)        # what is typed now, even before the search delay ran out
        index = 0
        while (row := self._list.get_row_at_index(index)) is not None:
            if self._filter(row):
                self._pick(row.material)
                return
            index += 1

    def _pick(self, material: str) -> None:
        self.popdown()
        self.emit("picked", material)

    def _use_cut_settings(self) -> None:
        self.popdown()
        self.emit("use-cut-settings")


class _MaterialRow(Gtk.ListBoxRow):
    def __init__(self, group: str, material: str, current: bool):
        super().__init__()
        self.group, self.material = group, material
        box = Gtk.Box(spacing=8)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        name = label(material)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        self._line = label("", "caption", "dim-label", "numeric")
        self._line.set_ellipsize(Pango.EllipsizeMode.END)
        texts.append(name)
        texts.append(self._line)
        box.append(texts)
        if current:
            box.append(Gtk.Image(icon_name="object-select-symbolic", valign=Gtk.Align.CENTER))
            self.add_css_class("current")
        self.set_child(box)
        self.set_line("", "")

    @property
    def line(self) -> str:
        """The settings line shown under the name."""
        return self._line.get_text()

    def set_line(self, line: str, tooltip: str) -> None:
        """Show the settings line; the tooltip carries the whole line (it may be ellipsized)."""
        self._line.set_text(line)
        self._line.set_visible(bool(line))
        self.set_tooltip_text(f"{self.material}\n{tooltip}" if tooltip else self.material)
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION], [line])
