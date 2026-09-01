---
id: "023"
title: An utterance belongs to the lane he addressed
status: complete
blocked_by: []
blocks: []
---

# An utterance belongs to the lane he addressed

## Overview

A turn was stamped with whichever lane was live when TRANSCRIPTION FINISHED, not the one he was
speaking to when he opened his mouth. Everything between those two moments — ASR, the voiceprint,
the turn model — takes seconds, and he can move the conversation inside that window. Switch early
and his words were delivered to an agent he had never addressed.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"Whenever I start speaking on a lane, I would like to finish speaking and be able to switch to a
> different lane while that thing that I just said just finishes transcribing. Right now, I have to
> finish speaking and wait until everything I have said finishes transcribing before switching to
> another lane. Because if I don't wait, whenever [it] finishes transcribing, it's going to be
> added to the lane I just switched to. And that doesn't make sense."*
>
> *"It worked."* (after testing the fix live)
>
> (2026-08-26, voice-dictated; verbatim record in `sessions/dev.jsonl`, turns 3003 and 3141 — that
> log is gitignored, so the quotes carry the weight and not the ids)

## Findings — his diagnosis was right, and the source said so before a line changed

`t_start` is captured when the utterance closes; `lanes.resolve` runs about 130 lines later, after
ASR and the voiceprint. It read `lanes.current` at the END of a multi-second pipeline.

🎯 **This is the shape spec `016` fixed OUTBOUND and nobody applied inbound.** That spec's rule is
*a stage belongs to the lane that owned it, resolved ONCE at the start* — a reply carries the lane
of the agent that composed it, however long synthesis and playback take. His words had no such
anchor.

⚠ **Mis-delivery is worse than loss, and that is why this ranked above the backlog.** A dropped
reply can be repeated. Words delivered to the wrong agent are in that agent's context and cannot be
taken out of it, and he only finds out by watching the wrong orb light up.

## Goals

- **An utterance carries the lane he was addressing from the moment he began speaking.**
- **A deliberate summons still moves the conversation** — that is him addressing someone on purpose.

## Requirements

### Functional

- **FR1** — **The live lane is latched on the silence→speech edge** and used to route the turn.
- **FR2** — **A leading wake name still switches**, unchanged.

### Non-functional

- **NFR1** — **The latch is per-utterance.** Left set, the next thing he says would inherit a lane
  he had already left, which is the same bug pointing the other way.
- **NFR2** — **Nothing latched behaves exactly as before**, so a turn arriving outside the speech
  path — or the first after a restart — is unaffected.

### Technical constraints

- **TC1** — **`lanes.resolve` stays pure.** It already takes `current` as an argument, so the fix
  is what is PASSED to it, not a change to the routing rules.

## Implementation Tasks

- [x] Latch the live lane when the buffer transitions from silence to speech.
- [x] Route against the latched lane, and clear it once the turn is resolved.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_utterance_lane.py` — **FR1.** A switch during transcription does not
      move the turn. **Verified by mutation:** resolving against the live lane turns this red.
- [x] **AC-2** `unit:tests/test_utterance_lane.py` — **NFR1.** The latch is cleared per utterance.
- [x] **AC-3** `unit:tests/test_utterance_lane.py` — **FR2.** A leading summons still switches.
- [x] **AC-4** `unit:tests/test_utterance_lane.py` — **NFR2.** Nothing latched falls back to the
      live lane.
- [x] **AC-5** `manual` — **FR1.** ✅ **Confirmed by JJ 2026-08-26**, live: he spoke a long sentence
      and switched before the transcript landed. *"It worked."*

## Testing Approach

### Validation Steps

1. Run `tests/test_utterance_lane.py`; confirm AC-1 fails when routing reads the live lane.
2. Run the full unit suite — `_emit` is on the path of every turn.

### Test Cases

| Situation | Expected |
|---|---|
| speak to magnus, tap atlas mid-ASR | turn stamped magnus |
| "hey atlas …" | switches to atlas |
| nothing latched | routes to the live lane |
| two utterances in a row | each carries its own lane |

## Out of Scope

- **Showing him that a turn is still being transcribed.** He asked to be able to switch, not to be
  told when it is safe to.

## References

- `specs/016` — the same rule, applied outbound, which this completes for the inbound direction.
- 🔴 **`tests/test_utterance_lane.py` carries a warning worth reading before writing any test in
  this repo:** its first version re-implemented the server's line in a local helper and passed
  against the broken code. Drive the real path or do not claim to.
