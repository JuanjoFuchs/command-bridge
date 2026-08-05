"""voice_tunnel.config — every tunable in one place, each with the reason it has that value.

Config-as-data (AGENTS.md convention 5): a number scattered in code is a number nobody dares
change. A number here with its rationale is one an operator can reason about.

STDLIB ONLY — imported by everything, including the pure-logic modules under test.
"""
from __future__ import annotations

import os
import re
import shutil
import sys

# ------------------------------------------------------------------ repo root

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
"""Absolute path to the directory containing the package, derived from THIS FILE, never the cwd.

An agent invokes `voice-tunnel` from wherever its own turn happens to be standing, so any default
resolved against `os.getcwd()` would scatter turn logs and settings across whatever directories
the caller passed through — the exact class of bug that makes a tool "work on my machine" and
nowhere else.

In a source checkout this IS the repo root. **After `pip install` it is `site-packages`**, which
is why nothing may hang off it unconditionally — see `_in_source_checkout`."""


def _in_source_checkout() -> bool:
    """Is this running from a git checkout, or from an installed package?

    **This distinction is the whole reason the tool is installable.** Every runtime path used to
    hang off ROOT — `<root>/.env`, `<root>/sessions`, `<root>/models`. That is correct in a
    checkout and actively wrong once installed, where ROOT is `site-packages`: turn logs and a
    600 MB ASR model would be written into the interpreter's library directory, to be silently
    destroyed by the next `pip install --upgrade` and to fail outright wherever site-packages is
    read-only (a system Python, a container, any managed environment).

    `pyproject.toml` is the marker because it sits beside the package in a checkout and is never
    installed to the site-packages root. `.git` alone would miss an sdist or a zip download.
    """
    return (
        os.path.isfile(os.path.join(ROOT, "pyproject.toml"))
        or os.path.isdir(os.path.join(ROOT, ".git"))
    )


def _user_dir(kind: str) -> str:
    """Per-user directory for `kind` ("config" or "data"), following each platform's convention.

    Conventions rather than one invented location, because these are directories a person has to
    find later — to read a transcript, to delete a voiceprint, to back something up. A tool that
    invents its own home makes every one of those a support question.

        Windows   %LOCALAPPDATA%\\voice-tunnel        (Local, not Roaming: models are large and
                                                       must not follow a roaming profile onto a
                                                       network share)
        macOS     ~/Library/Application Support/voice-tunnel
        Linux     $XDG_CONFIG_HOME | $XDG_DATA_HOME, else ~/.config | ~/.local/share
    """
    home = os.path.expanduser("~")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif kind == "config":
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "voice-tunnel")


def _default_path(name: str, kind: str = "data") -> str:
    """`<repo>/<name>` in a checkout, `<user dir>/<name>` once installed.

    A checkout keeps its state local so a dev run leaves nothing behind in $HOME and two working
    trees cannot fight over one turn log — the original rationale, still true. An installed copy
    has no repo to be local to.
    """
    base = ROOT if _in_source_checkout() else _user_dir(kind)
    return os.path.join(base, name)


def _int_env(name: str, default: int) -> int:
    """An int tunable overridable at runtime, so pacing can be tuned by feel in a live session
    instead of requiring a code edit and a restart of the conversation."""
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


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

NOISE_MARGIN = 3.0
"""How far above the measured room noise a window must be to count as speech.

A FIXED silence threshold assumes a quiet room, and JJ's is not one. Live 2026-07-31: with the
air conditioning running, its white noise sat above SILENCE_RMS_FLOOR, so trailing silence never
accumulated, the utterance never ended, and he had to MUTE HIS MICROPHONE to get a turn to close
— "I had finished speaking a while ago and I had to mute the microphone for you to pick up what
I said and not pick up the white noise".

So the floor is measured, not assumed: a window counts as speech only above
`max(SILENCE_RMS_FLOOR, noise_floor * NOISE_MARGIN)`. 3x is roughly 10 dB over ambient, which
separates speech from a fan or an AC while staying reachable by a quiet voice."""

NOISE_WINDOW_S = 8.0
NOISE_PERCENTILE = 20
"""The noise floor is the Nth percentile of the last NOISE_WINDOW_S of frame energies.

**A min-tracker was tried first and failed in the room.** It snapped DOWN to the quietest window
ever seen, so after one near-silent moment the AC sat ~24x above the latched floor and read as
speech indefinitely; the upward creep recovered under 2x per minute. JJ, live 2026-07-31:
"I finished speaking a while ago on this new turn, and the white noise kept running, and you
didn't recognize the stop" — a 63.7 second turn that only closed when he muted.

A rolling percentile has no memory of a moment that will never recur. Over 8 seconds there are
always inter-word gaps even in continuous speech, so the 20th percentile lands on the room rather
than on the voice — and when the AC switches on, the estimate follows within seconds."""

END_OF_UTTERANCE_MS = _int_env("VOICE_TUNNEL_END_OF_UTTERANCE_MS", 1500)
"""Silence that ends a turn. Override live with VOICE_TUNNEL_END_OF_UTTERANCE_MS.

Raised 1000 -> 1500 after JJ, live 2026-07-31: "whenever I haven't finished speaking, it ends my
turn and you start processing and you might interrupt me." He thinks in pauses, and a threshold
tuned for clipped dictation fragments a person who is composing a thought out loud.

The cost is 0.5 s added to every reply. That is the right trade here — being cut off mid-thought
ruins the exchange, while half a second of extra pause is merely slower. Earlier history:

at 700 ms a single continuous thought
**fragmented into five separate turns** — JJ, live, 2026-07-29: "...I don't like the voice much.
Um I like um" / "Voices that have a lower register." / "And do I need to say every time?" —
because filler-and-pause is normal speech, not the end of a sentence.

Nothing was lost (every fragment reached the log, which is the cursor contract working), but
fragmentation is still a real cost: it hands the agent four partial thoughts to reassemble
instead of one, and an agent that answers the first fragment answers the wrong question.

The latency that actually mattered was never this — Parakeet cut transcription from 2.64 s to
0.23 s. This pause buys the speaker room to think, which is what the whole exchange depends on."""

MIN_UTTERANCE_MS = 500
"""Discard anything shorter. Rejects coughs, clicks, and door slams that clear the energy gate
but carry no speech."""

INITIAL_GRACE_MS = 1000
"""Grace period after the session opens before end-of-utterance logic can fire, so the very
first turn is not closed before the speaker starts."""

SPEAK_GRACE_S = 0.8
"""Extra pause before speaking, after the speaker appears to have stopped.

The mid-utterance hold is not enough on its own: a pause longer than END_OF_UTTERANCE_MS closes
the turn, so someone who is merely thinking looks exactly like someone who has finished. JJ,
live 2026-07-29: "actually, you did interrupt me because I was not done speaking" — the hold had
released and he resumed a moment later.

So after the hold clears, wait this long and re-check. If they started again, hold again. Costs
0.8 s on every reply; buys not talking over the person you are meant to be listening to."""

PARTIAL_INTERVAL_S = 0.7
"""How often to re-transcribe the in-flight utterance for the live preview.

Only affordable because Parakeet runs at RTF ~0.08 — at whisper small.en's 0.88 this would eat
the CPU the real transcription needs. A partial is skipped entirely if the previous one is still
running, so the preview degrades to "less frequent" rather than falling behind or stalling
audio ingest. Set 0 to disable."""

MAX_UTTERANCE_MS = 120_000
"""Hard ceiling on a single turn. A stuck VAD must not buffer forever."""

# -------------------------------------------------------------------- wake word

WAKE_NAME = "assistant"
"""What you call the agent on the other end. Set it with `serve --wake <name>`.

**The tool holds no model, so nothing ties it to any vendor except this string.** JJ, live
2026-08-03: *"my goal is to be able to use this with any AI agent, not just cloud [Claude]."*

**The name should be the agent's own** — `--wake claude` when Claude starts it, `--wake codex`
under Codex, `--wake grok` under Grok. That is not a convention the tool enforces, because it
cannot know: it is a dumb pipe, and whatever started it is the only party that knows what it is.
The default here is deliberately a role rather than a product, so an unconfigured tunnel answers
to something true instead of to somebody else's trademark.

**"assistant" over "jarvis"**: Jarvis is a Marvel trademark — fine in a private tool, a real
problem the moment it ships."""

GREETINGS = ("hey", "hi", "ok", "okay", "yo", "hello")
"""Openers that mark an utterance as a summons. One of these is ALWAYS required.

**A greeting is not optional and is not configurable, and that is what makes any name safe.**
The wake phrase is a GREETING PLUS A NAME, and the combination is what nobody says by accident:
"let's ship it Thursday" is ordinary speech, "hey Thursday" is not. The same rule rescues every
name a user might actually pick — `grok` is an English verb, `cursor` and `gemini` are ordinary
words, and none of that matters once "hey" has to come first.

This started as a per-name setting (`WAKE_REQUIRE_GREETING`, overridable with
`VOICE_TUNNEL_WAKE_BARE`) on the theory that an uncommon name like "claude" could safely be said
bare. JJ removed the option: *"I would make it a default to always require the hey, and the only
thing the agent or the user can choose is the wake word that is pronounced after hey."*

**Deleting the option deleted a class of bug rather than a feature.** It is the tool asking every
user to correctly predict whether their agent's name collides with ordinary speech — a judgment
they can only get wrong, and whose failure mode is an agent that interrupts conversations it was
never part of."""


def wake_name() -> str:
    return (_env("VOICE_TUNNEL_WAKE_NAME") or WAKE_NAME).lower().strip()


def wake_phrases() -> tuple:
    """Every accepted summons: one greeting, then the name. Longest first so it strips fully.

    Text-level matching rather than an acoustic wake model: the ASR already runs on every buffer,
    so this needs no extra dependency, no model download, and no per-phrase training. The cost is
    that gating happens after transcription, which is fine because we are not trying to save
    power."""
    name = wake_name()
    return tuple(f"{g} {name}" for g in GREETINGS)

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

SENTENCE_SILENCE_S = 0.5
"""Default silence between sentences. Overridable live and persisted — see `sentence_pause`.

Piper defaults to 0.0, which runs sentences together: fine for one remark, bad for a list. JJ,
live from the phone 2026-07-31: "you read this list back to back and it was hard to understand."
Speech has no scrollback — the listener cannot re-read item two while item three is arriving, so
the pause IS the punctuation.

**0.5 s, and the shorter value tried here was a mistake.** The hiss JJ reported in the same
session ("started sounding like very loud white noise instead of silence") was CLIPPING, not the
gap — piper measured at peak 1.000, and clipping generates exactly the broadband harmonics that
sound like white noise (see PEAK_CEILING). The gaps themselves measured silent at 0.00001 RMS.
Shortening the pause was an over-correction shipped in the same edit as the clipping fix, so
neither of us could attribute the improvement, and it made pacing worse on its own. When the only
instrument is a person's ear, change one thing at a time."""

UNDELIVERED_MAX = 8
UNDELIVERED_MAX_AGE_S = 600.0
"""How many replies to hold for a disconnected phone, and for how long.

Bounded on BOTH axes deliberately. Unbounded, the fix is worse than the bug: you put the phone
down for an hour, come back, and get read a stack of stale answers to questions you have stopped
caring about — and each one costs real seconds you cannot skim past.

Ten minutes and eight clips covers the case this exists for (the screen locks, a call comes in, a
tab gets backgrounded) without covering "I left". Past that the right behaviour is to ask again,
because by then the question probably changed."""

PEAK_CEILING = 0.89
"""Synthesized audio is scaled to this peak before it is sent.

Piper output measured at peak 1.000 — clipped. Clipping generates broadband harmonics, which is
exactly what "white noise" sounds like, and speeding the voice up makes it worse by packing more
transients per second. Leaving ~1 dB of headroom costs nothing audible and removes a whole class
of distortion that is easy to misdiagnose as a bad model or a bad connection."""

SPEECH_SPEED = 1.18
"""How fast the agent talks, as a multiple of the voice's native pace. Higher is faster.

**SPEED is the unit this codebase speaks, everywhere** — settings file, `voice-tunnel rate`, the `/rate`
endpoint, `status`. Piper's own knob is `length_scale`, which is INVERTED (lower is faster), and
letting that leak out was a real defect: JJ asked for 2.0 expecting twice as fast and got half
speed. The inversion now exists in exactly one line, `length_scale_for` below, and nothing above
the piper boundary ever sees it.

1.18 (= 1/0.85) because en_GB-alan-medium — the voice JJ picked — reads noticeably slowly at
native pace ("I kind of liked Alan, it's just he speaks too slowly"). This scales duration, not
pitch, so the voice keeps its character. Above ~1.33 it starts clipping consonants and sounding
rushed. Override with VOICE_TUNNEL_SPEECH_SPEED, or live and persistently with `voice-tunnel rate --speed`."""

SPEED_MIN, SPEED_MAX = 0.5, 2.5
"""Accepted speed range: half speed to 2.5x. Bounded because a speed of 0 divides by zero on the
way to length_scale, and anything past ~2.5 is not speech any more."""

PAUSE_MAX = 1.5
"""Longest accepted sentence pause. Past this a reply stops sounding like one thought."""


def length_scale_for(speed: float) -> float:
    """Convert SPEED (higher is faster) to piper's length_scale (lower is faster).

    THE ONLY PLACE THE INVERSION EXISTS. Every caller above the piper boundary passes speed."""
    return 1.0 / max(SPEED_MIN, min(SPEED_MAX, float(speed)))


def speech_speed() -> float:
    """Persisted speech speed, so a preference tuned by ear survives a restart.

    JJ had to re-set this on every server start, which meant the value that was right for him
    lived only in a running process — the definition of a setting that will be lost."""
    raw = _env("VOICE_TUNNEL_SPEECH_SPEED")
    if raw:
        try:
            return max(SPEED_MIN, min(SPEED_MAX, float(raw)))
        except ValueError:
            pass
    # Backwards compatibility: honour a hand-written length_scale, converted. Retired because a
    # persisted setting in the inverted unit re-creates the exact confusion SPEECH_SPEED removes.
    legacy = _env("VOICE_TUNNEL_PIPER_LENGTH_SCALE")
    if legacy:
        try:
            return max(SPEED_MIN, min(SPEED_MAX, 1.0 / float(legacy)))
        except (ValueError, ZeroDivisionError):
            pass
    return SPEECH_SPEED


def sentence_pause() -> float:
    """Persisted pause between sentences. Same reason as speech_speed: tuned by ear, so it has
    to outlive the process that was tuned."""
    raw = _env("VOICE_TUNNEL_SENTENCE_PAUSE")
    if raw:
        try:
            return max(0.0, min(PAUSE_MAX, float(raw)))
        except ValueError:
            pass
    return SENTENCE_SILENCE_S

TTS_SR = 22050
"""Output rate for synthesized audio. SAPI and Piper both produce this comfortably."""

DEFAULT_PIPER_VOICE = "en_GB-alan-medium"
"""Which voice piper uses when nothing names one.

Alan is the voice JJ picked ("I kind of liked Alan") — the same choice SPEECH_SPEED above
was tuned for. Naming a default here is what lets `VOICE_TUNNEL_TTS=piper` be the ONLY setting a piper
session needs: with no default the backend refused to start unless the caller also threaded
VOICE_TUNNEL_PIPER_VOICE through every invocation, which is precisely the env-var tax this file exists to
remove. Falls back to the sole installed voice if Alan is not on disk, and to nothing if the
choice would be a guess between several."""

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
    """Where turn logs and voiceprints live. Repo-local in a checkout, per-user when installed."""
    return _env("VOICE_TUNNEL_DIR") or _default_path("sessions")


def verbose_default() -> bool:
    """Whether the agent narrates every action. Persisted, and GLOBAL rather than per-device.

    The split that matters: **verbose is about the AGENT, mute is about a MICROPHONE.** A
    preference for how much the agent explains itself is one preference JJ holds, so flipping it
    on a laptop must reach the phone. Muting is a fact about the device in front of him, so it
    stays local — muting a phone must not deafen a desktop in another room.

    JJ, live 2026-08-03: "I would like the verbose toggle to be server-side persisted. If I toggle
    it in a browser then it should be remembered on my phone."
    """
    return (_env("VOICE_TUNNEL_VERBOSE", "0") or "0") not in ("0", "false", "no", "off")


def cues_enabled() -> bool:
    """Audio cues on by default. They exist so a pause is legible without looking at the page —
    disable with VOICE_TUNNEL_CUES=0 if they ever become noise rather than information."""
    return (_env("VOICE_TUNNEL_CUES", "1") or "1") not in ("0", "false", "no", "off")


def owner_name() -> str:
    """Whose voice this tunnel belongs to. One operator, so a constant is enough — but it is a
    name rather than a boolean so a gallery can hold other speakers later (to *exclude* them)."""
    return _env("VOICE_TUNNEL_OWNER", "me")


def models_dir() -> str:
    """Where downloaded models live (Piper voices, Parakeet). Never vendored — a Parakeet
    checkpoint is ~600 MB, which belongs in a cache the user owns, not in a wheel."""
    return _env("VOICE_TUNNEL_MODELS_DIR") or _default_path("models")


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
    explicit = _env("VOICE_TUNNEL_PARAKEET_DIR")
    if explicit:
        return explicit
    default = os.path.join(models_dir(), "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")
    return default if os.path.isdir(default) else ""


def have_module(name: str) -> bool:
    """Is an optional runtime package importable? Checked WITHOUT importing it.

    `sherpa-onnx` and `piper-tts` are extras (`pip install voice-tunnel[all]`), so unlike in a
    checkout — where everything arrived together and this question never came up — either can be
    absent at runtime. `find_spec` answers it for the price of a path search rather than loading
    an ONNX runtime to find out.
    """
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def asr_engine() -> str:
    """`parakeet` when its model AND its runtime are both present, else `whisper`.

    **Both halves, and the second one is why this is not a one-liner.** Selecting on the model
    alone means `download asr` — which fetches 600 MB — flips the engine to Parakeet on an
    install that has no `sherpa-onnx` to load it with, and the failure lands at the first spoken
    word rather than at the download. A checkout always had both, so the distinction did not
    exist until this became installable with extras.

    An explicit VOICE_TUNNEL_ASR is still obeyed either way: someone who names an engine deserves
    the error rather than a silent substitution. `doctor` reports the mismatch.
    """
    forced = _env("VOICE_TUNNEL_ASR").lower()
    if forced in ("parakeet", "whisper"):
        return forced
    return "parakeet" if (parakeet_dir() and have_module("sherpa_onnx")) else "whisper"


def asr_threads() -> int:
    try:
        return max(1, int(_env("VOICE_TUNNEL_ASR_THREADS", "4")))
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
    return _env("VOICE_TUNNEL_WHISPER_MODEL", "base.en")


def asr_beam_size() -> int:
    """Beam width. Benchmarked as near-irrelevant to speed here (0.88 vs 0.92 RTF) — model
    size dominates by ~3x — so this stays at 1 rather than being a tuning knob anyone reaches
    for expecting a win."""
    try:
        return max(1, int(_env("VOICE_TUNNEL_ASR_BEAM", "1")))
    except ValueError:
        return 1


def tts_backend() -> str:
    return _env("VOICE_TUNNEL_TTS", "sapi").lower()


def extra_allow_cidrs() -> tuple[str, ...]:
    raw = _env("VOICE_TUNNEL_ALLOW_CIDRS")
    return tuple(c.strip() for c in raw.split(",") if c.strip())


def trusted_proxies() -> tuple[str, ...]:
    """Empty by default — see ai-docs/reference/security.md. An empty tuple means
    X-Forwarded-For is ignored entirely and the direct TCP peer decides."""
    raw = _env("VOICE_TUNNEL_TRUSTED_PROXIES")
    return tuple(c.strip() for c in raw.split(",") if c.strip())


# ------------------------------------------------------------------------ piper
#
# Resolution lives here rather than in tts.py because AGENTS.md convention 5 says tunables live
# in config with their reason. The practical payoff is that `piper` stops needing three env vars
# threaded through every call: the binary and the voice are both DERIVABLE from the checkout.


def piper_voices() -> list:
    """Piper voice NAMES installed in the models dir.

    A Piper voice is an `.onnx` **plus a sidecar `.onnx.json`** describing its phonemes and
    sample rate; piper refuses to load one without the other. Requiring the sidecar is therefore
    not a heuristic, it is the format — and it is what keeps unrelated ONNX models that share the
    directory (the titanet speaker embedder the voiceprint gallery uses) out of `voice-tunnel voices`,
    where they read as a voice you could select and then fail at synthesis time.
    """
    d = models_dir()
    if not os.path.isdir(d):
        return []
    names = []
    for f in os.listdir(d):
        if f.endswith(".onnx") and os.path.isfile(os.path.join(d, f + ".json")):
            names.append(f[:-5])
    return sorted(names)


def piper_bin() -> str:
    """Path to the piper executable, or "" if it cannot be found.

    Order: explicit env > the repo venv > PATH. The venv comes before PATH because piper is a
    pip package here (`venv/Scripts/piper.exe`), and a globally-installed piper of a different
    version would otherwise silently win over the one this checkout's requirements pinned.
    """
    explicit = _env("VOICE_TUNNEL_PIPER_BIN")
    if explicit:
        return explicit
    for candidate in (
        os.path.join(ROOT, "venv", "Scripts", "piper.exe"),   # Windows venv layout
        os.path.join(ROOT, "venv", "bin", "piper"),           # POSIX venv layout
    ):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("piper") or ""


def piper_inprocess() -> bool:
    """Load the piper voice into THIS process instead of spawning `piper.exe` per reply.

    On by default because the subprocess was the slowest thing in the whole loop by an order of
    magnitude. Measured on this machine, same voice, same text:

        subprocess   3.6 - 4.3 s   (a near-constant tax; ~3.5 s of it is process + model startup)
        in-process   0.17 - 0.58 s (scales with text length, which is the real synthesis cost)

    That is 7-26x, and it made TTS slower than transcription by more than 10x — Parakeet does its
    half in 0.23 s. The model loads once and is reused, so the tax is paid at `serve` time.

    Set VOICE_TUNNEL_PIPER_INPROCESS=0 to go back to spawning the binary. Kept as an escape hatch because
    VOICE_TUNNEL_PIPER_BIN may point at a non-Python piper (the C++ releases), for which the resident path
    is a different implementation, not the same one held open."""
    return (_env("VOICE_TUNNEL_PIPER_INPROCESS", "1") or "1") not in ("0", "false", "no", "off")


def piper_voice() -> str:
    """Path to the default `.onnx` voice, or "" if the choice would be a guess.

    Returns a PATH, unlike the `--voice` flag which takes a NAME — see tts.resolve_voice for why
    the flag refuses paths (it is reachable over the tunnel, and a path turns text-to-speech into
    a file probe). This value is not caller-supplied, so it may be a path.
    """
    explicit = _env("VOICE_TUNNEL_PIPER_VOICE")
    if explicit:
        return explicit
    d = models_dir()
    named = os.path.join(d, f"{DEFAULT_PIPER_VOICE}.onnx")
    if os.path.isfile(named):
        return named
    installed = piper_voices()
    # One installed voice is not a choice, so take it. Several is a choice, and guessing which
    # voice someone wants to hear is worse than saying "name one" — `voice-tunnel doctor` says how.
    return os.path.join(d, f"{installed[0]}.onnx") if len(installed) == 1 else ""


# ============================================================ the settings file
#
# THE PROBLEM THIS SOLVES. Before this existed, driving the tunnel with piper looked like:
#
#     VOICE_TUNNEL_DIR=... VOICE_TUNNEL_TTS=piper VOICE_TUNNEL_PIPER_BIN=... VOICE_TUNNEL_PIPER_VOICE=... python -c "import sys; ..."
#
# on EVERY call, because nothing persisted and the repo shipped a `.env.example` telling you to
# "copy to .env" that no code ever read. Four variables re-typed per invocation is not a
# configuration story, it is a ritual — and a ritual an agent performs from memory is a ritual it
# will eventually perform wrong.
#
# The format is `.env` rather than TOML or JSON deliberately:
#   * The repo already documents it (`.env.example`) and already gitignores it.
#   * Keys ARE environment variable names, so "env overrides file" needs no mapping layer —
#     it is one `setdefault` call and there is no second name for anything.
#   * Python 3.11's tomllib is READ-ONLY, so `voice-tunnel config set` would have needed a hand-rolled
#     TOML writer: a new way to corrupt a file, bought for no behaviour anyone asked for.

def env_file_default() -> str:
    """`<repo>/.env` in a checkout, `<user config dir>/.env` once installed.

    A function rather than the module-level constant this used to be: the answer depends on how
    the tool was installed, and a constant froze it at import time.
    """
    return _default_path(".env", kind="config")

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""What counts as a variable name when READING the file — the POSIX shell rule, permissive."""

_WRITABLE_KEY_RE = re.compile(r"^VOICE_TUNNEL_[A-Z0-9_]+$")
"""What `voice-tunnel config set` is allowed to WRITE — strict, this tool's own namespace only.

Permissive on read, strict on write, and the asymmetry is the point: a file a human hand-edited
should not be second-guessed, but a machine writing into it must stay in its lane. An agent that
hallucinates `voice-tunnel config set PATH ""` should be refused, not obeyed."""

_LOAD_REPORT: dict = {}
"""What the last load_env_file() actually did, so `config show` can attribute each value to env
or file. Once a value is in os.environ the two are indistinguishable, so this has to be recorded
at load time or not at all."""


def env_file_path() -> str:
    """Where persisted settings live, or VOICE_TUNNEL_ENV_FILE if pointed elsewhere.

    In a checkout: `<repo>/.env`, gitignored, for the same reason session logs are repo-local —
    settings that travel with the working tree need no per-machine setup, and a shared secret
    that never leaves it cannot be committed by accident. Installed: the per-user config
    directory, because there is no working tree to travel with.

    VOICE_TUNNEL_ENV_FILE exists so tests can point somewhere disposable — a suite that reads the
    developer's real settings is not a suite, it's a mood.
    """
    return _env("VOICE_TUNNEL_ENV_FILE") or env_file_default()


def parse_env_text(text: str) -> dict:
    """Parse `.env` text into {KEY: value}. Never raises — a malformed line is skipped.

    Tolerant on purpose: this file is hand-edited, and refusing to start because line 12 has a
    stray word would be a worse failure than ignoring line 12. Anything skipped is reported by
    `voice-tunnel config show` under `ignored`, so nothing is silently swallowed.

    Understands: `#` comments, blank lines, an optional `export ` prefix, and values wrapped in
    matching single or double quotes. An unquoted value may carry a trailing ` # comment`;
    a quoted one may not, because inside quotes a `#` is just a character.
    """
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            comment = value.find(" #")
            if comment != -1:
                value = value[:comment].rstrip()
        out[key] = value
    return out


def read_env_file(path: str | None = None) -> dict:
    """{KEY: value} from the settings file, or {} if there isn't one."""
    path = path or env_file_path()
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_env_text(fh.read())
    except OSError:
        return {}


def load_env_file(path: str | None = None) -> dict:
    """Apply the settings file to os.environ, NEVER overwriting a variable already set.

    Precedence is **process env > file > built-in default**, and that direction is the entire
    contract. A caller has to be able to override one setting for one invocation without editing
    a file every other session shares — scripts/e2e.py depends on exactly this, handing its child
    a VOICE_TUNNEL_DIR/VOICE_TUNNEL_TOKEN/VOICE_TUNNEL_TTS triple that must beat whatever the developer has persisted.

    Idempotent: a second call is a no-op, because the first call is what set the variable.

    Returns a report rather than None so `config show` and `doctor` can explain themselves:
    which keys were applied, which the environment already shadowed, which lines were ignored.
    """
    global _LOAD_REPORT
    path = path or env_file_path()
    values = read_env_file(path)
    applied, shadowed, ignored = [], [], []
    for key, value in values.items():
        if not _ENV_NAME_RE.match(key):
            ignored.append(key)
            continue
        if key in os.environ:
            shadowed.append(key)
            continue
        os.environ[key] = value
        applied.append(key)
    _LOAD_REPORT = {
        "file": path,
        "exists": os.path.exists(path),
        "applied": applied,
        "shadowed": shadowed,
        "ignored": ignored,
    }
    return dict(_LOAD_REPORT)


def load_report() -> dict:
    """What the last load_env_file() did. {} if it was never called this process."""
    return dict(_LOAD_REPORT)


def validate_setting(key: str, value: str) -> None:
    """Refuse a key/value that must not reach a file on disk. Raises ValueError with a remedy.

    The threat model is a hallucinating caller, not a hostile one (see "Rewrite Your CLI for AI
    Agents", pattern 4: treat agents like untrusted API users). A newline in a value would forge
    an extra setting on the next line; a control character would produce a file that reads back
    as something no one wrote; a stray quote would be re-parsed as a delimiter. All three are
    cheap to reject and expensive to debug.
    """
    if not _WRITABLE_KEY_RE.match(key or ""):
        raise ValueError(
            f"{key!r} is not a settable key. `voice-tunnel config set` writes only this tool's own "
            f"namespace: an upper-case name starting with VOICE_TUNNEL_ (e.g. VOICE_TUNNEL_TTS). "
            f"Run `voice-tunnel config show` for the full list."
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError(
            "a setting value cannot contain a newline or control character — it would forge a "
            f"second setting on the next line of {env_file_path()}"
        )
    if '"' in value or "'" in value:
        raise ValueError(
            "a setting value cannot contain a quote character; the reader treats quotes as "
            f"delimiters. Edit {env_file_path()} by hand if you really need one."
        )


def _format_env_value(value: str) -> str:
    """Quote only when the reader would otherwise mis-read it — bare values stay readable."""
    if value == "" or value != value.strip() or any(c in value for c in " \t#"):
        return f'"{value}"'
    return value


def _line_key(line: str) -> str:
    """The key a raw line assigns, or "" — used to find the line to replace."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    key, sep, _ = stripped.partition("=")
    return key.strip() if sep else ""


_HEADER = (
    "# voice-tunnel settings. Gitignored. Read automatically by every `voice-tunnel` command.\n"
    "# Process environment variables override anything here.\n"
    "# Managed by `voice-tunnel config set` / `voice-tunnel config unset`; hand-editing is fine too.\n"
)


def write_setting(key: str, value: str | None, path: str | None = None) -> dict:
    """Set (or, with value=None, remove) one key in the settings file, in place.

    Rewrites line by line instead of dumping a parsed dict, so comments, ordering and any
    hand-written key survive. A config file that loses its comments the first time a tool touches
    it is a config file people stop letting tools touch.
    """
    path = path or env_file_path()
    if value is not None:
        validate_setting(key, value)
    elif not _WRITABLE_KEY_RE.match(key or ""):
        raise ValueError(f"{key!r} is not a settable key (upper-case, VOICE_TUNNEL_ prefix)")

    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        existed = True
    except OSError:
        lines = _HEADER.splitlines()
        existed = False

    new_line = None if value is None else f"{key}={_format_env_value(value)}"
    out, replaced = [], 0
    for line in lines:
        if _line_key(line) == key:
            replaced += 1
            if replaced == 1 and new_line is not None:
                out.append(new_line)
            # Later duplicates are dropped: the reader keeps the LAST occurrence, so leaving
            # them would mean the file says one thing and the process does another.
            continue
        out.append(line)
    if new_line is not None and replaced == 0:
        out.append(new_line)

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")
    return {
        "file": path,
        "created": not existed,
        "key": key,
        "action": "unset" if value is None else "set",
        "replaced": replaced,
    }


# ------------------------------------------------------------ settings registry


def _setting(key: str, what: str, resolve, secret: bool = False) -> dict:
    return {"key": key, "what": what, "resolve": resolve, "secret": secret}


SETTINGS: tuple = (
    _setting("VOICE_TUNNEL_ENV_FILE", "path to this settings file itself (default <repo>/.env)",
             env_file_path),
    _setting("VOICE_TUNNEL_TOKEN", "shared secret for the WS handshake; generated at serve time if unset",
             lambda: _env("VOICE_TUNNEL_TOKEN"), secret=True),
    _setting("VOICE_TUNNEL_ALLOW_CIDRS", "extra CIDRs allowed (add 100.64.0.0/10 for Tailscale)",
             lambda: ",".join(extra_allow_cidrs())),
    _setting("VOICE_TUNNEL_TRUSTED_PROXIES", "leave empty unless a real proxy fronts this",
             lambda: ",".join(trusted_proxies())),
    _setting("VOICE_TUNNEL_DIR", "where turn logs live", session_dir),
    _setting("VOICE_TUNNEL_MODELS_DIR", "where downloaded models live", models_dir),
    _setting("VOICE_TUNNEL_TTS", "sapi | piper | none", tts_backend),
    _setting("VOICE_TUNNEL_PIPER_BIN", "piper executable; auto-found in the repo venv or on PATH",
             piper_bin),
    _setting("VOICE_TUNNEL_PIPER_VOICE", "default .onnx voice; auto-found in the models dir", piper_voice),
    _setting("VOICE_TUNNEL_PIPER_INPROCESS",
             "1 | 0 — hold the voice model in this process (7-26x faster than spawning piper)",
             lambda: "1" if piper_inprocess() else "0"),
    _setting("VOICE_TUNNEL_SPEECH_SPEED",
             f"how fast the agent talks; 1.0 is native pace, higher is faster "
             f"({SPEED_MIN}-{SPEED_MAX}). Set it live with `voice-tunnel rate --speed`",
             lambda: str(speech_speed())),
    _setting("VOICE_TUNNEL_SENTENCE_PAUSE",
             f"seconds of silence between sentences (0-{PAUSE_MAX}); the pause IS the "
             f"punctuation in speech. Set it live with `voice-tunnel rate --pause`",
             lambda: str(sentence_pause())),
    _setting("VOICE_TUNNEL_ASR", "parakeet | whisper (auto-selects parakeet when its model is present)",
             asr_engine),
    _setting("VOICE_TUNNEL_PARAKEET_DIR", "sherpa-onnx Parakeet model dir", parakeet_dir),
    _setting("VOICE_TUNNEL_WHISPER_MODEL", "whisper fallback model (parakeet is preferred)", whisper_model),
    _setting("VOICE_TUNNEL_ASR_THREADS", "ASR worker threads", lambda: str(asr_threads())),
    _setting("VOICE_TUNNEL_ASR_BEAM", "whisper beam width; size dominates speed, not this",
             lambda: str(asr_beam_size())),
    _setting("VOICE_TUNNEL_END_OF_UTTERANCE_MS", "silence that ends a turn; raise it if you get cut off",
             lambda: str(END_OF_UTTERANCE_MS)),
    _setting("VOICE_TUNNEL_CUES", "1 | 0 — short non-speech cues so a pause is audible",
             lambda: "1" if cues_enabled() else "0"),
    _setting("VOICE_TUNNEL_VERBOSE",
             "1 | 0 — narrate every action before doing it. Global across devices; "
             "set live with `voice-tunnel verbose on`",
             lambda: "1" if verbose_default() else "0"),
    _setting("VOICE_TUNNEL_OWNER", "name the voiceprint gallery learns under", owner_name),
    _setting("VOICE_TUNNEL_WAKE_NAME",
             "what you call the agent; 'hey <name>' summons it, and a greeting is always "
             "required. Set it to the agent's own name with `serve --wake <name>`",
             wake_name),
)
"""Every VOICE_TUNNEL_* variable, in one place, with what it does and how to read its live value.

ONE registry, three consumers: `voice-tunnel describe`'s env block, `voice-tunnel config show`, and the test that
asserts `.env.example` documents all of it. Before this, `describe` listed eight variables and
the code read seventeen — so the piper settings an agent could not run without were discoverable
only by reading tts.py. A contract that omits the thing you need is worse than no contract,
because it is trusted."""

REDACTED = "***"
"""Stand-in for a secret's value in a BULK dump.

`config show` redacts; `config get VOICE_TUNNEL_TOKEN` does not. The rule is that an explicit single-key
read is someone asking for that secret, while a bulk dump is someone asking for orientation and
getting the secret as a side effect — into a transcript, a log, and an agent's context."""


def effective(reveal: bool = False) -> list:
    """Every setting with its live value and WHERE it came from: env, file, or default.

    "Where from" is the question `config show` exists to answer. A value being wrong is easy;
    a value being wrong *because a stale process env is shadowing the file you just edited* is
    the one that costs an hour, and it is invisible unless something says so out loud.
    """
    from_file = set(_LOAD_REPORT.get("applied", ()))
    rows = []
    for spec in SETTINGS:
        key = spec["key"]
        raw = os.environ.get(key)
        if raw is None or not raw.strip():
            source = "default"
        elif key in from_file:
            source = "file"
        else:
            source = "env"
        value = spec["resolve"]()
        if spec["secret"] and value and not reveal:
            value = REDACTED
        rows.append({
            "key": key,
            "value": value,
            "source": source,
            "what": spec["what"],
        })
    return rows
