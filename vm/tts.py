"""vm.tts — text to speech, pluggable backend.

Backends:
  * ``sapi``  — Windows System.Speech via PowerShell. Zero install, fully offline, present on
                every Windows box. The default, so a fresh clone speaks without downloading a
                model.
  * ``piper`` — the documented upgrade path. Better voices, needs a binary + an .onnx voice.
  * ``none``  — silence of the right duration. For tests that care about plumbing, not audio.

Every backend's output goes through :func:`pad`, which is not cosmetic: Bluetooth sinks power
down between clips and swallow the first ~100 ms. This project is phone-first, so nearly every
session is Bluetooth, and unpadded audio presents as "the TTS is broken".

Output is always mono 16-bit PCM at :data:`vm.config.TTS_SR`.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import wave
from typing import List, Optional, Tuple

from . import config


class TTSError(RuntimeError):
    pass


def pad(pcm: bytes, sample_rate: int) -> bytes:
    """Prepend/append silence so a Bluetooth sink has time to wake and doesn't clip the tail."""
    lead = b"\x00\x00" * int(sample_rate * config.CHIME_LEADING_SILENCE_S)
    trail = b"\x00\x00" * int(sample_rate * config.CHIME_TRAILING_SILENCE_S)
    return lead + pcm + trail


def _read_wav_mono16(path: str) -> Tuple[bytes, int]:
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


def _synth_sapi(text: str) -> Tuple[bytes, int]:
    """Speak via Windows System.Speech into a temp WAV, then read it back.

    Goes through a file rather than a stream because SAPI's streaming API is COM-bound and
    this keeps the whole backend to one subprocess call with no pywin32 dependency.
    """
    if os.name != "nt":
        raise TTSError("the sapi backend requires Windows; set VM_TTS=piper or none")
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
    voiceprint gallery's speaker model out of `vm voices` — it was being offered as a selectable
    voice that then failed at synthesis time.
    """
    return config.piper_voices()


def resolve_voice(name: Optional[str]) -> Optional[str]:
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


def _synth_piper(text: str, voice_path: Optional[str] = None,
                 rate: Optional[float] = None) -> Tuple[bytes, int]:
    # Resolution moved into config: the binary is findable in the repo venv and the voice is
    # findable in the models dir, so `VM_TTS=piper` is now the ONLY setting a piper session
    # needs. Requiring all three to be exported on every call is what produced the wall of
    # env-var prefixes this refactor exists to delete.
    binary = config.piper_bin()
    voice = voice_path or config.piper_voice()
    if not binary or not voice:
        missing = " and ".join(
            [n for n, v in (("a piper binary", binary), ("an .onnx voice", voice)) if not v]
        )
        raise TTSError(
            f"the piper backend cannot start: no {missing}. Run `vm doctor` for the exact "
            f"remedy, or set it explicitly: `vm config set VM_PIPER_BIN <path>` / "
            f"`vm config set VM_PIPER_VOICE <path>` (see `vm voices`)."
        )
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        if rate is not None:
            length_scale = rate
        else:
            try:
                length_scale = float(
                    os.environ.get("VM_PIPER_LENGTH_SCALE") or config.PIPER_LENGTH_SCALE
                )
            except ValueError:
                length_scale = config.PIPER_LENGTH_SCALE
        proc = subprocess.run(
            [
                binary, "--model", voice, "--output_file", path,
                "--length-scale", str(length_scale),
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


def _synth_none(text: str) -> Tuple[bytes, int]:
    """Silence roughly as long as the text would take to say (~14 chars/second)."""
    seconds = max(0.4, len(text) / 14.0)
    return b"\x00\x00" * int(config.TTS_SR * seconds), config.TTS_SR


def synthesize(
    text: str, backend: str | None = None, voice: Optional[str] = None,
    rate: Optional[float] = None,
) -> Tuple[bytes, int]:
    """Return `(padded mono 16-bit PCM, sample_rate)`. Raises TTSError on failure.

    `voice` is a NAME from :func:`list_voices`, not a path — see :func:`resolve_voice`.
    """
    if not text or not text.strip():
        raise TTSError("nothing to speak")
    backend = (backend or config.tts_backend()).lower()
    # `sr`, not `rate`: `rate` is the SPEECH rate parameter and reusing the name for the
    # returned SAMPLE rate shadows it — a trap that would silently ignore the caller's speed
    # the moment anyone reordered these lines.
    if backend == "sapi":
        pcm, sr = _synth_sapi(text)
    elif backend == "piper":
        pcm, sr = _synth_piper(text, resolve_voice(voice), rate=rate)
    elif backend == "none":
        pcm, sr = _synth_none(text)
    else:
        raise TTSError(f"unknown TTS backend: {backend!r} (sapi|piper|none)")
    return pad(pcm, sr), sr


def write_wav(path: str, pcm: bytes, sample_rate: int) -> str:
    """Write mono 16-bit PCM to a WAV file. Used by the e2e harness to build a fake mic."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return path


def available() -> str:
    """Which backend would actually work right now — for `status` / `describe`."""
    backend = config.tts_backend()
    if backend == "sapi" and os.name == "nt":
        return "sapi"
    # Both halves, not just the binary: piper with no voice fails at the first `say`, and a
    # status line that said "piper" while synthesis was impossible sent you looking at the
    # network layer.
    if backend == "piper" and config.piper_bin() and config.piper_voice():
        return "piper"
    if backend == "none":
        return "none"
    return f"{backend} (unavailable)"
