---
id: "CB-002"
title: A full-page screenshot on every iteration — the UI verification harness
status: proposed
blocked_by: ["CB-001"]
blocks: ["CB-003"]
---

# A full-page screenshot on every iteration — the UI verification harness

## Overview

The merge's hard part is the UI (CB-003 folds two web clients into one page). You cannot iterate a UI
to a *verified* outcome by reasoning about markup — you have to see what rendered. tunnel-vision
already solved this with `shot`: a headless browser on a throwaway profile that photographs the page
without stealing a human's browser. This spec brings that capability into Command Bridge and makes it
**full-page and reliable**, so it is in place *before* any UI work begins.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"For folding everything into one server, especially the UI part, we first, I think, need to
> properly nail down a way for you to screenshot, to fully screenshot, which I think Tunnel Vision
> has it, the UI on every iteration step so that you can properly iterate and get to a real verified
> outcome."*
>
> (2026-09-01, voice-dictated; turn 4497 of `voice-tunnel/sessions/dev.jsonl` — gitignored, so the
> quote carries the weight and not the id.)

## Findings — the donor already exists

From `distill/tunnel-vision.md`, grounded in the code:

- tunnel-vision's `shot` command drives a **headless browser on a throwaway profile**, so it never
  steals the operator's logged-in browser (the same reason voice-tunnel's `uitest.py` uses a virtual
  device).
- Its default capture is **900×560** (the code, not the 1600×1000 its own contract claims — a bug to
  not inherit), and it has an undocumented `--look` flag.
- The agent-facing rule it serves is Tunnel Vision Guide rule 8: *screenshot the page before
  claiming it works.* Command Bridge inherits that effort gate.

🎯 **The gap to close is "fully".** A 900×560 shot crops the meeting layout (orb row + full-bleed
canvas + transcript column). The harness must capture the **whole page** at a real viewport, and —
because the layout is state-driven (CB-006) — be able to shoot a named state, not just whatever is
live.

## Goals

- **A full-page screenshot of the Command Bridge page, on demand, in one command.**
- **It never touches a human's browser** — throwaway profile, headless.
- **It can shoot a specific state/lane**, so a UI iteration is checked against the case it changed.

## Requirements

### Functional

- **FR1** — `command-bridge shot [--out PATH]` renders the live page in a headless throwaway-profile
  browser and writes a PNG; JSON out with the path and pixel dimensions.
- **FR2** — **Full-page by default** — the capture is the whole document at a real viewport, not a
  fixed crop. The viewport is configurable (`--viewport WxH`) with a phone-realistic default.
- **FR3** — `--lane <name>` shoots that lane's view; off-lane capture works (the tunnel-vision
  `shot`-ignores-`--lane` defect is already fixed in the donor — do not reintroduce it).
- **FR4** — Meaningful exit codes: 0 render ok, non-zero with a JSON `{error, code, remedy}` when the
  server is down or the browser can't start.

### Non-functional

- **NFR1** — **Fast enough to run every iteration** — a shot is a routine step, not a ceremony;
  target a few seconds, headless and reused where possible.
- **NFR2** — **Deterministic** — same state in, same picture out (fonts/animations settled before the
  shutter), so a diff means a real change.

### Technical constraints

- **TC1** — Reuse the existing browser tooling on this machine rather than adding a new dependency
  stack; the donor's headless driver is the starting point.
- **TC2** — The harness is a dumb tool — it photographs, it does not judge. The agent reads the
  picture.

## Implementation Tasks

- [ ] Port tunnel-vision's `shot` driver into `command_bridge/` (post-CB-001 naming).
- [ ] Make the capture full-page at a configurable viewport (FR2); drop the 900×560 default.
- [ ] Wire `--lane` and `--out`; JSON result with path + dimensions + exit codes.
- [ ] Settle fonts/animations before the shutter for determinism (NFR2).

## Acceptance Criteria

- [ ] **AC-1** `command:command-bridge shot --out /tmp/cb.png` — **FR1/FR2.** Produces a PNG whose
      height is the full document, not a fixed crop; JSON reports the real dimensions.
- [ ] **AC-2** `manual` — **FR2.** The shot of the meeting page shows the whole layout (orb row,
      canvas, transcript) with nothing clipped, at a phone-realistic viewport.
- [ ] **AC-3** `command` — **FR3.** `command-bridge --lane <name> shot` captures that lane's view
      while another lane is live.
- [ ] **AC-4** `command` — **FR4.** With the server down, `shot` exits non-zero with a JSON remedy.

## Testing Approach

### Validation Steps

1. Start the server, `shot`, open the PNG — confirm full-page, correct viewport.
2. Kill the server, `shot` again — confirm the error path.

### Test Cases

| Situation | Expected |
|---|---|
| live page | full-page PNG + JSON dims |
| `--viewport 390x844` | shot at that viewport |
| off-lane `--lane` | that lane's view, not the live one |
| server down | non-zero + `{error, code, remedy}` |

## Out of Scope

- **Visual-diff/regression assertions.** This spec delivers the picture; asserting on it is the UI
  specs' job (CB-006/007), and each will name what its screenshot must show.
- **Rendering changes to the page itself** — that is CB-003.

## References

- `distill/tunnel-vision.md` — the `shot` donor and the 900×560 / `--lane` notes.
- Tunnel Vision Guide rule 8 — the effort gate this makes executable in Command Bridge.
