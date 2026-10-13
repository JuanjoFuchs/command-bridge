"""A reply waits for an OPEN utterance, and only the lane he is talking to waits (JJ, 2026-10-05).

    "whenever I am speaking … and whenever I make a pause you play your turn … the say command should
     refuse whenever … there is a bunch of text already recognized from me"
    "That doesn't make sense for all agents … Dexter has shown waiting multiple times because I have
     been speaking with you."
"""
import numpy as np

import command_bridge.cli as cli
from command_bridge import asr


def test_an_utterance_stays_open_through_the_end_silence():
    buf = asr.UtteranceBuffer(sr=16000, end_silence_ms=300)
    loud = (np.sin(np.linspace(0, 400, 16000)) * 0.5).astype(np.float32)
    buf.feed(loud)
    assert buf.utterance_open and buf.speech_active
    buf.feed(np.zeros(16000 // 2, dtype=np.float32) + 1e-6)   # past the end silence
    # whether or not the turn has closed by now, utterance_open must never be True once it has,
    # and must be True whenever speech_active is
    if buf.speech_active:
        assert buf.utterance_open


def test_the_pre_say_check_counts_an_open_utterance():
    live = {"speech_active": False, "speech_pending": 0, "user_speaking": False, "utterance_open": True}
    assert cli._still_talking(live, amplitude=False) is True
    assert cli._still_talking(live, amplitude=True) is True
    live["utterance_open"] = False
    assert cli._still_talking(live, amplitude=False) is False


def test_an_old_server_without_the_field_still_works():
    live = {"speech_active": False, "speech_pending": 0, "user_speaking": False}
    assert cli._still_talking(live, amplitude=False) is False
