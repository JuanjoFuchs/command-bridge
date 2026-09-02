---
id: "011"
title: Speech-synced deixis — one command that says, shows, and points
status: complete         # pending | in_progress | complete
blocked_by: []           # 005 (canvas verbs under the one CLI) and the `cue`/`say --timings` contract already shipped
blocks: []
---

# Speech-synced deixis — one command that says, shows, and points

> **Implementer spec.** Drafted strategist-side and then, at JJ's direction 2026-09-02 (*"you are the
> implementing agent"*), refined against the codebase into the full spec below — concrete command
> contract, implementation tasks and test cases. Project node: Command Bridge.

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
  synthesis (the same source `say --timings` exposes as `words:[{w,t}]`), never from an estimate. A mark
  that runs **past the last word** (there is no later moment to schedule it at) fires at the **last
  measured word** rather than being dropped — a real time, not a guess — matching the shipped
  `canvas/cue.py`. The `timing` on a measured schedule is always `measured`; the estimated split is only
  reached when there are no `words` at all. (Consistent with Tunnel Vision Guide: never call an
  `estimated` cue word-synchronised.)
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
- **Shape (chosen against the codebase): `say` gains the deixis, reusing `cue`'s existing inline-mark
  syntax.** The canvas `cue` already takes `--text` carrying inline `[point:<selector>]` marks placed
  where each highlight belongs, plus `--words` (measured). So `say` accepts the **same** marked text:
  it strips the marks for synthesis, speaks the clean text with timings, then drives the canvas `cue`
  with the marked text and those measured `words`. One text, one command, and an agent who already
  knows `cue`'s marks knows this. A sibling verb was rejected — it would fork the mark vocabulary and
  make "always leverage deixis" a *different* command instead of the say you already send.

## Command contract

The plain form is unchanged. Deixis is present exactly when the spoken text carries one or more inline
`[point:<selector>]` marks (the `cue` vocabulary):

```
command-bridge say --lane magnus \
  "Revenue climbed in [point:#bar-q3]Q3 and dipped in [point:#bar-q4]Q4." [--show <frame-source>]
```

- The mark sits **immediately before the word it names**, so the highlight fires as that word is spoken.
- `--show <frame-source>` (optional) sets/replaces the shown frame first, reusing `set`'s sources, so
  "show this and point at it while I say this" is one call; marks may also target a frame already shown.
- The clean text (marks removed) is what is synthesized and what appears in the transcript.
- The result adds a `deixis` object: the marks fired with their measured offsets, any skipped (with the
  reason), and whether a canvas was shown — on top of the normal `say` result.

## Implementation Tasks

- [ ] Detect inline `[point:<selector>]` marks in `say`'s text; when none are present, the path is the
      existing `say` untouched (NFR1).
- [ ] Strip the marks to the clean spoken text (synthesis + transcript), keeping each mark's position
      so it can be re-anchored to the measured word after synthesis.
- [ ] Speak the clean text with timings (the existing `say --timings` measurement), and drive the canvas
      `cue` with the marked text + the measured `words`, carrying the say's lead (`held_for`) so a
      **held** clip's highlights fire when the lane goes live, not at issue time.
- [ ] Resolve each mark to its measured word; a mark that cannot be aligned is skipped and recorded, no
      highlight fired (FR2).
- [ ] Read whether a canvas is shared for the lane; when it is not, speak and drop the deixis, recording
      why (FR4). Never block the audio on the canvas.
- [ ] Extend the `say` result with the `deixis` object (FR5); document every new flag/field in
      `describe` and keep the describe-contract test green.

## Acceptance Criteria

- [ ] **AC1** — A single documented command speaks text and fires ≥2 highlights anchored to distinct
      words in that text, in one invocation, with no second command and no hand-passed timing array. *(integration)*
- [ ] **AC2** — Each highlight's fired offset equals the **measured** offset of its anchor word from the
      utterance's own timings (assert the emitted canvas-cue events carry the same `t` the say's
      `words:[{w,t}]` reports for that word). *(integration)*
- [ ] **AC3** — On a measured schedule every mark's `at` is a value drawn from `words` (a mark past the
      last word clamps to the **last measured word**, never an estimated split), and the schedule's
      `timing` is `measured`. *(unit — `canvas.cue.schedule`)*
- [ ] **AC4** — With no canvas shared for the lane, the command still speaks and the result reports the
      deixis dropped, with the reason; the audio is unaffected. *(integration)*
- [ ] **AC5** — A plain `say` with no deixis produces output identical to the pre-spec `say`. *(integration/regression)*
- [ ] **AC6** — The highlight actually lights its target on the canvas, and does so **on the measured
      word**: headless, the target carries `.pointed` AFTER the anchor word's time and NOT before it. *(integration — `tests/deixis_visual_check.py`)*
- [ ] **AC7** — Every new flag/field is documented in `command-bridge describe`, and the describe
      contract test stays green. *(unit)*

## Testing Approach

### Validation Steps
1. Drive the real command path (as the existing say/cue suites do — not a re-implemented helper) with a
   marked text against a fixture canvas that owns its frame; assert the emitted cue schedule.
2. Assert each fired mark's offset equals the measured word offset from the same utterance's `words`.
3. Toggle the canvas-shared state off and assert the audio still speaks and the `deixis` result names
   the drop.
4. Headless shot at the anchor word's moment shows the target lit (kittest-snapshot against a baseline).

### Test Cases
| Input | Expected |
|-------|----------|
| `say "climbed in [point:#q3]Q3 and [point:#q4]Q4"`, canvas shown | speaks "climbed in Q3 and Q4"; two cue marks fire at the measured `t` of "Q3" and "Q4" |
| Same, but `#q4` mark's word never aligns | `#q3` fires; result lists `#q4` skipped with reason; no guessed fire |
| Same text, **no** canvas shared | speaks the clean text; `deixis.dropped` names "no canvas shared"; audio unaffected |
| `say "just a plain turn"` (no marks) | identical to pre-spec `say`; no `deixis` object mutation of behaviour |

## Usage Examples

```bash
# Show a chart and point at two bars as you name them — one command.
command-bridge say --lane magnus \
  "Revenue climbed in [point:#bar-q3]Q3 then dipped in [point:#bar-q4]Q4." --show chart.json

# Point at something already on the canvas.
command-bridge say --lane kepler "The bug is in [point:#node-auth]the auth step."

# No marks → ordinary say, unchanged.
command-bridge say --lane magnus "On it."
```

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

## Findings — implementer (2026-09-02)

**Core built and tested (`command_bridge/cli.py`, `tests/test_deixis.py`).** `say` detects inline
`[point:<selector>]` marks, synthesizes the clean stripped text, forces `--timings`, and — on a clip
that was actually spoken — drives the canvas `cue` with the ORIGINAL marked text and the say's own
measured `words`, arming it (`held_for` as lead) when the say was held off-lane and firing it live
otherwise. It attaches a `deixis` object and degrades honestly (no canvas / no schedule / refused →
dropped-with-reason, audio untouched). `--now` + a mark is refused (measured timing can't exist yet).
Documented in `describe` (the `text` arg and the `deixis` return).

**All acceptance criteria met.** `tests/test_deixis.py` (12 cases) drives the real `cmd_say` and the
pure `canvas.cue.schedule`:
- **AC1/AC4/AC5/AC7** — the say→cue seam, honest degradation, plain-say-untouched, and describe, with
  only the HTTP boundary stubbed.
- **AC2/AC3** — the measured `words` reach the cue unchanged, and `canvas.cue.schedule` places each
  mark on its measured word, **clamping a mark past the last word to the last measured word** (never an
  estimated split) — this filled a real gap: that module had no unit test.
- **AC6** — `tests/deixis_visual_check.py` (a runnable harness, not collected by pytest) drives a live
  canvas headlessly and confirms the target is `.pointed` AFTER the anchor word's time and NOT before,
  so it checks the *timing*, not just that the highlight lands. Passed 2026-09-02.
- **FR3 `--show`** — built: `say --show <file>` places the frame first (kind inferred from the
  extension), tested; a bad source refuses before speaking.

Full say/cue/canvas/describe suites stay green. **Reconciliation worth noting for the next reader:** the
first draft's FR2/AC3 said an unaligned mark is "skipped"; the shipped `canvas/cue.py` deliberately
**clamps** it to the last measured word instead ("dropping it would silently lose a highlight the agent
asked for"), which still honours the never-a-guessed-time rule — the spec now matches the code.

## References

- Project node: Command Bridge — esp. *What the research settled* (the `cue` conjunction as the
  surviving differentiator) and the roadmap (relation to spec 009).
- Tunnel Vision Guide — the canvas verbs and the measured-vs-estimated `cue` rule.
- Spec `005-unified-command-surface` — brought `cue`/`point`/`set` under the one CLI and made `cue` the
  speech-synced highlight; this spec builds directly on it.
- The shipped `say --timings` contract (`words:[{w,t}]`, `words_aligned`, `timings_unavailable`) — the
  measured-timing source this command composes.
- Spec Writing Rules for Agents — the rules this metaspec follows.
