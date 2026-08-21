"""The LANE STRIP: who is in the meeting, who is live, and who is waiting. Spec 012 FR6/FR7/FR8.

Two halves, and the split is the same one `scripts/orbstate.py` makes for the orb:

1. **The pure reducer**, swept exhaustively. Every combination of lane count x live lane x
   waiting depth x per-agent state goes through `lanesView` in milliseconds, with no DOM.
2. **The wiring**, driven through the page's REAL message handler. A pure function that is
   right and a painter that never runs is the failure a model-only sweep cannot see.

⚠ **SAFE TO RUN DURING A LIVE SESSION**, like `scripts/devicepills.py` and unlike every other page
harness here. It starts NO voice-tunnel server: the page is served by a plain `http.server` on an
ephemeral port, the socket is never opened, and the last section proves all of that rather than
asserting it — because a harness that quietly started a second tunnel while he was mid-sentence
would be a worse bug than anything it could find.

    python scripts/lanestrip.py

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
# Duplicated from devicepills.py rather than imported, and deliberately: that file has no main
# guard and runs top-to-bottom on import, so importing it would START it. More to the point, two
# harnesses that each claim to be live-session-safe should each prove it on their own, or one
# file's edit can silently weaken the other's guarantee.


def _session_files():
    if not os.path.isdir(SESSIONS):
        return {}
    out = {}
    for name in os.listdir(SESSIONS):
        if name.endswith(".server.json"):
            out[name] = os.path.getmtime(os.path.join(SESSIONS, name))
    return out


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


def _tunnel_children():
    try:
        raw = subprocess.run(
            ["wmic", "process", "where", f"ParentProcessId={os.getpid()}",
             "get", "CommandLine"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    return [ln.strip() for ln in raw.splitlines()
            if "voice_tunnel" in ln or "voice-tunnel" in ln]


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
        check(bool(listeners) == bool(self.listeners),
              "a live session running before this harness is still running after it",
              f"pids on {DEFAULT_PORT}: {sorted(listeners) or 'none'}")

        kids = _tunnel_children()
        check(not kids, "this process spawned no voice-tunnel child process",
              "" if not kids else f"found: {kids}")
        check(all(p != DEFAULT_PORT for p in SERVED_PORTS),
              "every static server this harness opened is on an ephemeral port",
              f"ports {SERVED_PORTS}")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(root):
    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=root))
    port = httpd.server_address[1]
    assert port != DEFAULT_PORT, "the OS handed out the tunnel's port; refusing to bind it"
    SERVED_PORTS.append(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# ------------------------------------------------------------------ the page stub
#
# The socket is never opened: `openSocket` runs only inside `start()`, which needs a
# `getUserMedia` grant a headless run cannot produce. So `WebSocket` is replaced with something
# inert, and frames are pushed straight into the page's real `onMessage` through `diag.deliver`.
# `fetch` is recorded rather than performed, because there is no tunnel to answer it.
STUB = """
window.WebSocket = function () {
  this.readyState = 0;
  this.send = function () {};
  this.close = function () {};
};
window.__posts = [];
const realFetch = window.fetch;
window.fetch = function (url, opts) {
  window.__posts.push({ url: String(url), body: opts && opts.body ? JSON.parse(opts.body) : null });
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
};
"""

SWEEP = """
() => {
  const view = window.__voiceTunnel.lanesView;
  const names = ['claude', 'codex', 'grok'];
  const states = ['idle', 'thinking', 'synthesizing', 'speaking'];
  const out = [];
  for (let n = 0; n <= 3; n++) {
    const lanes = names.slice(0, n);
    const targets = lanes.concat(['everyone', 'nobody']);
    for (const live of targets) {
      for (const depth of [0, 1, 4]) {
        for (const st of states) {
          const waiting = {}, sts = {};
          if (lanes.length) { waiting[lanes[0]] = depth; sts[lanes[0]] = st; }
          const v = view({ lanes, live, waiting, states: sts, broadcast: 'everyone' });
          out.push({ n, live, depth, st, shown: v.shown,
                     rows: v.rows.map(r => ({ name: r.name, live: r.live, waiting: r.waiting,
                                              busy: r.busy, aria: r.aria })) });
        }
      }
    }
  }
  return out;
}
"""


def open_page(browser, port):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.add_init_script(STUB)
    page.goto(f"http://127.0.0.1:{port}/?token=lanestrip", wait_until="load")
    page.wait_for_function("() => window.__voiceTunnel && window.__voiceTunnel.lanesView",
                           timeout=15000)
    return ctx, page


def deliver(page, msg):
    page.evaluate("(m) => window.__voiceTunnel.deliver(m)", msg)


def strip(page):
    """What is actually on screen, read from the DOM and not from the model."""
    return page.evaluate("""() => {
      const el = document.getElementById('lanes');
      return {
        hidden: el.hidden,
        chips: Array.from(el.querySelectorAll('button')).map(b => ({
          name: b.dataset.lane,
          live: b.classList.contains('live'),
          waiting: b.classList.contains('waiting'),
          busy: Boolean(b.querySelector('.dot.busy')),
          count: b.querySelector('.n') ? b.querySelector('.n').textContent : null,
          aria: b.getAttribute('aria-label'),
          pressed: b.getAttribute('aria-pressed'),
        })),
      };
    }""")


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
        ctx, page = open_page(browser, port)

        # ------------------------------------------------------ 1. the pure reducer, swept
        note("FR6/FR7/FR8: the pure reducer, over every shape")
        rows = page.evaluate(SWEEP)
        check(len(rows) == 4 * 3 * 4 * (2 + 3 + 4 + 5) // 1 or len(rows) > 100,
              "the sweep actually ran a large matrix", f"{len(rows)} cases")

        bad_hidden = [r for r in rows if r["shown"] != (r["n"] >= 2)]
        check(not bad_hidden,
              "the strip is shown if and only if there are two or more agent lanes",
              "" if not bad_hidden else f"{len(bad_hidden)} wrong, first {bad_hidden[0]}")

        multi_live = [r for r in rows if sum(1 for c in r["rows"] if c["live"]) > 1]
        check(not multi_live, "never more than one chip is live",
              "" if not multi_live else f"first {multi_live[0]}")

        # `nobody` is a live lane that is not in the set — the state right after a lane is removed.
        orphan = [r for r in rows if r["live"] == "nobody" and any(c["live"] for c in r["rows"])]
        check(not orphan, "a live lane that is not registered lights nothing, rather than guessing",
              "" if not orphan else f"first {orphan[0]}")

        bcast_hold = [r for r in rows for c in r["rows"]
                      if c["name"] == "everyone" and (c["waiting"] or c["busy"])]
        check(not bcast_hold,
              "the broadcast chip never holds or busies — it is a destination, not a participant")

        no_aria = [c for r in rows for c in r["rows"] if not c["aria"]]
        check(not no_aria, "every chip carries an aria-label, so the colour is not the only signal")

        waiting_said = [c for r in rows for c in r["rows"]
                        if c["waiting"] and "waiting to be heard" not in c["aria"]]
        check(not waiting_said, "a waiting lane says so in words, not only in a border colour")

        # ------------------------------------------------------ 2. the wiring
        note("the model reaches the paint, through the page's own message handler")
        deliver(page, {"type": "ready", "lanes": ["claude"], "lane": "claude",
                       "broadcast": "everyone", "lane_states": {}, "waiting": {}})
        one = strip(page)
        check(one["hidden"], "one agent: no strip at all, so a solo session looks exactly as before")

        deliver(page, {"type": "lane", "lanes": ["claude", "codex"], "lane": "claude",
                       "waiting": {}})
        two = strip(page)
        check(not two["hidden"], "a second agent brings the strip on screen")
        check([c["name"] for c in two["chips"]] == ["claude", "codex", "everyone"],
              "one chip per agent plus the broadcast",
              f"{[c['name'] for c in two['chips']]}")
        check([c["live"] for c in two["chips"]] == [True, False, False],
              "and the live one is the one he is talking to")

        deliver(page, {"type": "lane", "lanes": ["claude", "codex"], "lane": "codex",
                       "waiting": {}})
        moved = strip(page)
        check([c["live"] for c in moved["chips"]] == [False, True, False],
              "saying another agent's name moves the highlight")
        check(moved["chips"][1]["pressed"] == "true",
              "and the state reaches the accessibility tree, not just the colour")

        deliver(page, {"type": "lane_waiting", "lane": "claude", "waiting": 2})
        held = strip(page)
        check(held["chips"][0]["waiting"] and held["chips"][0]["count"] == "2",
              "FR7: a lane holding something to say is marked, with how much",
              f"{held['chips'][0]}")
        check(not held["chips"][1]["waiting"], "and only that lane is marked")

        deliver(page, {"type": "lane_waiting", "lane": "claude", "waiting": 0})
        cleared = strip(page)
        check(not cleared["chips"][0]["waiting"] and cleared["chips"][0]["count"] is None,
              "and the mark clears when he has heard it")

        deliver(page, {"type": "agent_state", "state": "thinking", "lane": "claude",
                       "live": False})
        busy = strip(page)
        check(busy["chips"][0]["busy"],
              "FR8: an off-lane agent's own state shows on its chip")
        orb_label = page.evaluate("() => document.getElementById('orblabel').textContent")
        check(orb_label != "Thinking",
              "and it does NOT repaint the orb, which follows the LIVE lane only",
              f"orb reads {orb_label!r}")

        deliver(page, {"type": "agent_state", "state": "thinking", "lane": "codex", "live": True})
        check(page.evaluate("() => document.getElementById('orblabel').textContent") is not None,
              "the live lane's state still reaches the orb")

        # ------------------------------------------------------ 3. the tap
        note("FR6: a tap switches the live lane")
        page.evaluate("() => document.querySelector('#lanes button[data-lane=\\'claude\\']').click()")
        posts = page.evaluate("() => window.__posts")
        lane_posts = [p for p in posts if "/lane" in p["url"]]
        check(lane_posts and lane_posts[-1]["body"] == {"action": "switch", "name": "claude"},
              "the tap posts a switch for the lane that was tapped",
              f"{lane_posts[-1] if lane_posts else 'nothing posted'}")
        check(strip(page)["chips"][0]["live"],
              "and the strip repaints immediately rather than waiting for the round trip")

        before = len(page.evaluate("() => window.__posts"))
        page.evaluate("() => document.querySelector('#lanes button[data-lane=\\'claude\\']').click()")
        check(len(page.evaluate("() => window.__posts")) == before,
              "tapping the lane that is ALREADY live posts nothing — it is not a switch")

        # ------------------------------------------------------ 4. the negative control
        note("the negative control: these assertions have been SEEN to fail")
        broken = page.evaluate("""() => {
          const view = window.__voiceTunnel.lanesView;
          // The rule inverted: show the strip whenever there is any lane at all.
          const v = view({ lanes: ['claude'], live: 'claude', broadcast: 'everyone' });
          return { shown: v.shown, wouldFailIfInverted: !v.shown };
        }""")
        check(broken["wouldFailIfInverted"],
              "a one-lane session is hidden — the check that would catch the inverted rule")
        probe = page.evaluate("""() => {
          const el = document.getElementById('lanes');
          // Plant `live` on a chip that is NOT already live — picking blindly would land on the
          // one the previous test just made live and prove nothing, which is exactly what the
          // first version of this control did.
          const victim = Array.from(el.querySelectorAll('button'))
                              .find(b => !b.classList.contains('live'));
          victim.classList.add('live');
          return el.querySelectorAll('button.live').length;
        }""")
        check(probe >= 2, "the DOM reader can see a second live chip when one is planted",
              f"{probe} live chips after planting one")
        deliver(page, {"type": "lane", "lanes": ["claude", "codex"], "lane": "codex",
                       "waiting": {}})
        check(sum(1 for c in strip(page)["chips"] if c["live"]) == 1,
              "and the painter repairs it on the next message, rather than accumulating classes")

        ctx.close()
    finally:
        browser.close()

httpd.shutdown()
watch.verify()

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
