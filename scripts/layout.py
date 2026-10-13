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
    # A phone held sideways whose landscape width is UNDER 760, so it stays on the stacked phone
    # layout at a short height — the case where the meeting canvas was crushed by an open transcript
    # (JJ, 2026-09-15: "test and fix ... a horizontal view"). 915x412 above is >= 760 and reflows to
    # the desktop split, so it never exercised the short-and-narrow budget.
    ("small phone landscape",            740, 360),
    ("desktop",                         1280, 900),
]

# Build rows exactly as `addRow` does — a .row is a TWO-COLUMN grid (3.5rem tag | 1fr text), so a
# fixture with a single span drops the text into the 3.5rem column and wraps it one word per
# line. That inflates every height and measures a page nobody will ever see.
POPULATE = """([layout, laneCount, sharing]) => {
  const log = document.getElementById('log');
  // THE LANE STRIP, when the fixture asks for it (spec 012). It is `hidden` below two agent lanes,
  // so a run that never registers a second agent measures the page exactly as it was before the
  // feature existed — true, and not what that case is for. Driven through the page's own message
  // handler rather than by writing chips, so what gets measured is what the painter produces.
  if (laneCount >= 2) {
    const names = ['claude', 'codex', 'grok'].slice(0, laneCount);
    window.__voiceTunnel.deliver({ type: 'ready', lanes: names, lane: names[0],
      broadcast: 'everyone',
      // The WORST case on purpose: one lane holding a two-digit count and another mid-work, so the
      // row is measured at its widest and tallest rather than at rest.
      waiting: { [names[names.length - 1]]: 12 },
      lane_states: { [names[1]]: 'synthesizing' } });
  }
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
      // The page grants the wider cluster from `applyPills`, keyed on both pills being visible.
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
  // THE MEETING LAYOUT (spec 016), when the fixture asks for it: `body.sharing` on a wide screen
  // reflows to the canvas-left / lanes-over-transcript-right grid. Set directly here (a lane count of
  // >= 2 is required, so the lane stack — not the solo orb — is what lands in the right column). The
  // SSE that normally toggles this is stubbed in this harness, so the class is ours to own.
  if (sharing) document.body.classList.add('sharing');
  else document.body.classList.remove('sharing');
  log.scrollTop = log.scrollHeight;
}"""

MEASURE = """() => {
  const R = (id) => {
    const el = document.getElementById(id);
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width),
             h: Math.round(b.height), top: Math.round(b.top), right: Math.round(b.right),
             bottom: Math.round(b.bottom), hidden: el.hidden,
             disp: getComputedStyle(el).display };
  };
  const log = document.getElementById('log');
  const rows = log.querySelectorAll('.row');
  const last = rows[rows.length - 1].getBoundingClientRect();
  const lb = log.getBoundingClientRect();
  const ub = document.getElementById('mute').getBoundingClientRect();
  // Zero when a pill is not rendered, which is the honest reading rather than a missing
  // measurement — `problems()` skips the touch check at 0 instead of failing it, and
  // `pillsShown` is what stops "skipped everything" from reading as "passed everything".
  const pill = (wrap, sel) => document.getElementById(wrap).hidden
    ? 0 : Math.round(document.getElementById(sel).getBoundingClientRect().height);
  const wraps = ['devpick', 'micpick', 'spkpick'];
  return {
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    bodyScrollHeight: document.body.scrollHeight,
    bodyScrollWidth: document.body.scrollWidth,
    logBottom: Math.round(lb.bottom),
    logHeight: Math.round(lb.height),
    lastRowBottom: Math.round(last.bottom),
    logScrollsToEnd: Math.abs(log.scrollHeight - log.scrollTop - log.clientHeight) < 2,
    devTouchHeight: pill('devpick', 'dev'),
    micTouchHeight: pill('micpick', 'mic'),
    spkTouchHeight: pill('spkpick', 'spk'),
    muteTouchHeight: Math.round(ub.height),
    pillsShown: wraps.filter((id) => !document.getElementById(id).hidden).length,
    // THE MEETING GEOMETRY (spec 016 KD6): is the canvas the big left region, and are the lanes in
    // the right column ABOVE the transcript? Boxes for #sharecanvas, #orbs and #log, checked in
    // `problems()` only when `sharing` is on.
    sharing: document.body.classList.contains('sharing'),
    // THE FLEET HOME (spec 021): the objective graph is the default wide multi-agent view, so the
    // canvas is on-demand (hidden) rather than the centre. Checked in `problems()` with precedence
    // over the spec-016 meeting asserts, which now only describe the phone/canvas-open cases.
    fleet: document.body.classList.contains('fleet'),
    canvasBox: R('sharecanvas'),
    orbsBox: R('orbs'),
    logBoxR: R('log'),
    // 🔴 THE PER-AGENT CONTROL MOVED FROM THE CHIP STRIP TO THE ORB ROW (spec 014 FR4), so this
    // measures `#orbs` where it used to measure `#lanes`.
    //
    // ⚠ The REQUIREMENT is unchanged and so is every assertion below — at two or more agents there
    // must be a visible, tappable control per lane, one row where the viewport allows, 44px
    // minimum. What changed is which element carries it, because he asked for the badges to become
    // orbs. The strip is hidden whenever the row is shown, so leaving this pointed at `#lanes`
    // would have reported a clean sweep against nothing on screen — the exact vacuity the
    // `wantLanes` guard exists to catch, and it did catch it: 10 red the moment the row shipped.
    laneStripShown: !document.getElementById('orbs').hidden,
    laneChips: document.querySelectorAll('#orbs button').length,
    laneTouchHeight: (() => {
      const b = document.querySelector('#orbs button');
      return b ? Math.round(b.getBoundingClientRect().height) : 0;
    })(),
    // Does the row stay ONE row? Wrapping is allowed and costs height; reporting it means a
    // silent second line shows up as a number rather than as a mystery inside the overflow check.
    laneRows: (() => {
      const tops = new Set(Array.from(document.querySelectorAll('#orbs button'))
                                .map((b) => Math.round(b.getBoundingClientRect().top)));
      return tops.size;
    })(),
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
    # A page that overflows sideways has the same defect as one that overflows down: with
    # `overflow:hidden` on the body there is no scroll container to reach the clipped edge. The
    # meeting layout's two columns are the case most likely to spill horizontally.
    if m["bodyScrollWidth"] > m["innerWidth"] + 1:
        out.append(f"page overflows horizontally by {m['bodyScrollWidth'] - m['innerWidth']}px")
    if not m["logScrollsToEnd"]:
        out.append("the transcript cannot scroll to its own end")
    # The verbose toggle was retired (2026-09-02) — it is the only mode now — so the "mute and
    # verbose share a row" assertion went with it. The mute switch is still the one that has to stay
    # a real touch target.
    if m["muteTouchHeight"] < 44:
        out.append(f"mute switch is {m['muteTouchHeight']}px tall, under the 44px touch minimum")
    # THE ASSERTION THAT WAS MISSING. A pill measured at 0 is skipped below, which is right when it
    # is genuinely not rendered — but it also means an unhidden-nothing fixture would sail through
    # every touch check by measuring none of them.
    if not m["pillsShown"]:
        out.append("no device picker is on screen at all — the fixture measured nothing")
    # THE SAME GUARD, FOR THE LANE STRIP. A case that asked for lanes and measured a hidden row
    # would pass every geometry check below by measuring a page the feature is not on — which is
    # the shape of "18 of 18 clean" against a selector that no longer exists.
    if m.get("wantLanes") and not m["laneStripShown"]:
        out.append("the lane strip was asked for and is not on screen — the fixture measured "
                   "nothing")
    if not m.get("wantLanes") and m["laneStripShown"]:
        out.append("the lane strip is on screen with fewer than two agent lanes")
    if m["laneStripShown"] and m["laneTouchHeight"] < 44:
        out.append(f"lane chips are {m['laneTouchHeight']}px tall, under the 44px touch minimum — "
                   f"the same standard the device pickers are held to, and a control he has to "
                   f"aim at is worse than one costing a few pixels")
    # 0 means "not rendered in this layout", which is correct — see POPULATE. Only a pill that IS
    # on screen has to be tappable.
    for name, key in (("device picker", "devTouchHeight"),
                      ("microphone picker", "micTouchHeight"),
                      ("speaker picker", "spkTouchHeight")):
        if m[key] and m[key] < 44:
            out.append(f"{name} is {m[key]}px tall, under the 44px touch minimum")

    # -------------------------------------------------------- the meeting layout (spec 016, KD6)
    # `layout.py` set NO sharing case until this spec, so the whole meeting geometry shipped with
    # zero coverage. When the fixture turns `body.sharing` on (a wide viewport, >= 2 lanes), assert
    # the four claims of the redesign: the canvas is the big LEFT region, the lanes are in the RIGHT
    # column, the lanes sit ABOVE the transcript, and the transcript is in the right column too.
    if m.get("fleet") and m.get("sharing"):
        # THE FLEET HOME (spec 021 FR1/FR4/FR6): the objective fleet graph is the MAIN area, the
        # transcript is full-height on the right, and the canvas is NOT on screen by default (it is
        # on-demand — opened only on a click to an agent with a pointable frame). The agent cards
        # render over the fleet canvas as the centre cluster (#orbs is the positioning layer).
        c, o, lg = m["canvasBox"], m["orbsBox"], m["logBoxR"]
        if c and c.get("disp") != "none" and c.get("w", 0) > 10:
            out.append("fleet: the canvas is on screen by default — it must be on-demand (FR6)")
        if not o or o["hidden"] or o["w"] < m["innerWidth"] * 0.5:
            out.append("fleet: the fleet graph is not the main area (>= half the width)")
        if not lg or lg.get("disp") == "none" or lg["hidden"]:
            out.append("fleet: the transcript is not on screen")
        else:
            if lg["x"] < o["right"] - 2:
                out.append("fleet: the transcript is not in the right column")
            if lg["h"] < m["innerHeight"] * 0.6:
                out.append(f"fleet: the transcript is only {lg['h']}px tall of {m['innerHeight']} "
                           f"— not full height (FR4)")
    elif m.get("sharing"):
        c, o, lg = m["canvasBox"], m["orbsBox"], m["logBoxR"]
        # WHICH LAYOUT is on screen, not merely which width (JJ, 2026-09-15). The desktop side-by-side
        # meeting layout runs at >= 760px OR in landscape down to 560px — a phone held sideways is wide
        # and short, so it takes the split (canvas left, lanes+transcript right), not the stacked column.
        # The stacked ("phone") layout is therefore only portrait-narrow (or a tiny sub-560 viewport).
        landscape = m["innerWidth"] > m["innerHeight"]
        side_by_side = m["innerWidth"] >= 760 or (landscape and m["innerWidth"] >= 560)
        phone = not side_by_side
        if not c or c["disp"] == "none" or c["w"] < 10:
            out.append("meeting: the canvas is not on screen")
        elif phone:
            # PHONE MEETING (2026-09-15). In portrait the canvas is STACKED under the lane cards, not
            # a left column. The bug this guards was the iframe collapsing to a sliver at the bottom
            # under a full-height transcript, because sharing turned it on with no grid row. Assert the
            # canvas spans the width and gets a real slice of the height — it is the flexible 1fr row;
            # the transcript legitimately takes up to 38dvh when open, so the floor is well under the
            # 0.6 the desktop column asserts — and that the lane CARDS render as the grid ABOVE it (not
            # the voice discs, JJ 2026-09-15) with the transcript below.
            if c["w"] < m["innerWidth"] * 0.9:
                out.append(f"meeting/phone: the canvas is only {c['w']}px wide of {m['innerWidth']} "
                           f"— it should span the width")
            # Floor at 0.2, not the desktop 0.6: with the transcript open the canvas legitimately sits
            # near 0.31 on a small phone (it is the flexible row under a 38dvh transcript, and folds to
            # full height on a tap). The sliver bug drove it to ~0.05-0.10, so 0.2 separates the two
            # with margin rather than tracking the legit value one point away.
            if c["h"] < m["innerHeight"] * 0.2:
                out.append(f"meeting/phone: the canvas is only {c['h']}px tall of {m['innerHeight']} "
                           f"— it collapsed under the transcript")
            if o and not o["hidden"]:
                if o["bottom"] > c["top"] + 2:
                    out.append("meeting/phone: the lane cards are not above the canvas")
                if o["disp"] != "grid":
                    out.append(f"meeting/phone: the lanes render as '{o['disp']}', not the card grid "
                               f"— the phone is still showing the voice discs")
            else:
                out.append("meeting/phone: the lane cards are not on screen")
            if lg and lg.get("disp") != "none" and not lg["hidden"] and lg["top"] < c["bottom"] - 2:
                out.append("meeting/phone: the transcript is not below the canvas")
        else:
            # DESKTOP MEETING: the canvas is the big LEFT region, lanes + transcript in the RIGHT column.
            if c["w"] < m["innerWidth"] * 0.55:
                out.append(f"meeting: the canvas is only {c['w']}px wide of {m['innerWidth']} "
                           f"— not the majority of the width")
            if c["h"] < m["innerHeight"] * 0.6:
                out.append(f"meeting: the canvas is only {c['h']}px tall of {m['innerHeight']} "
                           f"— not the full content height")
            edge = c["right"] - 2
            if o and not o["hidden"]:
                if o["x"] < edge:
                    out.append("meeting: the lane stack is not in the right column "
                               "(it overlaps the canvas)")
                if o["bottom"] > lg["top"] + 2:
                    out.append("meeting: the lane stack is not above the transcript")
            else:
                out.append("meeting: the lane stack is not on screen")
            if lg["x"] < edge:
                out.append("meeting: the transcript is not in the right column")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None, help="directory to write one PNG per viewport")
    args = ap.parse_args()
    if args.shots:
        os.makedirs(args.shots, exist_ok=True)

    try:
        from pagesplit import ui_frame
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "playwright is not installed",
                          "remedy": f"{ROOT}/venv/Scripts/python -m pip install playwright "
                                    f"&& {ROOT}/venv/Scripts/python -m playwright install chromium"}))
        return 2

    # A scratch session dir, so a check never appends to a real turn log — AND a scratch HOME, so the
    # shared canvas file (`~/.command-bridge-canvas.json`) cannot leak a drawn frame in and silently
    # flip `body.sharing` on. The base cases must measure the base layout; the sharing case sets the
    # class itself (see STUB_SSE / POPULATE).
    iso_home = tempfile.mkdtemp(prefix="command-bridge-layout-home-")
    env = dict(os.environ, COMMAND_BRIDGE_DIR=tempfile.mkdtemp(prefix="command-bridge-layout-"),
               USERPROFILE=iso_home, HOME=iso_home, HOMEPATH=iso_home)
    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "bin", "command-bridge-run.py"), "serve",
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
            # AND EVERY ONE OF THEM TWICE: once as a single-agent session, which is the page as it
            # has always been, and once with THREE lanes so the strip is actually on screen. A
            # sweep that only ran the first would report the whole feature green while measuring a
            # row that is `hidden` — which is a harness passing against something that is not
            # there, the quietest failure this repo knows.
            cases = [(lay, n, False, lab, w, h)
                     for lay in ("grouped", "split")
                     for n in (0, 3)
                     for lab, w, h in VIEWPORTS]
            # THE MEETING LAYOUT (spec 016, + phone 2026-09-15). `body.sharing` needs the lane stack
            # (>= 2 lanes), and now reflows at EVERY width: the desktop right-column layout at >= 760px
            # and the stacked phone layout below it (cards on top, canvas, transcript folded under). So
            # the sharing case runs ALL viewports with three lanes — `problems()` branches the geometry
            # assertions on width. This geometry shipped with zero coverage until KD6; the phone half
            # was still uncovered until the cards came down to the phone.
            cases += [("grouped", 3, True, lab, w, h)
                      for lab, w, h in VIEWPORTS]
            for layout, lane_count, sharing, label, w, h in cases:
                top = browser.new_page(viewport={"width": w, "height": h})
                # The /events SSE toggles `body.sharing` from the canvas; stub it so THIS harness owns
                # the class deterministically (base cases off, the sharing case on) rather than
                # racing a canvas the server happens to hold. The page's render path is unchanged —
                # only the source of the class is — exactly as lanestrip.py stubs the WebSocket.
                top.add_init_script(
                    "window.EventSource = function () { this.addEventListener = function () {};"
                    "  this.close = function () {}; };")
                top.goto(f"http://127.0.0.1:{PORT}/?token={TOKEN}", wait_until="load")
                # Spec 023: the layout is the UI frame's, which fills the shell's viewport exactly, so
                # its measurements are the page's.
                page = ui_frame(top)
                page.wait_for_timeout(700)
                page.evaluate(POPULATE, [layout, lane_count, sharing])
                page.wait_for_timeout(400)
                m = page.evaluate(MEASURE)
                m["wantLanes"] = lane_count >= 2
                found = problems(m)

                mode = "meet " if sharing else ("lanes" if lane_count >= 2 else "solo ")
                tag = f"{layout:8} {mode}"
                print(f"{'PASS' if not found else 'FAIL'}  {tag} {label} ({w}x{h})")
                print(f"      {json.dumps(m)}")
                for f in found:
                    print(f"      -> {f}")
                    failures.append(f"{tag} {label}: {f}")

                if args.shots:
                    name = f"layout-{'meet-' if sharing else ''}{layout}-{w}x{h}.png"
                    top.screenshot(path=os.path.join(args.shots, name))
                top.close()
            browser.close()
    finally:
        server.terminate()

    print()
    print("ALL PASS" if not failures else f"{len(failures)} FAILURES")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
