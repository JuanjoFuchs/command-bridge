# Changelog

## Unreleased

A backlog audit found the Kokoro TTS backend shipped, running in production, and undeclared
everywhere a user or an agent would look for it — the same defect that made turn detection
unreachable in 0.2.0, one backend later. Everything here is that gap, plus the latent crash
sitting behind it.

Five specs then landed on top of it (`007`–`011`), and they share a shape worth naming: **each one
replaces a rule somebody had to remember with something the tool refuses to get wrong.** A build
that fails on a setting the CLI cannot describe, rather than a note asking you to keep a list up to
date. A picker that counts what it can actually offer, rather than a comment explaining when it is
useless. A `say` that will not speak over you, rather than a guide asking the agent not to.

`011` turns that same lens on the agent instead of the human. Every other spec here treats JJ's
attention as the scarce resource; this one treats **the agent's context window** that way, because
a CLI that restates its whole contract on every call spends exactly what talking was supposed to
save. It also closes the sharpest defect found so far: a refusal an agent could enter and **not
leave by reasoning**.

### Removed

- **`drain`.** The second name for `watch` is gone — not deprecated, not aliased, not kept for calls
  already in circulation. The two spellings ran the same code, and the second one was not cosmetic:
  it is what led an operating guide to write them up as two instruments with two waiting strategies,
  and to ship a wrong rule about which to use when. A call still carrying the old spelling gets
  `code: unknown_command`, the name that replaced it, and **its own arguments respelled onto
  `watch`**, so whatever session and cursor it carried come back in a line that runs. That is a
  lifeline for calls already in flight, not a command. **There is one waiting command and it is
  `watch`.**

  *(This entry was itself rewritten by the check that enforces the removal: the first draft
  demonstrated the respelling by quoting the old invocation, and the scan refused it as something a
  reader could copy and run. The guard fired on the person who committed it, which is the only kind
  of evidence worth having.)*

### Fixed

- 🔴 **A refusal you could not escape by reasoning** (spec `011`). `say` refuses while a turn is
  unread, and the refusal's `remedy` names the *server's* read cursor. But an agent holding its own,
  higher cursor would run `watch --since <its own>`, get `quiet`, consume nothing, and be refused
  again — identically, forever. Two cursors track one log and only one of them gates the refusal.
  **`watch` now resumes from the LOWER of the `--since` it was given and the server's
  `consumed_cursor`**, publishing `since_requested` and `resumed_from` when they differ. That is a
  rule the tool already stated in prose, in two places, moved into the command — so the refusal is
  now escapable whatever cursor the caller believed, not only by copying the remedy verbatim.
  *Bounded downward: a negative `consumed_cursor` never drags a caller to the head of the log,
  because "no read position" is not a read position of zero.*

### Added

- **A refused batch carries the unread turn once, not once per clip** (spec `011`). Four `say --now`
  clips fired back to back used to return four full copies of the same turn — so **the longer the
  thought you were trying to deliver, the more you were charged for being interrupted**, which is
  exactly backwards. A repeat refusal now carries the turn *ids* without their text and says so;
  a new turn or a successful read makes the next one full again. **A repeat is a flat 590 characters
  regardless of turn length**: at the ~700-character turn this was found on, a four-clip refused
  batch drops **6,024 → 3,276 characters, 45.6%**.
- **Procedural guidance is emitted when it changes something** (spec `011`). The multi-sentence
  explanation of the loop used to arrive on every call. It now arrives when the branch *changed*
  since that command's previous call in the session; a repeat carries the literal command alone plus
  a pointer to `describe`, marked `next_repeated`. **`next` itself is untouched in what matters** —
  every form, long or short, still carries a runnable command with the session and cursor already
  substituted. *Which is more than was true before: the sweep this required found that the single
  most-emitted string in the tool — the guidance that fires once per turn of every conversation —
  was printing a literal `<cursor>` placeholder it already had the value for.*
- **`command-bridge contextcost`'s script, and a CI gate under it** (spec `011`). `scripts/contextcost.py`
  recomputes what the above actually saves — both columns from the live code, so the number cannot
  rot into a stale constant — and `--gate` exits non-zero below the floors the spec pre-registered.
  It measures the payload the agent really receives, including the 23 characters the repeat marker
  costs. *It earned its keep immediately by failing: the first implementation of the short form came
  in at 22.5% against a 25% floor, and the fix was to shorten the form rather than to move the floor.*

- **`say` refuses when he has said something you never read** (spec `007`). It exits non-zero with
  `code: unread_turns`, synthesises nothing, queues nothing, and does not move the read cursor — so
  refusing costs nothing, because the next `watch` returns those turns exactly as it would have. The
  refusal hands back the turns themselves and a `remedy` that is the literal `watch` to run.
  **There is no flag that disables it**, and the check applies to `say --now` as well: the hurried
  path is precisely the one where the check used to get skipped. *The remedy resumes from the read
  cursor rather than the last turn id — the obvious choice deadlocks, because a `watch` starting at
  the head of the log returns nothing, marks nothing read, and refuses again forever.*
- **The acknowledgement cue means acknowledgement** (spec `007`). It used to fire at the end of the
  turn-logging path — before any agent had seen the turn, and whether or not one was listening — so
  it asserted acknowledgement for every utterance including the ones nobody would answer. It now
  follows the agent's intent to respond, which is what lets its **absence** carry information.
  `command-bridge consumed --not-responding` is how an agent reads a turn and stays silent.
- **`command-bridge pronounce "<text>"`** (spec `008`). Prints exactly what the engine will be handed,
  with no server involved — the thing you reach for when asking why a clip sounded wrong.
- **Speech normalisation before synthesis** (spec `008`), above the backend dispatch so every engine
  gets it. `0.2.6` was being spoken as "zero two six" and `1.0.0` as "one hundred": the dot was
  dropped and the digits ran together into a different number, with nothing in the sound to say
  anything had been lost. Numbers now take "point" and identifiers, extensions and domains take
  "dot". Clause breaks also get a pause, set as a fraction of the sentence pause so it keeps
  tracking the value tuned by ear. Measured through the recognizer rather than by ear: the fixed
  rendering transcribes back to `0.2.6`, the old one to `026`.
- **The five settings the code read in secret are registered** (spec `010`) —
  `VOICE_TUNNEL_BARGE_IN`, `..._BARGE_IN_THRESHOLD`, `..._CONSONANT_BOOST`, `..._WATCH_MAX_S` and
  `..._WATCH_DISCONNECTED_MAX_S` are now in `describe`, `config` and `.env.example`. **And a guard
  now fails the build** on any `VOICE_TUNNEL_*` the code reads and the registry does not declare,
  walking the source rather than comparing two hand-maintained lists — because a table that can only
  be wrong when somebody forgets to update it cannot catch somebody forgetting to update it.
- **`command-bridge download kokoro`.** `_ResidentKokoro._load` had been telling people to run this
  since the backend landed, and the parser rejected it with a usage error — the one instruction
  the failure gave you exited 2. It fetches both halves (the 325 MB `kokoro-v1.0.onnx` and the
  28 MB `voices-v1.0.bin`) and checks for them **separately**: model-without-pack is a real state
  after an interrupted run, and skipping it as "done" fails much later, at load.
- **A `[kokoro]` extra, and `kokoro-onnx` in `[all]`.** `voice_tunnel.tts` has imported it since
  the backend landed with nothing declaring it, so `VOICE_TUNNEL_TTS=kokoro` could only ever work
  in a checkout whose venv already carried the package — which is exactly where it was written and
  exactly where the test suite runs. It is in `[all]` rather than beside it because someone who
  has already run `pip install command-bridge[all]` and is then told to install `[kokoro]` has no
  way to know the first command was meant to have covered it.
- **A test that walks the source for optional imports** instead of checking the pairs someone
  remembered to list. The existing table could not fail for an import nobody added a row for,
  which is how a backend ships with no extra twice. Confirmed against the pre-fix tree: it flags
  `tts.py:333 kokoro_onnx` and nothing else.

### Fixed

- **No device picker is offered with nothing to choose** (spec `009`). On Android the browser
  enumerates one output and cannot route audio from the page anyway, so the speaker menu held a
  single entry and could never do anything. The page already had a rule meant to withdraw it, and
  that rule had never fired: the only signal it waited for is a refusal from `setSinkId`, and
  `setSinkId` is never called on the default path. So the decision is now a **count** — of distinct
  devices, after collapsing Chrome's `default`/`communications` aliases, because Chrome lists one
  physical output up to three times and counting rows would have fixed Android while leaving the
  same dead menu on every one-sound-card desktop. Where no picker is offered, the page names the
  route only when it can read it back off the live audio context, and otherwise says nothing — a
  stale label is the bug that started this.
- **The three Kokoro settings are registered.** `VOICE_TUNNEL_KOKORO_VOICE`, `_MODEL` and
  `_VOICES` have read the environment since the backend landed and were in no list, so
  `config get VOICE_TUNNEL_KOKORO_VOICE` answered *unknown setting* for a key that was live,
  honoured, and sitting in the owner's own `.env` selecting the voice he was listening to. Same
  defect as the turn variables before them; `describe`, `config show` and the `.env.example` drift
  test all read `config.SETTINGS`, so one row fixes all three surfaces.
- **`VOICE_TUNNEL_TTS` admits `kokoro` exists.** Its description read `sapi | piper | none` while
  `kokoro` was the backend in production use.
- **`doctor` no longer fails the working configuration.** `kokoro` fell through to the `else`
  branch and was reported as a hard FAIL — `backend=kokoro`, remedy *"VOICE_TUNNEL_TTS must be
  sapi | piper | none"* — which took `doctor.ok` to false on a correct install. A diagnostic that
  calls the live setup invalid is worse than none, because the next thing anyone does is follow
  its remedy and change something that was right. There is now a real kokoro branch, naming the
  runtime and the model halves separately because they are fixed by different commands.
- **A speed above 2.0 no longer makes every Kokoro reply raise.** `kokoro_onnx.create` opens with
  `assert speed >= 0.5 and speed <= 2.0`, while `SPEED_MAX` is 2.5 and `command-bridge rate --speed
  2.5` accepts and persists it. The clamp sits at the Kokoro boundary — the same place
  `length_scale_for` contains piper's inverted unit — rather than in `SPEED_MAX`, because piper
  handles 2.5 and the owner uses high speeds deliberately; lowering the global maximum would take
  a working setting away from one backend to accommodate the other. **It is not silent:**
  `status`/`voices` report `speed clamped 2.5→2.0` and `doctor` says the same, computed from the
  configured speed so the warning arrives *before* the first reply rather than after it.

## 0.2.7 — 2026-08-10

The README now says two commands: `npm install`, then one line pasted to your agent. An agent was
handed exactly that line and nothing else, and it worked right up to the last step — where it
produced a loopback URL and offered it as the page to open on a phone. Everything here is what
that run exposed.

### Added

- **`status` reports `phone: {ready, why, remedy}`** beside the URL. A browser will not give a
  page a microphone outside a secure context, so `http://` to anything but localhost yields *no
  microphone* rather than an error — the page looks connected and hears nothing. The warnings
  existed and named the wrong things: `the_loop` said `192.168.*`, the serve banner said "a LAN
  IP", and the address actually printed was loopback, which is not mic-less but unreachable
  outright. The one command whose job is to hand over that URL had no caveat at all. `the_loop`
  now tells the agent to read `phone.ready` *before* handing the link over, and notes that the
  default bind makes `false` the normal first answer.

### Fixed

- **The backoff cap is documented from the constant.** There were three hand-written copies and no
  two agreed — `watchdog.backoff` said 15min/30min, `watch --timeout` said 30min/1h, the
  constant's own docstring said thirty minutes, and the constant has been 540 seconds throughout.
  Two of them contradicted each other inside one `describe` payload, which does more damage than
  any single wrong number: it says the document is not maintained. All of them now read `9min`,
  generated from `WATCH_BACKOFF_MAX_S`.
- **`invocation.no_env_vars_needed` admits its exception.** It promised no per-call exports while
  `VOICE_TUNNEL_HOME` — the variable the tool's own isolation advice recommends — cannot be
  persisted and must be exported every call. An audit kept prefixing every command while reading
  the line that said it needn't.
- **The watchdog section names a behaviour, not an API.** It said "CronCreate, cron `* * * * *`",
  and an audit in a harness without that tool reported it as stale — the failure the same section
  warns about one line earlier. What matters is that the scheduler fires only while the session is
  idle, whatever it is called.

## 0.2.6 — 2026-08-10

The fifth cold-start audit reached a fully configured install in five commands and produced no
broken states at all. Everything below is the tool being wrong *about itself* to an agent that had
already done everything right — which is the only failure mode left once it works.

### Fixed

- **Every remedy `setup` covers now names `setup`.** `next` reads those strings to work out what
  one command fixes, so remedies naming only their narrow `pip install command-bridge[piper]` made
  `setup` look smaller than it is: the audit was told it covered two checks and handed pip lines
  for two more, when `[all]` had already fixed all four.
- **The ASR remedy no longer tells you to pin what selects itself.** It ended with
  `config set VOICE_TUNNEL_ASR parakeet` while the same `describe` warns that an explicit value
  wins over what is installed — so following it pinned a choice already made and took away the
  tool's ability to move you off it later. Installing the runtime is the whole fix.
- **`VOICE_TUNNEL_HOME` is documented.** The variable scoping the settings file, the model cache
  and the session directory was in no list at all, and `config get` called it an unknown setting.
  It now appears under `describe.env_process_only` and `config get` explains it — including *why*
  it cannot be persisted: it decides where the file that would persist it lives.
- **`describe` documents every flag the parser accepts.** `say --now`, `say --voice` and
  `watch --force` were missing, and two audits found them by running `--help` per subcommand.
  `--now` is the difference between interrupting a reply and queueing behind it, and `describe`'s
  own watchdog prompt uses it. A test now fails on any new flag that goes undocumented.
- **`watch` hands over a watchdog prompt that exists.** It attached the static section — whose
  `prompt` is null — directly beside the line telling you to use the ready-made text in `prompt`
  rather than paraphrasing it.
- **The same error code always means the same exit status.** `config get NOPE` exited 1 and
  `wake --name "two words"` exited 2, both carrying `code: invalid_input`, because one was
  returned and the other raised.
- **`status` reports the live wake name.** `wake` compares persisted against live and the live
  half read `null` forever, beside a correct list of phrases containing the name — the server
  visibly knew it and would not say it.
- **A global flag works after the subcommand.** `doctor --human` is how people write it, and it
  exited 2 with `unrecognized arguments: --human` while the usage line advertised `[--human]`.
- **`stop` documents both shapes** — `how` when it stopped something, `reason` when there was
  nothing to stop.

### Added

- **`status` returns `url`.** `serve` printed the client URL and its token exactly once, to
  stdout, and nothing could reproduce it; losing that output meant restarting the server to get
  back to the page. The token was already on disk.
- **The `serve` banner names `stop`.** It explained how to rename the assistant and change the
  speech rate, and not how to shut the thing down.

## 0.2.5 — 2026-08-10

A fourth cold-start audit, and the first run against a locally built wheel rather than a published
one. It reached the best available configuration in five commands — and then spent the rest of the
session being told to fix something `setup` cannot fix.

### Added

- **`command-bridge stop --session <s>`.** The tool could start a detached server and had no way to
  end one. `describe` tells you to run `serve` in the background and never said how it stops, so
  the only exit was the OS handle of whatever launched it — gone entirely in a new session. It
  asks the server to shut down first, so the turn log is flushed, and falls back to the pid.
- **`status` reports `pid`** (and `runtime_file`). The pid has been written at serve time since
  the beginning and surfaced nowhere, so an auditor had to save its own.
- **`doctor` reports `advisory`** — a fourth check state for things worth knowing that are not
  faults. `degraded` is only useful while it can be emptied.

### Fixed

- **`doctor`'s `next` is assembled from the checks' own remedies.** It was a template that named
  `command-bridge setup` for anything non-ok. An audit reached a state where the only remaining item
  was `shim_on_path` — a PATH observation `setup` does not touch — and was told, repeatedly and
  verbatim, to run the one command that could not possibly help, while the correct advice sat one
  level down in that check's own `remedy`. Advice that ignores the diagnosis is a loop.
- **`shim_on_path` is advisory, not degraded.** It reports what a *bare* `command-bridge` resolves
  to, which is permanently different for anyone calling this copy by absolute path — as its own
  remedy recommends. So it could never be cleared, `degraded` was never empty, and the field this
  release series exists to make trustworthy quietly became noise.
- **`wake` read mode returns the shape `describe` documents**, and no longer reports an unsaved
  default as `persisted` — a fresh install claimed a saved wake name while `config path` said the
  settings file did not exist.
- **`describe.invocation` resolves the runtime instead of reciting a checkout.** It described
  `bin/`, `<repo>/.env` and a shim, none of which exist in a pip install, so an agent on an
  installed copy went looking for a repository that was not there.
- **`--help` builds the cue list from the cue vocabulary.** It listed three; `describe` documented
  four; nothing said which was stale.
- **`voices` no longer says "not yet loaded".** Every CLI call is a fresh process that will never
  load a voice, so it was tautologically true and read as a warning — reported after Piper had
  demonstrably synthesized seconds earlier in the server.
- **The voiceprint check says how many samples it has and how many it takes.** "Enrolment happens
  automatically" with no denominator left an auditor unable to tell one turn from twenty. It is
  one.

## 0.2.4 — 2026-08-10

### Fixed

- **`doctor`'s next step names `setup` when `setup` is what fixes it.** With any check failing,
  the advice was "fix the failed checks above — each carries its own remedy", and on a bare
  install several of those remedies are the same single command. It now says which checks one
  `setup` covers — read off the remedy strings themselves, so it cannot drift from what they
  say — and no longer drops the list of fallbacks just because something else failed.

  This was red in CI for five consecutive pushes across four releases and could not reproduce on
  the machine it was written on: SAPI exists on Windows, so nothing failed here and only the
  already-correct degraded branch ever ran. The platform test now goes through one function that a
  test can replace.

## 0.2.3 — 2026-08-10

A third cold-start audit — fresh virtualenv, published wheel, no context but `command-bridge
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
- **`shim_on_path` no longer passes for somebody else's install.** `command-bridge` being on PATH
  says nothing about *which* command-bridge is on PATH — the audit's isolated copy reported `ok` the
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
fixing it could not work either. Found by giving an agent nothing but `command-bridge describe` and
asking it to reach the best available configuration.

### Fixed

- **Turn detection is installable.** It imports `onnxruntime` and `transformers`, and neither was
  declared in any extra — so the feature could not load for anyone who installed from PyPI. There
  is now a `turn` extra carrying both, included in `all`.
- **The remedies pointed at the wrong extra.** `doctor`, `download turn`, and the runtime error
  all advised `pip install command-bridge[parakeet]`, which installs sherpa-onnx and neither of the
  packages actually needed. Running the printed fix verbatim left you exactly as broken.

### Added

- **`command-bridge setup`** — installs the optional engines into the current interpreter and
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
