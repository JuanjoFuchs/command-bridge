"""THE STALL GUARD: what a clip does when its `onended` never arrives.

**Why this is its own harness.** The rule lives inside a 200 ms `setInterval` nested in a promise
that `drainClips` awaits — there is nothing a test can hook, and the failure it guards against is
a promise that never settles, which is indistinguishable from a slow test. So the rule was lifted
out of the timer into `clipStallAction`, and this file sweeps the predicate the timer now calls.

🔴 **The bug this exists to prevent, measured live 2026-08-26.** A clip for the Atlas lane was
sent, its orb went synthesizing → speaking, and nothing was audible for 43 seconds. JJ: *"its turn
never spoke… I had to switch back to your lane and then back to the Atlas lane for it to start
speaking."* The timing log showed `lane_held_flushed` at 12:04:34.945 and `played` at 12:05:18.260
— 126 ms after he tapped the lane back.

**The cause was one missing case in a predicate.** The guard finished on `closed` and treated every
other state as healthy. A `suspended` context is not closed: it is alive, it accepts
`createBufferSource()` and `start()`, and only its CLOCK is stopped — so the source never advances,
`onended` never fires, and `draining` stays true with every later clip queued behind it. The same
state was already understood ONE FUNCTION AWAY, in `reconnect()`, and never handled here.

⚠ **SAFE TO RUN DURING A LIVE SESSION**, like `scripts/lanestrip.py` and `scripts/devicepills.py`.
It starts NO voice-tunnel server: the page is served by a plain `http.server` on an ephemeral port,
the socket is stubbed out, and the last section PROVES that rather than asserting it.

    python scripts/clipstall.py

Needs Playwright (`pip install playwright && playwright install chromium`).
"""
from __future__ import annotations

import functools
import http.server
import os
import socketserver
import subprocess
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO, "voice_tunnel", "web")
SESSIONS = os.path.join(REPO, "sessions")
DEFAULT_PORT = 8765
SERVED_PORTS: list[int] = []

fails: list[str] = []


def check(ok, label, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(label)
    return ok


def note(text):
    print(f"\n--- {text} " + "-" * max(0, 78 - len(text)))


# ------------------------------------------------------------------ the safety net
#
# Duplicated from lanestrip.py on purpose, for the reason stated there: a harness that claims to be
# live-session-safe should prove it on its own, or one file's edit silently weakens another's
# guarantee. Importing is not an option either way — neither file has a main guard.


def _session_files():
    if not os.path.isdir(SESSIONS):
        return {}
    return {n: os.path.getmtime(os.path.join(SESSIONS, n))
            for n in os.listdir(SESSIONS) if n.endswith(".server.json")}


def _listeners_on(port):
    try:
        raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=30).stdout
    except Exception:
        return set()
    pids = set()
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            pids.add(parts[4])
    return pids


class ServerWatch:
    def __init__(self):
        self.sessions = _session_files()
        self.listeners = _listeners_on(DEFAULT_PORT)

    def verify(self):
        note("no voice-tunnel server process was started")
        now = _session_files()
        changed = sorted(k for k in set(self.sessions) | set(now)
                         if self.sessions.get(k) != now.get(k))
        check(not changed, "no sessions/*.server.json was created or modified",
              "" if not changed else f"changed: {changed}")
        listeners = _listeners_on(DEFAULT_PORT)
        check(listeners == self.listeners,
              f"the set of processes listening on the tunnel's port ({DEFAULT_PORT}) is unchanged",
              f"pids {sorted(self.listeners)} -> {sorted(listeners)}")
        check(all(p != DEFAULT_PORT for p in SERVED_PORTS),
              "every static server this harness opened is on an ephemeral port",
              f"ports {SERVED_PORTS}")


class Quiet(http.server.SimpleHTTPRequestHandler):
    # Signature matches the base class rather than swallowing *args: the copy of this in
    # lanestrip.py trips pyright's incompatible-override check, and a new file should not
    # inherit a finding it can avoid for free.
    def log_message(self, format, *args):  # noqa: A002 — the base class names it `format`
        pass


def serve(root):
    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=root))
    port = httpd.server_address[1]
    assert port != DEFAULT_PORT, "the OS handed out the tunnel's port; refusing to bind it"
    SERVED_PORTS.append(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


STUB = """
window.WebSocket = function () {
  this.readyState = 0;
  this.send = function () {};
  this.close = function () {};
};
"""

# Every state the spec of AudioContext.state admits, plus the two shapes the guard can actually be
# handed at runtime: a nulled `ctx` (stop() ran mid-drain) and an undefined one.
SWEEP = """
() => {
  const act = window.__voiceTunnel.clipStallAction;
  const states = ["running", "suspended", "closed", null, undefined, "interrupted"];
  const out = [];
  for (const s of states) {
    for (const ticks of [0, 1, 14, 15, 16, 99]) {
      out.push({ state: String(s), ticks, action: act(s, ticks) });
    }
  }
  return out;
}
"""

watch = ServerWatch()
httpd, port = serve(WEB)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright is not installed — `pip install playwright && playwright install chromium`")
    httpd.shutdown()
    sys.exit(2)

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    try:
        bctx = browser.new_context()
        page = bctx.new_page()
        page.add_init_script(STUB)
        page.goto(f"http://127.0.0.1:{port}/?token=clipstall", wait_until="load")
        page.wait_for_function(
            "() => window.__voiceTunnel && window.__voiceTunnel.clipStallAction", timeout=15000)

        note("the predicate, over every context state the guard can be handed")
        rows = page.evaluate(SWEEP)
        by = {(r["state"], r["ticks"]): r["action"] for r in rows}
        for r in rows:
            print(f"     {r['state']:11} ticks={r['ticks']:<3} -> {r['action']}")

        # A live clock. The source is advancing and `onended` will arrive on its own.
        check(all(by[("running", t)] == "wait" for t in (0, 1, 14, 15, 16, 99)),
              "a RUNNING context is always left alone, however long it has been playing")

        # 🔴 THE REGRESSION GUARD. Before 2026-08-26 this returned "wait" and wedged the queue.
        check(by[("suspended", 0)] == "resume" and by[("suspended", 1)] == "resume",
              "a SUSPENDED context is woken rather than waited on",
              f"got {by[('suspended', 0)]!r}")
        check(by[("suspended", 14)] == "resume" and by[("suspended", 15)] == "finish",
              "and it gives up at the limit instead of holding the queue forever",
              f"14 -> {by[('suspended', 14)]!r}, 15 -> {by[('suspended', 15)]!r}")
        check(by[("suspended", 99)] == "finish",
              "a context that will not wake never traps the clips behind it")

        # The case the old predicate got right, which must stay right.
        check(all(by[(s, 0)] == "finish" for s in ("closed", "null", "undefined")),
              "a closed or vanished context finishes at once — one clip lost, not the session")

        # An unknown state must fail toward WAITING, not toward dropping audio. Safari reports
        # "interrupted"; a state nobody here has seen is not a reason to bin a clip that may be
        # about to play.
        check(all(by[("interrupted", t)] == "wait" for t in (0, 99)),
              "an unrecognised state is treated as healthy rather than discarded",
              f"got {by[('interrupted', 0)]!r}")

        note("the rule the timer runs is the rule that was swept")
        wired = page.evaluate(
            "() => document.documentElement.innerHTML.includes('clipStallAction(ctx && ctx.state')")
        check(wired,
              "playClip's guard calls clipStallAction rather than re-implementing the predicate",
              "if this fails, the sweep above is testing a function nothing runs")

        bctx.close()
    finally:
        browser.close()

httpd.shutdown()
watch.verify()

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
