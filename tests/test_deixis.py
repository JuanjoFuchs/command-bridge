"""Spec 011 — speech-synced deixis on `say`.

Drives the REAL `cmd_say` orchestration; only the HTTP boundary (`_request` to /say, `_canvas` to
/canvas/cue) is stubbed, so the thing under test is exactly what ships: which text is synthesized,
which text and schedule reach the cue, and how the result degrades. The server-side alignment of a
mark to a word is the canvas cue's own suite; this file owns the say→cue seam.
"""
import types

import pytest

from command_bridge import cli


def _args(text, **kw):
    d = {"session": "t", "text": text, "voice": None, "now": False, "lane": "magnus", "timings": False}
    d.update(kw)
    return types.SimpleNamespace(**d)


def _stub_say(monkeypatch, say_result, sink=None):
    def fake(session, path, payload):
        if sink is not None:
            sink.append(payload)
        return dict(say_result)
    monkeypatch.setattr(cli, "_request", fake)


def _stub_cue(monkeypatch, cue_result, sink=None):
    def fake(session, op, payload, lane=""):
        if sink is not None:
            sink.append((op, payload, lane))
        return dict(cue_result)
    monkeypatch.setattr(cli, "_canvas", fake)


# ============================================================ the plain path is untouched (NFR1)

def test_plain_say_synthesizes_verbatim_and_never_touches_the_canvas(monkeypatch):
    said = []
    _stub_say(monkeypatch, {"running": True}, said)
    monkeypatch.setattr(cli, "_canvas",
                        lambda *a, **k: pytest.fail("a plain say must not call the canvas"))

    out = cli.cmd_say(_args("just a plain turn"))

    assert "deixis" not in out
    assert said[0]["text"] == "just a plain turn"
    assert "timings" not in said[0], "no marks means no forced schedule"


# ============================================================ the defect it delivers

def test_marks_speak_the_clean_text_and_cue_the_marked_text_on_the_measured_words(monkeypatch):
    said, cued = [], []
    words = [{"w": "Q3", "t": 1.0}, {"w": "Q4", "t": 2.0}]
    _stub_say(monkeypatch, {"running": True, "words": words}, said)
    _stub_cue(monkeypatch, {"ok": True}, cued)

    out = cli.cmd_say(_args("climbed in [point:#q3]Q3 and dipped in [point:#q4]Q4"))

    # synthesis got the CLEAN text, and deixis forced the measured schedule
    assert said[0]["text"] == "climbed in Q3 and dipped in Q4"
    assert said[0]["timings"] is True
    # the cue got the MARKED text and the SAME measured words the say returned
    op, cue_payload, lane = cued[0]
    assert op == "cue"
    assert cue_payload["text"] == "climbed in [point:#q3]Q3 and dipped in [point:#q4]Q4"
    assert cue_payload["words"] == words
    assert cue_payload["arm"] is False, "a live say fires now, it does not arm"
    assert lane == "magnus"
    assert out["deixis"]["fired"] is True
    assert out["deixis"]["marks"] == ["[point:#q3]", "[point:#q4]"]


def test_a_held_off_lane_say_arms_the_cue_with_the_say_lead(monkeypatch):
    cued = []
    _stub_say(monkeypatch, {"held_off_lane": True, "words": [{"w": "here", "t": 0.5}], "held_for": 3.0})
    _stub_cue(monkeypatch, {"ok": True}, cued)

    out = cli.cmd_say(_args("look [point:#x]here"))

    assert cued[0][1]["arm"] is True, "held off-lane → the highlights start when the lane goes live"
    assert cued[0][1]["lead"] == 3.0
    assert out["deixis"]["armed"] is True


# ============================================================ honest degradation (FR2/FR4)

def test_no_canvas_drops_the_deixis_but_never_the_audio(monkeypatch):
    _stub_say(monkeypatch, {"running": True, "words": [{"w": "there", "t": 0.5}]})
    _stub_cue(monkeypatch, {"error": "no canvas shared"})

    out = cli.cmd_say(_args("point [point:#x]there"))

    assert out.get("running") is True, "the words were still spoken"
    assert out["deixis"]["dropped"] == "no canvas shared"


def test_no_measured_schedule_never_fires_a_guessed_highlight(monkeypatch):
    _stub_say(monkeypatch, {"running": True})   # engine returned no `words`
    monkeypatch.setattr(cli, "_canvas",
                        lambda *a, **k: pytest.fail("no schedule must mean no cue, not a guess"))

    out = cli.cmd_say(_args("point [point:#x]there"))

    assert "dropped" in out["deixis"]


def test_a_refused_clip_points_at_nothing(monkeypatch):
    from command_bridge import config
    _stub_say(monkeypatch, {"code": config.UNREAD_REFUSAL_CODE, "unread_count": 1, "since": 5})
    monkeypatch.setattr(cli, "_canvas",
                        lambda *a, **k: pytest.fail("nothing was spoken, so nothing is pointed along"))

    out = cli.cmd_say(_args("point [point:#x]there"))

    assert out["deixis"]["dropped"]


# ============================================================ --now is incompatible with measured timing

def test_marks_with_now_are_refused_because_now_returns_before_the_schedule(monkeypatch):
    monkeypatch.setattr(cli, "_request", lambda *a, **k: pytest.fail("must refuse before speaking"))

    out = cli.cmd_say(_args("point [point:#x]there", now=True))

    assert out.get("code") == "bad_request"
    assert "measured timing" in out["error"]
