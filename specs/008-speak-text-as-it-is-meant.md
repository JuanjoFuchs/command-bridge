---
id: "008"
title: Speak text as it is meant — technical terms and phrase-level pacing
status: in_progress
blocked_by: []
blocks: []
---

# Speak text as it is meant

> **Refined from the strategist's metaspec by the repo implementer, 2026-08-19.** The metaspec's goal,
> ruling and constraints are preserved. **Its stated mechanism was measured and is false** — see
> [Measured ground truth](#measured-ground-truth-2026-08-19). The defect is real and is worse than
> described; the fix and the verification method both change as a result.

## Overview

The text an agent writes and the text a listener needs are not the same string. Two gaps are open, both
between composing a reply and synthesising it, and neither is a property of the voice model.

A version number is the clearest case: `0.2.6` does not arrive as a version number.

> **Completion rule:** This spec is not complete until every acceptance criterion is verified by the
> method named on it. Build-only verification is insufficient. The agent must iterate until verification
> passes.

## JJ's ruling (2026-08-19)

> I'd like to make sure that you are able to pronounce technical terms properly. When we mention
> something with dots — for example `0.2.6` — you don't say "point", you use a dot, and that is
> considered like an end of the sentence, so it's spoken as a wait, a silence.

*(Voice-dictated, so the transcript is fuzzy per the untrusted-transcript rule. The **ask** is
unambiguous and is what this spec implements: say the dots. The **diagnosis** in the second half is what
the measurement below contradicts.)*

## Measured ground truth (2026-08-19)

Measured on the engine he is actually listening to — `kokoro`, voice `bm_daniel`, speed `1.2`, sentence
pause `0.85`, read from the live `.env` — because a measurement on the module defaults answers about
`piper` at speed `1.18` and would have been a different question. *(That mistake was made once and
caught: `config` does not load the settings file on import, only the CLI entry point does.)*

### The dots are ELIDED, not turned into silences

| Text synthesised | Internal silence ≥ 60 ms | Recognizer hears |
|---|---|---|
| `0.2.6` | **none** | `026.` |
| `We are on 0.2.6.` | **none** | `We are on 026.` |
| `The version is 0.2.6 and it is ready.` | **none** | `The version is 026 and it is ready.` |
| `Version 1.0.0 shipped.` | **none** | `Version 100 shipped.` |
| `Open config.py.` | **none** | `Open Config Py.` |
| `The version is ready. It is ready now.` | **990 ms** | — |

**There is no silence to remove.** A real sentence boundary produces a 990 ms gap; a dotted term produces
none at all. What actually happens is that the dot is **dropped**, and the digits run together into a
different number — `0.2.6` is spoken as "zero two six" and `1.0.0` as "one hundred".

**This is worse than the metaspec described, not better.** A pause is a term arriving awkwardly. An
elision is a term arriving **wrong**, with nothing in the sound to signal that anything was lost. And it
explains the report exactly: a listener who hears "026" where a version was expected experiences the term
as broken, and "it fragmented into silences" is a reasonable thing to call that from the outside.

### The verification method is settled, and it is fully automatable

TC1 of the metaspec said the method for FR2 "needs deciding rather than assuming". It is decided:
**a round trip through the recognizer, with a negative arm that is separable only by the transformation
under test.**

| Synthesised | Recognizer hears |
|---|---|
| `0.2.6` | `026.` |
| `zero point two point six` | **`0.2.6.`** |
| `config dot pie` | **`config.py`** |
| `voice tunnel dot config dot speech speed` | **`Voice tunnel.config.speech speed.`** |

**The correct rendering round-trips back to the original string; the current one does not.** That rules
out the obvious trap — that `026` is a recognizer artifact rather than what was spoken — because the two
inputs differ by exactly the thing being fixed and produce different outputs. **The agent never has to
judge audio it cannot hear** (TC1), and the assertion is on the string, not on a verdict.

### Comma-level pacing genuinely does not exist

Measured on the same clips: a sentence boundary yields 990 ms; every comma and clause break inside a
sentence yields **0 ms of measurable silence**. FR3 is confirmed as an open gap rather than an assumed one.

## Goals

- A technical term arrives as one term, correctly, at the pace of speech.
- The pacing of a spoken reply is a property of what the text means, not of where its punctuation
  happens to fall.
- The normalisation is inspectable, so a wrong reading can be diagnosed rather than guessed at.

## Requirements

### Functional Requirements

- **FR1**: Text is **normalised for speech before synthesis**, as a named stage between the caller's
  string and the engine. *Rationale: the fix belongs on the path, not in the caller. Asking every agent
  to spell out its own version numbers is the same class of rule as the ones spec `007` exists to stop
  relying on.*
- **FR2**: **Dotted technical terms are spoken as terms**, with the separator **voiced**. The dot is
  never dropped and never a sentence boundary. The classes, and how each is voiced:

  | Class | Example | Spoken as | Why |
  |---|---|---|---|
  | version / dotted number | `0.2.6`, `1.0.0`, `3.5` | "point" | How the number is read aloud in English; `3.5` and `0.2.6` are the same construction and must not diverge |
  | dotted identifier | `voice_tunnel.config.speech_speed` | "dot" | How an engineer says it |
  | file extension | `config.py`, `.env` | "dot" | Same |
  | domain | `example.com` | "dot" | Same |
  | ellipsis | `...` | unchanged | It is prosody, not a term; "point point point" is the over-normalisation TC2 forbids |
  | sentence-final period | `ready.` | unchanged | It really is a sentence boundary |

- **FR3**: **Phrase-level pacing** — the comma-level prosody priced on 2026-08-06 and never built. A
  reply breathes where its meaning breaks, not only where a full stop lands. The break at a comma must
  be **measurably present and measurably shorter** than the break at a sentence end.
- **FR4**: The normalised string is **recoverable for a given clip** through the CLI, so "why did it say
  that" is answerable from the record rather than by re-deriving it. *Rationale: a transform nobody can
  inspect is a transform nobody can debug.*
- **FR5**: Normalisation is **engine-independent** — above whichever backend answers. *Both engines were
  measured and both drop the dot, so this is not a kokoro workaround.*

### Non-Functional Requirements

- **NFR1**: No perceptible latency added to the speaking path. This is string work against a model call
  measured in seconds (0.6–0.8 s per clip, measured).
- **NFR2**: The change is **audible on the case JJ named**. `0.2.6` read aloud is the acceptance case,
  and it is verified by the round trip rather than by anyone's ear.

### Technical Constraints

- **TC1**: **The agent cannot hear, and must not be the judge of whether this worked.** Resolved above:
  the recognizer is the judge for FR2 and a silence measurement is the judge for FR3. **No acceptance
  criterion in this spec is an audio verdict written by the agent that produced the audio.**
- **TC2**: **Over-normalising is a real failure and is worse than the current state, because it is
  silent.** A rule that rewrites an ellipsis, a sentence-final period, or ordinary prose containing a
  decimal changes what he hears with no signal that it did. Every class in FR2's table needs a
  counter-case proving the rule did **not** fire where it should not.
- **TC3**: Speed interacts with intelligibility and is already measured — every Kokoro British male loses
  36–50% of its words at speed 2.0, against alan's 3.6%. **Evaluate at the speed actually in use
  (`1.2`), not at 1.0**, and read it from the settings file rather than the module default.
- **TC4**: 🔴 **A live voice session is running on session `dev`, port 8765. Nothing in this spec may
  start, restart or stop a `voice-tunnel` server.** Synthesis and recognition are both callable
  in-process and need no server. Synthesis costs a short CPU burst on a machine that is currently
  serving a conversation; keep the acceptance suite's clip count small for that reason.
- **TC5**: The engine's own text frontend is a black box and is **not** to be modified or bypassed. The
  normalisation is a string transform above it.

## Key Decisions

| Decision | Why | Rejected |
|---|---|---|
| Normalise on the path, not in the caller | Every agent would otherwise carry the rule, and forget it | Telling agents to spell out versions |
| Voice the dot rather than suppress a pause | Measured: there is no pause. The dot is dropped, and the term is *wrong*, not merely fragmented | The metaspec's "elide or voice" — eliding is what it already does, and is the defect |
| "point" for numbers, "dot" for identifiers | It is how each is actually said; one uniform word is wrong for one of the two halves | A single separator word everywhere |
| The recognizer is the judge | Its negative arm is separable only by the transformation under test, so it cannot pass blind | A human listening; a sharpness/timbre score, which cannot see pacing at all |
| Keep the normalised string, retrievable per clip | Otherwise a wrong reading is unfalsifiable | Transform and discard |
| Engine-independent | Both installed engines drop the dot; the choice has already changed once | Kokoro-specific handling |

## Implementation Tasks

- [ ] Add the speech-normalisation stage between the caller's text and the engine, on the single path
      every backend goes through.
- [ ] Implement the FR2 classes, each with its counter-case in mind.
- [ ] Implement FR3's phrase-level pacing.
- [ ] Record the normalised string per clip and expose it through the CLI (FR4); update `describe` in the
      same change, per the repo's contract rule.
- [ ] Add the round-trip acceptance test, with its negative arm.
- [ ] Add the over-normalisation counter-cases (TC2).
- [ ] Add the pacing measurement with its own negative arm.
- [ ] Run the full suite.

## Acceptance Criteria

### The transform itself

- [ ] **AC1** (`unit`): each FR2 class normalises as its table row says, asserted on the **string**, with
      no audio involved.
- [ ] **AC2** (`unit`, **counter-cases for TC2**): the transform does **not** fire on — an ellipsis, a
      sentence-final period, a bare decimal already inside prose that reads correctly today, an
      abbreviation like `e.g.`, and a number with no dot. Each asserted as *unchanged*.
      *Rationale: over-normalising is silent, so the only way it surfaces is a test that fails when it
      happens.*
- [ ] **AC3** (`unit`): normalisation is applied on the shared synthesis path, so it reaches every
      backend (FR5) — asserted by driving the path with each backend selected, not by reading the source.

### It is audible, judged by the recognizer and not by the agent

- [ ] **AC4** (`integration`, **round trip**): synthesising `The version is 0.2.6 and it is ready.` and
      transcribing the result yields text containing `0.2.6`. **This test fails today**, returning `026`.
- [ ] **AC5** (`integration`, **negative arm**): the same round trip on the *pre-normalisation* string
      still yields `026`. *Rationale: AC4 passing proves nothing unless the untransformed input is shown
      to fail through the identical path. The two differ only by the transform.*
- [ ] **AC6** (`integration`): a dotted identifier and a file extension round-trip to text containing the
      dot.
- [ ] **AC7** (`integration`): run at the **live speed (1.2)** read from the settings file, not at 1.0
      and not at the module default (TC3).

### Pacing (FR3)

- [ ] **AC8** (`integration`): a comma inside a sentence produces a measurable silence, where today it
      produces **0 ms**.
- [ ] **AC9** (`integration`, **negative arm**): a sentence boundary in the same clip produces a
      **longer** silence than the comma. *Rationale: a change that simply lengthened every gap would pass
      AC8. This is the arm that separates "pacing" from "slower".*
- [ ] **AC10** (`integration`, **negative arm**): a clip with no comma and no sentence break gains **no**
      new internal silence. *Rationale: without this, "add a pause everywhere" passes both criteria above.*

### Inspectability (FR4)

- [ ] **AC11** (`integration`): after a `say`, the normalised string for that clip is retrievable through
      the CLI, keyed by the clip.
- [ ] **AC12** (`unit`): `describe` documents the field, per the repo's add-a-command-update-describe rule.

### Suite

- [ ] **AC13** (`integration`): `python -m pytest tests/` passes with no test weakened or skipped, and no
      `voice-tunnel` server was started (TC4).

## Testing Approach

### Validation steps

1. `venv/Scripts/python.exe -m pytest tests/` — the string-level rules and their counter-cases.
2. The round-trip acceptance test — synthesize in-process, transcribe in-process, assert on the returned
   text. No server, no phone, no human ear.
3. The pacing measurement — RMS-windowed silence detection over the rendered PCM, asserting the comma gap
   exists, the sentence gap is longer, and a clip with neither gains nothing.

### Test cases

| Input | Expected spoken form | Round-trip must contain |
|---|---|---|
| `0.2.6` | zero point two point six | `0.2.6` |
| `1.0.0` | one point zero point zero | `1.0.0` |
| `3.5 dollars` | three point five dollars | `3.5` |
| `config.py` | config dot pie | `config.py` |
| `example.com` | example dot com | `example.com` |
| `Wait... really?` | unchanged | no "point" |
| `It is ready.` | unchanged | no "point" |
| `e.g. this` | unchanged | no "dot" |

## Out of Scope

- Changing the voice or the engine. Settled; evidence is in the project node.
- The de-esser and any timbre work — closed by measurement.
- Learning his vocabulary from corrections, and the cadence table. Both need his data.
- SSML as a public interface. Internal markup is the implementer's call; the CLI contract stays plain text.
- Modifying or bypassing the engine's own text frontend (TC5).
- Anything that starts a server while the live session runs (TC4).

## References

- `specs/004-turn-detection.md` — the speed and intelligibility measurements that bound TC3.
- Project node: `Voice Tunnel` — the roadmap rows this spec closes, and the sharpness work behind TC1.

## Verified state

*Filled in by the implementer after verification, per the completion rule. Empty at the refined-spec
gate: nothing here has been built yet.*

**Baseline before any change (2026-08-19):** `python -m pytest tests/` — 892 passed, 2 skipped, exit 0.
