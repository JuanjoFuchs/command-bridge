"""The resident piper voice — the fix for TTS being 10x slower than transcription.

Spawning `piper.exe` per reply cost 3.6-4.3 s, nearly all of it interpreter + onnxruntime +
model startup, re-paid on every sentence spoken. Held resident it is 0.17-0.58 s.

These tests are model-free by default: loading a real .onnx takes ~4 s and pytest should not.
They pin the CONTRACT — sentence-silence placement, the fallback, the lock, and that a load
failure is sticky and visible — with a stub voice. The one test that needs the real model is
marked and skipped when it is not installed.
"""
import threading

import pytest

from vm import config, tts


# ----------------------------------------------------------------- a stub voice


class _StubChunk:
    def __init__(self, payload: bytes):
        self.audio_int16_bytes = payload


class _StubVoice:
    """Stands in for piper.PiperVoice: one chunk per '.'-separated sentence, like the real one."""

    def __init__(self, sample_rate: int = 22050):
        self.config = type("cfg", (), {"sample_rate": sample_rate})()
        self.calls = []
        self.concurrent = 0
        self.max_concurrent = 0

    def synthesize(self, text, syn_config=None):
        self.calls.append((text, getattr(syn_config, "length_scale", None)))
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            for sentence in [s for s in text.split(".") if s.strip()]:
                yield _StubChunk(b"\x11\x22" * len(sentence.strip()))
        finally:
            self.concurrent -= 1


@pytest.fixture
def resident(monkeypatch):
    """A fresh resident holder with a stub voice already loaded, isolated from the real one."""
    holder = tts._ResidentVoice()
    voice = _StubVoice()
    holder._voice = voice
    holder._path = "stub.onnx"
    monkeypatch.setattr(tts, "_RESIDENT", holder)
    monkeypatch.setattr(config, "piper_bin", lambda: "")
    monkeypatch.setattr(config, "piper_voice", lambda: "stub.onnx")
    monkeypatch.setattr(config, "tts_backend", lambda: "piper")
    return holder, voice


# --------------------------------------------------------- sentence boundaries


def test_silence_goes_between_sentences_and_never_after_the_last(resident):
    """Matches `piper --sentence-silence` exactly. A trailing gap would add dead air to the end
    of every single reply, which reads as the tunnel having hung."""
    holder, voice = resident
    pause, sr = 0.5, voice.config.sample_rate
    gap = int(sr * pause) * 2

    one, _ = holder.synthesize("Alpha.", "stub.onnx", 0.85, pause)
    two, _ = holder.synthesize("Alpha. Beta.", "stub.onnx", 0.85, pause)

    assert len(two) - len(one) == gap + len("Beta") * 2, "exactly one gap for two sentences"


def test_a_zero_pause_inserts_nothing(resident):
    holder, voice = resident
    assert holder.synthesize("A. B.", "stub.onnx", 0.85, 0.0)[0] == b"\x11\x22" * 2


def test_the_speed_reaches_piper_as_an_inverted_length_scale(resident, monkeypatch):
    """The end-to-end unit check: `speed=2.0` must arrive at piper as length_scale 0.5."""
    holder, voice = resident
    monkeypatch.setattr(config, "piper_inprocess", lambda: True)

    tts._synth_piper("Hello.", voice_path="stub.onnx", speed=2.0, pause=0.0)

    assert voice.calls[-1][1] == pytest.approx(0.5)


def test_synthesis_is_serialized_across_threads(resident):
    """Piper phonemizes through espeak-ng, a C library with global state. The server hands
    synthesis to an executor thread, so two replies really can overlap."""
    holder, voice = resident
    text = "One. Two. Three. Four. Five."

    threads = [threading.Thread(target=holder.synthesize, args=(text, "stub.onnx", 0.85, 0.0))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert voice.max_concurrent == 1, "espeak was called from two threads at once"


# ----------------------------------------------------------------- the fallback


def test_a_load_failure_is_sticky_and_names_itself(monkeypatch):
    """Retrying a broken import on every reply just pays the failure repeatedly — and the reason
    has to survive, because a silent fall back to spawning is a 20x latency regression whose
    only symptom is 'it feels slow again'."""
    holder = tts._ResidentVoice()
    attempts = []

    def explode(_path):
        attempts.append(_path)
        raise RuntimeError("onnx is unhappy")

    monkeypatch.setattr(holder, "_load", lambda p: (explode(p) if not holder.unavailable_reason
                                                    else None))
    with pytest.raises(RuntimeError):
        holder.synthesize("Hi.", "broken.onnx", 0.85, 0.0)
    holder.unavailable_reason = "onnx is unhappy"

    assert holder.synthesize("Hi.", "broken.onnx", 0.85, 0.0) is None
    assert len(attempts) == 1, "a failed load was retried"


def test_a_missing_voice_still_names_the_remedy(monkeypatch):
    monkeypatch.setattr(config, "piper_bin", lambda: "")
    monkeypatch.setattr(config, "piper_voice", lambda: "")

    with pytest.raises(tts.TTSError) as exc:
        tts._synth_piper("hello")

    assert "vm doctor" in str(exc.value)


def test_available_says_which_piper_path_is_live(monkeypatch, resident):
    holder, _ = resident
    monkeypatch.setattr(config, "piper_inprocess", lambda: True)

    assert tts.available() == "piper (resident)"

    holder.unavailable_reason = "the piper python package is not importable"
    assert "spawning per call" in tts.available()


def test_warm_is_a_no_op_for_a_backend_that_does_not_need_it(monkeypatch):
    monkeypatch.setattr(config, "tts_backend", lambda: "sapi")
    assert tts.warm()["warmed"] is False


# ------------------------------------------------------------- the real model


@pytest.mark.skipif(not config.piper_voice(), reason="no piper voice installed")
def test_the_real_voice_loads_and_speaks_faster_the_second_time():
    """The claim, against the actual model: the load is paid once, not per reply."""
    import time

    holder = tts._ResidentVoice()
    voice_path = config.piper_voice()

    t0 = time.perf_counter()
    first = holder.synthesize("The deploy finished.", voice_path, 0.85, 0.5)
    cold = time.perf_counter() - t0

    t0 = time.perf_counter()
    second = holder.synthesize("The deploy finished.", voice_path, 0.85, 0.5)
    warm = time.perf_counter() - t0

    assert first is not None and second is not None
    assert first[1] > 0 and len(first[0]) > 1000
    assert warm < cold, "the model was reloaded on the second call"
    # The subprocess path could not beat ~3.5 s no matter how short the text.
    assert warm < 2.0, f"a warm in-process synthesis took {warm:.2f}s"
