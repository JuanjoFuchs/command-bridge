---
title: Invocation, settings, voice rate, and the fresh-install check
description: The three install channels (PyPI, install script, Windows zip — no npm, no WinGet), how to invoke the tool from any cwd, where settings persist, how speed and pause are tuned, and how to test what a fresh install experiences without publishing.
applies_to: bin/, command_bridge/config.py, .env, scripts/coldstart.py
read_before: an invocation failure, changing a setting or the voice rate, or changing doctor / setup / describe / remedies / extras
---

# Invocation, settings, voice rate, and the fresh-install check

## Distribution channels — three, and only three

**PyPI** (`pipx install "command-bridge[all]"` / `uv tool install "command-bridge[all]"`), the
**install script** straight from the repository (`install.ps1` / `install.sh`, which clone and build
a private venv beside the checkout), and the **Windows `--onedir` bundle** as a zip on each GitHub
Release. `release.yml` produces all of it from one tag: PyPI publish, the GitHub Release with wheel,
sdist and zip attached.

**There is no npm and no WinGet channel** — both dropped 2026-10-14 (spec
`specs/voice-tunnel/003-npm-distribution.md` is superseded; spec 002 carries the note). Do not
re-add either to a README, a remedy or `describe`; the old `voice-tunnel` names on those registries
predate the rename and are not this tool.

## Invoking the tool — `command-bridge doctor`

For `command-bridge` not found, "which python", a missing dependency, anything that made you reach
for `python -c`. Every failing check carries the command that fixes it. **Never invoke this as
`python -c "import sys; sys.path.insert(...)"`** — `bin/command-bridge` (bash) and
`bin/command-bridge.cmd` (PowerShell/cmd) resolve the repo root and the venv from any cwd.

## Settings — `command-bridge config show`

TTS backend, piper/kokoro paths, where turn logs live, ASR engine. Settings persist in a `.env`
loaded by every command (`command-bridge config path` says where — repo-local in a checkout, the
user config dir once installed). `command-bridge config set COMMAND_BRIDGE_TTS piper` **once**, not
four env-var prefixes per call. Process env still overrides the file. Backends are
`sapi | piper | kokoro | none` — **kokoro is a NAME not a path**
(`COMMAND_BRIDGE_KOKORO_VOICE=bm_daniel`), because one pack holds all 54 voices, where a piper voice
IS a file.

## How the agent SOUNDS — `command-bridge rate --speed <n>` / `--pause <s>`

"Talk faster", "slow down", a list that ran together. Applies immediately AND persists, because these
are tuned by ear mid-conversation and used to be lost on every restart. **Speed is a MULTIPLE —
higher is faster.** Piper's inverted `length_scale` is not exposed anywhere above
`config.length_scale_for`; leaking it once produced half speed when the owner asked for double.
**Kokoro's own ceiling is 2.0, not the 2.5 `rate` accepts** — `kokoro_onnx` asserts it, so the value
is clamped at that backend's boundary (never in `SPEED_MAX`; piper handles 2.5 and he uses it) and
`status` / `doctor` say when they clamped.

## What a FRESH INSTALL experiences — `python scripts/coldstart.py --verify --brief`

For `doctor`, `setup`, `describe`, remedies, extras, or anything that reads differently from a wheel
than from a checkout. Builds the wheel from the working tree (uncommitted edits included) and installs
it into a throwaway virtualenv scoped by `COMMAND_BRIDGE_HOME`, then prints a paste-ready prompt for a
blind agent. **Never publish in order to test.** 0.2.1 through 0.2.4 were each found by handing an
agent a fresh install, and that loop ran through PyPI — four public releases in ninety minutes that
were really four test iterations, on an index with no unpublish. An editable install cannot
substitute: it resolves back to the checkout, so every path question answers as the checkout, which
is exactly where those four bugs lived.
