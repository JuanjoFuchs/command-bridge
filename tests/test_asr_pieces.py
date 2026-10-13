"""A turn is transcribed in pieces at his pauses while he is still talking (2026-10-06).

JJ: *"I want this whole thing to be fast. Don't stop until you have a full root cause and a plan of
action that you have tested."* The root cause: Parakeet is offline, so the whole turn was decoded
AFTER he stopped (~0.1 s per second of speech), and the live preview re-decoded the whole turn every
0.7 s with the final queued behind it — 11-20 s on minute-long turns. Pieces committed at pauses
leave only the tail.

NO MODEL AND NO SERVER: the segmenter is driven with synthetic audio and the recognizer is stubbed,
recording what audio each call actually received.
"""
import asyncio
import types

import numpy as np
import pytest

from command_bridge import asr, config, server

SR = config.TARGET_SR


def tone(seconds: float) -> np.ndarray:
    """Speech-like: 180 ms syllables with 70 ms gaps. A steady tone would not do — the segmenter
    learns its noise floor from the quietest 20% of the last 8 s, so seconds of unbroken tone become
    the 'room' and the turn closes mid-word. Real speech has the gaps."""
    t = np.arange(int(SR * seconds)) / SR
    on = (t % 0.25) < 0.18
    return (0.1 * np.sin(2 * np.pi * 220 * t) * on).astype(np.float32)


def quiet(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype=np.float32)


# -- join_pieces --------------------------------------------------------------------------------

def test_a_stop_the_model_added_at_a_breath_is_dropped_when_the_sentence_goes_on():
    assert asr.join_pieces(["I finish a long thought and then I wait.", "and the wait grows."]) \
        == "I finish a long thought and then I wait and the wait grows."


def test_a_real_sentence_end_is_kept():
    assert asr.join_pieces(["First this.", "Second that."]) == "First this. Second that."


def test_empty_pieces_are_skipped():
    assert asr.join_pieces(["", "hello there", ""]) == "hello there"


def test_the_artifact_filter_judges_the_whole_turn_not_a_piece():
    assert asr.join_pieces(["Yeah.", "let's do that."]) == "Yeah let's do that."
    assert asr.join_pieces(["you", ""]) == ""


# -- UtteranceBuffer.commit_point ---------------------------------------------------------------

ROOM = quiet(3.0)
"""A quiet room first. The segmenter measures its noise floor from what it hears, so a test that
opens on speech would teach it that the speech is the room."""


def fed(*parts) -> asr.UtteranceBuffer:
    b = asr.UtteranceBuffer(end_silence_ms=1500, grace_ms=0)
    for part in (ROOM, *parts):
        for i in range(0, part.size, 320):
            assert b.feed(part[i:i + 320]) is None
    return b


def test_a_pause_after_enough_speech_gives_a_cut_inside_the_pause():
    b = fed(tone(4.0), quiet(0.7))
    end = b.commit_point(b.onset_index(), int(SR * 0.6), int(SR * 3.0))
    assert end is not None
    speech_end = b.span(0).size - int(SR * 0.7)
    assert speech_end < end < b.span(0).size


def test_a_pause_shorter_than_the_threshold_gives_nothing():
    b = fed(tone(4.0), quiet(0.3))
    assert b.commit_point(b.onset_index(), int(SR * 0.6), int(SR * 3.0)) is None


def test_too_little_speech_since_the_last_cut_gives_nothing():
    b = fed(tone(2.0), quiet(0.7))
    assert b.commit_point(b.onset_index(), int(SR * 0.6), int(SR * 3.0)) is None


def test_the_same_pause_cannot_cut_twice():
    b = fed(tone(4.0), quiet(0.7))
    end = b.commit_point(b.onset_index(), int(SR * 0.6), int(SR * 3.0))
    assert b.commit_point(end, int(SR * 0.6), int(SR * 3.0)) is None


# -- the server path ----------------------------------------------------------------------------

@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setenv("COMMAND_BRIDGE_ASR_COMMIT_PAUSE_MS", "600")
    monkeypatch.setenv("COMMAND_BRIDGE_ASR_COMMIT_MIN_S", "3.0")
    st = server.TunnelState("t", token=None, gate_enabled=False)
    st.buffer = asr.UtteranceBuffer(end_silence_ms=1500, grace_ms=0)
    st.embedder = types.SimpleNamespace(available=False)
    calls = []

    def transcribe(samples, piece=False):
        calls.append((round(samples.size / SR, 1), piece))
        return f"part{len(calls)}."

    st.recognizer = types.SimpleNamespace(transcribe=transcribe, try_transcribe=lambda s: None)
    st.calls = calls
    monkeypatch.setattr(config, "PARTIAL_INTERVAL_S", 0)
    return st


def speak(st, *parts):
    """Feed audio through the real ingest path in 20 ms chunks, letting scheduled piece decodes
    run between chunks the way the event loop would."""
    emitted = []

    async def fake_emit(state, completed, loop, pieces=None):
        emitted.append(await server._transcribe_turn(state, completed[0], loop, pieces))

    async def drive():
        loop = asyncio.get_running_loop()
        for part in (ROOM, *parts):
            for i in range(0, part.size, 320):
                chunk = part[i:i + 320]
                utterance_before = st.buffer.utterance_id
                completed = st.buffer.feed(chunk)
                if completed is None:
                    server._maybe_commit(st, loop)
                    await asyncio.sleep(0)
                    if st.pieces.task is not None:
                        await st.pieces.task
                    continue
                pieces = server._take_pieces(st, utterance_before)
                await fake_emit(st, completed, loop, pieces)

    asyncio.run(drive())
    return emitted


def test_a_long_turn_with_pauses_decodes_only_the_tail_after_he_stops(state):
    out = speak(state, tone(4.0), quiet(0.7), tone(4.0), quiet(0.7), tone(2.0), quiet(1.6))
    pieces = [c for c in state.calls if c[1]]
    assert len(pieces) == 3                       # two at the pauses, plus the tail
    tail_seconds = pieces[-1][0]
    assert tail_seconds < 4.0                     # the tail, not the whole ~13 s turn
    # the stub answers lower-case "partN.", so each stop before the next piece is a breath, dropped
    assert out == ["part1 part2 part3."]


def test_a_short_turn_is_decoded_whole_exactly_as_before(state):
    out = speak(state, tone(2.0), quiet(1.6))
    assert state.calls == [(state.calls[0][0], False)]
    assert out == ["part1."]


def test_pieces_from_another_utterance_are_never_used(state):
    # The segmenter discards a too-short or noise-only utterance and reports it exactly like "not
    # finished", so the pieces' own utterance id is the only thing that can tell them apart.
    p = server._Pieces(base=123)
    p.upto, p.texts = 5000, ["stale."]
    state.pieces = p
    assert server._take_pieces(state, utterance_id=456) is None
    assert state.pieces.upto is None


def test_a_failed_piece_falls_back_to_one_whole_decode(state):
    p = server._Pieces(base=state.buffer.utterance_id)
    p.upto, p.texts, p.failed = 8000, [""], True
    samples = np.ones(SR * 2, dtype=np.float32)

    async def run():
        return await server._transcribe_turn(state, samples, asyncio.get_running_loop(), p)

    assert asyncio.run(run()) == "part1."
    assert state.calls == [(2.0, False)]
