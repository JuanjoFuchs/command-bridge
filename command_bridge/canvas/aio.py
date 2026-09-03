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
import json
import queue

from aiohttp import web

from . import server as canvas


def _sse(event: str, payload: dict) -> bytes:
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(payload))).encode()


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


async def handle_canvas_page(request: web.Request) -> web.Response:
    return web.Response(text=canvas.render(), content_type="text/html")


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
    server part... for the UI at least?"). The page's HTML/CSS/JS are baked into `PAGE` at import, so
    a code edit used to need a full `stop`+`serve` — which drops the audio, the lanes and every
    other agent. Instead: re-import the page module IN PLACE, re-bind the running server's view of it,
    then fan a `reload` event so every open tab re-fetches the regenerated page (its own JS logic
    changed, so a DOM morph won't do — the client has to re-run it). The voice WebSocket, the lanes
    and the turn log are never touched. Content/state updates already stream live over `/events`;
    this is only for changes to the page's own code. Unauthenticated like the page it reloads."""
    import importlib
    from . import page as _page
    try:
        importlib.reload(_page)
    except Exception as exc:                       # noqa: BLE001 — a broken edit must not kill the server
        return web.json_response(
            {"reloaded": 0, "error": "page module failed to re-import; the old UI is still serving",
             "detail": str(exc)[:300]}, status=500)
    canvas.PAGE_VERSION = _page.PAGE_VERSION        # re-bind the names server.py imported at startup
    canvas.render = _page.render
    n = canvas.publish("reload", {})
    return web.json_response({"reloaded": n, "version": _page.PAGE_VERSION})


def setup(app: web.Application) -> None:
    """Register the canvas routes on command-bridge's aiohttp app. Additive only — the voice routes
    are untouched. The page's own URLs (/events, /switch, /placed, /inspected) live at the root; the
    CLI ops live under /canvas/ so /cue does not collide with the voice cue."""
    app.router.add_get("/events", handle_events)
    app.router.add_get("/canvas", handle_canvas_page)
    app.router.add_get("/canvas/status", handle_canvas_status)
    app.router.add_post("/canvas/{op}", handle_canvas_op)
    app.router.add_post("/switch", handle_switch)
    app.router.add_post("/placed", handle_placed)
    app.router.add_post("/inspected", handle_inspected)
    app.router.add_post("/reload", handle_reload)
