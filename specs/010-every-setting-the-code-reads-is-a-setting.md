---
id: "010"
title: Every setting the code reads is a setting the CLI knows about
status: complete
blocked_by: []
blocks: []
---

# Every setting the code reads is a setting

> **Refined from the strategist's metaspec by the repo implementer, 2026-08-19.** The metaspec's goals, constraints and decisions are preserved. The instance list has been re-measured and is different from the one it carried — see [Measured ground truth](#measured-ground-truth-2026-08-19).

## Overview

Environment variables are read by the running code and are unknown to the CLI that is supposed to
describe it. `config get` calls each one an unknown setting; `describe` does not list them;
`.env.example` does not mention them. One of them — `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S` — is in
the live `.env` right now, hand-written with a comment, because the only way to set it was to edit
the file directly.

This is the same defect that had just been fixed for the Kokoro keys, found again the moment
someone looked. Fixing the instances without fixing the class means finding it a third time.

> **Completion rule:** This spec is not complete until every acceptance criterion is verified by the
> method named on it. Build-only verification is insufficient. The agent must iterate until
> verification passes.

## Measured ground truth (2026-08-19)

The metaspec named six instances. **Re-derived from the source rather than cited, and it is five** —
one of the six is not read by the code at all, and one name the metaspec did not mention is read and
unregistered but is a deliberate, already-documented exclusion.

Method: parse every `.py` under `voice_tunnel/` to an AST and collect the string-literal first
argument of every `_env(...)`, `os.getenv(...)`, `os.environ.get(...)` and `os.environ[...]`, then
diff against `config.SETTINGS`. AST rather than regex, because a regex over string literals cannot
tell a *read* from a *mention* — and this repo has docstrings that name retired variables in prose.

| Name | Read by the code? | In `SETTINGS`? | Verdict |
|---|---|---|---|
| `VOICE_TUNNEL_BARGE_IN` | yes — `config.py` | no | **instance — register** |
| `VOICE_TUNNEL_BARGE_IN_THRESHOLD` | yes — `config.py` | no | **instance — register** |
| `VOICE_TUNNEL_CONSONANT_BOOST` | yes — `config.py` | no | **instance — register** |
| `VOICE_TUNNEL_WATCH_MAX_S` | yes — `cli.py` | no | **instance — register** |
| `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S` | yes — `cli.py` | no | **instance — register** |
| `VOICE_TUNNEL_WAKE_BARE` | **no** | no | **not an instance** — see below |
| `VOICE_TUNNEL_PIPER_LENGTH_SCALE` | yes — `config.py` | no | excluded: retired legacy key (TC1) |
| `VOICE_TUNNEL_HOME` | yes — `config.py`, and named in `describe` | no | excluded: process-environment only (TC4) |

**`VOICE_TUNNEL_WAKE_BARE` is not an instance and must not be registered.** The name survives in two
places and neither is a read: a `config.py` docstring recording that the per-name bare-wake opt-in
was *deleted*, and a test that sets it precisely to prove it now does nothing. Registering it would
publish a setting that cannot be honoured — the exact inverse of this spec's goal, and worse, because
a documented knob that silently does nothing is less discoverable-as-broken than an undocumented one.

**`VOICE_TUNNEL_HOME` is read, unregistered, and correctly so.** It selects where the settings file
itself lives, so a value stored in that file could never be read in time to matter, and `describe`
already documents it under `env_process_only`. It is a second legitimate non-instance the metaspec did
not anticipate, which means the guard needs **two** exclusion routes, not the one TC1 contemplated.
(`describe` also claims `config set` refuses this key. It does not — see TC4.)

**Non-literal reads exist and are legitimate.** Eight call sites read the environment through a
variable rather than a literal — the `_env(name)` helper itself, `config get`/`config set` resolving
`args.key`, and the `effective()` walk over `SETTINGS`. These are the generic plumbing that makes the
registry work; a guard that fails on them would be disabled within a day (TC3).

## Goals

- Every variable the code reads is discoverable, readable and settable through the CLI.
- A future backend cannot ship with settings the tool cannot describe, because the build refuses.
- The registry stays the single place a setting is declared, so one entry serves `describe`, `config`,
  and the `.env.example` drift check.

## Requirements

### Functional Requirements

- **FR1**: The **five measured instances** are registered in `config.SETTINGS`, each with a `what`
  description that says what it does and what a sensible value looks like — the same standard the
  existing entries meet.
- **FR2**: A **guard fails the build when the code reads a setting the registry does not declare.** It
  walks the source rather than comparing two hand-maintained lists. *Rationale: this is the requirement
  that stops a fourth occurrence. A table that can only be wrong when someone forgets to update it
  cannot catch someone forgetting to update it.*
- **FR3**: The guard **names the offending variable and the file and line that reads it**, so the fix is
  obvious from the failure. *Rationale: a guard whose message requires an investigation gets suppressed.*
- **FR4**: The guard has a **known-good negative control** — it must be shown to fire on a constructed
  unregistered read, not merely to pass on the current tree, and that demonstration runs on every suite
  run rather than being a one-off someone performed once. *Rationale: this repo has shipped a denylist
  that refused nothing and three diagnostics that never populated; a green check is a hypothesis until
  it has been seen to fail.*
- **FR5**: A read is **classified into exactly one of four buckets**, and a name that falls in none of
  them fails:
  1. registered in `config.SETTINGS`;
  2. declared in `describe`'s `env_process_only` block (a setting that cannot live in the settings file);
  3. named in a **single explicitly-reasoned exclusion table**, one line of reason per entry;
  4. not in this tool's `VOICE_TUNNEL_*` namespace at all (`LOCALAPPDATA`, `XDG_*`).

  *Rationale: the metaspec anticipated one exclusion and there are two, so the exclusion route has to be
  a declared category rather than a special case. Deriving bucket 2 from the `describe` payload rather
  than re-listing it keeps the "one declaration, many consumers" property the registry already has.*
- **FR6**: `.env.example` documents the five new settings, so the existing drift test
  (`test_env_example_documents_every_setting`) passes without being weakened.

### Non-Functional Requirements

- **NFR1**: No behaviour change to any of the five settings. This spec makes them visible; it does not
  retune them.
- **NFR2**: The guard runs inside the existing pytest suite and adds no new tool, dependency or CI step.
  *Rationale: the repo's build gate is `python -m pytest tests/`; a guard that lives anywhere else is a
  guard that does not gate the build.*

### Technical Constraints

- **TC1**: **`VOICE_TUNNEL_PIPER_LENGTH_SCALE` must stay excluded**, and the exclusion must be explicit
  and reasoned in one place — not a silent omission that reads like an oversight to the next person.
- **TC2**: `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S` is **set in the live `.env` today at `540`**.
  Registering it must not change its current effective value, or the running configuration shifts under
  him as a side effect of documentation work. Its resolver must return the env/file value when set and
  the module constant (`28800.0`) when not.
- **TC3**: The guard walks source, so it must tolerate the legitimate ways a name can be constructed
  without producing false failures that get it disabled: a **non-literal** read is not a failure, and a
  name appearing only in **prose, a docstring, a comment or a dict key** is not a read.
- **TC4**: **`VOICE_TUNNEL_HOME` must stay excluded.** It is documented in `describe.env_process_only`;
  that block is the declaration, and the guard reads it rather than duplicating it.

  ⚠ **This constraint originally read "and must remain refused by `config set`". That was measured and
  is false.** `config set VOICE_TUNNEL_HOME <path>` succeeds today and writes the key into the settings
  file, where it can never be honoured — the key matches the writable-namespace pattern and nothing
  else checks it. `describe`'s own note on `config get VOICE_TUNNEL_HOME` states that "`config set` will
  refuse it", so **the published contract asserts a refusal the tool does not perform.** This is
  pre-existing and untouched by this spec. It is left unfixed here deliberately: adding the refusal is a
  behaviour change, and this spec's NFR1 is that behaviour does not change. It is the same *class* of
  defect this spec closes — a `describe` that cannot be trusted — pointing the other way, and it is
  flagged to the strategist rather than silently absorbed.
- **TC5**: The walk covers **`voice_tunnel/` only**. `tests/` deliberately sets variables the code does
  not read (that is how the retired bare-wake opt-in is proven inert), and `scripts/` are developer
  harnesses, not the running tool. Widening the walk to either would make the guard fail on correct code.

## Key Decisions

| Decision | Why | Rejected |
|---|---|---|
| Register the five, then add the guard | Adding the guard first fails the build on known instances and invites a whitelist | Guard first with exemptions |
| Walk the source, by AST | A hand-maintained pair of lists cannot catch the failure it exists for; a regex cannot tell a read from a docstring mention | A checklist in the contributing docs; a regex over string literals |
| Prove the guard fires, on every run | Three prior diagnostics in this repo never populated | Trusting a green run; a one-off manual demonstration |
| Two exclusion routes, not one | The measured tree has two legitimate non-instances with different reasons | One denylist covering both |
| Do **not** register `VOICE_TUNNEL_WAKE_BARE` | The code does not read it; a registered setting that cannot be honoured is worse than an unregistered one | Registering all six as the metaspec listed them |

## Implementation Tasks

- [x] Register `VOICE_TUNNEL_BARGE_IN`, `VOICE_TUNNEL_BARGE_IN_THRESHOLD`, `VOICE_TUNNEL_CONSONANT_BOOST`,
      `VOICE_TUNNEL_WATCH_MAX_S`, `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S` in `config.SETTINGS`, each with a
      live resolver and a `what` string in the register's existing voice.
- [x] Give the two `cli.py` watch ceilings resolvers the registry can call, without changing how the
      backoff reads them.
- [x] Document the five in `.env.example` alongside their neighbours.
- [x] Add the source-walking guard as a pytest module, with the four-bucket classification of FR5.
- [x] Give the guard an exclusion table carrying one reason per entry.
- [x] Add the negative control: the guard fires on a constructed unregistered read, asserted in the suite.
- [x] Add the false-positive controls: a docstring mention and a non-literal read must not fire it.
- [x] Run the full suite.

## Acceptance Criteria

### The five settings are reachable through the CLI

- [x] **AC1** (`integration`): `config get <key>` returns a value — not "unknown setting" — for each of the
      five. Driven through the CLI entry point in-process, not by asserting on the registry tuple.
- [x] **AC2** (`unit`): `describe`'s `env` block lists all five, since it is generated from `SETTINGS`.
- [x] **AC3** (`unit`): `config show` includes all five with a `source` of `default` in a hermetic
      environment.
- [x] **AC4** (`unit`): `.env.example` documents all five — the existing drift test covers this and must
      pass unmodified.
- [x] **AC5** (`integration`): `config set` then `config get` round-trips each of the five through a
      temp settings file.

### Behaviour is unchanged (NFR1, TC2)

- [x] **AC6** (`unit`): with no environment set, each of the five resolves to its module default —
      `BARGE_IN`, `BARGE_IN_THRESHOLD`, `CONSONANT_BOOST`, `WATCH_BACKOFF_MAX_S`, `WATCH_DISCONNECTED_MAX_S`.
- [x] **AC7** (`unit`): with `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S=540` in the environment — the live
      value — the disconnected ceiling resolves to `540.0`, and the registry's resolver reports the same
      number the backoff actually uses. *One number, two readers, asserted equal: the failure this guards
      against is a registry that describes a value the code does not use.*
- [x] **AC8** (`unit`): the existing `tests/test_watch_backoff.py` cases that set
      `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S` (including the "not a number" case) still pass unmodified.

### The guard (FR2, FR3, FR5)

- [x] **AC9** (`unit`): run against `voice_tunnel/`, the guard reports **zero** unclassified reads.
- [x] **AC10** (`unit`): the guard's failure message for an unclassified read contains the variable name,
      the file path and the line number.
- [x] **AC11** (`unit`): `VOICE_TUNNEL_PIPER_LENGTH_SCALE` and `VOICE_TUNNEL_HOME` are classified as
      *excluded* and *process-only* respectively — asserted by name, so deleting a reason silently is a
      test failure rather than a quiet pass.

### The guard has been seen to fail (FR4)

- [x] **AC12** (`unit`, **negative control**): pointed at a constructed fixture module that reads
      `VOICE_TUNNEL_NOT_A_REAL_SETTING`, the guard reports exactly that name with its file and line. This
      runs on every suite run. *Without it, AC9's zero is indistinguishable from a walker that parses
      nothing.*
- [x] **AC13** (`unit`, **false-positive control**): pointed at a fixture whose only occurrences of an
      unregistered name are in a docstring, a comment and a dict key, the guard reports nothing.
- [x] **AC14** (`unit`, **false-positive control**): pointed at a fixture containing a non-literal read
      (`_env(name)`), the guard reports nothing.

### Suite

- [x] **AC15** (`integration`): `python -m pytest tests/` passes with no test weakened or skipped.

## Testing Approach

### Validation steps

1. `venv/Scripts/python.exe -m pytest tests/ -q` — the whole suite, which now contains the guard.
2. `bin/voice-tunnel config get VOICE_TUNNEL_BARGE_IN` (and the other four) — the CLI answer, not the
   registry's.
3. `bin/voice-tunnel describe` — the `env` block carries all five.
4. `bin/voice-tunnel config show` against the live `.env` — confirms `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S`
   still reads `540` from `file`, which is TC2 verified on the running configuration rather than on a
   fixture.

### Test cases

| Input | Expected |
|---|---|
| `config get VOICE_TUNNEL_BARGE_IN`, nothing set | the default, source `default`, exit 0 |
| `config get VOICE_TUNNEL_WAKE_BARE` | unknown setting — it is not a setting and must not become one |
| `config get VOICE_TUNNEL_HOME` | refused as process-only, unchanged from today |
| live `.env` with `..._DISCONNECTED_MAX_S=540` | resolver and backoff both read `540.0` |
| `..._DISCONNECTED_MAX_S=not a number` | falls back to `28800.0`, unchanged from today |
| guard over `voice_tunnel/` | zero unclassified |
| guard over a fixture reading an unregistered name | fires, naming variable + file + line |
| guard over a fixture mentioning a name in a docstring | silent |
| guard over a fixture with `_env(name)` | silent |

## Out of Scope

- Retuning any of the five values.
- The Kokoro keys, already registered.
- The retired Piper length-scale key, beyond documenting why it is excluded.
- Registering `VOICE_TUNNEL_WAKE_BARE`, which is not read.
- Making `VOICE_TUNNEL_HOME` settable through `config set` — it cannot be, by construction.
- Widening the walk to `tests/` or `scripts/`.
- Any new setting. This spec closes a gap; it does not widen the surface.

## References

- Project node: `Voice Tunnel` — the roadmap row this closes, and the Kokoro fix that surfaced the class.
- `voice_tunnel/config.py` — `SETTINGS` and the `.env.example` drift test it already feeds.

## Verified state (2026-08-19)

Recorded by the team lead after **independent** verification. The checks below were re-run by the lead,
not taken from a sub-agent's self-report.

**Suite.** Baseline before any change: 577 collected, 575 passed, 2 skipped (both platform-conventional).
After: **618 collected, 616 passed, 2 skipped, 0 failed.** No existing test was modified, weakened or
skipped — `tests/test_watch_backoff.py` in particular is byte-identical and all of its cases pass,
including the unparseable-value one (AC8).

**The guard was verified by making it fail on the real package, not only on a fixture.** An
unregistered `os.environ.get("VOICE_TUNNEL_LEAD_MUTANT_CHECK")` was planted in `voice_tunnel/timing.py`,
outside either slice's file set. The guard failed with:

```
The code reads VOICE_TUNNEL_* variables the CLI does not declare.
  VOICE_TUNNEL_LEAD_MUTANT_CHECK  read at voice_tunnel/timing.py:183
Fix exactly one of these for each name:
  * register it in `config.SETTINGS` with a `what` and a live resolver — the usual answer;
  * if it decides WHERE the settings file lives, declare it in `cli.DESCRIBE['env_process_only']`;
  * if it is deliberately retired, add it to EXCLUDED in tests/test_settings_registry.py WITH ITS REASON.
```

The mutation was reverted and the file's checksum restored to its pre-mutation value. **This is what
makes AC9's zero a measurement rather than an absence** — the guard has now been seen to fire on the
real tree, at a call site neither slice authored, and to name the variable, the file and the line
(AC3/AC10) with a remedy that does not require an investigation.

**Registry membership, read off the live registry.** 31 settings before, **36 after**. The five are
registered; `VOICE_TUNNEL_WAKE_BARE`, `VOICE_TUNNEL_HOME` and `VOICE_TUNNEL_PIPER_LENGTH_SCALE` are not.
`describe.env` carries all 36 and `describe.env_process_only` carries `VOICE_TUNNEL_HOME` alone.

**TC2 verified on the running configuration, not a fixture.** `config show` against JJ's live `.env`
reports `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S = 540.0`, `source: file` — the value the currently
running server was started with, unchanged. The other four read their module defaults with
`source: default`. The registry's resolver and the backoff's own `_backoff_ceiling` are asserted equal
**to each other** rather than each against a literal, so a future edit that moves one and not the other
fails rather than drifts.

**A second, independent negative arm exists at the resolver level.** The delegating resolvers were
temporarily replaced with hand-written copies that ignore the override — the exact drift the design
prevents — and five tests failed with their intended messages before the mutation was reverted.

**Not fixed, deliberately, and flagged up:** `config set VOICE_TUNNEL_HOME` succeeds where `describe`
says it is refused. See TC4.
