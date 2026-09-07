"""The canvas rides the one server (spec 003).

The picture rendering is verified live with `command-bridge shot` (a screenshot needs a real
browser). What is unit-testable — and what would silently rot — is that the routes are actually
mounted on the voice server, that the frame ops behave, and that the surface stayed DUMB.
"""
import ast
import pathlib

from command_bridge import server as cb_server
from command_bridge.canvas import server as canvas


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
