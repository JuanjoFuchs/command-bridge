---
id: "028"
title: A held clip is not said twice
status: in_progress
blocked_by: []
blocks: []
---

# A held clip is not said twice

## Overview

When an agent speaks into a lane he is not looking at, the reply is held and the response says, in
as many words, **"Do not repeat it and do not say it another way: keep waiting on the watch above."**
That is an instruction, and an instruction is not a guarantee. An agent that ignores it queues a
second copy, and both play back-to-back the moment he returns to that lane.

He heard it happen and could not tell whether it was the agent or the tool.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"I think we should continue looking at issues because I just had an issue happen. And I don't
> know if it's an issue of the agent or the voice tunnel. The Kepler agent just spoke twice to me."*
>
> *"Make the same turn repeated twice."*
>
> *"Yeah, that's a good solution."* (to refusing the duplicate rather than trusting the instruction)
>
> (2026-08-27, voice-dictated; the turn log is gitignored and short-lived, so the quotes carry the
> weight and not the ids 3247, 3248, 3253)

## Findings — measured, and it was the agent

From `sessions/dev.timing.jsonl`:

```
13:05:15.439  spoken  clip-1787850314507  lane kepler  "Here it is. You were talking to someone…"
13:06:35.371  say_requested                lane kepler   chars 203
13:06:40.708  spoken  clip-1787850399725  lane kepler  "Here it is. You were talking to someone…"
13:07:25.191  lane_held_flushed            lane kepler   count 2
13:07:38.314  played  clip-1787850314507  lane kepler
13:07:51.002  played  clip-1787850399725  lane kepler
```

**Two clip ids, identical text, eighty-five seconds apart, both held and both played.** The tunnel
did exactly what it promises; the kepler agent said the same sentence twice.

🎯 **THE TOOL PRINTED THE RULE AND DID NOT ENFORCE IT, WHICH IS THE WHOLE FINDING.** The hold
response is unusually explicit — it names the failure and forbids it in the same breath — and it
still happened, because a sentence in a JSON field is advice to a model rather than a constraint on
a system. **The same shape this project has hit repeatedly from the other side:** a guarantee
documented at one site and unenforced at another.

⚠ **The cost is paid twice.** He hears the same thing twice, which is bad, and he cannot tell
whether his tool or his agent is broken — which is worse, because it makes every future report
ambiguous.

## Goals

- **An agent cannot queue the same sentence twice into one lane's hold.**
- **It is told clearly, so it can behave rather than merely fail.**
- **A genuine repeat later still works** — this is about a duplicate *waiting*, not about ever
  saying something twice.

## Requirements

### Functional

- **FR1** — **A `say` whose text matches a clip already held for that lane is REFUSED**, and
  nothing is synthesized or queued.
- **FR2** — **The refusal names the clip it duplicates** and how long that clip has been waiting,
  so the agent can see the hold is real rather than assume its first call failed.
- **FR3** — **Matching ignores case, surrounding whitespace and trailing punctuation**, because an
  agent told not to "say it another way" most cheaply varies exactly those.
- **FR4** — **The refusal applies to `--now` as well, once that clip is in the hold.** The hurried
  path is the one an agent takes when it thinks it was not heard, which is precisely when it
  repeats itself.

  ⚠ **`--now` returns BEFORE synthesis, so its clip lands in the hold a moment later — and two
  fire-and-forget calls issued in the same instant will both pass.** Found by the first draft of
  AC-6, which failed. **The window is left open deliberately:** the measured failure was
  eighty-five seconds wide, agents call `say` sequentially rather than concurrently, and closing a
  millisecond race would mean reserving keys before synthesis — defensive handling for a scenario
  that has not happened, which rule 7 of the spec-writing rules says to cut.

### Non-functional

- **NFR1** — **Refused before synthesis**, the same shape as the unread and no-lane refusals: a
  refusal that has already produced audio is not a refusal.

### Technical constraints

- **TC1** — **Only against clips currently HELD for that lane**, never against what has already
  played. Saying the same sentence again an hour later is ordinary speech, and blocking it would
  make the tool argue with him.
- **TC2** — **A different lane holding the same text is not a duplicate.** Two agents may
  legitimately reach the same sentence, and they are different voices to him.

## Implementation Tasks

- [ ] A normalizer for comparison — case, whitespace, trailing punctuation.
- [ ] The check in the `say` handler, beside the existing refusals and before synthesis.
- [ ] The refusal payload: code, the duplicated clip id, and its wait so far.

## Acceptance Criteria

- [ ] **AC-1** `unit:tests/test_duplicate_hold.py` — **FR1/NFR1.** A second `say` with identical
      text into a lane that already holds it is refused, and the hold depth stays 1.
      **Verify by mutation:** removing the check turns this red.
- [ ] **AC-2** `unit:tests/test_duplicate_hold.py` — **FR2.** The refusal carries the clip id it
      duplicates and a wait in seconds.
- [ ] **AC-3** `unit:tests/test_duplicate_hold.py` — **FR3.** Case, trailing whitespace and a
      changed final full stop are all still duplicates.
- [ ] **AC-4** `unit:tests/test_duplicate_hold.py` — **TC1.** Once the hold is flushed, the same
      text is accepted again.
- [ ] **AC-5** `unit:tests/test_duplicate_hold.py` — **TC2.** The same text held for a DIFFERENT
      lane does not block this one.
- [ ] **AC-6** `unit:tests/test_duplicate_hold.py` — **FR4.** `--now` is refused on the same terms.
- [ ] **AC-7** `unit:` full suite — the `say` handler is on nearly every path in this tool.
- [ ] **AC-8** `manual` — **FR1.** With two agents live, have one repeat itself off-lane and
      confirm only one clip plays on return. Manual because it needs a second agent actually
      misbehaving.

## Testing Approach

### Validation Steps

1. Write each assertion, then **break the fix and confirm the assertion goes red** — the standard
   this repo adopted after two worthless tests shipped on 2026-08-26.
2. Run the full unit suite.

### Test Cases

| Situation | Expected |
|---|---|
| same text, same lane, already held | refused; nothing synthesized; depth unchanged |
| same text differing only in case/space/full stop | refused |
| same text after the hold flushed | accepted |
| same text held for another lane | accepted |
| `--now` with duplicate text | refused |

## Out of Scope

- **Deduplicating against clips already played.** TC1 — that is ordinary repetition and his to
  judge, not the tool's.
- **Detecting a paraphrase.** Only an exact match after normalisation. Anything cleverer needs a
  model, and a tool that silently swallows an agent's second, *different* sentence would be a worse
  failure than the one being fixed.

## References

- `specs/012` FR7 — the off-lane hold whose instruction this makes enforceable.
- `specs/007` — the unread refusal whose shape and placement this copies.
