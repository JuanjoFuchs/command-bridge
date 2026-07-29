"""vm.config — every tunable in one place, each with the reason it has that value.

Config-as-data (AGENTS.md convention 5): a number scattered in code is a number nobody dares
change. A number here with its rationale is one an operator can reason about.

STDLIB ONLY — imported by everything, including the pure-logic modules under test.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------- audio format

TARGET_SR = 16000
"""ASR sample rate. Whisper resamples internally anyway; doing it once on ingest keeps the
hot path allocation-free."""

CLIENT_FRAME_MS = 40
"""How much audio the browser batches per WebSocket message. 40 ms balances two failure modes:
smaller floods the socket with per-message overhead, larger adds latency to wake detection."""

# ------------------------------------------------------------------ silence/VAD

SILENCE_RMS_FLOOR = 0.005
"""~-46 dBFS on float32 in [-1,1]. Buffers quieter than this are never sent to Whisper at all.
This is a hard guarantee, not an optimization: Whisper hallucinates confident text on silence
("you", "Thank you.") and an energy gate is the only thing that makes a silent room produce
zero turns (AC-1)."""

END_OF_UTTERANCE_MS = 1000
"""Silence that ends a turn.

Briefly lowered to 700 ms to cut latency, then reverted. At 700 ms a single continuous thought
**fragmented into five separate turns** — JJ, live, 2026-07-29: "...I don't like the voice much.
Um I like um" / "Voices that have a lower register." / "And do I need to say every time?" —
because filler-and-pause is normal speech, not the end of a sentence.

Nothing was lost (every fragment reached the log, which is the cursor contract working), but
fragmentation is still a real cost: it hands the agent four partial thoughts to reassemble
instead of one, and an agent that answers the first fragment answers the wrong question.

Keep it at 1000. The latency that actually mattered was the ASR — Parakeet cut transcription
from 2.64 s to 0.23 s. This second buys the speaker room to think, and spending it is the right
trade. VoiceMode independently landed on the same value."""

MIN_UTTERANCE_MS = 500
"""Discard anything shorter. Rejects coughs, clicks, and door slams that clear the energy gate
but carry no speech."""

INITIAL_GRACE_MS = 1000
"""Grace period after the session opens before end-of-utterance logic can fire, so the very
first turn is not closed before the speaker starts."""

MAX_UTTERANCE_MS = 120_000
"""Hard ceiling on a single turn. A stuck VAD must not buffer forever."""

# -------------------------------------------------------------------- wake word

WAKE_PHRASES = ("hey claude", "hi claude", "ok claude", "okay claude", "claude")
"""Matched case-insensitively against the transcript, longest first. Text-level matching rather
than an acoustic wake model: the ASR already runs on every buffer, so this needs no extra
dependency, no model download, and no per-phrase training. The cost is that gating happens
after transcription, which is fine because we are not trying to save power."""

CONVERSATION_WINDOW_S = 30.0
"""After an addressed turn, further turns stay addressed for this long (AC-6). Without it a
back-and-forth requires saying the wake phrase every single time, which is the fastest way to
make a voice assistant feel hostile."""

# ------------------------------------------------------------------------- TTS

CHIME_LEADING_SILENCE_S = 0.1
"""Bluetooth sinks power down between clips and swallow the first ~100 ms. Pad the head so the
sink is awake before speech starts. Learned from VoiceMode; this project is phone-first so
essentially every session is Bluetooth."""

CHIME_TRAILING_SILENCE_S = 0.2
"""Pad the tail for the same reason — without it the sink cuts the final syllable."""

TTS_SR = 22050
"""Output rate for synthesized audio. SAPI and Piper both produce this comfortably."""

# ------------------------------------------------------------------ networking

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

LOOPBACK_CIDRS = ("127.0.0.0/8", "::1/128")
"""Always allowed. Everything else is opt-in."""

TAILSCALE_CIDRS = ("100.64.0.0/10",)
"""The CGNAT range covering any tailnet. Only routable inside your own tailnet, which is what
makes it a reasonable thing to allow for phone access."""

# --------------------------------------------------------------- env overrides


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def session_dir() -> str:
    """Where turn logs live. Repo-local by default so a dev run leaves no trace in $HOME."""
    return _env("VM_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions"
    )


def models_dir() -> str:
    """Where downloaded models live (Piper voices, Parakeet). Gitignored — models are
    downloaded, never vendored."""
    return _env("VM_MODELS_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models"
    )


def parakeet_dir() -> str:
    """Path to a sherpa-onnx Parakeet TDT model directory, if present.

    Parakeet is the default engine when the model is on disk. Benchmarked on this machine over
    the same 3 s utterance, all producing identical text:

        whisper small.en   2.64 s  (RTF 0.88)
        whisper base.en    0.86 s  (RTF 0.28)
        parakeet-tdt-0.6b  0.34 s  (RTF 0.115)   <- default when present

    8x faster than small.en from a *larger*, more accurate model — NVIDIA's TDT transducer is
    simply a better architecture for this than an encoder-decoder that must generate tokens
    autoregressively. Whisper stays as the fallback so a fresh clone still works with nothing
    downloaded beyond faster-whisper's own model.
    """
    explicit = _env("VM_PARAKEET_DIR")
    if explicit:
        return explicit
    default = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "models",
        "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
    )
    return default if os.path.isdir(default) else ""


def asr_engine() -> str:
    """`parakeet` when its model is present, else `whisper`. Force with VM_ASR."""
    forced = _env("VM_ASR").lower()
    if forced in ("parakeet", "whisper"):
        return forced
    return "parakeet" if parakeet_dir() else "whisper"


def asr_threads() -> int:
    try:
        return max(1, int(_env("VM_ASR_THREADS", "4")))
    except ValueError:
        return 4


def whisper_model() -> str:
    """base.en, not small.en. Benchmarked on this machine over a 3 s utterance:

        small.en  2.64 s  (RTF 0.88)
        base.en   0.86 s  (RTF 0.28)   <- default
        tiny.en   0.43 s  (RTF 0.14)

    All three transcribed the test utterance identically, so small.en was buying nothing but
    latency. Drop to tiny.en if you want faster still and can accept more errors on hard audio;
    raise to small.en only if base starts making mistakes that matter.
    """
    return _env("VM_WHISPER_MODEL", "base.en")


def asr_beam_size() -> int:
    """Beam width. Benchmarked as near-irrelevant to speed here (0.88 vs 0.92 RTF) — model
    size dominates by ~3x — so this stays at 1 rather than being a tuning knob anyone reaches
    for expecting a win."""
    try:
        return max(1, int(_env("VM_ASR_BEAM", "1")))
    except ValueError:
        return 1


def tts_backend() -> str:
    return _env("VM_TTS", "sapi").lower()


def extra_allow_cidrs() -> tuple[str, ...]:
    raw = _env("VM_ALLOW_CIDRS")
    return tuple(c.strip() for c in raw.split(",") if c.strip())


def trusted_proxies() -> tuple[str, ...]:
    """Empty by default — see ai-docs/reference/security.md. An empty tuple means
    X-Forwarded-For is ignored entirely and the direct TCP peer decides."""
    raw = _env("VM_TRUSTED_PROXIES")
    return tuple(c.strip() for c in raw.split(",") if c.strip())
