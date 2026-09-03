---
id: "013"
title: Live UI updates without dropping the audio
status: pending
blocked_by: []
blocks: []
---

# Live UI updates without dropping the audio

## Overview

Iterating on the canvas UI (`command_bridge/canvas/page.py`) currently forces a full-page
`location.reload()` on the merged voice page (`web/index.html`, ~L3953). That reload destroys the
AudioContext and the voice WebSocket, cutting JJ off mid-conversation — so the reload is DEFERRED
while the channel is live (a held update; the amber dot on the power button), and a UI change is not
seen until he next toggles the conversation off. `command-bridge reload` (shipped 2026-09-03,
spec-less) hot-reloads `page.py` in the running server but still fans that same full-page reload to
the tab.

This spec makes a canvas UI update apply to the LIVE page without ever touching the audio. The audio
and the voice WebSocket live in the PARENT document (`web/index.html`); the canvas is an
`<iframe src="/canvas?embed=1">` (`page.py`) inside it. A canvas update should therefore reload ONLY
the canvas iframe — re-running its JS, re-applying its CSS, and restoring its frames from the
`/events` replay — while the parent, and so the audio, is never reloaded. JJ, 2026-09-03: *"I want
these UI updates to not mess with the audio ... do the whole thing."*

> **Completion rule:** This spec is not complete until all acceptance criteria are verified through
> the methods each names (headless / integration / manual-audio). Build-only verification is
> insufficient. The agent must iterate until verification passes.

## Goals

- A canvas UI change (`page.py` edit) applied via `command-bridge reload` becomes visible on the live
  page with no full-page reload and no audible interruption to a playing or pending voice clip.
- The voice WebSocket, the AudioContext, the mute/channel state, and the turn log survive the update.
- The canvas's own state (its frames and live lane) is restored after the update, not lost.

## Requirements

### Functional Requirements

- **FR1**: `command-bridge reload` applies the regenerated canvas UI to every open page by reloading
  ONLY the canvas iframe (`/canvas?embed=1`), not the parent document.
- **FR2**: The parent document — its voice WebSocket, AudioContext, audio playback, mute and channel
  state — is not reloaded, re-created, or interrupted by a canvas update.
- **FR3**: After the iframe reloads, the canvas restores its frames and live lane from the `/events`
  replay (version → sync → armed cues → backlog), with no frame lost.
- **FR4**: A canvas update need not be deferred while the channel is live, because it no longer risks
  the audio; the amber "update-waiting" hold is not used for the canvas-only path.
- **FR5**: When the change is to the PARENT document (`web/index.html`), the tool reports that a full
  reload is required and keeps the existing deferred-until-off behaviour — the audio-safe path covers
  canvas (`page.py`) changes only.

### Non-Functional Requirements

- **NFR1**: The canvas iframe reload re-renders within ~1s on the dev machine, reading as instant.
- **NFR2**: No regression to the `page.py` version-mismatch self-reload, the parent's deferred
  full-reload path, or a from-cold page load.

### Technical Constraints

- **TC1**: The audio and voice WebSocket are owned by the parent document; the canvas is a same-origin
  iframe and can be reloaded independently of the parent. The parent must not be reloaded on a canvas
  update.
- **TC2**: Reloading the iframe resets the canvas camera and briefly blanks it while it re-renders.
  Preserving camera position and removing the blank is the morphing refinement (Out of Scope; see
  References — idiomorph).

## Implementation Tasks

- [x] Route the canvas-update signal to an iframe-only reload rather than the parent's full reload.
      *(page.py: the canvas listens for `reload` and reloads its own iframe on a `canvas` target.)*
- [x] Confirm the iframe's `/events` re-subscription restores frames + live lane after the reload.
      *(The iframe reload re-runs page.py, which re-opens `/events` and replays version→sync→cues→
      backlog — the same restore path the version self-reload already relies on.)*
- [x] Suppress the deferred-hold (amber dot) for the canvas-only path; keep it for parent changes.
      *(A `canvas` reload never reaches the parent handler, so the amber hold is only ever set on a
      `page` target.)*
- [x] Distinguish a canvas (`page.py`) change from a parent (`web/index.html`) change so FR5 holds.
      *(aio.py `_reload_target` hashes index.html; `page` only on a real parent change, else `canvas`.)*

> **Deployment note:** because this change touches `web/index.html` (parent) and `canvas/aio.py`
> (the server), activating it needs ONE `stop`+`serve` — a page.py hot-reload cannot swap the server
> module or re-serve the parent doc. That single restart drops the audio once; every canvas reload
> AFTER it is audio-safe. The deterministic ACs (AC4/AC6 mechanism + unit tests) pass now; AC1/AC3/AC5
> (integration) and AC2 (manual-audio) are verified in that first post-restart session.

## Acceptance Criteria

### Core
- [ ] AC1: After a `page.py` edit + `command-bridge reload`, the change is visible on the live page
  and the PARENT document did not navigate (a parent-set sentinel / its `performance.timeUsedFor
  navigation` is unchanged; only the iframe's is new). — *integration*
- [ ] AC2: A voice clip playing when `command-bridge reload` fires is not cut, glitched, or
  restarted. — *manual-audio* (irreducible: real playback continuity has no headless proxy)
- [ ] AC3: The voice WebSocket and the channel/mute state are unchanged across the update — the same
  socket, no reconnect, no state reset. — *integration*
- [ ] AC4: Every frame and the live lane are present on the canvas after the update. — *headless*
  (drive `reload`, assert frame ids + live lane via `command-bridge inspect` / the SSE sync)

### Parent-change path
- [ ] AC5: A change to `web/index.html` still triggers the deferred full reload (held while live) and
  the tool reports that a full reload is required. — *integration*

### No regression
- [ ] AC6: The `page.py` version-mismatch self-reload and a from-cold page load both still work. —
  *headless*

## Testing Approach

### Validation Steps
1. Serve the session; open the page; confirm from-cold load renders (AC6).
2. Edit a visible `page.py` CSS value, run `command-bridge reload`; confirm the canvas shows the new
   value while a parent sentinel proves the parent did not navigate (AC1), and the frames/live lane
   are intact (AC4).
3. With a voice clip playing, run `command-bridge reload`; confirm the audio does not break (AC2) and
   the WebSocket/channel state is unchanged (AC3).
4. Edit `web/index.html`; confirm the deferred full reload path still fires and is reported (AC5).

### Test Cases
| Input | Expected |
|-------|----------|
| `page.py` CSS edit + `reload`, channel live | Canvas updates; parent + audio untouched; not deferred |
| `page.py` JS edit + `reload` | Iframe re-runs new JS; parent + audio untouched |
| `reload` while a clip is playing | Clip continues uninterrupted |
| `web/index.html` edit + `reload` | Deferred full reload, held while live, reported |
| version mismatch on connect | Iframe self-reloads (unchanged) |

## Usage Examples

```
# edit command_bridge/canvas/page.py  (a canvas CSS or JS change)
command-bridge reload --session dev
#   -> the canvas iframe reloads in place; the voice audio never blips
```

## Out of Scope

- DOM morphing (idiomorph) to make even the iframe reload flicker-free and camera-preserving — a
  refinement tracked separately; this spec reloads the iframe.
- Applying PARENT (`web/index.html`) changes without an audio blip — needs the audio moved into a
  context that survives a parent reload; a larger effort, its own spec.
- True hot-module-replacement (applying changed JS without ANY reload) — reloading the iframe re-runs
  its JS, which is sufficient here.

## References

- `command_bridge/web/index.html` — the full-page reload handler (~L3953) and its live-channel defer
  guard (`data-update-waiting`, the amber dot).
- `command_bridge/canvas/page.py` — the version-check self-reload (~L1089) and the `/events` sync
  replay that restores frames.
- The `command-bridge reload` hot-reload endpoint (`canvas/aio.py handle_reload`) that this builds on.
- JJ's genesis note the genesis note and the HTML-over-Websockets post it links — the transport pattern
  CB's canvas already embodies (server renders, client places).
- **idiomorph** — https://github.com/bigskysoftware/idiomorph — the DOM-morphing library for the
  flicker-free/camera-preserving refinement (Out of Scope here). Distill its practices before
  implementing that tier.
