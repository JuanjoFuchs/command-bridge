"""Spec 024 — a `played` receipt releases the lane whose clip it was, not whoever spoke last.

**The last instance of the pattern that ran through 013, 015, 016, 017 and 018:** a fact made
per-lane in STORAGE and left session-wide in RESOLUTION. `lane_states` is a dict; `speaking_lane`
was one slot. With two clips in flight the second overwrote the first, and the first receipt then
released the WRONG lane and nulled the slot — leaving the real speaker on "speaking" with nothing
left in the system that could ever clear it.

    "I see an agent lane that has its hand raised. And when I switch to it, it doesn't start
     playing. And another lane says speaking."
    "I don't know. Is it because I switch while the other one is synthesizing?"
    "Yes, we need to fix that. It just happened again with Atlas."

⚠ **The receipt always carried a clip id and never a lane** — the comment on `speaking_lane` said
so in as many words. The fix is not new information, it is keying by the id that was already there.

🔴 These drive `_speak` and the real `played` handler. An earlier spec in this same session shipped
tests that re-implemented the server's line instead of calling it, and a mutation check found them
worthless; see the note at the top of `tests/test_utterance_lane.py`.
"""
import asyncio

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


def say_on(state, lane, text):
    """Speak as `lane`, with that lane live so the clip actually goes out."""
    state.lanes.switch(lane)
    return asyncio.run(server._speak(state, text, None, lane=lane))


def report_played(state, sock, clip_id):
    """THE REAL RECEIPT, through the real control handler — the same JSON the page sends."""
    import json
    asyncio.run(server._on_control(state, json.dumps({"type": "played", "id": clip_id}), sock))


def test_each_clip_records_its_own_lane(state, sock):
    a = say_on(state, "magnus", "from magnus")
    b = say_on(state, "atlas", "from atlas")

    assert state.clip_owner[a["id"]] == "magnus"
    assert state.clip_owner[b["id"]] == "atlas", (
        "a second clip going out must not erase who the first one belonged to"
    )


def test_the_first_receipt_releases_ITS_lane_and_leaves_the_other_speaking(state, sock):
    """🔴 THE BUG, through the real handler. Two clips in flight; magnus's receipt arrives first.

    Under the single slot this released ATLAS — the lane that happened to speak last — and nulled
    the slot, so magnus stayed on "speaking" with nothing left that could clear it. That is the
    lane he saw stuck.
    """
    a = say_on(state, "magnus", "from magnus")
    say_on(state, "atlas", "from atlas")
    assert state.lane_states.get("atlas") == "speaking"

    report_played(state, sock, a["id"])

    assert state.lane_states.get("magnus") == "idle", "magnus's clip ended, so magnus is at rest"
    assert state.lane_states.get("atlas") == "speaking", (
        "atlas is still playing and must NOT have been released by somebody else's receipt"
    )
    assert a["id"] not in state.clip_owner, "and the finished clip is forgotten"


def test_the_second_receipt_then_releases_the_second_lane(state, sock):
    """The other half: once its own receipt lands, atlas comes to rest too. Without this the fix
    could pass by simply never releasing anything."""
    a = say_on(state, "magnus", "from magnus")
    b = say_on(state, "atlas", "from atlas")
    report_played(state, sock, a["id"])
    report_played(state, sock, b["id"])

    assert state.lane_states.get("atlas") == "idle"
    assert state.clip_owner == {}, "nothing in flight, nothing remembered"


def test_a_barge_in_forgets_everything_in_flight(state, sock, monkeypatch):
    """The owners would otherwise accumulate for the life of the session. Driven through the real
    barge path.

    ⚠ **EXCEPT THE CLIP THAT WAS PLAYING**, and that exception is spec 026 correcting spec 024.
    This test used to assert the map was emptied, on 024's reasoning that *"no receipt is coming
    for a queue the client was told to drop"* — which spec 025 then measured to be false. Stopping
    playback is exactly what makes the browser fire `onended`, so a `played` for the interrupted
    clip lands about 8 ms later. Emptying the map wholesale made that receipt fall through to the
    `or state.lanes.current` fallback and mark **the lane he is now talking to** idle.
    """
    import numpy as np
    monkeypatch.setattr(type(state.embedder), "available", property(lambda _s: True))
    monkeypatch.setattr(state.embedder, "embed", lambda _s: [0.1] * 192)
    monkeypatch.setattr(server.voiceprint, "match", lambda _e: ("me", 0.99))
    monkeypatch.setattr(server.config, "barge_in_enabled", lambda: True)
    monkeypatch.setattr(server.config, "barge_in_threshold", lambda: 0.15)

    say_on(state, "magnus", "from magnus")
    assert state.clip_owner

    # The gate reads `speaking_lane` (spec 026 FR2); `say_on` above already set it to magnus, so
    # this is the same-lane case — he is on magnus and magnus is speaking.
    played_clip = state.playing_clip
    state.user_speaking = True
    asyncio.run(server._maybe_barge(state, np.ones(int(16000 * 1.5), dtype=np.float32) * 0.2))

    assert state.barges == 1, "the barge must actually have fired, or this proves nothing"
    assert state.clip_owner == {played_clip: "magnus"}, (
        "only the interrupted clip is remembered, because only its receipt is still coming"
    )
