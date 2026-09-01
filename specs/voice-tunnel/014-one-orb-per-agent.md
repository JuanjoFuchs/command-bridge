---
id: "014"
title: One orb per agent
status: in_progress
blocked_by: []
blocks: []
---

# One orb per agent

## Overview

**The page still has one orb because it was built when there was one agent.** Spec `012` put three
in the room and `013` made them legible in the transcript, but the top half of the page still
centres on a single orb that has to stand for whichever agent is live — so it shows one agent's
state while he talks to another, and the name under it is the only thing that says who.

JJ, 2026-08-24, after driving three agents for an hour: *"now that we have multiple lanes and I can
talk to multiple agents, I think having a single orb in the middle makes no sense."*

**This spec replaces the centre orb with one orb per lane**, moves the session's on/off out to a
power button of its own, folds mute into the device control, and gives every lane a colour that
means *who*, never *what state*. All of it was chosen against a clickable preview rather than a
description — `scripts/uipreview.py`, arrangement C.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim (dictated 2026-08-24)

> *"the voice tunnel title that's rendered at the very top of the page, I would like to move it to
> the left. And in that same line, I would like to place the mute button and the device selectors
> and the virtual vose [verbose] mode… these controls will be aligned to the right instead."*

> *"I would like to join the mute button with the drop-down, you know, so that it's just the
> microphone button and that mutes and it has the arrow next to it that expands the drop-down. I
> wouldn't like to display the full text of the selected value. And the same for the speaker. But
> yeah. And if I click the speaker, I mute the speaker, right? Similar behavior."*

> *"I would like to test an arrangement C, which replaces the single orb in the middle and gives
> each lane its own orb with the same functionality and same behaviors that the current middle orb
> has. You know, for example, if I am addressing the Atlas agent, I would, whenever I speak, that's
> the orb that will pulse with my speech."*

> *"the behavior right now that I have that tapping the orb turns it off and turns it on… Maybe we
> add that button as a power button right to the left of the voice tunnel title."* · *"And that
> means that the orbs, each orb for each agent or each lane is just clicking on it, is just
> switching to that lane."*

> *"I was thinking giving each lane its own distinctive colour… because for example, I have orange,
> right? And the first agent has this blue, light blue thing. Maybe other agents have other colors.
> So also that way it's easier to recognize them in the transcript as well."*

> *"we shouldn't make the colors represent the statuses, because the statuses are represented within
> the orb. And make sure to update it so that the orbs show their statuses… And I would like also
> the orbs to — instead of codex wants you, I would like to have a count or a raised hand, you know,
> with the count of messages that are waiting for me."*

## Goals

- **He can tell which agent is which without reading a label** — colour and position do it.
- **Every control means exactly one thing.** The orb currently means two.
- **The transcript gets more of the screen**, which is what every layout request since 2026-08-07
  has actually been buying.

## Requirements

### Functional

- **FR1** — **One header row: the title on the left, every control on the right.** Title, power,
  microphone, speaker and verbose share a single line.
- **FR2** — **A power button, left of the title, owns the session.** It takes the on/off role the
  orb has today: releasing the microphone and stopping playback without closing the page.
- **FR3** — **Mute is folded into the device control.** One microphone button: the icon toggles
  mute, a caret opens the picker, and **no selected-value text is shown**. The speaker control
  behaves the same way, its icon muting output.
- **FR4** — **No centre orb. One orb per registered lane**, each carrying the behaviour the single
  orb has today for the lane it belongs to — including pulsing with his speech when that lane is
  live.
- **FR5** — **Colour is IDENTITY, never status.** Each lane has one hue, used for its orb, its name
  and its transcript rows. He keeps the warm that already means *you*.
- **FR6** — **Each orb states its own status in words, inside the orb** — listening / thinking /
  speaking / idle.
- **FR7** — **A lane holding speech shows a raised hand with a count**, replacing the words
  *wants you*. The count is how many clips wait.
- **FR8** — **Transcript rows carry the lane's hue**, so an agent is recognisable there too.
- **FR9** — **Tapping a lane's orb only switches the live lane.** It never toggles the session.

### Non-functional

- **NFR1** — **A single-lane session is not made worse.** With one lane there is one orb, which is
  what the page has today; the power button is new but the layout must not lose vertical space.
- **NFR2** — **44px minimum touch target** on every control in the header, unchanged from the rule
  `013` AC-14 already enforces.
- **NFR3** — **No new network chatter.** Everything here paints from state the page already
  receives.
- **NFR4** — **Nothing may weaken an existing test**; a test that moves is listed with its
  justification.

### Technical constraints

- **TC1** — 🔴 **The orb is not just a picture; it owns session lifecycle.** Wake lock, microphone
  acquisition, the audio graph, barge-in and the "tap to start" gesture all hang off it. FR4
  multiplies the *presentation* and FR2 moves the *lifecycle* — **these must not both live in the
  per-lane orbs**, or every orb will try to own the microphone.
- **TC2** — **A browser needs a user gesture to open a microphone.** The power button therefore has
  to be the thing first tapped, and it must be obvious enough to be tapped without instruction —
  today the orb says "Tap to start" and a bare power icon says nothing.
- **TC3** — **Hues must survive colour blindness and the light background.** Distinguish by
  lightness as well as hue, and hold 4.5:1 for text. Measured starting set: `#3559bd`, `#0f7a63`,
  `#8a3fa8` against the page's ground.
- **TC4** — **A lane set can grow past three.** The row must degrade rather than overflow, and the
  hue list must have a defined behaviour when it runs out.
- **TC5** — **Android Chrome, foreground tab only.** Inherited; nothing here can test it and
  nothing may violate it.
- **TC6** — **`013`'s guards must keep passing** — `lanestrip.py`, `layout.py`, `transcriptshot.py`
  and the full suite. This spec changes the page they measure, so the sweeps have to be updated
  rather than bypassed, and any case that stops applying is stated here.

### 🎯 Sequencing

**Slice A = FR1, FR2, FR3** — the header. Self-contained, and it is the half that buys the vertical
space.
**Slice B = FR4–FR9** — the orb row and the colour system. Depends on A only for where the header
ends.

## Findings — from the preview, before any code

**Three arrangements were built and clicked before one was chosen**, which is the reason this spec
exists in this shape rather than as a description. A and B kept the centre orb and put the lanes
around it as satellites; C removed it. Measured on the preview at 412px: **C starts the transcript
about 100px higher than A or B**, because satellites tracing a circle need vertical clearance for
their labels that a row of equal orbs does not.

🎯 **And the reason C is right is not the pixels.** With a centre orb, "which agent is this" is a
label attached to a shape that means something else. With one orb each, it is structural — the
question cannot be asked, because every orb is only ever itself. **A and B were arrangements; C is a
different model.**

⚠ **The preview is not the page.** It reuses the real stylesheet (lifted, not copied) so colour,
type and spacing are honest, but it has no microphone, no audio graph and no server. Everything in
TC1 is untested by it.

## Contract

The page gains no new server calls. It paints from what `/status` and the socket already carry:
`lanes`, `lane`, `default_lane`, `lane_states`, `lane_waiting`, `consumed_cursor`.

**Hue assignment is client-side and deterministic** — the Nth registered lane takes the Nth hue from
a fixed list, so two devices showing the same session agree without the server holding a preference.
When the list runs out it repeats, and lanes are additionally distinguished by name and position
(TC4).

## Implementation Tasks

### Slice A — the header

- [x] Title moves left; power, microphone, speaker and verbose sit right on the same row.
- [x] Power button owns start/stop, taking it off the orb, and keeps the "tap to start" affordance
      discoverable (TC2).
- [x] Mute folds into the microphone control: icon toggles, caret opens the picker, no value text.
- [x] The speaker control mirrors it, its icon muting output.

### Slice B — the orbs

- [x] One orb per registered lane, replacing the single centre orb.
- [x] Per-lane hue, applied to orb, name and transcript rows.
- [x] Status word rendered inside each orb.
- [x] Raised hand with a count on a lane holding speech.
- [x] Tapping an orb switches the live lane and nothing else.
- [x] The live lane's orb carries the speech pulse the centre orb has today.

## Acceptance Criteria

- [x] **AC-1** `harness:scripts/layout.py` — **FR1, NFR1, NFR2.** The header is ONE row at every
      swept viewport, in both picker layouts, solo and three-lane; every control is ≥44px; nothing
      overflows. The transcript's top edge is **higher than before this spec** at 412px, asserted as
      a number rather than eyeballed.
- [x] **AC-2** `unit` — **FR2, TC1.** Session lifecycle is owned by exactly one control. Asserted
      structurally: no per-lane orb handler touches the wake lock, the microphone, or the audio
      graph.
- [x] **AC-3** `harness:scripts/uipreview.py` — **FR3.** The caret opens the picker and the icon
      toggles mute, independently; no selected-value text is rendered in the header at any lane
      count.
- [x] **AC-4** `harness:scripts/lanestrip.py` — **FR4, FR9.** One orb per registered lane; tapping
      one switches the live lane and does not change session state.
- [x] **AC-5** `harness:scripts/lanestrip.py` — **FR5, TC3, TC4.** Hue assignment is a pure function of the lane list: the
      same list yields the same hues, distinct for the first N, defined when the list runs out.
- [x] **AC-6** `harness:scripts/lanestrip.py` — **FR5.** No hue is used to encode a status. Asserted
      by driving every state through one lane and confirming its hue never changes.
- [x] **AC-7** `harness:scripts/lanestrip.py` — **FR6.** Each orb renders its own lane's status word,
      and a state change on one lane leaves the others' text unmoved.
- [x] **AC-8** `harness:scripts/lanestrip.py` — **FR7.** A lane holding clips renders a hand with the
      count; the badge clears on flush. **Verified by mutation:** making the flush a no-op turns
      this red.
- [x] **AC-9** `harness:scripts/transcriptshot.py` — **FR8.** Rows from different lanes render in
      different hues, and his own rows keep the warm.
- [x] **AC-10** `unit` — **NFR4.** The full suite passes; no test deleted; every test touched is
      listed with its justification.
- [ ] **AC-11** `manual` — **TC1, TC2, FR4.** On his phone: the power button starts the session, a
      lane orb pulses with his speech, and switching lanes moves the pulse. **Irreducibly manual** —
      it needs a real microphone and a real phone, which is the one thing no harness here can
      produce.

## Testing Approach

### Validation steps

1. `venv/Scripts/python -m pytest tests/ -q` — no regressions.
2. `VOICE_TUNNEL_CORPUS_DIR=<sessions> … tests/test_lane_corpus.py` — the replay guards must RUN.
3. `scripts/layout.py`, `scripts/lanestrip.py`, `scripts/transcriptshot.py`, `scripts/uipreview.py`
   — all four, and all four start no server.

### Test cases

| Input | Expected |
|---|---|
| one registered lane | one orb, header intact, no hue clutter |
| three registered lanes | three orbs, three hues, one live |
| a lane goes from thinking to speaking | only that orb's word changes; no hue moves |
| two clips held for a lane | that orb shows a hand reading 2 |
| tap a non-live orb | live lane switches; session stays on |
| tap the power button | session stops; orbs dim; no lane switches |

## Out of Scope

- **Choosing a hue per agent by hand.** Deterministic by position for now; a stored preference is a
  separate, cheaper question.
- **Lane management from the page** — adding or removing a lane stays a CLI action.
- **Persisting the lane set across a restart.** Tracked in Voice Tunnel; this spec assumes
  whatever set the server reports.
- **Reworking the transcript beyond the hue** (FR8). The receipt and attribution are `013`'s.

## References

- `specs/012` — lanes; this spec is the UI catching up with the model it introduced.
- `specs/013` — attribution, badges and receipts; FR8 extends its transcript work.
- `scripts/uipreview.py` — the three arrangements, clickable; C is what this spec builds.
- Voice Tunnel — the revamp brief verbatim, and the roadmap rows it came from.
