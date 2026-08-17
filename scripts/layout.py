"""Layout acceptance — is the bottom of the transcript actually on screen?

**Why this exists as its own harness.** `e2e.py` proves the pipeline carries audio and `uitest.py`
proves a real microphone reaches it. Neither looks at geometry, and geometry is where this page
has failed twice on a real phone in ways every green test missed:

    "your responses are not rendering properly on the UI. Usually the last one is cut off"
    "the bottom of the screen is usually not visible and I cannot scroll down to see it"

Both are measurable, and the measurement is two claims that must hold at EVERY viewport:

    1. the page does not overflow the viewport   body.scrollHeight <= innerHeight
    2. the last transcript row is inside it      row.bottom      <= innerHeight

Claim 2 without claim 1 is the exact failure mode reported: the content exists and is simply
below the fold, with `overflow:hidden` on the body and no scroll container anywhere to reach it.
Checking only "did the row render" would have passed throughout.

The viewport list is the point. A phone is not one size — Android Chrome's URL bar shows and
hides as you scroll, and the difference (915 vs 732 CSS px on a Pixel 7) is larger than most
desktop breakpoints. The bug only appeared in the shorter of the two, which is why it survived
being looked at on a laptop.

    <repo>/venv/Scripts/python scripts/layout.py            # exits non-zero on the first failure
    <repo>/venv/Scripts/python scripts/layout.py --shots DIR # also write a PNG per viewport
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8794
TOKEN = "layout-check"

VIEWPORTS = [
    # label,                              w,    h
    ("pixel 7 portrait, url bar hidden", 412, 915),
    ("pixel 7 portrait, url bar shown",  412, 732),   # where the bug actually lived
    ("small phone portrait",             360, 640),
    ("pixel 7 landscape",                915, 412),
    ("desktop",                         1280, 900),
]

# Build rows exactly as `addRow` does — a .row is a TWO-COLUMN grid (3.5rem tag | 1fr text), so a
# fixture with a single span drops the text into the 3.5rem column and wraps it one word per
# line. That inflates every height and measures a page nobody will ever see.
POPULATE = """(layout) => {
  const log = document.getElementById('log');
  document.getElementById('mute').setAttribute('aria-checked', 'false');
  // TWO LAYOUTS, AND EXACTLY ONE OF THEM IS EVER ON SCREEN. `grouped` is the normal case: one
  // pill for one physical device, because `enumerateDevices()` reports the two halves of a headset
  // under a shared `groupId`. `split` is the fallback for platforms that group badly — and it is
  // the WIDER arrangement, so both are measured rather than assuming the narrow one is worse.
  //
  // `hidden` lives on the WRAPPER, not the select. The pill is a <label> carrying the glyph that
  // says which direction it points, and unhiding the select alone leaves the control collapsed to
  // zero height. `pillsShown` below is the assertion that was missing for exactly that: nothing
  // used to check that the thing being measured was actually on the page.
  const show = (wrap, sel, text) => {
    document.getElementById(wrap).hidden = false;
    document.getElementById(sel).innerHTML = '<option>' + text + '</option>';
  };
  if (layout === 'grouped') {
    show('devpick', 'dev', 'Headset (WH-1000XM4)');
  } else {
    show('micpick', 'mic', 'Headset Microphone (Realtek(R) Audio)');
    // THE OUTPUT PICKER ONLY EXISTS WHERE THE PLATFORM CAN ROUTE AUDIO PER PAGE, which is never
    // Android — `setSinkId` is unavailable there, so the page does not render a control it cannot
    // honour. A phone therefore shows one pill and a desktop shows two, and the fixture reproduces
    // that split rather than asserting one layout everywhere: forced into 360px, four controls wrap
    // the two switches onto separate lines and fail `switchesSameRow` — a true measurement of a
    // situation the target device cannot reach, which is the least useful kind of red.
    if (window.innerWidth >= 480) {
      show('spkpick', 'spk', 'Headphones (Realtek(R) Audio)');
      // The page grants the wider cluster from `paintGroup`, keyed on both pills being visible.
      // The fixture is faking that visibility, so it has to fake the consequence too — otherwise
      // this measures a two-pill row inside the one-pill width, which the page never renders.
      document.getElementById('controls').classList.add('split');
    }
  }
  const said = [
    ['me', 'what is the status of the deploy right now'],
    ['claude', 'That is running clean. 225 tests, no failures, and the tunnel is up.'],
  ];
  for (let i = 0; i < 12; i++) {
    const [who, text] = said[i % 2];
    const d = document.createElement('div');
    d.className = 'row' + (who === 'claude' ? ' claude' : ' addressed');
    const tag = document.createElement('span');
    tag.className = 'tag'; tag.textContent = who;
    const body = document.createElement('span');
    body.textContent = text;
    d.appendChild(tag); d.appendChild(body);
    log.appendChild(d);
  }
  log.scrollTop = log.scrollHeight;
}"""

MEASURE = """() => {
  const log = document.getElementById('log');
  const rows = log.querySelectorAll('.row');
  const last = rows[rows.length - 1].getBoundingClientRect();
  const lb = log.getBoundingClientRect();
  const vb = document.getElementById('verbose').getBoundingClientRect();
  const ub = document.getElementById('mute').getBoundingClientRect();
  // Zero when a pill is not rendered, which is the honest reading rather than a missing
  // measurement — `problems()` skips the touch check at 0 instead of failing it, and
  // `pillsShown` is what stops "skipped everything" from reading as "passed everything".
  const pill = (wrap, sel) => document.getElementById(wrap).hidden
    ? 0 : Math.round(document.getElementById(sel).getBoundingClientRect().height);
  const wraps = ['devpick', 'micpick', 'spkpick'];
  return {
    innerHeight: window.innerHeight,
    bodyScrollHeight: document.body.scrollHeight,
    logBottom: Math.round(lb.bottom),
    logHeight: Math.round(lb.height),
    lastRowBottom: Math.round(last.bottom),
    logScrollsToEnd: Math.abs(log.scrollHeight - log.scrollTop - log.clientHeight) < 2,
    // MUTE AND VERBOSE must share a row — that is the actual requirement ("move the mute to a
    // different toggle similar to the verbose mode on that same line"). The microphone picker
    // is allowed to wrap below on a narrow screen: three controls do not fit across 360px, and
    // forcing them would shrink a touch target rather than solve anything.
    switchesSameRow: Math.abs(ub.top - vb.top) < 6,
    muteLeftOfVerbose: ub.right <= vb.left + 1,
    devTouchHeight: pill('devpick', 'dev'),
    micTouchHeight: pill('micpick', 'mic'),
    spkTouchHeight: pill('spkpick', 'spk'),
    muteTouchHeight: Math.round(ub.height),
    pillsShown: wraps.filter((id) => !document.getElementById(id).hidden).length,
  };
}"""


def problems(m: dict) -> list:
    out = []
    # +1 absorbs sub-pixel rounding; anything real is tens of pixels.
    if m["bodyScrollHeight"] > m["innerHeight"] + 1:
        out.append(f"page overflows the viewport by {m['bodyScrollHeight'] - m['innerHeight']}px "
                   f"— with overflow:hidden there is no way to scroll to what is below")
    if m["logBottom"] > m["innerHeight"] + 1:
        out.append(f"transcript bottom {m['logBottom']} is below the fold ({m['innerHeight']})")
    if m["lastRowBottom"] > m["innerHeight"] + 1:
        out.append(f"the newest row ends at {m['lastRowBottom']}, off screen")
    if not m["logScrollsToEnd"]:
        out.append("the transcript cannot scroll to its own end")
    if not m["switchesSameRow"]:
        out.append("mute and verbose are not on the same line")
    if not m["muteLeftOfVerbose"]:
        out.append("mute is not to the left of verbose")
    if m["muteTouchHeight"] < 44:
        out.append(f"mute switch is {m['muteTouchHeight']}px tall, under the 44px touch minimum")
    # THE ASSERTION THAT WAS MISSING. A pill measured at 0 is skipped below, which is right when it
    # is genuinely not rendered — but it also means an unhidden-nothing fixture would sail through
    # every touch check by measuring none of them.
    if not m["pillsShown"]:
        out.append("no device picker is on screen at all — the fixture measured nothing")
    # 0 means "not rendered in this layout", which is correct — see POPULATE. Only a pill that IS
    # on screen has to be tappable.
    for name, key in (("device picker", "devTouchHeight"),
                      ("microphone picker", "micTouchHeight"),
                      ("speaker picker", "spkTouchHeight")):
        if m[key] and m[key] < 44:
            out.append(f"{name} is {m[key]}px tall, under the 44px touch minimum")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None, help="directory to write one PNG per viewport")
    args = ap.parse_args()
    if args.shots:
        os.makedirs(args.shots, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "playwright is not installed",
                          "remedy": f"{ROOT}/venv/Scripts/python -m pip install playwright "
                                    f"&& {ROOT}/venv/Scripts/python -m playwright install chromium"}))
        return 2

    # A scratch session dir, so a check never appends to a real turn log.
    env = dict(os.environ, VOICE_TUNNEL_DIR=tempfile.mkdtemp(prefix="voice-tunnel-layout-"))
    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "bin", "voice-tunnel-run.py"), "serve",
         "--session", "layout", "--port", str(PORT), "--token", TOKEN],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    failures = []
    try:
        time.sleep(6)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            # EVERY VIEWPORT IN BOTH LAYOUTS. The grouped pill is what the page normally renders;
            # the split pair is the fallback and the wider of the two. Measuring only one of them
            # is how the two-picker row came to fit while the cluster check it broke went unrun.
            cases = [(lay, lab, w, h) for lay in ("grouped", "split") for lab, w, h in VIEWPORTS]
            for layout, label, w, h in cases:
                page = browser.new_page(viewport={"width": w, "height": h})
                page.goto(f"http://127.0.0.1:{PORT}/?token={TOKEN}", wait_until="load")
                page.wait_for_timeout(700)
                page.evaluate(POPULATE, layout)
                page.wait_for_timeout(400)
                m = page.evaluate(MEASURE)
                found = problems(m)

                print(f"{'PASS' if not found else 'FAIL'}  {layout:8} {label} ({w}x{h})")
                print(f"      {json.dumps(m)}")
                for f in found:
                    print(f"      -> {f}")
                    failures.append(f"{layout} {label}: {f}")

                if args.shots:
                    page.screenshot(
                        path=os.path.join(args.shots, f"layout-{layout}-{w}x{h}.png"))
                page.close()
            browser.close()
    finally:
        server.terminate()

    print()
    print("ALL PASS" if not failures else f"{len(failures)} FAILURES")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
