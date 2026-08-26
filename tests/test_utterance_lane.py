"""Spec 023 — an utterance belongs to the lane he was speaking TO, not the one live when ASR ends.

**He found this from use, and diagnosed it himself.** JJ, 2026-08-26:

    "Whenever I start speaking on a lane, I would like to finish speaking and be able to switch to
     a different lane while that thing that I just said just finishes transcribing. Right now I
     have to finish speaking and wait until everything I have said finishes transcribing before
     switching to another lane. Because if I don't wait, whenever [it] finishes transcribing, it's
     going to be added to the lane I just switched to. And that doesn't make sense."

Confirmed in source before a line was changed: `t_start` is captured when the utterance closes,
but `lanes.resolve` runs ~130 lines later, AFTER ASR and the voiceprint — so it read whichever
lane was live at the END of a multi-second pipeline.

🎯 **The shape spec `016` fixed OUTBOUND and nobody applied inbound:** *a stage belongs to the lane
that owned it, resolved ONCE at the start.* A reply carries the lane of the agent that composed it;
his words did not carry the lane of the agent he was addressing.

⚠ **The mis-delivery is unrecoverable, which is what makes it worse than a dropped turn.** A lost
reply can be repeated. Words delivered to the wrong agent are in that agent's context and cannot be
taken back out.

🔴 **THESE TESTS DRIVE `_emit`, AND THE FIRST VERSION DID NOT.** It called `lanes.resolve` through
a local helper that re-implemented the server's own line, and a mutation check showed the guard was
worthless: reverting the fix in `server.py` left all six green, because they were testing the
helper. `tests/test_lane_hold.py` already carries this warning from a previous occurrence of the
same mistake — *"Drive the real path or do not claim to."*
"""
import asyncio

import pytest

from voice_tunnel import server


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "magnus")
    st = server.TunnelState("t", token=None)
    st.consumed_cursor = -1
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("atlas")
    # Identity is a separate gate with its own suite; point it at nothing so these tests measure
    # routing only, deterministically, whether or not a voiceprint is installed.
    st.embedder.model_path = str(tmp_path / "no-such-voiceprint.onnx")
    return st


async def _drive(state, completed):
    # The loop `_emit` runs ASR on must be the one it is awaited in, or `run_in_executor` builds a
    # future on a loop nobody is driving. Take the running one rather than making a second.
    await server._emit(state, completed, asyncio.get_running_loop())


def speak(state, text, t_start=0.0, t_end=1.0):
    """One utterance through the REAL gate-and-log path, and the lane it was stamped with."""
    state.recognizer.transcribe = lambda _samples: text
    asyncio.run(_drive(state, (None, t_start, t_end)))
    turns = [t for t in server.store.read_turns("t") if t.get("text")]
    assert turns, "the utterance never reached the log — this test proves nothing without it"
    return turns[-1]


def begin_utterance(state):
    """What the audio path latches on the silence→speech edge."""
    state.utterance_lane = state.lanes.current


# ============================================================ the defect itself


def test_a_switch_during_transcription_does_not_steal_the_utterance(state):
    """🔴 THE BUG, through the real emit path. He speaks to magnus, taps to atlas while ASR is
    still running, and the turn must still be stamped magnus."""
    begin_utterance(state)                   # he starts talking to magnus
    state.lanes.switch("atlas")              # he taps away before the transcript lands

    turn = speak(state, "so what do you think about that")

    assert turn["lane"] == "magnus", (
        "the words were spoken to magnus and belong to magnus, whatever happened since"
    )


def test_the_latch_is_cleared_so_the_next_utterance_is_not_inherited(state):
    """NFR1. Per-utterance state, not a second source of truth about the live lane — a stale latch
    would keep delivering to a lane he had already left, which is the same bug pointing the other
    way."""
    begin_utterance(state)
    state.lanes.switch("atlas")
    speak(state, "this one was for magnus")

    assert state.utterance_lane is None, "routing the turn must release the latch"

    begin_utterance(state)                   # he now starts speaking to atlas
    turn = speak(state, "and this one is for atlas", t_start=2.0, t_end=3.0)

    assert turn["lane"] == "atlas"


# ============================================ what must KEEP working


def test_naming_a_lane_still_switches(state):
    """⚠ The one case that must still move. Saying a name is him addressing somebody ON PURPOSE,
    and the resolver reads that from the transcript rather than from the live lane — so carrying
    the utterance's lane must not make a deliberate summons sticky."""
    begin_utterance(state)
    turn = speak(state, "hey atlas can you look at this")

    assert turn["lane"] == "atlas"
    assert state.lanes.current == "atlas", "and the conversation actually moves"


def test_an_ordinary_turn_with_nothing_latched_uses_the_live_lane(state):
    """A turn arriving outside the speech path — or the first after a restart — must behave
    exactly as before rather than routing to nobody."""
    state.lanes.switch("atlas")
    assert state.utterance_lane is None

    turn = speak(state, "carry on then")

    assert turn["lane"] == "atlas"


def test_a_summons_to_the_lane_he_is_already_on_does_not_move_anything(state):
    state.lanes.switch("atlas")
    begin_utterance(state)

    turn = speak(state, "hey atlas one more thing")

    assert turn["lane"] == "atlas"
    assert state.lanes.current == "atlas"
