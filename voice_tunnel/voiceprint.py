"""voice_tunnel.voiceprint — learn what JJ sounds like, so the wake word stops being mandatory.

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
import time
import wave
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import config

# Cosine bands. Deliberately conservative to start: below AUTO the wake phrase still works, so
# the cost of being strict is nil, while the cost of being loose is answering the television.
AUTO_THRESHOLD = 0.50
"""At or above this, treat the speaker as the owner without a wake phrase.

Set from measurement, not taste. Against a 180-sample centroid bootstrapped from recordings the
owner already had, scored on audio captured through a *different* device and pipeline (the
headset -> browser -> tunnel path):

    The owner, three tunnel sessions       0.711, 0.782, 0.801   <- accepted
    Three different TTS voices             0.000, 0.066, 0.132   <- rejected
    Three other human speakers             0.035, 0.055, 0.096   <- rejected

Lowest genuine 0.711 against highest impostor 0.132 — a 5.4x margin, with 0.50 sitting near the
midpoint. That the print *generalises across recording setups* is the important part: it was
learned from one microphone chain and still recognises the owner through a completely different
one, which is what makes silent enrolment viable.

Worth noting: the agent's own synthesized replies score 0.00-0.16, so the tunnel cannot mistake
itself for him even when echo cancellation lets its own voice back in."""

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
    """Write the gallery atomically, retrying the rename on Windows lock contention.

    `os.replace` is atomic and a torn gallery would be silently wrong, so the write must go
    through a temp file. But on Windows the rename intermittently raises PermissionError
    (WinError 5) when a scanner or indexer still holds a handle on the file just written — seen
    roughly once in four full test runs. Untreated that means a *silently lost enrolment*: the
    voice sample is computed, the centroid updated in memory, and then never persisted.

    A few short retries clear it; failing loudly afterwards is better than pretending it saved.
    """
    path = path or gallery_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    last: Optional[Exception] = None
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:      # transient handle held by another process
            last = exc
            time.sleep(0.02 * (attempt + 1))
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise RuntimeError(f"could not persist voiceprint gallery: {last}")


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


def speech_windows(
    samples: np.ndarray,
    sample_rate: int = config.TARGET_SR,
    window_s: float = 4.0,
    max_windows: int = 40,
    floor: float = 0.02,
) -> List[np.ndarray]:
    """Pick the loudest non-overlapping windows that plausibly contain speech.

    Deliberately crude — this is enrolment, not recognition. Taking the loudest windows spread
    across a long recording samples many different sentences and moods, which is what makes a
    centroid general rather than a fingerprint of one sentence.
    """
    n = int(sample_rate * window_s)
    if samples.size < n:
        return []
    scored = []
    for i in range(0, samples.size - n, n):
        w = samples[i : i + n]
        r = float(np.sqrt(np.mean(w * w)))
        if r >= floor:
            scored.append((r, i))
    scored.sort(reverse=True)
    # Spread the picks over the file rather than clustering on the single loudest passage.
    picked = sorted(i for _r, i in scored[: max_windows * 3])
    step = max(1, len(picked) // max_windows) if picked else 1
    return [samples[i : i + n] for i in picked[::step]][:max_windows]


def enroll_from_wav(
    path: str,
    name: str,
    embedder: "Embedder",
    channel: int = 0,
    max_windows: int = 25,
    gallery: Optional[str] = None,
) -> Dict[str, int]:
    """Learn a voice from one recording's channel. Returns `{windows, enrolled}`.

    Built for `meeting-copilot` session WAVs, which are 16 kHz stereo with **mic on the left and
    system audio on the right** — so channel 0 is JJ speaking, already labelled by construction
    with no diarization required. Hours of his real voice, in many rooms and moods, is a far
    better starting print than anything a scripted enrolment sentence would produce.

    **Only embeddings are computed.** No transcription, no audio is copied, and what lands in the
    gallery is a 192-dim centroid from which speech cannot be reconstructed — which is what makes
    it acceptable to point this at confidential meeting recordings.
    """
    with wave.open(path, "rb") as w:
        n_ch, width, rate, n_frames = (
            w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        )
        raw = w.readframes(n_frames)
    if width != 2:
        return {"windows": 0, "enrolled": 0}
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if n_ch > 1:
        a = a[channel::n_ch]
    if rate != config.TARGET_SR:
        from . import asr as _asr

        a = _asr.resample_linear(a, rate, config.TARGET_SR)

    windows = speech_windows(a, config.TARGET_SR, max_windows=max_windows)
    enrolled = 0
    for w_ in windows:
        emb = embedder.embed(np.ascontiguousarray(w_))
        if emb is not None:
            enroll(name, emb, path=gallery)
            enrolled += 1
    return {"windows": len(windows), "enrolled": enrolled}


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
