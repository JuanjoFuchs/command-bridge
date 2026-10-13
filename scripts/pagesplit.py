"""The page is two documents (spec 023) — helpers for the harnesses that drive it.

`/` is the AUDIO SHELL (`web/shell.html`: the microphone, playback, the voice socket) and `/ui` is
everything visible (`web/index.html`), loaded by the shell as a same-origin frame. The real server
routes both. A harness that serves `web/` statically needs the same two routes, or `/` would be the
UI on its own — which renders, but holds no audio, so nothing about clips, capture or the socket
can be tested through it.

Two things here, imported by the harnesses that need the audio half:

* `SplitPages` — a quiet static handler with the two routes.
* `ui_frame(page)` — the UI frame, once it has loaded. DOM checks go to the frame; `__voiceTunnel`
  is the same object in both documents, so either can drive it.
"""
from __future__ import annotations

import http.server

ROUTES = {"/": "/shell.html", "/ui": "/index.html"}


class SplitPages(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — the base class's name
        path, sep, query = self.path.partition("?")
        if path in ROUTES:
            self.path = ROUTES[path] + sep + query
        super().do_GET()

    def log_message(self, format, *args):  # noqa: A002 — the base class names it `format`
        pass


def ui_frame(page, timeout_ms: int = 15000):
    """The loaded UI frame inside the shell. Asked of the TOP document, which a UI reload leaves in
    place, rather than of a frame handle that a navigation can leave stale."""
    page.wait_for_function(
        """() => { const fr = document.getElementById('ui');
                   return !!(fr && fr.contentDocument && fr.contentDocument.readyState === 'complete'
                             && fr.contentWindow.__voiceTunnel); }""",
        timeout=timeout_ms)
    for frame in page.frames:
        if frame.parent_frame == page.main_frame and frame.url.split("?")[0].endswith("/ui"):
            return frame
    raise RuntimeError("the shell has no /ui frame")
