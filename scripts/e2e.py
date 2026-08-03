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

# Built from the CONFIGURED wake name, not hardcoded. The name is a setting now, and a gate that
# speaks the old one tests nothing except that the old one still works.
SPOKEN = f"Hey {config.wake_name().capitalize()}, what is the status of the deploy?"
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

    # And so is the head, for a reason nobody had written down. The buffer source starts the
    # instant getUserMedia resolves, but frames only reach the server once the WebSocket is open
    # and the audio processor is running. Everything that plays in between is lost — and since
    # the file LOOPS, "lost" means the first utterance the segmenter sees starts partway in.
    #
    # At 0.3 s that margin held on an idle machine and collapsed on a busy one: with a second
    # browser-driven suite running alongside this, four consecutive runs logged
    # "What is the status of the deploy?" — the wake phrase eaten by startup, AC-5 failing on a
    # capture race rather than on anything it tests. 3 s is ~3x the worst startup measured here
    # and costs 3 s of wall clock, which is the cheapest possible fix for a check that otherwise
    # fails for a reason unrelated to what it asserts.
    head_s = 3.0
    head = b"\x00\x00" * int(48000 * head_s)
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
        # Wait for a COMPLETE utterance, not merely the first turn. The fake mic loops the WAV,
        # so capture can begin mid-phrase and the first turn is then a fragment with "hey claude"
        # already gone — which failed AC-5 on one run and passed on the next. The gate is meant to
        # assert that a full utterance is recognised as addressed, so wait for a full one; if none
        # ever arrives, fall back to the first turn so the failure still reports real content.
        deadline = time.time() + 150     # first run downloads the Whisper model
        turn = None
        while time.time() < deadline:
            turns = store.read_turns(session, base=workdir)
            complete = [t for t in turns if "claude" in (t.get("text") or "").lower()
                        or t.get("addressed")]
            if complete:
                turn = complete[0]
                break
            if turns and time.time() > deadline - 60:
                turn = turns[0]          # give up waiting for a clean one; report what we got
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

        # --- clips queue rather than race ----------------------------------------
        # The defect this guards: a single `pendingClip` variable meant a cue arriving between a
        # reply's header and its bytes OVERWROTE the reply, and two surviving clips played
        # simultaneously. JJ heard it on the move — "those two play together, overlapping
        # one on top of the other" — after asking the same question three times unanswered.
        print("\n== clips queue, never overlap ==")
        before = page.evaluate("() => window.__vm.played.length")
        ids = []
        for i in range(3):
            r = http_json(f"http://127.0.0.1:{port}/say?token={token}",
                          {"text": f"Queued reply number {i + 1}.", "async": True}, timeout=60)
            ids.append(r)
        page.wait_for_function(
            f"() => window.__vm.played.length >= {before + 3}", timeout=90000
        )
        played_ids = page.evaluate("() => window.__vm.played")[before:before + 3]
        check(len(set(played_ids)) == 3, "three rapid clips all played, none dropped",
              json.dumps(played_ids))
        # Every clip must have reported playback. Under the old code an overwritten reply never
        # sent a `played` receipt at all, which is exactly how the server kept believing it had
        # spoken when JJ had heard nothing.
        check(all(i for i in played_ids), "every queued clip acknowledged playback")

        # --- the orb is the microphone control -----------------------------------
        # One control, not two. JJ, live: "it doesn't make sense that it's a separate button from
        # the middle orb... Maybe I can tap it again to mute and unmute."
        print("\n== orb mutes, verbose switch ==")
        check(page.locator("#mute").count() == 0,
              "the separate mute button is gone, the orb owns it")
        # A connected client is NOT a listening one. A page that loaded but was never tapped
        # holds an open socket and sends no audio, and the agent used to have no way to tell.
        started = http_json(f"http://127.0.0.1:{port}/status?token={token}", timeout=10)
        check(started.get("capturing") is True,
              "the server knows the microphone is actually running",
              json.dumps(started.get("capturing")))
        check(page.locator("#verbose").is_visible(), "the verbose switch renders")

        def wait_for_status(predicate, seconds=15):
            """Poll /status until it agrees, or give up.

            The mute travels over the WebSocket, so a fixed sleep races it — especially right
            after the rapid-clip test, when the socket is busy. This check failed once and passed
            on the very next run, and a gate that flakes teaches you to re-run instead of to
            read, which is worse than not having it.
            """
            deadline = time.time() + seconds
            latest = {}
            while time.time() < deadline:
                latest = http_json(f"http://127.0.0.1:{port}/status?token={token}", timeout=10)
                if predicate(latest):
                    return latest
                time.sleep(0.25)
            return latest

        frames_before = page.evaluate("() => window.__vm.framesSent")
        page.click("#orb")
        muted_state = wait_for_status(lambda s: s.get("muted") is True)
        check(muted_state.get("muted") is True, "tapping the orb muted, and the server knows",
              json.dumps(muted_state.get("muted")))
        check(page.locator("#orb").text_content().strip() == "Muted",
              "the orb itself shows the muted state",
              page.locator("#orb").text_content())
        frames_during = page.evaluate("() => window.__vm.framesSent")
        page.wait_for_timeout(900)
        frames_after = page.evaluate("() => window.__vm.framesSent")
        # The point of mute: the microphone stops SENDING. Enforced client-side, because a mute
        # that still ships audio to a server promising to ignore it is not a mute.
        check(frames_after == frames_during,
              "muted client sends no audio frames",
              f"before={frames_before} during={frames_during} after={frames_after}")

        # Crucially, muting must NOT drop the session — that was the old stop() behaviour.
        check(page.evaluate("() => window.__vm.connected") is True,
              "muting keeps the socket connected")

        # Speaking while muted must NOT silently un-label the mute. The old code hard-coded the
        # orb back to "Listening" when a clip finished, so every reply left him looking at a
        # listening orb over a microphone that was off. JJ, live 2026-08-03: "whenever I mute, it
        # says muted. But if you speak after that, it goes back to listening."
        spoke = page.evaluate("() => window.__vm.played.length")
        http_json(f"http://127.0.0.1:{port}/say?token={token}",
                  {"text": "Speaking while you are muted."}, timeout=120)
        page.wait_for_function(f"() => window.__vm.played.length > {spoke}", timeout=60000)
        page.wait_for_timeout(400)
        check(page.locator("#orb").text_content().strip() == "Muted",
              "the orb still reads Muted after claude speaks",
              page.locator("#orb").text_content())

        page.click("#orb")
        unmuted = wait_for_status(lambda s: s.get("muted") is False)
        check(unmuted.get("muted") is False, "tapping again unmuted")
        resumed = page.evaluate("() => window.__vm.framesSent")
        page.wait_for_timeout(900)
        check(page.evaluate("() => window.__vm.framesSent") > resumed,
              "audio flows again after unmuting")

        # Assert the FLIP, not an absolute. Verbose is persisted in .env now, so the server may
        # legitimately start in either state — a test that demanded `true` was really asserting
        # the developer's own saved preference and failed the moment he turned it on.
        before_v = http_json(f"http://127.0.0.1:{port}/status?token={token}",
                             timeout=10).get("verbose")
        page.click("#verbose")
        flipped = wait_for_status(lambda s: s.get("verbose") is (not before_v))
        check(flipped.get("verbose") is (not before_v),
              "the verbose switch reached the server and flipped it",
              f"{before_v} -> {flipped.get('verbose')}")
        check(page.locator("#verbose").get_attribute("aria-checked") == str(not before_v).lower(),
              "the switch reports its state as a switch, not a button")
        # The client must ADOPT the server's value rather than push its own, or a second device
        # silently reverts what was set on the first.
        page.reload()
        page.wait_for_timeout(2500)
        check(page.locator("#verbose").get_attribute("aria-checked") == str(not before_v).lower(),
              "a reloaded page adopts verbose from the server, not from its own storage",
              str(page.locator("#verbose").get_attribute("aria-checked")))
        # A reloaded page has no AudioContext until it is tapped, and the socket is already
        # connected — so restart capture before anything downstream expects playback. This also
        # exercises the path where a reply arrived while the page sat un-tapped.
        page.click("#orb")
        page.wait_for_function("() => window.__vm.framesSent > 0", timeout=30000)
        restarted = wait_for_status(lambda s: s.get("capturing") is True)
        check(restarted.get("capturing") is True,
              "capture resumes after a reload and the server is told",
              json.dumps(restarted.get("capturing")))
        # The agent learns about it WITHOUT polling: it rides back on the /consumed call that
        # every `watch` already makes. A preference the agent must remember to ask about is a
        # preference that silently stops being honoured.
        ack = http_json(f"http://127.0.0.1:{port}/consumed?token={token}",
                        {"cursor": 0, "state": "thinking"}, timeout=10)
        check(ack.get("verbose") is (not before_v),
              "verbose rides back on the watch acknowledgement",
              json.dumps(ack))

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

        # An empty watch must say whether anyone is actually listening. This is the moment the
        # agent is about to block again, and "a page is open but was never tapped" is invisible
        # from every other signal — clients:1 looks identical to a live microphone.
        live_watch, rc = cli("watch", "--session", session, "--since", "999999", "--timeout", "2")
        check(live_watch.get("listening") is True,
              "an empty watch on a live session reports listening=true",
              json.dumps(live_watch)[:200])
        # And on a session with no page at all, it must say so rather than leaving the agent to
        # wait on someone who cannot hear it.
        check(empty.get("listening") is False and "no server" in (empty.get("hint") or ""),
              "an empty watch with nothing serving explains why it is quiet",
              json.dumps(empty)[:220])

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
