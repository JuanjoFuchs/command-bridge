---
id: "021"
title: An expiry tells the agent
status: in_progress
blocked_by: []
blocks: []
---

# An expiry tells the agent

## Overview

A reply an agent speaks while he is on another lane is held until he comes back. If he does not
come back in time, it is dropped — and until now that happened in complete silence, in both
directions. He never learned a reply had existed; the agent that wrote it went on believing it had
been delivered, so it never restated, and the exchange simply had a hole in it that neither party
could see.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"But why are replies expiring after ten minutes? I haven't made that call. Why do we think
> replies should expire?"*
>
> *"Interesting. All right. So it kind of makes sense, that decision. But I think 10 minutes is too
> short. I think we should make it 30 minutes. And when it expires, it shouldn't tell me. It should
> tell the agent. The agent should be aware that their replies had expired and did not reach me.
> Maybe it helps them restate."*
>
> (2026-08-26, voice-dictated; verbatim record in `sessions/dev.jsonl`, turns 2971 and 2975 — that
> log is gitignored, so the quotes carry the weight and not the ids)

## Findings — how the bound got there, and why he was right to challenge it

He lost a reply and asked where the rule came from. **Nobody had decided it.** `_prune_lane_held`
reused `UNDELIVERED_MAX_AGE_S`, a constant written for a different situation, and inherited the
argument along with the number:

| | `undelivered` | `lane_held` |
|---|---|---|
| Who is listening | **nobody** — the phone is gone | **he is**, to another agent |
| Will he come back | unknown | yes, deliberately — he parked this lane |
| Risk the bound answers | a stale stack read at you an hour later | *(that risk, assumed)* |
| Old bound | 600 s | 600 s, by inheritance |

**Ten minutes is shorter than a single conversation with another lane**, which is exactly how a
reply he was still expecting evaporated while he was two lanes away.

🎯 **And the number was the smaller half.** The expiry was *invisible*, so the failure mode was not
"he got it late" but "an answer existed and no one involved could tell it had gone". His placement
of the notice is the insight: **telling him would be noise on a phone; telling the agent is
actionable**, because the agent is the only party that can restate.

## Goals

- **A parked agent's reply survives as long as he plausibly takes to come back.**
- **An agent learns that its reply never landed**, at a moment when restating is still useful.
- **He is not told.** He asked not to be, and a notice he cannot act on is interruption.

## Requirements

### Functional

- **FR1** — **An off-lane reply is held for thirty minutes**, not ten.
- **FR2** — **When one is dropped, the lane that spoke it is told what was lost** — the text, so
  the agent can decide whether to say it again.
- **FR3** — **The notice reaches the agent as it comes back to listen**, and is reported once.

### Non-functional

- **NFR1** — **A single-agent session is untouched.** Nothing is ever held off-lane there, so none
  of this allocates or reports.
- **NFR2** — **The notice is bounded** like the queue it replaces: it is held in memory for an
  agent that may never return.

### Technical constraints

- **TC1** — 🔴 **The lane hold gets its OWN age bound.** Reusing `UNDELIVERED_MAX_AGE_S` is what
  caused this: two different situations, one number, and the reasoning attached to the other one.
- **TC2** — **Cleared on delivery.** A re-armed watch must not re-announce the same dead reply
  every thirty seconds; an expiry is news exactly once.
- **TC3** — **Text, not audio.** The agent needs to know *what* went unheard so it can restate it;
  the synthesized bytes are worthless to it and expensive to keep.

## Implementation Tasks

- [x] `LANE_HELD_MAX_AGE_S`, separate from the disconnected-phone bound, at thirty minutes.
- [x] The prune records what it drops against the lane that spoke it, bounded by the same count cap.
- [x] `/watching` hands a lane its expiries when it opens a watch and clears them in the same call.
- [x] `watch` surfaces them in its result, beside the turns the agent is about to answer.
- [ ] `describe` documents `expired` on the `watch` return.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_expired_replies.py` — **FR1.** A reply eleven minutes old survives.
      **Verified by mutation:** restoring 600 s turns this and two siblings red.
- [x] **AC-2** `unit:tests/test_expired_replies.py` — **TC1.** The two bounds are different values,
      and the lane hold is the longer one.
- [x] **AC-3** `unit:tests/test_expired_replies.py` — **FR2.** A dropped reply is recorded against
      its lane with its text and clip id. **Verified by mutation:** removing the record turns this
      and three siblings red.
- [x] **AC-4** `unit:tests/test_expired_replies.py` — **FR2.** The notice does not leak to another
      lane, and a lane whose replies are still fresh is told nothing.
- [x] **AC-5** `unit:tests/test_expired_replies.py` — **NFR2.** The notice is capped, keeping the
      most recent — the ones still worth restating.
- [x] **AC-6** `unit:tests/test_expired_replies.py` — **TC3.** The text is read from the clip
      header rather than the hold wrapper, so the notice says *what* was lost rather than only
      *that* something was.
- [x] **AC-7** `unit:tests/test_expired_replies.py` — **NFR1.** A single-agent session populates
      neither store.
- [ ] **AC-8** `manual` — **FR3/TC2.** With two real agents: park one past the bound, come back,
      and confirm that agent is told once and does not repeat the notice on its next watch.
      Manual because it needs two live processes and thirty minutes of wall clock.

## Testing Approach

### Validation Steps

1. Run `tests/test_expired_replies.py`; confirm AC-1 fails at the old 600 s and AC-3 fails with the
   recording removed.
2. Run the full unit suite — `_prune_lane_held` is on the path of every off-lane `say`.

### Test Cases

| Situation | Expected |
|---|---|
| held 11 minutes | kept (dead under the old bound) |
| held 31 minutes | dropped, and the lane is told |
| two lanes, one stale | only the stale lane is told |
| more than the cap expires | the most recent are kept |
| single-agent session | nothing held, nothing reported |

## Out of Scope

- **Telling HIM.** He asked explicitly not to be told, and a notice he cannot act on is
  interruption rather than information.
- **Automatically restating.** Whether a thirty-minute-old answer is still worth saying is a
  judgement about the conversation, which is the agent's to make with the text in hand.
- **Persisting held clips across a restart.** They are in-memory and a restart still loses them;
  that is a separate gap, and it is on the board rather than in this spec.

## References

- `specs/012` — lanes and the off-lane hold this bounds.
- `config.UNDELIVERED_MAX_AGE_S` — the bound this deliberately stops sharing, and why.
