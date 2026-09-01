"""Spec 026 — every gate knows its lane.

Nine specs fixed one instance each of a single defect: **a fact made per-lane in STORAGE and left
session-wide in RESOLUTION.** He stopped asking for the tenth fix and asked for the sweep:

    "But I didn't barge in. I just switched the lane and was listening. I didn't interrupt you."
    "I need you to run a critical audit of all these gates and all the features to make sure that
     they are lane aware."

The audit found four more. This file guards all of them.

🔴 **DRIVE THE REAL PATH OR DO NOT CLAIM TO.** Two worthless tests shipped on 2026-08-26 by
re-implementing the server's own line in a local helper; both passed against broken code and only a
mutation check found them out. Everything below calls `_speak`, `_maybe_barge`, `_set_lane`,
`_flush_undelivered` or the real control handler. Every assertion here was confirmed by reverting
its fix and watching it go red.
"""
import asyncio
import json

import numpy as np
import pytest

from command_bridge import server


class _Sock:
    def __init__(self):
        self.headers = []
        self.blobs = []

    async def send_json(self, payload):
        self.headers.append(payload)

    async def send_bytes(self, data):
        self.blobs.append(data)


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("atlas")
    st.lanes.add("kepler")
    st.channel_open = True
    st.cues_enabled = False
    return st


@pytest.fixture
def sock(state):
    s = _Sock()
    state.clients.add(s)
    return s


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    def synth(text, voice=None, speed=None, pause=None):
        return (text.encode("utf-8") * 8), 22050

    monkeypatch.setattr(server.tts, "synthesize", synth)
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.0, raising=False)


@pytest.fixture
def his_voice(state, monkeypatch):
    """Make the voiceprint say "that is him, not the agent", so a barge can actually fire."""
    monkeypatch.setattr(type(state.embedder), "available", property(lambda _s: True))
    monkeypatch.setattr(state.embedder, "embed", lambda _s: [0.1] * 192)
    monkeypatch.setattr(server.voiceprint, "match", lambda _e: ("me", 0.99))
    monkeypatch.setattr(server.config, "barge_in_enabled", lambda: True)
    monkeypatch.setattr(server.config, "barge_in_threshold", lambda: 0.15)


def _loud():
    return np.ones(int(16000 * 1.5), dtype=np.float32) * 0.2


def say_on(state, lane, text):
    """Speak as `lane`, with that lane live so the clip actually goes out."""
    state.lanes.switch(lane)
    return asyncio.run(server._speak(state, text, None, lane=lane))


def say_off_lane(state, lane, text):
    """Speak as `lane` while he is looking at somebody else, so the clip is HELD."""
    return asyncio.run(server._speak(state, text, None, lane=lane))


class _Req:
    """The bare surface `handle_say` touches. The blue tick is written by the HTTP handler, not by
    `_speak`, so a test that called `_speak` would be testing the wrong function entirely."""

    def __init__(self, state, body):
        self.app = {"state": state}
        self.headers = {}
        self.query = {}
        self.remote = "127.0.0.1"
        self._body = body

    async def json(self):
        return self._body


def say_http(state, lane, text):
    """Drive the REAL `say` endpoint, which is where `lane_read_through` is written."""
    return asyncio.run(server.handle_say(_Req(state, {"text": text, "lane": lane})))


# ---------------------------------------------------------------- AC-1: FR1


def test_agent_state_follows_the_lane_he_switches_to(state, sock):
    """🔴 **THE ROOT OF THE BUG HE REPORTED.** `agent_state` was a cache written only when the lane
    it was told about was the live one, and no lane switch ever recomputed it — so from the moment
    he switched it described **the lane he had left**.

    Mutation: restore the stored field and this goes red.
    """
    say_on(state, "magnus", "from magnus")
    assert state.agent_state == "speaking", "magnus is live and speaking"

    asyncio.run(server._set_lane(state, "atlas", why="tap"))

    assert state.lane_states.get("magnus") == "speaking", "magnus really is still mid-clip"
    assert state.agent_state != "speaking", (
        "the live lane is atlas now, and atlas is not speaking — a cache that says otherwise is "
        "what opened the barge gate on a conversation he had left"
    )
    assert state.agent_state == "idle"


def test_assigning_agent_state_still_means_the_live_lane(state, sock):
    """TC1. Three test files and one call site assign it; the meaning is unchanged."""
    state.lanes.switch("atlas")
    state.agent_state = "thinking"
    assert state.lane_states["atlas"] == "thinking"
    assert state.lane_states.get("magnus") != "thinking", "and it must not leak to anybody else"


# ------------------------------------------------------------ AC-2: FR2/FR3


def test_interrupting_the_agent_he_is_talking_to_is_still_a_barge(state, sock, his_voice):
    """The behaviour that must NOT change. He is on magnus, magnus is speaking, he cuts in: that is
    a real interruption, and spec 025 says the clip he heard does not come back."""
    say_on(state, "magnus", "from magnus")
    state.user_speaking = True

    asyncio.run(server._maybe_barge(state, _loud()))

    assert state.barges == 1, "the barge must actually have fired, or this proves nothing"
    assert not state.lane_held.get("magnus"), (
        "he heard it and interrupted it — returning it would replay a turn he already got"
    )


def test_the_gate_opens_on_audio_in_the_air_not_on_the_stale_cache(state, sock, his_voice):
    """FR2. The gate's question is physical: is there a clip on the speaker? `speaking_lane` is the
    honest source. Mutation: point the gate back at `agent_state` and this goes red, because after
    the switch that cache reads "idle" for the live lane while magnus is still audibly talking."""
    say_on(state, "magnus", "from magnus")
    asyncio.run(server._set_lane(state, "atlas", why="tap"))
    state.user_speaking = True

    asyncio.run(server._maybe_barge(state, _loud()))

    assert state.barges == 1, (
        "audio was still playing, so speaking over it must still stop it — he cannot listen to one "
        "agent and talk to another at the same time"
    )


# ---------------------------------------------------------------- AC-3: FR4


def test_speaking_to_another_lane_returns_the_clip_instead_of_eating_it(state, sock, his_voice):
    """🔴 **WHAT HE ACTUALLY REPORTED.** *"I didn't barge in. I just switched the lane and was
    listening. I didn't interrupt you."*

    The audio has to stop — one speaker, one room — but magnus was never answered, so magnus's
    reply belongs back in its hold with the hand up.

    Mutation: pass `keep_playing=True` unconditionally and this goes red.
    """
    say_on(state, "magnus", "from magnus")
    asyncio.run(server._set_lane(state, "atlas", why="tap"))
    state.user_speaking = True

    asyncio.run(server._maybe_barge(state, _loud()))

    assert state.barges == 1
    held = [c["header"].get("text") for c in state.lane_held.get("magnus", [])]
    assert held == ["from magnus"], (
        f"magnus was talked over, not interrupted — its reply must come back, got {held}"
    )
    hands = [h for h in sock.headers if h.get("type") == "lane_waiting" and h.get("lane") == "magnus"]
    assert hands and hands[-1]["waiting"] == 1, "and the hand goes back up so he can find it"


# ---------------------------------------------------------------- AC-4: TC2


def test_a_cross_lane_barge_leaves_a_third_lanes_hold_alone(state, sock, his_voice):
    """Spec 012 TC3, which cost nine clips. Discarding the held speech of an agent he cannot even
    hear would be the tool deciding on his behalf that an answer he never received is unwanted."""
    state.lanes.switch("atlas")
    say_off_lane(state, "kepler", "kepler was waiting")
    assert len(state.lane_held.get("kepler", [])) == 1

    say_on(state, "magnus", "from magnus")
    asyncio.run(server._set_lane(state, "atlas", why="tap"))
    state.user_speaking = True
    asyncio.run(server._maybe_barge(state, _loud()))

    assert state.barges == 1
    kept = [c["header"].get("text") for c in state.lane_held.get("kepler", [])]
    assert kept == ["kepler was waiting"], (
        f"kepler was not part of this and must not lose anything, got {kept}"
    )


# ---------------------------------------------------------------- AC-5: FR5


def test_a_clip_queued_while_he_was_away_is_parked_not_played_at_the_wrong_lane(state, monkeypatch):
    """FR5. `_speak` tests `off_lane` BEFORE queueing, so this store only ever holds clips for the
    lane that was live THEN. The flush played all of them into whatever lane is live NOW.

    Mutation: drop the lane check in `_flush_undelivered` and this goes red.
    """
    # Nobody connected: the reply is synthesized and queued rather than refused.
    state.lanes.switch("magnus")
    say_off_lane(state, "magnus", "said while the phone slept")
    assert len(state.undelivered) == 1, "it must have been queued, or this proves nothing"
    assert state.undelivered[0]["lane"] == "magnus", "TC3: the lane travels with the clip"

    # His phone comes back — on a different lane.
    sock = _Sock()
    state.clients.add(sock)
    state.lanes.switch("atlas")
    delivered = asyncio.run(server._flush_undelivered(state))

    assert delivered == 0, (
        "nothing was delivered, and reporting otherwise tells the page that replies just arrived"
    )
    assert not sock.blobs, "magnus's reply must NOT be played into his conversation with atlas"
    held = [c["header"].get("text") for c in state.lane_held.get("magnus", [])]
    assert held == ["said while the phone slept"], (
        f"it belongs on magnus's hold, exactly where _speak would have put it, got {held}"
    )


def test_a_clip_for_the_live_lane_still_flushes_normally(state):
    """The other half: the check must not park everything. Without this the fix could pass by
    simply never delivering anything again."""
    state.lanes.switch("magnus")
    say_off_lane(state, "magnus", "said while the phone slept")

    sock = _Sock()
    state.clients.add(sock)
    delivered = asyncio.run(server._flush_undelivered(state))

    assert delivered == 1
    assert sock.blobs, "he is on magnus, so magnus's reply plays"
    assert not state.lane_held.get("magnus")


# ---------------------------------------------------------------- AC-6: FR6


def test_the_read_through_tick_uses_the_lanes_own_cursor(state, sock):
    """FR6. `consumed_cursor` is whatever cursor ANY lane last reported — assigned unconditionally,
    not even monotonically — so with two agents reading, this tick claimed that THIS lane had
    answered up to a point a DIFFERENT lane reached.

    Mutation: put `state.consumed_cursor` back and both assertions go red.
    """
    state.consumed_cursor = 99          # atlas read last, and read far ahead
    state.lane_consumed = {"magnus": 5, "atlas": 99}

    state.lanes.switch("magnus")
    say_http(state, "magnus", "from magnus")
    assert state.lane_read_through["magnus"] == 5, (
        "magnus answered what MAGNUS had in hand — 99 is atlas's reading, and claiming it would "
        "mark turns read that nobody on this lane has seen"
    )

    state.lanes.switch("atlas")
    say_http(state, "atlas", "from atlas")
    assert state.lane_read_through["atlas"] == 99


# ------------------------------------------------------- F5: the dead page


def test_the_last_page_leaving_returns_what_it_was_holding(state, sock):
    """F5. The clips were sent, never confirmed, and no receipt is coming from a socket that no
    longer exists — so without this they are simply gone, which is the one thing `lane_held` exists
    to prevent. And a stale `speaking_lane` would leave the barge gate permanently open, so the
    first thing he says after reconnecting registers as an interruption of nobody."""
    say_on(state, "magnus", "first")
    say_on(state, "magnus", "second")
    assert len(state.lane_inflight.get("magnus", [])) == 2

    asyncio.run(server._drop_client(state, sock))

    assert state.speaking_lane is None, "no page, nothing playing — the gate must close"
    assert state.playing_clip is None
    held = [c["header"].get("text") for c in state.lane_held.get("magnus", [])]
    assert held == ["second"], (
        f"the queued one comes back; the one on the speaker he part-heard does not (025), got {held}"
    )


def test_the_receipt_after_a_barge_still_names_its_own_lane(state, sock, his_voice):
    """Spec 026 correcting spec 024. `stop_playback` is what MAKES the receipt, so one is always on
    its way; emptying `clip_owner` wholesale sent it through the `or state.lanes.current` fallback
    and marked the lane he had just turned to idle.

    Mutation: clear the map unconditionally and this goes red.
    """
    clip = say_on(state, "magnus", "from magnus")
    asyncio.run(server._set_lane(state, "atlas", why="tap"))
    asyncio.run(server._set_agent_state(state, "thinking", "atlas"))
    state.user_speaking = True
    asyncio.run(server._maybe_barge(state, _loud()))

    # The browser's `onended`, ~8 ms behind the stop.
    asyncio.run(server._on_control(state, json.dumps({"type": "played", "id": clip["id"]}), sock))

    assert state.lane_states.get("atlas") == "thinking", (
        "atlas is mid-thought and had nothing to do with that clip — a receipt must never release "
        "whichever lane happens to be live"
    )
