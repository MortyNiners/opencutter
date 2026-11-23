# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 OpenCutter contributors
"""opencutter.identity: the version, the build hash of the package's sources and the contract
version that the background service reports and the app compares. Pure: no daemon, no display."""
import hashlib
import os
import shutil
from pathlib import Path

from opencutter import __version__
from opencutter import identity as ident

PACKAGE = Path(ident.__file__).resolve().parent


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "opencutter"
    shutil.copytree(PACKAGE, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def test_build_is_the_hash_of_the_sources_and_stable(tmp_path):
    assert len(ident.BUILD) == ident.BUILD_CHARS == 12 and int(ident.BUILD, 16) >= 0
    assert ident.build_hash(str(PACKAGE)) == ident.BUILD          # computed once, at import
    copy = _copy(tmp_path)
    assert ident.build_hash(str(copy)) == ident.BUILD             # the place does not count


def test_build_changes_with_any_source_change(tmp_path):
    copy = _copy(tmp_path)
    module = copy / "job" / "validate.py"
    module.write_bytes(module.read_bytes() + b"\n# a fix without a version bump\n")
    changed = ident.build_hash(str(copy))
    assert changed != ident.BUILD
    (copy / "job" / "validate.py").rename(copy / "job" / "validate2.py")     # a renamed file
    assert ident.build_hash(str(copy)) not in (ident.BUILD, changed)
    copy2 = _copy(tmp_path / "b")
    (copy2 / "device" / "extra.py").write_text("x = 1\n")                 # a new module
    assert ident.build_hash(str(copy2)) != ident.BUILD


def test_build_ignores_caches_and_other_files(tmp_path):
    copy = _copy(tmp_path)
    cache = copy / "device" / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "client.cpython-314.pyc").write_bytes(b"\0compiled")
    (copy / "gui" / "notes.txt").write_text("not a source")
    (copy / "gui" / "style.css").write_text("/* changed */")
    assert ident.build_hash(str(copy)) == ident.BUILD


def test_build_ignores_editor_lock_files_and_hidden_names(tmp_path):
    """An editor's lock file next to a module (Emacs's '.#window.py' is a dangling symlink),
    a hidden backup or a hidden directory must not change the build: the app and the service
    would then disagree about the build of one checkout."""
    copy = _copy(tmp_path)
    os.symlink("user@host.1234:1700000000", copy / "gui" / ".#window.py")
    (copy / "gui" / ".window.py").write_text("# a hidden backup\n")
    (copy / ".hidden").mkdir()
    (copy / ".hidden" / "x.py").write_text("x = 1\n")
    os.symlink(copy / "nowhere.py", copy / "device" / "dangling.py")
    assert ident.build_hash(str(copy)) == ident.BUILD


def test_identity_and_same_build():
    me = ident.identity()
    assert me == {"version": __version__, "build": ident.BUILD, "api": ident.API_VERSION}
    assert ident.API_VERSION == 2
    ping = {"ok": True, "connected": True, **me, "pid": os.getpid()}
    assert ident.same_build(ping)
    assert not ident.same_build({**ping, "build": "0123456789ab"})
    assert not ident.same_build({**ping, "api": 3})
    assert not ident.same_build({**ping, "api": True})            # not an int
    older = {k: v for k, v in ping.items() if k not in ("version", "build", "api", "pid")}
    assert not ident.same_build(older) and not ident.same_build(None)


def test_describe():
    assert ident.describe(ident.identity()) == f"OpenCutter {__version__} (build {ident.BUILD})"
    assert ident.this_build() == ident.describe(ident.identity())
    assert ident.describe({"ok": True}) == ("an older OpenCutter background service (it reports "
                                            "no version)")
    assert ident.describe(None).startswith("an older")


def test_the_version_counts_and_an_install_without_sources_hashes_its_compiled_files(
        tmp_path, monkeypatch):
    """Without any .py file (a sourceless install, a zip or a frozen bundle) two versions must
    not share one build: the version is part of the hash, and .pyc files stand in for the
    sources."""
    empty = tmp_path / "empty"
    empty.mkdir()
    nothing = hashlib.sha256(b"").hexdigest()[:ident.BUILD_CHARS]
    assert ident.build_hash(str(empty)) != nothing
    sourceless = tmp_path / "sourceless"
    (sourceless / "job").mkdir(parents=True)
    (sourceless / "__init__.pyc").write_bytes(b"\0compiled init")
    (sourceless / "job" / "plan.pyc").write_bytes(b"\0compiled plan")
    before = ident.build_hash(str(sourceless))
    assert before != ident.build_hash(str(empty))
    (sourceless / "job" / "plan.pyc").write_bytes(b"\0compiled plan, fixed")
    changed = ident.build_hash(str(sourceless))
    assert changed != before
    monkeypatch.setattr(ident, "__version__", "9.9.9")
    assert ident.build_hash(str(empty)) != nothing
    assert ident.build_hash(str(sourceless)) != changed
    assert ident.build_hash(str(PACKAGE)) != ident.BUILD             # another version


def test_build_on_disk_sees_a_change_after_the_start(tmp_path, monkeypatch):
    assert ident.build_on_disk() == ident.BUILD                      # nothing changed since
    copy = _copy(tmp_path)
    monkeypatch.setattr(ident, "PACKAGE_DIR", str(copy))
    assert ident.build_on_disk() == ident.BUILD
    (copy / "gui" / "sidebar.py").write_text("# edited while the app was open\n")
    assert ident.build_on_disk() != ident.BUILD
