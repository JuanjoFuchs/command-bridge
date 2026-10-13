"""Spec 011 — speech-synced deixis on `say`. Spec 020 extends the same seam with `--verify`.

Drives the REAL `cmd_say` orchestration; only the HTTP boundary (`_request` to /say, `_canvas` to
/canvas/cue) is stubbed, so the thing under test is exactly what ships: which text is synthesized,
which text and schedule reach the cue, and how the result degrades. The server-side alignment of a
mark to a word is the canvas cue's own suite; this file owns the say→cue seam.

Spec 020's `--verify` adds one more boundary: the headless render (`shot.capture`) and the runtime
lookup (`read_runtime`). Both are stubbed the same way — at the seam, never by re-implementing the
capture logic — so these tests prove the say→verify wiring, not the browser. The REAL headless
render (does a per-mark capture actually light that mark and not the others?) is
`tests/deixis_visual_check.py`'s AC5 section, which needs a live server and a real browser.
"""
import types

import pytest

from command_bridge import cli, shot


def _args(text, **kw):
    d = {"session": "t", "text": text, "voice": None, "now": False, "lane": "magnus",
         "timings": False, "show": None}
    d.update(kw)
    return types.SimpleNamespace(**d)


def _lane_file(name, text):
    """A frame file in magnus's lane folder for session `t` (spec 022: `--show` names a file already
    in the lane folder; it no longer copies one in). conftest points COMMAND_BRIDGE_DIR at a tmp dir."""
    from command_bridge.canvas.server import canvas_root
    d = canvas_root("t") / "magnus"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(text, encoding="utf-8")
    return f


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


def _stub_runtime(monkeypatch, rt=None):
    """A running server, or None to simulate "nothing to capture against" (spec 020 FR5)."""
    monkeypatch.setattr(cli, "read_runtime",
                        lambda session: rt if rt is not None else
                        {"host": "127.0.0.1", "port": 8770, "token": "t"})


def _stub_no_server(monkeypatch):
    monkeypatch.setattr(cli, "read_runtime", lambda session: None)


def _stub_capture(monkeypatch, *, fail=None, fail_selector=None):
    """Stand in for `shot.capture` (the headless render), recording every call.

    `fail` makes EVERY call fail (the base render dies — nothing else will succeed either).
    `fail_selector` fails only calls for that one selector, so the other captures still land —
    the partial-degradation path (a bad selector sinks one picture, not the whole set).
    """
    calls = []

    def fake(url, out, viewport=None, lane="", settle_ms=0, point="", look="", **kw):
        calls.append({"url": url, "out": out, "lane": lane, "point": point, "look": look})
        if fail:
            return {"error": fail, "code": "shot_failed"}
        if fail_selector is not None and point == fail_selector:
            return {"error": f"selector {point!r} failed to render", "code": "shot_failed"}
        return {"ok": True, "path": out}

    monkeypatch.setattr(shot, "capture", fake)
    return calls


# ============================================================ the plain path is untouched (NFR1)

def test_plain_say_synthesizes_verbatim_and_never_touches_the_canvas(monkeypatch):
    said = []
    _stub_say(monkeypatch, {"running": True}, said)
    monkeypatch.setattr(cli, "_canvas",
                        lambda *a, **k: pytest.fail("a plain say must not call the canvas"))

    out = cli.cmd_say(_args("just a plain turn"))

    assert "deixis" not in out
    assert "verify" not in out, "spec 020: absent --verify, the result carries no verify field at all"
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


def test_a_held_off_lane_say_arms_the_cue_with_no_extra_lead(monkeypatch):
    cued = []
    _stub_say(monkeypatch, {"held_off_lane": True, "words": [{"w": "here", "t": 0.5}], "held_for": 3.0})
    _stub_cue(monkeypatch, {"ok": True}, cued)

    out = cli.cmd_say(_args("look [point:#x]here"))

    assert cued[0][1]["arm"] is True, "held off-lane → the highlights start when the lane goes live"
    assert cued[0][1]["lead"] == 0.0, "the marks self-sync from audio start; held_for is not the lead-in"
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


# ============================================================ --show places the frame first (FR3)

def test_show_focuses_the_frame_then_speaks_then_cues(monkeypatch):
    f = _lane_file("chart.vl.json", '{"mark": "bar"}')
    ops = []
    _stub_say(monkeypatch, {"running": True, "words": [{"w": "Q3", "t": 1.0}]})
    _stub_cue(monkeypatch, {"ok": True}, ops)

    out = cli.cmd_say(_args("see [point:#bar-0]Q3", show=str(f)))

    # spec 022: the file is ALREADY a frame (the watcher rendered it), so nothing is copied — the
    # camera is brought to it (so the highlights are on-screen — the live-demo fix), then the cue
    assert [o[0] for o in ops] == ["look", "cue"], "no frame op: --show no longer copies content"
    assert ops[0][1]["id"] == "chart", "the id is the stem, compound suffix and all"
    assert ops[1][1]["look"] == "chart", "the cue carries the camera move so it rides the schedule"
    assert out["deixis"]["fired"] is True


def test_show_accepts_a_bare_frame_id(monkeypatch):
    ops = []
    _stub_say(monkeypatch, {"running": True, "words": [{"w": "Q3", "t": 1.0}]})
    _stub_cue(monkeypatch, {"ok": True}, ops)

    cli.cmd_say(_args("see [point:#bar-0]Q3", show="plan"))

    assert ops[0] == ("look", {"id": "plan", "all": False}, "magnus")


def test_a_deixis_say_without_show_asks_the_cue_for_no_camera_move(monkeypatch):
    cued = []
    _stub_say(monkeypatch, {"running": True, "words": [{"w": "x", "t": 0.1}]})
    _stub_cue(monkeypatch, {"ok": True}, cued)

    cli.cmd_say(_args("point [point:#x]here"))

    assert cued[0][1]["look"] == "", "no --show → no frame to travel to; the agent owns the view"


def test_a_bad_show_source_refuses_before_a_word_is_spoken(monkeypatch):
    monkeypatch.setattr(cli, "_request",
                        lambda *a, **k: pytest.fail("a bad --show must refuse before speaking"))

    out = cli.cmd_say(_args("see [point:#x]this", show="/no/such/file.json"))

    assert out.get("code") == "invalid_input"


def test_a_show_file_outside_the_lane_folder_is_refused_with_where_to_write_it(monkeypatch, tmp_path):
    """spec 022: a frame is a file IN the lane folder; a scratch file elsewhere is not copied in
    (that copy was the duplication the spec removed) — it is refused, naming the folder."""
    f = tmp_path / "scratch.mmd"
    f.write_text("graph TD; a-->b", encoding="utf-8")
    monkeypatch.setattr(cli, "_request",
                        lambda *a, **k: pytest.fail("a bad --show must refuse before speaking"))

    out = cli.cmd_say(_args("see [point:#x]this", show=str(f)))

    assert out.get("code") == "invalid_input"
    assert "magnus" in out["remedy"] and "t.canvas" in out["remedy"]


# ============================================================ the measured schedule (AC3) — pure

def test_schedule_places_each_mark_on_its_measured_word():
    from command_bridge.canvas import cue as canvas_cue
    words = [{"w": "Revenue", "t": 0.0}, {"w": "climbed", "t": 0.5},
             {"w": "in", "t": 0.9}, {"w": "Q3", "t": 1.2}]
    out = canvas_cue.schedule("[point:#a]Revenue climbed in [point:#b]Q3", words=words)
    assert out["timing"] == "measured"
    at = {m["selector"]: m["at"] for m in out["marks"]}
    assert at["#a"] == 0.0     # before word 0
    assert at["#b"] == 1.2     # before word 3 (Q3)


def test_a_mark_past_the_last_word_clamps_to_the_last_measured_word_not_a_guess():
    from command_bridge.canvas import cue as canvas_cue
    words = [{"w": "done", "t": 0.0}, {"w": "now", "t": 0.4}]
    out = canvas_cue.schedule("done now [point:#end]", words=words)
    assert out["timing"] == "measured"
    assert out["marks"][0]["at"] == 0.4, "the last measured word — never an estimated split"


def test_no_words_degrades_to_estimated_then_immediate_never_a_measured_claim():
    from command_bridge.canvas import cue as canvas_cue
    assert canvas_cue.schedule("a [point:#m]b c", seconds=3.0)["timing"] == "estimated"
    imm = canvas_cue.schedule("a [point:#m]b c")
    assert imm["timing"] == "immediate"
    assert imm["marks"][0]["at"] == 0.0


# ============================================================ --now is incompatible with measured timing

def test_marks_with_now_are_refused_because_now_returns_before_the_schedule(monkeypatch):
    monkeypatch.setattr(cli, "_request", lambda *a, **k: pytest.fail("must refuse before speaking"))

    out = cli.cmd_say(_args("point [point:#x]there", now=True))

    assert out.get("code") == "bad_request"
    assert "measured timing" in out["error"]


# ============================================================ spec 020 — `--verify` (AC1/AC2/AC3/AC4)

def test_verify_returns_the_base_frame_plus_one_capture_per_mark(monkeypatch, tmp_path):
    """AC1/AC2: one `say --show ... --verify` invocation with two marks returns a SET — the base
    frame plus one capture per mark, each selector-labelled — with no separate `shot` call."""
    f = _lane_file("chart.mmd", "graph TD; a-->b")
    words = [{"w": "Q3", "t": 1.0}, {"w": "Q4", "t": 2.0}]
    _stub_say(monkeypatch, {"running": True, "words": words})
    _stub_cue(monkeypatch, {"ok": True})
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(cli, "_await_legibility",
                        lambda *a, **k: {"min_font_px": 22.0, "readable": True, "fit_scale": 1.0})
    calls = _stub_capture(monkeypatch)

    out = cli.cmd_say(_args("revenue [point:#q3]Q3 and [point:#q4]Q4",
                            show=str(f), verify=True))

    assert out["running"] is True, "verify never touches the audio branch"
    v = out["verify"]
    assert v["captured"] is True
    selectors = [c["selector"] for c in v["captures"]]
    assert selectors == ["frame", "#q3", "#q4"], "the base frame first, then one per mark, in order"
    for c in v["captures"]:
        assert c["path"], "a reference the agent can read"
        assert c["legibility"] == {"min_font_px": 22.0, "readable": True, "fit_scale": 1.0}
    # each capture asked the render for exactly one highlight (or none, for the base)
    assert [c["point"] for c in calls] == ["", "#q3", "#q4"]
    assert all(c["lane"] == "magnus" for c in calls), "captured from THIS lane's frame"
    # AC5 regression (found live on `magnus`, 2026-09-16): EVERY capture must ask the render to
    # focus the just-drawn frame, not the whole canvas — this is also what scopes `point`'s own
    # search to inside that frame instead of the whole stage (see `_capture_verify`'s docstring).
    assert all(c["look"] == "chart" for c in calls), (
        "every capture — base AND each per-mark — must focus the --show frame (FR3), not just draw it"
    )


def test_verify_with_marks_but_no_show_still_captures_against_the_existing_frame(monkeypatch):
    """FR2's other half: marks alone (no --show, pointing at something already on the canvas —
    spec 011's own example) are enough to trigger verify; --show is not required."""
    _stub_say(monkeypatch, {"running": True, "words": [{"w": "step", "t": 0.3}]})
    _stub_cue(monkeypatch, {"ok": True})
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(cli, "_await_legibility", lambda *a, **k: None)
    calls = _stub_capture(monkeypatch)

    out = cli.cmd_say(_args("the bug is in [point:#node-auth]step", verify=True))

    assert out["verify"]["captured"] is True
    assert [c["selector"] for c in out["verify"]["captures"]] == ["frame", "#node-auth"]
    assert "legibility" not in out["verify"]["captures"][0], (
        "no --show means no known frame id — legibility is omitted, never fabricated"
    )
    assert len(calls) == 2
    assert all(c["look"] == "" for c in calls), (
        "no --show this call means no known frame id to focus — none is asserted, not guessed"
    )


def test_a_dense_frame_reports_unreadable_in_the_same_verify_call(monkeypatch, tmp_path):
    """AC3: the legibility reading rides every capture, and a dense frame's `readable: false`
    shows up in the SAME call — no separate `shot`/`set` round trip needed to learn it."""
    f = _lane_file("wall.md", "# dense")
    _stub_say(monkeypatch, {"running": True, "words": [{"w": "see", "t": 0.1}]})
    _stub_cue(monkeypatch, {"ok": True})
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(cli, "_await_legibility",
                        lambda *a, **k: {"min_font_px": 6.0, "readable": False, "fit_scale": 0.3})
    _stub_capture(monkeypatch)

    out = cli.cmd_say(_args("[point:#a]see this", show=str(f), verify=True))

    assert out["verify"]["captures"][0]["legibility"]["readable"] is False


def test_verify_still_captures_while_the_agent_is_off_lane(monkeypatch):
    """AC4: verify works off-lane — the captures come from the lane's own frame even though the
    live highlights have not fired for him yet (the say is HELD, not delivered)."""
    _stub_say(monkeypatch, {"held_off_lane": True, "held_for": 3.0,
                            "words": [{"w": "here", "t": 0.5}]})
    _stub_cue(monkeypatch, {"ok": True})
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(cli, "_await_legibility", lambda *a, **k: None)
    _stub_capture(monkeypatch)

    out = cli.cmd_say(_args("look [point:#x]here", verify=True))

    assert out["held_off_lane"] is True
    assert out["deixis"]["armed"] is True, "the deixis half is unaffected by verify"
    assert out["verify"]["captured"] is True
    assert [c["selector"] for c in out["verify"]["captures"]] == ["frame", "#x"]


# ============================================================ spec 020 — honest degradation (AC6/AC8)

def test_verify_is_a_stated_no_op_when_the_call_neither_draws_nor_points(monkeypatch):
    """FR6: requesting verify on a say that neither shows a frame nor carries a mark is a no-op
    capture with a stated reason — never an error, and never even attempts a render."""
    _stub_say(monkeypatch, {"running": True})
    monkeypatch.setattr(cli, "_canvas",
                        lambda *a, **k: pytest.fail("no marks and no --show — nothing to cue"))
    calls = _stub_capture(monkeypatch)

    out = cli.cmd_say(_args("just a plain turn", verify=True))

    assert out["running"] is True
    assert out["verify"]["captured"] is False
    assert "reason" in out["verify"] and "nothing" in out["verify"]["reason"]
    assert calls == [], "a no-op must never attempt a render"


def test_verify_skips_honestly_with_no_canvas_reachable(monkeypatch):
    """AC6: no server for this session (no canvas to capture) still speaks; `verify` names why."""
    _stub_say(monkeypatch, {"running": True, "words": [{"w": "there", "t": 0.5}]})
    _stub_cue(monkeypatch, {"ok": True})
    _stub_no_server(monkeypatch)

    out = cli.cmd_say(_args("point [point:#x]there", verify=True))

    assert out.get("running") is True, "the words were still spoken"
    assert out["verify"]["captured"] is False
    assert "reason" in out["verify"]


def test_verify_skips_when_the_clip_was_refused(monkeypatch):
    """A refusal means nothing was spoken, so there is nothing this call's to have drawn a
    trustworthy picture of either — verify degrades the same way deixis does."""
    from command_bridge import config
    _stub_say(monkeypatch, {"code": config.UNREAD_REFUSAL_CODE, "unread_count": 1, "since": 5})
    monkeypatch.setattr(cli, "_canvas",
                        lambda *a, **k: pytest.fail("nothing was spoken — nothing to cue or capture"))
    calls = _stub_capture(monkeypatch)

    out = cli.cmd_say(_args("point [point:#x]there", verify=True))

    assert out["verify"]["captured"] is False
    assert calls == []


def test_a_forced_capture_failure_still_speaks_and_still_raises_a_hand(monkeypatch):
    """AC8: the capture runs off the speech path — a forced render failure still speaks (here:
    still HOLDS and ARMS off-lane, the held equivalent of speaking) rather than losing the turn."""
    _stub_say(monkeypatch, {"held_off_lane": True, "held_for": 3.0,
                            "words": [{"w": "here", "t": 0.5}]})
    _stub_cue(monkeypatch, {"ok": True})
    _stub_runtime(monkeypatch)
    _stub_capture(monkeypatch, fail="the screenshot failed")

    out = cli.cmd_say(_args("look [point:#x]here", verify=True))

    assert out["held_off_lane"] is True, "the audio/hold branch is untouched by a capture failure"
    assert out["deixis"]["armed"] is True, "the hand-raise/arm is untouched by a capture failure"
    assert out["verify"]["captured"] is False
    assert "screenshot failed" in out["verify"]["reason"]


def test_a_bad_mark_selector_sinks_only_that_one_capture(monkeypatch, tmp_path):
    """TC3/degradation: a per-mark render failure is reported on THAT entry, not the whole set —
    the base frame and the other mark still come back so the agent isn't blind to everything."""
    f = _lane_file("chart.mmd", "graph TD; a-->b")
    _stub_say(monkeypatch, {"running": True,
                            "words": [{"w": "Q3", "t": 1.0}, {"w": "Q4", "t": 2.0}]})
    _stub_cue(monkeypatch, {"ok": True})
    _stub_runtime(monkeypatch)
    monkeypatch.setattr(cli, "_await_legibility", lambda *a, **k: None)
    _stub_capture(monkeypatch, fail_selector="#q4")

    out = cli.cmd_say(_args("see [point:#q3]Q3 and [point:#q4]Q4", show=str(f), verify=True))

    assert out["verify"]["captured"] is True
    caps = {c["selector"]: c for c in out["verify"]["captures"]}
    assert "path" in caps["frame"] and "path" in caps["#q3"]
    assert "error" in caps["#q4"] and "path" not in caps["#q4"]


# ============================================================ spec 020 — `describe` contract (AC9)

def test_verify_flag_and_return_field_are_documented():
    say = cli.DESCRIBE["commands"]["say"]
    assert "--verify" in say["args"], "the describe-contract test also checks this dynamically"
    assert "verify" in say["returns"]
