"""Spec 011 AC6, spec 020 AC5 — prove a deixis mark actually LIGHTS its target on the canvas, at the
anchor word (AC6), and that spec 020's verify capture lights EXACTLY the mark it was asked for and no
other (AC5).

NOT a pytest (no `test_` prefix, so it is not collected): it needs a live canvas server and a real
headless browser, which CI does not have. Run it by hand against the isolated shot server:

    command-bridge shot ...              # (any time) ensures a server is up on :8790, session `shot`
    python tests/deixis_visual_check.py  # → prints the timeline and PASS/FAIL, exit 0 on PASS

AC6 places a frame with a target, connects a headless page, fires the cue the say→cue fusion
produces, and asserts the target carries `.pointed` AFTER the anchor word's measured time and NOT
before it — so it checks the timing, not merely that the highlight eventually lands. Verified
2026-09-02: `{"target_found": true, "before_cue": false, "before_mark_time": false,
"after_mark_time": true}`.

AC5 drives the EXACT mechanism `_capture_verify`/`shot.capture` use for a per-mark verify capture —
a fresh headless page hitting the canvas URL with `?shot=…&point=<selector>` — and asserts that
capturing `#bar-q3` lights `#bar-q3` and NOT `#bar-q4`, and vice versa: each per-mark picture shows
that target highlighted and not the others.
"""
import json
import subprocess
import sys
from urllib.parse import quote

from playwright.sync_api import sync_playwright

PY = "venv/Scripts/python.exe"
URL = "http://127.0.0.1:8790/canvas"


def cli(*args):
    r = subprocess.run([PY, "-m", "command_bridge.cli", *args, "--session", "shot"],
                       capture_output=True, text=True, cwd=".")
    return (r.stdout + r.stderr).strip()


cli("set", "--lane", "magnus", "--id", "vischeck",
    "--html", "<div id='bar-q3' style='padding:60px;font-size:32px'>Q3</div>"
              "<div id='bar-q4' style='padding:60px;font-size:32px'>Q4</div>")
cli("switch", "magnus")

results = {}

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 800, "height": 600})
    pg.goto(URL, wait_until="networkidle")
    pg.wait_for_timeout(500)

    def lit(selector="#bar-q3", page=None):
        return (page or pg).evaluate(
            "(sel) => { const el = document.querySelector(sel);"
            "return el ? el.classList.contains('pointed') : null; }", selector)

    before = lit()
    cli("cue", "--lane", "magnus", "--text", "look [point:#bar-q3]here",
        "--words", json.dumps([{"w": "look", "t": 0.0}, {"w": "here", "t": 0.25}]))
    pg.wait_for_timeout(150)
    mid = lit()          # before the 0.25s mark — must not be lit yet
    pg.wait_for_timeout(400)
    after = lit()        # past the mark — lit
    ac6_ok = (before is not None) and after is True and mid is False
    results["AC6"] = {"target_found": before is not None, "before_cue": before,
                       "before_mark_time": mid, "after_mark_time": after, "ok": ac6_ok}

    # ---- spec 020 AC5: a per-mark verify capture, driven the way `shot.capture(point=...)` drives
    # it — a fresh headless page hitting `?shot=<ms>&lane=<lane>&point=<selector>` — lights ONLY the
    # selector it was asked for.
    def shot_pointed(point_selector):
        settle = 800
        url = URL + f"?shot={settle}&lane=magnus"
        if point_selector:
            url += "&point=" + quote(point_selector, safe="")
        page = b.new_page(viewport={"width": 800, "height": 600})
        page.goto(url, wait_until="load", timeout=8000)
        q3, q4 = lit("#bar-q3", page), lit("#bar-q4", page)
        page.close()
        return q3, q4

    base_q3, base_q4 = shot_pointed(None)
    q3_q3, q3_q4 = shot_pointed("#bar-q3")
    q4_q3, q4_q4 = shot_pointed("#bar-q4")
    ac5_ok = (base_q3 is False and base_q4 is False
              and q3_q3 is True and q3_q4 is False
              and q4_q3 is False and q4_q4 is True)
    results["AC5"] = {"base": {"q3": base_q3, "q4": base_q4},
                       "point_q3": {"q3": q3_q3, "q4": q3_q4},
                       "point_q4": {"q3": q4_q3, "q4": q4_q4}, "ok": ac5_ok}

    cli("remove", "vischeck", "--lane", "magnus")
    b.close()

print(json.dumps(results))
for name in ("AC6", "AC5"):
    print(name, "PASS" if results[name]["ok"] else "FAIL")
sys.exit(0 if all(r["ok"] for r in results.values()) else 1)
