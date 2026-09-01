"""Diff the DESIGN (arrangement C in the preview) against the BUILD (the live page), by measuring both.

    python scripts/uidiff.py [--out DIR] [--no-shot]

WHY THIS EXISTS, in his words (2026-08-25): *"So I want you to div [diff] arrangement C against the
live live page. Want you to take screenshots of both and you tell me what's different."*

**And the reason it is a MEASUREMENT rather than me reading two files.** 2026-08-24 produced five
defects in a row that he found by looking at the page while every harness stayed green, and the
lesson written into `scripts/uisim.py` was that a golden over a pure model cannot see a lying view.
The same trap is open here in a new place: *comparing a design to a build by reading their source*
is exactly the pure-model check again. So both sides are rendered, both are probed with the SAME
extractor, and the difference is whatever the two probes disagree about.

⚠ **THE PREVIEW IS REBUILT FIRST, NOT READ FROM DISK.** `uipreview.py` lifts its stylesheet from
`index.html`, so a stale `preview-revamp.html` on disk is a picture of an older page and would make
this diff report differences that no longer exist — or hide ones that do.

STARTS NO VOICE-TUNNEL SERVER and proves it, like `uisim.py` and `uipreview.py` — his daily driver
is usually live on 8765 while this runs.
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
SERVED: list[int] = []

PHONE = {"width": 412, "height": 915}

# The state arrangement C is drawn in, so the live page is asked the same question. Taken from
# `uipreview.LANES` + its `cls` row rather than invented here: claude live and listening, codex
# thinking, atlas speaking with three held clips.
LANES = ["claude", "codex", "atlas"]
LIVE = "claude"
STATES = {"claude": "listening", "codex": "thinking", "atlas": "speaking"}
HELD = ("atlas", 3)

# The four transcript rows the preview shows, in order, so tag colour and tick state are compared
# on the same content.
ROWS = [
    ("you", "codex", 10, "Hey Codex, run the tests."),
    ("agent", "codex", None, "Running them now."),
    ("agent", "atlas", None, "I have three things for you."),
    ("you", "claude", 40, "One more thing."),
]

# --------------------------------------------------------------------- the shared extractor
#
# ONE function, two pages. If the design and the build were probed by two different readers, the
# reader is where the difference could hide — which is the whole failure mode this file exists to
# avoid.
PROBE = r"""
(cfg) => {
  const root = document.querySelector(cfg.root);
  if (!root) return { error: "root not found: " + cfg.root };
  const q  = (s) => (s ? root.querySelector(s) : null);
  const qa = (s) => (s ? [...root.querySelectorAll(s)] : []);
  const rect = (e) => (e ? e.getBoundingClientRect() : null);
  const h = (e) => { const r = rect(e); return r ? Math.round(r.height) : 0; };
  const w = (e) => { const r = rect(e); return r ? Math.round(r.width) : 0; };
  // A zero-height box is the only honest answer to "is it gone" — the `hidden` ATTRIBUTE lies
  // whenever a display rule outranks it, which is the bug that ate 165px of transcript twice.
  const vis = (e) => !!e && h(e) > 0;
  const col = (e) => (e ? getComputedStyle(e).color : null);
  const txt = (e) => (e ? e.textContent.replace(/\s+/g, " ").trim() : null);

  const header = q(cfg.header);
  const title = header ? header.querySelector("h1") : null;
  const power = q(cfg.power);

  // WHAT SITS IN THE HEADER, described by what it IS rather than by what it is called — a
  // <select> and an icon+caret button are the same idea and different objects, and that
  // difference is the one he is most likely to be pointing at.
  const controls = [];
  if (header) {
    header.querySelectorAll("button, select").forEach((el) => {
      if (el === power) return;
      if (!vis(el)) return;
      const isSel = el.tagName.toLowerCase() === "select";
      // A caret is a caret however it is drawn: an inline <svg class="caret"> in the design, a
      // pair of corner gradients on the native select in the build. Asking "is there an element
      // called caret" would have said the build has none and been wrong about what is on screen.
      const bg = getComputedStyle(el).backgroundImage || "";
      const caret = !!el.querySelector('.caret, [class*="caret"]')
                    || (isSel && bg.includes("gradient"));
      // 🔴 WHAT IS PAINTED, NOT WHAT IS SET. `selectedOptions[0].text` is the MODEL — it returns
      // the device name whether or not a single pixel of it reaches the screen, and a probe that
      // reads it would have called a transparent label "shows the full text". The alpha of the
      // computed colour is the screen's answer.
      const painted = isSel
        ? !/rgba\([^)]*,\s*0\s*\)/.test(getComputedStyle(el).color)
        : true;
      controls.push({
        kind: el.tagName.toLowerCase(),
        label: el.getAttribute("aria-label") || el.getAttribute("title") || txt(el) || "",
        shows_text: !painted ? ""
          : (isSel ? (el.selectedOptions.length ? el.selectedOptions[0].text : "") : txt(el)),
        caret,
        h: h(el), w: w(el),
      });
    });
  }

  const lanes = qa(cfg.lane).filter(vis);
  const laneRead = lanes.map((el) => {
    const disc = el.querySelector(cfg.disc);
    return {
      name: txt(el.querySelector(cfg.nm)),
      status: txt(el.querySelector(cfg.stat)),
      // IDENTITY LIVES ON THE RING (his ruling: colour is identity, never status), so the ring's
      // border is the honest place to read a lane's hue from on both sides.
      hue: disc ? getComputedStyle(disc).borderTopColor : null,
      name_colour: col(el.querySelector(cfg.nm)),
      diameter: disc ? Math.round(rect(disc).width) : 0,
      hand: (() => { const x = el.querySelector(cfg.hand); return vis(x) ? txt(x) : null; })(),
      timer: (() => { const x = cfg.timer ? el.querySelector(cfg.timer) : null;
                      return vis(x) ? txt(x) : null; })(),
      live: el.classList.contains("live"),
    };
  });

  const log = q(cfg.log);
  const rows = qa(cfg.row).map((r) => ({
    tag: txt(r.querySelector(".tag")),
    tag_colour: col(r.querySelector(".tag")),
    tick: (() => { const t = r.querySelector(".tick");
                   return t ? (t.dataset.state || txt(t)) : null; })(),
  }));

  return {
    header_present: vis(header),
    header_h: h(header),
    header_order: header
      ? [...header.children].filter(vis).map((e) => e.id || e.tagName.toLowerCase() + "." +
          (e.className || "").toString().trim().split(/\s+/)[0]).join(" | ")
      : null,
    power_present: vis(power),
    power_left_of_title: !!(power && title && rect(power).left < rect(title).left),
    power_pressed: power ? power.getAttribute("aria-pressed") : null,
    controls_in_header: controls.length,
    controls,
    centre_orb_visible: vis(q(cfg.centre)),
    chip_strip_visible: vis(q(cfg.chips)),
    lane_count: lanes.length,
    lanes: laneRead,
    hues_distinct: new Set(laneRead.map((l) => l.hue)).size === laneRead.length,
    hands: laneRead.filter((l) => l.hand).length,
    log_h: h(log),
    log_share: h(log) && h(root) ? Math.round((h(log) / h(root)) * 100) : 0,
    // WHAT IS ABOVE THE TRANSCRIPT, named and measured. A share on its own says the build is
    // tighter or looser; only the stack says which band ate the difference, and that is the
    // question worth answering — the whole layout thread since 2026-08-07 is "give the
    // transcript the rest of the phone".
    stack: (() => {
      const out = [];
      const walk = (el, depth) => {
        [...el.children].forEach((c) => {
          if (!vis(c)) return;
          const tag = c.id || (c.className || "").toString().trim().split(/\s+/)[0]
                      || c.tagName.toLowerCase();
          if (c === log || c.contains(log)) {
            if (c === log) out.push({ band: tag, h: h(c) });
            else walk(c, depth + 1);
          } else if (h(c) > 0) {
            out.push({ band: tag, h: h(c) });
          }
        });
      };
      walk(root, 0);
      return out;
    })(),
    rows,
    tick_states: [...new Set(rows.map((r) => r.tick).filter(Boolean))].sort(),
    tags_coloured: new Set(rows.map((r) => r.tag_colour)).size > 1,
  };
}
"""

DESIGN_CFG = {
    "root": ".rv-rail > .rv-phone:nth-of-type(3) .rv-case",
    "header": ".rv-head", "power": ".rv-power",
    "lane": ".rv-lane", "disc": ".orb", "stat": ".stat", "nm": ".nm", "hand": ".hand",
    "timer": None,
    "centre": ".rv-orb", "chips": None,
    "log": ".rv-log", "row": ".rv-log .row",
}

BUILD_CFG = {
    "root": "body",
    "header": "#head", "power": "#power",
    "lane": "#orbs .laneorb", "disc": ".disc", "stat": ".stat", "nm": ".nm", "hand": ".hand",
    "timer": ".lanetimer",
    "centre": "#orbwrap", "chips": "#lanes",
    "log": "#log", "row": "#log .row",
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


def serve(directory: pathlib.Path):
    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(directory)))
    port = httpd.server_address[1]
    assert port != TUNNEL_PORT, "the OS handed out the tunnel's port; refusing to bind it"
    SERVED.append(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def drive_build(page):
    """Deliver the messages that put the real page in arrangement C's state."""
    page.evaluate("(m) => window.__voiceTunnel.deliver(m)", {
        "type": "ready", "verbose": True, "lanes": LANES, "lane": LIVE,
        "default_lane": LANES[0], "broadcast": "everyone",
        "lane_states": {}, "waiting": {}, "watching_lanes": [LIVE], "lane_consumed": {},
    })
    for who, lane, tid, text in ROWS:
        if who == "you":
            page.evaluate("(m) => window.__voiceTunnel.deliver(m)",
                          {"type": "turn", "id": tid, "text": text, "addressed": True, "lane": lane})
        else:
            page.evaluate("([l, t]) => window.__voiceTunnel.renderAgentRow(l, t)", [lane, text])
    for lane, state in STATES.items():
        page.evaluate("(m) => window.__voiceTunnel.deliver(m)",
                      {"type": "agent_state", "lane": lane, "state": state, "live": lane == LIVE,
                       "watching_lanes": [LIVE], "lane_consumed": {"codex": 10}})
    page.evaluate("(m) => window.__voiceTunnel.deliver(m)",
                  {"type": "lane_waiting", "lane": HELD[0], "waiting": HELD[1]})
    page.evaluate("(m) => window.__voiceTunnel.deliver(m)",
                  {"type": "consumed", "cursor": 10, "pending": 0, "lane_consumed": {"codex": 10}})
    # THE DEVICE PICKERS ONLY EXIST AFTER A PERMISSION GRANT, which headless cannot give: device
    # labels are empty until the microphone is allowed, so `applyPills` keeps both pills hidden and
    # a shot taken here would compare a header that has no device controls in it at all. Planting
    # two labelled options and unhiding them stands in for that grant — it is the same thing the
    # browser does on his machine, done by hand, and without it this diff silently skips the exact
    # row he asked about.
    page.evaluate("""() => {
      const fill = (id, names) => {
        const sel = document.getElementById(id);
        if (!sel) return;
        sel.innerHTML = "";
        names.forEach((n, i) => {
          const o = document.createElement("option");
          o.value = "d" + i; o.textContent = n; sel.appendChild(o);
        });
        const pick = sel.closest(".pick");
        if (pick) pick.hidden = false;
      };
      fill("mic", ["Headset Microphone (Jabra Evolve2 65)", "Microphone Array (Realtek)"]);
      fill("spk", ["Default - Headset Earphone (Jabra Evolve2 65)", "Speakers (Realtek)"]);
    }""")
    # THE SESSION SWITCH IS STATE, NOT DESIGN. The preview draws C with the session on; headless
    # has no microphone, so the page cannot be started for real. Try the button, and if it will
    # not flip, the report says so rather than counting a dimmed orb row as a design difference.
    try:
        page.click("#power", timeout=1500)
        page.wait_for_timeout(200)
    except Exception:
        pass


# --------------------------------------------------------------------- the comparison
#
# Each row names ONE claim, reads it off both probes, and says whether they agree. A claim whose
# two sides are not comparable (a px height against a different case height) is compared as a
# share instead, and one that is state rather than design is marked so it cannot be mistaken for
# a defect.
def compare(design, build):
    def lane_field(p, f):
        return {l["name"].lower(): l[f] for l in p["lanes"] if l.get("name")}

    rows = [
        ("one header row, title + controls", design["header_present"], build["header_present"]),
        ("power button present", design["power_present"], build["power_present"]),
        ("power button LEFT of the title", design["power_left_of_title"], build["power_left_of_title"]),
        ("controls living in the header", design["controls_in_header"], build["controls_in_header"]),
        # HIS ACTUAL COMPLAINT, as one claim. Not "how many controls" — whether any of them prints
        # a device name at the user.
        ("a control printing a device name",
         [c["shows_text"] for c in design["controls"] if c["shows_text"]],
         [c["shows_text"] for c in build["controls"] if c["shows_text"]]),
        ("every device control has a caret",
         all(c["caret"] for c in design["controls"] if c["kind"] == "select" or c["caret"]),
         all(c["caret"] for c in build["controls"] if c["kind"] == "select")),
        ("centre orb gone", not design["centre_orb_visible"], not build["centre_orb_visible"]),
        ("chip strip gone", not design["chip_strip_visible"], not build["chip_strip_visible"]),
        ("one orb per lane", design["lane_count"], build["lane_count"]),
        ("status word inside each orb", lane_field(design, "status"), lane_field(build, "status")),
        ("one hue per lane, all distinct", design["hues_distinct"], build["hues_distinct"]),
        ("lane ring hues", lane_field(design, "hue"), lane_field(build, "hue")),
        ("orb diameter", lane_field(design, "diameter"), lane_field(build, "diameter")),
        ("raised hand, one lane only", design["hands"], build["hands"]),
        ("raised hand carries the count", [l["hand"] for l in design["lanes"] if l["hand"]],
         [l["hand"] for l in build["lanes"] if l["hand"]]),
        ("transcript tags carry lane colour", design["tags_coloured"], build["tags_coloured"]),
        ("tick states in use", design["tick_states"], build["tick_states"]),
        ("transcript share of the screen", str(design["log_share"]) + "%",
         str(build["log_share"]) + "%"),
    ]

    print("\n" + "=" * 78)
    print(f"{'CLAIM':<38} {'DESIGN (C)':<18} {'BUILD (live)':<18}")
    print("=" * 78)
    diffs = []
    for label, d, b in rows:
        same = d == b
        mark = "  " if same else "->"
        ds, bs = str(d), str(b)
        print(f"{mark}{label:<36} {ds[:17]:<18} {bs[:17]:<18}")
        if not same:
            diffs.append((label, d, b))

    print("\n--- the vertical stack, top to bottom " + "-" * 39)
    for name, p in (("DESIGN", design), ("BUILD", build)):
        total = sum(b["h"] for b in p["stack"])
        bands = "  ".join(f"{b['band']}:{b['h']}" for b in p["stack"])
        print(f"  {name:<7} {bands}   (sum {total})")

    print("\n--- the controls, side by side " + "-" * 46)
    for name, p in (("DESIGN", design), ("BUILD", build)):
        print(f"  {name}:")
        for c in p["controls"] or [{"label": "(none in the header)", "kind": "", "caret": "",
                                    "shows_text": "", "h": 0}]:
            print(f"    {c.get('kind',''):<7} {str(c.get('label',''))[:30]:<32}"
                  f" caret={str(c.get('caret','')):<5}"
                  f" text={str(c.get('shows_text') or '')[:22]:<24} h={c.get('h',0)}")

    if diffs:
        print("\n--- WHAT DIFFERS " + "-" * 60)
        for label, d, b in diffs:
            print(f"  * {label}\n      design: {d}\n      build : {b}")
    else:
        print("\n  no differences on any measured claim.")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "sessions" / "shots"))
    ap.add_argument("--no-shot", action="store_true")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Rebuild the preview so its stylesheet is today's, not whatever is on disk.
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "uipreview.py"),
                        "--out", str(out), "--no-shot"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout + r.stderr)
        return 1
    print(r.stdout.strip().splitlines()[0])

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed — cannot render either side")
        return 1

    web_srv, web_port = serve(WEB)
    prev_srv, prev_port = serve(out)

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # --- the DESIGN side: arrangement C, cropped to its own phone case
        ctx = browser.new_context(viewport={"width": 1400, "height": 950}, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{prev_port}/preview-revamp.html", wait_until="load")
        page.wait_for_timeout(300)
        design = page.evaluate(PROBE, DESIGN_CFG)
        if not args.no_shot:
            page.locator(DESIGN_CFG["root"]).screenshot(path=str(out / "diff-design-C.png"))
        ctx.close()

        # --- the BUILD side: the real page, driven into the same state
        ctx = browser.new_context(viewport=PHONE, device_scale_factor=2)
        page = ctx.new_page()
        page.add_init_script(STUB)
        page.goto(f"http://127.0.0.1:{web_port}/?token=diff", wait_until="load")
        page.wait_for_function("() => window.__voiceTunnel && window.__voiceTunnel.deliver",
                               timeout=15000)
        drive_build(page)
        page.wait_for_timeout(400)
        build = page.evaluate(PROBE, BUILD_CFG)
        if not args.no_shot:
            page.screenshot(path=str(out / "diff-build-live.png"), full_page=True)
        ctx.close()

        if design.get("error") or build.get("error"):
            print("probe failed:", design.get("error"), build.get("error"))
            return 1

        diffs = compare(design, build)

        # A composite, so the two are looked at the way they are compared: together.
        if not args.no_shot:
            comp = out / "diff-side-by-side.html"
            comp.write_text(
                "<!doctype html><meta charset='utf-8'>"
                "<style>body{margin:0;background:#0b0d10;font:12px/1.4 ui-monospace,monospace;"
                "color:#8b949e;display:flex;gap:24px;padding:20px;align-items:flex-start}"
                "figure{margin:0}figcaption{padding:0 0 8px;letter-spacing:.14em;"
                "text-transform:uppercase;font-size:10px}"
                "img{display:block;width:412px;border:1px solid #23282f;border-radius:12px}</style>"
                "<figure><figcaption>design &mdash; arrangement C</figcaption>"
                "<img src='diff-design-C.png'></figure>"
                "<figure><figcaption>build &mdash; the live page</figcaption>"
                "<img src='diff-build-live.png'></figure>", encoding="utf-8")
            ctx = browser.new_context(viewport={"width": 940, "height": 1000},
                                      device_scale_factor=2)
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{prev_port}/diff-side-by-side.html", wait_until="load")
            page.wait_for_timeout(250)
            page.screenshot(path=str(out / "diff-side-by-side.png"), full_page=True)
            ctx.close()
            print(f"\nshot {out / 'diff-side-by-side.png'}")

        browser.close()

    web_srv.shutdown()
    prev_srv.shutdown()
    print(f"\nno tunnel started by this harness — pids on {TUNNEL_PORT}: {_listeners()}")
    print(f"served on ephemeral ports {SERVED}")
    return 1 if diffs else 0


def _listeners():
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=20)
    except Exception:
        return "unknown"
    pids = set()
    for line in r.stdout.splitlines():
        if f":{TUNNEL_PORT}" in line and "LISTENING" in line:
            pids.add(line.split()[-1])
    return sorted(pids)


if __name__ == "__main__":
    raise SystemExit(main())
