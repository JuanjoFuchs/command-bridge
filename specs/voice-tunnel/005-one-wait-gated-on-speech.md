---
id: "005"
title: One wait, gated on speech
status: complete
blocked_by: []
blocks: []
---

# One Wait, Gated on Speech

## Overview

The tunnel currently makes an agent choose between two waiting commands — `watch`, which backs off
30s → 1m → 2m → 4m → 8m → 9m, and `drain`, which collapses 5 → 3 → 2. **Both are clocks standing in
for a signal the server already publishes.** `status` reports `user_speaking` (client mic level) and
`speech_active` (server segmenter) live, so every rung is a guess at silence when the answer is one
field away.

The cost is measured, not theoretical. The owner named it live on 2026-08-17 after feeling it:
*"after I finished speaking you do a long wait of a watch… I thought we were no longer doing those
waits."* In the same session an agent used `watch` where `drain` belonged — twice — paying a
30-second rung each time to discover nothing had arrived.

This spec replaces the timers with the signal, and collapses the two commands into one. It also
addresses a second defect found the same day: the turn model closing an utterance early and dropping
the tail of a sentence.

> **Completion rule:** This spec is not complete until all acceptance criteria are verified through
> the repo's `pytest` suite plus the browser harnesses in `scripts/` (`channel.py`, `orbstate.py`)
> and a timing measurement from a real session log. Build-only verification is insufficient. The
> agent must iterate until verification passes.

## Goals

- Waiting costs nothing once he has stopped talking, and never cuts across him while he is talking.
- One waiting command instead of two, so the choice that agents get wrong stops existing.
- A muted microphone reads as *not speaking*, at the source.
- The turn model stops truncating him mid-sentence.

## Requirements

### Functional Requirements

- **FR1**: A muted client must report `speech_active` false. Frames stopping means speech stopped;
  today the flag sticks true indefinitely because nothing arrives to close the utterance.
- **FR2**: The wait holds while the speaking signals indicate he is talking, and returns as soon as
  they indicate he is not. No floor, no rungs, no backoff ladder in the speaking case.
- **FR2a** *(added by the implementer after a live regression — see Findings)*: When the agent is
  holding turns it has not yet answered, a wait with nothing to report returns **immediately**,
  with no rung and no `next_wait`. *"the watch was going to hold if the client detected that I was
  sending audio. And if not, it was going to resolve immediately."*
- **FR3**: One command performs the wait, and it is **`watch`** — the name that already existed,
  made smart enough that `drain` is unnecessary. `drain` continues to work as a deprecated alias
  for one release. The exact invocation the scheduled watchdog emits keeps working, which with
  `watch` as the primary name is simply correct rather than a concession. *(Refined from the
  strategist's wording after his ruling — see Key Decisions.)*
- **FR4**: `describe` states the new behaviour, and the watchdog prompt it generates emits the new
  command form.
- **FR5**: The turn model's confidence threshold is tunable without a code change, and its default
  is set from a measurement rather than inherited from upstream.
- **FR6** *(his, 2026-08-17)*: `say` returns anything he said that the agent never read. *"the say
  command resolves, it should include in its response whatever I said that wasn't drained
  before."* This makes the spec's own invariant — *no speech may be pending when you speak* —
  **structural rather than remembered**: until now it depended on an agent choosing to run the
  wait first, which is a discipline, and one violated repeatedly in live sessions.

### Non-Functional Requirements

- **NFR1**: The wait exposes no tuning flags. Every option is a decision an agent makes wrong under
  time pressure, and adding one to configure this wait would recreate the defect being removed.
- **NFR2**: Latency between the owner finishing an utterance and the wait returning is bounded by
  the segmenter's own end-of-utterance delay, with nothing added on top.

### Technical Constraints

- **TC1**: `user_speaking` and `speech_active` lag differently, which is why `drain` reads both
  today. **Returning on the first `false` from either signal will cut him off mid-sentence.**
- **TC2**: The disconnected case is already solved and must not regress — with no client connected,
  the wait is flat at 8 hours rather than laddering, because a page that is not open cannot produce
  a turn. See `WATCH_DISCONNECTED_MAX_S`.
- **TC3**: An explicit caller-supplied timeout must keep meaning exactly what it means today.
- **TC4**: Turn detection must not simply be disabled. The fallback is a fixed 1500 ms timer that
  was *raised from 1000 because it kept cutting him off* — a known-worse failure of the same kind.

### Requirement Traceability

| Requirement | Acceptance Criteria |
|---|---|
| FR1 | AC1, AC2, AC3, AC4 |
| FR2 | AC5, AC6, AC7, AC8 |
| FR2a | AC23, AC24, AC25, AC26 |
| FR3 | AC9, AC10, AC11, AC12 |
| FR4 | AC13, AC14, AC15 |
| FR5 | AC16, AC17, AC18 |
| FR6 | AC27, AC28, AC29, AC30, AC31 |
| NFR1 | AC12, AC19 |
| NFR2 | AC7, AC8 |
| TC1 | AC5, AC6, AC7 |
| TC2 | AC20 |
| TC3 | AC21 |
| TC4 | AC18 |

## Key Decisions

- **Fix the flag, do not route around it.** FR1 comes first and everything else depends on it. Under
  a speaking-gated wait, a signal that lies about muting does not merely mis-narrate — it hangs the
  tunnel, because the wait never observes him stopping. The owner made this call directly:
  *"It seems that we should fix the flag, right?"*
- **The two commands were never two jobs.** They are one job with two hard-coded wait strategies,
  and the distinction dissolves once the gate is on speech rather than on a clock.
- **A tool that cannot be used wrongly beats a rule an agent must remember.** Four sections of the
  Voice Tunnel Guide exist only to teach the `watch`/`drain` distinction; most of that prose
  deletes itself when there is one command. This is the same principle as encoding the guide into
  `describe`.
- **The invariant to build against:** *no speech may be pending when you speak.* Everything else —
  rungs, thresholds, which command — is implementation detail judged against that one sentence.

### The command is `watch`. There is no third name — his ruling, 2026-08-17

> *"I don't like that we have had a watch command, then a drain command, and now we have, I think,
> a wait command. I only want to have one watch command that is smart and does all the things that
> it's supposed to do, right? I never meant it to be three different commands."*

**This overturns an implementer decision, and the reasoning behind the reversal is the useful
part.** FR3 says `watch` and `drain` both survive as aliases, which was read as implying the
surviving command needs a third name; `wait` was chosen because it states the contract — it
blocks — where `watch` suggests observation that might be passive.

That reasoning was sound and it solved the wrong problem. **He never asked for a rename.** He
asked for `watch` to become smart enough that `drain` was unnecessary. A new name is a new thing
to learn and it arrives while the old names are still running, so introducing one made the surface
*larger* at the exact moment the goal was to shrink it. He counted the result and got three
commands.

So: **`watch` is the command**, `drain` is the single deprecated alias for one release, and `wait`
does not exist as a public name at all — not in the parser, not in `--help`, not in `describe`,
not in any `next` string. That last one matters most: **a tool that keeps emitting a name is
teaching it**, which is exactly how `--waits 5,3,2` outlived the ladder it configured.

It also dissolves the watchdog question. A live scheduled job invokes
`voice-tunnel watch --session dev --since <cursor>` every minute; with `watch` as the primary name
that is no longer a compatibility concession, it is simply correct.

### One rule governs every return

> **The wait returns only at a moment when he is not speaking.**
> What it returns — turns, a control change, or an empty heartbeat — depends on what happened.

That single sentence replaces both ladders. It is the invariant above, made into the command's
only rule, and it is what lets NFR1 hold: there is nothing left to configure, because there is no
schedule — only a condition.

### `speech_active` may END the wait; `user_speaking` may only EXTEND it — the answer to TC1

TC1 is right that the naive reading is fatal, and the reason is sharper than "they lag":
**the two signals are not two opinions to be combined. They are the trailing and leading edges of
the same fact.**

- `speech_active` is the server's segmenter. It goes false only after `END_OF_UTTERANCE_MS` of
  continuous silence **and** the turn model agreeing he sounded finished (spec 004 pushes the
  trailing-silence counter back when the model says *incomplete*). So it already encodes the
  project's best available answer to "has he finished". It is late, and it is authoritative.
- `user_speaking` is the client reading its own microphone level, with a 120 ms minimum run and a
  700 ms hangover. It goes true within ~120 ms of him starting. It also goes false during any gap
  longer than the hangover, **including gaps inside a sentence**. It is early, and it is noisy.

Therefore: a return authorised by `user_speaking` going false is a return authorised by a breath.
A return authorised by `speech_active` going false is a return authorised by the segmenter. The
implementation keeps the existing additive OR — hold while *either* is true — and the asymmetry
above is what that OR means, stated so the next agent cannot "simplify" it into a first-false test.

**Adding a confirmation window after `speech_active` drops was considered and rejected**, because
NFR2 forbids it and because it would re-derive, worse, the 1500 ms the segmenter has already spent.

### The rungs were buying certainty the agent's own latency already provides — measured

This is the load-bearing evidence for deleting the ladder rather than shortening it. Measured on
the live 2026-08-17 session (see *Findings*):

| | |
|---|---|
| When he continues after a turn closes, he resumes within | **10 s in 100% of cases** (median 0.52 s, p90 4.14 s, max 5.98 s) |
| The agent's own `consumed → say_requested` on the same day | **≥15 s in 100% of turns** (p10 20.6 s, median 29.0 s) |

**The two distributions do not overlap.** The fastest reply ever observed is more than three times
the longest resumption ever observed. So by the moment the agent is ready to speak, every
continuation has already landed in the log as a turn — and running `wait` again immediately before
`say` returns it. The collapsing ladder was spending up to 10.5 s of *his* time re-deriving a fact
the next command observes for free.

This is also why the owner's two demands are not in tension. *"Do not wait long after I speak to
begin thinking"* is served by returning the instant `speech_active` is false; *"if I'm speaking, do
not cut over me"* is served by the pre-`say` wait, by the server's own hold loop in `_speak`, and
by the barge-in gate. **The wait gates speaking, not starting.**

### The idle ladder survives; the speaking ladder does not

FR2 removes rungs *in the speaking case*. With nothing happening at all there is no speech signal to
gate on, so the existing backoff (TC2's flat disconnected ceiling, the 30 s-doubling ladder when
connected and quiet, an explicit `--timeout` honoured exactly) is kept unchanged. Those rungs pace a
*heartbeat*; they never decide whether he has finished.

### `--timeout` is not a tuning flag

NFR1 bans options that configure the wait; TC3 requires `--timeout` to keep its meaning. These do
not conflict: `--timeout` expresses a fact about the **caller's harness** (its maximum tool timeout),
not a preference about the **gate**. Nothing an agent can pass changes when the wait decides he has
stopped talking. `--waits` and `--max-seconds` are the flags that did configure the gate, and they
are gone from `wait` — accepted-and-reported-ignored on the `drain` alias only, so a literal
invocation already in circulation does not crash.

### FR1 is fixed in two halves, because there are two failure shapes

- **Events flush.** Every event that stops frames — mute, orb off / capture released, channel close,
  socket drop — flushes the utterance buffer, so nothing said before it is silently eaten. Mute and
  disconnect already did this; capture-off and channel-close did not.
- **The published flag is guarded on frame recency.** For everything nobody told us about — frames
  in flight arriving after a mute, an Android tab suspended in the background, a wedged page holding
  a live socket — `speech_active` and `user_speaking` are published false once no audio frame has
  arrived for `END_OF_UTTERANCE_MS`. That window is not a new constant: it is exactly the silence
  that would have closed the utterance had frames kept coming.

Both halves are needed. The first alone leaves races; the second alone loses audio.

### `speech_pending` — the invariant made observable

There is a window in which both speech signals are false and he has nevertheless just spoken: the
utterance has closed and the turn has not been transcribed yet. Measured on 2026-08-05 at ~1–2 s,
and ~13 s on long dictation. A wait that returned there would hand back nothing while he waited for
an answer.

So the server publishes `speech_pending` — utterances closed and not yet logged — and the wait
treats it exactly like a speech signal. The owner's own words are the specification:
*"the gist is making sure that there's any speech drained before you speak."* A count of pending
speech is that sentence as a field.

### FR5: the threshold is NOT the lever — measured and falsified

Voice Tunnel proposed raising `TURN_THRESHOLD` as *"the cheap lever"*. **Measurement on the
session that produced the complaint says it is not a lever at all** (full numbers in *Findings*):

- Every truncation observed came from the `model-early` early-exit path. `model-complete`,
  `model-exhausted` and `too-long` produced none.
- The smart-turn probabilities at the five truncations were **0.480, 0.869, 0.965, 0.969, 0.977** —
  **median 0.965, above the 0.931 median of the early exits that were correct.** The model is not
  marginally confident and wrong; it is confidently wrong, and confidence does not rank truncation.
- The sweep is therefore perverse: no threshold removes the truncations faster than it removes the
  good closes. Dropping four of five needs 0.98, which keeps 3 of 48 early exits (94% lost);
  dropping all five needs 0.985, which keeps none — i.e. it is disabling the early exit while
  pretending to tune it.

**The lever that does work is `TURN_MIN_SILENCE_MS`**, the floor below which no amount of confidence
may close a turn. All five truncations closed at a measured trailing silence of **0.44–0.48 s** —
the first opportunity the 400 ms floor allows — while the early-exit population is bimodal at
~0.46 s and ~1.04 s, exactly reproducing the code's own `TURN_INCOMPLETE_DELAY_MS` rate limiter.
Raising the floor is flat over **600–900 ms** (12 of 48 early exits kept at every value in that
band), so the default becomes **800 ms**: the middle of the measured plateau rather than its edge.

This satisfies FR5 as written — the number is set from a measurement rather than inherited from
HuggingFace — and corrects which number FR5 should have named. **`TURN_THRESHOLD` keeps its 0.5
default**, because no measured value of it is better, and it becomes settable so the finding can be
re-tested rather than re-argued. TC4 is respected: turn detection stays on, the extension half is
untouched, and the timer is not restored.

## Implementation Tasks

- [x] `speech_active` and `user_speaking` are published from one place on the server state, false
      when muted, when nothing is capturing, when no client is connected, or when no audio frame has
      arrived for `END_OF_UTTERANCE_MS`.
- [x] Flush the utterance buffer when capture is released and when the channel closes, matching what
      mute and disconnect already do.
- [x] Publish `speech_pending` — utterances closed and not yet logged — and reset `user_speaking`
      when the last page goes away.
- [x] `wait` command: one blocking call that returns only at a quiet moment, with the idle ladder,
      the disconnected ceiling and an explicit `--timeout` preserved.
- [x] `watch` and `drain` as aliases dispatching to the same implementation; `drain`'s `--waits` and
      `--max-seconds` accepted and reported as ignored rather than silently honoured.
- [x] `describe`: `wait` as the canonical command entry, `watch`/`drain` sharing its documentation
      by reference so the two cannot drift; `RULE_1`/`RULE_3` and `the_loop` rewritten; every `next`
      string emitting `wait`.
- [x] `WATCHDOG_PROMPT` emits `voice-tunnel watch`, which is also the literal a scheduled job is
      already running — so the migration is a no-op for it.
- [x] `TURN_MIN_SILENCE_MS` default 400 → 800, both it and `TURN_THRESHOLD` readable from the
      environment and registered in `config.SETTINGS`, `.env.example` and `describe`'s env block.
- [x] Publish `agent_holds_turns` — derived from delivery, speech and an empty check — so the
      pre-reply position answers immediately without the caller having to say which position it
      is in.
- [x] A test that drives `main()` for every subcommand, so a renamed handler cannot leave the
      dispatch table pointing at nothing again.
- [x] `scripts/channel.py` pushes real PCM alongside the speaking signal, via a new
      `__voiceTunnel.pushAudio(ms)` hook on the page.
- [x] `say` returns `unread` / `unread_count` / `cursor` on both the blocking and the
      fire-and-forget path, without advancing the read cursor.
- [x] `held_for_speech` published, so the pre-existing always-firing hold warning branches on
      the fact rather than on a number that is never zero.

## Acceptance Criteria

### FR1 — a stopped microphone reads as not speaking

- [x] **AC1**: With the client muted, `status.speech_active` and `status.user_speaking` are both
      false even though the buffer held an open utterance when the mute landed. `unit`
- [x] **AC2**: With no audio frame for longer than `END_OF_UTTERANCE_MS`, both signals read false
      regardless of what the buffer or the last client message said. `unit`
- [x] **AC3**: Releasing capture (orb off) and closing the channel each flush the buffer, so words
      spoken immediately before are still logged as a turn rather than discarded. `unit`
- [x] **AC4**: With no client connected, both signals read false and `speech_pending` is 0. `unit`

### FR2 / TC1 — the gate

- [x] **AC5**: `user_speaking` true with `speech_active` false does NOT end the wait — the leading
      signal may only extend. `unit`
- [x] **AC6**: `speech_active` true with `user_speaking` false does NOT end the wait. `unit`
- [x] **AC7**: With turns already collected, the wait returns on the first poll at which both
      signals are false and `speech_pending` is 0 — no rung, no grace, nothing added. `unit`
- [x] **AC8**: `speech_pending` non-zero holds the wait even when both speech signals are false, so
      a turn still in transcription is never missed. `unit`

### FR3 — one command, two aliases

- [x] **AC9**: `voice-tunnel watch --session <s> --since <c>` performs the wait, and `wait` is
      not a command at all. `integration`
- [x] **AC10**: `watch` and `drain` dispatch to the same implementation — asserted by identity of
      the handler, not by comparing behaviour. `unit`
- [x] **AC11**: The exact invocation the currently-scheduled watchdog emits,
      `voice-tunnel watch --session dev --since <cursor>`, parses and runs. `integration`
- [x] **AC12**: `wait --help` offers no `--waits` and no `--max-seconds`; `drain` still accepts both
      and reports them in `ignored` rather than honouring them. `unit`

### FR4 — describe is the contract

- [x] **AC13**: `describe` documents `wait`; `watch` and `drain` carry `alias_of: "wait"` and share
      wait's `args`/`returns`/`notes` object rather than copies of it. `unit`
- [x] **AC14**: The generated watchdog prompt contains `voice-tunnel watch`, and neither
      `voice-tunnel drain` nor `voice-tunnel wait`. `unit`
- [x] **AC15**: No `next` string, `the_loop` entry, or RULE in `describe` instructs the reader to
      choose between two waiting commands. `unit`

### FR5 — the turn model

- [x] **AC16**: `VOICE_TUNNEL_TURN_MIN_SILENCE_MS` and `VOICE_TUNNEL_TURN_THRESHOLD` are readable
      from the environment, registered in `config.SETTINGS`, documented in `.env.example`, and
      settable with `voice-tunnel config set`. `unit`
- [x] **AC17**: `TURN_MIN_SILENCE_MS` defaults to 800, and no utterance closes early on less
      trailing silence than that. `unit`
- [x] **AC18**: Turn detection remains enabled by default and the extension path is unchanged — a
      detector saying *incomplete* still extends the wait, bounded by `TURN_MAX_WAIT_MS`. `unit`

### FR2a — the pre-reply check has no ladder either

- [x] **AC23**: With a client connected, him not speaking, no turns since the cursor, and the agent
      holding unanswered turns, the wait returns in **under 100 ms** and its payload carries no
      `next_wait`. `unit` + `integration` against a live server
- [x] **AC24**: In that same position, if he IS speaking the wait still holds until he stops.
      `unit`
- [x] **AC25**: With the agent holding nothing — the listening position — the wait still blocks on
      the idle ladder rather than returning immediately, so the loop RULE_1 requires cannot become
      a hot spin. `unit`
- [x] **AC26**: A pre-reply check that comes back empty ends the batch, so the next call blocks;
      one that comes back with turns does not, so "fold them in and wait again" stays instant. And
      neither advances the idle backoff streak. `unit`

### FR6 — speaking hands back what was never read

- [x] **AC27**: A `say` made while unread turns exist returns them in `unread`, with
      `unread_count` and a `cursor` to resume from. `unit` + `integration` against a live server
- [x] **AC28**: It does **not** advance the read cursor — the next `watch` returns the same turns,
      so an agent that ignores the field loses nothing. `unit`
- [x] **AC29**: `--now` carries them too, because that is the path an agent takes when it is in a
      hurry, which is when the check gets skipped. `unit`
- [x] **AC30**: Turns the wake gate rejected are not handed back, and the list is bounded so a
      reply cannot carry the whole log. `unit`
- [x] **AC31**: `held_for_speech` distinguishes a hold caused by him talking from the grace pass
      that runs on every reply, and the `next` string branches on it rather than on
      `held_for > 0`. `unit` + `integration`

### Not-regressions

- [x] **AC19**: The wait has no flag that changes when it decides he has stopped talking. `unit`
- [x] **AC20**: With no client connected the wait is flat at `WATCH_DISCONNECTED_MAX_S`, not
      laddered. `unit`
- [x] **AC21**: An explicit `--timeout` is honoured exactly and still disables both the ladder and
      the disconnected ceiling. `unit`
- [x] **AC22**: The full `pytest` suite passes, and `scripts/layout.py`, `scripts/orbstate.py` and
      `scripts/channel.py` pass on their own ports. `integration`

## Testing Approach

### Validation Steps

1. `python -m pytest tests/ -q` — the whole suite, not only the new files. The wait is reached by
   `test_drain.py`, `test_watch_backoff.py`, `test_watch_single.py`, `test_cli_surface.py` and
   `test_describe_agrees_with_itself.py`, and those are the regression surface.
2. `python scripts/orbstate.py` and `python scripts/channel.py` — the orb and the controls are
   driven through a real socket, which is the only thing that exercises `capturing`, `muted` and
   `channel_open` end to end. **Both spawn a server; they use ports 8801 and 8799, never 8765.**
3. `python scripts/layout.py` — geometry, unaffected in principle and run because the page changed.
4. `python scripts/e2e.py` — failed at HEAD for two pre-existing reasons. FR5 fixed the first (see
   Findings); the second is a stale assertion about the mute button and is deliberately left.

### Test Cases

| Input | Expected |
|---|---|
| muted, buffer mid-utterance | `speech_active` false, `user_speaking` false |
| last frame 2 s ago, nothing reported | both signals false |
| capture released mid-sentence | buffer flushed, the words become a turn |
| `user_speaking` true, `speech_active` false | wait holds |
| `user_speaking` false, `speech_active` true | wait holds |
| both false, `speech_pending` 1 | wait holds |
| both false, `speech_pending` 0, turns collected | wait returns immediately, `finished: true` |
| agent holds unanswered turns, he is quiet, nothing new | returns in <100 ms, no `next_wait` |
| agent holds nothing, he is quiet, nothing new | blocks on the idle ladder |
| an empty pre-reply check, then another wait | the second one blocks |
| `say` with two unread turns | `unread_count: 2`, `next` says READ THEM NOW |
| `say --now` with unread turns | same — it is sampled before synthesis |
| `say` on a clean reply | `unread_count: 0`, `held_for_speech: false`, no alarm |
| read `unread`, then `watch` | the same turns come back; the cursor never moved |
| speaking continuously past the safety ceiling | returns `reason: "ceiling"`, `finished: false` |
| nobody connected, quiet | one flat 8 h wait, not a ladder |
| `--timeout 480` | waits exactly 480 s, no ladder, no 8 h |
| `drain --waits 5,3,2` | runs, `ignored: ["--waits"]` in the payload |
| `watch --session dev --since 3` | runs (the scheduled watchdog's literal) |
| trailing silence 0.5 s, model says complete | turn does NOT close (below the 800 ms floor) |
| trailing silence 0.9 s, model says complete | turn closes early |
| model says incomplete | wait extends, bounded by `TURN_MAX_WAIT_MS` |

### Why the browser harnesses and not only unit tests

`speech_active`, `user_speaking`, `muted` and `capturing` all cross the socket, and every previous
defect in this area was invisible to the unit suite: a mute that left the flag stuck true, an orb
that said "Listening" over a released microphone, a `ScriptProcessorNode` that sent nothing while
the page looked healthy. `channel.py` is the only thing in the repo that touches those controls
through a real page.

## Usage Examples

```bash
# The loop, entire. ONE command in both positions — that is the whole point.
voice-tunnel serve --session dev --wake claude      # detached
voice-tunnel status --session dev                   # hand the user `url`
voice-tunnel watch --session dev --since -1         # BLOCKS until he has spoken and stopped
  # ... do the work his turn asks for ...
voice-tunnel watch --session dev --since 42         # immediately before speaking; returns at once
                                                    # if he is quiet, holds if he is talking
voice-tunnel say   --session dev "All green."
  # ^ and if he said anything you never read, THIS hands it back — `unread`, `unread_count`,
  #   `cursor`. "No speech may be pending when you speak" stops depending on memory.
voice-tunnel watch --session dev --since 43         # back to listening

# The one deprecated alias, for one release. Same implementation, same payload.
voice-tunnel drain --session dev --since 42         # `--waits` / `--max-seconds` accepted, ignored
```

A quiet return, mid-conversation:

```json
{
  "turns": [{"id": 43, "text": "what is the status of the deploy?", "addressed": true}],
  "cursor": 43, "count": 1,
  "finished": true, "reason": "turns",
  "user_speaking": false, "speech_active": false, "speech_pending": 0,
  "elapsed_s": 2.4,
  "next": "do the work his turn asks for FIRST, then run `voice-tunnel watch ...` immediately before any say"
}
```

## Findings — implementer

### The corpus, and how it was made trustworthy

Measurements are from session `dev`, the live conversation of 2026-08-17 — the one that produced
this spec. Two things had to be established before any number could be believed.

**`sessions/dev.wav` is not the session.** `TunnelState.close_capture()` runs in the websocket
handler's `finally`, and `capture()` reopens the file with mode `"wb"` on the next frame — so the
failsafe capture holds only the audio since the **last reconnect**, while `t_start`/`t_end` count
from server start. The two differ by a constant. This is worth recording on its own: the capture
exists so *"the ASR is bad"* can be turned into a question you answer by listening, and it silently
discards everything before the most recent reconnect. Out of scope here; not a hypothetical.

**The first two alignment attempts were wrong and their numbers were discarded.** Slicing by
`t_start`/`t_end` directly produced probabilities with a plausible-looking distribution that was
pure noise — the confirmation transcription of those slices matched the logged text 0.00. A
speech-mask cross-correlation then produced a clean 34.7× peak that was also wrong (match 0.02),
because the mask was built from turns spanning several server runs. **A confident peak is not a
correct one**; the recognizer is what caught both.

The offset was finally pinned at **5269.14 s** and validated against a property the code itself
guarantees rather than against anything this pass chose: `UtteranceBuffer` may close on
`model-early` only while trailing silence is inside `[TURN_MIN_SILENCE_MS, END_OF_UTTERANCE_MS)`,
and on the timer or `model-complete` only at or beyond `END_OF_UTTERANCE_MS`. At the chosen offset,
**98% of 56 closes land inside the window the code enforces**, and the per-reason bands fall out
exactly:

| end_reason | n | trailing silence at close (measured) |
|---|---:|---|
| `model-early` | 48 | min 0.20 s · **median 0.46 s** · max 1.08 s |
| `model-complete` | 3 | min 1.50 s · median 2.14 s · max 2.78 s |
| `model-exhausted` | 5 | 3.34 – 3.36 s |

Corpus: 56 turns, ids 771–826, 13:16:59 – 13:52:35 on 2026-08-17. It begins at the reconnect that
happened at the moment of the reported incident, so turn 771 is literally *"So something weird just
happened. I was still speaking and the transcription started and finished and it did not catch the
last sentence of what I was saying."*

### The timer is dead; the model is the segmenter

From the timing log alone — no audio, no alignment, exact — over all 139 turns logged on
2026-08-17:

| end_reason | count | share |
|---|---:|---:|
| `model-early` | 108 | **77.7%** |
| `model-exhausted` | 18 | 12.9% |
| `model-complete` | 13 | 9.4% |
| `timer` | 0 | **0.0%** |

`turn_model_ms`: median 66.7 ms, p90 85.9 ms, max 179.6 ms — inside NFR3 of spec 004.

**`END_OF_UTTERANCE_MS` never decides anything any more.** Spec 004 shipped the model as an
adjustment to a timer; in practice the model closes every single turn, and 78% of them through the
early-exit path that spec 004 itself identified as *"where the risk is"*. That reframes TC4: the
fallback timer is not a safety net that occasionally catches things, it is unreachable in normal
operation.

And the early exit is where every truncation lives. Over all 812 turns in the log, the share of
turns whose text ends without terminal punctuation **and** which he resumed within 2 s:

| end_reason | n | truncation-shaped |
|---|---:|---:|
| `model-early` | 631 | 38 (**6.0%**) |
| `model-exhausted` | 82 | 1 (1.2%) |
| `model-complete` | 84 | 0 (0.0%) |
| `too-long` | 15 | 0 (0.0%) |

### The threshold cannot separate a truncation from a correct close

Smart-turn probabilities recovered by re-running the model over the aligned audio at each closure
(48 early exits, 5 truncation-shaped):

| | probability |
|---|---|
| `model-early`, all 48 | min 0.288 · p10 0.578 · **median 0.931** · p90 0.977 · max 0.985 |
| the five truncations | 0.480 · 0.869 · 0.965 · 0.969 · 0.977 — **median 0.965** |

**The truncations score above the median correct early exit.** Sweep:

| threshold | early exits kept | truncations kept | correct `model-complete` lost |
|---:|---:|---:|---:|
| 0.50 (today) | 44 / 48 | 4 / 5 | 1 |
| 0.90 | 29 | 3 | 3 |
| 0.97 | 9 | 1 | 3 |
| 0.98 | 3 | 0 | 3 |
| 0.985 | **0** | 0 | 3 |

There is no value that keeps the benefit and drops the harm. **The hypothesis recorded in
Voice Tunnel — that the threshold is "the cheap lever" — is falsified.** It was a reasonable
guess from the outside: it assumed the model was hesitant at the boundary. It is not; it is certain
and wrong, which is a different defect and needs a different instrument.

*Caveat kept honest:* the model is re-run on the emitted turn's audio, which is the buffer with its
leading silence trimmed, whereas the live call saw the untrimmed buffer. The model reads only the
last 8 s, so the two coincide for every utterance shorter than that; the handful scoring below 0.5
despite having closed as `model-early` are the boundary cases where they do not.

### The floor is the lever, and 800 ms is the middle of a measured plateau

All five truncations closed at trailing silence of **0.44–0.48 s** — the first check the 400 ms
floor permits. The early-exit trailing distribution is bimodal at ~0.46 s and ~1.04 s, which
reproduces the code's own rate limiter exactly: the first check is allowed at
`TURN_MIN_SILENCE_MS`, the next `TURN_INCOMPLETE_DELAY_MS` (600 ms) later.

| floor | early exits able to fire | truncations able to fire |
|---:|---:|---:|
| 400 ms (today) | 47 / 48 | 5 / 5 |
| 600 ms | 12 | **0** |
| 800 ms | 12 | **0** |
| 900 ms | 12 | **0** |
| 1000 ms | 10 | 0 |
| 1200 ms | 0 | 0 |

600–900 ms are indistinguishable, so **800 ms** is chosen as the centre of the plateau rather than
its edge — a value picked at 600 would sit 120 ms from the worst observed truncation.

**What the floor does and does not buy, stated precisely.** It guarantees no turn is closed early
inside a pause shorter than 800 ms. It cannot protect a pause *longer* than the floor: of the five
truncations, his real pauses were 0.64 s, 0.80 s, 0.98 s, 1.14 s and 2.00 s, so a floor alone
rescues only the shortest. **The other four are not segmenter defects at all** — a one-second pause
is a genuine end of utterance by any standard — they are one thought arriving as several turns,
which is the waiting layer's job and not the segmenter's. Conflating the two is what made this look
like a single bug. The floor fixes the first; the collapsed wait plus the pre-`say` wait fixes the
second.

Cost: the 36 early exits below the new floor each wait about 1.05 s longer. Against a ladder that
was costing up to 10.5 s per exchange, that is bought back several times over.

### FR5 moved the first `scripts/e2e.py` failure, and the link is causal not incidental

`e2e.py` has been failing at HEAD for two reasons. **The first is now fixed, and it was fixed by
the floor rather than by anything about the wait.** Run on the same fixture, same machine, only
the floor changed:

| `VOICE_TUNNEL_TURN_MIN_SILENCE_MS` | transcript of the spoken WAV | result |
|---|---|---|
| 400 (the old default) | `'Hey assistant.'` | **FAIL** — `missing ['status', 'deploy']`, 11 checks in |
| 800 (the new default) | `'Hey assistant, what is the status of the deploy?'` | **PASS**, and 22 checks in |

This is the synthetic half of the same defect as the live one: the utterance was being closed at
the comma, on the first check the 400 ms floor permitted. A synthetic failure and a live one
agreeing is much stronger evidence than either alone, and it is now a controlled A/B rather than
an observation.

**The second failure is untouched and deliberately so** — `the separate mute button is gone, the
orb owns it`, which is a stale assertion: mute was split back off the orb into its own control on
2026-08-06, so the harness is asserting a design that was deliberately reversed. Out of scope
here, and not fixed silently.

### Why no confirmation window was added — the two distributions

Restated here because it is the finding that authorises deleting the ladder rather than shortening
it. On the same session: when he continues after a turn closes he resumes within **10 s in 100% of
cases** (median 0.52 s, p90 4.14 s, max 5.98 s, n=35), while the agent's `consumed → say_requested`
was **≥15 s in 100% of turns** (n=80, p10 20.6 s, median 29.0 s, p90 58.0 s).

Non-overlapping, with a 2.5× margin between the extremes. Every continuation is already a turn by
the time the agent is ready to speak. A grace window after `speech_active` drops would therefore
add latency to *every* exchange to buy certainty that arrives free on the next call — and it would
violate NFR2 to do it.

### `speech_active` was already correct on mute; the holes were elsewhere

FR1 describes the flag sticking true when muted. Reading the code, the mute path had already been
fixed — `_on_control`'s `muted` branch flushes the buffer, and `flush()` resets `_seen_speech`
in both of its branches — and the websocket handler's `finally` flushes on disconnect. The
remaining holes were the ones nobody had named:

- **capture released (orb off) and channel close** do not flush, and since 2026-08-16 the orb
  releases the microphone — so the most common way to stop talking was the one still leaving a
  half-utterance open;
- **`user_speaking` is never reset server-side.** The client sends `speaking: false` on mute and on
  stop, but a socket that dies mid-word leaves the flag true forever. Under the old drain this was
  cosmetic; under a speaking-gated wait it hangs the tunnel outright — the precise failure FR1's
  key decision predicts, arriving through the other signal;
- **frames in flight after a mute** re-open the buffer after the flush.

Hence the two-half fix in Key Decisions. The staleness guard is what makes the class closed rather
than the three cases enumerated: any future way of stopping frames is covered without a new branch.

### The regression this spec caused, and the distinction that fixes it

**Removing the rungs from the speaking path and leaving them on the silent path made the thing
this spec exists to fix WORSE.** Reported live within minutes of the first working build:

> *"I don't like that this wait. If I say nothing, this waits for 30 seconds. That's slow."*

He is right, and the arithmetic is the indictment: the pause before every reply went from the
drain's 10.5 s to the idle ladder's **30 s**. FR2's *"no backoff ladder in the speaking case"* was
read as leaving the silent case alone — and the silent case is the one an agent hits immediately
before every `say`.

**The two positions are the same command with the same arguments against the same tunnel state,
and they want opposite things.** Before a reply, an answer in milliseconds. While listening, a
block — because an immediate empty return makes the loop RULE_1 requires a hot spin, one agent
turn per iteration. So "always return immediately" is not available, and neither is a flag: NFR1
rules it out, and the concrete argument is that `--timeout 0` as the documented workaround would
be exactly the option an agent forgets under pressure, which is how `watch`-vs-`drain` was got
wrong twice in one session.

**The distinction that works is derived, not declared: has the agent been handed turns it has not
yet answered?** Set when turns are delivered, cleared when it speaks, and cleared when a wait
comes back empty — which is precisely the drain loop, where arriving turns keep the batch alive
and the first empty round ends it. Nothing is passed by the caller and nothing is announced by the
agent, which is the same rule the orb already follows: *"a status the agent announces is a claim,
and a claim is wrong exactly when it matters."*

Measured against a live server after the fix:

| position | wall time | `next_wait` |
|---|---:|---|
| pre-reply check, he is quiet | **234 ms** (of which ~200 ms is Python interpreter startup) | absent |
| listening, he is quiet | 3.3 s at `--timeout 3`, laddered as before | present |
| `watch` alias, pre-reply | 234 ms | absent |
| `drain` alias, pre-reply | 234 ms | absent |

A pre-reply check also does **not** advance the backoff streak. Otherwise a check before every
reply would inflate the listening ladder, so a few exchanges would leave the next real listen
opening on a four-minute ceiling one second after he stopped talking — the same defect the old
drain needed its own streak bookkeeping to avoid.

### Renaming the waiting command is a live-traffic migration, not a refactor

FR3 asks for the aliases on the grounds of agent habits. **The stronger argument arrived by
breaking it.** Renaming `cmd_watch` to `cmd_wait` left the dispatch table pointing at a name that
no longer existed, and the effect was not a failing test — it was an outage: every CLI invocation
raised `NameError`, the agent driving the live session lost the ability to `watch` or `say` and
fell back to curl, and the scheduled watchdog firing `voice-tunnel watch --session dev --since
<cursor>` every minute erred on each firing.

Two things follow, and both are now pinned by tests:

- **A scheduled job cannot be updated atomically with the code.** For as long as any harness holds
  the old spelling, the old spelling has to run. That is a stronger reason for FR3 than habit, and
  it is why the aliases are real subparsers mapping to one function object rather than a
  rename-with-a-shim.
- **The suite never called `main()` with a real argv**, so nothing in it ever built the dispatch
  table. Every test passed while every real invocation failed. `test_wait.py` now drives `main()`
  for each alias, and asserts more generally that every subcommand the parser offers can be
  dispatched — a command that parses and then cannot dispatch is a `KeyError` at the moment of
  use, invisible to an import check.

### The harness was asserting a state that cannot occur, and its hold check was vacuous

`scripts/channel.py` drove `signal('speaking', true)` with no audio, because a headless run cannot
produce a `getUserMedia` grant. Under FR1 that now reads as *not speaking* — correctly: in
production "he is speaking" and "frames are arriving" are the same fact.

The fix is not to weaken the guard. The harness gained `__voiceTunnel.pushAudio(ms)`, which pushes
speech-shaped PCM through the same socket the microphone uses, and it now asserts `capturing` too.
**Doing so revealed that the existing check was already vacuous:** "a reply is HELD while he is
speaking" measured `held_for` ≈ 0.9 s, which is `SPEAK_GRACE_S` and is returned whether or not
anything was held. With real frames it measures 15.1 s. The check would have passed against a
server that never held a clip at all.

Same lesson as the air-conditioner bug recorded in Voice Tunnel: every synthetic test passed
because they all fed digital zeros as "silence". A test that simulates half of a physical pairing
tests nothing about the half it omitted.

### `held_for` is never zero, so the warning built on it fired on every reply

Found by running a real `say` against a live server rather than a fake. The hold loop always
spends `SPEAK_GRACE_S` re-checking before it commits, so **`held_for` comes back at ~0.9 s on a
completely clean reply** — and every branch downstream tested `held_for > 0`. The result: *"the
server held this clip because he was still speaking while you were composing it — drain again"*
was emitted after **every single reply**, including ones where nobody had said a word.

That is the same defect class as a health check with only two values: a warning that always fires
is one nobody reads, and it was diluting the single most important signal `say` had. The server
already knew the answer — the hold loop's `announced` flag is set only when it actually observed
him talking — so it is now published as `held_for_speech` rather than inferred from a number that
cannot express it. Pre-existing, unrelated to the collapse, and fixed because FR6 was about to
build a second warning on the same broken predicate.

### "You must be hitting the wrong installation" is the reflex to distrust

Twice during this work a caller reported the CLI broken — `NameError: cmd_watch`, then
`KeyError: 'watch'` — and both times the working tree really was inconsistent for the window they
hit. The first response to the second report was to suggest they were running the system
site-packages copy rather than the checkout. **They were not**: same shim, same venv interpreter,
same command string, working minutes earlier and minutes later.

Recorded because that hypothesis is the project's own most-cited trap (see the runtime incident in
Voice Tunnel), which makes it the first thing anyone reaches for — and reaching for it first
is how a real inconsistency gets dismissed. **During a rename the same invocation can succeed and
fail minutes apart, so a breakage report from a caller who can name their interpreter should be
treated as live until disproved.** The operational rule that follows: for a rename, add the new
key alongside the old, verify dispatch, *then* remove the old — never leave the table missing a
name something is actively invoking.

### The failsafe capture is truncated on every reconnect

Found while building the corpus, recorded because it is not a hypothetical: `close_capture()` runs
in the websocket handler's `finally` and `capture()` reopens the file with mode `"wb"`, so
`sessions/<s>.wav` holds only the audio since the LAST reconnect while `t_start`/`t_end` count
from server start. The capture exists so that *"the ASR is bad"* can be turned into a question you
answer by listening, and it silently discards everything before the most recent reconnect. Out of
scope here.

## Out of Scope

- Replacing the RMS/percentile speech detector with Silero VAD. Tracked separately in
  Voice Tunnel; this spec consumes whatever signal exists.
- Any change to TTS, the de-esser, or voice selection.
- Editing Voice Tunnel Guide. The implementer **names** the sections that become obsolete; the
  owner cuts them himself.
- The two pre-existing `scripts/e2e.py` failures, except where FR5 changes the first one — in which
  case say so explicitly rather than fixing it silently.
- Removing the `watch` and `drain` aliases. They survive this release by FR3; retiring them is a
  later spec.
- The failsafe-capture truncation found while building the corpus (`close_capture` + `"wb"` reopen
  discards everything before the last reconnect). Recorded in *Findings*, not fixed here.

## References

- Voice Tunnel — Next Actions carries the full rationale, the owner's verbatim words, and the
  live incident that produced this spec.
- `specs/004-turn-detection.md` — the smart-turn integration this modifies.
- Voice Tunnel Guide — the behaviour contract the collapse simplifies.
