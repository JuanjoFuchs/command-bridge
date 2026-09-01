---
id: "027"
title: A latch outlives the utterance that made it
status: in_progress
blocked_by: []
blocks: []
---

# A latch outlives the utterance that made it

## Overview

Spec `023` latches the live lane on the silence-to-speech edge so a switch made mid-sentence cannot
redirect words he had already started saying. The latch is cleared in one place: when a turn is
routed. **Any speech edge that never produces a routed turn — a cough, a false start, a blip below
the turn model's threshold — leaves the latch set**, and the guard that holds it across an utterance
(`and state.utterance_lane is None`) then prevents it being re-latched. It survives every subsequent
lane switch and delivers his next real sentence to the lane he was standing on when the noise
happened.

He hit it while trying to ask a different agent a question, and it blocked him from using the room.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"I just experienced an issue."* · *"I had switched lanes before I started speaking, yet my
> previous turn was attributed to you."*
>
> *"No, we need the lane fixing because that other churn [turn] was not for you."*
>
> *"That is critical, is currently blocking me interacting with the other agents."*
>
> (2026-08-27, voice-dictated; the turn log `sessions/dev.jsonl` is gitignored and short-lived, so
> the quotes carry the weight and not the ids 3205, 3206, 3211, 3212)

## Findings — measured, not inferred

From `sessions/dev.timing.jsonl`, the sequence that produced it:

```
12:24:13.021  utterance_end                       (previous utterance)
12:24:16.072  consumed        cursor 3202  lane magnus
12:24:46.110  lane            atlas  why=tap      ← he switches
12:25:14.885  utterance_end   audio_s 12.02       ← so speech BEGAN ~12:25:02, 16 s after the tap
12:25:18.226  turn_logged     3203 "…is the other guide loaded…"
                                                  ← delivered to MAGNUS
12:25:56.240  turn_logged     3204 "Hey Atlas, no, I want to see it…"
12:25:56.327  consumed        cursor 3204  lane atlas   ← he had to say the name to get through
```

🔴 **Sixteen seconds separate the tap from the first word, so no live race explains it.** The latch
was already set before the tap and could not be updated afterwards. The repeat at 12:25:56 is the
cost, and it is the tell: **an explicit summons was the only thing that could still move the
conversation**, which is what the latch is supposed to leave possible and everything else is not.

🎯 **The shape, for the fourth time this week: a value latched on an edge and invalidated on only
the success path.** `agent_state` was a cache with one writer and no invalidation (spec `026` F1);
this is a latch with one clearer and no expiry. **The guard that makes a latch correct DURING an
utterance is exactly what makes it wrong BETWEEN utterances.**

⚠ **The fix must not undo spec `023`.** Clearing on every switch would restore the original defect:
he switches mid-sentence and the words he already started saying follow him to the new lane. The
distinction that separates the two cases is whether an utterance is actually open.

## Goals

- **A deliberate switch outranks a latch made before it.**
- **A switch made mid-sentence still cannot redirect the sentence in progress** (spec `023`).
- **No latch survives the silence it was made in.**

## Requirements

### Functional

- **FR1** — **Changing the live lane with no utterance in flight clears the latch**, so the next
  utterance latches the lane he actually switched to.
- **FR2** — **Changing the live lane while an utterance is IN FLIGHT leaves the latch alone**,
  where "in flight" spans from the first word to the moment the turn is routed — **including the
  seconds of transcription after he has stopped speaking.**

  🔴 **HE CORRECTED THIS REQUIREMENT MID-IMPLEMENTATION, AND THE CORRECTION EXPOSED A BUG IN THE
  FIX.** Told that the guard protected switching mid-sentence, he answered: *"that is not a
  requirement. I would never switch while talking."* · *"I don't know if that requirement is making
  this thing more complicated than it needs to be."* **He is right about the case he named and it
  is the wrong case.** The first draft asked `buffer.speech_active`, which reads false for the
  entire 3.3 s of ASR measured above — so it would have cleared the latch during exactly the window
  spec `023` was written for, which is the gesture he asked for in the first place: *"I would like
  to finish speaking and be able to switch to a different lane while that thing that I just said
  just finishes transcribing."* **The requirement is not about talking; it is about the utterance
  not yet having landed.**
- **FR3** — **Both ways of switching clear it** — a tap and a spoken wake name — because both are
  him addressing somebody on purpose.

### Non-functional

- **NFR1** — **No new state.** The signals needed (is a lane switch happening, is speech active)
  both already exist at the switch site.

### Technical constraints

- **TC1** — **"In flight" is `state.talking()`, and nothing narrower.** It already composes the
  three signals — the buffer's speech, the client's own report, and `speech_pending` for an
  utterance closed and still in transcription — and it exists because *"both speech signals read
  false while he has in fact just spoken."* Any predicate assembled at this call site instead would
  be a second answer to a question that already has one place to ask it.
- **TC2** — **A switch to the lane already live must still clear a stale latch.** `_set_lane` is
  documented as idempotent, and the no-op case is exactly the "he taps the lane he is already on"
  gesture that should reset a wrong latch.

## Implementation Tasks

- [ ] Clear `utterance_lane` in `_set_lane` when the audio buffer reports no speech in progress.
- [ ] Cover the mid-utterance case so spec `023` cannot regress.

## Acceptance Criteria

- [ ] **AC-1** `unit:tests/test_utterance_lane.py` — **FR1.** A latch set before a switch does not
      survive it when he is silent: the next utterance routes to the lane he switched to.
      **Verify by mutation:** removing the clear turns this red.
- [ ] **AC-2** `unit:tests/test_utterance_lane.py` — **FR2.** A switch DURING an open utterance
      leaves the latch, and the in-flight words still route to the lane he began on.
- [ ] **AC-3** `unit:tests/test_utterance_lane.py` — **FR3/TC2.** A spoken wake-name switch clears
      it too, and so does a switch to the lane already live.
- [ ] **AC-4** `unit:` full suite — `_set_lane` is on the wake path, the tap path and the restart
      path.
- [ ] **AC-5** `manual` — **FR1.** Switch lanes, wait, then speak: the turn lands on the lane he
      switched to. Manual because the latch is set by real audio crossing a real threshold, which
      is the part a unit test necessarily stubs.

## Testing Approach

### Validation Steps

1. Write each assertion, then **break the fix and confirm the assertion goes red** — the standard
   this repo adopted after two worthless tests shipped on 2026-08-26.
2. Run the full unit suite.

### Test Cases

| Situation | Expected |
|---|---|
| latch set, he is silent, lane switched | latch cleared; next utterance routes to the new lane |
| latch set, utterance open, lane switched | latch kept; that utterance routes to the old lane |
| latch set, switch to the lane already live | latch cleared (TC2) |
| no latch set, lane switched | nothing to clear, no error |

## Out of Scope

- **Expiring the latch on a timer.** A wall-clock bound would need a number nobody can justify, and
  the switch is a better signal than any duration: it is him saying who he is talking to.
- **Clearing the latch when speech merely stops.** The end of speech is where the turn is produced,
  and the existing clear already covers the path where one arrives. Adding a second clearer there
  would risk racing the routing that reads it.

## References

- `specs/023` — the latch this repairs; its guarantee is FR2 here.
- `specs/026` — the same shape one layer up: a value written on one path and never invalidated.
