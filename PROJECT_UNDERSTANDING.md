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

Forked in spirit from an earlier project by the same author: **dumb tool + smart
external agent**, a JSONL turn log, and a `watch --since <cursor>` contract that guarantees no
turn is dropped while the agent thinks. We deliberately did **not** adopt the blocking
request/response shape used by `mbailey/voicemode`'s `converse` tool — that design has to fight
MCP call timeouts with queues, callbacks, and pause semantics. Polling a log has none of that.

## What was forked / borrowed, and what changed

| From | Taken | Changed |
|---|---|---|
| Prior art: a streaming meeting transcriber | Streaming ASR: LocalAgreement-2 commit strategy, RMS silence gate, hallucination filter | Single mic channel only — no diarization, no system-loopback, no voiceprints |
| Prior art: the same transcriber's turn log | JSONL turn log, id-as-cursor, session-id hardening | Turn schema drops `speaker`; adds `addressed` |
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
  the agent on the far end already has tools for that, and uses them.
- **Session-based, foreground.** Android Chrome cannot record from a background tab; that is a
  platform ceiling, not a bug to fix. The page holds a Screen Wake Lock instead.
- **Android Chrome is the only target.** No iOS work.

## State

Harness scaffolded. See `specs/001-voice-tunnel.md` for the build unit and its acceptance
criteria, and the task list in the driving session for progress.

## Gotchas

- **A LAN IP is not a secure context.** `getUserMedia` is undefined on `http://192.168.x.x`;
  `http://localhost` is fine. The phone needs real HTTPS → Tailscale Serve.
- **Windows + Python 3.11.** `faster-whisper` may live in a different virtualenv than the
  one you are in; `scripts/` resolve an interpreter that has it rather than assuming.
- **Never `cd` in a chained shell command** when an agent drives this repo from another
  working directory — permission rules commonly match on the full command prefix, so
  `cd x && y` is not the same grant as `y`.
