"""Focus-fit acceptance (spec 018) — is a focused frame FULLY VISIBLE, and is the canvas ratio
exposed?

`look <id>` used to make a frame's WIDTH fill the viewport, so a frame much taller than it is wide
overflowed — its top and bottom fell off screen and JJ could not see the whole thing. This harness
renders the real `/canvas` page at known viewport sizes, `set`s frames of known shapes, focuses each
with a real `look`, and MEASURES the focused frame's on-screen box against the viewport:

    AC1  a frame taller than the viewport shows its FULL HEIGHT (top and bottom within the viewport)
    AC2  a frame wider than the viewport shows its FULL WIDTH, centered vertically
    AC3  a genuinely long document still fits WIDTH and PARKS at the top (the fallback is intact)

and drives `status` to assert the fourth and fifth:

    AC4  status reports viewport.aspect (= width/height) for the live lane, null when no page
    AC5  `command-bridge describe` documents viewport.aspect

Geometry is invisible to the unit suite and to `e2e.py`, exactly as `layout.py` says — a frame can
render and still be half off screen. So this measures the box, the way `layout.py` measures the
transcript.

    <repo>/venv/Scripts/python scripts/framefit.py            # exits non-zero on the first failure
    <repo>/venv/Scripts/python scripts/framefit.py --shots DIR # also write a PNG per case

ISOLATED: its own COMMAND_BRIDGE_HOME + COMMAND_BRIDGE_DIR and its own port, so it never touches a
live session on the default port.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8796                     # NOT the default 8765 — a live session must be untouched
TOKEN = "framefit-check"
EPS = 2                         # sub-pixel rounding absorb; a real clip is tens of px

# A frame's on-disk shape is deterministic when it is an SVG with a fixed width/height (the card
# gives mermaid/svg `max-width: none`), so the box the camera has to fit is known before the run.
def _svg(w: int, h: int) -> str:
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
            '<rect x="1" y="1" width="%d" height="%d" fill="none" stroke="#888"/>'
            '<line x1="0" y1="0" x2="%d" y2="%d" stroke="#4af"/>'
            '<text x="8" y="24" font-size="18" fill="#4af">%dx%d</text>'
            '</svg>' % (w, h, w, h, w - 2, h - 2, w, h, w, h))


# A genuinely long DOCUMENT: narrow (the .md cap is 68ch) and very tall — many paragraphs, so the
# whole thing cannot be contained at a readable scale and the fit-width/park-top fallback must fire.
_LONG_DOC = "\n\n".join(
    "## Section %d\n\nThis is a paragraph of a genuinely long document. " % i
    + ("It has enough prose that fitting the whole thing on screen would make the text far too "
       "small to read, which is the case the fit-width, park-at-the-top fallback exists for. ") * 3
    for i in range(40))


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path), data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def _get(path: str) -> dict:
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


# Measure the focused frame's client box AND the viewport (#view), rounded.
MEASURE = """(fid) => {
  const view = document.getElementById('view').getBoundingClientRect();
  const card = document.querySelector('[data-frame="' + fid + '"]');
  const b = card ? card.getBoundingClientRect() : null;
  const R = (r) => r ? { left: Math.round(r.left), top: Math.round(r.top),
                         right: Math.round(r.right), bottom: Math.round(r.bottom),
                         w: Math.round(r.width), h: Math.round(r.height) } : null;
  return { view: R(view), card: R(b) };
}"""


def _contains(view, card):
    return (card["left"] >= view["left"] - EPS and card["right"] <= view["right"] + EPS
            and card["top"] >= view["top"] - EPS and card["bottom"] <= view["bottom"] + EPS)


def _fits_width(view, card):
    return card["left"] >= view["left"] - EPS and card["right"] <= view["right"] + EPS


def _centered_v(view, card):
    top_gap = card["top"] - view["top"]
    bot_gap = view["bottom"] - card["bottom"]
    return abs(top_gap - bot_gap) <= max(24, 0.06 * view["h"])


def _centered_h(view, card):
    left_gap = card["left"] - view["left"]
    right_gap = view["right"] - card["right"]
    return abs(left_gap - right_gap) <= max(24, 0.06 * view["w"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None, help="directory to write one PNG per case")
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

    iso_home = tempfile.mkdtemp(prefix="command-bridge-framefit-home-")
    env = dict(os.environ, COMMAND_BRIDGE_DIR=tempfile.mkdtemp(prefix="command-bridge-framefit-"),
               USERPROFILE=iso_home, HOME=iso_home, HOMEPATH=iso_home)
    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "bin", "command-bridge-run.py"), "serve",
         "--session", "framefit", "--port", str(PORT), "--token", TOKEN],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    failures: list[str] = []
    measurements: list[dict] = []
    try:
        time.sleep(6)
        live = _get("/canvas/status").get("live_lane") or "main"

        # ---- AC4: viewport is null before any page connects --------------------------------
        vp_no_client = _get("/canvas/status").get("viewport", "MISSING")
        if vp_no_client not in (None,):
            failures.append(f"AC4: viewport should be null with no page connected, got {vp_no_client!r}")

        cases = [
            # id,       viewport,     frame,                     kind,      expectation
            ("tall",   (1280, 720),  _svg(400, 1400),           "svg",     "contain"),
            ("tallp",  (412, 915),   _svg(360, 1200),           "svg",     "contain"),
            ("wide",   (1280, 720),  _svg(1600, 300),           "svg",     "contain"),
            ("widep",  (915, 412),   _svg(1600, 260),           "svg",     "contain"),
            ("board",  (1280, 800),  _svg(520, 1500),           "svg",     "contain"),
            ("doc",    (1280, 720),  _LONG_DOC,                 "markdown","fallback"),
        ]

        with sync_playwright() as p:
            browser = p.chromium.launch()
            for cid, (w, h), content, kind, expect in cases:
                # a clean canvas per case
                _post("/canvas/clear", {"lane": live})
                page = browser.new_page(viewport={"width": w, "height": h})
                page.goto(f"http://127.0.0.1:{PORT}/canvas?token={TOKEN}", wait_until="load")
                page.wait_for_timeout(700)
                _post("/canvas/frame", {"id": cid, "kind": kind, "content": content, "lane": live})
                page.wait_for_timeout(700)
                _post("/canvas/look", {"id": cid, "lane": live})
                page.wait_for_timeout(1500)   # flight + transition settle
                m = page.evaluate(MEASURE, cid)
                view, card = m["view"], m["card"]

                found = []
                if not card:
                    found.append("the focused frame did not render at all")
                elif expect == "contain":
                    if not _contains(view, card):
                        found.append(f"frame not fully visible: card={card} view={view}")
                    # a tall frame is centered horizontally; a wide one vertically
                    if card["h"] >= card["w"] and not _centered_h(view, card):
                        found.append("tall frame not horizontally centered")
                    if card["w"] > card["h"] and not _centered_v(view, card):
                        found.append("wide frame not vertically centered")
                else:  # fallback: fit width, parked at top, overflowing the bottom
                    if not _fits_width(view, card):
                        found.append(f"long doc should fit width: card={card} view={view}")
                    if card["top"] > view["top"] + 0.15 * view["h"]:
                        found.append(f"long doc not parked at top: card.top={card['top']} view.top={view['top']}")
                    if card["bottom"] <= view["bottom"] + EPS:
                        found.append("long doc did not overflow — it is not the fallback case (make it taller)")

                measurements.append({"case": cid, "expect": expect, "view": view, "card": card,
                                     "problems": found})
                status = "PASS" if not found else "FAIL"
                print(f"{status}  {cid:6} {expect:8} ({w}x{h})  {json.dumps({'view': view, 'card': card})}")
                for f in found:
                    print(f"      -> {f}")
                    failures.append(f"{cid}: {f}")

                # ---- AC4: with a page connected, viewport carries width/height/aspect ----------
                # The viewport is the CANVAS's available area (#view = the window minus the header),
                # which is exactly what a frame must fit and author toward — so its aspect is
                # width/height of THAT, not of the raw window. The invariant is internal: aspect ==
                # round(width/height, 2), a real measurement the page pushed, not a guess.
                if cid == "tall":
                    st = _get("/canvas/status")
                    vp = st.get("viewport")
                    if not isinstance(vp, dict):
                        failures.append(f"AC4: viewport missing with a page connected: {st.get('viewport')!r}")
                    else:
                        vw, vh, asp = vp.get("width"), vp.get("height"), vp.get("aspect")
                        if not (vw and vh):
                            failures.append(f"AC4: viewport lacks width/height: {vp!r}")
                        elif asp != round(vw / vh, 2):
                            failures.append(f"AC4: viewport.aspect={asp} != round(w/h)={round(vw/vh, 2)} "
                                            f"(w={vw} h={vh})")
                        elif vw != w:
                            failures.append(f"AC4: reported width {vw} != canvas width {w}")
                        else:
                            print(f"      AC4 PASS status.viewport = {json.dumps(vp)} "
                                  f"(canvas area = window {w}x{h} minus header)")

                if args.shots:
                    page.screenshot(path=os.path.join(args.shots, f"framefit-{cid}-{w}x{h}.png"))
                page.close()
            browser.close()

        # ---- AC5: describe documents viewport.aspect ---------------------------------------
        desc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "bin", "command-bridge-run.py"), "describe"],
            capture_output=True, text=True, env=env)
        if "viewport.aspect" not in desc.stdout and "aspect" not in desc.stdout:
            failures.append("AC5: `describe` does not document viewport.aspect")
        else:
            print("PASS  AC5   describe documents viewport.aspect")
    finally:
        server.terminate()

    print()
    if args.shots is None:
        # a compact dump so a run's numbers are inspectable without --shots
        print(json.dumps(measurements, indent=1))
    print("ALL PASS" if not failures else f"{len(failures)} FAILURES")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
