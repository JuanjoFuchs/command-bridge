---
title: Browser and Android constraints
description: Secure-context rules, why a LAN IP can never work, the foreground-only mic ceiling on Android, wake lock, the Bluetooth playback padding, and why the page cannot choose an output device on Android.
applies_to: voice_tunnel/web/index.html, voice_tunnel/server.py
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

## The page cannot choose an output device on Android. It can only be honest about it.

**Reported live 2026-08-15:** *"whenever you restart the server, the client is still up. For some
reason, it stops using my Bluetooth device and starts outputting through the earpiece. And
however, the drop-down didn't change. It still says Bluetooth."*

**The routing half is the platform, not us.** `HTMLMediaElement.setSinkId` is unsupported in
Chrome on Android — MDN records it as *"not available due to a limitation in Android"*
([crbug 41276355](https://crbug.com/41276355)), and Firefox Android carries the same note against
its own bug. `AudioContext.setSinkId` is listed as supported there only by BCD's *mirroring* rule,
which is what the data says when nobody has tested the mobile entry, so treat it as unverified
rather than as a yes. Android owns output routing; a page does not get a vote.

**Why the EARPIECE specifically, and not the loudspeaker.** Android engages hardware echo
cancellation by opening the capture on the `VOICE_COMMUNICATION` source, which puts the whole
audio session into communication mode. That mode's fallback output, when no headset route is
currently up, is the earpiece — the receiver you hold to your ear on a call. So "it came out of
the earpiece" is a *signature*, not a detail: it means the session was in communication mode and
the Bluetooth route was not established at the moment the output stream was (re)opened. We ask for
`echoCancellation: true` and must keep doing so — without it the agent transcribes itself — so
this mode is not something to switch off.

**What the client does about it, and what it deliberately does not.** It does not fake a fix. The
page feature-detects *both* halves — is `setSinkId` on the prototype at all, and does
`enumerateDevices()` return any `audiooutput` entries — and renders the output picker only where
both hold. A control that cannot move audio is not shown, because offering one is the same defect
as the reported one, built on purpose.

Where selection *is* possible it goes through one function, `applySink`, which is re-run on every
reconnect, on `devicechange`, and before each clip. A socket coming back means time passed with
the page not looking, and the route is no longer something it can assume survived.

**THE RULE, which outlives the platform question: a picker shows what is IN USE, never what was
requested.** The reported drop-down was the *microphone*, it had not moved, and it was telling the
truth about the only thing it has ever controlled — it just made no claim about direction, so it
got read as the routing control. Two things follow. Paint the input list from the live track's own
`getSettings()`, never from the snapshot taken at `start()`, which cannot change and therefore
cannot report a device that moved. Paint the output list from the sink the `AudioContext` reports
back, never from the id last requested. And note that **re-selecting the option already selected
fires no `change` event at all** — so a stale `<select>` can never be repaired by the user tapping
it, which is why it has to repair itself.

**The diagnostic that settles it on a real phone**, because nothing in this repo can:
`window.__voiceTunnel.sink` carries `supported` (did the platform ever offer a choice), `why`
(which half of the detect failed), `wanted` versus `inUse` (was the choice kept), and `devices`.
Without it, "the platform can't", "it was never applied" and "it was applied and lost" are three
different bugs that look identical from outside — which is how the first guess at this one named a
`setSinkId` call the page did not contain.

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
