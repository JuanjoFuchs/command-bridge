---
id: "001"
title: Package and CLI identity — the rename to Command Bridge
status: pending
blocked_by: []
blocks: ["002", "003"]
---

# Package and CLI identity — the rename to Command Bridge

## Overview

Command Bridge is a fork of voice-tunnel: a 150-commit clone whose ~53 unpushed commits (lanes +
canvas) become the new product, while the published voice-tunnel is frozen. But the clone is still
**byte-identical** — the package is `voice_tunnel/` at version 0.2.7, the CLI is `voice-tunnel`, and
the 40 settings are prefixed `VOICE_TUNNEL_`. This spec renames the identity so everything in the
repo is Command Bridge, and it goes **first** because every later spec (the screenshot harness, the
server merge, the UI) is built and tested against the renamed surface.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes. **The suite is the gate: ~847 tests must stay green through the rename.**

## What he said, verbatim

> *"In this new repo everything should be called Command Bridge and not Voice Tunnel."*
>
> (2026-09-01, voice-dictated; turn 4496 of `voice-tunnel/sessions/dev.jsonl` — that log is
> gitignored, so the quote carries the weight and not the id.)

## Findings — what the distillation says the rename touches

From `distill/voice-tunnel.md`, grounded in the code:

- The Python package `voice_tunnel/` — the import root used across the server, CLI, and ~63 test
  files.
- `cli.py::build_parser()` and its 21-entry `handlers` dict; the console-script entry point and the
  `bin/` shim both spell `voice-tunnel`.
- `pyproject.toml` — `name = "voice-tunnel"`, version `0.2.7`, the `[project.scripts]` entry, and any
  packaging metadata.
- **40 settings** read as `VOICE_TUNNEL_*`, registered in `config.SETTINGS` (the one registry behind
  `describe`, `config show`, and the `.env.example` drift test), plus 2 read-but-unregistered
  (`VOICE_TUNNEL_HOME`, legacy `VOICE_TUNNEL_PIPER_LENGTH_SCALE`).
- User-visible branding: `describe`/`doctor` output, `--help` text, the web page `<title>`, README,
  AGENTS.md.

🔴 **The live `.env` is the trap.** The running session's token is `VOICE_TUNNEL_TOKEN`, pinned in
`.env` so it survives restart. A hard prefix swap silently invalidates every value a live operator
already set — the token, the pinned voice, the speed. **The rename must not break a `.env` that
predates it.**

## Goals

- **Everything in the repo reads as Command Bridge** — package, CLI, settings prefix, branding.
- **The test suite stays green** — a rename that breaks tests is not done; the ~847 tests are the
  proof the behaviour is unchanged.
- **A pre-existing `.env` keeps working** — the operator's pinned token, voice, and speed survive.

## Requirements

### Functional

- **FR1** — The import root is `command_bridge/`; no module imports `voice_tunnel`.
- **FR2** — The CLI is `command-bridge`; the console-script entry and the `bin/` shim invoke it.
- **FR3** — Settings resolve under the `COMMAND_BRIDGE_*` prefix, registered in `config.SETTINGS`,
  surfaced by `describe` and `config show`, and documented in `.env.example`.
- **FR4** — **Back-compat: a `VOICE_TUNNEL_*` value is still honored** when its `COMMAND_BRIDGE_*`
  equivalent is unset, with a one-time deprecation note. So a live `.env` (the pinned
  `VOICE_TUNNEL_TOKEN` included) keeps working.
- **FR5** — User-visible branding (`describe`/`doctor`/`--help`, the page `<title>`, README,
  AGENTS.md) says Command Bridge.

### Non-functional

- **NFR1** — **Behaviour is byte-unchanged.** This is a rename, not a refactor; the only observable
  difference is the name. The suite passing is the evidence.
- **NFR2** — The version resets to a Command Bridge `0.x` line (its own release history), not
  voice-tunnel's `0.2.7`.

### Technical constraints

- **TC1** — **The published voice-tunnel is not touched.** The rename lives only in `command-bridge`;
  the frozen remote keeps `voice_tunnel/`.
- **TC2** — Settings resolution stays a single source of truth: one resolver reads
  `COMMAND_BRIDGE_*` then falls back to `VOICE_TUNNEL_*`, so no call site learns both prefixes.
- **TC3** — **The verification environment is command-bridge's own venv.** The repo was cloned
  code-only (no venv, no models), so creating that virtual environment and installing dependencies —
  enough that `python -m pytest tests/` runs in-repo — is the first step of this spec; every AC below
  is verified in it.
- **TC4** — **Use the available tooling for the mechanical work:** `ast-grep` (0.45) for the
  structural moves and the import/prefix codemods, `pyright` (1.1.410) as the type-check safety net
  after each edit. A rename touching this many call sites is where a structural tool earns its place
  over hand-editing.

## Implementation Tasks

- [ ] **First:** create command-bridge's own virtual environment and install dependencies so
      `python -m pytest tests/` runs in-repo (the clone was code-only) — the verification environment
      for every AC (TC3).
- [ ] Rename `voice_tunnel/` → `command_bridge/`; update every import — drive it with `ast-grep`, not
      by hand (TC4), then run the suite.
- [ ] `pyproject.toml`: name, version reset, `[project.scripts]` entry; update the `bin/` shim.
- [ ] Introduce the `COMMAND_BRIDGE_*` prefix in the settings resolver with `VOICE_TUNNEL_*`
      fallback (FR4); re-key `config.SETTINGS`; regenerate `.env.example`.
- [ ] Sweep user-visible strings (describe/doctor/help/page title/README/AGENTS.md).
- [ ] Update tests that import `voice_tunnel` or assert the old prefix/branding; keep the suite green.

## Acceptance Criteria

- [ ] **AC-1** `command:python -m pytest tests/` — **NFR1.** The full suite passes after the rename
      (the ~847 tests are the behaviour-unchanged proof).
- [ ] **AC-2** `unit:tests/test_config_*.py` — **FR3/FR4.** `COMMAND_BRIDGE_TOKEN` resolves; with it
      unset and `VOICE_TUNNEL_TOKEN` set, the old value is still honored with a deprecation note.
- [ ] **AC-3** `command:command-bridge describe` — **FR2/FR5.** The renamed CLI runs and its contract
      names Command Bridge; `grep -ri voice_tunnel command_bridge/` returns nothing (FR1).
- [ ] **AC-4** `manual` — **FR4.** A `.env` carrying only `VOICE_TUNNEL_*` values (a real
      pre-rename file) still starts a working server with the pinned token/voice/speed.

## Testing Approach

### Validation Steps

1. Run the full suite before and after; the delta is import/prefix/branding edits only.
2. Start the server from a `VOICE_TUNNEL_*`-only `.env` and confirm the token authenticates.

### Test Cases

| Situation | Expected |
|---|---|
| `COMMAND_BRIDGE_TOKEN` set | used |
| only `VOICE_TUNNEL_TOKEN` set | used, deprecation note once |
| both set | `COMMAND_BRIDGE_*` wins |
| `import voice_tunnel` anywhere | none exist |

## Out of Scope

- **Rewriting the inherited voice-tunnel specs.** They describe features that are now Command Bridge;
  they were shelved under `specs/voice-tunnel/` so Command Bridge's own sequence starts at 001, and
  they are not renumbered or rewritten.
- **Publishing under the new name** — that is spec 010, after the merge is proven.

## References

- `distill/voice-tunnel.md` — the code-grounded inventory this spec's touch-list comes from.
- `specs/010-every-setting-the-code-reads-is-a-setting.md` — the registry rule the new prefix must
  keep satisfying.
