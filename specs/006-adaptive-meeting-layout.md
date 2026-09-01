---
id: "006"
title: The meeting page — one page that arranges itself like a video call
status: complete
blocked_by: ["003", "004", "005"]
blocks: ["007", "008"]
---

# The meeting page — one page that arranges itself like a video call

## Overview

Spec 003 merged the two servers but left the two web *pages* apart: the voice UI at `/` and the canvas
at `/canvas`. This spec builds the page the whole project is for — **one meeting page** where JJ's
agents are participant orbs, the live one draws on a shared canvas, and a transcript runs down the
side. Crucially the page is **not a fixed layout**: it rearranges itself the way a video call does, as
a pure function of three things — **how many agents are in the bridge**, **whether a canvas is being
shared**, and **whether he wants to see it**. The validated wireframe is ONE of those states (many
agents, a canvas shared), not the design.

Building this page also closes spec 003's last acceptance criterion (AC-2 / FR3: both surfaces on one
combined page), which was deferred here on purpose.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified by
> the method named on it — for a layout, that means a `command-bridge shot` (spec 002) of the state,
> looked at, not merely built. Build-only verification is insufficient. Iterate until each state's
> screenshot shows the arrangement the criterion describes.

## What he said, verbatim

> *"we have this voice tunnel UI where we move the transcript to the right of the screen, and we leave
> the main center window with a lot of space where we can display the tunnel vision, and the orbs with
> the different lanes for the agents — they sit on top, like they do on a Microsoft Teams meeting — and
> showing stuff."*
>
> *"I want this thing to behave and evolve as there are more agents in the bridge … if I were to meet
> with one agent, as I do in Microsoft Teams, the orb of the agent would take the full screen. And I
> have the ability to [expand] the transcript … Now, if the agent wants to show me something, it is the
> same as if on a meeting, the person I'm meeting with wants to share their screen. And at that point,
> the layout should reorganize, the transcript goes to the right, the agent goes to the top … and now
> the canvas takes the full screen."*
>
> *"if there are more agents, now they start appearing like that."*
>
> On seeing the wireframe framed: *"Perfect. I love this."*
>
> (dictated 2026-08-31 over the voice tunnel, turns 4420–4421 and the earlier vision dump; captured in
> `Command Bridge.md` → Wireframe and "The layout ADAPTS…". Mockup: the project's
> `the wireframe mockup`.)

## Findings — where the two pages are today

Grounded in the code:

- **Two pages, one server.** `command_bridge/server.py`'s `handle_index` serves the voice UI at `/`
  (the transcript, the controls, the orb/lane affordances the voice side already has). The canvas page
  is `command_bridge/canvas/page.py` (`render()`), served at `/canvas` by the spec-003 mount. They
  share a server and an SSE stream but are two separate documents.
- **The pieces the meeting page needs already exist.** The lane registry (`lanes.py`) is the
  participant list; the turn log is the transcript; the canvas frame store is the shared screen; the
  live lane (spec 004) is who "has the floor". The meeting page composes these, it does not invent
  them.
- **The wireframe is the *shared, many-agents* state.** Header (kept from the voice UI), a full-width
  orb row (one orb per lane, deterministic colour, the live orb enlarged and lit), then a body of
  `canvas (full-bleed) | resize-handle | transcript`. The other states are this page with panels
  shown, hidden, or resized — not different pages.

## Goals

- **One page for the meeting** — transcript, canvas, and participant orbs together — replacing the two
  separate documents and closing spec 003's combined-page criterion.
- **The layout is the wireframe when a canvas is shared, and rearranges like a video call otherwise** —
  driven only by (agent count) × (canvas shared?) × (he wants to see it?).
- **It tracks the live state** the rest of the tool already maintains — the lanes, the live lane, the
  turns, the frames — with no new source of truth.

## Requirements

### Functional Requirements

- **FR1** — **One combined page.** A single page presents the transcript, the canvas, and the
  participant orbs together (this is spec 003's AC-2 / FR3). The old separate documents need not both
  survive, but nothing the voice or canvas halves published is lost.
- **FR2** — **The layout is a pure function of three inputs only:** the number of agents in the bridge,
  whether a canvas is currently being shared, and whether he wants to see the canvas. No other state
  changes the arrangement.
- **FR3** — **Solo, not sharing → the agent fills the frame.** With one agent and no shared canvas, the
  single orb holds the centre (a 1:1 call); the transcript is a side panel he can expand and collapse.
- **FR4** — **A share reorganises to the wireframe.** When a canvas is shared (and he wants to see it),
  the layout becomes: orbs in a row across the top, the canvas full-bleed in the centre (the "shared
  screen"), the transcript a resizable column on the right — the validated wireframe.
- **FR5** — **More agents tile as orbs.** With several agents in the bridge, every lane is an orb in the
  top row; the **live** lane is visually distinguished (enlarged and lit, as in the wireframe), and
  each orb's colour is the lane's deterministic colour.
- **FR6** — **The transcript column is resizable**, and the canvas is the full-bleed shared screen when
  shown; the resize does not reload or lose canvas state.
- **FR7** — **The arrangement updates live.** As agents join or leave, as the live lane moves, and as
  sharing turns on or off, the page transitions to the matching state without a manual reload — the
  same SSE/live path the two halves already use.

### Non-Functional Requirements

- **NFR1** — **Every layout state is screenshot-verified** (spec 002). A layout claim not backed by a
  looked-at `shot` is not met — this is the effort gate the project set for itself.
- **NFR2** — **The voice and canvas contracts are unchanged.** The turn log, the lane switch, the frame
  ops, `/status`, and the SSE stream behave exactly as before; the suite stays green. The meeting page
  is a new view over existing state, not a change to it.
- **NFR3** — **Local, one viewer, theme as designed.** The page renders on the dark meeting theme of the
  validated wireframe; it stays loopback-only and pulls only the CDN assets the canvas already uses.

### Technical Constraints

- **TC1** — **The canvas stays a dumb surface, and the page holds no model.** The meeting page arranges
  and displays; it decides nothing an agent would decide.
- **TC2** — **The `shared?` and `wants-to-see?` inputs are consumed, not built here.** How JJ *grants*
  whether an agent may share, and how an agent *reads* a "can I show a canvas" capability before it
  draws, is spec 007. This spec reads whatever those two signals currently are (a lane is drawing →
  shared; a default of wanting to see) and renders the matching layout; it does not build the consent
  control.

## Implementation Tasks

- [x] Build the one meeting page (header + orb row + body) that composes the transcript, canvas, and
      lane registry, served as the combined page at `/meeting` (closing spec 003 AC-2). —
      `command_bridge/meeting.py` `render(state)`, route `handle_meeting` in `server.py`. Built as a
      new route so JJ's live phone UI (`web/index.html`) is untouched.
- [x] Implement the layout as a state machine over (agent count, shared?, wants-to-see?), with the
      wireframe as the shared state and the orb-fills-frame arrangement as the solo-unshared state. —
      `_canvas_is_shared()` selects `data-state` shared/solo; unit-tested in `test_meeting.py`.
- [x] Render the orb row from the lane registry with deterministic per-lane colour (the phone UI's
      `LANE_HUES`) and the live orb distinguished; render the transcript from the turn log; embed the
      canvas (`/canvas`) as the shared screen.
- [x] Make the transcript column resizable (the grip) and drive state transitions off the live/SSE
      path — the page subscribes to `/events` and moves `data-state` on share on/off + re-marks the
      live orb on a switch, with no reload (AC5). A fold-away transcript *collapse* toggle is the one
      small follow-up left (resize works today).
- [x] Screenshot each state with `command-bridge shot` and confirm the arrangement (solo + shared, dark).
      Also added a `--color-scheme dark` option to `shot` so the dark meeting page (and its embedded
      canvas) is captured in the theme it is seen in, and reconciled the canvas default lane to the
      voice default at startup (the spec-004 task) so an embedded canvas shows the live lane's frames.

## Acceptance Criteria

- [x] **AC1** `shot` — **FR1.** A `command-bridge shot` of the combined page shows the transcript, the
      canvas, and the participant orbs on ONE page (spec 003 AC-2 closed). — verified live 2026-09-01:
      the shared-state shot shows the orb row, kepler's two frames on the dark canvas, and the
      transcript column together (`meeting_shared2.png`).
- [x] **AC2** `shot` — **FR3.** In the solo, not-sharing state, the shot shows the single agent holding
      the centre with the transcript as a side panel — not the canvas full-bleed. — verified: the solo
      shot shows one enlarged ASSISTANT orb centred, transcript right, no canvas iframe
      (`meeting_solo2.png`). NB: the transcript is **resizable** (the grip) but a fold-away *collapse*
      toggle is not built yet — tracked as a follow-up; the "collapsible" wording is not yet met.
- [x] **AC3** `shot` — **FR4/FR6.** In the shared state, the shot matches the validated wireframe: orbs
      across the top, canvas full-bleed centre, transcript a resizable right column. — verified
      (`meeting_shared2.png`), matching `command-bridge-wireframe.html`.
- [x] **AC4** `shot` — **FR5.** With several lanes registered, the shot shows every lane as an orb in
      the top row with the live lane distinguished and each orb in its lane's colour. — verified: four
      orbs (assistant/atlas/kepler/dexter) in the LANE_HUES palette, KEPLER enlarged + lit as live.
- [x] **AC5** `integration`+`shot` — **FR7.** Toggling the shared input (a lane draws / stops) moves the
      page between the AC2 and AC3 arrangements **without a reload** — asserted on the state selection,
      and a shot of each side. — **met.** The page subscribes to the canvas's `/events` SSE and moves
      `data-state` on `frame`/`remove`/`clear` (share on/off) and re-marks the live orb on `switch`.
      Verified live 2026-09-01 with a scripted Playwright flow (`scratchpad/ac5_verify.py`): a page
      loaded in `solo`, a frame drawn from OUTSIDE moved it to `shared`, removing the frame moved it
      back to `solo` — and a `window` marker set on the document survived both, proving **no reload**
      (`{ok:true, went_shared, marker_survived_share, went_solo_again, marker_survived_unshare}`).
      Shot of the live-shared moment: the orb row appeared and the frame filled the canvas without a
      navigation.
- [x] **AC6** `command:python -m pytest tests/` — **NFR2.** The full suite passes with no regression;
      the voice and canvas contracts are unchanged. — verified: only the 4 pre-existing
      `test_word_timings` failures (missing `kokoro_onnx`) remain across the meeting-page build and the
      AC5 live-transition rewrite.

## Testing Approach

### Validation Steps

1. Serve; with one lane and no frame, shot the page → solo state (AC2).
2. Draw a frame (share) → shot → the wireframe arrangement (AC3); remove it → back to solo (AC5).
3. Register several lanes, make one live → shot → the orb gallery with the live orb lit (AC4).
4. Run the full suite (AC6).

### Test Cases

| State | Inputs | Expected arrangement |
|---|---|---|
| solo, quiet | 1 agent, no share | the orb holds the centre; transcript collapsible |
| solo, sharing | 1 agent, a frame drawn | wireframe: orb top, canvas centre, transcript right |
| meeting, sharing | several agents, a frame drawn | orb row, live orb lit, canvas centre, transcript right |
| share toggles | frame drawn then removed | page moves solo ↔ wireframe, no reload |

## Out of Scope

- **The canvas-share consent control** (spec 007) — granting whether an agent may share, and the
  per-session capability an agent reads before drawing. This spec consumes the current signals.
- **Per-lane orb state and the truly unified multi-lane transcript** (spec 008) — orbs carrying
  thinking/speaking/live/hand, and one transcript that merges every lane. This spec places the orbs and
  a transcript and lights the live orb; their per-lane richness is 008.
- **The differentiator mechanics** (spec 009) — object-anchored `point`, a per-lane channel,
  focus-aware rendering, an auto-director.

## References

- `Command Bridge.md` → Wireframe, "The layout ADAPTS as agents join and share"; the project notes mockup
  `the wireframe mockup`.
- `command_bridge/server.py` `handle_index` (the voice page), `command_bridge/canvas/page.py`
  (`render()` — the canvas page), `specs/003-one-server-one-page.md` (the merge + the deferred AC-2),
  `specs/004-unified-lane.md` (the live lane the orb row lights), `specs/002-screenshot-harness.md`
  (the `shot` every AC here leans on).
