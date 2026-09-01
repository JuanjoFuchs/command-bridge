"""Spec 016 — a stage belongs to the lane it is ABOUT, and whoever opens it closes it.

**The report, 2026-08-25, three lanes live on his phone:** *"I have noticed that the different
lanes statuses also change. For example, if I am talking to you, I do see that the Atlas lane says
transcribing."* And, a minute later, the diagnosis he asked for rather than a patch: *"we have to
manage multiple statuses. We need a better state machine for this that understands multiple states
for all the lanes and is able to collapse them into a single state for the solo mode."*

**The mechanism these tests pin.** Every stage of an exchange is two calls — an open and a close —
with real work between them. `_set_agent_state` used to resolve an absent lane to
`state.lanes.current`, so both calls asked the same question at different times. The live lane
moves in that gap, and when it does the stage opens on one lane and closes on another. The first
one is stranded on a word forever, because nothing else in the server ever revisits it.

⚠ **DRIVEN THROUGH THE REAL FLOWS, not by calling the publisher directly.** The publisher was never
the bug — it did exactly what it was asked. The bug was in what the callers asked for, twice, and a
test that calls `_set_agent_state` itself cannot see that.

NO SERVER IS STARTED HERE, for the same reason spec 012's suite starts none: a live voice session
runs on `dev` throughout this work.
"""
import asyncio

import pytest

from command_bridge import server, store


class _Client:
    """A socket that records instead of sending.

    ⚠ **A bare `object()` DOES NOT WORK, and it fails in the most misleading way available.**
    `_broadcast_json` prunes any client it cannot send to — so the first state change of the flow
    silently emptied `state.clients`, the deliverability test thirty lines later read `no_client`,
    and the clip was queued instead of played. The assertion then failed on the STATE, which looks
    exactly like the state machine being wrong rather than the fixture being unable to receive.
    """

    def __init__(self):
        self.json = []

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, data):
        pass


class _Req:
    """Only what the handlers touch — the stub shape spec 007's suite uses."""

    def __init__(self, state, body):
        self.app = {"state": state}
        self._body = body
        self.remote = "127.0.0.1"
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setenv("COMMAND_BRIDGE_WAKE_NAME", "magnus")
    st = server.TunnelState("t", token=None)
    st.consumed_cursor = -1
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("atlas")
    # Identity is a separate gate with its own suite; point it at nothing so these tests measure
    # ownership only and are deterministic whether or not a voiceprint is installed.
    st.embedder.model_path = str(tmp_path / "no-such-voiceprint.onnx")
    return st


def run(coro):
    return asyncio.run(coro)


async def _drive(state, completed):
    # The loop `_emit` runs ASR on must be the one it is awaited in, or `run_in_executor` builds a
    # future on a loop nobody is driving.
    await server._emit(state, completed, asyncio.get_running_loop())


def speak_into(state, text, t_start=0.0, t_end=1.0):
    """Drive one utterance through the real transcribe-gate-log path."""
    state.recognizer.transcribe = lambda _samples: text
    run(_drive(state, (None, t_start, t_end)))
    return store.read_turns("t")[-1]


# ------------------------------------------------------------------ AC-1: the stranded lane

def test_a_lane_that_loses_the_conversation_mid_utterance_does_not_keep_the_word(state):
    """AC-1 — FR1, FR2. HIS REPORT, AS AN ASSERTION, through the real flow.

    atlas is live from an earlier summons. He starts speaking; the tunnel marks `transcribing`,
    which lands on atlas because the words have not been recognised yet and atlas is the only
    honest answer available. ASR then returns "hey magnus ..." and the wake gate moves the
    conversation. Before spec 016 the closing `idle` resolved to the live lane AGAIN — magnus —
    and atlas was never told its stage had ended.

    ⚠ The stranding was PERMANENT: nothing else in the server revisits a lane's state, so the orb
    read `transcribing` until the page was reloaded.
    """
    state.lanes.current = "atlas"

    turn = speak_into(state, "hey magnus what is the status")

    assert turn["lane"] == "magnus", "the gate moved the conversation, which is the precondition"
    assert state.lanes.current == "magnus"
    assert state.lane_states.get("atlas") != "transcribing", (
        "atlas is stranded on a stage it can never leave: the word was given to it while it was "
        "live, and the idle that would clear it went to whichever lane was live afterwards"
    )
    assert state.lane_states.get("atlas") == "idle", (
        "the lane that opened the stage is the lane that must be returned to rest"
    )


def test_an_utterance_that_does_not_move_the_conversation_rests_the_same_lane(state):
    """The control for AC-1: with no switch, the opener and the live lane are the same, and the
    behaviour is exactly what it was before spec 016. A fix that only worked when a switch
    happened would have quietly changed the common case."""
    state.lanes.current = "magnus"

    speak_into(state, "hey magnus keep going")

    assert state.lane_states.get("magnus") == "idle"
    assert "atlas" not in state.lane_states, "a lane nobody addressed gains no state at all"


# ------------------------------------------------------------------ AC-2: the clip's own lane

def test_a_clip_marks_the_lane_it_is_FOR_not_whichever_lane_is_live(state, monkeypatch):
    """AC-2 — FR3. The outbound half, and the sharper case of the two.

    Transcription is at least ABOUT the live lane at the instant it opens. A clip is not: it
    belongs to the agent that composed it, and that agent may lose the conversation while its
    words are still playing. Marking the live lane `speaking` claims a different agent is talking
    than the one being heard.
    """
    monkeypatch.setattr(server.tts, "synthesize", lambda text, **k: (b"\x00\x01" * 64, 22050))
    monkeypatch.setattr(server, "_send_clip", lambda *a, **k: asyncio.sleep(0))
    state.clients.add(_Client())
    state.channel_open = True
    state.lanes.current = "magnus"

    run(server._speak(state, "all green", None, lane="magnus"))
    assert state.lane_states["magnus"] == "speaking"
    assert state.speaking_lane == "magnus"

    # He moves to atlas while magnus's clip is still in the air.
    run(server._set_lane(state, "atlas"))

    assert state.lane_states["magnus"] == "speaking", "magnus is the one being heard"
    assert state.lane_states.get("atlas") != "speaking", (
        "atlas is shown speaking because it happens to be live; the clip is magnus's"
    )


def test_the_playback_receipt_releases_the_lane_that_was_speaking(state, monkeypatch):
    """AC-2 — FR3, second half. The `played` receipt carries a clip id and no lane, and by the time
    it lands the conversation may have moved. Releasing the live lane would leave the real speaker
    on `speaking` for good and reset an agent that never opened its mouth."""
    monkeypatch.setattr(server.tts, "synthesize", lambda text, **k: (b"\x00\x01" * 64, 22050))
    monkeypatch.setattr(server, "_send_clip", lambda *a, **k: asyncio.sleep(0))
    state.clients.add(_Client())
    state.channel_open = True

    run(server._speak(state, "all green", None, lane="magnus"))
    run(server._set_lane(state, "atlas"))
    # The receipt, as the websocket handler applies it.
    run(server._set_agent_state(state, "idle", state.speaking_lane or state.lanes.current))

    assert state.lane_states["magnus"] == "idle", "the speaker is the one returned to rest"
    assert state.lane_states.get("atlas") != "idle" or "atlas" not in state.lane_states, (
        "atlas was never speaking, so the receipt has nothing to say about it"
    )


# ------------------------------------------------------------------ AC-3: no unowned stage

def test_a_stage_cannot_be_published_without_naming_its_owner(state):
    """AC-3 — FR4. The defaulting parameter was the bug's habitat: it let a caller mean "the live
    lane" without saying WHEN, and the answer differed between the open and the close. Removing
    the default makes the ambiguity unexpressible rather than merely discouraged, which is the
    only version of this that survives the next stage somebody adds."""
    with pytest.raises(TypeError):
        run(server._set_agent_state(state, "thinking"))


# ------------------------------------------------------------------ AC-4: solo collapses

def test_a_single_lane_session_still_has_exactly_one_state(monkeypatch, tmp_path):
    """AC-4 — NFR1. With one lane there is nothing to attribute, so the per-lane machine must
    produce exactly what the page showed before lanes existed: one state, on the one lane, and
    `agent_state` still carrying it for every client that predates the per-lane field."""
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setenv("COMMAND_BRIDGE_WAKE_NAME", "magnus")
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry("magnus")

    for stage in ("transcribing", "thinking", "speaking", "idle"):
        run(server._set_agent_state(st, stage, st.lanes.current))
        assert st.agent_state == stage
        assert st.lane_states == {"magnus": stage}, (
            f"a solo session grew a second lane state at {stage!r}: {st.lane_states}"
        )
