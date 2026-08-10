# Changelog

## 0.2.3 — 2026-08-10

A third cold-start audit — fresh virtualenv, published wheel, no context but `voice-tunnel
describe` — reached a working install and ended up on the system voice anyway. Nothing had
failed; the tool simply never used what it had just installed.

### Fixed

- **A complete Piper install now selects itself.** `tts_backend()` returned a hardcoded `sapi`
  unless `VOICE_TUNNEL_TTS` was exported by hand, while `asr_engine()` had always upgraded itself
  the moment its model appeared. So `setup` could install the engine, download a neural voice,
  report success on every step, and leave synthesis robotic — with the only fix being a setting no
  output ever named. An explicit `VOICE_TUNNEL_TTS` still wins.
- **The `sapi` remedy reads what is already installed.** It advised "run setup" whether or not
  setup had run, so after a successful one it recommended a no-op and the actual remaining gap
  went unnamed. With Piper and a voice present it now names `config set VOICE_TUNNEL_TTS piper`.
- **`piper.exe` beside the running interpreter wins over every other copy.** That is by
  definition the one this process's packages were installed with; it was missing from the search
  order entirely, so a fully isolated installation still resolved a binary belonging to some other
  Python on PATH.
- **`shim_on_path` no longer passes for somebody else's install.** `voice-tunnel` being on PATH
  says nothing about *which* voice-tunnel is on PATH — the audit's isolated copy reported `ok` the
  whole time while naming a console script from a different installation, still carrying another
  agent's wake name. A mismatch is now `degraded` and says so in full.
- **A failing `say` reports what the server said.** The client read the error body, failed to find
  the JSON shape it expected, and discarded it — so `SAPI produced no audio` arrived as a bare
  `HTTP 500`. An unparseable body is now kept (truncated), which is the only clue there is.

### Added

- **`status` reports `errors` per subsystem.** One `last_error` slot had twelve writers, so an
  optional subsystem nobody had asked about could overwrite the failure being actively
  investigated. Both views are exposed; `last_error` still means most-recent.

### Removed

- **`describe` no longer calls `setup` "one command to make a fresh install fully capable".**
  It installs engines and downloads models; whether the result got *used* depended on a setting it
  never touched, which made the claim false in exactly the case it was written for.

## 0.2.2 — 2026-08-10

Three things a cold-start audit found while confirming 0.2.1's fix. Each is a variant of the same
mistake: reporting a state as fine when it is merely functional.

### Added

- **`VOICE_TUNNEL_HOME`** — one root that scopes settings, models and turn logs together.
  `VOICE_TUNNEL_DIR` only ever scoped the session directory, which reads like isolation and is
  not: an audit set it, believed its environment was pristine, and found a wake name already
  applied that had leaked from a different installation through the machine-wide settings file.
  Individual variables still win, so isolating everything while sharing one 600 MB model cache
  remains possible.
- **`doctor` reports `runtime.shared`** — which paths other copies of the tool also use. Sharing
  models is deliberate; discovering it by surprise is not.

### Fixed

- **Piper spawning per reply is now reported as degraded.** It passed silently while `detail`
  quietly changed between `spawning piper.exe` and `resident (in-process)` — a difference this
  codebase measures at 7–26× on synthesis alone. A fallback hiding behind a pass is exactly what
  `degraded` exists to end.
- **The voiceprint check no longer disappears when it starts working.** It only appeared when the
  model was *missing*, so installing it removed the line, and readiness had to be confirmed
  through a different command. It now reports either way, including how many voices are enrolled.
- **`bytes` became `bytes_fetched` in download results.** `0` read as "this file is empty or
  corrupt" when it meant "nothing was downloaded, it is already here".

## 0.2.1 — 2026-08-10

Turn detection did not work in 0.2.0 outside a source checkout, and the tool's own advice for
fixing it could not work either. Found by giving an agent nothing but `voice-tunnel describe` and
asking it to reach the best available configuration.

### Fixed

- **Turn detection is installable.** It imports `onnxruntime` and `transformers`, and neither was
  declared in any extra — so the feature could not load for anyone who installed from PyPI. There
  is now a `turn` extra carrying both, included in `all`.
- **The remedies pointed at the wrong extra.** `doctor`, `download turn`, and the runtime error
  all advised `pip install voice-tunnel[parakeet]`, which installs sherpa-onnx and neither of the
  packages actually needed. Running the printed fix verbatim left you exactly as broken.

### Added

- **`voice-tunnel setup`** — installs the optional engines into the current interpreter and
  downloads all four models (voice, recognizer, voiceprint, turn) in one idempotent command.
  Assembling that from four separate instructions is four chances to do three of them, and the
  two axes involved are easy to confuse: the extras supply the engines, the downloads supply the
  models, and a model without its engine is a state this has produced in practice.
- **`doctor` reports a DEGRADED state.** `ok: true` used to cover both "correctly configured" and
  "running on the system voice and the slow recognizer because nothing better is installed".
  Those now differ: `degraded` lists what is on a fallback, and every non-ok check carries a
  `remedy` you can run verbatim — a field that was previously null on every line, because a
  passing check discarded it.
- **`doctor` reports `runtime`** — version, interpreter, package location, settings file, models
  directory, and whether this is a source checkout. No single check can ask "am I even the
  installation you provisioned?"; the set can.

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
