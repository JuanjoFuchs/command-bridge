"""vm.config — every tunable in one place, each with the reason it has that value.

Config-as-data (AGENTS.md convention 5): a number scattered in code is a number nobody dares
change. A number here with its rationale is one an operator can reason about.

STDLIB ONLY — imported by everything, including the pure-logic modules under test.
"""
from __future__ import annotations

import os
import re
import shutil

# ------------------------------------------------------------------ repo root

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
"""Absolute path to the repo root, derived from THIS FILE rather than the cwd.

Every default path in this module hangs off it. An agent invokes `vm` from wherever its own turn
happens to be standing, so any default resolved against `os.getcwd()` would scatter turn logs and
settings across whatever directories the caller passed through — the exact class of bug that
makes a tool "work on my machine" and nowhere else."""


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

END_OF_UTTERANCE_MS = _int_env("VM_END_OF_UTTERANCE_MS", 1500)
"""Silence that ends a turn. Override live with VM_END_OF_UTTERANCE_MS.

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

SENTENCE_SILENCE_S = 0.5
"""Silence Piper inserts between sentences.

Piper defaults to a value that runs sentences together, which is fine for one remark and bad for
a list. JJ, live from the phone 2026-07-31: "you read this list back to back and it was hard to
understand." Speech has no scrollback — the listener cannot re-read item two while item three is
arriving, so the pause IS the punctuation.

0.28 s, not longer. A phone's output AGC ramps its gain during a gap of true digital silence and
amplifies the noise floor — JJ, live from the phone: "that whatever thing you added started
sounding like very loud white noise instead of silence." Measured: the WAV's gaps really are
silent (0.00001 RMS), so the hiss is the DEVICE chasing the silence, not the file. A shorter gap
still marks the boundary without giving the gain time to climb."""

PEAK_CEILING = 0.89
"""Synthesized audio is scaled to this peak before it is sent.

Piper output measured at peak 1.000 — clipped. Clipping generates broadband harmonics, which is
exactly what "white noise" sounds like, and speeding the voice up makes it worse by packing more
transients per second. Leaving ~1 dB of headroom costs nothing audible and removes a whole class
of distortion that is easy to misdiagnose as a bad model or a bad connection."""

PIPER_LENGTH_SCALE = 0.85
"""Speech rate for Piper. Lower is faster; 1.0 is the model's native pace.

Set to 0.85 because en_GB-alan-medium — the voice JJ picked — reads noticeably slowly at
native pace ("I kind of liked Alan, it's just he speaks too slowly"). This scales duration, not
pitch, so the voice keeps its character. Below ~0.75 it starts clipping consonants and sounding
rushed. Override per-deployment with VM_PIPER_LENGTH_SCALE."""

TTS_SR = 22050
"""Output rate for synthesized audio. SAPI and Piper both produce this comfortably."""

DEFAULT_PIPER_VOICE = "en_GB-alan-medium"
"""Which voice piper uses when nothing names one.

Alan is the voice JJ picked ("I kind of liked Alan") — the same choice PIPER_LENGTH_SCALE above
was tuned for. Naming a default here is what lets `VM_TTS=piper` be the ONLY setting a piper
session needs: with no default the backend refused to start unless the caller also threaded
VM_PIPER_VOICE through every invocation, which is precisely the env-var tax this file exists to
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
    """Where turn logs live. Repo-local by default so a dev run leaves no trace in $HOME."""
    return _env("VM_DIR") or os.path.join(ROOT, "sessions")


def cues_enabled() -> bool:
    """Audio cues on by default. They exist so a pause is legible without looking at the page —
    disable with VM_CUES=0 if they ever become noise rather than information."""
    return (_env("VM_CUES", "1") or "1") not in ("0", "false", "no", "off")


def owner_name() -> str:
    """Whose voice this tunnel belongs to. One operator, so a constant is enough — but it is a
    name rather than a boolean so a gallery can hold other speakers later (to *exclude* them)."""
    return _env("VM_OWNER", "me")


def models_dir() -> str:
    """Where downloaded models live (Piper voices, Parakeet). Gitignored — models are
    downloaded, never vendored."""
    return _env("VM_MODELS_DIR") or os.path.join(ROOT, "models")


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
    default = os.path.join(models_dir(), "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8")
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
    directory (the titanet speaker embedder the voiceprint gallery uses) out of `vm voices`,
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
    explicit = _env("VM_PIPER_BIN")
    if explicit:
        return explicit
    for candidate in (
        os.path.join(ROOT, "venv", "Scripts", "piper.exe"),   # Windows venv layout
        os.path.join(ROOT, "venv", "bin", "piper"),           # POSIX venv layout
    ):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("piper") or ""


def piper_voice() -> str:
    """Path to the default `.onnx` voice, or "" if the choice would be a guess.

    Returns a PATH, unlike the `--voice` flag which takes a NAME — see tts.resolve_voice for why
    the flag refuses paths (it is reachable over the tunnel, and a path turns text-to-speech into
    a file probe). This value is not caller-supplied, so it may be a path.
    """
    explicit = _env("VM_PIPER_VOICE")
    if explicit:
        return explicit
    d = models_dir()
    named = os.path.join(d, f"{DEFAULT_PIPER_VOICE}.onnx")
    if os.path.isfile(named):
        return named
    installed = piper_voices()
    # One installed voice is not a choice, so take it. Several is a choice, and guessing which
    # voice someone wants to hear is worse than saying "name one" — `vm doctor` says how.
    return os.path.join(d, f"{installed[0]}.onnx") if len(installed) == 1 else ""


# ============================================================ the settings file
#
# THE PROBLEM THIS SOLVES. Before this existed, driving the tunnel with piper looked like:
#
#     VM_DIR=... VM_TTS=piper VM_PIPER_BIN=... VM_PIPER_VOICE=... python -c "import sys; ..."
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
#   * Python 3.11's tomllib is READ-ONLY, so `vm config set` would have needed a hand-rolled
#     TOML writer: a new way to corrupt a file, bought for no behaviour anyone asked for.

ENV_FILE_DEFAULT = os.path.join(ROOT, ".env")

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""What counts as a variable name when READING the file — the POSIX shell rule, permissive."""

_WRITABLE_KEY_RE = re.compile(r"^VM_[A-Z0-9_]+$")
"""What `vm config set` is allowed to WRITE — strict, this tool's own namespace only.

Permissive on read, strict on write, and the asymmetry is the point: a file a human hand-edited
should not be second-guessed, but a machine writing into it must stay in its lane. An agent that
hallucinates `vm config set PATH ""` should be refused, not obeyed."""

_LOAD_REPORT: dict = {}
"""What the last load_env_file() actually did, so `config show` can attribute each value to env
or file. Once a value is in os.environ the two are indistinguishable, so this has to be recorded
at load time or not at all."""


def env_file_path() -> str:
    """Where persisted settings live: `<repo>/.env`, or VM_ENV_FILE if pointed elsewhere.

    Repo-local and gitignored for the same reason session logs are: settings that travel with the
    checkout need no per-machine setup step, and a shared secret that never leaves the working
    tree cannot be committed by accident. VM_ENV_FILE exists so tests can point somewhere
    disposable — a suite that reads the developer's real settings is not a suite, it's a mood.
    """
    return _env("VM_ENV_FILE") or ENV_FILE_DEFAULT


def parse_env_text(text: str) -> dict:
    """Parse `.env` text into {KEY: value}. Never raises — a malformed line is skipped.

    Tolerant on purpose: this file is hand-edited, and refusing to start because line 12 has a
    stray word would be a worse failure than ignoring line 12. Anything skipped is reported by
    `vm config show` under `ignored`, so nothing is silently swallowed.

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
        with open(path, "r", encoding="utf-8") as fh:
            return parse_env_text(fh.read())
    except OSError:
        return {}


def load_env_file(path: str | None = None) -> dict:
    """Apply the settings file to os.environ, NEVER overwriting a variable already set.

    Precedence is **process env > file > built-in default**, and that direction is the entire
    contract. A caller has to be able to override one setting for one invocation without editing
    a file every other session shares — scripts/e2e.py depends on exactly this, handing its child
    a VM_DIR/VM_TOKEN/VM_TTS triple that must beat whatever the developer has persisted.

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
            f"{key!r} is not a settable key. `vm config set` writes only this tool's own "
            f"namespace: an upper-case name starting with VM_ (e.g. VM_TTS). "
            f"Run `vm config show` for the full list."
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
    "# voice-mode settings. Gitignored. Read automatically by every `vm` command.\n"
    "# Process environment variables override anything here.\n"
    "# Managed by `vm config set` / `vm config unset`; hand-editing is fine too.\n"
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
        raise ValueError(f"{key!r} is not a settable key (upper-case, VM_ prefix)")

    try:
        with open(path, "r", encoding="utf-8") as fh:
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
    _setting("VM_ENV_FILE", "path to this settings file itself (default <repo>/.env)",
             env_file_path),
    _setting("VM_TOKEN", "shared secret for the WS handshake; generated at serve time if unset",
             lambda: _env("VM_TOKEN"), secret=True),
    _setting("VM_ALLOW_CIDRS", "extra CIDRs allowed (add 100.64.0.0/10 for Tailscale)",
             lambda: ",".join(extra_allow_cidrs())),
    _setting("VM_TRUSTED_PROXIES", "leave empty unless a real proxy fronts this",
             lambda: ",".join(trusted_proxies())),
    _setting("VM_DIR", "where turn logs live", session_dir),
    _setting("VM_MODELS_DIR", "where downloaded models live", models_dir),
    _setting("VM_TTS", "sapi | piper | none", tts_backend),
    _setting("VM_PIPER_BIN", "piper executable; auto-found in the repo venv or on PATH",
             piper_bin),
    _setting("VM_PIPER_VOICE", "default .onnx voice; auto-found in the models dir", piper_voice),
    _setting("VM_PIPER_LENGTH_SCALE", "piper speech rate; lower is faster",
             lambda: _env("VM_PIPER_LENGTH_SCALE") or str(PIPER_LENGTH_SCALE)),
    _setting("VM_ASR", "parakeet | whisper (auto-selects parakeet when its model is present)",
             asr_engine),
    _setting("VM_PARAKEET_DIR", "sherpa-onnx Parakeet model dir", parakeet_dir),
    _setting("VM_WHISPER_MODEL", "whisper fallback model (parakeet is preferred)", whisper_model),
    _setting("VM_ASR_THREADS", "ASR worker threads", lambda: str(asr_threads())),
    _setting("VM_ASR_BEAM", "whisper beam width; size dominates speed, not this",
             lambda: str(asr_beam_size())),
    _setting("VM_END_OF_UTTERANCE_MS", "silence that ends a turn; raise it if you get cut off",
             lambda: str(END_OF_UTTERANCE_MS)),
    _setting("VM_CUES", "1 | 0 — short non-speech cues so a pause is audible",
             lambda: "1" if cues_enabled() else "0"),
    _setting("VM_OWNER", "name the voiceprint gallery learns under", owner_name),
)
"""Every VM_* variable, in one place, with what it does and how to read its live value.

ONE registry, three consumers: `vm describe`'s env block, `vm config show`, and the test that
asserts `.env.example` documents all of it. Before this, `describe` listed eight variables and
the code read seventeen — so the piper settings an agent could not run without were discoverable
only by reading tts.py. A contract that omits the thing you need is worse than no contract,
because it is trusted."""

REDACTED = "***"
"""Stand-in for a secret's value in a BULK dump.

`config show` redacts; `config get VM_TOKEN` does not. The rule is that an explicit single-key
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
