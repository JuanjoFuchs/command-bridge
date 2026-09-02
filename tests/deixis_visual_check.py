"""Spec 011 AC6 — prove a deixis mark actually LIGHTS its target on the canvas, at the anchor word.

NOT a pytest (no `test_` prefix, so it is not collected): it needs a live canvas server and a real
headless browser, which CI does not have. Run it by hand against the isolated shot server:

    command-bridge shot ...              # (any time) ensures a server is up on :8790, session `shot`
    python tests/deixis_visual_check.py  # → prints the timeline and PASS/FAIL, exit 0 on PASS

It places a frame with a target, connects a headless page, fires the cue the say→cue fusion produces,
and asserts the target carries `.pointed` AFTER the anchor word's measured time and NOT before it — so
it checks the timing, not merely that the highlight eventually lands. Verified 2026-09-02:
`{"target_found": true, "before_cue": false, "before_mark_time": false, "after_mark_time": true}`.
"""
import json
import subprocess
import sys

from playwright.sync_api import sync_playwright

PY = "venv/Scripts/python.exe"
URL = "http://127.0.0.1:8790/canvas"


def cli(*args):
    r = subprocess.run([PY, "-m", "command_bridge.cli", *args, "--session", "shot"],
                       capture_output=True, text=True, cwd=".")
    return (r.stdout + r.stderr).strip()


cli("set", "--lane", "magnus", "--id", "vischeck",
    "--html", "<div id='bar-q3' style='padding:60px;font-size:32px'>Q3</div>")
cli("switch", "magnus")

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 800, "height": 600})
    pg.goto(URL, wait_until="networkidle")
    pg.wait_for_timeout(500)

    def lit():
        return pg.evaluate(
            "() => { const el = document.querySelector('#bar-q3');"
            "return el ? el.classList.contains('pointed') : null; }")

    before = lit()
    cli("cue", "--lane", "magnus", "--text", "look [point:#bar-q3]here",
        "--words", json.dumps([{"w": "look", "t": 0.0}, {"w": "here", "t": 0.25}]))
    pg.wait_for_timeout(150)
    mid = lit()          # before the 0.25s mark — must not be lit yet
    pg.wait_for_timeout(400)
    after = lit()        # past the mark — lit
    cli("remove", "vischeck", "--lane", "magnus")
    b.close()

print(json.dumps({"target_found": before is not None, "before_cue": before,
                  "before_mark_time": mid, "after_mark_time": after}))
ok = (before is not None) and after is True and mid is False
print("AC6", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
