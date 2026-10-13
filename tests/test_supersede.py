"""Spec 012 — a newer clip supersedes a still-held INTENT announcement.

Drives the REAL `_speak` held-clip path (TTS stubbed to distinct bytes). He is talking to `claude`
throughout, so a clip for `codex` is off-lane and held — which is the only state superseding acts on.
"""
import asyncio

import pytest

from command_bridge import server


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry("claude")
    st.lanes.add("codex")
    st.lanes.add("gpt")
    st.channel_open = True
    st.cues_enabled = False
    st.lanes.switch("claude")   # he is on claude → a codex/gpt clip is held off-lane
    return st


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    def synth(text, voice=None, speed=None, pause=None):
        return (text.encode("utf-8") * 8), 22050
    monkeypatch.setattr(server.tts, "synthesize", synth)


def say(state, text, lane, intent=False):
    return asyncio.run(server._speak(state, text, None, lane=lane, intent=intent))


def held_texts(state, lane):
    return [c["header"]["text"] for c in state.lane_held.get(lane, [])]


# ============================================================ the feature

def test_a_newer_clip_supersedes_a_held_intent(state):
    r1 = say(state, "on it, running the tests", "codex", intent=True)
    assert r1["held_off_lane"] is True
    assert held_texts(state, "codex") == ["on it, running the tests"]

    say(state, "tests pass", "codex")               # a newer RESULT
    assert held_texts(state, "codex") == ["tests pass"], (
        "the stale announcement was dropped; only the current state is held")


def test_a_result_is_not_dropped_by_a_newer_clip(state):
    say(state, "tests pass", "codex")               # NOT an intent
    say(state, "and coverage is up", "codex")
    assert held_texts(state, "codex") == ["tests pass", "and coverage is up"], (
        "a result is not dropped by a newer clip")


def test_a_newer_intent_supersedes_an_older_one(state):
    say(state, "about to start A", "codex", intent=True)
    say(state, "about to start B", "codex", intent=True)
    assert held_texts(state, "codex") == ["about to start B"], "a newer intent supersedes an older one"


# ============================================================ scoped strictly (NFR2)

def test_superseding_does_not_cross_lanes(state):
    say(state, "codex about to", "codex", intent=True)
    say(state, "gpt about to", "gpt", intent=True)   # a DIFFERENT lane
    assert held_texts(state, "codex") == ["codex about to"], "superseding does not cross lanes"
    assert held_texts(state, "gpt") == ["gpt about to"]


def test_a_clip_played_LIVE_supersedes_the_lanes_held_intent(state, monkeypatch):
    """2026-09-30: an "about to" held before a restart stayed parked after the agent's next clip went
    out live, so the card showed an intent pending on the lane he was talking to. Supersede used to
    run only on the held path."""
    say(state, "about to restart the server", "codex", intent=True)
    assert held_texts(state, "codex") == ["about to restart the server"]

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(server, "_send_clip", _noop)
    monkeypatch.setattr(server, "_broadcast_json", _noop)
    monkeypatch.setattr(server, "_push_cue", _noop)
    state.clients.add(object())                     # a page is connected...
    state.lanes.switch("codex")                     # ...and he is on codex, so this one plays live
    r = say(state, "server's back", "codex")
    assert r["held_off_lane"] is False
    assert held_texts(state, "codex") == [], "the live result retired the stale announcement"


def test_a_clip_is_counted_in_flight_only_until_it_is_stored(state):
    """The graceful-shutdown drain waits on `synth_inflight` (2026-09-30: a restart killed dexter's
    "shipped and live" mid-synthesis). It must return to zero once the clip is held or sent, and on
    a failure too, or every restart would wait out the full cap."""
    say(state, "shipped and live", "codex")
    assert state.synth_inflight == 0
    assert held_texts(state, "codex") == ["shipped and live"], "stored before the count dropped"


def test_an_intent_played_live_is_never_held_or_superseded(state):
    state.lanes.switch("codex")                     # now ON codex → its clip is live, not held
    r = say(state, "about to, but he is right here", "codex", intent=True)
    assert r["held_off_lane"] is False
    assert state.lane_held.get("codex", []) == []
