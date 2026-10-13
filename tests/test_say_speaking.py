"""A clip never starts while he is speaking, and `say` refuses instead of needing a pre-say watch.
Spec 028, AC1-AC8.

JJ, 2026-10-08: *"we are wasting turns on that extra watch with --reply. I think just the say command
can reject immediately if I'm still speaking"* — and *"I have seen multiple times where I'm still
speaking and a turn from you starts playing and gets interrupted immediately."*

The second complaint had a measured cause: the hold in `_speak_now` released at a hard 15 s while his
utterance was still running (five of seven held clips, 10-05 → 10-08), and nothing looked at the turn
log after acceptance, so a clip made stale by a turn he finished during the hold still played.

NO SERVER IS STARTED. Handlers run in-process against this test's own TunnelState, with a fake page
socket that records what it was sent — "did it start" is asserted on the socket, not on the payload.
"""
import asyncio
import json
from typing import Any

import pytest

from command_bridge import cli, config, server, store


class _Req:
    def __init__(self, state, body):
        self.app = {"state": state}
        self._body = body
        self.remote = "127.0.0.1"
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


class _Page:
    """A connected page: records every header and byte payload it is sent."""

    def __init__(self):
        self.json = []
        self.bytes = 0

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, data):
        self.bytes += len(data)

    def headers(self):
        # The agent's clips only — cue earcons ("synthesized", "speaking") are headers too.
        return [m for m in self.json if m.get("type") == "audio_header"
                and str(m.get("id", "")).startswith("clip-")]


class _Synth:
    def __init__(self):
        self.calls = []

    def __call__(self, text, voice=None, speed=1.0, pause=0.0):
        self.calls.append(text)
        return b"\x00\x00" * 1000, 22050


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.consumed_cursor = -1
    return st


@pytest.fixture
def page(state):
    p = _Page()
    state.clients.add(p)
    state.channel_open = True
    return p


@pytest.fixture
def synth(monkeypatch):
    fake = _Synth()
    monkeypatch.setattr(server.tts, "synthesize", fake)
    monkeypatch.setattr(config, "SPEAK_GRACE_S", 0.0)
    # The hold loop counts `waited` in 0.1 s steps; real sleeping would make a 20 s hold take 20 s.
    real_sleep = asyncio.sleep

    async def _fast(_s, *a, **k):
        await real_sleep(0)

    monkeypatch.setattr(server.asyncio, "sleep", _fast)
    return fake


def _talking(state, monkeypatch, calls: int | None):
    """He is talking (segmenter) for the next `calls` checks, then quiet; None = forever."""
    n = {"left": calls}

    def talking(*, amplitude=True):
        if n["left"] is None:
            return True
        if n["left"] > 0:
            n["left"] -= 1
            return True
        return False

    monkeypatch.setattr(state, "talking", talking)


def said(state, text="hello", **body):
    req: Any = _Req(state, {"text": text, **body})
    resp = asyncio.run(server.handle_say(req))
    return resp.status, json.loads(resp.text or "{}")


def spoke(session, text, addressed=True):
    return store.append_turn(session=session, text=text, t_start=0.0, t_end=1.0,
                             addressed=addressed, reason="wake")


# --------------------------------------------------------------- FR1/FR2: refuse at say-time


@pytest.mark.parametrize("now", [False, True])
def test_say_refuses_while_he_is_speaking_and_synthesizes_nothing(state, page, synth, monkeypatch,
                                                                  now):
    """AC1 — `say` and `say --now`: 428 `user_speaking`, nothing synthesized, nothing sent."""
    _talking(state, monkeypatch, None)
    status, payload = said(state, "answer", **({"async": True} if now else {}))
    assert status == 428
    assert payload["code"] == config.SPEAKING_REFUSAL_CODE and payload["spoke"] is False
    assert "--reply" in payload["remedy"] and "--lane" in payload["remedy"]
    assert f"--since {payload['since']}" in payload["remedy"]
    assert synth.calls == [] and page.headers() == []


def test_room_noise_alone_never_refuses(state, page, synth):
    """AC2 — raw amplitude (`user_speaking`) with the segmenter quiet: accepted and played (TC1)."""
    state.user_speaking = True
    status, payload = said(state, "answer")
    assert status == 200 and payload.get("delivered") is True
    assert synth.calls == ["answer"]


def test_an_off_lane_clip_is_held_not_refused(state, page, synth, monkeypatch):
    """AC3 — he is talking, but to another lane: this clip is held, never refused (FR2)."""
    state.lanes.add("atlas")
    _talking(state, monkeypatch, None)
    status, payload = said(state, "answer", lane="atlas")
    assert status == 200 and payload.get("held_off_lane") is True


def test_muted_or_quiet_is_accepted(state, page, synth, monkeypatch):
    """AC4 — a muted mic cannot be mid-sentence."""
    state.muted = True
    _talking(state, monkeypatch, None)
    status, _ = said(state, "answer")
    assert status == 200


# --------------------------------------------------------------- FR3/FR4/FR5: play-time


def _speak(state, accepted_head=None):
    return asyncio.run(server._speak_now(state, "the reply", None, accepted_head=accepted_head))


def test_a_long_turn_past_15s_is_held_not_played_into(state, page, synth, monkeypatch):
    """AC5 — talking for ~20 s of hold (the old cap was 15 s), then quiet: played only after."""
    _talking(state, monkeypatch, 200)
    out = _speak(state)
    assert out["delivered"] is True and out["held_for"] > 15.0
    assert len(page.headers()) == 1


def test_still_talking_at_the_cap_drops_the_clip(state, page, synth, monkeypatch):
    """AC6 — the cap ends in silence plus a notice, never in playing over him."""
    monkeypatch.setattr(config, "SPEAK_HOLD_MAX_S", 1.0)
    _talking(state, monkeypatch, None)
    out = _speak(state)
    assert out["dropped"] == "still_speaking" and out["delivered"] is False
    assert page.headers() == []
    notice = state.lane_played[state.lanes.current][-1]
    assert notice["cut"] == "dropped" and notice["why"] == "still_speaking"
    assert notice["heard_s"] == 0 and notice["text"] == "the reply"


def test_a_turn_he_finished_after_acceptance_drops_the_clip(state, page, synth):
    """AC7 — an addressed turn above the accepted head makes the clip stale: not sent."""
    head = store.last_turn_id(state.session)
    spoke(state.session, "and one more thing")
    out = _speak(state, accepted_head=head)
    assert out["dropped"] == "newer_turn"
    assert page.headers() == []
    assert state.lane_played[state.lanes.current][-1]["why"] == "newer_turn"


def test_an_unaddressed_turn_does_not_drop_it(state, page, synth):
    """AC7 — only a turn addressed to this lane makes the clip stale."""
    head = store.last_turn_id(state.session)
    spoke(state.session, "talking to someone in the room", addressed=False)
    out = _speak(state, accepted_head=head)
    assert out["delivered"] is True and "dropped" not in out
    assert len(page.headers()) == 1


def test_a_turn_already_read_does_not_drop_it(state, page, synth):
    """TC2 — the turn the agent is answering was in the log before acceptance."""
    spoke(state.session, "the question")
    out = _speak(state, accepted_head=store.last_turn_id(state.session))
    assert out["delivered"] is True


def test_a_dropped_clip_does_not_move_the_idle_anchor(state, page, synth, monkeypatch):
    """FR5 — he heard nothing, so no silence began after it (spec 024)."""
    monkeypatch.setattr(config, "SPEAK_HOLD_MAX_S", 1.0)
    _talking(state, monkeypatch, None)
    _speak(state)
    assert state.lanes.current not in state.lane_last_played


# --------------------------------------------------------------- spec 029 FR9/FR10


def test_a_full_text_refusal_marks_the_turns_read_so_say_works_again(state, page, synth):
    """029 AC6 — the refusal hands over every unread turn, counts them read, and the retry speaks."""
    spoke(state.session, "one more thing")
    status, payload = said(state, "answer")
    assert status == 428 and payload["code"] == config.UNREAD_REFUSAL_CODE
    assert payload["marked_read"] is True
    assert payload["cursor"] == store.last_turn_id(state.session)
    assert state.lane_cursor(state.lanes.current) == payload["cursor"]
    assert any(m.get("type") == "consumed" for m in page.json)
    status, again = said(state, "answer, revised")
    assert status == 200 and again.get("delivered") is True


def test_a_truncated_refusal_leaves_the_cursor(state, page, synth, monkeypatch):
    """029 AC6 — more unread than the refusal can carry: today's behaviour, watch remedy."""
    monkeypatch.setattr(config, "UNREAD_ON_SAY_MAX", 1)
    before = state.lane_cursor(state.lanes.current)
    spoke(state.session, "first")
    spoke(state.session, "second")
    status, payload = said(state, "answer")
    assert status == 428 and payload["marked_read"] is False
    assert state.lane_cursor(state.lanes.current) == before
    assert "watch" in payload["remedy"]


@pytest.mark.parametrize("pieces,partial,want", [
    (["so the thing is", "we need"], "ignored", "so the thing is we need"),
    ([], "so the thing", "so the thing"),
    ([], "", ""),
])
def test_user_speaking_refusal_returns_what_he_has_said_so_far(state, page, synth, monkeypatch,
                                                                pieces, partial, want):
    """029 AC7 — committed pieces, else the live preview, else ""; nothing marked read."""
    _talking(state, monkeypatch, None)
    state.pieces.texts = list(pieces)
    state.partial_text = partial
    before = state.lane_cursor(state.lanes.current)
    status, payload = said(state, "answer")
    assert status == 428 and payload["code"] == config.SPEAKING_REFUSAL_CODE
    assert payload["partial"] == want
    assert state.lane_cursor(state.lanes.current) == before


# --------------------------------------------------------------- FR6/FR7: hints and describe


def test_describe_and_the_turns_hint_teach_the_one_call_loop():
    """AC8 — `user_speaking` is a documented code, and the turns `next` names no pre-say watch."""
    assert config.SPEAKING_REFUSAL_CODE in cli.DESCRIBE["error_codes"]
    say = cli.DESCRIBE["commands"]["say"]
    assert config.SPEAKING_REFUSAL_CODE in say["returns"]["REFUSAL"]
    assert "DROPPED" in say["returns"]
    live = {"clients": 1, "channel_open": True, "capturing": True, "muted": False}
    for verbose in (False, True):
        _, literal, full = cli._next_branch([{"id": 1}], {**live, "verbose": verbose}, 1)
        assert "--reply" not in literal and "--reply" not in full
        assert config.SPEAKING_REFUSAL_CODE in full
