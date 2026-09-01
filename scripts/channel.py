"""The control surface — orb, mute, and the speaking signal — through a real browser socket.

**Why a third harness.** `e2e.py` proves audio survives the pipeline and `layout.py` proves the
page fits on a screen. Neither can reach these states, because every one of them requires
`running`, which requires a real `getUserMedia` grant that a headless run cannot produce. So the
controls a person actually touches were the least tested part of the page.


Four states, and the whole point is that they are now INDEPENDENT — that is what splitting mute
off the orb bought:

    channel  mute  | he is heard | he hears
    -----------------------------------------
    open     off   | yes         | yes
    open     on    | no          | yes          <- the case the old orb could not express
    closed   -     | no          | no, queued

Plus the speaking signal: while the client says he is talking, a reply must be HELD, not played.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8799
TOKEN = "channel-check"
SESSION = "chan"

fails = []


def check(ok, label, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(label)


def api(path, payload=None):
    url = f"http://127.0.0.1:{PORT}{path}?token={TOKEN}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


# A scratch session dir and the zero-install voice: this checks CONTROL FLOW, not audio
# quality, and it must run on a machine with no models downloaded.
env = dict(os.environ, COMMAND_BRIDGE_DIR=tempfile.mkdtemp(prefix="voice-tunnel-chan-"),
           COMMAND_BRIDGE_TTS="sapi")
server = subprocess.Popen(
    [sys.executable, f"{ROOT}/bin/voice-tunnel-run.py", "serve",
     "--session", SESSION, "--port", str(PORT), "--token", TOKEN],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

try:
    time.sleep(7)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{PORT}/?token={TOKEN}", wait_until="load")
        page.wait_for_function("() => window.__voiceTunnel && window.__voiceTunnel.connected", timeout=15000)

        # The page has loaded but the orb was never tapped: the channel must be CLOSED, so a
        # reply is queued rather than played into a room nobody opened.
        st = api("/status")
        check(st["channel_open"] is False, "a freshly loaded page starts with the channel closed")

        before = st["undelivered"]
        r = api("/say", {"text": "Queued while closed."})
        check(r["delivered"] is False, "a reply is not delivered while the channel is closed")
        check(r.get("reason") == "channel_closed",
              "the reason distinguishes a closed channel from a dropped client",
              f"reason={r.get('reason')!r}")
        check(api("/status")["undelivered"] == before + 1, "it went to the undelivered queue")

        # Open the channel the way the orb does. The queue must drain on its own.
        page.evaluate("() => window.__voiceTunnel.signal('channel', true)")
        time.sleep(1.5)
        st = api("/status")
        check(st["channel_open"] is True, "the client can open the channel")
        check(st["undelivered"] == 0, "opening the channel drains what was queued",
              f"undelivered={st['undelivered']}")

        # Mute is now INDEPENDENT: he cannot be heard, but he still hears.
        page.evaluate("() => window.__voiceTunnel.signal('muted', true)")
        time.sleep(0.8)
        st = api("/status")
        check(st["muted"] is True and st["channel_open"] is True,
              "muted and channel-open are independent states")
        r = api("/say", {"text": "You can still hear this."})
        check(r["delivered"] is True, "a muted user still RECEIVES replies")

        # The speaking signal must HOLD a clip rather than play over him.
        #
        # THE FLAG ALONE IS NO LONGER A STATE THAT CAN HAPPEN. Since spec 005 the server publishes
        # both speech signals false once no audio frame has arrived for END_OF_UTTERANCE_MS,
        # because in production "he is speaking" and "frames are arriving" are the same fact — a
        # page asserting the first while the second has stopped is a page that has frozen, and
        # under a speaking-gated wait that lie hangs the tunnel outright.
        #
        # So the harness now pushes real PCM alongside the flag. That is not a workaround for the
        # guard: sending only the flag was asserting an impossible state, and it had quietly made
        # the hold check below VACUOUS — it measured SPEAK_GRACE_S whether or not anything was
        # holding, so it would have passed against a server that never held a clip at all.
        #
        # `capturing` has to be asserted too, and that is the guard being RIGHT rather than in the
        # way: with the microphone released there is nobody to hear, so a page claiming to be
        # speaking while not capturing is exactly the contradiction the guard exists to refuse.
        # The harness is simulating a live microphone, so it has to say so.
        #
        # The frames are pumped by an interval INSIDE the page. A Python thread cannot do it:
        # Playwright's sync API is greenlet-based and calling it from another thread raises
        # "cannot switch to a different thread".
        page.evaluate("""() => {
          const d = window.__voiceTunnel;
          d.signal('muted', false);
          d.signal('capturing', true);
          d.signal('speaking', true);
          d.pushAudio(600);
          window.__pump = setInterval(() => d.pushAudio(200), 150);
        }""")
        time.sleep(0.4)
        st = api("/status")
        check(st["user_speaking"] is True, "the server records the speaking signal")
        check(st["speech_active"] is True,
              "and the segmenter agrees, because real frames arrived",
              f"speech_active={st['speech_active']} frames={st['frames_received']}")

        t0 = time.time()
        r = api("/say", {"text": "This must wait."})
        held = time.time() - t0
        check(r["held_for"] >= 0.5, "a reply is HELD while he is speaking",
              f"held_for={r['held_for']}s wall={held:.1f}s")

        page.evaluate("""() => {
          clearInterval(window.__pump);
          window.__voiceTunnel.signal('speaking', false);
        }""")
        # Long enough for the frame-recency guard to conclude the audio stopped. This is the
        # server-side half of "he finished", and it is what the wait is now gated on — so it is
        # worth asserting directly rather than only through the hold it produces.
        time.sleep(2.5)
        st = api("/status")
        check(st["speech_active"] is False and st["user_speaking"] is False,
              "frames stopping reads as speech stopping",
              f"speech_active={st['speech_active']} user_speaking={st['user_speaking']}")
        t0 = time.time()
        r = api("/say", {"text": "This can go now."})
        check(r["held_for"] < 2.0, "and released once he stops",
              f"held_for={r['held_for']}s")

        browser.close()
finally:
    server.terminate()

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
