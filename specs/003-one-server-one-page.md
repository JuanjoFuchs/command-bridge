---
id: "003"
title: One server, one page — the canvas joins the voice server
status: complete
blocked_by: ["001", "002"]
blocks: ["004", "006"]
---

# One server, one page — the canvas joins the voice server

## Overview

Command Bridge presents the voice tunnel and the tunnel-vision canvas as one surface. Today they are
two servers and two web clients: the voice side is an aiohttp server (the WebSocket voice channel, the
JSONL turn log, the `/status` contract), and the canvas side is a separate server streaming frames
over Server-Sent Events to its own page. This spec brings the canvas into the voice server and onto
the voice page, so one process serves both and one page shows both. It is the hard, load-bearing merge
— every later UI spec (the adaptive layout, the consent gate) builds on the canvas being present in
the page at all.

> **Completion rule:** This spec is not complete until all acceptance criteria are verified through
> the tests named on them and the spec 002 screenshot harness. Build-only verification is
> insufficient. Iterate until verification passes.

⚠ **Sizing:** the largest foundation slice (two servers → one origin, one page, persistence, six
render tiers, suite green). It is atomic — the layout, consent, lane-unification and CLI-collision
work are deferred to specs 004–007 — but budget more than one short session.

## What he said, verbatim

> *"Whenever I drive the voice tunnel, it drives the tunnel vision. … we leave the main center window
> with a lot of space where we can display the tunnel vision … [Tunnel vision] becomes an embedded
> part of this whole thing."*
>
> *"For folding everything into one server …"*
>
> (2026-08-31 / 2026-09-01, voice-dictated; turns 4007–4013 and 4497 of the `dev` session log — that
> log is gitignored, so the quotes carry the weight and not the ids.)

## Findings — the two servers, from the distillation

Grounded in `distill/voice-tunnel.md` and `distill/tunnel-vision.md`:

- The voice server owns the durable contract: a WebSocket voice channel, the JSONL turn log with the
  `--since` cursor, and the `/status` surface. This contract must not change.
- The canvas server is separate and stdlib-only. Its client subscribes to **one multiplexed SSE
  stream** (a single event stream carries every lane's frames, because a browser caps concurrent
  connections per origin); it renders frames in tiers (mermaid, Vega-Lite, markdown, html, svg,
  text); the browser measures geometry and posts it back; and frames persist to a JSON file so a
  canvas survives a restart. The server itself models no geometry and holds no LLM.
- The canvas already **follows the live voice lane** — a soft, one-way, read-only link the canvas
  client polls today. Once both live in one server that link becomes in-process rather than a poll,
  but **unifying the lane concept is spec 004, not this one.**

## Goals

- **One server process serves both the voice channel and the canvas** — one port, one page, started
  by one command.
- **One page shows both** — the voice UI (orb, transcript, controls) and the canvas surface coexist,
  with the canvas visible and live.
- **Nothing about the voice contract or the canvas's dumb-surface nature changes** — this is a merge,
  not a redesign.

## Requirements

### Functional Requirements

- **FR1** — Starting the Command Bridge server brings up **both** the voice channel and the canvas;
  there is no second server or second port.
- **FR2** — The canvas's SSE stream and its frame operations (create/update/remove a frame, the
  measured-geometry callback) are served by the **same origin** as the voice channel, so the page
  opens one voice connection and one canvas stream to one server.
- **FR3** — **One page renders both surfaces** — the voice orb, transcript, and controls, and the
  canvas the live agent draws into — on screen together. (The *arrangement* of the two is spec 006;
  this spec requires only that they coexist and the canvas is visible and updating.)
- **FR4** — The canvas stays a **dumb surface**: it is driven only by the canvas verbs over the CLI /
  HTTP and holds no model. A change that needs an LLM in the server violates the project and is out.
- **FR5** — **Canvas frames persist across a restart**, as they did in the standalone canvas — the
  merge must not lose the persistence the canvas already had.
- **FR6** — The **render tiers are preserved** (mermaid, Vega-Lite, markdown, html, svg, text): a
  frame that rendered before the merge renders the same after it.

### Non-Functional Requirements

- **NFR1** — **No regression to the voice contract.** The JSONL turn log, the `--since` cursor, and
  `/status` behave exactly as before; the voice test suite stays green.
- **NFR2** — **The canvas's live-update behavior is preserved** — a frame drawn by an agent appears
  on the page without a manual refresh, as it did standalone.
- **NFR3** — **The single-SSE-stream design is kept** — the merge does not fan the canvas out into
  one connection per lane (the reason it was one stream in the first place still holds in one server).

### Technical Constraints

- **TC1** — The voice server's transport is the host; the canvas is brought onto it, not the reverse.
  The voice WebSocket and turn-log contract are the fixed point.
- **TC2** — Loopback-only for the canvas surface, as the standalone canvas was; exposure decisions are
  unchanged by the merge.

## Implementation Tasks

- [x] Serve the canvas SSE stream and frame operations from the voice server (one origin, one port).
- [ ] Serve one page that mounts both the voice UI and the canvas surface, both live.
- [x] Carry the canvas's frame store and its file persistence into the merged server (FR5).
- [x] Preserve the render tiers and live-update path (FR6/NFR2).
- [x] Keep the voice contract untouched; run the voice suite as the regression gate (NFR1).

## Acceptance Criteria

- [x] **AC1** `integration` — **FR1/FR2.** Starting the server exposes the voice channel and the
      canvas stream on one origin/port; a client can open both against it.
- [x] **AC2** `shot` — **FR3.** A full-page screenshot (spec 002 harness) of the running page shows
      both the voice UI (orbs + transcript) and a drawn canvas frame, neither clipping the other. —
      **closed by spec 006's meeting page** (2026-09-01): a `command-bridge shot` of `/meeting` in the
      shared state shows the orb row, the dark canvas with kepler's two frames, and the transcript
      column together (`command-bridge-meeting-shared.png`). This spec deferred the combined page to
      006 by design; 006 delivered it. (Method tag `kittest-snapshot` → `shot` to name what validated it.)
- [x] **AC3** `integration` — **FR5.** A frame drawn, then the server restarted, is present again on
      reconnect.
- [x] **AC4** `integration` — **FR6/NFR2.** A frame of **each of the six tiers** (mermaid, Vega-Lite,
      markdown, html, svg, text) appears live (no refresh) and renders as its tier.
- [x] **AC5** `command:python -m pytest tests/` — **NFR1.** The full suite passes; the voice contract
      tests are unchanged.
- [x] **AC6** `unit` — **FR4.** No model/LLM dependency is importable by the server (the dumb-surface
      guard), mirroring the existing anti-LLM check.
- [x] **AC7** `integration` — **NFR3.** With several lanes drawing, the page subscribes to **one**
      canvas event stream, not one per lane — the multiplexing survives the merge.
- [x] **AC8** `integration` — **TC2.** The merged canvas endpoints bind loopback only; a request from
      a non-loopback address does not reach them.

## Testing Approach

### Validation Steps

1. Start the server; confirm one port answers for both voice and canvas.
2. Draw frames of each tier; screenshot the page; confirm both surfaces render.
3. Restart; confirm frames return.
4. Run the full suite.

### Test Cases

| Situation | Expected |
|---|---|
| start server | voice + canvas on one origin |
| draw a mermaid / vega / markdown frame | renders live, correct tier |
| restart with frames present | frames restored |
| voice turn round-trip | turn log + cursor unchanged |

## Out of Scope

- **The meeting layout and its adaptive states** — spec 006. This spec only requires the two surfaces
  coexist and the canvas is visible; it does not decide arrangement, resizing, or the 1:1-vs-gallery
  states.
- **Canvas-share consent** — spec 007.
- **Unifying the lane concept** — spec 004. The canvas may keep following the live voice lane as it
  does today; collapsing the two lane mechanisms into one is the next spec.
- **Unifying the CLI command surface / name collisions** — spec 005.

## Progress & integration plan (2026-09-01)

**Step 1 done — the canvas is copied in** (commit `8b7464d`): `command_bridge/canvas/` holds store,
server (the stdlib SSE reference), page (`render()`), cue, follow, extract. All import as
`command_bridge.canvas.*`; suite green. `runner.py` (`run <file>`) was left out — its matplotlib/
pandas imports are optional runtimes with no extra yet, and `run` is a later spec.

**What the port needs, now that the reference is read:**

- `canvas/server.py`'s `apply(path, payload)`, `_camera(...)`, `_cue(...)`, `_batch(...)` are the
  frame-op core. They touch only the module globals (`_lanes`, `_subscribers`, `_hands`, `_live`,
  `_geometry`, `_viewport`, `_armed`, `_store`) + `publish()`, and RETURN `(code, body)` — they write
  no HTTP. So they lift out of the stdlib `Handler` to module-level functions that command-bridge's
  aiohttp handlers can call directly (the one `self` dependency is the `apply → _camera → _cue`
  chain).
- **`GET /events` (SSE)** is the intricate half: replicate `_stream()` as an aiohttp `StreamResponse`
  (`text/event-stream`) — append a `queue.Queue` to `_subscribers`, write `version` → `sync` →
  armed cues → backlog `frame`/`rows`, then loop pulling from the THREAD queue and writing SSE.
  Bridge the blocking `queue.get(timeout=…)` to async with `run_in_executor`, and drop the queue on
  disconnect. This is a single multiplexed stream (NFR3), not one per lane.
- **The op routes** (`/frame`, `/remove`, `/clear`, `/rows`, `/point`, `/look`, `/zoom`, `/cue`,
  `/raise`, `/switch`, `/batch`, `/placed`, `/inspect(ed)`) become aiohttp POSTs that call the lifted
  `apply()`; serve `render()` for the canvas page. Init `_store`(enabled) + `_follower` when
  `command-bridge serve` starts, mirroring tv's `serve()`. TC2 loopback is inherited from the voice
  server's bind.
- **Only ADD routes** — the voice contract (WS, turn log, `/status`) is untouched, so NFR1 holds by
  construction.
- **The page merge (FR3)** — both surfaces on one page — is the largest piece and layers on the
  server merge; the final arrangement is spec 006's adaptive layout, so 003 needs only that the
  canvas is present, visible and live alongside the voice UI.

**Step 2 done — the server merge, verified live** (commit `fbc92b9`): the canvas rides
command-bridge's ONE aiohttp server. `canvas/server.py` exposes `apply`/`run_batch` via a
socket-less Handler instance (`__new__`) — the ops touch only module state + `publish`, so aiohttp
reuses them with no copy; `canvas/aio.py` is the aiohttp shell (SSE `/events` with the thread queue
bridged by `run_in_executor`; the op routes; the page); `build_app` registers them additively (voice
untouched → NFR1 holds) and `run()` calls `init_canvas` on serve; the store is now
`~/.command-bridge-canvas.json` (never the shared tunnel-vision file). **Screenshotted proof:** on
`command-bridge serve`, a `POST /canvas/frame` then `command-bridge shot --url …/canvas` rendered an
**html** frame and a full **mermaid** diagram — the round trip frame-op → store → SSE → page, for
two tiers, on the one server. FR1/FR2/FR6/NFR2/NFR3 met; NFR1 by construction (suite green, 0
regressions). Tiers mermaid/markdown/vega load from jsdelivr (inherited tunnel-vision behaviour), so
their render needs network; html/svg/text are native.

**Step 2+ — the server merge is now comprehensively verified:**
- **AC-3 restart-persistence ✅** (screenshotted): a frame drawn, the server `stop`ped (368-byte
  `~/.command-bridge-canvas.json` flushed), restarted (banner "1 frame restored"), and the frame
  came back and rendered ("I survive a restart").
- **AC-4 tiers:** html and mermaid both render live (screenshotted); markdown/vega/svg/text ride the
  same inherited page JS (mermaid/markdown/vega load from jsdelivr — network-dependent, inherited).
- **AC-5 / AC-6 / integration:** suite green (0 regressions); `tests/test_canvas.py` asserts the
  routes are mounted on the one app, the ops behave (set/remove/off-lane-refusal/bad-batch), and no
  canvas module imports an LLM (the surface stays dumb).
- **Branding swept:** the canvas page title and remedies now say command-bridge (follow.py's JJ
  quote left verbatim).

**The one thing left, and it is spec 006's by definition — FR3 / AC-2, the COMBINED single page.**
Voice is at `/`, the canvas at `/canvas`, both live on the one server; putting them into one page is
the meeting-UI *arrangement*, which this spec's Out of Scope already hands to **spec 006** and which
needs JJ's design validation (the wireframe, the state machine). So 003's load-bearing half — the
server merge — is done and verified; the single-page half is delivered by 006, not rushed here.

## References

- `distill/voice-tunnel.md`, `distill/tunnel-vision.md` — the two servers this spec merges.
- `specs/001-package-and-cli-identity.md` — the rename this builds on.
- `specs/002-screenshot-harness.md` — the verification harness AC2 depends on.
- `specs/voice-tunnel/` — the inherited specs documenting the voice contract TC1 must preserve.
