---
id: "004"
title: Learned Turn Detection
status: in_progress
blocked_by: ["001"]
blocks: []
---

# Learned Turn Detection

## Overview

Replace the fixed end-of-utterance silence timeout with a learned decision about whether the
speaker has actually finished, and remove the hand-tuned constant from the client's speaking
signal. Both changes are mined from [huggingface/speech-to-speech][hf], which runs this stack in
production on thousands of Reachy Mini robots.

`VOICE_TUNNEL_END_OF_UTTERANCE_MS` is 1500 ms. It was raised from 1000 after the owner was repeatedly cut
off mid-thought, and **it is a permanent compromise**: short enough still interrupts someone
composing out loud, long enough makes every short question wait 1.5 s for nothing. No single
number fixes both, because the right wait depends on whether the sentence sounded finished.

> **Completion rule:** This spec is not complete until the acceptance criteria are verified by
> running the model against real recorded speech from `sessions/*.wav` — utterances the owner actually
> spoke, including the ones that were cut off. Unit tests with synthetic arrays are necessary and
> not sufficient: the failure this fixes is about prosody, which synthetic audio does not have.

## Goals

- End a turn when the speaker has finished, not when a timer expires.
- Cut the wait on a clearly-finished short question well below 1500 ms.
- Keep waiting when the utterance sounds unfinished, past where the timer would have given up.
- Preserve `UtteranceBuffer`'s purity so its turn-boundary logic stays unit-testable with no model.
- Never make the tunnel less reliable than the timer it replaces: every failure degrades to
  current behaviour.

## Requirements

### Functional Requirements

- **FR1**: When trailing silence reaches the timeout, the buffer consults a turn detector before
  closing the utterance. `complete` closes it; `incomplete` extends the wait.
- **FR2**: Extension is bounded. After `VOICE_TUNNEL_TURN_MAX_WAIT_MS` beyond the normal timeout,
  the turn closes regardless of what the model says.
- **FR3**: With no detector configured, behaviour is byte-identical to today's timer.
- **FR4**: A detector that raises, times out, or is unavailable degrades to the timer and records
  the reason once, not per utterance.
- **FR5**: `voice-tunnel download turn` fetches the model; `doctor` reports whether it is present
  and names the command when it is not.
- **FR6**: The client's speaking signal requires a minimum run of continuous speech before it
  asserts, so a cough or a keyboard knock cannot hold a reply.
- **FR7**: `turns` and `timing` record which mechanism ended each turn and how long the model
  added, so the change is measurable rather than assumed.

### Non-Functional Requirements

- **NFR1**: CPU only. No GPU code path, consistent with TC6 of spec 002.
- **NFR2**: Inference runs only at a speech-to-silence boundary, never per audio chunk.
- **NFR3**: Added latency at the boundary is under 150 ms on this machine.
- **NFR4**: The model is an optional extra. A core install keeps the timer and says so.

### Technical Constraints

- **TC1**: Model is [pipecat-ai/smart-turn-v3][st] v3.2, `smart-turn-v3.2-cpu.onnx`, int8, ~8 MB,
  BSD-2-Clause.
- **TC2**: Inference through `onnxruntime`, already shipped under the `[parakeet]` extra.
- **TC3**: Input is 16 kHz mono float32, the **last 8 seconds** of the utterance.
  `config.TARGET_SR` is already 16000.
- **TC4**: Log-mel features come from `transformers.WhisperFeatureExtractor`, which makes
  `transformers` a new dependency of the extra.
- **TC5**: `UtteranceBuffer` must not import a model. The detector is INJECTED as a callable, so
  the buffer stays pure and testable with a fake.
- **TC6**: Defaults follow HuggingFace's production values: threshold 0.5, max wait 2000 ms,
  incomplete delay 600 ms.

### Requirement Traceability

| Requirement | Acceptance Criteria |
|---|---|
| FR1 | AC1, AC2, AC8 |
| FR2 | AC3 |
| FR3 | AC4 |
| FR4 | AC5 |
| FR5 | AC6, AC7 |
| FR6 | AC9 |
| FR7 | AC10 |
| NFR2 | AC11 |
| NFR3 | AC11 |
| NFR4 | AC4, AC7 |
| TC5 | AC4, AC12 |
| TC6 | AC1, AC3 |

## Key Decisions

### The detector is injected, not imported

`UtteranceBuffer` documents itself as *"Pure — no model, no I/O — so the turn-boundary logic is
unit-testable with synthetic arrays."* That property is load-bearing: the buffer holds the
hardest-won tuning in the project, including the fix for the AC white-noise bug that no synthetic
test could reproduce. Importing onnxruntime into it would make every one of those tests depend on
a model download.

So the buffer takes a `turn_detector` callable — `(samples) -> True | False | None` — and the
server wires the real one in. No detector is the current behaviour exactly.

### Extend the wait, never shorten it below the floor

The obvious symmetric design lets a confident `complete` end the turn EARLY, before the 1500 ms
has elapsed. That is where most of the latency win is, and it is also where the risk is: ending
early on a wrong prediction reintroduces the exact failure the 1500 ms was raised to fix.

**Both directions ship, but the early exit is gated behind its own floor**
(`VOICE_TUNNEL_TURN_MIN_SILENCE_MS`, default 400 ms). Below that no amount of model confidence
closes a turn, because sub-400 ms gaps are inside normal speech.

### Degrade to the timer, always

Every failure mode — model absent, load error, inference exception, extras not installed — falls
back to the current timer. A tunnel that stops segmenting because a turn-detection model is
missing would be a far worse regression than the one being fixed.

### Deferred, with reasons

**Silero VAD v5 is not in this spec.** It would replace the RMS-plus-rolling-percentile
segmentation, which is the single most delicately tuned piece of code here — the noise floor is
measured rather than fixed precisely because a fixed one failed against a real air conditioner,
and that was found live, not by testing. Smart Turn sits *after* the VAD boundary and needs no
change to it, so it can be taken on its own. Replacing the VAD is a separate spec with its own
regression risk.

**Speculative turns are DECIDED AGAINST, not merely deferred — Live, 2026-08-06.**

He asked how an agent would even learn that a turn had grown, given `watch` blocks and the drain
loop already exists, and then answered it himself: *"I don't understand how these speculative
turns would be different."*

He is right. **Draining already handles "more is arriving."** The only thing speculation buys is
starting work a second or two before the speaker finishes — and this system has measured that
gap twice: the tool spends ~2 s and the AGENT spends 15-30 s. Saving one second at the front of a
thirty-second wait is noise. **It is a latency fix for a bottleneck this design does not have**,
bought with a contract change that turns a turn from a fact into a draft.

Revisit only if agent latency drops by an order of magnitude, which would change the arithmetic
rather than the argument.

The original architectural objection, kept because it is still true:
`SpeculativeTurnTracker` ends a turn provisionally, starts work, and reopens it with a revision
bump if the speaker continues. HuggingFace can do that because the turn lives inside their
pipeline until committed. **Here a turn is appended to a JSONL log and handed to an external
agent through a cursor** — by the time it could be reopened, the agent may already have read it,
reasoned about it, and replied. Adopting this needs a revision field in the turn schema and a way
to tell an agent "the thing you just read grew", which is a contract change, not an optimisation.
Worth doing; not worth smuggling into this one.

## Implementation Tasks

- [x] `voice_tunnel/turndetect.py` — load the ONNX model, extract features, return
      `(complete, probability, inference_ms)`. Lazy load, single session, serialized.
- [x] Inject the detector into `UtteranceBuffer` as a callable; keep the no-detector path
      identical.
- [x] Extend and early-exit logic at the existing `ended` seam, with both bounds.
- [x] `voice-tunnel download turn` and a `doctor` check.
- [x] Client: require a minimum run of speech before asserting `speaking`.
- [x] Record the end reason and the model's added latency in the turn log and `timing`.
- [x] Config entries with their rationale, per the config-as-data convention.

## Findings — implementer

### Measured on this machine

| | |
|---|---|
| Model | `smart-turn-v3.2-cpu.onnx`, **8.68 MB**, BSD-2-Clause |
| **Cold first call** | **4.7 s** — ONNX session plus the `transformers` import |
| **Warm inference** | **133 ms** median on a 4 s utterance, **59 ms** on a 1 s one |
| Live server, warmed | `calls: 1, last_ms: 59.4` |

**The thread count was wrong until it was measured, and the intuition was backwards.** This
shipped with `intra_op_num_threads = 1` on the reasoning that a model running beside ASR should
be polite. Measured over a 4 s utterance:

    intra_op=1   212 ms
    intra_op=2   196 ms
    intra_op=4   133 ms     <- default now
    intra_op=8   156 ms     <- contention beats parallelism past here

Being "polite" cost 79 ms on every turn, and 8 threads was slower than 4 anyway. The default is
now 4.

**Cold start had to be warmed, for exactly the reason the piper voice is.** 4.7 s unwarmed lands
on the first thing he says — while he is waiting to find out whether any of this works.

### What the real-speech pass did and did not establish

Scored against 29 utterances from `sessions/live.wav`, real speech through this tunnel. The model
loads, runs at the measured latency, and calls finished questions complete. **It has not yet been
shown to prevent a cut-off**, and the reason is a limitation of the corpus rather than the model:
every segment in the log is audio the OLD timer already decided was over, so the set is biased
toward complete by construction. Two observations worth keeping:

- `"Sure, um"` scored **complete**, which is wrong on its face.
- `"can you hear me?"` scored **complete** once and **incomplete** once, on two different
  recordings of the same words — which is the model reading prosody, and may be right both times.

**The honest state: the mechanism is in and instrumented, the benefit is not yet demonstrated.**
`end_reason` and `turn_model_ms` are in the timing log precisely so the next live session answers
this with data instead of impressions.

## Acceptance Criteria

- [ ] **AC1**: An utterance the model calls incomplete does NOT close at the normal timeout; it
      closes later. `unit` with an injected fake.
- [ ] **AC2**: An utterance called complete closes at or before the normal timeout. `unit`
- [ ] **AC3**: A detector that always says incomplete cannot hold a turn open beyond
      `TURN_MAX_WAIT_MS` past the normal timeout. `unit`
- [ ] **AC4**: With no detector, every existing segmentation test passes unchanged and the buffer
      imports no model. `unit`
- [ ] **AC5**: A detector that raises falls back to the timer, the turn still closes, and the
      reason is recorded once. `unit`
- [ ] **AC6**: `voice-tunnel download turn` fetches the model and is idempotent. `integration`
- [ ] **AC7**: `doctor` reports the detector as present or names the command that installs it, and
      never fails a working install for its absence. `integration`
- [ ] **AC8**: Against **real recorded speech** from `sessions/*.wav`, the model calls a
      deliberately trailing utterance incomplete and a finished question complete. `integration`
- [ ] **AC9**: A 50 ms burst does not assert the client speaking signal; a 400 ms one does.
      `integration`
- [ ] **AC10**: A closed turn records which mechanism ended it and the model's added milliseconds.
      `unit`
- [ ] **AC11**: Inference runs once per boundary, not per chunk, and adds under 150 ms measured on
      this machine. `integration`
- [ ] **AC12**: `voice_tunnel/asr.py` contains no import of onnxruntime or transformers at module
      scope. `unit`

## Testing Approach

### Against real speech, not synthetic arrays

`sessions/*.wav` holds every utterance the owner has spoken through this tunnel, including the ones that
were cut off mid-thought — the failures that produced the 1500 ms constant. **That is the test
set.** Prosody is the entire signal the model reads, and synthetic audio has none.

### Test Cases

| Input | Expected |
|---|---|
| "what is the status" (finished, falling) | complete, closes at or before the timeout |
| "I was thinking that maybe we could—" (trailing) | incomplete, wait extends |
| detector always incomplete | closes at timeout + TURN_MAX_WAIT_MS |
| detector raises | closes at the timeout, reason recorded once |
| no detector configured | identical to today |
| 50 ms burst on the client | speaking not asserted |

## Out of Scope

- Silero VAD, and any change to how speech-versus-silence is detected.
- Speculative turns and turn revisions.
- Barge-in — cutting the agent off mid-sentence — which remains its own roadmap item.
- The OpenAI Realtime protocol.
- Any change to the wake gate or the voiceprint.

## References

- [huggingface/speech-to-speech][hf] — `VAD/smart_turn.py`, `VAD/vad_handler.py`; the source of
  the defaults in TC6
- [pipecat-ai/smart-turn][st] — the model, its training data and its licence
- `specs/002-packaging.md` — the extras pattern this follows
- `Voice Interface to Claude` — the research and what was deliberately not taken

[hf]: https://github.com/huggingface/speech-to-speech
[st]: https://github.com/pipecat-ai/smart-turn
