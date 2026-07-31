"""The PATH shims — the layer that decides whether `vm` is a command or a ritual.

These run the shims as real subprocesses from an unrelated cwd, because every bug they guard
against is invisible in-process: an import that only works from the repo root, a batch file that
executes fragments of its own source, a launcher that resolves the wrong directory. All three
happened; none would be caught by importing vm.cli and calling main().
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

from vm import config

BIN = os.path.join(config.ROOT, "bin")


def test_the_launcher_runs_from_an_unrelated_cwd(tmp_path):
    """The whole point. `cwd=tmp_path` is what breaks a naive `python -m vm.cli`, and it is the
    situation an agent is always in — it invokes the tool from wherever its own turn is standing.
    """
    proc = subprocess.run(
        [sys.executable, os.path.join(BIN, "vm-run.py"), "describe"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["tool"] == "voice-mode"


def test_the_launcher_needs_no_environment(tmp_path):
    """No VM_* variables, no PYTHONPATH, no activated venv: the four env-var prefixes this whole
    change exists to delete."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VM_", "PYTHONPATH"))}
    env["VM_ENV_FILE"] = str(tmp_path / "absent.env")

    proc = subprocess.run(
        [sys.executable, os.path.join(BIN, "vm-run.py"), "config", "path"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["file"].endswith(".env")


def test_the_launcher_puts_this_checkout_first_on_sys_path():
    """A shim named after a directory must mean that directory, even if another copy of the
    package is installed in the interpreter's site-packages."""
    source = open(os.path.join(BIN, "vm-run.py"), "r", encoding="utf-8").read()
    assert "sys.path.insert(0" in source


def _git_bash():
    """The bash `bin/vm` actually targets: Git Bash / MSYS, where `cygpath` exists.

    Explicitly NOT WSL's bash. On Windows `shutil.which("bash")` regularly resolves to
    C:\\Windows\\System32\\bash.exe, which is a Linux environment with a different filesystem
    view — it cannot even open `D:\\...\\bin\\vm` and exits 127. Handing that to this test made
    it fail on a perfectly good shim, purely as a function of PATH order.
    """
    if os.name != "nt":
        return shutil.which("bash")
    candidates = []
    git = shutil.which("git")
    if git:  # Git for Windows ships bash a sibling directory over from git.exe
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        candidates.append(found)
    return next((c for c in candidates if os.path.isfile(c)), None)


@pytest.mark.skipif(_git_bash() is None, reason="no Git Bash on this machine (WSL bash is not it)")
def test_the_bash_shim_works_from_an_unrelated_cwd(tmp_path):
    proc = subprocess.run(
        [_git_bash(), os.path.join(BIN, "vm"), "describe"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["tool"] == "voice-mode"


@pytest.mark.skipif(os.name != "nt", reason="cmd shim is Windows-only")
def test_the_cmd_shim_works_from_an_unrelated_cwd(tmp_path):
    proc = subprocess.run(
        [os.path.join(BIN, "vm.cmd"), "describe"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120, shell=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["tool"] == "voice-mode"


@pytest.mark.skipif(os.name != "nt", reason="cmd shim is Windows-only")
def test_the_cmd_shim_is_ascii_with_crlf():
    """cmd.exe parses a batch file by BYTE OFFSET. One multi-byte character anywhere -- an
    em-dash inside a *comment* was enough -- shifts every following line, and the shim begins
    executing fragments of its own source: `setlocal` arrived as `tlocal`, and the tool reported
    that it could not find its own repo. Nothing about the file looks wrong when you read it,
    which is exactly why this needs to be a test and not a note.
    """
    raw = open(os.path.join(BIN, "vm.cmd"), "rb").read()

    assert not raw.startswith(b"\xef\xbb\xbf"), "a UTF-8 BOM confuses cmd.exe"
    non_ascii = [b for b in raw if b > 0x7F]
    assert not non_ascii, f"vm.cmd must be pure ASCII; found {len(non_ascii)} high bytes"
    assert b"\r\n" in raw, "vm.cmd must use CRLF line endings"


def test_the_bash_shim_has_no_carriage_returns():
    """`#!/usr/bin/env bash\\r` makes the kernel hunt for an interpreter literally named
    "bash\\r", and the shim dies with "bad interpreter: no such file or directory" on a machine
    where nothing is missing. The repo is developed on Windows with core.autocrlf=true, so this
    is one `git clone` away at all times -- .gitattributes pins it, and this proves the pin.
    """
    raw = open(os.path.join(BIN, "vm"), "rb").read()
    assert b"\r" not in raw, "bin/vm must be LF-only or its shebang breaks"


def test_line_endings_are_pinned_for_both_shims():
    attributes = open(os.path.join(config.ROOT, ".gitattributes"), "r", encoding="utf-8").read()
    assert "bin/vm" in attributes and "eol=lf" in attributes
    assert "*.cmd" in attributes and "eol=crlf" in attributes


def test_no_shim_invokes_python_dash_c():
    """The smell this change removes. `python -c "import sys; sys.path.insert(...)"` had to
    splice a path into a raw string, so a root ending in a backslash closed the string early and
    produced a SyntaxError instead of a CLI."""
    for name in ("vm", "vm.cmd"):
        source = open(os.path.join(BIN, name), "r", encoding="utf-8").read()
        assert "-c " not in source, f"{name} still invokes python with an inline program"
