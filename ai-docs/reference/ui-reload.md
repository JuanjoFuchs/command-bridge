---
title: Landing a UI change on the open page
description: How `command-bridge reload` pushes an edited page to the human's open tab without a refresh, which document reloads when, and the one case that still needs a refresh.
applies_to: command_bridge/web/index.html, command_bridge/web/shell.html, command_bridge/canvas/page.py
read_before: making a UI change land on his OPEN page, or deciding whether a change needs stop+serve
---

# Landing a UI change on the open page

After editing `command_bridge/web/index.html`, `web/shell.html` or `canvas/page.py`, run
`command-bridge reload`.

**Push it; never tell him to refresh.** The page is two documents (spec 023): `/` is the AUDIO SHELL
(`web/shell.html` — the mic, playback and the voice socket) and everything he sees is the UI frame
inside it (`web/index.html`, served at `/ui`). `reload` re-imports `page.py` and sends a `reload`
event carrying each page's signature on disk; each open page compares the signature it was served
with.

- An `index.html` change reloads ONLY the UI frame, **immediately, even mid-clip or mid-sentence**
  (`target: ui`) — the audio never notices.
- A `page.py` change reloads only the canvas (`target: canvas`).
- Only a `shell.html` change needs the full reload, DEFERRED while the channel is live and applied at
  the next orb-off (`target: page`) — so keep the shell small and put anything visual in the UI frame.

No stop+serve needed — only a server-CODE change (`server.py`, `config.py`, `tts.py`, …) needs
`stop`+`serve`.

⚠ **A server restart that changes how pages are SERVED (the routes, the signature stamp) needs one
refresh of an open tab**; a restart alone does not, because the pages compare signatures.
