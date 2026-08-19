---
id: "011"
title: The agent's context window is a budget the CLI spends
status: in_progress
blocked_by: []
blocks: []
---

# The agent's context window is a budget the CLI spends

> **Refined from the strategist's metaspec by the repo implementer, 2026-08-19.** The metaspec's
> framing, goals and out-of-scope rulings are preserved verbatim in intent. What is added: the
> measured cost of every payload this spec touches, the mechanical root cause of FR2 (three routes,
> one of them reproduced), a correction to FR3's premise that the measurement falsified, and
> acceptance criteria that are **all machine-checked** — see the completion rule.

## Overview

Every other spec in this repo treats the human as the scarce resource — his attention, his hearing,
his patience. **This one treats the agent's context window that way**, because it is the resource the
tunnel exists to protect and the CLI currently spends it carelessly.

JJ's framing, which is the reason this spec exists at all:

> **(verbatim, dictated 2026-08-14)** *"I have noticed now that I am using my voice tunnel, I am
> saving on context window a lot and I am getting more value out of Opus V by talking than by reading
> what it writes… And it's been a very long back and forth. And my context window is not growing."*

**A CLI that restates its whole contract on every call is spending exactly what talking was supposed
to save.** The tool is not the wrong shape — it is verbose in ways that buy nothing on the second and
later occurrence.

He raised it directly after watching a refusal land:

> **(verbatim, dictated 2026-08-19)** *"What about the ergonomics of the CLI for you? Because I did
> notice that in the rejections, I think my terms were duplicated. And I don't know if JSON is the
> right output. You tell me."*

> **Completion rule:** This spec is not complete until every acceptance criterion is verified by the
> method named on it. Build-only verification is insufficient. The agent must iterate until
> verification passes.

> **🔴 No criterion in this spec is verified by a human looking.** JJ's ruling, 2026-08-19: *"don't
> add any manual acceptance criterion. The whole goal of this is that all my intent and my judgment
> gets encoded into the spec so that the agent can run it end to end with full verification without a
> human in the loop."* Where a property genuinely cannot be machine-checked in this repo, it is named
> in What cannot be verified here as an open fact — **not written as an `manual` criterion that
> nobody will run.**

## Measured ground truth (2026-08-19)

Every number below was measured against the live `dev` session log (1,365 turns, 1,261 addressed) and
the working tree, in process, **with no server started or contacted.**

### The population these payloads carry

| | chars |
|---|---|
| median addressed turn, `text` only | **74** |
| p90 addressed turn, `text` only | 258 |
| the turn the metaspec measured | 702 |
| median addressed turn as a full JSON object | 244 |

**A turn object costs ~170 characters more than its text** — `session`, `t_start`, `t_end`,
`addressed`, `reason`, `final`, `wall`. That is 170 chars of overhead per turn, and
`UNREAD_ON_SAY_MAX` is 20. See Out of Scope for why this spec does not take it.

### What a refused batch costs today (FR1)

Four `say --now` clips, all refused, one unread turn:

| turn size | one refusal | **four refusals** |
|---|---|---|
| median (74 chars of speech) | 934 | **3,736** |
| the measured one (702 chars) | 1,567 | **6,268** |

Field-by-field, one refusal on the 702-char turn: `unread` **875** · `next` **287** · `error` 129 ·
`remedy` 47 · `code` 14 · the four scalars 14.

**Two fields are 74% of it, and they are the two this spec addresses** — `unread` (FR1) and `next`
(FR3). Nothing else is worth touching.

### 🔴 FR3's premise is wrong for the commonest call, and right everywhere else

The metaspec says *"the same multi-sentence explanation of the loop currently arrives on every
`watch`, every `say`, and every refusal."* **Measured, that is false for a quiet `watch`** — the
steady-state branch emits 51 characters (``run `voice-tunnel watch --session dev --since 1354` ``)
inside a 278-character payload. There is nothing to win there.

It is true, and expensive, on every other branch:

| `_next_action` branch | chars | repeats while… |
|---|---|---|
| turns delivered, verbose off | **406** | every turn of every conversation |
| channel closed | 379 | he keeps it closed |
| turns delivered, verbose on | 361 | every turn of every conversation |
| nobody connected | 312 | the page stays away |
| `say` refused (written by `cmd_say`) | **287** | every clip of a refused batch |
| `say --now` succeeded | **226** | every clip of every answer |
| muted | 172 | he stays muted |
| orb off | 119 | the orb stays off |
| `say` clean | 125 | every clip of every answer |
| **quiet watch** | **51** | — already cheap |

**So FR3's target is the branch that repeats, not the call that repeats.** A thirty-turn conversation
pays the 406-character turns-branch thirty times, and a three-clip answer pays the 226-character
`--now` branch three times, for guidance that has not changed since the previous call of the same
command.

### The three-clip answer (FR4's second scenario)

Three `say --now` payloads: **996 chars, of which `next` is 678 (68%).** The three `next` strings are
byte-identical and arrive within seconds of each other.

## Goals

- Cut what the CLI spends on the agent's context **without removing anything the agent actually
  needs**.
- Close a cursor trap that can make a refusal unrecoverable for an agent that trusts its own state.
- Keep JSON, and keep `next`. Both earn their place — see Out of Scope.
- **Every guarantee in this spec is enforced by a test, not by an instruction.**

## Requirements

### FR1 — A refused batch carries the unread turn ONCE, not once per clip

Measured 2026-08-19: four `say --now` clips were fired back to back, all four were refused, and
**each refusal carried the full text of the same ~700-character turn**. Four copies of one turn, for
one refusal event.

Carrying the turn text **once** is genuinely valuable — it saves the agent a round trip to find out
what it missed, and the agent frequently has everything it needs to re-compose without another call.
Carrying it N times is pure duplication that scales with the size of the answer being attempted,
which is exactly backwards: **the longer the thought the agent was trying to deliver, the more it is
charged for being interrupted.**

**The refusal identity is `(since, last_turn_id)`** — where the agent has read to, and the head of the
log. A refusal whose identity matches the previous one is a *repeat* and omits the turn text. Any new
turn, or any successful read, changes the identity and the next refusal is full again.

**The text is never unrecoverable**, which is what makes the omission safe: `remedy` still delivers
every omitted turn, and the ids are still listed.

### FR2 — 🔴 A refusal's remedy must be reachable from the cursor the agent is holding

**This is the defect, not the duplication.** The refusal named `--since 1353` while the agent was
holding cursor `1354`. Running `watch --since 1354` returned `quiet` and **did not clear the
refusal** — the turn stayed unread, the next `say` was refused identically, and an agent that trusted
its own cursor over the remedy string would loop forever.

#### Root cause, measured

The trap is that two cursors track the same log and **only one of them gates the refusal**. The
server's `consumed_cursor` decides whether `say` refuses; the agent's `--since` decides what `watch`
delivers. `watch` advances the server's cursor **only when it delivers turns** — the CLI posts
`/consumed` inside `if turns:`. So `watch --since <ahead of consumed_cursor>` returns `quiet`,
consumes nothing, and leaves the refusal exactly where it was.

Three routes put the agent ahead. All three end in the same non-recovering state:

| # | route | evidence |
|---|---|---|
| **a** | A `watch` that delivers **zero** turns still returns an **advanced** cursor: unaddressed turns are consumed rather than deferred, and the timeout path returns the advanced value. No `/consumed` is posted, because none were delivered. | **Reproduced.** A log of one addressed turn and two unaddressed ones: `watch --since 0` returns `[]` with cursor `2`, while the server stays at `0` |
| **b** | The `/consumed` post is best-effort and its failure is swallowed, so a delivered batch can leave the server cursor behind with **nothing anywhere reporting it** | code: the post sits under a bare `except Exception` in the watch loop |
| **c** | An agent deriving its cursor from `last_turn_id` rather than the **lower** of the two cursors — a rule the tool states in prose, in two places, and does not enforce | code: the rule appears in `describe` and in the recovery text, as an instruction to the agent |

**The fix has to be in the tool, and it has to be at the cursor rather than at the message.** Route
(c) is the tool asking the agent to do arithmetic it can do itself; routes (a) and (b) produce the
divergence with the agent behaving perfectly. A remedy string cannot close (a) or (b), because the
agent never sees a reason to distrust its own cursor.

**→ `watch` resolves the cursor it will actually resume from as the LOWER of the `--since` it was
given and the server's `consumed_cursor`, and says so when they differ.** This is the rule the manual
already states, moved from prose into the command. After it, **every** `watch` on a session with an
unread turn delivers that turn, whatever cursor the caller believed — so the refusal is escapable by
any route, not only by copying the remedy verbatim.

**The clamp can only ever deliver MORE, never fewer, turns**, which is what makes it safe to ship
while a live session is running (TC1). Its worst case is a re-read of a turn whose `/consumed` post
was lost — and this repo has already ruled on that exact trade: double-delivery is a re-read,
consuming too eagerly is words silently dropped.

### FR3 — Procedural guidance is emitted when it changes something, not on every call

`next` is load-bearing and must survive: on 2026-08-19 it named the exact command to run at a moment
the agent's own reasoning was wrong, and following it was the only thing that worked. **This
requirement is about repetition, not removal.**

**Every `next` — long form or short — still carries a literal, runnable command with the session and
the cursor already substituted.** That property is the reason the field works, and it is not what is
being cut. What is cut is the *rationale prose* on a call where the branch has not changed since the
previous call of the same command in the same session.

- **Branch changed, or first call of that command in this session → the full guidance**, unchanged.
- **Same branch as this command's previous call → the command alone**, plus a short tail saying the
  reasoning is unchanged and where to get it.

**Keyed per command, not globally.** A conversation alternates `watch` → `say` → `watch` → `say`, so a
single global "last branch" would see a change on every call and suppress nothing. Each command
compares against its own previous branch.

### FR4 — Say what this saves, measured

State the before and after in characters for a realistic exchange — a three-clip answer, a refused
batch, and a quiet `watch` — so the claim that this buys context back is a number rather than an
intention. If the saving turns out to be trivial, that is a finding worth reporting rather than a
spec worth shipping.

**The finding is already partly in: the quiet `watch` is trivial and is reported as such** (278 chars,
51 of them `next`). This spec does not try to shrink it, and the criterion for it is that it **must
not grow**.

**The measurement ships as a runnable gate, not as a number in prose.** A number written into a
document rots the first time the payloads move; a script that recomputes it and exits non-zero
cannot. It asserts the *property* — a repeat costs materially less than a first — rather than a
historical constant, so it stays true after the next payload change.

### Non-Functional Requirements

- **NFR1**: No new round trip. FR1 and FR3 use state the server or the CLI already holds; FR2 uses
  the `/status` response `cmd_watch` already fetches before it starts waiting.
- **NFR2**: No behaviour change when the server is unreachable. FR2's clamp needs a `consumed_cursor`
  from `/status`; without one, `--since` is used exactly as given.

### Technical Constraints

- **TC1**: 🔴 **A live voice session is running on session `dev` and must not be disturbed.** No
  criterion may start, stop or restart a server, and none may run a harness that starts one. The CLI
  is loaded from the working tree on every invocation, so **FR2's and FR3's changes reach the live
  agent the moment the file is saved**; the server keeps the code it started with, so FR1's does not
  until it restarts. Both facts are stated in What cannot be verified here rather than papered
  over.
- **TC2**: **FR2's clamp must be safe to arrive mid-session**, because it will. It is, by
  construction: `min()` can only lower the resume point, and a lower resume point can only deliver
  turns the server believes are unread. It cannot skip a turn and cannot suppress one.
- **TC3**: **`describe` is the contract** (convention 3). Any field this spec adds to a payload is
  documented in `describe` in the same change, and the drift is asserted rather than trusted.
- **TC4**: **The refusal's guarantees from spec `007` are load-bearing and must not regress**: nothing
  is synthesised, nothing is queued, the read cursor does not move, no flag disables the check, and
  `--now` is refused on the same condition. FR1 changes what a *repeat* refusal carries, never
  whether it refuses.
- **TC5**: **FR3's persisted branch state shares a file with the watch backoff.** That file is
  currently written whole, so a naive write drops `empty_streak` and silently resets the backoff
  ladder. The write must preserve keys it does not own, and that must be asserted.
- **TC6**: CI lints `voice_tunnel/`, `tests/` and `scripts/` with ruff and runs the suite on three
  operating systems with **core dependencies only** — no piper, no model, no microphone. Anything
  added here runs on that floor.

## Implementation Tasks

- [x] Resolve `watch`'s effective resume cursor as the lower of `--since` and the server's
      `consumed_cursor`, and publish both numbers when they differ.
- [x] Give a repeated refusal — same `(since, last_turn_id)` as the one before it — the ids of the
      unread turns without their text, and say in the payload that the text was already delivered.
- [x] Emit `next`'s rationale only when the branch changed since the same command's previous call in
      the same session; always emit the literal command.
- [x] Persist the per-command branch beside the watch backoff without clobbering it.
- [x] Document every new field in `describe` in the same change.
- [x] Add a measurement script that recomputes the saving and exits non-zero below its floors, and
      wire it into CI.

## Acceptance Criteria

**Validation methods used here:** `unit` (pure logic or an in-process handler), `integration` (against
a fixture the test owns — a temp session directory, a real subprocess). **No criterion is `manual`.**

### FR1 — the refused batch carries the turn once

- [ ] **AC1** (`unit`): the **first** refusal for an unread set carries the unread turns **with their
      `text`**, exactly as today.
- [ ] **AC2** (`unit`): a **second** refusal with the same `(since, last_turn_id)` carries the same
      turn **ids** and **no `text`**, and the payload states that the text was omitted.
- [ ] **AC3** (`unit`, **negative control**): when a **new turn arrives** between two refusals, the
      second refusal carries **full text again**. *Without this, AC2 passes on an implementation that
      simply stopped sending text.*
- [ ] **AC4** (`unit`, **negative control**): after the unread set is **read** (the cursor advances),
      the next `say` is **not refused at all** — so the repeat state cannot be what makes it pass.
- [ ] **AC5** (`unit`): a repeat refusal still carries `error`, `code`, `remedy`, `since`,
      `last_turn_id` and `unread_count` with the same values as the first. *The recovery path is
      unchanged; only the text is dropped.*
- [ ] **AC6** (`unit`, TC4): on both a first and a repeat refusal, `tts.synthesize` is **never
      called**, nothing enters the undelivered queue, and the read cursor does **not** move.
- [ ] **AC7** (`unit`): a repeat refusal is **measurably smaller** than the first for a turn of at
      least median length — asserted on serialized character count, not on field presence.

### FR2 — the cursor trap is closed

- [ ] **AC8** (`integration`, **the defect**): with the server's `consumed_cursor` **behind** an
      unread addressed turn and the caller passing a `--since` **ahead** of it, `watch` **delivers
      that turn** and the read cursor advances past it. *This is the exact state the loop was
      observed in; it is asserted end to end through the watch path against a temp session, not
      inferred from the clamp helper.*
- [ ] **AC9** (`integration`, **the loop itself**): running the observed sequence — refusal, then
      `watch` at the agent's own (higher) cursor, then `say` — ends in `say` **succeeding**. *The
      requirement is "the agent can leave this state", and only a test that re-enters it can show
      that.*
- [ ] **AC10** (`unit`): when `--since` is **at or below** `consumed_cursor`, the resume point is
      **unchanged** and no clamp fields appear. *Negative control: the clamp must not fire on the
      normal path.*
- [ ] **AC11** (`unit`): `--since -1` is never clamped upward or downward — "from the beginning"
      survives.
- [ ] **AC12** (`unit`, NFR2): when `/status` is unreachable or reports no `consumed_cursor`,
      `--since` is used exactly as given and nothing new appears in the payload.
- [ ] **AC13** (`unit`): when the clamp fires, the payload publishes **both** the cursor requested and
      the cursor resumed from, under names that cannot be confused for one another. *The correction is
      visible rather than magic — an agent whose cursor was wrong can see that it was.*
- [ ] **AC14** (`unit`, **route (a)**): the reproduction is pinned — a log whose only new turns are
      **unaddressed** advances the caller's cursor while delivering nothing, and the resulting
      divergence is recovered by the clamp. *This is the route that needs no mistake by anyone, so it
      is the one that must not regress.*

### FR3 — guidance when it changes something

- [ ] **AC15** (`unit`): the **first** call of a command in a session emits the **full** guidance for
      its branch.
- [ ] **AC16** (`unit`): a **second** call of the same command taking the **same** branch emits a
      `next` that is **materially shorter** and is marked as a repeat — asserted on character count
      and on the marker.
- [ ] **AC17** (`unit`, **negative control**): a second call taking a **different** branch emits the
      **full** guidance. *Without this, AC16 passes on an implementation that shortened everything
      after the first call.*
- [ ] **AC18** (`unit`, **the property that must not be lost**): **every** `next` this tool emits —
      long form and short form, across every branch of every command that has one — contains a
      literal command with the session substituted, and no unresolved placeholder. *Asserted by
      sweeping the branches, not by checking one.*
- [ ] **AC19** (`unit`): the branches are tracked **per command**, so an alternating `watch` → `say` →
      `watch` sequence suppresses the repeat on each command's second call. *This is the case a global
      key gets wrong.*
- [ ] **AC20** (`unit`, TC5): writing the branch state **preserves `empty_streak`**, and writing
      `empty_streak` preserves the branch state.
- [ ] **AC21** (`unit`): the short form **names how to recover the full reasoning**, so an agent that
      only ever sees short forms is not stranded.

### FR4 — the saving is a number, and the number is a gate

- [ ] **AC22** (`integration`): a measurement command reports, for each of the three named
      exchanges — a three-clip answer, a refused batch, a quiet `watch` — the characters spent before
      and after, and the reduction.
- [ ] **AC23** (`integration`, **the gate**): that command **exits non-zero** when the refused-batch
      reduction falls below **40%** or the three-clip reduction below **25%**. *Floors chosen from the
      measured numbers above with headroom, so the gate fails on a regression rather than on noise.*
- [ ] **AC24** (`integration`, **proven to fire**): the gate is shown to **fail** on a payload that
      does not save — not merely to pass on one that does. *A gate whose failure has never been
      observed is indistinguishable from one that cannot fail.*
- [ ] **AC25** (`unit`, the honest scenario): the quiet `watch` payload **does not grow**. *The
      finding is that it is already cheap; the criterion protects that rather than inventing a
      saving.*
- [ ] **AC26** (`integration`): the measurement runs with **no server started and none contacted**,
      and on the CI floor — core dependencies only, no model, no microphone. *Asserted, because a gate
      that cannot run in CI is a gate nobody runs.*

### Contract and suite

- [ ] **AC27** (`unit`, TC3): every field this spec adds to a payload appears in `describe`, and the
      existing describe-agrees-with-itself check still passes.
- [ ] **AC28** (`integration`): the full suite passes with **no test weakened, skipped or deleted**,
      and **no `voice-tunnel` server started or contacted** (TC1). The pre-change test count is
      recorded so a shrinking suite is visible.
- [ ] **AC29** (`integration`): `ruff check voice_tunnel/ tests/ scripts/` is clean (TC6).

## Testing Approach

### Validation steps

1. `venv/Scripts/python.exe -m pytest tests/ -q` — the whole suite, no server.
2. `venv/Scripts/python.exe -m ruff check voice_tunnel/ tests/ scripts/`.
3. The measurement command, in report mode and in gate mode.
4. `bin/voice-tunnel describe` — confirm every new field is documented.

### Test cases

**FR1 — refusal repeats**

| Sequence | Second refusal carries |
|---|---|
| refuse, refuse (nothing changed) | ids only, text omitted |
| refuse, new turn arrives, refuse | full text again |
| refuse, turn is read, `say` | no refusal at all |

**FR2 — cursor resolution**

| `--since` | server `consumed_cursor` | resumes from | clamp reported |
|---|---|---|---|
| 1354 | 1353 | **1353** | yes |
| 1353 | 1353 | 1353 | no |
| 1350 | 1353 | 1350 | no |
| −1 | 1353 | −1 | no |
| 1354 | (server unreachable) | 1354 | no |

**FR3 — guidance**

| Call | Branch | `next` |
|---|---|---|
| `watch` #1 | turns | full |
| `say` #1 | `--now` | full |
| `watch` #2 | turns | short + marker |
| `say` #2 | `--now` | short + marker |
| `watch` #3 | muted | full (branch changed) |
| `watch` #4 | muted | short + marker |

## Out of Scope

- **Changing the output format.** JSON stays. The agent's verdict when asked directly was that JSON
  is right — unambiguous, parseable, and the structured fields are what make the contract
  enforceable. **The cost is volume, not format**, and a human-readable mode would be a different
  feature for a different reader (`--human` already exists).
- **Removing `next`, `remedy`, or the invariants from `describe`.** `describe` is the contract and is
  read deliberately, once. This spec governs what rides along on *every* call.
- **The human-facing ergonomics of multi-clip answers.** Being tested live over the coming days and
  governed by the guide, not by code — see `Voice Tunnel Guide.md` rule 3,
  which now caps an answer at three clips because JJ forgets the first one otherwise.
- **Trimming the per-turn metadata inside `unread`** — `session`, `t_start`, `t_end`, `reason`,
  `final`, `wall`, measured at **~170 characters per turn against a median 74-character utterance**,
  and up to 20 turns per refusal. It is a real saving and it is deliberately not taken here: it
  changes the shape of the turn object an agent reads, which is a different contract from how many
  times that object is sent. Recorded with its number so the decision is a decision rather than an
  oversight.
- **Changing how `/consumed` is posted, or making its failure loud.** Route (b) above is a genuine
  hole and FR2's clamp makes it recoverable rather than fatal. Making the failure visible is a
  separate change to the reporting contract.
- **The size of `describe` itself.** It is read once, deliberately.

## What cannot be verified here

Stated as facts rather than written as `manual` criteria nobody will run, per the ruling at the top.

- **Nothing is verified against a running server.** A live session is on `dev` (TC1) and may not be
  disturbed, so every server-side behaviour is verified through in-process handlers and against
  temp-directory fixtures. What that leaves open is the interaction of the real aiohttp request path
  with these payloads — the shapes are asserted, the transport is not.
- **FR1 does not reach the live session until the server restarts**, because a running server keeps
  the code it started with. FR2 and FR3 live in the CLI and reach it on the next invocation.
- **The subjective claim — that the saving is worth having in a real conversation — is not
  measurable here.** What is measurable is the character count, and that is what AC22–AC25 assert.
  Whether it changes how long a session can run is a live observation, and this spec deliberately
  does not pretend otherwise.

## References

- `Voice Tunnel.md` — the roadmap and the bandwidth framing this spec
  serves
- `reflections\bandwidth.md` — JJ's own thinking on why context spend is
  the point
- `Voice Tunnel Guide.md` — the operating rules the CLI's guidance
  duplicates
- `specs/005-one-wait-gated-on-speech.md` — the single waiting command whose cursor this spec makes
  authoritative
- `specs/007-the-tool-enforces-the-loop.md` — the refusal whose payload FR1 and FR2 are about, and
  whose "Verified state" section already recorded a sibling of the FR2 deadlock in the remedy string
