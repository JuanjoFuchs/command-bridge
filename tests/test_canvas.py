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
