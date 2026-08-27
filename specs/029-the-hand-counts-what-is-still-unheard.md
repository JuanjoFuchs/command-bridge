---
id: "029"
title: The hand counts what is still unheard
status: in_progress
blocked_by: []
blocks: []
---

# The hand counts what is still unheard

## Overview

The raised hand on a lane orb is supposed to answer one question: **how much has this agent said
that you have not heard yet?** It currently gets that wrong in two opposite directions — it counts
things that are not speech at all, and it refuses to count things that genuinely are.

He reported both in the same breath, as two separate observations, and they have one answer.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim

> *"Whenever the agents either read my turns or do something, a hand quickly appears and disappears
> in this lane... whatever the agent is interacting with. I don't know why."*
>
> *"That is not the issue that I just described to you. What I described to you is while I was on
> the Kepler lane and I was saying stuff to it, as it synthesized and read my turns, I saw a hand
> quickly appear and disappear on the Magnus lane. I would assume because the Magnus lane is the
> default one, the primary one. Something is going on. So we need to fix both."*
>
> *"The hand number should decrease gradually as the [queued] turns get played."*
>
> (2026-08-27, voice-dictated; the turn log is gitignored and short-lived, so the quotes carry the
> weight and not the ids 3335–3337, 3343, 3344)

## Findings

**Measured first: the server is not doing it.** Twenty-two holds were created after 13:20 and not
one was flushed within three seconds of being raised, so no hand goes up and down on the server's
account. The flicker is drawn by the page.

### A — a cue is counted as somebody's clip, and it lands on the default lane

`_push_cue` sends the acknowledgement sounds through `_send_clip` with this header:

```python
{"type": "audio_header", "id": f"cue-{name}", "sample_rate": rate,
 "bytes": len(pcm), "text": "", "cue": name}
```

**There is no `lane` on it** — every other clip has carried one since spec `013` FR4. The page then
resolves the owner with a fallback:

```js
playingLane = header.lane || defaultLane;
```

so **every cue is attributed to the default lane and counts as one clip waiting there, for exactly
as long as the cue plays.** Cues fire when an agent reads a turn and on stage changes, which is
precisely *"whenever the agents either read my turns or do something"*. **His guess at the cause was
right**: it is because magnus is the default.

🎯 **A missing fact plus a helpful default is how a system invents information.** The fallback was
written for clips that predate lanes; a cue is not a clip that predates lanes, it is not a clip at
all. **The fix is not a better default — it is to stop asking the question**, because a cue has no
owner to be waiting for.

### B — the focused lane's hand is forced to zero rather than counting down

```js
const waiting = live ? 0 : ((m.waiting && m.waiting[name]) || 0);
```

⚠ **This was HIS OWN RULE, and it is quoted in the code from 2026-08-25:** *"the hand with the turns
should only show whenever the lane is not focused."* Under it the count vanishes the instant he
switches, however many clips are still queued.

**He has now asked for the opposite**, and the two requests are reconcilable rather than
contradictory: the old one objects to a hand that *asks for attention he is already giving*; the new
one wants a *countdown of what is still to play*. Both are satisfied by making the hand mean **still
unheard** in every case — on a lane he is not on that is the held backlog, and on the lane he is on
it is the queue draining to zero on its own.

## Goals

- **The hand counts replies that have not reached his ears, and nothing else.**
- **On the focused lane it counts DOWN as each clip plays**, reaching zero by itself.
- **A cue never appears in that count, on any lane.**

## Requirements

### Functional

- **FR1** — **A cue is excluded from the waiting count** and never becomes the playing lane.
- **FR2** — **The focused lane shows what is still queued or playing**, rather than a forced zero.
- **FR3** — **A lane he is not on still shows its held backlog**, unchanged.
- **FR4** — **The count reaches zero on its own** when the last clip finishes.

### Non-functional

- **NFR1** — **No new server state.** Everything needed is already on the wire: the header's `cue`
  marker, and the per-lane held count.

### Technical constraints

- **TC1** — **The cue is identified by its own `cue` field, not by a missing lane.** Giving cues a
  lane instead would make them count, which is the defect; and testing for "no lane" would also
  catch any genuinely lane-less clip, which should be attributed rather than dropped.
- **TC2** — **The server's count and the page's local queue must not double-count.** The server
  broadcasts zero for a lane at the instant it hands the queue over, and the remainder is local —
  that split is what makes the countdown work and must be preserved.

## Implementation Tasks

- [ ] Exclude cues when tallying the local queue and when setting the playing lane.
- [ ] Remove the forced zero for the focused lane.

## Acceptance Criteria

- [ ] **AC-1** `kittest-input:scripts/uisim.py` — **FR1.** With three lanes and a cue arriving while
      a non-default lane is live, no orb shows a raised hand. **Verify by mutation:** restore the
      `header.lane || defaultLane` fallback for cues and this goes red.
- [ ] **AC-2** `kittest-input:scripts/uisim.py` — **FR2/FR4.** On the focused lane with two clips
      queued, the hand reads two, then one, then nothing.
- [ ] **AC-3** `kittest-input:scripts/uisim.py` — **FR3.** A lane he is not on still shows its held
      count, which is the behaviour spec `013` shipped.
- [ ] **AC-4** `unit:` full suite — the orb row is painted by one function used on every path.
- [ ] **AC-5** `manual` — **FR1.** Talk to a non-default lane and watch the default lane's orb: no
      hand appears while cues fire. Manual because the flicker is sub-second and he found it by eye.

## Testing Approach

### Validation Steps

1. Write each assertion in `uisim.py`, then **break the fix and confirm it goes red** — a golden
   over a pure model cannot see a lying view, which is why that harness exists.
2. Run the full unit suite.

### Test Cases

| Situation | Expected |
|---|---|
| cue arrives, kepler live, magnus default | no hand anywhere |
| two clips queued on the live lane | hand reads 2, then 1, then gone |
| lane he is not on holds three | hand reads 3 |
| last clip finishes | hand gone without a switch |

## Out of Scope

- **Giving cues a lane.** TC1 — it would make them count, which is the bug.
- **Changing what the hand looks like.** Its form was settled in spec `014`; this is only about the
  number behind it.

## References

- `specs/013` FR4 — every clip carries its lane; the cue header is the one that never did.
- `specs/014` — the orb and its hand.
- `specs/022` — sent-versus-played, which is the distinction the countdown relies on.
