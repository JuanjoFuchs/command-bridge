"""voice_tunnel.tts — text to speech, pluggable backend.

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
audio — :data:`voice_tunnel.config.TTS_SR` for SAPI and piper, 24 kHz for kokoro. Assuming one
global rate here would either resample kokoro down for nothing or play it 9% slow.
"""
from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import threading
import wave

from . import config


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
        raise TTSError("the sapi backend requires Windows; set VOICE_TUNNEL_TTS=piper or none")
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
    keeps the voiceprint gallery's speaker model out of `voice-tunnel voices` — it was being
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
            for i, chunk in enumerate(voice.synthesize(text, syn)):
                if i:
                    parts.append(silence)
                parts.append(chunk.audio_int16_bytes)
        if not parts:
            raise TTSError("piper produced no audio")
        return b"".join(parts), sample_rate


_RESIDENT = _ResidentVoice()


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

    @property
    def loaded(self) -> bool:
        return self._kokoro is not None

    def _load(self):
        if self.unavailable_reason:
            return None
        if self._kokoro is not None:
            return self._kokoro
        model, voices = config.kokoro_model(), config.kokoro_voices_bin()
        # Named separately: having the model without the pack is the state `download` exists to
        # prevent, and "kokoro is broken" is a useless thing to tell someone who is missing one file.
        missing = [n for n, p in (("kokoro-v1.0.onnx", model), ("voices-v1.0.bin", voices)) if not p]
        if missing:
            self.unavailable_reason = (
                f"{' and '.join(missing)} not in {config.models_dir()} — run "
                f"`voice-tunnel download kokoro`"
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
        return self._kokoro

    def voices(self) -> list:
        """Voice names inside the pack, or [] if the model cannot be loaded to ask it."""
        with self._lock:
            k = self._load()
            return sorted(k.get_voices()) if k is not None else []

    def synthesize(self, text: str, voice: str, speed: float, pause: float) -> tuple[bytes, int]:
        """Mono 16-bit PCM at Kokoro's own rate. Raises TTSError — there is no fallback path.

        Sentences are synthesized separately and joined with silence, matching what the piper
        backend does, because Kokoro returns one array for the whole input and would otherwise run
        every sentence together at whatever pace the model chose.
        """
        import re

        with self._lock:
            k = self._load()
            if k is None:
                raise TTSError(f"the kokoro backend cannot start: {self.unavailable_reason}")
            lang = config.kokoro_lang_for(voice)
            # Split on sentence enders, keeping the punctuation — Kokoro's prosody depends on it.
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
            parts: list[bytes] = []
            rate = config.KOKORO_SR
            for i, sentence in enumerate(sentences or [text]):
                try:
                    samples, rate = k.create(sentence, voice=voice, speed=speed, lang=lang)
                except Exception as exc:
                    raise TTSError(f"kokoro failed on {sentence[:40]!r}: {exc}") from exc
                if i:
                    # SAMPLES first — `quiet` does the x2 for 16-bit. Computing a BYTE count
                    # directly lands odd for some pauses and shifts every later sample by one
                    # byte, decoding the rest of the clip as broadband noise. Heard on the piper
                    # path, 2026-08-06.
                    parts.append(quiet(int(rate * pause)))
                parts.append(_float32_to_pcm16(_articulate(samples, rate)))
        if not parts:
            raise TTSError("kokoro produced no audio")
        return b"".join(parts), rate


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
    models dir, so `VOICE_TUNNEL_TTS=piper` is the ONLY setting a piper session needs. Requiring all three
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
            f"the piper backend cannot start: no {missing}. Run `voice-tunnel doctor` for the exact "
            f"remedy, or set it explicitly: `voice-tunnel config set VOICE_TUNNEL_PIPER_BIN <path>` / "
            f"`voice-tunnel config set VOICE_TUNNEL_PIPER_VOICE <path>` (see `voice-tunnel voices`)."
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
            f"was found to fall back to. Run `voice-tunnel doctor`."
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

    `voice` is a NAME from :func:`list_voices`, not a path — see :func:`resolve_voice`.

    `speed` is a MULTIPLE OF NATIVE PACE: higher is faster. It is deliberately not piper's
    `length_scale`, which is inverted — that inversion leaked out once already and produced half
    speed when the owner asked for double.
    """
    if not text or not text.strip():
        raise TTSError("nothing to speak")
    backend = (backend or config.tts_backend()).lower()
    # `sr`, not `rate`: the returned SAMPLE rate would shadow a speech-rate name — a trap that
    # would silently ignore the caller's speed the moment anyone reordered these lines.
    if backend == "sapi":
        pcm, sr = _synth_sapi(text)
    elif backend == "piper":
        pcm, sr = _synth_piper(text, resolve_voice(voice), speed=speed, pause=pause)
    elif backend == "kokoro":
        pcm, sr = _KOKORO.synthesize(
            text,
            _resolve_kokoro_voice(voice),
            # Kokoro's `speed` is already a multiple where higher is faster, so it takes the
            # caller's value unchanged. No length_scale inversion exists on this path, and none
            # should be added — that inversion is the bug config.length_scale_for was written to
            # contain, and it belongs to piper alone.
            speed=config.speech_speed() if speed is None else speed,
            pause=config.sentence_pause() if pause is None else pause,
        )
    elif backend == "none":
        pcm, sr = _synth_none(text)
    else:
        raise TTSError(f"unknown TTS backend: {backend!r} (sapi|piper|kokoro|none)")
    return pad(normalize(pcm), sr), sr


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
            return f"kokoro (resident, {config.kokoro_voice()})"
        missing = " and ".join(
            n for n, p in (("kokoro-v1.0.onnx", config.kokoro_model()),
                           ("voices-v1.0.bin", config.kokoro_voices_bin())) if not p
        )
        return f"kokoro (unavailable — missing {missing})"
    if backend == "none":
        return "none"
    return f"{backend} (unavailable)"
