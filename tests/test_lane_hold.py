"""Spec 012 Slice B — **AC-16, the TC3 gate.** Held speech must actually arrive.

TC3 is not a hypothetical. Measured 2026-08-20: nine clips issued while the channel was closed
each returned `queued: true`, and **none played when it reopened.** FR7's entire value is that a
waiting agent is not silently dropped, so the hold has to be proven to deliver before any UI mark
promises that it does.

**The assertions are on the BYTES ARRIVING, never on `queued: true`** — that flag was true for all
nine lost clips, which is precisely why it cannot be the thing a test believes.

The one mechanism found that can lose all nine is barge-in clearing the queue wholesale. That
clearing is right for the lane he is ON and wrong for a lane he is not on, so the barge-in case
below is the specific regression this file exists to prevent.

NO SERVER IS STARTED HERE. A live voice session was running on `dev` throughout this work.
"""
import asyncio

import pytest

from voice_tunnel import server
from voice_tunnel.lanes import BROADCAST


class _Sock:
    """A client that records what it was actually sent, in order."""

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
    st.lanes = server.lanes_mod.LaneRegistry("claude")
    st.lanes.add("codex")
    st.channel_open = True
    st.cues_enabled = False
    return st


@pytest.fixture
def sock(state):
    s = _Sock()
    state.clients.add(s)
    return s


def say(state, text, lane):
    """Synthesize through the real path, with TTS stubbed to a known payload."""
    return asyncio.run(server._speak(state, text, None, lane=lane))


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    """Real synthesis is slow, needs a voice on disk, and is not what this file measures. The
    bytes are distinct per text so a delivered clip can be identified rather than merely counted.
    """
    def synth(text, voice=None, speed=None, pause=None):
        return (text.encode("utf-8") * 8), 22050

    monkeypatch.setattr(server.tts, "synthesize", synth)
    # The hold decision must not depend on the speech-grace loop, which is a different guard with
    # its own suite. Nothing is speaking in these tests.
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.0, raising=False)


def _loud():
    """Enough samples for the barge window, at a level the RMS gate accepts."""
    import numpy as np
    return (np.ones(int(16000 * 1.5), dtype=np.float32) * 0.2)


@pytest.fixture
def _his_voice(monkeypatch, state):
    """Make the voiceprint say 'this is him' without needing a model on disk.

    Identity has its own suite (`scripts/bargein.py` drives the real gallery with his recorded
    speech). Here it is stubbed so this file measures ONE thing: what a barge-in does to a hold.
    """
    monkeypatch.setattr(type(state.embedder), "available", property(lambda _self: True))
    monkeypatch.setattr(state.embedder, "embed", lambda _s: [0.1] * 192)
    monkeypatch.setattr(server.voiceprint, "match", lambda _e: ("me", 0.99))
    monkeypatch.setattr(server.config, "barge_in_enabled", lambda: True)
    monkeypatch.setattr(server.config, "barge_in_threshold", lambda: 0.15)
    return state


def audio_texts(sock):
    """Which clips actually reached the wire, by their text, in delivery order."""
    return [h.get("text") for h in sock.headers if h.get("type") == "audio_header"]


# ------------------------------------------------------------------ AC-16

def test_an_off_lane_reply_does_not_play(state, sock):
    """The first half of FR7: it must not talk over the conversation he is having."""
    state.lanes.switch("claude")
    out = say(state, "codex here", lane="codex")

    assert out["delivered"] is False
    assert out["held_off_lane"] is True
    assert out["reason"] == "off_lane"
    assert audio_texts(sock) == [], "nothing may reach the wire while another lane is live"
    assert len(state.lane_held["codex"]) == 1


def test_a_held_reply_delivers_in_full_when_its_lane_becomes_live(state, sock):
    """**THE GATE.** Asserted on the bytes, not on `queued` — that flag was true for all nine
    clips TC3 measured as lost."""
    state.lanes.switch("claude")
    say(state, "codex here", lane="codex")
    assert audio_texts(sock) == []

    asyncio.run(server._set_lane(state, "codex", why="wake"))

    assert audio_texts(sock) == ["codex here"]
    assert sock.blobs == [b"codex here" * 8], "the audio itself has to arrive, whole"
    assert state.lane_held.get("codex") in (None, []), "and the hold is emptied by delivering it"


def test_a_held_reply_still_delivers_after_a_barge_in(_his_voice, state, sock):
    """**THE SPECIFIC REGRESSION THIS FILE EXISTS FOR.** Barge-in clears `undelivered` wholesale,
    and that is right for the lane he is ON: playing the next clip at a man who just interrupted
    is the same interruption wearing a different hat. It is wrong for a lane he is NOT on — he has
    not interrupted an agent he cannot hear, and discarding its speech would be the tool deciding
    on his behalf that an answer he never received is no longer wanted."""
    state.lanes.switch("claude")
    say(state, "codex here", lane="codex")

    # HE INTERRUPTS FOR REAL, through `_maybe_barge` itself. An earlier version of this test
    # simulated it by calling `state.undelivered.clear()` directly, and a mutation check showed
    # that guard was worthless: reintroducing the exact TC3 bug left it green, because it was
    # testing the simulation rather than the code. Drive the real path or do not claim to.
    state.agent_state = "speaking"
    state.user_speaking = True
    state.undelivered.append({"header": {"type": "audio_header"}, "pcm": b"x", "at": 0.0})
    asyncio.run(server._maybe_barge(state, _loud()))

    assert state.barges == 1, "the barge must actually have fired, or this proves nothing"
    assert state.undelivered == [], "and it must still drop the LIVE lane's queue"

    asyncio.run(server._set_lane(state, "codex", why="wake"))

    assert audio_texts(sock) == ["codex here"], (
        "barge-in silences the LIVE lane; it must not discard another lane's held speech"
    )


def test_the_barge_in_path_does_not_touch_the_lane_hold(state):
    """Asserted on the source as well as the behaviour, because the coupling would be reintroduced
    by someone tidying two similar-looking stores into one."""
    import inspect
    body = inspect.getsource(server._maybe_barge)

    assert "undelivered.clear()" in body, "the live lane's queue is still dropped"
    assert "lane_held" not in body.replace("lane_held` IS DELIBERATELY NOT", ""), (
        "barge-in must not reach into another lane's hold"
    )


def test_several_held_replies_arrive_oldest_first(state, sock):
    state.lanes.switch("claude")
    for text in ("first", "second", "third"):
        say(state, text, lane="codex")

    asyncio.run(server._set_lane(state, "codex", why="tap"))

    assert audio_texts(sock) == ["first", "second", "third"]


def test_a_held_reply_says_how_long_it_waited(state, sock):
    """A reply arriving four minutes after the question, with no acknowledgement that time
    passed, reads as the agent being slow rather than as him having been elsewhere."""
    state.lanes.switch("claude")
    say(state, "codex here", lane="codex")
    asyncio.run(server._set_lane(state, "codex", why="wake"))

    header = next(h for h in sock.headers if h.get("type") == "audio_header")
    assert "delayed_s" in header
    assert header.get("held_off_lane") is True


def test_each_lane_holds_its_own_and_only_its_own(state, sock):
    """Switching to codex must not release grok's held speech at him."""
    state.lanes.add("grok")
    state.lanes.switch("claude")
    say(state, "from codex", lane="codex")
    say(state, "from grok", lane="grok")

    asyncio.run(server._set_lane(state, "codex", why="wake"))

    assert audio_texts(sock) == ["from codex"]
    assert len(state.lane_held["grok"]) == 1, "grok is still waiting, and still has its clip"


def test_the_live_lane_is_never_held(state, sock):
    """The agent he IS talking to speaks immediately — the hold must not become a delay on the
    normal path."""
    state.lanes.switch("codex")
    out = say(state, "codex here", lane="codex")

    assert out["delivered"] is True
    assert out["held_off_lane"] is False
    assert audio_texts(sock) == ["codex here"]


def test_a_broadcast_is_never_held(state, sock):
    state.lanes.switch("claude")
    out = say(state, "everyone listen", lane=BROADCAST)

    assert out["delivered"] is True
    assert audio_texts(sock) == ["everyone listen"]


def test_a_say_with_no_lane_is_never_held(state, sock):
    """Every existing single-agent caller passes no lane and must be untouched by all of this."""
    state.lanes.switch("codex")
    out = say(state, "hello", lane=None)

    assert out["delivered"] is True
    assert out["held_off_lane"] is False
    assert audio_texts(sock) == ["hello"]


def test_the_hold_is_bounded_the_same_way_the_undelivered_queue_is(state):
    """Coming back to an agent after twenty minutes and being read a stack of stale answers is a
    worse experience than being told to ask again, so the hold is bounded on both axes."""
    state.lanes.switch("claude")
    for i in range(server.config.UNDELIVERED_MAX + 4):
        say(state, f"clip {i}", lane="codex")

    assert len(state.lane_held["codex"]) == server.config.UNDELIVERED_MAX
    # It is the OLDEST that are dropped, so what survives is what he is most likely to still want.
    assert state.lane_held["codex"][-1]["header"]["text"].endswith(
        str(server.config.UNDELIVERED_MAX + 3))


def test_a_lane_that_is_waiting_is_visible_in_status(state):
    """FR7's other half — the UI mark needs something to paint from, and it must be a fact the
    server measured rather than a claim an agent made."""
    state.lanes.switch("claude")
    say(state, "codex here", lane="codex")

    assert state.snapshot()["lane_waiting"] == {"codex": 1}


def test_the_page_is_told_the_moment_a_lane_starts_waiting(state, sock):
    """A mark that only appears on the next poll is a mark that lags the thing it describes."""
    state.lanes.switch("claude")
    say(state, "codex here", lane="codex")

    waiting = [h for h in sock.headers if h.get("type") == "lane_waiting"]
    assert waiting and waiting[-1] == {"type": "lane_waiting", "lane": "codex", "waiting": 1}
