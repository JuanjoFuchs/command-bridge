"""The canvas rides the one server (spec 003).

The picture rendering is verified live with `command-bridge shot` (a screenshot needs a real
browser). What is unit-testable — and what would silently rot — is that the routes are actually
mounted on the voice server, that the frame ops behave, and that the surface stayed DUMB.
"""
import ast
import pathlib

from command_bridge import server as cb_server
from command_bridge import store
from command_bridge.canvas import server as canvas
from command_bridge.canvas import page as canvas_page


def _routes(app):
    paths = set()
    for res in app.router.resources():
        info = res.get_info()
        paths.add(info.get("path") or info.get("formatter") or "")
    return paths


def test_the_canvas_routes_are_mounted_on_the_one_server():
    """FR1/FR2: no second server — /events, the canvas page, and the op routes are on the SAME
    aiohttp app that carries the voice channel."""
    app = cb_server.build_app("cvtest", "tok")
    paths = _routes(app)
    assert "/events" in paths, paths          # the SSE stream
    assert "/canvas" in paths                 # the canvas page
    assert "/placed" in paths                 # the page's geometry callback
    assert "/draw" in paths                    # spec 015: the page's `send` posts the sketch here
    assert any(p.startswith("/canvas/") for p in paths)  # the /canvas/{op} ops
    # And the voice contract is still there, untouched (NFR1).
    for voice in ("/", "/status", "/ws", "/say"):
        assert voice in paths, f"{voice} missing — the voice routes must be untouched"


def test_a_frame_op_sets_stores_and_reports():
    """The op path aiohttp calls: set a frame, and it lands in the lane's store and reports it."""
    lane = "_t_ops"
    code, body = canvas.apply("/frame", {"id": "x", "kind": "html", "content": "<b>hi</b>",
                                         "lane": lane})
    assert code == 200 and body["ok"] and body["id"] == "x"
    assert "x" in canvas._lanes.get(lane, {})
    # remove is scoped and reports the count
    code, body = canvas.apply("/remove", {"id": "x", "lane": lane})
    assert code == 200 and body["removed"] == "x"
    canvas._lanes.pop(lane, None)


def test_a_camera_verb_is_refused_from_a_lane_that_is_not_live():
    """The refusal is the lane rule the whole design rests on — an off-lane agent cannot grab the
    camera. It survives the merge because the same apply() enforces it."""
    canvas.set_live("_t_live")
    code, body = canvas.apply("/point", {"selector": "#a", "lane": "_t_other"})
    assert code == 409 and body["live_lane"] == "_t_live"


def test_a_point_survives_a_switch_away_and_back():
    """2026-09-07: a `point` was published and then forgotten, so JJ pointing at a box, switching
    to another agent, and switching back left the box unmarked — 'the pointing didn't work'. The
    point is now stored per lane and re-published when the lane goes live again."""
    import queue as _q
    canvas.set_live("_pt_a")
    canvas.apply("/point", {"selector": "onlane", "lane": "_pt_a"})
    assert canvas._point.get("_pt_a", {}).get("selector") == "onlane", "the point must be stored"

    sub = _q.Queue()
    canvas._subscribers.append(sub)
    try:
        canvas.set_live("_pt_b")          # switch AWAY
        canvas.set_live("_pt_a")          # switch BACK
        events = []
        while not sub.empty():
            events.append(sub.get_nowait())
    finally:
        canvas._subscribers.remove(sub)

    points = [p for (e, p) in events if e == "point" and p.get("lane") == "_pt_a"]
    assert points and points[-1]["selector"] == "onlane", \
        "switching back to a lane must restore its point"
    canvas._point.pop("_pt_a", None)
    canvas._point.pop("_pt_b", None)


def test_an_empty_selector_clears_the_stored_point():
    """`point` with no selector is the CLEAR — it must remove the stored point, not persist a
    highlight of nothing that would re-appear on the next switch or reconnect."""
    canvas.set_live("_pt_c")
    canvas.apply("/point", {"selector": "box", "lane": "_pt_c"})
    assert "_pt_c" in canvas._point
    canvas.apply("/point", {"selector": "", "lane": "_pt_c"})
    assert "_pt_c" not in canvas._point


def test_a_bad_batch_op_is_reported_not_fatal():
    results = canvas.run_batch([{"op": "set", "id": "b", "content": "y", "lane": "_t_b"},
                                {"op": "nope"}])
    assert results[0].get("ok")
    assert "unknown op" in results[1].get("error", "")
    canvas._lanes.pop("_t_b", None)


def test_the_canvas_surface_stays_dumb():
    """FR4 / the one rule: the canvas holds no model. No canvas module may import an LLM client —
    a surface that grew a model would violate the split that makes the tool verifiable."""
    banned = {"openai", "anthropic", "transformers", "torch", "llama_cpp", "google",
              "cohere", "mistralai", "ollama"}
    canvas_dir = pathlib.Path(cb_server.__file__).resolve().parent / "canvas"
    offenders = []
    for path in canvas_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                if n in banned:
                    offenders.append(f"{path.name}:{node.lineno} imports {n!r}")
    assert not offenders, "the canvas grew a model:\n  " + "\n  ".join(offenders)


# ============================================================ spec 015 — a canvas he can draw on


def test_an_ink_frame_stores_and_persists_via_the_existing_frame_path():
    """T1/AC1: an `ink` frame is just a frame with kind:ink and an svg body — it rides the SAME
    `/frame` machinery (no new storage path), so its kind and content are kept verbatim."""
    lane = "_ink_store"
    svg = '<svg><path d="M0 0 L10 10"/></svg>'
    code, body = canvas.apply("/frame", {"id": "ink-0", "kind": "ink", "content": svg,
                                         "at": [40, 50], "lane": lane})
    assert code == 200 and body["kind"] == "ink"
    stored = canvas._lanes.get(lane, {}).get("ink-0")
    assert stored and stored["kind"] == "ink" and stored["content"] == svg
    assert stored["at"] == [40, 50], "an ink frame carries its canvas-space placement"
    canvas._lanes.pop(lane, None)


def test_draw_stores_an_ink_frame_on_the_live_lane_and_surfaces_a_canvas_turn(tmp_sessions):
    """T3/AC3/AC4: `send` posts the strokes; `/draw` stores ONE ink frame on the LIVE lane and
    appends a `source:"canvas"` turn that the agent's `watch` returns, naming the frame."""
    lane = "_ink_live"
    session = "inkdraw"
    canvas.set_live(lane)
    canvas._session = session
    svg = '<svg viewBox="0 0 30 20"><path d="M1 1 L20 15"/></svg>'
    code, body = canvas.apply("/draw", {"content": svg, "at": [100, 120], "title": "sketch"})

    # one ink frame, on the live lane, auto-ided
    assert code == 200 and body["ok"] and body["kind"] == "ink" and body["lane"] == lane
    assert body["source"] == "canvas"
    fid = body["id"]
    assert fid == "ink-0"
    stored = canvas._lanes.get(lane, {}).get(fid)
    assert stored and stored["content"] == svg and stored["at"] == [100, 120]

    # AC4: the agent's watch (lane-filtered, addressed-only) returns it as a canvas turn
    turns, _ = store.watch(session, -1, timeout=0.5, addressed_only=True,
                           lane=lane, default_lane=lane)
    canvas_turns = [t for t in turns if t.get("source") == "canvas"]
    assert canvas_turns, "the draw must surface a source:canvas turn to the agent"
    t = canvas_turns[-1]
    assert t["lane"] == lane and t["addressed"] is True
    assert fid in t["text"] and lane in t["text"], t["text"]
    assert body["turn"] == t["id"]
    canvas._lanes.pop(lane, None)


def test_draw_lands_on_the_live_lane_even_when_the_payload_names_another(tmp_sessions):
    """KD5: his ink lands on the lane he is LOOKING AT (the live one), never on a lane a payload
    names — a human on a phone has no concept of a caller's lane."""
    canvas.set_live("_ink_here")
    canvas._session = "inklane"
    code, body = canvas.apply("/draw", {"content": "<svg></svg>", "lane": "_ink_elsewhere"})
    assert code == 200 and body["lane"] == "_ink_here"
    assert "ink-0" in canvas._lanes.get("_ink_here", {})
    assert "_ink_elsewhere" not in canvas._lanes
    canvas._lanes.pop("_ink_here", None)


def test_successive_sketches_coexist_as_separate_ink_frames(tmp_sessions):
    """KD4: his raw sketch and (later) the agent's polish sit side by side — so each `send` gets a
    fresh, collision-free id rather than replacing the last drawing."""
    lane = "_ink_multi"
    canvas.set_live(lane)
    canvas._session = "inkmulti"
    a = canvas.apply("/draw", {"content": "<svg>a</svg>"})[1]
    b = canvas.apply("/draw", {"content": "<svg>b</svg>"})[1]
    assert a["id"] == "ink-0" and b["id"] == "ink-1"
    assert set(canvas._lanes.get(lane, {})) >= {"ink-0", "ink-1"}
    canvas._lanes.pop(lane, None)


def test_an_ink_annotation_lands_over_the_frame_it_was_drawn_on(tmp_sessions):
    """FR2a/AC5 (data layer): a stroke drawn on top of an agent frame is stored as an ink frame on
    the SAME lane, positioned in the frame's region — the annotation lands on the shared canvas.
    (The pixel-level overlay is verified in the browser by scripts/drawtest.py.)"""
    lane = "_ink_annot"
    canvas.set_live(lane)
    canvas._session = "inkannot"
    # an agent frame occupying canvas box [200,200]..[500,360]
    canvas.apply("/frame", {"id": "watch-logic", "kind": "mermaid", "content": "graph TD;A-->B",
                            "at": [200, 200], "lane": lane})
    # a circle scribbled ON it: bbox ~[260,240]..[420,320]
    ink = canvas.apply("/draw", {"content": '<svg viewBox="0 0 160 80"><path d="M0 0 L160 80"/></svg>',
                                 "at": [260, 240]})[1]
    frames = canvas._lanes.get(lane, {})
    assert "watch-logic" in frames and ink["id"] in frames, "the annotation coexists with its target"
    ink_at = frames[ink["id"]]["at"]
    # the ink origin sits INSIDE the target frame's box — i.e. the mark is on the diagram
    assert 200 <= ink_at[0] <= 500 and 200 <= ink_at[1] <= 360, ink_at
    canvas._lanes.pop(lane, None)


def test_the_page_renders_ink_and_carries_the_drawing_toolbar():
    """The render path actually wires the drawing layer: the page has the pen/erase/send toolbar,
    an ink render branch, a `/draw` submit, and it does NOT gate drawing on a secure context (TC3)."""
    html = canvas_page.render()
    for needle in ('id="tool-pen"', 'id="tool-erase"', 'id="tool-send"', 'ink-live',
                   'kind === "ink"', 'fetch("/draw"', "pointerdown"):
        assert needle in html, f"the page is missing {needle!r}"
    assert "isSecureContext" not in html, "drawing must not be gated on a secure context (FR4/TC3)"
