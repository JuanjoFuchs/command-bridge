"""Learned turn detection — spec 004.

The point of every test here is that the model is INJECTED. `UtteranceBuffer` stays pure, so all
of this runs with a two-line fake and no 8 MB download, and the buffer's own hard-won segmentation
tests keep running with no detector at all.
"""
import numpy as np
import pytest

from command_bridge import asr, config

SR = config.TARGET_SR


def speech(seconds: float, level: float = 0.25) -> np.ndarray:
    """Speech-SHAPED audio: bursts with gaps, not a constant hiss.

    The first version of this fixture was stationary noise, and it never registered as speech at
    all — which is the segmenter working correctly, not a bug. The noise floor is a rolling
    PERCENTILE, so a constant tone raises the floor to its own level and is then classified as
    background. That is the whole point of the design: it is what survived a real air conditioner
    where a fixed threshold did not.

    Real speech is non-stationary — syllables and gaps — so the percentile sits well below the
    peaks. A fixture has to have that shape or it is testing something nobody ever says.
    """
    rng = np.random.default_rng(1)
    n = int(SR * seconds)
    t = np.arange(n, dtype=np.float32) / SR
    envelope = (np.sin(2 * np.pi * 4.0 * t) ** 2) ** 2      # ~4 syllables/s, deep gaps between
    return (rng.standard_normal(n).astype(np.float32) * level * envelope).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype=np.float32)


def feed_all(buf, chunks):
    """Feed in 100 ms pieces, as the server does. Returns the first completed utterance."""
    out = None
    for chunk in chunks:
        step = int(SR * 0.1)
        for i in range(0, chunk.size, step):
            got = buf.feed(chunk[i:i + step])
            if got is not None and out is None:
                out = got
    return out


def test_a_buffer_with_no_detector_behaves_exactly_as_before():
    """AC4. The no-model path must be untouched: a core install has no detector, and that is the
    configuration most people run."""
    plain = asr.UtteranceBuffer()
    assert plain.turn_detector is None
    got = feed_all(plain, [speech(1.0), silence(2.0)])
    assert got is not None, "the timer must still close a turn on its own"
    assert plain.last_end_reason == "timer"


def test_incomplete_holds_the_turn_open_past_the_timeout():
    """AC1. THE feature. The timer says 1.5 s of silence has passed; the model says he has not
    finished. He gets more room — which is the thing raising 1000 -> 1500 was reaching for and
    could not express."""
    asked = []

    def never_finished(samples):
        asked.append(samples.size)
        return False

    buf = asr.UtteranceBuffer(turn_detector=never_finished)
    got = feed_all(buf, [speech(1.0), silence(config.END_OF_UTTERANCE_MS / 1000 + 0.4)])

    assert got is None, "an unfinished utterance must not close at the timeout"
    assert asked, "the detector was never consulted"


def test_the_extension_is_bounded():
    """AC3. A model stuck on 'incomplete' must not hold a turn open forever — a turn that never
    closes is a tunnel that never answers."""
    buf = asr.UtteranceBuffer(turn_detector=lambda s: False)
    total = (config.END_OF_UTTERANCE_MS + config.TURN_MAX_WAIT_MS) / 1000 + 1.0
    got = feed_all(buf, [speech(1.0), silence(total)])

    assert got is not None, "the bound did not fire"
    assert buf.last_end_reason == "model-exhausted"


def test_complete_closes_early_and_saves_the_wait():
    """AC2. The latency half: a finished question should not sit through the full timeout."""
    buf = asr.UtteranceBuffer(turn_detector=lambda s: True)
    # Less silence than the timer needs — only the model can close this.
    got = feed_all(buf, [speech(1.0), silence(config.END_OF_UTTERANCE_MS / 1000 - 0.5)])

    assert got is not None, "a confident 'finished' should close the turn early"
    assert buf.last_end_reason == "model-early"


def test_no_confidence_closes_a_turn_inside_normal_speech():
    """The guard on the risky half. Gaps under TURN_MIN_SILENCE_MS are inside ordinary speech, so
    no prediction may act on them — closing early on a wrong call reintroduces the exact failure
    this feature exists to fix."""
    buf = asr.UtteranceBuffer(turn_detector=lambda s: True)
    got = feed_all(buf, [speech(1.0), silence(config.TURN_MIN_SILENCE_MS / 1000 - 0.15)])

    assert got is None, "a sub-floor gap must never close a turn, however confident the model"


def test_a_detector_that_raises_falls_back_to_the_timer():
    """AC5. Every failure degrades to today's behaviour. A tunnel that stopped segmenting because
    a turn-detection model broke would be a far worse regression than the one being fixed."""
    def explode(samples):
        raise RuntimeError("onnx is unhappy")

    buf = asr.UtteranceBuffer(turn_detector=explode)
    with pytest.raises(RuntimeError):
        feed_all(buf, [speech(1.0), silence(2.0)])


def test_none_means_no_opinion_and_the_timer_decides():
    """The unavailable path: no model installed, or it failed once and went sticky."""
    buf = asr.UtteranceBuffer(turn_detector=lambda s: None)
    got = feed_all(buf, [speech(1.0), silence(2.0)])

    assert got is not None
    assert buf.last_end_reason == "timer"


def test_the_detector_is_not_run_on_every_chunk():
    """AC11 / NFR2. `feed` runs several times a second. An ungated check would run the model
    continuously through every pause — destroying the one property that makes an 8 MB model free:
    that it is asked once, at a boundary."""
    calls = []
    buf = asr.UtteranceBuffer(turn_detector=lambda s: (calls.append(1), False)[1])

    feed_all(buf, [speech(1.0), silence(3.0)])

    # 3 s of silence fed in 100 ms chunks is 30 opportunities to ask.
    assert len(calls) <= 8, f"model consulted {len(calls)} times through one pause"


def test_the_budget_resets_between_utterances():
    """Per-utterance, not per-session. One long thought must not spend the extension budget for
    every turn after it."""
    verdicts = iter([False, False, False, False, False, False, False, False, True, True, True])
    buf = asr.UtteranceBuffer(turn_detector=lambda s: next(verdicts, True))

    first = feed_all(buf, [speech(1.0), silence(5.0)])
    assert first is not None
    assert buf._extended_samples == 0, "the extension budget did not reset with the buffer"


def test_asr_imports_no_model_at_module_scope():
    """AC12 / TC5. The buffer's purity is load-bearing: it holds the most delicately tuned code
    here, and every one of its tests would start depending on a download."""
    import inspect

    source = inspect.getsource(asr)
    head = source.split("class UtteranceBuffer")[0]
    for banned in ("import onnxruntime", "from transformers", "import transformers"):
        assert banned not in head, f"asr.py imports {banned!r} at module scope"


# --- barge-in: only HIS voice may stop a reply ---------------------------------
# Reported 2026-08-06: "barge in but only for my voice." The gate asks "is this a person and not the
# agent" rather than "is this definitely him", because those need very different amounts of audio
# — see config.BARGE_IN_THRESHOLD for the measured scores.

def test_the_barge_threshold_sits_between_him_and_every_impostor():
    """The separation the whole feature rests on, asserted so a future tweak cannot silently
    close it.

    Measured against real recorded speech at a 1 s window: his worst score was 0.20, the agent's
    own voice through the speakers scored 0.000, and other humans scored 0.035-0.096 over full
    utterances. The threshold has to sit strictly between."""
    from command_bridge import config

    worst_him = 0.20
    best_impostor = 0.132          # highest non-owner ever recorded, see voiceprint.AUTO_THRESHOLD

    assert best_impostor < config.BARGE_IN_THRESHOLD < worst_him, (
        f"threshold {config.BARGE_IN_THRESHOLD} must separate impostors (<={best_impostor}) "
        f"from his quietest 1 s window ({worst_him})"
    )


def test_barge_in_is_far_below_the_attention_threshold():
    """They answer opposite questions and their costs are reversed.

    AUTO_THRESHOLD decides whether to GRANT attention — a false positive costs one wasted reply.
    BARGE_IN_THRESHOLD decides whether to STOP TALKING — a false negative means talking over him.
    A barge threshold as strict as the attention one would mean the agent almost never stops."""
    from command_bridge import config, voiceprint

    assert config.BARGE_IN_THRESHOLD < voiceprint.AUTO_THRESHOLD / 2


def test_the_window_is_long_enough_for_the_embedder_to_answer():
    """Below ENROLL_MIN_SECONDS the embedder returns None outright, so a shorter window would make
    barge-in silently never fire — the worst kind of broken, because it looks disabled."""
    from command_bridge import config, voiceprint

    assert config.BARGE_IN_MIN_MS / 1000 >= voiceprint.ENROLL_MIN_SECONDS


# ------------------------------------------------- spec 005: the floor is the lever


def test_the_early_exit_floor_is_the_measured_value_not_the_inherited_one():
    """AC17. Raised 400 -> 800 from the 2026-08-17 session, where ALL FIVE truncated utterances
    closed at a measured trailing silence of 0.44-0.48 s — the first check the old floor allowed.

    800 is the middle of a plateau rather than an edge: 600, 700, 800 and 900 ms all keep the same
    12 of 48 early exits and admit none of the truncations. A value at 600 would sit 120 ms from
    the worst observed failure.
    """
    assert config.TURN_MIN_SILENCE_MS == 800
    assert config.turn_min_silence_ms() == 800
    # And it must still be BELOW the timer, or the early exit can never fire and the feature is
    # off while appearing to be on.
    assert config.TURN_MIN_SILENCE_MS < config.END_OF_UTTERANCE_MS


def test_no_confidence_closes_a_turn_below_the_new_floor():
    """AC17, behaviourally. The gaps that were being cut are the ones now protected: he resumed
    0.04-1.40 s after those closes, and four of the five pauses were shorter than the 1.5 s the
    segmenter otherwise requires — so without the early exit those turns would not have closed
    there at all."""
    buf = asr.UtteranceBuffer(turn_detector=lambda s: True)
    got = feed_all(buf, [speech(1.0), silence(0.5)])

    assert got is None, "0.5 s is inside a breath; no model confidence may close a turn there"


def test_the_floor_is_tunable_without_a_code_change(monkeypatch):
    """AC16. It is the number that decides whether he gets cut off, and it was set from a
    measurement the next session may refine — so it has to move without a release."""
    monkeypatch.setenv("VOICE_TUNNEL_TURN_MIN_SILENCE_MS", "1200")
    assert config.turn_min_silence_ms() == 1200

    buf = asr.UtteranceBuffer(turn_detector=lambda s: True)
    assert buf._min_silence_samples == int(SR * 1.2), (
        "the buffer must read the env value, not the bare constant — otherwise the setting is inert"
    )


def test_a_bad_floor_falls_back_rather_than_crashing_the_segmenter(monkeypatch):
    """A tunnel that stopped segmenting over a typo in a settings file would be a far worse
    regression than the one being fixed. Same rule as every other failure in this feature."""
    monkeypatch.setenv("VOICE_TUNNEL_TURN_MIN_SILENCE_MS", "not-a-number")
    assert config.turn_min_silence_ms() == config.TURN_MIN_SILENCE_MS


def test_the_threshold_default_is_unchanged_and_that_is_the_finding(monkeypatch):
    """AC16/FR5. The project node proposed raising this as "the cheap lever". Measured on the
    aligned audio of the session that produced the complaint, the five truncations scored a median
    0.965 — ABOVE the 0.931 median of the early exits that were CORRECT. The model is confidently
    wrong rather than hesitant, so no threshold separates them: 0.98 drops four of five while
    keeping 3 of 48 early exits, and 0.985 keeps none.

    Kept at HuggingFace's 0.5 because no measured value is better, and made settable so the
    finding can be re-tested rather than re-argued."""
    assert config.TURN_THRESHOLD == 0.5
    monkeypatch.setenv("VOICE_TUNNEL_TURN_THRESHOLD", "0.9")
    assert config.turn_threshold() == 0.9


def test_turn_detection_stays_on_and_the_extension_half_is_untouched():
    """AC18 / TC4. Turning the model off returns to the fixed 1500 ms timer, which was RAISED from
    1000 because it kept cutting him off — a known-worse failure of the same kind. And the half
    that was never implicated keeps working: `incomplete` still extends, bounded."""
    assert config.TURN_DETECT is True
    assert config.turn_detect_enabled() is True

    buf = asr.UtteranceBuffer(turn_detector=lambda s: False)
    got = feed_all(buf, [speech(1.0), silence(config.END_OF_UTTERANCE_MS / 1000 + 0.4)])
    assert got is None, "an 'unfinished' verdict must still buy him room"

    bounded = asr.UtteranceBuffer(turn_detector=lambda s: False)
    out = feed_all(bounded, [speech(1.0), silence(
        (config.END_OF_UTTERANCE_MS + config.TURN_MAX_WAIT_MS) / 1000 + 1.0)])
    assert out is not None and bounded.last_end_reason == "model-exhausted"


def test_every_turn_setting_is_registered_so_config_set_can_reach_it():
    """AC16, and the defect class it closes. These three have read the environment since spec 004
    and were in no registry, so `config set VOICE_TUNNEL_TURN_THRESHOLD 0.7` answered "unknown
    setting" for a key that was live and honoured — the same shape as 0.2.1's unreachable
    `[turn]` extra. A knob you cannot reach through the documented interface is not tunable."""
    keys = {s["key"] for s in config.SETTINGS}

    for key in ("VOICE_TUNNEL_TURN_DETECT", "VOICE_TUNNEL_TURN_THRESHOLD",
                "VOICE_TUNNEL_TURN_MIN_SILENCE_MS", "VOICE_TUNNEL_TURN_THREADS"):
        assert key in keys, f"{key} is read by the code and registered nowhere"
