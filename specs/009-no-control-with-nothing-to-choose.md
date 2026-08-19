---
id: "009"
title: Do not render a control with nothing to choose
status: in_progress
blocked_by: []
blocks: []
---

# Do not render a control with nothing to choose

> **Refined from the strategist's metaspec by the repo implementer, 2026-08-19.** The metaspec's goals,
> ruling, constraints and decisions are preserved. What is added: the measured mechanism by which the
> dead control survives, the definition of "selectable" that the fix turns on, and a verification plan
> that reaches the Android case without a phone in the loop.

## Overview

The page shows a microphone picker and a speaker picker. On Android — the platform he actually uses —
the browser enumerates no *choosable* audio output, so the speaker picker renders with a single entry
and can never do anything.

An earlier change tried to solve this by pairing the two into one control. That was the wrong shape:
pairing needs both halves to exist, and on Android one of them does not. This spec takes the other
route, which is available on every platform.

> **Completion rule:** This spec is not complete until every acceptance criterion is verified by the
> method named on it. Build-only verification is insufficient. The agent must iterate until verification
> passes. The one criterion that needs a real Android session is named as such and is the only one.

## JJ's ruling (2026-08-18)

Reported live, on a headset, with the speaker list offering only `default`:

> It seems we need to be smarter than that, right? If there is no speaker to choose, why would we show
> a drop-down to choose a speaker?

## Measured mechanism (2026-08-19) — why the existing capability check cannot fire

The page already has a rule that hides the output picker when the platform cannot route audio:
`$spkpick.hidden = !sinkSupported || grouped`. **It does not fire on Android, and the reason is not
that the check is wrong — it is that the evidence it waits for never arrives.**

`sinkSupported` is decided two ways, and both pass on Android Chrome:

1. **Feature detection** — `setSinkId in AudioContext.prototype`. Present on current Chrome for
   Android, even though the call cannot honour a request there.
2. **A refusal at call time** — `applySink` catches `NotSupportedError`, sets `sinkRefused`, and hides
   the picker. **This is the check that would be correct, and it is unreachable on the default path:**
   `applySink` returns early when `normSink(currentSink()) === normSink(sinkWanted)`, and at page load
   both are `""`. Nothing calls `setSinkId`, so nothing throws, so the picker is never withdrawn.

**The dead control survives precisely because the only thing that could prove it dead is never
invoked.** That is why "detect harder" is not the fix and counting is: a control with one option is
observably useless without needing the platform to admit it.

### What "more than one selectable output" has to mean

Naive counting gets this wrong in both directions, because **Chrome lists one physical device up to
three times** — once for real and once each as the `default` and `communications` pseudo-devices, all
sharing a `groupId`. The page already carries the machinery for this (`ALIAS_IDS`, `normSink`,
`cleanLabel`, `pairDevices`), and the counting rule must reuse it rather than invent a second notion of
device identity.

| Situation | Raw `audiooutput` entries | Distinct devices | Picker |
|---|---|---|---|
| Android Chrome | 1 (`default`, no label) | 1 | **absent** |
| Desktop, one sound card | 3 (`default`, `communications`, real) | 1 | **absent** |
| Desktop, card + headset | 5–6 | 2 | present |
| Nothing enumerated | 0 | 0 | **absent** |

**Two entries that are the same device said twice are one choice, not two.** A rule that counts rows
keeps the dead control on every single-output desktop; a rule that counts devices does not.

The same collapse applies to inputs (FR2), where Chrome plays the same alias trick.

## Goals

- A control appears only when it can change something.
- The page stops implying a capability the platform does not have.
- The Android session looks like what it is, rather than like a desktop session with a broken menu.

## Requirements

### Functional Requirements

- **FR1**: An output picker renders **only when there is more than one selectable output**, counting
  *distinct devices* after collapsing Chrome's `default` / `communications` aliases — not rows returned
  by `enumerateDevices`. With none, or with only one, it is absent — not disabled, not greyed, not
  showing one entry. *Rationale: a disabled control still asserts that the capability exists and is
  merely unavailable right now, which is a different and false claim.*
- **FR2**: The same rule applies to the **input** picker, on its own terms and with the same alias
  collapse. The asymmetry is the platform's, not the page's.
- **FR3**: When routing is not selectable, the page **says where the audio is going if it can know, and
  says nothing if it cannot.** It can know only when the sink read back from the **live**
  `AudioContext` matches an enumerated device carrying a real label. A remembered preference, a
  `sinkWanted`, or a start-time snapshot is not knowing. *Rationale: the existing failure is not the
  missing control but the lying one — a picker that read "Bluetooth" while audio played from the
  earpiece. Silence is honest; a stale label is not.*
- **FR4**: The visibility decision is a **pure function of the enumeration facts**, and nothing outside
  it writes a pill's visibility. *Rationale: this is the `orbView` lesson applied to the same page. The
  orb read "Thinking" with no clock because four handlers painted it independently and nothing in the
  suite could see that; the pills currently have the same shape, with `paintSink`, `paintGroup` and
  `refreshDevices` each assigning `hidden`. A rule that lives in three assignments cannot be swept, and
  a rule that cannot be swept is a rule that will be tested on one path and broken on another.*
- **FR5**: The decision **re-runs on every enumeration change**, not once at first paint (TC2).

### Non-Functional Requirements

- **NFR1**: No change to the desktop experience where both pickers are real and used — i.e. where more
  than one distinct device exists on a side. *A desktop with exactly one output does lose a control it
  had; that is FR1 working, not a regression, and it is called out here so it is not discovered as a
  surprise.*
- **NFR2**: No change to the grouped single-pill layout when pairing succeeds. This spec governs what
  happens when a side has nothing to choose; spec-level ownership of the paired case stays where it is.

### Technical Constraints

- **TC1**: **Android cannot be made to route audio from the page.** `setSinkId` is unsupported there, so
  the underlying capability is genuinely absent and this spec does not attempt it. What is in scope is
  not showing a control for it.
- **TC2**: Device enumeration is **permission-dependent and changes after the microphone is granted**, so
  a decision made at first paint may be wrong a second later. The render must react to enumeration
  changes rather than sampling once. The page already refreshes on the grant, the context opening, a
  reconnect and the browser's own `devicechange`; the new rule must sit on that path and not beside it.
- **TC3**: A **real Android session** is the only way to confirm that his phone enumerates the way this
  spec assumes. Everything else — including the Android *shape* — is verified without a phone, by
  driving the real page with a stubbed enumeration (see Testing Approach). **This spec claims automated
  coverage of the behaviour and manual coverage of the premise, and does not conflate them.**
- **TC4**: 🔴 **A live voice session is running on session `dev`, port 8765. No verification for this
  spec may start, restart or stop a `voice-tunnel` server.** The existing page harnesses
  (`layout.py`, `channel.py`, `orbstate.py`, `bargein.py`) each start one on their own port, so **none
  of them may be run or extended for this work.** The page's device logic does not need a tunnel — it
  needs the page, a browser, and a stubbed `enumerateDevices` — so verification serves the page over a
  plain static HTTP server on an ephemeral port. That is not a voice-tunnel process and cannot reach
  8765 or the session directory.
- **TC5**: The pills are **built while hidden** today, deliberately, so that a device appearing can put
  them back on screen already populated. Hiding must stay a *visibility* decision; do not stop building
  the lists.

## Key Decisions

| Decision | Why | Rejected |
|---|---|---|
| Hide the control | It can never do anything on the platform he uses | Disable or grey it — still claims the capability |
| Count distinct devices, not enumerated rows | Chrome lists one device up to three times; counting rows leaves the dead control on every one-output desktop | `outs.length > 1` |
| Count, rather than detect the platform harder | The correct capability check exists and is structurally unreachable on the default path | Calling `setSinkId` at load purely to provoke a refusal |
| Keep the desktop behaviour unchanged where there is a real choice | Both pickers work there and he uses them | One universal simplified control |
| Say nothing rather than label the route | A stale label is the bug that started this | Best-effort route label |
| One pure function owns visibility | Three assignments cannot be swept; the orb shipped a defect of exactly this shape | Adding a fourth `hidden =` at the new site |
| Verify with a stubbed enumeration in a real browser | Reaches the Android case with no phone, and proves the model is wired to the DOM | Structural source assertions only; a manual-only criterion |

## Implementation Tasks

- [ ] Extract a pure function of the enumeration facts that returns which pills are visible, alongside
      the reason each hidden one is hidden.
- [ ] Make it the only writer of pill visibility; the existing assignments call it or are removed.
- [ ] Collapse alias devices before counting, reusing the page's existing identity machinery.
- [ ] Apply the same rule to the input pill.
- [ ] Add the route readout for FR3, painted from the live context only, absent when unknowable.
- [ ] Expose the model on `window.__voiceTunnel` so a harness can read it without scraping the DOM.
- [ ] Add the exhaustive sweep of the pure function over the enumeration combinations.
- [ ] Add the browser harness that drives the real page with a stubbed enumeration, over a static
      server, starting no voice-tunnel process.
- [ ] Add the structural pytest assertions that keep the single-writer property from regressing.
- [ ] Run the full suite.

## Acceptance Criteria

### The rule itself (FR1, FR2)

- [ ] **AC1** (`unit`): with **zero** distinct outputs, the output pill is hidden.
- [ ] **AC2** (`unit`): with **one** distinct output — including the Android shape, a single unlabelled
      `default` — the output pill is hidden.
- [ ] **AC3** (`unit`): with **one** distinct output presented as Chrome's three rows (`default`,
      `communications`, real, one `groupId`), the output pill is hidden. *This is the case a row count
      gets wrong, and it is a desktop case, so it is not hypothetical.*
- [ ] **AC4** (`unit`): with **two or more** distinct outputs, the output pill is visible.
- [ ] **AC5** (`unit`): AC1–AC4 hold for the **input** pill against input enumerations.
- [ ] **AC6** (`unit`, **exhaustive sweep**): the pure function is swept over every combination of
      {0, 1 alias-collapsed, 1 multi-row, 2+} inputs × the same for outputs × {setSinkId present,
      absent} × {paired, unpaired}, and in every combination **no pill is visible with fewer than two
      choices**, and exactly one layout is on screen. *Rationale: the orb's defect was found by sweeping
      384 combinations of a pure model; the same instrument is what makes "we handled the cases" a
      measurement.*

### It is wired to the page, not merely computed (FR4)

- [ ] **AC7** (`integration`, real browser): the real page, served statically with
      `navigator.mediaDevices.enumerateDevices` stubbed to the **Android shape**, renders **no output
      pill** after a `devicechange`. Asserted on the DOM, not on the model.
- [ ] **AC8** (`integration`, real browser): the same page, stubbed with **two distinct outputs**,
      renders the output pill with two options.
- [ ] **AC9** (`integration`, real browser): switching the stub from two outputs to one and dispatching
      `devicechange` **removes** the pill without a reload (FR5/TC2).
- [ ] **AC10** (`unit`, structural): exactly one function assigns pill visibility. Asserted over the page
      source so a fourth `hidden =` cannot be added silently. *This is the criterion that keeps FR4 true
      after this spec is closed.*

### The route readout (FR3)

- [ ] **AC11** (`integration`, real browser): with routing unselectable and the live sink unreadable
      (the Android shape), the page shows **no route text at all**.
- [ ] **AC12** (`integration`, real browser): with one output that carries a real label and a live sink
      that matches it, the route text names that device.
- [ ] **AC13** (`unit`): the route text is never derived from `sinkWanted`, a stored preference, or a
      start-time snapshot. *Negative control: with a stored preference naming device A and a live sink
      reporting device B, the readout says B or says nothing — never A.*

### Instrument controls

- [ ] **AC14** (`integration`, **negative control**): the browser harness is shown to **fail** when the
      rule is inverted — a deliberately broken build renders the pill on the Android shape and the
      harness reports it. *Without this, AC7's "no pill" is indistinguishable from a harness that never
      found the pill, never loaded the page, or asserted on an element that no longer exists.*
- [ ] **AC15** (`integration`): the whole verification runs with **no `voice-tunnel` server process
      started**, asserted by the harness itself rather than by intention (TC4).

### Suite

- [ ] **AC16** (`integration`): `python -m pytest tests/` passes with no test weakened or skipped, and
      `python scripts/layout.py` is **not** run (TC4) — its geometry assertions are noted as unverified
      for this change, see Verified state.

### The premise, which only he can confirm

- [ ] **AC17** (`manual`, **the only manual criterion**): on his own Android phone, in a real session,
      the speaker picker is gone and nothing about the microphone or the audio route changed. *Why it
      cannot be automated: every automated criterion above verifies the page's response to an assumed
      enumeration. Only his device can confirm the enumeration is the one assumed — a stub proves the
      page handles the Android shape, never that Android produces it.*

## Testing Approach

### Validation steps

1. `venv/Scripts/python.exe -m pytest tests/` — the pure sweep and the structural assertions.
2. The browser harness — serve `voice_tunnel/web/index.html` over a plain static HTTP server on an
   ephemeral port, open it in headless Chromium, install the enumeration stub before the page's
   scripts run, dispatch `devicechange`, and read both `window.__voiceTunnel` and the DOM.
3. Run the harness's own negative control, which must fail on an inverted rule.
4. Hand AC17 to JJ as a single question, on a session he is already in.

### Test cases

| Enumerated outputs | Distinct | Output pill |
|---|---|---|
| none | 0 | hidden |
| `default` (no label) — the Android shape | 1 | hidden |
| `default`, `communications`, `abc123` (one `groupId`) | 1 | hidden |
| card + headset (two `groupId`s) | 2 | visible, two options |
| two outputs, then one after `devicechange` | 2 → 1 | visible, then removed |

| Live sink | Enumerated label | Route text |
|---|---|---|
| unreadable | none | nothing |
| `abc123` | "Headset (WH-1000XM4)" | names it |
| stored preference A, live sink B | B labelled | names B, never A |

## Out of Scope

- Making Android honour an output selection. Not possible from the page.
- The Bluetooth route moving after a server restart — same platform limit, tracked separately as a known
  dead end.
- Any redesign of the page beyond the pickers and the route readout FR3 requires.
- Changing the paired single-pill layout, which is spec `008`-era work and is not revisited here.
- Running or extending any harness that starts a `voice-tunnel` server, for the duration of the live
  session (TC4).

## References

- Project node: `Voice Tunnel` — the two device rows this spec makes tractable, and the live report
  behind them.
- `scripts/orbstate.py` — the pure-model-plus-real-transitions pattern AC6 and AC7 follow.

## Verified state

*Filled in by the implementer after verification, per the completion rule. Empty at the refined-spec
gate: nothing here has been built yet.*

**Baseline before any change (2026-08-19):** `python -m pytest tests/` — 617 passed, 2 skipped, exit 0
(the count includes spec `010`'s 42 new tests).
