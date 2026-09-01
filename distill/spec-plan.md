# Command Bridge — proposed spec plan

> **Status: SUPERSEDED as the authority — kept as the reasoning trail.** The authoritative roadmap now
> lives in the project note `Command Bridge` , and the specs themselves are being written
> in `../specs/cb-*.md`. This file is the scratch that generated them; where it disagrees with the
> note or a written spec, they win. **JJ ruled on sequencing 2026-09-01: rename first (spec 001), then
> the screenshot harness (spec 002) as the UI-verification prerequisite, then the server merge.** The
> spec numbering below reflects that order.
>
> Grounded in: [`voice-tunnel.md`](./voice-tunnel.md) · [`tunnel-vision.md`](./tunnel-vision.md)
> (the two code-grounded distillations).

## Where the fork stands right now

- **Seeded.** `command-bridge` is a full local clone of voice-tunnel — 150 commits, all 53 unpushed
  fork commits (lanes + canvas + the recent audio fixes) included. Origin removed, so it can never
  push to the frozen voice-tunnel remote.
- **Byte-identical so far.** The package is still `voice_tunnel/` at v0.2.7; the rename has not
  happened. So "seeded" means *the history is here*, not *the fork is renamed*.
- **voice-tunnel is frozen** at its published `origin/main` (53 commits behind), left as the finished
  experiment. Nothing was pushed; the freeze holds by default.

## The architecture the specs must settle FIRST

The two parents are **two servers and two web clients** today. Command Bridge is meant to be *one
surface*, so the foundational specs are about union, not new features:

| Fact from the distill | Consequence for the merge |
|---|---|
| voice-tunnel = **aiohttp**, 13 routes, WS handshake auth, JSONL turn-log + `--since` cursor | This is the spine. The canvas joins *this* server. |
| tunnel-vision = **separate stdlib SSE** server (`GET /events`), 17 commands, own web client | The canvas SSE + frame store must fold in (or mount as a sub-app), and the two pages become one. |
| Package still `voice_tunnel/` v0.2.7, **40 settings** prefixed `VOICE_TUNNEL_`, **~847 tests** | The rename is real, test-heavy work — not a `sed`. |
| **Lanes exist in BOTH.** Canvas follows the voice lane by a 100 ms poll (`Follower`, one-way) | One server → one lane state, direct. The poll-follow retires. |
| CLI collisions: `serve`, `status`, `describe`, `lane`/`switch`, `watch` exist on both sides | The unified CLI has to resolve them. |

## Proposed specs (in JJ's settled order)

### Phase 1 — Foundation

- **spec 001 · Package & CLI identity — the rename, FIRST.** ✍️ *written: `../specs/001-package-and-cli-identity.md`.*
  Rename the `voice_tunnel` package, the CLI entry, the `VOICE_TUNNEL_*` settings prefix
  (back-compat so a live `.env` — the pinned token — survives), reset the version. *Touches: 40
  settings, 21 handlers, ~847 tests; the suite is the gate.*
- **spec 002 · Screenshot harness — the UI-verification prerequisite.** ✍️ *written:
  `../specs/002-screenshot-harness.md`.* Port tunnel-vision's `shot` (headless, throwaway profile),
  made **full-page**, so the merged page can be screenshotted every iteration to a verified outcome.
- **spec 003 · One server, one page.** Fold tunnel-vision's SSE canvas + frame store into the aiohttp
  server; merge the two web clients into one page. **The hard one** — everything visual depends on it,
  and it depends on spec 002 to be verifiable.
- **spec 004 · Unified lane.** Collapse voice-lane and canvas-lane into one lane concept; retire the
  100 ms poll-follow now both live in one server.
- **spec 005 · Unified command surface.** Resolve the ~6 command collisions; bring the canvas verbs
  (`set`/`look`/`point`/`cue`/`run`/`raise`/…) under the one `command-bridge` CLI.

### Phase 1 — Meeting UI

- **spec 006 · Adaptive meeting layout.** Layout = *(agent count) × (canvas shared?) × (he wants to see
  it?)*. 1:1 fills the frame; more agents tile as orbs; a share reorganizes to the validated wireframe.
  **The wireframe is ONE state, not the design.**
- **spec 007 · Canvas-share consent.** *He* grants whether an agent may share; the agent reads a
  per-session "can/should I show a canvas" capability **before it draws**. The load-bearing new
  requirement; the visual sibling of the wake/voiceprint gate.

### Phase 2 — Lane parity

- **spec 008 · One transcript, orbs as participants.** One shared transcript; orbs carry per-lane state;
  the wake-name switch drives voice lane and canvas lane together (falls out of spec 004).

### Phase 3 — Differentiators (from the prior-art research)

- **spec 009 · The mechanic donors** (likely splits): object-anchored `point`; a Flight-Director per-lane
  channel; focus-aware rendering; an auto-director.

### Phase 4 — Package

- **spec 010 · Publish.** Open source under the Command Bridge name — PyPI (npm optional per JJ); GitHub
  repo. voice-tunnel's remote stays frozen and untouched.

## Sequencing (settled by JJ 2026-09-01)

**Rename first (spec 001)**, then **the screenshot harness (spec 002)** because the UI can't be iterated
to a *verified* outcome without full-page shots, then the rest of the foundation (spec 003 the server
merge — the real risk, spec 004 lane, spec 005 CLI), then the UI specs (spec 006/007, which depend on
002+003). Phases 2–4 follow.

⚠ **One execution prerequisite the seed left open:** the clone is code-only (no venv, no models), and
the ~847-test gate on spec 001 needs `venv/Scripts/python.exe`. So before the rename can be *verified*,
command-bridge needs a venv — a fresh one, or its tests pointed at voice-tunnel's. JJ's call; flagged
for when execution starts.
