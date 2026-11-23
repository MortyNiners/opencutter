<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 OpenCutter contributors -->

# Translator glossary

OpenCutter's window is translated with gettext (domain `opencutter`). English is the source
language and the fallback: a text that is not translated, or marked fuzzy, stays English. This
page explains the terms the translations share. It holds no translations: each language decides
its own words in its `po/<code>.po`, and keeps them the same everywhere in that file.

The first languages are Romanian (`ro`), German (`de`), French (`fr`), Spanish (`es`), Italian
(`it`), Portuguese (`pt`, which also serves `pt_BR` until a Brazilian file exists) and Russian
(`ru`).

## How to work on a translation

No gettext tools are needed on the computer; `build-aux/translations.py` does everything in plain
Python:

```
python3 build-aux/translations.py init ro    # start po/ro.po (and add ro to po/LINGUAS)
python3 build-aux/translations.py update     # bring every .po up to date with the template
python3 build-aux/translations.py compile    # write src/opencutter/locale/<code>/.../opencutter.mo
python3 build-aux/translations.py check      # what the tests check (exit status 1 on a problem)
```

The compiled catalogs are kept in the repository next to the code, so commit the `.mo` file
together with the `.po` file (`tests/test_i18n.py` fails when they differ). `sdk-check` runs GNU
`msgfmt --check` as an extra check, with the host's msgfmt or the GNOME SDK's through Flatpak.

To see a translation, choose the language in Preferences → General → Language and start
OpenCutter again (or start it with `LANGUAGE=ro`).

## Rules for every language

- **Write your own translation.** Never copy texts from Silhouette Studio, its manuals or its
  language files: they are Silhouette's, and they may not be used here. Using the ordinary word
  of your language for an ordinary thing (blade, cut, mat, speed) is of course fine.
- **Placeholders.** Keep every `{name}` exactly as it is (same name, same braces); you may move
  it to wherever your sentence needs it. Do not add new ones. `compile` refuses a translation
  with other placeholders, because the window would fail to show it.
- **Markup.** Some texts carry Pango markup: `<b>…</b>` and `<span weight='500'>…</span>`. Keep
  the same tags around the matching words, and write `&amp;` for an ampersand in those texts.
  Texts without tags are shown as plain text, but a few are put into a label that reads markup,
  so avoid `&` and `<` in any translation.
- **Plural forms.** A text with a count has one translation per plural form of your language
  (the header's `Plural-Forms`: three for `ro` and `ru`, two for the others). The count is
  `{n}`.
- **Sentences the background service writes.** Many texts come from OpenCutter's background
  service or from the machine state as finished English sentences, and the window translates them
  by matching them against their templates. Translate them like any other text. A few are pieces
  that are added after a sentence; their comment says so: keep the space at the start.
- **Ellipsis and quotes.** Keep the single character `…` where the English has it (an action that
  opens something, or a state that is still going on). Use the quotation marks your language
  uses; the English texts use “ ” around a reply the machine sent.
- **Length.** Button labels, tabs, the status pill and the Tool cards have little room. Prefer
  the short form your desktop (GNOME) uses for the same idea.
- **Tone.** Short, plain, direct sentences that say what to do. Use the polite or the informal
  form as GNOME applications in your language usually do, and keep to it.

## What is never translated

- **Machine and model names**: CAMEO, CAMEO 4, CAMEO 4 Plus, CAMEO 4 Pro, CAMEO 5, CAMEO 5 Plus,
  CAMEO Pro MK-II, CAMEO 5 Alpha, CAMEO 5 Alpha Plus, Portrait, Portrait 3, Portrait 4, CURIO and
  the other names a machine reports. "Silhouette" and "Silhouette America, Inc." are names too.
- **Product names**: OpenCutter, **AutoBlade**, Inkscape, Bluetooth, USB, SVG, Flatpak.
- **Command and protocol notation**: status digits in quotes (`'3'`), GPGL commands and replies
  (`U`, `TG1`, `FN0`, `ESC 09`), firmware versions (`V5.xx`), file names and paths, command
  lines (`opencutter stop`, `opencutter service stop`) and anything in backquotes.
- **Units**: `mm`, `in`; `s` and `min` follow your language's usual abbreviation for seconds and
  minutes. Sizes such as `12 × 12 in` and `A4` stay as they are.
- **The keys of the keyboard and the menus of other programs**: write the key as it is printed
  on keyboards in your country (Esc, Ctrl, Shift), and Inkscape's menu items (Path > Object to
  Path, Path > Trace Bitmap) as Inkscape shows them in your language.

## Terms

The words below are the user's working vocabulary: they name what the user sees on the machine,
in the window and in Silhouette's own instructions. Pick one word for each and use it everywhere.
Where a term is a label in the window it is written with capitals in English (Cutting Mat, Blade
Depth); follow the capitalisation rules of your language.

### The machine and the media

- **Cutting Mat** (also "mat", "cutting mat"): the sticky carrier sheet the media sits on, fed
  into the machine. Page Setup, the Load sheet, the canvas caption (`CUTTING MAT {mat}`, written
  in capitals), many hints.
- **Media**, **Media Size**: the sheet of material placed on the mat (paper, vinyl, card) and its
  size. Page Setup, the Send sheet, banners.
- **Load**, **Unload**: the two buttons on the machine that pull the mat in and push it out. In
  English they are written as the machine shows them; name them the way the machine's own panel
  and Silhouette's printed instructions name them in your language, or keep the English word in
  bold if those are in English.
- **Load Media** (machine status): what the machine reports when it waits for a mat. One of the
  machine status words below.
- **Pause** (the machine's Pause key, and the app's Pause button): stops the machine after the
  moves it already planned; **Resume** goes on from there. Not the same as **Cancel Job**, which
  ends the job.
- **Tool holder**, **Tool 1**, **Tool 2**, **Tool**: the carriage slots for tools. "Tool 1" is the
  left holder; a one-holder machine shows "Tool". Context `tool holder`; `{number}` is the digit.
- **Carriage**: the part that carries the tool holders across the mat.
- **Power button**: the machine's on/off button; the safety texts tell to press it when the
  machine keeps moving. Keep it clear and unmistakable.

### Machine status (context `machine status`)

What the machine reports about itself, in the status pill, the popover and the Load sheet:
**Ready**, **Busy**, **Load Media**, **Paused**, **Cancelling**, **Rejected** (another connection,
such as USB, holds the machine). Short words or short phrases; they also appear inside sentences
such as "status '1' = Busy".

The link states: **Not Connected**, **Connecting…**, **Connected**, **Could not Connect**,
**Unsupported Machine**, **Checking connection**.

### Tools (context `tool`)

The tools the machine detects in a holder: **Ratchet Blade**, **Deep-Cut Blade**, **Kraft
Blade, 2mm**, **Kraft Blade, 3mm**, **Rotary Blade**, **Punch Tool**, **Sketch Pen**, **Foil
Tool**, **No Tool Detected**. These are Silhouette's product names: translate the descriptive
word (blade, pen, tool) if your language's Silhouette packaging does, or keep the English name;
the **AutoBlade** stays as it is.

### What the user chooses (Send panel)

- **Material**: the material to cut (one preset per material and action). The Basic set's
  materials and material types are in context `material`: **Vinyl**, **Paper**, **Sketch Pen**
  (the material name of the pen's starting point). A presets file's own names are never
  translated.
- **Action** (context `action` for the names): what the tool does to the lines: **Cut**,
  **Sketch** (draw with a pen), **Score** (press a line without cutting through), **Kraft Cut**,
  **Rotary Cut**, **Punch**, **Foil**, **No Cut**. "Cut" also exists as a button (context
  `button`: the button at the outline check that lowers the tool and cuts); the two may have
  different words.
- **Tool**: the field and the cards that show which tool the job needs in which holder.
- **Cut Settings**: the expandable group of values below; **Edit** opens it, **Done** closes it.
  With no materials for a machine, "Cut Settings" is also what the Material field shows.
- **Blade Depth**: how far the AutoBlade's blade sticks out, a step number (1 = 0.1 mm). Also
  "Blade Depth {value}" and the short line "Blade Depth 1 = 0.1 mm".
- **Force**: how hard the tool presses. **Speed**: how fast the tool moves. **Acceleration**
  (short form "Accel." in one line, keep it short too).
- **Passes**: how many times the tool goes over every line ("{n} pass", "{n} passes").
- **Line Segment Overcut**, **Overcut**: blade settings that cut a little past corners and line
  ends so the shape comes out cleanly.
- **Preset**: a stored set of values for a material and action; **Reset to preset** drops the
  user's changes. **Basic set**: OpenCutter's own small preset list. **Presets file**: a file of
  presets the user provides.

### What the user does

- **Send** (the big button, the header button, the sheet "Send to {machine}" and the tab):
  sends the job to the machine; nothing moves before it. Use the word your desktop uses for
  sending to a device or printer.
- **Test Cut**: a small square cut with the current settings to check them before the real job;
  "Test Complete", "Test in Progress", "Test stopped".
- **Cancel Job**: ends the running job and stops the machine at once (Esc). Distinct from
  **Cancel**, which only closes a dialog or leaves the outline check.
- **Stop Machine**: sends the stop to the machine after a job, without a job running.
- **Job**: one design sent to the machine with its settings; "Job in progress", "Job cancelled",
  "{action} complete".
- **Outline check**, **Preview outline**: before cutting, the tool traces the outer box of the
  design with the tool up; "Is the outline in the right place?" and the Cut button follow.
- **Page Setup** (the tab): machine, Cutting Mat, Media Size, Orientation (**Portrait** /
  **Landscape**, context `orientation`; "Portrait" is also a machine name, never translated
  there), **Reveal Cutting Mat** (show the whole mat around the media).
- **Origin**: the corner the machine starts from (top left of the mat's grid).
- **Cut area**: the part of the mat the machine can reach; "Fit to Mat", "Fit to Media".
- **Align** and its items: **Center to Page**, **Align Left**, **Align Center**, **Align Right**,
  **Align Top**, **Align Middle**, **Align Bottom**.
- **Design**: the SVG file the user opened; **Open design…**, "Open an SVG design".
- **Background service**: OpenCutter's own program that keeps the Bluetooth link to the machine
  ("OpenCutter's background service"). Plain words, not "daemon".
- **Pair** (Settings → Bluetooth): pairing the machine with the computer in the desktop's
  Bluetooth settings; use the word GNOME uses in your language, and its name for "Settings →
  Bluetooth".
