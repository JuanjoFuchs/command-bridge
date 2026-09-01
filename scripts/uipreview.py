"""Render the proposed UI revamp as THREE side-by-side phone frames, semi-interactive, and shoot it.

    python scripts/uipreview.py [--out DIR] [--no-shot]

WHAT THIS IS FOR. JJ dictated a revamp on 2026-08-24 (verbatim in Voice Tunnel) and asked the
right question before anyone built it: *"Is there any way you can build that without functionality
in a different like in a preview HTML so that I can see it? … Or do you think does it make sense to
invoke codex for image generation of this UI?"*

**Preview HTML, and the reason is his own rule from the blog hero** — *"instead of using AI
generation for the image, I want us to plot it or to diagram it using tooling."* A generated picture
of a UI asserts a layout nobody measured: it cannot be checked at 412px, it has no 44px touch
targets, and it cannot become the page. This can, on all three counts.

Then, on seeing it: *"Can you show me the options within like a phone layout? So arrangement A, like
the full UI on the left within a phone case and arrangement B within a phone case. And can you make
like the UI semi-interactive so that the clicks work and all of that?"* — hence the frames and the
click wiring.

**ARRANGEMENT C IS HIS, and it is the one that changes the design rather than the layout:**
*"now that we have multiple lanes and I can talk to multiple agents, I think having a single orb in
the middle makes no sense… gives each lane its own orb with the same functionality and same
behaviors that the current middle orb has. For example, if I am addressing the Atlas agent, I would,
whenever I speak, that's the orb that will pulse with my speech."*

⚠ **THE STYLESHEET IS LIFTED FROM `index.html`, NOT COPIED.** A preview with its own CSS drifts from
the page within a day and then flatters it — which would make this exactly the generated picture it
exists to avoid. Only the DELTA is written here.

STARTS NO VOICE-TUNNEL SERVER and proves it, like `devicepills.py`, `lanestrip.py` and
`transcriptshot.py` — his daily driver is usually live on 8765 while this runs.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "command_bridge" / "web" / "index.html"
TUNNEL_PORT = 8765

# 🔴 COLOUR IS IDENTITY, NEVER STATUS — his ruling, 2026-08-24: *"we shouldn't make the colors
# represent the statuses, because the statuses are represented within the orb."* One hue per lane,
# so an agent is recognisable at a glance in the orb row AND in the transcript: *"that way it's
# easier to recognize them in the transcript as well."*
#
# ⚠ **He keeps the warm.** *"I have orange, right? And the first agent has this blue."* That also
# settles the clash flagged a minute earlier — warm meant "you" everywhere on the page and was
# being borrowed for "waiting", which is now a raised hand instead of a colour.
#
# Hues picked to stay legible on the page's light ground and to survive the three common colour
# blindnesses by differing in LIGHTNESS as well as hue — blue, green and violet at 4.5:1+ on
# #f2f2ef. The status word inside the orb is what actually carries state, so colour never has to.
LANES = [
    ("claude", "listening", "#3559bd", 0),
    ("codex", "thinking", "#0f7a63", 0),
    ("atlas", "speaking", "#8a3fa8", 3),
]

DELTA = """
/* === REVAMP DELTA — the only rules this preview adds ============================= */

body { display:block !important; background:#0b0d10; padding:1.2rem; }
.rv-rail { display:flex; gap:1.4rem; align-items:flex-start; justify-content:center;
           flex-wrap:wrap; }

/* A PHONE CASE, so each option is judged at the width it will actually live at. He asked for
   this outright after seeing the first render stacked: "show me the options within like a phone
   layout... within a phone case." */
.rv-phone { width:412px; flex:0 0 412px; }
.rv-phone > .cap { text-align:center; font-family:var(--mono); font-size:.6rem;
                   letter-spacing:.14em; text-transform:uppercase; color:#8b949e;
                   padding:0 0 .5rem; }
.rv-case {
  border:10px solid #23282f; border-radius:34px; overflow:hidden;
  height:760px; display:flex; flex-direction:column;
  background:var(--bg); box-shadow:0 12px 40px rgba(0,0,0,.5);
}

/* ONE HEADER ROW: title left, every control right. Buys back a full row of phone screen, which
   is the through-line of every layout request he has made since 2026-08-07. */
.rv-head { display:flex; align-items:center; gap:.6rem; padding:.5rem .8rem; }
.rv-head h1 { margin:0; font-size:.85rem; letter-spacing:.02em; }
.rv-head .rv-controls { margin-left:auto; display:flex; align-items:center; gap:.4rem;
                        position:relative; }

/* MUTE FOLDED INTO THE PICKER. "just the microphone button and that mutes and it has the arrow
   next to it that expands the drop-down. I wouldn't like to display the full text of the
   selected value." So: icon + caret, no label text, 44px tall. */
.rv-dev { display:inline-flex; align-items:center; gap:.1rem; min-height:44px; padding:0 .4rem;
          border:1px solid var(--edge-2); border-radius:12px; background:transparent;
          color:var(--fg); cursor:pointer; }
.rv-dev svg { width:19px; height:19px; fill:none; stroke:currentColor; stroke-width:1.7;
              stroke-linecap:round; stroke-linejoin:round; }
.rv-dev .caret { width:13px; height:13px; opacity:.6; }
.rv-dev[data-off="true"] { color:var(--warm); border-color:var(--warm); }
.rv-dev[data-off="true"] .slash { opacity:1; }
.rv-dev .slash { opacity:0; }

/* The power button is the session, so it reads as ON by default and goes warm when off — the
   one control on this row whose OFF state is the notable one. */
.rv-power { margin-right:.1rem; color:var(--cool); border-color:color-mix(in srgb,var(--cool) 45%,transparent); }
.rv-power[data-off="true"] { color:var(--faint); border-color:var(--edge-2); }
.rv-case[data-off="true"] .rv-lane .orb { filter:grayscale(1) brightness(.7); }
.rv-case[data-off="true"] .rv-lane.live .orb { animation:none;
  border-color:var(--edge-2); color:var(--faint); box-shadow:none; }

.rv-menu { position:absolute; top:52px; right:0; z-index:9; min-width:190px;
           background:var(--bg-deep); border:1px solid var(--edge-2); border-radius:12px;
           padding:.3rem; display:none; box-shadow:0 8px 24px rgba(0,0,0,.5); }
.rv-menu[data-open="true"] { display:block; }
.rv-menu button { display:block; width:100%; text-align:left; background:transparent;
                  border:0; color:var(--fg); font-size:.78rem; padding:.5rem .6rem;
                  border-radius:8px; cursor:pointer; min-height:40px; }
.rv-menu button[aria-checked="true"] { color:var(--cool); }
.rv-menu button:hover { background:var(--edge); }

/* --- A and B: one big orb with satellites tracing it --------------------------- */
.rv-stage { position:relative; display:grid; place-items:center; padding:1.2rem 0 3rem; }
.rv-orb { width:148px; height:148px; border-radius:50%;
          background:radial-gradient(circle at 38% 32%, #2a3340, #12161c 70%);
          border:1px solid var(--edge-2); display:grid; place-items:center;
          color:var(--dim); font-size:.78rem; }
.rv-sat { position:absolute; display:flex; flex-direction:column; align-items:center; gap:.15rem;
          cursor:pointer; }
.rv-sat .mini { width:38px; height:38px; border-radius:50%;
                background:radial-gradient(circle at 38% 32%, #2b3441, #141920 70%);
                border:1px solid var(--edge-2); display:grid; place-items:center; }
.rv-sat .mini i { width:7px; height:7px; border-radius:50%; background:var(--dim); }
.rv-sat.live .mini { border-color:var(--cool);
                     box-shadow:0 0 10px color-mix(in srgb,var(--cool) 55%,transparent); }
.rv-sat.live .mini i { background:var(--cool); }
.rv-sat.waiting .mini { border-color:var(--warm);
                        box-shadow:0 0 10px color-mix(in srgb,var(--warm) 55%,transparent); }
.rv-sat.waiting .mini i { background:var(--warm); }
.rv-sat .nm { font-family:var(--mono); font-size:.5rem; letter-spacing:.12em;
              text-transform:uppercase; color:var(--faint); white-space:nowrap; }
.rv-sat.live .nm { color:var(--cool); }
.rv-sat.waiting .nm { color:var(--warm); }
.rv-sat .st { font-family:var(--mono); font-size:.45rem; letter-spacing:.08em;
              color:var(--faint); white-space:nowrap; }

/* --- C: NO CENTRE ORB. One orb per lane, its own colour, status inside ---------- */
/* His idea, and the only one of the three that changes what the page MEANS: with several agents
   a single orb has no referent — it would have to show one agent's state while he talks to
   another. N orbs makes "which agent is this" structural instead of a label. */
.rv-grid { display:flex; align-items:flex-start; justify-content:center; gap:.5rem;
           padding:1.3rem .5rem 1.5rem; }
.rv-lane { flex:1 1 0; display:flex; flex-direction:column; align-items:center; gap:.3rem;
           cursor:pointer; min-width:0; }
.rv-lane .orbwrap { position:relative; }
.rv-lane .orb { width:88px; height:88px; border-radius:50%;
                background:radial-gradient(circle at 38% 32%, #2a3340, #12161c 70%);
                border:1px solid var(--edge-2); display:grid; place-items:center;
                transition:transform .18s ease; }
/* THE STATUS LIVES INSIDE THE ORB, in words. "the statuses are represented within the orb...
   make sure the orbs show their statuses, and it says speaking or thinking or whatever." */
.rv-lane .orb .stat { font-family:var(--mono); font-size:.46rem; letter-spacing:.08em;
                      text-transform:lowercase; color:#9aa4b0; padding:0 .3rem;
                      text-align:center; }
/* IDENTITY, not state: the ring is the lane's own hue at all times. Being LIVE changes the
   ring's WEIGHT and adds the pulse — never its colour, which is the whole point of the ruling. */
.rv-lane .orb { border-color:color-mix(in srgb, var(--lane-c) 55%, var(--edge-2)); }
.rv-lane.live .orb { border-color:var(--lane-c);
                     box-shadow:0 0 0 3px color-mix(in srgb,var(--lane-c) 24%,transparent),
                                0 0 26px color-mix(in srgb,var(--lane-c) 45%,transparent);
                     animation:rvpulse 1.5s ease-in-out infinite; }
.rv-lane.live .orb .stat { color:color-mix(in srgb, var(--lane-c) 70%, #e8edf3); }
.rv-lane .nm { font-family:var(--mono); font-size:.56rem; letter-spacing:.12em;
               text-transform:uppercase; color:var(--lane-c); opacity:.62; }
.rv-lane.live .nm { opacity:1; }

/* A RAISED HAND WITH A COUNT, replacing the words "wants you". His words: "instead of codex
   wants you, I would like to have a count or a raised hand, with the count of messages that are
   waiting for me." A badge is glanceable at orb size in a way a two-word label is not, and it
   carries HOW MANY, which the label never did. */
.rv-lane .hand { position:absolute; top:-4px; right:-6px;
                 min-width:26px; height:26px; padding:0 .25rem; border-radius:13px;
                 background:var(--bg-deep); border:1px solid var(--lane-c);
                 color:var(--lane-c);
                 display:inline-flex; align-items:center; justify-content:center; gap:.12rem;
                 font-family:var(--mono); font-size:.6rem; font-weight:700;
                 box-shadow:0 0 12px color-mix(in srgb,var(--lane-c) 40%,transparent); }
.rv-lane .hand .em { font-size:.72rem; line-height:1; }
.rv-lane:not(.waiting) .hand { display:none; }
@keyframes rvpulse {
  0%,100% { transform:scale(1); }
  50%     { transform:scale(1.06); }
}
@media (prefers-reduced-motion:reduce) { .rv-lane.live .orb { animation:none; } }

.rv-log { flex:1; overflow:auto; }

/* 🔴 THE COLLAPSE BAND, because the BUILD has one and a preview that omits it is not comparable.
   *"I want arrangement C to have the transcript collapsing section."* (2026-08-25)
   It costs a row of the phone, which is exactly the kind of thing a layout is being judged on —
   leaving it out of the drawing made arrangement C look like it bought more transcript than it
   actually does. Same glyph and same weight as `#collapse` on the page. */
.rv-collapse {
  display:flex; align-items:center; justify-content:center; gap:.35rem;
  min-height:34px; padding:0;
  background:transparent; border:0; cursor:pointer;
  font-family:var(--mono); font-size:.58rem; letter-spacing:.16em;
  text-transform:uppercase; color:var(--faint);
}
.rv-collapse svg { width:13px; height:13px; fill:none; stroke:currentColor; stroke-width:2;
                   stroke-linecap:round; stroke-linejoin:round; }
"""

MIC = ('<svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="10.5" rx="3"/>'
       '<path d="M5.5 11.5a6.5 6.5 0 0 0 13 0"/><path d="M12 18v2.6"/>'
       '<path class="slash" d="M4.8 19.2 19.2 4.8"/></svg>')
SPK = ('<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4z"/>'
       '<path d="M17 9.5a4 4 0 0 1 0 5"/><path class="slash" d="M4.8 19.2 19.2 4.8"/></svg>')
VRB = '<svg viewBox="0 0 24 24"><path d="M4 7h16"/><path d="M4 12h11"/><path d="M4 17h7"/></svg>'
CARET = '<svg class="caret" viewBox="0 0 24 24"><path d="M7 10.5 12 15l5-4.5"/></svg>'

DEVICES = ["Jabra Evolve2 65", "Headset Earphone", "Speakers (Realtek)"]


def menu(kind: str) -> str:
    items = "".join(
        f'<button role="menuitemradio" aria-checked="{str(i == 0).lower()}" '
        f'data-pick="{kind}">{d}</button>' for i, d in enumerate(DEVICES))
    return f'<div class="rv-menu" data-menu="{kind}" role="menu">{items}</div>'


POWER = ('<svg viewBox="0 0 24 24"><path d="M12 3.5v8"/>'
         '<path d="M7.2 6.4a7.5 7.5 0 1 0 9.6 0"/></svg>')


def head() -> str:
    # THE POWER BUTTON, LEFT OF THE TITLE (his ruling, 2026-08-24). With one orb it could carry
    # two jobs — is the session live, and which agent is this — because there was only ever one
    # agent. Arrangement C makes that impossible: N orbs cannot each mean "the session". So the
    # session-level job moves out to its own control and the orbs mean exactly one thing.
    #
    # *"the behavior right now that I have that tapping the orb turns it off and turns it on…
    # Maybe we add that button as a power button right to the left of the voice tunnel title."*
    # *"And that means that the orbs, each orb for each agent or each lane is just clicking on it,
    # is just switching to that lane."*
    return f"""
      <div class="rv-head">
        <button class="rv-dev rv-power" data-tog="pwr" aria-label="Session on or off"
                aria-pressed="true">{POWER}</button>
        <h1>voice&#8209;tunnel</h1>
        <div class="rv-controls">
          <button class="rv-dev" data-tog="mic" aria-label="Microphone">{MIC}{CARET}</button>
          <button class="rv-dev" data-tog="spk" aria-label="Speaker">{SPK}{CARET}</button>
          <button class="rv-dev" data-tog="vrb" aria-label="Verbose">{VRB}</button>
          {menu('mic')}{menu('spk')}
        </div>
      </div>"""


def satellites(arrangement: str) -> str:
    if arrangement == "arc":
        spots = ["left:calc(50% - 138px); top:40px",
                 "right:calc(50% - 138px); top:40px",
                 "right:calc(50% - 106px); top:132px"]
    else:
        spots = ["left:calc(50% - 130px); top:22px",
                 "left:calc(50% - 130px); top:98px",
                 "right:calc(50% - 130px); top:60px"]
    cls = ["live", "", "waiting"]
    return "\n        ".join(
        f'<div class="rv-sat {c}" data-lane="{n}" style="{s}">'
        f'<span class="mini"><i></i></span><span class="nm">{n}</span>'
        f'<span class="st">{st}</span></div>'
        for (n, st, _hue, _w), c, s in zip(LANES, cls, spots))


COLLAPSE = ('<button class="rv-collapse">transcript'
            '<svg viewBox="0 0 24 24"><path d="M7 10.5 12 15l5-4.5"/></svg></button>')


def log(coloured: bool) -> str:
    """The transcript, optionally carrying each lane's own hue.

    ⚠ **Only arrangement C gets the colours**, so the preview shows what the choice actually buys:
    *"that way it's easier to recognize them in the transcript as well."* A and B keep today's
    single-colour rows, which is the honest comparison — the hues are part of C's proposal, not a
    free improvement applied everywhere to flatter it.
    """
    hue = {n: c for n, _s, c, _w in LANES}

    def tag(text: str, lane: str) -> str:
        style = f' style="color:{hue[lane]}"' if coloured else ""
        return f'<span class="tag"{style}>{text}</span>'

    return f"""
      <div class="rv-log" id="log" role="log">
        <div class="row addressed">{tag("you → codex", "codex")}<span class="text">Hey Codex, run the tests. <span class="tick" data-state="read">✓</span></span></div>
        <div class="row claude">{tag("codex", "codex")}<span class="text">Running them now.</span></div>
        <div class="row claude">{tag("atlas", "atlas")}<span class="text">I have three things for you.</span></div>
        <div class="row addressed">{tag("you → claude", "claude")}<span class="text">One more thing. <span class="tick" data-state="sent">✓</span></span></div>
      </div>"""


def frame(cap: str, body: str) -> str:
    return f'<div class="rv-phone"><div class="cap">{cap}</div><div class="rv-case">{body}</div></div>'


SCRIPT = """
<script>
(() => {
  // SEMI-INTERACTIVE ONLY, and deliberately so: taps and menus work because he asked to click
  // through it, and nothing here talks to a server. It is a layout to judge, not a build.
  document.querySelectorAll('.rv-case').forEach((root) => {
    root.addEventListener('click', (ev) => {
      const tog = ev.target.closest('.rv-dev');
      const pick = ev.target.closest('[data-pick]');
      const lane = ev.target.closest('[data-lane]');

      if (pick) {
        const m = pick.closest('.rv-menu');
        m.querySelectorAll('button').forEach((b) => b.setAttribute('aria-checked', 'false'));
        pick.setAttribute('aria-checked', 'true');
        m.dataset.open = 'false';
        return;
      }
      if (tog) {
        const kind = tog.dataset.tog;
        const menu = root.querySelector(`.rv-menu[data-menu="${kind}"]`);
        // The caret half opens the list; the icon half toggles. One control, two targets —
        // which is the part of his sketch that most needs trying by thumb rather than by eye.
        const onCaret = ev.target.closest('.caret');
        if (onCaret && menu) {
          root.querySelectorAll('.rv-menu').forEach((m) => {
            if (m !== menu) m.dataset.open = 'false';
          });
          menu.dataset.open = menu.dataset.open === 'true' ? 'false' : 'true';
        } else {
          tog.dataset.off = tog.dataset.off === 'true' ? 'false' : 'true';
          // The power button owns the whole session, so its off state dims every orb — the thing
          // tapping the single orb used to do, now said once for all of them.
          if (kind === 'pwr') {
            root.dataset.off = tog.dataset.off;
            tog.setAttribute('aria-pressed', tog.dataset.off === 'true' ? 'false' : 'true');
          }
        }
        return;
      }
      root.querySelectorAll('.rv-menu').forEach((m) => (m.dataset.open = 'false'));
      if (lane) {
        const group = lane.classList.contains('rv-lane') ? '.rv-lane' : '.rv-sat';
        root.querySelectorAll(group).forEach((el) => el.classList.remove('live'));
        lane.classList.add('live');
        lane.classList.remove('waiting');
      }
    });
  });
})();
</script>"""


def build(style: str) -> str:
    a = frame("A — tracing the circle",
              head() + f'<div class="rv-stage"><div class="rv-orb">Listening</div>{satellites("arc")}</div>'
              + log(False))
    b = frame("B — a column each side",
              head() + f'<div class="rv-stage"><div class="rv-orb">Listening</div>{satellites("column")}</div>'
              + log(False))

    cls = ["live", "", "waiting"]
    lanes = "".join(
        f'<div class="rv-lane {c}" data-lane="{n}" style="--lane-c:{hue}">'
        f'<div class="orbwrap">'
        f'<div class="orb"><span class="stat">{st}</span></div>'
        f'<span class="hand"><span class="em">&#9995;</span>{wait or ""}</span>'
        f'</div>'
        f'<span class="nm">{n}</span></div>'
        for (n, st, hue, wait), c in zip(LANES, cls))
    c = frame("C — no centre orb, one per lane",
              head() + f'<div class="rv-grid">{lanes}</div>' + COLLAPSE + log(True))

    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>voice-tunnel UI revamp — three arrangements</title>"
            f"<style>{style}{DELTA}</style></head><body>"
            f'<div class="rv-rail">{a}{b}{c}</div>{SCRIPT}</body></html>')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "sessions" / "shots"))
    ap.add_argument("--no-shot", action="store_true")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    src = PAGE.read_text(encoding="utf-8")
    m = re.search(r"<style>(.*?)</style>", src, re.S)
    if not m:
        print("could not find the page's <style> block — refusing to invent one")
        return 1
    style = m.group(1)
    print(f"lifted {len(style)} chars of the REAL stylesheet from {PAGE.name}")

    html = out / "preview-revamp.html"
    html.write_text(build(style), encoding="utf-8")
    print(f"wrote {html}")
    if args.no_shot:
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed — the HTML is written; open it by hand")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 950}, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(html.as_uri(), wait_until="load")
        page.wait_for_timeout(300)
        shot = out / "preview-revamp.png"
        page.screenshot(path=str(shot), full_page=True)

        # MEASURED, not just drawn — the whole reason this is HTML and not a picture.
        small = page.evaluate("""() => {
          const bad = [];
          document.querySelectorAll('.rv-dev, .rv-menu button').forEach((b) => {
            const r = b.getBoundingClientRect();
            if (r.width && r.height && r.height < 44) {
              bad.push({ label: b.getAttribute('aria-label') || b.textContent.trim(),
                         h: Math.round(r.height) });
            }
          });
          return bad;
        }""")
        # 🔴 THE CHECK THE FIRST RENDER DID NOT HAVE. A satellite's label escaping its frame
        # overlaps whatever is below it; that breaks no touch target and causes no horizontal
        # overflow, so both earlier measurements reported success against a visibly wrong picture.
        escapes = page.evaluate("""() => {
          const bad = [];
          document.querySelectorAll('.rv-case').forEach((box, i) => {
            const b = box.getBoundingClientRect();
            box.querySelectorAll('.rv-sat, .rv-orb, .rv-lane').forEach((el) => {
              const r = el.getBoundingClientRect();
              if (r.right > b.right + 0.5 || r.left < b.left - 0.5 || r.bottom > b.bottom + 0.5) {
                bad.push({ frame: i, el: el.className.trim() });
              }
            });
          });
          return bad;
        }""")
        overlaps = page.evaluate("""() => {
          const bad = [];
          document.querySelectorAll('.rv-case').forEach((box, i) => {
            const els = [...box.querySelectorAll('.rv-sat, .rv-lane')];
            for (let a = 0; a < els.length; a++)
              for (let c = a + 1; c < els.length; c++) {
                const p = els[a].getBoundingClientRect(), q = els[c].getBoundingClientRect();
                if (p.left < q.right && q.left < p.right && p.top < q.bottom && q.top < p.bottom)
                  bad.push({ frame: i, pair: [els[a].dataset.lane, els[c].dataset.lane] });
              }
          });
          return bad;
        }""")
        # The interactivity he asked for, asserted rather than assumed.
        page.locator('.rv-case').first.locator('.rv-dev[data-tog="mic"] .caret').click()
        menu_open = page.evaluate(
            "() => document.querySelector('.rv-menu[data-menu=\\'mic\\']').dataset.open === 'true'")
        page.locator('.rv-case').nth(2).locator('.rv-lane[data-lane="atlas"]').click()
        c_live = page.evaluate(
            "() => [...document.querySelectorAll('.rv-case')][2]"
            ".querySelector('.rv-lane.live').dataset.lane")
        # The split he ruled: the power button owns the session, the orbs own the lane. Asserted
        # in both directions, because the failure mode is one control quietly doing both again.
        page.locator('.rv-case').nth(2).locator('.rv-power').click()
        c_off = page.evaluate(
            "() => [...document.querySelectorAll('.rv-case')][2].dataset.off === 'true'")
        page.locator('.rv-case').nth(2).locator('.rv-lane[data-lane="codex"]').click()
        still_off = page.evaluate(
            "() => [...document.querySelectorAll('.rv-case')][2].dataset.off === 'true'")
        lane_after = page.evaluate(
            "() => [...document.querySelectorAll('.rv-case')][2]"
            ".querySelector('.rv-lane.live').dataset.lane")
        # RESET TO THE RESTING STATE BEFORE THE FINAL CAPTURE. The assertions above leave menus
        # open and lanes switched, and a still of a half-interacted page misreads as the design —
        # frame A looked broken in one shot purely because a dropdown was sitting over its orb.
        page.evaluate("""() => {
          document.querySelectorAll('.rv-menu').forEach((m) => (m.dataset.open = 'false'));
          document.querySelectorAll('.rv-case').forEach((c) => (c.dataset.off = 'false'));
          document.querySelectorAll('.rv-power').forEach((p) => (p.dataset.off = 'false'));
          document.querySelectorAll('.rv-case').forEach((c) => {
            const lanes = [...c.querySelectorAll('.rv-lane, .rv-sat')];
            lanes.forEach((el) => el.classList.remove('live'));
            const first = lanes.find((el) => el.dataset.lane === 'claude');
            if (first) first.classList.add('live');
            // RESTORE `waiting` TOO. Tapping a lane clears it (that is the real behaviour — you
            // heard them), so the assertions above stripped atlas's raised hand and the shot went
            // out without the badge it exists to show. A reset that restores only SOME of the
            // state is how a screenshot ends up disagreeing with the design it illustrates.
            const atlas = lanes.find((el) => el.dataset.lane === 'atlas');
            if (atlas) atlas.classList.add('waiting');
          });
        }""")
        page.wait_for_timeout(200)
        page.screenshot(path=str(shot), full_page=True)

        # ASSERTED AFTER THE RESET, because the shot is the artifact. The raised hand and its
        # count are the whole of his "instead of codex wants you" request; a preview that renders
        # them only until something is clicked is not showing him the design.
        hand = page.evaluate("""() => {
          const c = [...document.querySelectorAll('.rv-case')][2];
          const el = c.querySelector('.rv-lane.waiting .hand');
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return { text: el.textContent.trim(), visible: r.width > 0 && r.height > 0,
                   lane: el.closest('.rv-lane').dataset.lane };
        }""")
        hues = page.evaluate("""() => {
          const c = [...document.querySelectorAll('.rv-case')][2];
          const out = {};
          c.querySelectorAll('.rv-lane').forEach((l) => {
            out[l.dataset.lane] = getComputedStyle(l.querySelector('.nm')).color;
          });
          return out;
        }""")
        stats = page.evaluate("""() => {
          const c = [...document.querySelectorAll('.rv-case')][2];
          return [...c.querySelectorAll('.rv-lane .orb .stat')].map((s) => s.textContent.trim());
        }""")

        print(f"\n=== three frames -> {shot}")
        print(f"    touch targets under 44px: {small if small else 'none'}")
        print(f"    elements escaping their phone case: {escapes if escapes else 'none'}")
        print(f"    lanes overlapping each other: {overlaps if overlaps else 'none'}")
        print(f"    dropdown opens on the caret: {menu_open}")
        print(f"    tapping a lane in C makes it live: {c_live}")
        print(f"    C: raised hand in the shot: {hand}")
        print(f"    C: status words inside the orbs: {stats}")
        print(f"    C: one hue per lane: {hues}")
        print(f"    C: hues are all distinct: {len(set(hues.values())) == len(hues)}")
        print(f"    the power button turns the session off: {c_off}")
        print(f"    ...and tapping a lane while off does NOT turn it back on: {still_off} "
              f"(lane switched to {lane_after})")
        ctx.close()
        browser.close()

    net = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = sorted({ln.split()[-1] for ln in net.splitlines()
                   if f":{TUNNEL_PORT} " in ln and "LISTENING" in ln})
    print(f"\nno tunnel started by this harness — pids on {TUNNEL_PORT}: {pids or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
