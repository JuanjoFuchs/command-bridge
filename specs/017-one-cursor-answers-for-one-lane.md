---
id: "017"
title: One cursor answers for one lane, everywhere it is asked
status: in_progress
blocked_by: []
blocks: []
---

# One cursor answers for one lane, everywhere it is asked

## Overview

Spec 015 gave every lane its own read cursor and pointed the transcript's tick marks at it. It did
not point anything else at it. **Every other question of the form "where have you read to" — the
unread count, the refusal's remedy, the resume hint the watchdog reads out of `status` — kept
asking `consumed_cursor`, which is one number for the whole session that every lane's `watch`
overwrites.**

So a lane can be told it is caught up on turns it never received, by the arithmetic rather than by
a bug in the delivery, and everything downstream agrees with it.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim (2026-08-25)

> *"What about the cursor? Is the cursor a single number for the full server? Or is now the cursor
> an independent number per lane?"*

> *"Let's say if you are at cursor 100 and I switch to a different lane, give a few turns, what is
> the cursor number that that other lane is going to have? Is it going to go to 101, 102, 103? Or is
> it also going to have its own 100?"*

> *"Make sure all of that is fixed, then commit all the work we have pending on that repo."*

## Findings — measured on the live session while he asked

**`status` on his own three-lane session, at the moment the question was asked:**

```
consumed_cursor  2604
last_turn_id     2604
lane_consumed    {'magnus': 2604, 'atlas': 2597, 'kepler': 2589}
pending_turns    0
```

magnus had just read, so the session cursor sat at the head of the log. **A kepler agent asking
where to resume would have been handed 2604 and skipped fifteen turns**, and `pending_turns` read
`0` throughout — so nothing anywhere reported a problem.

**And the answer to his second question is the reason this is invisible rather than obvious: turn
ids are GLOBAL.** One sequence for the whole log, whatever lane a turn belongs to. Turn 101 exists
exactly once. A lane's cursor is a position on one shared ruler, not a private count — so a cursor
borrowed from the wrong lane is always a plausible number, never an out-of-range one.

🎯 **This is the fourth appearance of one shape, and the pattern is now specific enough to state as
a rule: making a fact per-lane means changing where it is STORED *and* every place it is
RESOLVED.** Spec 013 stored the transcript tag per lane and left the resolution global. Spec 015
stored the cursor per lane and left the resolution global. Spec 016 stored the state per lane and
left the resolution global. Each time, the half that was done made the half that was missing
invisible, because the feature demonstrably worked in the place someone was looking.

## Goals

- **One question, one answer, wherever it is asked:** how far a given lane has read.
- **A lane is never told it is caught up on turns it did not receive.**
- **A single-agent session is arithmetically identical to before.**

## Requirements

### Functional

- **FR1** — **There is one place that answers "where has this lane read to"**, and everything that
  needs the answer asks it there.
- **FR2** — **The unread count and the turns it names are measured against the asking lane's
  cursor**, not the session's.
- **FR3** — **A refusal's remedy resumes from the same cursor the refusal was measured against.**
  Two numbers here make the refusal unescapable: the remedy fetches turns the complaint was not
  about, so the cursor never passes the turns being complained about.
- **FR4** — **The scheduled watchdog prompt names the per-lane cursor**, because that prompt is the
  text an agent actually follows and `describe` outranks every prose copy of it.

### Non-functional

- **NFR1** — **A single-lane session is arithmetically unchanged.** With one lane the two numbers
  are the same number.
- **NFR2** — **The session cursor is the fallback, not the rival.** A lane that has never reported
  has no cursor of its own, and thousands of turns predate lanes; for both, the session number is
  the honest answer. Falling back to zero would replay the entire log at a newly-joined agent.

### Technical constraints

- **TC1** — **Turn ids are global and monotonic across all lanes.** Nothing here may reinterpret an
  id as lane-relative; two lanes never hold the same id, and that is what the whole scheme rests on.
- **TC2** — **A cursor only moves forward.** A `watch` resuming from an older `--since` is
  re-reading, not un-reading.
- **TC3** — **`status` has no lane.** It answers for the server, so it keeps publishing
  `consumed_cursor` and `lane_consumed` side by side and lets the caller pick — which is why FR4 is
  a documentation requirement rather than a code one.

## Implementation Tasks

- [x] One accessor answers "where has this lane read to", with the session cursor as the documented
      fallback.
- [x] The unread computation measures against that cursor and publishes it, so no caller has to
      re-derive it.
- [x] The refusal's remedy and its `since` field both come from that published number.
- [x] The watchdog prompt in `describe` sends agents to `lane_consumed[<your lane>]`, with
      `consumed_cursor` named as the fallback and the reason it is wrong on a multi-lane session.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_lane_cursor_is_per_lane.py` — **FR1, NFR2.** A lane with its own
      cursor gets its own answer; a lane without one falls back to the session cursor; neither says
      anything about any other lane.
- [x] **AC-2** `unit:tests/test_lane_cursor_is_per_lane.py` — **FR2.** His scenario shrunk: two
      turns for one lane, another lane reads past them, and the first lane still reports both as
      unread. **Verified by mutation** — restoring the session cursor turns this red.
- [x] **AC-3** `unit:tests/test_lane_cursor_is_per_lane.py` — **FR3.** The refusal's `since` and its
      remedy command carry the same number the count was measured against. **Also goes red under
      the same mutation.**
- [x] **AC-4** `unit:tests/test_lane_cursor_is_per_lane.py` — **NFR1.** A one-lane session answers
      identically whether or not the lane has ever reported.
- [x] **AC-5** `unit:tests/test_lane_cursor_is_per_lane.py` — **FR4.** The scheduled prompt names
      `lane_consumed`, and keeps its older `turns_logged` warning.
- [ ] **AC-6** `manual` — **TC1.** On his phone with three lanes: let one lane read ahead, then have
      a behind lane resume from what `status` offers it and confirm it receives the turns it had
      not seen. Manual because it needs three real agents reading at different rates, which is the
      condition that produced the measurement above and cannot be staged from one process.

## Testing Approach

### Validation Steps

1. Run `tests/test_lane_cursor_is_per_lane.py`; confirm AC-2 and AC-3 fail when the accessor is
   replaced by `consumed_cursor` — the mutation is the evidence the assertions bite.
2. Restore, confirm the file passes with no assertion relaxed.
3. Run the full unit suite: the refusal path is shared with `test_say_refusal.py` and
   `test_pending_turns.py`.

### Test Cases

| Situation | Expected |
|---|---|
| lane has its own cursor | that lane's number |
| lane has never reported | the session cursor |
| another lane reads past this lane's turns | this lane still reports them unread |
| refusal raised for a lane | remedy resumes from that lane's cursor |
| one lane in the session | identical to before this spec |

## Out of Scope

- **Making `pending_turns` per-lane.** It feeds the session-level `consumed` broadcast and
  `status`, which have no lane to answer for (TC3). The per-lane count that an agent actually acts
  on is the unread count in FR2.
- **Persisting per-lane cursors across a restart.** Tracked separately in Voice Tunnel; the
  session cursor already persists and NFR2 makes it the fallback.

## References

- `specs/013` — the transcript tag: stored per lane, resolved globally. First of the four.
- `specs/015` — the read cursor: stored per lane, resolved globally. This spec is its other half.
- `specs/016` — the agent state: same shape again, fixed the same day.
- Voice Tunnel — the board, with his words.
