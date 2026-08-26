---
id: "022"
title: Sending is not hearing
status: in_progress
blocked_by: []
blocks: []
---

# Sending is not hearing

## Overview

When he switches to a lane, that lane's held replies are flushed to the browser. Until now the
flush **popped** them out of `lane_held` — the one store barge-in is documented never to touch —
and pushed them into the playback queue, which the very next `stop_playback` empties wholesale.

Between the lane switch and the first playback receipt, an agent's replies existed nowhere the
server could recover them. If he spoke in that window, they were gone, and the hand had already
come down, so nothing on screen said they had ever existed.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"I just switched to Kepler while you were still speaking this last turn."*
>
> *"And after you finished, I didn't hear Kepler's turns. And they had two turns pending. And now
> the hand with the two counts is lost. And I don't know what Kepler wanted to say."*
>
> *"Let's finish the verification and then you fix it."* · *"Go ahead, start the fix."*
>
> (2026-08-26, voice-dictated; verbatim record in `sessions/dev.jsonl`, turns 3024, 3025, 3028,
> 3062 — that log is gitignored, so the quotes carry the weight and not the ids)

## Findings — the guard was real and the clips walked past it

`_maybe_barge` carries an explicit comment, written for spec 012 TC3, saying `lane_held` is
deliberately not touched: *"he has not interrupted an agent he cannot hear."* **That comment was
true and the clips were still lost**, because the protection guards a STORE and the flush had
already taken them out of it.

From the timing log, his exact incident:

```
14:15:51.042  lane               kepler (tap)
14:15:51.116  lane_held_flushed  kepler  count 3      <- popped from lane_held, sent to browser
14:16:07.717  barge_in           score 0.542          <- he speaks; clipQueue.length = 0
                                                      <- no `played` for any of the three
```

🎯 **A guarantee attached to a container is not attached to its contents.** Everything about the
separation of `lane_held` from `undelivered` was correct, tested, and commented — and none of it
applied for the seconds that mattered, because the object being protected had moved.

⚠ **And it failed silently in both directions**, which is why he could only report it as *"I don't
know what Kepler wanted to say."* The hand came down at flush time on the assumption that sending
was delivering.

🔴 **This is the same shape as spec `021`, reached by a different door.** There, a reply expired
and nobody was told. Here, a reply was destroyed and nobody was told. Both are the tunnel losing
speech without leaving a trace, which is the one failure lanes exist to make impossible.

## Goals

- **Nothing an agent said is lost because he happened to speak at the wrong moment.**
- **The hand tells the truth**: down when a reply is on its way, back up if it did not make it.
- **Barge-in keeps its meaning** — it silences the lane he interrupted, and only that one.

## Requirements

### Functional

- **FR1** — **The flush keeps the server's copy until playback is confirmed.** Sending is not
  hearing; everything between the socket and his ear can still drop a clip.
- **FR2** — **A playback receipt releases that copy**, matched by clip id.
- **FR3** — **When the client is told to drop its queue, un-played clips return to their lane's
  hold and the hand goes back up.**

### Non-functional

- **NFR1** — **A clip he actually heard is not retained.** The copy is insurance, not a leak: a
  long session must not accumulate every reply ever flushed.
- **NFR2** — **Barge-in still drops the live lane's queue.** The fix must not turn an interruption
  into a delay.

### Technical constraints

- **TC1** — **Receipts can arrive out of order** when a lane switch interleaves two flushes, so
  the release matches on id rather than popping positionally.
- **TC2** — **Returned clips go in front of anything that arrived while they were in flight**, so
  a lane's replies still reach him oldest-first.
- **TC3** — **The returned clips are re-pruned on the way back**, so the age bound from spec `021`
  still applies and a clip cannot outlive it by making a round trip.

## Implementation Tasks

- [x] An in-flight store, per lane, holding what has been sent and not yet confirmed.
- [x] The flush populates it instead of dropping its only copy.
- [x] The `played` receipt releases the matching clip.
- [x] `stop_playback` returns everything still in flight to its lane's hold and republishes the
      waiting count.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_lane_hold.py` — **FR1/FR3.** A reply flushed on a lane switch and
      then barged over is back on that lane's hold, the hand is up, and it still reaches him on the
      next switch. **Verified by mutation:** removing the return turns this red.
- [x] **AC-2** `unit:tests/test_lane_hold.py` — **FR2/NFR1.** A `played` receipt releases the
      server's copy, so nothing accumulates for a clip he heard.
- [x] **AC-3** `unit:tests/test_lane_hold.py` — **NFR2.** Barge-in still empties the live lane's
      `undelivered`, asserted by driving the real barge path.
- [x] **AC-4** `unit:tests/test_lane_hold.py` — the guard against re-coupling now names the
      DESTRUCTIVE calls. ⚠ **It previously asserted that the string `lane_held` was absent from
      `_maybe_barge`, and this spec broke that proxy without breaking the guarantee** — barge-in
      now writes to the hold in order to protect it.
- [ ] **AC-5** `manual` — **FR3.** On his machine with two lanes: park a reply, switch to that
      lane, speak over it, and confirm the hand returns and the reply plays on the next switch.
      Manual because the window needs a real voice, real barge-in scoring and a real switch to
      open — the exact conditions under which it was found.

## Testing Approach

### Validation Steps

1. Run `tests/test_lane_hold.py`; confirm AC-1 fails with `_return_inflight` removed.
2. Run the full unit suite — the flush is on the path of every lane switch.
3. Restart the server before re-testing live: this is server-side and a running process holds the
   old code.

### Test Cases

| Situation | Expected |
|---|---|
| flush, then barge-in | reply back on the hold, hand up, delivered on next switch |
| flush, then `played` | server's copy released |
| barge-in with nothing in flight | live lane's queue still dropped, nothing else changes |
| two flushes interleaved by a switch | each receipt releases its own clip |

## Out of Scope

- **Making barge-in lane-aware in the browser.** The server-side return makes the client queue
  disposable, which is a smaller and more testable change than teaching the page which clips
  belong to whom.
- **Persisting in-flight clips across a restart.** They are in-memory like `lane_held` itself;
  that gap is on the board and is not this defect.

## References

- `specs/012` — the `lane_held` / `undelivered` separation this restores in practice.
- `specs/016` — its AC-6 is the manual check that surfaced this, and it failed on first attempt.
- `specs/021` — the same silent-loss shape, through expiry rather than interruption.
