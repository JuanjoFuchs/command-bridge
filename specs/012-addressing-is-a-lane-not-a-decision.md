---
id: "012"
title: Addressing is a lane, not a decision
status: in_progress
blocked_by: []
blocks: []
kind: metaspec
---

# Addressing is a lane

> **This began as a METASPEC** written by the strategist, carrying intent, the
> decisions already taken, and the constraints the implementer must not rediscover. **It has been
> refined in place** by `voice-tunnel:architect` against the live code: the rulings it left open are
> made below with their evidence, implementation tasks and acceptance criteria are added, and the
> work is split at the seam the metaspec named.
>
> **Completion rule:** not complete until every acceptance criterion below is verified by the method
> named on it. Build-only verification is insufficient. Iterate until verification passes.

## Overview

One microphone, one transcript, **several agents** — and the tunnel remembers which one he is talking
to. Saying *"hey Codex"* makes Codex the live lane; everything he says after that goes to Codex and
nobody else, until he switches again.

**The purpose is bandwidth**, which is this project's stated *why* (Voice Tunnel: *"I am trying
to increase the bandwidth between me, my intent, my ideas, and my agents"*). Today he can only talk to
one agent at a time and the others are idle while he does. JJ, 2026-08-21: *"I am on a critical need of
increasing my intent per minute."*

## JJ's design, verbatim (dictated 2026-08-21, stitched from eight consecutive turns)

> *"Maybe this could be like a meeting. I'm in a meeting with multiple agents, and I refer to those
> agents, I named them. For example, let's say you are Claude and then I have another one that's Codex,
> and this UI, the client, would show multiple lanes. The transcript is just one, and maybe the agent
> that's speaking, or the agent I'm speaking to, is the different lanes. And I say 'hey Claude' and the
> lane changes to you, and that means that I hear you and you hear me. And if I say 'hey Codex' I switch
> the lane to the other one, and then I hear them and they hear me. And while I am on the lane with the
> other one, the other agent cannot barge in — what it's saying would be queued up. But there would need
> to be something visual in the UI that tells me that that agent has stuff to say. And I would like to be
> able to keep track of, still, see if one agent is thinking, transcribing, or synthesizing."*

> *"I would also like to be able to control the lanes from the UI by clicking around. Because not only
> should I be able to address one agent, there should be a way in which I can broadcast to all the
> agents."*

> *"Only when I address a particular agent, that turn enters that agent's context window, right?"*

> *"We would need to design somehow the CLI and the server as well, so that it can take the turns and
> has the proper cues, to avoid any issues."*

## Why this is not the anti-goal it looks like

Voice Tunnel wrote down on day one that **chasing true addressivity is out** — *"that's the one
genuine research risk and it's explicitly out"* — and the "many voices" question has been blocked on it
since July. **This spec does not lift that anti-goal; it routes around it.**

**Addressing became STATE instead of a judgment.** Every earlier sketch assumed the tool had to work out
who each turn was for, which is inference and is correctly forbidden. **A lane is sticky.** The wake name
is a *switch*, thrown once and then remembered, so the tool never decides anything — it looks up a stored
value. He names an agent about as often as he changes subject, which is the cost he was willing to pay
and the two he refused (a name every turn, a tap every turn) are both avoided by the same property.

**It also stays on the dumb side of the dumb-tool/smart-agent split.** An exact wake match plus a stored
current-lane is a lookup. The tool still holds no LLM, still makes no decision about content, and still
knows nothing about what any agent is doing.

⚠ **The line to hold:** if any part of the implementation needs to *guess* which lane a turn belongs to,
it has crossed into the anti-goal and the design is wrong, not the anti-goal.

## Goals

- **He can hold a conversation with several agents in one session**, switching by voice, without naming
  an agent every turn and without tapping anything.
- **A turn reaches exactly the agent it was addressed to** — the others never see it.
- **An agent that is off-lane is visibly waiting, not silently dropped.**

## Requirements

### Functional

- **FR1** — A session has **N lanes and exactly one live lane**. The live lane is server state, readable
  by the CLI and visible in the UI; there is never ambiguity about who is being talked to.
- **FR2** — **The wake name switches the live lane, and the switch is sticky.** *"hey Codex"* makes Codex
  live and it stays live until something switches it. This is the existing per-agent wake name
  (`wake --name`), which is already fuzzy-matched — see **TC2**, which is the sharp edge here.
- **FR3** — **The turn carrying the wake word belongs to the NEW lane, including the words after it.**
  *"Hey Codex, run the tests"* is one turn: it switches the lane **and** delivers an instruction, and the
  instruction goes to Codex. A design that switches on turn N and routes on turn N+1 loses the sentence
  he actually cared about.
- **FR4** — **A turn is stamped with its lane, and `watch` returns only the turns for the agent that is
  watching**, plus broadcasts. This is the routing layer, and it is what JJ meant by *"only when I address
  a particular agent, that turn enters that agent's context window"* — an off-lane agent does not receive
  the turn at all, rather than receiving it and being asked to ignore it.
- **FR5** — **A broadcast lane addresses every agent at once.**
- **FR6** — **The UI shows the lanes and a tap switches the live one.** The wake word is the hands-free
  path; the tap is for when his hands are free. ⚠ He refused being *required* to tap, not the tap itself.
- **FR7** — **An off-lane agent's speech does not play. It is held, and its lane is visibly marked as
  having something to say.** Held, not discarded — see **TC3**, which is why that word is load-bearing.
- **FR8** — **Per-lane state is visible**: thinking / transcribing / synthesizing / speaking. The server
  already announces every one of these stages for a single agent; this is fan-out plus a place to show it.
- **FR9** — **`watch` and `say` carry a lane**, and an agent cannot speak into a lane that is not its own.

### 🎯 Sequencing — FR1–FR5 ship first, and they deliver the whole bandwidth win on their own

**The routing half is what lets him talk to two agents. The UI half makes it comfortable.** With FR1–FR5
alone he can already hold two conversations from one microphone, with the existing page, because voice is
the switch and the transcript is already shared. FR6–FR8 are a second slice.

⚠ **This spec is too big for one session as a whole** (Spec Writing Rules for Agents); the
implementer should split it at that seam rather than starting everywhere.

**Split taken:** **Slice A = FR1–FR5 + FR9** (routing, CLI, server, turn schema — no page changes).
**Slice B = FR6–FR8** (the lanes UI, the hold, per-lane state). Slice B does not begin until AC-16 has
passed, per TC3.

### Non-functional

- **NFR1** — **A lane switch is not perceptible.** It happens in the same path as the wake gate, on state
  the server already holds. If switching costs a round trip he will feel it on every subject change.
- **NFR2** — **Nothing is lost at a switch.** A turn in flight when the lane changes belongs to whichever
  lane it was addressed to, decided once, at the wake gate.
- **NFR3** — **A single-lane session behaves EXACTLY as it does today.** Not approximately: the verdict
  (`addressed`, `reason`) for every turn in the recorded corpus must be unchanged. This is what makes the
  feature safe to ship into a live daily-driver tunnel, and it is verifiable rather than asserted (AC-2).

### Technical constraints

- **TC1** — **Addressing is a lookup, never an inference.** Stated as a constraint because it is the
  property that keeps this inside the North Star, and it is the one an implementation would erode first.
- **TC2** — 🔴 **A fuzzy wake match against N names can switch to the WRONG lane, and that is a new failure
  mode this project has never had.** The matcher uses two thresholds (0.55 after a greeting, 0.80 bare)
  precisely because **Parakeet has never once transcribed "Claude" correctly from his headset** — it
  produced Grab, Grub, God, Well, Joe, Clock. With one agent a bad match means *not addressed*, which is
  recoverable by repeating. **With several, a bad match means the turn lands in the wrong agent's context
  window**, where it cannot be recalled and may be acted on. **The implementer must rule on the ambiguous
  case; the recommendation is to refuse to switch and say so audibly**, because a refusal costs one repeat
  and a mis-switch costs an unwanted action plus a polluted context.
  → **Ruled below** in *The TC2 ruling*, with two of this constraint's own premises corrected by measurement.
- **TC3** — ⚠ **Held speech must not reuse the queue path that already loses clips.** Measured 2026-08-20:
  nine clips issued while the channel was closed each returned `queued: true`, and **none played when the
  channel reopened**. The lane hold is a *different* case — the channel is open, another lane is live — so
  it is new code rather than the broken path, but FR7's entire value is that the waiting agent is not
  silently dropped. **Confirm the hold delivers before building the UI mark that promises it does.**
  → **Mechanism identified below** in *Findings — TC3*. It gates Slice B via AC-16.
- **TC4** — **Barge-in is gated on JJ's voiceprint**, so no agent can interrupt another through the room.
  Suppression in FR7 is therefore about **playback**, not about the microphone.
- **TC5** — **Android Chrome, foreground tab only.** More lanes does not change the platform rule.
  *Inherited platform ceiling — nothing here can test it, and nothing here can violate it.*
- **TC6** — **One transcript.** He was explicit: *"the transcript is just one."* Lanes are a view over a
  single log, not several logs — which is also what makes the shared context readable to him afterwards.
- **TC7** — **The single-waiter guard is per SESSION today and must become per LANE.** `watch` refuses to
  start when `status.watch_open` is true, because two waits on one log race for turns and one cursor
  silently falls behind. That guard is correct and must be kept — but N agents watching N lanes is the
  normal case here, so a session-wide flag would refuse every agent after the first. It becomes one wait
  per `(session, lane)`.
- **TC8** — **A lane name must be exact-matchable, and it is ONE token.** The switch is an exact string
  match on the single word after the greeting (see the ruling), so a name the recognizer never renders
  exactly is a name he can never switch to by voice, and a name containing a space or a hyphen can never
  be matched at all — normalization splits it into two tokens. Registration therefore refuses a malformed
  name, a duplicate, and the reserved broadcast name.
  ⚠ **It does NOT refuse a name merely similar to another lane, and an earlier draft of this spec was
  wrong to say it should.** Measured while implementing: `claude` and `codex` score **0.55** against each
  other — exactly the threshold that draft proposed — so the rule would have rejected this feature's
  primary use case at registration. It would also have bought nothing, on two counts. Mis-routes are
  already structurally impossible, because only an exact name switches. And lane-to-lane similarity did
  not predict false refusals either: that same 0.55 pair produced **zero** across all 109 greeting-led
  utterances. **The predictor is proximity to ORDINARY SPEECH, not to another lane** — every measured
  false refusal was `grok`, against `go`, `got` and `god`. So the cost is reported rather than refused:
  `lane add` warns and `lane list` carries the number, because a name whose only fault is costing an
  occasional repeat is his call to keep.

## The TC2 ruling — only an exact lane name switches a lane

**Ruling.** With the live lane as `current` and the registered lane set as `lanes`, the outcome for an
utterance that opens with a greeting is decided by this table, evaluated top to bottom. Nothing else
switches a lane.

| The token after the greeting | Outcome |
|---|---|
| **exactly** a registered lane name `L`, and `L != current` | **SWITCH to `L`.** This turn belongs to `L` (FR3) |
| **exactly** `current` | stay on `current` |
| scores < 0.55 against **every** lane | stay on `current` — it is just the next word of a sentence |
| scores ≥ 0.55, and the **strict top scorer is `current`** | stay on `current` — a mangled form of the name already live changes nothing |
| scores ≥ 0.55, top scorer is **not** `current` (or ties) | **REFUSE.** No lane. `addressed: false`, `lane: null`, `reason: "ambiguous:<candidates>"`. Not delivered to anyone, and audibly signalled |

**Why refusal and not a best guess:** the two errors are not symmetrical and the metaspec is right about
the asymmetry. A refusal costs one repeat. A mis-switch puts an instruction into an agent's context where
it cannot be recalled and may be acted on. The table is ordered so that **the only irreversible act —
moving the conversation to a different agent — requires the strongest possible evidence, an exact match**,
while every weaker signal resolves to the *status quo*, which is free to be wrong because it changes nothing.

**Why this is a lookup and not an inference (TC1):** every row is string equality or a fixed-threshold
comparison against a stored set. Nothing consults context, content, or history.

**Why it needs no special case for a single lane:** with `N == 1` the only lane *is* `current`, so rows 1
and 5 can never fire and the table collapses to today's behaviour exactly. NFR3 is therefore a property of
the design rather than a compatibility layer, and AC-2 proves it against the real corpus.

### Two premises of TC2 corrected by measurement

Both were true when written and are no longer. Measured over **every turn in `sessions/*.jsonl` — 2,460
turns, 2026-07-29 to 2026-08-21** — replaying the live matcher.

**1. "Parakeet has never once transcribed Claude correctly from his headset" is no longer true, and the
change is sharp.** Of 109 greeting-led utterances, the name was rendered **exactly** in 45:

| Period | greeting-led utterances | name exact | rate |
|---|---|---|---|
| 2026-07-29 → 08-06 | 29 | 0 | **0.0%** |
| 2026-08-07 → 08-21 | 80 | 45 | **56.3%** |
| best single day (08-18) | 15 | 13 | **86.7%** |

The corpus does not establish *why* it changed and this spec does not guess; what the ruling needs is the
current rate, not the cause. **The consequence is the ruling itself:** exact-only switching was
unthinkable at 0% and is the safe default at 56%.

**2. The real hazard is not a fuzzy match picking the wrong name — it is a summons carrying NO name at
all.** The matcher's third rule accepts *a greeting followed by anything*, and that rule is **51% of all
phrase-granted summons** (52 of 102; exact 45, fuzzy 3, elsewhere-in-sentence 2). The tokens it accepts are
ordinary next-words, not manglings — `i` ×12, `can` ×9, `let` ×3, `so` ×2. **With one agent that rule is
correct and load-bearing. With N lanes it names nobody**, and any attempt to score those tokens against a
lane set produces confident nonsense: `go` → `grok` **0.67**, `got` → `grok` 0.57, `sorry` → `cursor` 0.55,
`again` → `gemini` 0.55 — all above the 0.55 threshold that exists to rescue real manglings. **A margin
rule does not catch these**, because they have no competitor: they are unambiguous and wrong. That is what
rules fuzzy matching out across lanes rather than merely tuning it.

### Measured cost of the ruling

Same corpus, replayed through the table above with `current = claude`:

| Lane set | corpus | mis-routes | false refusals |
|---|---|---|---|
| `claude, codex` | all 109 | **0 (0.0%)** | **0 (0.0%)** |
| `claude, codex` | 84 since 08-07 | **0 (0.0%)** | **0 (0.0%)** |
| `claude, codex, grok` | all 109 | **0 (0.0%)** | 5 (4.6%) — `go`, `got`, `god`×2, `grog` |
| `claude, codex, grok` | 84 since 08-07 | **0 (0.0%)** | 2 (2.4%) — `go`, `got` |

**Mis-routes are structurally zero** for any lane set, because only an exact name switches. **False
refusals are a property of the NAMES, not of the rule** — every one of them is `grok`, a short name close
to ordinary speech (`go`, `got`, `god`), and none of them is a name close to another *lane*. That
distinction is the whole of TC8: `claude` and `codex` are 0.55 similar to each other and cost nothing,
while `grok` is unlike every other lane here and costs 2.4%. **Confusability with the lane set is the
wrong thing to measure**; confusability with ordinary speech is the right thing, and it is a cost to
report rather than a rule to enforce, because the corpus that measures it is his speech and is not
something a fresh install has.

## The two remaining rulings

**A broadcast enters EVERY agent's context, unconditionally — never "only the idle ones."** Filtering on
idleness would mean routing on `agent_state`, which is a *claim an agent makes about itself*, and this repo
already holds that a status the agent announces is a claim rather than a fact. It would also make delivery
unexplainable ("why did Codex not get that?" — "it said it was thinking"). **And it re-introduces exactly
the drop the cursor contract exists to prevent:** `watch --since <cursor>` already guarantees that a busy
agent loses nothing while it thinks, so busy-ness is *already solved* and does not need a second, worse
mechanism. Every lane gets the turn; each reads it when it next watches.

**The refusal is announced by the AGENT, not by a sound — and my own implementation task was wrong about
this.** TC2 asks that a refusal be "said audibly", and the obvious reading is a new cue. The repo has
already refused that, in an executable guard whose docstring is explicit: *"a fifth (a quieter capture
tick, to buy back the liveness signal) is his call, not the implementer's."* I had written the fifth cue
and the test caught it, which is the guard doing precisely its job.

**It is right for a better reason than vocabulary size.** A cue can only say that *something* went wrong.
An agent can say *"did you mean Claude or Codex?"* — so the signal goes to the layer that owns words, and
the tunnel keeps holding none. The wait of the **live lane** returns with `reason: "ambiguous"` and the
candidates; that lane is the one he was already talking to, so it is a lookup rather than a guess about
who should ask. Whether a sound is *also* wanted stays open, and stays his.

**An agent IS told it went off-lane, on the wait it is already sitting in.** `watch` already returns for a
turn *or a control change*, whichever comes first — that is how pressing mute becomes visible to the agent.
A lane switch is a control change, so this is registration in an existing seam and not new machinery. The
wait returns `reason: "lane"` with the new `live_lane`, and the agent can say so before going quiet. **The
alternative — silently stopping — is unacceptable here** because an agent cannot then distinguish "he is not
talking" from "he is talking to someone else", and the whole operating discipline of this repo is that an
agent must never leave him talking to nobody.

## Findings — architect

**A live session was running throughout this work** (`dev`, uptime 86 min, 1 client, `channel_open: true`,
1,866 turns) and is JJ's daily driver. Two consequences, both binding on Slice A: **every harness in
`scripts/` except `devicepills.py` starts a server or drives the real socket and must not be run**, and
`rate` / `wake` / `config set` apply live and must not be touched. Slice A is verified entirely by `unit`,
`integration` and `corpus` methods, which need neither a port nor a microphone. This is a constraint on
*when* Slice B's harness work can run, not on whether.

**Baseline before any change: 1,083 tests pass, 2 skipped** (both platform-specific: macOS and XDG path
conventions).

**One pre-existing test was corrected, and it is the only test this spec touches that it did not write.**
`test_spawning_piper_is_reported_as_degraded` asserted `status == "degraded"` whenever `doctor`'s TTS
detail mentioned spawning. With the piper engine present and **no voice installed**, the honest answer is
`failed`, and the test's own stated intent — *"a spawning engine is a fallback, not a clean pass"* — is
satisfied by it. The assertion now checks exactly that claim (`!= "ok"`) and additionally pins `degraded`
when a voice **is** present, so it is **stronger** than before wherever it previously fired. It had never
fired: a developer machine has a voice, and CI has no `piper.exe`, so neither produces the one combination
that reveals it. **An isolated worktree does** — it inherits the venv and none of the downloaded models —
which is the same fresh-install blind spot `coldstart.py` exists for, reached from a different direction.

**`reason: "wake"` does not mean "he said the phrase".** It covers both a phrase grant and a conversation-
window grant, which is why 954 of the 1,056 wake-granted turns in the corpus match no phrase rule at all.
Any analysis that reads `reason == "wake"` as "a summons was spoken" over-counts by roughly 10×. The
routing code must branch on the *grant* (`WakeGate.last_grant`), which already distinguishes them, and not
on the persisted reason.

**Findings — tests this spec changed, and why none of them was weakened.** AC-15 forbids weakening a
test to accommodate lanes, so every existing test that moved is listed here with its justification.

| Test | Change | Why it is not a weakening |
|---|---|---|
| `test_no_flag_anywhere_on_say_disables_the_check` | `--lane` added to the allowed flag set | The guard exists to force a deliberate look at any new flag, and asks outright whether it is a way around the refusal. **It is not, structurally:** every branch `--lane` opens on the server *returns an error* (`unknown_lane` or `off_lane`), so it can only ADD a way to be refused, and the unread check still runs on every path that reaches speech. It makes `say` strictly harder to use |
| `test_the_wait_has_no_flag_that_changes_when_it_returns` | `--lane` added to the allowed flag set | `--lane` changes WHICH turns come back, never WHEN the wait returns — the same kind of flag as `--all-turns`, which the set already allowed for the same reason. No value of it reaches the speech signals the return is gated on |
| Four `store.watch` stubs (`test_watch`, `test_watch_backoff`, `test_watch_cursor_clamp`, `test_next_repetition`) and one in `scripts/contextcost.py` | new parameters named explicitly | A test double whose signature has drifted from the real one. Named explicitly rather than swallowed by a `**kwargs` catch-all, because the value of these stubs is that they fail loudly when the real signature moves — which is exactly what they just did |
| `test_spawning_piper_is_reported_as_degraded` | assertion narrowed to its own stated claim | Committed separately, since it is nobody's feature. Strictly **stronger** where it previously fired; see that commit |

**Findings — TC3.** The existing undelivered queue has two flush triggers — client reconnect and channel
reopen — and both are present and look correct, so the reported total loss of nine clips is not a missing
flush. Two mechanisms are capable of it and one is capable of losing *all* of them: **barge-in clears the
entire queue unconditionally** (`state.undelivered.clear()`), on the reasoning that playing the next clip
at a man who just interrupted is the same interruption wearing a different hat. That reasoning is right for
the lane he is *on* and wrong for a lane he is *not* on. The cap (`UNDELIVERED_MAX = 8`) explains exactly
one of the nine and cannot explain the rest. **Binding on Slice B: the lane hold is a separate store from
`undelivered`, is flushed when its lane becomes live, and is NOT cleared by barge-in** — barge-in silences
the live lane, it does not discard another lane's pending speech.

## Decisions already taken

| Decision | |
|---|---|
| **This stays in `voice-tunnel`; it is not a new project** | JJ asked directly whether it should be "Agent Meeting" instead. It should not: a lane touches the wake matcher, the playback queue, the turn log and the page — all inside this server — so a separate repo would have to fork it to reach them. This is a new **arc**, not a new product |
| **The wake name is the switch** | it already exists, is already per-agent and already persisted |
| **Off-lane agents are suppressed at playback, not muted at the microphone** | one microphone, one transcript |
| **Only an exact lane name switches a lane** | ruled above, on measured evidence: mis-routes are structurally zero, and fuzzy scoring across a lane set produces confident wrong answers a margin rule cannot catch |
| **A broadcast reaches every lane** | ruled above: the cursor already solves busy-ness, and routing on a self-reported state is not a lookup |
| **An off-lane agent is told, on the wait it is already in** | ruled above: reuses the existing control-change return |
| **The broadcast lane is spelled `everyone`, everywhere** | one name on the wire, in the CLI, in the turn field, and in what he says out loud. Spec 007 removed a second spelling of one concept for a reason; this does not add one back. `everyone` is reserved and cannot be registered as an agent lane |

## Contract

The shapes an agent depends on. Everything else is implementation and is derived from the live code.

```
voice-tunnel lane list                       # every lane, which is live, per-lane pending counts
voice-tunnel lane add <name>                 # register; refuses a reserved, duplicate or multi-word name
voice-tunnel lane remove <name>
voice-tunnel lane switch <name>              # the deterministic seam the tests drive; `everyone` allowed
voice-tunnel watch --session dev --lane codex --since <cursor>
voice-tunnel say   --session dev --lane codex "All green."
```

Turn schema gains ONE field:

```json
{"id": 1867, "session": "dev", "lane": "codex", "text": "hey codex run the tests",
 "addressed": true, "reason": "wake", "final": true, "wall": "..."}
```

- **`lane`** — the lane this turn was addressed to; `"everyone"` for a broadcast; `null` when the wake
  gate refused to resolve one (TC2 row 5). **Absent means the default lane** — the `--wake` lane the server
  was started with — so every one of the 1,866 turns already on disk keeps routing to the single agent that
  has been reading them.
- `watch --lane L` returns turns where `lane` is `L`, `everyone`, or absent-and-`L`-is-default. It never
  returns another lane's turns and never returns `lane: null`.

New error codes, per convention 8 (`{error, code, remedy}`):

| `code` | When |
|---|---|
| `off_lane` | `say --lane L` where `L` is not the caller's lane (FR9) |
| `unknown_lane` | any command naming a lane that is not registered |
| `lane_exists` | `lane add` for a name already registered, reserved, or not a single word token (TC8) |
| `watch_open` | unchanged in meaning, now scoped to `(session, lane)` (TC7) |

## Implementation Tasks

### Slice A — routing (FR1–FR5, FR9)

- [x] A lane registry on the server: N lanes, one live, the `--wake` lane registered as default at startup.
- [x] `lane` subcommand — `list` / `add` / `remove` / `switch`. `add` refuses a malformed, duplicate or
      reserved name and REPORTS confusability rather than refusing on it (TC8).
- [x] Resolve the lane in the wake gate per the TC2 table; return the resolved lane and the grant alongside
      the existing `(addressed, text)` verdict.
- [x] Stamp the turn with its lane at append time, once, at the point of the gate decision (NFR2).
- [x] `store.turns_since` / `store.watch` filter by lane the way they already filter by `addressed_only`,
      with the same rule that a skipped turn still advances the cursor.
- [x] `watch --lane`; the single-waiter guard keyed on `(session, lane)` (TC7).
- [x] `say --lane`; refuse a foreign lane with `off_lane`.
- [x] Live lane published in `/status` and broadcast to clients on change.
- [x] `watch` returns on a lane change with `reason: "lane"` and the new `live_lane`.
- [x] Tell somebody about an ambiguous refusal — see *the refusal is announced by the agent, not by a
      sound* below. The wait of the LIVE lane returns with `reason: "ambiguous"` and the candidates.
- [x] `describe` updated in the same commit (convention 3); `ai-docs/reference/turn-log.md` updated with the
      `lane` field and the absent-means-default rule.

### Slice B — lanes UI (FR6, FR7, FR8) — does not begin until AC-16 passes

- [x] Confirm the hold delivers (AC-16), per TC3. **Verified by mutation, not only by a green run:**
      reintroducing the exact TC3 bug (barge-in clearing the hold) and separately making the flush a
      no-op each turn the guards red. The first attempt at the barge-in test SIMULATED the barge by
      clearing the queue directly, and the mutation showed it stayed green against the real bug —
      it now drives `_maybe_barge` itself.
- [x] A per-lane hold store, flushed when its lane becomes live, not cleared by barge-in.
- [ ] The lane strip in `web/index.html`: one row per lane, the live one marked, a tap switches it.
- [ ] The "has something to say" mark on a held lane.
- [ ] Per-lane agent state (thinking / transcribing / synthesizing / speaking).

## Acceptance Criteria

Every criterion names its validation method. `corpus` means replayed against the recorded turn logs in
`sessions/*.jsonl` — this repo's habit of measuring rather than asserting, made into a test method.

### Slice A — routing

- [x] **AC-1** `unit` — **TC1, TC2.** The TC2 table is a pure function of `(token, lanes, current)`. All five rows are
      covered, including both tie cases, and it returns one of `switch` / `stay` / `refuse` and never
      raises on an empty or non-ASCII token.
- [x] **AC-2** `corpus` — **NFR3, the regression guard.** Every turn in `sessions/*.jsonl` replayed through
      the new gate with a single registered lane produces a byte-identical `(addressed, reason)` verdict to
      today's. Any difference fails.
- [x] **AC-3** `corpus` — **FR2, TC1.** With lane set `{claude, codex}`, replaying the corpus produces **zero** switches to
      a lane the token did not name exactly. The mis-route count is asserted at 0, not merely reported.
- [x] **AC-4** `unit` — `"hey codex run the tests"` yields ONE turn stamped `lane: "codex"` whose text is
      unmodified and still contains the wake phrase (FR3, and the existing never-strip rule).
- [x] **AC-5** `unit` — **FR2, NFR2.** A turn with no wake phrase, inside the conversation window, is stamped with the
      **current** lane and does not switch it (stickiness).
- [x] **AC-6** `unit` — **TC2.** An ambiguous token yields `addressed: false`, `lane: null`, and a `reason` beginning
      `ambiguous:` that names the candidates. It is returned to **no** lane's `watch`.
- [x] **AC-7** `integration` — **FR4, FR5, TC6.** `watch --lane codex` returns codex turns and `everyone` turns, never a
      `claude` turn, and the cursor advances past the turns it filtered out (the existing skipped-turns-are-
      consumed rule).
- [x] **AC-8** `integration` — **FR4, TC6.** A turn with **no** `lane` field is returned to the default lane's watch and to
      no other, proving the 1,866 turns already on disk keep working.
- [x] **AC-9** `integration` — Two waits on the SAME `(session, lane)`: the second is refused with
      `watch_open`. Two waits on DIFFERENT lanes of the same session: **both run** (TC7).
- [x] **AC-10** `integration` — **FR9.** `say --lane <not live>` **does not play.** ⚠ **This criterion was
      deliberately REVERSED between the slices, and the reversal is the point rather than a correction.**
      Slice A refused outright (`code: off_lane`, nothing synthesized), which was right while there was
      nowhere safe to put the audio. Slice B built the hold, and he asked for that in as many words —
      *"what it is saying would be queued up"*. A refusal makes the waiting agent's answer HIS problem to
      ask for again. What survives unchanged is the guarantee that matters and it is asserted on the wire:
      **nothing reaches the client while another lane is live.** `say --lane <unknown>` still refuses with
      `unknown_lane`, since that is a caller error rather than bad timing.
- [x] **AC-11** `integration` — **FR1, FR9.** A lane switch returns an open `watch` on the lane that just lost the
      conversation, with `reason: "lane"` and the new `live_lane`.
- [x] **AC-12** `unit` — **FR1, TC8.** `lane add everyone` is refused with `lane_exists`, as is a duplicate
      and a name that is not a single word token. **`lane add codex` alongside `claude` SUCCEEDS** — the
      pair scores 0.55 against each other and must not be treated as a collision, which is the specific
      regression this criterion exists to prevent.
- [x] **AC-13** `unit` — **NFR1.** Lane resolution is a pure function of state already in memory:
      it takes the lane set and the live lane as arguments, performs no I/O, and is called in the same
      pass as the wake verdict. Asserted by calling it with no server, no socket and no session directory.
- [x] **AC-14** `unit` — `describe` lists `lane`, every new flag, and all four new error codes, and the
      existing describe-agrees-with-itself test passes unchanged.
- [x] **AC-15** `unit` — The full existing suite still passes. **Baseline 1,083 passed / 2 skipped; now
      1,172 passed / 3 skipped** (89 added; the extra skip is the worktree having no piper voice on disk).
      No test deleted. Four were TOUCHED and each is recorded in *Findings — tests this spec changed*;
      none was weakened.

### Slice B — the UI and the hold

- [x] **AC-16** `integration` — **The TC3 gate, and Slice B does not start until it passes.** A clip issued
      to an off-lane agent is held, and **delivers in full when that lane becomes live** — asserted on the
      bytes arriving, not on `queued: true`. Repeated with a barge-in between the hold and the switch: the
      held clip **still delivers** (**FR7, TC4**), because barge-in silences the live lane and must not discard another
      lane's pending speech.
- [ ] **AC-17** `harness:scripts/devicepills.py` — **FR6.** The lane strip does not break the device pickers. This is
      the one page harness that starts no server and is safe to run during a live session.
- [ ] **AC-18** `harness:scripts/layout.py` — **FR6.** The lane strip fits at all five viewports and the newest
      transcript row is still on screen. ⚠ Requires no live session.
- [ ] **AC-19** `harness:scripts/orbstate.py` — **FR8.** Per-lane state flows through the pure reducer; the golden
      snapshot is re-blessed deliberately and the diff is read, not accepted blind.
- [ ] **AC-20** `manual` — **FR6, FR7.** Two agents, one phone, one session: he switches by voice and by tap, both are
      heard, and the off-lane one is visibly waiting. **Irreducibly manual** — it needs a real microphone, a
      real phone and two real agents, which is the one thing no harness in this repo can produce.

## Testing Approach

### Validation steps

1. `venv/Scripts/python -m pytest tests/ -q` — must stay at 1,083 passed / 2 skipped plus the new tests.
2. The `corpus` tests read `sessions/*.jsonl` read-only. They must **skip, not fail**, when the corpus is
   absent, so a fresh clone and CI stay green — the corpus is JJ's speech and is not in the repo.
3. Slice B harnesses only when no live session is running (`voice-tunnel status` exits 3).

### Test cases

| Input (lane set `{claude, codex}`, live `claude`) | Expected |
|---|---|
| `"hey codex run the tests"` | switch to `codex`; turn stamped `codex` |
| `"hey claude run the tests"` | stay `claude`; turn stamped `claude` |
| `"hey can you run the tests"` | stay `claude` (`can` scores 0.44 — below threshold) |
| `"hey cloud run the tests"` | stay `claude` (`cloud`→claude 0.73 is the strict top scorer and IS current) |
| `"hey cloud run the tests"`, live `codex` | **refuse**; `lane: null`, `reason: "ambiguous:claude"` |
| `"run the tests"` inside the window | stay `claude` |
| `"hey everyone stand down"` | `lane: "everyone"`; returned to every lane's watch |
| a turn on disk with no `lane` field | returned to the default lane only |

## Out of Scope

- **Routing work between agents, monitoring them, or steering them.** That is
  Voice Tunnel's written anti-goal and is already served by `sb sessions`, `agent-mail` and
  `ccpulse`. Lanes carry *speech*, nothing else.
- **Inferring who a turn is for.** See TC1.
- **A distinct voice per agent.** `say --voice` already exists; whether each lane pins one is a separate,
  cheaper question.
- **Fixing the closed-channel clip loss.** Tracked in Voice Tunnel; this spec must not build on it
  (TC3), but it does not fix it either. The mechanism identified in *Findings — TC3* is recorded there for
  whoever picks that up, and is used here only to keep the lane hold clear of it.
- **Improving the recognizer's rendering of a name.** The exact-match rate is measured and used; raising it
  is a different piece of work.

## References

- Voice Tunnel — the roadmap row, the North Star and anti-goals, and the "One voice or many?"
  open question this spec closes.
- `specs/007` — the refusal machinery and the loop the tool now enforces; FR9 extends its surface.
- `specs/005` — the one wait gated on speech, which `watch --lane` must not break.
- Voice Tunnel Guide — the standing rules for how an agent behaves in a session, all of which become
  per-lane rules.
- `ai-docs/reference/turn-log.md` — the cursor contract the `lane` filter extends.
