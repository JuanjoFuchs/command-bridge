"""Every `say` reports the frames on the caller's lane that will not land (JJ, 2026-10-06).

    "I thought we already included some tooling within the canvas commands … that logic helped check
    the font size and the readability … How will the agents get that feedback now?"

Spec 022 moved drawing to files; the page kept measuring each frame's smallest text, but only the
retired `set` read it back, so an agent never learned its board rendered at 8 px.
"""
import types

from command_bridge import cli


def _status(frames, clients=1):
    return {"clients": clients, "lanes": {"magnus": frames, "atlas": [
        {"id": "other", "readable": False, "min_font_px": 3.0}]}}


def test_a_too_small_frame_on_my_lane_is_reported():
    issues = cli._canvas_issues(_status([
        {"id": "team-week", "file": "team-week.html", "readable": False, "min_font_px": 8.7},
        {"id": "todo", "readable": True, "min_font_px": 18.9}]), "magnus")
    assert issues == [{"id": "team-week", "file": "team-week.html",
                       "too_small": {"min_font_px": 8.7, "needs_px": cli.config.CANVAS_READABLE_PX}}]


def test_another_lanes_frames_are_not_mine():
    assert cli._canvas_issues(_status([]), "magnus") == []


def test_no_page_means_nothing_was_measured():
    assert cli._canvas_issues(_status([{"id": "x", "readable": False, "min_font_px": 2}], clients=0),
                              "magnus") == []


def test_a_render_error_is_reported_even_with_no_page_open():
    """atlas, 2026-10-06: a parse error stays true whether or not his page is open."""
    issues = cli._canvas_issues(_status([{"id": "map", "render_error": "Parse error on line 2"}],
                                        clients=0), "magnus")
    assert issues == [{"id": "map", "render_error": "Parse error on line 2"}]


def test_a_render_failure_is_reported_too():
    issues = cli._canvas_issues(_status([{"id": "d", "render_error": "mermaid: parse error"}]), "magnus")
    assert issues[0]["render_error"] == "mermaid: parse error"


def test_say_leads_its_next_with_the_fix(monkeypatch):
    monkeypatch.setattr(cli, "_request", lambda s, path, p=None: _status(
        [{"id": "team-week", "file": "team-week.html", "readable": False, "min_font_px": 8.7}]))
    out = cli._attach_canvas_check(types.SimpleNamespace(session="dev", lane="magnus"),
                                   {"queued": True, "next": "run `command-bridge watch --since 5`"})
    assert out["canvas_check"][0]["id"] == "team-week"
    assert out["next"].startswith("FIX YOUR CANVAS — 1 frame on your lane")
    assert out["next"].endswith("run `command-bridge watch --since 5`")


def test_a_clean_canvas_adds_nothing(monkeypatch):
    monkeypatch.setattr(cli, "_request", lambda s, path, p=None: _status(
        [{"id": "todo", "readable": True, "min_font_px": 18.9}]))
    out = cli._attach_canvas_check(types.SimpleNamespace(session="dev", lane="magnus"), {"queued": True})
    assert "canvas_check" not in out
