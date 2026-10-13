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


# THE TWO PAGE DOCUMENTS' SIGNATURES (spec 023). `web/shell.html` is the top-level AUDIO SHELL — it
# holds the AudioContext, the microphone and the voice socket — and `web/index.html` is the UI frame
# inside it, which in turn holds the canvas iframe. A UI edit reloads only the UI frame, live; a
# shell edit is the one change that still needs a full, deferred reload (FR6).
#
# Each page is SERVED stamped with the signature of the file it came from (`stamp_page`), and a
# `reload` event carries the signatures now on disk, so the open page compares the two and decides
# for itself. That is what closes the old gap where a server restart re-baselined the server's own
# record and a pushed reload then reported `canvas` to a tab still running the old page.
#
# `_sigs` is only what `reload` REPORTS back to the CLI (which file moved since the last reload);
# the page never depends on it. Seeded in `setup()`.
SHELL_PAGE = "shell.html"
UI_PAGE = "index.html"
SIG_PLACEHOLDER = "__CB_PAGE_SIG__"
_sigs: dict[str, str | None] = {}


def _web_path(name: str) -> str:
    """Kept in this module (not imported from server.WEB_DIR) so the canvas shell has no load-time
    dependency on the voice server; the directory is the same `command_bridge/web/`."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", name)


def page_signature(name: str) -> str | None:
    """A short SHA-256 of one page document on disk, or None if it cannot be read."""
    try:
        with open(_web_path(name), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return None


def stamp_page(name: str) -> tuple[str, str | None] | None:
    """The page's text with its own signature written in where `SIG_PLACEHOLDER` stands, or None if
    the file cannot be read. Hashing the bytes that are served is what makes the stamp true."""
    try:
        with open(_web_path(name), "rb") as f:
            raw = f.read()
    except OSError:
        return None
    sig = hashlib.sha256(raw).hexdigest()[:16]
    return raw.decode("utf-8").replace(SIG_PLACEHOLDER, sig), sig


def _reload_target(prev: dict[str, str | None], cur: dict[str, str | None]) -> str:
    """What a pushed reload will do, for the CLI's report: `page` (a full, deferred reload) when the
    SHELL changed since the last reload, `ui` (the UI frame reloads live) when the UI changed, else
    `canvas` (only the canvas iframe reloads).

    Biased away from `page` on any uncertainty — a first-ever hash or an unreadable file. `page` is
    the expensive verdict: it waits for the orb to go off, so it is reported only on evidence."""
    def moved(name: str) -> bool:
        a, b = prev.get(name), cur.get(name)
        return a is not None and b is not None and a != b
    if moved(SHELL_PAGE):
        return "page"
    if moved(UI_PAGE):
        return "ui"
    return "canvas"


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


async def handle_rendered(request: web.Request) -> web.Response:
    return await _apply(request, "/rendered")


async def handle_draw(request: web.Request) -> web.Response:
    """`POST /draw` — the human handed over a sketch (spec 015 T3). An INBOUND-from-the-page route,
    sibling of `/placed` and `/inspected` (root-level, not under /canvas/ where the agent's ops live):
    the drawing surface posts here on `send`, and `apply("/draw")` stores it as an `ink` frame on the
    live lane and surfaces the canvas turn. Kept off the agent's `/frame` verb on purpose — see the
    `/draw` comment in canvas.server."""
    return await _apply(request, "/draw")


async def handle_canvas_file(request: web.Request) -> web.StreamResponse:
    """`GET /canvas/file/<lane>/<name>` — an IMAGE frame's pixels (spec 022). The frame carries this
    URL instead of the bytes, so a png an agent drops into its lane folder renders without being
    inlined anywhere. Only a plain file name directly inside a lane folder, and only an image type:
    no separators, no traversal, and never the frame sources or the index."""
    from . import folder as fs
    lane, name = request.match_info["lane"], request.match_info["name"]
    ext = os.path.splitext(name)[1].lower()
    if (not canvas._store.enabled or "/" in name or "\\" in name or ".." in name
            or fs.ignored(name) or ext not in fs.IMAGE_TYPES):
        return web.json_response({"error": "not found"}, status=404)
    path = canvas._store.lane_dir(lane) / name
    if not path.is_file():
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(path, headers={"Content-Type": fs.IMAGE_TYPES[ext],
                                           "Cache-Control": "no-store"})


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
    from .folder import wall_of_text

    which = request.query.get("lane") or ""

    def advice(f: dict) -> dict:
        # spec 022: with no `set` to return them, the draw-time feedback rides on each frame here —
        # its file, a render failure the page reported, and the wall-of-text advisory.
        out: dict = {}
        if f.get("file"):
            out["file"] = f["file"]
        if f.get("render_error"):
            out["render_error"] = f["render_error"]
        nudge = wall_of_text(str(f.get("content") or ""), str(f.get("kind") or ""))
        if nudge:
            out["wall_of_text"] = nudge
        return out

    with canvas._lock:
        live = canvas._live
        clients = len(canvas._subscribers)
        lanes = {
            lane: [{"id": f.get("id"), "kind": f.get("kind"), "title": f.get("title", ""),
                    **advice(f),
                    # spec 019 FR2: each frame carries its legibility — the page-measured effective
                    # min font, the contain-fit scale, and `readable` against READABLE_PX. Null when
                    # no page is connected (nothing to measure), the same contract as the viewport.
                    **canvas.frame_legibility(
                        (canvas._geometry.get(lane) or {}).get(f.get("id")) if clients else None)}
                   for f in frames.values()]
            for lane, frames in canvas._lanes.items()
            if not which or lane == which
        }
        # The viewport of the lane being reported (the one in question, else the live one), with its
        # DERIVED aspect ratio (spec 018 FR3) so `command-bridge status` — which folds this in under
        # `canvas` — carries the canvas shape an agent authors toward. Null when no page is connected
        # (there is no viewport, and a stale one is a lie), exactly as the standalone `_status` does.
        vp = canvas.viewport_with_aspect(canvas._viewport.get(which or live)) if clients else None
    # spec 022: where each lane's frames live — the folder an agent writes a file into to draw.
    folder = canvas.lane_folder(which or live)
    return web.json_response({"live_lane": live, "clients": clients, "lanes": lanes,
                              "viewport": vp, "folder": folder,
                              "canvas_dir": str(canvas._store.root) if canvas._store.enabled else None})


def init_canvas(session: str = "dev", fresh: bool = False, follow: bool = True) -> int:
    """Mirror `canvas.server.serve()`'s state setup, minus the ThreadingHTTPServer: load the
    persisted canvas, start the debounced writer, and start the voice-lane follower. Returns the
    number of frames restored. Call once, on serve — not at app creation (it starts threads)."""
    from .follow import Follower

    # spec 015 AC4: the canvas surfaces a human draw as a turn in THIS session's log — the same one
    # the voice server appends to and `command-bridge watch` reads — so the two share one turn stream.
    canvas._session = session
    # spec 022: the canvas is the lane folders under sessions/<session>.canvas/, and a watcher turns
    # every file an agent writes there into a frame. The old single JSON is migrated once, here.
    restored = canvas.start_folder(session, fresh=fresh)

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

    spec 013 + 023 — WHICH document reloads. The audio shell (`web/shell.html`) holds the
    AudioContext, the microphone and the voice socket; the UI frame (`web/index.html`) sits inside it
    and the canvas iframe inside that. The event always carries `shell_sig` and `ui_sig`, the
    signatures now on disk: the canvas iframe reloads itself on every event (spec 013), the shell
    reloads the UI frame when the UI's stamp differs — immediately, even while live — and does its
    own full reload, deferred until the orb is off, only when the shell's stamp differs. `target` is
    the server's report of which of those the edit needed (`canvas` | `ui` | `page`). The voice
    WebSocket, the lanes and the turn log are never touched. Content/state updates already stream
    live over `/events`; this is only for changes to a page's own code. Unauthenticated like the
    page it reloads."""
    import importlib

    from . import page as _page
    global _sigs
    try:
        importlib.reload(_page)
    except Exception as exc:                       # noqa: BLE001 — a broken edit must not kill the server
        return web.json_response(
            {"reloaded": 0, "error": "page module failed to re-import; the old UI is still serving",
             "detail": str(exc)[:300]}, status=500)
    canvas.PAGE_VERSION = _page.PAGE_VERSION        # re-bind the names server.py imported at startup
    canvas.render = _page.render
    cur = {SHELL_PAGE: page_signature(SHELL_PAGE), UI_PAGE: page_signature(UI_PAGE)}
    target = _reload_target(_sigs, cur)
    _sigs = cur
    n = canvas.publish("reload", {"target": target, "shell_sig": cur[SHELL_PAGE], "ui_sig": cur[UI_PAGE]})
    return web.json_response({"reloaded": n, "version": _page.PAGE_VERSION, "target": target,
                              "full_reload": target == "page"})


def setup(app: web.Application) -> None:
    """Register the canvas routes on command-bridge's aiohttp app. Additive only — the voice routes
    are untouched. The page's own URLs (/events, /switch, /placed, /inspected) live at the root; the
    CLI ops live under /canvas/ so /cue does not collide with the voice cue."""
    global _sigs
    # Baseline both pages so the first reload can report which one moved (spec 023).
    _sigs = {SHELL_PAGE: page_signature(SHELL_PAGE), UI_PAGE: page_signature(UI_PAGE)}
    app.router.add_get("/events", handle_events)
    app.router.add_get("/canvas", handle_canvas_page)
    app.router.add_get("/settle", handle_settle)  # shot's load-event hold — see handle_settle
    app.router.add_get("/canvas/status", handle_canvas_status)
    app.router.add_get("/canvas/file/{lane}/{name}", handle_canvas_file)  # spec 022: image frames
    app.router.add_post("/canvas/{op}", handle_canvas_op)
    app.router.add_post("/switch", handle_switch)
    app.router.add_post("/placed", handle_placed)
    app.router.add_post("/inspected", handle_inspected)
    # The page's mermaid render-outcome report (JJ, 2026-09-24). It was only ever mounted on the
    # standalone canvas server, so on this one every report 404'd and `render_error` never reached
    # `status` — found 2026-09-25 in the page console while verifying spec 022.
    app.router.add_post("/rendered", handle_rendered)
    app.router.add_post("/draw", handle_draw)  # spec 015: the page's `send` posts the sketch here
    app.router.add_post("/reload", handle_reload)
