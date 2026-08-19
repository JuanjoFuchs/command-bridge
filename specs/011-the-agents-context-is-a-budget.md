---
id: "011"
title: The agent's context window is a budget the CLI spends
status: complete
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

Three routes put the agent ahead of the server. **They do not all reach the deadlock, and the
difference is worth stating precisely** — see the correction below the table.

| # | route | reaches the deadlock? | evidence |
|---|---|---|---|
| **a** | A `watch` that delivers **zero** turns still returns an **advanced** cursor: unaddressed turns are consumed rather than deferred, and the timeout path returns the advanced value. No `/consumed` is posted, because none were delivered. | **No — divergence only** | **Reproduced.** A log of one addressed turn and two unaddressed ones: `watch --since 0` returns `[]` with cursor `2`, while the server stays at `0` |
| **b** | The `/consumed` post is best-effort and its failure is swallowed, so a delivered batch can leave the server cursor behind with **nothing anywhere reporting it** | **Yes** | code: the post sits under a bare `except Exception` in the watch loop |
| **c** | An agent deriving its cursor from `last_turn_id` rather than the **lower** of the two cursors — a rule the tool states in prose, in two places, and does not enforce | **Yes** | code: the rule appears in `describe` and in the recovery text, as an instruction to the agent |

#### 🔴 Correction — route (a) supplies the divergence, not the deadlock

**Written into this spec as "all three end in the same non-recovering state", and that is wrong.**
Caught by the implementing slice while building AC14, and re-derived independently before being
accepted here.

`turns_since` takes its new cursor from the **full** slice before the addressed filter, so the cursor
advances past a turn without delivering it **only when that turn is unaddressed**. Therefore after
route (a) every *addressed* turn still has an id **above** the caller's advanced cursor, and the next
`watch` delivers it and posts `/consumed` normally.

**Re-derived on a four-turn log** (addressed, unaddressed, addressed, unaddressed), sweeping every
start cursor: no addressed turn is ever skipped. The deadlock needs an addressed turn at an id **at
or below** the caller's cursor, which only (b) and (c) can produce — and that is exactly the observed
live shape: server `1353`, caller `1354`, unread turn id `1354`, so `id > cursor` is false for the
very turn being complained about.

**Route (a) still matters and stays in the spec**, because it is the one that needs *nobody to make a
mistake* — it manufactures the cursor divergence that (b) and (c) then convert into a deadlock, and
it is the reason an agent's cursor cannot be trusted in the first place.

**The fix has to be in the tool, and it has to be at the cursor rather than at the message.** Route
(c) is the tool asking the agent to do arithmetic it can do itself; routes (a) and (b) produce the
divergence with the agent behaving perfectly. A remedy string cannot close (b), because the agent
never sees a reason to distrust its own cursor.

**→ `watch` resolves the cursor it will actually resume from as the LOWER of the `--since` it was
given and the server's `consumed_cursor`, and says so when they differ.** This is the rule the manual
already states, moved from prose into the command. After it, **every** `watch` on a session with an
unread turn delivers that turn, whatever cursor the caller believed — so the refusal is escapable by
any route, not only by copying the remedy verbatim.

**The clamp can only ever deliver MORE, never fewer, turns**, which is what makes it safe to ship
while a live session is running (TC1). Its worst case is a re-read of a turn whose `/consumed` post
was lost — and this repo has already ruled on that exact trade: double-delivery is a re-read,
consuming too eagerly is words silently dropped.

#### 🔴 The clamp is bounded downward, and a NEGATIVE read position is the bound

**Found by the implementing slice and ruled on here, because it is a hole in this spec's own
subject.** `min()` is unbounded downward, so a server reporting `consumed_cursor: -1` turns
`watch --since 1354` into a resume from the head of the log — **every turn in the session, 1,391 of
them on `dev` at the time of writing**, with no equivalent of the refusal's `UNREAD_ON_SAY_MAX` cap.
A spec about the agent's context budget cannot ship a silent full-log replay as its fix.

`-1` is not an ordinary cursor. It is the sentinel `read_consumed_cursor` returns for **two different
facts it cannot tell apart** — "nothing was ever read" and "the cursor file was missing or
unreadable". The second is a lost-state case where the server's belief is wrong and the caller's is
right, and it is the only value that can convert a resume into a full replay.

**→ RULED: the clamp does not fire when the server reports a NEGATIVE `consumed_cursor` and the
caller passed a non-negative `--since`.** Every other clamp proceeds unchanged.

**The rule is written as the class, not the literal, and that was the implementer's call rather than
this spec's original wording.** It proposed `consumed < 0` over `consumed == -1` on the grounds that
`-1` is only the spelling `read_consumed_cursor` happens to produce today, and a future sentinel
should not be able to walk past a guard aimed at a number. **Accepted, and the spec follows the code
rather than the other way round**, because the predicate the bound really tests is *"does the server
have a valid read position"* — and since turn ids start at `0`, **negativity is precisely how "no" is
encoded.** The two spellings are identical in practice today.

⚠️ **The boundary that makes this safe rather than merely broader: `consumed_cursor: 0` still
clamps.** Zero is a real read position — the first turn, read — and a guard written `<= 0` would have
silently swallowed it and left a genuine unread turn undeliverable. Verified directly by the team
lead: `0` clamps to `0`; `-1`, `-2`, `-5` and `-999` all decline. *A widened guard has to be checked
at the edge it was widened past, not only at the value that prompted it.*

**`--since -1` never reaches this guard at all.** The pre-existing `since < 0` early return owns it,
so AC31 is **structurally** protected rather than incidentally passing — "from the beginning" cannot
be broken by any future change to the bound.

**What this trades away, stated rather than discovered:** in a genuinely fresh session the deadlock
survives, and the escape is the remedy string, which already names `--since -1`. That is acceptable
because the state is one where the agent's cursor came from somewhere the server never agreed with —
and because the alternative is the tool silently spending the whole context budget this spec exists
to protect. **The underlying defect is the overloaded sentinel**, and it is named in Out of Scope
rather than fixed here.

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

- [x] **AC1** (`unit`): the **first** refusal for an unread set carries the unread turns **with their
      `text`**, exactly as today.
- [x] **AC2** (`unit`): a **second** refusal with the same `(since, last_turn_id)` carries the same
      turn **ids** and **no `text`**, and the payload states that the text was omitted.
- [x] **AC3** (`unit`, **negative control**): when a **new turn arrives** between two refusals, the
      second refusal carries **full text again**. *Without this, AC2 passes on an implementation that
      simply stopped sending text.*
- [x] **AC4** (`unit`, **negative control**): after the unread set is **read** (the cursor advances),
      the next `say` is **not refused at all** — so the repeat state cannot be what makes it pass.
- [x] **AC5** (`unit`): a repeat refusal still carries `code`, `remedy`, `since`, `last_turn_id` and
      `unread_count` with **the same values as the first**, and its `error` **begins with the first
      refusal's `error` verbatim** before adding where the text went. *The recovery path is unchanged;
      only the text is dropped.*
      **Corrected 2026-08-19, mid-implementation.** This criterion originally listed `error` among the
      identical fields, which contradicts FR1's own output contract two sections above — that contract
      requires the repeat's `error` to say the text was delivered on the first refusal. Both could not
      hold, and the implementing slice reported the contradiction rather than picking a side. The
      prefix rule settles it: a caller substring-matching the refusal sentence still matches, and the
      new information is additive rather than a replacement.
- [x] **AC6** (`unit`, TC4): on both a first and a repeat refusal, `tts.synthesize` is **never
      called**, nothing enters the undelivered queue, and the read cursor does **not** move.
- [x] **AC7** (`unit`): a repeat refusal is **measurably smaller** than the first for a turn of at
      least median length — asserted on serialized character count, not on field presence.

### FR2 — the cursor trap is closed

- [x] **AC8** (`integration`, **the defect**): with the server's `consumed_cursor` **behind** an
      unread addressed turn and the caller passing a `--since` **ahead** of it, `watch` **delivers
      that turn** and the read cursor advances past it. *This is the exact state the loop was
      observed in; it is asserted end to end through the watch path against a temp session, not
      inferred from the clamp helper.*
- [x] **AC9** (`integration`, **the loop itself**): running the observed sequence — refusal, then
      `watch` at the agent's own (higher) cursor, then `say` — ends in `say` **succeeding**. *The
      requirement is "the agent can leave this state", and only a test that re-enters it can show
      that.*
- [x] **AC10** (`unit`): when `--since` is **at or below** `consumed_cursor`, the resume point is
      **unchanged** and no clamp fields appear. *Negative control: the clamp must not fire on the
      normal path.*
- [x] **AC11** (`unit`): `--since -1` is never clamped upward or downward — "from the beginning"
      survives.
- [x] **AC12** (`unit`, NFR2): when `/status` is unreachable or reports no `consumed_cursor`,
      `--since` is used exactly as given and nothing new appears in the payload.
- [x] **AC13** (`unit`): when the clamp fires, the payload publishes **both** the cursor requested and
      the cursor resumed from, under names that cannot be confused for one another. *The correction is
      visible rather than magic — an agent whose cursor was wrong can see that it was.*
- [x] **AC14** (`unit`, **route (a)**): the reproduction is pinned — a log whose only new turns are
      **unaddressed** advances the caller's cursor while delivering nothing — and from that diverged
      state the clamp makes the caller's stale cursor stop deciding what gets read. *This is the route
      that needs no mistake by anyone, so it is the one that must not regress. It asserts the
      divergence and the recovery, **not** a deadlock, per the correction under FR2: route (a) alone
      cannot strand an addressed turn.*
- [x] **AC30** (`unit`, the downward bound): a server reporting a **negative** `consumed_cursor`
      against a **non-negative** `--since` does **not** clamp — the caller's cursor is used as given
      and no clamp fields appear. *Asserted against a real multi-thousand-turn log, so an unbounded
      clamp fails by actually returning the whole log rather than merely looking expensive.*
- [x] **AC32** (`unit`, the edge the guard was widened past): `consumed_cursor: 0` **does** clamp.
      *Zero is a real read position — the first turn, read. A guard written `<= 0` would swallow it
      and leave a genuine unread turn undeliverable, and nothing else in this spec would notice.*
- [x] **AC31** (`unit`, **negative control** for AC30): `--since -1` against `consumed_cursor: -1`
      still resumes from the beginning. *Without this, AC30 passes on an implementation that broke
      "from the beginning" entirely.*

### FR3 — guidance when it changes something

- [x] **AC15** (`unit`): the **first** call of a command in a session emits the **full** guidance for
      its branch.
- [x] **AC16** (`unit`): a **second** call of the same command taking the **same** branch emits a
      `next` that is **materially shorter** and is marked as a repeat — asserted on character count
      and on the marker.
- [x] **AC17** (`unit`, **negative control**): a second call taking a **different** branch emits the
      **full** guidance. *Without this, AC16 passes on an implementation that shortened everything
      after the first call.*
- [x] **AC18** (`unit`, **the property that must not be lost**): **every** `next` this tool emits —
      long form and short form, across every branch of every command that has one — contains a
      literal command with the session substituted, and no unresolved placeholder. *Asserted by
      sweeping the branches, not by checking one.*
- [x] **AC19** (`unit`): the branches are tracked **per command**, so an alternating `watch` → `say` →
      `watch` sequence suppresses the repeat on each command's second call. *This is the case a global
      key gets wrong.*
- [x] **AC20** (`unit`, TC5): writing the branch state **preserves `empty_streak`**, and writing
      `empty_streak` preserves the branch state.
- [x] **AC21** (`unit`): the short form **names how to recover the full reasoning**, so an agent that
      only ever sees short forms is not stranded.

### FR4 — the saving is a number, and the number is a gate

- [x] **AC22** (`integration`): a measurement command reports, for each of the three named
      exchanges — a three-clip answer, a refused batch, a quiet `watch` — the characters spent before
      and after, and the reduction. **The refused batch is reported at three turn sizes — 74 (median),
      258 (p90) and 702 (the observed one) — and all three are printed whether or not they pass.**
- [x] **AC23** (`integration`, **the gate**): that command **exits non-zero** when the **702-char**
      refused-batch reduction falls below **40%**, or the three-clip reduction below **25%**.

      **Why the gate is pinned to 702 and not to the median, stated so it cannot be read as
      goalpost-moving.** The turn size dominates this number: measured on the shipped
      implementation, a four-clip refused batch saves **46.2%** at 702 chars, **28.8%** at 258, and
      **13.3%** at 74. Picking the size *after* seeing those figures would be exactly the failure the
      pre-registration discipline exists to prevent — so the reason has to be independent of them,
      and it is: **702 is the size FR1 was written about.** The metaspec's own words are *"each
      refusal carried the full text of the same ~700-character turn"*, and FR1's rationale is
      explicitly that the cost scales with the size of the thought being delivered. The observed
      defect is the gated case.

      **And the weaker numbers are reported rather than hidden** (AC22), because they carry the real
      finding: **a repeat refusal is a flat cost regardless of turn length**, so the saving is large
      exactly where the requirement said it hurt and small where there was little to save. A gate on
      the median would fail on a correct implementation; suppressing the median would hide what the
      fix actually does.
- [x] **AC24** (`integration`, **proven to fire**): the gate is shown to **fail** on a payload that
      does not save — not merely to pass on one that does. *A gate whose failure has never been
      observed is indistinguishable from one that cannot fail.*
- [x] **AC25** (`unit`, the honest scenario): the quiet `watch` payload **does not grow**. *The
      finding is that it is already cheap; the criterion protects that rather than inventing a
      saving.*
- [x] **AC26** (`integration`): the measurement runs with **no server started and none contacted**,
      and on the CI floor — core dependencies only, no model, no microphone. *Asserted, because a gate
      that cannot run in CI is a gate nobody runs.*

### Contract and suite

- [x] **AC27** (`unit`, TC3): every field this spec adds to a payload appears in `describe`, and the
      existing describe-agrees-with-itself check still passes.
- [x] **AC28** (`integration`): the full suite passes with **no test weakened, skipped or deleted**,
      and **no `voice-tunnel` server started or contacted** (TC1). The pre-change test count is
      recorded so a shrinking suite is visible.
- [x] **AC29** (`integration`): `ruff check voice_tunnel/ tests/ scripts/` — the exact command CI
      runs — is clean (TC6).
      **🔴 It was NOT clean before this spec, and that is a finding, not an obstacle.** Measured
      independently on a clean worktree at HEAD: **15 errors** — `B905` ×5 in `scripts/devicepills.py`
      and `I001` ×10 across three test files. The cause is drift, not new code: `pyproject.toml` pins
      `ruff>=0.1.0`, and 0.16.x flags function-level import blocks and bare `zip()` that older
      versions did not. **CI has not run since before these commits landed, so the failure is
      invisible until the next push — at which point the lint step fails before it ever reaches the
      gate this spec adds.** That is what makes clearing it load-bearing here rather than adjacent
      housekeeping: an unreached gate is an unobserved gate. The sweep lands as its own commit so it
      can be read and reverted separately from the spec's behaviour changes.

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
| 1354 | **−1** | **1354** | no — the downward bound (AC30) |
| 1354 | **any other negative** | **1354** | no — the bound is the class, not the literal |
| 1354 | **0** | **0** | **yes** — zero is a real read position (AC32) |
| −1 | −1 | −1 | no (AC31) |

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
- **Un-overloading the `-1` cursor sentinel.** `read_consumed_cursor` returns `-1` for two facts it
  cannot tell apart — "nothing was ever read" and "the cursor file was missing or unreadable" — and
  that ambiguity is what forces the downward bound above to be a blunt rule rather than a precise
  one. Distinguishing them is a change to the store's on-disk contract and to what a restarted server
  believes about a session, which is a different unit of work from what this spec touches. **Recorded
  here because the bound is a workaround for it, and a workaround whose cause is unnamed is the kind
  that gets removed by someone who cannot see why it exists.**
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

## Verified state (2026-08-19)

Recorded by the team lead after **independent** verification. Every number below was re-derived by
the lead against the working tree, not accepted from an implementing slice's report.

**The measurement, from `scripts/contextcost.py`.** Both columns computed from the live code.

| exchange | before | after | saved | cut | floor | verdict |
|---|---|---|---|---|---|---|
| refused batch, 4 clips @ 74 | 3,512 | 2,648 | 864 | 24.6% | — | reported |
| refused batch, 4 clips @ 258 | 4,248 | 2,832 | 1,416 | 33.3% | — | reported |
| **refused batch, 4 clips @ 702** | **6,024** | **3,276** | **2,748** | **45.6%** | 40% | **pass** (+338) |
| **3-clip answer** | **942** | **698** | **244** | **25.9%** | 25% | **pass** (+8) |
| quiet watch, 2 calls | 612 | 612 | 0 | 0.0% | no-grow | pass |

**A repeat refusal is a flat 590 characters at every turn size** — `[1506, 590, 590, 590]` at 702,
`[878, 590, 590, 590]` at 74. FR1's complaint was that the charge for being interrupted scaled with
the size of the thought. It no longer scales at all.

**The gate is real, proven by the lead's own mutation rather than the building slice's.** Neutering
FR3's branch memo so every `next` is emitted full drops the three-clip arm to **0.0%** and the gate
exits **1**. The refused-batch arm was likewise driven red by neutering FR1's memo.

**FR2's behaviour table was re-derived directly against `_resume_cursor`**, all nine rows, including
the two the lead added late. `consumed_cursor` of `-1`, `-2`, `-5` and `-999` all decline to clamp;
`0` clamps to `0`. **Mutating the guard to `<= 0` turns four tests red**, AC32's row among them, so
the widened edge is defended rather than merely correct today.

**No payload grows.** Swept all nine `next` branches in serialized bytes: `muted` now shortens
(186 → 179) where it previously declined, `no_clients` 324 → 116, `turns` 414 → 191; `orb_off`,
`quiet`, `quiet_verbose` and `no_server` correctly decline because their short form would cost more
than it saves.

**Suite: 1,008 → 1,080 tests, 0 failed, 2 skipped** (both pre-existing macOS/XDG platform skips). No
test weakened, skipped or deleted. **`ruff check voice_tunnel/ tests/ scripts/`: clean**, from 15
errors at HEAD.

⚠️ **One artifact worth recording so it is not rediscovered:** the 1,008 baseline was taken in a
detached worktree, where one TTS test fails because `models/` is gitignored and therefore absent
there. It passes in the real checkout. **A baseline taken outside the working tree inherits whatever
that tree's ignored files were providing.**

### What was NOT verified, and why

- **Nothing ran against a live server.** A live voice session held session `dev` throughout — it went
  from turn 1,365 to 1,425 while this was built — so no criterion started, stopped, restarted or
  contacted a server. Every server-side behaviour is verified through in-process handlers and
  temp-directory fixtures. **What that leaves open is the real aiohttp request path**: the payload
  shapes are asserted, the transport carrying them is not.
- **FR1 is not active in the live session**, because a running server keeps the code it started with.
  FR2 and FR3 reached it immediately — the CLI loads from the working tree on every invocation — and
  finding 6 records that arriving mid-conversation as an unplanned production test that passed.
- **The subjective claim is untested by construction.** Whether this makes a session last materially
  longer is a live observation over days. The character counts are what this spec can assert, and
  they are what it asserts.

## Findings from implementation (2026-08-19)

Recorded here rather than in a status message, because a finding that lives in a reply is separated
from the requirement it qualifies. Each was surfaced by an implementing slice and **re-derived by the
team lead before being accepted.**

### 1. 🔴 The most-emitted string in the tool could not be run verbatim

`_next_action`'s `turns` branch — the 402-character guidance that fires **once per turn of every
conversation**, the single most-emitted string this CLI produces — spelled its cursor as the literal
text `--since <cursor>`. Meanwhile the function's own docstring promised *"session and cursor already
substituted"* and `describe` promised *"session and cursor filled in"*. **The cursor was known and
simply was not interpolated.**

So the one branch an agent sees most was the one branch whose command it could not paste and run —
which is precisely the property that made `next` worth keeping (FR3's opening paragraph). Fixed.

🎯 **It was found only because AC18 mandates a *sweep* of every branch rather than a check of one.**
A criterion written as "the `next` field contains a runnable command" would have passed on the first
branch tested. *This is the argument for sweeping any property that is claimed universally.*

### 2. 🔴 A marker can cost more than the text it saves

FR3's first implementation shortened the `muted` branch by 14 characters and then spent 23 on the
`next_repeated` marker: **the payload grew, 515 → 524.** The change was a saving measured on the
*string* and a regression measured on the *object that ships*.

Fixed with an explicit cost guard — the short form is emitted only when it saves more than the marker
costs — and pinned by a test that sweeps every branch **in serialized bytes**. This also makes the
already-bare branches (`quiet` at 51 chars, `orb_off` at 119, `undelivered` at 131) self-classifying:
they never shorten and never grow, which satisfies AC25 by construction rather than by a hand-kept
list of exemptions.

⚠️ **The general form is worth carrying beyond this spec: measure the artifact that ships, not the
field you edited.** A per-field improvement and a per-payload regression are the same change.

### 3. Two `watch` exits are deliberately exempt from the repeat rule

`watch_open` (*"do nothing — the running wait has it"*) and `ceiling` (*"this is NOT permission to
reply"*) always arrive whole. Their prose **warns against the obvious action** rather than restating
the loop, so reducing them to the bare command would emit guidance that says the opposite of what the
branch means. Documented in `describe` and pinned by a test.

**This is a real limit on FR3's rule and not an exception to it:** the rule cuts *rationale that
repeats*, and these two carry *contradiction of an instinct*, which is not the same thing.

### 4. A repeat refusal costs a flat 453 characters regardless of turn length

Independently re-measured by the team lead through FR1's documented seam:

| turn `text` | first refusal | repeat | four-clip batch, before → after |
|---|---|---|---|
| 74 (median) | 551 | **453** | 2,204 → 1,910 (**13.3%**) |
| 258 (p90) | 735 | **453** | 2,940 → 2,094 (**28.8%**) |
| 702 (observed) | 1,179 | **453** | 4,716 → 2,538 (**46.2%**) |

*(Server payload only; the CLI's `next` is added on top and is FR3's saving.)*

🎯 **The flat repeat is the actual result, and it is a better one than the percentage.** FR1's
complaint was that *"the longer the thought the agent was trying to deliver, the more it is charged
for being interrupted."* After the change the charge for being interrupted **does not scale with the
thought at all.** The percentage is small at the median precisely because there was little to save
there — which is why AC22 requires all three sizes to be printed rather than only the gated one.

### 5. 🔴 A test suite that writes into the live conversation's state

FR3 made `cmd_say` persist per-session state. `tests/conftest.py`'s autouse fixture isolates
`VOICE_TUNNEL_ENV_FILE` but **not** the session directory — that is the opt-in `tmp_sessions` fixture
— so tests calling `cmd_say` with session `"dev"` began writing to the real `sessions/` directory,
which is where a live conversation keeps its state.

Three consequences, all measured rather than reasoned about:
- `tests/test_say_refusal.py` became **order-dependent**: an assertion on `next` passes only while
  that test runs before the file's other refusal test, because the second call receives FR3's short
  form. Reproduced by reversing the order — it fails. **A test whose result depends on ordering — or
  on whether a human happens to be mid-sentence — is not a test.**
- A suite run was observed writing `sessions/s.watch.json`, and its `empty_streak: 212` survived
  **only** because TC5's read-modify-write requirement was already in place.
- 🔴 **The sharpest one, and it is worse than the backoff:** the suite would have written
  `next_branch.say = "refused"` into the live session's file, where the live agent had `"async"`.
  **The next thing JJ's agent said would have received the wrong branch's guidance** — a test run
  reaching into a running conversation and changing what the tool tells the agent. Not a crash, not
  a lost turn: wrong advice, silently, with nothing to attribute it to.

Closed by isolating the session directory in the autouse fixture, which fixes the class rather than
the three instances found. `tests/test_say_refusal.py` then needed **no edit** — isolation alone
restored order-independence, verified across natural order, reversed order and five shuffle seeds.

⚠️ **And the obvious check for this is vacuous:** `git status --porcelain sessions/` reports clean no
matter what happens, because `sessions/` is gitignored.

🎯 **What worked instead is worth carrying: attribute the writes rather than diffing the directory.**
The live server writes `sessions/` continuously, so mtimes and hashes cannot separate its traffic
from the suite's — an idle control confirmed three files kept growing with no pytest running. The
verification that actually answered the question was a `sys.addaudithook` inside the pytest process
recording every mutating filesystem event under `sessions/`, which **observes the suite and is blind
to the server by construction.** It returned 30 touches, all from one unrelated pre-existing test,
and zero writes to any `.watch.json`, `.jsonl`, `.wav` or `.consumed.json`.

⚠️ **And the leak was reproduced without ever re-creating it** — re-pointing the directory at a
stand-in that outlives each test reproduces the pre-fix property exactly, with no possibility of
writing into the live conversation. *Demonstrating a defect must not require committing it.*

### 6. TC5's guard was confirmed in production, unintentionally

Because the CLI loads from the working tree on every invocation (TC1), **the live `dev` session began
running FR3's code the moment the file was saved.** Its `sessions/dev.watch.json` shows a real
`next_branch` written by the live agent — `{"watch": "turns_verbose", "say": "refused"}`, mtime
matching `dev.consumed.json` to the second — **with `empty_streak` preserved through the write.**

That is AC20's property holding on live traffic rather than on a fixture, and it is the strongest
evidence available that the TC5 trap is closed. It is also a reminder of what TC1 means in practice:
this spec's CLI changes were never staged, they were live on arrival.

### 7. 🔴 The three-clip gate MISSES, by 23.5 characters — and the floor is not moving

**Measured on the shipped implementation: 22.51% against the pre-registered 25% floor.** The gate
exits non-zero. `contextcost.py --gate` is red, and so is the CI step this spec adds.

| exchange | before | after | reduction | floor | verdict |
|---|---|---|---|---|---|
| refused batch, 702 (gated) | 6,024 | 3,324 | **44.8%** | 40% | **pass**, +290 chars |
| refused batch, 258 | 4,248 | 2,880 | 32.2% | — | reported |
| refused batch, 74 | 3,512 | 2,696 | 23.2% | — | reported |
| **three-clip answer** | **942** | **730** | **22.51%** | **25%** | 🔴 **miss, −23.5 chars** |
| quiet watch | 612 | 612 | 0.0% | — | does not grow (AC25) |

**The floor stays where it was pre-registered, and so does the measurement basis.** Both of the
available ways to turn this green are the failure the pre-registration exists to prevent:

- *Lower the floor to 20%* — moving a goalpost after seeing the ball land.
- *Stop counting the `next_repeated` marker* — `after_without_marker` is 684, which reads as
  **27.4%** and clears the floor. But the marker is 23 characters the agent genuinely receives, and
  this repo's own `NEXT_REPEAT_MARKER_COST` guard exists precisely because a saving measured on the
  string and a regression measured on the payload are the same change (finding 2). Excluding it here
  would contradict a guard written three findings ago.

🎯 **The gate is not wrong. The short form is not short enough — and that is a finding the gate
produced, which is what a gate is for.** `next` goes 224 → 95 on a repeat, but ~47 of those 95 are
the tail `— same reasoning; see \`voice-tunnel describe\``, which is nearly as long as the command it
follows and is **redundant with `next_repeated: true` already being in the payload.** Tightening it
is on-mission rather than a dodge: this spec's whole subject is words that buy nothing on the second
occurrence, and that tail is one.

⚠️ **And the arithmetic is structural, not incidental.** The saving is one full→short swap per
*extra* clip against a fixed first-clip cost, so it grows with clip count: **2 clips 16.9% · 3 clips
22.5% · 4 clips 25.3% · 5 clips 27.0%.** The operating guide caps an answer at three clips, so **the
gated case is the least favourable one that can occur** — which is the right case to gate on, and
also the reason the floor cannot be met by hoping for longer answers.

### 8. The spec measured a refused batch two different ways, and did not notice

AC23's pre-registered figures (46.2% / 28.8% / 13.3%) were computed on the **server refusal payload
alone**. The "Measured ground truth" table above them was computed on the **agent-visible total**
(1,567 chars at 702, of which `next` is 287). Those are different denominators for the same named
quantity, sitting a few hundred lines apart in one document.

**Resolved by gating on the agent-visible total**, which is the stricter basis (44.8% rather than
46.8%) and the one that matches what the requirement is about — what the agent pays. The server-only
figures are still printed so the pre-registration remains reproducible and the divergence is visible
rather than argued about.

*Two numbers for one quantity is the defect; which one wins matters less than that the document
stopped saying both.*

### 9. Two ground-truth numbers in this spec were slightly wrong

Both were measured by the team lead before implementation, by constructing payload dicts by hand
rather than calling the code — which is the same class of error as citing instead of re-deriving.

- **A quiet `watch` is 306 characters, not 278.** The hand-built dict omitted `quiet_rounds`,
  `waited`, `next_wait` and `listening`. AC25 is unaffected: 51 of those characters are still `next`,
  all 51 are the runnable command, and the payload does not grow.
- **The absolute refusal figures run ~4% high** (6,268 vs 6,024 for four refusals at 702), because
  the live log's four-digit turn ids and cursors are a few characters wider than a harness's
  single-digit ones. It moves the percentage by well under a point.

*Neither changes a verdict, and both are recorded rather than quietly corrected, because the
before-numbers are what the after-numbers are checked against.*

### 10. A negative control was proven to be one, by surviving the mutant

FR2's slice mutation-checked its work in **both** directions, and the second direction is the one
worth recording. Neutering the clamp to the identity function killed AC8, AC9, AC13, AC14's recovery
arm and the first behaviour row — the expected result. Then restoring the *unbounded* `min()` killed
AC30 and its behaviour row **while AC31 and AC8 survived.**

🎯 **That survival is the evidence AC31 is a real negative control rather than a second copy of
AC30.** An AC31 that had died under the unbounded mutant would have been separable by the same thing
AC30 is separable by, and would have measured nothing extra.

*This is the delegation guide's rule — a negative arm invariant to the change under test has not
tested it — run as an actual experiment rather than asserted. The cheap version is to mutate the code
the guard protects and check which criteria stay green; the ones that stay green are the ones
carrying independent information.*

### 11. CI's lint step was already red, and nobody could see it

`ruff check voice_tunnel/ tests/ scripts/` — the exact command CI runs — failed at HEAD with **15
errors** (`B905` ×5, `I001` ×10), none of them from this spec's work. `pyproject.toml` pins
`ruff>=0.1.0`, so CI installs the current 0.16.x, which flags shapes older versions did not.

**The reason it went unnoticed is the part worth recording: CI had not run since before those commits
landed.** A gate that has not executed is indistinguishable from a gate that passes. Cleared as a
separate commit so it reads and reverts independently of this spec's behaviour changes — and cleared
at all because the gate this spec *adds* sits downstream of the lint step, so a red lint would mean
the new gate never runs.

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
