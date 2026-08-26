"""Spec 020 — `say --timings` reports WHEN each word is spoken, from the pass that made the audio.

**Why these are unit tests over a pure function rather than assertions about real speech.** The
conversion from per-token durations to word offsets is arithmetic with four places to be wrong —
the rounding, the pad, the sentence gaps and the trim — and every one of them produces plausible
audio and a plausible number. The cross-check against a second model (AC-7) is what proves the
result describes the real timeline; these prove the arithmetic, which is what would silently drift.

🔴 **The two findings that shaped this file, both measured 2026-08-26:**

1. **Cumulating the RAW FLOATS instead of the rounded values drifts 567 ms over a 7.5 s sentence.**
   The export emits pre-round float durations; the reference implementation rounds each. Both
   readings produce a monotonic schedule that looks correct at the start of a sentence.
2. **`kokoro-onnx` 0.5.0 sends `speed` as `np.int32` on the `input_ids` branch**, which the
   timestamped graph rejects outright. `test_a_fractional_speed_reaches_the_model_as_a_float` is
   the regression guard, and it is the reason the model call is issued here rather than by the
   library.
"""
import numpy as np
import pytest

from voice_tunnel import config, tts

SPACE = 16          # the space token in kokoro's vocab; word boundaries are a split on it
UNIT = tts.KOKORO_UNIT_SAMPLES


def ids_for(groups: list[int]) -> list[int]:
    """`[pad, ...group, space, ...group, pad]` — the shape the export is always handed."""
    out: list[int] = [0]
    for n, size in enumerate(groups):
        if n:
            out.append(SPACE)
        out.extend(range(100, 100 + size))
    return out + [0]


# ============================================================ AC-1: one entry per word, in order


def test_each_group_between_spaces_becomes_one_span():
    """AC-1/FR1. Three words of two tokens each, every duration one unit."""
    ids = ids_for([2, 2, 2])
    spans = tts.word_offsets(ids, [1.0] * len(ids), SPACE)

    assert len(spans) == 3, "one span per group, and the spaces are not groups"
    assert [s for s, _ in spans] == [1 * UNIT, 4 * UNIT, 7 * UNIT], (
        "each group starts after the pad, its predecessors, and the spaces between them"
    )
    assert all(end > start for start, end in spans), "a span must have width"


def test_the_spans_are_strictly_ordered():
    ids = ids_for([1, 3, 2, 1])
    spans = tts.word_offsets(ids, [2.0] * len(ids), SPACE)
    starts = [s for s, _ in spans]

    assert starts == sorted(starts), "speech does not go backwards"


# ============================================== AC-5: the leading pad is silence, not a word


def test_the_leading_pad_is_excluded_and_pushes_word_one_later():
    """AC-5/TC3. The pad token measured 11.946 units ≈ 299 ms against a word-one start of 0.300 s,
    so it IS the leading silence — reporting it as a word would put a highlight on nothing."""
    ids = ids_for([2, 2])
    durations = [12.0] + [1.0] * (len(ids) - 2) + [1.0]
    spans = tts.word_offsets(ids, durations, SPACE)

    assert len(spans) == 2, "the pads at both ends are not words"
    assert spans[0][0] == 12 * UNIT, (
        "word one starts AFTER the pad — if this is 0 the whole schedule is early by the pad"
    )


# ==================================================== the rounding, which is the drift guard


def test_each_duration_is_rounded_before_it_is_summed():
    """🔴 THE DRIFT GUARD. `round(sum)` and `sum(round)` differ by up to a unit per token, and the
    error accumulates in one direction down a long reply."""
    ids = ids_for([1, 1, 1, 1])   # pad w SP w SP w SP w pad -> the last word is token 7
    # Each 0.6 rounds to 1, so group four starts at unit 7. A raw float cumsum would put it at
    # 0.6 * 7 = 4.2 units — earlier, monotonic, and wrong by 70 ms on a four-word phrase.
    spans = tts.word_offsets(ids, [0.6] * len(ids), SPACE)

    assert spans[-1][0] == 7 * UNIT, (
        "cumulate the ROUNDED durations; a raw float cumsum drifts 567 ms over 7.5 s"
    )


def test_a_duration_that_rounds_to_zero_still_costs_one_unit():
    """The `max(1, …)` floor. Without it the law misses by 4,800 samples at speed 2.5 — and it is
    also the mechanism behind the ~2.3x speed ceiling, since a token cannot go below 25 ms."""
    ids = ids_for([1, 1])
    spans = tts.word_offsets(ids, [0.1] * len(ids), SPACE)

    assert spans[0][0] == 1 * UNIT, "the pad rounds to zero but still occupies a unit"
    assert spans[1][0] == 3 * UNIT, "so does every token after it"


# ============================================ AC-1/FR4: labels degrade loudly, never silently


def test_labels_are_the_real_words_when_the_counts_line_up():
    labels, aligned = tts.label_groups("move the camera", ["mˈuːv", "ðə", "kˈamɹəɹ"])

    assert labels == ["move", "the", "camera"]
    assert aligned is True


def test_a_merged_pair_falls_back_to_sound_groups_and_says_so():
    """🔴 MEASURED, and on the issue's own example. *"The camera is on the left."* is six words and
    five groups, because `on the` becomes a single `ɒnðə`. Nothing recoverable connects them — the
    tokenizer returns a bare string — so the labels degrade and `aligned` reports it."""
    labels, aligned = tts.label_groups("the camera is on the left",
                                       ["ðə", "kˈamɹəɹ", "ɪz", "ɒnðə", "lˈɛft"])

    assert aligned is False, "a mismatch must be reported, never papered over"
    assert labels == ["ðə", "kˈamɹəɹ", "ɪz", "ɒnðə", "lˈɛft"], (
        "fall back to what was actually spoken rather than guessing a split"
    )


def test_an_expanded_number_is_also_a_mismatch():
    """The other direction: `54` becomes two groups, so the count goes UP."""
    _labels, aligned = tts.label_groups("we have 54 voices",
                                        ["wiː", "hav", "fˈɪfti", "fˈɔː", "vˈɔɪsɪz"])

    assert aligned is False


# ================================================ AC-4: the dtype guard on the model call


class _RecordingSession:
    """A stand-in for the ONNX session that records the dtypes it was handed."""

    def __init__(self, with_durations=True):
        self.feeds: list[dict] = []
        self._with_durations = with_durations

    def get_inputs(self):
        return [type("I", (), {"name": n})() for n in ("input_ids", "style", "speed")]

    def get_outputs(self):
        names = ["waveform"] + (["durations"] if self._with_durations else [])
        return [type("O", (), {"name": n})() for n in names]

    def run(self, _outputs, feeds):
        self.feeds.append(feeds)
        n = feeds["input_ids"].shape[1]
        out = [np.zeros(n * tts.KOKORO_UNIT_SAMPLES, dtype=np.float32)]
        if self._with_durations:
            out.append(np.ones(n, dtype=np.float32))
        return out


class _FakeTokenizer:
    vocab = {" ": SPACE}

    def phonemize(self, text, lang=None):
        return " ".join("x" * (len(w) or 1) for w in text.split())

    def tokenize(self, phonemes):
        return [SPACE if c == " " else 100 for c in phonemes]


class _FakeKokoro:
    def __init__(self, with_durations=True):
        self.sess = _RecordingSession(with_durations)
        self.tokenizer = _FakeTokenizer()

    def get_voice_style(self, _name):
        return np.zeros((512, 1, 256), dtype=np.float32)

    def create(self, text, voice=None, speed=1.0, lang=None):
        """The library call, used only on the no-durations path — which is the point of having
        it here: a fake without it would let that branch pass by never being taken."""
        return np.zeros(240, dtype=np.float32), config.KOKORO_SR


def test_a_fractional_speed_reaches_the_model_as_a_float(monkeypatch):
    """🔴 AC-4/TC1 — THE REGRESSION GUARD FOR THE BUG THAT WOULD HAVE BROKEN EVERY REPLY.

    `kokoro-onnx` 0.5.0 sends `np.array([speed], dtype=np.int32)` whenever the export names its
    first input `input_ids` — which the timestamped one does. The graph declares `speed` as float,
    so it raises `InvalidArgument` and the tunnel goes silent. Routing the call through the library
    again would turn this red, which is exactly what it is for.
    """
    fake = _FakeKokoro()
    tts._KOKORO._run(fake, "xx xx", "bm_daniel", 1.2)

    speed = fake.sess.feeds[0]["speed"]
    assert speed.dtype == np.float32, "an int32 speed is rejected by the graph outright"
    assert speed[0] == pytest.approx(1.2), (
        "and a cast to int would silently make his 1.2 a 1 if the graph ever accepted it"
    )


def test_the_style_vector_is_looked_up_by_token_count():
    """A (512, 1, 256) pack indexed by the wrong row yields a validly-shaped vector for a
    different-length utterance — audio, no error, wrong voice."""
    fake = _FakeKokoro()
    pack = np.zeros((512, 1, 256), dtype=np.float32)
    for i in range(512):
        pack[i, 0, 0] = i
    fake.get_voice_style = lambda _n: pack

    tts._KOKORO._run(fake, "xx xx", "bm_daniel", 1.0)

    # "xx xx" -> 5 tokens, so the row is 5.
    assert fake.sess.feeds[0]["style"][0, 0] == 5


# ======================================== AC-3/AC-6: the flag changes the REPORT, not the audio


def test_without_the_flag_no_schedule_is_reported(monkeypatch):
    """AC-3/FR3."""
    fake = _FakeKokoro()
    monkeypatch.setattr(tts._KOKORO, "_load", lambda: fake)
    monkeypatch.setattr(tts._KOKORO, "_timed", True)

    _pcm, _rate, schedule = tts._KOKORO.synthesize("Alpha beta.", "bm_daniel", 1.0, 0.0)

    assert schedule is None, "a caller who did not ask must not be handed one"


def test_the_audio_is_identical_whether_or_not_timings_were_asked_for(monkeypatch):
    """AC-3/NFR2. The flag must change the REPORT and nothing else — if the schedule cost a
    different waveform, every measurement taken with it would describe a clip nobody hears."""
    monkeypatch.setattr(tts._KOKORO, "_load", lambda: _FakeKokoro())
    monkeypatch.setattr(tts._KOKORO, "_timed", True)
    plain, _r, _s = tts._KOKORO.synthesize("Alpha beta.", "bm_daniel", 1.0, 0.0)

    monkeypatch.setattr(tts._KOKORO, "_load", lambda: _FakeKokoro())
    timed, _r2, sched = tts._KOKORO.synthesize("Alpha beta.", "bm_daniel", 1.0, 0.0, timings=True)

    assert plain == timed, "asking for timings must not change a single sample"
    assert sched is not None and sched["words"], "and it must still produce the schedule"


def test_a_later_sentence_is_offset_by_the_silence_before_it(monkeypatch):
    """AC-2/FR2. 🔴 THE GAP COUNTS. Sentences are synthesized separately and joined with silence,
    and if that silence is left out of the running offset then every word of every later sentence
    is early by the sum of the gaps before it. The error GROWS down the reply, so the first
    sentence looks perfect and the last is a beat ahead — the shape of drift that gets blamed on
    the model rather than on the arithmetic."""
    monkeypatch.setattr(tts._KOKORO, "_load", lambda: _FakeKokoro())
    monkeypatch.setattr(tts._KOKORO, "_timed", True)

    pause = 0.85
    _pcm, rate, sched = tts._KOKORO.synthesize("Alpha beta. Gamma delta.", "bm_daniel", 1.0,
                                               pause, timings=True)

    firsts = [w["t"] for w in sched["words"]]
    assert len(firsts) == 4, "four words across two sentences"
    # Word three opens the second sentence, so it must sit past the first sentence's audio AND
    # the gap. Without `cursor += n` it would land at the end of the audio alone.
    gap_s = pause * 1.0
    assert firsts[2] >= firsts[1] + gap_s, (
        f"the {gap_s}s sentence gap is missing from the offset: {firsts}"
    )


def test_an_export_without_durations_declines_instead_of_estimating(monkeypatch):
    """AC-6/FR4. `None` is not `{"words": []}` — 'I cannot report' and 'there is nothing to
    report' are different answers, and a caller falls back differently on each."""
    monkeypatch.setattr(tts._KOKORO, "_load", lambda: _FakeKokoro(with_durations=False))
    monkeypatch.setattr(tts._KOKORO, "_timed", False)

    _pcm, _rate, schedule = tts._KOKORO.synthesize("Alpha beta.", "bm_daniel", 1.0, 0.0,
                                                   timings=True)

    assert schedule is None, "an engine with no durations must say so, not return an empty list"


def test_the_bluetooth_lead_in_moves_every_word(monkeypatch):
    """FR2. `pad()` prepends silence so a Bluetooth sink can wake, and that silence is part of the
    clip the caller times against. Missing it puts the whole schedule ~100 ms early — small enough
    to read as tuning, and wrong on every single word."""
    monkeypatch.setattr(tts._KOKORO, "_load", lambda: _FakeKokoro())
    monkeypatch.setattr(tts._KOKORO, "_timed", True)
    monkeypatch.setattr(config, "tts_backend", lambda: "kokoro")

    _pcm, sr, sched = tts.synthesize_timed("Alpha beta.", backend="kokoro", speed=1.0, pause=0.0,
                                           timings=True)

    lead = config.CHIME_LEADING_SILENCE_S
    assert sched["words"][0]["t"] >= lead - 0.001, (
        f"word one cannot start before the {lead}s lead-in that precedes it in the audio"
    )
