---
id: "015"
title: A canvas he can draw on
status: pending
blocked_by: []
blocks: []
---

# A canvas he can draw on

## Overview

The shared canvas is **one-way** today: an agent draws (`set` / `point` / `cue` / `chart`) and JJ
watches. Intent flows agent → canvas → JJ's eyes, and never back. So when JJ wants to convey a
*spatial* idea — a UX mockup, a layout, a diagram — he has to describe it in words, and a paragraph
of speech is a low-bandwidth way to hand over something a shaky rectangle would carry instantly.

This spec makes the canvas **two-way**: JJ can point at it and draw on it, and the agent can read
what he drew. JJ, 2026-09-09: *"make the canvas bidirectional so that I can also point and I can
also draw and maybe write — but mainly point and draw. I would like to convey ideas for a UX by
drawing a mockup in the UI, or a diagram, in a way that you can understand and then you can polish.
That improves the bandwidth for my intent sharing."* And on how he'd reach it: *"a different URL for
the canvas — a URL I can render on my phone, and since my phone has a touch screen, or my tablet, I
can use my finger or a pen to draw there."*

The point half is nearly free: the canvas already addresses every element by name (spec 011's
deixis — the agent points at a node by id), so the same channel run **inbound** lets JJ tap an
element and the agent learn which one he meant. The draw half is new but small at v0, because the
agent can already **read a picture**: `shot` screenshots the canvas and the agent interprets the
image. A freehand sketch handed over as an image is a thing the agent can already understand — the
work is capturing the strokes and getting them onto a touch device, not teaching the agent to see.

A reach note that shapes the design: **drawing does not need the microphone**, so it is not bound by
the secure-context rule that forces the voice page onto an https tunnel. A drawing-only canvas view
can be opened on a phone or tablet over a plain LAN address — getting the canvas onto his hand is
strictly easier than getting voice there.

> **Completion rule:** This spec is not complete until all acceptance criteria are verified through
> the testing methodology below — Playwright integration for the touch/draw UI, `shot` for the
> render-and-read path, and the small `manual` phone core. Build-only verification is insufficient;
> iterate until verification passes.

> **Testing method:** Playwright-driven integration (the pattern of `scripts/uitest.py` /
> `scripts/e2e.py`) for the `/canvas` drawing surface and the submit path; `shot` screenshots for the
> render-and-read path; `?mock=` for deterministic UI states; and a small irreducible `manual` core
> for real finger/pen on a real touchscreen, which a headless pointer cannot fully stand in for.

## Goals

- Raise the bandwidth of JJ → agent intent-sharing for anything **spatial** — a rough sketch or a
  point should carry more, faster, than describing it in speech.
- Let JJ **direct the agent's attention** on the canvas: point at an element and have the agent
  know which one ("polish *this* box", "the arrow *here* is wrong").
- Let JJ **draw** a mockup or diagram freehand on a touchscreen device, and have the agent read it
  and act on it — the "his rough → agent polishes" loop.
- Let JJ **mark up the agent's own diagrams** — draw directly on a frame the agent produced to give
  feedback in place ("this arrow is wrong", circle *that* box), the highest-bandwidth correction
  there is.

## Requirements

### Functional Requirements

- **FR1 (point, inbound).** JJ can select/tap an element on the canvas from his device, and the
  agent learns *which named element* he indicated on its next `watch` (or an equivalent read) —
  the deixis channel of spec 011 run in reverse.
- **FR2 (draw).** JJ can draw freehand strokes on the shared canvas from a **touchscreen** device
  using a finger or a pen. Strokes are freeform ink, not snapped to shapes, and can be laid **over
  the agent's existing frames** as well as on empty canvas — see FR2a.
- **FR2a (annotate over the agent's frames).** Because the strokes land on the *shared* canvas, JJ
  can draw **on top of a frame the agent drew** — circle a box, cross out an arrow, scribble a note
  beside it — which is feedback delivered *directly on the diagram*. This is the richest form of
  FR1's pointing: a mark on the thing beats a tap on it. JJ, 2026-09-09: *"I could even draw on top
  of your frames … that can help me point or give feedback directly on the diagrams. That would be
  amazing."*
- **FR3 (the agent reads it).** The agent obtains what JJ drew as an **image it can interpret** —
  it `shot`s the frame and reads the picture — so it can understand the sketch and respond: polish
  it into a clean frame (mermaid/SVG), ask about it, or act on it. JJ, 2026-09-09: *"you would see
  this by screenshotting the frame."*
- **FR4 (reach).** The drawing surface is reachable on a phone/tablet as a view that does **not**
  require a microphone secure-context — a plain LAN address is sufficient for drawing-only, so the
  device does not need the voice tunnel to draw.
- **FR5 (a minimal pen).** The tools are deliberately spare — *"whatever is fastest"*: a pen, an
  **eraser**, and **a couple of colors only if they are cheap to add**. No pressure sensitivity.

### Non-Functional Requirements

- **NFR1 (local-only).** Strokes and the surface stay on JJ's machine, exactly like the rest of
  Command Bridge — nothing about drawing leaves the box.
- **NFR2 (no second render path).** The drawing view reuses the existing canvas render/`/events`
  machinery rather than forking a second page that can drift, in the spirit of `?mock=` and the
  one-server-one-page rule (spec 003).

### Technical Constraints

- **TC1.** The agent has no continuous view of the canvas; it reads on demand. The read path is the
  existing screenshot mechanism — the sketch must land somewhere `shot` (or its equivalent) can
  capture, not in a channel only the browser holds.
- **TC2.** Two clients may be on one canvas at once (laptop *viewing*, phone *drawing*). The design
  must decide how a stroke made on one surfaces on the other (see Open Questions).
- **TC3.** A touch device draws with pointer/touch events; a plain-http LAN page can use them (they
  are not gated on secure context the way `getUserMedia` is). This is what makes FR4 cheaper than
  voice reach.

## Key Decisions

All settled with JJ in review on 2026-09-09; the four open questions this draft carried are now
folded in as decisions.

- **KD1 — separate drawing VIEW, one shared canvas.** JJ asked for "a different URL … I can render
  on my phone." The decision: a dedicated **drawing route/view** the touch device opens, backed by
  the **same** canvas state (not a second, disconnected canvas). The phone is a drawing surface onto
  the shared canvas; the laptop stays the viewing surface. His drawings **persist as frames on that
  shared canvas** — *"my drawings persist in the shared canvas you and me have."*
- **KD2 — freeform ink (settled).** Freeform is enough: the agent screenshots the sketch and its
  own vision does the interpreting, then it produces the clean version in mermaid/SVG. *"Freeform is
  fine because you're going to screenshot and then you can use Mermaid or whatever or SVG to produce
  what you understood from my input."* Snapping strokes to shapes and handwriting-to-text OCR stay a
  **later spec** — not needed for the bandwidth win.
- **KD3 — the agent reads via `shot`, not by parsing strokes (settled).** Reuse the existing
  screenshot path (the agent interprets the image) rather than build a stroke parser — the thing
  that makes FR3 small.
- **KD4 — his input and the agent's rendering are SEPARATE FRAMES (settled).** His raw sketch is one
  frame; the agent's cleaned-up interpretation is another; both live on the one shared canvas, side
  by side, so the "rough → polished" pair is visible together. And because it is one shared surface,
  he can lay strokes **over** the agent's frames to annotate them (FR2a).
- **KD5 — draw-then-submit, not a live mirror (settled).** A stroke does not stream to the laptop as
  it is drawn; JJ finishes and hands the frame over. *"Draw then submit is fine."* Far cheaper than a
  real-time two-way mirror, and it matches "sketch → hand it over → you polish."
- **KD6 — a minimal pen, chosen for speed (settled).** Pen + eraser; a couple of colors only if they
  are cheap; **no pressure**. *"Whatever is fastest."*
- **KD7 — point (FR1) is the first slice.** It is nearly free (addressable nodes already exist) and
  high-value, so it can ship ahead of, or alongside, the draw slice.

## Contracts

These are the interfaces the feature adds; the *how* (function names, file layout) the implementer
derives from `command_bridge/canvas/`.

- **`GET /canvas`** *(exists)* — the standalone canvas page (`handle_canvas_page` → `canvas.render()`,
  the same document the voice page embeds as an iframe). This is the surface JJ opens on the touch
  device, with `?token=`. It gains the drawing layer below. No new route is needed for the surface
  itself.
- **`ink` frame kind** *(new)* — a frame whose content is JJ's strokes as **SVG paths**. It renders
  on the canvas like any frame (so both JJ and the agent see the sketch), persists with the canvas,
  and is captured by `shot` exactly like a `mermaid`/`svg` frame. Stored via the existing frame path
  (`/canvas/frame`, kind `ink`) so it rides all the existing per-lane frame machinery.
- **Submit (draw-then-submit)** *(new inbound)* — a `send` control on `/canvas` POSTs the accumulated
  strokes; the server stores them as an `ink` frame on the **live** lane and **surfaces a
  canvas-sourced turn to the agent** (see below). Mirrors the existing inbound POST pattern
  (`/placed`, `/inspected`) rather than inventing a transport.
- **Inbound point (FR1)** *(new inbound)* — tapping a rendered element on `/canvas` POSTs that
  element's node id; the server surfaces a canvas-sourced turn naming the node.
- **Canvas-sourced turn** *(the surfacing decision)* — an inbound canvas action reaches the agent the
  **same way speech does: as a turn its `watch` returns**, tagged as canvas-sourced (e.g. a
  `source: "canvas"` turn whose text names the frame or node: *"drew on lane magnus, frame ink-7"* /
  *"pointed at watch-logic"*). One loop handles speech and canvas intent alike; the agent then `shot`s
  the frame and acts. *Rationale: the whole agent contract is `watch` → turns → act; a second
  inbound channel the agent has to poll would fork that.*
- **Reach** — a drawing-only `/canvas` never calls `getUserMedia`, so it is **not** gated on a secure
  context and works over a plain-http LAN address; the **token still gates it**, and the CIDR
  allowlist must admit the LAN (or a tunnel fronts it, as voice does today).

## Implementation Tasks

Ordered so the first testable slice is the whole loop end-to-end, then the additions.

- [ ] **T1 — `ink` frame kind.** Store and render an SVG-strokes frame; confirm `shot` captures it.
- [ ] **T2 — drawing layer on `/canvas`.** Pointer/touch strokes over the canvas (including over
  existing frames), a **pen** and an **eraser**, and a **send** control. Freeform paths; no pressure.
- [ ] **T3 — submit path.** `send` POSTs the strokes → an `ink` frame on the live lane, and the server
  surfaces a canvas-sourced turn to the agent.
- [ ] **T4 — inbound point.** Tapping a rendered element POSTs its node id → a canvas-sourced turn.
- [ ] **T5 — reach.** `/canvas` served on a LAN bind, token-gated, drawing-only; documented in
  `status.phone` / `describe` so the phone URL is discoverable.
- [ ] **T6 — colors.** A small fixed palette (2–3 colors), only if it stays cheap (KD6).
- [ ] **T7 — agent loop.** On a canvas turn: `shot` the frame, read it; for a sketch, produce a clean
  frame (mermaid/SVG) as a *separate* frame; for a point, act on the named node.

## Acceptance Criteria

### Core loop (the MVP slice)
- [ ] **AC1** — a stored `ink` frame renders its strokes on `/canvas`. *(kittest-snapshot: `shot` the
  page, the strokes appear.)*
- [ ] **AC2** — `shot` of a lane holding an `ink` frame returns an image that contains the strokes,
  readable by the agent. *(integration.)*
- [ ] **AC3** — pointer/touch strokes on `/canvas` draw a path; the eraser removes strokes.
  *(integration via Playwright pointer events; plus `manual` for real finger/pen — the irreducible.)*
- [ ] **AC4** — `send` stores an `ink` frame on the live lane AND the agent's next `watch` returns a
  `source: "canvas"` turn naming that frame. *(integration.)*
- [ ] **AC5** — strokes drawn on top of an existing frame are captured in the submitted `ink` frame,
  i.e. the annotation lands on the shared canvas. *(integration.)*

### Point + reach
- [ ] **AC6** — tapping a rendered element surfaces a `source: "canvas"` turn naming that node on the
  agent's `watch`. *(integration.)*
- [ ] **AC7** — `/canvas` loads and accepts drawing over a plain-http LAN address with the token and
  **never requests the microphone**; a wrong/absent token is refused. *(integration + `manual` on a
  real phone.)*

### The polish loop
- [ ] **AC8** — given a submitted sketch, the agent `shot`s it and produces a clean frame from it that
  coexists with the raw `ink` frame on the same lane. *(manual — the interpretation is the agent's
  vision; assert the `shot` happened and a distinct rendered frame was produced.)*

## Testing Approach

### Validation Steps
1. Unit/integration for the `ink` frame kind: store → render → `shot` (T1/AC1/AC2), no browser.
2. Playwright drives `/canvas` (the `scripts/uitest.py` / `e2e.py` pattern): synth pointer strokes,
   eraser, `send`; assert the `ink` frame and the canvas turn (T2–T4/AC3–AC5).
3. Token/reach: load `/canvas` with and without the token over a non-loopback bind; assert no
   `getUserMedia` call (AC7).
4. `manual` pass on a real phone/tablet: finger and pen actually draw, `send` lands, the agent reads
   it — the part a headless pointer cannot certify.

### Test Cases
| Input | Expected |
|-------|----------|
| Draw two strokes, `send` | one `ink` frame on the live lane; a `source: canvas` turn naming it |
| Draw over frame `watch-logic`, `send` | the `ink` frame captures the overlay; turn names the lane |
| Tap element `watch-logic` on `/canvas` | a `source: canvas` turn: "pointed at watch-logic" |
| Open `/canvas` with no token over LAN | refused; the mic is never requested |
| `shot` a lane with an `ink` frame | image contains the strokes |

## Out of Scope

- **Structured shape recognition / snapping and handwriting-to-text OCR** (KD2) — a later spec.
- **A real-time two-way mirror** — the laptop does not see strokes as they are drawn; v0 is
  draw-then-submit (KD5).
- **Pen pressure sensitivity** (KD6) — a plain pen, an eraser, and at most a couple of cheap colors.
- **Typed/written text on the canvas** — JJ's "maybe write" was tentative and secondary to "mainly
  point and draw"; text is deferred so it cannot pull the v0 wide.
- **The Command Bridge UI redesign** (orbs → a grid of labeled boxes, lanes moved over the
  transcript, more canvas height) — that is a **separate spec (016)**, and notably it is the *first
  thing JJ intends to draw* using this one. This spec builds the tool; that spec is a use of it.
- **Any change to the agent → canvas verbs** (`set`/`point`/`cue`/`chart`) — this adds the inbound
  direction, it does not touch the outbound one.

## References

- `📦 Command Bridge` — project node: the two-way-canvas idea, the bandwidth framing, and JJ's
  verbatim words this spec came from.
- `specs/011-speech-synced-deixis.md` — the agent's **outbound** point/deixis; FR1 is its inbound twin.
- `specs/003-one-server-one-page.md` — the one-page UI + `/events` machinery NFR2/KD1 reuse.
- The canvas verbs and the `shot` read path — [[Tunnel Vision Guide]] (behaviour) and
  `command-bridge describe`.
