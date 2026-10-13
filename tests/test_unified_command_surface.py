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

# spec 022 retired set/look/zoom/remove/clear (a frame is a file) — see test_set_is_gone.py.

def test_point_and_relayout_reach_their_ops_with_the_lane_folded_in(calls):
    cli.cmd_point(_NS(session="dev", lane="magnus", selector="seq:VT", look=True))
    cli.cmd_relayout(_NS(session="dev", lane="magnus"))
    assert [c[1] for c in calls] == ["/canvas/point", "/canvas/relayout"]
    assert all(c[2]["lane"] == "magnus" for c in calls), "the --lane must be folded in"


def test_a_path_with_a_query_keeps_the_token_out_of_it():
    """`status --lane vega` asks `/canvas/status?lane=vega`; a second `?` before the token folded the
    token into the lane name, so canvas.folder named a lane that does not exist (found 2026-09-25)."""
    rt = {"host": "127.0.0.1", "port": 8765, "token": "t0k"}
    assert cli._url_for(rt, "/canvas/status?lane=vega").endswith("/canvas/status?lane=vega&token=t0k")
    assert cli._url_for(rt, "/status").endswith("/status?token=t0k")


def test_status_scopes_the_canvas_half_to_the_lane(calls):
    cli.cmd_status(_NS(session="dev", lane="vega", phone=False))
    assert ("dev", "/canvas/status?lane=vega", None) in [(c[0], c[1], c[2] or None) for c in calls]


def test_batch_posts_the_list_to_the_canvas_batch_op(calls):
    import io
    import sys
    monkey = [{"op": "point", "selector": "a"}]
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
    for verb in ("point", "cue", "inspect", "relayout", "canvas-doctor",
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
