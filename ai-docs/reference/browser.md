---
title: Browser and Android constraints
description: Secure-context rules, why a LAN IP can never work, the foreground-only mic ceiling on Android, wake lock, and the Bluetooth playback padding.
applies_to: web/index.html, vm/server.py
read_before: touching the web client, changing how the phone reaches the server, or debugging "no mic"
---

# Browser and Android constraints

Target is **Android Chrome only**. No iOS work.

## Secure context — the constraint that dictates the architecture

`getUserMedia` requires a secure context. Concretely:

| Origin | Mic works? |
|---|---|
| `http://localhost` / `127.0.0.1` | **Yes** — localhost is a secure context by definition |
| `http://192.168.x.x` (LAN IP) | **No** — `navigator.mediaDevices` is `undefined`, access throws `TypeError` |
| `https://<host>.<tailnet>.ts.net` | **Yes** — Tailscale Serve provisions a real Let's Encrypt cert |

**This is why the phone path requires Tailscale and cannot be "just the LAN IP".** It is not a
preference or a hardening step; a LAN IP produces no microphone at all. Localhost being exempt
is what lets the automated e2e test run without any TLS setup.

## The mic is foreground-only. This is a ceiling, not a bug.

Per Chrome's own documentation: a site can record while you are on it, but **if you switch to
another tab or another app, it cannot record**. True background capture needs a native
foreground service (`FOREGROUND_SERVICE_MICROPHONE`) — i.e. an app, which is out of scope.

Consequences, all deliberate:
- The product is **session-based**: you open the page for a working block, not all day.
- The page must keep the screen alive itself (below) rather than assume the user will.
- "Ambient, phone in pocket, screen off" is **not achievable in a browser.** Don't try.

## Screen Wake Lock, not a third-party app

`navigator.wakeLock.request("screen")` keeps the screen from dimming or locking. Baseline 2025,
supported in Android Chrome, requires a secure context (which we already have).

The lock is **auto-released when the document becomes inactive**, so it must be re-acquired:

```js
document.addEventListener("visibilitychange", async () => {
  if (wakeLock !== null && document.visibilityState === "visible")
    wakeLock = await navigator.wakeLock.request("screen");
});
```

This replaces installing a Caffeine-type app — the page keeps itself awake, nothing to install.

## The start button is a requirement, not decoration

Autoplay policy blocks audio playback until the user has interacted with the page. The session
needs an explicit tap to start anyway (mic permission), and that same gesture unlocks the
`AudioContext` for TTS playback. **Resume the `AudioContext` inside the click handler** — doing
it later, off a network event, is too late and playback silently fails.

## Bluetooth padding — pad every clip

Bluetooth audio sinks power down between sounds and **clip the first ~100 ms** of playback.
Nearly every session here is Bluetooth (earbuds, headset audio), so:

- **0.1 s of leading silence** before any cue or utterance, so the sink is awake by the time
  speech starts.
- **0.2 s of trailing silence** after, so the tail is not cut.

Without this the wake acknowledgement and the first syllable of every reply get eaten, and it
presents as "the TTS is broken" — a bug that costs a week if you don't know to look here.

## Audio format on the wire

The browser captures at the device rate (typically 48 kHz) and the ASR wants 16 kHz mono
float32. The client downsamples and sends raw little-endian `Int16` frames over the WebSocket —
no codec, no MediaRecorder container to demux. Simple, lossless enough for speech, and it keeps
the server free of format negotiation.
