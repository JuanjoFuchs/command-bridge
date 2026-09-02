---
id: "011"
title: Speech-synced deixis — one command that says, shows, and points
status: pending          # pending | in_progress | complete
blocked_by: []           # 005 (canvas verbs under the one CLI) and the `cue`/`say --timings` contract already shipped
blocks: []
---

# Speech-synced deixis — one command that says, shows, and points

> **Strategist metaspec (the project notes agent).** This is the leaner, high-level draft per
> Spec Writing Rules for Agents. It fixes the *contract* and the *decisions*;
> the repo implementer refines the Implementation Tasks, Test Cases and any remaining Acceptance
> Criteria against the actual codebase before building. Project node: Command Bridge.

## Overview

Command Bridge is voice **and** canvas — and the one differentiator the prior-art research says survives
is the **`cue` conjunction**: a live, agent-authored canvas with a highlight *measured* to the words
being spoken (Command Bridge). Today an agent reaches that
conjunction by hand: `set` a frame, `say --timings` to get the spoken-word offsets, capture the `words`
array, then `cue --words <that array>` to fire the highlight in sync. Three commands and a manual
hand-off of timing data between two of them.

This spec makes **deixis** — pointing at the thing while you name it — a **single command**: one
invocation that speaks the text, optionally shows a canvas frame, and fires highlights on named canvas
targets **anchored to words or phrases in that same text**, using the say's own **measured** timings.
The point is not brevity; it is that the differentiator becomes the **default path** an agent takes,
not a discipline it must remember. JJ, by voice 2026-09-02: *"the agents should always leverage deixis …
in a way that it's just one command … one command that's issued with the say that also shows me
something, and when the say is played does the [deixis] on it."*

> **Completion rule:** This spec is not complete until all acceptance criteria are verified through the
> testing methodology below (integration tests driving the real command path, plus a headless
> canvas-event assertion). Build-only verification is insufficient. The agent must iterate until
> verification passes.

## Goals

- Make speech-synced deixis reachable in **one command**, so an agent points-while-speaking by default
  rather than by remembering to chain `say --timings` → `cue`.
- Keep every highlight **measured**, never estimated — the timing comes from the same synthesis that
  produces the audio, so the highlight lands on the word as it is actually spoken.
- Degrade **honestly** when there is no canvas to point at (driving, transcript-only, or the viewer has
  not consented to a shared canvas) — speak anyway, skip the highlights, and say so in the result.
- Change nothing about the plain `say` for turns that carry no deixis.

## Requirements

### Functional Requirements

- **FR1** — A single command speaks the given text on a lane AND fires one or more canvas highlights,
  each anchored to a **word or phrase within that text**, in one invocation. No second command and no
  hand-carried timing array.
- **FR2** — Each highlight's onset is taken from the **measured** word offsets of *this* utterance's
  synthesis (the same source `say --timings` exposes as `words:[{w,t}]`), never from an estimate. A
  highlight whose anchor phrase cannot be aligned to a measured word is **skipped and reported**, not
  fired at a guessed time. (Consistent with Tunnel Vision Guide: never call an `estimated` cue
  word-synchronised.)
- **FR3** — The command MAY set/replace the shown canvas frame as part of the same invocation (so
  "show me something and point at it while you say this" is one call), and MAY reference targets already
  on the canvas.
- **FR4** — When no canvas is shared for the lane (not consented per the future spec 007, or nothing is
  being shown), the command still **speaks**, **omits** the highlights, and the result **states** that
  the deixis was dropped and why — the audio half must never be held hostage to the visual half.
- **FR5** — The result is machine-readable and reports, at minimum: the utterance id/clip, the measured
  offset each highlight fired at (or why it was skipped), and whether the canvas was shown.

### Non-Functional Requirements

- **NFR1** — The plain `say` path is byte-for-byte unchanged for calls that specify no deixis; this is
  additive, not a reshape of `say`.
- **NFR2** — Ordering guarantee: a highlight never fires before its anchor word is spoken; the audio and
  the canvas events derive from one measured schedule, so they cannot disagree about when a word lands
  (the same single-source-of-truth discipline as spec 004's unified lane).
- **NFR3** — Untrusted-input and locality unchanged: `turn.text` stays untrusted, audio/timings/canvas
  stay local (Voice Conversation Rules).

### Technical Constraints

- **TC1** — Builds on shipped contracts only: `say --timings` (`words:[{w,t}]`, `words_aligned`,
  `timings_unavailable`) and the canvas speech-synced `cue`/`point` under the one CLI (spec 005). It does
  not re-implement synthesis or timing measurement — it composes them.
- **TC2** — The command surface stays consistent with spec 005's rule that `describe` documents every
  flag, default and unit; whatever flags this adds are authoritative in `describe`, not in a guide.
- **TC3** — The dumb-tool discipline holds (Command Bridge): the tool
  moves audio, pixels and measured timings; it holds no model and makes no decision about *what* to
  point at — the agent authors the anchors.

## Key Decisions (strategist)

- **This is one command, not a guide instruction.** A guide telling agents to cue every say relies on
  memory and rots — the same failure class as forgetting to re-arm a watch. A single command makes
  deixis **structural**: the easy path and the correct path are the same call. (JJ, 2026-09-02.)
- **Anchor by word/phrase in the text, not by absolute time.** The agent writes what it will say and
  what to point at *as it says it* ("highlight the Q3 bar on the word Q3"); the command resolves the
  time from the measured schedule. The agent never computes a timestamp.
- **Shape is deferred to the implementer, but the seam is fixed:** whether this is `say` gaining
  deixis flags or a sibling verb that wraps `say`+`cue` is the implementer's call against the codebase
  (spec rule 6, "what not how"). What is fixed: one invocation, measured timing, honest degradation.

## Acceptance Criteria

- [ ] **AC1** — A single documented command speaks text and fires ≥2 highlights anchored to distinct
      words in that text, in one invocation, with no second command and no hand-passed timing array. *(integration)*
- [ ] **AC2** — Each highlight's fired offset equals the **measured** offset of its anchor word from the
      utterance's own timings (assert the emitted canvas-cue events carry the same `t` the say's
      `words:[{w,t}]` reports for that word). *(integration)*
- [ ] **AC3** — An anchor phrase that does not align to a measured word is **skipped and named in the
      result**, and no highlight fires for it. *(integration)*
- [ ] **AC4** — With no canvas shared for the lane, the command still speaks and the result reports the
      deixis dropped, with the reason; the audio is unaffected. *(integration)*
- [ ] **AC5** — A plain `say` with no deixis produces output identical to the pre-spec `say`. *(integration/regression)*
- [ ] **AC6** — The highlight is visible on the canvas at/after the anchor word, verified headlessly
      against an approved baseline (a shot at the moment the anchor word is spoken shows the target lit). *(kittest-snapshot)*
- [ ] **AC7** — Every new flag/field is documented in `command-bridge describe`, and the describe
      contract test stays green. *(unit)*

## Out of Scope

- The canvas drawing verbs themselves (`set`, `point`, `look`, `zoom`, …) — they exist (spec 005); this
  composes them.
- **Object-anchored `point`, focus-aware rendering, the Flight-Director channel, an auto-director** —
  the other differentiator mechanics (Command Bridge roadmap spec 009). This spec is the *say+show+cue
  unification*, not those.
- **Canvas-share consent** (roadmap spec 007) — this spec *reads* whether a canvas is shared (FR4) but
  does not define the consent gate; it degrades correctly whatever that gate decides.
- Any change to synthesis, voiceprint, wake, or the turn model.
- Multi-lane / broadcast deixis — one lane per invocation, as `say` already is.

## References

- Project node: Command Bridge — esp. *What the research settled* (the `cue` conjunction as the
  surviving differentiator) and the roadmap (relation to spec 009).
- Tunnel Vision Guide — the canvas verbs and the measured-vs-estimated `cue` rule.
- Spec `005-unified-command-surface` — brought `cue`/`point`/`set` under the one CLI and made `cue` the
  speech-synced highlight; this spec builds directly on it.
- The shipped `say --timings` contract (`words:[{w,t}]`, `words_aligned`, `timings_unavailable`) — the
  measured-timing source this command composes.
- Spec Writing Rules for Agents — the rules this metaspec follows.
