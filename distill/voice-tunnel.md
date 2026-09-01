# voice-tunnel — factual distillation (what is actually built)

**Source:** `voice-tunnel` · package version `0.2.7` (pyproject) · package dir `voice_tunnel/`.
**Method:** grounded in the code (cli.py / server.py / config.py / store.py / lanes.py / the ASR·TTS·turn·voiceprint·wake modules / specs frontmatter / tests). Where a doc and the code disagree, the code wins and it is called out.

One-line identity (from `__init__` / AGENTS.md / DESCRIBE): *a local CLI an agent starts that gives a phone browser a hands-free, two-way voice channel to that agent. It holds no LLM and makes no decisions — the agent that starts it is the intelligence.* Entry point: `voice_tunnel.cli:main` (console script `voice-tunnel`; also `python -m voice_tunnel`, a pure absolute-import delegation for PyInstaller/PATH-less installs).

---

## 1. CLI command surface

Parser built in `cli.py::build_parser()` (argparse). One global flag, then a required subcommand. `main()` dispatches via a `handlers` dict of **21 top-level commands**. Global flags are accepted *after* the subcommand too (argv is re-ordered). Output is JSON (`--human` → `indent=2`); errors go to stderr as `{error, code, remedy}`. Exit codes: `0` ok, `1` operation failed (`EXIT_ERROR`), `2` bad input (`EXIT_USAGE`), `3` no server (`EXIT_NO_SERVER`, triggered by `running: false`).

**Global flag**
- `--human` — pretty (indented) output for people.

**The 21 subcommands** (flags exact from argparse):

| Command | Positionals | Flags | Notes |
|---|---|---|---|
| `describe` | — | `--session` (default `dev`, substituted into the watchdog prompt) | Emits the machine-readable contract (the `DESCRIBE` dict). "Read this first." |
| `doctor` | — | — | Preflight; returns `{ok, checks[], failed[], degraded[], advisory[], runtime}`. Non-`ok` exits `EXIT_ERROR`. |
| `setup` | — | `--engines-only`, `--models-only` | pip-installs the optional extras AND downloads every model in one command. |
| `config` | — | subcommand required: `show` · `path` · `get <key>` · `set <key> <value>` · `unset <key>` | Persisted settings in the `.env`. `show` redacts secrets; `get <secret>` does not. |
| `serve` | — | `--session dev`, `--host` (`127.0.0.1`), `--port` (`8765`), `--token`, `--no-wake-gate`, `--wake NAME` | Starts the tunnel (long-running). `--wake` = what it answers to after a greeting; persists. |
| `watch` | — | `--session dev`, `--since` (int, default `-1`), `--timeout` (float, default `None`), `--force`, `--lane`, `--all-turns` | **THE one waiting command.** Blocks until he has spoken AND stopped. Built by a `_watch_parser` factory kept as the single seam. `--since -1` = from the beginning. `--force` overrides the "watch already open" refusal. `--lane` filters to that lane + broadcasts. `--all-turns` also returns not-for-you turns (which still advance the cursor). |
| `say` | `text` | `--session dev`, `--voice`, `--lane`, `--now`, `--timings` | Speaks text to the client. `--now` returns immediately, synthesizes in background. `--timings` returns per-word timestamps (Kokoro timestamped model only; refuses elsewhere rather than estimating; cannot combine with `--now`). `--lane` refused with `off_lane` when he is addressing someone else. |
| `voices` | — | — | Lists installed piper voices. |
| `pronounce` | `text` | — | Shows what the engine will actually be handed for this text (no server). |
| `cue` | `name` | `--session dev` | Plays a short non-speech cue. Names built live from `cues.names()`: `heard`, `speaking`, `thinking`, `tool`. |
| `voiceprint` | — | `--forget NAME`, `--learn-from WAV_OR_DIR`, `--owner`, `--channel` (int, `0`=mic/you, `1`=system/others) | Who the tunnel recognizes by voice; manage the gallery. |
| `rate` | — | `--session dev`, `--speed` (float, `0.5`–`2.5`), `--pause` (float, `0`–`1.5`), `--no-save` | How fast the agent talks; higher = faster. Applies live AND persists unless `--no-save`. |
| `verbose` | `state` (`on`\|`off`, optional) | `--session dev`, `--no-save` | Narrate every action; global, persists, live. Omit `state` to read. |
| `download` | `what` (`voice`\|`kokoro`\|`asr`\|`voiceprint`\|`turn`, optional), `name` (optional) | `--list`, `--force` | Fetch a voice / ASR model / voiceprint / turn model. Omit args or `--list` to list. |
| `lane` | — | `--session dev`; sub-action (optional): `list` · `add <name>` · `remove <name>` · `switch <name>` | Multi-agent on one mic: who is in, who is live. Bare `lane` (no action) lists. |
| `wake` | — | `--session dev`, `--name`, `--no-save` | What the agent answers to after "hey"; single word. Omit `--name` to read. Persists, live. |
| `consumed` | — | `--session dev`, `--cursor` (int, **required**), `--not-responding` | Move the read boundary by hand (watch does this automatically). `--not-responding` suppresses the acknowledgement cue and returns the orb to Listening. |
| `status` | — | `--session dev` | Live server state (JSON). |
| `stop` | — | `--session dev` | Stop a detached server. |
| `turns` | — | `--session dev`, `--limit` (int, `0`=all) | Read the turn log from disk. |
| `timing` | — | `--session dev`, `--limit` (int, default `10`, `0`=all) | Where the time went, per exchange (reads the timing JSONL). |

**Retired commands** (`RETIRED_COMMANDS` in cli.py): `drain` → `watch`. An unknown command returns JSON `{error, code:"unknown_command", remedy, commands[]}`; a *retired* one additionally returns `replaced_by`, `why`, and the **same invocation respelled** (session + cursor carried through) — a migration lifeline for calls in flight, not documentation.

**Contract note:** the machine-readable contract lives in the `DESCRIBE` dict (cli.py ~L416). It encodes the operating loop as `RULE_1..3`, a `watchdog` block (register a recurring "cron" job because nothing in the CLI can force an agent back into `watch`), `the_loop` (doctor → setup → serve → status → watch → say → watch), and per-command `args`/`returns`. `describe` is declared the source of truth over any doc including the README.

---

## 2. Server architecture

`voice_tunnel/server.py` (~180 KB) — a single `aiohttp` web app. `build_app(session, token, gate_enabled)` creates the app and stows one `TunnelState` in `app["state"]`. `run()` validates the session, generates a token if unset, **refuses to bind a non-loopback host without a token**, then warms TTS and the turn model on background threads before serving.

### HTTP + WebSocket endpoints
`app.add_routes([...])`:
- `GET  /` → `handle_index` — serves the single-page client (`voice_tunnel/web/index.html`, ~228 KB / 3745 lines, self-contained, no build step).
- `GET  /health` → `handle_health`
- `GET  /status` → `handle_status` — the state an agent polls.
- `POST /say` → `handle_say` — synthesize + deliver (or hold/queue).
- `POST /shutdown` → `handle_shutdown`
- `POST /consumed` → `handle_consumed` — move the read cursor; drives the acknowledgement-cue decision (`_will_respond`).
- `POST /watching` → `handle_watching` — the agent declaring a `watch` open/closed; **the agent no longer declares its state, the server derives it.**
- `POST /cue` → `handle_cue`
- `POST /rate` → `handle_rate`
- `POST /verbose` → `handle_verbose`
- `POST /wake` → `handle_wake`
- `POST /lane` → `handle_lane`
- `GET  /ws` → `handle_ws` — the audio channel. **Auth is checked here, before `ws.prepare()`** (deliberately not in HTTP middleware, which would leave the mic endpoint open). `WebSocketResponse(heartbeat=30, max_msg_size=8 MiB)`.

The CLI subcommands are thin clients: they POST to these endpoints against a running server, and report `running: false` (exit 3) when none is up.

### The turn log + `--since` cursor contract (`store.py`, stdlib-only)
- One JSONL file per session: `<VOICE_TUNNEL_DIR>/<session>.jsonl`, one turn object per line. Single writer (line-buffered appends) + concurrent whole-file readers → no locking.
- **Turn schema:** `{id, session, t_start, t_end, text, addressed, reason, final, wall, lane?}`. `text` is UNTRUSTED (mic speech, data never instructions). `reason` = why addressed (`wake` | `voice:<sim>` | `not-addressed` | `ambiguous:...`).
- **`lane` semantics (3 states):** absent = the default lane (every pre-lanes turn; keeps them reaching the original agent); present-and-a-name = that lane; present-but-`null` = a *refused* turn that reaches nobody. `stamp_lane` decides whether the key is written at all.
- **`turns_since(cursor)`** returns *every* turn with `id > cursor` plus the new cursor — returning all of them is the contract (the agent reasons for unbounded time between calls). `addressed_only` and `lane` filters drop turns from the *result* but the cursor **still advances past them** (consumed, not deferred — so the room can chatter for an hour without waking or re-reading).
- **`watch(cursor, timeout, poll=0.1)`** blocks until a matching turn exists or timeout; on timeout returns `([], new_cursor)` (empty = heartbeat, not error).
- **`last_turn_id`** is read off disk (`-1` for empty), never a server counter — the fresh-watcher start point.
- **Cursor persistence:** `<session>.consumed.json` holds the read position across restarts (seeded into `TunnelState.consumed_cursor` from disk, not `-1`) — fixing the "307-pending-of-306" replay after a bounce.
- **Lane persistence:** `<session>.lanes.json` (`read_lanes`/`write_lanes`) — the room (registered guest lanes) survives a restart; the default is NOT restored (it's whatever `--wake` was typed this start).
- **Timing log:** `<session>.timing.jsonl` (`timing.py`) — every pipeline stage stamps `mono` + `wall`; read back by `voice-tunnel timing`. Append-only, wrapped so it can never break a session.

### Lanes (multi-agent on one mic) — `lanes.py` (stdlib-only, pure)
- Exactly one lane is **live** at a time. The wake name is a switch thrown once and remembered. `LaneRegistry(default)` holds `_lanes` + `current`; the default lane cannot be removed (it owns all unlabelled historical turns). `BROADCAST = "everyone"` is a reserved, always-addressable destination that no agent may register.
- **Wake-as-switch:** `resolve(text, lanes, current)` returns a `Resolution(action, lane, candidates)` where action ∈ `switch` / `stay` / `refuse`. **The one rule: only an EXACT lane name after a LEADING greeting switches.** Everything weaker resolves to the live lane (a `stay`, which is free because it changes nothing). A greeting-led token that looks like a switch but matches no lane exactly → `refuse` (turn goes to nobody, costs one repeat). Fuzzy matching can only ever cause a refuse or a stay, never a switch. Name rule `^[a-z][a-z0-9]{1,31}$` (one lowercase token — it's matched as the single word after the greeting). This is deliberately NOT addressivity inference (the project's one written anti-goal).
- **Per-lane state on `TunnelState`** (all keyed by lane): `lane_states` (each agent's transcribing/thinking/synthesizing/speaking/idle), `lane_consumed` (per-lane read cursor — the session-wide `consumed_cursor` "is a lie with N lanes"), `lane_read_through` (highest turn in hand when a lane last spoke — the WhatsApp "blue tick" half), `lane_spoke_at`, `lane_held` (clips an off-lane agent made while he talked to someone else — deliberately NOT `undelivered`), `lane_inflight` (sent-to-client-not-yet-confirmed), `lane_expired` (aged-out replies, handed over once on the next watch-open), `lane_holds_turns`, `clip_owner` (which lane each in-flight clip belongs to), `utterance_lane` (which lane he was addressing when he started the utterance — latched on the silence→speech edge so a mid-sentence switch can't redirect words already spoken, spec 023), `watching_lanes`.
- `/ws` `ready` frame sends the whole lane picture on connect (lane, lanes, broadcast, lane_states, watching_lanes, lane_consumed, default_lane, waiting) — a phone reconnects on every lock, so the full snapshot is the common case.

### Audio pipeline (mic in → log)
`handle_ws` loop: BINARY → `_on_audio`, TEXT → `_on_control`.
`_on_audio(raw)`: `pcm16_to_float32` → `resample_linear(client_sr → TARGET_SR=16000)` if needed → `state.capture` (diagnostics) → **`_maybe_barge`** → latch `utterance_lane` on the silence→speech edge → `UtteranceBuffer.feed(samples)`. Not complete → maybe fire a live `partial` preview (`_maybe_partial`, throttled by `PARTIAL_INTERVAL_S=0.7`, skipped if one is running). Complete → `_emit`.
`_emit(completed)`: stamp `utterance_end` → set opener lane `transcribing` → `recognizer.transcribe` off the event loop → drop empty/hallucination (no cue) → **wake gate** `state.wake.evaluate(text)` → **voiceprint** (`embed` + `match`; enroll on wake-confirmed turns) → `should_address(...)` → **lane routing** `lanes_mod.resolve(text, names, addressed_lane)` (refuse ⇒ not addressed + `ambiguous` bump + wake the live lane's watch to ask) → apply switch only on an addressed turn → `store.append_turn(..., stamp_lane=True)` → broadcast `turn` + `consumed` → set opener lane `idle`.
`_flush` handles the mute/orb-tap/dropped-socket close path (flushes a mid-sentence utterance so a disconnect never eats the last words).

### ASR (`asr.py`)
Utterance-buffered (not streaming): buffer until end-of-utterance, transcribe once — the turn boundary *is* the utterance end. Silence discipline against Whisper hallucinations: (1) RMS energy pre-gate (`SILENCE_RMS_FLOOR=0.005`, guarantees a silent room emits zero turns), (2) faster-whisper's built-in Silero VAD, (3) a whole-utterance known-artifact phrase filter (`_HALLUCINATIONS`). Model loads lazily, stays resident; everything above the model boundary is pure. Engine auto-selects **parakeet** (sherpa-onnx, RTF ~0.077) when its model dir is present, else **whisper** (faster-whisper, default `base.en`). Optional contextual-biasing hotwords (`hotwords.txt`, only active when the parakeet dir also has a `bpe.vocab`).

### TTS + delivery (`tts.py`, `speech.py`, `cues.py`)
Pluggable backends: **sapi** (Windows System.Speech via PowerShell — zero-install default), **piper** (in-process model, 7–26× faster than spawning), **kokoro** (24 kHz, one model + one 54-voice pack; speed capped at `KOKORO_SPEED_MAX=2.0`), **none** (silence of the right duration, for plumbing tests). Output always mono 16-bit PCM; sample rate travels with the audio (`TTS_SR=22050` for sapi/piper, 24 kHz kokoro). Every clip is `pad()`-ed with leading/trailing silence because Bluetooth sinks sleep between clips and swallow the first ~100 ms (phone-first). `speech.py` normalizes technical terms (e.g. `0.2.6` → words so the recognizer doesn't hear `026`) and segments a reply where meaning breaks. Cues are synthesized (not sampled), distinct by pitch contour: `heard` (rising), `thinking` (flat mid), `tool` (low short tick), `speaking` (falling).

**Delivery / hold / expiry machinery.** The `_clip_lock` is held across a clip's JSON header AND its binary frame so concurrent `_speak` tasks can't interleave header/bytes on the wire. `undelivered` = clips synthesized while nobody was connected (bounded by count AND age), flushed on reconnect (`_flush_undelivered`, `resumed` frame). `lane_held` = clips for an off-lane agent (never touched by barge-in — the deliberate split from `undelivered`, which barge clears wholesale). `lane_inflight`/`clip_owner`/`_sent_seq` track sent-but-unconfirmed clips: the client's `played` receipt carries a clip id, and `speaking_lane`/`clip_owner` name whose clip is actually on the speaker so the receipt releases the right lane. `_return_inflight(keep_playing=...)` returns in-flight clips to their holds on disconnect or cross-lane barge. `lane_expired` holds aged-out replies, delivered exactly once on the next watch-open (`handle_watching` pops them).

### Derived agent state (`handle_watching` + `_set_agent_state`)
The agent no longer announces what it is doing; the server derives five states from events it can see: `transcribing` (audio arrived + ASR running), `thinking` (a `watch` handed turns over and no new watch started), `synthesizing` (`/say` in flight), `speaking` (client reported playback), `idle` (a `watch` is open). `watch_open`/`watching_lanes` are how a detached-watch-vs-watchdog duplicate is avoided (`watch` refuses to start when one is already open unless `--force`). State is attributed to the lane that owns it so a background agent can't repaint the live orb.

### Voiceprint gating + barge-in (`voiceprint.py`, `_maybe_barge`)
The voiceprint gate is **additive only** — a voice match can only *grant* attention (make the wake word non-mandatory), never withhold it (`AUTO_THRESHOLD=0.50`). Enrolls a running centroid from wake-confirmed turns; local-only, gitignored, never leaves the machine. Barge-in: only *his* voice can interrupt a reply — the gate asks "is this NOT the agent's own echo" (agent echo scores 0.000; `BARGE_IN_THRESHOLD=0.15`, `BARGE_IN_MIN_MS=1000`), requiring the client's `user_speaking` signal AND the voiceprint. A **cross-lane barge** (he spoke to someone else over a clip) stops the audio but returns the un-answered clip to its lane's hold with the hand up (`keep_playing`), distinct from a real interruption of the lane he's on (spec 025: the clip he heard is not returned).

### Security (`security.py`)
Allowlist decides on the direct TCP peer; `X-Forwarded-For` honored only when the peer is a configured trusted proxy (empty by default — VoiceMode post-CVE rule). Auth on the WS handshake, not HTTP middleware. Token via `secrets.token_urlsafe`. `LOOPBACK_CIDRS = 127.0.0.0/8, ::1/128`.

---

## 3. Settings registry (`config.py` `SETTINGS`)

`SETTINGS` is the one registry read by three consumers: `describe`'s env block, `config show`, and the `.env.example` drift test. **38 registered `VOICE_TUNNEL_*` variables.** Defaults below are the code's resolved values.

| Variable | Default | Purpose |
|---|---|---|
| `VOICE_TUNNEL_ENV_FILE` | `<repo>/.env` | path to the settings file itself |
| `VOICE_TUNNEL_TOKEN` | *(generated at serve)* | shared secret for the WS handshake (secret) |
| `VOICE_TUNNEL_ALLOW_CIDRS` | *(empty)* | extra CIDRs allowed (add `100.64.0.0/10` for Tailscale) |
| `VOICE_TUNNEL_TRUSTED_PROXIES` | *(empty)* | trusted proxies; leave empty unless a real proxy fronts this |
| `VOICE_TUNNEL_PUBLIC_URL` | *(empty)* | the https URL a phone opens when a forwarder fronts the port; makes `status.phone.ready` true |
| `VOICE_TUNNEL_DIR` | *(session dir)* | where turn logs live |
| `VOICE_TUNNEL_MODELS_DIR` | *(models dir)* | where downloaded models live |
| `VOICE_TUNNEL_TTS` | *(auto)* | backend: `sapi` \| `piper` \| `kokoro` \| `none` |
| `VOICE_TUNNEL_PIPER_BIN` | *(auto-found)* | piper executable (repo venv or PATH) |
| `VOICE_TUNNEL_PIPER_VOICE` | *(auto-found)* | default `.onnx` voice in the models dir |
| `VOICE_TUNNEL_PIPER_INPROCESS` | `1` | hold the piper voice model in-process (7–26× faster) |
| `VOICE_TUNNEL_KOKORO_VOICE` | `bm_daniel` (DEFAULT_KOKORO_VOICE) | kokoro voice NAME (style vector inside the one pack) |
| `VOICE_TUNNEL_KOKORO_MODEL` | *(auto-found)* | `kokoro-v1.0.onnx` |
| `VOICE_TUNNEL_KOKORO_VOICES` | *(auto-found)* | `voices-v1.0.bin` voice pack (required as well as the model) |
| `VOICE_TUNNEL_SPEECH_SPEED` | *(via `speech_speed()`)* | talk speed; 1.0 native, higher faster (`0.5`–`2.5`; kokoro caps 2.0) |
| `VOICE_TUNNEL_SENTENCE_PAUSE` | *(via `sentence_pause()`)* | seconds of silence between sentences (`0`–`1.5`) |
| `VOICE_TUNNEL_CONSONANT_BOOST` | `0` (CONSONANT_BOOST) | lift consonants for fast speech; off by default |
| `VOICE_TUNNEL_DEESS` | `0` | tame piercing 's'; 0 disables |
| `VOICE_TUNNEL_ASR` | *(auto: parakeet if present)* | `parakeet` \| `whisper` |
| `VOICE_TUNNEL_PARAKEET_DIR` | *(auto)* | sherpa-onnx Parakeet model dir |
| `VOICE_TUNNEL_HOTWORDS_FILE` | `<repo>/hotwords.txt` | contextual-biasing hotword list (needs `bpe.vocab` in parakeet dir) |
| `VOICE_TUNNEL_HOTWORDS_SCORE` | `2.0` | bias strength toward a hotword on final decodes |
| `VOICE_TUNNEL_WHISPER_MODEL` | `base.en` | whisper fallback model |
| `VOICE_TUNNEL_ASR_THREADS` | `4` | ASR worker threads |
| `VOICE_TUNNEL_ASR_BEAM` | `1` | whisper beam width |
| `VOICE_TUNNEL_END_OF_UTTERANCE_MS` | `1500` | silence that ends a turn (timer fallback) |
| `VOICE_TUNNEL_TURN_DETECT` | `1` | use the learned turn model to decide turn end (else the timer) |
| `VOICE_TUNNEL_TURN_THRESHOLD` | *(via `turn_threshold()`)* | probability ≥ which an utterance counts finished |
| `VOICE_TUNNEL_TURN_MIN_SILENCE_MS` | `800` | floor below which no model confidence closes a turn early |
| `VOICE_TUNNEL_TURN_THREADS` | `4` | ONNX intra-op threads for the turn model |
| `VOICE_TUNNEL_BARGE_IN` | `1` | let his voice stop a reply mid-sentence |
| `VOICE_TUNNEL_BARGE_IN_THRESHOLD` | `0.15` (BARGE_IN_THRESHOLD) | voiceprint cosine floor for "a person, not the agent's echo" |
| `VOICE_TUNNEL_WATCH_MAX_S` | `540` | ceiling the `watch` backoff ladder tops out at (9 min, inside a 10-min tool timeout) |
| `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S` | `28800` | flat wait when no turn can arrive (no page / orb off) — 8 h |
| `VOICE_TUNNEL_CUES` | `1` | short non-speech cues so a pause is audible |
| `VOICE_TUNNEL_VERBOSE` | `0` | narrate every action; global, persists, live |
| `VOICE_TUNNEL_OWNER` | `me` | name the voiceprint gallery learns under |
| `VOICE_TUNNEL_WAKE_NAME` | `assistant` (WAKE_NAME) | what you call the agent; "hey <name>" summons it |

**Read but NOT in the registry** (found via grep, so noted for completeness): `VOICE_TUNNEL_HOME` (scopes model/config/gallery dirs to a throwaway root — used by `coldstart.py`) and `VOICE_TUNNEL_PIPER_LENGTH_SCALE` (a legacy alias for speech speed, honored for back-compat). `VOICE_TUNNEL_WAKE_BARE` appears only in a comment (a retired idea) and is **not** read. So the code effectively reads **40** distinct env vars; 38 are the discoverable, settable surface.

Related non-env constants (config.py): `DEFAULT_HOST=127.0.0.1`, `DEFAULT_PORT=8765`, `TARGET_SR=16000`, `TTS_SR=22050`, `SPEED_MIN/MAX=0.5/2.5`, `KOKORO_SPEED_MAX=2.0`, `PAUSE_MAX=1.5`, `PARTIAL_INTERVAL_S=0.7`, `GREETINGS=(hey, hi, ok, okay, yo, hello)`.

---

## 4. Feature set by arc

| Arc | Capability | Status |
|---|---|---|
| **Foundation** | Local aiohttp server; phone-browser client (one self-contained page); WS audio both ways; JSONL turn log + `--since` cursor; `serve`/`watch`/`say`/`status`/`turns`; token + CIDR auth on the WS handshake | built |
| **Foundation** | Utterance-buffered ASR (whisper default, parakeet auto when present) with 3-layer silence/hallucination discipline | built |
| **Reach** | `status.phone.ready` + ngrok detection; `VOICE_TUNNEL_PUBLIC_URL` for tailscale-serve/cloudflared/any reverse proxy; exposure warning (tunnel bypasses the CIDR allowlist so the token is the only gate) | built |
| **Reach** | Packaging (pip extras `piper`/`kokoro`/`parakeet`/`turn`/`all`/`dev`); npm distribution wrapper | partial (specs 002/003 `in_progress`) |
| **Hands-free** | Wake-phrase gate on the transcript ("hey <name>", greeting mandatory, fuzzy name match); voiceprint gate that makes the wake word optional (additive only); `--no-wake-gate` | built |
| **Turn-taking** | Learned turn detection (Smart-Turn v3.2, 8 MB ONNX) at the speech→silence boundary, degrades to the `END_OF_UTTERANCE_MS` timer on any failure; `TURN_MIN_SILENCE_MS` floor | built (spec 004 complete) |
| **Turn-taking** | Barge-in — only his voiceprint can interrupt a reply; cross-lane barge distinguished from a real interruption | built |
| **Waiting** | One waiting command (`watch`) enforcing the loop; empty-watch = heartbeat; backoff ladder (30→…→`WATCH_MAX_S`); flat long wait when disconnected; `say` refuses when he said something you never read | built (specs 005/007 complete) |
| **Voice quality** | Multi-backend TTS (sapi/piper/kokoro/none); Bluetooth padding; peak normalization; live `rate` (speed/pause); technical-term normalization + meaning-based segmentation; de-ess / consonant-boost knobs; per-word `--timings` (kokoro) | built (spec 008 `in_progress`) |
| **Voice quality** | Non-speech cues by pitch contour; cue fires on intent-to-respond, not on capture | built |
| **Lanes** | Several agents on one mic; lane = a switch thrown once (exact-name-after-greeting only, never inferred); registry with immovable default + reserved `everyone` broadcast; room survives restart; per-lane cursors/state/holds; utterance latches its addressed lane | built (specs 012–019, 023–029 complete) |
| **Delivery** | `undelivered` reconnect queue; per-lane `lane_held`; in-flight tracking + `played` receipts naming their own lane/clip; expiry handed over once; clip-stall guard for suspended audio contexts | built (specs 020/024/025/027/028/029 complete; 021/022/026 `in_progress`) |
| **Multi-agent** | Per-lane derived agent state; lane strip in the UI; ambiguity counter + refuse-to-route; `watching_lanes`; observability across N agents | built |
| **Devices** | Device pickers (mic/output pills) with alias-collapsed device counts + route readout; Android enumeration handling | built |
| **Agent-ergonomics** | `describe` machine contract; `doctor`/`setup`; `config` persistence; errors carry `{code, remedy}` + stable exit codes; retired-command respelling; timing log; verbose narration toggle; watchdog guidance | built |

---

## 5. Specs inventory

Frontmatter `status` verbatim (spec 001 uses an inline `**Status:**` line, not YAML). **29 specs; 21 complete/accepted, 6 `in_progress`, plus 002/003 packaging `in_progress`.** Counting "shipped" as complete+accepted = **22 of 29**; 7 remain `in_progress`.

| # | Title | What (one line) | Status |
|---|---|---|---|
| 001 | The voice tunnel | The whole tunnel: capture, wake-gate, STT, turn log + cursor, TTS, the CLI | accepted (inline) |
| 002 | Packaging and Distribution | Ship as a wheel with optional extras | in_progress |
| 003 | npm Distribution | Distribute/launch via npm | in_progress |
| 004 | Learned Turn Detection | Smart-Turn model ends a turn when it *sounds* finished | complete |
| 005 | One wait, gated on speech | A single waiting command gated on server speech signals | complete |
| 006 | The orb being off is not "quiet" | Orb off = conversation not live; replies queue server-side | complete |
| 007 | The tool enforces the loop… | The tool, not the agent's memory, enforces watch-before-say | complete |
| 008 | Speak text as it is meant | Technical-term normalization + phrase-level pacing | in_progress |
| 009 | Do not render a control with nothing to choose | Hide pickers/controls with <2 choices | complete |
| 010 | Every setting the code reads is a setting… | One registry backs describe/config/.env drift test | complete |
| 011 | The agent's context window is a budget… | The CLI spends the agent's context deliberately (per-session state) | complete |
| 012 | Addressing is a lane, not a decision | Lanes by exact-name lookup, never addressivity inference | complete (metaspec) |
| 013 | A lane you can see, and cannot bypass | Visible per-lane delivery + unanswered bound | complete |
| 014 | One orb per agent | Per-lane orb state | complete |
| 015 | One cursor per reader, and idle means idle | Per-lane read cursor; idle disambiguated | complete |
| 016 | A stage belongs to a lane… | Whoever opens a pipeline stage closes it | complete |
| 017 | One cursor answers for one lane, everywhere | Per-lane cursor consistent across every query | complete |
| 018 | What lanes did not reach — the audit | Fix the gaps lanes left (batch-end per lane, timing stamp) | complete |
| 019 | A restart keeps the room | Persist + restore registered lanes across restart | complete |
| 020 | A clip knows when its words land | `played` receipt marks read-through | complete |
| 021 | An expiry tells the agent | Aged-out replies reported to their agent once | in_progress |
| 022 | Sending is not hearing | Hold a copy until the played receipt (don't lose flushed clips) | in_progress |
| 023 | An utterance belongs to the lane he addressed | Latch the lane at utterance start, not at routing | complete |
| 024 | A receipt names its own lane | Per-clip owner map so receipts release the right lane | complete |
| 025 | The clip he heard is not returned | A real barge doesn't re-deliver the heard clip | complete |
| 026 | Every gate knows its lane | Barge/gates read `speaking_lane`, not the live-lane cache | in_progress |
| 027 | A latch outlives the utterance that made it | Attention/lane latch persists past the utterance | complete |
| 028 | A held clip is not said twice | Dedup held-clip delivery | complete |
| 029 | The hand counts what is still unheard | Waiting mark counts still-unheard clips | complete |

---

## 6. Test surface

- **Invocation:** `python -m pytest tests/ -v` (pyproject `[tool.pytest.ini_options]`: `minversion=8.0`, `addopts="-ra -q --strict-markers"`, `testpaths=["tests"]`). Then `python scripts/e2e.py` for the real-browser end-to-end. The dev venv (`voice-tunnel\venv`) carries pytest + playwright + ruff + build + twine (the `dev` extra).
- **Rough count:** **63 `test_*.py` files, ~847 test functions.** Pure logic — no mic, no model (the model boundary is stubbed/injected). `conftest.py` autouse fixtures make every test hermetic: `VOICE_TUNNEL_ENV_FILE` → nonexistent, `VOICE_TUNNEL_DIR` → tmp, `VOICE_TUNNEL_*` snapshot/restore, and `_ngrok_fronts` stubbed so no live tunnel leaks in. `tests/__init__.py` is deliberately a *regular* package (kaleido ships a competing top-level `tests` in site-packages).
- **Harnesses in `scripts/`** (browser/audio checks beyond unit tests, most starting NO server so they're safe during a live session):
  - `e2e.py` — drives real Chrome with a WAV as the fake mic through the full pipeline.
  - `layout.py` — asserts the page never outgrows the viewport (geometry, 5 viewports) — invisible to unit tests + e2e.
  - `uisim.py` — drives the real page into any server state, asserts AND photographs it (named whole-page scenarios); a golden over a pure model can't see a lying view.
  - `orbstate.py` — sweeps all 384/480 combinations through the pure orb reducer, diffs `tests/golden/orb-states.json`, then drives real transitions.
  - `lanestrip.py` — sweeps the pure `lanesView`, then drives the page's message handler with a stubbed socket (no server).
  - `devicepills.py` — sweeps 64 enumeration shapes through pure `pillsView`, then a browser with stubbed `enumerateDevices` (tests Android with no phone).
  - `clipstall.py` — the stall guard: what a clip does when `onended` never arrives (suspended audio context); no server.
  - `channel.py` — orb/mute/channel/speaking controls through a real socket (needs a getUserMedia grant).
  - `bargein.py` — real socket with his recorded speech + the agent's TTS; needs the real voiceprint gallery.
  - `coldstart.py` — builds the wheel from the working tree, installs into a throwaway venv scoped by `VOICE_TUNNEL_HOME`, prints a paste-ready prompt for a blind agent ("never publish to test").
  - Others: `speechcheck.py`, `sharpness.py`/`sharpness_ab.py` (voice sharpness/DIN 45692), `contextcost.py`, `uidiff.py`/`uipreview.py`/`transcriptshot.py`, `laneceiling.py`, `coldstart.py`, `diagram.py`, `probe_capture.py`, `uitest.py` (real mic).
  - Fixtures/golden: `tests/fixtures/`, `tests/golden/` (e.g. `orb-states.json`), `tests/artifacts_probe.wav`.
