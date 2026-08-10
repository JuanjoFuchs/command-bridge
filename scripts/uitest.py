"""Drive the real UI end to end with no human: real Chrome, a real microphone, screenshots.

**How this differs from scripts/e2e.py.** e2e substitutes `getUserMedia` with a MediaStream built
from a WAV, which proves the pipeline but bypasses the operating system and Chrome's audio
processing entirely — so it can never reproduce anything caused by echo cancellation, automatic
gain control, or device warm-up, which is where the hardest bugs have actually lived.

This harness instead speaks out loud into a **VB-Audio Virtual Cable**: TTS is played to
"CABLE Input" and Chrome captures "CABLE Output" as a genuine microphone. Everything Chrome
does to real mic audio happens here, and the machine's own default recording device is never
touched — the page selects the cable by label via `?device=`.

Usage:
    venv/Scripts/python.exe scripts/uitest.py [--headed] [--keep] [--ec 0|1] [--shots DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from voice_tunnel import asr, security, store, tts  # noqa: E402

OUT_MATCH = "CABLE Input"     # we play into this
IN_MATCH = "CABLE Output"     # Chrome records from this
PY = sys.executable

PASSES: list[str] = []
FAILS: list[str] = []


def ok(label: str, detail: str = "") -> None:
    PASSES.append(label)
    print(f"  [PASS] {label}{('  — ' + detail) if detail else ''}", flush=True)


def fail(label: str, detail: str = "") -> None:
    FAILS.append(f"{label}: {detail}")
    print(f"  [FAIL] {label}{('  — ' + detail) if detail else ''}", flush=True)


def check(cond: bool, label: str, detail: str = "") -> bool:
    (ok if cond else fail)(label, detail)
    return bool(cond)


HOST_API_PREFERENCE = ("Windows WASAPI", "MME", "Windows DirectSound")
"""Same physical device appears once per host API. WDM-KS is deliberately excluded: opening it
raises `WdmSyncIoctl: DeviceIoControl` on this machine. Device INDEXES are not stable across
runs, so always resolve by name + host API rather than caching a number."""


def find_device(match: str, want_output: bool) -> int:
    for api_name in HOST_API_PREFERENCE:
        for i, d in enumerate(sd.query_devices()):
            ch = d["max_output_channels"] if want_output else d["max_input_channels"]
            if ch <= 0 or match.lower() not in d["name"].lower():
                continue
            if sd.query_hostapis(d["hostapi"])["name"] == api_name:
                return i
    raise RuntimeError(f"no usable audio device matching {match!r}")


def speak_into_cable(text: str, device: int, on_tick=None,
                     voice: str = "en_GB-alan-medium") -> float:
    """Synthesize and play into the virtual cable, calling `on_tick` between chunks.

    Written as an explicit chunked OutputStream on the CALLING thread rather than `sd.play`
    on a background thread: PortAudio failed with a WDM-KS ioctl error when opened off the
    main thread while Playwright was driving the browser. Chunking is also what lets the
    caller sample the page *while* audio is flowing, which is the only time the level ring and
    the live partial exist.
    """
    pcm, rate = tts.synthesize(text, backend="piper", voice=voice)
    samples = asr.pcm16_to_float32(pcm)
    target = int(sd.query_devices(device)["default_samplerate"])
    if target != rate:
        samples = asr.resample_linear(samples, rate, target)
    # Scale to a realistic speaking level. At unity the cable clips (peak 1.000), which is not
    # what a microphone delivers and would make the harness easier than reality.
    peak = float(np.max(np.abs(samples))) or 1.0
    samples = (samples * (0.6 / peak)).astype(np.float32)
    # A little lead-in so the cable is streaming before speech starts.
    samples = np.concatenate([np.zeros(int(target * 0.3), np.float32), samples])

    if on_tick is None:
        # The known-good path. sd.play hands the whole buffer to PortAudio at once, which is
        # measurably cleaner than writing it in chunks — chunked writes introduced enough
        # discontinuity to garble transcription outright. Use this whenever the audio itself
        # is what is being measured.
        sd.play(samples, samplerate=target, device=device, blocking=True)
        return samples.size / target

    # Instrumented path: chunked so the caller can sample the page mid-utterance. Audio
    # quality here is knowingly worse — every `on_tick` stalls the writer — so callers must
    # NOT assert on transcription from this path.
    block = int(target * 0.2)
    with sd.OutputStream(samplerate=target, device=device, channels=1,
                         dtype="float32", blocksize=block, latency="high") as stream:
        for i in range(0, samples.size, block):
            chunk = samples[i:i + block]
            if chunk.size < block:
                chunk = np.pad(chunk, (0, block - chunk.size))
            stream.write(np.ascontiguousarray(chunk.reshape(-1, 1)))
            on_tick()
    return samples.size / target


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def wait_healthy(port: int, timeout: float = 40) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if http_json(f"http://127.0.0.1:{port}/health", timeout=2).get("ok"):
                return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(0.25)
    raise RuntimeError("server never became healthy")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--ec", default="1", choices=["0", "1"], help="echo cancellation on/off")
    ap.add_argument("--shots", default=os.path.join(ROOT, "tests", "artifacts", "ui"))
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    # This process synthesizes the "user's voice" itself, so it needs Piper too — not just the
    # server subprocess it spawns.
    os.environ.setdefault("VOICE_TUNNEL_PIPER_BIN", os.path.join(ROOT, "venv", "Scripts", "piper.exe"))
    os.environ.setdefault(
        "VOICE_TUNNEL_PIPER_VOICE", os.path.join(ROOT, "models", "en_GB-alan-medium.onnx")
    )

    os.makedirs(args.shots, exist_ok=True)
    out_dev = find_device(OUT_MATCH, want_output=True)
    in_dev = find_device(IN_MATCH, want_output=False)
    print("=" * 72)
    print("voice-tunnel UI test — real Chrome, real microphone via virtual cable")
    print("=" * 72)
    print(f"  play into : [{out_dev}] {sd.query_devices(out_dev)['name']}")
    print(f"  chrome mic: [{in_dev}] {sd.query_devices(in_dev)['name']}")
    print(f"  echoCancellation={args.ec}")

    session, token, port = "uitest", security.generate_token(), free_port()
    workdir = os.path.join(ROOT, "tests", "artifacts", "uitest-sessions")
    os.makedirs(workdir, exist_ok=True)
    for stale in (f"{session}.jsonl", f"{session}.wav"):
        p = os.path.join(workdir, stale)
        if os.path.exists(p):
            os.remove(p)

    env = dict(
        os.environ,
        VOICE_TUNNEL_DIR=workdir,
        VOICE_TUNNEL_TOKEN=token,
        VOICE_TUNNEL_TTS="piper",
        VOICE_TUNNEL_PIPER_BIN=os.path.join(ROOT, "venv", "Scripts", "piper.exe"),
        VOICE_TUNNEL_PIPER_VOICE=os.path.join(ROOT, "models", "en_GB-alan-medium.onnx"),
    )
    proc = subprocess.Popen(
        [PY, "-c",
         f"import sys; sys.path.insert(0, r'{ROOT}'); from voice_tunnel.cli import main; "
         f"raise SystemExit(main(['serve','--session','{session}','--port','{port}']))"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    shot_n = 0

    def shot(page, name):
        nonlocal shot_n
        shot_n += 1
        p = os.path.join(args.shots, f"{shot_n:02d}-{name}.png")
        page.screenshot(path=p)
        print(f"       shot -> {os.path.relpath(p, ROOT)}")
        return p

    browser = pw = None
    try:
        wait_healthy(port)
        ok("server healthy")

        pw = sync_playwright().start()
        launch_args = ["--use-fake-ui-for-media-stream",
                       "--autoplay-policy=no-user-gesture-required"]
        try:
            browser = pw.chromium.launch(headless=not args.headed, channel="chrome",
                                         args=launch_args)
        except Exception:
            browser = pw.chromium.launch(headless=not args.headed, args=launch_args)
        page = browser.new_page(viewport={"width": 760, "height": 900})
        page.on("console", lambda m: print(f"    [console:{m.type}] {m.text}") if m.type == "error" else None)

        url = (f"http://127.0.0.1:{port}/?token={token}"
               f"&device={IN_MATCH.replace(' ', '%20')}&ec={args.ec}&ns=0&agc=0")
        page.goto(url, wait_until="load")
        shot(page, "loaded")

        page.click("#orb")
        page.wait_for_function("() => window.__voiceTunnel && window.__voiceTunnel.connected === true", timeout=25000)
        ok("connected")

        label = page.evaluate("() => window.__voiceTunnel.deviceLabel")
        check(IN_MATCH.lower() in (label or "").lower(),
              "chrome is capturing the virtual cable", f"device={label!r}")
        ok(f"capture backend = {page.evaluate('() => window.__voiceTunnel.capture')}")
        print(f"       constraints: {page.evaluate('() => window.__voiceTunnel.constraints')}")

        page.wait_for_function("() => window.__voiceTunnel.framesSent > 5", timeout=25000)
        shot(page, "listening")

        # ---- speak, exactly as a person would --------------------------------
        lines = [
            "Hey Claude, what is the status of the deploy?",
            "And can you also check the other repository?",
        ]
        peak_level = 0.0
        state = {"shot": False, "ticks": 0}

        def tick():
            """Sample the page between audio chunks — the ring level and the live partial
            only exist while speech is actually flowing."""
            nonlocal peak_level
            state["ticks"] += 1
            if state["ticks"] % 5:          # ~every second; evaluate() stalls the audio stream
                return
            try:
                peak_level = max(peak_level, page.evaluate("() => window.__voiceTunnel.liveLevel"))
                if not state["shot"] and peak_level > 0.15 and state["ticks"] > 20:
                    shot(page, "mid-speech-ring-and-partial")
                    state["shot"] = True
            except Exception:
                pass

        # PASS 1 — clean playback, no instrumentation. Sampling the page between audio chunks
        # stalls the output stream enough to garble what is played, which then reads as an ASR
        # failure. Never instrument the signal you are measuring.
        for line in lines:
            print(f"\n  speaking (clean): {line!r}")
            speak_into_cable(line, out_dev)
            time.sleep(config_end_wait())
            print(f"       turns so far: {len(store.read_turns(session, base=workdir))}")
        shot(page, "after-clean-utterances")
        clean_turns = store.read_turns(session, base=workdir)

        # PASS 2 — instrumented. Transcription quality is NOT asserted here; this pass exists
        # only to prove the ring moves and a live partial renders while speech is flowing.
        print("\n  speaking (instrumented, for UI only)")
        speak_into_cable(
            "Checking whether the level ring moves while I am speaking to you now.",
            out_dev, on_tick=tick,
        )
        time.sleep(config_end_wait())
        shot(page, "after-instrumented-utterance")

        check(peak_level > 0.1,
              "the orb ring reacts to speech (live level)", f"peak level={peak_level:.2f}")
        partials = page.evaluate("() => window.__voiceTunnel.partials")
        check(len(partials) > 0,
              "live partial transcription was shown while speaking",
              f"{len(partials)} partials, last={partials[-1]!r}" if partials else "none")

        turns = store.read_turns(session, base=workdir)
        check(len(clean_turns) >= 1, "speech through a real mic produced a turn",
              f"{len(clean_turns)} clean turns")
        for t in turns:
            print(f"       id={t['id']} addressed={t['addressed']}  {t['text']!r}")

        if clean_turns:
            first = clean_turns[0]["text"].lower()
            check("status" in first or "deploy" in first,
                  "FIRST utterance carries its content", repr(clean_turns[0]["text"]))
            check(clean_turns[0]["addressed"] is True,
                  "first utterance is addressed (wake gate)")
        if len(clean_turns) > 1:
            second = clean_turns[1]["text"].lower()
            check("repositor" in second or "check" in second,
                  "second utterance carries its content", repr(clean_turns[1]["text"]))

        # ---- the agent replies; UI must show it ------------------------------
        print("\n  agent replies")
        http_json(f"http://127.0.0.1:{port}/consumed?token={token}",
                  {"cursor": turns[-1]["id"] if turns else -1, "state": "thinking"})
        time.sleep(0.4)
        shot(page, "thinking-and-divider")
        divider = page.evaluate("() => { const d=document.querySelector('.divider'); return d && d.textContent; }")
        check(bool(divider), "read-boundary divider is rendered in the log", repr(divider))

        said = http_json(f"http://127.0.0.1:{port}/say?token={token}",
                         {"text": "The deploy finished twelve minutes ago and all checks passed."},
                         timeout=180)
        page.wait_for_function("() => window.__voiceTunnel.played.length > 0", timeout=90000)
        ok("reply played in the browser", f"held_for={said.get('held_for')}s")
        shot(page, "after-reply")

        states = page.evaluate("() => window.__voiceTunnel.states")
        for stage in ("transcribing", "thinking", "synthesizing", "speaking"):
            check(stage in states, f"UI displayed the '{stage}' stage",
                  f"saw {sorted(set(states))}")

        # Cues: audible state. Assert they reached the browser AND stayed out of the speech
        # channel — a cue counted as a spoken reply would silently corrupt the played-clip check.
        played_cues = page.evaluate("() => window.__voiceTunnel.cues")
        check(len(played_cues) > 0, "audio cues played in the browser",
              f"{len(played_cues)}: {played_cues[:4]}")
        check(all(not c.startswith("cue-") for c in page.evaluate("() => window.__voiceTunnel.played")),
              "cues are not mistaken for spoken replies")
        rows = page.evaluate("() => [...document.querySelectorAll('.row.claude')].length")
        check(rows <= 1, "cues add no transcript rows", f"{rows} claude rows for 1 reply")

        errors = page.evaluate("() => window.__voiceTunnel.errors")
        check(not errors, "client reported no errors", json.dumps(errors))

        status = http_json(f"http://127.0.0.1:{port}/status?token={token}")
        print(f"\n  audio ingested {status.get('audio_seconds')}s, "
              f"turns {status.get('turns_logged')}")

        # Close the page first: the server finalizes the failsafe WAV on disconnect, and a
        # wave file read before close has no valid header.
        browser.close()
        browser = None
        time.sleep(1.0)
        _analyze_capture(os.path.join(workdir, f"{session}.wav"), turns)
        return 0 if not FAILS else 1

    finally:
        try:
            if browser:
                browser.close()
            if pw:
                pw.stop()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _analyze_capture(wav: str, turns: list) -> None:
    """Measure the audio the server actually received, per turn.

    Spectral centroid is the load-bearing number here: the owner's first utterance measured 405 Hz
    against 1515 Hz for later ones — heavily low-passed, missing the high frequencies that
    carry consonants. Level alone would not have shown that.
    """
    import wave

    if not (os.path.exists(wav) and turns):
        print("  (no capture to analyze)")
        return
    with wave.open(wav, "rb") as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    print(f"\n  captured audio ({a.size / sr:.1f}s):")
    for t in turns:
        seg = a[int(t["t_start"] * sr):int(min(t["t_end"] * sr, a.size))]
        if seg.size < sr // 10:
            continue
        sp = np.abs(np.fft.rfft(seg * np.hanning(seg.size)))
        fr = np.fft.rfftfreq(seg.size, 1 / sr)
        cen = float((sp * fr).sum() / max(sp.sum(), 1e-9))
        rms = float(np.sqrt(np.mean(seg ** 2)))
        print(f"    id={t['id']} rms={rms:.4f} peak={float(np.max(np.abs(seg))):.3f} "
              f"centroid={cen:.0f}Hz")
        if t["id"] == turns[0]["id"]:
            check(cen > 800, "first utterance is not muffled",
                  f"centroid={cen:.0f}Hz (muffled would be <600Hz)")


def config_end_wait() -> float:
    from voice_tunnel import config

    return config.END_OF_UTTERANCE_MS / 1000.0 + 2.5


if __name__ == "__main__":
    rc = main()
    print("\n" + "=" * 72)
    print(f"{len(PASSES)} passed, {len(FAILS)} failed")
    for f in FAILS:
        print(f"  FAILED: {f}")
    raise SystemExit(rc)
