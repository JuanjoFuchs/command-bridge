"""End-to-end acceptance for spec 001 (AC-E1..E4).

Drives a **real browser** whose microphone is a WAV file, so the path under test is the genuine
one: browser mic capture -> WebSocket -> resample -> VAD segmentation -> Whisper -> wake gate ->
JSONL turn log -> `watch` cursor -> TTS -> WebSocket -> Web Audio playback in the page.

Nothing on that path is mocked. That matters, because every component here is individually
plausible and the failures live in the seams — sample-rate mismatches, an utterance that never
ends because the fake mic loops without a gap, audio that decodes but never plays.

Run:  venv/Scripts/python.exe scripts/e2e.py [--headed] [--keep]
Exit: 0 = every assertion held; non-zero = the first failure, printed.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vm import config, security, store, tts  # noqa: E402

SPOKEN = "Hey Claude, what is the status of the deploy?"
EXPECT_WORDS = ("status", "deploy")
REPLY = "The deploy finished twelve minutes ago and all checks passed."

PY = sys.executable
PASSES: list[str] = []


class Failure(RuntimeError):
    pass


def ok(label: str) -> None:
    PASSES.append(label)
    print(f"  [PASS] {label}", flush=True)


def check(condition: bool, label: str, detail: str = "") -> None:
    if not condition:
        raise Failure(f"{label}{(' — ' + detail) if detail else ''}")
    ok(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_fake_mic(path: str) -> str:
    """Synthesize the spoken line and pad it with a long tail of silence.

    The tail is load-bearing. Chrome **loops** the fake capture file, so without a gap longer
    than END_OF_UTTERANCE_MS the speech runs back-to-back forever and the segmenter never sees
    an utterance boundary — the turn would never close and the test would hang.
    """
    import numpy as np

    from vm import asr as _asr

    pcm, rate = tts.synthesize(SPOKEN, backend="sapi")

    # Resample to 48 kHz before writing. Chrome's fake-capture device wants its native rate;
    # handed a 22.05 kHz file it does not error — it silently delivers SILENCE, which then
    # looks exactly like a broken server. Getting this wrong cost a full debug cycle.
    samples = _asr.pcm16_to_float32(pcm)
    s48 = _asr.resample_linear(samples, rate, 48000)
    body = (np.clip(s48, -1, 1) * 32767).astype("<i2").tobytes()

    # The tail is also load-bearing: Chrome LOOPS this file, so without a gap longer than
    # END_OF_UTTERANCE_MS the speech runs back-to-back forever and no utterance ever closes.
    gap_s = (config.END_OF_UTTERANCE_MS / 1000.0) + 1.5
    head = b"\x00\x00" * int(48000 * 0.3)
    tail = b"\x00\x00" * int(48000 * gap_s)
    tts.write_wav(path, head + body + tail, 48000)
    return path


def _fake_mic_script(wav_path: str) -> str:
    """An init script that makes getUserMedia return the WAV as a looping live MediaStream.

    Installed before any page script runs, so the client sees a normal microphone. The audio
    genuinely flows through Web Audio and out over the WebSocket — this replaces the device,
    not the pipeline.
    """
    import base64

    with open(wav_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    return (
        "(() => {"
        f'  const B64 = "{b64}";'
        "  const bin = atob(B64);"
        "  const bytes = new Uint8Array(bin.length);"
        "  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);"
        "  if (!navigator.mediaDevices) navigator.mediaDevices = {};"
        "  navigator.mediaDevices.getUserMedia = async () => {"
        "    const ctx = new (window.AudioContext || window.webkitAudioContext)();"
        "    if (ctx.state === 'suspended') await ctx.resume();"
        "    const buf = await ctx.decodeAudioData(bytes.buffer.slice(0));"
        "    const dest = ctx.createMediaStreamDestination();"
        "    const src = ctx.createBufferSource();"
        "    src.buffer = buf; src.loop = true;"
        "    src.connect(dest); src.start();"
        "    window.__vmFakeMic = { seconds: buf.duration, rate: buf.sampleRate };"
        "    return dest.stream;"
        "  };"
        "})();"
    )


def http_json(url: str, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def wait_for_server(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if http_json(f"http://127.0.0.1:{port}/health", timeout=2).get("ok"):
                return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(0.25)
    raise Failure("server did not become healthy in time")


def wav_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def run(headed: bool, keep: bool) -> int:
    from playwright.sync_api import sync_playwright

    workdir = tempfile.mkdtemp(prefix="vm-e2e-")
    session = "e2e"
    token = security.generate_token()
    port = free_port()
    env = dict(os.environ, VM_DIR=workdir, VM_TOKEN=token, VM_TTS="sapi")

    wav = os.path.join(workdir, "mic.wav")
    print(f"\n== build the fake microphone ==")
    build_fake_mic(wav)
    check(os.path.getsize(wav) > 1000, "AC-E0 fake mic WAV synthesized",
          f"{os.path.getsize(wav)} bytes, {wav_duration(wav):.1f}s")

    print(f"\n== start the tunnel ==  (port {port}, session {session})")
    proc = subprocess.Popen(
        [PY, "-c",
         f"import sys; sys.path.insert(0, r'{ROOT}'); from vm.cli import main; "
         f"raise SystemExit(main(['serve','--session','{session}','--port','{port}']))"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    browser = None
    pw = None
    try:
        wait_for_server(port)
        ok("server is healthy")

        # --- AC-7 / AC-8: the gate refuses before any audio can flow -------------
        print("\n== security gate ==")
        try:
            http_json(f"http://127.0.0.1:{port}/status", timeout=5)
            raise Failure("status without a token should be refused")
        except urllib.error.HTTPError as exc:
            check(exc.code == 403, "AC-7 request with no token is refused", f"HTTP {exc.code}")
        try:
            http_json(f"http://127.0.0.1:{port}/status?token=wrong", timeout=5)
            raise Failure("status with a wrong token should be refused")
        except urllib.error.HTTPError as exc:
            check(exc.code == 403, "AC-7 request with a wrong token is refused", f"HTTP {exc.code}")

        status = http_json(f"http://127.0.0.1:{port}/status?token={token}", timeout=5)
        check(status.get("session") == session, "valid token reaches /status")

        # --- drive a real browser ------------------------------------------------
        print("\n== real browser, WAV as microphone ==")
        pw = sync_playwright().start()
        args = [
            "--use-fake-ui-for-media-stream",        # auto-grant the mic prompt
            "--autoplay-policy=no-user-gesture-required",
        ]
        try:
            browser = pw.chromium.launch(headless=not headed, channel="chrome", args=args)
            which = "chrome"
        except Exception:
            browser = pw.chromium.launch(headless=not headed, args=args)
            which = "bundled chromium"
        ok(f"browser launched ({which})")

        page = browser.new_page()
        # Substitute the DEVICE layer only. Chrome's own fake-capture flags deliver pure
        # silence on this machine — verified with scripts/probe_capture.py, where even the
        # built-in beep measures 0.00015 RMS — so they cannot inject audio here.
        #
        # Instead getUserMedia returns a genuine MediaStream built from the WAV through Web
        # Audio. Everything above the OS microphone driver stays real: the same
        # createMediaStreamSource -> ScriptProcessor -> Int16 -> WebSocket path the phone uses,
        # the real server, real Whisper, real TTS, real playback. Only the driver is stubbed,
        # and a headless browser could never exercise that anyway.
        page.add_init_script(_fake_mic_script(wav))
        page.on("console", lambda m: print(f"    [console:{m.type}] {m.text}", flush=True))
        page.goto(f"http://127.0.0.1:{port}/?token={token}", wait_until="load")
        ok("client page loaded")

        page.click("#orb")   # the user gesture: mic permission + AudioContext unlock
        page.wait_for_function("() => window.__vm && window.__vm.connected === true", timeout=20000)
        ok("websocket connected after the start tap")

        page.wait_for_function("() => window.__vm.framesSent > 5", timeout=20000)
        frames = page.evaluate("() => window.__vm.framesSent")
        ok(f"browser is streaming microphone audio ({frames} frames)")

        # Assert the mic carries SIGNAL, not just frames. Without this check a silent fake
        # device is indistinguishable from a broken server, and you debug the wrong half.
        page.wait_for_function(
            f"() => window.__vm.peakRms > {config.SILENCE_RMS_FLOOR}", timeout=30000
        )
        peak = page.evaluate("() => window.__vm.peakRms")
        check(peak > config.SILENCE_RMS_FLOOR,
              "captured audio is above the silence floor",
              f"peak RMS {peak:.4f} vs floor {config.SILENCE_RMS_FLOOR}")

        # --- AC-E1: the spoken sentence becomes a turn ---------------------------
        print("\n== transcription and wake gating ==")
        deadline = time.time() + 150     # first run downloads the Whisper model
        turn = None
        while time.time() < deadline:
            turns = store.read_turns(session, base=workdir)
            if turns:
                turn = turns[0]
                break
            time.sleep(0.5)
        check(turn is not None, "AC-E1 spoken audio produced a turn in the log",
              "no turn appeared within 150s")

        text = (turn.get("text") or "").lower()
        print(f"    transcript: {turn.get('text')!r}")
        missing = [w for w in EXPECT_WORDS if w not in text]
        check(not missing, "AC-E1 transcript carries the spoken content",
              f"missing {missing} in {text!r}")
        check(turn.get("addressed") is True, "AC-5 wake phrase marked the turn addressed")
        check("claude" not in text, "AC-5 wake phrase stripped from the agent's text",
              f"still present in {text!r}")

        # --- AC-E2: watch returns it from the cursor -----------------------------
        print("\n== the cursor contract ==")
        turns, cursor = store.turns_since(session, -1, base=workdir)
        check(len(turns) >= 1, "AC-E2 watch --since -1 returns the turn")
        check(cursor == turns[-1]["id"], "AC-E2 cursor points at the last returned turn")
        empty, same = store.turns_since(session, cursor, base=workdir)
        check(empty == [] and same == cursor, "AC-2 resuming from the cursor returns nothing new")

        # --- AC-E3: say -> audio arrives and plays -------------------------------
        print("\n== speaking back ==")
        before = page.evaluate("() => window.__vm.played.length")
        said = http_json(f"http://127.0.0.1:{port}/say?token={token}", {"text": REPLY}, timeout=120)
        check(said.get("queued") is True, "AC-E3 say queued a clip", json.dumps(said))
        page.wait_for_function(
            f"() => window.__vm.played.length > {before}", timeout=60000
        )
        played = page.evaluate("() => window.__vm.played")
        check(played[-1] == said.get("id"), "AC-E3 the page played the clip it was sent",
              f"page={played[-1]} server={said.get('id')}")

        # The ack is asynchronous — the page records playback locally and *then* sends the
        # message, so a single read can beat it to the server. Poll rather than point-check.
        final = {}
        ack_deadline = time.time() + 15
        while time.time() < ack_deadline:
            final = http_json(f"http://127.0.0.1:{port}/status?token={token}", timeout=10)
            if final.get("last_played") == said.get("id"):
                break
            time.sleep(0.25)
        check(final.get("last_played") == said.get("id"),
              "AC-E3 the page acknowledged playback back to the server",
              f"server last_played={final.get('last_played')} expected={said.get('id')}")

        errors = page.evaluate("() => window.__vm.errors")
        check(not errors, "client reported no errors", json.dumps(errors))

        # --- the agent-facing surface -------------------------------------------
        # Everything above went through HTTP directly. The product an agent actually drives
        # is the CLI, so exercise that too — otherwise the thing under test is not the thing
        # that ships.
        print("\n== the CLI an agent drives ==")

        def cli(*argv, timeout=180):
            r = subprocess.run(
                [PY, "-c",
                 f"import sys; sys.path.insert(0, r'{ROOT}'); from vm.cli import main; "
                 f"raise SystemExit(main({list(argv)!r}))"],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout,
            )
            try:
                return json.loads(r.stdout or "{}"), r.returncode
            except json.JSONDecodeError:
                raise Failure(f"`vm {' '.join(argv)}` did not emit JSON: {r.stdout[:200]}{r.stderr[:200]}")

        desc, rc = cli("describe")
        check(rc == 0 and desc.get("tool") == "voice-mode", "vm describe returns the contract")
        check("watch" in desc.get("commands", {}), "describe documents every command")

        st, rc = cli("status", "--session", session)
        check(rc == 0 and st.get("session") == session,
              "vm status reaches the running server via the runtime file",
              json.dumps(st)[:200])

        w, rc = cli("watch", "--session", session, "--since", "-1", "--timeout", "5")
        check(rc == 0 and w.get("count", 0) >= 1, "vm watch --since -1 returns the turn")
        check(w.get("cursor") == w["turns"][-1]["id"], "vm watch returns a usable cursor")

        before2 = page.evaluate("() => window.__vm.played.length")
        s2, rc = cli("say", "--session", session, "Acknowledged, standing by.")
        check(rc == 0 and s2.get("queued") is True, "vm say queued a clip", json.dumps(s2)[:200])
        page.wait_for_function(f"() => window.__vm.played.length > {before2}", timeout=60000)
        ok("vm say reached the browser and played")

        # Use an IDLE session, not the live one. The fake mic loops, so the live session keeps
        # producing real turns and a "nothing new" assertion against it is inherently racy —
        # it would fail for the right reason and read as a bug.
        empty, rc = cli("watch", "--session", "e2eidle", "--since", "-1", "--timeout", "2")
        check(rc == 0 and empty.get("count") == 0 and empty.get("cursor") == -1,
              "vm watch times out to an empty heartbeat, not an error",
              json.dumps(empty)[:200])

        print("\n== summary ==")
        print(f"    turns logged     {final.get('turns_logged')}")
        print(f"    audio ingested   {final.get('audio_seconds')}s")
        print(f"    tts backend      {final.get('tts_backend')}")
        print(f"    asr model        {final.get('asr_model')}")
        return 0

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
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        if os.environ.get("VM_E2E_SERVER_LOG"):
            print("\n--- server output ---\n" + (out or ""))
        if keep:
            print(f"\nworkdir kept: {workdir}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--keep", action="store_true", help="keep the temp workdir")
    args = ap.parse_args()

    print("=" * 70)
    print("voice-mode end-to-end acceptance (spec 001)")
    print("=" * 70)
    started = time.time()
    try:
        rc = run(args.headed, args.keep)
    except Failure as exc:
        print(f"\n  [FAIL] {exc}", flush=True)
        print(f"\n{len(PASSES)} passed before the failure. FAILED in {time.time()-started:.1f}s")
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"\n  [ERROR] {type(exc).__name__}: {exc}")
        return 2
    print(f"\nALL {len(PASSES)} CHECKS PASSED in {time.time()-started:.1f}s")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
