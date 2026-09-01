---
id: "020"
title: A clip knows when its words land
status: in_progress
blocked_by: []
blocks: []
---

# A clip knows when its words land

## Overview

`say` returns how long a clip is and nothing about its shape. A caller that wants to point at the
thing being named — highlight a diagram region, advance a slide, move a cursor — can only align to
the whole clip, so a pointer either runs ahead of the words or lags them, and a sentence with three
references degrades to one gesture.

**The schedule already exists at the moment `say` returns.** The clip is fully synthesized before
it is handed to the transport, and Kokoro's graph decides each token's duration on the way to the
waveform. Nothing is missing; it is simply not reported.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he asked for, verbatim

> *"`say` should return **when each word will be spoken**, so a caller can align something else to
> the speech — a highlight, a slide, an animation — instead of estimating it. Crucially: at
> synthesis time, in the `say` response itself, not streamed during playback."*
> (GitHub issue #1, opened by JJ via another agent)

> *"I think, yes, that's fine."* · *"So while I listened to that, you built what you were going to
> build."* (2026-08-26, turns 2902 and 2904)

## Findings — measured before writing this, and two of them change the design

Full landscape pass in Local TTS for the Voice Tunnel - Research. What was measured **on this
machine, against this checkout**:

| # | Finding | Consequence |
|---|---|---|
| 1 | The timestamped export is **bit-identical** to the shipping model on audio — `max abs diff 0.0`, same weights, one extra output | **Swapping the model file carries no audio risk at all.** This is what makes the change small |
| 2 | `samples == sum(max(1, round(dᵢ))) × 600`, **exact on all 14 cases** — four voices, speeds 0.8–4.0 | The conversion is arithmetic, not estimation |
| 3 | Word starts agree with **Parakeet TDT** — an independent model that never saw the durations — to **±46 ms (1.9 duration units)** | The feature is demonstrated, not argued |
| 4 | 🔴 **`kokoro-onnx` 0.5.0 casts `speed` to `np.int32` on the `input_ids` branch** | **The naive swap makes every reply RAISE** — see TC1 |
| 5 | Token→word grouping is a split on the space token (id 16): 74 tokens → 12 words, first try | The grouping is genuinely simple; the risk is upstream of it |
| 6 | `Kokoro.tokenizer.phonemize()` is reusable, and gives the same phonemes the tunnel already speaks | **The misaki-normalisation risk stays the library's problem**, not ours |

🔴 **Finding 4 breaks the tunnel outright.** The shipping export names its input `tokens` and the
library sends `speed` as float32; the timestamped export names it `input_ids`, and on that branch
the library sends `np.array([speed], dtype=np.int32)` into a graph that declares the input float:

```
InvalidArgument: Unexpected input data type. Actual: (tensor(int32)), expected: (tensor(float))
```

⚠ **I predicted this would be a SILENT slowdown — "his 1.2 becomes 1" — and wrote that into this
spec before testing it.** It is not silent; it raises, and every Kokoro reply fails. The
prediction was wrong in the direction that matters for sequencing: a silent regression can ship
and be fixed later, a hard failure cannot ship at all. **Which is why the model preference is not
flipped until the caller that can drive it exists** — see `config.kokoro_timestamped_model`.

🎯 **The transferable half: a drop-in replacement is only drop-in on the axis you compared.** The
audio was bit-identical, which is exactly what made the swap look free — and the breakage was in a
*sibling* argument that the comparison never touched.

## Goals

- **A caller can set its timers before the audio starts.** The schedule ships in the `say` response.
- **A clip that is not asked for timings costs exactly what it costs today.**
- **The timeline reported is the timeline played** — every offset the audio path introduces is
  accounted for, not approximated.

## Requirements

### Functional

- **FR1** — **`say --timings` returns a word schedule** alongside the existing fields: each entry
  carries the word and the seconds from the start of the clip at which it begins.
- **FR2** — **Offsets are measured against the clip as delivered**, including the inter-sentence
  silence the backend inserts and any leading silence it trims. A schedule true only for the first
  sentence is worse than none.
- **FR3** — **Without the flag the response is byte-for-byte what it is today**, and no extra work
  is done.
- **FR4** — **An engine that cannot produce a schedule says so** rather than estimating one. A
  guessed offset presented in the same shape as a measured one is the failure this feature exists
  to remove.

### Non-functional

- **NFR1** — **No second forward pass.** The durations come from the pass that made the audio.
- **NFR2** — **The audio is unchanged**, bit for bit, whether or not timings were requested.

### Technical constraints

- **TC1** — 🔴 **The synthesis call must not route `speed` through `kokoro-onnx` 0.5.0's
  `input_ids` branch**, which casts it to `int32` (finding 4). The tunnel keeps the library for
  phonemization, tokenization and voice-style lookup — the parts that are hard and that upstream
  owns — and issues the model call itself, where the dtype is ours.
- **TC2** — **Trimming is ours, so the trimmed amount is known.** The library trims leading and
  trailing silence inside `create`; a trim we do not perform is an offset we cannot subtract.
- **TC3** — **The first token is padding, not speech.** Measured at 11.946 units ≈ 299 ms against a
  word-one start of 0.300 s. It must be excluded from the first word rather than highlighted.
- **TC4** — **Model file swap, not a dependency bump.** `create_timed()` needs `kokoro-onnx` 0.6.x;
  the two facts above mean we are not calling it, so the pin stays where it is.

## Implementation Tasks

- [x] The timestamped export is resolvable on its own (`config.kokoro_timestamped_model`) **without
      being preferred yet** — the preference flips only once the backend below can drive it, because
      handing it to the library raises rather than degrades.
- [x] The resident Kokoro backend issues its own model call — reusing the library for phonemes,
      tokens and voice style — and keeps the per-token durations beside the audio. **It takes that
      path only when the loaded graph actually reports durations**, decided from the session's own
      outputs rather than the filename, so an install without the export is byte-for-byte unchanged.
- [x] Durations become word offsets: group on the space token, convert with the measured law, and
      accumulate across sentence pieces, their gaps, the trim, and the Bluetooth lead-in.
- [x] `say` grows `--timings`; the schedule rides in the response and is absent without the flag.
      Refused alongside `--now`, which returns before a clip exists to describe.
- [x] `describe` documents the flag, `words`, `words_aligned` and `timings_unavailable`.
- [x] `download kokoro` fetches the timestamped export **instead of** the plain one — same size,
      same audio, plus the durations, so fetching the other costs 325 MB to get strictly less — and
      `kokoro_model()` prefers it. An install predating this keeps working and still counts as
      installed; upgrading is offered rather than forced, behind `--force`.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_word_timings.py` — **FR1.** One entry per group between spaces,
      strictly ordered, the pads excluded.
- [x] **AC-2** `unit:tests/test_word_timings.py` — **FR2.** A second sentence's words are offset by
      the first sentence's audio **and** the silence between them. **Mutation-relevant:** dropping
      `cursor += n` puts every later word early by the accumulated gaps.
- [x] **AC-3** `unit:tests/test_word_timings.py` — **NFR2 / FR3.** The PCM is byte-identical with
      and without `--timings`, and no schedule is returned unless it was asked for.
- [x] **AC-4** `unit:tests/test_word_timings.py` — **TC1.** A fractional speed reaches the graph as
      float32. **Verified by mutation:** restoring the library's `np.int32` turns this red.
- [x] **AC-5** `unit:tests/test_word_timings.py` — **TC3.** The leading pad is not a word, and word
      one starts after it. Two sibling tests cover the rounding and the `max(1, …)` floor, and
      **both go red** when the raw floats are cumulated instead.
- [x] **AC-6** `unit:tests/test_word_timings.py` — **FR4.** An export with no durations returns
      `None`, not an empty list — the server turns that into `timings_unavailable` with a reason.
- [x] **AC-7** `integration` — **FR2.** Measured 2026-08-26 through the real `say` path at his
      speed 1.2, against Parakeet TDT on the same clip: **mean +41 ms, residual spread 84 ms** over
      nine words, `words_aligned: true`, ASR transcript exact. **The second method is the point** —
      the arithmetic can be self-consistent and still describe the wrong timeline.
- [ ] **AC-8** `manual` — **FR1.** He plays a clip and a pointer driven by the schedule stays on the
      word. Manual because "looks aligned to a person" is the actual requirement, and ±46 ms is
      below what a test can claim on his behalf.

## Testing Approach

### Validation Steps

1. Run the new test file; confirm AC-4 goes red when the model call is routed through the library.
2. Confirm the full suite passes — the resident backend is on the path of most TTS tests.
3. Run the cross-check against Parakeet on a freshly synthesized clip (AC-7).

### Test Cases

| Situation | Expected |
|---|---|
| one sentence, default voice | one entry per word, ascending, first ≈ 0 |
| three sentences | later offsets include both audio and gaps |
| same text with and without `--timings` | identical PCM |
| speed 1.2 | clip length matches speed 1.2, not speed 1 |
| leading pad | excluded; word one starts after it |
| backend without durations | absence reported, no estimates |

## Usage Examples

```bash
voice-tunnel say --session dev --lane magnus --timings "The camera is on the left."
```

```json
{"queued": true, "id": "clip-1787673005128", "seconds": 2.35,
 "words": [{"w": "The", "t": 0.0}, {"w": "camera", "t": 0.31}, {"w": "is", "t": 0.79}]}
```

## Out of Scope

- **Piper and SAPI schedules.** Piper's shipped ONNX export returns audio only; FR4 covers saying so.
- **Forced alignment as a fallback.** It is a second model and a second pass, and NFR1 rules it out
  for the engine we actually run.
- **Character offsets into the input text.** Offered as an alternative in the issue; words are what
  a pointer needs, and both would be two contracts to keep true.
- **Anything that consumes the schedule.** The surface this feeds — Tunnel Vision — is a
  separate tool and a separate spec.
- **Per-lane voices.** Raised by JJ the same morning and parked on the board; it changes which voice
  a clip uses, not whether a clip can say when its words land.

## References

- `specs/008` — speech segmentation; the piece-and-gap structure FR2 must account for.
- Local TTS for the Voice Tunnel - Research — the measurements above, and the landscape pass
  that concluded the engine does not need replacing.
- GitHub issue #1 — the request, and why the timing is the only unsolved part of deixis.
