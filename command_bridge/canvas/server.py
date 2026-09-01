"""The surface. Holds the canvases and fans changes out to every open browser.

Deliberately dumb: it holds no model, makes no decisions, and knows nothing
about what it is displaying. The agent that drives it is the intelligence —
the same split that makes `voice-tunnel` verifiable.

It also models **no geometry**. Only the browser knows how big a rendered thing
turned out to be or where the camera ended up, so the page posts that back to
`/placed` and the server just relays it. That is what lets an agent ask "can he
actually see frame `costs` right now?" and get a true answer.

**Lanes.** Each agent gets its own canvas, and exactly one lane is live. Only
the live lane may move the camera; a background lane raises a hand instead.
Addressing is *state*, not a judgment call — which is what keeps a dumb tool
routing correctly, and is borrowed wholesale from `voice-tunnel`.

Transport is **one multiplexed SSE stream**, not one per lane or per frame.
HTTP/1.1 caps a browser at 6 connections per origin, so fanning out connections
dies at seven.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .cue import schedule as cue_schedule
from .follow import Follower
from .page import PAGE_VERSION, render
from .store import Store

DEFAULT_PORT = 8770
STATE_FILE = Path.home() / ".command-bridge.json"
DEFAULT_FRAME = "main"
DEFAULT_LANE = "main"

# Every connected browser gets its own queue; a push fans out to all of them.
_subscribers: list[queue.Queue] = []
_lock = threading.Lock()

# lane -> ordered {frame id: frame}. A lane appears the moment it is written to.
_lanes: dict[str, dict[str, dict]] = {}
# lane -> {"count": int, "why": str}. Cleared when the human switches to it.
_hands: dict[str, dict] = {}
_live = DEFAULT_LANE

# Geometry and viewport per lane, as last reported BY THE PAGE. Never inferred.
_geometry: dict[str, dict] = {}
_viewport: dict[str, dict] = {}

# lane -> a cue schedule waiting for that lane to go live. Replayed to a page
# that connects late, because the whole point is that it survives until the
# moment of the switch.
_armed: dict[str, dict] = {}

# In-flight `inspect` questions: id -> [Event, answer]. The page is the only
# thing that knows what a selector resolves to, so the answer has to come back
# FROM it — one request out on the stream, one POST back.
_asked: dict[str, list] = {}
_ask_n = 0

# The canvas on disk. Replaced by `serve`; the default is inert so importing the
# module (tests, `describe`) never touches a real file.
_store = Store(enabled=False)

# Mirrors the voice tunnel's live lane, when there is one. Replaced by `serve`;
# the default is inert so importing the module never reaches for another tool.
_follower = Follower(enabled=False)


def set_live(lane: str) -> int:
    """Give a lane the floor. The one place `_live` moves.

    Factored out because there are now two callers with the same semantics: the
    human clicking a chip, and the canvas following the voice tunnel. A second
    copy of "switch, clear the hand, publish, persist" would drift.
    """
    global _live
    with _lock:
        if _live == lane:
            return 0
        _live = lane
        _hands.pop(lane, None)
        _lanes.setdefault(lane, {})
        # The page fires its own copy on the switch. Drop ours so a browser
        # connecting AFTER the switch does not replay a schedule that has
        # already run.
        _armed.pop(lane, None)
    _store.touch()
    return publish("switch", {"lane": lane})


def live_lane() -> str:
    return _live


def _canvas_snapshot() -> dict:
    """What the writer thread persists. Called off the request path."""
    with _lock:
        return {"lanes": {lane: dict(frames) for lane, frames in _lanes.items()
                          if frames},
                "geometry": {lane: dict(g) for lane, g in _geometry.items() if g},
                "live": _live}


def publish(event: str, payload: dict) -> int:
    """Fan one SSE event out to every connected browser. Returns the count."""
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        q.put((event, payload))
    return len(targets)


def _lane_of(payload: dict) -> str:
    return str(payload.get("lane") or DEFAULT_LANE)


def _frames_of(lane: str) -> dict[str, dict]:
    return _lanes.setdefault(lane, {})


def _frame_view(lane: str, fid: str) -> dict:
    """A frame as `status` reports it: what it is, plus where it landed."""
    f = _lanes[lane][fid]
    out = {"id": fid, "kind": f["kind"], "title": f["title"]}
    if f.get("scale"):
        out["scale"] = f["scale"]
    if f.get("at"):
        out["at"] = f["at"]
    # Age in seconds, so the agent can decide whether a frame is stale without
    # doing clock arithmetic against a wall-clock string it has to parse.
    if f.get("created"):
        out["age_s"] = round(time.time() - f["created"], 1)
        out["created"] = f["created"]
    if f.get("updated"):
        out["updated_s"] = round(time.time() - f["updated"], 1)
    geo = (_geometry.get(lane) or {}).get(fid)
    if geo:
        out.update(geo)
    return out


def _lane_summary() -> list[dict]:
    # An EMPTY lane is not a lane, it is a name somebody typed. Switching to one
    # materialises it (`/switch` setdefaults so the floor always has somewhere to
    # land), and clicking a chip is a switch — so browsing the header used to
    # leave a permanent empty chip behind, and every agent that ever touched the
    # canvas added one. Show a lane when it has something to show, is live, or is
    # asking to be.
    return [
        {"lane": name, "frames": len(frames), "live": name == _live,
         **({"raised": _hands[name]} if name in _hands else {})}
        for name, frames in sorted(_lanes.items())
        if frames or name == _live or name in _hands
    ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - the base class's signature
        if getattr(self.server, "verbose", False):
            sys.stderr.write("%s %s\n" % (self.address_string(), format % args))

    # ---- plumbing --------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _event(self, event: str, payload: dict) -> None:
        body = "event: %s\ndata: %s\n\n" % (event, json.dumps(payload))
        self.wfile.write(body.encode())
        self.wfile.flush()

    # ---- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        # Match the PATH, not the raw request line: `/?shot=4000` is still the
        # page, and comparing `self.path == "/"` 404s it. Found by screenshotting
        # the surface and reading `{"error": "not found"}` off the image.
        route = self.path.split("?", 1)[0]
        if route == "/":
            self._send(200, render().encode(), "text/html; charset=utf-8")
        elif route == "/events":
            self._stream()
        elif route == "/status":
            self._status()
        elif route == "/settle":
            # A deliberately slow, empty script. `shot` injects it so the page's
            # LOAD EVENT waits for it — which is what makes a headless capture
            # deterministic instead of a race against the SSE delivering frames.
            # Virtual time cannot be used for this: an open SSE stream is a
            # pending fetch, so the budget never expires and the browser hangs.
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(self.path).query)
            ms = min(20000, max(0, int((q.get("ms") or ["2500"])[0])))
            time.sleep(ms / 1000)
            self._send(200, b"// settled", "application/javascript")
        else:
            self._json(404, {"error": "not found"})

    def _status(self) -> None:
        # `status?lane=x` inspects a lane WITHOUT switching to it, so an agent
        # can check its own canvas while somebody else holds the floor.
        want = ""
        if "?" in self.path:
            from urllib.parse import parse_qs, urlsplit
            want = (parse_qs(urlsplit(self.path).query).get("lane") or [""])[0]
        with _lock:
            clients = len(_subscribers)
            lane = want or _live
            frames = [_frame_view(lane, fid) for fid in _lanes.get(lane, {})]
            # With nobody connected there IS no viewport. Say so rather than
            # reporting a stale one: an agent told "nothing is visible" behaves
            # correctly, an agent told a lie does not.
            viewport = _viewport.get(lane) if clients else None
            body = {"clients": clients, "lane": lane, "live_lane": _live,
                    "lanes": _lane_summary(), "frames": frames,
                    "viewport": viewport, "page_version": PAGE_VERSION,
                    # Named so a human can find, inspect or delete the record
                    # without being told where it lives.
                    "canvas_file": str(_store.path) if _store.enabled else None,
                    # Says out loud whether the canvas is following the voice
                    # tunnel. A soft dependency that fails silently must still
                    # be visible when someone asks.
                    "follow": _follower.status()}
        self._json(200, body)

    def _stream(self) -> None:
        q: queue.Queue = queue.Queue()
        with _lock:
            _subscribers.append(q)
            backlog = [(lane, dict(f)) for lane, frames in _lanes.items()
                       for f in frames.values()]
            live, hands = _live, dict(_hands)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            # Announce the page version first. A tab running an older one
            # reloads itself, so the surface upgrades without anyone being
            # asked to refresh — the thing it exists to avoid.
            self._event("version", {"version": PAGE_VERSION})
            # The server is the source of truth for what exists, so say what it
            # believes in BEFORE replaying it: a page that survived a restart is
            # still holding cards this server never heard of, and without this
            # it would show a canvas nobody can address.
            self._event("sync", {"live": live,
                                 "lanes": {lane: [f["id"] for f in fr.values()]
                                           for lane, fr in _lanes.items()},
                                 "hands": hands})
            # An armed cue is waiting for a switch that has not happened yet, so
            # a page connecting in the meantime has to receive it or the
            # highlights are lost to a reload.
            for armed in list(_armed.values()):
                self._event("cue", armed)
            for lane, frame in backlog:
                self._event("frame", {**frame, "lane": lane})
                # A chart's accumulated rows follow its spec, or a late browser
                # renders empty axes and nothing else.
                if frame.get("rows"):
                    self._event("rows", {"id": frame["id"], "lane": lane,
                                         "insert": frame["rows"], "remove_all": True,
                                         "data_name": frame.get("data_name", "table")})
            while True:
                try:
                    event, payload = q.get(timeout=15)
                    self._event(event, payload)
                except queue.Empty:
                    # A comment line keeps proxies and the browser from
                    # deciding the connection died.
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    # ---- mutations -------------------------------------------------------

    def do_POST(self) -> None:
        global _live
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._json(400, {"error": "bad json: %s" % exc})
            return
        if self.path == "/batch":
            self._json(200, {"results": self._batch(payload)})
            return
        code, body = self.apply(self.path, payload)
        self._json(code, body)

    # Verb -> route, so a batch element can name the verb an agent already knows
    # rather than a URL it should never have had to learn.
    OPS = {"set": "/frame", "remove": "/remove", "clear": "/clear",
           "point": "/point", "look": "/look", "zoom": "/zoom",
           "cue": "/cue", "raise": "/raise", "switch": "/switch"}

    def _batch(self, payload: dict) -> list[dict]:
        """Apply many operations in order, and never abort on one failure.

        A batch is a picture. Losing one frame is better than losing the
        picture, so a failed operation returns its error in place and the rest
        still run — the caller can see exactly which one went wrong.
        """
        ops = payload if isinstance(payload, list) else payload.get("ops") or []
        results = []
        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                results.append({"error": "operation %d is not an object" % i})
                continue
            name = str(op.get("op") or "")
            route = self.OPS.get(name)
            if not route:
                results.append({"error": "unknown op %r" % name,
                                "known": sorted(self.OPS)})
                continue
            try:
                code, body = self.apply(route, {k: v for k, v in op.items() if k != "op"})
            except Exception as exc:  # noqa: BLE001 - one bad op must not kill the batch
                results.append({"error": "%s: %s" % (type(exc).__name__, exc), "op": name})
                continue
            results.append(body if code == 200 else {**body, "status": code})
        return results

    def apply(self, path: str, payload: dict) -> tuple[int, dict]:
        """One operation. Returns (status, body).

        Split out from `do_POST` so `/batch` can run many without re-parsing
        or re-dispatching through HTTP.
        """
        global _live
        lane = _lane_of(payload)

        if path == "/frame":
            fid = str(payload.get("id") or DEFAULT_FRAME)
            frame = {"id": fid, "kind": payload.get("kind", "mermaid"),
                     "content": payload.get("content", ""),
                     "title": payload.get("title", "")}
            if payload.get("scale"):
                frame["scale"] = payload["scale"]
            if payload.get("at"):
                frame["at"] = payload["at"]
            with _lock:
                # Age is stamped HERE, not by the caller: an agent that could
                # set its own timestamps could make a stale frame look fresh,
                # and the whole point of showing age is that it is not a claim.
                # Re-placing an id keeps `created` and moves `updated`, so a
                # chart refreshed all afternoon reads as old-and-live rather
                # than new.
                prev = _lanes.get(lane, {}).get(fid)
                frame["created"] = prev["created"] if prev and prev.get("created") \
                    else time.time()
                frame["updated"] = time.time()
                # Re-placing an id replaces that frame in place. dict preserves
                # insertion order, so an existing id keeps its layout slot.
                _frames_of(lane)[fid] = frame
                count = len(_lanes[lane])
            _store.touch()
            return 200, {"ok": True, "delivered_to": publish("frame", {**frame, "lane": lane}),
                         "id": fid, "lane": lane, "kind": frame["kind"], "frames": count}

        if path == "/remove":
            fid = str(payload.get("id") or "")
            with _lock:
                known = fid in _lanes.get(lane, {})
                _lanes.get(lane, {}).pop(fid, None)
                (_geometry.get(lane) or {}).pop(fid, None)
                count = len(_lanes.get(lane, {}))
            if not known:
                return 404, {"error": "no frame %r in lane %r" % (fid, lane),
                             "frames": list(_lanes.get(lane, {}))}
            _store.touch()
            return 200, {"ok": True, "removed": fid, "lane": lane, "frames": count,
                         "delivered_to": publish("remove", {"id": fid, "lane": lane})}

        if path == "/clear":
            # Scoped to the caller's lane on purpose: a lane must not be able
            # to delete another lane's work.
            with _lock:
                _lanes.pop(lane, None)
                _geometry.pop(lane, None)
                _viewport.pop(lane, None)
                empty = not any(_lanes.values())
            # `clear` is the explicit throw-this-away verb, so it must not leave
            # a copy that reappears on the next restart. Clearing the LAST lane
            # removes the file outright rather than writing an empty one.
            if empty:
                _store.discard()
            else:
                _store.touch()
            return 200, {"ok": True, "lane": lane,
                         "delivered_to": publish("clear", {"lane": lane})}

        if path == "/rows":
            fid = str(payload.get("id") or DEFAULT_FRAME)
            frame = _lanes.get(lane, {}).get(fid)
            # A row append with no chart to append to is a mistake worth naming:
            # silently drawing nothing would look like a rendering bug.
            if not frame or frame.get("kind") != "vega":
                return 404, {"error": "no chart %r in lane %r" % (fid, lane),
                             "remedy": "send the spec first: "
                                       "command-bridge chart --id %s --spec <file>" % fid,
                             "charts": [k for k, f in _lanes.get(lane, {}).items()
                                        if f.get("kind") == "vega"]}
            msg = {"id": fid, "lane": lane,
                   "insert": payload.get("insert") or [],
                   "remove_all": bool(payload.get("remove_all")),
                   "data_name": payload.get("data_name") or "table"}
            # The rows ARE kept, and this was a bug before it was a decision.
            # Not storing them looked like the pure choice — the spec is the
            # frame, rows are a delta — but a browser that connects later gets
            # the spec and no data, so every `shot` photographed an empty chart.
            # The cost this tier saves is on the WIRE, per update; replaying
            # accumulated rows to a new client does not give any of that back.
            with _lock:
                if msg["remove_all"]:
                    frame["rows"] = []
                frame.setdefault("rows", []).extend(msg["insert"])
                frame["data_name"] = msg["data_name"]
            _store.touch()
            return 200, {"ok": True, "delivered_to": publish("rows", msg),
                         "id": fid, "lane": lane,
                         "inserted": len(msg["insert"])}

        if path in ("/point", "/look", "/zoom", "/cue"):
            return self._camera(path, payload, lane)

        if path == "/raise":
            with _lock:
                if lane == _live:
                    return 200, {"ok": True, "lane": lane, "live": True,
                                 "note": "you already have the floor — just say it"}
                hand = _hands.setdefault(lane, {"count": 0, "why": ""})
                hand["count"] += 1
                if payload.get("why"):
                    hand["why"] = str(payload["why"])
                snapshot = dict(hand)
            return 200, {"ok": True, "lane": lane, "raised": snapshot,
                         "live_lane": _live,
                         "delivered_to": publish("raise", {"lane": lane, **snapshot})}

        if path == "/switch":
            target = str(payload.get("to") or lane)
            return 200, {"ok": True, "live_lane": target,
                         "delivered_to": set_live(target)}

        if path == "/inspect":
            global _ask_n
            with _lock:
                _ask_n += 1
                ask = "q%d" % _ask_n
                _asked[ask] = [threading.Event(), None]
            sent = publish("inspect", {"id": ask, "lane": lane,
                                       "selector": payload.get("selector", "")})
            if not sent:
                with _lock:
                    _asked.pop(ask, None)
                return 409, {"error": "nobody is looking — no browser is connected",
                             "remedy": "hand him the url from `status`"}
            ev, _ = _asked[ask]
            # A short wait: this is a localhost round trip through an already-open
            # stream. If it does not answer in a second the page is wedged, and
            # saying so beats hanging.
            ok = ev.wait(1.5)
            with _lock:
                answer = _asked.pop(ask, [None, None])[1]
            if not ok or answer is None:
                return 504, {"error": "the page did not answer in time",
                             "selector": payload.get("selector", "")}
            return 200, {"ok": True, "selector": payload.get("selector", ""),
                         **answer}

        if path == "/inspected":
            ask = str(payload.get("id") or "")
            with _lock:
                slot = _asked.get(ask)
                if slot:
                    slot[1] = {k: v for k, v in payload.items() if k != "id"}
                    slot[0].set()
            return 200, {"ok": True}

        if path == "/placed":
            # The page telling us what it measured. Not fanned out — this is
            # inbound truth, and echoing it would be a loop.
            with _lock:
                _geometry[lane] = payload.get("frames") or {}
                _viewport[lane] = payload.get("viewport") or {}
            # Geometry is persisted so a restored canvas keeps its layout rather
            # than re-packing into a different one. This fires often; the
            # debounce is what makes that free.
            _store.touch()
            return 200, {"ok": True}

        return 404, {"error": "not found"}

    def _camera(self, path: str, payload: dict, lane: str) -> tuple[int, dict]:
        """`point` / `look` / `zoom` — refused from a lane that is not live.

        This is the rule the whole lanes design exists for: an agent cannot
        take the camera from the agent that has it. The refusal names the live
        lane so the caller knows to `raise` instead of retrying.
        """
        # 🔴 ARMING is the one camera-adjacent thing a BACKGROUND lane may do,
        # and refusing it defeats the whole feature: an armed cue exists
        # precisely because the lane is not live yet. Nothing moves now — the
        # schedule is stored and only runs if and when he switches here, which
        # is still his decision. Everything else stays refused.
        if lane != _live and not (path == "/cue" and payload.get("arm")):
            return 409, {"error": "lane %r is not live — %r has the floor" % (lane, _live),
                         "live_lane": _live, "lane": lane,
                         "remedy": "command-bridge raise --lane %s --why '<what you want to show>'"
                                   % lane}

        if path == "/look":
            msg = {"id": payload.get("id") or "", "all": bool(payload.get("all")),
                   "lane": lane}
            if msg["id"] and msg["id"] not in _lanes.get(lane, {}):
                return 404, {"error": "no frame %r in lane %r" % (msg["id"], lane),
                             "frames": list(_lanes.get(lane, {}))}
            return 200, {"ok": True, "delivered_to": publish("look", msg), **msg}

        if path == "/point":
            msg = {"selector": payload.get("selector", ""),
                   "zoom": bool(payload.get("zoom")),
                   "look": bool(payload.get("look")), "lane": lane}
            out = {"ok": True, "delivered_to": publish("point", msg), **msg}
            # A highlight the viewer cannot see is a reference with no referent.
            target = str(msg["selector"] or "").lstrip("#.")
            vp = _viewport.get(lane) or {}
            if target and target in (vp.get("offscreen") or []) \
                    and not (msg["zoom"] or msg["look"]):
                out["warning"] = ("frame %r is off screen — run `look %s` (or pass --look) "
                                  "or he will not see it" % (target, target))
            return 200, out

        if path == "/cue":
            return self._cue(payload, lane)

        msg = {"selector": payload.get("selector", ""), "scale": payload.get("scale"),
               "lane": lane}
        return 200, {"ok": True, "delivered_to": publish("zoom", msg), **msg}

    def _cue(self, payload: dict, lane: str) -> tuple[int, dict]:
        """One clip's worth of highlights, sent as ONE event.

        The schedule runs in the page, not here: N separately-timed messages
        would put the network jitter inside the sentence, and a CLI that slept
        for the length of its own clip could not listen while it spoke.
        """
        if payload.get("cancel"):
            with _lock:
                _armed.pop(lane, None)
            msg = {"cancel": True, "lane": lane}
            return 200, {"ok": True, "cancelled": True, "lane": lane,
                         "delivered_to": publish("cue", msg)}

        text = str(payload.get("text") or "")
        if not text:
            return 400, {"error": "cue needs --text (or --cancel)",
                         "remedy": "command-bridge cue --text 'the box [point:a] on the left'"}

        plan = cue_schedule(text, payload.get("words"), payload.get("seconds"))
        msg = {"marks": plan["marks"], "timing": plan["timing"], "lane": lane}

        # ARMED: the schedule is set but the clock does not start until this lane
        # goes live. That is the held-clip case — a background agent's clip plays
        # BY ITSELF the moment he switches, and the agent gets no event at that
        # instant, so the timers have to already be in the page.
        if payload.get("arm"):
            msg["arm"] = True
            msg["lead_ms"] = int(float(payload.get("lead") or 0) * 1000)
            # An armed cue may bring the camera with it. Without this the first
            # highlight can land on a frame that is not in view — a reference
            # with no referent — and the agent cannot fix it, because it is not
            # live at the moment the schedule starts.
            if payload.get("look"):
                msg["look"] = str(payload["look"])
            with _lock:
                _armed[lane] = msg
            out = {"ok": True, "armed": True, "lane": lane,
                   "delivered_to": publish("cue", msg), **plan,
                   "lead_ms": msg["lead_ms"],
                   "note": "fires when lane %r goes live, after lead_ms" % lane}
            return 200, out

        out = {"ok": True, "delivered_to": publish("cue", msg), **plan, "lane": lane}

        # Same referent check `point` makes, once per mark. The server only knows
        # about FRAMES — a selector naming something inside one is resolved in the
        # page and skipped there if it misses, exactly as a plain `point` does.
        frames = _lanes.get(lane, {})
        vp = _viewport.get(lane) or {}
        offscreen = set(vp.get("offscreen") or [])
        warnings = []
        for mk in plan["marks"]:
            sel = str(mk.get("selector") or "").lstrip("#.")
            if not sel:
                warnings.append("a mark has an empty selector and will do nothing")
            elif sel in offscreen:
                warnings.append("frame %r is off screen — he will not see that mark"
                                % sel)
            elif sel in frames:
                mk["frame"] = True
        if warnings:
            out["warnings"] = warnings
        return 200, out


# ---- module-level access to the operations, for an aiohttp host ------------
# `apply` / `_camera` / `_cue` / `_batch` touch only the module state and
# `publish()` — never the request socket — and return `(code, body)` rather than
# writing HTTP. So a Handler instance built WITHOUT its socket-bound __init__ can
# run them, which is what lets command-bridge's aiohttp server reuse the exact
# frame-op logic instead of keeping a second copy that would drift.
_ops = Handler.__new__(Handler)


def apply(path: str, payload: dict) -> tuple[int, dict]:
    """One canvas operation by route (`/frame`, `/point`, `/switch`, …). Pure w.r.t. HTTP:
    mutates the canvas state, fans an SSE event through `publish`, and returns `(status, body)`."""
    return _ops.apply(path, payload)


def run_batch(payload: dict) -> list[dict]:
    """Many operations in order; one failure returns its error in place and the rest still run."""
    return _ops._batch(payload)


def serve(port: int = DEFAULT_PORT, verbose: bool = False,
          fresh: bool = False, follow: bool = True,
          session: str = "dev") -> None:
    """Run until interrupted. Binds loopback only — nothing else can reach it."""
    global _live, _store, _follower
    _store = Store()
    restored = 0
    if fresh:
        # Start empty WITHOUT destroying what is on disk: `--fresh` is "not
        # today's canvas", not "throw the canvas away". The saved one comes
        # back on the next normal start, and the first change here overwrites
        # it — which is the same bargain any editor makes.
        print(json.dumps({"fresh": True, "canvas_file": str(_store.path)}), flush=True)
    else:
        lanes, geo, live = _store.load()
        with _lock:
            _lanes.update(lanes)
            _geometry.update(geo)
            if live:
                _live = live
        restored = sum(len(f) for f in lanes.values())
    _store.start(_canvas_snapshot)

    # Soft: if there is no voice tunnel, this finds nothing, says so in
    # `status`, and never mentions it again.
    _follower = Follower(session=session, enabled=follow)
    _follower.start(set_live, live_lane)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.verbose = verbose  # type: ignore[attr-defined]
    url = "http://127.0.0.1:%d/" % port
    # The state file is how the client finds a server on a non-default port,
    # so the agent never has to remember which one it started.
    STATE_FILE.write_text(json.dumps({"port": port, "url": url, "started": time.time()}))
    print(json.dumps({"serving": url, "port": port, "page_version": PAGE_VERSION,
                      "canvas_file": str(_store.path),
                      "restored_frames": restored,
                      "restored_lanes": sorted(_lanes)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE_FILE.unlink(missing_ok=True)
