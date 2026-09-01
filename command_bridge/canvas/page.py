"""The page itself — one HTML document, served whole, no build step.

Everything the browser needs is here: the SSE client, the canvas, the camera,
the pointer, and the self-upgrade check. It is a string rather than a file on
disk so the server has nothing to find at runtime and packaging has nothing to
miss.

`__VERSION__` is replaced at serve time with a hash of this template. A page
running an older hash reloads itself, so changing this file upgrades every open
tab without anyone being told to refresh.
"""

from __future__ import annotations

import hashlib

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>command-bridge</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<!-- `@highlightjs/cdn-assets`, NOT the `highlight.js` npm package: the latter
     ships modules, not a browser bundle, so its /lib/highlight.min.js 404s and
     `hljs` is silently undefined. Two stylesheets, media-gated, so code reads
     in either theme without the page having to know which one it is in. -->
<link rel="stylesheet" media="(prefers-color-scheme: light)"
      href="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/styles/github.min.css">
<link rel="stylesheet" media="(prefers-color-scheme: dark)"
      href="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/styles/github-dark.min.css">
<script src="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11/highlight.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@14/marked.min.js"></script>
<!-- Vega for the chart tier. The spec goes out ONCE; every update after it is a
     changeset of rows, which is the only render tier here whose second update is
     cheaper than its first. The page still works if these fail to load — a chart
     frame degrades to its spec as text rather than taking the surface down. -->
<script src="https://cdn.jsdelivr.net/npm/vega@5/build/vega.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5/build/vega-lite.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6/build/vega-embed.min.js"></script>
<style>
  :root {
    --bg: #fbfaf8; --fg: #24211d; --dim: #8a8378;
    --edge: #e5e0d8; --glow: #d97757; --card: #ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #171513; --fg: #eeeae4; --dim: #8a8378;
            --edge: #302c28; --glow: #e08a68; --card: #1e1c1a; }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: var(--bg); color: var(--fg);
    font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
    display: flex; flex-direction: column;
  }
  header {
    display: flex; align-items: center; gap: .6rem;
    padding: .5rem .9rem; border-bottom: 1px solid var(--edge);
    font-size: 12px; color: var(--dim); flex: none;
  }
  /* EMBED MODE (?embed=1): the meeting page (spec 006) embeds this canvas and already carries a
     header + the participant orbs, so the canvas's own header — the lane chips, the title, the
     frame count — is redundant inside it (JJ, 2026-09-01: 'on the canvas there's no need to show
     the lane names … it's live with the agent that's selected'). Hidden, not removed, so /canvas
     standalone is unchanged. */
  html.embed header { display: none; }
  #dot { width: 7px; height: 7px; border-radius: 50%; background: var(--dim); flex: none; }
  #dot.live { background: #4a9d5f; }
  #count { margin-left: .8rem; }
  #lanes { display: flex; gap: .35rem; margin-left: auto; align-items: center; }
  .lane {
    font: 11px/1 ui-sans-serif, system-ui, sans-serif; letter-spacing: .04em;
    padding: .3rem .55rem; border: 1px solid var(--edge); border-radius: 999px;
    background: transparent; color: var(--dim); cursor: pointer;
  }
  .lane:hover { color: var(--fg); }
  .lane.live { color: var(--fg); border-color: var(--fg); }
  /* A raised hand is the whole point of a background lane: it must be visible
     without being loud enough to interrupt what he is reading. */
  .lane.raised { border-color: var(--glow); color: var(--glow); }
  .lane .hand { margin-left: .3rem; }
  main {
    flex: 1; min-height: 0; overflow: hidden; position: relative;
    cursor: grab; touch-action: none;
    /* Dragging the canvas is a PAN, and it must not also sweep a text
       selection across every card it crosses. Selection stays off for the
       whole surface rather than per card: a drag that starts on a card is
       still a pan, so re-enabling selection inside cards just moves the bug
       to wherever the pointer happened to go down. */
    user-select: none; -webkit-user-select: none;
  }
  main.dragging { cursor: grabbing; }
  /* The camera. One transform on a wrapper, so nothing inside needs to know it
     is being zoomed — which is what keeps `point` working at any scale. */
  #stage {
    position: absolute; top: 0; left: 0;
    transform-origin: 0 0; will-change: transform;
  }
  /* Cards are absolutely positioned in canvas space. The layout pass measures
     them at scale 1 and packs them; nothing here depends on the camera. */
  .card {
    position: absolute;
    background: var(--card); border: 1px solid var(--edge); border-radius: 8px;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
    /* `width: max-content` is load-bearing. An absolutely-positioned box
       shrink-wraps against its containing block, and #stage has no width of its
       own — so without this every card collapsed into a narrow column and the
       camera dutifully zoomed out to fit the wrong shape. */
    width: max-content; max-width: 1400px;
  }
  /* A DIAGRAM sizes its own card. The 1400px cap exists to stop a wall of
     HTML growing without limit, but an SVG ignores it (`max-width: none`
     below), so a wide sequence diagram spilled past its own frame and
     `look` fitted the BOX rather than the drawing — he had to zoom out by
     hand. Whatever the camera is asked to frame has to be what the card
     actually measures. */
  .card[data-kind="mermaid"], .card[data-kind="svg"] { max-width: none; }
  .card > .bar {
    font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--dim); padding: .45rem .8rem; border-bottom: 1px solid var(--edge);
    white-space: nowrap; display: flex; gap: .8rem; justify-content: space-between;
  }
  /* Age sits to the right of the title and stays quieter than it: it answers
     "is this still true", which is a second question, not the headline. */
  .card > .bar > .age { opacity: .62; letter-spacing: .04em; }
  /* `zoom` rather than `transform: scale()` — zoom changes the element's LAYOUT
     box, so the packer and the camera measure the scaled size and everything
     downstream just works. A transform would leave a card claiming its
     unscaled footprint and overlapping its neighbour. */
  .card > .body { padding: 1.1rem; }
  .card svg { max-width: none !important; }
  .card pre { white-space: pre-wrap; font: 13px/1.5 ui-monospace, "Cascadia Code", monospace; }
  /* Markdown. Deliberately narrow: a measure wider than about 70 characters is
     where a rendered document stops being glanceable and becomes the wall of
     text this surface exists to replace. */
  /* 🔴 NOTHING INSIDE A CARD SCROLLS. A long document is simply a tall card,
     and the CANVAS is the scrollbar — that is what the camera is for. A card
     with its own scroll region traps content in a box the camera cannot reach,
     so zooming out shows you a small box with a scrollbar rather than the
     document. `look` handles the tall case by fitting the WIDTH and starting
     at the top, which is reading position rather than an unreadable overview. */
  .md { max-width: 68ch; }
  .md > :first-child { margin-top: 0; }
  .md > :last-child { margin-bottom: 0; }
  .md h1, .md h2, .md h3 { line-height: 1.25; margin: 1.4em 0 .5em; }
  .md h1 { font-size: 1.6em; } .md h2 { font-size: 1.3em; } .md h3 { font-size: 1.1em; }
  .md p, .md ul, .md ol, .md blockquote { margin: 0 0 .9em; }
  .md li { margin: .2em 0; }
  .md code { font: .87em ui-monospace, "Cascadia Code", monospace;
             background: rgba(128,128,128,.13); border-radius: 4px; padding: .1em .35em; }
  /* `pre-wrap`, not `overflow-x` — a code block that scrolls sideways hides
     its own right-hand side from the camera. Wrapping keeps every character
     reachable by zooming. */
  .md pre { background: rgba(128,128,128,.09); border: 1px solid rgba(128,128,128,.22);
            border-radius: 6px; padding: .7rem .9rem; white-space: pre-wrap; }
  .md pre code { background: none; padding: 0; font-size: 13px; }
  .md blockquote { border-left: 3px solid var(--edge); padding-left: .9em;
                   margin-left: 0; opacity: .85; }
  .md table { border-collapse: collapse; margin: 0 0 .9em; }
  .md th, .md td { padding: .3em .8em; border-bottom: 1px solid rgba(128,128,128,.25);
                   text-align: left; }
  .md a { color: inherit; }
  .md hr { border: 0; border-top: 1px solid var(--edge); margin: 1.4em 0; }
  .md img { max-width: 100%; }
  /* Vega paints axis text and lines with its own theme colours, which is the
     same both-themes problem matplotlib has. Force them to inherit the page. */
  .vega-embed { width: max-content; }
  .card .vega-embed svg text { fill: currentColor !important; }
  .card .vega-embed .role-axis line,
  .card .vega-embed .role-axis path { stroke: currentColor !important; opacity: .45; }
  .card .vega-embed summary { display: none; }   /* the actions menu is noise here */
  #empty {
    position: absolute; inset: 0; display: grid; place-items: center;
    color: var(--dim); font-size: 13px; pointer-events: none;
  }
  #zoom {
    position: absolute; right: .7rem; bottom: .6rem;
    font: 11px ui-monospace, "Cascadia Code", monospace;
    color: var(--dim); background: color-mix(in srgb, var(--bg) 82%, transparent);
    border: 1px solid var(--edge); border-radius: 4px; padding: .15rem .4rem;
    pointer-events: none; opacity: 0; transition: opacity .25s;
  }
  #zoom.show { opacity: 1; }

  /* The pointer. A highlighted thing pulses twice and then holds, so a glance
     that arrives late still finds it. */
  .pointed {
    outline: 2px solid var(--glow) !important;
    outline-offset: 3px; border-radius: 3px;
    animation: pulse 1.1s ease-out 2;
  }
  .pointed rect, .pointed polygon, .pointed circle, .pointed path {
    stroke: var(--glow) !important; stroke-width: 2.5px !important;
  }
  /* A sequence STEP is pointed at as three separate elements, and two of them
     are an SVG `line` and a `text`. The rule above only reaches descendants, so
     without these the arrow and the label stayed grey while the number lit —
     which is what "only the number was highlighted" looked like. Text takes a
     fill; a line takes a stroke. */
  line.pointed { stroke: var(--glow) !important; stroke-width: 2.5px !important; }
  text.pointed, .pointed text, .pointed tspan {
    fill: var(--glow) !important; font-weight: 600;
  }
  /* When the thing being pointed at is SEVERAL elements — a sequence step is a
     number, a label and an arrow — outlining each one draws three or four
     separate boxes, which reads as several things rather than one. JJ:
     "I would like just one orange rectangle grouping the number, the arrow,
     the label." So a group gets a single halo over their combined bounds. */
  .halo {
    position: absolute; pointer-events: none;
    border: 2px solid var(--glow); border-radius: 6px;
    animation: pulse 1.1s ease-out 2;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 color-mix(in srgb, var(--glow) 55%, transparent); }
    100% { box-shadow: 0 0 0 14px transparent; }
  }
</style>

<script>if (new URLSearchParams(location.search).has("embed")) document.documentElement.classList.add("embed");</script>
<header>
  <span id="dot"></span><span id="title">waiting for the agent…</span>
  <span id="lanes"></span>
  <span id="count"></span>
</header>
<main id="view">
  <div id="stage"></div>
  <div id="empty">Nothing drawn yet.</div>
  <div id="zoom">100%</div>
</main>

<script>
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: "loose",
    theme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default",
  });

  const stage = document.getElementById("stage");
  const view = document.getElementById("view");
  const dot = document.getElementById("dot");
  const title = document.getElementById("title");
  const countTag = document.getElementById("count");
  const emptyTag = document.getElementById("empty");
  const zoomTag = document.getElementById("zoom");
  const lanesTag = document.getElementById("lanes");

  // One canvas per lane. `cards`/`order` always point at the LIVE lane's, so
  // every function below stays lane-agnostic — switching swaps what they see.
  const canvases = new Map();   // lane -> {cards, order, cam, el}
  const hands = new Map();      // lane -> {count, why}
  const views = new Map();      // frame id -> live vega view, for row streaming
  let live = "main";
  let cards, order;             // bound to the live canvas by `useLane`
  let seq = 0;

  function canvasFor(lane) {
    let c = canvases.get(lane);
    if (!c) {
      const el = document.createElement("div");
      el.className = "canvas";
      el.dataset.lane = lane;
      el.style.display = "none";
      stage.appendChild(el);
      c = { cards: new Map(), order: [], cam: { x: 0, y: 0, k: 1 }, el };
      canvases.set(lane, c);
    }
    return c;
  }

  function useLane(lane) {
    // Park the outgoing lane's camera so switching back returns the view
    // where it was left, rather than resetting it.
    if (live && canvases.has(live)) canvases.get(live).cam = { ...cam };
    live = lane;
    const c = canvasFor(lane);
    for (const [name, other] of canvases) other.el.style.display = name === lane ? "" : "none";
    cards = c.cards; order = c.order;
    cam = { ...c.cam };
    apply(false);
    hands.delete(lane);
    paintLanes();
    layout();
    // The moment that matters for a held clip: this lane just got the floor, so
    // anything armed for it starts now.
    fireArmed(lane);
  }

  function paintLanes() {
    lanesTag.innerHTML = "";
    for (const lane of [...canvases.keys()].sort()) {
      // Same rule the server applies to `status`: an empty lane is a name
      // somebody typed, not a canvas. Clicking a chip is a switch and a switch
      // creates the lane, so browsing the header used to leave a chip behind
      // for every name ever visited. Keep it if it holds something, has the
      // floor, or is asking for it.
      const canvas = canvases.get(lane);
      if (!canvas.cards.size && lane !== live && !hands.has(lane)) continue;
      const b = document.createElement("button");
      b.className = "lane" + (lane === live ? " live" : "") + (hands.has(lane) ? " raised" : "");
      const hand = hands.get(lane);
      b.textContent = lane;
      if (hand) {
        const s = document.createElement("span");
        s.className = "hand";
        s.textContent = hand.count > 1 ? "✋" + hand.count : "✋";
        b.appendChild(s);
        if (hand.why) b.title = hand.why;
      }
      // The HUMAN switches. Nothing else on this page may.
      b.onclick = () => {
        fetch("/switch", { method: "POST",
                           headers: { "Content-Type": "application/json" },
                           body: JSON.stringify({ to: lane }) }).catch(() => {});
        useLane(lane);
      };
      lanesTag.appendChild(b);
    }
  }

  const GAP = 48;            // canvas-space gutter between cards
  const ROW_WIDTH = 2400;    // wrap the packing at this width

  // ---- the camera -------------------------------------------------------
  let cam = { x: 0, y: 0, k: 1 };
  let tagTimer = null;

  function apply(animate) {
    stage.style.transition = animate ? "transform .35s cubic-bezier(.4,0,.2,1)" : "none";
    stage.style.transform = `translate(${cam.x}px, ${cam.y}px) scale(${cam.k})`;
    zoomTag.textContent = Math.round(cam.k * 100) + "%";
    zoomTag.classList.add("show");
    clearTimeout(tagTimer);
    tagTimer = setTimeout(() => zoomTag.classList.remove("show"), 1200);
  }

  function extent() {
    // The union of every card, in canvas space.
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const { el } of cards.values()) {
      const x = el.offsetLeft, y = el.offsetTop;
      x0 = Math.min(x0, x); y0 = Math.min(y0, y);
      x1 = Math.max(x1, x + el.offsetWidth); y1 = Math.max(y1, y + el.offsetHeight);
    }
    if (!isFinite(x0)) return null;
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }

  // Spec 007: a long move ZOOMS OUT, travels, and zooms back in — the way a map
  // does — because a straight interpolation across a big canvas reads as a cut
  // and throws away the spatial relationship, which is the only thing a canvas
  // buys over a list. A short hop stays a direct move; flying out and back for
  // a neighbour is theatre.
  const FLIGHT_MS = 600, HOP_MS = 350;
  let flight = null;

  function cancelFlight() {
    if (flight) { cancelAnimationFrame(flight); flight = null; }
  }

  function flyTo(target) {
    cancelFlight();
    const port = view.getBoundingClientRect();
    const from = { ...cam };
    // Canvas-space centres of both endpoints; the dip is whatever scale shows
    // both at once, and never further out than that.
    const c0 = { x: (port.width / 2 - from.x) / from.k, y: (port.height / 2 - from.y) / from.k };
    const c1 = { x: (port.width / 2 - target.x) / target.k,
                 y: (port.height / 2 - target.y) / target.k };
    const span = Math.hypot(c1.x - c0.x, c1.y - c0.y);
    const both = Math.min(port.width, port.height) / Math.max(span + 1, 1);
    const low = Math.min(from.k, target.k, Math.max(both, 0.05));
    // A hop is a move that is already mostly on screen, or barely a scale change.
    const hop = span * Math.min(from.k, target.k) < Math.min(port.width, port.height);
    if (hop || low >= Math.min(from.k, target.k) * 0.9) {
      cam = { ...target };
      apply(true);
      setTimeout(report, HOP_MS + 40);
      return;
    }
    const t0 = performance.now();
    const ease = t => t < .5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3) / 2;
    const step = now => {
      const t = Math.min(1, (now - t0) / FLIGHT_MS);
      const e = ease(t);
      // Scale dips to `low` at the midpoint, then recovers: out, across, in.
      const dip = Math.sin(Math.PI * t);
      const k = Math.exp(Math.log(from.k) * (1 - e) + Math.log(target.k) * e)
              * Math.pow(low / Math.min(from.k, target.k), dip);
      const cx = c0.x + (c1.x - c0.x) * e, cy = c0.y + (c1.y - c0.y) * e;
      cam = { k, x: port.width / 2 - cx * k, y: port.height / 2 - cy * k };
      stage.style.transition = "none";
      stage.style.transform = `translate(${cam.x}px, ${cam.y}px) scale(${cam.k})`;
      zoomTag.textContent = Math.round(cam.k * 100) + "%";
      zoomTag.classList.add("show");
      if (t < 1) { flight = requestAnimationFrame(step); }
      else { flight = null; clearTimeout(tagTimer);
             tagTimer = setTimeout(() => zoomTag.classList.remove("show"), 1200);
             report(); }
    };
    flight = requestAnimationFrame(step);
  }

  function frameBox(box, animate, margin = 40) {
    // Scale to FILL the viewport, up as well as down. A small drawing sitting
    // at 100% in the middle of a big window is the bug, not the safe default.
    const port = view.getBoundingClientRect();
    if (!box || !box.w || !box.h) return;
    const k = Math.max(0.05, Math.min(
      (port.width - margin) / box.w, (port.height - margin) / box.h, 4));
    cam = {
      k,
      x: port.width / 2 - (box.x + box.w / 2) * k,
      y: port.height / 2 - (box.y + box.h / 2) * k,
    };
    apply(animate);
  }

  function fit(animate) { frameBox(extent(), animate); }

  function boxOf(el) {
    // An element's box in CANVAS space, whatever the camera is doing.
    const b = el.getBoundingClientRect();
    const port = view.getBoundingClientRect();
    return {
      x: (b.left - port.left - cam.x) / cam.k,
      y: (b.top - port.top - cam.y) / cam.k,
      w: b.width / cam.k,
      h: b.height / cam.k,
    };
  }

  // Below this, text is on screen but not readable — so "fits" and "can be
  // read" stop being the same thing and the camera has to choose.
  const READABLE = 0.62;

  function zoomTo(el, margin = 80, fly = false) {
    const box = boxOf(el);
    const port = view.getBoundingClientRect();
    if (!box.w || !box.h) return;
    const whole = Math.min((port.width - margin) / box.w,
                           (port.height - margin) / box.h);
    if (whole >= READABLE) {
      if (fly) { flyTo(camFor(box, margin)); } else { frameBox(box, true, margin); }
      return;
    }
    // Too tall to show whole AND read. Fit the width and park at the top —
    // reading position. He pans down with the canvas, which is the scrollbar.
    const k = Math.min((port.width - margin) / box.w, 4);
    const target = { k, x: port.width / 2 - (box.x + box.w / 2) * k,
                     y: margin / 2 - box.y * k };
    if (fly) flyTo(target); else { cam = target; apply(true); }
  }

  function camFor(box, margin) {
    const port = view.getBoundingClientRect();
    const k = Math.max(0.05, Math.min((port.width - margin) / box.w,
                                      (port.height - margin) / box.h, 4));
    return { k, x: port.width / 2 - (box.x + box.w / 2) * k,
             y: port.height / 2 - (box.y + box.h / 2) * k };
  }

  view.addEventListener("wheel", e => {
    e.preventDefault();
    cancelFlight();                    // the human always wins an in-flight move
    stage.style.transition = "none";
    const port = view.getBoundingClientRect();
    const mx = e.clientX - port.left, my = e.clientY - port.top;
    const k = Math.max(0.05, Math.min(6, cam.k * Math.exp(-e.deltaY * 0.0015)));
    cam.x = mx - (mx - cam.x) * (k / cam.k);
    cam.y = my - (my - cam.y) * (k / cam.k);
    cam.k = k;
    apply(false);
    clearTimeout(window._rt);
    window._rt = setTimeout(report, 250);
  }, { passive: false });

  let drag = null;
  view.addEventListener("pointerdown", e => {
    cancelFlight();
    stage.style.transition = "none";
    drag = { x: e.clientX - cam.x, y: e.clientY - cam.y, moved: false };
    view.setPointerCapture(e.pointerId);
  });
  view.addEventListener("pointermove", e => {
    if (!drag) return;
    // Only claim the gesture once it has actually moved. A click that never
    // travels should still be able to select, focus or follow a link.
    if (!drag.moved) {
      drag.moved = true;
      view.classList.add("dragging");
    }
    // Belt and braces: a selection can still be started before `user-select`
    // is consulted on some paths, and a half-highlighted card during a pan is
    // exactly what he reported.
    getSelection()?.removeAllRanges();
    cam.x = e.clientX - drag.x;
    cam.y = e.clientY - drag.y;
    apply(false);
  });
  const endDrag = () => { drag = null; view.classList.remove("dragging"); report(); };
  view.addEventListener("pointerup", endDrag);
  view.addEventListener("pointercancel", endDrag);
  view.addEventListener("dblclick", () => fit(true));
  // A resize does NOT refit. Where he is looking is his, and a window resize
  // is not a request to go somewhere else — refitting threw away his place
  // every time the window changed size. It also broke `shot --look`: the
  // headless browser fires a resize while sizing itself, which silently
  // overrode the frame that had just been requested, so the capture came
  // back showing the whole canvas while the server reported the look ok.
  addEventListener("resize", () => report());

  // ---- the canvas -------------------------------------------------------

  function layout() {
    // A halo is drawn over WHERE some elements were, in stage coordinates. Any
    // re-layout — a frame replaced, added, removed — moves or destroys those
    // elements and leaves the box floating over nothing. JJ saw exactly that:
    // "a weird orange box around nothing". A highlight outlives its referent
    // for as long as this is not cleared, which is worse than no highlight.
    stage.querySelectorAll(".halo").forEach(n => n.remove());

    // Deterministic shelf packing in insertion order: the same sequence of
    // frames produces the same positions every time. Explicitly-placed cards
    // are honoured and excluded from the packing.
    let x = 0, y = 0, rowH = 0;
    for (const id of order) {
      const c = cards.get(id);
      if (!c) continue;
      if (c.at) {
        c.el.style.left = c.at[0] + "px";
        c.el.style.top = c.at[1] + "px";
        continue;
      }
      const w = c.el.offsetWidth, h = c.el.offsetHeight;
      if (x > 0 && x + w > ROW_WIDTH) { x = 0; y += rowH + GAP; rowH = 0; }
      c.el.style.left = x + "px";
      c.el.style.top = y + "px";
      x += w + GAP;
      rowH = Math.max(rowH, h);
    }
    emptyTag.style.display = cards.size ? "none" : "grid";
    countTag.textContent = cards.size
      ? cards.size + (cards.size === 1 ? " frame" : " frames") : "";
    report();
  }

  function report() {
    // Tell the server where things ended up and what is on screen. The server
    // models no geometry — only the browser knows how big a rendered thing is,
    // and only the browser knows where the camera ended up.
    const port = view.getBoundingClientRect();
    const frames = {}, visible = [], partial = [], offscreen = [];
    for (const [id, { el }] of cards) {
      frames[id] = { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight };
      const b = el.getBoundingClientRect();
      const inside = b.left >= port.left && b.right <= port.right
                  && b.top >= port.top && b.bottom <= port.bottom;
      const touches = b.right > port.left && b.left < port.right
                   && b.bottom > port.top && b.top < port.bottom;
      (inside ? visible : touches ? partial : offscreen).push(id);
    }
    fetch("/placed", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lane: live, frames,
                             viewport: { visible, partial, offscreen, scale: cam.k } }),
    }).catch(() => { /* the surface works fine without the server knowing */ });
  }

  async function paint(body, msg) {
    if (msg.kind === "mermaid") {
      // Render off-document, then swap in. mermaid.render throws on a bad
      // definition and an agent WILL send one — show it rather than blanking
      // the card, because a blank card looks like the tunnel died.
      try {
        const { svg } = await mermaid.render("m" + (++seq), msg.content);
        body.innerHTML = svg;
      } catch (err) {
        body.innerHTML = "<pre></pre>";
        body.firstChild.textContent = String(err && err.message || err);
      }
    } else if (msg.kind === "vega") {
      // Keep the VIEW, not just the markup: `rows` later calls change() on it,
      // which is what makes an update a changeset instead of a re-render.
      if (!window.vegaEmbed) {
        body.innerHTML = "<pre></pre>";
        body.firstChild.textContent = msg.content;
      } else {
        body.innerHTML = "";
        try {
          const spec = typeof msg.content === "string" ? JSON.parse(msg.content) : msg.content;
          // Transparent, and every colour taken from the page: vega defaults to
          // a white plot area, which is invisible-on-white in one theme and a
          // glaring white slab in the other. Same both-themes rule as the
          // matplotlib chrome.
          const res = await vegaEmbed(body, spec, {
            actions: false, renderer: "svg", background: "transparent",
            config: { background: "transparent",
                      view: { stroke: "transparent" },
                      axis: { domainColor: "currentColor", tickColor: "currentColor",
                              gridColor: "currentColor", gridOpacity: 0.12,
                              labelColor: "currentColor", titleColor: "currentColor" },
                      legend: { labelColor: "currentColor", titleColor: "currentColor" },
                      title: { color: "currentColor" } },
          });
          views.set(msg.id || "main", res.view);
        } catch (err) {
          body.innerHTML = "<pre></pre>";
          body.firstChild.textContent = String(err && err.message || err);
        }
      }
    } else if (msg.kind === "markdown") {
      // Rendered client-side for the same reason mermaid is: the agent emits
      // the source it was going to write anyway, and the browser does the
      // layout. Markdown is the cheapest tier that can carry prose.
      if (window.marked) {
        body.innerHTML = '<div class="md">' + marked.parse(msg.content) + "</div>";
      } else {
        body.innerHTML = '<pre class="md"></pre>';
        body.firstChild.textContent = msg.content;
      }
    } else if (msg.kind === "html" || msg.kind === "svg") {
      body.innerHTML = msg.content;
    } else if (msg.kind === "text") {
      body.innerHTML = "<pre></pre>";
      body.firstChild.textContent = msg.content;
    } else {
      body.innerHTML = "";
    }
    unshrink(body);
    highlight(body);
  }

  async function place(msg) {
    const lane = msg.lane || "main";
    const canvas = canvasFor(lane);
    const known = canvas.cards;
    const id = msg.id || "main";
    let c = known.get(id);
    if (!c) {
      const el = document.createElement("div");
      el.className = "card";
      el.dataset.frame = id;
      el.dataset.kind = msg.kind || "";
      el.innerHTML = '<div class="bar"></div><div class="body"></div>';
      canvasFor(msg.lane || live).el.appendChild(el);
      c = { el, body: el.querySelector(".body"), at: null };
      known.set(id, c);
      canvas.order.push(id);
    }
    if (msg.at) c.at = msg.at;
    // Scale is how the canvas carries IMPORTANCE. A frame rendered at 2x is
    // readable at the zoom level where a 1x frame beside it is not, so the
    // summary is what you take in from the overview and the full text is what
    // you lean in for. Zoom level becomes the reading order.
    // Zoom the BODY, not the card. `zoom` reports an element's own offsetWidth
    // in its pre-zoom coordinate space, so zooming the card made the packer
    // measure it unscaled and lay the next frame straight on top of it. Zoom
    // the body and the card's box grows around it honestly.
    c.body.style.zoom = msg.scale && Number(msg.scale) > 0 ? String(msg.scale) : "";
    // The bar carries the frame's AGE beside its name. On a canvas that never
    // forgets, "what am I looking at" and "is this still true" are different
    // questions, and only the first one was answerable before.
    // Set on EVERY render, not just creation: re-placing an id can change
    // the kind, and a stale `data-kind` would cap a diagram's width again.
    c.el.dataset.kind = msg.kind || "";
    const bar = c.el.querySelector(".bar");
    bar.textContent = msg.title || id;
    if (msg.created) {
      c.created = msg.created * 1000;
      const age = document.createElement("span");
      age.className = "age";
      bar.appendChild(age);
    }
    paintAges();
    // A background lane must be invisible until it raises a hand — so it does
    // not touch the title, the count, or the camera.
    if (lane === live) {
      title.textContent = msg.title || id;
      requestAnimationFrame(() => layout());
    }
    await paint(c.body, msg);
    if (lane === live) requestAnimationFrame(() => layout());
    paintLanes();
  }

  function drop(id, lane) {
    const canvas = canvasFor(lane || live);
    const c = canvas.cards.get(id);
    if (!c) return;
    c.el.remove();
    canvas.cards.delete(id);
    const i = canvas.order.indexOf(id);
    if (i >= 0) canvas.order.splice(i, 1);
    if ((lane || live) === live) layout();
  }

  function clearAll(lane) {
    const name = lane || live;
    const canvas = canvasFor(name);
    canvas.el.innerHTML = "";
    canvas.cards.clear();
    canvas.order.length = 0;
    canvas.cam = { x: 0, y: 0, k: 1 };
    if (name === live) {
      cam = { x: 0, y: 0, k: 1 };
      apply(true);
      layout();
      title.textContent = "waiting for the agent…";
    }
    paintLanes();
  }

  function highlight(root) {
    if (!window.hljs) return;
    root.querySelectorAll("pre code").forEach(el => {
      try { hljs.highlightElement(el); } catch (_) { /* unknown language */ }
    });
  }

  function unshrink(root) {
    // Mermaid stamps width="100%" and an inline max-width on its <svg>, which
    // makes the drawing track its container instead of having a size of its
    // own. Give it back its intrinsic size from the viewBox, or the layout has
    // nothing real to measure.
    const svg = root.querySelector("svg");
    if (!svg) return;
    svg.removeAttribute("width");
    svg.removeAttribute("height");
    svg.style.maxWidth = "none";
    const vb = svg.viewBox && svg.viewBox.baseVal;
    if (vb && vb.width && vb.height) {
      svg.style.width = vb.width + "px";
      svg.style.height = vb.height + "px";
    }
  }

  // ---- frame age --------------------------------------------------------

  // Relative, not absolute: "14:03" makes you do the subtraction, and the only
  // thing anyone wants from this is how stale the frame is.
  function ago(ms) {
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    const d = Math.round(s / 86400);
    return d === 1 ? "yesterday" : d + "d ago";
  }

  function paintAges() {
    for (const canvas of canvases.values()) {
      for (const c of canvas.cards.values()) {
        const el = c.el.querySelector(".age");
        if (el && c.created) el.textContent = ago(c.created);
      }
    }
  }

  // A canvas left open all day would otherwise still say "just now" at dusk.
  setInterval(paintAges, 30000);

  // ---- the pointer ------------------------------------------------------

  function cardEl(id) {
    if (!id) return null;
    return stage.querySelector(`[data-frame="${CSS.escape(String(id).replace(/^[#.]/, ""))}"]`);
  }

  // The frame the camera was last brought to. THE CAMERA TAKES PRECEDENCE OVER
  // HIGHLIGHTING (JJ, 2026-08-27): "you should always have a scene or a frame
  // focused before highlighting" — so the focused frame is the search SCOPE,
  // not just the view. On a canvas of five diagrams a bare `VT` matched a
  // subgraph in a flowchart the camera was nowhere near, and the highlight
  // landed off screen where it could not possibly help.
  let focused = null;

  // A STEP in a sequence diagram is not one element — it is a number, a label
  // and an arrow, drawn as three siblings with no shared parent. Highlighting
  // just the number is what `point seq:.sequenceNumber` gives you, and JJ's
  // words for that were: "I would expect the full step to be highlighted,
  // including the number, the label and the arrow."
  //
  // Mermaid emits each class in message order, so the Nth of each belongs to
  // step N. That is the only relationship available — there is no grouping in
  // the markup to hang this on.
  // Point at PROSE by what it says: `road:text:persistence` finds the bullet
  // about persistence. Selectors reach a diagram's nodes by name, but a
  // markdown frame has nothing to name — an agent would have to count list
  // items and hope the document has not changed since it wrote them.
  //
  // The SMALLEST match wins. Every ancestor up to the card also contains the
  // string, so the largest match is always the whole frame — which is the one
  // answer that is never useful.
  function textEl(root, needle) {
    const want = String(needle).toLowerCase().trim();
    if (!want) return null;
    const hits = [...root.querySelectorAll(
      "li, p, h1, h2, h3, h4, h5, td, th, code, blockquote, text, tspan, span, div")]
      .filter(n => (n.textContent || "").toLowerCase().includes(want));
    hits.sort((a, b) => (a.textContent || "").length - (b.textContent || "").length);
    return hits[0] || null;
  }

  function stepEls(root, n) {
    const i = Math.max(0, n - 1);
    const nth = sel => [...root.querySelectorAll(sel)][i];
    return [nth(".sequenceNumber"),
            nth(".messageText"),
            // Lines alternate messageLine0/messageLine1, so they have to be
            // collected together to stay in document order.
            nth('[class*="messageLine"]')].filter(Boolean);
  }

  // Everything a selector matches. `point` highlights all of them; callers that
  // need a single element for the camera take the first.
  function findEls(selector) {
    if (!selector) return [];
    const scoped = String(selector).match(/^([^: ]+):(.+)$/);
    if (scoped) {
      const host = cardEl(scoped[1]);
      if (host) {
        const step = scoped[2].match(/^step:([0-9]+)$/);
        if (step) return stepEls(host, Number(step[1]));
        const txt = scoped[2].match(/^text:(.+)$/);
        if (txt) { const t = textEl(host, txt[1]); return t ? [t] : []; }
        const one = findIn(host, scoped[2]);
        return one ? [one] : [];
      }
    }
    const el = findEl(selector);
    return el ? [el] : [];
  }

  function findEl(selector) {
    if (!selector) return null;

    // `frame:selector` scopes explicitly, and beats every heuristic below.
    const scoped = String(selector).match(/^([^: ]+):(.+)$/);
    if (scoped) {
      const host = cardEl(scoped[1]);
      if (host) {
        const step = scoped[2].match(/^step:([0-9]+)$/);
        if (step) return stepEls(host, Number(step[1]))[0] || null;
        return findIn(host, scoped[2]) || null;
      }
    }

    // A frame id wins: `point costs` means that card, not something inside one.
    const card = cardEl(selector);
    if (card) return card;

    // Then the focused frame, before anything else on the canvas.
    const home = focused && cardEl(focused);
    if (home) {
      const hit = findIn(home, selector);
      if (hit) return hit;
    }
    return findIn(stage, selector);
  }

  function findIn(root, selector) {
    try {
      const exact = root.querySelector(selector);
      if (exact) return exact;
    } catch (_) { /* an agent-authored selector can be malformed */ }

    // Otherwise resolve a mermaid id. Mermaid names a node "flowchart-FR-6" and
    // the EDGE ending at it "L_AG_FR_0", so a plain substring match finds the
    // arrow. Nodes first, and a whole-segment match beats a substring, or
    // pointing at "FR" would also match a node called "FRAME".
    const bare = selector.replace(/^[#.]/, "");
    const seg = id => (id || "").split(/[-_]/);
    const nodes = [...root.querySelectorAll("g.node, .node")];
    const hit = nodes.find(n => seg(n.id).includes(bare))
             || nodes.find(n => (n.id || "").includes(bare));
    if (hit) return hit;

    const rest = [...root.querySelectorAll("[id]")].filter(
      n => !/^L[-_]/.test(n.id) && !n.classList.contains("edgePath"));

    // Sequence diagrams name their parts differently: a participant's id is
    // `actor0` / `root-0`, and the ALIAS you wrote lives in `data-id` and
    // `name`. Prefer the participant BOX over its life-line — highlighting a
    // 0.5px vertical rule is technically correct and invisible.
    const named = [...root.querySelectorAll(
      `[data-id="${CSS.escape(bare)}"], [name="${CSS.escape(bare)}"]`)];
    const exactNamed =
         named.find(n => (n.dataset.type || n.dataset.et || "").includes("participant"))
      || named.find(n => n.tagName.toLowerCase() !== "line")
      || named[0];

    // 🔴 EXACTNESS BEFORE PROXIMITY, and the order here is the whole fix.
    // The canvas holds every frame at once, so a SUBSTRING id match can win in
    // a diagram the camera is nowhere near. `point VT` hit the `VT` subgraph of
    // another flowchart, and `point K` hit a node called `KOK` — both landed
    // off screen while the sequence diagram sat there unhighlighted. Same shape
    // as the original edge-vs-node bug: a fuzzy match beating an exact one.
    return rest.find(n => n.id === bare)
        || rest.find(n => seg(n.id).includes(bare))
        || exactNamed
        || rest.find(n => n.id.includes(bare))
        || null;
  }

  function point(selector) {
    // Clear the previous pointer first: two things lit at once is exactly the
    // strobe-light failure the research warned about.
    stage.querySelectorAll(".pointed").forEach(n => n.classList.remove("pointed"));
    stage.querySelectorAll(".halo").forEach(n => n.remove());
    const els = findEls(selector);
    if (els.length > 1) halo(els);        // one box around the group
    else els.forEach(el => el.classList.add("pointed"));
  }

  // Draw a single rectangle over the combined bounds of several elements.
  // Positioned in STAGE space, so it survives panning and zooming like any
  // other card: client rects come back in screen pixels, and dividing by the
  // camera scale is what puts them back into the coordinate system the stage
  // is drawn in.
  function halo(els) {
    const s = stage.getBoundingClientRect();
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const el of els) {
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) continue;   // an empty label contributes nothing
      x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top);
      x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
    }
    if (!isFinite(x0)) return;
    const pad = 7;
    const d = document.createElement("div");
    d.className = "halo";
    d.style.left   = ((x0 - s.left) / cam.k - pad) + "px";
    d.style.top    = ((y0 - s.top)  / cam.k - pad) + "px";
    d.style.width  = ((x1 - x0) / cam.k + pad * 2) + "px";
    d.style.height = ((y1 - y0) / cam.k + pad * 2) + "px";
    stage.appendChild(d);
  }

  // A cue is one clip's worth of highlights, timed against the speech. The
  // timers live here rather than in the sender so the network jitter stays
  // OUTSIDE the sentence: one event arrives, and every mark after it fires
  // from the local clock.
  let cueTimers = [];

  // lane -> a schedule waiting for that lane to go live. The held-clip case:
  // a background agent's clip plays BY ITSELF the moment he switches to it, and
  // the agent gets no event at that instant — so the timers have to be sitting
  // here already, not requested when the switch happens.
  const armed = new Map();

  function cancelCue() {
    cueTimers.forEach(clearTimeout);
    cueTimers = [];
  }

  function fireArmed(lane) {
    const msg = armed.get(lane);
    if (!msg) return;
    armed.delete(lane);
    // Bring the camera FIRST and let it travel during the lead-in, so the frame
    // is settled before the first word lands on it.
    if (msg.look) {
      const el = cardEl(msg.look) || findEl(msg.look);
      // Scope follows the camera here too, and it matters more on this path:
      // an armed cue runs while the agent is not live and cannot correct a
      // mark that resolved into the wrong frame.
      if (cardEl(msg.look)) focused = msg.look;
      if (el) zoomTo(el, 80, true);
    }
    // `lead_ms` is the clip's own lead-in — the silence before the first word is
    // audible. Marks are offsets into the AUDIO, so the clock starts when the
    // audio does, not when the lane moved.
    if (msg.lead_ms > 0) setTimeout(() => runCue(msg.marks), msg.lead_ms);
    else runCue(msg.marks);
  }

  function runCue(marks) {
    // A second cue during a running one would put the pointer in two places —
    // the strobe-light failure again, just spread over time.
    cancelCue();
    (marks || []).forEach(mk => {
      const ms = Math.max(0, Number(mk.at) || 0) * 1000;
      if (ms === 0) { point(mk.selector); return; }
      cueTimers.push(setTimeout(() => point(mk.selector), ms));
    });
  }

  // ---- the stream -------------------------------------------------------

  // The page upgrades itself. Content never needs a refresh — that is what the
  // stream is for — but a change to THIS script would, and being told to reload
  // is exactly the thing this surface exists to avoid. So the server announces
  // the version it serves and an older page replaces itself. EventSource
  // reconnects on its own after a restart, so it fires untouched.
  const PAGE_VERSION = "__VERSION__";

  useLane("main");   // binds `cards`/`order` before the first event arrives

  // `?shot=<ms>` is one-shot mode, used by `command-bridge shot`. A headless
  // browser waits for the page to finish loading before it captures, and an
  // open EventSource means it never does — so in this mode we render, settle,
  // then CLOSE the stream, which is what lets the capture fire and the browser
  // exit. Nothing else about the page changes.
  const SHOT = new URLSearchParams(location.search).get("shot");
  // `?lane=<name>` pins THIS page to one lane, for capture only. An agent
  // that is not live cannot photograph its own canvas otherwise: the page
  // renders whoever holds the floor, so a background agent gets someone
  // else's drawing — or an empty canvas it may read as its own render
  // having failed. Pinning is LOCAL: the server's live lane never moves,
  // and nothing changes on his screen. Only honoured in shot mode, so it
  // can never become a way to take the floor.
  const PIN = SHOT !== null
    ? new URLSearchParams(location.search).get("lane") : null;

  const es = new EventSource("/events");
  if (SHOT !== null) {
    // Hold the LOAD EVENT open until the frames have arrived and rendered.
    // `/settle` is an empty script the server answers slowly, and a pending
    // script blocks load — so the headless shutter fires after it, every time,
    // rather than racing the stream. Then close the stream so the browser can
    // finish and exit.
    const settle = Math.max(200, Number(SHOT) || 2500);

    // Framing happens HERE, not via a server `look`, because a capture may be
    // pinned to a lane that is not live — and a camera verb from a background
    // lane is refused, correctly. This is local to the capture page.
    //
    // It runs late in the settle window: frames arrive over a second or two and
    // the page fits the first one as it lands, so an early look is overwritten
    // by the page's own startup. (A window RESIZE used to overwrite it too;
    // that handler no longer refits.)
    const WANT = new URLSearchParams(location.search).get("look");
    if (WANT) setTimeout(() => {
      focused = WANT;
      const el = cardEl(WANT) || findEl(WANT);
      if (el) zoomTo(el, 80, false);   // no flight: the shutter is not waiting
    }, Math.max(200, settle * 0.8));

    const hold = document.createElement("script");
    hold.src = "/settle?ms=" + settle;
    hold.onload = hold.onerror = () => {
      try { es.close(); } catch (_) {}
    };
    document.head.appendChild(hold);
  }
  es.onopen = () => { dot.classList.add("live"); };
  es.onerror = () => { dot.classList.remove("live"); };
  es.addEventListener("version", e => {
    if (JSON.parse(e.data).version !== PAGE_VERSION) location.reload();
  });
  es.addEventListener("frame", async e => {
    const msg = JSON.parse(e.data);
    const lane = msg.lane || "main";
    await place(msg);
    // First frame on an empty canvas: fit it, so a single drawing still fills
    // the window the way it did before there was a canvas. Only for the live
    // lane — a background lane must never move the view.
    if (lane === live && cards.size === 1) requestAnimationFrame(() => fit(false));
  });
  es.addEventListener("sync", e => {
    // The server just told us what it believes in. Anything we are still
    // showing that it does not know about survived a server restart and is now
    // unaddressable — drop it rather than display a frame nobody can `look` at.
    // The camera is left alone: a reconnect must not move the view.
    const msg = JSON.parse(e.data);
    hands.clear();
    for (const [lane, h] of Object.entries(msg.hands || {})) hands.set(lane, h);
    for (const [lane, ids] of Object.entries(msg.lanes || {})) {
      const keep = new Set(ids);
      const canvas = canvasFor(lane);
      for (const id of [...canvas.cards.keys()]) if (!keep.has(id)) drop(id, lane);
    }
    const want = PIN || msg.live;
    if (want && want !== live) useLane(want); else paintLanes();
  });
  es.addEventListener("rows", async e => {
    // The cheap update: no markup crosses the wire, only the rows.
    const msg = JSON.parse(e.data);
    if (msg.lane && msg.lane !== live) return;
    const view = views.get(msg.id);
    if (!view || !window.vega) return;
    try {
      const cs = vega.changeset();
      if (msg.remove_all) cs.remove(() => true);
      if (msg.insert && msg.insert.length) cs.insert(msg.insert);
      await view.change(msg.data_name || "table", cs).runAsync();
      // Vega does not always resize for new data; the camera measures the card,
      // so a chart that silently outgrew its box would be cropped.
      view.resize();
      requestAnimationFrame(() => layout());
    } catch (_) { /* a bad row set must not take the surface down */ }
  });
  es.addEventListener("remove", e => { const m = JSON.parse(e.data); drop(m.id, m.lane); });
  es.addEventListener("clear", e => clearAll(JSON.parse(e.data).lane));
  es.addEventListener("raise", e => {
    const m = JSON.parse(e.data);
    if (m.lane === live) return;           // it has the floor; a hand is noise
    hands.set(m.lane, { count: m.count, why: m.why });
    canvasFor(m.lane);
    paintLanes();
  });
  es.addEventListener("switch", e => {
    if (PIN) return;                 // pinned for a capture; the floor is not ours
    useLane(JSON.parse(e.data).lane);
  });
  // ---- inspect ----------------------------------------------------------
  // The page answers a structured question about a selector, so an agent can
  // ask "did this resolve, and can he see it" for ~50 tokens of JSON instead
  // of ~1500 as an image. Most of what screenshots were used for here was
  // never a visual question — it was this one.
  es.addEventListener("inspect", e => {
    const msg = JSON.parse(e.data);
    const port = view.getBoundingClientRect();
    const els = findEls(msg.selector);
    const seen = r => r.right > port.left && r.left < port.right &&
                      r.bottom > port.top && r.top < port.bottom;
    const card = n => { const c = n.closest ? n.closest("[data-frame]") : null;
                        return c ? c.dataset.frame : null; };
    fetch("/inspected", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: msg.id,
        live: live,
        resolved: els.length,
        elements: els.slice(0, 8).map(n => {
          const r = n.getBoundingClientRect();
          return {
            tag: n.tagName.toLowerCase(),
            el_id: n.id || null,
            data_id: (n.dataset && n.dataset.id) || null,
            cls: (n.getAttribute("class") || "").slice(0, 80) || null,
            frame: card(n),
            pointed: n.classList.contains("pointed"),
            visible: seen(r),
            text: (n.textContent || "").trim().slice(0, 60) || null,
          };
        }),
        halo: !!stage.querySelector(".halo"),
        focused: focused,
      }),
    }).catch(() => {});
  });

  es.addEventListener("cue", e => {
    const msg = JSON.parse(e.data);
    if (msg.cancel) { armed.delete(msg.lane); cancelCue(); point(""); return; }
    // An ARMED cue is the one message a background lane may send: it is stored,
    // not run, so the server's usual "not live" refusal does not apply.
    if (msg.arm) {
      armed.set(msg.lane, msg);
      if (msg.lane === live) fireArmed(msg.lane);   // already here: just go
      return;
    }
    if (msg.lane && msg.lane !== live) return;  // server already refused it
    runCue(msg.marks);
  });
  es.addEventListener("point", e => {
    const msg = JSON.parse(e.data);
    if (msg.lane && msg.lane !== live) return;  // server already refused it
    // A plain `point` is a deliberate override, so it wins over a schedule
    // still running — otherwise the next mark would yank the highlight back.
    cancelCue();
    point(msg.selector);
    // Pointing and moving the camera are one gesture when asked for together:
    // a highlight outside the viewport is a reference with no referent.
    if (msg.zoom || msg.look) {
      const el = findEl(msg.selector);
      if (el) zoomTo(el);
    }
  });
  es.addEventListener("look", e => {
    const msg = JSON.parse(e.data);
    if (msg.lane && msg.lane !== live) return;  // server already refused it
    // `look --all` is a deliberate step back to the whole canvas, so it drops
    // the scope with the view: nothing is focused, and a selector may match
    // anywhere again.
    if (msg.all || !msg.id) { focused = null; fit(true); return; }
    const el = cardEl(msg.id) || findEl(msg.id);
    if (el) {
      // The camera and the search scope move together. This is the rule:
      // focus a frame, THEN highlight inside it.
      if (cardEl(msg.id)) focused = msg.id;
      zoomTo(el, 80, true);
    }
  });
  es.addEventListener("zoom", e => {
    const msg = JSON.parse(e.data);
    if (msg.lane && msg.lane !== live) return;  // server already refused it
    if (msg.selector) {
      const el = findEl(msg.selector);
      if (el) zoomTo(el);
    } else if (msg.scale === "fit" || msg.scale == null) {
      fit(true);
    } else {
      const port = view.getBoundingClientRect();
      const k = Math.max(0.05, Math.min(6, Number(msg.scale)));
      cam.x = port.width / 2 - (port.width / 2 - cam.x) * (k / cam.k);
      cam.y = port.height / 2 - (port.height / 2 - cam.y) * (k / cam.k);
      cam.k = k;
      apply(true);
    }
  });
  // A settled camera is reported so `status` can answer "what can he see?".
  stage.addEventListener("transitionend", report);
</script>
"""


# Hashed with the placeholder still in it, so the value is stable across
# restarts and changes exactly when the page's code changes.
PAGE_VERSION = hashlib.sha256(PAGE.encode()).hexdigest()[:12]


def render() -> str:
    """The page, with its version stamped in."""
    return PAGE.replace("__VERSION__", PAGE_VERSION)
