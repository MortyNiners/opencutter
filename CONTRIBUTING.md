# Contributing to OpenCutter

Thank you for helping. OpenCutter drives a machine with a sharp blade, so a few rules are
stricter than usual. Please read [ARCHITECTURE.md](ARCHITECTURE.md) (the module contract and the
safety layers) and [docs/PROTOCOL.md](docs/PROTOCOL.md) (what may and may not be sent to the
machine) before you change anything in `device` or `job`.

By contributing you agree that your contribution is licensed under the GNU General Public
License, version 3 or later (GPL-3.0-or-later), like the rest of the project. Every `.py` file
starts with `# SPDX-License-Identifier: GPL-3.0-or-later` and
`# SPDX-FileCopyrightText: 2026 OpenCutter contributors` (in a `/* */` comment in CSS), and so
does every file in `build-aux/` and `.github/` (after the `#!` line of a script); a test checks
this.

Bugs and results from a machine go to the issue tracker, where two forms ask for what helps:
**Bug report** and **Model report** ([.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/); the
model report asks for what "Reporting a model" below lists). A safety problem (a machine that
moves when it should not, a stop that does not stop, a command sent that should not be) or a
security problem is reported privately, never in a public issue: see [SECURITY.md](SECURITY.md).
A pull request starts from a short checklist
([docs/pull_request_template.md](docs/pull_request_template.md)): tests run, no Silhouette data,
no personal data, docs updated.

## What must never enter the repository

- **No code that reads Silhouette files.** OpenCutter must not contain code or data that reads,
  decrypts or redistributes Silhouette Studio files, firmware or data: no readers for Studio
  installers or resource files, no keys or ciphers for them, no converters for their material
  tables, and no tests or tools that use them. OpenCutter's own presets come from the Basic set,
  the Cut Settings and the presets file described in the README.
- **No Silhouette data.** No Silhouette Studio installers or parts of them, no encrypted or
  plain resource files (material tables, language files, key files), no decrypted text from
  them, no rows, column names or whole tables copied from them (machine, command, parameter or
  material tables), no command syntax written in Studio's own placeholder notation, no preset
  tables or presets files made from them (`presets.json`), and no screenshots or text copied
  from Silhouette software or documentation.
- **No firmware.** No firmware images or update files, and no disassembly or decompiled code of
  any firmware.
- **No captures.** No Bluetooth or USB captures (`.btsnoop`, `.pcap`, ...), your own or
  published ones, and no logs that contain them.
- **No personal data.** No machine names, Bluetooth addresses, serial numbers, user names or home
  directory paths in code, tests, fixtures or logs you attach.

**What may go in: facts about each machine's interface.** For interoperability, the repository
may state how each machine is driven: its command forms, value ranges, mat sizes and `TG` values,
the replies it gives, and its identity strings (the `FG` model text, USB ids, Bluetooth name
prefixes). Write them in your own words: as typed values in the model's machine profile
(`src/opencutter/machines/`, one module per model family) and as prose in its section of
[docs/PROTOCOL.md](docs/PROTOCOL.md), each value with a short comment that gives its basis
("Studio 5.0.556 machine data", "firmware analysis", "measured on V5.05" or "public captures and
reports"). The fourth basis is for what others have published, for example a USB capture of
Studio driving a machine or a machine's `FG` reply in a forum post: public captures and reports,
described in our own words. Say what kind of source it was and what it showed, and keep the
capture itself, and any log or dump made from it, out of the repository (see "No captures").
Everything else in the list above stays forbidden: a profile is written by hand, value by value,
never generated from Silhouette files, and it holds no table copied from them.

`.gitignore` covers the usual file names, but it is not a guarantee: check `git status` before
every commit.

## Built-in presets

- Every value in the Basic set (`BASIC_PRESETS` in `src/opencutter/presets/model.py`) must come
  from **your own test cuts on your own machine**. Never copy a row from Silhouette Studio's
  material settings, from a presets file made from them, or from another program's tables.
- Write down in a comment what you cut, with which tool and material, and what you checked
  (for example "a 25.4 mm square cut cleanly and matched the mat grid"). Set `verified=True`
  only for a setting you cut on a real machine of every model the row names; mark everything
  else as untested.
- The Basic set is for the CAMEO 4 family: the CAMEO 4, 4 Plus and 4 Pro, which share the
  CAMEO 4's holders, tools, ranges and firmware. Its rows are not verified on the Plus and the
  Pro. A Basic set for another model needs test cuts on that model.
- The one existing vinyl setting has the same values as Studio's "Vinyl, Silhouette Oracal 651"
  preset. It is in the Basic set because it was cut and checked on our machine, and the README
  says where the values come from.

## Protocol and safety

- Describe protocol facts in your own words in `docs/PROTOCOL.md`. No firmware addresses, no
  disassembly listings, and nothing from Silhouette software beyond short command names.
- Never add a command to the builder or the validator's allowlist without adding it, with its
  range and meaning, to `docs/PROTOCOL.md` section 5 (and to the model's own section when only
  some models take it). Never add anything from the never-send list (section 6).
- Keep every existing check. A change may make validation stricter, never weaker.
- Every model's values live in its machine profile (`src/opencutter/machines/`). The registry
  checks each profile against the global rules of `machines/safety.py` when it loads: the
  never-send list, the ESC rules, the stop (ESC 09 twice), the `FM` rule, write sizes and ranges
  no firmware exceeds. A profile can be stricter than these rules, never wider, and no profile
  removes an entry from the never-send list.
- The CAMEO 4's bytes must not change: `tests/test_profile_characterization.py` and the golden
  byte tests (`tests/test_job_builder.py`, `tests/test_device_programs.py`,
  `tests/test_job_review_bytes.py`, `tests/test_job_plan.py`) are not edited to make a change
  pass.

## Tests

- Run `.venv/bin/python -m pytest -q` before you send a change. Add a test for every fix.
- **Never touch a real machine from a test.** No Bluetooth, no `bluetoothctl`, `busctl` or
  `btmon`, no connection to a real OpenCutter socket. Tests that import `opencutter.device`
  install the stubs from `tests/stubs.py` first and use a temporary socket and lock path.
  Tests that stop a background service (the shutdown op, `opencutter service stop`, the app
  replacing a service) do it only in a private `$XDG_RUNTIME_DIR`, against stand-ins they start
  themselves (`tests/service_stubs.py`).
- Do not open windows on your display: use `gtk4-broadwayd` with `GDK_BACKEND=broadway`, as the
  README shows.
- Tests use only the files of this repository and data they make up themselves.

## Building the Flatpak locally

The manifest is `build-aux/flatpak/io.github.mortyniners.OpenCutter.yml`; the release bundles are
built from it (see "Releasing"). You need `flatpak-builder` (from your distribution, or
`org.flatpak.Builder` from Flathub: then write `flatpak run org.flatpak.Builder` wherever the
commands below say `flatpak-builder`) and the GNOME 51 SDK and runtime from Flathub:

```sh
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user flathub org.gnome.Platform//51 org.gnome.Sdk//51
```

Put the build folders outside the repository: the repository tests read every file in the
repository's folder, and a build leaves thousands there. Then, from the repository root:

```sh
B=~/.cache/opencutter-flatpak        # any folder outside the repository (a build takes about 30 MB)
flatpak-builder --user --force-clean --state-dir="$B/state" --repo="$B/repo" "$B/build" \
    build-aux/flatpak/io.github.mortyniners.OpenCutter.yml
flatpak build-bundle "$B/repo" "$B/opencutter-local.flatpak" io.github.mortyniners.OpenCutter \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo
```

`flatpak-builder` builds the Python modules from the PyPI files pinned in the manifest, then
OpenCutter from your working copy as it is (without `.git`, `.venv`, `tests` and the `build` and
`dist` folders that `pip install .` leaves), checks that the installed command runs and that its
modules and icons are there, and installs nothing, since there is no `--install`.
`flatpak build-bundle` makes the bundle that the README's Install section describes. A build
takes less than a minute. "As it is" includes files you have not committed and files that
`.gitignore` hides: build a bundle that you give to anyone else from a clean clone (or a
`git worktree`) of a commit, as the release workflow does.

**Trying the bundle without touching your own installation.** `FLATPAK_USER_DIR` makes the
`--user` commands of one shell use a separate Flatpak installation. It needs the GNOME 51 runtime
of its own, with its graphics drivers and translations (up to about 2.5 GB on disk; `flatpak
install` lists the sizes before it asks):

```sh
export FLATPAK_USER_DIR="$B/installation"
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user flathub org.gnome.Platform//51
flatpak install --user "$B/opencutter-local.flatpak"
flatpak run --command=opencutter io.github.mortyniners.OpenCutter --version
```

The test copy is still the same app for everything else. It may reach the CAMEO 4 over Bluetooth;
it uses the same `~/.var/app/io.github.mortyniners.OpenCutter/` folder as an OpenCutter Flatpak you
use (settings, presets file, service log); and it finds the background service that your own
OpenCutter started, and the other way round. To try it without Bluetooth, take its BlueZ
permission away in the separate installation (the app then says "Bluetooth is off or not
available"):

```sh
flatpak override --user --system-no-talk-name=org.bluez io.github.mortyniners.OpenCutter
```

To keep it apart from your own OpenCutter as well, give the commands that run it a home folder,
a runtime folder and a session bus of their own:

```sh
mkdir -p "$B/home"
env HOME="$B/home" XDG_RUNTIME_DIR="$(mktemp -d)" dbus-run-session -- \
    flatpak run --command=opencutter io.github.mortyniners.OpenCutter service status
```

When you are done, `unset FLATPAK_USER_DIR` and delete the folder `$B`.

## Releasing

A release is a version tag, `vX.Y.Z`. The workflow `.github/workflows/flatpak.yml` runs the
tests and builds the Flatpak bundle on GitHub Actions for every push to `main`, every pull request
and every Run workflow, and keeps the bundle as an artifact of that run. For a version tag, once
the tests have passed and the bundle is built, it also attaches the bundle,
`opencutter-X.Y.Z.flatpak`, and `SHA256SUMS` to the GitHub release of the tag; a failed test
releases nothing. The tests run there with Python 3.14, the Python of the GNOME 51 runtime, and
with the versions of bleak, dbus-fast and svgelements that the Flatpak manifest installs (change
them in both files together), but without PyGObject: the tests that need GTK are skipped there,
so run the whole suite yourself, as step 2 says.

1. Set the version, for example 0.1.0, in three places: `version` in `pyproject.toml`,
   `__version__` in `src/opencutter/__init__.py`, and a new first `<release>` in
   `data/io.github.mortyniners.OpenCutter.metainfo.xml`:

   ```xml
   <release version="0.1.0" type="stable" date="2026-12-01">
     <description>
       <p>What changed, in plain words.</p>
     </description>
   </release>
   ```

   Only a release has the form X.Y.Z. A development version such as `0.2.0.dev0` is written
   `0.2.0~dev0` in the metainfo, with `type="development"`, and cannot be released.

   For a release, also name it in the README's Install section: the link to the current
   release, the bundle `opencutter-0.1.0.flatpak` in the download and install commands, and
   the tag in `git clone --branch v0.1.0` (a test checks these against the newest stable
   `<release>`). The screenshot URLs in the metainfo point at the tag of a release
   (`https://raw.githubusercontent.com/MortyNiners/opencutter/v0.1.0/data/screenshots/...`): when
   the screenshots in `data/screenshots/` change, point them at the new tag.
2. Check the three and run the tests:

   ```sh
   python3 build-aux/release-version.py --tag v0.1.0      # prints version=0.1.0
   .venv/bin/python -m pytest -q
   ```

3. Commit the change, tag that commit and push both:

   ```sh
   git tag -a v0.1.0 -m "OpenCutter 0.1.0"
   git push origin main v0.1.0
   ```

4. The workflow builds the bundle. Then its release job checks the tag again and creates the
   release v0.1.0 with `opencutter-0.1.0.flatpak` and `SHA256SUMS` in one step (the release
   stays a draft until both files are uploaded, then it is published). If you want to write
   the release yourself, make it a **draft** before you push the tag: the job adds the two
   files to it, and you publish it. Do not publish it without its files: with immutable
   releases (a repository setting), a published release gets no files later, and its tag can
   no longer be deleted or moved. A tag that does not match the version stops the workflow
   before anything is released: delete the tag (`git push --delete origin v0.1.0` and
   `git tag -d v0.1.0`), fix the version and tag again.
5. Check the release page, <https://github.com/MortyNiners/opencutter/releases>: both files are
   there, and the release notes say how to install. Edit the notes if you like. Download the
   bundle and install it once, as the README says.
6. Set the next development version, for example `0.2.0.dev0`, with a new first
   `<release version="0.2.0~dev0" type="development" date="...">`.

Re-running the workflow of a tag adds files that are missing (a `SHA256SUMS` whose bundle never
arrived is replaced), but it never replaces a bundle that is attached: a new build is never
byte-identical, so its checksum would change under the same version. To fix a release, make a
new version.

## Hardware tests

Only run OpenCutter on a real machine when you mean to, with the safety notes of the README in
mind: load the mat right before the job, keep the outline preview on, use scrap material, keep
your hands clear and the power button within reach, and do not press the machine's Pause key
during a job (OpenCutter ends the job then; press Unload or the power button, never Pause
again). If you report a result at <https://github.com/MortyNiners/opencutter/issues>, say what
"Reporting a model" below lists.

Every model OpenCutter knows has a support level (see "Machine profiles" in
[ARCHITECTURE.md](ARCHITECTURE.md) and section 11 of [docs/PROTOCOL.md](docs/PROTOCOL.md)), and
the level alone decides what OpenCutter sends to it:

- **Tested**: the profile has cut on real hardware. Today that is only the CAMEO 4 with firmware
  V5.xx (tested on V5.05). A model or firmware line becomes tested only after a report of real
  cuts on it, and the facts the report confirmed (for example each mat's measured `U` reply) go
  into its profile.
- **Not yet tested**: ported from Studio's machine data and the firmware analysis, with a few
  values from public captures and reports, never run with OpenCutter. Jobs are allowed;
  README.md lists the model as not yet tested, and the app shows no note about it. A model is added at this level only when every fact needed to move it
  safely is known in its profile: its exact `FG` text, its stop, its tool check (the ESC 15
  reply), its ranges, and the `TG` value and cutting area of each mat it offers. A mat whose `U`
  reply is not known is offered only at this level, and never with an unknown `TG` value.
- **Recognized**: the model is named, but a fact needed to move it safely is missing. It gets
  ESC 05 and `FG` only, and its reason says, in plain words, what is missing. It moves up only
  when that fact is known, with tests.

Whatever the level, nothing is driven on a guess: a fact that is not known keeps the model at a
lower level, or leaves the mat or the tool out of its profile.

## Reporting a model

If you own a model that is not yet tested, or one that OpenCutter only recognizes, a report of
what it does helps to move it up a level. Open an issue at
<https://github.com/MortyNiners/opencutter/issues> with:

- **The `FG` reply**, as the machine sends it: the `Firmware (FG):` line of `opencutter info`,
  or for a machine the service refused, the `model:` line of `opencutter service status`. Both
  leave out the spaces at the end. To see the exact reply, close the app, run
  `opencutter service stop`, then `opencutter serve -v` in a terminal: it logs every reply.
- **The tool replies**: the `Tools (ESC 15):` line of `opencutter info`, once for each tool you
  have in each holder, and once with the holders empty. On a model with one holder, say which of
  the two numbers changes.
- **The `U` reply of each mat**: load the mat right before a job and send it. The service log
  (`daemon.log`, see Troubleshooting in the README, or the terminal of `opencutter serve`) has
  one line `job: U -> '...' (...)` per job. Say which mat it was.
- **What was cut**: the mat, the tool and its holder, the material, the settings (Blade Depth,
  Force, Speed, Acceleration, Passes), and what you measured: whether a 1-inch square came out as
  one inch, where it landed against the mat grid, and whether the cut went cleanly through.
- **The stop**, if you tried it (with a hand on the power button): whether a running cut stopped
  at once after Cancel Job or `opencutter stop`, whether anything moved afterwards, and the
  service log's lines from the stop on (`STOP (ESC 09 x2) sent, ...`, `after the stop the
  machine reports ...` and the `[status change]` lines). Whether the stop also drops what was
  already sent to the machine is not known yet (docs/PROTOCOL.md, section 9). After a stop,
  press Unload or the power button; do not press the machine's Pause key to continue.

Leave out personal data: `opencutter status` and the service log show the machine's Bluetooth
name, which holds its serial number, and its Bluetooth address. Replace both before you post.
The connection menu shows the name too: cover it in a screenshot.
