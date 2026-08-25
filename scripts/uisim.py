"""Drive the REAL page into any server state, assert it, and photograph it. Spec 015 FR3.

    python scripts/uisim.py                 # every scenario, shot + checked
    python scripts/uisim.py --only lanes    # one scenario by name
    python scripts/uisim.py --list          # what scenarios exist

WHY THIS EXISTS, in his words: *"you need to build a way to mimic everything from the server on the
UI so that you can drive the UI with synthetic statuses and so that you can simulate every scenario
and you can properly test the UI with screenshots and stuff."* (2026-08-24)

🔴 **HE ASKED FOR IT AFTER A DAY IN WHICH EVERY DEFECT HE FOUND HAD A GREEN HARNESS OVER IT.** In
order: a chip strip rendering underneath the orb row that replaced it; `display:grid` beating the
`hidden` attribute and eating 165px of transcript; the busy timer silently lost with the orb that
contained it; one lane's read marking every lane's turns; and `idle` standing for four different
situations. `scripts/orbstate.py` matched its 480-case golden through all five.

🎯 **The transferable reason, and it is why this is a different instrument rather than more cases:
a golden over a PURE MODEL cannot see a missing, duplicated or lying VIEW.** Every one of those
bugs left the model correct. What was wrong was what reached the screen — so the check has to run
against the screen.

**What it is not.** Not a replacement for `lanestrip.py` (which sweeps reducers exhaustively) or
`layout.py` (which measures geometry across viewports). This is the third leg: named, whole-page
states, each asserted AND photographed, so a human can look at the same thing the assertion read.

⚠ **STARTS NO VOICE-TUNNEL SERVER, and proves it** — his daily driver is usually live on 8765.
The page is served statically on an ephemeral port and driven through its own message handler.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import pathlib
import socketserver
import subprocess
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "voice_tunnel" / "web"
TUNNEL_PORT = 8765
SERVED: list[int] = []
fails: list[str] = []


def check(ok, label, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(label)
    return ok


# --------------------------------------------------------------------- the state vocabulary
#
# One helper per server message the page actually reacts to. Keeping them as functions rather than
# raw dicts means a scenario reads as a sequence of EVENTS, and a message whose shape changes is
# fixed in one place instead of in every scenario that happens to use it.

def ready(lanes=("claude",), live="claude", states=None, waiting=None,
          watching=(), consumed=None):
    return {"type": "ready", "verbose": True, "lanes": list(lanes), "lane": live,
            "default_lane": lanes[0], "broadcast": "everyone",
            "lane_states": states or {}, "waiting": waiting or {},
            "watching_lanes": list(watching), "lane_consumed": consumed or {}}


def turn(i, text, lane=None, addressed=True):
    m = {"type": "turn", "id": i, "text": text, "addressed": addressed}
    if lane is not None:
        m["lane"] = lane
    return m


def agent_state(lane, state, live=False, watching=None, consumed=None):
    m = {"type": "agent_state", "lane": lane, "state": state, "live": live}
    if watching is not None:
        m["watching_lanes"] = list(watching)
    if consumed is not None:
        m["lane_consumed"] = consumed
    return m


def switch(live, lanes, waiting=None):
    return {"type": "lane", "lane": live, "lanes": list(lanes), "why": "wake",
            "default_lane": lanes[0], "waiting": waiting or {}}


def consumed(cursor, per_lane=None, pending=0):
    return {"type": "consumed", "cursor": cursor, "pending": pending,
            "lane_consumed": per_lane or {}}


def held(lane, n):
    return {"type": "lane_waiting", "lane": lane, "waiting": n}


# --------------------------------------------------------------------- the scenarios
#
# Each is (name, messages, assertions). The assertions are what makes this a harness rather than a
# screenshot gallery — a picture nobody checks is how four of today's five defects survived.

def _probe(page, expr):
    return page.evaluate(expr)


# 🔴 MEASURE WHAT RENDERS, NEVER THE `hidden` ATTRIBUTE.
#
# The first version of this file asserted `element.hidden`, and a mutation reintroducing the very
# bug it was written for — `display:grid` on `#orbwrap` overriding the attribute, which cost the
# transcript 165px — stayed GREEN. **The attribute was set correctly the whole time; the element
# was on screen anyway.** That is the same failure this harness exists to catch, made inside the
# harness, one file after writing the sentence "a golden over a pure model cannot see a lying view".
#
# A zero-height box is the only honest answer to "is it gone", because it survives every way an
# element can be visible while claiming otherwise.
def _gone(page, sel):
    return page.evaluate(
        "(s) => { const e = document.querySelector(s);"
        "         return !e || e.getBoundingClientRect().height === 0; }", sel)


def _shown(page, sel):
    return page.evaluate(
        "(s) => { const e = document.querySelector(s);"
        "         return Boolean(e) && e.getBoundingClientRect().height > 0; }", sel)


def solo(page):
    """One agent: the page must look exactly as it did before lanes existed."""
    return [
        (lambda: _gone(page, "#orbs"), True, "no orb row below two agents"),
        (lambda: _gone(page, "#lanes"), True, "no chip strip either"),
        (lambda: _shown(page, "#orbwrap"), True, "the single orb is the one on screen"),
    ]


def three_lanes(page):
    """Three agents, one live, one holding speech — the state he actually runs in."""
    return [
        (lambda: _probe(page, "() => document.querySelectorAll('#orbs .laneorb').length"),
         3, "one orb per agent"),
        (lambda: _gone(page, "#orbwrap"),
         True, "and the single orb is gone, not hiding behind them"),
        (lambda: _gone(page, "#lanes"),
         True, "and the chip strip it replaced is gone too"),
        (lambda: _probe(page, "() => document.querySelectorAll('#orbs .hand').length"),
         1, "exactly one raised hand, on the lane that is holding"),
        (lambda: _probe(page,
                        "() => new Set([...document.querySelectorAll('#orbs .nm')]"
                        ".map(e => getComputedStyle(e).color)).size"),
         3, "three distinct identity colours"),
    ]


def per_lane_read(page):
    """015 FR1 — codex has read, claude has not. His exact bug report, as a picture."""
    return [
        (lambda: _probe(page, "() => document.querySelector"
                              "('#log .row[data-lane=\"codex\"] .tick').dataset.state"),
         "read", "the lane that read shows read"),
        (lambda: _probe(page, "() => document.querySelector"
                              "('#log .row[data-lane=\"claude\"] .tick').dataset.state"),
         "sent", "and the lane that did NOT read still shows sent"),
    ]


def working_not_idle(page):
    """015 FR2 — a background agent heads-down must not read as idle."""
    return [
        (lambda: _probe(page, "() => [...document.querySelectorAll('#orbs .laneorb')]"
                              ".map(b => b.dataset.lane + ':' +"
                              " b.querySelector('.stat').textContent.trim()).join(',')"),
         "claude:listening,codex:working,atlas:idle",
         "listening, working and idle are three different things"),
    ]


SCENARIOS = {
    "solo": (
        [ready(lanes=("claude",), watching=["claude"]),
         turn(1, "Hey Claude, can you hear me?", lane="claude"),
         consumed(1, {"claude": 1})],
        solo,
    ),
    "lanes": (
        [ready(lanes=("claude", "codex", "atlas"), live="claude",
               watching=["claude"], consumed={"claude": 4, "codex": 4}),
         turn(1, "Hey Codex, run the tests.", lane="codex"),
         turn(2, "Hey Atlas, what about the freedom strategy?", lane="atlas"),
         turn(3, "Hey everyone, stand down.", lane="everyone"),
         turn(4, "Hey Claude, one more thing.", lane="claude"),
         agent_state("codex", "thinking", watching=["claude"],
                     consumed={"claude": 4, "codex": 4}),
         held("atlas", 2),
         consumed(4, {"claude": 4, "codex": 4})],
        three_lanes,
    ),
    "per-lane-read": (
        [ready(lanes=("claude", "codex"), live="claude", watching=["claude"]),
         turn(10, "Hey Claude, this one is mine.", lane="claude"),
         turn(40, "Hey Codex, this one is yours.", lane="codex"),
         consumed(40, {"codex": 40})],
        per_lane_read,
    ),
    "working": (
        [ready(lanes=("claude", "codex", "atlas"), live="claude"),
         agent_state("codex", "idle", watching=["claude"],
                     consumed={"claude": 3, "codex": 9})],
        working_not_idle,
    ),
    "session-off": (
        [ready(lanes=("claude", "codex"), live="claude", watching=["claude"],
               consumed={"claude": 2}),
         turn(1, "Hey Claude, are you there?", lane="claude"),
         consumed(1, {"claude": 1})],
        lambda page: [
            (lambda: _probe(page, "() => document.getElementById('power')"
                                  ".getAttribute('aria-pressed')"),
             "false", "the power button reads OFF before anything is started"),
            (lambda: _probe(page, "() => document.getElementById('orbs').dataset.off"),
             "true", "and the orb row says so once, rather than each orb inventing it"),
        ],
    ),
}


STUB = """
window.WebSocket = function () {
  this.readyState = 0; this.send = function () {}; this.close = function () {};
};
window.__posts = [];
window.fetch = function (u, o) {
  window.__posts.push({ url: String(u), body: o && o.body ? JSON.parse(o.body) : null });
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
};
"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(WEB)))
    port = httpd.server_address[1]
    assert port != TUNNEL_PORT, "the OS handed out the tunnel's port; refusing to bind it"
    SERVED.append(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def run_scenario(browser, port, name, messages, assertions, out, shoot):
    print(f"\n--- {name} " + "-" * max(0, 70 - len(name)))
    shots = []
    for label, w, h in [("phone", 412, 915), ("desktop", 1100, 860)]:
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2)
        page = ctx.new_page()
        page.add_init_script(STUB)
        page.goto(f"http://127.0.0.1:{port}/?token=sim", wait_until="load")
        page.wait_for_function("() => window.__voiceTunnel && window.__voiceTunnel.deliver",
                               timeout=15000)
        for m in messages:
            page.evaluate("(m) => window.__voiceTunnel.deliver(m)", m)
        page.wait_for_timeout(350)

        # ASSERT ON THE PHONE PASS ONLY — the claims are about state, not about width, and
        # `layout.py` already owns the viewport sweep. Running them twice would double every
        # failure line for no extra information.
        if label == "phone":
            for get, want, why in assertions(page):
                got = get()
                check(got == want, why, "" if got == want else f"got {got!r}, want {want!r}")

        if shoot:
            shot = out / f"sim-{name}-{label}.png"
            page.screenshot(path=str(shot), full_page=True)
            shots.append(shot.name)
        ctx.close()
    if shots:
        print(f"  shot {', '.join(shots)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run one scenario by name")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-shot", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "sessions" / "shots"))
    args = ap.parse_args()

    if args.list:
        for n in SCENARIOS:
            print(n)
        return 0

    names = [args.only] if args.only else list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"no such scenario: {unknown} — try --list")
        return 2

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed — `pip install playwright && playwright install chromium`")
        return 2

    before = _listeners()
    httpd, port = serve()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for n in names:
                messages, assertions = SCENARIOS[n]
                run_scenario(browser, port, n, messages, assertions, out, not args.no_shot)
            browser.close()
    finally:
        httpd.shutdown()

    print("\n--- no voice-tunnel server was started " + "-" * 40)
    after = _listeners()
    check(after == before, f"the set of listeners on {TUNNEL_PORT} is unchanged",
          f"{sorted(before)} -> {sorted(after)}")
    check(all(p != TUNNEL_PORT for p in SERVED), "every static server used an ephemeral port",
          f"{SERVED}")

    print("\n" + (f"{len(fails)} FAILURES: {fails}" if fails else "ALL PASS"))
    return 1 if fails else 0


def _listeners():
    try:
        raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return set()
    return {ln.split()[-1] for ln in raw.splitlines()
            if "LISTENING" in ln and ln.split()[1].endswith(f":{TUNNEL_PORT}")}


if __name__ == "__main__":
    sys.exit(main())
