---
id: "012"
title: Addressing is a lane, not a decision
status: pending
blocked_by: []
blocks: []
kind: metaspec
---

# Addressing is a lane

> **This is a METASPEC**, written by the strategist. It carries intent, the decisions
> already taken, and the constraints the implementer must not rediscover. **The repo implementer
> refines it in place** against the live code, adds implementation tasks and acceptance criteria with
> validation methods, then builds.
>
> **Completion rule:** not complete until every acceptance criterion the implementer writes is verified
> by the method named on it. Build-only verification is insufficient. Iterate until verification passes.

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

### Non-functional

- **NFR1** — **A lane switch is not perceptible.** It happens in the same path as the wake gate, on state
  the server already holds. If switching costs a round trip he will feel it on every subject change.
- **NFR2** — **Nothing is lost at a switch.** A turn in flight when the lane changes belongs to whichever
  lane it was addressed to, decided once, at the wake gate.

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
- **TC3** — ⚠ **Held speech must not reuse the queue path that already loses clips.** Measured 2026-08-20:
  nine clips issued while the channel was closed each returned `queued: true`, and **none played when the
  channel reopened**. The lane hold is a *different* case — the channel is open, another lane is live — so
  it is new code rather than the broken path, but FR7's entire value is that the waiting agent is not
  silently dropped. **Confirm the hold delivers before building the UI mark that promises it does.**
- **TC4** — **Barge-in is gated on JJ's voiceprint**, so no agent can interrupt another through the room.
  Suppression in FR7 is therefore about **playback**, not about the microphone.
- **TC5** — **Android Chrome, foreground tab only.** More lanes does not change the platform rule.
- **TC6** — **One transcript.** He was explicit: *"the transcript is just one."* Lanes are a view over a
  single log, not several logs — which is also what makes the shared context readable to him afterwards.

## Decisions already taken

| Decision | |
|---|---|
| **This stays in `voice-tunnel`; it is not a new project** | JJ asked directly whether it should be "Agent Meeting" instead. It should not: a lane touches the wake matcher, the playback queue, the turn log and the page — all inside this server — so a separate repo would have to fork it to reach them. This is a new **arc**, not a new product |
| **The wake name is the switch** | it already exists, is already per-agent and already persisted |
| **Off-lane agents are suppressed at playback, not muted at the microphone** | one microphone, one transcript |

## Open for the implementer to rule on

- **The ambiguous wake match** (TC2). The recommendation is in the constraint; the ruling is the
  implementer's, and it belongs in this spec once made.
- **Whether a broadcast enters every agent's context or only idle ones.** Both are defensible; the spec
  should say which and why.
- **Whether an agent is told it went off-lane**, or simply stops receiving turns.

## Out of scope

- **Routing work between agents, monitoring them, or steering them.** That is
  Voice Tunnel's written anti-goal and is already served by `sb sessions`, `agent-mail` and
  `ccpulse`. Lanes carry *speech*, nothing else.
- **Inferring who a turn is for.** See TC1.
- **A distinct voice per agent.** `say --voice` already exists; whether each lane pins one is a separate,
  cheaper question.
- **Fixing the closed-channel clip loss.** Tracked in Voice Tunnel; this spec must not build on it
  (TC3), but it does not fix it either.

## References

- Voice Tunnel — the roadmap row, the North Star and anti-goals, and the "One voice or many?"
  open question this spec closes.
- `specs/007` — the refusal machinery and the loop the tool now enforces; FR9 extends its surface.
- `specs/005` — the one wait gated on speech, which `watch --lane` must not break.
- Voice Tunnel Guide — the standing rules for how an agent behaves in a session, all of which become
  per-lane rules.
