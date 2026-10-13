"""Spec 039 follow-up — the gap between sentences shrinks with the speed.

JJ, 2026-10-09, at speed 1.6: the gaps "should shrink proportionally to the speed you are applying."
So the stored pause is the gap at NATIVE pace (1.0x), and a clip at speed s uses pause / s, never
below PAUSE_FLOOR_S (sentences must not run together) unless the stored pause is itself smaller.
"""
import pytest

from command_bridge import config, tts


def test_the_gap_divides_by_the_speed():
    assert config.effective_pause(0.96, 1.6) == pytest.approx(0.6)     # his feel at 1.6
    assert config.effective_pause(0.96, 1.2) == pytest.approx(0.8)
    assert config.effective_pause(0.96, 1.0) == pytest.approx(0.96)


def test_the_gap_has_a_floor():
    assert config.effective_pause(0.4, 2.5) == pytest.approx(config.PAUSE_FLOOR_S)
    assert config.PAUSE_FLOOR_S == pytest.approx(0.2)


def test_a_pause_set_below_the_floor_is_his_choice():
    assert config.effective_pause(0.0, 2.0) == 0.0
    assert config.effective_pause(0.1, 1.0) == pytest.approx(0.1)


class _Stub:
    def create(self, text, voice=None, speed=1.0, lang=None):
        return [0.1] * 2400, config.KOKORO_SR


def _gap_samples(monkeypatch, speed, pause):
    """Zero samples a two-sentence clip gains over one sentence: the inserted gap."""
    monkeypatch.setattr(tts._KOKORO, "_load", lambda: _Stub())
    monkeypatch.setattr(tts._KOKORO, "_timed", False)
    monkeypatch.setattr(config, "tts_backend", lambda: "kokoro")
    monkeypatch.setattr(config, "deess", lambda: 0.0)
    monkeypatch.setattr(config, "consonant_boost", lambda: 0.0)
    one, _ = tts.synthesize("Alpha beta.", speed=speed, pause=pause)
    two, _ = tts.synthesize("Alpha. Beta.", speed=speed, pause=pause)
    # Two pieces = one piece's 2400 samples more than one piece, plus the gap between them.
    return (len(two) - len(one)) // 2 - 2400


def test_a_clip_at_speed_uses_the_shrunk_gap(monkeypatch):
    """Through the real synthesis path, at a native speed (no stretch to confound the count)."""
    rate = config.KOKORO_SR
    slow = _gap_samples(monkeypatch, 1.0, 0.96)
    fast = _gap_samples(monkeypatch, 1.2, 0.96)
    assert slow == int(rate * 0.96)
    assert fast == int(rate * config.effective_pause(0.96, 1.2))


def test_the_stream_uses_the_same_gap(monkeypatch):
    monkeypatch.setattr(tts._KOKORO, "_load", lambda: _Stub())
    monkeypatch.setattr(tts._KOKORO, "_timed", False)
    monkeypatch.setattr(config, "tts_backend", lambda: "kokoro")
    monkeypatch.setattr(config, "deess", lambda: 0.0)
    pieces = list(tts.synthesize_stream("Alpha. Beta.", speed=1.2, pause=0.96))
    first = pieces[0][0]
    # The first piece carries the lead pad, its audio, then the gap before the next sentence.
    lead = len(tts.quiet(int(config.KOKORO_SR * config.CHIME_LEADING_SILENCE_S)))
    tail_zeros = (len(first) - lead) // 2 - 2400
    assert tail_zeros == int(config.KOKORO_SR * config.effective_pause(0.96, 1.2))
