# Changelog

## 0.2.0 — 2026-08-10

A minor rather than a patch, because four things break callers written against 0.1.2. Everything
below came out of using the tunnel to hold real conversations; none of it was found by testing.

### Breaking

- **`consumed` no longer takes `--state`, and you almost certainly should not call `consumed` at
  all.** `watch` marks turns read as it hands them over — delivering them *is* the
  acknowledgement — and the agent's status is now derived from which commands are running rather
  than declared by the agent. A status the agent reports is a claim, and a claim is wrong exactly
  when it matters: when the agent said "thinking" and then wandered off.
- **The wake phrase is no longer stripped from the transcript.** `"hey claude what is the
  status"` is delivered whole, where 0.1.2 delivered `"what is the status"`. An agent can ignore
  two words on its own; editing them made the log disagree with what the speaker remembers
  saying, and the failure was silent every time. Anything parsing turn text should expect the
  phrase.
- **`watch` can return an error instead of turns.** It refuses to start when another watch is
  already open on the same session, because concurrent watches race for the same turns and one
  cursor silently falls behind. `--force` overrides. Callers that assume `turns` is always
  present need a branch.
- **`watch --timeout` changed meaning.** Omit it and the wait now backs off on its own — 30 s
  doubling to 9 minutes while nothing happens, resetting the instant a turn lands or a button
  moves. Pass it and it is a hard ceiling, honoured exactly. In 0.1.2 it was a flat 30 s either
  way.

### Added

- **`watch` returns when a control moves, not only when someone speaks.** Mute, unmute, opening
  or closing the channel, tapping the orb, toggling verbose, a page connecting or dropping — each
  resolves a blocked watch within about a second and comes back as
  `{"event": "control", "changed": {"muted": false}}`. Muted and disconnected used to be blind
  spots the agent could only leave by guessing when to poll.
- **Barge-in, gated on the owner's voiceprint.** Talking over a reply stops it mid-sentence, and
  only the owner's voice does — not the room, not the television, and not the agent's own speech
  leaking back through the speakers, which is the failure that would otherwise dominate.
- **Turn detection.** A turn ends when the sentence *sounds* finished
  ([smart-turn v3.2](https://github.com/pipecat-ai/smart-turn), 8 MB, CPU, run only during
  silence) rather than when a fixed silence timer expires. Every failure path degrades to the
  timer.
- **A confident stranger loses the conversation window.** The 30-second window is an inference —
  that whoever is speaking now is whoever spoke a moment ago — and a voice confidently not the
  owner's is evidence the inference is wrong. It cannot override a spoken wake phrase, and an
  unsure score keeps its attention.
- **Whatever grants attention now extends the window.** A turn addressed by voiceprint used to
  open no window at all, so short follow-ups — too brief to score — arrived unaddressed in the
  middle of a conversation.
- **`status` reports `last_turn_id` and `watch_open`.**
- **`describe --session <s>` returns a ready-to-schedule watchdog prompt**, and a `watchdog`
  section explaining why one is required: nothing in this CLI can force an agent back into
  `watch` once it has stopped, and a scheduled prompt in the harness can.
- **`no mic` on the orb** when the microphone is refused, instead of silently reverting to
  "tap to start" — which was indistinguishable from a tap that never registered.
- **A collapsible transcript**, and an elapsed-seconds counter inside the orb for each working
  state.

### Fixed

- **Muting mid-thought erased what the agent was doing.** The server published mute through the
  agent-state channel, so the orb dropped from "Thinking" to "Listening" and its counter vanished
  while the agent kept working.
- **Closing the channel did not stop audio already playing.** The server refused to send new
  replies but a delivered clip played to the end, so "off" only took effect after the current
  sentence — exactly when it is least wanted.
- **The read boundary rendered below the fold, every time.** It is inserted after the row that
  was just scrolled to, so a scroll-to-the-row policy could never show it.
- **Inter-sentence silence was an odd number of bytes** at some pause lengths, misaligning every
  16-bit sample after the gap into loud broadband noise.
- **Mute is session state, not a device preference.** It no longer survives closing the tab, so a
  machine shut down while muted does not come back deaf on a page that looks live.
- **The version is read from package metadata**, so the tool cannot misreport what it is.

## 0.1.2 — 2026-08-05

Report the version you actually are: `__version__` was hardcoded, so `pip show` and `describe`
disagreed after a bump.

## 0.1.1 — 2026-08-05

The frozen entry point needs an absolute import. `__main__.py` used a relative one, which is
correct for `python -m voice_tunnel` and wrong under PyInstaller, which runs that file as
`__main__` with no parent package — so 0.1.0 was broken on the Windows bundle.

## 0.1.0 — 2026-08-05

First release.
