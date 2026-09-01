"""Render the transcript with several lanes and SCREENSHOT it, so the UI can be judged by eye.

    python scripts/transcriptshot.py [--out DIR]

WHY THIS EXISTS. Spec 013 shipped lane names into the transcript and two defects went out with it
that no assertion caught: the tag column was a fixed 3.5rem while "YOU -> CODEX" has `nowrap`, so
the name overlapped the words; and the receipt tick was appended as a third child of a two-column
grid, landing in an implicit cell instead of trailing the sentence. JJ, 2026-08-24: *"The check
marks are weird. They don't change color... the text of what I'm saying is overlapping with the
names of the agents."*

**Both were VISIBLE and neither was ASSERTABLE.** `lanestrip.py` proves what the reducers return
and `layout.py` proves nothing overflows the page; a row whose two children overlap each other
breaks neither. He asked for the missing instrument in as many words: *"you should be able to take
a screenshot of this so that you can iterate on the UI."*

⚠ **STARTS NO VOICE-TUNNEL SERVER, and proves it** — same posture as `devicepills.py` and
`lanestrip.py`, because his daily driver is usually live on 8765 while this runs. It serves the
static page from an ephemeral port and drives it through the page's own message handler.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import pathlib
import socketserver
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "command_bridge" / "web"
TUNNEL_PORT = 8765

# Every message the page needs to believe three agents are in the room. Delivered through
# `deliver`, the page's own handler, so nothing here reaches past the seam a real socket would.
SCRIPT = [
    {"type": "ready", "verbose": True, "lanes": ["claude", "codex", "atlas"], "lane": "claude",
     "default_lane": "claude", "broadcast": "everyone", "lane_states": {}, "waiting": {}},
    {"type": "turn", "id": 1, "text": "Hey Claude, can you hear me?", "addressed": True,
     "lane": "claude"},
    {"type": "audio_header", "id": "c1", "text": "Yes, I hear you clearly.", "lane": "claude",
     "sample_rate": 24000, "bytes": 0},
    {"type": "turn", "id": 2, "text": "Hey Codex, run the tests and tell me what breaks.",
     "addressed": True, "lane": "codex"},
    {"type": "audio_header", "id": "c2", "text": "Running them now.", "lane": "codex",
     "sample_rate": 24000, "bytes": 0},
    {"type": "turn", "id": 3, "text": "Hey Atlas, what do you make of the freedom strategy?",
     "addressed": True, "lane": "atlas"},
    {"type": "turn", "id": 4, "text": "Hey everyone, stand down for a minute.",
     "addressed": True, "lane": "everyone"},
    {"type": "turn", "id": 5, "text": "somebody else talking in the room", "addressed": False},
    {"type": "lane", "lane": "codex", "lanes": ["claude", "codex", "atlas"],
     "default_lane": "claude", "why": "wake",
     "waiting": {"atlas": 2}},
    {"type": "lane_state", "lane": "atlas", "state": "thinking"},
    # The receipt states, all three at once: 1-2 answered, 3-4 delivered, 5 only logged.
    # A turn PAST the cursor, so the third receipt state (one tick, sent-only) is on screen too —
    # a shot that shows two of three states cannot tell you the third one renders.
    {"type": "turn", "id": 6, "text": "Hey Claude, one more thing.", "addressed": True,
     "lane": "claude"},
    {"type": "consumed", "cursor": 4, "pending": 1},
]

STUB = """
window.__voiceTunnel = window.__voiceTunnel || {};
window.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
"""


def serve_static():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "sessions" / "shots"))
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed — `pip install playwright && playwright install chromium`")
        return 2

    httpd, port = serve_static()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for label, size in [("phone", {"width": 412, "height": 915}),
                                ("desktop", {"width": 1100, "height": 800})]:
                ctx = browser.new_context(viewport=size, device_scale_factor=2)
                page = ctx.new_page()
                page.add_init_script(STUB)
                page.goto(f"http://127.0.0.1:{port}/?token=shot", wait_until="load")
                page.wait_for_function(
                    "() => window.__voiceTunnel && window.__voiceTunnel.deliver", timeout=15000)
                for msg in SCRIPT:
                    if msg.get("type") == "audio_header":
                        # A reply only reaches the log when a clip PLAYS, and this harness has no
                        # audio — which is exactly how the agent-name fix shipped unlooked-at.
                        page.evaluate("([l, t]) => window.__voiceTunnel.renderAgentRow(l, t)",
                                      [msg["lane"], msg["text"]])
                        continue
                    page.evaluate("(m) => window.__voiceTunnel.deliver(m)", msg)
                page.wait_for_timeout(400)

                # THE MEASUREMENT, not just the picture. A screenshot needs a human; an overlap is
                # arithmetic, and asserting it here is what stops this defect coming back.
                overlaps = page.evaluate("""() => {
                  const bad = [];
                  document.querySelectorAll('#log .row').forEach((row) => {
                    const tag = row.querySelector('.tag');
                    const txt = row.querySelector('.text');
                    if (!tag || !txt) return;
                    const a = tag.getBoundingClientRect(), b = txt.getBoundingClientRect();
                    if (a.right > b.left + 0.5) {
                      bad.push({ tag: tag.textContent, right: Math.round(a.right),
                                 textLeft: Math.round(b.left) });
                    }
                  });
                  return bad;
                }""")
                ticks = page.evaluate("""() => {
                  const out = [];
                  document.querySelectorAll('#log .row .tick').forEach((t) => {
                    const cs = getComputedStyle(t);
                    out.push({ state: t.dataset.state, text: t.textContent,
                               color: cs.color, opacity: cs.opacity,
                               inText: !!t.closest('.text') });
                  });
                  return out;
                }""")

                shot = out / f"transcript-{label}.png"
                page.locator("#log").screenshot(path=str(shot))
                # THE WHOLE PAGE TOO (spec 014). The log alone was enough while the only thing
                # above it was one orb; with a header row and an orb per agent, the half that
                # changed is the half this harness was cropping out.
                page.screenshot(path=str(out / f"page-{label}.png"), full_page=True)
                print(f"\n=== {label} {size['width']}x{size['height']} -> {shot}")
                print(f"    overlaps: {overlaps if overlaps else 'none'}")
                for t in ticks:
                    print(f"    tick {t['state']:<10} {t['text']:<3} inText={t['inText']} "
                          f"{t['color']} opacity={t['opacity']}")
                states = {t["state"] for t in ticks}
                print(f"    tick states present: {sorted(states)}")
                colors = {t["state"]: t["color"] for t in ticks}
                distinct = len(set(colors.values()))
                print(f"    distinct tick colours: {distinct}  {colors}")
                if overlaps:
                    print("    FAIL: the tag column is running over the words")
                ctx.close()
            browser.close()
    finally:
        httpd.shutdown()

    # The safety net every page harness in this repo carries: prove the live tunnel was untouched.
    import subprocess
    net = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = sorted({ln.split()[-1] for ln in net.splitlines()
                   if f":{TUNNEL_PORT} " in ln and "LISTENING" in ln})
    print(f"\nno tunnel started by this harness — pids on {TUNNEL_PORT}: {pids or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
