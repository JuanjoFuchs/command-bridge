# 001 — The voice tunnel

**Status:** ✅ accepted (2026-07-28) — 66 unit tests, 29 end-to-end checks, three consecutive
clean runs. The one criterion a machine cannot close is a real phone with a real microphone.
**Outcome:** an agent runs one command, opens a page on an Android phone, and holds a spoken
conversation with that agent — hands-free after the initial tap, wake-word gated.

This spec is the WHAT and the acceptance criteria. It does not prescribe the HOW.

---

## Scope

**In:** audio capture from a phone browser, wake-word gating, speech-to-text, a turn log with a
cursor, text-to-speech back to the phone, and the CLI an agent drives.

**Out, explicitly:** agent supervision, steering other agents, fleet monitoring, addressivity
inference, iOS, background/locked-screen capture, any cloud service, any LLM inside this repo.

## The CLI surface

| Command | Contract |
|---|---|
| `describe` | Emits the full command/flag/schema contract as JSON. **The live source of truth.** |
| `serve` | Starts the server. Prints the URL and the token. Runs until stopped. |
| `watch --since <cursor>` | Blocks until a turn with `id > cursor` exists; returns **all** such turns plus the new cursor. On timeout returns empty and the same cursor. |
| `say <text>` | Speaks text to the connected client. Returns when queued, not when finished. |
| `status` | Connection state, session, turn count, TTS backend, ASR model. |

## Acceptance criteria

Each is objectively checkable. `AC-E*` are the end-to-end gate.

### Turn log
- **AC-1** A turn is appended only when speech is confirmed final; a silent channel produces **zero** turns.
- **AC-2** `watch --since N` returns every turn with `id > N`, in order — not just the newest.
- **AC-3** A session id containing `..`, a path separator, or a control char is **rejected**, not sanitized.

### Wake gating
- **AC-4** With gating on, a turn not containing the wake phrase is logged with `addressed: false`.
- **AC-5** A turn containing the wake phrase is logged with `addressed: true`, and the phrase itself is stripped from the text handed to the agent.
- **AC-6** Once addressed, a follow-up turn within the conversation window is also `addressed: true` without repeating the phrase — so a back-and-forth doesn't require saying it every time.

### Security
- **AC-7** A WebSocket connection with no token, or a wrong token, is **refused before any audio frame is read**.
- **AC-8** A request from an IP outside the allowlist is refused, and **a spoofed `X-Forwarded-For` does not change that verdict** when no trusted proxy is configured.
- **AC-9** Token comparison is constant-time.

### Audio
- **AC-10** Every synthesized clip carries ≥0.1 s leading and ≥0.2 s trailing silence.
- **AC-11** The server accepts raw 16-bit PCM frames and resamples to 16 kHz mono for ASR.

### End-to-end (the gate)
- **AC-E1** A real Chrome instance, fed a WAV as its microphone, connects to `vm serve`, and the spoken sentence appears as a turn in the log with `addressed: true`.
- **AC-E2** `watch --since -1` returns that turn.
- **AC-E3** `say` causes audio to arrive at the browser and the page reports it played.
- **AC-E4** The whole loop runs headless, unattended, with a non-zero exit on any failure.

## Non-goals for this spec

Tailscale provisioning is documented, not automated — it is a one-time operator step and the
localhost path is a secure context, so it is not on the critical path for acceptance.

## Result

Every criterion above is exercised by an automated check. `scripts/e2e.py` prints one line per
criterion and exits non-zero on the first failure.

**What the harness proved:** capture → WebSocket → resample → VAD segmentation → Whisper →
wake gate → JSONL log → cursor → TTS → Web Audio playback → acknowledgement, and the same loop
again through the CLI. Observed transcript: `"what is the status of the deploy?"` from spoken
audio, `addressed: true`, wake phrase stripped.

**What it does not prove:** the OS microphone driver. Chrome's fake-capture flags deliver
silence on this machine (measured, see `ai-docs/reference/browser.md`), so the e2e substitutes
the device layer with a real `MediaStream` built from a WAV. Everything above the driver is
genuine. A phone in a hand is the remaining gap, and it is a gap no headless run could close.

**Bugs this spec's acceptance criteria caught that the unit tests did not:** a
`ScriptProcessorNode` created with zero output channels (never pulled by the audio graph, so the
mic silently sent nothing), and a segmenter that reset its trailing-silence counter before the
minimum-length check read it (so a 50 ms click was emitted as a turn).

## Verification strategy

Unit tests cover pure logic with no mic and no model. The end-to-end script drives real Chrome
via Playwright with `--use-file-for-fake-audio-capture`, which makes the browser's microphone a
WAV file — so the acceptance path exercises the genuine browser → WebSocket → ASR → log →
TTS → browser loop, with no human and no mocking of the parts that usually hide bugs.
