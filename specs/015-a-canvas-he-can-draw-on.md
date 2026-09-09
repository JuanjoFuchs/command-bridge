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
> the repo's testing methodology (`kittest` for the touch/draw UI, `integration` for the
> capture-and-read path). Build-only verification is insufficient. The agent must iterate until
> verification passes.

> **Mode note:** This is a strategist **metaspec** — the vision, requirements, constraints, key
> decisions and open questions. The repo implementer refines it into full Implementation Tasks,
> Acceptance Criteria and Testing Approach against the actual canvas code, after JJ's review and
> after the open questions below are settled.

## Goals

- Raise the bandwidth of JJ → agent intent-sharing for anything **spatial** — a rough sketch or a
  point should carry more, faster, than describing it in speech.
- Let JJ **direct the agent's attention** on the canvas: point at an element and have the agent
  know which one ("polish *this* box", "the arrow *here* is wrong").
- Let JJ **draw** a mockup or diagram freehand on a touchscreen device, and have the agent read it
  and act on it — the "his rough → agent polishes" loop.

## Requirements

### Functional Requirements

- **FR1 (point, inbound).** JJ can select/tap an element on the canvas from his device, and the
  agent learns *which named element* he indicated on its next `watch` (or an equivalent read) —
  the deixis channel of spec 011 run in reverse.
- **FR2 (draw).** JJ can draw freehand strokes on the canvas from a **touchscreen** device using a
  finger or a pen. Strokes are freeform ink, not snapped to shapes.
- **FR3 (the agent reads it).** The agent can obtain what JJ drew as an **image it can interpret**
  (via the existing `shot` path or an equivalent), so it can understand the sketch and respond —
  polish it into a real frame, ask about it, or act on it.
- **FR4 (reach).** The drawing surface is reachable on a phone/tablet as a view that does **not**
  require a microphone secure-context — a plain LAN address is sufficient for drawing-only, so the
  device does not need the voice tunnel to draw.
- **FR5 (short text, optional at v0).** JJ can place a short text label on the canvas ("mainly point
  and draw", so text is the lowest-priority of the three and may slip to a follow-on).

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

- **KD1 — separate drawing VIEW, one shared canvas.** JJ asked for "a different URL … I can render
  on my phone." The decision: a dedicated **drawing route/view** the touch device opens, backed by
  the **same** canvas state (not a second, disconnected canvas). The phone is a drawing surface onto
  the shared canvas; the laptop stays the viewing surface.
- **KD2 — freeform ink at v0, structured shapes later.** The v0 hands the agent a *picture* of a
  freehand sketch and lets the agent's vision do the interpreting. Snapping strokes to boxes/arrows,
  or OCR-ing handwriting, is a real feature but a **later spec** — it is not needed to get the
  bandwidth win.
- **KD3 — the agent reads via `shot`, not by parsing strokes.** Reuse the mechanism that already
  exists (screenshot → the agent interprets the image) rather than build a stroke parser. This is
  what makes FR3 small.
- **KD4 — point (FR1) is the first slice.** It is nearly free (addressable nodes exist) and
  high-value, so it can ship ahead of, or alongside, the draw slice.

## Open Questions (settle with JJ before implementation)

- **OQ1.** How structured does the sketching need to be to be useful — is freeform ink genuinely
  enough for the first version, or does JJ expect at least snap-to-rectangle so a mockup reads
  cleanly?
- **OQ2.** Does a drawing **persist as a shared canvas frame** everyone sees, or land on a private
  scratch layer JJ hands over deliberately ("here, look at this now")? The bandwidth story wants the
  deliberate hand-over; the collaboration story wants the shared frame.
- **OQ3.** Laptop-view + phone-draw at once (TC2): **real-time mirror**, or **draw-then-submit**?
  Draw-then-submit is far cheaper and matches "sketch → hand it over → you polish."
- **OQ4.** Pen niceties — pressure, eraser, a couple of colors: any of these needed at v0, or all
  deferred?

## Out of Scope

- **Structured shape recognition / snapping and handwriting-to-text OCR** (KD2) — a later spec.
- **Real-time collaborative multi-cursor editing** — at most draw-then-submit at v0 (OQ3).
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
