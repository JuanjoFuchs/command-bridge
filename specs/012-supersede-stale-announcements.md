---
id: "012"
title: Supersede a stale announcement turn
status: complete         # pending | in_progress | complete
blocked_by: []           # builds on the held-clip queue (lane_held) that already ships
blocks: []
---

# Supersede a stale announcement turn

> **Implementer spec** (JJ: "you are the implementing agent"). Project node: Command Bridge,
> parked idea #1. Written against the codebase.

## Overview

An agent's off-lane `say` is **held** and raises a hand on its orb until he comes back to that lane. But
some clips are **announcements of intent** — *"I'm about to run the tests"* — and by the time he switches
back, the thing is done and the announcement is noise. JJ, 2026-09-01: *"you announce that you're going
to do something and then after a while … you have done that thing. So hearing … the turn where you
announced you were going to do something is not valuable … how could we note that this new turn
supersedes that old turn?"*

This spec lets an agent **mark a clip as an intent announcement**. While such a clip is still **held**
(he has not heard it), a **newer clip from the same agent on the same lane supersedes it** — the held
announcement is dropped, so when he returns he hears the current state, not a stale promise. A clip he
has already heard is never touched; a normal (result) clip is never dropped.

> **Completion rule:** This spec is not complete until all acceptance criteria are verified through the
> testing methodology below (integration tests driving the real held-clip path). Build-only verification
> is insufficient. The agent must iterate until verification passes.

## Goals

- Let an agent say *"here is what I'm about to do"* without that becoming stale noise he has to sit
  through later.
- Drop only what has actually gone stale: an **intent** clip that is **still held** (unheard), and only
  when the **same agent** has since spoken again **on the same lane**.
- Keep the raised-hand count honest — a superseded clip is gone, so the count drops with it.
- Change nothing for a clip he is hearing live, and nothing for a result clip.

## Requirements

### Functional Requirements

- **FR1** — A `say` clip can be marked as an **intent announcement** (a new flag on `say`). Unmarked
  clips behave exactly as today.
- **FR2** — When a new clip is queued **held** for a lane, any **held, unheard intent clip from the same
  agent on that lane** is **dropped** (superseded). The new clip is held in its place.
- **FR3** — Superseding applies ONLY to **held** intent clips. An intent clip that plays **live** (he is
  on the lane) is heard normally and is never retroactively dropped.
- **FR4** — A clip that is NOT marked intent is never dropped by a newer clip — a result stands until he
  hears it. Only intent clips are supersedable.
- **FR5** — The `say` result reports when this clip **superseded** an earlier held announcement (how many,
  which), and the drop is stamped to the timing log — so the agent and a later reader can see it happened.

### Non-Functional Requirements

- **NFR1** — The hand/`waiting` count a lane shows reflects the held queue after superseding, so it never
  counts a clip that has been dropped.
- **NFR2** — Superseding is scoped strictly by **(agent-lane, held, intent)**; it never reaches across
  lanes, never drops another agent's clip, and never drops a clip already delivered.
- **NFR3** — Untrusted-input and locality unchanged; no new network path.

### Technical Constraints

- **TC1** — Builds on the existing held-clip queue (`lane_held`) and the hand/`waiting` accounting; it
  adds a mark and a drop rule, not a new delivery mechanism.
- **TC2** — `describe` documents the new flag and result field and stays authoritative (spec 005 rule).

## Key Decisions (implementer)

- **Drop, not de-prioritise.** The parked idea left this open; dropping is right because a stale intent
  is *noise*, not lower-priority signal — de-prioritising still makes him hear it. Gone is the honest state.
- **Any newer clip supersedes a held intent, not only another intent.** A result (*"tests pass"*) makes
  the intent (*"about to run tests"*) stale just as much as a newer intent does — the test is "is there
  anything newer from you on this lane", not "is the newer thing also an announcement".
- **The mark is the agent's, per clip.** The tool cannot know which clips are intent vs result — the
  agent says so, the same dumb-tool/smart-agent line the rest of Command Bridge holds. So it is a flag on
  the clip, not a heuristic on the text.
- **Live playback is out of scope for superseding.** If he is on the lane, the intent plays at once and
  the whole problem (a stale held clip) never arises.

## Implementation Tasks

- [ ] Add the intent mark to `say` (a flag) and carry it onto the held-clip record in `lane_held`.
- [ ] When appending a held clip for a lane, drop any held intent clips from the same agent on that lane
      first, and decrement the hand/`waiting` count by what was dropped.
- [ ] Surface the supersede outcome on the `say` result and stamp it to the timing log.
- [ ] Leave the live-playback and non-intent paths untouched.
- [ ] Document the flag and the result field in `describe`; keep the describe-contract test green.

## Acceptance Criteria

- [ ] **AC1** — `say --intent` (or the chosen flag) marks a clip; an unmarked `say` is byte-for-byte
      unchanged in behaviour. *(integration/regression)*
- [ ] **AC2** — Two held clips, same agent+lane, the FIRST marked intent: after the second is queued the
      first is gone, only the second is held, and the hand count is 1. *(integration)*
- [ ] **AC3** — A newer **result** (unmarked) clip supersedes a held **intent** clip the same way. *(integration)*
- [ ] **AC4** — A held **result** (unmarked) clip is NOT dropped by a newer clip — both remain, hand count 2. *(integration)*
- [ ] **AC5** — An intent clip queued while he is **on** the lane plays live and is not dropped by a later clip. *(integration)*
- [ ] **AC6** — Superseding never crosses lanes or agents: a held intent on lane A is untouched when agent
      B, or the same agent on lane C, speaks. *(integration)*
- [ ] **AC7** — The `say` result names the supersede (count) and the timing log carries the drop; the new
      flag/field are in `describe` and the contract test is green. *(integration + unit)*

## Testing Approach

### Validation Steps
1. Drive the real held-clip append path (as the lane-hold suite does) with an intent clip then a newer
   clip; assert the queue and the hand count.
2. Repeat with the newer clip unmarked (result-supersedes-intent) and with the held clip unmarked
   (result-not-dropped).
3. Cross-lane / cross-agent negative cases.
4. Assert the `say` result field and the timing stamp.

### Test Cases
| Held now | New clip (same agent+lane) | After |
|---|---|---|
| `intent` A | any B | only B held; hand = 1; result says superseded 1 |
| `result` A | any B | A and B held; hand = 2 |
| — (he is on the lane) | `intent` | plays live; nothing held |
| `intent` A on lane X | clip on lane Y / other agent | A still held on X |

## Usage Examples

```bash
# An announcement that should not outlive its result.
command-bridge say --lane magnus --intent "On it — running the full suite now."
# ... later, same lane, still unheard by him:
command-bridge say --lane magnus "Suite's green, 847 passed."   # supersedes the announcement
```

## Out of Scope

- Any change to how a HEARD clip behaves, or to live playback.
- Superseding across lanes or across agents (explicitly excluded, NFR2).
- A heuristic that guesses which clips are announcements — the agent marks them (Key Decisions).
- The expiry-by-time idea for held clips (a different lever); this is supersede-by-newer only.
- Editing or merging clip audio — a superseded clip is dropped whole, not spliced.

## Findings — implementer (2026-09-02)

**Built and tested.** `say --intent` marks the clip (`intent` on the header); the off-lane held-append
in `_speak` drops this agent's still-held intent clips before adding the new one, decrements the hand
count, reports `superseded` on the say result, and stamps `announcement_superseded` to the timing log.
Live playback and non-intent clips are untouched — the change lives entirely in the `off_lane` branch.
All acceptance criteria verified by `tests/test_supersede.py` (6 cases, real `_speak` with TTS stubbed):
newer-supersedes-intent, result-not-dropped, newer-intent-supersedes-older, no cross-lane leak, and a
live intent never held. Describe documents `--intent` + the `superseded` field (contract test green);
`say`'s flag-census guard given the deliberate look. Full say/lane/hold suite stays green. **Needs a
server restart to deploy.**

## References

- Command Bridge — Parked idea #1 (JJ's verbatim + the synthesis this spec implements).
- The held-clip queue (`lane_held`) and the hand/`waiting` accounting (spec 029 lineage) this builds on.
- Spec Writing Rules for Agents — the rules this spec follows.
