"""vm.voiceprint — learn what JJ sounds like, so the wake word stops being mandatory.

The wake word has been the weakest link in this project. A general-purpose recognizer rendered
"Claude" as Grab, Grub, God, Well, Joe, Clock, Clos, quote, club and Crawley and never once got
it right from the headset — because it is a rare token carried by a fraction of a second of
audio. **Speaker identity is carried by the whole utterance**, so it degrades gracefully exactly
where a wake word fails outright.

Two operations, mirroring `meeting-copilot/mc/voiceprint.py`, which already proved this stack
here (NeMo TitaNet-large; wespeaker-CAM++ and 3dspeaker-CAM++ collapsed speakers to near chance):

  * **enroll** — merge an utterance's embedding into a running centroid. `count` grows with every
    confirmed turn, so the print strengthens the more it is used. Nothing to sit through: the
    only utterances enrolled are ones already confirmed as JJ by the wake phrase.
  * **match** — cosine-compare a new utterance against the gallery.

**The gate is ADDITIVE, never subtractive** (see :func:`should_address`). A voice match can only
ever *grant* attention, never withhold it. A false positive costs one wasted read; a false
negative would mean being ignored while speaking, which is the failure this whole project keeps
tripping over. The wake phrase therefore keeps working forever, unchanged.

**Voiceprints are biometric.** The gallery is local-only, gitignored, and never leaves the
machine — same rule as the transcript.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import config

# Cosine bands. Deliberately conservative to start: below AUTO the wake phrase still works, so
# the cost of being strict is nil, while the cost of being loose is answering the television.
AUTO_THRESHOLD = 0.50
"""At or above this, treat the speaker as the owner without a wake phrase.

Set from measurement, not taste. TitaNet-large on real captures from this machine, 2026-07-29:

    JJ vs JJ (three separate sessions)   0.644, 0.660, 0.812
    JJ vs two different TTS voices       0.083 .. 0.201

0.50 sits ~2.5x above the highest impostor score and well below the lowest genuine one, so both
error modes need a large excursion to occur. An earlier 0.62 was inside the noise of genuine
single-sample variation — and the gallery compares against a *centroid*, which scores higher
than any single pair, so the real operating margin is wider still.

Worth noting: the agent's own synthesized replies score 0.13-0.20 against JJ, so the tunnel
cannot mistake itself for him even when echo cancellation lets its own voice back in."""

ENROLL_MIN_SECONDS = 1.0
"""Utterances shorter than this carry too little voice to learn from."""

MAX_CENTROID_COUNT = 200
"""Cap the running-average weight so the print keeps adapting to how he sounds *now* — a cold,
a different headset, a new room — instead of being frozen by its first hundred samples."""


def gallery_path() -> str:
    return os.path.join(config.session_dir(), "voiceprints.json")


def _load(path: Optional[str] = None) -> Dict:
    path = path or gallery_path()
    if not os.path.exists(path):
        return {"version": 1, "speakers": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "speakers": {}}


def _save(data: Dict, path: Optional[str] = None) -> None:
    path = path or gallery_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)          # atomic; a torn gallery would be silently wrong


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class Embedder:
    """TitaNet-large speaker embeddings. Loads lazily and is serialized, for the same reason
    the ASR model is: one ONNX session shared by the ingest path and any offline analysis."""

    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path or os.path.join(
            config.models_dir(), "nemo_en_titanet_large.onnx"
        )
        self._extractor = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return os.path.exists(self.model_path)

    def _ensure(self):
        if self._extractor is None:
            import sherpa_onnx  # lazy heavy import

            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=self.model_path, num_threads=config.asr_threads()
            )
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        return self._extractor

    def embed(self, samples: np.ndarray, sample_rate: int = config.TARGET_SR):
        """Return a float32 embedding, or None if unavailable/too short to be meaningful."""
        if not self.available or samples is None:
            return None
        if samples.size < int(sample_rate * ENROLL_MIN_SECONDS):
            return None
        with self._lock:
            ex = self._ensure()
            stream = ex.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=np.ascontiguousarray(samples, dtype=np.float32))
            stream.input_finished()
            if not ex.is_ready(stream):
                return None
            return np.asarray(ex.compute(stream), dtype=np.float32)


def enroll(name: str, embedding, path: Optional[str] = None) -> Dict:
    """Merge an embedding into `name`'s running centroid and return that speaker's record."""
    if embedding is None:
        return {}
    data = _load(path)
    rec = data["speakers"].get(name)
    vec = np.asarray(embedding, dtype=np.float32)
    if rec is None:
        rec = {"centroid": vec.tolist(), "count": 1}
    else:
        prev = np.asarray(rec["centroid"], dtype=np.float32)
        n = min(int(rec.get("count", 1)), MAX_CENTROID_COUNT)
        rec["centroid"] = ((prev * n + vec) / (n + 1)).tolist()
        rec["count"] = int(rec.get("count", 1)) + 1
    rec["updated"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    data["speakers"][name] = rec
    _save(data, path)
    return rec


def match(embedding, path: Optional[str] = None) -> Tuple[Optional[str], float]:
    """Best (name, similarity) in the gallery, or (None, 0.0)."""
    if embedding is None:
        return None, 0.0
    data = _load(path)
    best, best_sim = None, 0.0
    for name, rec in data.get("speakers", {}).items():
        sim = cosine(embedding, np.asarray(rec["centroid"], dtype=np.float32))
        if sim > best_sim:
            best, best_sim = name, sim
    return best, best_sim


def known(path: Optional[str] = None) -> List[Dict]:
    data = _load(path)
    return [
        {"name": n, "count": r.get("count", 0), "updated": r.get("updated")}
        for n, r in sorted(data.get("speakers", {}).items())
    ]


def forget(name: str, path: Optional[str] = None) -> bool:
    data = _load(path)
    if name in data.get("speakers", {}):
        del data["speakers"][name]
        _save(data, path)
        return True
    return False


def should_address(
    wake_said: bool, speaker: Optional[str], similarity: float, owner: str = "me"
) -> Tuple[bool, str]:
    """Combine the wake gate and the voice gate. Returns `(addressed, reason)`.

    **Additive by construction.** If the wake phrase fired, this returns True regardless of what
    the voiceprint thinks — a voice match can grant attention but can never take it away. That
    asymmetry is the whole safety argument: a false positive wastes one read, a false negative
    means being ignored mid-sentence, and this project has already demonstrated how bad that
    feels.
    """
    if wake_said:
        return True, "wake"
    if speaker == owner and similarity >= AUTO_THRESHOLD:
        return True, f"voice:{similarity:.2f}"
    return False, "not-addressed"
