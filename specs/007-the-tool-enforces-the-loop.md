---
id: "007"
title: The tool enforces the loop instead of trusting the agent to remember it
status: in_progress
blocked_by: []
blocks: []
---

# The tool enforces the loop

> **Refined from the strategist's metaspec by the repo implementer, 2026-08-19.** The metaspec's goals,
> rulings, constraints and decisions are preserved. What is added: the state the refusal is built on
> (it already exists), the ruling TC2 asked for, and the measured fact that the acknowledgement cue
> does not currently mean what FR3 assumes it means.

## Overview

Three of the loop's rules are currently enforced by the agent remembering them, and the agent forgets.
The guide has been rewritten three times to say the same things, and the failures still recur — which is
the signal that the rule belongs in the tool rather than in prose.

This spec moves two of them into the CLI and removes a third source of confusion. It does not add
capability; it removes ways to get the existing capability wrong.

> **Completion rule:** This spec is not complete until every acceptance criterion is verified by the
> method named on it. Build-only verification is insufficient. The agent must iterate until verification
> passes.

## JJ's rulings (2026-08-18 and 2026-08-19)

> The `say` command should exit with an error and not stream what you're saying if there was a turn from
> me that you didn't see. It should say the operator did not hear you because there was this turn —
> process it, and if you want to restate your message, do so.

> Whenever you hear something that you are not going to acknowledge, it doesn't make sense to reproduce
> the sound as if you heard me, as if you are acknowledging me.

> Everything is just `watch`, and there's no need to give a synonym.

## Measured ground truth (2026-08-19)

### FR1's state already exists, and is already sampled in the right place

`say` already computes the unread set **before synthesis, on both paths**, and already hands it back:

- `_unread_turns(state)` returns `{unread, unread_count, cursor}`, derived from the **log**
  (`last_turn_id - consumed_cursor`), never from a process counter.
- It is called before the `fire_and_forget` branch, so `--now` carries it too. The code's own comment
  says why: *"`--now` is exactly the path an agent takes when it is in a hurry, which is when it skips
  the check."*
- It filters on `addressed`, so room chatter that never passed the wake gate cannot trigger anything.
- It deliberately **does not advance the read cursor**, so refusing loses nothing — the next `watch`
  still returns those turns.

**So FR1 is a change of verdict, not a change of measurement.** Today the tool speaks and then warns; the
ruling is that it must refuse and not speak. NFR1 follows for free: the check compares state already in
hand, before any synthesis is started.

### TC1 resolves itself, and the reason is worth writing down

JJ asked: *"if I am muted, it means that there is no turn from me that's pending to be read, right?"*
Correct, and structurally so. `pending_turns` is `last_turn_id - consumed_cursor` read from the log. A
muted microphone produces no frames, so no utterance closes, so **no new turn is appended and
`last_turn_id` does not move.** Muting cannot manufacture an unread turn. What muting also does not do is
*forgive* a turn said before the mute — and that is correct: those words were still said and still
unread.

### FR3's premise needs correcting — the cue does not mean what the metaspec assumes

The metaspec says the acknowledgement cue *"still sounds"* when a turn is read and left unanswered,
implying it currently fires on reading. **It does not fire on reading at all.** `heard` is pushed at the
end of the turn-logging path, the moment ASR finishes and the turn is appended — **before the agent has
seen it, and regardless of whether any agent is even listening.**

So the cue's current meaning is *"your words were captured and written down"*, not *"they were read"* and
certainly not *"they were acknowledged"*. JJ's complaint stands either way and is arguably stronger: a
sound that fires on capture asserts acknowledgement for **every** utterance, including the ones the agent
will deliberately not answer under rule 10.

⚠ **This makes FR3 a bigger change than "move one call", and it has a cost that must be stated rather
than discovered.** Today he gets an immediate audible confirmation that the tunnel heard him. Gating the
cue on the agent's intent to respond **removes that confirmation** — there will be silence between him
finishing a sentence and the agent deciding. The compensating fact is that **the page already paints the
turn into the transcript the moment it is logged**, over the same broadcast, so the confirmation still
exists visually. **On a phone in a pocket it does not.** See the ruling section below.

## Goals

- Speaking over an unread turn becomes impossible rather than discouraged.
- The acknowledgement cue means acknowledgement, so its absence is information.
- One waiting command with one name, so nobody can describe two instruments where there is one.

## Requirements

### Functional Requirements

- **FR1**: `say` **refuses** when the session holds a turn the caller has not consumed. It exits
  non-zero, **does not synthesise, does not queue a clip**, and names the unread turns — id and enough
  text for the agent to recognise them. *Rationale: the current design makes the check optional, and an
  optional check on a discipline that has failed repeatedly is not a control.*
- **FR2**: The refusal carries a **remedy the agent can act on directly** — the literal `watch` command
  with the session and cursor filled in — and the unread turns themselves, so recovery costs no extra
  round trip. **There is no flag that disables the check.** *Rationale: a bypass flag would be reached
  for under exactly the conditions the check exists for; this repo has already deleted one such flag
  from the classification queue for the same reason.*
- **FR3**: The **acknowledgement cue fires only when the agent is going to respond.** Reading a turn and
  deliberately leaving it unanswered emits **no acknowledgement cue**. The agent's existing read
  signal already carries an intent field; the cue follows that intent rather than following capture.
- **FR4**: 🔴 **`drain` is removed. The only waiting command is `watch`.** Not deprecated, not aliased,
  not kept "for invocations already in circulation" — gone from the command surface, from `describe`, and
  from the help. `voice-tunnel drain` exits as an unknown command, and its message names `watch` as the
  replacement. *Rationale: `watch` and `drain` already run the same code, and the second name is not
  cosmetic — it is what led the operating guide to write them up as two instruments with two different
  waiting strategies, and to ship a wrong rule on 2026-08-19. A soft alias would leave that hazard in
  place while looking like it had been dealt with.*
- **FR5**: The refusal follows the repo's error contract — `{error, code, remedy}` with a **stable code
  slug** to branch on, registered in the documented code list, and a documented exit code. *Rationale:
  convention 8. An agent cannot branch on prose.*

### Non-Functional Requirements

- **NFR1**: The FR1 check costs nothing perceptible on the speaking path. It compares state the server
  already holds and already samples at that exact point.

### Technical Constraints

- **TC1**: **Muted is not the same as unread.** Resolved above: structurally true, because a muted mic
  appends no turn and `pending_turns` is read from the log. **An acceptance criterion must pin it anyway**
  — the property is currently a consequence of two other decisions, and nothing would fail if a later
  change broke it.
- **TC2**: **RULED: FR1 applies to `say --now` as well as to blocking `say`.** The metaspec left this
  open. The reasons, in order:
  1. The unread set is *already* sampled before the fire-and-forget branch, and the code comment gives
     the reason — `--now` is the hurried path, which is when the check gets skipped. Exempting it puts
     the hole exactly where the code says it is most likely to be used.
  2. Rule 7 (narrate before you work) is not broken by this. The narration clip is composed immediately
     after a `watch`, which advances the cursor — so nothing is unread and the refusal cannot fire. If
     something *is* unread at that moment, then he has spoken again and narrating the old plan is
     precisely the failure FR1 exists to stop.
  3. A refusal that half the callers can bypass is not a control, and `--now` is one argument away.

  ⚠ *This is the one ruling in this spec that changes agent-visible behaviour on a path JJ did not name.
  It is flagged rather than absorbed.*
- **TC3**: The queued-clip behaviour is load-bearing and must survive: an undelivered `say` is held until
  he is back, never discarded. **The refusal returns before `_speak` is entered, so nothing is
  synthesised and nothing enters the undelivered queue** — an early return must not reintroduce the
  409-and-drop this project already fixed.
- **TC4**: The cue vocabulary is `heard` / `thinking` / `tool` / `speaking`. FR3 concerns the
  acknowledgement cue only; the others are unaffected.
- **TC5**: 🔴 **A live voice session is running on session `dev`, port 8765, and this spec changes the
  commands that session is using.** No verification may start, restart or stop a server. The suite
  verifies server behaviour structurally and through in-process handlers; that is the ceiling here, and
  **the criteria say which parts are therefore unverified rather than claiming otherwise.**
- **TC6**: Removing `drain` changes what the project's Voice Tunnel Guide tells agents to do. **That guide
  is not in this repo and is not this spec's to edit** — it is flagged upward instead.
- **TC7**: 🔴 **The live session is armed RIGHT NOW and this change is visible to it immediately.**
  Measured at 13:45 on 2026-08-19: server pid alive, last turn 12:56, last watch re-armed 13:37. **The
  CLI is loaded from the working tree on every invocation**, so unlike the server — which keeps running
  the code it started with — a change to the command surface reaches the live agent the moment the file
  is saved, not on a restart.

  **Consequence: the agent currently driving that conversation follows a guide that tells it to run
  `drain` before every reply, and that command will stop existing mid-session.** This is why the batch
  ordered 007 last, and ordering does not remove it.

  **→ Therefore the removal must be sequenced within the change itself: the helpful unknown-command
  message naming `watch` goes in FIRST, and the alias is removed SECOND.** There must be no window in
  which `drain` is gone and the only thing a caller gets is argparse's generic "invalid choice". Done in
  that order, the worst case for the live session is one failed call that names its own replacement —
  which is the designed migration path working, rather than a break.

## 🔴 The ruling FR3 needs, which is JJ's and not the implementer's

**Gating the acknowledgement cue on intent removes his only audible confirmation that the tunnel heard
him at all.** The implementer's reading of his ruling — *"whenever you hear something that you are not
going to acknowledge, it doesn't make sense to reproduce the sound as if you heard me"* — is that this is
exactly what he asked for, and FR3 is written that way.

**But there are two defensible designs and only he can choose:**

| Option | He hears on capture | He hears when the agent will answer | Cost |
|---|---|---|---|
| **A — as specced** | nothing | the acknowledgement cue | Silence after every sentence until the agent decides. On a phone in a pocket there is no confirmation the tunnel is alive |
| **B — split the vocabulary** | a distinct, quieter capture tick | the acknowledgement cue | Keeps the liveness signal; costs one new sound in a four-sound vocabulary TC4 says is fixed |

**This spec implements A**, because it is the literal reading of his words and because B widens a
vocabulary the metaspec froze. **Reversing to B is a small change and the question should be put to him**,
since the thing being removed is feedback he has had in every session so far.

## Key Decisions

| Decision | Why | Rejected |
|---|---|---|
| Refuse in the tool rather than document harder | Three guide rewrites did not fix it | A fourth restatement |
| No bypass flag | It would be used exactly when the check matters | `--force` |
| The refusal applies to `--now` too | The hurried path is the one that skips checks; the code already samples the state there | Exempting `--now` to protect rule 7 |
| Refuse **before** synthesis | Nothing to discard, nothing queued, no 409-and-drop | Synthesise then discard |
| **Remove `drain` outright; `watch` is the only waiting command** | The two names caused a wrong rule to ship. JJ, 2026-08-19: *"I want to be explicit. The only command is `watch`."* | A note saying "these are the same"; a deprecation alias that keeps both names working |
| Implement cue option A, and flag option B | A is the literal reading of his ruling; B widens a frozen vocabulary | Deciding B unilaterally; leaving the cue on capture |

## Implementation Tasks

- [ ] Refuse in the say path when the unread set is non-empty, before synthesis, on both the blocking and
      the fire-and-forget branches.
- [ ] Give the refusal the repo's error shape, a stable code slug, a documented exit code, the unread
      turns, and the literal `watch` command as its remedy.
- [ ] Register the new code slug in the documented code list and update `describe` in the same change.
- [ ] Move the acknowledgement cue off the capture path and onto the agent's respond intent.
- [ ] Remove `drain` — the alias table, its handler, its subparser, its `describe` entry, the dispatch
      row, and every prose mention that tells a reader it exists.
- [ ] Make `voice-tunnel drain` fail as an unknown command with a message naming `watch`.
- [ ] Sweep the repo's own docs and harnesses for `drain` and update what would now mislead.
- [ ] Run the full suite.

## Acceptance Criteria

### The refusal (FR1, FR2, FR5)

- [ ] **AC1** (`unit`): with one unread addressed turn, the say path **returns a refusal** and
      **`tts.synthesize` is never called**. *Asserted on the synthesis call, not only on the return value
      — "did not speak" is the requirement, and a refusal that still synthesised would pass a
      return-value-only check.*
- [ ] **AC2** (`unit`): the refusal payload carries `error`, a stable `code`, a `remedy` containing the
      literal `watch` command with the session and cursor filled in, and the unread turns with their ids
      and text.
- [ ] **AC3** (`unit`): the CLI exits **non-zero** on a refusal, with the documented code.
- [ ] **AC4** (`unit`): the new code slug appears in `describe`'s documented code list and the command's
      own documentation. *Convention 3.*
- [ ] **AC5** (`unit`): **no flag anywhere on `say` disables the check.** Asserted over the parser, so
      adding one later fails. *A prohibition with no test can only be observed failing.*
- [ ] **AC6** (`unit`, TC2 ruling): `say --now` refuses on the same condition as blocking `say`.
- [ ] **AC7** (`unit`, TC3): on a refusal, nothing is queued — the undelivered queue is unchanged, and
      the read cursor is **not** advanced, so the next `watch` still returns those turns.
- [ ] **AC8** (`unit`, **negative control**): with **zero** unread turns, `say` proceeds normally and
      synthesis is called. *Without this, AC1 is indistinguishable from a say path that is simply
      broken.*
- [ ] **AC9** (`unit`, TC1): a **muted** session with everything read does **not** refuse; a muted session
      with a turn said *before* the mute and still unread **does** refuse. *Both arms, because the
      property currently falls out of other decisions and nothing would notice if it stopped.*
- [ ] **AC10** (`unit`): an **unaddressed** turn — room speech that never passed the wake gate — does not
      cause a refusal.

### The cue (FR3)

- [ ] **AC11** (`unit`): the acknowledgement cue is **not** emitted on the turn-logging path.
- [ ] **AC12** (`unit`): it **is** emitted when the agent signals it will respond.
- [ ] **AC13** (`unit`, **negative control**): it is **not** emitted when the agent reads a turn and
      signals it will not respond. *This is the case JJ reported; without an arm that fires only here,
      AC11 and AC12 can both pass with the cue simply moved to "every read".*
- [ ] **AC14** (`unit`, TC4): the other three cues are unaffected.

### `drain` is gone (FR4)

- [ ] **AC15** (`integration`): `voice-tunnel drain` exits non-zero as an unknown command, and its
      message names `watch`.
- [ ] **AC16** (`unit`): `drain` appears nowhere in `describe` — not as a command, not as an alias, not
      in any `alias_of`/`deprecated` field.
- [ ] **AC17** (`unit`): `drain` appears in no help text, no parser, and no dispatch table.
- [ ] **AC18** (`unit`): **no remaining occurrence of the word in the repo tells a reader the command
      exists.** Historical prose explaining that it *was* removed is allowed and expected; an instruction
      to run it is not. *Asserted by a scan, because the word appears dozens of times in commentary and a
      blanket ban would be unmaintainable while a blanket allowance is how the instruction survives.*

### Suite

- [ ] **AC19** (`integration`): `python -m pytest tests/` passes with no test weakened or skipped, and no
      `voice-tunnel` server was started (TC5).

## Testing Approach

### Validation steps

1. `venv/Scripts/python.exe -m pytest tests/` — the whole suite.
2. `bin/voice-tunnel drain` — confirm it fails as unknown and names `watch`.
3. `bin/voice-tunnel describe` — confirm `drain` is absent and the new code slug is present.

### Test cases

| State | `say` | `say --now` |
|---|---|---|
| nothing unread | speaks | speaks |
| one unread addressed turn | refuses, no synthesis | refuses, no synthesis |
| unread turn, session muted | refuses — muting does not forgive what was said | refuses |
| muted, nothing unread | speaks | speaks |
| unaddressed room speech only | speaks | speaks |

| Agent signal | Acknowledgement cue |
|---|---|
| turn logged, agent has not read it | **no** |
| read, will respond | **yes** |
| read, deliberately not responding | **no** |

## Out of Scope

- Changing what the waiting command *does* — spec `005` settled that.
- The cue contours or their audio design.
- Any change to how turns are transcribed, segmented or logged.
- The agent-side operating rules themselves, which live in the project's Voice Tunnel Guide — flagged
  upward under TC6, not edited here.
- Adding a capture-liveness cue (option B above) unless JJ chooses it.

## References

- `specs/005-one-wait-gated-on-speech.md` — the single waiting command this spec removes the second name
  for.
- `specs/006-orb-off-is-not-quiet.md` — the control-event semantics the refusal must not fight.
- Project node: `Voice Tunnel` in the project notes — the roadmap rows this spec closes.

## Verified state

*Filled in by the implementer after verification, per the completion rule. Empty at the refined-spec
gate: nothing here has been built yet.*
