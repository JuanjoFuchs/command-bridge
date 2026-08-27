---
id: "026"
title: Every gate knows its lane
status: in_progress
blocked_by: []
blocks: []
---

# Every gate knows its lane

## Overview

Nine specs have now fixed one instance each of a single defect: **a fact made per-lane in STORAGE
and left session-wide in RESOLUTION.** He stopped asking for the tenth fix and asked for the sweep
instead — an audit of every gate and every field, so the remaining instances are found by looking
rather than by being bitten.

The sweep found **four**. One is the bug he reported. Three had never been seen.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"But I didn't barge in. I just switched the lane and was listening. I didn't interrupt you."*
>
> *"Yes, definitely. And I need you to run a critical audit of all these gates and all the features
> to make sure that they support and they are lane aware."*
>
> *"Once you run the full audit, I want you to compile all the findings and for the gaps where the
> features and commands and server status and state and whatever is not lane aware and should be,
> write a spec and fix it and test it."*
>
> (2026-08-26, voice-dictated; verbatim record in `sessions/dev.jsonl`, turns 3186–3189 — that log
> is gitignored, so the quotes carry the weight and not the ids)

## Findings — the whole surface, counted

`TunnelState` carries **58 fields: 13 per-lane, 45 session-wide.** Most of the 45 are session-wide
correctly and must stay that way — there is one microphone, one browser, one recognizer, one
voiceprint, one process. A lane is a conversation, not a device.

The test applied to each: **does this field describe a CONVERSATION, or the MACHINE?** Four failed.

### F1 — `agent_state` is a cache with one writer and no invalidation 🔴

`_set_agent_state` writes it only `if who == state.lanes.current`. `_set_lane` never recomputes it.
**So the instant he switches lanes, `agent_state` describes the lane he left**, and stays wrong
until that new lane happens to change state.

The barge gate reads it: `if not config.barge_in_enabled() or state.agent_state != "speaking"`.
He switched to Atlas while Magnus was still speaking, said something to Atlas, and the gate — still
holding Magnus's `speaking` — fired and killed Magnus's clip. That is exactly what he described,
and *"I didn't interrupt you"* is the correct account of it.

🎯 **The distinction the gate actually needs is not the one it was reaching for.** Two different
questions were being answered by one stale field:

- **"Is audio in the air?"** — the physical question, and the honest source is `speaking_lane`.
- **"Whose reply is he interrupting?"** — the conversational question, answered by comparing that
  lane against the one he is addressing.

**Both answers matter, and they lead to different behaviour.** If he interrupts the agent he is
talking to, the clip has been heard and must not come back (spec `025`). If he speaks to somebody
else while a clip plays, the audio still has to stop — he cannot listen and talk at once — but that
agent has NOT been answered, so its reply belongs back in its hold with the hand up.

### F2 — `_flush_undelivered` replays into whichever lane is live now

`_speak` tests `off_lane` **before** queueing, so `undelivered` only ever holds clips for the lane
that was live **then**. The flush, on reconnect, sends all of them unconditionally into the lane
that is live **now**.

⚠ This is spec `012` TC3 in the sibling store. The comment at the enqueue site says the two stores
are *"DELIBERATELY NOT"* shared, precisely so this bug is not inherited — and then the flush side
never got the check the hold side has. **A guarantee enforced on one side of a queue is not a
guarantee.** Same shape as spec `022`, where `lane_held` was protected and the flush walked the
clips out of it.

### F3 — the blue tick is fed a session-wide cursor

`state.lane_read_through[_who] = state.consumed_cursor`. `lane_read_through` is per-lane;
`consumed_cursor` is whatever cursor **any** lane last reported, assigned unconditionally and not
even monotonically. `lane_consumed[lane]` — the per-lane, monotonic value — already exists, and
`lane_cursor()` already exists to read it. So lane X's read-through tick claims X answered up to a
point that lane Y reported.

### F4 — `/status` and `/wait` publish the same stale cache

Both return `state.agent_state` and inherit F1 verbatim. Fixing F1 at the field fixes both, which is
why this spec changes the field rather than its three readers.

### F5 — a page that dies takes its clips with it

Found while writing the guard for F1. When the last client disconnects, the handler cleared
`capturing` and `user_speaking` and stopped there. Everything in `lane_inflight` — sent, never
confirmed — stayed there, and no receipt was ever coming from a socket that no longer exists. The
clips were simply gone, which is the one thing `lane_held` exists to prevent.

⚠ And `speaking_lane` survived too, which after F1's fix means **a permanently open barge gate**:
the first thing he says after reconnecting registers as an interruption of nobody, and a barge
clears `undelivered` — where his replies were waiting while the phone was away.

### F6 — `lane_inflight` was only ever populated by ONE of the two senders

Found when F4's guard could not see the clip it was supposed to hand back. Spec `022` added
`lane_inflight` to `_flush_lane_held`, because that was the path that lost Kepler's three replies.
`_speak`'s own delivery path — his lane was live, so the clip went straight out — recorded nothing.
**The same guarantee applied to one of two senders is not a guarantee**, which is F2's shape again
in a third place.

🔴 **And `playing_clip` was wrong for a day.** Spec `025` stored it, assigning on every send — so
with two replies out it named the SECOND, the one still queued, while the browser played the first.
A barge would have handed back the reply he had just heard and dropped the one he had not: the
exact inversion of the fix that spec was written to make. It is now **derived** from the send order,
like `agent_state`, and a derived value cannot be stale.

### What was checked and is correctly session-wide

`buffer`, `recognizer`, `embedder`, `clients`, `channel_open`, `capturing`, `client_sr`, `muted`,
`user_speaking`, `speech_pending`, `barge_buf`, `barges`, `last_barge_score`, `partial_text`,
`last_audio_at`, `token`, `session`, `wake`, `voice_samples`, `turns_logged`, `watch_open` — all
properties of the device or the process. `speaking_lane`, `utterance_lane` and `playing_clip` are
session-wide by design: they are *singletons that NAME a lane*, which is the correct shape for
"one speaker, one microphone".

`cues_enabled`, `speech_speed` and `sentence_pause` are session-wide settings that arguably belong
per-lane — that is his existing backlog item for **deterministic per-lane name, colour and voice**,
and it is deliberately not folded in here.

## Goals

- **A gate fires on the lane it is about**, not on whichever lane was live when a cache was last
  written.
- **Speaking to one agent never destroys another agent's reply.**
- **Nothing an agent said is lost without somebody being told** — extended to the last queue that
  did not honour it.

## Requirements

### Functional

- **FR1** — **`agent_state` is derived from the live lane**, never stored, so a lane switch cannot
  leave it stale.
- **FR2** — **The barge gate opens on audio actually being in the air**, whichever lane owns it.
- **FR3** — **An interruption of the lane he is addressing behaves as it does today**: the clip is
  heard and does not come back.
- **FR4** — **Speaking while a DIFFERENT lane's clip plays stops the audio and returns that clip to
  its lane's hold**, hand up. It was not answered.
- **FR5** — **`_flush_undelivered` re-tests the lane at flush time**, and parks anything off-lane in
  that lane's hold instead of playing it at him.
- **FR6** — **The read-through tick uses the lane's own cursor.**
- **FR7** — **A page that goes away hands back what it was holding**, and closes the barge gate.
- **FR8** — **Both senders record what they sent**, so anything droppable is returnable.
- **FR9** — **`playing_clip` is derived from the send order**, never assigned.

### Non-functional

- **NFR1** — **No new state.** Every fact this needs is already recorded; the audit found
  resolution bugs, not missing information.

### Technical constraints

- **TC1** — **`agent_state` keeps a setter.** Three test files and one call site assign it, and its
  meaning — "the live lane's state" — is unchanged. Assignment routes to `lane_states[current]`.
- **TC2** — **The cross-lane barge must not clear that lane's hold.** Spec `012` TC3: the wholesale
  clear is right for the lane he interrupted and wrong for one he cannot hear.
- **TC3** — **An undelivered clip must record its lane at enqueue time**, because by flush time the
  live lane has moved — which is the bug.

## Implementation Tasks

- [x] `agent_state` becomes a property over `lane_states[lanes.current]`, with a setter.
- [x] The barge gate keys on `speaking_lane`; the disposition splits on lane.
- [x] `_return_inflight` takes `keep_playing`, so a cross-lane barge returns the clip too.
- [x] `undelivered` entries carry their lane; the flush re-tests it.
- [x] `lane_read_through[_who] = state.lane_cursor(_who)`.
- [x] `_drop_client` extracted from the websocket `finally` and given the F5 cleanup.
- [x] `_speak` records what it sends in `lane_inflight`.
- [x] `playing_clip` derived from a send-order ticket rather than assigned.
- [x] The barge keeps the interrupted clip's owner, whose receipt is still coming.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_lane_aware_gates.py` — **FR1.** After a lane switch, `agent_state`
      reports the NEW lane's state, not the old one's. **Killed mutation M1.**
- [x] **AC-2** `unit:tests/test_lane_aware_gates.py` — **FR2/FR3.** A barge on the lane he is
      addressing still fires and still does not return the played clip. **Killed M2.**
- [x] **AC-3** `unit:tests/test_lane_aware_gates.py` — **FR4.** Speaking while another lane's clip
      plays stops it AND returns it to that lane's hold with the hand up. **Killed M3 and M6.**
- [x] **AC-4** `unit:tests/test_lane_aware_gates.py` — **TC2.** That cross-lane barge leaves the
      other lane's existing hold intact.
- [x] **AC-5** `unit:tests/test_lane_aware_gates.py` — **FR5.** A clip queued while he was away is
      parked, not played, when he reconnects on a different lane. **Killed M4.** Its sibling proves
      the check does not simply park everything.
- [x] **AC-6** `unit:tests/test_lane_aware_gates.py` — **FR6.** Two lanes with different cursors:
      each tick reads its own. **Killed M5.** Driven through `handle_say`, because the tick is
      written by the HTTP handler and a test calling `_speak` would prove nothing.
- [x] **AC-7** `unit:tests/test_lane_aware_gates.py` — **FR7/FR9.** A dead page hands its queued
      clip back and closes the gate. **Killed M8 and M9.**
- [x] **AC-8** `unit:tests/test_lane_aware_gates.py` — the receipt that follows a barge still names
      its own lane, and does not release whichever lane he has since turned to. **Killed M7.**
- [x] **AC-9** `unit:` full suite — this changes a field read on nearly every path.
- [ ] **AC-10** `manual` — **FR4.** With one agent speaking, switch lanes and say something to
      another: the first agent's hand comes back up and its reply plays when you return. Manual
      because the barge needs a real voice above the threshold.

## Testing Approach

### Validation Steps

1. Write each assertion, then **break the fix and confirm the assertion goes red.** Two worthless
   tests shipped on 2026-08-26 by re-implementing the server's own line in a local helper; the rule
   that came out of it is at the top of `tests/test_lane_hold.py` — **drive the real path or do not
   claim to.**
2. Run the full unit suite.

### Test Cases

| Situation | Expected |
|---|---|
| switch lanes while an agent is speaking | `agent_state` follows the new lane |
| barge on the lane he is addressing | clip counted heard, not returned |
| speak while another lane's clip plays | audio stops, clip returns to that lane's hold |
| that same cross-lane barge | the other lane's existing hold untouched |
| reconnect on a different lane than the clip's | clip parked in its lane's hold |
| two lanes, different cursors | each read-through tick reads its own |

## Out of Scope

- **Per-lane voice, colour and name.** His backlog item, deliberately separate: it is a feature with
  a design question attached (how many lanes to render), not a resolution bug.
- **Retiring `speaking_lane`, `utterance_lane`, `playing_clip`.** Singletons that name a lane are
  the right shape for one speaker and one microphone.

## References

- `specs/013`, `015`, `016`, `017`, `018`, `023`, `024` — the same seam, one instance per spec.
- `specs/012` TC3 — the hold-versus-undelivered split that F2 completes.
- `specs/022`, `025` — the in-flight store this spec's barge change threads through.
