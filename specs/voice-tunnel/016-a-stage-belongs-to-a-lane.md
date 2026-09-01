---
id: "016"
title: A stage belongs to a lane, and whoever opens it closes it
status: in_progress
blocked_by: []
blocks: []
---

# A stage belongs to a lane, and whoever opens it closes it

## Overview

Every state this tunnel publishes — `transcribing`, `thinking`, `waiting`, `synthesizing`,
`speaking`, `idle` — is one half of a pair. Something opens the stage, real work happens, something
closes it. With one agent that was safe, because there was only one thing the pair could be about.
With three agents it is not: **the pair is resolved against whichever lane is live at the moment of
each call, and the live lane can move between the open and the close.** When it does, one lane is
left holding a word that nothing will ever take back.

This spec replaces the implicit resolution with an explicit owner, fixed when a stage opens.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim (2026-08-25)

> *"I have noticed that the different lanes statuses also change. For example, if I am talking to
> you, I do see that the Atlas lane says transcribing."*

> *"I fear that the status leak is a more concerning issue in general because since now we are doing
> multiple lanes, we have to manage multiple statuses. We need a better state machine for this that
> understands multiple states for all the lanes and is able to collapse them into a single state for
> the solo mode."*

> *"I don't know what's the status of this state machine, but if necessary and if it's not designed
> to handle this complexity, it seems to need to rework it and if so, it might require a spec."*

## Findings — the defect, measured before it was designed against

**Reproduced in `tests/test_lane_state_ownership.py` before any change was made.** The failing
assertion prints the whole disease:

```
assert 'transcribing' != 'transcribing'
  where {'atlas': 'transcribing', 'magnus': 'idle'} = TunnelState.lane_states
```

The sequence is his, off his own phone, and every step of it is ordinary use:

| step | live lane | what the server does | who gets the word |
|---|---|---|---|
| 1 | atlas | he said "hey atlas" earlier; the switch is sticky | — |
| 2 | atlas | he starts speaking; audio closes; ASR begins | **atlas** ← `transcribing` |
| 3 | atlas | ASR returns *"hey magnus, ..."* | — |
| 4 | **magnus** | the wake gate moves the conversation | — |
| 5 | magnus | the stage ends | **magnus** ← `idle` |

🔴 **Nothing in the server ever revisits atlas.** Its orb reads `transcribing` until the page is
reloaded — so the leak is not a flicker, it is permanent, and the only reason it looks like a
flicker is that he reloads often.

**Why step 2 cannot simply be attributed better.** At the moment `transcribing` is set, *nobody
knows which lane the utterance is for* — that is the answer ASR is being run to obtain. The stage
genuinely belongs to whoever was live when it opened. The bug is not the attribution at open time;
it is that the close re-derives the owner instead of remembering it.

**Nine of the twelve stage calls resolve the lane implicitly** (`transcribing`, `synthesizing`,
`speaking`, `waiting`, and five separate `idle`s). Three pass a lane explicitly. Every implicit one
is a place where the pair can split.

🎯 **The transferable shape, and it is the third time this repo has hit it: a fact that was
SESSION-WIDE while there was one agent does not become per-lane by being stored in a dict keyed by
lane.** The read cursor was the same mistake (spec 015 FR1), and so was the transcript tag
(spec 013 FR3). Each time, the storage was made per-lane and the *resolution* was left global.

## Goals

- **A stage has one owner for its whole life**, named when it opens and used when it closes.
- **A lane can never be left holding a stage it did not finish.**
- **A single-agent session behaves exactly as it did before lanes existed** — one state, one orb.

## Requirements

### Functional

- **FR1** — **A stage's owner is fixed at open time.** The lane that a stage is opened against is
  the lane it is closed against, regardless of what happened to the live lane in between.
- **FR2** — **A lane that loses the conversation mid-stage is returned to a resting state**, not
  left on the word it was given — on every exit from the flow, including failure.
- **FR3** — **An outbound clip's stages belong to the lane that composed it**, not to whichever
  lane is live while it plays. An agent that lost the conversation is still the one speaking.
- **FR4** — **No stage may be published without naming its owner.** The owner is a required input,
  so an implicit resolution is not expressible rather than merely discouraged.

### Non-functional

- **NFR1** — **A single-lane session is unchanged.** One lane means one owner for everything, and
  `agent_state` keeps meaning what every existing client reads it to mean: the state of the agent
  he is talking to.
- **NFR2** — **No new inference.** Ownership is recorded at the moment it is already known. Nothing
  here may guess an owner from content, timing, or the shape of a state.
- **NFR3** — **Ownership is server-side.** The page paints what it is told; it must not be asked to
  reconstruct which lane a stage belonged to, because it does not see the flow that opened it.

### Technical constraints

- **TC1** — **The live lane can change at any await point.** ASR, synthesis and playback are all
  long, and the gate can move the conversation during any of them. Any design that reads
  `lanes.current` twice in one flow is wrong by construction.
- **TC2** — 🔴 **At the moment transcription starts, the owning lane is genuinely unknown** — it is
  what ASR is about to reveal. The open must therefore be allowed to attribute to the live lane and
  the *handover* must clean up, rather than the open waiting for an answer that does not exist yet.
- **TC3** — **The wire format cannot break the page that is already open.** `agent_state` messages
  keep their existing keys; anything new is additive.

## Implementation Tasks

- [x] The stage owner becomes a required input to the state publisher, so no call site can resolve
      it implicitly. **Nine call sites were resolving it implicitly; the type checker named every
      one of them the moment the default was removed**, which is the argument for removing it
      rather than auditing.
- [x] Each flow that opens a stage captures its owner once and threads it through to the close —
      the transcription flow and the outbound clip flow.
- [x] The flow releases its owner on the failure path as well as the success path.
- [x] The outbound clip path attributes `synthesizing` / `waiting` / `speaking` and the closing
      `idle` to the clip's own lane, including when the closing receipt arrives after a switch.
- [x] Barge-in releases the interrupted lane rather than the live one.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_lane_state_ownership.py` — **FR1, FR2.** His exact sequence, driven
      through the real transcription flow: atlas live, ASR returns a summons for magnus, the stage
      closes — atlas reads `idle`, not `transcribing`. **This assertion was SEEN to fail** on the
      code as it stood, printing `{'atlas': 'transcribing', 'magnus': 'idle'}`. Paired with a
      control that pins the no-switch case unchanged.
- [x] **AC-2** `unit:tests/test_lane_state_ownership.py` — **FR3.** A clip composed by one lane
      marks that lane `speaking`, and switching mid-clip does not move the word to the lane that is
      now live; the playback receipt then releases the lane that was speaking.
- [x] **AC-3** `unit:tests/test_lane_state_ownership.py` — **FR4.** Publishing a stage without an
      owner raises rather than silently attributing it to the live lane.
- [x] **AC-4** `unit:tests/test_lane_state_ownership.py` — **NFR1.** A single-lane session drives
      every stage and ends with exactly one entry in `lane_states` and a matching `agent_state`.
- [x] **AC-5** `harness:scripts/lanestrip.py` — **NFR3.** The per-lane reducer sweep passes
      unchanged: each orb reads its own lane's state and no other's.
- [ ] **AC-6** `manual` — **TC1.** On his phone with three agents: switch lanes while one is
      mid-reply and confirm no orb is left on a word. Manual because the race this fixes needs real
      ASR latency and a real switch to open the window — a test can only assert the ordering it
      chooses, not that the window is closed in practice. 🔴 **STILL OPEN, and asking him did not close it.** JJ, 2026-08-26: *"I have never switched to another lane mid-reply."* **Absence of the symptom is not evidence here** — he has never opened the window this guards, so a morning of clean use says nothing about it. This one needs a DELIBERATE attempt: a long reply started on one lane and a switch made while it is still speaking.

      🔴 **ATTEMPTED 2026-08-26, AND IT FAILED — three of Kepler's replies were destroyed.** JJ: *"I just switched to Kepler while you were still speaking this last turn."* · *"I didn't hear Kepler's turns. And they had two turns pending. And now the hand with the two counts is lost. And I don't know what Kepler wanted to say."* The timing log:

      ```
      14:15:51.116  lane_held_flushed  kepler  count 3   <- flushed into the browser
      14:16:07.717  barge_in           score 0.542       <- he speaks
                                                         <- no `played` for any of the three
      ```

      🎯 **THE FLUSH MOVES CLIPS OUT OF A SAFE STORE AND INTO ONE HIS NEXT WORD WIPES.** `lane_held` is deliberately separate from `undelivered` precisely because barge-in clears the playback queue wholesale — that separation is documented in `_speak` — but the flush hands them to the client, and from that moment they are in the queue barge-in empties. **And the loss is silent in both directions**: the hand is gone because the flush emptied the server's copy, so nothing on screen says three answers existed. Same class as the expiry `021` just fixed, reached by a different door.

      ⚠ **This is why the AC was not marked passed on absence of the symptom.** A morning of clean use said nothing, and the first deliberate attempt lost three replies.

      ✅ **FIXED by `specs/022-sending-is-not-hearing.md`** the same afternoon: the flush now keeps the server's copy until playback is confirmed, so a dropped browser queue returns the clips to their lane's hold and raises the hand again. **This AC stays open until he re-runs it live** — the fix is server-side and unit-tested, and the thing it guards is precisely a race that only a real voice and a real switch can open.

## Testing Approach

### Validation Steps

1. Run `tests/test_lane_state_ownership.py` and confirm AC-1 fails on the current code — the
   reproduction is the evidence the fix is aimed at the right thing.
2. Apply the change; the same file passes with no assertion relaxed.
3. Run the full unit suite plus `scripts/lanestrip.py`, `scripts/layout.py`, `scripts/orbstate.py`
   and `scripts/uisim.py` — the per-lane view is shared with three of them.

### Test Cases

| Sequence | Expected |
|---|---|
| open on A, close while A is still live | A resting, B untouched |
| open on A, conversation moves to B, close | **A resting**, B resting — neither stranded |
| clip for A plays while B is live | A speaking, B untouched |
| one lane, every stage in turn | one entry in `lane_states`, `agent_state` matching |
| publish a stage with no owner | refused |

### Findings — what the change cost elsewhere

**Two structural tests broke on a change that took nothing away**, and both were matching a whole
call as a string literal rather than the thing they were about:

- `test_speech_signals.py` sliced the source between `waited = 0.0` and the exact text
  `await _set_agent_state(state, "speaking")`. The added argument moved the marker. Re-anchored to
  the stage name, which is what actually delimits the hold loop.
- `test_device_group.py` matched `<label class="pick" id="devpick"` and broke when a second class
  was added beside `pick`. Re-anchored to "`pick` is one of the classes".

🎯 **Both are the same lesson and it is worth stating once: a structural test should match the
PROPERTY it is defending, not the line that happened to express it.** A test that fails on an
addition is a test that will be edited on reflex rather than read, and the next time it fails the
edit will be the reflex again.

## Out of Scope

- **Persisting lane states across a restart.** A state is a statement about work in flight; after a
  restart there is none. Lanes not surviving a restart is its own tracked defect in
  Voice Tunnel.
- **New states.** The vocabulary is unchanged; only who owns each word changes.
- **Agents reporting more diligently.** Spec 015 TC3 settled that: an agent heads-down is by
  definition not making calls, which is why `watching_lanes` exists.

## References

- `specs/012` — lanes; the registry whose `current` this spec stops reading twice.
- `specs/013` — the transcript tag: storage made per-lane, resolution left global. Same shape.
- `specs/015` — the read cursor: the same shape again, and the harness lesson behind AC-5.
- Voice Tunnel — the board, with his words.
