"""Audio plumbing — spec 001 AC-1, AC-10, AC-11. No model, no microphone."""
import numpy as np
import pytest

from voice_tunnel import asr, config, tts

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


# --- leading-silence trim and model serialization -----------------------------

def test_leading_silence_is_trimmed_to_a_short_preroll():
    """Measured 2026-07-29: the first utterance of a session carried ~1.7s of silent runway and
    transcribed worse than the same audio sliced from its speech onset."""
    b = asr.UtteranceBuffer()
    sr = config.TARGET_SR
    b.feed(_silence(int(sr * 2.0)))          # long silent runway before anyone speaks
    b.feed(_speech(int(sr * 1.5)))
    out = None
    for _ in range(30):
        out = b.feed(_silence(int(sr * 0.1)))
        if out is not None:
            break
    assert out is not None
    samples, t_start, _t_end = out
    # Derived from config, not hardcoded: END_OF_UTTERANCE_MS is a tunable, and a test that
    # bakes in its current value fails the moment someone tunes it — which teaches people to
    # edit tests instead of thinking.
    trailing = config.END_OF_UTTERANCE_MS / 1000.0
    expected = b.preroll_samples / sr + 1.5 + trailing
    assert samples.size < int(sr * (expected + 0.6)), "leading silence was not trimmed"
    assert samples.size > int(sr * (1.5 - 0.1)), "trimmed too aggressively — speech was cut"
    assert t_start > 1.5, "t_start should move forward with the trim"


def test_preroll_is_preserved_so_a_soft_first_consonant_survives():
    b = asr.UtteranceBuffer()
    sr = config.TARGET_SR
    b.feed(_silence(int(sr * 1.0)))
    b.feed(_speech(int(sr * 1.0)))
    out = None
    for _ in range(30):
        out = b.feed(_silence(int(sr * 0.1)))
        if out is not None:
            break
    assert out is not None
    # Should keep roughly preroll + speech + trailing, not cut flush to the onset.
    assert out[0].size >= int(sr * 1.0) + b.preroll_samples - int(sr * 0.05)


def test_partials_skip_rather_than_race_the_final_transcription():
    """One model, two callers. Concurrent decodes measurably corrupted output, so finals take
    the lock and partials skip when it is held."""
    r = asr.Recognizer()
    loud = _speech(config.TARGET_SR)
    r._lock.acquire()
    try:
        assert r.try_transcribe(loud) is None, "a partial must skip while the model is busy"
    finally:
        r._lock.release()


def test_try_transcribe_ignores_silence_without_touching_the_model():
    r = asr.Recognizer()
    assert r.try_transcribe(_silence(config.TARGET_SR)) is None
    assert r._model is None


def test_turn_timestamps_are_ordered_and_match_the_returned_audio():
    """Regression: the pre-roll trim added `cut` to a t_start that was already absolute,
    producing t_start > t_end. Inverted ranges slice to nothing, so every downstream analysis
    silently found no audio instead of failing loudly."""
    b = asr.UtteranceBuffer()
    sr = config.TARGET_SR
    b.feed(_silence(int(sr * 3.0)))          # long runway, forces a trim
    b.feed(_speech(int(sr * 1.5)))
    out = None
    for _ in range(30):
        out = b.feed(_silence(int(sr * 0.1)))
        if out is not None:
            break
    assert out is not None
    samples, t_start, t_end = out
    assert t_start < t_end, f"inverted range: {t_start} .. {t_end}"
    # The window must be at least as long as the audio it describes.
    assert (t_end - t_start) >= samples.size / sr - 0.05


# --- adaptive noise floor -----------------------------------------------------

def _noise(n, level, seed=7):
    return (np.random.RandomState(seed).randn(n) * level).astype(np.float32)


def test_a_turn_still_closes_over_room_noise():
    """Regression (Live, 2026-07-31): with the AC running, ambient noise sat above the fixed
    silence floor, trailing silence never accumulated, the utterance never ended, and he had to
    MUTE HIS MICROPHONE to get a turn to close.
    """
    sr = config.TARGET_SR
    b = asr.UtteranceBuffer()
    ac = 0.012                                   # above SILENCE_RMS_FLOOR (0.005)
    assert ac > config.SILENCE_RMS_FLOOR, "the test must reproduce the real condition"

    for _ in range(15):                          # let the tracker learn the room
        b.feed(_noise(int(sr * 0.1), ac))
    b.feed(_speech(int(sr * 1.5)))               # speak over it

    out = None
    for _ in range(40):                          # stop speaking; noise continues
        out = b.feed(_noise(int(sr * 0.1), ac))
        if out is not None:
            break
    assert out is not None, "the turn must close over continuing room noise"


def test_speech_is_still_detected_in_a_noisy_room():
    sr = config.TARGET_SR
    b = asr.UtteranceBuffer()
    for _ in range(15):
        b.feed(_noise(int(sr * 0.1), 0.012))
    b.feed(_speech(int(sr * 1.0)))
    out = None
    for _ in range(40):
        out = b.feed(_noise(int(sr * 0.1), 0.012))
        if out is not None:
            break
    assert out is not None
    assert out[0].size > int(sr * 0.5), "the speech itself must survive, not just the boundary"


def test_a_quiet_moment_does_not_latch_the_floor_low_forever():
    """THE regression for the 63.7-second turn (Live, 2026-07-31).

    A min-tracker snapped DOWN to the quietest window ever seen. After one near-silent moment the
    AC sat ~24x above that latched floor and read as speech indefinitely, so the turn only closed
    when he MUTED — "I finished speaking a while ago on this new turn, and the white noise kept
    running, and you didn't recognize the stop".

    A rolling percentile has no memory of a moment that will not recur.
    """
    sr = config.TARGET_SR
    b = asr.UtteranceBuffer()

    b.feed(_silence(int(sr * 1.0)))              # a near-silent moment, as in a real session
    latched = b._noise_floor

    for _ in range(int(config.NOISE_WINDOW_S * 10) + 5):   # then the AC starts and stays on
        b.feed(_noise(int(sr * 0.1), 0.012))

    assert b._noise_floor > latched * 5, (
        f"floor stayed latched at {latched:.5f} while the room ran at 0.012 "
        f"(estimate {b._noise_floor:.5f})"
    )
    assert not b.speech_active, "continuous room noise must not read as someone speaking"


def test_the_floor_follows_the_room_back_down():
    sr = config.TARGET_SR
    b = asr.UtteranceBuffer()
    for _ in range(int(config.NOISE_WINDOW_S * 10) + 5):
        b.feed(_noise(int(sr * 0.1), 0.02))
    noisy = b._noise_floor
    for _ in range(int(config.NOISE_WINDOW_S * 10) + 5):
        b.feed(_noise(int(sr * 0.1), 0.001))
    assert b._noise_floor < noisy / 3, "the estimate must follow the room when it goes quiet"


# ------------------------------------------------------------------ de-esser (2026-08-15)

def _pcm(samples):
    import numpy as np
    return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _rms(pcm):
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt((x ** 2).mean()))


def test_the_deesser_attenuates_a_sibilant_burst():
    """A high-frequency burst is what an 's' looks like to the detector: nearly all of its energy
    sits above the 4 kHz split. It must come out quieter.

    Reported 2026-08-15 on the move: *"whenever you pronounce an S, it sounds very high and it
    makes someone nearby headache."*
    """
    import numpy as np
    from voice_tunnel import tts

    rate = 22050
    t = np.arange(rate // 2) / rate
    sibilant = _pcm(0.5 * np.sin(2 * np.pi * 7000 * t))

    out = tts._deess(sibilant, rate)

    assert _rms(out) < _rms(sibilant) * 0.85, "sibilance must actually come down"


def test_the_deesser_leaves_a_vowel_alone():
    """THE CONSTRAINT THAT MAKES THIS DIFFERENT FROM A TREBLE CUT. `_articulate` exists because
    the owner could not hear consonant attacks at speed; a filter that dulls everything would
    undo that. Below the detector's threshold the gain is exactly 1.0, so a low tone must come
    back bit-identical."""
    import numpy as np
    from voice_tunnel import tts

    rate = 22050
    t = np.arange(rate // 2) / rate
    vowel = _pcm(0.5 * np.sin(2 * np.pi * 220 * t))

    assert tts._deess(vowel, rate) == vowel


def test_deess_zero_is_a_true_bypass():
    """0 must return the input object untouched, not a re-quantized copy — otherwise 'off' still
    costs a float32 round trip on every clip."""
    import numpy as np
    from voice_tunnel import config, tts

    rate = 22050
    t = np.arange(1000) / rate
    pcm = _pcm(0.5 * np.sin(2 * np.pi * 7000 * t))

    original = config.DEESS
    try:
        config.DEESS = 0.0
        assert tts._deess(pcm, rate) is pcm
    finally:
        config.DEESS = original


def test_a_midrange_tone_is_not_treated_as_sibilance():
    """The regression the FIRST detector shipped with, pinned so it cannot come back.

    A one-pole difference `x[n] - a*x[n-1]` was used as the high-band detector and its
    coefficient is a LOW-pass pole — it passed a 1 kHz tone at most of full amplitude, so
    ordinary voiced speech scored above the sibilance threshold and got attenuated. The fix was
    a framed FFT, where the split is a bin index and cannot be off by a filter design.
    """
    import numpy as np
    from voice_tunnel import tts

    rate = 22050
    t = np.arange(rate // 2) / rate
    midrange = _pcm(0.5 * np.sin(2 * np.pi * 1000 * t))

    assert tts._deess(midrange, rate) == midrange


def test_the_deesser_cuts_the_band_and_not_the_level():
    """THE BUG THE FIRST WORKING VERSION HAD, and it passed every test above.

    That version detected sibilant frames correctly and multiplied the whole frame by a gain. It
    removed 33% of the high-band energy and the owner heard no difference at all: *"I didn't feel
    a difference. Can you go harder?"* Ducking a frame makes an 's' quieter while leaving its
    spectrum exactly as harsh, and the peak normalizer downstream returns part of the level.

    So the assertion is a RATIO between bands, not a level: the sibilant band must come down
    while the band below it stays where it was. A frame-gain implementation moves both together
    and fails this, however loud the reduction looks in aggregate.
    """
    import numpy as np
    from voice_tunnel import config, tts

    rate = 22050
    t = np.arange(rate // 3) / rate
    # A sibilant with some voicing bleeding into it, which is what an 's' inside a word looks
    # like. The mix is deliberately high-dominant: an EQUAL mix scores 0.506 against the 0.55
    # threshold and is correctly left alone — half vowel is not an 's', and a de-esser that
    # fired on it would be the treble cut this one exists not to be.
    mixed = _pcm(0.15 * np.sin(2 * np.pi * 300 * t) + 0.5 * np.sin(2 * np.pi * 7500 * t))

    def band(pcm, lo, hi):
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        n = (x.size // 512) * 512
        spec = np.abs(np.fft.rfft(x[:n].reshape(-1, 512), axis=1))
        bins = spec.shape[1] - 1
        return float(spec[:, int(lo / (rate / 2) * bins):int(hi / (rate / 2) * bins)].sum())

    original = config.DEESS
    try:
        config.DEESS = 1.0
        out = tts._deess(mixed, rate)
    finally:
        config.DEESS = original

    low_kept = band(out, 100, 2000) / band(mixed, 100, 2000)
    high_cut = band(out, 4500, 11000) / band(mixed, 4500, 11000)

    assert low_kept > 0.9, f"the voice under 2 kHz must survive, kept {low_kept:.1%}"
    assert high_cut < 0.6, f"the sibilant band must actually come down, kept {high_cut:.1%}"
