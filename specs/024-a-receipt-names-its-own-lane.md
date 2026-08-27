---
id: "024"
title: A receipt names its own lane
status: in_progress
blocked_by: []
blocks: []
---

# A receipt names its own lane

## Overview

`speaking_lane` was ONE slot for N agents. A second clip going out overwrote the first, so the
first `played` receipt released the wrong lane and nulled the slot — leaving the real speaker on
"speaking" with nothing left in the system that could ever clear it.

He saw it as a lane stuck saying *speaking* while a different lane's hand stayed raised.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"I have noticed this bug. Like I see an agent lane that has its hand raised. And when I switch
> to it, it doesn't start playing. And another lane says speaking."*
>
> *"I don't know. Is it because I switch while the other one is synthesizing?"* · *"This has just
> started happening."*
>
> *"I think it was the Dexter line."* · *"Yes, we need to fix that. It just happened again with
> Atlas."*
>
> (2026-08-26, voice-dictated; verbatim record in `sessions/dev.jsonl`, turns 3102–3105, 3124,
> 3127 — that log is gitignored, so the quotes carry the weight and not the ids)

## Findings — the last instance of a pattern that ran through five specs

**A fact made per-lane in STORAGE and left session-wide in RESOLUTION.** `lane_states` is a dict;
`speaking_lane` was a single string. The same seam produced spec `013` (the transcript tag), `015`
(the read cursor), `016` (the agent state), `017` (the cursor's consumers) and `018` (four more
facts). This is the last one on that list.

⚠ **His first report also contained a false lead, and chasing it cost time.** *"This has just
started happening"* pointed at spec `022`, shipped an hour earlier. It was not that: the log showed
`022` working correctly — an Atlas switch flushed three clips and all three played. The defect is
older and was simply easier to hit once four lanes were busy.

🎯 **The receipt always carried a clip id and never a lane**, and the comment on `speaking_lane`
said so in as many words: *"the receipt carries a clip id and no lane, and by the time it lands he
may have moved on."* The fix is not new information — it is keying by the id that was already
there, instead of guessing from a slot.

## Goals

- **A receipt releases the lane whose clip it was**, whoever else has spoken since.
- **A lane still playing is not released by somebody else's receipt.**

## Requirements

### Functional

- **FR1** — **Every clip sent records the lane it belongs to**, including each clip in a flush,
  which sends several at once.
- **FR2** — **A `played` receipt releases the lane its clip names.**
- **FR3** — **A lane whose clip is still playing keeps its state** until its own receipt lands.

### Non-functional

- **NFR1** — **The map holds only what is in flight.** A session that plays a thousand replies must
  not carry a thousand entries.

### Technical constraints

- **TC1** — **`speaking_lane` is cleared only when the receipt names it.** Clearing it
  unconditionally is precisely what stranded the other speaker.
- **TC2** — **A dropped playback queue forgets everything in flight**, because no receipt is coming
  for any of it.

  🔴 **THE STATED REASON IS FALSE, AND SPEC 025 MEASURED IT.** A receipt IS coming for one of them:
  the clip that was playing, because stopping playback is what makes the browser fire `onended`.
  Spec `026` narrows this constraint to everything *except* that clip. The claim was plausible,
  written confidently, and wrong — worth leaving visible rather than quietly editing away.

## Implementation Tasks

- [x] A clip→lane map, populated wherever a clip goes out.
- [x] The receipt looks its lane up by clip id.
- [x] `speaking_lane` is cleared only on a matching receipt; barge-in clears the map.

## Acceptance Criteria

- [x] **AC-1** `unit:tests/test_two_lanes_speaking.py` — **FR1.** Two clips out, each records its
      own lane.
- [x] **AC-2** `unit:tests/test_two_lanes_speaking.py` — **FR2/FR3.** The first receipt releases
      ITS lane and leaves the other speaking. **Verified by mutation:** the single-slot version
      turns this and one sibling red.
- [x] **AC-3** `unit:tests/test_two_lanes_speaking.py` — **FR2.** The second receipt then releases
      the second lane, so the fix cannot pass by never releasing anything.
- [x] **AC-4** `unit:tests/test_two_lanes_speaking.py` — **NFR1/TC2.** A real barge-in forgets
      everything in flight **except the clip that was playing**.

      ⚠ **AMENDED BY SPEC 026.** This originally read "forgets everything", on TC2's reasoning that
      a dropped queue means no receipt is coming. Spec `025` then measured the opposite:
      `stop_playback` is precisely what MAKES the browser fire `onended`, so a `played` for the
      interrupted clip lands about 8 ms later. Emptying the map wholesale sent that receipt through
      the `or state.lanes.current` fallback and marked **the lane he had just turned to** idle.
      One entry survives the clear, and its own receipt removes it.
- [ ] **AC-5** `manual` — **FR3.** With two agents replying at once: confirm no lane is left saying
      "speaking" after both clips have played. Manual because it needs two real agents and real
      playback timing to interleave the receipts.

## Testing Approach

### Validation Steps

1. Run `tests/test_two_lanes_speaking.py`; confirm AC-2 fails against the single slot.
2. Run the full unit suite — `_speak` and the control handler are on nearly every path.

### Test Cases

| Situation | Expected |
|---|---|
| two clips in flight | each records its own lane |
| first receipt arrives | that lane idle, the other still speaking |
| second receipt arrives | both idle, map empty |
| barge-in | map cleared, clips returned to their holds |

## Out of Scope

- **Retiring `speaking_lane` entirely.** It is still the right answer for "who is talking right
  now" in `status`, and narrowing this change to the release path keeps it reviewable.

## References

- `specs/013`, `015`, `016`, `017`, `018` — the same per-lane-vs-session seam, five times.
- `specs/022` — shipped an hour before this was reported, and wrongly suspected; the log cleared it.
