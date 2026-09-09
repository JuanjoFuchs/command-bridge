"""Mount the canvas onto command-bridge's aiohttp server: the SSE stream, the frame-op routes, and
the canvas page. The operation logic and state live in `canvas.server` (apply / run_batch / publish
/ the module globals); this file is only the aiohttp shell around them, so there is no second copy
of the frame rules to drift.

Two things worth knowing:

- **One multiplexed SSE stream** (spec 003 NFR3). Every browser shares `/events`; a push fans out to
  all of them. It is one stream, not one per lane, because a browser caps concurrent connections per
  origin and the voice WebSocket already spends one.
- **The thread queue is bridged to async.** The canvas fans events out through a `queue.Queue` fed
  from any thread (a `switch`, the follower, a CLI POST handled on the executor). The SSE handler
  pulls from it with `run_in_executor`, so the blocking `get()` never stalls the event loop.

Only ADDED to the app — the voice routes (WS, turn log, /status, /say …) are untouched, so the voice
contract holds by construction (spec 003 NFR1).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue

from aiohttp import web

from . import server as canvas


def _sse(event: str, payload: dict) -> bytes:
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(payload))).encode()


# THE PARENT DOCUMENT'S SIGNATURE, so a `reload` can tell a canvas change from a parent change
# (spec 013 FR5). `web/index.html` is the PARENT — it holds the AudioContext and the voice socket —
# and the canvas is an iframe inside it. A page.py edit reloads only the iframe (audio untouched); an
# index.html edit is the one that still needs a full parent reload. This tracks what the parent was
# last known to hold so `handle_reload` can spot when index.html itself moved. Seeded in `setup()`.
_index_sig: str | None = None


def _index_signature() -> str | None:
    """SHA-256 of the parent document on disk, or None if it cannot be read.

    Kept in this module (not imported from server.WEB_DIR) so the canvas shell has no load-time
    dependency on the voice server; the path is the same `command_bridge/web/index.html`."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _reload_target(prev_sig: str | None, cur_sig: str | None) -> str:
    """Which document a pushed reload is for: `page` (a full PARENT reload — spec 013 FR5) only when
    the parent document actually changed since it was last hashed, else `canvas` (the iframe reloads
    itself, audio untouched — FR1).

    Biased to `canvas` on any uncertainty — a first-ever hash (`prev` None) or an unreadable file
    (`cur` None). `page` is the expensive verdict: it drops the audio, so it is returned only on
    positive evidence that the parent moved, never on a guess."""
    if prev_sig is None or cur_sig is None:
        return "canvas"
    return "page" if cur_sig != prev_sig else "canvas"


async def handle_events(request: web.Request) -> web.StreamResponse:
    """The canvas SSE stream. Replays version -> sync -> armed cues -> backlog frames, then streams
    live events until the browser goes away."""
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store",
        "Connection": "keep-alive",
    })
    await resp.prepare(request)

    q: queue.Queue = queue.Queue()
    with canvas._lock:
        canvas._subscribers.append(q)
        backlog = [(lane, dict(f)) for lane, frames in canvas._lanes.items()
                   for f in frames.values()]
        live, hands = canvas._live, dict(canvas._hands)
        armed = list(canvas._armed.values())
        point = dict(canvas._point.get(live) or {})

    try:
        # Version first, so a tab on an older build reloads itself (no refresh ever). Then `sync`,
        # the server's truth about what exists, BEFORE replaying it — a page that survived a restart
        # may be holding cards this process never heard of.
        await resp.write(_sse("version", {"version": canvas.PAGE_VERSION}))
        await resp.write(_sse("sync", {
            "live": live,
            "lanes": {lane: [f["id"] for f in fr.values()]
                      for lane, fr in canvas._lanes.items()},
            "hands": hands}))
        for a in armed:
            await resp.write(_sse("cue", a))
        for lane, frame in backlog:
            await resp.write(_sse("frame", {**frame, "lane": lane}))
            if frame.get("rows"):
                await resp.write(_sse("rows", {"id": frame["id"], "lane": lane,
                                               "insert": frame["rows"], "remove_all": True,
                                               "data_name": frame.get("data_name", "table")}))
        # The live lane's static point, AFTER its frames so the referent it names already exists
        # (2026-09-07). A highlight set before this page connected — or before it reconnected — is
        # otherwise lost, which is half of why "the pointing didn't work".
        if point.get("selector"):
            await resp.write(_sse("point", point))
        loop = asyncio.get_event_loop()
        while True:
            try:
                event, payload = await loop.run_in_executor(None, q.get, True, 15)
            except queue.Empty:
                # A comment line keeps proxies and the browser from deciding the connection died.
                await resp.write(b": keep-alive\n\n")
                continue
            await resp.write(_sse(event, payload))
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with canvas._lock:
            if q in canvas._subscribers:
                canvas._subscribers.remove(q)
    return resp


async def _apply(request: web.Request, path: str) -> web.Response:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — a bad body is a 400, not a crash
        return web.json_response({"error": "bad json"}, status=400)
    if path == "/batch":
        return web.json_response({"results": canvas.run_batch(payload)})
    code, body = canvas.apply(path, payload)
    return web.json_response(body, status=code)


async def handle_canvas_op(request: web.Request) -> web.Response:
    """`POST /canvas/<op>` — the CLI-driven frame ops (frame, point, look, zoom, cue, raise, switch,
    remove, clear, rows, inspect, batch). Prefixed so `/cue` does not collide with the voice `/cue`."""
    return await _apply(request, "/" + request.match_info["op"])


async def handle_switch(request: web.Request) -> web.Response:
    return await _apply(request, "/switch")


async def handle_placed(request: web.Request) -> web.Response:
    return await _apply(request, "/placed")


async def handle_inspected(request: web.Request) -> web.Response:
    return await _apply(request, "/inspected")


async def handle_draw(request: web.Request) -> web.Response:
    """`POST /draw` — the human handed over a sketch (spec 015 T3). An INBOUND-from-the-page route,
    sibling of `/placed` and `/inspected` (root-level, not under /canvas/ where the agent's ops live):
    the drawing surface posts here on `send`, and `apply("/draw")` stores it as an `ink` frame on the
    live lane and surfaces the canvas turn. Kept off the agent's `/frame` verb on purpose — see the
    `/draw` comment in canvas.server."""
    return await _apply(request, "/draw")


async def handle_canvas_page(request: web.Request) -> web.Response:
    return web.Response(text=canvas.render(), content_type="text/html")


async def handle_settle(request: web.Request) -> web.Response:
    """The `shot` settle script. A deliberately slow, empty script the capture page loads so its LOAD
    EVENT waits for the SSE to deliver frames — which is what makes a headless capture deterministic
    instead of a race against the stream (an open SSE never reaches network-idle, so that is not the
    wait to use). It mirrors the ThreadingHTTPServer's `/settle`, and it is needed HERE because a
    lane-scoped `shot --lane` targets the aiohttp `/canvas` (cli.cmd_shot): without this route the
    page's `/settle?ms=` 404s, its `onerror` closes the stream early, and every capture of this
    server's canvas comes back empty. `asyncio.sleep`, never `time.sleep`, so a long settle does not
    block the event loop the SSE frames arrive on."""
    try:
        ms = int(request.query.get("ms", "2500"))
    except (TypeError, ValueError):
        ms = 2500
    ms = min(20000, max(0, ms))
    await asyncio.sleep(ms / 1000)
    return web.Response(body=b"// settled", content_type="application/javascript")


async def handle_canvas_status(request: web.Request) -> web.Response:
    """The canvas half of `command-bridge status` (spec 005 FR4): which lane is live and the frames
    present per lane. A GET — the draw ops are POSTs under /canvas/<op> — so it never mutates.
    Optional ?lane= scopes the frame list to one lane."""
    which = request.query.get("lane") or ""
    with canvas._lock:
        live = canvas._live
        clients = len(canvas._subscribers)
        lanes = {
            lane: [{"id": f.get("id"), "kind": f.get("kind"), "title": f.get("title", "")}
                   for f in frames.values()]
            for lane, frames in canvas._lanes.items()
            if not which or lane == which
        }
    return web.json_response({"live_lane": live, "clients": clients, "lanes": lanes})


def init_canvas(session: str = "dev", fresh: bool = False, follow: bool = True) -> int:
    """Mirror `canvas.server.serve()`'s state setup, minus the ThreadingHTTPServer: load the
    persisted canvas, start the debounced writer, and start the voice-lane follower. Returns the
    number of frames restored. Call once, on serve — not at app creation (it starts threads)."""
    from .follow import Follower
    from .store import Store

    # spec 015 AC4: the canvas surfaces a human draw as a turn in THIS session's log — the same one
    # the voice server appends to and `command-bridge watch` reads — so the two share one turn stream.
    canvas._session = session
    canvas._store = Store()
    restored = 0
    if not fresh:
        lanes, geo, live = canvas._store.load()
        with canvas._lock:
            canvas._lanes.update(lanes)
            canvas._geometry.update(geo)
            if live:
                canvas._live = live
        restored = sum(len(f) for f in lanes.values())
    canvas._store.start(canvas._canvas_snapshot)

    # Soft: with no voice tunnel this finds nothing, says so in status, and never mentions it again.
    canvas._follower = Follower(session=session, enabled=follow)
    canvas._follower.start(canvas.set_live, canvas.live_lane)
    return restored


async def handle_reload(request: web.Request) -> web.Response:
    """Hot-reload the UI WITHOUT dropping the session (JJ, 2026-09-03: "can't we auto-reload the
    server part... for the UI at least?" / "I want these UI updates to not mess with the audio").

    The page's HTML/CSS/JS are baked into `PAGE` at import, so a code edit used to need a full
    `stop`+`serve` — which drops the audio, the lanes and every other agent. Instead: re-import the
    page module IN PLACE, re-bind the running server's view of it, then fan a `reload` event.

    spec 013 — WHICH document reloads. The parent document (`web/index.html`) holds the AudioContext
    and the voice socket; the canvas is an `<iframe>` inside it. So a `reload` carries a `target`:
    `canvas` (the default) tells the iframe to reload ITSELF while the parent — and the audio — is
    never touched; `page` tells the parent to do the full, defer-while-live reload it always did.
    The target is `page` only when `index.html` itself changed (FR5); a page.py edit is `canvas`, so
    iterating on the canvas costs nothing audible. The voice WebSocket, the lanes and the turn log
    are never touched. Content/state updates already stream live over `/events`; this is only for
    changes to a page's own code. Unauthenticated like the page it reloads."""
    import importlib
    from . import page as _page
    global _index_sig
    try:
        importlib.reload(_page)
    except Exception as exc:                       # noqa: BLE001 — a broken edit must not kill the server
        return web.json_response(
            {"reloaded": 0, "error": "page module failed to re-import; the old UI is still serving",
             "detail": str(exc)[:300]}, status=500)
    canvas.PAGE_VERSION = _page.PAGE_VERSION        # re-bind the names server.py imported at startup
    canvas.render = _page.render
    cur_sig = _index_signature()
    target = _reload_target(_index_sig, cur_sig)
    _index_sig = cur_sig
    n = canvas.publish("reload", {"target": target})
    return web.json_response({"reloaded": n, "version": _page.PAGE_VERSION, "target": target,
                              "full_reload": target == "page"})


def setup(app: web.Application) -> None:
    """Register the canvas routes on command-bridge's aiohttp app. Additive only — the voice routes
    are untouched. The page's own URLs (/events, /switch, /placed, /inspected) live at the root; the
    CLI ops live under /canvas/ so /cue does not collide with the voice cue."""
    global _index_sig
    _index_sig = _index_signature()  # spec 013: baseline the parent doc so the first reload can spot an index.html change
    app.router.add_get("/events", handle_events)
    app.router.add_get("/canvas", handle_canvas_page)
    app.router.add_get("/settle", handle_settle)  # shot's load-event hold — see handle_settle
    app.router.add_get("/canvas/status", handle_canvas_status)
    app.router.add_post("/canvas/{op}", handle_canvas_op)
    app.router.add_post("/switch", handle_switch)
    app.router.add_post("/placed", handle_placed)
    app.router.add_post("/inspected", handle_inspected)
    app.router.add_post("/draw", handle_draw)  # spec 015: the page's `send` posts the sketch here
    app.router.add_post("/reload", handle_reload)
