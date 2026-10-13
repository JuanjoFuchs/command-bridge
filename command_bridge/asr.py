"""command_bridge.asr — utterance-buffered speech recognition.

**Why this is simpler than a streaming ASR.** A meeting transcriber works where
you cannot wait for a speaker to stop — it needs incremental commits (LocalAgreement-2) to react
mid-monologue. Here the turn boundary *is* the end of the utterance: nobody wants a reply before
they have finished their sentence. So we buffer until end-of-utterance and transcribe once. That
is more accurate (Whisper sees whole sentences), far less code, and has no duplicate-commit class
of bug at all. At RTF ~0.1 a 3-second utterance transcribes in ~0.3 s.

Silence discipline, inherited from that prior art because Whisper confidently hallucinates on silence
("you", "Thank you."):
  1. an RMS energy gate means a near-silent buffer is never sent to the model (this is what
     makes AC-1 a guarantee rather than a hope),
  2. faster-whisper's built-in Silero VAD drops non-speech inside otherwise-active audio, and
  3. a known-artifact phrase filter drops whole-utterance junk.

The model loads lazily and stays resident. Everything above the model boundary is pure and
unit-testable without faster-whisper installed.
"""
from __future__ import annotations

import sys
import threading
from collections import deque

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


def has_speech(samples: np.ndarray, floor: float = config.SILENCE_RMS_FLOOR,
               window_s: float = config.PIECE_SPEECH_WINDOW_S) -> bool:
    """True if ANY short window reaches the energy floor (spec 027).

    The gate for a piece of a turn the segmenter already accepted as speech. `is_silent` averages
    over the whole buffer, and a turn's tail is its last words plus seconds of closing silence: a
    short or soft ending averaged that way read as silence and was dropped undecoded (turns 11200,
    11229 on 2026-10-08, which JJ had to say again)."""
    if samples is None or len(samples) == 0:
        return False
    a = np.asarray(samples, dtype=np.float32)
    win = max(1, int(config.TARGET_SR * window_s))
    voiced = 0
    for i in range(0, a.size, win):
        if rms(a[i:i + win]) >= floor:
            voiced += 1
            if voiced >= config.PIECE_SPEECH_MIN_WINDOWS:
                return True
    return False


def is_hallucination(text: str) -> bool:
    return text.strip().lower() in _HALLUCINATIONS


def join_pieces(pieces: list[str]) -> str:
    """One turn's text from the pieces transcribed at his pauses, then judged as a whole.

    Each piece is decoded as if it were a complete utterance, so the model may close it with a
    full stop where he only paused for breath. When the next piece opens in lower case, the
    model itself is saying the sentence went on, so that stop is dropped."""
    out = ""
    for p in (p.strip() for p in pieces):
        if not p:
            continue
        if out and p[0].islower() and out.endswith("."):
            out = out[:-1]
        out = f"{out} {p}" if out else p
    return "" if is_hallucination(out) else out


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
        turn_detector=None,
    ) -> None:
        self.turn_detector = turn_detector
        """Optional `(samples) -> True | False | None` — does this sound finished?

        **INJECTED, never imported**, and the docstring above is why: this class is pure so its
        turn-boundary logic stays unit-testable with synthetic arrays and no model. It holds the
        most delicately tuned code in the project — the noise floor is measured rather than fixed
        because a fixed one failed against a real air conditioner, found live and not by testing —
        and every one of those tests would start depending on an 8 MB download.

        `None` means "no opinion": unavailable, or it failed. The timer decides, exactly as it did
        before this existed. See command_bridge/turndetect.py and specs/004-turn-detection.md.
        """
        self._last_asked_silence = -10**9
        """Trailing-silence count at the last early check, so `feed` cannot run the model on
        every audio chunk. Starts far negative so the first check is always allowed."""
        self._extended_samples = 0
        """How much extra silence the detector has already bought for THIS utterance. Bounded by
        TURN_MAX_WAIT_MS so a model stuck on 'incomplete' can never hold a turn open forever."""
        self.last_end_reason: str | None = None
        """Which mechanism closed the last turn: `timer`, `model-complete`, `model-early`,
        `model-exhausted`, or `too-long`. Recorded so the change is measurable rather than
        assumed — without it, 'is the model helping' is a matter of opinion."""
        self._max_extend_samples = int(sr * config.TURN_MAX_WAIT_MS / 1000)
        self._incomplete_delay_samples = int(sr * config.TURN_INCOMPLETE_DELAY_MS / 1000)
        # Through the reader, not the constant: this is the number that decides whether he gets
        # cut off, and it was raised 400 -> 800 from a measurement that the next session may
        # refine. A bare constant here would make COMMAND_BRIDGE_TURN_MIN_SILENCE_MS inert.
        self._min_silence_samples = int(sr * config.turn_min_silence_ms() / 1000)
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
        # Rolling energies for the noise estimate. A deque, not a min-tracker: see
        # config.NOISE_PERCENTILE for why min-tracking failed in a real room.
        self._win_samples = int(sr * 0.02) or 1
        self._levels: deque = deque(
            maxlen=max(8, int(config.NOISE_WINDOW_S * sr / self._win_samples))
        )
        self._threshold = silence_floor
        self._noise_floor = silence_floor
        self.preroll_samples = int(sr * 0.2)
        """Audio kept before detected speech onset. Enough to protect a soft first consonant,
        which the energy gate can miss, without handing the model a long silent runway."""
        self.last_cut = 0
        """Samples trimmed off the FRONT of the buffer in the last utterance `feed` returned (the
        silent runway before speech). A piece committed mid-turn is indexed in buffer samples, so
        this is what maps its end into the returned utterance."""

    @property
    def seconds_buffered(self) -> float:
        return self._buf.size / float(self.sr)

    def snapshot(self) -> np.ndarray | None:
        """A copy of the audio buffered so far, or None if there isn't speech in it yet.

        For live partial transcription: the in-flight utterance can be transcribed repeatedly
        while it is still being spoken, without disturbing the buffer that will become the
        final turn.
        """
        if not self._seen_speech or self._buf.size == 0:
            return None
        return self._buf.copy()

    @property
    def utterance_id(self) -> int:
        """Changes every time the buffer starts a new utterance — including one it discarded as
        too short or as noise, which `feed` reports the same way as 'not finished'. Pieces cut from
        an utterance are only valid while this is unchanged."""
        return self._buf_start_total

    def onset_index(self) -> int:
        """Buffer index where the in-flight utterance's audio begins: speech onset minus the
        preroll, the same trim `feed` applies to the utterance it returns."""
        onset = self._utterance_start_sample - self._buf_start_total
        return max(0, onset - self.preroll_samples)

    def commit_point(self, start: int, pause_samples: int, min_samples: int) -> int | None:
        """Where to cut a piece for transcription mid-turn, or None.

        A piece ends inside a pause he is in RIGHT NOW (trailing silence of at least
        `pause_samples`), halfway into it so neither side clips a consonant, and only when at
        least `min_samples` of audio lie between `start` and the pause. Called per chunk, so the
        caller moves `start` to the returned point; the same pause then cannot fire again."""
        if not self._seen_speech or pause_samples <= 0:
            return None
        if self._trailing_silence < pause_samples:
            return None
        speech_end = self._buf.size - self._trailing_silence
        if speech_end - start < min_samples:
            return None
        return speech_end + self._trailing_silence // 2

    def span(self, start: int, end: int | None = None) -> np.ndarray:
        """A copy of buffer samples [start, end)."""
        return self._buf[start:end].copy()

    @property
    def speech_active(self) -> bool:
        """True when someone is mid-utterance — speech has started and the end-of-utterance
        silence has not yet elapsed.

        Used to hold synthesized audio back rather than talking over the speaker, which is the
        difference between a conversation and a dictation machine that shouts.
        """
        return self._seen_speech and self._trailing_silence < self.end_silence_samples

    @property
    def utterance_open(self) -> bool:
        """True from his first syllable until the utterance is CLOSED and handed off — including the
        stretch where the end-of-utterance silence has passed but the turn model judged him
        unfinished and is holding the turn open for more (TURN_MAX_WAIT_MS, bounded).

        `speech_active` already reads False in that stretch, and nothing is `speech_pending` yet
        because nothing has closed, so a reply held only on those two played into his mid-turn pause
        (JJ, 2026-10-05: a clip played while "there is a bunch of text already recognized from me
        because … you haven't read what I … have just said or I am saying")."""
        return self._seen_speech

    def feed(self, samples: np.ndarray) -> tuple[np.ndarray, float, float] | None:
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
            self._levels.append(level)
            if level >= self._threshold:
                if not self._seen_speech:
                    # Anchor the utterance to where speech actually began.
                    self._utterance_start_sample = self._total_fed + i
                self._seen_speech = True
                self._trailing_silence = 0
            elif self._seen_speech:
                self._trailing_silence += chunk.size

        # Recompute the room estimate once per feed rather than per 20 ms window — it changes
        # on the timescale of a fan switching on, not a syllable.
        if self._levels:
            floor = float(np.percentile(np.fromiter(self._levels, dtype=np.float32),
                                        config.NOISE_PERCENTILE))
            self._noise_floor = floor
            self._threshold = max(self.silence_floor, floor * config.NOISE_MARGIN)

        self._buf = np.concatenate([self._buf, samples])
        self._total_fed += samples.size

        if self._total_fed < self.grace_samples and not self._seen_speech:
            return None

        ended = self._seen_speech and self._trailing_silence >= self.end_silence_samples
        too_long = self._buf.size >= self.max_samples
        if ended:
            self.last_end_reason = "timer"
        elif too_long:
            self.last_end_reason = "too-long"

        # ASK THE MODEL, at the boundary the timer just found. This is the whole feature: the
        # timer says "1.5 s of silence has passed", which is not the same question as "has he
        # finished". Only here, never per chunk — that is what makes an 8 MB model free.
        if ended and self.turn_detector is not None:
            if self._extended_samples >= self._max_extend_samples:
                # Bounded. A model stuck on "incomplete" must not hold a turn open forever: a
                # turn that never closes is a tunnel that never answers.
                self.last_end_reason = "model-exhausted"
            else:
                verdict = self.turn_detector(self._buf)
                if verdict is False:
                    # Still going. Buy him more room and re-ask after another stretch of quiet,
                    # rather than on the next chunk — the answer cannot change that fast.
                    self._trailing_silence = max(
                        0, self.end_silence_samples - self._incomplete_delay_samples
                    )
                    self._extended_samples += self._incomplete_delay_samples
                    ended = False
                    self.last_end_reason = None
                elif verdict is True:
                    self.last_end_reason = "model-complete"
                # verdict is None -> no opinion, the timer already decided. Reason stays "timer".

        # EARLY EXIT: a confident "finished" closes the turn before the full timeout, which is
        # where most of the latency win lives. Gated by its own floor because this is the risky
        # half — closing early on a wrong prediction is exactly the failure that raising
        # 1000 -> 1500 was meant to fix, and gaps under TURN_MIN_SILENCE_MS are inside normal
        # speech.
        #
        # RATE-LIMITED, and that is not an optimisation. `feed` runs per audio chunk — several
        # times a second — so an ungated check here would run the model continuously through
        # every pause, which is precisely the "never per chunk" property that makes it free.
        # Asked at most once per TURN_INCOMPLETE_DELAY_MS of additional silence.
        if (
            not ended
            and not too_long
            and self.turn_detector is not None
            and self._seen_speech
            and self._min_silence_samples <= self._trailing_silence < self.end_silence_samples
            and self._buf.size >= self.min_samples
            and self._trailing_silence - self._last_asked_silence >= self._incomplete_delay_samples
        ):
            self._last_asked_silence = self._trailing_silence
            if self.turn_detector(self._buf) is True:
                ended = True
                self.last_end_reason = "model-early"

        if not (ended or too_long):
            # Deliberately NOT cleared here. It is the reason the LAST turn ended, and `feed` runs
            # several times a second afterwards — clearing it on every non-ending chunk meant the
            # value was almost always None by the time anything read it.
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
        self.last_cut = cut
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
        # Per-UTTERANCE, not per-session: the extension budget and the ask-rate limiter both
        # have to start fresh, or one long thought would spend the budget for every turn after it.
        self._extended_samples = 0
        self._last_asked_silence = -10**9
        self._buf_start_total = self._total_fed

    def flush(self) -> tuple[np.ndarray, float, float] | None:
        """End of stream: emit whatever is buffered if it looks like speech."""
        if self._buf.size < self.min_samples or is_silent(self._buf, self.silence_floor):
            self._reset_buffer()
            return None
        out = self._buf
        t_start = self._utterance_start_sample / float(self.sr)
        t_end = self._total_fed / float(self.sr)
        self.last_cut = 0
        self._reset_buffer()
        return out, t_start, t_end


def parakeet_files(d: str) -> dict[str, str]:
    """The model files in a Parakeet directory, whichever precision it holds (spec 041): the int8
    export (the CPU's), the fp32 build (the GPU's), or the fp16 export (correct on the CPU only).
    `{encoder, decoder, joiner, tokens, precision}`."""
    import os

    for precision in ("int8", "fp32", "fp16"):
        if os.path.exists(os.path.join(d, f"encoder.{precision}.onnx")):
            break
    else:
        precision = "int8"      # the historic name, so a missing model still errors on that path
    path = {k: os.path.join(d, f"{k}.{precision}.onnx") for k in ("encoder", "decoder", "joiner")}
    return {**path, "tokens": os.path.join(d, "tokens.txt"), "precision": precision}


def _add_cuda_dll_dirs() -> int:
    """See `config.add_cuda_dll_dirs` (spec 041); a name here so tests can stub it."""
    return config.add_cuda_dll_dirs()


def _sherpa_has_cuda() -> bool:
    """Is the installed sherpa-onnx a CUDA build? (spec 041) The CPU wheel ACCEPTS provider="cuda"
    and silently runs on the CPU, and in the server nvidia-smi cannot tell (Kokoro already makes the
    process a GPU compute app). The build can: a CUDA wheel ships onnxruntime_providers_cuda.dll."""
    import importlib.util
    import os

    spec = importlib.util.find_spec("sherpa_onnx")
    if spec is None or not spec.origin:
        return False
    lib = os.path.join(os.path.dirname(spec.origin), "lib")
    return any(os.path.exists(os.path.join(lib, n))
               for n in ("onnxruntime_providers_cuda.dll", "libonnxruntime_providers_cuda.so"))


def asr_device() -> dict:
    """Is the live recognizer really on the GPU? Asked of nvidia-smi, because a provider that loads
    can still run elsewhere (spec 041; the research's "silent fallback" trap). `{listed, why}`."""
    import os
    import subprocess

    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"listed": None, "why": f"nvidia-smi unavailable: {exc}"}
    listed = str(os.getpid()) in {line.strip() for line in out.splitlines()}
    return {"listed": listed,
            "why": "this process is a GPU compute app" if listed else "not listed by nvidia-smi"}


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
        model_name: str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        engine: str | None = None,
    ) -> None:
        self.engine = (engine or config.asr_engine()).lower()
        self.model_name = model_name or (
            "parakeet-tdt-0.6b-v2" if self.engine == "parakeet" else config.whisper_model()
        )
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._hotwords = ""  # set by _ensure_parakeet; "" means no biasing / preview streams
        # Spec 041: the provider the model ACTUALLY loaded on (cuda can fall back to cpu), and why
        # when it did. None until the model loads.
        self.provider_effective: str | None = None
        self.provider_note: str | None = None
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
            files = parakeet_files(d)
            encoder, decoder, joiner, tokens = (files[k] for k in ("encoder", "decoder", "joiner", "tokens"))
            threads = config.asr_threads()
            # CONTEXTUAL BIASING, opt-in and gated on config.parakeet_hotwords() — see there for
            # why (bpe.vocab, phrases, modified_beam_search) must line up. When it returns None the
            # plain branch runs greedy, byte-identical to before. Measured 2026-08-31.
            #
            # 🔴 THE HOTWORDS ARE NOT BAKED INTO THE MODEL. They are held on `self._hotwords` and
            # applied PER STREAM, on final decodes only — because the live preview transcribes a
            # partial utterance repeatedly, and on a short partial the hotword over-boosts into
            # "Claude Claude Claude" (he saw exactly this in the live window). So the model is built
            # with modified_beam_search + bpe.vocab but NO hotwords_file; the phrases ride on the
            # final's stream and never the preview's. See _transcribe_parakeet.
            # provider: cpu (default) / cuda / directml — moves the 0.6B decode off the CPU
            # (JJ, 2026-09-21). 🔴 A CUDA/cuDNN mismatch SILENTLY falls back to cpu — verify with nvidia-smi.
            provider = config.asr_provider()
            # Force-greedy bypasses hotword biasing, which needs modified_beam_search — documented to
            # hallucinate / return empty ~20% on NeMo TDT (sherpa-onnx#3267) and heavier on CPU than greedy.
            hw = None if config.asr_force_greedy() else config.parakeet_hotwords()
            self._hotwords = hw["phrases"] if hw else ""

            def build(provider, encoder, decoder, joiner, tokens):
                if hw:
                    return sherpa_onnx.OfflineRecognizer.from_transducer(
                        encoder=encoder, decoder=decoder, joiner=joiner, tokens=tokens,
                        num_threads=threads, model_type="nemo_transducer",
                        decoding_method="modified_beam_search",
                        hotwords_score=hw["score"],
                        modeling_unit="bpe", bpe_vocab=hw["bpe_vocab"],
                        provider=provider,
                    )
                return sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=encoder, decoder=decoder, joiner=joiner, tokens=tokens,
                    num_threads=threads, model_type="nemo_transducer",
                    provider=provider,
                )

            self.provider_note = None

            def on_cpu(why: str):
                # NEVER A DEAD ASR, AND NEVER A FAKE GPU. Back to the CPU on the int8 model it is
                # fast on, with the reason in the serve log and on `provider_note`.
                self.provider_note = f"cuda unavailable, on cpu: {why}"
                print(f"command-bridge ASR: {self.provider_note}", file=sys.stderr, flush=True)
                cpu_dir = os.path.join(config.models_dir(),
                                       "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")
                cpu = parakeet_files(cpu_dir if os.path.isdir(cpu_dir) else d)
                self.provider_effective = "cpu"
                return build("cpu", cpu["encoder"], cpu["decoder"], cpu["joiner"], cpu["tokens"])

            if provider == "cuda" and not _sherpa_has_cuda():
                self._model = on_cpu("the installed sherpa-onnx is the CPU build (no "
                                     "onnxruntime_providers_cuda.dll); install the +cuda12.cudnn9 wheel")
            elif provider == "cuda":
                # The CUDA provider needs cuDNN 9 / cuBLAS 12, which come from the nvidia-* wheels
                # Kokoro already uses; without their folders on the search path it fails to LOAD.
                _add_cuda_dll_dirs()
                try:
                    self._model = build("cuda", encoder, decoder, joiner, tokens)
                    self.provider_effective = "cuda"
                except RuntimeError as exc:
                    self._model = on_cpu(str(exc))
            else:
                self._model = build(provider, encoder, decoder, joiner, tokens)
                self.provider_effective = provider
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

    def _transcribe_parakeet(self, samples: np.ndarray, final: bool) -> str:
        model = self._ensure_parakeet()
        # Hotwords only on the FINAL decode. The preview passes none, so a short partial cannot
        # over-boost a hotword — see _ensure_parakeet. `self._hotwords` is "" unless biasing is
        # configured, in which case the plain create_stream() path is still greedy-equivalent.
        if final and self._hotwords:
            stream = model.create_stream(self._hotwords)
        else:
            stream = model.create_stream()
        stream.accept_waveform(config.TARGET_SR, samples)
        model.decode_stream(stream)
        return stream.result.text or ""

    # -- the interface ------------------------------------------------------
    def _run(self, samples: np.ndarray, final: bool, piece: bool = False) -> str:
        if self.engine == "parakeet":
            text = self._transcribe_parakeet(samples, final)
        else:
            text = self._transcribe_whisper(samples)
        text = " ".join(text.split())
        # The artifact filter judges a WHOLE utterance ("so" alone is junk, "so what" is not).
        # A mid-turn piece is not one: "Yeah." followed by a pause is a real word of a longer
        # turn, so pieces skip it and the joined turn is judged as a whole instead.
        if not text or (not piece and is_hallucination(text)):
            return ""
        return text

    def transcribe(self, samples: np.ndarray, piece: bool = False) -> str:
        """Transcribe one complete utterance at 16 kHz. Returns '' for silence/artifacts.

        Takes the model lock and waits: a real turn must never be dropped or degraded because
        a preview happened to be running. `piece=True` is a mid-turn piece (see `join_pieces`),
        which skips the whole-utterance artifact filter and is gated on speech PRESENCE rather than
        whole-buffer energy (spec 027): a piece lies inside a turn already accepted as speech, so the
        gate may keep his words but can never create a turn.
        """
        if samples is None or samples.size == 0:
            return ""
        if (not has_speech(samples)) if piece else is_silent(samples):
            return ""
        samples = np.asarray(samples, dtype=np.float32)
        with self._lock:
            return self._run(samples, final=True, piece=piece)

    def try_transcribe(self, samples: np.ndarray) -> str | None:
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
            return self._run(np.asarray(samples, dtype=np.float32), final=False)
        finally:
            self._lock.release()
