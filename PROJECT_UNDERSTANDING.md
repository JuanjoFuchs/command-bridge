# PROJECT_UNDERSTANDING

Current-state orientation. Keep it short — durable detail lives in `ai-docs/`.

## What this is

A **voice tunnel**: a local CLI an agent starts, which serves a page to a phone browser and
carries audio both ways. The agent that starts it supplies all intelligence.

```
Phone browser (no install)
   |  HTTPS (Tailscale Serve for the phone; http://localhost is already a secure context)
   v
voice-tunnel serve  ── the DUMB half, no LLM ──────────────────────────────┐
   mic  -> wake gate -> ASR -> append turn to sessions/<s>.jsonl  │
   spkr <- TTS <- text handed to `voice-tunnel say`                         │
                                                                  v
                                              The agent that started it (SMART)
                                              voice-tunnel watch --since <cursor> -> reason -> voice-tunnel say
```

## Why it is shaped this way

Forked in spirit from `meeting-copilot` (same author, same machine): **dumb tool + smart
external agent**, a JSONL turn log, and a `watch --since <cursor>` contract that guarantees no
turn is dropped while the agent thinks. We deliberately did **not** adopt the blocking
request/response shape used by `mbailey/voicemode`'s `converse` tool — that design has to fight
MCP call timeouts with queues, callbacks, and pause semantics. Polling a log has none of that.

## What was forked / borrowed, and what changed

| From | Taken | Changed |
|---|---|---|
| `meeting-copilot` (`mc/asr.py`) | Streaming ASR: LocalAgreement-2 commit strategy, RMS silence gate, hallucination filter | Single mic channel only — no diarization, no system-loopback, no voiceprints |
| `meeting-copilot` (`mc/store.py`) | JSONL turn log, id-as-cursor, session-id hardening | Turn schema drops `speaker`; adds `addressed` |
| `mbailey/voicemode` (MIT) | Security posture and audio tunables — **studied, re-implemented, not vendored** | Auth moved onto the WS handshake; constant-time token compare; see below |

**Three lessons taken from VoiceMode's source, each earned the hard way by them:**
1. Their IP allowlist trusted `X-Forwarded-For` unconditionally (GHSA-2qvv-vjq9-g5r4, CVSS 8.6)
   — a spoofed header reached microphone recording. We decide on the direct TCP peer.
2. Their middleware passes non-HTTP scopes straight through, so a WebSocket would be
   **unauthenticated**. Our auth lives on the WS handshake itself.
3. Chime padding (0.1 s leading, 0.2 s trailing) exists because Bluetooth sinks sleep and clip
   playback. This project is phone-first, so nearly every session is Bluetooth.

## Decisions already made (don't relitigate)

- **Wake word only, no addressivity.** You say "hey claude". No inference about whether you're
  being addressed mid-sentence — that was the one real research risk and it is out of scope.
- **Scope is the tunnel.** No agent supervision, steering, or fleet monitoring in this repo;
  `sb sessions` / `agent-mail` / `ccpulse` already do that, and the agent on the far end uses them.
- **Session-based, foreground.** Android Chrome cannot record from a background tab; that is a
  platform ceiling, not a bug to fix. The page holds a Screen Wake Lock instead.
- **Android Chrome is the only target.** No iOS work.

## State

Harness scaffolded. See `specs/001-voice-tunnel.md` for the build unit and its acceptance
criteria, and the task list in the driving session for progress.

## Gotchas

- **A LAN IP is not a secure context.** `getUserMedia` is undefined on `http://192.168.x.x`;
  `http://localhost` is fine. The phone needs real HTTPS → Tailscale Serve.
- **Windows + Python 3.11.** `faster-whisper` lives in the `meeting-copilot` venv on this
  machine; `scripts/` resolve an interpreter that has it.
- **Never `cd` in a chained shell command** when driving this repo from the project notes session —
  the project's permission rules match on the full command prefix.
