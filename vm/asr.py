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

    @property
    def seconds_buffered(self) -> float:
        return self._buf.size / float(self.sr)

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
            if rms(chunk) >= self.silence_floor:
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
        self._reset_buffer()

        speech_samples = out.size - trailing
        if speech_samples < self.min_samples or is_silent(out, self.silence_floor):
            return None  # too short, or nothing but noise — not a turn
        return out, t_start, t_end

    def _reset_buffer(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._trailing_silence = 0
        self._seen_speech = False

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
    """Wraps faster-whisper. The model loads on first use, never at import, so the CLI stays
    fast for commands that never touch audio."""

    def __init__(
        self,
        model_name: Optional[str] = None,
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self.model_name = model_name or config.whisper_model()
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # imported lazily on purpose

            self._model = WhisperModel(
                self.model_name, device=self.device, compute_type=self.compute_type
            )
        return self._model

    def transcribe(self, samples: np.ndarray) -> str:
        """Transcribe one complete utterance at 16 kHz. Returns '' for silence/artifacts."""
        if samples is None or samples.size == 0:
            return ""
        if is_silent(samples):
            return ""
        model = self._ensure_model()
        segments, _info = model.transcribe(
            np.asarray(samples, dtype=np.float32),
            language="en",
            beam_size=5,
            temperature=0.0,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments if s.text and s.text.strip())
        text = " ".join(text.split())
        if not text or is_hallucination(text):
            return ""
        return text
