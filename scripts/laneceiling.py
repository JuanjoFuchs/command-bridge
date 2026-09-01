"""How many lane orbs stay LEGIBLE in one row? Measure it, then shoot it.

    python scripts/laneceiling.py              # sweep 2..10 on phone + desktop
    python scripts/laneceiling.py --max 12
    python scripts/laneceiling.py --no-shot    # numbers only

WHY THIS EXISTS, in his words: *"you should tell me how many we should render. That's something you
should validate visually before asking for my feedback."* (2026-08-26, after I asked him to pick the
number). The lane ceiling is a UI question and never a voice-supply one — `voices-v1.0.bin` ships 54
voices, 28 of them English — so the only thing that can answer it is the orb row at his real
viewport widths.

**What it measures, and why each one is a ceiling on its own:**

| measure | why it bounds the count |
|---|---|
| orb diameter | below ~44 CSS px a round control stops being reliably tappable with a thumb |
| row overflow | `scrollWidth > clientWidth` means orbs are off-screen, which is worse than small |
| name clipped | a name that ellipsises stops being the thing he learns the lane by |
| gap | orbs closer than ~8px read as one blob and mis-taps go up |

⚠ **The number this prints is a CEILING, not a target.** It says where the row stops working, not
how many agents are useful — and the smallest of the four bounds is the answer, because a row that
fits but cannot be tapped has not fit.

⚠ **STARTS NO VOICE-TUNNEL SERVER**, same as `uisim.py`: the page is served statically on an
ephemeral port and driven through its own message handler.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import pathlib
import socketserver
import subprocess
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "command_bridge" / "web"
TUNNEL_PORT = 8765

# Ten names that survive a wake-word match: distinct first syllables, no shared prefix, two
# syllables each so the TTS lands them the same way. The first four are the ones already in use.
NAMES = ["magnus", "atlas", "kepler", "dexter", "orion",
         "vega", "rigel", "lyra", "nova", "juno", "cygnus", "altair"]

# Below this a round control is not reliably tappable with a thumb — the smaller of Apple's 44pt
# and Android's 48dp, in CSS px, which is the unit `getBoundingClientRect` reports.
MIN_TAP_PX = 44.0
MIN_GAP_PX = 8.0

STUB = """
window.WebSocket = function () {
  this.readyState = 0; this.send = function () {}; this.close = function () {};
};
window.fetch = function () { return Promise.resolve({ ok: true, json: () => Promise.resolve({}) }); };
"""

MEASURE = """
() => {
  const row = document.getElementById('orbs');
  if (!row) return { rendered: 0 };
  const orbs = [...row.querySelectorAll('.laneorb')];
  const boxes = orbs.map(o => o.getBoundingClientRect());
  // The tappable target is the orb's own circle when it has one, else the button box.
  const dots = orbs.map(o => (o.querySelector('.dot') || o).getBoundingClientRect());
  const names = [...row.querySelectorAll('.nm')];
  let gap = Infinity;
  for (let i = 1; i < boxes.length; i++) gap = Math.min(gap, boxes[i].left - boxes[i-1].right);
  return {
    rendered: orbs.length,
    minTap: dots.length ? Math.min(...dots.map(b => Math.min(b.width, b.height))) : 0,
    minOrbW: boxes.length ? Math.min(...boxes.map(b => b.width)) : 0,
    minGap: boxes.length > 1 ? gap : null,
    overflow: row.scrollWidth - row.clientWidth,
    clipped: names.filter(n => n.scrollWidth > n.clientWidth + 1).length,
    rowH: row.getBoundingClientRect().height,
    offscreen: boxes.filter(b => b.right > document.documentElement.clientWidth + 0.5
                              || b.left < -0.5).length,
  };
}
"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(WEB)))
    port = httpd.server_address[1]
    assert port != TUNNEL_PORT, "the OS handed out the tunnel's port; refusing to bind it"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def ready(lanes):
    """A room of N agents, the first live, each one having reported a state so no orb is a ghost."""
    states = {}
    for i, n in enumerate(lanes):
        states[n] = ("listening" if i == 0 else "thinking" if i % 2 else "idle")
    return {"type": "ready", "verbose": True, "lanes": list(lanes), "lane": lanes[0],
            "default_lane": lanes[0], "broadcast": "everyone", "lane_states": states,
            "waiting": {lanes[-1]: 2} if len(lanes) > 1 else {},
            "watching_lanes": [lanes[0]], "lane_consumed": {}}


def verdict(rows):
    """The ceiling is the LARGEST n at which every bound still holds — and the first bound to
    break is the one worth reporting, because it names what to fix if he wants more lanes."""
    ok, first_break = 0, None
    for r in rows:
        why = []
        if r["rendered"] != r["n"]:
            why.append(f"only {r['rendered']} of {r['n']} orbs rendered")
        if r["minTap"] < MIN_TAP_PX:
            why.append(f"tap target {r['minTap']:.0f}px < {MIN_TAP_PX:.0f}")
        if r["overflow"] > 0.5:
            why.append(f"row overflows by {r['overflow']:.0f}px")
        if r["offscreen"]:
            why.append(f"{r['offscreen']} orb(s) off-screen")
        if r["clipped"]:
            why.append(f"{r['clipped']} name(s) clipped")
        if r["minGap"] is not None and r["minGap"] < MIN_GAP_PX:
            why.append(f"gap {r['minGap']:.0f}px < {MIN_GAP_PX:.0f}")
        if why:
            if first_break is None:
                first_break = (r["n"], why)
        else:
            ok = r["n"]
    return ok, first_break


def sweep(browser, port, label, w, h, counts, out, shoot):
    print(f"\n--- {label} {w}x{h} " + "-" * max(0, 52 - len(label)))
    print(f"  {'n':>2}  {'drawn':>5}  {'tap':>6}  {'orbW':>6}  {'gap':>5}  "
          f"{'ovf':>5}  {'clip':>4}  {'off':>3}  {'rowH':>5}")
    rows = []
    for n in counts:
        lanes = NAMES[:n]
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2)
        page = ctx.new_page()
        page.add_init_script(STUB)
        page.goto(f"http://127.0.0.1:{port}/?token=sim", wait_until="load")
        page.wait_for_function("() => window.__voiceTunnel && window.__voiceTunnel.deliver",
                               timeout=15000)
        page.evaluate("(m) => window.__voiceTunnel.deliver(m)", ready(lanes))
        page.wait_for_timeout(300)
        m = page.evaluate(MEASURE)
        m["n"] = n
        rows.append(m)
        gap = "-" if m.get("minGap") is None else f"{m['minGap']:.0f}"
        print(f"  {n:>2}  {m['rendered']:>5}  {m['minTap']:>6.0f}  {m['minOrbW']:>6.0f}  "
              f"{gap:>5}  {m['overflow']:>5.0f}  {m['clipped']:>4}  {m['offscreen']:>3}  "
              f"{m['rowH']:>5.0f}")
        if shoot:
            page.screenshot(path=str(out / f"ceiling-{label}-{n:02d}.png"), full_page=True)
        ctx.close()
    ok, brk = verdict(rows)
    print(f"  → last count where every bound holds: {ok}")
    if brk:
        print(f"  → first break at {brk[0]}: {'; '.join(brk[1])}")
    return label, ok, brk


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--min", type=int, default=2)
    ap.add_argument("--no-shot", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "sessions" / "shots"))
    args = ap.parse_args()

    counts = list(range(args.min, args.max + 1))
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed — `pip install playwright && playwright install chromium`")
        return 2

    before = _listeners()
    httpd, port = serve()
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # His two real surfaces: the phone he actually talks into, and the desktop the page
            # is usually open on. A narrower phone is included because 360px is the common floor.
            for label, w, h in [("phone", 412, 915), ("phone-narrow", 360, 800),
                                ("desktop", 1100, 860)]:
                results.append(sweep(browser, port, label, w, h, counts, out, not args.no_shot))
            browser.close()
    finally:
        httpd.shutdown()

    print("\n--- verdict " + "-" * 58)
    ceiling = min(ok for _, ok, _ in results)
    for label, ok, brk in results:
        note = f" (breaks at {brk[0]}: {brk[1][0]})" if brk else ""
        print(f"  {label:14} holds to {ok}{note}")
    print(f"\n  CEILING = {ceiling}  — the narrowest surface decides it")
    if not args.no_shot:
        print(f"  shots in {out}")

    after = _listeners()
    print("\n--- no command-bridge server was started " + "-" * 40)
    print(f"  listeners on {TUNNEL_PORT} unchanged: {after == before}")
    return 0


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
