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
    # `speaking_lane` and not `agent_state`, because that is what the gate reads now (spec 026 FR2).
    # It names WHOSE clip is on the speaker, which is the only thing that can be talked over;
    # `agent_state` answered "is the agent I am talking TO speaking" out of a cache a lane switch
    # left stale. Setting the honest signal here is also what makes the interruption below
    # same-lane — he is on claude and claude is the one speaking.
    state.speaking_lane = "claude"
    state.user_speaking = True
    state.undelivered.append({"header": {"type": "audio_header"}, "pcm": b"x", "at": 0.0})
    asyncio.run(server._maybe_barge(state, _loud()))

    assert state.barges == 1, "the barge must actually have fired, or this proves nothing"
    assert state.undelivered == [], "and it must still drop the LIVE lane's queue"

    asyncio.run(server._set_lane(state, "codex", why="wake"))

    assert audio_texts(sock) == ["codex here"], (
        "barge-in silences the LIVE lane; it must not discard another lane's held speech"
    )


def test_the_barge_in_path_never_DISCARDS_a_lane_hold(state):
    """The coupling would be reintroduced by someone tidying two similar-looking stores into one.

    ⚠ **This used to assert that the string `lane_held` did not appear in `_maybe_barge` at all,
    and spec 022 broke that proxy without breaking the guarantee.** Barge-in now RETURNS un-played
    clips to their lane's hold — it writes to the store it must never empty — so "the name is
    absent" stopped meaning "the hold is safe". The invariant worth holding is *discard*, so that
    is what is asserted: the destructive calls, by name.
    """
    import inspect
    body = inspect.getsource(server._maybe_barge)

    assert "undelivered.clear()" in body, "the live lane's queue is still dropped"
    for destructive in ("lane_held.clear()", "lane_held.pop(", "lane_held = {}"):
        assert destructive not in body, (
            f"barge-in must never {destructive} — he interrupted the lane he is ON, "
            f"not an agent he cannot hear"
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


def test_the_off_lane_hint_tells_the_agent_to_SPEAK_not_to_wait():
    """🔴 THE DEADLOCK, AS AN ASSERTION ON THE WORDS THAT CAUSED IT.

    Every mechanism in this file already worked: an off-lane `say` is held, not refused, and it
    raises a hand with a count. What failed was the SENTENCE the tool hands the agent when he
    switches away. It ended *"then wait on this same watch"* and said nothing about speaking — and
    silence about the one thing the agent is holding reads as a rule against doing it.

    JJ, 2026-08-25: *"I've noticed some agents waiting for me to return to their lane before saying
    something. And that defeats the purpose because the only reason I will return to an agent's
    lane is because they said something."*

    🎯 **A capability nobody is told about is not a capability.** The hold has been shipped and
    tested since spec 012; the agents using it were told, in the only place they read, to wait.
    """
    from voice_tunnel import cli

    hint = cli.DESCRIBE["commands"]["watch"]["returns"]["on_lane"]
    assert "say" in hint.lower(), "the off-lane branch must name the action, not only the wait"
    assert "deadlock" in hint.lower(), (
        "and it must say WHY waiting is wrong — the reason is the circle, and an agent that only "
        "reads 'you may speak' will still default to the politer-looking silence"
    )
    assert "held" in hint.lower(), "and that the clip survives, which is what makes speaking safe"


def test_a_flushed_reply_survives_a_barge_in_and_the_hand_comes_back(_his_voice, state, sock):
    """🔴 SPEC 022 — THE LOSS JJ HIT ON 2026-08-26, and the one this file's older guard missed.

    The store was protected; the CLIPS were not. `_flush_lane_held` popped them out of
    `lane_held` and pushed them to the browser, and the `stop_playback` that barge-in broadcasts
    empties that queue wholesale. Between the lane switch and the first `played` receipt, an
    agent's replies existed nowhere the server could recover them.

        "I just switched to Kepler while you were still speaking this last turn."
        "I didn't hear Kepler's turns. And they had two turns pending. And now the hand with the
         two counts is lost. And I don't know what Kepler wanted to say."

    Timing log: `lane_held_flushed kepler count 3`, `barge_in` 16 s later, and no `played` for any
    of the three.
    """
    state.lanes.switch("claude")
    say(state, "codex first", lane="codex")
    say(state, "codex second", lane="codex")

    # He taps over to codex. BOTH clips are flushed to the browser and are now in flight; the
    # first is on the speaker, the second is queued behind it.
    asyncio.run(server._set_lane(state, "codex", why="tap"))
    assert len(state.lane_inflight.get("codex", [])) == 2, (
        "the flush must keep a copy of each until playback lands"
    )
    assert not state.lane_held.get("codex"), "and it is no longer merely held"

    # He speaks before it plays. Barge-in tells the page to drop its whole queue.
    # `speaking_lane` is the gate's source since spec 026 FR2 — and the flush above set it to
    # "codex" for real, so this only restates what the server already did. He is ON codex, so this
    # is a same-lane interruption and the played clip stays gone (spec 025).
    state.speaking_lane = "codex"
    state.user_speaking = True
    asyncio.run(server._maybe_barge(state, _loud()))
    assert state.barges == 1, "the barge must actually have fired, or this proves nothing"

    # ⚠ THE SECOND comes back; the FIRST does not, and that distinction is spec 025. He heard the
    # one that was playing — interrupting it is what he DID — and stopping it is what makes the
    # browser send its receipt, so returning it would replay a turn he has already heard. Measured
    # live 2026-08-26: returned at 16:09:56.121, its receipt at 16:09:56.129, replayed five
    # minutes later. Everything queued BEHIND it never reached his ears and must survive.
    assert not state.lane_inflight.get("codex"), "nothing may stay in flight after the queue is dropped"
    held = [c["header"]["text"] for c in state.lane_held.get("codex", [])]
    assert held == ["codex second"], (
        f"only the un-played clip returns, not the one he interrupted — got {held}"
    )
    waiting = [m for m in sock.headers
               if m.get("type") == "lane_waiting" and m.get("lane") == "codex"]
    assert waiting and waiting[-1]["waiting"] == 1, (
        "the hand must come back up — a silent loss is what made this unreportable"
    )

    # And it still reaches him on the next switch.
    asyncio.run(server._set_lane(state, "claude", why="tap"))
    asyncio.run(server._set_lane(state, "codex", why="tap"))
    assert "codex second" in audio_texts(sock)


def test_a_played_receipt_releases_the_servers_copy(state, sock):
    """NFR1. The copy is insurance, not a leak — a clip he actually heard must not be held
    forever, or a long session accumulates every reply ever flushed."""
    state.lanes.switch("claude")
    say(state, "codex answer", lane="codex")
    asyncio.run(server._set_lane(state, "codex", why="tap"))

    clip_id = next(h["id"] for h in sock.headers if h.get("type") == "audio_header")
    server._clip_played(state, clip_id)

    assert not state.lane_inflight.get("codex"), "the receipt is what lets the server forget it"
