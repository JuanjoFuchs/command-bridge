---
id: "003"
title: One server, one page — the canvas joins the voice server
status: pending
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

- [ ] Serve the canvas SSE stream and frame operations from the voice server (one origin, one port).
- [ ] Serve one page that mounts both the voice UI and the canvas surface, both live.
- [ ] Carry the canvas's frame store and its file persistence into the merged server (FR5).
- [ ] Preserve the render tiers and live-update path (FR6/NFR2).
- [ ] Keep the voice contract untouched; run the voice suite as the regression gate (NFR1).

## Acceptance Criteria

- [ ] **AC1** `integration` — **FR1/FR2.** Starting the server exposes the voice channel and the
      canvas stream on one origin/port; a client can open both against it.
- [ ] **AC2** `kittest-snapshot` — **FR3.** A full-page screenshot (spec 002 harness) of the running
      page shows both the voice UI and a drawn canvas frame, neither clipping the other.
- [ ] **AC3** `integration` — **FR5.** A frame drawn, then the server restarted, is present again on
      reconnect.
- [ ] **AC4** `integration` — **FR6/NFR2.** A frame of each render tier appears live (no refresh) and
      renders as its tier.
- [ ] **AC5** `command:python -m pytest tests/` — **NFR1.** The full suite passes; the voice contract
      tests are unchanged.
- [ ] **AC6** `unit` — **FR4.** No model/LLM dependency is importable by the server (the dumb-surface
      guard), mirroring the existing anti-LLM check.

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

## References

- `distill/voice-tunnel.md`, `distill/tunnel-vision.md` — the two servers this spec merges.
- `specs/001-package-and-cli-identity.md` — the rename this builds on.
- `specs/002-screenshot-harness.md` — the verification harness AC2 depends on.
- `specs/voice-tunnel/` — the inherited specs documenting the voice contract TC1 must preserve.
