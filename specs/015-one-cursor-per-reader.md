---
id: "015"
title: One cursor per reader, and idle means idle
status: in_progress
blocked_by: []
blocks: []
---

# One cursor per reader, and idle means idle

## Overview

**Two defects JJ found by looking at the page, both of which every harness was green for.** They
share a cause: a field that meant one thing while there was one agent, left unexamined when there
were three.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim (2026-08-24)

> *"I think we may have a bug because I think I have noticed that whenever one lane reads, I think
> we're marking everything as read."*

> *"I have noticed that the agents say idle, but in reality, you are not idle, right? Whenever
> you're not listening and whenever you're doing something, you're thinking."*

> *"You need to build a way to mimic everything from the server on the UI so that you can drive the
> UI with synthetic statuses and so that you can simulate every scenario and you can properly test
> the UI with screenshots and stuff."*

## Goals

- **A receipt speaks for one reader**, never for the room.
- **The orb row tells the truth about what each agent is doing**, including the agents that never
  say anything about themselves.
- **The page can be driven into any server state and photographed**, so a defect that is visible is
  also assertable.

## Requirements

### Functional

- **FR1** — **The read receipt is per lane.** A turn shows read when the lane it was addressed to
  has consumed past it, and not because another lane has.
- **FR2** — **A lane that has read turns and is not sitting in a `watch` reads as WORKING**, not
  idle. A lane in a wait reads as listening; a lane that has never read anything stays idle.
- **FR3** — **A UI simulator** that can put the page into an arbitrary server state — lane set,
  live lane, per-lane states, holds, cursors, session on or off — and screenshot it.

### Non-functional

- **NFR1** — **A single-lane session is unchanged.** With one reader the session cursor *is* that
  reader's cursor, and the derived states collapse to what the page shows today.
- **NFR2** — **No new inference.** FR2 is a lookup over `watching_lanes`, a fact the server already
  holds. Nothing here may guess from content or timing.
- **NFR3** — **A reported state always wins over a derived one.** The derivation fills a silence; it
  must never override an agent that spoke.

### Technical constraints

- **TC1** — **Cursors are monotonic per lane.** A `watch` resuming from an older `--since` is
  re-reading, not un-reading; a receipt that flickered back to grey is worse than one never drawn.
- **TC2** — **A turn with no lane falls back to the session cursor.** Thousands of turns predate
  lanes and had exactly one reader; that reader's cursor is the session one.
- **TC3** — 🔴 **The server only learns a state an agent REPORTS.** That is the whole cause of FR2,
  and it cannot be fixed by asking agents to report more — an agent heads-down is by definition not
  making calls.

## Findings — the pattern behind all three

**Every defect in this spec was found by JJ looking at the page, and every one of them had a green
harness over it.** Today's list, in order: a chip strip rendering underneath the orb row that
replaced it; `display:grid` beating the `hidden` attribute and eating 165px of transcript; the busy
timer silently lost with the orb that contained it; the read cursor above; and `idle` standing for
four different situations.

🎯 **The common shape: a golden over a PURE MODEL cannot see a missing, duplicated or lying VIEW.**
`orbstate.py` matched its 480 cases through all five. The model was right every time; what was on
screen was not. **His simulator (FR3) is the correction** — an instrument that drives the real page
into a named state and photographs it, so the thing being checked is the thing he looks at.

## Implementation Tasks

- [x] Server keeps `lane_consumed: {lane: id}`, advanced monotonically by the lane on `/consumed`.
      **The watch was already sending its lane and the server was discarding it.**
- [x] `lane_consumed` published in `/status` and on the `consumed` broadcast.
- [x] Rows carry their lane; the receipt reads that lane's cursor, falling back to the session
      cursor for a turn with no lane.
- [x] `watching_lanes` and `lane_consumed` sent on every `agent_state` broadcast and at handshake.
- [x] `laneOrbsView` derives listening / working / idle when no state was reported.
- [x] FR3 — the simulator: `scripts/uisim.py`.

## Acceptance Criteria

- [x] **AC-1** `harness:scripts/lanestrip.py` — **FR1.** With `lane_consumed: {codex: 40}`, a codex
      turn at 40 reads and a claude turn at 10 does NOT. This is his exact report, as an assertion.
- [x] **AC-2** `harness:scripts/lanestrip.py` — **FR2, NFR2, NFR3.** A lane in a wait reads
      listening; one that has read and is not waiting reads working; one that never read stays
      idle; and a reported state beats all three.
- [x] **AC-3** `unit` — **NFR1.** The full suite passes unchanged.
- [x] **AC-4** `harness:scripts/uisim.py` — **FR3.** Five named scenarios, each asserted AND
      photographed at phone and desktop; starts no server and proves it.
      **Verified by mutation:** reintroducing the `display:grid`-beats-`hidden` bug turns
      *"the single orb is gone, not hiding behind them"* red.
      🔴 **And the first version of that check did NOT go red** — it read the `hidden` ATTRIBUTE,
      which was set correctly all along while the element rendered anyway. The same lying-view
      failure the harness exists to catch, made inside the harness. It now measures a zero-height
      box, which is the only answer that survives every way an element can be visible while
      claiming otherwise.
- [ ] **AC-5** `manual` — **TC3.** On his phone with three agents: a background agent working shows
      as working, and reading in one lane leaves another lane's ticks grey.

## Out of Scope

- **Persisting per-lane cursors across a restart.** The session cursor already persists; per-lane
  is in memory, and a restart's cursor handling is its own tracked defect in Voice Tunnel.
- **Reporting states more diligently from agents.** TC3 — that is the thing that cannot work.

## References

- `specs/012` — lanes; delivery went per-lane here and the readers did not.
- `specs/013` — the receipt, built on the session cursor. FR1 is the half that was missed.
- `specs/014` — the orb row that made `idle` visible per agent, and so made FR2 obvious.
- Voice Tunnel — both defects on the board, with his words.
