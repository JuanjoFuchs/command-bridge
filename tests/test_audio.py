"""Audio plumbing — spec 001 AC-1, AC-10, AC-11. No model, no microphone."""
import numpy as np
import pytest

from vm import asr, config, tts


# --------------------------------------------------------------- PCM handling


def test_pcm16_round_trips_to_float32():
    """AC-11 — the browser sends little-endian int16; the ASR wants float32 in [-1,1]."""
    original = np.array([0, 16384, -16384, 32767, -32768], dtype="<i2")
    out = asr.pcm16_to_float32(original.tobytes())
    assert out.dtype == np.float32
    assert np.isclose(out[0], 0.0)
    assert np.isclose(out[1], 0.5, atol=1e-4)
    assert np.isclose(out[2], -0.5, atol=1e-4)
    assert -1.0 <= out.min() and out.max() <= 1.0


def test_empty_pcm_is_an_empty_array_not_a_crash():
    assert asr.pcm16_to_float32(b"").size == 0


def test_resample_changes_length_proportionally():
    """AC-11 — 48 kHz from the browser down to the 16 kHz the model wants."""
    src = np.sin(np.linspace(0, 40 * np.pi, 4800)).astype(np.float32)
    out = asr.resample_linear(src, 48000, 16000)
    assert abs(out.size - 1600) <= 1
    assert out.dtype == np.float32


def test_resample_is_a_noop_at_the_same_rate():
    src = np.ones(100, dtype=np.float32)
    assert asr.resample_linear(src, 16000, 16000).size == 100


# -------------------------------------------------------------- silence gating


def test_silence_is_detected():
    """AC-1 — this gate is what makes 'a silent room emits zero turns' a guarantee.
    Whisper hallucinates confident text on silence, so it must never see it."""
    assert asr.is_silent(np.zeros(16000, dtype=np.float32))


def test_speech_level_audio_is_not_silent():
    loud = (np.random.RandomState(0).randn(16000) * 0.2).astype(np.float32)
    assert not asr.is_silent(loud)


def test_recognizer_returns_empty_for_silence_without_loading_a_model():
    """The energy gate must short-circuit BEFORE the model is touched — proven here by the
    fact that this passes with no model downloaded."""
    r = asr.Recognizer()
    assert r.transcribe(np.zeros(16000, dtype=np.float32)) == ""
    assert r._model is None


@pytest.mark.parametrize("junk", ["you", "Thank you.", "Bye.", " so ", "[BLANK_AUDIO]"])
def test_known_hallucination_artifacts_are_filtered(junk):
    assert asr.is_hallucination(junk)


def test_real_speech_is_not_filtered():
    assert not asr.is_hallucination("so what should I do about the deploy")
    assert not asr.is_hallucination("thank you for checking the logs")


# ------------------------------------------------------- utterance segmentation


def _speech(n, level=0.2, seed=0):
    return (np.random.RandomState(seed).randn(n) * level).astype(np.float32)


def _silence(n):
    return np.zeros(n, dtype=np.float32)


def test_silence_alone_never_completes_an_utterance():
    """AC-1 at the segmentation layer."""
    b = asr.UtteranceBuffer()
    for _ in range(20):
        assert b.feed(_silence(1600)) is None


def test_speech_then_silence_completes_one_utterance():
    b = asr.UtteranceBuffer()
    sr = config.TARGET_SR
    assert b.feed(_speech(sr)) is None                    # 1s of speech, still going
    out = None
    for _ in range(int(config.END_OF_UTTERANCE_MS / 1000 * 10) + 3):
        out = b.feed(_silence(int(sr * 0.1)))
        if out is not None:
            break
    assert out is not None, "end-of-utterance silence should close the turn"
    samples, t_start, t_end = out
    assert samples.size > 0
    assert t_end > t_start


def test_a_click_shorter_than_the_minimum_is_discarded():
    """Coughs and door slams clear the energy gate but are not speech."""
    b = asr.UtteranceBuffer()
    sr = config.TARGET_SR
    b.feed(_speech(int(sr * 0.05)))                       # 50 ms, under MIN_UTTERANCE_MS
    out = None
    for _ in range(30):
        out = b.feed(_silence(int(sr * 0.1)))
        if out is not None:
            break
    assert out is None


def test_flush_emits_a_pending_utterance():
    b = asr.UtteranceBuffer()
    b.feed(_speech(config.TARGET_SR))
    out = b.flush()
    assert out is not None, "a disconnect must not silently eat the last thing said"


def test_flush_on_silence_emits_nothing():
    b = asr.UtteranceBuffer()
    b.feed(_silence(config.TARGET_SR))
    assert b.flush() is None


# -------------------------------------------------------------- TTS behaviour


def test_synthesized_audio_is_padded_for_bluetooth():
    """AC-10 — Bluetooth sinks sleep between clips and clip the first ~100ms. Without this
    the first syllable of every reply is eaten and it presents as 'the TTS is broken'."""
    pcm, rate = tts.synthesize("testing one two three", backend="none")
    lead = int(rate * config.CHIME_LEADING_SILENCE_S) * 2
    trail = int(rate * config.CHIME_TRAILING_SILENCE_S) * 2
    assert pcm[:lead] == b"\x00" * lead
    assert pcm[-trail:] == b"\x00" * trail


def test_pad_adds_the_configured_amounts():
    body = b"\x11\x22" * 100
    out = tts.pad(body, 1000)
    assert len(out) == len(body) + 2 * (int(1000 * 0.1) + int(1000 * 0.2))


def test_empty_text_is_rejected():
    with pytest.raises(tts.TTSError):
        tts.synthesize("   ", backend="none")


def test_unknown_backend_is_rejected():
    with pytest.raises(tts.TTSError):
        tts.synthesize("hello", backend="nonsense")
