"""voice_tunnel.turndetect — did he actually finish, or is he still thinking?

**The problem this replaces.** `END_OF_UTTERANCE_MS` is 1500. It was raised from 1000 after the owner was
cut off mid-thought, and it is a permanent compromise: short enough still interrupts someone
composing out loud, long enough makes every short question wait a second and a half for nothing.
**No single number fixes both, because the right wait depends on whether the sentence sounded
finished** — and that is not something a silence timer can know.

Smart Turn v3.2 ([pipecat-ai/smart-turn-v3][st], BSD-2-Clause, 8.3 MB int8 ONNX) reads the last
eight seconds of audio and returns the probability that the utterance is COMPLETE. It is
audio-native rather than text-based, so it reads prosody — the rising, unresolved intonation of
"I was thinking that maybe we could…" — which no transcript can show.

**Why it is affordable.** It runs ONCE, at a speech-to-silence boundary, never per audio chunk.
That property is stated in HuggingFace's own integration and it is what makes an 8 MB model free
in practice: the boundary already exists in `UtteranceBuffer`, and this is a question asked at it.

Mined from [huggingface/speech-to-speech][hf], which ships this model on by default and runs it
in production on thousands of Reachy Mini robots. Their tuned defaults are adopted wholesale in
`config` rather than re-derived.

**Every failure degrades to the timer.** Model missing, extras not installed, load error,
inference exception — all of them fall back to exactly the behaviour that exists today. A tunnel
that stopped segmenting because a turn-detection model was absent would be a much worse
regression than the one this fixes.

[st]: https://github.com/pipecat-ai/smart-turn
[hf]: https://github.com/huggingface/speech-to-speech
"""
from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

from . import config

logger = logging.getLogger(__name__)

MODEL_FILE = "smart-turn-v3.2-cpu.onnx"
MODEL_URL = f"https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/{MODEL_FILE}"
MODEL_SAMPLE_RATE = 16000
MAX_AUDIO_SECONDS = 8
"""The model's window. Longer audio is truncated KEEPING THE END, because the question being
asked is about how the utterance finished, not how it began."""


def model_path() -> str:
    return os.path.join(config.models_dir(), MODEL_FILE)


def installed() -> bool:
    # Same one-megabyte floor the downloader uses: an HTML error page saved under a .onnx name
    # is the failure that otherwise surfaces much later as an unreadable protobuf error.
    return os.path.exists(model_path()) and os.path.getsize(model_path()) >= (1 << 20)


class TurnDetector:
    """Answers one question: does this audio sound finished?

    Returns `True` (complete), `False` (still going), or `None` (no opinion — unavailable, or it
    failed). **None is not an error to the caller**; it means "use the timer", which is what the
    tunnel did before this existed.
    """

    def __init__(self, threshold: float | None = None) -> None:
        self.threshold = config.turn_threshold() if threshold is None else threshold
        self._session = None
        self._features = None
        self._lock = threading.Lock()
        self.unavailable_reason: str | None = None
        """Set once, on the first failure, and then reported rather than re-derived. Retrying a
        broken import on every utterance pays the failure repeatedly and floods the log with the
        same line — the same sticky-failure rule the resident TTS voice already follows."""
        self.last_ms: float = 0.0
        self.calls: int = 0

    def _ensure(self):
        if self._session is not None or self.unavailable_reason:
            return self._session
        if not installed():
            self.unavailable_reason = (
                f"{MODEL_FILE} is not installed — `voice-tunnel download turn`"
            )
            return None
        try:
            # transformers prints "PyTorch was not found" to stderr on import, which lands in the
            # middle of the serve banner and reads as a broken install. Nothing here wants torch —
            # the feature extractor is numpy — so the warning is noise, and noise on startup is
            # how people learn to ignore startup output.
            os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
            os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
            import onnxruntime as ort
            from transformers import WhisperFeatureExtractor
        except ImportError as exc:
            # `[turn]`, NOT `[parakeet]`. This said parakeet until 0.2.1 and was simply false:
            # that extra installs sherpa-onnx and neither of the two packages imported above, so
            # anyone following it stayed exactly as broken and reasonably concluded the feature
            # was unsupported on their machine.
            self.unavailable_reason = (
                f"turn detection needs `pip install voice-tunnel[turn]` ({exc})"
            )
            return None
        try:
            opts = ort.SessionOptions()
            # Single-threaded inter-op and a modest intra-op count: this runs at a boundary while
            # ASR may be starting on the same machine, and a model that grabs every core to save
            # 20 ms would take them from the thing the user is actually waiting for.
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = max(1, config.turn_threads())
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._session = ort.InferenceSession(
                model_path(), sess_options=opts, providers=["CPUExecutionProvider"]
            )
            self._features = WhisperFeatureExtractor(chunk_length=MAX_AUDIO_SECONDS)
            self._input = self._session.get_inputs()[0].name
        except Exception as exc:                      # a corrupt or mismatched model
            self.unavailable_reason = f"could not load {MODEL_FILE}: {exc}"
            self._session = None
        return self._session

    def complete(self, samples: np.ndarray) -> bool | None:
        """True if the utterance sounds finished, False if not, None for no opinion."""
        if samples is None or samples.size == 0:
            return None
        with self._lock:
            session = self._ensure()
            if session is None:
                return None
            try:
                t0 = time.perf_counter()
                audio = np.asarray(samples, dtype=np.float32)
                want = MAX_AUDIO_SECONDS * MODEL_SAMPLE_RATE
                # Keep the END. The model is judging how the utterance finished.
                audio = audio[-want:] if audio.size > want else audio

                feats = self._features(
                    audio,
                    sampling_rate=MODEL_SAMPLE_RATE,
                    return_tensors="np",
                    padding="max_length",
                    max_length=want,
                    truncation=True,
                    do_normalize=True,
                )
                x = np.expand_dims(feats.input_features.squeeze(0).astype(np.float32), axis=0)
                prob = float(session.run(None, {self._input: x})[0][0].item())
                self.last_ms = (time.perf_counter() - t0) * 1000.0
                self.calls += 1
                return prob >= self.threshold
            except Exception as exc:
                # One line, once. See unavailable_reason.
                self.unavailable_reason = f"turn detection failed: {exc}"
                logger.warning("turn detection disabled: %s", exc)
                return None

    def warm(self) -> dict:
        """Load NOW, on a background thread at serve, so the first turn is not the slow one.

        Measured on this machine: the first call costs **4.7 s** — the ONNX session plus the
        `transformers` import, which is heavy on its own — against 133 ms warm. Unwarmed, that 4.7 s
        lands at the worst possible moment: the very first thing he says, while he is waiting to
        find out whether any of this works. Exactly the reasoning that already warms the piper voice.
        """
        try:
            self.complete(np.zeros(MODEL_SAMPLE_RATE, dtype=np.float32))
        except Exception as exc:                      # warming must never take the server down
            self.unavailable_reason = self.unavailable_reason or str(exc)
        return self.describe()

    def describe(self) -> dict:
        return {
            "installed": installed(),
            "available": self.unavailable_reason is None and installed(),
            "reason": self.unavailable_reason,
            "threshold": self.threshold,
            "calls": self.calls,
            "last_ms": round(self.last_ms, 1),
        }
