---
id: "025"
title: The clip he heard is not returned
status: in_progress
blocked_by: []
blocks: []
---

# The clip he heard is not returned

## Overview

Spec `022` gave every sent clip a copy in `lane_inflight` so a barge-in could hand back what the
browser's queue was about to drop. It handed back **too much**: the clip that was actually on the
speaker went back too, and played again five minutes later.

The window is eight milliseconds wide and it is not a race that can be lost — it is a race that is
lost **every time**, because the two events are causally linked.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"When I switched back to your lane, you read back a turn that you have said a while ago... And
> when that turn finished playing, your status still said speaking for around, I don't know, 10, 15
> more seconds. And then it went back to thinking. And immediately you played two more turns... And
> in those two turns, while they were playing, you didn't say speaking."*
>
> *"So something's up with the fix you did."*
>
> (2026-08-26, voice-dictated; verbatim record in `sessions/dev.jsonl` — that log is gitignored, so
> the quotes carry the weight and not the ids)

## Findings — a self-inflicted race, measured

From `sessions/dev.timing.jsonl`:

```
16:09:56.120  barge_in           score 0.667
16:09:56.121  inflight_returned  count 1              ← returned the clip on the speaker
16:09:56.129  played  clip-1787774892135              ← its receipt, 8 ms too late
16:14:59.705  played  clip-1787774892135              ← the same clip again, 5 minutes later
```

🔴 **`stop_playback` is what MAKES the receipt.** Stopping the audio element fires `onended` in the
browser, which sends `played`. So the receipt is not merely *sometimes* late — it is **caused** by
the barge and therefore always lands just after it. `_return_inflight` ran first, found the clip
still unconfirmed, and put it back. Then the receipt arrived, found `clip_owner` already emptied,
and released nothing — which is the fifteen seconds of stuck *speaking* he also reported.

🎯 **The fix is a distinction the code never drew: SENT is not HEARD, but neither is it UNHEARD.**
Spec `022` split "sent" from "confirmed played" and treated everything unconfirmed as lost. There
are three states, not two — *queued behind the speaker*, *on the speaker*, and *finished* — and only
the first has genuinely reached nobody.

⚠ **Everything queued BEHIND the interrupted clip must still come back.** That is spec `022`'s whole
purpose and this must not undo it: he interrupted one reply, not the four sitting behind it.

## Goals

- **A clip he has heard is never replayed**, however the playback ended.
- **A clip he has not heard still comes back**, exactly as spec `022` promised.

## Requirements

### Functional

- **FR1** — **The clip on the speaker is recorded as such**, both when sent directly and when it is
  the first of a flush.
- **FR2** — **Barge-in does not return the clip that was playing.**
- **FR3** — **Barge-in still returns everything queued behind it**, and the hand comes back up.
- **FR4** — **The record is cleared by its own receipt**, so the next clip is not mistaken for it.

### Non-functional

- **NFR1** — **One clip id, not a queue.** The browser plays one at a time; anything richer is a
  second model of the client's state to keep in sync.

### Technical constraints

- **TC1** — **Only the FIRST clip of a flush is unreturnable.** A flush sends several and they play
  in order, so the rest are queued and have reached nobody.
- **TC2** — **The receipt must clear it by id**, never unconditionally: a `played` for an older clip
  arriving late would otherwise unlock the one now speaking.

## Implementation Tasks

- [x] `state.playing_clip`, set in `_speak` on the deliverable path.
- [x] Set in `_flush_lane_held` for the first clip only.
- [x] `_return_inflight` filters it out before returning a lane's clips.
- [x] The `played` handler clears it when the id matches.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_lane_hold.py::test_a_flushed_reply_survives_a_barge_in_and_the_hand_comes_back`
      — **FR2/FR3.** Two clips flushed, barge-in: the first is not returned, the second is, and the
      hand shows one. **Verified by mutation:** removing the filter turns it red.
- [x] **AC-2** `unit:tests/test_lane_hold.py` — **FR4.** The matching receipt clears
      `playing_clip`; a non-matching one leaves it.
- [x] **AC-3** `unit:` full suite (1258 tests) — `_speak`, the flush and the control handler are on
      nearly every path in this server.
- [ ] **AC-4** `manual` — **FR2.** Interrupt a reply mid-sentence, then return to that lane: the
      interrupted clip does not play again. Manual because the race needs a real voice, a real
      barge and real browser playback timing to open.

## Testing Approach

### Validation Steps

1. Run the lane-hold suite; confirm AC-1 fails with the filter removed.
2. Run the full unit suite.

### Test Cases

| Situation | Expected |
|---|---|
| one clip playing, barge-in | nothing returned; its receipt releases the lane |
| two clips flushed, barge-in | the second returns, the first does not |
| receipt for the playing clip | `playing_clip` cleared |
| receipt for an older clip | `playing_clip` untouched |

## Out of Scope

- **Partial-clip resumption.** He heard some of it; the tunnel cannot know how much, and replaying
  from a guessed offset would be worse than either alternative.

## References

- `specs/022` — the store this narrows; its guarantee for queued clips is unchanged.
- `specs/024` — `clip_owner`, the sibling map keyed the same way.
- `specs/026` — the audit that followed, which found this same gate reading the wrong lane.
