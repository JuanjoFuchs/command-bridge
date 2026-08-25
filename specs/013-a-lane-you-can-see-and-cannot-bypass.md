---
id: "013"
title: A lane you can see, and cannot bypass
status: in_progress
blocked_by: []
blocks: []
---

# A lane you can see, and cannot bypass

## Overview

Spec `012` shipped lanes and they routed correctly on the first live run. **Every defect below came
from that run** — the first time JJ drove three agents from one microphone, 2026-08-24 — and they
fall into two groups that are the same mistake from two sides: **the tool knows which lane
everything belongs to and does not enforce it, and the page knows and does not show it.**

The routing itself is not in question. Turns `2381` and `2382` were stamped `codex`, never reached
the `claude` watch, and the switch back on `2383` worked. What failed is everything around it.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification
> passes.

## What he said, verbatim (dictated 2026-08-24, first three-agent session)

> *"Hey Claude, I'm talking to you, but somehow Codex replied, and I don't want that."*

> *"Also, in the transcript, I don't know who's talking to me. And I want to know on the UI who I am
> addressing."* · *"In the transcript everybody shows as Claude."*

> *"I want the names of who I'm speaking to. So it should say you, then an arrow, and say Claude, or
> the other lane names. So, or to everyone, maybe, so that in the transcript I know who I am
> addressing."*

> *"Also, I see that the badges that we have created for each of the different lanes, they should
> somehow represent their statuses. I should know if an agent is listening or thinking or something
> else."* · *"I don't know what's the best way to do it. Maybe the different orbs, or maybe we make
> the badges bigger. That's something we need to iterate on."*

> *"I need a way to know that an agent is trying to talk to me so that I can divert my attention to
> that agent. I think we should use the badges for that."*

> *"then um I think we should force a lane now."* · *"Um a say without a lane should reject."*

> *"I think we need to change the red hint that line that we have that says Claude has read to here.
> I would like that to be like check marks like what WhatsApp has… once it's sent, it's one check.
> Once it arrives to the destination, it's two checks. And once the recipient has read the message,
> the two checks turn blue. I would like something similar, but respecting this design."*

> *"it seems to me we need to fix the next attribute because you've been stuck multiple times now
> waiting on watch."* · *"I've noticed that you get blocked in watch instead of responding."*

## Goals

- **An agent cannot speak into a conversation it is not part of** — by refusal, not by convention.
- **He can tell, from the page alone, who he is addressing and who is addressing him.**
- **An agent with something to say is visible while it waits**, so he can choose to turn to it.

## Requirements

### Functional

- **FR1** — **`say` REFUSES when no lane is given and more than one lane is registered.** The
  refusal names the caller's options. With a single lane the flag stays optional and nothing
  changes.
- **FR2** — **The unread-refusal counts only turns in the caller's lane.** A turn addressed to
  another lane can never refuse a `say`, because the caller's own `watch --lane` is structurally
  forbidden to deliver it.
- **FR3** — **Each transcript entry from him carries the lane it was addressed TO**, including the
  broadcast lane.
- **FR4** — **Each transcript entry from an agent carries the lane it came FROM**, so several
  agents are distinguishable rather than all rendering under the server's wake name.
- **FR5** — **Every lane's badge carries that lane's live state** — idle / listening / thinking /
  speaking — updating as the state changes.
- **FR6** — **A lane holding undelivered speech is marked as waiting, with how many clips wait.**
  The mark clears when the hold flushes.
- **FR7** — **Delivery state per turn is exposed as TWO states** — logged, and taken into an
  agent's context by its `watch` — rendered as a **single tick whose COLOUR carries the state**,
  and the read-boundary line is **removed** rather than reworded. → **ruled down from three by JJ
  on first sight of it; see below.**
- **FR8** — **`watch` reports how long he has been waiting for a reply**, so an agent folding turns
  in has an observable bound instead of an unbounded instruction.

### Non-functional

- **NFR1** — **A single-lane session behaves exactly as it does today.** Every FR above is inert
  with one lane registered: no new refusal, no new badge, and the receipt degrades to the states a
  one-agent session can produce.
- **NFR2** — **The page adds no request of its own.** Lane state, hold depth and receipt state all
  ride the status payload the client already receives.
- **NFR3** — **Nothing here may weaken an existing test.** Same rule as `012` AC-15: a test that
  moves is listed with its justification.

### Technical constraints

- **TC1** — **The server cannot know who is calling.** Identity is per-invocation, which is *why*
  FR1 is a refusal rather than an inference. Any design that guesses the caller's lane from
  connection state, timing or last-writer is wrong.
- **TC2** — 🔴 **FR1 is a breaking change for any agent already running.** A live session has agents
  mid-loop that pass no `--lane`; the refusal must therefore carry the remedy with the caller's
  likely lane named, the same way the unread refusal carries its `watch`.
- **TC3** — 🔴 **Delivered and read are THE SAME EVENT here, so there are only two states to draw.**
  A `watch` hands a turn over *and* advances the cursor in one act. **This constraint was written
  correctly and then not followed through** — the design invented a third state by redefining
  "read" as *answered*, which is not a delivery state at all. Ruled back to two on first sight; see
  *FR7 ruled down to TWO states* below.
- **TC4** — **The form of FR5/FR6 is explicitly unsettled.** JJ: *"Maybe the different orbs, or maybe
  we make the badges bigger. That's something we need to iterate on."* Ship the smallest legible
  thing and leave the layout easy to change; do not spend the session on it.
- **TC5** — **Android Chrome, foreground tab only.** Inherited from `012` TC5; nothing here can
  test it and nothing here may violate it.
- **TC6** — **`012`'s corpus guard must keep passing.** The replay over `sessions/*.jsonl` is the
  regression instrument for lane resolution and this spec must not move its verdicts.
- **TC7** — ⚠ **The corpus guards SKIP when no corpus is present**, which is every fresh checkout
  and CI. A green run is therefore not evidence they ran; the run that verifies AC-3 must point
  `VOICE_TUNNEL_CORPUS_DIR` at a real sessions directory and say so.

### 🎯 Sequencing

**Slice A = FR1, FR2, FR8** — the enforcement and loop fixes. No page changes, and they are what
stops the failure that ended the first live session.
**Slice B = FR3–FR7** — the page: attribution, badges, receipts.

## The FR8 ruling — the livelock, and why the rule was right

**Observed three times in one session, ending with JJ having to say *"respond"* and then
*"Ignore the next hint."*** The loop an agent is told to run is: wait before speaking, and if the
wait returns turns, fold them in and wait again. **It has no terminating condition while he is still
talking** — every wait returns turns, so the agent never reaches the `say`, and the more he says the
longer he goes unanswered. Under lanes it is worse, because a cross-lane turn can also trigger a
fold (FR2's bug, from the other side).

**The rule is not wrong and must not be removed.** It exists because an agent that speaks over a
turn it has not read is the failure this whole repo is built around. What is missing is a bound.

### 🔴 The first ruling was wrong, and the way it was wrong is the finding

**Drafted, then discarded before implementation: "`watch` reports whether it returned anything NEW,
and the loop terminates on the first wait that does not."** It does not bound anything. An agent
folding correctly advances its cursor every round, so every wait while he is still talking returns
turns it has genuinely never seen. `fresh` would read `true` on every iteration of exactly the
livelock it was meant to break, and `false` only in the case the loop already exits on — an empty
`quiet` return.

🎯 **The tell, worth carrying past this spec: a proposed signal that is TRUE in the failure case and
FALSE in the case that already works is not a bound, it is a restatement of `count > 0`.** Check a
new field against the failing trace before writing it, not against the story of the failure.

**Ruling: `watch` returns `unanswered_s` — how long the OLDEST turn he is still owed a reply to has
been waiting.** The server has both halves already: when each turn was logged, and when that lane
last spoke. The loop's exit becomes a number the agent can compare rather than a judgement it has to
make while under exactly the pressure that causes the failure.

**Why this one does bound it:** it rises monotonically while he talks and resets only when the agent
actually answers, so it distinguishes *"he added one more sentence"* from *"he has been waiting
ninety seconds"* — which is the distinction the folding agent needs and `count` cannot make. **The
bound belongs in the tool and not in the guide**, for the same reason `007` moved the drain
discipline into code: an instruction that must be remembered under time pressure is one that will be
forgotten under time pressure.

## Findings — measured while specifying

**FR1's mechanism is one predicate.** `off_lane = bool(lane) and lane != current and lane != broadcast`.
With `--lane` omitted, `lane` is `None`, `bool(None)` is false, and the clip is never off-lane —
so it plays regardless of whose conversation is live. **The hold built for `012` FR7 was never
reached**, which is why nine of `012`'s acceptance criteria could pass while the feature failed in
the first minute of real use: every test passed `--lane` explicitly.

🎯 **The transferable defect: a guard whose predicate is satisfied by the ABSENCE of the thing it
guards.** It is not a missing check — the check is there, correct, and tested. It simply never fires
for the caller who most needs it, and that caller is the one who did the least.

**FR2 is two specs disagreeing at a seam neither owns.** `007` refuses a `say` on anything unread in
the session. `012` filters delivery per lane and scoped the *waiter* guard to `(session, lane)`
(TC7) — **and left the refusal session-wide.** Measured: a turn stamped `lane: atlas` refused
Claude's `say`, naming a turn Claude's own `watch --lane claude` had correctly never returned. Each
spec is right on its own and the combination blocks an agent with turns it is forbidden to read.

### 🔴 A TEST WAS HOLDING THE BUG IN PLACE, and its name said the opposite

**`test_say_with_no_lane_still_works` passed throughout, and what it actually pinned was the
defect.** Its docstring reads *"Every existing single-agent caller passes no lane and must be
untouched"* — but the fixture it runs against registers **two** lanes, `claude` and `codex`. So the
behaviour it locked in was *a no-lane `say` being accepted while several agents share the session*,
which is precisely FR1's failure. It has been split: the compatibility claim now runs against a
genuinely one-lane registry, and the two-lane case asserts the refusal.

🎯 **The transferable part is not "the fixture was wrong", it is that the test's NAME and DOCSTRING
both described the case it was not running.** Nobody re-reads a passing test, and a green assertion
carrying a confident description of the wrong scenario is indistinguishable from coverage. **When a
guard is supposed to protect a boundary, assert the boundary in the test, not in its prose** — this
one never registered the single lane its name depended on.

**Findings — tests this spec changed.** NFR3 forbids weakening one, so every test that moved is
listed with its justification.

| Test | Change | Why it is not a weakening |
|---|---|---|
| `test_say_with_no_lane_still_works` | split in two; renamed `…_when_there_is_only_one_lane` and pinned to a one-lane registry, plus a new two-lane refusal test | **Strictly stronger.** It now tests the case its name always claimed AND the case it was silently permitting. Nothing it asserted before is lost — a one-lane no-lane `say` still returns 200 |
| `test_say_samples_unread_before_synthesis` | matches `unread = _unread_turns(` instead of `_unread_turns(state)` | A source-text assertion that broke on an added argument while the ordering it exists to protect was never in question. It pins WHERE the sample happens, which is the whole claim; matching the argument list added nothing and made it brittle |

### 🔴 FR7 ruled down to TWO states, on first sight, and the third was never a delivery state

**Shipped with three, corrected within the hour of him looking at it.** JJ, 2026-08-24: *"I think
we do only have two states, right? Not three like WhatsApp… as soon as one of my terms enters your
context window, that's read by you."* Then the ruling: *"we just keep one check mark, right? And
that check mark is gray and then blue. And I think we should remove the red to here line as well.
That's redundant now."*

**He is right, and TC3 had already said so without following it through.** That constraint records
that delivered and read are the same event in this tool — a `watch` hands a turn over *and* moves
the cursor. Rather than accept the consequence (two states), the design invented a third by
redefining "read" as **answered**. That is not a delivery state at all: **the answer is already the
next row in the transcript**, so the tick was re-reporting something the reader can see, one line
below, in words.

🎯 **The tell, and it generalises past receipts: a borrowed model brought its own state count.**
WhatsApp has three states because *sent to server*, *delivered to device* and *opened by a human*
are genuinely three events there. Here the middle two collapse, and the missing third was
manufactured to fill a shape that came from somewhere else. **Count the events the system actually
has before deciding how many indicators to draw.**

**What replaced it:** one tick, grey when no agent has taken the turn and blue once it is inside
one's context. The divider is deleted rather than reworded — it marked ONE boundary for ONE reader,
which stopped being coherent the moment three agents shared the log.

## Contract

`say` gains no flag; it gains a refusal.

```
voice-tunnel say --session dev "All green."
# → exit 1 when >1 lane is registered
{"error": "…", "code": "no_lane", "remedy": "voice-tunnel say --session dev --lane <yours> \"…\"",
 "lanes": ["claude", "codex", "atlas"], "live_lane": "claude"}
```

`watch` gains one returned field:

```
{"turns": [...], "cursor": 2405, "count": 2, "unanswered_s": 93.4}
```

- **`unanswered_s`** — seconds since the oldest turn addressed to this lane that the lane has not
  yet spoken after; `null` when he is owed nothing. It **rises while he keeps talking and resets
  when the lane actually says something**, which is what makes it a bound rather than a restatement
  of `count`.

New error code:

| `code` | When |
|---|---|
| `no_lane` | `say` with no `--lane` while more than one lane is registered (FR1) |

Status payload gains, per lane:

| field | meaning |
|---|---|
| `lane_states` | already present; now populated for every registered lane, not only the live one |
| `lane_waiting` | already present; now carries the hold DEPTH per lane rather than presence |
| `lane_receipts` | per lane, the highest turn id that lane has been delivered and the highest it has read |

## Implementation Tasks

### Slice A — enforcement and the loop

- [x] `say` refuses with `no_lane` when `--lane` is absent and more than one lane is registered;
      the remedy names the flag and the payload lists the registered lanes and the live one.
- [x] The unread check that refuses a `say` counts only turns in the caller's lane, with absent
      `lane` still meaning the default lane.
- [x] `watch` returns `unanswered_s` for the caller's lane.
- [x] `describe` updated in the same commit (convention 3): the `no_lane` code, the `unanswered_s`
      field, and the `say` refusal.

### Slice B — the page

- [x] His transcript entries render the lane they were addressed to, broadcast included.
- [x] Agent transcript entries render the lane they came from.
- [x] Every lane badge renders that lane's state, and updates on change.
- [x] A lane holding speech renders as waiting with its depth, and clears on flush.
- [x] The read-boundary line is replaced by a per-turn progressive receipt.

## Acceptance Criteria

### Slice A

- [x] **AC-1** `unit` — **FR1.** With two lanes registered, a `say` carrying no lane is refused with
      `code: no_lane`, nothing is synthesized, and the payload lists the registered lanes. With ONE
      lane registered the same call succeeds unchanged (NFR1).
- [x] **AC-2** `integration` — **FR1, TC2.** The refusal's `remedy` is a literal command that
      succeeds when run verbatim with a valid lane substituted.
- [x] **AC-3** `corpus` — **TC6, NFR1.** `012`'s replay over `sessions/*.jsonl` produces verdicts
      byte-identical to today's. ⚠ Run with `VOICE_TUNNEL_CORPUS_DIR` set and record that it ran —
      a skipped corpus guard is not a passing one (TC7). **Ran 2026-08-24 with
      `VOICE_TUNNEL_CORPUS_DIR` pointed at his real sessions directory: 7 replay guards
      executed, none skipped, all green.**
- [x] **AC-4** `integration` — **FR2.** A turn stamped for lane B does not refuse a `say` from lane
      A. Asserted by driving the real refusal path, not by calling the counter directly.
- [x] **AC-5** `integration` — **FR2, NFR1.** A turn with no `lane` field still refuses a `say` from
      the default lane, proving absent-means-default survives the per-lane scoping.
- [x] **AC-6** `unit` — **FR8.** `unanswered_s` is `null` when the lane owes nothing, RISES across
      two waits while turns keep arriving and no `say` happens, and RESETS to `null` after that
      lane speaks. ⚠ The rising assertion is the one that matters — a field that only distinguishes
      empty from non-empty is `count` under another name, which is the version of FR8 this spec
      discarded.
- [x] **AC-7** `unit` — **TC1.** No code path infers a caller's lane from connection state, timing
      or last writer. Asserted by the refusal firing identically for two callers indistinguishable
      to the server.
- [x] **AC-8** `unit` — `describe` carries `no_lane` and `unanswered_s`, and the existing
      describe-agrees-with-itself suite passes unchanged.
- [x] **AC-9** `unit` — **NFR3.** The full suite passes. No test deleted; every test touched is
      listed with its justification in *Findings — tests this spec changed*.

### Slice B

- [x] **AC-10** `harness:scripts/lanestrip.py` — **FR3, FR4.** With three lanes and turns from two
      of them, each of his entries renders its addressed-to lane and each agent entry renders its
      from lane. Asserted on rendered text, not on the model behind it.
- [x] **AC-11** `harness:scripts/lanestrip.py` — **FR5.** Each badge renders its own lane's state
      and changes when that lane's state changes, with the other lanes' badges unmoved.
- [x] **AC-12** `harness:scripts/lanestrip.py` — **FR6.** A lane with held clips renders as waiting
      with its depth, and the mark clears when the hold flushes. **Verified by mutation:** making
      the flush a no-op must turn this red.
- [x] **AC-13** `harness:scripts/lanestrip.py` — **FR7, TC3.** The receipt has exactly TWO
      states, both reachable, and the verdict depends on the delivery cursor ALONE — a guard
      that fails if `read_through` ever creeps back into it and reintroduces the third state.
      ⚠ Also verified BY EYE via `scripts/transcriptshot.py`, because the two defects he found
      after this first shipped were both visible and neither was assertable.
- [x] **AC-14** `harness:scripts/layout.py` — **NFR2.** The layout sweep passes at every viewport
      in both picker layouts, solo and three-lane, with the strip's own guard still failing a case
      that measured a hidden strip.
- [ ] **AC-15** `manual` — **FR3–FR7 together.** Three agents, one session: he can tell from the
      page who he is addressing, who spoke, which agent is waiting, and whether his last turn was
      read. **Irreducibly manual** — it is a judgement about legibility, which no harness can make.

## Testing Approach

### Validation steps

1. `venv/Scripts/python -m pytest tests/ -q` — the full suite, no regressions.
2. `VOICE_TUNNEL_CORPUS_DIR=<sessions> venv/Scripts/python -m pytest tests/test_lane_corpus.py` —
   the replay guards must RUN, not skip (TC7).
3. Slice B harnesses start no server and are safe during a live session; confirm with their own
   `ServerWatch` assertions.

### Test cases

| Input (lanes `{claude, codex}`, live `claude`) | Expected |
|---|---|
| `say` with no `--lane` | refused, `code: no_lane`, nothing synthesized |
| `say --lane claude` | plays |
| `say --lane codex` | held, and delivered when codex becomes live |
| `say` with no `--lane`, only `claude` registered | plays — unchanged from today |
| unread turn on `codex`, `say --lane claude` | plays; the codex turn does not refuse it |
| unread turn with no `lane`, `say --lane claude` | refused — absent means the default lane |
| turns arriving, lane has not spoken | `unanswered_s` rises across successive waits |
| lane says something | next wait reports `unanswered_s: null` |

## Out of Scope

- **Fixing the closed-channel clip loss.** Still tracked in Voice Tunnel; the lane hold is a
  separate store and this spec does not touch the broken one.
- **Choosing the final visual language for badges and receipts.** TC4 — he asked for iteration, so
  this ships the smallest legible version and no more.
- **Per-lane voices.** `say --voice` exists; pinning one per lane is a separate, cheaper question.
- **Inferring a caller's lane.** TC1 — the refusal exists precisely because this is out of scope.
- **Raising the recognizer's exact-match rate on a name.** Measured and used by `012`; improving it
  is different work.

## References

- `specs/012` — lanes; this spec fixes what its first live run exposed.
- `specs/007` — the refusal machinery; FR2 rescopes its unread check and FR8 bounds its loop.
- `specs/011` — the turn text carried in a refusal, which FR8's `fresh` complements.
- Voice Tunnel — the roadmap rows these came from, with his verbatim words.
- Voice Tunnel Guide — the standing rules; FR8 removes the one that could not terminate.
