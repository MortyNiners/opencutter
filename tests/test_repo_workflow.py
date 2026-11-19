# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""The release workflow (.github/workflows/flatpak.yml): it runs the tests and builds the Flatpak
bundle from the manifest for tags, Run workflow, pull requests and main, and only a pushed
version tag releases, after the tests, the build and the version check, with the one write
permission of the workflow. The tests run with the Python and the dependency versions of the
manifest, without PyGObject. Actions are pinned to commit SHAs. The notes of a new release come
from a quoted heredoc, rendered here with the workflow's own lines and a sample version: the
screenshot of the tag, the sections, the machines of the registry, the README's install
commands and limitations, links to the project only, and nothing left unexpanded. No network:
the workflow is only read, and the suite's own run without PyGObject is tried in a child
pytest."""
import os
import re
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from opencutter import APP_ID
from opencutter.machines import Support, registry

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO / ".github" / "workflows" / "flatpak.yml"
TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(TEXT)
JOBS = WORKFLOW["jobs"]
MANIFEST_PATH = f"build-aux/flatpak/{APP_ID}.yml"
MANIFEST = yaml.safe_load((REPO / MANIFEST_PATH).read_text(encoding="utf-8"))
FLATHUB = "https://dl.flathub.org/repo/flathub.flatpakrepo"
VERSION = "${{ steps.version.outputs.version }}"
TAG_ONLY = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
PUBLISH = "Create the release, or add the files to it"
TEST_COMMAND = "python -m pytest -q -p no:cacheprovider"
PROJECT = "https://github.com/MortyNiners/opencutter"
RAW = "https://raw.githubusercontent.com/MortyNiners/opencutter"
NOTES_START = "cat > notes.md <<'EOF'\n"
MODEL_REPORT = f"{PROJECT}/issues/new?template=model_report.yml"      # the issue form
NOTES_SED = 'sed -i -e "s/@VERSION@/$VERSION/g" -e "s/@TAG@/$TAG/g" notes.md'
NOTES_SECTIONS = ["Highlights", "Machines", "Install", "Known limitations", "Report a model"]
SAMPLE = "9.8.7"                  # a version no file names: nothing may stay at the current one
MODEL_NAMES = sorted((e.name for e in registry.entries()), key=len, reverse=True)


def triggers() -> dict:
    # YAML 1.1 (PyYAML) reads the key "on" as True; GitHub reads it as "on"
    return WORKFLOW.get("on", WORKFLOW.get(True))


def steps(job: str, uses: str | None = None) -> list[dict]:
    found = JOBS[job]["steps"]
    return [s for s in found if uses is None or s.get("uses", "").startswith(uses + "@")]


def runs(job: str) -> str:
    return "\n".join(s["run"] for s in steps(job) if "run" in s)


def flat_text(s: str) -> str:
    """Collapse line breaks and runs of spaces, so phrases can be found across wrapped lines."""
    return re.sub(r"\s+", " ", s)


def publish_script() -> str:
    return steps("release")[[s.get("name") for s in steps("release")].index(PUBLISH)]["run"]


def notes_block() -> str:
    """The lines of the publish step that write notes.md: the heredoc and the sed after it."""
    script = publish_script()
    start = script.index(NOTES_START)
    return script[start:script.index("gh release create", start)]


def render_notes(tmp_path: Path, version: str = SAMPLE) -> str:
    """notes.md as the release job writes it: the workflow's own lines, run by a separate bash
    with nothing but a sample VERSION and TAG (and PATH)."""
    done = subprocess.run(["bash", "-e", "-c", notes_block()], cwd=tmp_path,
                          env={"PATH": "/usr/bin:/bin", "VERSION": version, "TAG": f"v{version}"},
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return (tmp_path / "notes.md").read_text(encoding="utf-8")


def notes_section(notes: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |^---$|\Z)", notes, re.M | re.S)
    assert m, f"the release notes have no section {title!r}"
    return m[1]


def mentions(doc: str, name: str) -> bool:
    """True when `doc` names the model `name` itself, not only a longer model name that contains
    it ("CAMEO 5" in "CAMEO 5 Plus")."""
    for longer in MODEL_NAMES:
        if longer != name and name in longer:
            doc = doc.replace(longer, " ")
    return re.search(re.escape(name) + r"(?![0-9A-Za-z])", doc) is not None


def anchors(path: Path) -> set[str]:
    """The anchors GitHub gives the headings of a Markdown file."""
    titles = re.findall(r"^#+ (.+?)\s*$", path.read_text(encoding="utf-8"), re.M)
    return {re.sub(r"[^\w\- ]", "", t.lower()).replace(" ", "-") for t in titles}


def readme_flatpak_commands() -> list[str]:
    """The first sh block of the README's "The Flatpak": download, check, install."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    part = re.search(r"^### The Flatpak$(.*?)^### ", readme, re.M | re.S)[1]
    return re.search(r"^```sh\n(.*?)^```$", part, re.M | re.S)[1].splitlines()


def test_triggers_tags_run_workflow_pull_requests_and_main():
    on = triggers()
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}
    assert on["push"] == {"branches": ["main"], "tags": ["v*"]}


def test_permissions_are_read_only_except_the_release_job():
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert set(JOBS) == {"test", "build", "release"}
    assert "permissions" not in JOBS["test"] and "permissions" not in JOBS["build"]  # from the top
    assert JOBS["release"]["permissions"] == {"contents": "write"}
    # nowhere else: no other permission block, no write-all
    assert len(re.findall(r"^\s*permissions:", TEXT, re.M)) == 2
    assert len(re.findall(r":\s*write\b", TEXT)) == 1 and "write-all" not in TEXT


def test_the_release_job_runs_only_for_a_pushed_version_tag_after_the_tests_and_the_build():
    release = JOBS["release"]
    assert sorted(release["needs"]) == ["build", "test"]
    assert release["if"] == TAG_ONLY
    # the tests and the build run for every trigger, side by side
    for job in ("test", "build"):
        assert "if" not in JOBS[job] and "needs" not in JOBS[job], job


def test_every_action_is_pinned_to_a_commit_sha_with_its_version():
    uses = re.findall(r"^\s*(?:- )?uses: (\S+)(.*)$", TEXT, re.M)
    assert len(uses) == 7
    for ref, comment in uses:
        action, _, sha = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", sha), ref
        assert re.fullmatch(r" # v\d+(\.\d+)*", comment), (ref, comment)
    pins = {}
    for ref, comment in uses:                  # one version of each action in the whole file
        pins.setdefault(ref.split("@")[0], set()).add(ref + comment)
    assert all(len(v) == 1 for v in pins.values()), pins
    assert set(pins) == {"actions/checkout", "actions/setup-python", "actions/upload-artifact",
                         "actions/download-artifact",
                         "flatpak/flatpak-github-actions/flatpak-builder"}


def test_checkouts_keep_no_credentials():
    for job in JOBS:
        for step in steps(job, "actions/checkout"):
            assert step["with"]["persist-credentials"] is False


def test_the_test_job_runs_the_suite_with_the_runtimes_python():
    test = JOBS["test"]
    assert test["runs-on"] == "ubuntu-24.04" and "container" not in test
    names = [s["name"] for s in test["steps"]]
    assert names == ["Check out the repository", "Set up Python",
                     "Install pytest and the dependencies", "Run the tests"]
    [setup] = steps("test", "actions/setup-python")
    # the Python of the GNOME runtime, as the manifest's paths name it (python3.14)
    manifest_text = (REPO / MANIFEST_PATH).read_text(encoding="utf-8")
    [runtime_python] = set(re.findall(r"/lib/python(3\.\d+)/", manifest_text))
    assert setup["with"] == {"python-version": runtime_python}
    # the whole suite, as CONTRIBUTING.md runs it: pyproject.toml's testpaths
    assert test["steps"][-1]["run"] == TEST_COMMAND


def ci_pins() -> dict[str, str]:
    """name: version of each package the test job installs (each is pinned with ==)."""
    [install] = [s for s in steps("test") if s.get("name") == "Install pytest and the dependencies"]
    words = install["run"].split()
    assert words[:4] == ["python", "-m", "pip", "install"]
    options = [w for w in words[4:] if w.startswith("-")]
    assert options == ["--no-cache-dir", "--only-binary=:all:"]       # wheels only, no build code
    pins = [w.split("==") for w in words[4:] if not w.startswith("-")]
    assert all(len(p) == 2 and re.fullmatch(r"\d+(\.\d+)*", p[1]) for p in pins), pins
    return dict(pins)


def test_the_test_job_installs_the_manifests_versions_and_no_pygobject():
    requirements = pytest.importorskip("packaging.requirements")
    version = pytest.importorskip("packaging.version")
    canonical = pytest.importorskip("packaging.utils").canonicalize_name
    pins = {canonical(name): v for name, v in ci_pins().items()}
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    # every dependency of the app and of its tests, at a version that satisfies pyproject.toml
    for dep in project["dependencies"] + project["optional-dependencies"]["test"]:
        req = requirements.Requirement(dep)
        pin = pins[canonical(req.name)]
        assert version.Version(pin) in req.specifier, (dep, pin)
    # the app's dependencies at the very versions the manifest installs in the bundle
    built = {}
    for module in MANIFEST["modules"]:
        for command in module.get("build-commands", []):
            built.update((canonical(n), v)
                         for n, v in re.findall(r"\s([A-Za-z0-9_.-]+)==([0-9.]+)\s*$", command))
    runtime = {canonical(requirements.Requirement(d).name) for d in project["dependencies"]}
    assert runtime <= set(built)
    assert {name: pins[name] for name in runtime} == {name: built[name] for name in runtime}
    # PyYAML for the tests that read the manifest and this workflow, else they are skipped
    assert "pyyaml" in pins
    # no GTK: the tests that need it are skipped (tests/conftest.py)
    assert not {"pygobject", "pycairo", "gi"} & set(pins)


def test_without_pygobject_the_gtk_tests_are_skipped_and_the_others_run(tmp_path):
    """What the test job relies on, tried here whether PyGObject is installed or not: a child
    pytest in which gi cannot be imported. A module that needs GTK is skipped, not an error, and
    so is the headless check, which needs GTK; a module that tests the GUI's logic (it imports
    opencutter.gui, but nothing of GTK) runs."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    hidden = ("DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "BROADWAY_DISPLAY", "GDK_BACKEND",
              "DBUS_SESSION_BUS_ADDRESS", "OPENCUTTER_SOCKET")
    env = {k: v for k, v in os.environ.items() if k not in hidden}
    env.update(XDG_RUNTIME_DIR=str(runtime), NO_AT_BRIDGE="1", GTK_A11Y="none")
    modules = ["tests/test_gui_window.py", "tests/test_gui_review_headless.py",
               "tests/test_gui_material_choice.py"]
    code = ("import sys, pytest; sys.modules['gi'] = None; "      # import gi: ImportError
            "sys.exit(pytest.main(sys.argv[1:]))")
    done = subprocess.run([sys.executable, "-c", code, "-q", "-p", "no:cacheprovider", "-rs",
                           *modules], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=300)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    summary = out.strip().splitlines()[-1]
    assert re.fullmatch(r"=* ?\d+ passed, 2 skipped in .*", summary), summary
    assert "error" not in summary and "failed" not in summary
    assert "SKIPPED [1] tests/conftest.py" in out and "(tests/test_gui_window.py:" in out
    assert "test_gui_review_headless.py: needs GTK 4, and PyGObject is not installed" in out


def test_the_build_runs_in_flathubs_image_of_the_manifests_runtime():
    container = JOBS["build"]["container"]
    assert MANIFEST["runtime"] == "org.gnome.Platform"
    assert container["image"] == ("ghcr.io/flathub-infra/flatpak-github-actions:gnome-"
                                  + MANIFEST["runtime-version"])
    assert container["options"] == "--privileged"            # bwrap inside the container


def test_the_bundle_is_built_from_the_manifest_with_flathub_as_runtime_repo():
    [build] = steps("build", "flatpak/flatpak-github-actions/flatpak-builder")
    args = build["with"]
    assert args["manifest-path"] == MANIFEST_PATH and (REPO / MANIFEST_PATH).is_file()
    assert args["bundle"] == f"opencutter-{VERSION}.flatpak"
    assert args["repository-name"] == "flathub" and args["repository-url"] == FLATHUB
    assert args["cache"] is False                            # from scratch, nothing cached
    # the build folders lie in the checkout, which is the manifest's dir source: it skips them
    [source] = [s for m in MANIFEST["modules"] if m["name"] == "opencutter" for s in m["sources"]]
    assert {args["build-dir"], args["repo-dir"], args["state-dir"]} <= set(source["skip"])
    # the README's remote is the same Flathub
    assert FLATHUB in (REPO / "README.md").read_text(encoding="utf-8")


def test_the_version_is_read_and_checked_before_the_build():
    build = steps("build")
    [version] = [s for s in build if s.get("id") == "version"]
    assert build.index(version) < build.index(
        steps("build", "flatpak/flatpak-github-actions/flatpak-builder")[0])
    assert 'python3 build-aux/release-version.py --tag "$REF_NAME" >> "$GITHUB_OUTPUT"' in version["run"]
    assert 'python3 build-aux/release-version.py >> "$GITHUB_OUTPUT"' in version["run"]
    assert version["env"] == {"REF_TYPE": "${{ github.ref_type }}",
                              "REF_NAME": "${{ github.ref_name }}"}
    assert JOBS["build"]["outputs"] == {"version": VERSION}
    assert (REPO / "build-aux" / "release-version.py").is_file()


def test_the_release_job_checks_the_tag_then_attaches_the_bundle_and_its_checksum():
    release = steps("release")
    names = [s.get("name") for s in release]
    check = release[names.index("Check that the tag is the version")]
    assert 'python3 build-aux/release-version.py --tag "$TAG"' in check["run"]
    assert '"$TAG" != "v$BUILT"' in check["run"]
    assert check["env"]["TAG"] == "${{ github.ref_name }}"
    [download] = steps("release", "actions/download-artifact")
    publish = release[names.index(PUBLISH)]
    assert release.index(check) < release.index(download) < release.index(publish)
    # the artifact of this run's build job, checked against the checksum written there
    [upload] = steps("build", "actions/upload-artifact")
    assert upload["with"]["name"] == f"opencutter-{VERSION}-x86_64"
    assert upload["with"]["path"].splitlines() == [f"opencutter-{VERSION}.flatpak", "SHA256SUMS"]
    assert upload["with"]["if-no-files-found"] == "error"
    assert download["with"]["name"] == "opencutter-${{ needs.build.outputs.version }}-x86_64"
    assert 'sha256sum "$BUNDLE" > SHA256SUMS' in runs("build")
    assert "sha256sum --check --strict SHA256SUMS" in runs("release")
    assert publish["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_a_new_release_is_created_with_its_files_in_one_command():
    """gh then creates a draft, uploads and publishes: a release is never public without its
    bundle, which immutable releases would make permanent."""
    script = publish_script()
    flat = re.sub(r"\s*\\\n\s*", " ", script)
    [create] = [line.strip() for line in flat.splitlines() if "gh release create" in line]
    assert create == ('gh release create "$TAG" --repo "$GITHUB_REPOSITORY" --verify-tag '
                      '--title "OpenCutter $VERSION" --notes-file notes.md "dist/$BUNDLE" '
                      'dist/SHA256SUMS')
    assert 'BUNDLE="opencutter-$VERSION.flatpak"' in script
    # the bundle is never replaced: only a SHA256SUMS whose bundle never arrived is
    uploads = [line.strip() for line in flat.splitlines() if "gh release upload" in line]
    assert uploads == ['gh release upload "$TAG" --repo "$GITHUB_REPOSITORY" "dist/$BUNDLE"',
                       'gh release upload "$TAG" --repo "$GITHUB_REPOSITORY" --clobber '
                       'dist/SHA256SUMS']
    assert script.index('grep -qxF "$BUNDLE" assets.txt') < script.index("gh release upload")


def test_the_release_notes_are_a_quoted_heredoc_with_two_placeholders():
    """The shell expands nothing in the notes (backticks, $): sed puts in the version and the
    tag, right after the heredoc and before the release is created."""
    script = publish_script()
    assert re.findall(r"<<-?\s*(\S+)", script) == ["'EOF'"]          # the one heredoc, quoted
    block = notes_block()
    body, after = block[len(NOTES_START):].split("\nEOF\n", 1)
    assert after.strip() == NOTES_SED
    assert set(re.findall(r"@[A-Z_]+@", body)) == {"@VERSION@", "@TAG@"}
    assert "$" not in body                              # a $VERSION here would reach the page as is


def test_the_rendered_release_notes_name_the_version_and_leave_nothing_unexpanded(tmp_path):
    notes = render_notes(tmp_path)
    current = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert f"opencutter-{SAMPLE}.flatpak" in notes and f"/v{SAMPLE}/" in notes
    assert current["version"] not in notes                    # nothing is left at a fixed version
    assert "$" not in notes and "EOF" not in notes and not re.search(r"@[A-Z_]+@", notes)
    # the backticks arrive as they are, in pairs; the code blocks are closed
    assert "`SHA256SUMS`" in notes and "`flatpak install`" in notes
    assert notes.count("```") % 2 == 0
    blocks = notes.strip().split("\n\n")
    for block in blocks:
        lines = block.splitlines()
        if block.startswith("```"):
            assert lines[-1] == "```", block
            continue
        assert all(line.count("`") % 2 == 0 for line in lines), block
        # one line per paragraph and per list item: GitHub could show a wrapped line as it is
        assert len(lines) == 1 or all(line.startswith("- ") for line in lines), block
    assert notes.endswith(".\n") and "\n\n\n" not in notes


def test_the_release_notes_show_the_screenshot_then_the_intro_and_the_sections(tmp_path):
    notes = render_notes(tmp_path)
    first, intro = notes.split("\n\n")[:2]
    # the main window of the tagged release, the metainfo's default screenshot
    shot = "data/screenshots/main-window.png"
    assert re.fullmatch(rf"!\[OpenCutter's main window: [^\]]+\]\({re.escape(RAW)}/v{SAMPLE}/"
                        rf"{re.escape(shot)}\)", first), first
    assert (REPO / shot).is_file()
    metainfo = ET.parse(REPO / "data" / f"{APP_ID}.metainfo.xml").getroot()
    [default] = [s for s in metainfo.iter("screenshot") if s.get("type") == "default"]
    assert default.findtext("image").endswith("/" + shot)
    # one paragraph, honest about the test status, before the sections, in this order
    assert not intro.startswith(("#", "-", "!")) and "\n" not in intro
    assert "tested on one CAMEO 4 with firmware V5.05" in intro
    assert "have not been tested on a real machine yet" in intro and "sharp blade" in intro
    assert re.findall(r"^## (.+)$", notes, re.M) == NOTES_SECTIONS
    highlights = re.findall(r"^- \*\*.+?\*\* .+$", notes_section(notes, "Highlights"), re.M)
    assert 3 <= len(highlights) <= 5
    assert len(re.findall(r"^- ", notes_section(notes, "Highlights"), re.M)) == len(highlights)
    # the notice, at the end, after a rule
    footer = notes.rsplit("\n---\n", 1)[1]
    for words in ("not affiliated with, endorsed by or supported by Silhouette America, Inc.",
                  "Silhouette, CAMEO, Portrait and CURIO are trademarks of their respective "
                  "owners.", "GNU General Public License, version 3 or later"):
        assert words in footer, words


def test_the_release_notes_list_the_machines_of_the_registry(tmp_path):
    """Tested, not yet tested and recognized, as the registry has them, and no claim of a
    hardware test beyond the tested models."""
    notes = render_notes(tmp_path)
    found = dict(re.findall(r"^- \*\*(.+?):\*\* (.+)$", notes_section(notes, "Machines"), re.M))
    assert list(found) == ["Tested on a real machine", "Not yet tested",
                           "Recognized, not supported yet"]
    tested, untested, recognized = found.values()
    for p in registry.tested():
        for line in p.identity.lines:
            words = f"the {p.name} with firmware V{line.major}.xx"
            assert words in (tested if line.support is Support.TESTED else untested), words
    assert {n for n in MODEL_NAMES if mentions(tested, n)} == {p.name for p in registry.tested()}
    for p in registry.profiles():
        if p.support is Support.UNTESTED:
            assert mentions(untested, p.name) and not mentions(recognized, p.name), p.id
    for model in registry.recognized_models():
        assert not mentions(tested, model.name) and not mentions(untested, model.name), model.id
        if model.identity.ble_names:                   # the ones a Bluetooth user may own
            assert mentions(recognized, model.name), model.id
        else:
            assert "USB-only" in recognized, model.id
    claims = re.findall(r"(?<![Nn]ot )(?<![Nn]ot yet )(?<!not been )(?<!never been )"
                        r"(?<!not yet been )\b(?:[Tt]ested|[Vv]erified|[Ww]orks?|[Ww]orked|[Cc]ut)"
                        r"(?: [a-z]+){0,3}? (?:on|with) ([^.;]*)", flat_text(notes))
    claimed = {n for claim in claims for n in MODEL_NAMES if mentions(claim, n)}
    assert claimed == {p.name for p in registry.tested()}, claimed


def test_the_release_notes_install_as_the_readme_does(tmp_path):
    """The README's download, check and install commands for this version, Flathub as the
    remote of the manifest's runtime, and the remote-add command of the README."""
    notes = render_notes(tmp_path)
    install = notes_section(notes, "Install")
    assert "64-bit PC (x86_64)" in install
    commands = re.findall(r"^```sh\n(.*?)^```$", install, re.M | re.S)[0]
    readme_version = re.search(r"opencutter-([0-9.]+)\.flatpak", "\n".join(
        readme_flatpak_commands()))[1]
    expected = [flat_text(line.replace(readme_version, SAMPLE))
                for line in readme_flatpak_commands()]
    assert [flat_text(line) for line in commands.splitlines()] == expected
    assert f"flatpak install --user ./opencutter-{SAMPLE}.flatpak" in expected
    assert f"sha256sum --check SHA256SUMS # prints: opencutter-{SAMPLE}.flatpak: OK" in expected
    # the runtime is the manifest's, from Flathub; flatpak install adds the remote itself
    assert f"GNOME {MANIFEST['runtime-version']} runtime from Flathub" in install
    assert "without the Flathub remote it adds it" in install
    remote_add = f"flatpak remote-add --user --if-not-exists flathub {FLATHUB}\n"
    assert remote_add in install and remote_add in (REPO / "README.md").read_text(encoding="utf-8")


def test_the_release_notes_link_only_to_the_project_at_its_tag(tmp_path):
    """Every link leads to the project's files at the released tag (to a heading that exists),
    its model report form, the release's own two files, Flathub or Flatpak's setup page."""
    notes = render_notes(tmp_path)
    urls = re.findall(r"https?://[^\s)\]`]+", notes)
    allowed = {f"{RAW}/v{SAMPLE}/data/screenshots/main-window.png", MODEL_REPORT,
               f"{PROJECT}/releases/download/v{SAMPLE}/opencutter-{SAMPLE}.flatpak",
               f"{PROJECT}/releases/download/v{SAMPLE}/SHA256SUMS", FLATHUB,
               "https://flatpak.org/setup/"}
    blob = re.compile(rf"{re.escape(PROJECT)}/blob/v{SAMPLE}/(README|CONTRIBUTING)\.md#([a-z-]+)")
    for url in urls:
        m = blob.fullmatch(url)
        if m:
            assert m[2] in anchors(REPO / f"{m[1]}.md"), url
        else:
            assert url in allowed, url
    # the sections point to the README's and CONTRIBUTING's own words on them
    limits = notes_section(notes, "Known limitations")
    assert f"{PROJECT}/blob/v{SAMPLE}/README.md#known-limitations)" in limits
    report = notes_section(notes, "Report a model")
    assert f"({MODEL_REPORT})" in report
    form = REPO / ".github" / "ISSUE_TEMPLATE" / MODEL_REPORT.rsplit("=", 1)[1]
    assert yaml.safe_load(form.read_text(encoding="utf-8"))["name"] == "Model report"
    assert f"{PROJECT}/blob/v{SAMPLE}/CONTRIBUTING.md#reporting-a-model)" in report
    assert "serial number" in report and "Bluetooth address" in report


def test_the_known_limitations_of_the_release_notes_are_the_readmes(tmp_path):
    """Each limitation the notes name is still one in the README: when one is solved there, the
    notes must drop it too."""
    limits = flat_text(notes_section(render_notes(tmp_path), "Known limitations"))
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    readme = flat_text(re.sub(r"^> ?", "", readme, flags=re.M))        # the status is a quote
    for words in ("stopping a running cut with Cancel Job", "has not been measured",
                  "Print & Cut", "matless cutting", "a USB connection",
                  "every mat but the 12 × 12 in", "Background Apps", "`flatpak kill`",
                  "**Disconnect**", "The bundle is not signed", "`sha256sum --check`"):
        assert words.lower() in limits.lower(), words
        assert words.lower() in readme.lower(), words


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("assets, expected", [
    (None, [["release", "view"], ["release", "create", "v0.1.0", "--repo", "MortyNiners/opencutter",
                                  "--verify-tag", "--title", "OpenCutter 0.1.0", "--notes-file",
                                  "notes.md", "dist/opencutter-0.1.0.flatpak", "dist/SHA256SUMS"]]),
    ([], [["release", "view"],
          ["release", "upload", "v0.1.0", "--repo", "MortyNiners/opencutter",
           "dist/opencutter-0.1.0.flatpak"],
          ["release", "upload", "v0.1.0", "--repo", "MortyNiners/opencutter", "--clobber",
           "dist/SHA256SUMS"]]),
    (["SHA256SUMS"], [["release", "view"],
                      ["release", "upload", "v0.1.0", "--repo", "MortyNiners/opencutter",
                       "dist/opencutter-0.1.0.flatpak"],
                      ["release", "upload", "v0.1.0", "--repo", "MortyNiners/opencutter",
                       "--clobber", "dist/SHA256SUMS"]]),
    (["opencutter-0.1.0.flatpak", "SHA256SUMS"], [["release", "view"]]),
], ids=["no-release", "draft-made-by-hand", "upload-broke-off", "already-released"])
def test_the_release_script_with_a_stand_in_gh(tmp_path, assets, expected):
    """The publish step's script, run with a stand-in gh that only records its arguments: what
    it does for a tag without a release, for a release made by hand, for one whose upload broke
    off before the bundle arrived, and for one that has its bundle (nothing is replaced)."""
    script = publish_script()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/bash\n"
                  'printf "%s\\0" "$@" >> "$GH_LOG"; printf "\\n" >> "$GH_LOG"\n'
                  'if [ "$1 $2" = "release view" ]; then\n'
                  '  [ -f "$GH_ASSETS" ] || { echo "release not found" >&2; exit 1; }\n'
                  '  cat "$GH_ASSETS"\n'
                  "fi\n")
    gh.chmod(0o755)
    (tmp_path / "dist").mkdir()
    if assets is not None:
        (tmp_path / "assets-of-release").write_text("".join(a + "\n" for a in assets))
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "GH_LOG": str(tmp_path / "gh.log"),
           "GH_ASSETS": str(tmp_path / "assets-of-release"), "GH_TOKEN": "x", "TAG": "v0.1.0",
           "VERSION": "0.1.0", "GITHUB_REPOSITORY": "MortyNiners/opencutter"}
    done = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    calls = [line.split("\0")[:-1] for line in (tmp_path / "gh.log").read_text().splitlines()]
    assert [c[:2] if c[:2] == ["release", "view"] else c for c in calls] == expected
    if assets is None:
        notes = (tmp_path / "notes.md").read_text(encoding="utf-8")
        assert notes.startswith("![OpenCutter's main window: ")
        assert f"]({RAW}/v0.1.0/data/screenshots/main-window.png)\n" in notes
        assert "\nflatpak install --user ./opencutter-0.1.0.flatpak\n" in notes
        assert "$" not in notes and not re.search(r"@[A-Z_]+@", notes)
    elif "opencutter-0.1.0.flatpak" in assets:
        assert "it is not replaced" in done.stdout


def test_no_expression_is_pasted_into_a_shell_script():
    """Values reach the scripts through env, never as ${{ }} inside run (script injection)."""
    for job in JOBS:
        for step in steps(job):
            assert "${{" not in step.get("run", ""), (job, step.get("name"))


def test_every_step_has_a_name_and_jobs_have_time_limits():
    for job in JOBS.values():
        assert 0 < job["timeout-minutes"] <= 60
        assert all(step.get("name") for step in job["steps"])
