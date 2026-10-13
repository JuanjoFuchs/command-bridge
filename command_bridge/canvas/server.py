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
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import CANVAS_READABLE_PX
from . import folder as canvas_folder
from .cue import schedule as cue_schedule
from .follow import Follower
from .page import PAGE_VERSION, render

# The readable floor for a focused frame's smallest text (spec 019). `readable = min_font_px >=
# READABLE_PX`, where `min_font_px` is the EFFECTIVE min font the PAGE measured — the frame's
# smallest text times spec 018's contain-fit scale, i.e. the px that text renders at when `look`
# frames this frame on his screen. Lives in `config.py` as data (convention 5); re-exported here
# because this module derives `readable` against it and `cli.describe` publishes it.
READABLE_PX = CANVAS_READABLE_PX

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

# lane -> the CURRENT static point on that lane's canvas, or absent when nothing
# is pointed. A `point` was published and then forgotten, so it did not survive
# the two things that happen constantly mid-explanation: JJ switching AWAY from a
# lane and back (the point was never re-sent, so it vanished — "the pointing
# didn't work", 2026-09-07), and a page reconnecting (the SSE replay carried
# frames and armed cues but not the highlight). Stored here so both restore it,
# exactly as `_armed` is restored — a reference whose referent disappears the
# moment his attention returns is worse than no reference at all.
_point: dict[str, dict] = {}

# lane -> the ONE camera move (`look <id>` / `look --all`) an off-lane agent asked for and could
# not run, because a background lane may not move the camera (see `_camera`). Held here instead of
# refused (spec 017), and drained by `set_live` the instant that lane goes live — BEFORE the lane's
# held voice clips play — so JJ lands already looking at the frame the clip is about, the camera
# half of the held-clip mechanism spec 013 gave voice. At most ONE per lane (KD1/KD2): a later
# `look` with nothing spoken between overwrites the earlier one, because the camera has a single
# position, not a timeline. Only the INTENT is held (the frame id / `--all`), never frozen pixel
# coordinates — the actual camera is recomputed in the page from its current viewport on replay
# (KD3), so a window resized between the hold and the switch still frames correctly. In-memory like
# `_armed` and `_hands`: it survives until the switch, not across a restart.
_look_held: dict[str, dict] = {}

# In-flight `inspect` questions: id -> [Event, answer]. The page is the only
# thing that knows what a selector resolves to, so the answer has to come back
# FROM it — one request out on the stream, one POST back.
_asked: dict[str, list] = {}
_ask_n = 0

# WHICH SESSION'S TURN LOG a human draw/point surfaces into (spec 015 AC4). The canvas writes the
# SAME `<session>.jsonl` the voice server and `command-bridge watch` read, so an inbound canvas
# action rides the one agent contract — watch → turns → act — instead of a second channel the agent
# would have to poll. Set by `serve` / `init_canvas`; the default matches the voice server's so an
# inert import (tests, `describe`) never guesses a real session.
_session = "dev"

# The canvas on disk: a folder of native files per lane (spec 022). Replaced by `serve` /
# `init_canvas`; the default is inert so importing the module (tests, `describe`) never touches a
# real folder.
_store = canvas_folder.FolderStore(Path("."), enabled=False)

# The file watcher that turns an agent's edits into frame ops (spec 022 FR3). None until serve.
_watcher = None


def canvas_root(session: str) -> Path:
    """`sessions/<session>.canvas/` (spec 022 KD9): beside the turn log and timing log it belongs
    with, per-session prefixed, and inside the already-gitignored `sessions/` runtime-state dir."""
    from ..config import session_dir
    return Path(session_dir()) / ("%s.canvas" % session)


def lane_folder(lane: str) -> str | None:
    """Where an agent writes a frame for `lane` — the path `status` and `describe` hand out."""
    if not _store.enabled:
        return None
    return str(_store.lane_dir(lane))


_liaisons: frozenset[str] = frozenset()
"""Specs 037/038: the liaison lanes. While any is set, edits never move the camera and only a
liaison's explicit camera moves do. Set by the voice server (`set_liaisons`), which owns the mode."""


def set_liaisons(liaisons) -> None:
    global _liaisons
    _liaisons = frozenset(liaisons or ())


def _on_file_upsert(lane: str, frame: dict) -> None:
    """The watcher saw a frame file created or changed: render it, and focus it (spec 022 FR3/FR7).

    Focus rides ON the frame event (`focus: True`) for the live lane, so the page moves the camera
    only once the card has rendered and been laid out — a separate `look` would race the paint and
    frame an empty card. Off the live lane the look is HELD (spec 017) and a later edit overwrites it,
    which is the coalesce-to-latest JJ asked for. A `_`-prefixed file never takes focus.

    In LIAISON MODE (spec 037) no edit takes focus, on any lane: JJ, "I want you to focus on a frame
    while you speak to me about that frame" — an agent's save must not pull the camera off it."""
    quiet = canvas_folder.background(frame.get("file") or "") or bool(_liaisons)
    apply("/frame", {**frame, "lane": lane, "focus": not quiet})
    if not quiet and lane != _live:
        _ops._hold_look({"id": frame["id"]}, lane)


def _on_file_delete(lane: str, fid: str, name: str) -> None:
    """The watcher saw a frame file deleted: remove the frame. Removing IS deleting the file (FR8)."""
    apply("/remove", {"id": fid, "lane": lane, "file_gone": True})


def start_folder(session: str, fresh: bool = False) -> int:
    """Point the canvas at `sessions/<session>.canvas/`, migrate the old single-file store once,
    load every lane folder, and start the index writer and the watcher. Returns frames restored."""
    global _store, _watcher
    root = canvas_root(session)
    migrated = canvas_folder.migrate(canvas_folder.LEGACY_FILE, root)
    if migrated:
        print(json.dumps({"canvas_migrated": migrated}), file=sys.stderr, flush=True)
    _store = canvas_folder.FolderStore(root)
    restored = 0
    if not fresh:
        lanes, geo, _ = _store.load()
        with _lock:
            _lanes.update(lanes)
            _geometry.update(geo)
        restored = sum(len(f) for f in lanes.values())
    _store.start(_canvas_snapshot)
    if _watcher is not None:
        _watcher.stop()
    from .watcher import Watcher
    _watcher = Watcher(_store, _on_file_upsert, _on_file_delete)
    _watcher.start()
    return restored

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
        pt = _point.get(lane)
        # A camera move held while this lane was in the background (spec 017). Popped here, published
        # below, and gone — it fires exactly once, on the switch it was waiting for.
        look = _look_held.pop(lane, None)
    _store.touch()
    n = publish("switch", {"lane": lane})
    # REPLAY A HELD `look` BEFORE THE HELD CLIPS (spec 017 FR2/FR3). The voice server flushes this
    # lane's held voice clips immediately AFTER this returns (`_set_lane` → `set_live` → then
    # `_flush_lane_held`), so publishing the camera move here — synchronously, before the flush —
    # is what lands JJ on the frame FIRST, then starts the clip that talks about it. Published after
    # the `switch` event so the page has already made this lane's canvas current, and before the
    # point restore so the deliberate pre-position wins the camera and the highlight lands on top.
    if look:
        publish("look", look)
    # RESTORE THE LANE'S POINT (2026-09-07). The page renders the newly-live lane fresh, so a
    # highlight set on it earlier is gone until re-sent. Without this, pointing at a box, switching
    # away to another agent, and switching back left the box unmarked — the referent vanished the
    # moment his attention returned. Published AFTER the switch so the lane's canvas is up first.
    if pt:
        publish("point", pt)
    return n


def live_lane() -> str:
    return _live


def _surface_canvas_turn(lane: str, text: str) -> dict | None:
    """Append a canvas-sourced turn so the agent's `watch` returns it (spec 015 AC4).

    The whole agent contract is `watch` → turns → act; an inbound canvas action (a draw, later a
    point) reaches the agent the SAME way speech does — as a turn on its watch — rather than a
    second inbound channel it would have to poll. Tagged `source="canvas"` so the agent knows to go
    `shot` the frame and read it, and addressed to `lane` (the live lane he drew on) so that lane's
    own lane-filtered watch delivers it and no one else's.

    Best-effort by design: the frame is already stored and on screen before this runs, so a log
    write that fails must never fail the draw. The turn-log store is imported lazily (and named to
    stay distinct from this module's canvas `Store`) so the module's import surface is unchanged.
    """
    from .. import store as turnlog

    try:
        now = time.time()
        return turnlog.append_turn(
            session=_session, text=text, t_start=now, t_end=now,
            addressed=True, reason="canvas", lane=lane, stamp_lane=True, source="canvas")
    except Exception:  # noqa: BLE001 — surfacing is best-effort; the draw itself already succeeded
        return None


DRAW_SHOTS_DIR = Path.home() / ".command-bridge-draws"
"""Where an auto-shot of a human draw lands. Home-based so it is always writable, and the EXACT path
rides in the canvas turn's text — the agent reads the file the turn names, it never guesses the dir."""


def _draw_shot_path(lane: str, fid: str) -> Path:
    """A collision-free PNG path for this draw. The millisecond stamp keeps a re-drawn `ink-0` from
    overwriting a previous shot before the agent has read it, and the sanitise keeps a lane or frame
    id from escaping the directory."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in "%s-%s" % (lane, fid))
    return DRAW_SHOTS_DIR / ("%s-%d.png" % (safe, int(time.time() * 1000)))


def _spawn_draw_shot(lane: str, out: Path) -> None:
    """Shoot `lane`'s canvas in a private browser, in the BACKGROUND, so a human draw reaches the
    agent WITH its picture rather than a pointer to go run `shot` (JJ 2026-09-09: "wouldn't it be
    better that whatever you receive is the screenshot").

    A subprocess, on purpose and twice over: the shot drives sync Playwright, which cannot run inside
    this server's event loop; and a separate process means a missing browser degrades to 'the file
    never appears', never a wedged draw. Fire-and-forget — the frame and the turn are already
    delivered, so the picture simply arrives a few seconds behind, at the path the turn already named.
    `python -m command_bridge` rather than the console script, so it runs whether or not `bin/` is on
    the server's PATH (the same reason __main__.py exists)."""
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [sys.executable, "-m", "command_bridge", "shot",
             "--session", _session, "--lane", lane, "--out", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except Exception:  # noqa: BLE001 — a shot that cannot even start must not fail the draw
        pass


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


def _frame_view(lane: str, fid: str, measured: bool = True) -> dict:
    """A frame as `status` reports it: what it is, plus where it landed.

    `measured` says whether a page is connected (its geometry can be trusted): with none, the spec-019
    legibility is nulled the same way the viewport is, since a stale reading is a lie (FR2)."""
    f = _lanes[lane][fid]
    out = {"id": fid, "kind": f["kind"], "title": f["title"]}
    # A render failure the page reported (JJ, 2026-09-24) — carried on the frame, not the geometry, so
    # it survives with no live page and reaches an agent that drew off the lane JJ is watching.
    if f.get("render_error"):
        out["render_error"] = f["render_error"]
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
    # spec 019: the effective min font, fit scale and `readable` verdict — nulled when no page is
    # connected (see `measured`). frame_legibility re-states min_font_px / fit_scale from the geometry
    # merged above and adds `readable`, so a stale geo value cannot leak in as a live reading.
    out.update(frame_legibility(geo if measured else None))
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


def viewport_with_aspect(vp: dict | None) -> dict | None:
    """The reported viewport with its DERIVED aspect ratio folded in (spec 018 FR3/KD2).

    `aspect = width / height` (2 dp) — a value an agent reads to author a diagram toward the canvas
    shape (a wide canvas gets a wide diagram) instead of guessing and drawing a tall frame that never
    fits. Derived here, from the single measured `width`/`height` the page pushes to `/placed`, so it
    is never a second inferable number that can disagree with the ratio the fit uses (NFR1). Returns
    None unchanged when there is no viewport (no page connected — a stale ratio is a lie), and leaves
    a viewport that carries no width/height untouched rather than inventing an aspect."""
    if not vp:
        return None
    out = dict(vp)
    w, h = out.get("width"), out.get("height")
    if w and h:
        out["aspect"] = round(w / h, 2)
    return out


def frame_legibility(geo: dict | None) -> dict:
    """Per-frame legibility for `status` (spec 019 FR2) — the same page-measures / server-relays path
    as the viewport (KD1).

    Reads the EFFECTIVE min font + contain-fit scale the page pushed to `/placed` (`min_font_px`,
    `fit_scale`) out of the frame's geometry, and derives `readable = min_font_px >= READABLE_PX`. The
    server RELAYS; it never computes a font size (TC1). Pass `geo=None` when no page is connected — the
    values come back null, exactly as the viewport is null with no page, because a stale legibility is
    a lie an agent would author against."""
    g = geo or {}
    mf = g.get("min_font_px")
    fs = g.get("fit_scale")
    readable = (mf >= READABLE_PX) if isinstance(mf, (int, float)) else None
    return {"min_font_px": mf, "readable": readable, "fit_scale": fs}


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
            frames = [_frame_view(lane, fid, measured=bool(clients))
                      for fid in _lanes.get(lane, {})]
            # With nobody connected there IS no viewport. Say so rather than
            # reporting a stale one: an agent told "nothing is visible" behaves
            # correctly, an agent told a lie does not. When there IS one, its
            # aspect ratio rides along so an agent can author to the canvas shape
            # (spec 018 FR3).
            viewport = viewport_with_aspect(_viewport.get(lane)) if clients else None
            body = {"clients": clients, "lane": lane, "live_lane": _live,
                    "lanes": _lane_summary(), "frames": frames,
                    "viewport": viewport, "page_version": PAGE_VERSION,
                    # Named so a human can find, inspect or delete the record
                    # without being told where it lives.
                    "canvas_dir": str(_store.root) if _store.enabled else None,
                    "folder": lane_folder(lane),
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
    # spec 022: no draw/remove/clear/look/zoom op — a frame is a file in the lane folder, and focus
    # follows the latest edit. What an agent still CALLS is pointing and switching.
    OPS = {"point": "/point", "cue": "/cue", "switch": "/switch", "relayout": "/relayout",
           "show": "/show"}

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
            # EVERY FRAME IS A FILE (spec 022 FR9). The watcher arrives here with the file it read
            # (`file`); anything else — a generator (`chart`, the human's ink), an internal script —
            # is written into the lane folder FIRST, atomically, with its hash recorded so the
            # watcher recognises it as already on screen (TC3). One ingestion path, one home for
            # content: the file.
            if payload.get("file"):
                frame["file"] = str(payload["file"])
            elif _store.enabled:
                try:
                    frame["file"] = _store.write_frame(lane, fid, str(frame["kind"]),
                                                       str(frame["content"]))
                except OSError as exc:
                    return 500, {"error": "could not write the frame file: %s" % exc,
                                 "folder": lane_folder(lane)}
            # The full path rides on the frame so the page's "copy path" button can hand it to him
            # (JJ, 2026-10-01: "another one to copy a path to the file the frame is rendering").
            folder = lane_folder(lane)
            if frame.get("file") and folder:
                frame["path"] = str(Path(folder) / str(frame["file"]))
            with _lock:
                # STICKY (spec 022 FR6): a re-render keeps the position the frame already has, so
                # editing a file never makes it jump.
                old = _lanes.get(lane, {}).get(fid)
                if old and old.get("at") and not frame.get("at"):
                    frame["at"] = old["at"]
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
            # `focus` rides on the EVENT, never the stored frame, so a reconnect's replay of the
            # backlog does not fly the camera around (spec 022 FR7).
            event = {**frame, "lane": lane}
            if payload.get("focus"):
                event["focus"] = True
            return 200, {"ok": True, "delivered_to": publish("frame", event),
                         "id": fid, "lane": lane, "kind": frame["kind"], "frames": count,
                         **({"file": frame["file"]} if frame.get("file") else {})}

        if path == "/draw":
            # THE HUMAN DREW (spec 015 T3, KD5). Draw-then-submit: the page hands the accumulated
            # strokes over as ONE svg, and this stores them as an `ink` frame and surfaces the sketch
            # to the agent as a turn its `watch` returns.
            #
            # A DEDICATED OP, NOT `/frame` with kind:ink — and both halves of that are load-bearing.
            # (1) His ink must land on the lane he is LOOKING AT — the live lane — never on a lane an
            # agent named in a payload; a human on a phone has no concept of the caller's lane. (2)
            # `/frame` is the agent's OUTBOUND verb (spec 011); firing a canvas turn on every agent
            # draw would be wrong, so the turn-surfacing lives HERE and `/frame` stays untouched (the
            # constraint: this adds the inbound direction, it does not touch the outbound one).
            target = _live
            with _lock:
                existing = _lanes.get(target, {})
                # A stable, collision-free id per sketch so several drawings coexist as SEPARATE
                # frames (KD4 — his raw sketch and the agent's polish sit side by side).
                fid = str(payload.get("id") or "")
                if not fid:
                    n = 0
                    while ("ink-%d" % n) in existing:
                        n += 1
                    fid = "ink-%d" % n
            # Reuse the `/frame` storage path verbatim (no second copy of the age/persist/fan-out
            # rules to drift) by handing it an ink frame pinned to the live lane. NOT under `_lock`:
            # `/frame` takes the lock itself and this Lock is not reentrant.
            code, body = self.apply("/frame", {
                "id": fid, "kind": "ink", "content": payload.get("content", ""),
                "title": payload.get("title") or "sketch", "lane": target,
                **({"at": payload["at"]} if payload.get("at") else {})})
            if code != 200:
                return code, body
            # SURFACE IT (AC4), and PUSH THE PICTURE, not a pointer (JJ 2026-09-09). The shot path is
            # computed FIRST so it rides in the very turn the draw surfaces; the shot itself runs in a
            # background process and lands at that path a few seconds later. Best-effort — the frame is
            # already stored and delivered to the page, so neither the turn nor the shot can fail it.
            shot_out = _draw_shot_path(target, fid)
            turn = _surface_canvas_turn(
                target, "drew on lane %s, frame %s · shot: %s" % (target, fid, shot_out))
            _spawn_draw_shot(target, shot_out)
            return 200, {**body, "kind": "ink", "source": "canvas", "shot": str(shot_out),
                         "turn": (turn.get("id") if turn else None)}

        if path == "/remove":
            fid = str(payload.get("id") or "")
            with _lock:
                known = fid in _lanes.get(lane, {})
                gone = _lanes.get(lane, {}).pop(fid, None)
                (_geometry.get(lane) or {}).pop(fid, None)
                # A held focus on a frame that no longer exists would replay a look at nothing.
                if (_look_held.get(lane) or {}).get("id") == fid:
                    _look_held.pop(lane, None)
                count = len(_lanes.get(lane, {}))
            # Removing a frame IS deleting its file (spec 022 FR8) — otherwise it would come back
            # on the next restart. Skipped when the watcher is reporting the file already gone.
            if gone and gone.get("file") and not payload.get("file_gone") and _store.enabled:
                _store.delete_frame(lane, gone["file"])
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
                dropped = _lanes.pop(lane, None) or {}
                _geometry.pop(lane, None)
                _viewport.pop(lane, None)
                # A camera move held for a lane whose frames are being thrown away has nothing left
                # to focus (spec 017) — drop it so a later switch does not replay a look at nothing.
                _look_held.pop(lane, None)
                empty = not any(_lanes.values())
            # The files ARE the frames (spec 022): clearing a lane deletes its frame files.
            if _store.enabled:
                for f in dropped.values():
                    if f.get("file"):
                        _store.delete_frame(lane, f["file"])
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
                # PIN each frame where the page laid it (spec 022 FR6): from then on it is an `at`,
                # replayed to every page and written into the lane's index.json, so neither a
                # reconnect nor a restart re-packs the board. The page is the only placer (it alone
                # knows sizes), so its report is the truth — including the one case it moves a
                # pinned frame, an overlap with an older one that grew. Only a frame it actually
                # measured (non-zero size): a still-rendering card has not found its real spot yet.
                for fid, g in _geometry[lane].items():
                    f = _lanes.get(lane, {}).get(fid)
                    if (f is not None and isinstance(g, dict)
                            and (g.get("w") or 0) > 0 and (g.get("h") or 0) > 0):
                        f["at"] = [g.get("x", 0), g.get("y", 0)]
            # Geometry is persisted so a restored canvas keeps its layout rather
            # than re-packing into a different one. This fires often; the
            # debounce is what makes that free.
            _store.touch()
            return 200, {"ok": True}

        if path == "/relayout":
            # THE ON-DEMAND RE-PACK (spec 022 FR6). The board is sticky — a new frame never moves
            # the others — so when JJ WANTS it tidied, this forgets every pinned position on the lane
            # and has the page pack it once more from scratch; the new positions are then pinned again.
            with _lock:
                for f in _lanes.get(lane, {}).values():
                    f.pop("at", None)
                _geometry.pop(lane, None)
            _store.touch()
            return 200, {"ok": True, "lane": lane,
                         "delivered_to": publish("relayout", {"lane": lane})}

        if path == "/show":
            # THE AGENT OPENS OR CLOSES ITS OWN CANVAS (JJ, 2026-10-13: *"I would also like a way for
            # you to control when to open and close the canvas"*, for any agent with its own lane).
            # Dumb relay: the page decides, and only acts when the lane is the one he is on.
            want = str(payload.get("action") or "")
            if want not in ("open", "close"):
                return 400, {"error": "action must be open or close", "code": "invalid_input"}
            return 200, {"ok": True, "lane": lane, "action": want,
                         "delivered_to": publish("show", {"lane": lane, "action": want})}

        if path == "/rendered":
            # A diagram frame's RENDER OUTCOME (JJ, 2026-09-24): the page reports whether a mermaid
            # frame drew or threw. Merged onto the FRAME — not into `_geometry`, which /placed replaces
            # wholesale — and fired regardless of the live lane, so `status` (and `set` through
            # `_await_legibility`) can tell the DRAWING agent its diagram failed even when JJ is on
            # another lane. Without this the agent saw a silent "ok" on a blank frame and gave up.
            fid = str(payload.get("id") or "")
            err = payload.get("error")
            with _lock:
                frame = _lanes.get(lane, {}).get(fid)
                if frame is not None:
                    if err:
                        frame["render_error"] = str(err)
                    else:
                        frame.pop("render_error", None)
            return 200, {"ok": True}

        return 404, {"error": "not found"}

    def _camera(self, path: str, payload: dict, lane: str) -> tuple[int, dict]:
        """`point` / `look` / `zoom` — refused from a lane that is not live.

        This is the rule the whole lanes design exists for: an agent cannot
        take the camera from the agent that has it. The refusal names the live
        lane so the caller knows to just SAY what it has instead of retrying —
        an off-lane `say` is held for that lane and raises a hand with a count,
        which is the signal that brings him back (there is no `raise` command).
        """
        # LIAISON MODE (spec 037): the camera is the liaison's alone. Any other lane's camera move is
        # refused, live or not, held or not.
        if _liaisons and lane not in _liaisons:
            return 409, {"error": "liaison mode: only a liaison moves the camera (%s)" % ", ".join(sorted(_liaisons)),
                         "code": "liaison_mode", "liaisons": sorted(_liaisons), "lane": lane,
                         "remedy": "write your frame and brief your liaison; the liaison points at it"}
        # 🔴 ARMING is the one camera-adjacent thing a BACKGROUND lane may do,
        # and refusing it defeats the whole feature: an armed cue exists
        # precisely because the lane is not live yet. Nothing moves now — the
        # schedule is stored and only runs if and when he switches here, which
        # is still his decision. Everything else stays refused.
        if lane != _live and not (path == "/cue" and payload.get("arm")):
            # 🔴 A `look` from a background lane is HELD, not refused (spec 017). It is the one camera
            # move that is meaningful WITHOUT a clip — pre-positioning — and the verb JJ hit. `point`
            # and `cue` are speech-synced and ride their clip's schedule, so they replay when that
            # held clip replays and need no separate hold; `zoom` is a live-only attention nudge.
            # Everything but `look` therefore stays refused off-lane (TC1/KD2).
            if path == "/look":
                return self._hold_look(payload, lane)
            return 409, {"error": "lane %r is not live — %r has the floor" % (lane, _live),
                         "live_lane": _live, "lane": lane,
                         "remedy": "you can't take the camera from the live lane — just say what "
                                   "you have with `command-bridge say`; an off-lane say is held "
                                   "for your lane and raises a hand with a count, which is what "
                                   "brings him back"}

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
            # PERSIST so the highlight survives a switch-away-and-back and a reconnect (see
            # `_point`). An empty selector is the CLEAR, so it removes the stored point rather than
            # storing a highlight of nothing.
            if msg["selector"]:
                _point[lane] = msg
            else:
                _point.pop(lane, None)
            out = {"ok": True, "delivered_to": publish("point", msg), **msg}
            # A highlight the viewer cannot see is a reference with no referent.
            target = str(msg["selector"] or "").lstrip("#.")
            vp = _viewport.get(lane) or {}
            if target and target in (vp.get("offscreen") or []) \
                    and not (msg["zoom"] or msg["look"]):
                # spec 022 retired `look`; `point --look` is the camera move an agent still has.
                out["warning"] = ("frame %r is off screen — re-run `point %s --look` to bring "
                                  "the camera there, or he will not see it" % (target, target))
            return 200, out

        if path == "/cue":
            return self._cue(payload, lane)

        msg = {"selector": payload.get("selector", ""), "scale": payload.get("scale"),
               "lane": lane}
        return 200, {"ok": True, "delivered_to": publish("zoom", msg), **msg}

    def _hold_look(self, payload: dict, lane: str) -> tuple[int, dict]:
        """Hold an off-lane `look` (spec 017 FR1) instead of refusing it, and record the target so
        `set_live` can replay it the moment this lane goes live — before the lane's held clips.

        Validated exactly as the live `/look` is: an `id` naming a frame that is not on THIS lane
        (the caller's own canvas) is a typo worth catching, so it 404s rather than holding a camera
        move at nothing. An empty `id` (and no `--all`) is the deliberate 'frame the whole canvas'
        look, held as-is. Storing overwrites any earlier held `look` on this lane, which IS the
        collapse rule (FR4): the camera has one position, so two consecutive holds with nothing
        spoken between them keep only the last."""
        fid = str(payload.get("id") or "")
        if fid and fid not in _lanes.get(lane, {}):
            return 404, {"error": "no frame %r in lane %r" % (fid, lane),
                         "frames": list(_lanes.get(lane, {}))}
        msg = {"id": fid, "all": bool(payload.get("all")), "lane": lane}
        with _lock:
            _look_held[lane] = msg
            live = _live
        return 200, {"held": True, "id": fid or None, "all": msg["all"], "lane": lane,
                     "live_lane": live, "reason": "off_lane",
                     "next": "HELD — it focuses your frame when he switches to your lane, "
                             "before your held clip."}

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
    global _follower, _session
    _session = session  # spec 015: the log a human draw surfaces its canvas turn into (AC4)
    # spec 022: the lane folders under sessions/<session>.canvas/, watched. `--fresh` starts empty
    # WITHOUT destroying what is on disk — "not today's canvas", not "throw the canvas away".
    restored = start_folder(session, fresh=fresh)

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
                      "canvas_dir": str(_store.root),
                      "restored_frames": restored,
                      "restored_lanes": sorted(_lanes)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE_FILE.unlink(missing_ok=True)
