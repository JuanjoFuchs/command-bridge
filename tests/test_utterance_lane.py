"""Utterance routing — a turn lands on the lane he is on WHEN TRANSCRIPTION FINISHES.

**This is the REVERT of specs 023 and 027**, at JJ's explicit direction 2026-09-02. Asked whether a
finished transcription should land on the lane he STARTED speaking to or the one he is ON when it
finishes, he chose the latter — *"where I am at when transcription finishes. That was the behaviour
before we broke it."* So there is no per-utterance latch any more: `_emit` resolves against
`state.lanes.current`. His words FOLLOW him.

A spoken name still switches, because naming an agent is addressing it on purpose — the resolver reads
the transcript for that, not the live lane.

⚠ The behaviour 023 used to protect (a tap made mid-transcription NOT redirecting the in-flight
utterance) is intentionally gone; the trade is his, and he takes it by waiting for transcription before
switching.

🔴 THESE TESTS DRIVE THE REAL `_emit`. A helper that re-implements the routing line proves nothing —
`tests/test_lane_hold.py` carries that warning from a past occurrence.
"""
import asyncio

import pytest

from command_bridge import server


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setenv("COMMAND_BRIDGE_WAKE_NAME", "magnus")
    st = server.TunnelState("t", token=None)
    st.consumed_cursor = -1
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("atlas")
    # Identity is a separate gate with its own suite; point it at nothing so these tests measure
    # routing only, whether or not a voiceprint is installed.
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


# ============================================================ his words follow him (the revert)


def test_a_turn_lands_on_the_current_lane_at_finish(state):
    """He is on atlas by the time ASR lands, so the turn is atlas — it followed him."""
    state.lanes.switch("atlas")
    assert speak(state, "so what do you think about that")["lane"] == "atlas"


def test_switching_mid_transcription_now_MOVES_the_turn(state):
    """The exact thing spec 023 used to prevent, reinstated on purpose: routing reads the current lane
    at finalize, so a tap made while ASR is still running redirects the words to the new lane."""
    state.lanes.switch("atlas")               # he moved the live lane while it transcribed
    assert speak(state, "this lands on atlas now")["lane"] == "atlas"


def test_no_switch_stays_on_the_live_lane(state):
    assert speak(state, "carry on then")["lane"] == "magnus"


# ============================================ a spoken name still addresses on purpose


def test_naming_a_lane_still_switches(state):
    turn = speak(state, "hey atlas can you look at this")
    assert turn["lane"] == "atlas"
    assert state.lanes.current == "atlas", "and the conversation actually moves"


def test_a_name_for_the_lane_already_live_does_not_move_anything(state):
    state.lanes.switch("atlas")
    turn = speak(state, "hey atlas one more thing")
    assert turn["lane"] == "atlas"
    assert state.lanes.current == "atlas"
