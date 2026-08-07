"""The orb's state machine — exhaustively as pure logic, then as real transitions.

**Why this harness exists, in one sentence:** the orb said "Thinking" with no elapsed-seconds
counter, and nothing in the suite could have caught it.

JJ, live 2026-08-07: *"the thinking status had the timer, the seconds counter, and then the
second counter disappeared while it was in thinking status. So I want to make sure we do proper
unit testing of that logic and proper UI testing of the transitions, so that we can catch
regressions."*

The bug had two halves, and they need two different kinds of test:

1. **The server published mute through the agent-state channel** (`_set_agent_state("idle")` on
   every mute toggle), so muting mid-thought erased what the agent was doing. That is a
   TRANSITION bug — it only appears when two events interleave — and it is covered at the bottom
   of this file, against a real server and a real browser socket.

2. **The page painted the orb from four separate handlers**, so whichever ran last won and none
   of them knew about the others. That is a LOGIC bug, and the fix was to make the orb a pure
   function of a model (`orbView`). Pure means it can be swept exhaustively: every combination of
   running x phase x channel x mute x playing x agent-state, 300-odd cases, in about a second.

**On snapshot testing** (he asked): yes, but of the MODEL, not of pixels. The golden file below
records what every state combination renders to. A screenshot diff on this page would fail on
every glow, gradient and animation frame while missing "the label is right but the clock is
gone" — which is the actual defect. A state snapshot fails on exactly that and nothing else.

Run: `python scripts/orbstate.py [--update]`
"""
import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "tests", "golden", "orb-states.json")
PORT = 8801
TOKEN = "orb-check"
SESSION = "orb"
UPDATE = "--update" in sys.argv

fails = []

# A microphone that exists. Every state below "Tap to start" requires `running`, which requires a
# real getUserMedia grant — so without this the harness would only ever see the one state it does
# not need to test. An oscillator is enough: this checks the STATE MACHINE, and e2e.py is where
# audio content is proved.
FAKE_MIC = """(() => {
  if (!navigator.mediaDevices) navigator.mediaDevices = {};
  navigator.mediaDevices.getUserMedia = async () => {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === 'suspended') await ctx.resume();
    const dest = ctx.createMediaStreamDestination();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    // SILENT on purpose. A stream loud enough to hear is a stream the server segments, and it
    // will happily transcribe a test tone — which then writes agent_state out from under the
    // assertions below. This harness needs frames to exist, not to mean anything; e2e.py is
    // where audio with content is proved.
    gain.gain.value = 0.0;
    osc.frequency.value = 180;
    osc.connect(gain); gain.connect(dest); osc.start();
    return dest.stream;
  };
  if (!navigator.mediaDevices.enumerateDevices) {
    navigator.mediaDevices.enumerateDevices = async () => [];
  }
})();"""


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


AGENT_STATES = ["idle", "transcribing", "thinking", "synthesizing", "waiting", "speaking"]
PHASES = ["idle", "starting", "warming", "live", "denied"]
TIMED = {"transcribing", "thinking", "synthesizing", "waiting"}

env = dict(os.environ, VOICE_TUNNEL_DIR=tempfile.mkdtemp(prefix="voice-tunnel-orb-"),
           VOICE_TUNNEL_TTS="sapi")
server = subprocess.Popen(
    [sys.executable, f"{ROOT}/bin/voice-tunnel-run.py", "serve",
     "--session", SESSION, "--port", str(PORT), "--token", TOKEN],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

try:
    time.sleep(7)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--use-fake-device-for-media-stream",   # a REAL MediaDevices, silent hardware
            "--use-fake-ui-for-media-stream",       # auto-grant, no prompt to click
            "--autoplay-policy=no-user-gesture-required",
        ])
        page = browser.new_page()
        page.add_init_script(FAKE_MIC)
        page.goto(f"http://127.0.0.1:{PORT}/?token={TOKEN}", wait_until="load")
        page.wait_for_function(
            "() => window.__voiceTunnel && window.__voiceTunnel.orbView", timeout=15000)

        # ---------------------------------------------------------------- pure logic
        combos = [
            {"running": r, "phase": ph, "channelOpen": c, "muted": m, "playing": pl,
             "agentState": a}
            for r, ph, c, m, pl, a in itertools.product(
                [True, False], PHASES, [True, False], [True, False], [True, False], AGENT_STATES)
        ]
        views = page.evaluate(
            "(cases) => cases.map(c => window.__voiceTunnel.orbView(c))", combos)
        snapshot = {
            "|".join(str(c[k]) for k in
                     ("running", "phase", "channelOpen", "muted", "playing", "agentState")): v
            for c, v in zip(combos, views, strict=True)
        }
        check(len(snapshot) == len(combos), "every combination produced a view",
              f"{len(snapshot)} cases")

        # The properties that matter, asserted as rules rather than read off the snapshot — a
        # golden file only catches CHANGE, and cannot say whether the current behaviour is right.
        live = [(c, v) for c, v in zip(combos, views, strict=True)
                if c["running"] and c["phase"] == "live" and c["channelOpen"]
                and not c["playing"]]

        check(all(v["timer"] == c["agentState"]
                  for c, v in live if c["agentState"] in TIMED),
              "every working state runs its own clock, muted or not")
        check(all(v["timer"] is None for c, v in live if c["agentState"] not in TIMED),
              "idle and speaking never run a clock")

        # THE REGRESSION. Mute must change the idle label and nothing else — no state, no clock.
        by_key = {(c["muted"], c["agentState"]): v for c, v in live}
        differs = [s for s in AGENT_STATES
                   if by_key[(True, s)] != by_key[(False, s)]]
        check(differs == ["idle"],
              "mute changes ONLY the idle label, never another state and never the clock",
              f"states affected by mute: {differs}")
        check(by_key[(True, "idle")]["label"] == "Muted"
              and by_key[(False, "idle")]["label"] == "Listening",
              "muted renames Listening to Muted")
        check(by_key[(True, "thinking")]["label"] == "Thinking"
              and by_key[(True, "thinking")]["timer"] == "thinking",
              "muted while thinking still says Thinking and still counts",
              "the exact case he reported")

        # Precedence, each layer answering a different question.
        check(all(v["label"] == "Tap to start"
                  for c, v in zip(combos, views, strict=True)
                  if not c["running"] and c["phase"] not in ("starting", "denied")),
              "no session means Tap to start, whatever the server says")
        check(all(v["label"] == "No mic" for c, v in zip(combos, views, strict=True) if c["phase"] == "denied"),
              "a refused microphone says so ON THE ORB, from every other state",
              "silence here reads as a tap that never registered")
        check(all(v["label"] == "Warming up"
                  for c, v in zip(combos, views, strict=True) if c["running"] and c["phase"] == "warming"),
              "warming outranks everything below it")
        check(all(v["label"] == "Off" for c, v in zip(combos, views, strict=True)
                  if c["running"] and c["phase"] == "live" and not c["channelOpen"]),
              "a closed channel reads Off, not Muted and not the agent's state")
        check(all(v["label"] == "Speaking" and v["timer"] is None for c, v in zip(combos, views, strict=True)
                  if c["running"] and c["phase"] == "live" and c["channelOpen"] and c["playing"]),
              "audible playback wins the label and carries no clock")

        # ---------------------------------------------------------------- snapshot
        if UPDATE:
            os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
            with open(GOLDEN, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=1, sort_keys=True)
            print(f"\nwrote {GOLDEN} ({len(snapshot)} states)")
        elif os.path.exists(GOLDEN):
            with open(GOLDEN, encoding="utf-8") as f:
                golden = json.load(f)
            drift = sorted(k for k in set(golden) | set(snapshot)
                           if golden.get(k) != snapshot.get(k))
            check(not drift, "the state table matches the golden snapshot",
                  "" if not drift else f"{len(drift)} changed, first: {drift[0]} "
                                       f"{golden.get(drift[0])} -> {snapshot.get(drift[0])}")
        else:
            check(False, "golden snapshot exists", "run with --update to create it")

        # ---------------------------------------------------------------- real transitions
        # CLICK the real controls rather than calling diag.signal. `signal` talks to the
        # SERVER only — it never touches the page's own model — so an assertion about what the
        # ORB says has to go through the button, or it passes without testing anything. This
        # harness caught exactly that mistake in its own first draft.

        # Start a genuine session: the tap is the browser's own requirement for a microphone, so
        # this is the same path a person takes rather than a back door around it.
        page.click("#orb")
        page.wait_for_function("() => window.__voiceTunnel.framesSent > 3", timeout=20000)
        page.wait_for_function(
            "() => document.getElementById('orblabel').textContent === 'Listening'", timeout=15000)
        check(True, "a real session starts and settles on Listening")
        time.sleep(0.4)

        def orb():
            return page.evaluate("() => window.__voiceTunnel.orb")

        def label():
            return page.evaluate("() => document.getElementById('orblabel').textContent")

        def timer_visible():
            return page.evaluate(
                "() => { const e = document.getElementById('orbtimer');"
                "        return !e.hidden && /\\d/.test(e.textContent); }")

        # Drive the agent through a working state the way the server does.
        api("/consumed", {"cursor": 0, "state": "thinking"})
        time.sleep(0.7)
        check(label() == "Thinking", "a thinking agent shows Thinking", f"label={label()!r}")
        check(timer_visible(), "and the clock is running")

        # THE BUG, end to end: mute while it is thinking.
        page.click("#mute")
        time.sleep(0.9)
        check(api("/status")["agent_state"] == "thinking",
              "muting does not overwrite what the agent is doing",
              "the server used to force idle here")
        check(label() == "Thinking", "the orb still says Thinking through a mute",
              f"label={label()!r}")
        check(timer_visible(), "and the clock survives the mute",
              "this is the regression he reported")

        page.click("#mute")
        time.sleep(0.5)
        api("/consumed", {"cursor": 0, "state": "idle"})
        time.sleep(0.6)
        check(label() == "Listening" and not timer_visible(),
              "back to idle: Listening, no clock")

        page.click("#mute")
        time.sleep(0.6)
        check(label() == "Muted", "muted at idle reads Muted", f"label={label()!r}")
        check(page.evaluate("() => document.getElementById('mute').getAttribute('aria-checked')")
              == "true", "and the mute button is the thing carrying that state")
        page.click("#mute")
        time.sleep(0.4)

        # Closing the channel outranks the agent's state.
        api("/consumed", {"cursor": 0, "state": "thinking"})
        time.sleep(0.6)
        page.click("#orb")          # the orb IS the channel switch once a session is running
        time.sleep(0.8)
        check(label() == "Off", "a closed channel reads Off even mid-think", f"label={label()!r}")
        check(not timer_visible(), "and stops the clock")
        check(api("/status")["agent_state"] == "thinking",
              "closing the channel does not overwrite the agent's state either")

        page.click("#orb")
        time.sleep(0.8)
        check(label() == "Thinking", "reopening restores what was actually happening",
              f"label={label()!r}")
        api("/consumed", {"cursor": 0, "state": "idle"})
        time.sleep(0.5)

        # ---------------------------------------------------------------- state is DERIVED
        # A WATCH THAT HAS RETURNED MEANS THINKING; A WATCH THAT IS OPEN MEANS LISTENING.
        #
        # The agent no longer declares its status, so this has to hold from the tool's own
        # behaviour. The first version reported only the opening edge, which made "thinking"
        # last milliseconds — the loop drains by re-watching — and painted the real 20-second
        # think as "Listening". JJ, 2026-08-07: "I haven't seen the thinking that often."
        import subprocess as _sp2

        _sp2.run([sys.executable, "-m", "voice_tunnel", "watch", "--session", SESSION,
                  "--since", str(api("/status")["turns_logged"] - 1), "--timeout", "2"],
                 capture_output=True, text=True, cwd=ROOT, env=env)
        time.sleep(0.8)
        check(api("/status")["agent_state"] == "thinking",
              "a watch that RETURNED leaves the agent thinking",
              "the edge that was missing")
        check(page.evaluate("() => document.getElementById('orblabel').textContent") == "Thinking",
              "and the orb says so without anyone declaring it")

        # ---------------------------------------------------------------- watch wakes on mute
        # A BLOCKED WATCH MUST RETURN WHEN HE PRESSES A BUTTON, not only when he speaks.
        #
        # JJ, 2026-08-07, after four abandonments in one session: "the fact that the guide said
        # that when muted we should stop watching doesn't make any sense. Because how else would
        # you know when I am muted? ... let's make sure that whenever I mute or unmute, that
        # resolves the watch so that you immediately get notified whenever that button was
        # pressed, similar to the verbose mode."
        #
        # The old contract made those states invisible AND told the agent to stop looking, so the
        # only way to notice he had come back was to guess when to poll.
        import subprocess as _sp
        import threading as _th

        cursor_now = api("/status")["turns_logged"] - 1
        result = {}

        def _blocked_watch():
            t0 = time.time()
            r = _sp.run([sys.executable, "-m", "voice_tunnel", "watch", "--session", SESSION,
                         "--since", str(cursor_now), "--timeout", "25"],
                        capture_output=True, text=True, cwd=ROOT, env=env)
            result["elapsed"] = time.time() - t0
            result["out"] = r.stdout.strip()

        th = _th.Thread(target=_blocked_watch)
        th.start()
        time.sleep(3)                      # let it be genuinely blocked, not racing the start
        page.click("#mute")
        th.join(timeout=40)
        try:
            woke = json.loads(result.get("out") or "{}")
        except Exception:
            woke = {}
        check(woke.get("event") == "control",
              "pressing MUTE wakes a blocked watch", f"after {result.get('elapsed', -1):.1f}s")
        check(woke.get("changed", {}).get("muted") is True,
              "and names which button moved", f"changed={woke.get('changed')}")
        nxt = woke.get("next") or ""
        # Assert the PROPERTY, not the wording: the hint must hand back a runnable watch and must
        # never tell the agent to stop. An earlier version of this check pinned the exact phrase
        # "keep watching" and broke the moment the hints became literal commands — which is a test
        # asserting prose rather than behaviour.
        check("voice-tunnel watch --session" in nxt and "stop watching" not in nxt,
              "and hands back a runnable watch instead of telling the agent to stop",
              f"next={nxt!r}")
        check(result.get("elapsed", 99) < 20,
              "it returns promptly rather than riding out the timeout",
              f"{result.get('elapsed', -1):.1f}s")
        page.click("#mute")                # back to unmuted for the checks that follow
        time.sleep(0.6)

        # No branch of the hint may ever tell the agent to stop watching again.
        raw = _sp.run([sys.executable, "-m", "voice_tunnel", "watch", "--session", SESSION,
                       "--since", str(api("/status")["turns_logged"] - 1), "--timeout", "2"],
                      capture_output=True, text=True, cwd=ROOT, env=env).stdout
        check("stop watching" not in raw, "no hint tells the agent to stop watching", raw[-90:])

        # ---------------------------------------------------------------- collapse
        check(page.evaluate("() => !document.getElementById('log').hidden"),
              "the transcript starts visible")
        page.click("#collapse")
        time.sleep(0.3)
        check(page.evaluate("() => document.getElementById('log').hidden"),
              "tapping collapse hides the transcript")
        check(page.evaluate("() => document.getElementById('orb').offsetHeight > 0"),
              "the orb and its buttons stay")
        check(page.evaluate("() => document.body.scrollHeight <= window.innerHeight + 1"),
              "and the page still fits without a hole where the transcript was")

        page.reload(wait_until="load")
        page.wait_for_function("() => window.__voiceTunnel", timeout=10000)
        check(page.evaluate("() => document.getElementById('log').hidden"),
              "collapsed survives a reload")
        check(page.evaluate(
            "() => document.getElementById('collapse').getAttribute('aria-expanded')") == "false",
            "and the header reports itself collapsed to assistive tech")
        page.click("#collapse")
        time.sleep(0.3)
        check(page.evaluate("() => !document.getElementById('log').hidden"),
              "and it comes back")

        # The toggle is the transcript's HEADER, not a control in the orb's row.
        #
        # A row has to exist first: an EMPTY transcript hides its own header (a control for an
        # empty box is noise), so measuring before anything is said returns a zero rect and every
        # geometry assertion below passes or fails for the wrong reason.
        page.evaluate("""() => {
          const d = document.createElement('div');
          d.className = 'row';
          d.textContent = 'geometry fixture';
          document.getElementById('log').appendChild(d);
        }""")
        time.sleep(0.2)
        geo = page.evaluate("""() => {
          const r = (id) => document.getElementById(id).getBoundingClientRect();
          const c = r('collapse'), ctl = r('controls'), lg = r('log'), o = r('orbwrap');
          return { belowControls: c.top >= ctl.bottom - 1,
                   aboveLog: c.bottom <= lg.top + 1,
                   centered: Math.abs((c.left + c.right) / 2 - (o.left + o.right) / 2) < 6,
                   labelled: document.getElementById('collapse').textContent.trim()
                             .toLowerCase().startsWith('transcript') };
        }""")
        check(geo["belowControls"], "the transcript header sits below the control row")
        check(geo["aboveLog"], "and directly above the transcript it controls")
        check(geo["centered"], "centered on the orb")
        check(geo["labelled"], "and it says what it opens rather than being a bare icon")

        # ---------------------------------------------------------------- the FIRST TAP
        # A SECOND browser, with Chrome's own fake capture device and NO getUserMedia stub.
        #
        # Every other harness in this repo — this one above, e2e.py, channel.py — replaces
        # `navigator.mediaDevices.getUserMedia` with something that returns a synthetic stream.
        # That is right for testing audio, and it means NONE of them execute the real start
        # path: the permission call itself, the deviceId constraint, the fallback when a
        # remembered microphone has vanished, or `populateMics()`. So "tap to start does nothing"
        # was a failure mode with no coverage at all, which is exactly how it reached a person.
        #
        # `--use-fake-device-for-media-stream` gives a REAL MediaDevices implementation backed by
        # a silent device: real permission grant, real enumerateDevices, real track settings. The
        # audio is worthless and that is fine — this asserts the session STARTS, not what it hears.
        real = browser.new_context()
        rp = real.new_page()
        rerrors = []
        rp.on("pageerror", lambda e: rerrors.append(str(e)))
        rp.goto(f"http://127.0.0.1:{PORT}/?token={TOKEN}", wait_until="load")
        rp.wait_for_function("() => window.__voiceTunnel", timeout=15000)
        check(rp.evaluate("() => document.getElementById('orblabel').textContent") == "Tap to start",
              "a fresh page invites the first tap")
        rp.click("#orb")
        try:
            rp.wait_for_function(
                "() => document.getElementById('orblabel').textContent === 'Listening'",
                timeout=25000)
            started = True
        except Exception:
            started = False
        seen_label = rp.evaluate("() => document.getElementById('orblabel').textContent")
        seen_err = rp.evaluate("() => document.getElementById('err').textContent")
        check(started, "TAPPING THE ORB STARTS A SESSION on a real device path",
              f"label={seen_label!r} err={seen_err!r}")
        # WAIT rather than sample: "Listening" is reached either when frames arrive OR after the
        # 4 s warm-up ceiling, so a single read right after it can legitimately catch zero.
        try:
            rp.wait_for_function("() => window.__voiceTunnel.framesSent > 3", timeout=15000)
        except Exception:
            pass
        frames = rp.evaluate("() => window.__voiceTunnel.framesSent")
        check(frames > 3, "and audio is actually flowing after it", f"frames={frames}")
        check(not rerrors, "no uncaught exception in the start path", "; ".join(rerrors[:2]))
        real.close()

        # A REFUSED microphone must be visible and RETRYABLE. Third context, no fake device
        # flag at all, so the permission is genuinely unavailable.
        denied = browser.new_context()
        dp = denied.new_page()
        dp.add_init_script(
            "navigator.mediaDevices.getUserMedia = () => "
            "Promise.reject(new DOMException('Permission denied', 'NotAllowedError'));")
        dp.goto(f"http://127.0.0.1:{PORT}/?token={TOKEN}", wait_until="load")
        dp.wait_for_function("() => window.__voiceTunnel", timeout=15000)
        dp.click("#orb")
        time.sleep(1.2)
        check(dp.evaluate("() => document.getElementById('orblabel').textContent") == "No mic",
              "a refused tap says No mic on the orb rather than falling back to Tap to start",
              "the ambiguity that cost a live session")
        check(not dp.evaluate("() => document.getElementById('err').hidden"),
              "and the error line explains it")
        denied.close()

        # ---------------------------------------------------------------- the read boundary
        # THE DIVIDER MUST BE ON SCREEN, not one line below the fold.
        #
        # `placeDivider` inserts it AFTER the row that was just scrolled to, so with a
        # scroll-to-the-row policy it was permanently invisible. JJ, 2026-08-07: "the text that
        # says claude has read to here is not always displayed... it's below the scroll and I
        # need to scroll to see it."
        page.evaluate("""() => {
          const log = document.getElementById('log');
          for (let i = 0; i < 40; i++) {
            const d = document.createElement('div');
            d.className = 'row';
            d.dataset.id = String(i);
            d.textContent = 'filler line ' + i + ' with enough text to wrap on a narrow log';
            log.appendChild(d);
          }
        }""")
        api("/consumed", {"cursor": 39})
        time.sleep(1.2)
        seen = page.evaluate("""() => {
          const log = document.getElementById('log');
          const d = log.querySelector('.divider');
          if (!d) return { present: false };
          const lr = log.getBoundingClientRect(), dr = d.getBoundingClientRect();
          return { present: true,
                   visible: dr.top >= lr.top - 1 && dr.bottom <= lr.bottom + 1,
                   atEnd: Math.abs(log.scrollTop + log.clientHeight - log.scrollHeight) < 4 };
        }""")
        check(seen.get("present"), "the read boundary is drawn in the transcript")
        check(seen.get("visible"), "and it is ON SCREEN without scrolling",
              "it used to land one line below the fold, every time")
        check(seen.get("atEnd"), "the log is scrolled to its true end")

        # ---------------------------------------------------------------- the removed line
        check(page.evaluate("() => document.getElementById('state') === null"),
              "the status narration line is gone")
        check(page.evaluate("() => document.getElementById('err').hidden"),
              "the error line is hidden until there is an error")

        # ---------------------------------------------------------------- one row
        row = page.evaluate("""() => {
          const r = (id) => document.getElementById(id).getBoundingClientRect();
          const m = r('mute'), v = r('verbose'), o = r('orbwrap');
          return { sameRow: Math.abs(m.top - v.top) < 2,
                   below: m.top > o.bottom - 1,
                   gap: m.top - o.bottom,
                   muteFirst: m.left < v.left };
        }""")
        check(row["sameRow"], "mute and verbose share one row with the picker")
        check(row["below"], "the row sits under the orb")
        check(row["gap"] < 26, "close under the orb, not floating", f"gap={row['gap']:.0f}px")
        check(row["muteFirst"], "in reading order: mute, picker, verbose")
        width = page.evaluate("""() => {
          const r = (id) => document.getElementById(id).getBoundingClientRect();
          return { mic: r('mic').width, controls: r('controls').width, orb: r('orbwrap').width };
        }""")
        check(width["mic"] <= 160, "the picker is not the widest thing on the page",
              f"mic={width['mic']:.0f}px")
        check(width["controls"] <= width["orb"] * 1.35,
              "and the three read as one cluster under the orb, not a spanning bar",
              f"controls={width['controls']:.0f}px orb={width['orb']:.0f}px")

        browser.close()
finally:
    server.terminate()

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
