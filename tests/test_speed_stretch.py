"""Spec 039 — real speed past Kokoro's 2.0 ceiling, by synthesizing at a safe native pace and
time-stretching the audio (WSOLA, pitch kept), with the word timings scaled to match.

Measured 2026-10-09 (kokoro, bm_daniel, Parakeet as the judge): at 2.0 the model compresses the first
consonant of a sentence away ("Atlas finished..." heard as "has finished...", "The API" lost "The"),
and 2.5 was silently clamped to 2.0 — identical audio, 2.77 s for both. JJ heard every consonant at
1.25. So the model never runs above `KOKORO_NATIVE_SPEED_MAX`; the rest of the speed is a stretch.

No model here: the stretch is pure numpy, and the backend is driven with stubs.
"""
import numpy as np
import pytest

from command_bridge import config, tts

SR = config.KOKORO_SR


def _tone(seconds=1.0, hz=220.0):
    t = np.arange(int(SR * seconds)) / SR
    return (0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def _peak_hz(x):
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    return float(np.fft.rfftfreq(x.size, 1.0 / SR)[int(np.argmax(spec))])


# ------------------------------------------------------------------------------ the plan

def test_his_speed_is_untouched():
    """1.2 is his live setting: the model gets exactly 1.2 and nothing is stretched."""
    assert config.kokoro_speed_plan(1.2) == (pytest.approx(1.2), 1.0)


def test_above_the_native_ceiling_the_model_stays_safe_and_the_rest_is_stretch():
    for speed in (1.5, 2.0, 2.5, config.SPEED_MAX):
        native, stretch = config.kokoro_speed_plan(speed)
        assert native == pytest.approx(config.KOKORO_NATIVE_SPEED_MAX)
        assert native * stretch == pytest.approx(min(speed, config.SPEED_MAX))
        assert native <= config.KOKORO_SPEED_MAX     # never near kokoro_onnx's assertion
    assert config.KOKORO_NATIVE_SPEED_MAX < config.KOKORO_SPEED_MAX


# ------------------------------------------------------------------------------ the stretch

def test_stretch_shortens_by_the_factor():
    x = _tone(2.0)
    for factor in (1.5, 2.0):
        y = tts.time_stretch(x, factor, SR)
        assert abs(y.size - x.size / factor) < SR * 0.03, (factor, y.size)


def test_stretch_keeps_the_pitch():
    """The point of WSOLA over resampling: faster, NOT higher. A resample by 2 would read 440 Hz."""
    y = tts.time_stretch(_tone(2.0, 220.0), 2.0, SR)
    assert abs(_peak_hz(y) - 220.0) < 6.0


def test_no_stretch_is_the_same_samples():
    x = _tone(0.5)
    assert tts.time_stretch(x, 1.0, SR) is x


def test_stretch_does_not_click():
    """Overlap-add must not leave seams: the sample-to-sample jump stays at a sine's own scale."""
    x = _tone(1.0, 220.0)
    y = tts.time_stretch(x, 1.8, SR)
    inner = y[400:-400]
    assert float(np.max(np.abs(np.diff(inner)))) < 3 * float(np.max(np.abs(np.diff(x))))


# ------------------------------------------------------------------------------ the backend

class _Stub:
    """kokoro_onnx.Kokoro with its own speed assertion and a second of tone per call."""

    def __init__(self):
        self.speeds = []

    def create(self, text, voice=None, speed=1.0, lang=None):
        assert 0.5 <= speed <= 2.0, "Speed should be between 0.5 and 2.0"
        self.speeds.append(speed)
        return _tone(1.0), SR


def test_a_fast_speed_reaches_the_model_at_the_native_ceiling_and_is_shorter(monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(tts._KOKORO, "_load", lambda: stub)
    monkeypatch.setattr(tts._KOKORO, "_timed", False)
    native, _, _ = tts._KOKORO.synthesize("Hello there.", "bm_daniel",
                                          speed=config.KOKORO_NATIVE_SPEED_MAX, pause=0.0)
    fast, _, _ = tts._KOKORO.synthesize("Hello there.", "bm_daniel", speed=2.4, pause=0.0)
    assert stub.speeds == [pytest.approx(config.KOKORO_NATIVE_SPEED_MAX)] * 2
    ratio = len(native) / len(fast)
    assert ratio == pytest.approx(2.4 / config.KOKORO_NATIVE_SPEED_MAX, rel=0.05)
    assert tts._KOKORO.stretched_by == pytest.approx(2.4 / config.KOKORO_NATIVE_SPEED_MAX)


def test_his_speed_never_runs_the_stretch(monkeypatch):
    """NFR: no added latency and no change in sound at the speed he uses today."""
    stub = _Stub()
    monkeypatch.setattr(tts._KOKORO, "_load", lambda: stub)
    monkeypatch.setattr(tts._KOKORO, "_timed", False)

    def boom(*a, **k):
        raise AssertionError("the stretch ran at a native speed")
    monkeypatch.setattr(tts, "time_stretch", boom)
    tts._KOKORO.synthesize("Hello there.", "bm_daniel", speed=1.2, pause=0.0)
    assert stub.speeds == [pytest.approx(1.2)]
    assert tts._KOKORO.stretched_by is None


class _Tok:
    vocab = {" ": 16}

    def phonemize(self, text, lang):
        return "ə bˈiː sˈiː"           # three spoken groups


class _TimedStub:
    tokenizer = _Tok()


def test_word_timings_scale_with_the_stretch(monkeypatch):
    """A stretched clip with unscaled timings points a word late, more so down the reply."""
    durs = np.array([1, 4, 1, 6, 1, 8, 1], dtype=np.float32)   # pad, a, sp, b, sp, c, pad
    ids = [0, 10, 16, 11, 16, 12, 0]

    def fake_run(k, phonemes, voice, speed):
        n = int(durs.sum()) * tts.KOKORO_UNIT_SAMPLES
        return _tone(n / SR), durs, ids

    monkeypatch.setattr(tts._KOKORO, "_load", lambda: _TimedStub())
    monkeypatch.setattr(tts._KOKORO, "_timed", True)
    monkeypatch.setattr(tts._KOKORO, "_run", fake_run)
    monkeypatch.setattr("kokoro_onnx.trim.trim", lambda s: (s, (0, len(s))), raising=False)
    _, _, slow = tts._KOKORO.synthesize("a b c", "bm_daniel", speed=config.KOKORO_NATIVE_SPEED_MAX,
                                        pause=0.0, timings=True)
    _, _, fast = tts._KOKORO.synthesize("a b c", "bm_daniel", speed=2.4, pause=0.0, timings=True)
    factor = 2.4 / config.KOKORO_NATIVE_SPEED_MAX
    assert slow is not None and fast is not None
    assert [w["w"] for w in fast["words"]] == ["a", "b", "c"]
    for s, f in zip(slow["words"], fast["words"]):
        assert f["t"] == pytest.approx(s["t"] / factor, abs=0.002)
