"""Spec 006 — the adaptive meeting page.

The look is verified with `command-bridge shot` (a layout needs a real browser). What is unit-testable
— and what would silently rot — is the STATE SELECTION (the layout is a pure function of agent count ×
shared? × wants-to-see?), the deterministic lane hue, the route being mounted, and the surface staying
dumb. The pixels are the shot's job; the branch that picks which pixels is these tests' job.
"""
import ast
import pathlib

from command_bridge import meeting
from command_bridge import server as cb_server
from command_bridge.canvas import server as canvas


def _state(monkeypatch, tmp_path, lanes=("magnus",)):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = cb_server.TunnelState("t", token=None)
    st.lanes = cb_server.lanes_mod.LaneRegistry(lanes[0])
    for lane in lanes[1:]:
        st.lanes.add(lane)
    return st


def _clear_canvas():
    with canvas._lock:
        canvas._lanes.clear()


def test_the_meeting_route_is_mounted():
    app = cb_server.build_app("t", token=None)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    assert "/meeting" in paths


def test_hue_is_deterministic_by_lane_order():
    """FR5 / JJ: 'the colour for the n lanes should be deterministic … first Magnus … next Atlas.'
    The hue is the lane's POSITION in the registry, so it never moves under it — and it is the same
    palette the phone UI uses, so an orb is the same colour on both pages."""
    lanes = ["magnus", "atlas", "kepler", "dexter"]
    assert meeting.hue(lanes, "magnus") == meeting.LANE_HUES[0]
    assert meeting.hue(lanes, "atlas") == meeting.LANE_HUES[1]
    assert meeting.hue(lanes, "kepler") == meeting.LANE_HUES[2]
    assert meeting.hue(lanes, "nobody") == "#3a3a46"  # unknown lane: a fallback, never a crash


def test_solo_state_when_nothing_is_shared(monkeypatch, tmp_path):
    """FR3. One agent, no canvas shared → the agent holds the centre; the canvas is NOT full-bleed."""
    _clear_canvas()
    html = meeting.render(_state(monkeypatch, tmp_path, ("magnus",)))
    assert 'data-state="solo"' in html
    assert 'src="/canvas"' not in html, "no shared-screen iframe until someone shares"
    assert "MAGNUS" in html


def test_shared_state_when_a_frame_is_present(monkeypatch, tmp_path):
    """FR4. A frame on the canvas = a screen being shared → the wireframe: the canvas is embedded
    full-bleed and the transcript is the right column."""
    _clear_canvas()
    canvas.apply("/frame", {"id": "x", "kind": "html", "content": "<b>hi</b>", "lane": "magnus"})
    try:
        html = meeting.render(_state(monkeypatch, tmp_path, ("magnus",)))
        assert 'data-state="shared"' in html
        assert 'src="/canvas"' in html
    finally:
        _clear_canvas()


def test_every_lane_is_an_orb_and_the_live_one_is_named(monkeypatch, tmp_path):
    """FR5. Several agents → every lane is an orb; the header names the live one."""
    _clear_canvas()
    st = _state(monkeypatch, tmp_path, ("magnus", "atlas", "kepler", "dexter"))
    st.lanes.switch("kepler")
    html = meeting.render(st)
    for lane in ("MAGNUS", "ATLAS", "KEPLER", "DEXTER"):
        assert lane in html
    assert "live:" in html and "KEPLER" in html


def test_the_meeting_surface_holds_no_model():
    """TC1 / the rule both parents share: the page arranges and displays; it decides nothing an
    agent would. No LLM client may be imported."""
    banned = {"openai", "anthropic", "transformers", "torch", "llama_cpp", "cohere", "mistralai",
              "ollama", "google"}
    tree = ast.parse(pathlib.Path(meeting.__file__).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module.split(".")[0])
    offenders = set(names) & banned
    assert not offenders, f"the meeting surface grew a model: {offenders}"
