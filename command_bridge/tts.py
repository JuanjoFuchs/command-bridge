"""command_bridge.tts — text to speech, pluggable backend.

Backends:
  * ``sapi``  — Windows System.Speech via PowerShell. Zero install, fully offline, present on
                every Windows box. The default, so a fresh clone speaks without downloading a
                model.
  * ``piper`` — the documented upgrade path. Better voices, needs a binary + an .onnx voice.
  * ``kokoro``— warmer, more natural voices than piper, at 24 kHz. ONE model plus ONE voice pack
                holding every voice, so selecting a voice costs no extra download. Heavier per
                call than resident piper, which is the trade being made.
  * ``none``  — silence of the right duration. For tests that care about plumbing, not audio.

Every backend's output goes through :func:`pad`, which is not cosmetic: Bluetooth sinks power
down between clips and swallow the first ~100 ms. This project is phone-first, so nearly every
session is Bluetooth, and unpadded audio presents as "the TTS is broken".

Output is always mono 16-bit PCM. The SAMPLE RATE is the backend's own and travels back with the
audio — :data:`command_bridge.config.TTS_SR` for SAPI and piper, 24 kHz for kokoro. Assuming one
global rate here would either resample kokoro down for nothing or play it 9% slow.
"""
from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import threading
import wave

from . import config, speech


class TTSError(RuntimeError):
    pass


def normalize(pcm: bytes) -> bytes:
    """Scale down to config.PEAK_CEILING if the audio is hotter than that.

    Only ever attenuates — quiet speech is left alone rather than being pumped up, because
    boosting also boosts whatever noise came with it.
    """
    import array

    samples = array.array("h")
    samples.frombytes(pcm)
    if not samples:
        return pcm
    peak = max(abs(s) for s in samples)
    ceiling = int(32767 * config.PEAK_CEILING)
    if peak <= ceiling:
        return pcm
    scale = ceiling / peak
    for i, v in enumerate(samples):
        samples[i] = int(v * scale)
    return samples.tobytes()


def quiet(n_samples: int) -> bytes:
    """`n_samples` of silence, as bytes. Takes SAMPLES, never a byte count.

    The argument unit is the whole point. Computing a byte count directly (`int(rate * pause * 2)`)
    lands ODD for some pause values — 0.7 and 0.85 at 22050 Hz both do — and an odd byte count
    shifts every later sample by one byte, so the rest of the clip decodes as loud broadband
    noise. Found by ear, live, 2026-08-06, and the reason this is a named function rather than an
    inline expression repeated at each call site.

    **This used to emit ±1 LSB dither instead of zeros**, on the theory that a sink powering down
    through a long gap was swallowing the first word of each sentence. That theory was wrong twice
    over: it did not fix the symptom, and at 24 kHz the alternating block produced an audible
    ~187 Hz hum in every pause — JJ, 2026-08-14: *"I did hear some weird sounds when you stopped on
    a punctuation."* The actual cause was SPEECH SPEED (2.0 compressed each sentence's opening
    consonant past the point the ear re-acquires after a pause). Recorded here so nobody re-derives
    the dither idea: it is a plausible-sounding fix for a problem that was never in the audio.
    """
    return b"\x00\x00" * max(0, n_samples)


def pad(pcm: bytes, sample_rate: int) -> bytes:
    """Prepend/append silence so a Bluetooth sink has time to wake and doesn't clip the tail."""
    lead = quiet(int(sample_rate * config.CHIME_LEADING_SILENCE_S))
    trail = quiet(int(sample_rate * config.CHIME_TRAILING_SILENCE_S))
    return lead + pcm + trail


def _read_wav_mono16(path: str) -> tuple[bytes, int]:
    """Read a WAV as mono 16-bit PCM. Downmixes stereo; refuses non-16-bit rather than guess."""
    with wave.open(path, "rb") as w:
        n_ch, width, rate, n_frames = (
            w.getnchannels(),
            w.getsampwidth(),
            w.getframerate(),
            w.getnframes(),
        )
        raw = w.readframes(n_frames)
    if width != 2:
        raise TTSError(f"expected 16-bit PCM, got {width * 8}-bit")
    if n_ch == 2:
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        mono = [
            (samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)
        ]
        raw = struct.pack(f"<{len(mono)}h", *mono)
    elif n_ch != 1:
        raise TTSError(f"unsupported channel count: {n_ch}")
    return raw, rate


def _synth_sapi(text: str) -> tuple[bytes, int]:
    """Speak via Windows System.Speech into a temp WAV, then read it back.

    Goes through a file rather than a stream because SAPI's streaming API is COM-bound and
    this keeps the whole backend to one subprocess call with no pywin32 dependency.
    """
    if os.name != "nt":
        raise TTSError("the sapi backend requires Windows; set COMMAND_BRIDGE_TTS=piper or none")
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        # Single-quoted PS literal; double any quote in the text so it cannot break out.
        safe = text.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.SetOutputToWaveFile('{path}'); "
            f"$s.Speak('{safe}'); "
            "$s.Dispose()"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise TTSError(f"SAPI failed: {proc.stderr.strip()[:400]}")
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise TTSError("SAPI produced no audio")
        return _read_wav_mono16(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def list_voices() -> list:
    """Voices selectable RIGHT NOW, which means: for the backend that is actually live.

    Backend-aware rather than piper-only, because the two name spaces do not overlap at all
    (`en_GB-alan-medium` vs `bm_daniel`). Listing piper's voices while kokoro is speaking would
    offer six names that every one of which fails at synthesis time — the same class of bug the
    sidecar check below was added to fix.

    For piper it delegates to config so "what is installed" has one definition (AGENTS.md
    convention 5). That listing requires each `.onnx` to have its sidecar `.onnx.json`, which
    keeps the voiceprint gallery's speaker model out of `command-bridge voices` — it was being
    offered as a selectable voice that then failed at synthesis time.
    """
    if config.tts_backend() == "kokoro":
        return _KOKORO.voices()
    return config.piper_voices()


def _resolve_kokoro_voice(name: str | None) -> str:
    """Validate a Kokoro voice NAME against the pack, or fall back to the configured default.

    Validated for the same reason :func:`resolve_voice` refuses paths: `/say` is reachable over
    the tunnel, so caller-supplied strings are checked against a known set before they reach a
    model loader. Unknown names are rejected loudly instead of silently substituting the default —
    a silent substitution is how you spend a session wondering why `--voice` does nothing.
    """
    if not name:
        return config.kokoro_voice()
    known = _KOKORO.voices()
    if known and name not in known:
        raise TTSError(f"unknown kokoro voice {name!r}; available: {', '.join(known)}")
    return name


def resolve_voice(name: str | None) -> str | None:
    """Map a voice NAME to its model path inside the models dir.

    Deliberately not an arbitrary path: `/say` is reachable over the tunnel, and letting a
    caller name any file on disk turns a text-to-speech endpoint into a file probe. A name that
    doesn't resolve is rejected rather than falling back silently.
    """
    if not name:
        return None
    if name not in list_voices():
        raise TTSError(
            f"unknown voice {name!r}; available: {', '.join(list_voices()) or '(none)'}"
        )
    return os.path.join(config.models_dir(), f"{name}.onnx")


class _ResidentVoice:
    """A piper voice loaded once into THIS process and reused for every reply.

    **This is the single biggest latency win in the tool.** Spawning `piper.exe` per reply cost
    3.6-4.3 s of which ~3.5 s was pure startup — interpreter, onnxruntime, and the ONNX model,
    re-paid on every sentence the agent spoke. Held resident, the same synthesis takes 0.17-0.58 s
    and scales with text length, which is the work actually being done. Before this, TTS was more
    than 10x slower than transcription (Parakeet: 0.23 s) and nobody had measured it.

    Serialized under a lock, deliberately. Piper phonemizes through espeak-ng, a C library with
    global state that is not safe to call from several threads at once, and the server hands
    synthesis to an executor thread. There is one speaker on one tunnel, so serializing costs
    nothing real and removes a class of crash that would look like a random audio glitch.

    A load failure is sticky and falls back to the subprocess forever after: if the library is not
    importable or the model will not load, retrying it per reply just pays the failure repeatedly.
    A *synthesis* failure is NOT caught here — the subprocess runs the same code on the same text
    and would fail the same way, so masking it would only hide a real bug.
    """

    def __init__(self) -> None:
        self._voice = None
        self._path: str | None = None
        self._lock = threading.Lock()
        self.unavailable_reason: str | None = None

    @property
    def loaded(self) -> bool:
        return self._voice is not None

    def _load(self, voice_path: str):
        """Return a loaded voice for `voice_path`, or None if the resident path cannot be used."""
        if self.unavailable_reason:
            return None
        if self._voice is not None and self._path == voice_path:
            return self._voice
        try:
            from piper import PiperVoice
        except ImportError as exc:
            self.unavailable_reason = f"the piper python package is not importable ({exc})"
            return None
        try:
            self._voice = PiperVoice.load(voice_path)
            self._path = voice_path
        except Exception as exc:      # a corrupt model, a missing sidecar, an onnxruntime fault
            self.unavailable_reason = f"{os.path.basename(voice_path)} would not load ({exc})"
            self._voice = None
            self._path = None
            return None
        return self._voice

    def synthesize(
        self, text: str, voice_path: str, length_scale: float, pause: float
    ) -> tuple[bytes, int] | None:
        """Mono 16-bit PCM, or None if the resident path is unusable and the caller should spawn.

        Sentence silence is inserted BETWEEN chunks and never after the last one, matching
        `piper --sentence-silence` exactly — piper yields one audio chunk per sentence, so this
        is the same seam its CLI writes into, not an approximation of it.

        **The text is handed over one `speech.segments` piece at a time** (spec 008, FR3), so a
        clause break gets a real gap of its own instead of the 0 ms every comma measured. The
        gap is a MULTIPLE of the same `pause` a sentence gets, never an independent number, which
        is what guarantees a comma can never outlast the full stop it sits inside. Comma-free
        text produces exactly one piece per sentence, so it is byte-for-byte what this did
        before.
        """
        from piper import SynthesisConfig

        with self._lock:
            voice = self._load(voice_path)
            if voice is None:
                return None
            syn = SynthesisConfig(length_scale=length_scale)
            sample_rate = voice.config.sample_rate
            # SAMPLES first, then x2 for 16-bit — never `int(rate * pause * 2)`.
            # That form rounds to a BYTE count, which lands odd for some pauses (0.7 and
            # 0.85 at 22050 Hz), and an odd byte count shifts every subsequent sample by
            # one byte: the rest of the clip decodes as loud broadband noise. The default
            # 0.5 happens to be even, which is why this survived until someone asked for a
            # longer pause and heard static. Found by ear, live, 2026-08-06.
            # NEAR-silence, not zeros — see `quiet()`. A digital-silence gap here is what made
            # the first word of every sentence after the first inaudible on a sleeping sink.
            silence = quiet(int(sample_rate * pause))
            parts = []
            for piece, gap in speech.segments(text) or [(text, 0.0)]:
                for i, chunk in enumerate(voice.synthesize(piece, syn)):
                    if i:
                        parts.append(silence)
                    parts.append(chunk.audio_int16_bytes)
                if gap:
                    parts.append(quiet(int(sample_rate * pause * gap)))
        if not parts:
            raise TTSError("piper produced no audio")
        return b"".join(parts), sample_rate


_RESIDENT = _ResidentVoice()


KOKORO_UNIT_SAMPLES = 600
"""Samples of audio per duration unit in Kokoro's timestamped export — 25 ms at 24 kHz.

**Measured, not derived, because the arithmetic disagrees with the model.** `config.json`
(`upsample_rates=[10,6]`, `gen_istft_hop_size=5`) implies 300, which is wrong by exactly a factor
of two. The relationship that holds is:

    samples == sum(max(1, round(dᵢ))) * 600

**exact — zero error — across 14 cases** on 2026-08-26: four voices and speeds 0.8 through 4.0.
Both roundings carry weight. Rounding the SUM instead of each token matched 0 of 10; the
`max(1, …)` floor is what closed the last two, because a token never costs less than one unit and
at high speed many round to zero. See the local-TTS research notes."""


def word_offsets(tokens, durations, space_id):
    """Start/end SAMPLE offsets for each spoken group, from per-token durations (spec 020 FR1).

    A "group" is a run of tokens between spaces — which is what the model actually paces as a
    unit. The first and last tokens are the pad the export brackets every sequence with, and the
    leading one is not silence-adjacent decoration: measured at 11.946 units ≈ 299 ms against a
    word-one start of 0.300 s, it IS the leading silence. It is excluded here rather than
    highlighted (TC3).

    ⚠ **Cumulate the ROUNDED values, never the raw floats.** The durations arrive as pre-round
    float32 — the PyTorch reference rounds them and the ONNX export does not — so a naive
    `cumsum(durations)` looks right and drifts: **567 ms by the end of a 7.5 s sentence** at the
    speed he actually runs. That is the failure where a highlight is correct for three words and a
    word behind by the end, which is the hardest kind to attribute later.
    """
    units = [max(1, int(round(float(d)))) for d in durations]
    starts, acc = [], 0
    for u in units:
        starts.append(acc)
        acc += u
    spans: list[list[int]] = []
    open_group = False
    for i, tid in enumerate(tokens):
        if i == 0 or i == len(tokens) - 1 or tid == space_id:
            open_group = False
            continue
        if not open_group:
            open_group = True
            spans.append([starts[i], 0])
        spans[-1][1] = starts[i] + units[i]
    return [(a * KOKORO_UNIT_SAMPLES, b * KOKORO_UNIT_SAMPLES) for a, b in spans]


def label_groups(text: str, groups: list[str]) -> tuple[list[str], bool]:
    """Pair spoken groups with the words they came from. Returns `(labels, aligned)`.

    🔴 **The pairing is not always one-to-one, and this reports that instead of guessing.**
    Phonemization is free to merge and to split: measured 2026-08-26, *"The camera is on the
    left."* is six words and FIVE groups because `on the` becomes a single `ɒnðə`, and *"We have
    54 voices"* expands one token into two. The tokenizer returns a bare string with no alignment
    back to the text, so nothing recoverable connects them when the counts differ.

    **When they match, the labels are the real words and `aligned` is True.** When they do not,
    the labels fall back to the phoneme groups and `aligned` is False — the caller still gets a
    correct SCHEDULE, at the granularity the engine actually paces, and is told the words are not
    a per-word mapping. FR4: an estimate presented in the shape of a measurement is the thing this
    feature exists to remove.

    ⚠ **Do not "fix" this by phonemizing each word separately and concatenating.** That would make
    the mapping exact and change the audio: `ɒnðə` is the cross-word run a natural reading
    produces, and NFR2 holds the sound constant.
    """
    words = text.split()
    if len(words) == len(groups):
        return words, True
    return groups, False


class _ResidentKokoro:
    """The Kokoro model held open in THIS process, for the same reason :class:`_ResidentVoice` is.

    Kokoro has no CLI to spawn, so unlike piper there is no subprocess to fall back to — resident
    is the only path. That makes the sticky-failure rule matter more, not less: if the model will
    not load, every reply must fail with the SAME named reason rather than re-paying a multi-second
    load to rediscover it. `available()` surfaces that reason so a dead backend is visible in
    `status` instead of presenting as "the tunnel went quiet".

    One model serves every voice — a voice is a style vector inside the pack — so switching voices
    mid-session costs a dictionary lookup, not a reload. That is the real reason `--voice` is cheap
    here and expensive under piper.

    Serialized under a lock for the same reason as piper: espeak-ng phonemization underneath is C
    with global state, and the server synthesizes on an executor thread.
    """

    def __init__(self) -> None:
        self._kokoro = None
        self._lock = threading.Lock()
        self.unavailable_reason: str | None = None
        # The speed the LAST synthesis asked for, when it was above Kokoro's ceiling and had to
        # be clamped — None when nothing was clamped. Kept so the clamp is a reportable fact
        # rather than a thing that quietly happened; see `synthesize` and `available`.
        self.speed_clamped_from: float | None = None
        # Whether the loaded export reports per-token durations. Decided at load from the graph's
        # own outputs, never from the filename.
        self._timed = False

    @property
    def loaded(self) -> bool:
        return self._kokoro is not None

    def _load(self):
        if self.unavailable_reason:
            return None
        if self._kokoro is not None:
            return self._kokoro
        # `kokoro_model` already prefers the timestamped export when it is on disk — identical
        # weights (audio measured bit-for-bit equal, 2026-08-26) plus a per-token duration output.
        # Asking it rather than choosing here is what keeps `doctor` and `status` naming the file
        # this actually loaded, instead of the one they would have picked.
        model, voices = config.kokoro_model(), config.kokoro_voices_bin()
        # Named separately: having the model without the pack is the state `download` exists to
        # prevent, and "kokoro is broken" is a useless thing to tell someone who is missing one file.
        missing = [n for n, p in (("kokoro-v1.0.onnx", model), ("voices-v1.0.bin", voices))
                   if not p]
        if missing:
            self.unavailable_reason = (
                f"{' and '.join(missing)} not in {config.models_dir()} — run "
                f"`command-bridge download kokoro`"
            )
            return None
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            self.unavailable_reason = f"the kokoro-onnx package is not importable ({exc})"
            return None
        try:
            self._kokoro = Kokoro(model, voices)
        except Exception as exc:
            self.unavailable_reason = f"the kokoro model would not load ({exc})"
            self._kokoro = None
            return None
        # ASK THE SESSION, NOT THE FILENAME. Whether this export reports durations is a property
        # of the graph, and a path can be overridden by `COMMAND_BRIDGE_KOKORO_MODEL` to anything.
        # This flag is also what keeps the ORIGINAL synthesis path intact: without durations the
        # backend still goes through `Kokoro.create` exactly as before, so swapping the model is
        # the only thing that changes behaviour.
        try:
            self._timed = any(o.name == "durations" for o in self._kokoro.sess.get_outputs())
        except Exception:
            self._timed = False
        return self._kokoro

    MAX_TOKENS = 510
    """The export's context, minus the two pad tokens it brackets every sequence with."""

    def _run(self, k, phonemes: str, voice: str, speed: float):
        """One forward pass, issued HERE rather than through `kokoro_onnx.Kokoro.create`.

        🔴 **This exists because of the dtype on ONE argument.** The timestamped export names its
        first input `input_ids`, and on that branch kokoro-onnx 0.5.0 sends
        `np.array([speed], dtype=np.int32)` into a graph that declares `speed` as float — so every
        call raises `InvalidArgument: Actual: (tensor(int32)), expected: (tensor(float))`.
        Measured 2026-08-26. The library is still doing the hard part above us — `phonemize` owns
        misaki's normalisation and `get_voice_style` owns the pack — and this owns the one line
        that would otherwise make the swap impossible.

        Returns `(float32 samples, durations or None, token ids)`. Batched on spaces exactly as
        the library batches, because the export asserts on a sequence longer than its context.
        """
        import numpy as np

        style_pack = np.asarray(k.get_voice_style(voice))
        chunks: list[str] = []
        for word in phonemes.split(" "):
            if chunks and len(chunks[-1]) + 1 + len(word) <= self.MAX_TOKENS:
                chunks[-1] = f"{chunks[-1]} {word}"
            else:
                chunks.append(word[: self.MAX_TOKENS])
        names = {i.name for i in k.sess.get_inputs()}
        key = "input_ids" if "input_ids" in names else "tokens"
        outs = [o.name for o in k.sess.get_outputs()]
        audio_key = "waveform" if "waveform" in outs else outs[0]

        parts, durs, all_ids = [], [], []
        for chunk in chunks:
            toks = list(k.tokenizer.tokenize(chunk))
            ids = [0, *toks, 0]
            # The style vector is indexed BY TOKEN COUNT — a (510, 1, 256) pack, not one vector.
            # Getting this wrong yields a validly-shaped vector for a different-length utterance,
            # which produces audio and no error.
            style = style_pack[len(toks)]
            if style.ndim == 1:
                style = style[None, :]
            got = k.sess.run(None, {
                key: np.array([ids], dtype=np.int64),
                "style": style.astype(np.float32),
                "speed": np.array([speed], dtype=np.float32),   # <- float32. The whole reason.
            })
            named = dict(zip(outs, got))
            parts.append(np.asarray(named[audio_key]).squeeze())
            all_ids.append(ids)
            if "durations" in named:
                durs.append(np.asarray(named["durations"]).squeeze())
        if len(parts) == 1:
            return parts[0], (durs[0] if durs else None), all_ids[0]
        # A batched piece cannot carry one duration series, because the concatenation drops the
        # pad handling between chunks. Audio still joins; timings are declined rather than
        # guessed (FR4).
        return np.concatenate(parts), None, all_ids[0]

    def voices(self) -> list:
        """Voice names inside the pack, or [] if the model cannot be loaded to ask it."""
        with self._lock:
            k = self._load()
            return sorted(k.get_voices()) if k is not None else []

    def synthesize(self, text: str, voice: str, speed: float, pause: float,
                   timings: bool = False) -> tuple[bytes, int, dict | None]:
        """Mono 16-bit PCM at Kokoro's own rate. Raises TTSError — there is no fallback path.

        Returns `(pcm, rate, schedule)`. `schedule` is None unless `timings` was asked for and the
        loaded export actually carries durations — never an estimate standing in for one (FR4).

        Sentences are synthesized separately and joined with silence, matching what the piper
        backend does, because Kokoro returns one array for the whole input and would otherwise run
        every sentence together at whatever pace the model chose. **Clause breaks are pieces too**
        (spec 008, FR3) — a comma measured 0 ms of silence against a sentence's 990 ms, so a reply
        only ever breathed where a full stop happened to fall. See `speech.segments`.

        **The speed is clamped to Kokoro's own ceiling here**, at the boundary, for the same
        reason `config.length_scale_for` contains piper's inverted unit at its boundary: the limit
        belongs to this engine and not to the project. `kokoro_onnx.create` asserts
        `speed <= 2.0`, so a persisted 2.5 — which `rate --speed` accepts, because `SPEED_MAX` is
        2.5 and piper handles it — would raise an AssertionError instead of speaking. Clamping is
        recorded on the instance, not swallowed: `available()` reports it so `status` says the
        voice is not running at the number the settings file shows.
        """
        with self._lock:
            k = self._load()
            if k is None:
                raise TTSError(f"the kokoro backend cannot start: {self.unavailable_reason}")
            asked, speed = speed, config.kokoro_speed(speed)
            self.speed_clamped_from = asked if speed != asked else None
            lang = config.kokoro_lang_for(voice)
            # Split on sentence enders AND clause breaks, keeping the punctuation on the piece
            # before the gap — Kokoro's prosody depends on it, and a fragment stripped of its
            # comma is read with a falling, finished tone. The split moved into `speech.segments`
            # so both resident backends pace from one definition.
            schedule: list[dict] = []
            aligned = True
            parts: list[bytes] = []
            rate = config.KOKORO_SR
            # Sample offset of the NEXT piece within the clip, so a word's time is measured
            # against the whole reply rather than its own sentence (FR2). `_articulate` is
            # sample-wise and `quiet` is exact, so this counter stays true to the bytes.
            cursor = 0
            for piece, gap in speech.segments(text) or [(text, 0.0)]:
                dur = ids = None
                lead = 0
                try:
                    if self._timed:
                        # The durations path. `phonemize` and the voice pack stay the library's
                        # job; only the model call is ours, and only because of the `speed` dtype.
                        phonemes = k.tokenizer.phonemize(piece, lang)
                        samples, dur, ids = self._run(k, phonemes, voice, speed)
                        # Trim the way the library does, so the PCM is unchanged — but keep WHAT
                        # it removed. A leading trim we do not subtract is a constant error on
                        # every word in the piece.
                        from kokoro_onnx.trim import trim as trim_audio
                        samples, (lead, _end) = trim_audio(samples)
                    else:
                        # THE ORIGINAL PATH, byte for byte. Without a durations-bearing export
                        # nothing about synthesis changes — which is what keeps this swap
                        # reversible by moving one file.
                        samples, rate = k.create(piece, voice=voice, speed=speed, lang=lang)
                except Exception as exc:
                    raise TTSError(f"kokoro failed on {piece[:40]!r}: {exc}") from exc
                if timings and dur is not None and ids is not None:
                    space_id = k.tokenizer.vocab.get(" ")
                    labels, ok = label_groups(piece, phonemes.split())
                    aligned = aligned and ok
                    for (a, _b), label in zip(word_offsets(ids, dur, space_id), labels):
                        schedule.append({"w": label,
                                         "t": round(max(0, cursor + a - int(lead)) / rate, 3)})
                elif timings:
                    aligned = False
                pcm = _float32_to_pcm16(_articulate(samples, rate))
                parts.append(pcm)
                cursor += len(pcm) // 2
                if gap:
                    # SAMPLES first — `quiet` does the x2 for 16-bit. Computing a BYTE count
                    # directly lands odd for some pauses and shifts every later sample by one
                    # byte, decoding the rest of the clip as broadband noise. Heard on the piper
                    # path, 2026-08-06.
                    # `gap` is 1.0 at a full stop and CLAUSE_PAUSE_RATIO at a comma, so the two
                    # can never come out equal and the reply never ends on a trailing gap.
                    n = int(rate * pause * gap)
                    parts.append(quiet(n))
                    # THE SILENCE COUNTS. Leaving it out of the cursor puts every word of every
                    # later sentence early by the sum of the gaps before it — which grows down the
                    # reply, so the first sentence looks perfect and the last is a beat ahead.
                    cursor += n
        if not parts:
            raise TTSError("kokoro produced no audio")
        # None, not an empty schedule, when this export has no durations — "I cannot report" and
        # "there is nothing to report" are different answers and a caller acts differently on
        # each (FR4). The server turns the None into `timings_unavailable` with a reason.
        report = {"words": schedule, "aligned": aligned} if (timings and self._timed) else None
        return b"".join(parts), rate, report


_KOKORO = _ResidentKokoro()


def _articulate(samples, rate: int):
    """Make consonants audible at speed. Returns float32 in [-1, 1]; a no-op when strength is 0.

    **The problem this solves, measured rather than guessed.** Asking a TTS model for faster
    speech compresses duration, and that compression falls hardest on consonants: they are already
    the quietest part of speech (measured 2026-08-14: the 20 ms after a sentence start read 64-224
    RMS against a peak of 8123, under 3% of full scale) and the shortest. Past about 1.4x, JJ
    stopped being able to hear "the first consonant of each word" — on Kokoro AND piper, on a
    male AND a female voice, at both a 0.85 s and a 0.3 s sentence pause. Four variables changed,
    the symptom did not, which is what ruled out the voice, the backend, and the gap and left the
    speed itself.

    **Why not just slow down.** He tried 1.25 and could hear everything: *"I like this speed,
    although I would like to try faster."* Intelligibility and pace were in direct conflict, so
    the fix has to buy one without spending the other.

    Two stages, both chosen because they act on exactly the band consonants occupy:

    1. **Pre-emphasis** — ``y[n] = x[n] - a*x[n-1]``, a first-order high-pass that lifts 2-8 kHz
       where stops and fricatives live, and leaves the vowel fundamental alone. This is the same
       filter speech recognizers have used as a front end for decades, for the same reason: it is
       where the phonetic information is.
    2. **Companding** — ``|x| ** e`` with e < 1, which raises quiet samples much more than loud
       ones. A consonant at 3% of full scale gains far more than a vowel at 80%, so the dynamic
       distance between them narrows without a compressor's attack/release to tune or to pump.

    Renormalized to the original peak afterwards, so this changes the BALANCE inside a clip and
    never its loudness — otherwise every reply would arrive louder than the last cue.
    """
    strength = config.consonant_boost()
    if strength <= 0:
        return samples
    import numpy as np

    x = np.asarray(samples, dtype=np.float32)
    if x.size < 2:
        return samples
    peak = float(np.max(np.abs(x))) or 1.0

    # 1. Pre-emphasis, mixed in rather than replacing the signal: full-strength pre-emphasis is
    #    thin and sibilant, and the point is to make speech clearer, not brighter.
    #    The coefficient is derived from the SAMPLE RATE rather than hardcoded, because the same
    #    0.85 that puts the corner near 1 kHz at 22.05 kHz (piper) moves it at 24 kHz (kokoro) —
    #    and a filter that shifts when you change voice engines is a filter nobody can tune.
    a = float(np.exp(-2.0 * np.pi * 1000.0 / rate))
    emphasized = np.empty_like(x)
    emphasized[0] = x[0]
    emphasized[1:] = x[1:] - a * x[:-1]
    y = (1.0 - 0.5 * strength) * x + (0.5 * strength) * emphasized

    # 2. Companding. e=1 is untouched; e=0.6 at full strength is audible but not shouty.
    exponent = 1.0 - 0.4 * strength
    y = np.sign(y) * (np.abs(y) ** exponent)

    new_peak = float(np.max(np.abs(y))) or 1.0
    return (y * (peak / new_peak)).astype(np.float32)


def _deess(pcm: bytes, rate: int) -> bytes:
    """Tame sibilance — the piercing 's' — without dulling the rest of the voice.

    Reported live 2026-08-15: *"whenever you pronounce an S, it
    sounds very high and it's harsh to listen to."* Two things make this worse here than on a
    normal TTS setup. The voice runs at **2x**, so the same number of fricatives arrive in half
    the time and the ear gets no gap to recover in; and it is played through a phone speaker on
    speakerphone, which has a presence peak in the same 5-8 kHz band the sibilance lives in.

    **The cut is SPECTRAL, and the version before it was not — that is the whole lesson here.**
    The first working de-esser detected sibilant frames correctly and then multiplied the entire
    frame by a gain. Measured, it removed 33% of the high-band energy. Listened to, it changed
    nothing: *"I didn't feel a difference."* Ducking a frame makes the 's' quieter while leaving
    its spectrum exactly as harsh, and the peak normalizer downstream then hands part of the
    level straight back. **Loudness was never the complaint. Brightness was.**

    So this attenuates the offending BINS and leaves the rest of the frame at unity:

    - Detection stays the same, because it was right: an 's' puts nearly all its energy above
      4 kHz while vowels sit below it, so the high-to-total ratio finds sibilance without needing
      to know the phoneme.
    - Attenuation is applied above 4.5 kHz only, so the 2-4 kHz band where consonant *attacks*
      live is untouched. That band is not negotiable — `_articulate` above exists because the
      owner could not hear the first consonant of each word at speed.
    - Hann window, 50% overlap, overlap-add. A rectangular window would step the spectrum at
      every frame edge and click, which reads as a bad connection rather than a filter.

    Frames that are not sibilant are reconstructed from their untouched spectrum, so the only
    thing they pick up is float round-trip noise well below the 16-bit floor.
    """
    strength = config.deess()
    if strength <= 0:
        return pcm
    try:
        import numpy as np
    except ImportError:  # numpy is optional for the sapi-only install
        return pcm

    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    # 23 ms at 22.05 kHz: long enough to resolve the 4 kHz split, short enough that a gain change
    # lands inside one phoneme rather than across two.
    size = 512
    hop = size // 2
    if x.size < size * 2:
        return pcm

    win = np.hanning(size + 1)[:size].astype(np.float32)
    starts = np.arange(0, x.size - size + 1, hop)
    frames = np.stack([x[s:s + size] for s in starts]) * win
    spec = np.fft.rfft(frames, axis=1)
    mag = np.abs(spec)

    nyq = rate / 2.0
    bins = spec.shape[1] - 1
    detect = max(1, int(4000.0 / nyq * bins))
    # Detection splits at 4 kHz, attenuation starts at 4.5 kHz. Deliberately not the same number:
    # detecting low catches the whole fricative, cutting higher spares the consonant attacks.
    cut = max(1, int(4500.0 / nyq * bins))

    ratio = mag[:, detect:].sum(axis=1) / (mag.sum(axis=1) + 1e-9)
    over = np.clip((ratio - 0.55) / 0.45, 0.0, 1.0)

    # A FLOOR UNDER THE DYNAMIC CUT, and it is here because measurement beat the design twice.
    # `en_GB-alan-medium` puts **55% of its total energy above 4 kHz** — natural speech is under
    # 20% — and that is true of every frame, not just the fricatives. Speed is not the cause:
    # 1.0x, 1.4x and 2.0x measure within a point of each other. So a de-esser that acts only on
    # the brightest frames leaves a voice that is fatiguing on all of them, which is what "I
    # didn't feel a difference" meant, twice, after two rewrites that both measured as working.
    #
    # The shelf is shallower than the sibilant cut and starts above it, so consonant attacks in
    # the 2-4.5 kHz band are untouched by either. `_articulate` exists because those attacks were
    # already too quiet once; nothing here is allowed to take them back.
    floor = 0.45 * strength
    gain = 1.0 - np.maximum((0.88 * strength) * over, floor)

    # NOTHING UP THERE, NOTHING TOUCHED. The shelf is unconditional in TIME but not in
    # FREQUENCY, so a signal with no energy above the cut has nothing to shelve — and returning
    # the caller's own bytes is a stronger statement than returning a float round-trip of them.
    # It is also the guarantee the tests actually pin: a vowel must come back identical, and
    # "identical" has to survive adding an always-on shelf or it was never a real promise.
    # 1e-3 (-60 dB), not something tighter: a Hann window's sidelobes plus int16 quantization
    # put a PURE 220 Hz tone at 1.26e-4 above a 4.5 kHz cut, so a threshold of 1e-4 would have
    # called that leakage "content" and shelved a signal that has none.
    if float(mag[:, cut:].sum()) <= 1e-3 * float(mag.sum() + 1e-9):
        return pcm

    shaped = spec.copy()
    # Ramp the cut in over ~1 kHz instead of a brick wall: an abrupt spectral edge rings in the
    # time domain and adds its own artefact where the point was to remove one.
    ramp_end = min(spec.shape[1], cut + max(1, int(1000.0 / nyq * bins)))
    ramp = np.linspace(0.0, 1.0, ramp_end - cut, dtype=np.float32) if ramp_end > cut else None
    for i, g in enumerate(gain):
        if g >= 1.0:
            continue
        if ramp is not None:
            shaped[i, cut:ramp_end] *= (1.0 - ramp * (1.0 - g))
        shaped[i, ramp_end:] *= g

    out = np.zeros(x.size, dtype=np.float32)
    norm = np.zeros(x.size, dtype=np.float32)
    rebuilt = np.fft.irfft(shaped, n=size, axis=1).astype(np.float32) * win
    for i, s in enumerate(starts):
        out[s:s + size] += rebuilt[i]
        norm[s:s + size] += win * win
    # Hann-squared at 50% overlap sums to a constant across the interior but tapers at both ends,
    # so divide by the actual window sum rather than assuming it. Below the guard the frames never
    # covered the sample at all; keep the original there instead of fading to silence.
    covered = norm > 1e-6
    out[covered] /= norm[covered]
    out[~covered] = x[~covered]

    return (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def _float32_to_pcm16(samples) -> bytes:
    """Kokoro returns float32 in [-1, 1]; every sink in this tool speaks 16-bit PCM.

    Clipped before scaling rather than after: a model overshoot past 1.0 would otherwise wrap
    around int16 and present as a loud click, which is exactly the kind of artefact that gets
    blamed on Bluetooth.
    """
    import array

    out = array.array("h", (int(max(-1.0, min(1.0, float(s))) * 32767) for s in samples))
    return out.tobytes()


def warm() -> dict:
    """Load the voice model NOW, so the first reply is not the slow one.

    Called at `serve` time on a background thread. Without it the ~4 s model load lands on
    whatever the agent says first, which is the worst possible place for it: the user has just
    spoken and is waiting to find out whether the thing works at all.
    """
    backend = config.tts_backend()
    if backend == "kokoro":
        voice = config.kokoro_voice()
        with _KOKORO._lock:
            ok = _KOKORO._load() is not None
        return {
            "warmed": ok,
            "voice": voice,
            "reason": _KOKORO.unavailable_reason if not ok else None,
        }
    if backend != "piper" or not config.piper_inprocess():
        return {"warmed": False, "reason": "not using a resident backend"}
    voice = config.piper_voice()
    if not voice:
        return {"warmed": False, "reason": "no voice is configured"}
    with _RESIDENT._lock:
        ok = _RESIDENT._load(voice) is not None
    return {
        "warmed": ok,
        "voice": os.path.basename(voice),
        "reason": _RESIDENT.unavailable_reason if not ok else None,
    }


def _piper_paths(voice_path: str | None) -> tuple[str, str]:
    """Resolve (binary, voice), raising a TTSError that names the remedy if either is missing.

    Resolution lives in config: the binary is findable in the repo venv and the voice in the
    models dir, so `COMMAND_BRIDGE_TTS=piper` is the ONLY setting a piper session needs. Requiring all three
    on every call is what produced the wall of env-var prefixes this design exists to delete.
    """
    binary = config.piper_bin()
    voice = voice_path or config.piper_voice()
    # The binary is only needed for the subprocess path; a resident voice needs the .onnx alone.
    need_binary = not (config.piper_inprocess() and voice)
    if (need_binary and not binary) or not voice:
        missing = " and ".join(
            [n for n, v in (("a piper binary", binary if need_binary else "-"),
                            ("an .onnx voice", voice)) if not v]
        )
        raise TTSError(
            f"the piper backend cannot start: no {missing}. Run `command-bridge doctor` for the exact "
            f"remedy, or set it explicitly: `command-bridge config set COMMAND_BRIDGE_PIPER_BIN <path>` / "
            f"`command-bridge config set COMMAND_BRIDGE_PIPER_VOICE <path>` (see `command-bridge voices`)."
        )
    return binary, voice


def _synth_piper(text: str, voice_path: str | None = None,
                 speed: float | None = None,
                 pause: float | None = None) -> tuple[bytes, int]:
    binary, voice = _piper_paths(voice_path)
    # THE ONE PLACE the inversion happens. Everything above here speaks SPEED (higher is faster);
    # piper wants length_scale (lower is faster). See config.length_scale_for.
    length_scale = config.length_scale_for(
        config.speech_speed() if speed is None else speed
    )
    pause = config.sentence_pause() if pause is None else pause

    if config.piper_inprocess():
        result = _RESIDENT.synthesize(text, voice, length_scale, pause)
        if result is not None:
            return result
        # Fell through: the resident path is unusable, so spawn. Not silent — `available()`
        # reports the reason, because "piper is mysteriously slow again" is exactly the symptom
        # this would otherwise present as.

    if not binary:
        raise TTSError(
            f"piper cannot run in-process ({_RESIDENT.unavailable_reason}) and no piper binary "
            f"was found to fall back to. Run `command-bridge doctor`."
        )
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        proc = subprocess.run(
            [
                binary, "--model", voice, "--output_file", path,
                "--length-scale", str(length_scale),
                # The pause between sentences is what makes a spoken list parseable — speech
                # has no scrollback, so the boundary has to be audible.
                "--sentence-silence", str(pause),
            ],
            input=text,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise TTSError(f"piper failed: {proc.stderr.strip()[:400]}")
        return _read_wav_mono16(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _synth_none(text: str) -> tuple[bytes, int]:
    """Silence roughly as long as the text would take to say (~14 chars/second)."""
    seconds = max(0.4, len(text) / 14.0)
    return b"\x00\x00" * int(config.TTS_SR * seconds), config.TTS_SR


def synthesize(
    text: str, backend: str | None = None, voice: str | None = None,
    speed: float | None = None, pause: float | None = None,
) -> tuple[bytes, int]:
    """Return `(padded mono 16-bit PCM, sample_rate)`. Raises TTSError on failure.

    The two-value contract is deliberate and unchanged: five test files and `pronounce` call this,
    and a word schedule is of no use to any of them. :func:`synthesize_timed` is the same call
    with the schedule attached.
    """
    pcm, sr, _ = synthesize_timed(text, backend, voice, speed, pause)
    return pcm, sr


def synthesize_timed(
    text: str, backend: str | None = None, voice: str | None = None,
    speed: float | None = None, pause: float | None = None, timings: bool = False,
) -> tuple[bytes, int, dict | None]:
    """Return `(padded mono 16-bit PCM, sample_rate, schedule)`. Raises TTSError on failure.

    `schedule` is `{"words": [{"w", "t"}, ...], "aligned": bool}` when `timings` is asked for and
    the engine can answer, and **None when it cannot** — an engine without per-token durations
    declines rather than estimating (FR4).

    `voice` is a NAME from :func:`list_voices`, not a path — see :func:`resolve_voice`.

    `speed` is a MULTIPLE OF NATIVE PACE: higher is faster. It is deliberately not piper's
    `length_scale`, which is inverted — that inversion leaked out once already and produced half
    speed when the owner asked for double.

    **NORMALISATION HAPPENS HERE, ABOVE THE BACKEND DISPATCH** (spec 008, FR1/FR5). This is the
    one place every backend goes through, and both installed engines were measured DROPPING the
    dot in a technical term — `0.2.6` came back as "026" from piper and from kokoro alike — so
    the fix belongs on the path rather than in one engine or, worse, in every caller. Asking each
    agent to spell out its own version numbers is the same class of rule spec 007 exists to stop
    relying on. `command-bridge pronounce` calls the same function, so what it prints is what the
    engine is handed.
    """
    if not text or not text.strip():
        raise TTSError("nothing to speak")
    # After the emptiness check, so "nothing to speak" still describes the caller's own input.
    text = speech.normalize_for_speech(text)
    backend = (backend or config.tts_backend()).lower()
    # `sr`, not `rate`: the returned SAMPLE rate would shadow a speech-rate name — a trap that
    # would silently ignore the caller's speed the moment anyone reordered these lines.
    schedule = None
    if backend == "sapi":
        pcm, sr = _synth_sapi(text)
    elif backend == "piper":
        pcm, sr = _synth_piper(text, resolve_voice(voice), speed=speed, pause=pause)
    elif backend == "kokoro":
        pcm, sr, schedule = _KOKORO.synthesize(
            text,
            _resolve_kokoro_voice(voice),
            # Kokoro's `speed` is already a multiple where higher is faster, so it takes the
            # caller's value unchanged. No length_scale inversion exists on this path, and none
            # should be added — that inversion is the bug config.length_scale_for was written to
            # contain, and it belongs to piper alone.
            speed=config.speech_speed() if speed is None else speed,
            pause=config.sentence_pause() if pause is None else pause,
            timings=timings,
        )
    elif backend == "none":
        pcm, sr = _synth_none(text)
    else:
        raise TTSError(f"unknown TTS backend: {backend!r} (sapi|piper|kokoro|none)")
    # AFTER every backend and BEFORE normalize, so one setting covers piper, kokoro and sapi
    # alike and the gain it removes is not immediately handed back by the normalizer.
    pcm = _deess(pcm, sr)
    # `pad` PREPENDS silence so a Bluetooth sink has time to wake, and that silence is part of the
    # clip the caller will time against. Every word moves by it. Missing this would put the whole
    # schedule 100 ms early — small enough to look like tuning and wrong on every single word.
    if schedule is not None:
        lead = int(sr * config.CHIME_LEADING_SILENCE_S) / sr
        schedule = {**schedule,
                    "words": [{**w, "t": round(w["t"] + lead, 3)} for w in schedule["words"]]}
    return pad(normalize(pcm), sr), sr, schedule


def write_wav(path: str, pcm: bytes, sample_rate: int) -> str:
    """Write mono 16-bit PCM to a WAV file. Used by the e2e harness to build a fake mic."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return path


def available() -> str:
    """Which backend would actually work right now, and how fast — for `status` / `describe`.

    Says WHICH piper path is live, not just "piper". A silent fall back from the resident voice
    to spawning the binary is a 20x latency regression whose only symptom is "it feels slow
    again", so it has to be visible in the one place someone already looks.
    """
    backend = config.tts_backend()
    if backend == "sapi" and os.name == "nt":
        return "sapi"
    if backend == "piper":
        voice = config.piper_voice()
        binary = config.piper_bin()
        # The voice matters more than the binary now: resident synthesis needs only the .onnx.
        # A status line saying "piper" while synthesis was impossible sent you to the network
        # layer once already, so both halves are still checked — just per path.
        if config.piper_inprocess() and voice:
            if _RESIDENT.unavailable_reason:
                return (f"piper (spawning per call — resident load failed: "
                        f"{_RESIDENT.unavailable_reason})")
            # "not yet loaded" only ever meant "this is a CLI process, not the server". Every CLI
            # invocation is a fresh interpreter that will never load a voice, so the phrase was
            # tautologically true there and read as a warning — an audit reported `voices` saying
            # it after piper had demonstrably synthesized, in the server, seconds earlier. The
            # load state belongs to whoever is holding the model; `status` is where to ask.
            return "piper (resident)"
        if binary and voice:
            return "piper (per-call subprocess)"
    if backend == "kokoro":
        # Kokoro has NO subprocess fallback, so unlike piper there is no degraded-but-working
        # state to report — it either holds the model or it says nothing at all. That makes the
        # failure reason the whole message: a bare "kokoro (unavailable)" would send someone to
        # the network layer for what is usually one missing file.
        if _KOKORO.unavailable_reason:
            return f"kokoro (unavailable — {_KOKORO.unavailable_reason})"
        if config.kokoro_model() and config.kokoro_voices_bin():
            # THE CLAMP IS SAID OUT LOUD, and said from CONFIG rather than from what a past
            # synthesis happened to do. A CLI process has never spoken, so an instance flag alone
            # would report nothing in the one place someone looks before starting a session —
            # and the whole point is to warn that the persisted speed is not the speed that will
            # be used, BEFORE the first reply rather than after it.
            asked = config.speech_speed()
            if asked > config.KOKORO_SPEED_MAX:
                return (f"kokoro (resident, {config.kokoro_voice()}; speed clamped "
                        f"{asked}→{config.KOKORO_SPEED_MAX} — kokoro's own ceiling, "
                        f"COMMAND_BRIDGE_SPEECH_SPEED asks for more)")
            return f"kokoro (resident, {config.kokoro_voice()})"
        missing = " and ".join(
            n for n, p in (("kokoro-v1.0.onnx", config.kokoro_model()),
                           ("voices-v1.0.bin", config.kokoro_voices_bin())) if not p
        )
        return f"kokoro (unavailable — missing {missing})"
    if backend == "none":
        return "none"
    return f"{backend} (unavailable)"
