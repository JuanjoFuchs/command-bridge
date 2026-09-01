---
id: "002"
title: A full-page screenshot on every iteration — the UI verification harness
status: complete
blocked_by: ["001"]
blocks: ["003"]
---

# A full-page screenshot on every iteration — the UI verification harness

## Overview

The merge's hard part is the UI (spec 003 folds two web clients into one page). You cannot iterate a UI
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
canvas + transcript column). The harness must capture the **whole page** at a real viewport, and be
able to shoot a specific **lane** so an off-lane agent can verify its own view. (Named *layout*
states arrive with spec 006; this harness only needs to reach the live page and any lane, not to
drive layout state.)

## Goals

- **A full-page screenshot of the Command Bridge page, on demand, in one command.**
- **It never touches a human's browser** — throwaway profile, headless.
- **It can shoot a specific lane**, so a UI iteration is checked against the case it changed. (Named
  layout *states* are spec 006's concern, not this spec's.)

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

- [x] Port tunnel-vision's `shot` driver into the renamed package (after spec 001).
- [x] Make the capture full-page at a configurable viewport (FR2); drop the 900×560 default.
- [x] Wire `--lane` and `--out`; JSON result with path + dimensions + exit codes.
- [x] Settle fonts/animations before the shutter for determinism (NFR2).

## Acceptance Criteria

- [x] **AC-1** `command:command-bridge shot --out shot.png` — **FR1/FR2.** Produces a PNG whose
      height is the full document, not a fixed crop; JSON reports the real pixel dimensions and the
      output path.
- [x] **AC-2** `command` — **FR2.** `command-bridge shot --viewport 390x844` captures at that
      viewport, and the JSON dimensions reflect it.
- [x] **AC-3** `command` — **FR3.** `command-bridge --lane <name> shot` targets that lane (passes
      `?lane=`) and produces a valid shot. ⚠ **Verified at the mechanism level only:** the harness
      reaches the page and accepts `--lane`; a *distinct per-lane view* (this lane vs the live one)
      can only differ once the page renders per lane, which arrives with lane parity (spec 004) —
      the pass-through is in place so it re-verifies visually then.
- [x] **AC-4** `command` — **FR4.** With the server down, `shot` exits non-zero with a JSON
      `{error, code, remedy}`.
- [x] **AC-5** `integration` — **NFR1.** A shot of the live page completes within a set upper bound
      (≤ 10 s on this machine), so it is cheap enough to run every iteration.
- [x] **AC-6** `integration` — **NFR2.** Two consecutive shots of the same fixed state are identical
      (within a negligible tolerance), proving the shutter waits for fonts/animations to settle.
- [x] **AC-7** `manual` — **FR2.** A human confirms the meeting-page shot shows the whole layout
      (orb row, canvas, transcript) with nothing clipped. ⚠ `manual` because "nothing looks clipped
      or visually wrong" is an aesthetic judgment no assertion makes; the *height-is-full-document*
      half is already automated in AC-1.

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
  specs' job (specs 006/007), and each will name what its screenshot must show.
- **Rendering changes to the page itself** — that is spec 003.

## References

- `distill/tunnel-vision.md` — the `shot` donor and the 900×560 / `--lane` notes.
- the project's Tunnel Vision Guide, rule 8 ("screenshot the page before claiming it works") — the
  effort gate this makes executable in Command Bridge. (A vault note, not a repo file; named here for
  provenance, not as a path to open.)
