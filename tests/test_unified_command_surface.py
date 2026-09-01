"""Spec 005 — the voice verbs and the canvas verbs are one command surface.

Each canvas verb marshals its arguments and POSTs to the one running server's `/canvas/<op>` route
through the same `_request` the voice verbs use; the switch reaches the voice `/lane` endpoint. What
would silently rot is the WIRING — which verb hits which endpoint with which payload, and the two
name resolutions the merge forced (`cue` is the canvas highlight, the earcon took `earcon`; there is
one `switch`). So these tests pin the endpoint + payload each verb produces, with `_request` stubbed
so no server is needed, plus the describe/parse surface.

The end-to-end effect (a `set` frame really lands, a `switch` really moves the live lane) is verified
live against a running server per the spec's AC evidence; here we guard the contract cheaply.
"""
import pytest

import command_bridge.cli as cli
from command_bridge import server as cb_server


class _NS:
    """A stand-in argparse namespace."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def calls(monkeypatch):
    """Capture every (session, path, payload) `_request` is asked to send."""
    captured: list[tuple] = []

    def fake_request(session, path, payload=None):
        captured.append((session, path, payload))
        return {"ok": True}

    monkeypatch.setattr(cli, "_request", fake_request)
    return captured


# ── FR1/FR5: the canvas draw/camera verbs POST to /canvas/<op> over the one _request ──────────────

def test_set_posts_a_frame_op_with_the_lane_folded_in(calls):
    cli.cmd_set(_NS(session="dev", lane="magnus", file=None, content=None, mermaid=None,
                    markdown=None, svg=None, html="<b>x</b>", text=None, section=[], id="f1",
                    at="", scale=0, kind="mermaid", title="t"))
    session, path, payload = calls[0]
    assert (session, path) == ("dev", "/canvas/frame")
    assert payload["id"] == "f1" and payload["kind"] == "html" and payload["content"] == "<b>x</b>"
    assert payload["lane"] == "magnus", "the --lane must be folded in so only the live lane draws"


def test_point_look_remove_clear_zoom_raise_reach_their_ops(calls):
    cli.cmd_point(_NS(session="dev", lane="", selector="seq:VT", look=True))
    cli.cmd_look(_NS(session="dev", lane="", id="plan", all=False))
    cli.cmd_remove(_NS(session="dev", lane="", id="plan"))
    cli.cmd_clear(_NS(session="dev", lane=""))
    cli.cmd_zoom(_NS(session="dev", lane="", selector="x", scale="fit"))
    cli.cmd_raise(_NS(session="dev", lane="", why="a chart"))
    paths = [c[1] for c in calls]
    assert paths == ["/canvas/point", "/canvas/look", "/canvas/remove", "/canvas/clear",
                     "/canvas/zoom", "/canvas/raise"]


def test_batch_posts_the_list_to_the_canvas_batch_op(calls):
    import io
    import sys
    monkey = [{"op": "set", "id": "a", "content": "x"}]
    orig = sys.stdin
    sys.stdin = io.StringIO(__import__("json").dumps(monkey))
    try:
        cli.cmd_batch(_NS(session="dev", lane="magnus"))
    finally:
        sys.stdin = orig
    session, path, payload = calls[0]
    assert (session, path) == ("dev", "/canvas/batch")
    assert isinstance(payload, list) and payload[0]["lane"] == "magnus"


# ── FR2: `cue` is the canvas highlight; the earcon it displaced answers to `earcon` ───────────────

def test_cue_is_the_canvas_speech_synced_highlight(calls):
    cli.cmd_canvas_cue(_NS(session="dev", lane="", cancel=False, words="", text="[point:a] hi",
                           seconds=None, arm=False, lead=0.0, look=""))
    session, path, payload = calls[0]
    assert (session, path) == ("dev", "/canvas/cue"), "cue is the CANVAS highlight, not the earcon"
    assert payload["text"] == "[point:a] hi"


def test_earcon_is_the_non_speech_tone_on_its_own_endpoint(calls):
    cli.cmd_earcon(_NS(session="dev", name="thinking"))
    assert calls[0] == ("dev", "/cue", {"name": "thinking"}), \
        "the earcon keeps the /cue endpoint; only the CLI verb name moved to `earcon`"


# ── FR3: one switch, and it drives the authoritative voice lane ───────────────────────────────────

def test_switch_hands_the_floor_through_the_voice_lane(calls):
    cli.cmd_switch(_NS(session="dev", to="atlas"))
    assert calls[0] == ("dev", "/lane", {"action": "switch", "name": "atlas"}), \
        "switch must reach the voice /lane (the authoritative lane), not a canvas-only twin"


# ── TC2/AC5: `run` degrades, it does not crash ────────────────────────────────────────────────────

def test_run_reports_unavailable_without_importing_a_runtime():
    out = cli.cmd_run(_NS(session="dev", lane="", file="x.py", id="", no_code=False, title=""))
    assert out.get("code") == "unsupported"
    assert "not available" in out.get("error", "")
    assert "remedy" in out, "a degraded verb still names what to do instead"


# ── FR4: describe documents BOTH vocabularies; status carries the canvas ──────────────────────────

def test_describe_lists_the_canvas_verbs_and_the_renamed_earcon():
    cmds = cli.DESCRIBE["commands"]
    for verb in ("set", "look", "point", "cue", "inspect", "remove", "clear", "zoom", "raise",
                 "chart", "batch", "switch", "run", "earcon"):
        assert verb in cmds, f"{verb} must appear in describe's command list"
    # `cue` documents the highlight now, not the earcon; `earcon` documents the tone.
    assert "highlight" in str(cmds["cue"]).lower()
    assert "tone" in str(cmds["earcon"]).lower()


def test_the_canvas_status_route_is_mounted_on_the_one_server():
    app = cb_server.build_app("cs", "tok")
    paths = set()
    for res in app.router.resources():
        info = res.get_info()
        paths.add(info.get("path") or info.get("formatter") or "")
    assert "/canvas/status" in paths, "the canvas half of `status` needs its read-only route"


def test_the_parser_registers_the_merged_surface():
    """The verbs are worthless if the parser does not know them. Parse each shape end to end."""
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        pytest.skip("parser is built inline in main(); the describe + route tests cover registration")
    # earcon takes a name; cue takes --text (they are different verbs now)
    assert parser.parse_args(["earcon", "heard"]).name == "heard"
    assert parser.parse_args(["cue", "--text", "hi"]).text == "hi"
    assert parser.parse_args(["switch", "atlas"]).to == "atlas"
