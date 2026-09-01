---
id: "019"
title: A restart keeps the room
status: in_progress
blocked_by: []
blocks: []
---

# A restart keeps the room

## Overview

A lane registered with `lane add` lives only in the running process. Restart the server and the
registry is rebuilt from `serve --wake` alone, so every other agent's lane is gone and every one of
their blocking `watch` calls dies with the socket.

**The turn log survives, which is what makes this easy to miss**: the conversation comes back
intact and only the room is empty.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"will restarting the tunnel kill the other lanes? Will it impact the other agents?"*
> (2026-08-24 — the answer was yes on both counts)

> *"Now let's implement those three fixes."* · *"Do we need a spec for them?"* (2026-08-25)

## Findings — two of the three "fixes" were already fixed

**He asked for three restart failures to be fixed. Measured before writing anything, only one is
still real** — the other two were closed by later specs and the roadmap never caught up:

| Roadmap row | Measured 2026-08-25 | Verdict |
|---|---|---|
| Lanes do not survive a restart | `['magnus','atlas','kepler']` → `['magnus']` | 🔴 **real** |
| A restart marks every unread turn as read | cursor `5` against a head of `8`, from the persisted file | ✅ already fixed |
| A clip into a closed orb is lost | queued 1, delivered 1 on reopen, bytes on the wire | ✅ appears fixed |

⚠ **The third result is weaker than it looks and is NOT being closed on it.** The measurement here
is one clip inside a test process; the original was **nine clips across ~25 minutes on a real
phone**, and the reopen path now runs on both a reconnect and a channel reopen. It needs a live
re-test before that row is struck.

🎯 **The transferable half: a stale board costs more than a missing one.** Three rows were carried
as open, he asked for all three, and two would have been re-implemented on top of working code.
**Measuring first cost one probe script; believing the board would have cost a day.**

## Goals

- **A restart changes who is listening, never who is invited.**
- **The flag he typed still decides the default lane** — persistence must not quietly outrank it.
- **A single-agent session is untouched**, including on disk.

## Requirements

### Functional

- **FR1** — **The registered lane set survives a restart.** Whatever `lane add` registered is there
  again when the server comes back.
- **FR2** — **A removal survives too.** Persisting additions alone would make `lane remove` a
  suggestion a restart reverses.

### Non-functional

- **NFR1** — **A single-agent session is unchanged**, and writes nothing.
- **NFR2** — **The registry keeps its no-I/O contract.** It is pure state so the routing rules stay
  unit-testable without a session directory; the persistence lives with the code that already has
  a session name.

### Technical constraints

- **TC1** — 🔴 **The DEFAULT lane is not restored, only the guests.** `--wake` is what he typed on
  this start, and a persisted default silently overriding it would make the flag a suggestion and
  make the lane a turn belongs to depend on a file he cannot see. A previous default comes back as
  an ordinary lane, so turns already stamped with it still reach somebody.
- **TC2** — **A bad file must not stop the server starting.** It is written best-effort and read on
  the start path; a truncated write, a hand-edit or a name that is no longer valid degrades to "no
  guests". A tunnel that refuses to start is worse than one that asks him to re-add a lane, which
  is exactly the behaviour being replaced.
- **TC3** — **Written on the change, not on shutdown.** A server is stopped by `stop`, by Ctrl-C,
  by a crash and by the machine sleeping; only the first runs anything. A save-on-exit would be
  absent in precisely the cases a restart follows.

## Implementation Tasks

- [x] The lane set is read and written beside the turn log, in the same shape and with the same
      best-effort discipline as the persisted read cursor.
- [x] The server restores the guests when it constructs its registry, skipping the default and
      skipping any name that no longer validates.
- [x] `lane add` and `lane remove` both persist the resulting set.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_lanes_survive_a_restart.py` — **FR1.** Lanes added before a restart
      are present after it. **Verified by mutation:** removing the restore turns this red, along
      with three of its siblings.
- [x] **AC-2** `unit:tests/test_lanes_survive_a_restart.py` — **FR2.** A removed lane stays removed
      across a restart.
- [x] **AC-3** `unit:tests/test_lanes_survive_a_restart.py` — **TC1.** Restarting under a different
      `--wake` name takes the new default and still restores the guests, including the previous
      default as an ordinary lane.
- [x] **AC-4** `unit:tests/test_lanes_survive_a_restart.py` — **NFR1.** A single-agent session
      restores nothing and writes no file.
- [x] **AC-5** `unit:tests/test_lanes_survive_a_restart.py` — **TC2.** A corrupt file and an invalid
      name both degrade to "no guests" rather than to a server that will not start.
- [ ] **AC-6** `manual` — **FR1.** On his machine with three lanes: restart, and confirm the other
      agents' watches resume without anything being re-added by hand. Manual because the failure is
      paid by other processes reconnecting, which a single-process test cannot stage.

## Testing Approach

### Validation Steps

1. Run `tests/test_lanes_survive_a_restart.py`; confirm AC-1 fails when the restore is removed.
2. Restore, confirm the file passes with no assertion relaxed.
3. Run the full unit suite — `TunnelState` construction is on the path of nearly every server test,
   so a read on that path has to be invisible to all of them.

### Test Cases

| Situation | Expected |
|---|---|
| two lanes added, restart | both present |
| one removed, restart | the removal holds |
| restart under a new `--wake` | new default, guests restored, old default now a guest |
| one lane only | nothing written, nothing restored |
| corrupt file / invalid name | server starts, guests skipped |

## Out of Scope

- **Persisting which lane is LIVE.** A restart is a break in the conversation and he re-engages
  with a wake word anyway; restoring the live lane would put him mid-thread with an agent that has
  no memory of it.
- **Per-lane read cursors across a restart.** Tracked with spec 017 NFR2, where the session cursor
  is the documented fallback.
- **The lost-clip row.** Measured as apparently fixed above, but on evidence too weak to close —
  it needs a live re-test, not a re-implementation.

## References

- `specs/012` — lanes; the registry this makes durable.
- `specs/017` — the persisted read cursor, whose shape and best-effort discipline this copies.
- Voice Tunnel — the board, which was stale on two of the three rows this spec came from.
