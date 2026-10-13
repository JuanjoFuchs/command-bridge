"""Font-legibility acceptance (spec 019) — does the tool tell an agent, ON DRAW, whether a frame's
text is readable at his screen?

Spec 018 makes a focused frame fully visible by CONTAINING it — but containing a large frame into a
small viewport renders its text tiny, and the agent that drew it cannot tell. Spec 019 has the PAGE
measure each frame's EFFECTIVE minimum font (its smallest text times the contain-fit scale) and report
it, with a `readable` flag against a documented floor. This harness renders the real `/canvas` page at
known viewport sizes, `set`s frames of known text size, and reads back the reported legibility over the
same page→/placed→status path the live tool uses:

    AC1  a large, text-dense frame in a small viewport → readable:false, min_font_px < READABLE_PX
    AC2  a lean frame with large text → readable:true, min_font_px >= READABLE_PX
    AC3  the SAME frame reports a SMALLER min_font_px at a smaller viewport (the fit scale shrinks it)
    AC4  status reports per-frame {min_font_px, readable, fit_scale}, and null with no page connected
    AC5  a `set` with a page returns `legibility`; a `set` with no page returns promptly without it
    AC6  `command-bridge describe` documents the fields + READABLE_PX

Font sizes are invisible to the unit suite (a frame renders and still be unreadable when contained),
so — like framefit.py / layout.py — this MEASURES the rendered result through a real browser.

    <repo>/venv/Scripts/python scripts/fontfit.py            # exits non-zero on the first failure
    <repo>/venv/Scripts/python scripts/fontfit.py --shots DIR # also write a PNG per case

ISOLATED: its own COMMAND_BRIDGE_HOME + COMMAND_BRIDGE_DIR and its own port (NOT 8765), so it never
touches a live session.
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
RUN = os.path.join(ROOT, "bin", "command-bridge-run.py")
PORT = 8797                     # NOT 8765 (live) and NOT 8796 (framefit)
TOKEN = "fontfit-check"


def _svg(w: int, h: int, font: int, label: str = "smallest") -> str:
    # A frame of KNOWN text size in a KNOWN box: 1:1 viewBox so a user unit is a canvas px, and one
    # <text> so it IS the smallest text — its effective size is font × the contain-fit scale.
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'
            '<rect x="1" y="1" width="%d" height="%d" fill="none" stroke="#888"/>'
            '<text x="12" y="%d" font-size="%d" fill="#4af">%s %dpx</text>'
            '</svg>' % (w, h, w, h, w - 2, h - 2, font + 6, font, label, font))


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path), data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def _get(path: str) -> dict:
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def _leg(lane: str, fid: str) -> dict | None:
    for f in _get("/canvas/status").get("lanes", {}).get(lane, []):
        if f.get("id") == fid:
            return f
    return None


def _cli_set(env: dict, lane: str, fid: str, content: str) -> tuple[dict, float]:
    """Drive the real `command-bridge set` CLI (which is what an agent runs) and time it, so AC5
    exercises the best-effort round-trip and the non-blocking no-page path for real."""
    t0 = time.time()
    res = subprocess.run(
        [sys.executable, RUN, "set", "--session", "fontfit", "--id", fid,
         "--svg", content, "--lane", lane],
        capture_output=True, text=True, env=env)
    dt = time.time() - t0
    try:
        return json.loads(res.stdout or "{}"), dt
    except json.JSONDecodeError:
        return {"error": "unparseable", "stdout": res.stdout, "stderr": res.stderr}, dt


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

    iso_home = tempfile.mkdtemp(prefix="command-bridge-fontfit-home-")
    env = dict(os.environ, COMMAND_BRIDGE_DIR=tempfile.mkdtemp(prefix="command-bridge-fontfit-"),
               USERPROFILE=iso_home, HOME=iso_home, HOMEPATH=iso_home)
    server = subprocess.Popen(
        [sys.executable, RUN, "serve",
         "--session", "fontfit", "--port", str(PORT), "--token", TOKEN],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    # The floor the server judges `readable` against — read from the code so the harness can never
    # drift from the constant it is checking.
    sys.path.insert(0, ROOT)
    from command_bridge.canvas import server as canvas_server
    FLOOR = canvas_server.READABLE_PX

    failures: list[str] = []
    measurements: list[dict] = []
    try:
        time.sleep(6)
        live = _get("/canvas/status").get("live_lane") or "main"

        # ---- AC5 (no page): a set with NOBODY connected returns promptly, no legibility ----------
        out, dt = _cli_set(env, live, "nopage", _svg(800, 600, 20))
        if "legibility" in out:
            failures.append(f"AC5: set with no page must NOT include legibility, got {out.get('legibility')!r}")
        elif not out.get("ok"):
            failures.append(f"AC5: set with no page did not succeed: {out!r}")
        else:
            print(f"PASS  AC5   set (no page) returned in {dt:.2f}s with no legibility")
        if dt > 3.0:
            failures.append(f"AC5: set with no page blocked for {dt:.2f}s — it must not wait the window")
        _post("/canvas/clear", {"lane": live})

        # ---- AC4 (no page): per-frame legibility is null before any browser connects -------------
        _post("/canvas/frame", {"id": "prewarm", "kind": "svg", "content": _svg(900, 700, 16),
                                "lane": live})
        pre = _leg(live, "prewarm")
        if not pre:
            failures.append("AC4: the frame is missing from status with no page connected")
        elif pre.get("min_font_px") is not None or pre.get("readable") is not None:
            failures.append(f"AC4: legibility must be null with no page connected, got {pre!r}")
        else:
            print("PASS  AC4   status legibility is null with no page connected")
        _post("/canvas/clear", {"lane": live})

        # id,     viewport,     frame,                 expect,        note
        cases = [
            ("dense", (412, 915),  _svg(1600, 1200, 14), "unreadable"),   # AC1: big + tiny text, phone
            ("lean",  (1280, 800), _svg(500, 350, 18),   "readable"),     # AC2: small + large text
            ("scaleb",(1280, 800), _svg(1600, 1200, 14), "scale"),        # AC3: same frame, two ports
            ("scales",(640, 400),  _svg(1600, 1200, 14), "scale"),        # AC3: smaller port
        ]
        scale_min: dict[str, float] = {}

        with sync_playwright() as p:
            browser = p.chromium.launch()
            for cid, (w, h), content, expect in cases:
                _post("/canvas/clear", {"lane": live})
                page = browser.new_page(viewport={"width": w, "height": h})
                page.goto(f"http://127.0.0.1:{PORT}/canvas?token={TOKEN}", wait_until="load")
                page.wait_for_timeout(700)
                _post("/canvas/frame", {"id": cid, "kind": "svg", "content": content, "lane": live})
                page.wait_for_timeout(1300)   # render + layout + the /placed report
                leg = _leg(live, cid)

                found = []
                if not leg or leg.get("min_font_px") is None:
                    found.append(f"no legibility reported for {cid}: {leg!r}")
                else:
                    mf, rd, fs = leg["min_font_px"], leg["readable"], leg["fit_scale"]
                    if expect == "unreadable":
                        if not (mf < FLOOR):
                            found.append(f"expected min_font_px < {FLOOR}, got {mf}")
                        if rd is not False:
                            found.append(f"expected readable:false, got {rd}")
                    elif expect == "readable":
                        if not (mf >= FLOOR):
                            found.append(f"expected min_font_px >= {FLOOR}, got {mf}")
                        if rd is not True:
                            found.append(f"expected readable:true, got {rd}")
                    else:  # scale
                        scale_min[cid] = mf
                    if fs is None:
                        found.append("fit_scale missing")

                measurements.append({"case": cid, "expect": expect, "viewport": [w, h], "leg": leg,
                                     "problems": found})
                status = "PASS" if not found else "FAIL"
                print(f"{status}  {cid:7} {expect:10} ({w}x{h})  {json.dumps(leg)}")
                for f in found:
                    print(f"      -> {f}")
                    failures.append(f"{cid}: {f}")

                # ---- AC4 (page connected) + AC5 (page connected) on the first case ----------------
                if cid == "dense":
                    st_frame = _leg(live, cid)
                    if not st_frame or st_frame.get("min_font_px") is None:
                        failures.append(f"AC4: page connected but status legibility null: {st_frame!r}")
                    else:
                        print(f"      AC4 PASS status carries {json.dumps(st_frame)} with a page connected")
                    # AC5: a set with THIS page connected returns the drawn frame's legibility.
                    out, dt = _cli_set(env, live, "withpage", _svg(1500, 1100, 13))
                    page.wait_for_timeout(200)
                    leg5 = out.get("legibility")
                    if not isinstance(leg5, dict) or leg5.get("min_font_px") is None:
                        failures.append(f"AC5: set with a page must return legibility, got {out.get('legibility')!r}")
                    else:
                        print(f"      AC5 PASS set (page) returned legibility={json.dumps(leg5)} in {dt:.2f}s")
                    _post("/canvas/remove", {"id": "withpage", "lane": live})

                if args.shots:
                    page.screenshot(path=os.path.join(args.shots, f"fontfit-{cid}-{w}x{h}.png"))
                page.close()
            browser.close()

        # ---- AC3: the same frame reports a smaller min_font_px at a smaller viewport --------------
        if "scaleb" in scale_min and "scales" in scale_min:
            if not (scale_min["scales"] < scale_min["scaleb"]):
                failures.append(f"AC3: min_font_px did not shrink with the viewport: "
                                f"{scale_min['scaleb']} (1280x800) vs {scale_min['scales']} (640x400)")
            else:
                print(f"PASS  AC3   min_font_px shrinks with viewport: "
                      f"{scale_min['scaleb']} (1280x800) -> {scale_min['scales']} (640x400)")
        else:
            failures.append("AC3: the two scale cases did not both report a min_font_px")

        # ---- AC6: describe documents the fields + READABLE_PX -------------------------------------
        desc = subprocess.run([sys.executable, RUN, "describe"], capture_output=True, text=True, env=env)
        doc = desc.stdout
        needed = ["min_font_px", "readable", "fit_scale", ("%g" % FLOOR)]
        missing = [n for n in needed if n not in doc]
        if missing:
            failures.append(f"AC6: describe is missing {missing}")
        else:
            print(f"PASS  AC6   describe documents min_font_px/readable/fit_scale + floor {FLOOR:g}")
    finally:
        server.terminate()

    print()
    if args.shots is None:
        print(json.dumps(measurements, indent=1))
    print(f"READABLE_PX = {FLOOR:g}")
    print("ALL PASS" if not failures else f"{len(failures)} FAILURES")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
