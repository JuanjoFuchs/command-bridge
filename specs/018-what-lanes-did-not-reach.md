---
id: "018"
title: What lanes did not reach — the audit
status: in_progress
blocked_by: []
blocks: []
---

# What lanes did not reach — the audit

## Overview

Spec 012 made addressing a lane. Specs 013, 015, 016 and 017 each found one more fact that had been
made per-lane in storage and left session-wide in resolution — and each was found by JJ noticing
something wrong on the page, weeks apart, one at a time.

**This spec stops finding them one at a time.** It is an inventory of every piece of state the
server and the CLI hold, sorted into two lists: the facts that are genuinely one per session, and
the facts that describe an agent and are still held once for all of them. The second list is the
work; the first list exists so nobody "fixes" something that is already right.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim (2026-08-25)

> *"I fear that the status leak is a more concerning issue in general because since now we are doing
> multiple lanes, we have to manage multiple statuses."*

> *"I don't want you to change anything yet. I need you to understand the decision [detection] and
> tell me if it's real or not. Do we have logs? Are you able to see this? If not, maybe we should
> have logs."*

> *"it seems we need to improve the timings, the logs and everything else to now support the
> multi-lane architecture. And you need to fix that flag."*

> *"what other parts of the architecture and observability and general everything else should have
> changed and have not changed to support a new multi-lane architecture."*

## Findings — the inventory

### 🔴 Still one per session, and shouldn't be

| # | What | Where | What it costs him |
|---|---|---|---|
| 1 | `agent_holds_turns` | server | **His report.** One boolean decides whether a wait is a fast pre-reply check or a long listen. Any lane raises it by taking a turn; **any lane's empty watch clears it for everyone.** So an agent about to answer him is read as merely listening and waits a full rung — *"time I'm waiting for an answer should have arrived sixty seconds earlier."* |
| 2 | `watch_open` + the watchdog's STEP 0 | server + `describe` | The watchdog is told to do nothing when `watch_open` is true, and that is true while ANY lane watches. **So a lane whose watch has died is never re-armed while another lane is listening.** Observed today: turns 2555–2559 accumulated on `magnus` with `watching_lanes: ['atlas']`. The per-lane fact (`watching_lanes`) already exists and the consumer was never switched to it. |
| 3 | `_empty_streak` | CLI, keyed by session | The backoff ladder is shared. A lane quiet for nine minutes leaves the ladder at its cap, so a **different** lane's first wait after he speaks can open on a long rung. |
| 4 | `last_refusal` / `refusal_repeat` | server | The unread-refusal memo is one pair for the session, so one lane's refusal can make **another lane's FIRST refusal render as a repeat** — ids instead of turn text, on the one occasion the text is what the agent needs. |
| 5 | The timing log | `timing.stamp` calls | **No stamp carries a lane, and there is no watch event at all.** `consumed` records a cursor and nothing else. None of rows 1–4 can be seen in it, which is why row 1 had to be found by reading source. |

### ✅ Genuinely one per session — leave them

| What | Why it is right |
|---|---|
| `muted`, `capturing`, `channel_open`, `user_speaking`, `speech_pending` | Facts about HIM and his device. There is one microphone. |
| `verbose`, `speech_speed`, `sentence_pause`, `cues_enabled` | His preferences for the conversation, not an agent's. |
| `undelivered` | "Nobody is listening at all" — a closed channel or no client. `lane_held` is its per-lane counterpart and spec 012 TC3 is the reason they must stay apart. |
| `agent_state` | Deliberately the LIVE lane's state: it is what the single orb paints, and `lane_states` is the fan-out. Redefining it would change the answer given to every existing caller. |
| `ambiguous`, `last_ambiguous`, `barges`, `last_barge_score` | About the wake gate and the microphone, not about any agent. |
| `errors`, `last_error` | Subsystem health. A TTS failure belongs to the tunnel. |
| `consumed_cursor` | Now the documented FALLBACK for a lane that has never reported (017 NFR2), not a rival to the per-lane cursors. |

🎯 **The pattern worth naming, because it has now produced five defects: a fact becomes per-lane
when its STORAGE changes AND every CONSUMER changes.** Row 2 is the purest case — the per-lane fact
was published by spec 012 and the watchdog that reads it was never told. The storage change is the
visible half, it makes the feature demonstrably work where someone is looking, and that is exactly
what hides the other half.

## Goals

- **Every fact that describes an agent is asked and answered per agent.**
- **The logs can show it.** A defect of this shape should be visible in the timing log rather than
  requiring someone to read the source.
- **The list of session-wide facts is written down**, so the next reader does not have to re-derive
  which ones are deliberate.

## Requirements

### Functional

- **FR1** — **Whether a wait is a pre-reply check or a listen is a per-lane fact.** One lane's
  activity must not change how another lane's wait behaves.
- **FR2** — **The watchdog acts on whether THIS lane is watching**, not on whether any lane is.
- **FR3** — **The backoff ladder is per lane.** A quiet lane must not pace a busy one.
- **FR4** — **The refusal memo is per lane**, so an agent's first refusal always carries the text.
- **FR5** — **Every timing stamp that belongs to a lane carries it**, and the watch's open and
  close are recorded, so rows 1–4 are observable rather than inferable.

### Non-functional

- **NFR1** — **A single-lane session is unchanged** in behaviour and in what it logs, beyond the
  lane name appearing on stamps that always described that one lane anyway.
- **NFR2** — **No new inference.** Every per-lane answer comes from a lane the caller already
  names; nothing here may guess which lane a call belongs to.
- **NFR3** — **The wire and the log stay additive.** A page or an agent that predates this keeps
  working.

### Technical constraints

- **TC1** — **`watch_open` keeps its meaning.** Spec 012 TC7 chose to join it with
  `watching_lanes` rather than redefine it, because redefining silently changes the answer given to
  every existing caller. FR2 is therefore a change to the CONSUMER, not to the field.
- **TC2** — **The CLI's streak store is a file keyed by session.** Making it per lane changes a
  persisted shape, so it must tolerate the old one rather than reset every ladder on upgrade.
- **TC3** — **The timing log is append-only and already large** (2.6 MB on the live session). New
  fields are cheap; new events must be worth a line.

## Implementation Tasks

- [x] The pre-reply-versus-listen fact is recorded and read per lane, with `agent_holds_turns`
      re-derived from the fan-out so every existing reader sees what it always saw.
- [x] The watchdog's first step reads whether the asking lane is in `watching_lanes`, and says
      explicitly that a non-empty list belonging to OTHER lanes is not a reason to stay silent.
- [x] The backoff streak is stored per lane, inheriting the existing session-keyed value.
- [x] The refusal memo and its repeat counter are keyed by lane, and the documented
      `last_refusal = None` reset still works.
- [x] Timing stamps carry the lane where they have one, and the watch is stamped at both ends with
      the `holds` fact that decides fast-check versus long-listen.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_multilane_observability.py` — **FR1.** One lane takes a turn, the
      other lane's watch returns empty, and the first lane is still holding. **Verified by
      mutation:** restoring the session-wide clear turns this red, along with its converse.
- [x] **AC-2** `unit:tests/test_multilane_observability.py` — **FR2.** The scheduled prompt names
      `watching_lanes`, states that another lane's watch is not yours, and keeps the `watch_open`
      fallback for a server that predates the list.
- [x] **AC-3** `unit:tests/test_multilane_observability.py` — **FR3, TC2.** A streak on one lane
      does not move another's once that lane has its own; an existing session-keyed value is
      inherited rather than discarded.
- [x] **AC-4** `unit:tests/test_multilane_observability.py` — **FR4.** Two lanes each refuse once
      and both first refusals carry the turn text; a genuine repeat on one lane is still a repeat.
- [x] **AC-5** `unit:tests/test_multilane_observability.py` — **FR5.** `consumed` carries its lane,
      and `watch_open` / `watch_closed` both appear carrying the lane and `holds`.
- [x] **AC-6** `unit` — **NFR1.** The full suite passes, including the single-lane suites and the
      refusal, context-cost and speech-signal files that read this state.
- [ ] **AC-7** `manual` — **FR1, FR5.** On his phone with three lanes: ask a background lane a
      question while another lane is listening, and confirm the answer arrives without the pause he
      timed — then confirm the timing log shows why.

### Findings — what the implementation cost elsewhere

**Three tests in this spec's own file failed for a reason that had nothing to do with the code**,
and the cause is worth recording because it is the fixture version of the lying-view problem:
the helper posted `{"watching": ...}` while the endpoint reads `open`, **which defaults to true** —
so every "the watch closed" call was silently a "the watch opened" call. A fixture that names a
field its producer does not read fails in the direction of the old behaviour, which is exactly the
direction that looks like a real defect.

**Two existing tests needed updating and both were asserting a shape rather than a property:**
`test_say_refusal_repeat` compared `state.last_refusal` to a bare tuple, and `context_cost.py`
relies on `last_refusal = None` as a reset. The first was re-anchored to the lane's entry; the
second is preserved by normalising a non-dict memo back to an empty map, so the documented reset
sentence stays true.

## Out of Scope

- **Persisting per-lane state across a restart.** Tracked separately in Voice Tunnel; the
  session cursor already persists and 017 NFR2 makes it the fallback.
- **Changing what `watch_open` means** (TC1) or what `agent_state` means. Both were deliberate and
  both have callers.
- **New states, new cues, or a fifth sound.** The vocabulary is unchanged.

## References

- `specs/012` — lanes; `watching_lanes` was published here and row 2's consumer was never switched.
- `specs/013` · `specs/015` · `specs/016` · `specs/017` — the four previous instances of this exact
  shape, one per fact, each found by looking rather than by auditing.
- Voice Tunnel — the board, with his words.
