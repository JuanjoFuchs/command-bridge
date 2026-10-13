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
  /* COPY BUTTONS in the title bar (JJ, 2026-10-01): one copies the frame's content (its source,
     or the image itself), one copies the full path of the file it renders. Quiet until hovered. */
  .card > .bar > .tools { margin-left: auto; display: flex; gap: .3rem; }
  .card > .bar > .tools > button {
    font: inherit; font-size: 10px; letter-spacing: .04em; color: var(--dim); background: none;
    border: 1px solid var(--edge); border-radius: 4px; padding: 0 .4rem; cursor: pointer; opacity: .55;
  }
  .card:hover > .bar > .tools > button { opacity: 1; }
  .card > .bar > .tools > button:hover { color: var(--fg, #e7eaf1); }
  .card > .bar > .tools > button.done { color: #5fd38d; border-color: #5fd38d; }
  /* AN IMAGE NEVER GRABS THE DRAG (JJ, 2026-10-01: "when I try to drag the canvas it doesn't move
     and it's like as if I'm grabbing the image"). The browser's native image drag swallowed the
     pan gesture; with it off, a drag over a picture pans the canvas like anywhere else. */
  .card img, .card video, .card svg image { -webkit-user-drag: none; user-drag: none; user-select: none; }
  /* `zoom` rather than `transform: scale()` — zoom changes the element's LAYOUT
     box, so the packer and the camera measure the scaled size and everything
     downstream just works. A transform would leave a card claiming its
     unscaled footprint and overlapping its neighbour. */
  .card > .body { padding: .4rem; }
  /* Media fills the frame. A body whose ONLY child is an image or a video sheds
     its padding and the element bleeds to the card edges — no wasted margin, no
     caption strip. The LABEL goes in the frame title (the `.bar`), never a line of
     text below the media. `line-height:0` kills the inline descender gap that
     otherwise leaves a thin strip under a replaced element. (Live 2026-09-11 —
     JJ: "the content should feel the full frame, and don't add labels below it.") */
  .card > .body:has(> img:only-child),
  .card > .body:has(> video:only-child) { padding: 0; line-height: 0; }
  .card > .body > img:only-child,
  .card > .body > video:only-child { display: block; max-width: 100%; border-radius: 0 0 7px 7px; }
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
  /* A mermaid diagram rendered inside a markdown frame keeps mermaid's natural pixel width, which
     ran past the card's right edge; scale it DOWN to the prose column instead (never up). */
  .md-mermaid { margin: 12px 0; }
  /* !important because mermaid writes its natural width as an INLINE max-width on the svg, which
     outranks any stylesheet rule — the first version of this rule was silently ignored. */
  .md-mermaid svg { max-width: 100% !important; height: auto; }
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
  }
  /* The pulse is on the HTML halo ONLY, never on `.pointed`. A CSS animation on
     an SVG element that lives inside a CSS-`transform`ed, `will-change: transform`
     ancestor (our `#stage` camera) makes WebKit promote that element to its own
     layer WITHOUT the ancestor transform — so the node visibly JUMPS to the
     untransformed origin, animates there, and snaps back. JJ saw exactly this:
     "the box has an animation and moves around away from its location on the
     diagram" (2026-09-11). The box-shadow the pulse animates is invisible on an
     SVG node anyway, so scoping it to the halo loses nothing and the static
     outline still holds the highlight for a late glance. */
  div.pointed { animation: pulse 1.1s ease-out 2; }
  /* A mermaid node (g.node) is a self-contained box: the `.pointed` outline above
     already IS the pointer rectangle. Recolouring its own border on top drew a
     SECOND orange line 3px inside the outline — "two orange lines next to each
     other" (JJ, 2026-09-03). So exclude `.node` from the descendant recolour and
     let the outline stand alone; non-node containers (a hand-authored group, a
     sequence step's parts) still get their inner shapes marked. */
  .pointed:not(.node):not(.cluster) rect, .pointed:not(.node):not(.cluster) polygon,
  .pointed:not(.node):not(.cluster) circle, .pointed:not(.node):not(.cluster) path {
    stroke: var(--glow) !important; stroke-width: 2.5px !important;
  }
  /* A sequence STEP is pointed at as three separate elements, and two of them
     are an SVG `line` and a `text`. The rule above only reaches descendants, so
     without these the arrow and the label stayed grey while the number lit —
     which is what "only the number was highlighted" looked like. Text takes a
     fill; a line takes a stroke. */
  line.pointed { stroke: var(--glow) !important; stroke-width: 2.5px !important; }
  text.pointed, .pointed:not(.node):not(.cluster) text, .pointed:not(.node):not(.cluster) tspan {
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

  /* ---- spec 015: the drawing layer (his ink) --------------------------------
     An INK frame is the human's freehand strokes, and it is deliberately NOT a packed card: it is a
     transparent overlay positioned in canvas space at the coordinates he drew, so a stroke laid over
     a frame stays ON that frame (FR2a/AC5). No card chrome, no title bar, and pointer-events:none so
     it never blocks a drag — or a stroke drawn on the frame beneath it. The packer already skips it
     because a submitted sketch always carries an explicit `at`. */
  .card[data-kind="ink"] {
    background: transparent; border: none; box-shadow: none;
    max-width: none; pointer-events: none;
  }
  .card[data-kind="ink"] > .bar { display: none; }
  .card[data-kind="ink"] > .body { padding: 0; }
  .card[data-kind="ink"] svg { overflow: visible; }
  /* The LIVE preview while he draws. A child of #stage so it lives in canvas space and pans/zooms
     with the frames — which is why a stroke stays glued to what it was drawn over. It draws no
     events itself, and it carries no size of its own: absolute canvas coordinates paint outside its
     1x1 box because overflow is visible. */
  #ink-live { position: absolute; left: 0; top: 0; width: 1px; height: 1px;
              overflow: visible; pointer-events: none; }
  /* A spare toolbar (FR5): pen, eraser, send — "whatever is fastest". Anchored to the viewport
     corner so it is reachable on a phone without hunting, and above the canvas. */
  #tools { position: absolute; left: .6rem; bottom: .6rem; display: flex; gap: .3rem; z-index: 10; }
  #tools .tool {
    font: 12px ui-sans-serif, system-ui, sans-serif; letter-spacing: .02em;
    padding: .4rem .62rem; border: 1px solid var(--edge); border-radius: 7px;
    background: color-mix(in srgb, var(--card) 88%, transparent); color: var(--fg);
    cursor: pointer; -webkit-user-select: none; user-select: none;
    touch-action: manipulation;   /* a tap on a tool fires at once, never held for a gesture */
  }
  #tools .tool.on { border-color: var(--glow); color: var(--glow); }
  /* While a drawing tool is active the surface is a canvas to ink on, not one to grab-and-pan. */
  main.inking { cursor: crosshair; }
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
  <!-- spec 015: the drawing toolbar. Pen / eraser / send — the whole tool JJ asked for. -->
  <div id="tools">
    <button id="tool-pen" class="tool" title="Draw with a finger or pen">Pen</button>
    <button id="tool-erase" class="tool" title="Erase strokes">Erase</button>
    <button id="tool-send" class="tool" title="Hand the sketch to the agent">Send</button>
  </div>
</main>

<script>
  // A built-in Command Bridge mermaid theme so an agent writes a plain diagram and it
  // matches the canvas with no hand-styling (JJ, 2026-09-03). The load-bearing rule:
  // NO orange anywhere in here — orange is the deixis POINTER's colour (var(--glow)), and
  // a node painted the pointer's colour reads as permanently pointed-at, so a real point at
  // it barely shows ("that is confusing"). Nodes stay uniform + neutral; focus comes from the
  // pointer, and any deliberate accent an agent adds must be a COOL hue, never the glow.
  (function initMermaid() {
    var dark = matchMedia("(prefers-color-scheme: dark)").matches;
    var vars = dark ? {
      darkMode: true, background: "transparent",
      fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif",
      primaryColor: "#242a36", primaryBorderColor: "#5a6478", primaryTextColor: "#eef0f4",
      secondaryColor: "#2b3240", tertiaryColor: "#20242e",
      mainBkg: "#242a36", nodeBorder: "#5a6478", nodeTextColor: "#eef0f4",
      lineColor: "#8791a3", textColor: "#eef0f4",
      clusterBkg: "#1a1e27", clusterBorder: "#3a4150", titleColor: "#eef0f4",
      edgeLabelBackground: "#1a1e27",
    } : {
      darkMode: false, background: "transparent",
      fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif",
      primaryColor: "#eef1f5", primaryBorderColor: "#8a93a5", primaryTextColor: "#2d3142",
      secondaryColor: "#e3e7ee", tertiaryColor: "#f3f5f8",
      mainBkg: "#eef1f5", nodeBorder: "#8a93a5", nodeTextColor: "#2d3142",
      lineColor: "#6b7280", textColor: "#2d3142",
      clusterBkg: "#f3f5f8", clusterBorder: "#c7cdd8", titleColor: "#2d3142",
      edgeLabelBackground: "#f3f5f8",
    };
    // suppressErrorRendering: on a bad definition mermaid otherwise injects its
    // own "Syntax error in text" bomb graphic into document.body — an orphan that
    // is NOT scoped to the failing card, so it hangs over EVERY lane in an already-
    // open browser until a reload. We already catch the throw in paint() and show
    // the message inside the card; this stops the global bomb so a single agent's
    // bad frame can't smear an error across the whole canvas. (Live 2026-09-11.)
    mermaid.initialize({ startOnLoad: false, securityLevel: "loose", theme: "base", themeVariables: vars, suppressErrorRendering: true });
  })();

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
    repaintPending();   // diagrams this lane received while hidden (see place())
    // spec 022: a lane he has never framed opens on its latest edited frame (a held off-lane look,
    // replayed right after this by the server, still wins — it is the more specific ask).
    if (focusLatestFor === lane) { focusLatestFor = null; focusLatestOnOpen(); }
    else if (cards.size && cam.x === 0 && cam.y === 0 && cam.k === 1) {
      const latest = latestCardId();
      if (latest) autoFocus(latest, false);
    }
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

  function frameBox(box, animate, margin = 16) {
    // Scale to FILL the viewport, up as well as down. A small drawing sitting
    // at 100% in the middle of a big window is the bug, not the safe default.
    // Margin is tight and the cap is 8× (raised from 4×): a small diagram should
    // use the canvas, not float in it (JJ, 2026-09-03: "the frame does not take
    // the full available canvas").
    const port = view.getBoundingClientRect();
    if (!box || !box.w || !box.h) return;
    if (!portReady()) { focusWhenShown = true; return; }
    const k = Math.max(0.05, Math.min(
      (port.width - margin) / box.w, (port.height - margin) / box.h, 8));
    cam = {
      k,
      x: port.width / 2 - (box.x + box.w / 2) * k,
      y: port.height / 2 - (box.y + box.h / 2) * k,
    };
    apply(animate);
  }

  function fit(animate) { frameBox(extent(), animate); }

  function boxOf(el) {
    // An element's box in CANVAS space, whatever the camera is doing. The element's rect is what is
    // RENDERED right now, so divide by the RENDERED transform (the computed matrix, which tracks a
    // CSS transition mid-flight), not by `cam` — `apply(true)` sets `cam` to the TARGET at once, and
    // measuring against the target during the 0.35s glide landed the camera off its frame (JJ,
    // 2026-09-28: "whenever I open it, it kind of sometimes scrolls off screen").
    const b = el.getBoundingClientRect();
    const port = view.getBoundingClientRect();
    let k = cam.k, x = cam.x, y = cam.y;
    const m = getComputedStyle(stage).transform;
    if (m && m.startsWith("matrix(")) {
      const v = m.slice(7, -1).split(",").map(Number);
      if (v.length === 6 && v.every(isFinite) && v[0] > 0) { k = v[0]; x = v[4]; y = v[5]; }
    }
    return {
      x: (b.left - port.left - x) / k,
      y: (b.top - port.top - y) / k,
      w: b.width / k,
      h: b.height / k,
    };
  }

  // The canvas is created with the parent page and stays display:none until he opens it, so for
  // most of its life the viewport measures 0x0. Camera maths against a 0x0 port lands at the 5% zoom
  // floor with the frame on the top-left corner — and that bad camera was then KEPT (parked per lane,
  // and no longer the untouched default that triggers a re-focus), so the canvas opened on empty
  // space. Every framing function now refuses while the port is not really there; the focus it
  // skipped is replayed the moment the canvas becomes visible (the #view ResizeObserver below).
  function portReady() {
    const p = view.getBoundingClientRect();
    return p.width > 40 && p.height > 40;
  }
  let focusWhenShown = false;

  // The FR1/FR2 fork (spec 018): CONTAIN the whole frame down to this fraction of its authored
  // size; below it, a whole-frame view is too small to be worth it, so a genuinely long DOCUMENT
  // fits its width and parks at the top instead (reading position, pan down). This is the "diagram
  // vs long document" decision, made by SCALE — the page owns it (TC2), and it is the ONE knob.
  //
  // Lowered from 0.62 to 0.30 on 2026-09-11. At 0.62 a frame merely TALLER than wide — a status
  // board, contain-scale ~0.44 measured in a 1280x800 window — fell below the floor and was
  // width-filled, so its top and bottom ran off screen and JJ could not see the whole thing
  // ("frames that have a lot more height than width ... do not allow me to see the full height").
  // Measured (scripts/framefit.py): tall/wide diagrams and boards contain at 0.42-0.52 and a
  // genuinely long markdown document contains at ~0.07 — so 0.30 sits with clear margin between the
  // two clusters, containing the diagrams (FR1) while the long document still falls back (FR2).
  const READABLE = 0.30;

  // ---- spec 019: per-frame font legibility ----------------------------------------------------
  // The page is the only thing that knows the rendered text sizes AND the contain fit (TC1), so it
  // measures, on the same `report()` beat it already sends geometry and the viewport, each frame's
  // EFFECTIVE minimum font: its smallest text element, times the scale a `look` would CONTAIN the
  // frame at on his screen (spec 018's fit). The agent that drew it reads that back and knows whether
  // the frame will be readable before JJ ever focuses it.
  //
  // FOCUS_MARGIN is the gutter `look` leaves around a focused frame (`zoomTo(el, 80)` — the `look`
  // event handler below passes 80), so the fit scale reported here is the scale `look` actually
  // applies, never a second inferable number (NFR1).
  const FOCUS_MARGIN = 80;

  function fitScaleFor(w, h) {
    // Spec 018's contain fit for a w×h CANVAS-space box (offset sizes, camera-independent) in the
    // current viewport: the scale that makes the WHOLE frame fit, bounded by the tighter axis. The
    // same math `camFor` uses, so the fit an agent reasons about is the fit `look` will apply.
    const port = view.getBoundingClientRect();
    if (!w || !h) return null;
    return Math.max(0.05, Math.min((port.width - FOCUS_MARGIN) / w,
                                   (port.height - FOCUS_MARGIN) / h, 8));
  }

  function renderedFontPx(el) {
    // One text element's font size in CANVAS space (stage scale 1) — the camera divided out, so the
    // caller can multiply by the predictive fit scale to get the on-screen size when focused.
    // Measured from the element's OWN geometry (its screen box over its local box) so SVG viewBox
    // scaling, mermaid's intrinsic sizing and a body `zoom` are all captured with no per-kind special
    // case: font-size is in the same local space as that box, so the ratio cancels cleanly and the
    // result is the true rendered px whatever the drawing's internal scale.
    const local = parseFloat(getComputedStyle(el).fontSize) || 0;
    if (!local) return 0;
    const rect = el.getBoundingClientRect();
    if (!rect.height) return 0;
    let screenPerLocal;
    if (el.getBBox) {                        // SVG text: getBBox is in user units
      let bb; try { bb = el.getBBox(); } catch (_) { return 0; }
      if (!bb || !bb.height) return 0;
      screenPerLocal = rect.height / bb.height;
    } else {                                 // HTML: offsetHeight is the same box in local px
      const oh = el.offsetHeight || 0;
      if (!oh) return 0;
      screenPerLocal = rect.height / oh;
    }
    return local * screenPerLocal / cam.k;
  }

  function minFontPx(bodyEl) {
    // The SMALLEST rendered text in the frame's BODY (KD3 — the worst-case glyph decides readability,
    // not the average; the title BAR is chrome held at a fixed size, so it is excluded — the caller
    // passes `.body`). Leaf text runs only: an element whose whole text is already carried by a child
    // is a wrapper, and measuring it would just re-measure the child.
    if (!bodyEl) return null;
    const nodes = bodyEl.querySelectorAll(
      "text, tspan, li, p, h1, h2, h3, h4, h5, h6, td, th, code, blockquote, a, span, div, pre");
    let min = Infinity;
    for (const el of nodes) {
      const s = (el.textContent || "").trim();
      if (!s) continue;
      if ([...el.children].some(c => (c.textContent || "").trim() === s)) continue;  // a wrapper
      const fs = renderedFontPx(el);
      if (fs > 0) min = Math.min(min, fs);
    }
    return isFinite(min) ? min : null;
  }

  function zoomTo(el, margin = 24, fly = false) {
    // Hidden: remember THIS target (spec 037). A bare focusWhenShown replays as "the latest edit",
    // which is not what an explicit look asked for.
    if (!portReady()) { lookWhenShown = { el, margin }; return; }
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
    const k = Math.min((port.width - margin) / box.w, 8);
    const target = { k, x: port.width / 2 - (box.x + box.w / 2) * k,
                     y: margin / 2 - box.y * k };
    if (fly) flyTo(target); else { cam = target; apply(true); }
  }

  function camFor(box, margin) {
    const port = view.getBoundingClientRect();
    const k = Math.max(0.05, Math.min((port.width - margin) / box.w,
                                      (port.height - margin) / box.h, 8));
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

  // ---- pinch-zoom (touch) ----------------------------------------------
  // Two fingers zoom the camera about their midpoint (and pan by it) in BOTH pan and draw modes, so
  // a phone can zoom into a sketch without leaving the pen. `touch-action: none` stops the browser
  // doing this for us, so we track the raw pointers and drive the same `cam` the wheel handler does.
  // Registered BEFORE the pan and draw handlers so `pts` is already updated when their
  // `pts.size >= 2` guards run — a second finger then turns a pan, or a half-drawn stroke, into a
  // pinch cleanly. JJ 2026-09-09: two-finger zoom did nothing on the phone.
  const pts = new Map();          // every active canvas pointer: id -> {x, y} in client coords
  let pinch = null;               // set when the 2nd finger lands: start distance, anchor, and k

  function pinchStart() {
    const [a, b] = [...pts.values()];
    const port = view.getBoundingClientRect();
    const mx = (a.x + b.x) / 2 - port.left, my = (a.y + b.y) / 2 - port.top;
    // The canvas-space point under the initial midpoint; held under the (moving) midpoint as they
    // zoom, which is zoom-about-the-pinch and two-finger pan at once.
    pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1,
              ax: (mx - cam.x) / cam.k, ay: (my - cam.y) / cam.k, k: cam.k };
    // A pinch is neither a pan nor a stroke: drop whatever the first finger began so it neither
    // commits a stray line nor keeps panning under the zoom.
    drag = null; view.classList.remove("dragging");
    if (inking) { curStroke = null; inking = false; renderInk(); }
  }

  function pinchMove() {
    const [a, b] = [...pts.values()];
    const port = view.getBoundingClientRect();
    const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
    const mx = (a.x + b.x) / 2 - port.left, my = (a.y + b.y) / 2 - port.top;
    cancelFlight();                 // the human always wins an in-flight move
    cam.k = Math.max(0.05, Math.min(6, pinch.k * dist / pinch.dist));
    cam.x = mx - pinch.ax * cam.k;
    cam.y = my - pinch.ay * cam.k;
    apply(false);
  }

  view.addEventListener("pointerdown", e => {
    if (e.target.closest("#tools")) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 2) { e.preventDefault(); pinchStart(); }
  }, { passive: false });
  view.addEventListener("pointermove", e => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size >= 2 && pinch) { e.preventDefault(); pinchMove(); }
  }, { passive: false });
  const dropPinchPointer = e => {
    if (!pts.delete(e.pointerId)) return;
    if (pts.size < 2) pinch = null;   // back to one finger: pan/draw resumes on the next fresh touch
  };
  view.addEventListener("pointerup", dropPinchPointer);
  view.addEventListener("pointercancel", dropPinchPointer);

  view.addEventListener("pointerdown", e => {
    // A pointerdown on a toolbar button bubbles up to here; capturing it would steal the button's
    // own click (pointer capture retargets the click off the button). So the toolbar owns its taps.
    if (e.target.closest("#tools")) return;
    if (pts.size >= 2) return;          // two fingers is a pinch-zoom, not a pan
    if (drawMode) return;              // a drawing tool owns the pointer (spec 015); do not pan
    cancelFlight();
    stage.style.transition = "none";
    drag = { x: e.clientX - cam.x, y: e.clientY - cam.y, moved: false };
    view.setPointerCapture(e.pointerId);
  });
  view.addEventListener("pointermove", e => {
    if (pts.size >= 2) return;          // a pinch is in progress; the pan is suspended
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

  // ---- the drawing layer (spec 015) -------------------------------------
  // His ink, draw-then-submit (KD5). Strokes are captured straight into CANVAS coordinates — the
  // inverse of the camera transform, the same math `boxOf` uses — so a stroke stays glued to
  // whatever it was drawn over even if he pans or zooms between strokes, and the sketch he hands
  // over lands on the shared canvas exactly where the frames are (FR2a/AC5). The agent reads it by
  // screenshot (KD3); the browser only captures and submits, it parses nothing.
  //
  // Pointer/touch, and NOT gated on a secure context — pointer events work over plain http, which
  // is what makes reaching this on a LAN cheaper than voice (TC3). No pressure (KD6).
  const PEN_PX = 3;      // on-screen stroke width; stored in canvas units as PEN_PX / cam.k
  const ERASE_PX = 16;   // on-screen eraser radius
  let drawMode = null;   // null = pan (unchanged), 'pen', or 'erase'
  let strokes = [];      // committed strokes: [{ pts:[{x,y} in canvas coords], w }]
  let curStroke = null;
  let inking = false;

  // The preview lives in canvas space, on top of everything, rebuilt on each move — a human draws a
  // handful of strokes, so a full rebuild is far cheaper than tracking nodes, and it stays correct
  // through pan/zoom for free because it rides #stage.
  const inkLive = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  inkLive.id = "ink-live";
  stage.appendChild(inkLive);

  const toolPen = document.getElementById("tool-pen");
  const toolErase = document.getElementById("tool-erase");
  const toolSend = document.getElementById("tool-send");

  function viewToCanvas(e) {
    const port = view.getBoundingClientRect();
    return { x: (e.clientX - port.left - cam.x) / cam.k,
             y: (e.clientY - port.top - cam.y) / cam.k };
  }

  function strokeD(pts, ox, oy) {
    if (!pts.length) return "";
    // A single tap is `M x y L x y` — with a round linecap that paints a dot rather than nothing.
    const p = pts.map(q => (q.x - ox).toFixed(1) + " " + (q.y - oy).toFixed(1));
    return pts.length === 1 ? ("M" + p[0] + " L" + p[0]) : ("M" + p.join(" L"));
  }

  function strokePath(s, ox, oy) {
    return '<path d="' + strokeD(s.pts, ox || 0, oy || 0) + '" fill="none" stroke="currentColor"'
      + ' stroke-width="' + s.w.toFixed(2) + '" stroke-linecap="round" stroke-linejoin="round"/>';
  }

  function renderInk() {
    const all = curStroke ? strokes.concat([curStroke]) : strokes;
    inkLive.innerHTML = all.map(s => strokePath(s, 0, 0)).join("");
  }

  function eraseAt(p) {
    // Canvas-space hit test: drop any stroke that passes within the eraser radius of the pointer.
    const r = ERASE_PX / cam.k;
    const kept = strokes.filter(s => !s.pts.some(q => Math.hypot(q.x - p.x, q.y - p.y) <= r));
    if (kept.length !== strokes.length) { strokes = kept; renderInk(); }
  }

  view.addEventListener("pointerdown", e => {
    if (!drawMode) return;             // pan mode owns the pointer (handler above)
    if (pts.size >= 2) return;         // two fingers is a pinch-zoom, not a stroke
    if (e.target.closest("#tools")) return;   // a tap on Send/Erase is not the start of a stroke
    e.preventDefault();
    view.setPointerCapture(e.pointerId);
    inking = true;
    const p = viewToCanvas(e);
    if (drawMode === "erase") { eraseAt(p); return; }
    curStroke = { pts: [p], w: PEN_PX / cam.k };
    renderInk();
  });
  view.addEventListener("pointermove", e => {
    if (pts.size >= 2) return;          // suspended while a pinch-zoom is happening
    if (!drawMode || !inking) return;
    const p = viewToCanvas(e);
    if (drawMode === "erase") { eraseAt(p); return; }
    curStroke.pts.push(p);
    renderInk();
  });
  function endStroke() {
    if (curStroke && curStroke.pts.length) strokes.push(curStroke);
    curStroke = null; inking = false; renderInk();
  }
  view.addEventListener("pointerup", () => { if (drawMode) endStroke(); });
  view.addEventListener("pointercancel", () => { if (drawMode) endStroke(); });

  function setMode(m) {
    drawMode = drawMode === m ? null : m;   // tapping the active tool again returns to pan
    toolPen.classList.toggle("on", drawMode === "pen");
    toolErase.classList.toggle("on", drawMode === "erase");
    view.classList.toggle("inking", !!drawMode);
    // Keep the preview above any frame placed since (a later DOM sibling paints last).
    if (drawMode) stage.appendChild(inkLive);
  }
  toolPen.onclick = () => setMode("pen");
  toolErase.onclick = () => setMode("erase");
  toolSend.onclick = () => submitDrawing();

  async function submitDrawing() {
    if (!strokes.length) return;            // nothing to hand over
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, maxW = 0;
    for (const s of strokes) {
      maxW = Math.max(maxW, s.w);
      for (const q of s.pts) {
        x0 = Math.min(x0, q.x); y0 = Math.min(y0, q.y);
        x1 = Math.max(x1, q.x); y1 = Math.max(y1, q.y);
      }
    }
    // Pad by the stroke width so a mark at the very edge is not clipped by the viewBox.
    const pad = maxW + 4;
    const ox = x0 - pad, oy = y0 - pad;
    const w = (x1 - x0) + pad * 2, h = (y1 - y0) + pad * 2;
    // Author the svg in LOCAL coords (translated by the bbox origin) with a viewBox of the same
    // size, and hand over `at:[ox,oy]` in CANVAS space. Placed there, the local coords map straight
    // back onto the canvas — so the ink lands exactly over the frame it annotates (AC5).
    const paths = strokes.map(s => strokePath(s, ox, oy)).join("");
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + w.toFixed(1) + '" height="'
      + h.toFixed(1) + '" viewBox="0 0 ' + w.toFixed(1) + ' ' + h.toFixed(1) + '">' + paths + '</svg>';
    try {
      await fetch("/draw", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: svg, at: [ox, oy], title: "sketch" }) });
    } catch (_) {
      return;   // keep the strokes so a failed send can be retried rather than silently lost
    }
    // Draw-then-submit clears the local buffer on send; the ink now lives as a frame on the shared
    // canvas and arrives back over /events like any other frame.
    strokes = []; curStroke = null; renderInk();
    setMode(null);
  }

  // ---- the canvas -------------------------------------------------------

  function layout() {
    // A halo is drawn over WHERE some elements were, in stage coordinates. Any
    // re-layout — a frame replaced, added, removed — moves or destroys those
    // elements and leaves the box floating over nothing. JJ saw exactly that:
    // "a weird orange box around nothing". A highlight outlives its referent
    // for as long as this is not cleared, which is worse than no highlight.
    stage.querySelectorAll(".halo").forEach(n => n.remove());

    // STICKY placement (spec 022 FR6 — JJ: "I wouldn't want the whole board to jump every time").
    // Walk the frames oldest first. A frame that already has a position KEEPS it; a new one goes into
    // the first free gap (top-most, then left-most) that clears every frame already placed, and is
    // then PINNED there — so adding, editing or removing a frame never moves any other. If a pinned
    // frame now overlaps an OLDER one (the older grew), only the newer frame moves. A frame with no
    // size yet (still rendering) is placed provisionally and pinned once it has one. Ink (the human's
    // sketch) is exempt: it is drawn ON TOP of what it annotates, so it keeps its spot and blocks
    // nothing. Deterministic: the same frames in the same order land in the same places.
    const placed = [];
    const hits = (x, y, w, h) => placed.some(r =>
      x < r.x + r.w + GAP && x + w + GAP > r.x && y < r.y + r.h + GAP && y + h + GAP > r.y);
    const freeSpot = (w, h) => {
      let bottom = 0;
      const cand = [[0, 0]];
      for (const r of placed) {
        cand.push([r.x + r.w + GAP, r.y], [r.x, r.y + r.h + GAP]);
        bottom = Math.max(bottom, r.y + r.h + GAP);
      }
      cand.push([0, bottom]);
      let best = null;
      for (const [x, y] of cand) {
        if (x > 0 && x + w > ROW_WIDTH) continue;
        if (hits(x, y, w, h)) continue;
        if (!best || y < best[1] || (y === best[1] && x < best[0])) best = [x, y];
      }
      return best || [0, bottom];
    };
    for (const id of order) {
      const c = cards.get(id);
      if (!c) continue;
      const w = c.el.offsetWidth, h = c.el.offsetHeight;
      const ink = c.el.dataset.kind === "ink";
      let pos = c.at;
      if (!ink && (!pos || hits(pos[0], pos[1], w, h))) {
        pos = freeSpot(w, h);
        if (w > 0 && h > 0) c.at = pos;
      }
      if (!pos) pos = [0, 0];
      c.el.style.left = pos[0] + "px";
      c.el.style.top = pos[1] + "px";
      if (!ink) placed.push({ x: pos[0], y: pos[1], w, h });
    }
    emptyTag.style.display = cards.size ? "none" : "grid";
    countTag.textContent = cards.size
      ? cards.size + (cards.size === 1 ? " frame" : " frames") : "";
    report();
  }

  // RE-PACK WHEN A FRAME'S MEASURED SIZE SETTLES (JJ, 2026-09-22: frames "one on top of the other",
  // and "blank" until a double-click). `layout()` shelf-packs by offsetWidth/offsetHeight, but async
  // content — a mermaid SVG being unshrunk, markdown reflow, a webfont load — finishes AFTER the
  // post-paint layout ran, so that first pack measured 0/partial sizes and laid every frame at the
  // origin on top of the last. A ResizeObserver re-runs the pack whenever a card actually changes
  // size (debounced so a burst of settling frames costs one pass), and re-frames the view IF he has
  // not moved it yet — so switching to a lane lands on its frames instead of empty space.
  let _relayoutTimer = null;
  let pendingFocus = null;   // spec 022: an auto-focus still waiting for its card's final size
  // AN EXPLICIT CAMERA MOVE WINS (spec 037). `point --look`, `look` and `zoom` are an agent choosing
  // what he sees while it talks about it; an auto-focus is only "here is the latest edit". The edit's
  // own settle timers and pendingFocus used to re-frame the latest edit AFTER the explicit move — so
  // "write the frame, then point at it" reported ok and the camera went elsewhere (backlog 10-09).
  let lastExplicit = 0;
  let lookWhenShown = null;   // an explicit target asked for while the canvas was hidden
  function explicitCamera() {
    lastExplicit = Date.now();
    pendingFocus = null;
    bootFocusUntil = 0;
    focusWhenShown = false;
  }
  // THE EXPLICIT TARGET OUTLIVES A HIDE (JJ, 2026-10-13, liaison mode: "your update to the Lyra frame
  // stole the camera away from the outline"). `point outline --look` landed, the parent then OPENED the
  // canvas for the clip, and opening always flew to the latest EDITED frame (link-1) — an edit moving
  // the camera in the one mode where edits never may. So the last explicit target per lane is kept, and
  // opening lands on it; only an auto-focusing edit (never sent in liaison mode) supersedes it.
  let explicitTarget = null;   // {lane, el, margin}
  function rememberExplicit(el, margin) { explicitTarget = el ? { lane: live, el, margin } : null; }
  // spec 022: for a moment after the page (re)loads, the backlog is still arriving and sizing, so
  // each settle re-lands on the latest edited frame instead of the first frame's whole-board fit.
  let bootFocusUntil = Date.now() + 2500;
  // AUTO-FOCUS ZOOM CAP (spec 022, JJ 2026-09-25: "the camera flew to an empty space"). The contain
  // fit of spec 018 blows a SMALL frame up to 8x, so a three-line note filled the screen and a few
  // pixels of settling offset landed him in blank card. An automatic focus is a "here is the new
  // thing", not a close read: it frames the card at no more than AUTOFOCUS_MAX_K, so it arrives at
  // reading size with the board around it for context.
  const AUTOFOCUS_MAX_K = 1.25;
  function autoFocus(id, fly) {
    const el = liveCardEl(id);
    if (!portReady()) { focusWhenShown = true; return; }
    if (!el || !el.offsetWidth || !el.offsetHeight) return;
    focused = id;
    const box = boxOf(el);
    const t = camFor(box, 80);
    if (t.k > AUTOFOCUS_MAX_K) {
      const port = view.getBoundingClientRect();
      const k = AUTOFOCUS_MAX_K;
      t.k = k;
      t.x = port.width / 2 - (box.x + box.w / 2) * k;
      t.y = port.height / 2 - (box.y + box.h / 2) * k;
    }
    if (fly) { flyTo(t); } else { cam = t; apply(true); }
  }
  // The live lane's most recently created or edited frame (by the server's `updated` stamp; ink is
  // the human's own sketch, not something to be brought to).
  function latestCardId() {
    let best = null, t = -1;
    for (const [id, c] of cards || []) {
      if (c.el.dataset.kind === "ink") continue;
      if (String(id).startsWith("_")) continue;   // a `_status.md`-style background frame updates quietly; it is never "the latest thing"
      if ((c.updated || 0) > t) { t = c.updated || 0; best = id; }
    }
    return best;
  }
  // The parent page opened the canvas (spec 022): land on the latest edited frame. Same-origin only.
  // OPENING always lands on the latest frame (JJ, 2026-09-28: "is not scrolling to the file they
  // touched last") — even on a lane whose view he panned before, because opening is "show me what's
  // new". The message names the lane: it is posted 350ms after the parent's switch request, and when
  // the canvas's own `switch` event has not arrived yet, focusing now would frame the PREVIOUS lane's
  // cards and the new lane would then restore its parked view. So a mismatch waits for useLane.
  let focusLatestFor = null;
  function focusLatestOnOpen() {
    layout();
    const ex = explicitTarget;
    if (ex && ex.lane === live && ex.el && ex.el.isConnected) {   // the agent's explicit look wins
      pendingFocus = null;
      zoomTo(ex.el, ex.margin, false);
      return;
    }
    const latest = latestCardId();
    if (!latest) return;
    // retried by relayoutSoon while the card is still sizing (mermaid renders after this)
    pendingFocus = { id: latest, until: Date.now() + 2500 };
    autoFocus(latest, false);
  }
  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin || !e.data || e.data.type !== "cb-focus-latest") return;
    const lane = e.data.lane;
    if (lane && lane !== live) {
      focusLatestFor = lane;
      // …but not forever: when the parent already believed it was on this lane it sends no switch at
      // all, so if none arrives, he tapped THIS lane open — show it.
      setTimeout(() => { if (focusLatestFor === lane && live !== lane) useLane(lane); }, 1500);
      return;
    }
    focusLatestOnOpen();
  });
  // The canvas becoming VISIBLE (display:none -> shown) resizes the viewport from 0x0: replay any
  // framing that was refused while hidden.
  new ResizeObserver(() => {
    if (portReady()) repaintPending();   // the canvas was just shown: draw what arrived while hidden
    if (lookWhenShown && portReady()) {   // an explicit target wins over "the latest edit" (spec 037)
      const t = lookWhenShown;
      lookWhenShown = null;
      focusWhenShown = false;
      if (t.el && t.el.isConnected) zoomTo(t.el, t.margin, false);
      return;
    }
    if (focusWhenShown && portReady()) { focusWhenShown = false; focusLatestOnOpen(); }
  }).observe(view);
  // The LIVE lane's card for an id. `cardEl` searches every lane's canvas, and a hidden lane that
  // happens to hold the same id would answer first with a zero-size box.
  function liveCardEl(id) {
    const c = cards && cards.get(id);
    return c ? c.el : null;
  }
  function relayoutSoon() {
    clearTimeout(_relayoutTimer);
    _relayoutTimer = setTimeout(() => {
      layout();
      if (pendingFocus) {
        if (Date.now() < pendingFocus.until && liveCardEl(pendingFocus.id)) {
          autoFocus(pendingFocus.id, false);
          return;
        }
        pendingFocus = null;
      }
      // cam at the untouched default means he has neither panned nor been framed here yet. spec 022
      // (JJ, 2026-09-25: "whenever I come back to the canvas or open the canvas, the camera would be
      // focused on the last edited or created frame"): land on the most recently edited frame, not
      // the whole board. A panned/zoomed lane is left exactly as he left it.
      if (cards && cards.size && (Date.now() < bootFocusUntil
                                  || (cam.x === 0 && cam.y === 0 && cam.k === 1))) {
        const latest = latestCardId();
        if (latest) autoFocus(latest, false); else fit(false);
      }
    }, 60);
  }
  const RO = new ResizeObserver(() => relayoutSoon());

  function report() {
    // Tell the server where things ended up and what is on screen. The server
    // models no geometry — only the browser knows how big a rendered thing is,
    // and only the browser knows where the camera ended up.
    const port = view.getBoundingClientRect();
    const frames = {}, visible = [], partial = [], offscreen = [];
    for (const [id, { el }] of cards) {
      const g = { x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight };
      // spec 019 FR1: the EFFECTIVE min font — the smallest BODY text, at the scale a `look` would
      // contain this frame at on his screen — reported per frame beside the geometry, so the agent
      // that drew it learns whether it is readable without JJ having to look first. `fit_scale` is
      // spec 018's contain fit; `min_font_px` is (smallest canvas-space font) × that scale.
      const fs = fitScaleFor(g.w, g.h);
      if (fs != null) {
        g.fit_scale = Math.round(fs * 1000) / 1000;
        const mf = minFontPx(el.querySelector(".body"));
        if (mf != null) g.min_font_px = Math.round(mf * fs * 10) / 10;
      }
      frames[id] = g;
      const b = el.getBoundingClientRect();
      const inside = b.left >= port.left && b.right <= port.right
                  && b.top >= port.top && b.bottom <= port.bottom;
      const touches = b.right > port.left && b.left < port.right
                   && b.bottom > port.top && b.top < port.bottom;
      (inside ? visible : touches ? partial : offscreen).push(id);
    }
    fetch("/placed", {
      method: "POST", headers: { "Content-Type": "application/json" },
      // width/height are the MEASURED canvas area (spec 018 FR3/NFR1): the exact rect the fit above
      // uses, so the aspect an agent reads (derived server-side as width/height) is the same ratio
      // the camera fits to — never a second, inferable value that could disagree.
      body: JSON.stringify({ lane: live, frames,
                             viewport: { visible, partial, offscreen, scale: cam.k,
                                         width: Math.round(port.width),
                                         height: Math.round(port.height) } }),
    }).catch(() => { /* the surface works fine without the server knowing */ });
  }

  // AN HTML FRAME'S CSS STAYS IN ITS FRAME (2026-09-25). HTML frames are inserted raw, so a frame
  // authored as a standalone page — `<style>body { padding:28px; font:16px Roboto }</style>`, which is
  // what an agent writes for a kanban board or a design mock — restyled the WHOLE canvas document: the
  // leak padded <body> 28px, which is why JJ saw the Pen/Erase/Send bar sitting far from the canvas
  // corner, and it swapped the canvas font too. Every rule in the frame's own <style> tags is rewritten
  // to match only inside this card's body: `html`/`body`/`:root` become the frame itself (so the page's
  // own padding/background still apply — to the frame, as its author meant), everything else becomes a
  // descendant of it. NOT a shadow root: `point`, `cue` and the legibility report find frame elements
  // by id in THIS document, and a shadow boundary would hide them.
  // LINKS IN A FRAME OPEN OUTSIDE THE PAGE (JJ, 2026-10-09). A plain <a href> in a markdown or html
  // frame navigated the canvas document itself, so tapping a Google Drive link replaced the whole
  // page with Drive and dropped the voice UI. Every outward link opens a new tab instead; in-frame
  // anchors (#…) keep their default.
  function openLinksOutside(body) {
    for (const a of body.querySelectorAll("a[href]")) {
      if (a.getAttribute("href").startsWith("#")) continue;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
  }

  let frameScopeSeq = 0;
  function scopeFrameStyles(body) {
    const styles = body.querySelectorAll("style");
    if (!styles.length) return;
    const tag = body.dataset.cbscope || (body.dataset.cbscope = "f" + (++frameScopeSeq));
    const scope = '[data-cbscope="' + tag + '"]';
    const one = (sel) => {
      const s = sel.trim();
      if (!s) return s;
      // (backslashes doubled: this JS lives in a plain Python string, PAGE)
      const rest = s.replace(/^((html|body|:root)(?![\\w-])\\s*)+/i, "");
      if (rest === s) return scope + " " + s;             // an ordinary selector: inside the frame
      if (!rest) return scope;                            // `body` / `html` / `:root` alone: the frame
      return /^[.#\\[:]/.test(rest) ? scope + rest        // `body.dark` -> the frame, with that class
                                   : scope + " " + rest;  // `body .x` / `html > p` -> inside the frame
    };
    const fix = (rules) => {
      for (const r of rules) {
        if (r.selectorText != null) {
          try { r.selectorText = r.selectorText.split(",").map(one).join(", "); } catch (_) {}
        } else if (r.cssRules) {
          fix(r.cssRules);                                // @media / @supports / @layer blocks
        }
      }
    };
    for (const st of styles) { try { if (st.sheet) fix(st.sheet.cssRules); } catch (_) {} }
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
        // JJ, 2026-09-24: the error shows on the canvas but the AGENT that drew it never learned and
        // gave up, falling back to markdown. RETURN it so `place()` reports the failure to the server
        // regardless of live lane — the real case is an agent drawing while JJ is on ANOTHER lane,
        // where the live-lane-only `report()` never runs, so a silent "ok" was all the agent saw.
        return String(err && err.message || err);
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
        openLinksOutside(body);
        // FENCED MERMAID INSIDE MARKDOWN (JJ, 2026-09-29: "agents are writing a markdown frame with
        // Mermaid inside of it, and the mermaid doesn't render"). marked turns ```mermaid into a plain
        // <pre><code class="language-mermaid"> block and stops there, so the diagram showed as its
        // source. Render each block with the same mermaid.render a `.mmd` frame uses, in place. A bad
        // block shows its error where the diagram would be, and the first error is RETURNED so the
        // agent learns it, as for a `.mmd` frame (see the mermaid branch above).
        let firstErr = null;
        for (const code of [...body.querySelectorAll("pre > code.language-mermaid")]) {
          const pre = code.parentElement;
          try {
            const { svg } = await mermaid.render("m" + (++seq), code.textContent);
            const fig = document.createElement("div");
            fig.className = "md-mermaid";
            fig.innerHTML = svg;
            pre.replaceWith(fig);
          } catch (err) {
            const msgText = String(err && err.message || err);
            code.textContent = msgText + "\\n\\n" + code.textContent;   // doubled backslashes: PAGE is a Python string, so a single one becomes a real line break in the JS
            if (!firstErr) firstErr = msgText;
          }
        }
        if (firstErr) return firstErr;
      } else {
        body.innerHTML = '<pre class="md"></pre>';
        body.firstChild.textContent = msg.content;
      }
    } else if (msg.kind === "html" || msg.kind === "svg" || msg.kind === "ink") {
      // `ink` (spec 015) is the human's strokes, already an svg in canvas-local coords — rendered
      // like any inline svg; the card CSS makes it a transparent, click-through overlay so it sits
      // ON TOP of whatever it was drawn over.
      body.innerHTML = msg.content;
      if (msg.kind === "html") scopeFrameStyles(body);
      openLinksOutside(body);
    } else if (msg.kind === "text") {
      body.innerHTML = "<pre></pre>";
      body.firstChild.textContent = msg.content;
    } else if (msg.kind === "image") {
      // spec 022: an image the agent dropped into its lane folder. The frame carries the URL the
      // server serves it from (content-hashed, so an overwrite re-renders), never the bytes.
      const img = document.createElement("img");
      img.src = msg.content;
      img.alt = msg.id || "";
      img.draggable = false;   // a drag over a picture pans the canvas (see the CSS rule)
      img.style.maxWidth = "100%";
      img.style.display = "block";
      img.onload = () => relayoutSoon();
      body.innerHTML = "";
      body.appendChild(img);
    } else {
      body.innerHTML = "";
    }
    unshrink(body);
    highlight(body);
  }

  // Belt and braces for the image drag: an <img> inside an html frame isn't created by paint(), so
  // stop any native drag that starts inside a card (Firefox ignores -webkit-user-drag).
  document.addEventListener("dragstart", e => { if (e.target.closest && e.target.closest(".card")) e.preventDefault(); }, true);

  // ---- copy buttons (JJ, 2026-10-01) ------------------------------------------------------------
  // "content" copies what the agent wrote — the frame's SOURCE (markdown, mermaid, html, svg, json,
  // text), which is what pastes usefully elsewhere — or, for an image frame, the picture itself.
  // "path" copies the full path of the file the frame renders.
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (_) {}
    // Fallback for a context without the async clipboard (an iframe policy, plain http off-box).
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch (_) {}
    ta.remove(); return ok;
  }
  async function copyImage(url) {
    try {
      const blob = await (await fetch(url)).blob();
      let png = blob;
      if (blob.type !== "image/png") {   // the clipboard takes PNG everywhere; convert anything else
        const bmp = await createImageBitmap(blob);
        const cv = document.createElement("canvas"); cv.width = bmp.width; cv.height = bmp.height;
        cv.getContext("2d").drawImage(bmp, 0, 0);
        png = await new Promise(res => cv.toBlob(res, "image/png"));
      }
      await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
      return true;
    } catch (_) { return copyText(new URL(url, location.href).href); }
  }
  function flash(btn, ok) {
    const was = btn.textContent;
    btn.textContent = ok ? "copied" : "failed"; btn.classList.toggle("done", ok);
    setTimeout(() => { btn.textContent = was; btn.classList.remove("done"); }, 1200);
  }
  function addCopyTools(bar, c) {
    const tools = document.createElement("span");
    tools.className = "tools";
    const mk = (label, title, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = label; b.title = title;
      // Stop the press from starting a pan or a frame drag on the card underneath.
      b.addEventListener("pointerdown", e => e.stopPropagation());
      b.addEventListener("click", async e => { e.stopPropagation(); flash(b, await fn()); });
      tools.appendChild(b);
    };
    mk("copy", "Copy this frame's content", () => {
      const m = c.src || {};
      return m.kind === "image" ? copyImage(m.content) : copyText(String(m.content ?? ""));
    });
    if (c.path) mk("path", "Copy the path of this frame's file", () => copyText(c.path));
    bar.appendChild(tools);
  }

  // Frames whose rendering needs real layout (mermaid, alone or fenced inside markdown).
  function needsLayout(msg) {
    return msg.kind === "mermaid" ||
      (msg.kind === "markdown" && /```\\s*mermaid/.test(String(msg.content || "")));
  }
  // A DIAGRAM'S SYNTAX, CHECKED WITHOUT DRAWING IT (atlas, 2026-10-06, with JJ's go: a frame that failed
  // to parse was never reported to the agent that wrote it). Drawing waits until the lane is on screen
  // (see place()), and with the canvas closed — the fleet home — that is never, so the error JJ later saw
  // had reached nobody. `mermaid.parse` needs no layout, so the verdict is reported the moment the frame
  // arrives. Covers a `.mmd` frame and every ```mermaid block in a markdown one; null when all parse.
  async function diagramSyntaxError(msg) {
    const blocks = msg.kind === "mermaid" ? [String(msg.content || "")]
      : [...String(msg.content || "").matchAll(/```\\s*mermaid\\s*\\n([\\s\\S]*?)```/g)].map((m) => m[1]);
    for (const src of blocks) {
      try { await mermaid.parse(src); }
      catch (err) { return String(err && err.message || err); }
    }
    return null;
  }
  function reportRendered(lane, id, error) {
    // Fire-and-forget — the surface still works if the server never learns.
    fetch("/rendered", { method: "POST", headers: { "Content-Type": "application/json" },
                         body: JSON.stringify({ lane, id, error: error || null }) }).catch(() => {});
  }
  // Paint any deferred diagram on the LIVE lane now that it can be measured (see place()).
  async function repaintPending() {
    for (const c of (cards || new Map()).values()) {
      if (!c.pending || !c.body.getClientRects().length) continue;
      const msg = c.pending;
      c.pending = null;
      await place(msg);
    }
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
      RO.observe(el);   // re-pack when this card's content finishes rendering (see relayoutSoon)
    }
    if (msg.at) c.at = msg.at;
    if (msg.updated) c.updated = msg.updated;   // spec 022: which frame is the latest edit
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
    c.src = msg; c.path = msg.path || msg.file || "";
    if (msg.kind !== "ink") addCopyTools(bar, c);
    paintAges();
    // A background lane must be invisible until it raises a hand — so it does
    // not touch the title, the count, or the camera.
    if (lane === live) {
      title.textContent = msg.title || id;
      requestAnimationFrame(() => layout());
    }
    // A DIAGRAM IS NOT DRAWN WHILE IT CANNOT BE SEEN (JJ, 2026-09-29: "after opening the canvas … the
    // mermaid inside the markdown frame is not rendering"). mermaid lays text out with real
    // measurements, and inside a display:none lane — or the whole canvas iframe before it is opened —
    // there are none, so it throws "Could not find a suitable point for the given distance" and the
    // frame shows the error instead of the diagram. Defer it: keep the message on the card and paint it
    // the moment its lane is visible (repaintPending, from useLane and from the canvas being shown).
    if (needsLayout(msg) && !c.body.getClientRects().length) {
      reportRendered(lane, id, await diagramSyntaxError(msg));   // known now, even unseen
      c.pending = msg;
      c.body.innerHTML = '<pre class="md"></pre>';
      c.body.firstChild.textContent = "diagram renders when this lane is on screen";
      return;
    }
    c.pending = null;
    const renderError = await paint(c.body, msg);
    if (needsLayout(msg)) {
      // Report the render outcome for a diagram frame REGARDLESS of live lane (JJ, 2026-09-24), so
      // the drawing agent learns it FAILED instead of seeing a silent "ok" on a blank frame. null
      // clears a prior failure on a good re-render. Markdown with ```mermaid blocks counts too: its
      // error was returned by paint() and then dropped here (2026-10-06).
      reportRendered(lane, id, renderError);
    }
    if (lane === live) requestAnimationFrame(() => layout());
    paintLanes();
  }

  function drop(id, lane) {
    const canvas = canvasFor(lane || live);
    const c = canvas.cards.get(id);
    if (!c) return;
    try { RO.unobserve(c.el); } catch (_) {}
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

    // A frame id wins: `point costs` means that card, not something inside one. The LIVE lane's card
    // first: `cardEl` searches every lane, and a hidden lane holding the same id answers with a
    // zero-size box the camera cannot move to (spec 037).
    const card = liveCardEl(String(selector).replace(/^[#.]/, "")) || cardEl(selector);
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

    // Exclude EDGES so a subgraph CONTAINER wins its name. Mermaid v11 prefixes
    // every id with `mermaid-<ts>-`, so the edge `gate-->kept` is
    // `mermaid-<ts>-L_gate_kept_0` (doesn't start with "L") with class
    // `flowchart-link` (not `edgePath`) — it slipped BOTH old filters, sat earlier
    // in the DOM than the `...-kept` cluster, and `point kept` hit the arrow, not
    // the box (JJ, 2026-09-03). Drop `flowchart-link` and any `L_`-segment id too.
    const rest = [...root.querySelectorAll("[id]")].filter(
      n => !/^L[-_]/.test(n.id) && !/(?:^|[-_])L[-_]/.test(n.id)
        && !n.classList.contains("edgePath") && !n.classList.contains("flowchart-link"));

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
      rememberExplicit(el, 80);
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
    // Both `?look=` and `?point=` (spec 020) POLL for their target rather than firing once at a
    // fixed fraction of `settle`. A busy lane (several other frames already on it, real magnus
    // evidence 2026-09-16) means the just-drawn frame's card can exist before its CONTENTS finish
    // rendering — a mermaid diagram lays out asynchronously — so a single fixed-time shot at the
    // target gambled on a race it does not always win: `point` fired before the node existed, found
    // nothing, and applied no highlight at all. Polling applies each one the MOMENT it resolves.
    // The deadline is `settle * 2`, not `settle`: `shot.capture` (the Python caller) waits ONE
    // `settle` window for `/settle` to answer before `load` fires, THEN — with no `wait_selector`,
    // which a `?point=` target never has (there is no plain CSS selector for a fuzzy mermaid-node
    // match) — waits a SECOND full `settle_ms` after load before the shutter actually fires. Polling
    // to only one `settle` window (this render's earlier bug) gives up a whole window before the
    // picture is even taken. The very last attempt at the deadline still runs unconditionally — so a
    // genuinely bad selector still reports itself honestly (no highlight), rather than the poll
    // just going quiet forever; and once the browser is torn down for the screenshot, the poll's
    // timers simply stop firing, so there is no cost to a generous bound.
    const shotDeadline = performance.now() + Math.max(600, settle * 2);
    const WANT = new URLSearchParams(location.search).get("look");
    if (WANT) {
      const tryLook = () => {
        // `focused` is set on every attempt, not only a resolving one: `point` (below) scopes ITS
        // OWN search to `cardEl(focused)` FIRST (`findEl`'s "the focused frame, before anything
        // else on the canvas" rule) — the card usually exists well before its contents finish
        // rendering, so setting this early is what lets a `[point:]` mark resolve against the
        // FRAME JUST DRAWN instead of matching some unrelated node elsewhere on a busy canvas
        // (the second, compounding half of the same live failure: an unscoped stage-wide search).
        focused = WANT;
        const el = cardEl(WANT) || findEl(WANT);
        if (el) { zoomTo(el, 80, false); return; }   // no flight: the shutter is not waiting
        if (performance.now() < shotDeadline) setTimeout(tryLook, 100);
      };
      setTimeout(tryLook, 200);
    }

    // `?point=<selector>` (spec 020): apply ONE highlight LOCALLY, for the verify capture only.
    // Same reasoning as `?look=` above — a `point` from a background lane is refused server-side
    // (live-lane-only), and this render may be pinned to a lane that is not live. Calling the
    // page's own `point()` directly never touches the server's `_point` state or fans an SSE event,
    // so it cannot be seen by anyone else and cannot fight a highlight already showing for real.
    // Starts after `look` above so `focused` is already scoped by the time this searches.
    const WANT_POINT = new URLSearchParams(location.search).get("point");
    if (WANT_POINT) {
      const tryPoint = () => {
        if (findEls(WANT_POINT).length) { point(WANT_POINT); return; }
        if (performance.now() < shotDeadline) { setTimeout(tryPoint, 100); return; }
        point(WANT_POINT);   // last attempt regardless — an honest empty point, not silence
      };
      setTimeout(tryPoint, 300);
    }

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
  // A pushed `reload` (POST /reload, spec 013) re-runs THIS iframe's JS in place. The canvas owns
  // its own reload the same way it owns the version self-reload above — because the audio shell
  // around it must NEVER reload for a canvas change, or the audio blips. `location.reload()` here
  // reloads only this iframe, so a page.py edit costs nothing audible. A `ui`-targeted event is the
  // one to skip: the UI frame that holds this iframe is reloading and recreates it anyway (spec 023).
  // A `page` (shell) change waits for the orb to go off, so this reloads now rather than wait for it.
  es.addEventListener("reload", e => {
    let target = "canvas";
    try { target = (JSON.parse(e.data || "{}").target) || "canvas"; } catch (_) {}
    if (target !== "ui") location.reload();
  });
  es.addEventListener("frame", async e => {
    const msg = JSON.parse(e.data);
    const lane = msg.lane || "main";
    await place(msg);
    // AUTO-FOCUS FOLLOWS THE LATEST EDIT (spec 022 FR7). The server marks a frame event `focus` when
    // an agent's file was just created or edited on the LIVE lane; the camera flies to it (the
    // zoom-out, pan, zoom-in of `flyTo`) once the card has rendered and been laid out — which is why
    // it rides on the frame event rather than a separate `look` that would race the paint. Off-lane
    // the server holds the look instead (spec 017) and it replays on switch-back.
    if (msg.focus && lane === live && !PIN) {
      const id = msg.id || "main";
      // The card's size can still be settling (a mermaid SVG unshrinking, a webfont), and a camera
      // framed on a half-sized card lands zoomed into empty space — JJ, 2026-09-25: "I see spec
      // twenty two, but it's empty." So fly there once it has laid out, and keep the focus PENDING
      // for a short window: every re-layout in that window (relayoutSoon) re-frames it on its
      // final size, without the flight.
      pendingFocus = { id, until: Date.now() + 2500 };
      if (explicitTarget && explicitTarget.lane === lane) explicitTarget = null;   // the edit takes over
      // Each settle yields to an explicit camera move made after this edit (spec 037).
      const at = Date.now();
      setTimeout(() => { if (lastExplicit < at) autoFocus(id, true); }, 250);
      // A second, flight-free settle: the sticky placer may still move a just-sized card once.
      setTimeout(() => { if (lastExplicit < at) autoFocus(id, false); }, 1000);
      return;
    }
    // First frame on an empty canvas: fit it, so a single drawing still fills
    // the window the way it did before there was a canvas. Only for the live
    // lane — a background lane must never move the view.
    if (lane === live && cards.size === 1 && Date.now() >= bootFocusUntil)
      requestAnimationFrame(() => fit(false));
  });
  es.addEventListener("relayout", e => {
    // spec 022 FR6's on-demand re-pack: forget every pinned position on the lane and pack it afresh
    // (the new positions are pinned again and reported, so the tidy board is the sticky one).
    const lane = JSON.parse(e.data).lane || "main";
    const canvas = canvasFor(lane);
    for (const c of canvas.cards.values()) if (c.el.dataset.kind !== "ink") c.at = null;
    if (lane === live) { layout(); fit(true); }
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
      explicitCamera();
      const el = findEl(msg.selector);
      rememberExplicit(el, 24);
      if (el) zoomTo(el);
    }
  });
  es.addEventListener("look", e => {
    const msg = JSON.parse(e.data);
    if (msg.lane && msg.lane !== live) return;  // server already refused it
    explicitCamera();
    // `look --all` is a deliberate step back to the whole canvas, so it drops
    // the scope with the view: nothing is focused, and a selector may match
    // anywhere again.
    if (msg.all || !msg.id) { focused = null; fit(true); return; }
    const el = liveCardEl(msg.id) || cardEl(msg.id) || findEl(msg.id);
    if (el) {
      // The camera and the search scope move together. This is the rule:
      // focus a frame, THEN highlight inside it.
      if (liveCardEl(msg.id) || cardEl(msg.id)) focused = msg.id;
      rememberExplicit(el, 80);
      zoomTo(el, 80, true);
    }
  });
  es.addEventListener("zoom", e => {
    const msg = JSON.parse(e.data);
    if (msg.lane && msg.lane !== live) return;  // server already refused it
    explicitCamera();
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
