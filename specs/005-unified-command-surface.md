---
id: "005"
title: One CLI — the voice verbs and the canvas verbs are one command surface
status: complete
blocked_by: ["003", "004"]
blocks: []
---

# One CLI — the voice verbs and the canvas verbs are one command surface

## Overview

Command Bridge is a fork of two tools, and each shipped its own CLI: `voice-tunnel` (serve, watch,
say, lane, wake, cue, status, describe, …) and `tunnel-vision` (serve, set, look, point, cue, raise,
switch, status, describe, …). Spec 003 put both halves in one server; this spec puts both
vocabularies behind the one `command-bridge` binary. Six command NAMES exist in both vocabularies —
`serve`, `status`, `describe`, `lane`, `switch`, `watch` — and must resolve to one meaning each; the
canvas's own verbs (`set`, `look`, `point`, `cue`, `raise`, `zoom`, `inspect`, `remove`, `clear`,
`batch`) are not in the `command-bridge` CLI at all yet and must be brought under it. The one verb
whose name is contested and load-bearing is **`cue`**: in the voice vocabulary it plays a non-speech
earcon; in the canvas vocabulary it is the speech-synced highlight the merge exists to deliver. `cue`
becomes the highlight; the earcon yields the name.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it (a unit/integration/command test, or the spec 002 `shot`). Build-only
> verification is insufficient. Iterate until verification passes.

## What he said, verbatim

> *"a local CLI + web UI that presents Voice Tunnel and Tunnel Vision as **one surface**."*
>
> On what makes it worth building — the research verdict he accepted: *"the one differentiator that
> genuinely survives is the `cue` conjunction (live + agent-authored canvas + speech-synced
> highlight)."*
>
> Roadmap row (`Command Bridge.md`, 2026-09-01): *"**Unified command surface** — resolve the ~6
> command collisions (`serve`/`status`/`describe`/`lane`/`switch`/`watch`); bring the canvas verbs
> under the one `command-bridge` CLI."*

## Findings — the two command vocabularies today

Grounded in the code:

- **The `command-bridge` CLI is the voice CLI.** `command_bridge/cli.py` registers the voice verbs
  (`serve`, `watch`, `say`, `lane` with `list`/`add`/`remove`/`switch`, `wake`, `consumed`, `status`,
  `describe`, `stop`, `turns`, `timing`, `rate`, `verbose`, `download`, `voices`, `pronounce`,
  `voiceprint`) plus, already ported, `shot` (spec 002). Its `cue` verb plays a **non-speech earcon**
  (`command-bridge cue <name>`, names drawn from the cue vocabulary — the ack/thinking/done sounds).
- **The canvas verbs are not in this CLI.** The canvas ops live only as HTTP routes
  (`POST /canvas/<op>`, mounted by spec 003) and in the parent `tunnel-vision` CLI, whose verbs are
  `set` (place/replace a frame), `look`, `point`, `cue` (**highlights timed to the sentence being
  spoken** — `--text`/`--words`/`--arm`/`--lead`), `raise`, `switch`, `zoom`, `inspect`, `remove`,
  `clear`, `batch`, `chart`, `run`, plus its own `serve`/`status`/`describe`/`shot`.
- **The six shared names.** `serve` (both start a server — one server now, so one `serve`), `status`
  (voice server state vs. what is on the canvas), `describe` (each tool's contract), `lane` (the
  voice lane registry; the canvas expressed the same idea as a top-level `switch`), `switch` (canvas
  top-level vs. voice `lane switch` — spec 004 made the lane one), and `watch` (voice-only; the
  canvas has no equivalent).
- **`cue` is the contested verb.** Two unrelated behaviours share the name. The canvas `cue` is the
  named differentiator; the voice earcon is a minor manual trigger (the automatic acknowledgement
  sound is played by the server, not by this subcommand, so renaming the subcommand does not touch
  it). So `cue` resolves to the canvas highlight and the earcon takes a new verb.

## Goals

- **One binary, one vocabulary.** Every voice verb and every canvas verb is a `command-bridge`
  subcommand, reaching the one running server; there is no second CLI and no second port to target.
- **Each of the six shared names has exactly one meaning**, chosen so nothing an agent relied on
  silently changes behaviour under the same name.
- **`cue` is the speech-synced canvas highlight** — the conjunction the whole merge is justified by —
  and the earcon it displaced is still reachable under its own name.

## Requirements

### Functional Requirements

- **FR1** — The canvas draw and camera verbs — `set`, `look`, `point`, `raise`, `zoom`, `inspect`,
  `remove`, `clear`, `batch` — are `command-bridge` subcommands that drive the in-process canvas
  through the one running server, with the same flags and behaviour they had as canvas verbs.
- **FR2** — `command-bridge cue` is the **speech-synced highlight** (the canvas `cue`: arm/point marks
  timed to a `say --timings`). The non-speech **earcon** that previously answered to `cue` is reachable
  under a distinct verb (`earcon <name>`) with the earcon vocabulary unchanged, so no behaviour is
  lost — only the name it lives under.
- **FR3** — `switch` is one act. `command-bridge switch <lane>` hands the floor to a lane and is
  equivalent to `command-bridge lane switch <lane>`; because spec 004 unified the lane, either form
  moves the voice lane and the canvas together. There is not a second, canvas-only switch that could
  move a different lane.
- **FR4** — `serve`, `status`, and `describe` are single, unified commands. `serve` starts the one
  server that carries both halves (already true post-003). `status` reports the canvas alongside the
  voice state — at minimum the live lane and the frames present per lane. `describe` documents **both**
  vocabularies: the canvas verbs appear in its command list.
- **FR5** — Every canvas verb resolves the **one** running server's client URL (the same resolution
  `shot` uses), so a single `--session` reaches voice and canvas alike; no verb targets a second
  endpoint, port, or binary.

### Non-Functional Requirements

- **NFR1** — **The voice contract is otherwise unchanged.** Every existing voice subcommand, flag and
  output behaves as before and the voice test suite stays green. The **one** deliberate exception is
  the `cue` → `earcon` rename (FR2); it is called out here because it is the single intended change to
  an existing voice command's name.
- **NFR2** — **No second server, no second binary.** Bringing the canvas verbs under the CLI adds no
  process and no port; a canvas verb with no server running fails the same clean, remedied way the
  voice verbs do, not with a stack trace.

### Technical Constraints

- **TC1** — The canvas stays a **dumb surface** (the rule both parents share). The new CLI verbs are
  transport only — they marshal arguments and POST to a canvas op; they hold no rendering or decision
  logic, which stays in the canvas.
- **TC2** — The canvas `run` verb (execute a file, render its code + result) is **out of scope**: its
  runtime (`runner.py`, matplotlib/pandas) was deliberately not ported in the merge. Its absence must
  be graceful — a clear "not available in this build" message with a non-zero exit, never a traceback
  or an import error at startup.

## Implementation Tasks

- [x] Register the canvas draw/camera verbs (`set`, `look`, `point`, `raise`, `zoom`, `inspect`,
      `remove`, `clear`, `batch`, `chart`) as `command-bridge` subcommands that POST to the in-process
      canvas ops through the one client-URL resolution. — `cli.py` `_canvas()` → `_request(session,
      "/canvas/<op>", …)`; each takes `--session` + `--lane`.
- [x] Make `cue` the canvas speech-synced highlight; move the voice earcon to `earcon <name>` with its
      vocabulary intact; update the one describe/help entry and any test that drove `cue <name>`. —
      `cmd_canvas_cue` → `/canvas/cue`, `cmd_earcon` → `/cue`; no test drove the CLI `cue` earcon (the
      cue tests exercise the server-side `_push_cue`/`cues`, unaffected).
- [x] Add the top-level `switch <lane>` as the alias of `lane switch <lane>`; confirm no canvas-only
      switch path can set a lane the voice side does not know. — `cmd_switch` → `/lane`
      `{action:"switch"}`; an unknown lane returns `unknown_lane` from the voice registry.
- [x] Fold the canvas into `status` (live lane + per-lane frame presence) and into `describe` (the
      canvas verbs in the command list). — new read-only `GET /canvas/status` (aio.py) merged into
      `cmd_status` under `canvas`; the canvas verbs + a `cue`/`earcon` entry added to `DESCRIBE`.
- [x] Make the omitted `run` verb fail with a clear "not available in this build" message, not an
      import error. — `cmd_run` returns `{code:"unsupported", remedy:…}`; no runner import.

## Acceptance Criteria

- [x] **AC1** `integration` — **FR1/FR5.** Against a running server, each canvas draw/camera verb
      (`set`, `remove`, `clear`, `point`, `look`, `zoom`, `raise`, `inspect`, `batch`) invoked through
      the `command-bridge` CLI reaches its canvas op and takes effect (e.g. `set` then a `status`/shot
      shows the frame; `remove` removes it), using only the one `--session` resolution. — verified live
      2026-09-01: `set --html --id demo` → `{ok, frames:1}`; `status.canvas` showed `demo` on lane
      `main`; `remove demo` → `{frames:0}`. Payload/endpoint marshalling pinned by
      `tests/test_unified_command_surface.py`.
- [x] **AC2** `command` — **FR2.** `command-bridge cue --help` describes the speech-synced highlight
      (accepts `--text`/`--words`/`--arm`), NOT an earcon; `command-bridge earcon <name>` plays the
      sound the old `cue <name>` did, and the earcon vocabulary is unchanged. — verified live: `cue
      --help` shows `--text/--words/--arm`; `earcon --help` shows the `name` positional. Endpoints
      pinned: `cue`→`/canvas/cue`, `earcon`→`/cue`.
- [x] **AC3** `integration` — **FR3.** `command-bridge switch <lane>` moves the live lane to `<lane>`
      (identical to `lane switch <lane>`), and — inherited from spec 004 — the canvas live lane
      follows. No second switch path sets a lane the voice registry has not registered. — verified
      live: `switch atlas` → live lane `atlas` (confirmed by `lane list`); an unregistered lane
      returns `unknown_lane` from the voice registry.
- [x] **AC4** `command` — **FR4.** `command-bridge status` output includes the canvas's live lane and
      the frames present per lane; `command-bridge describe` lists the canvas verbs among its commands.
      — verified live: `status.canvas` = `{live_lane, clients, lanes:{…}}`; `describe.commands`
      contains all of set/look/point/cue/earcon/switch/chart/raise/inspect/run.
- [x] **AC5** `command` — **TC2.** `command-bridge run <file>` exits non-zero with a message that says
      the run verb is not available in this build; the CLI still starts and every other verb works
      (no import error from the missing runtime). — verified live: `run foo.py` → exit 1,
      `{code:"unsupported"}`; the CLI and every other verb work.
- [x] **AC6** `command:python -m pytest tests/` — **NFR1.** The full suite passes with no regression
      against the recorded baseline; the voice verbs are unchanged except the `cue`→`earcon` rename
      (which no test drove at the CLI level). — verified: only the 4 pre-existing `test_word_timings`
      failures (missing `kokoro_onnx`) remain, no new ones. NB: the `test_cli_surface` contract test
      (`describe` documents every parser flag) caught that the canvas verbs' flags needed per-flag
      `DESCRIBE` entries — fixed, so `describe` stays the true contract.

## Testing Approach

### Validation Steps

1. Start one server; drive each canvas verb through `command-bridge <verb>` and confirm the effect via
   `status` and a `shot`.
2. Check `cue --help`, `earcon <name>`, and `describe`/`status` output for the merged surface.
3. Confirm `run` degrades with a message, and the full suite has no new failures.

### Test Cases

| Command | Expected |
|---|---|
| `command-bridge set --html … --id x` then `status` | frame `x` present on the live lane |
| `command-bridge cue --help` | speech-synced highlight, not an earcon |
| `command-bridge earcon <name>` | plays the earcon the old `cue <name>` played |
| `command-bridge switch <lane>` | live lane moves; canvas follows (spec 004) |
| `command-bridge describe` | canvas verbs listed among the commands |
| `command-bridge run f.py` | non-zero exit, "not available in this build", no traceback |

## Out of Scope

- **The canvas `run` verb** — its runtime was not ported; this spec only makes its absence graceful.
- **The adaptive meeting layout** (spec 006) and the **canvas-share consent capability** (spec 007) —
  this spec unifies the *command surface*, not the page or the sharing gate.
- **Any change to what the canvas verbs *do*** beyond the name/namespace resolutions above — the
  ops themselves are inherited unchanged from the merge (spec 003).
- **Verifying the Vega-Lite render tier on the merged page** — the `chart` verb IS wired into the CLI
  (a thin `set`-with-`kind:vega` plus `--rows` append), but confirming that the vega tier actually
  *renders* on the merged `/canvas` page (a live shot of a chart) belongs to the render-tier work,
  not to unifying the command surface; no AC here asserts a vega image.

## References

- `command_bridge/cli.py` (the voice CLI), `command_bridge/canvas/server.py` (`apply()` — the canvas
  op set), `command_bridge/canvas/aio.py` (`POST /canvas/<op>`), the parent `tunnel-vision/tunnel_vision/cli.py`
  (the canvas verbs' flags, to reproduce as contracts).
- `specs/002-screenshot-harness.md` (the `shot` verb + the one client-URL resolution the canvas verbs
  reuse), `specs/003-one-server-one-page.md` (one server), `specs/004-unified-lane.md` (one lane, so
  `switch` moves both halves).
