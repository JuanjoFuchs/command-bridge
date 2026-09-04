---
id: "014"
title: A barge-in tells the interrupted agent what he heard
status: pending
blocked_by: []
blocks: []
---

# A barge-in tells the interrupted agent what he heard

## Overview

When JJ barges in on an agent's clip — his voiceprint talking over a reply — the server stops the
audio (`_maybe_barge`, `server.py`) and the agent finds out only that its next `watch` returned. It
is not told **which** of its clips he cut into, or **how much of it he heard** before cutting in. So
the agent cannot tell a reply he heard in full from one he heard the first three seconds of, and it
re-covers either everything or nothing. JJ, 2026-09-04, turn 1060: *"when your next watch resolves …
you know that I barged in and on which one of your turns I barged in … maybe we can have that
barge-in with a timestamp so you may know how much of your turn did I hear."*

This spec makes a real barge-in deposit a small record for the interrupted lane, surfaced on that
agent's next `watch`: the clip's text, how many seconds of it played, and its total length. The agent
then knows exactly what he did and did not hear, and can re-cover only the unheard remainder.

The server already knows most of it at barge time — the interrupted clip id (`state.playing_clip`),
the lane, and the clip's text and duration. The one thing only the client knows is **how much
played**: the browser owns the AudioContext, so the offset comes from there, reported back when the
client receives `stop_playback`.

> **Completion rule:** This spec is not complete until all acceptance criteria are verified through
> the methods each names (headless / integration / manual-audio). Build-only verification is
> insufficient. The agent must iterate until verification passes — and the offset ACs are
> irreducibly manual, because a real barge over real playback has no headless proxy.

## Goals

- After JJ barges in on an agent's clip, that agent's **next `watch`** carries a `barge` record
  naming the interrupted clip's text and how much of it he heard.
- The "how much" is the CLIENT's measurement (AudioContext playback position), not a server estimate
  from send time — a queued or network-delayed clip must not report a wrong offset.
- The record is delivered ONCE, like turns: read on the next watch and then cleared, never restated.
- Nothing changes about barge-in's existing disposition (spec 025/026): the clip he interrupted still
  does not come back on a real barge; a cross-lane barge still returns its clip.

## Requirements

### Functional Requirements

- **FR1**: On a REAL barge (he interrupted the agent he was addressing — `cross_lane` is false in
  `_maybe_barge`), the server records a barge event for the interrupted lane: the clip id, the clip
  text, the seconds heard, the clip's total seconds, and a wall timestamp.
- **FR2**: The seconds-heard figure is reported by the CLIENT from the AudioContext position of the
  clip at the moment it received `stop_playback`, not derived server-side from when the clip was sent.
- **FR3**: The interrupted agent's next `watch` payload includes a `barge` field
  (`{clip, text, heard_s, total_s, fraction, at}`) for its lane, alongside any turns — a barge and a
  turn can arrive on the same wait and neither displaces the other.
- **FR4**: The barge record is cleared once returned on a watch (delivered once, like a turn), so a
  later watch does not restate a barge already reported.
- **FR5**: A cross-lane barge (he spoke to another agent over this clip — the clip comes back per spec
  026) does NOT create a barge record: he did not cut this agent off, and its reply still stands. The
  agent learns it went off-lane through the existing lane-event path, not through this record.

### Non-Functional Requirements

- **NFR1**: If the client's offset report is missing or late (an old client, a dropped socket), the
  record still returns with `heard_s: null` and the `clip`/`text` present — knowing WHICH clip he cut
  into is useful even without the exact offset, and absent is not zero.
- **NFR2**: No regression to barge-in's identity gate, its clip-disposition (real vs cross-lane), or
  the `played`-receipt state machine.

### Technical Constraints

- **TC1**: The AudioContext lives in the parent document (`web/index.html`); it is the only party that
  knows the true playback position. The offset therefore requires a client→server message, added to
  the `stop_playback` handling path.
- **TC2**: `heard_s` must be clamped to `[0, total_s]`: clock skew or a late report can otherwise show
  him hearing more than the clip was long.
- **TC3**: The record is keyed by the INTERRUPTED lane (`state.speaking_lane` at barge), not the live
  lane — after a barge the live lane is frequently already someone else.

## Implementation Tasks

- [ ] Client (`web/index.html`): on `stop_playback` with `reason: "barge_in"`, before tearing down the
      playing source, read the clip's elapsed playback seconds from the AudioContext and send
      `{type: "barge_offset", clip, heard_s}` up the control socket.
- [ ] Server (`server.py`): handle `barge_offset` — look up the clip's text and total_s, clamp
      `heard_s`, and store `state.barge_by_lane[interrupted_lane] = {clip, text, heard_s, total_s,
      fraction, at}`. Seed the record at barge time (FR1) with `heard_s: null`; the client message
      fills the offset in (NFR1).
- [ ] Server: expose `barge_by_lane` on the status/consumed path the CLI reads, so a lane's pending
      barge is visible to its watch.
- [ ] CLI (`cli.py cmd_watch`): surface the interrupted lane's `barge` record in the watch payload
      alongside turns (FR3), and acknowledge/clear it once returned (FR4) — the same read-boundary
      mechanism `/consumed` already uses for turns.
- [ ] Confirm a cross-lane barge writes no record (FR5).

## Acceptance Criteria

### Core
- [ ] AC1: After a real barge over a playing clip, the interrupting agent's next `watch` returns a
      `barge` field naming that clip's text. — *manual-audio* (a real barge has no headless proxy)
- [ ] AC2: `heard_s` is within ~0.3s of how long the clip actually played before the barge, and never
      exceeds `total_s`. — *manual-audio*
- [ ] AC3: The `barge` record returns exactly once; the following `watch` does not restate it. —
      *headless* (seed a record in state, assert it is present then absent across two watches)
- [ ] AC4: A barge and a turn arriving on the same wait are BOTH present in the payload. — *headless*

### Disposition
- [ ] AC5: A cross-lane barge writes no `barge` record; the interrupted clip still returns to its hold
      (unchanged spec-026 behaviour). — *integration*

### Degraded
- [ ] AC6: With no client offset report, the record still returns with `clip`/`text` present and
      `heard_s: null`. — *headless*

### No regression
- [ ] AC7: Barge-in identity gating, real-vs-cross-lane disposition, and the `played` receipt path are
      unchanged. — *headless* (existing barge tests stay green)

## Testing Approach

### Validation Steps
1. Serve; on the live page, have the agent `say` a long clip; barge in ~2s in; confirm the agent's
   next `watch` reports the clip text and `heard_s ≈ 2` (AC1/AC2).
2. Seed a `barge_by_lane` record in a unit fixture; assert it returns on one watch and is gone on the
   next (AC3); assert a turn on the same watch coexists with it (AC4).
3. Switch to another agent mid-clip (cross-lane); confirm no record and the clip returns (AC5).
4. Drive a barge with the offset message suppressed; confirm the degraded record (AC6).

### Test Cases
| Input | Expected |
|-------|----------|
| Real barge ~2s into a 6s clip | Next watch: `barge {text, heard_s≈2, total_s≈6, fraction≈0.33}` |
| Two watches after one barge | First carries the record; second does not |
| Barge + a new turn on one wait | Payload has both `turns` and `barge` |
| Cross-lane barge | No `barge` record; clip returns to the hold |
| Barge with no client offset | `barge {clip, text, heard_s: null}` |

## Usage Examples

```
# The agent said a 6s reply; JJ cut in at 2s. The agent's next watch:
command-bridge watch --since <cursor>
#   -> { "turns": [...], "barge": { "clip": "clip-...", "text": "the full reply text",
#        "heard_s": 2.1, "total_s": 6.0, "fraction": 0.35, "at": "..." }, ... }
#   The agent re-covers only what came after ~2.1s, instead of repeating the whole thing.
```

## Out of Scope

- Automatically re-saying the unheard remainder — the record gives the agent what it needs to decide;
  composing the re-cover is the agent's judgment, not the tool's.
- A cross-lane "how much did he hear" figure — the clip comes back in full there (spec 026), so the
  offset is moot; only the interrupt case needs it.
- Word-level "he heard up to word N" via the `say --timings` schedule — a refinement on top of the
  seconds offset; seconds are enough to know what to re-cover.

## References

- `command_bridge/server.py` — `_maybe_barge` (the barge gate; `interrupted_clip`,
  `state.speaking_lane`, `cross_lane`), the `played`-receipt handler (`_on_control`, `kind == "played"`),
  and `record_spoken` (where the clip text is stamped).
- `command_bridge/web/index.html` — the `stop_playback` handler and the AudioContext playback path
  (the `onended`/`AudioContext.currentTime` clip-end tracking is where the offset is read).
- `command_bridge/cli.py` — `cmd_watch` and `_watch_payload`, where turns are surfaced and the
  `/consumed` read-boundary is set; the `barge` field rides the same mechanism.
- specs/voice-tunnel/ barge-in lineage (identity gate, real-vs-cross-lane disposition) and the
  supersede/return specs (012, 025-era) that own the clip's fate after a barge.
