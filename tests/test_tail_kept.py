"""The last words of a long turn are kept (spec 027).

JJ, 2026-10-08: "It was not split. The next turn was me repeating the part that got clipped." The tail
of a long turn (his last words plus the closing silence) was gated on whole-buffer energy, so a short
ending averaged with seconds of silence was dropped without being decoded.

NO MODEL: the recognizer's engine call is stubbed, and the gate in front of it is the real one.
"""
import asyncio
import threading

import numpy as np

from command_bridge import asr, config, server
from tests.test_asr_pieces import quiet, speak, state, tone  # noqa: F401  (state is a fixture)

SR = config.TARGET_SR


def soft_words(seconds: float) -> np.ndarray:
    """Two soft words: above the floor while voiced, but diluted below it by closing silence."""
    t = np.arange(int(SR * seconds)) / SR
    on = (t % 0.25) < 0.18
    return (0.012 * np.sin(2 * np.pi * 220 * t) * on).astype(np.float32)


def recognizer() -> tuple[asr.Recognizer, list[float]]:
    """A real Recognizer (so the real gate runs) whose engine call is stubbed and recorded."""
    r = asr.Recognizer.__new__(asr.Recognizer)
    decoded: list[float] = []

    def run(samples, final, piece=False):
        decoded.append(round(samples.size / SR, 1))
        return "vault with that"

    setattr(r, "_lock", threading.Lock())
    setattr(r, "_run", run)
    return r, decoded


TAIL = np.concatenate([soft_words(0.6), quiet(3.0)])


def test_the_tail_really_is_silent_by_the_old_whole_buffer_gate():
    assert asr.is_silent(TAIL)        # the regression's precondition, measured not assumed


def test_ac1_a_short_ending_plus_closing_silence_is_decoded_as_a_piece():
    r, decoded = recognizer()
    assert r.transcribe(TAIL, piece=True) == "vault with that"
    assert decoded == [3.6]


def test_ac2_a_whole_utterance_keeps_the_old_gate():
    r, decoded = recognizer()
    assert r.transcribe(TAIL) == ""
    assert decoded == []


def test_ac3_pure_silence_as_a_piece_is_never_decoded():
    r, decoded = recognizer()
    assert r.transcribe(quiet(3.0), piece=True) == ""
    assert decoded == []


def test_a_single_click_is_not_speech():
    click = quiet(1.0)
    click[100:110] = 0.9                          # one loud spike: clears the floor in ONE window
    assert not asr.has_speech(click)              # a word spans at least two (PIECE_SPEECH_MIN_WINDOWS)


def test_ac4_the_server_path_decodes_the_tail(state):  # noqa: F811
    real, _ = recognizer()
    calls: list[bool] = []

    def transcribe(samples, piece=False):
        calls.append(piece)
        return real.transcribe(samples, piece=piece) if piece else "first."

    state.recognizer.transcribe = transcribe
    out = speak(state, tone(4.0), quiet(0.7), soft_words(0.6), quiet(1.6))
    assert calls.count(True) >= 2                 # the piece at the pause, then the tail
    assert out and out[-1].endswith("vault with that")
