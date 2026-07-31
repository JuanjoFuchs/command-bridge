"""vm.asr — utterance-buffered speech recognition.

**Why this is simpler than meeting-copilot's streaming ASR.** `mc` transcribes a meeting, where
you cannot wait for a speaker to stop — it needs incremental commits (LocalAgreement-2) to react
mid-monologue. Here the turn boundary *is* the end of the utterance: nobody wants a reply before
they have finished their sentence. So we buffer until end-of-utterance and transcribe once. That
is more accurate (Whisper sees whole sentences), far less code, and has no duplicate-commit class
of bug at all. At RTF ~0.1 a 3-second utterance transcribes in ~0.3 s.

Silence discipline, inherited from `mc` because Whisper confidently hallucinates on silence
("you", "Thank you."):
  1. an RMS energy gate means a near-silent buffer is never sent to the model (this is what
     makes AC-1 a guarantee rather than a hope),
  2. faster-whisper's built-in Silero VAD drops non-speech inside otherwise-active audio, and
  3. a known-artifact phrase filter drops whole-utterance junk.

The model loads lazily and stays resident. Everything above the model boundary is pure and
unit-testable without faster-whisper installed.
"""
from __future__ import annotations

import threading
from typing import List, Optional, Tuple

import numpy as np

from . import config

# Whole-utterance outputs that are almost always silence artifacts rather than speech.
# Only applied when the ENTIRE utterance equals one of these — "so" alone is junk, but
# "so what should I do" is a real question.
_HALLUCINATIONS = {
    "you", "you.", "thank you.", "thank you", "thanks for watching.",
    "thanks for watching", "thanks.", "thank you for watching.",
    "thank you for watching", "bye.", "bye", ".", "..", "...",
    "please subscribe.", "so.", "so", "yeah.", "okay.", "ok.", "[blank_audio]",
    "silence", "(silence)", "[silence]",
}


def rms(samples: np.ndarray) -> float:
    if samples is None or len(samples) == 0:
        return 0.0
    a = np.asarray(samples, dtype=np.float32)
    return float(np.sqrt(np.mean(a * a)))


def is_silent(samples: np.ndarray, floor: float = config.SILENCE_RMS_FLOOR) -> bool:
    """True if the buffer is below the energy floor. The coarse pre-gate that guarantees a
    silent room emits zero turns."""
    return rms(samples) < floor


def is_hallucination(text: str) -> bool:
    return text.strip().lower() in _HALLUCINATIONS


def pcm16_to_float32(raw: bytes) -> np.ndarray:
    """Little-endian int16 bytes (what the browser sends) to float32 in [-1, 1]."""
    if not raw:
        return np.zeros(0, dtype=np.float32)
    ints = np.frombuffer(raw, dtype="<i2")
    return (ints.astype(np.float32) / 32768.0).copy()


def resample_linear(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Linear resample. Adequate for speech at these rates and keeps scipy out of the deps."""
    if src_sr == dst_sr or samples.size == 0:
        return samples.astype(np.float32)
    duration = samples.size / float(src_sr)
    n_out = max(1, int(round(duration * dst_sr)))
    x_old = np.linspace(0.0, duration, num=samples.size, endpoint=False)
    x_new = np.linspace(0.0, duration, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


class UtteranceBuffer:
    """Energy-based segmentation: accumulate audio, and report when an utterance has ended.

    Pure — no model, no I/O — so the turn-boundary logic is unit-testable with synthetic
    arrays. `feed` returns a completed utterance's samples, or None.
    """

    def __init__(
        self,
        sr: int = config.TARGET_SR,
        silence_floor: float = config.SILENCE_RMS_FLOOR,
        end_silence_ms: int = config.END_OF_UTTERANCE_MS,
        min_ms: int = config.MIN_UTTERANCE_MS,
        max_ms: int = config.MAX_UTTERANCE_MS,
        grace_ms: int = config.INITIAL_GRACE_MS,
    ) -> None:
        self.sr = sr
        self.silence_floor = silence_floor
        self.end_silence_samples = int(sr * end_silence_ms / 1000)
        self.min_samples = int(sr * min_ms / 1000)
        self.max_samples = int(sr * max_ms / 1000)
        self.grace_samples = int(sr * grace_ms / 1000)
        self._buf = np.zeros(0, dtype=np.float32)
        self._trailing_silence = 0
        self._seen_speech = False
        self._total_fed = 0
        self._utterance_start_sample = 0
        self._buf_start_total = 0
        self._noise_floor = silence_floor
        """Running estimate of the room's noise level, tracked rather than assumed."""
        self.preroll_samples = int(sr * 0.2)
        """Audio kept before detected speech onset. Enough to protect a soft first consonant,
        which the energy gate can miss, without handing the model a long silent runway."""

    @property
    def seconds_buffered(self) -> float:
        return self._buf.size / float(self.sr)

    def snapshot(self) -> Optional[np.ndarray]:
        """A copy of the audio buffered so far, or None if there isn't speech in it yet.

        For live partial transcription: the in-flight utterance can be transcribed repeatedly
        while it is still being spoken, without disturbing the buffer that will become the
        final turn.
        """
        if not self._seen_speech or self._buf.size == 0:
            return None
        return self._buf.copy()

    @property
    def speech_active(self) -> bool:
        """True when someone is mid-utterance — speech has started and the end-of-utterance
        silence has not yet elapsed.

        Used to hold synthesized audio back rather than talking over the speaker, which is the
        difference between a conversation and a dictation machine that shouts.
        """
        return self._seen_speech and self._trailing_silence < self.end_silence_samples

    def feed(self, samples: np.ndarray) -> Optional[Tuple[np.ndarray, float, float]]:
        """Add audio. Returns `(samples, t_start, t_end)` when an utterance completes."""
        if samples is None or samples.size == 0:
            return None
        samples = np.asarray(samples, dtype=np.float32)

        # Track speech vs silence on a short window so a pause inside a sentence doesn't
        # end the turn, but a real gap does.
        window = int(self.sr * 0.02) or 1
        for i in range(0, samples.size, window):
            chunk = samples[i : i + window]
            level = rms(chunk)
            # Track the room. A fixed threshold assumes a quiet room; with an AC running, the
            # ambient noise sits above it, trailing silence never accumulates, and the turn
            # never closes — the user has to mute their microphone to be heard. See
            # config.NOISE_MARGIN.
            if level < self._noise_floor:
                self._noise_floor = level                       # snap down instantly
            else:
                self._noise_floor *= config.NOISE_FLOOR_DECAY   # creep up slowly
            threshold = max(self.silence_floor, self._noise_floor * config.NOISE_MARGIN)
            if level >= threshold:
                if not self._seen_speech:
                    # Anchor the utterance to where speech actually began.
                    self._utterance_start_sample = self._total_fed + i
                self._seen_speech = True
                self._trailing_silence = 0
            elif self._seen_speech:
                self._trailing_silence += chunk.size

        self._buf = np.concatenate([self._buf, samples])
        self._total_fed += samples.size

        if self._total_fed < self.grace_samples and not self._seen_speech:
            return None

        ended = self._seen_speech and self._trailing_silence >= self.end_silence_samples
        too_long = self._buf.size >= self.max_samples

        if not (ended or too_long):
            return None

        out = self._buf
        t_start = self._utterance_start_sample / float(self.sr)
        t_end = self._total_fed / float(self.sr)
        # Read the trailing-silence count BEFORE resetting — _reset_buffer() zeroes it, and
        # measuring the whole buffer instead of just the speech lets a 50 ms click through
        # as a turn (regression: test_a_click_shorter_than_the_minimum_is_discarded).
        trailing = self._trailing_silence if ended else 0
        buf_start = self._buf_start_total          # capture BEFORE reset overwrites it
        onset = self._utterance_start_sample - buf_start
        self._reset_buffer()

        speech_samples = out.size - trailing
        if speech_samples < self.min_samples or is_silent(out, self.silence_floor):
            return None  # too short, or nothing but noise — not a turn

        # Trim the silent runway ahead of speech. Measured 2026-07-29: the first utterance of a
        # session carried ~1.7 s of leading silence and transcribed noticeably worse than the
        # same audio re-transcribed from its speech onset. Later utterances start near speech
        # and were always fine — which is what made the first one look like an ASR problem.
        cut = max(0, min(onset - self.preroll_samples, out.size - self.min_samples))
        if cut > 0:
            out = out[cut:]
            # t_start must be the absolute time of the FIRST SAMPLE IN `out`, which is
            # buffer-start + cut. Adding `cut` to the speech-onset time instead double-counts
            # the offset and produced t_start > t_end — inverted ranges that silently yielded
            # empty slices in every downstream time-based analysis.
            t_start = (buf_start + cut) / float(self.sr)
        return out, t_start, t_end

    def _reset_buffer(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._trailing_silence = 0
        self._seen_speech = False
        self._buf_start_total = self._total_fed

    def flush(self) -> Optional[Tuple[np.ndarray, float, float]]:
        """End of stream: emit whatever is buffered if it looks like speech."""
        if self._buf.size < self.min_samples or is_silent(self._buf, self.silence_floor):
            self._reset_buffer()
            return None
        out = self._buf
        t_start = self._utterance_start_sample / float(self.sr)
        t_end = self._total_fed / float(self.sr)
        self._reset_buffer()
        return out, t_start, t_end


class Recognizer:
    """One utterance in, text out. Two engines behind a single method.

    Engine choice is automatic: Parakeet when its model is on disk (8x faster than whisper
    small.en, see config.parakeet_dir), whisper otherwise so a fresh clone still works. Either
    way the model loads on first use, never at import, so CLI commands that never touch audio
    stay instant.

    The silence gate runs BEFORE either model is touched, which is what makes "a silent room
    emits zero turns" a guarantee rather than a property of whichever engine is loaded.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        device: str = "cpu",
        compute_type: str = "int8",
        engine: Optional[str] = None,
    ) -> None:
        self.engine = (engine or config.asr_engine()).lower()
        self.model_name = model_name or (
            "parakeet-tdt-0.6b-v2" if self.engine == "parakeet" else config.whisper_model()
        )
        self.device = device
        self.compute_type = compute_type
        self._model = None
        # One recognizer, potentially two callers: the live partial preview and the real
        # end-of-utterance transcription, both dispatched to a thread pool. Neither
        # sherpa-onnx nor faster-whisper promises thread safety on a shared model, and
        # concurrent decodes were measurably corrupting output — re-transcribing a turn's own
        # captured audio offline produced BETTER text than the live pass on the same bytes.
        # Serialize. Finals block; partials skip (see try_transcribe).
        self._lock = threading.Lock()

    # -- engines ------------------------------------------------------------
    def _ensure_whisper(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy on purpose

            self._model = WhisperModel(
                config.whisper_model(), device=self.device, compute_type=self.compute_type
            )
        return self._model

    def _ensure_parakeet(self):
        if self._model is None:
            import os

            import sherpa_onnx  # lazy on purpose

            d = config.parakeet_dir()
            if not d:
                raise RuntimeError("parakeet engine selected but no model directory found")
            self._model = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=os.path.join(d, "encoder.int8.onnx"),
                decoder=os.path.join(d, "decoder.int8.onnx"),
                joiner=os.path.join(d, "joiner.int8.onnx"),
                tokens=os.path.join(d, "tokens.txt"),
                num_threads=config.asr_threads(),
                model_type="nemo_transducer",
            )
        return self._model

    def _transcribe_whisper(self, samples: np.ndarray) -> str:
        model = self._ensure_whisper()
        segments, _info = model.transcribe(
            samples,
            language="en",
            beam_size=config.asr_beam_size(),
            temperature=0.0,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments if s.text and s.text.strip())

    def _transcribe_parakeet(self, samples: np.ndarray) -> str:
        model = self._ensure_parakeet()
        stream = model.create_stream()
        stream.accept_waveform(config.TARGET_SR, samples)
        model.decode_stream(stream)
        return stream.result.text or ""

    # -- the interface ------------------------------------------------------
    def _run(self, samples: np.ndarray) -> str:
        if self.engine == "parakeet":
            text = self._transcribe_parakeet(samples)
        else:
            text = self._transcribe_whisper(samples)
        text = " ".join(text.split())
        if not text or is_hallucination(text):
            return ""
        return text

    def transcribe(self, samples: np.ndarray) -> str:
        """Transcribe one complete utterance at 16 kHz. Returns '' for silence/artifacts.

        Takes the model lock and waits: a real turn must never be dropped or degraded because
        a preview happened to be running.
        """
        if samples is None or samples.size == 0 or is_silent(samples):
            return ""
        samples = np.asarray(samples, dtype=np.float32)
        with self._lock:
            return self._run(samples)

    def try_transcribe(self, samples: np.ndarray) -> Optional[str]:
        """Transcribe only if the model is free; return None if it is busy.

        For the live partial preview. Skipping is the correct behaviour under contention — the
        preview is disposable, and a partial that waits behind a final would be stale by the
        time it rendered anyway.
        """
        if samples is None or samples.size == 0 or is_silent(samples):
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._run(np.asarray(samples, dtype=np.float32))
        finally:
            self._lock.release()
