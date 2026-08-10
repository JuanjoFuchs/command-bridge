"""voice_tunnel.tts — text to speech, pluggable backend.

Backends:
  * ``sapi``  — Windows System.Speech via PowerShell. Zero install, fully offline, present on
                every Windows box. The default, so a fresh clone speaks without downloading a
                model.
  * ``piper`` — the documented upgrade path. Better voices, needs a binary + an .onnx voice.
  * ``none``  — silence of the right duration. For tests that care about plumbing, not audio.

Every backend's output goes through :func:`pad`, which is not cosmetic: Bluetooth sinks power
down between clips and swallow the first ~100 ms. This project is phone-first, so nearly every
session is Bluetooth, and unpadded audio presents as "the TTS is broken".

Output is always mono 16-bit PCM at :data:`voice_tunnel.config.TTS_SR`.
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


def pad(pcm: bytes, sample_rate: int) -> bytes:
    """Prepend/append silence so a Bluetooth sink has time to wake and doesn't clip the tail."""
    lead = b"\x00\x00" * int(sample_rate * config.CHIME_LEADING_SILENCE_S)
    trail = b"\x00\x00" * int(sample_rate * config.CHIME_TRAILING_SILENCE_S)
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
    """Piper voices available in the models dir, by name.

    Delegates to config so "what is installed" has one definition (AGENTS.md convention 5). The
    listing there also requires each `.onnx` to have its sidecar `.onnx.json`, which keeps the
    voiceprint gallery's speaker model out of `voice-tunnel voices` — it was being offered as a selectable
    voice that then failed at synthesis time.
    """
    return config.piper_voices()


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
            silence = bytes(int(sample_rate * pause) * 2)
            parts = []
            for i, chunk in enumerate(voice.synthesize(text, syn)):
                if i:
                    parts.append(silence)
                parts.append(chunk.audio_int16_bytes)
        if not parts:
            raise TTSError("piper produced no audio")
        return b"".join(parts), sample_rate


_RESIDENT = _ResidentVoice()


def warm() -> dict:
    """Load the voice model NOW, so the first reply is not the slow one.

    Called at `serve` time on a background thread. Without it the ~4 s model load lands on
    whatever the agent says first, which is the worst possible place for it: the user has just
    spoken and is waiting to find out whether the thing works at all.
    """
    if config.tts_backend() != "piper" or not config.piper_inprocess():
        return {"warmed": False, "reason": "not using the resident piper backend"}
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
    elif backend == "none":
        pcm, sr = _synth_none(text)
    else:
        raise TTSError(f"unknown TTS backend: {backend!r} (sapi|piper|none)")
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
    if backend == "none":
        return "none"
    return f"{backend} (unavailable)"
