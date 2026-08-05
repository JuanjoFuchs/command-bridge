---
title: Browser and Android constraints
description: Secure-context rules, why a LAN IP can never work, the foreground-only mic ceiling on Android, wake lock, and the Bluetooth playback padding.
applies_to: web/index.html, voice_tunnel/server.py
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

## Use AudioWorklet — ScriptProcessorNode dies in a background tab

**Measured 2026-07-29, and it is the single worst bug found in this project.**
`ScriptProcessorNode` runs its callback on the **main thread**, and Chrome throttles main-thread
timers in a backgrounded tab. Switching to another window produced **14 seconds of exact digital
zero** in the server's failsafe capture — `peak=0.000`, not a quiet room, no audio at all — and
every word spoken in that window was lost silently. The page still said "Listening".

Since the entire point is to talk *while doing something else*, that is fatal. `AudioWorklet`
runs on the audio rendering thread and keeps delivering while backgrounded — verified at 2325
frames in 6 seconds with the tab behind another window.

Two things to keep right:
- **Batch in the worklet.** It receives a 128-sample render quantum, ~375/sec at 48 kHz. Posting
  each one is 375 WebSocket messages a second of mostly header. Accumulate ~40 ms first.
- **Keep the ScriptProcessor fallback**, and record which one is live in `window.__voiceTunnel.capture`
  — otherwise "is my audio even flowing" becomes unanswerable again.

> The failsafe WAV is what made this findable. Levels-over-time showed speech, then a block of
> exact zeros, then speech — a shape no amount of reasoning about ASR accuracy would have
> produced. **When transcription looks wrong, listen to what actually arrived first.**

## Audio processing: NS and AGC off, EC on

Chrome's WebRTC processing is tuned for telephony, not recognition. `noiseSuppression` gates and
spectrally subtracts; `autoGainControl` **ramps over the first second or two**, which is the best
explanation for a first utterance transcribing worse than later ones. Dictation tools that beat
us here (Handy) read the raw device.

`echoCancellation` stays **on**: the reply plays out of the speakers into this same microphone,
and without EC the agent transcribes itself. Override per session with `?ns=1&agc=1&ec=0`.

## ScriptProcessorNode must declare an output channel (fallback path only)

`ctx.createScriptProcessor(4096, 1, 0)` — zero outputs — is never pulled by the audio graph, so
`onaudioprocess` **never fires** and the microphone silently produces nothing. Use
`(4096, 1, 1)` and mute it through a zero-gain node into `destination`.

The node is deprecated in favour of `AudioWorkletNode` and Chrome logs a warning. It stays
because it is universally present including headless, and for one local user at 4096 frames the
main-thread cost is irrelevant — swapping it in would add a module-loading failure mode to the
critical path in exchange for nothing.

## Chrome's fake audio capture does not work here (verified)

`--use-fake-device-for-media-capture` and `--use-file-for-fake-audio-capture=<wav>` deliver
**pure silence** on this machine. Measured with `scripts/probe_capture.py`: even the built-in
beep, with no file involved at all, reads a peak RMS of 0.00015 across both installed Chrome and
Playwright's bundled Chromium. It is not the WAV, the sample rate, the path separator, or the
audio constraints — all were tested and eliminated.

So `scripts/e2e.py` substitutes the **device layer only**: an init script replaces
`getUserMedia` with a genuine `MediaStream` built from the WAV via
`AudioContext.createMediaStreamDestination()`. Everything above the OS driver stays real — the
same `createMediaStreamSource` → `ScriptProcessor` → Int16 → WebSocket path the phone uses, the
real server, real Whisper, real TTS, real Web Audio playback.

**Be honest about what that does and does not prove.** It exercises the entire application
pipeline. It does not exercise the operating system's microphone driver — which a headless
browser could never reach anyway. The one thing still requiring a human is a real phone with a
real microphone.

> **The diagnostic that made this findable:** the client tracks `window.__voiceTunnel.peakRms`, and the
> e2e asserts on it before waiting for a transcript. Without that, "the mic is delivering
> silence" and "the server dropped my audio" are indistinguishable from outside, and you debug
> the wrong half for hours. Keep that assertion.

## Audio format on the wire

The browser captures at the device rate (typically 48 kHz) and the ASR wants 16 kHz mono
float32. The client downsamples and sends raw little-endian `Int16` frames over the WebSocket —
no codec, no MediaRecorder container to demux. Simple, lossless enough for speech, and it keeps
the server free of format negotiation.
