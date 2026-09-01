"""Spec 008 acceptance measurement — does the agent SAY the term, and does it BREATHE?

WHY THIS EXISTS, AND WHY IT IS A SCRIPT
---------------------------------------
Spec 008 has two halves that no unit test can reach. `tests/` in this repo holds **no mic and no
model** (AGENTS.md → Layout), and both halves need a model:

  * FR2 asks whether a dotted term ARRIVES as that term. The only honest judge is the recognizer,
    because the agent that made the audio cannot hear it (TC1). A string assertion on the
    transform proves the transform; it does not prove the engine said it.
  * FR3 asks whether a reply breathes at a comma. The judge is the rendered PCM, not anyone's ear.

So this is a script, run on demand, exactly like `layout.py` (geometry), `orbstate.py` (the orb)
and `sharpness.py` (timbre). It starts **no server** (TC4) — `tts.synthesize` and
`asr.Recognizer.transcribe` are both callable in-process.

THE THING THAT MAKES THE ROUND TRIP EVIDENCE RATHER THAN DECORATION
-------------------------------------------------------------------
Every positive check here is paired with a negative arm that is separable **only** by the thing
under test:

    AC4  normalised   "The version is 0.2.6 and it is ready."  ->  must contain 0.2.6
    AC5  un-normalised, IDENTICAL PATH, same clip length       ->  must still contain 026

Without AC5, AC4 passing is indistinguishable from a recognizer that would have written `0.2.6`
whatever it heard. The same shape governs pacing:

    AC8  a comma gains a measurable silence
    AC9  the sentence break in the same words is LONGER   (else "pacing" is just "slower")
    AC10 the same words with neither gain NOTHING         (else "pause everywhere" passes both)

And TC2 — over-normalising is worse than the defect, because nothing signals it happened. An
ellipsis, `e.g.`, a sentence-final period and ordinary prose must round-trip with no "point" and
no "dot" anywhere in them.

WHY BOTH ARMS DISABLE THE ON-PATH TRANSFORM
-------------------------------------------
FR1 puts the normalisation ON the synthesis path. That is correct for the product and fatal for
AC5: once `tts.synthesize` normalises for itself, the "un-normalised" arm is normalised too and
the negative arm silently becomes a second copy of the positive one — it would pass, and mean
nothing. So the harness neutralises the on-path stage for the round-trip clips and applies the
transform itself to exactly one arm. Same engine, same downstream chain, same clip; the ONLY
difference is the transform. The pacing clips are the opposite case and go through the real,
unpatched path, because pacing is a property of what `say` actually renders.

⚠ THE SETTINGS TRAP — this cost one measurement on this spec already
--------------------------------------------------------------------
`config` does NOT load the settings file on import; only the CLI entry point does. A harness that
forgets `config.load_env_file()` measures **piper at speed 1.18** while the listener is on
**kokoro at 1.2**, and every number it prints is plausible. This calls it first, and prints the
backend, voice, speed and pause it resolved at the top of every run, so a future reader can see
which engine answered (AC7 / TC3).

USAGE
-----
    venv/Scripts/python.exe scripts/speechcheck.py describe
    venv/Scripts/python.exe scripts/speechcheck.py run              # exits non-zero on any failure
    venv/Scripts/python.exe scripts/speechcheck.py run --no-cache   # re-synthesise everything
    venv/Scripts/python.exe scripts/speechcheck.py prove            # show the instrument go RED

`prove` is not optional decoration. This repo has shipped a denylist that refused nothing and
three diagnostics that never populated. A harness nobody has watched fail is indistinguishable
from one that cannot fail, so `prove` feeds the checks a claim that is known to be false and
requires them to go red.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from command_bridge import asr, config, tts  # noqa: E402  (after sys.path)

# --------------------------------------------------------------------------------------------
# Choices this harness makes. Every one is a decision, not a law, so each is named and printed.
# --------------------------------------------------------------------------------------------

# CHOICE 1 — window length for the silence detector. 10 ms is short enough to resolve a stop
# closure and long enough that a single zero-crossing does not read as a gap.
WINDOW_MS = 10.0

# CHOICE 2 — the silence threshold is RELATIVE TO THE CLIP'S OWN PEAK, not absolute. It has to
# be: `tts.pad` prepends 100 ms and appends 200 ms of digital zeros for Bluetooth sinks, and
# `normalize` scales the whole clip to PEAK_CEILING, so an absolute floor would find the padding,
# call it prosody, and move with every level change. -50 dB below the loudest 10 ms window keeps
# the engine's own noise floor out while still counting the exact zeros `tts.quiet` inserts.
SILENCE_REL_DB = -50.0

# CHOICE 3 — how long a quiet run has to be before it counts as a pause. 60 ms is the spec's own
# threshold, carried over so this harness's numbers are comparable with the ground truth measured
# on 2026-08-19. Natural stop closures sit below it; the 990 ms sentence gap sits far above.
MIN_SILENCE_MS = 60.0

# CHOICE 4 — runs shorter than the decision threshold are still REPORTED, down to 30 ms, so a
# near-miss is visible rather than being rounded away into "none".
REPORT_SILENCE_MS = 30.0

_RECOGNIZER = None
_CACHE = None
_CACHE_DIRTY = False
_STATS = {"synthesized": 0, "cached": 0}


class SpeechCheckError(RuntimeError):
    """Raised with a runnable remedy, matching this repo's `doctor`/`remedy` convention."""


# --------------------------------------------------------------------------------------------
# The seam Slice A builds. Absent until it lands, and that state is reported, never faked.
# --------------------------------------------------------------------------------------------

def load_transform():
    """Return (normalize_for_speech, None) or (None, why-it-is-missing).

    Deliberately does NOT fall back to a local implementation. A harness that implements the
    thing it is measuring is the failure mode `sharpness.py` was written to end: a metric
    authored by the same pass that authors the fix can only agree with it.
    """
    try:
        from command_bridge.speech import normalize_for_speech
    except Exception as exc:  # ImportError today, anything at all once it exists
        return None, f"{type(exc).__name__}: {exc}"
    if not callable(normalize_for_speech):
        return None, "command_bridge.speech.normalize_for_speech is not callable"
    return normalize_for_speech, None


@contextlib.contextmanager
def on_path_transform_disabled():
    """Neutralise whatever normalisation the synthesis path applies for itself.

    See the module docstring: this is what keeps AC5 a real negative arm instead of a second
    copy of AC4. Patches every `normalize_for_speech` bound anywhere under `command_bridge.`,
    which covers both `from .speech import normalize_for_speech` and `speech.normalize_for_speech(...)`
    call styles, and restores them all on the way out.
    """
    def identity(text):
        return text

    patched = []
    for name, mod in list(sys.modules.items()):
        if not (name == "command_bridge" or name.startswith("command_bridge.")):
            continue
        fn = getattr(mod, "normalize_for_speech", None)
        if callable(fn):
            mod.normalize_for_speech = identity
            patched.append((mod, name, fn))
    try:
        yield [name for _, name, _ in patched]
    finally:
        for mod, _, fn in patched:
            mod.normalize_for_speech = fn


# --------------------------------------------------------------------------------------------
# Engine identity — printed at the top of every run so nobody has to guess which engine answered
# --------------------------------------------------------------------------------------------

def engine_settings() -> dict:
    """Resolve the LIVE settings, after loading the settings file. TC3 / AC7 live here."""
    report = config.load_env_file()
    backend = config.tts_backend()
    if backend == "kokoro":
        voice = config.kokoro_voice()
    elif backend == "piper":
        voice = os.path.basename(config.piper_voice() or "") or "(none)"
    else:
        voice = f"({backend})"
    return {
        "settings_file": report.get("file"),
        "settings_exists": report.get("exists"),
        "settings_applied": len(report.get("applied", [])),
        "settings_shadowed": report.get("shadowed", []),
        # What the FILE says for the two settings AC7 is about, so the criterion can be asserted
        # rather than asserted-in-prose. Only these two keys are carried: the settings file also
        # holds COMMAND_BRIDGE_TOKEN, and a harness that prints its whole environment into a report
        # is a credential leak wearing a diagnostic's clothes.
        "file_values": {k: v for k, v in config.read_env_file().items()
                        if k in ("COMMAND_BRIDGE_TTS", "COMMAND_BRIDGE_SPEECH_SPEED")},
        "module_default_speed": config.SPEECH_SPEED,
        "tts_backend": backend,
        "voice": voice,
        "speech_speed": config.speech_speed(),
        "sentence_pause": config.sentence_pause(),
        "deess": config.deess(),
        "consonant_boost": config.consonant_boost(),
        "asr_engine": config.asr_engine(),
        "asr_model": ("parakeet-tdt-0.6b-v2" if config.asr_engine() == "parakeet"
                      else config.whisper_model()),
    }


def recognizer():
    global _RECOGNIZER
    if _RECOGNIZER is None:
        _RECOGNIZER = asr.Recognizer()
    return _RECOGNIZER


# --------------------------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------------------------

def internal_silences(pcm: bytes, rate: int) -> tuple[list, dict]:
    """Silent runs INSIDE a clip, in ms, with the diagnostics that make the number auditable.

    Leading and trailing padding is trimmed first — `tts.pad` puts 100 ms of zeros at the front
    and 200 ms at the back of every clip, and an instrument that counts those has found the
    Bluetooth workaround and called it prosody.
    """
    samples = asr.pcm16_to_float32(pcm)
    n = max(1, int(rate * WINDOW_MS / 1000.0))
    usable = (samples.size // n) * n
    if usable < n * 3:
        return [], {"windows": 0, "note": "clip too short to window"}
    frames = samples[:usable].reshape(-1, n)
    win_rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    peak = float(win_rms.max())
    if peak <= 0:
        return [], {"windows": int(win_rms.size), "peak_rms": 0.0, "note": "clip is digital silence"}
    threshold = peak * (10.0 ** (SILENCE_REL_DB / 20.0))
    loud = win_rms >= threshold
    idx = np.flatnonzero(loud)
    diag = {
        "windows": int(win_rms.size),
        "peak_window_rms": round(peak, 6),
        "threshold_rms": round(float(threshold), 8),
        "speech_span_s": 0.0,
    }
    if idx.size == 0:
        diag["note"] = "no window reached the threshold"
        return [], diag
    first, last = int(idx[0]), int(idx[-1])
    # The floor must be measured INSIDE the speech span. Taken over the whole clip it reports the
    # 300 ms of digital-zero padding `tts.pad` adds and comes back 0.0 for every clip, which looks
    # like infinite headroom and tells the reader nothing about whether a SOFT (non-zero) pause
    # would be detectable at all.
    inside = win_rms[first:last + 1]
    quiet_inside = inside[inside < threshold]
    diag["lead_pad_ms"] = round(first * WINDOW_MS, 1)
    diag["trail_pad_ms"] = round((win_rms.size - 1 - last) * WINDOW_MS, 1)
    diag["speech_span_s"] = round((last - first + 1) * WINDOW_MS / 1000.0, 3)
    diag["p10_inside_rms"] = round(float(np.percentile(inside, 10)), 8)
    diag["quietest_inside_rms"] = round(float(inside.min()), 8)
    # How far the quiet windows actually sit below the decision line. A large negative number means
    # the detector has room to see a soft pause; ~0 would mean it only ever sees digital zeros.
    diag["quiet_headroom_db"] = (
        round(20.0 * np.log10(max(float(quiet_inside.mean()), 1e-12) / threshold), 1)
        if quiet_inside.size else None)

    runs, start = [], None
    for i in range(first, last + 1):
        if not loud[i]:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i))
            start = None
    out = []
    for a, b in runs:
        ms = (b - a) * WINDOW_MS
        if ms >= REPORT_SILENCE_MS:
            out.append({"at_s": round((a - first) * WINDOW_MS / 1000.0, 3), "ms": round(ms, 1)})
    return out, diag


def transcribe(pcm: bytes, rate: int) -> str:
    samples = asr.pcm16_to_float32(pcm)
    samples = asr.resample_linear(samples, rate, config.TARGET_SR)
    return recognizer().transcribe(samples)


# --------------------------------------------------------------------------------------------
# Clip cache — the RENDERED AUDIO, keyed on the FINAL STRING plus the full engine identity, so a
# cached clip can only ever be audio identical to what a fresh run would render.
#
# The WAV is what is cached, not the derived numbers, for two reasons. Every clip costs CPU on a
# machine that may be carrying a live conversation (TC4), and a cache of derived numbers goes
# stale the moment the measurement changes — which is how a harness ends up reporting yesterday's
# answer with today's confidence. Caching the audio means the detector can be rewritten and
# re-run for free, and a human can LISTEN to any clip this thing made a claim about.
# --------------------------------------------------------------------------------------------

def cache_dir() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(root, "command-bridge-speechcheck")


def cache_file() -> str:
    return os.path.join(cache_dir(), "clips.json")


def read_wav(path: str) -> tuple[bytes, int]:
    import wave
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SpeechCheckError(f"{path}: expected mono 16-bit PCM")
        return w.readframes(w.getnframes()), w.getframerate()


def cache_load() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            with open(cache_file(), encoding="utf-8") as fh:
                _CACHE = json.load(fh)
        except (OSError, ValueError):
            _CACHE = {}
    return _CACHE


def cache_save() -> None:
    if not _CACHE_DIRTY or _CACHE is None:
        return
    os.makedirs(cache_dir(), exist_ok=True)
    with open(cache_file(), "w", encoding="utf-8") as fh:
        json.dump(_CACHE, fh, indent=1, sort_keys=True)


def cache_key(final_text: str, env: dict) -> str:
    """Everything that determines the AUDIO. Nothing that determines the measurement — the
    measurement is re-derived from the cached WAV on every run."""
    sig = json.dumps([
        final_text, env["tts_backend"], env["voice"], env["speech_speed"], env["sentence_pause"],
        env["deess"], env["consonant_boost"], env["asr_engine"], env["asr_model"],
    ], sort_keys=True)
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()


def render(final_text: str, env: dict, use_cache: bool = True) -> dict:
    """Synthesise + transcribe + measure ONE string. The unit of CPU cost in this harness."""
    global _CACHE_DIRTY
    key = cache_key(final_text, env)
    store = cache_load()
    wav = os.path.join(cache_dir(), key + ".wav")
    hit = use_cache and key in store and os.path.exists(wav)

    if hit:
        pcm, rate = read_wav(wav)
        transcript, synth_s = store[key]["transcript"], store[key].get("synth_s", 0.0)
        _STATS["cached"] += 1
    else:
        t0 = time.time()
        pcm, rate = tts.synthesize(final_text)
        synth_s = round(time.time() - t0, 2)
        transcript = transcribe(pcm, rate)
        os.makedirs(cache_dir(), exist_ok=True)
        tts.write_wav(wav, pcm, rate)
        store[key] = {"final_text": final_text, "transcript": transcript,
                      "synth_s": synth_s, "rate": rate, "wav": wav}
        _CACHE_DIRTY = True
        _STATS["synthesized"] += 1

    silences, diag = internal_silences(pcm, rate)
    return {
        "final_text": final_text,
        "transcript": transcript,
        "silences": silences,
        "diag": diag,
        "duration_s": round(len(pcm) / 2.0 / rate, 3),
        "rate": rate,
        "synth_s": synth_s,
        "wav": wav,
        "cached": hit,
    }


# --------------------------------------------------------------------------------------------
# The clips. Twelve, deliberately — every one costs CPU on a machine that may be mid-conversation.
#   transform=True  -> the harness applies normalize_for_speech and the on-path stage is disabled
#   transform=False -> the identical path with the transform applied to nothing (the negative arm)
#   transform=None  -> the REAL, unpatched path, exactly as `say` renders it (the pacing clips)
# --------------------------------------------------------------------------------------------

CLIPS = [
    # id,   text,                                             transform, why
    ("G1", "zero point two point six", False,
     "instrument calibration: the recognizer CAN write dotted digits"),
    ("G2", "config dot pie", False,
     "instrument calibration: the recognizer CAN write a file extension"),
    ("R1", "The version is 0.2.6 and it is ready.", False,
     "AC5 negative arm — un-normalised, identical path"),
    ("R2", "The version is 0.2.6 and it is ready.", True,
     "AC4 — the acceptance case JJ named"),
    ("R3", "Open config.py.", True,
     "AC6 — file extension"),
    ("R4", "Check command_bridge.config.speech_speed now.", True,
     "AC6 — dotted identifier"),
    ("O1", "Wait... really?", True,
     "TC2 — an ellipsis is prosody, not three points"),
    ("O2", "It is ready.", True,
     "TC2 — a sentence-final period really is a sentence boundary"),
    ("O3", "Use a short clip, e.g. this one.", True,
     "TC2 — an abbreviation is not a dotted identifier"),
    # The pacing triple: the SAME WORDS, differing only in the punctuation under test. This is a
    # stronger design than one combined clip, because attributing gaps inside a combined clip by
    # index breaks precisely when a gap is 0 ms — which is the state being measured.
    ("Pa", "We shipped it, and then we tested it.", None,
     "AC8 — one comma, no sentence break"),
    ("Pb", "We shipped it. And then we tested it.", None,
     "AC9 — the same break as a sentence boundary"),
    ("Pc", "We shipped it and then we tested it.", None,
     "AC10 — neither, and TC2's ordinary-prose control"),
]

WORD_POINT = re.compile(r"\bpoints?\b", re.I)
WORD_DOT = re.compile(r"\bdots?\b", re.I)

# The kokoro backend's OWN sentence splitter, copied from `tts._ResidentKokoro.synthesize`. It is
# reproduced here — not imported, because it is a local in that method — so every measured silence
# can be ATTRIBUTED rather than admired. A gap that this regex predicts is the engine's injected
# `quiet(int(rate * pause))`; a gap it does not predict came from the voice model itself.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def split_count(text: str) -> int:
    return len(SENTENCE_SPLIT.split(text.strip())) - 1


def longest_silence_ms(rec: dict) -> float:
    return max((s["ms"] for s in rec["silences"]), default=0.0)


def check(status: str, ac: str, claim: str, measured: str) -> dict:
    return {"status": status, "ac": ac, "claim": claim, "measured": measured}


def verdict(ok: bool, blocked: bool = False) -> str:
    if blocked:
        return "BLOCKED"
    return "PASS" if ok else "FAIL"


def build_clips(env: dict, normalize, use_cache: bool) -> dict:
    """Render every clip. `normalize` is None when Slice A has not landed."""
    out = {}
    for cid, text, transform, why in CLIPS:
        if transform is None:                       # the real path, untouched
            rec = render(text, env, use_cache)
            rec["mode"] = "real path (unpatched)"
        elif transform is False:                    # negative arm — transform applied to nothing
            with on_path_transform_disabled() as patched:
                rec = render(text, env, use_cache)
            rec["mode"] = f"transform OFF (patched: {', '.join(patched) or 'nothing on path'})"
        else:                                       # positive arm — transform applied exactly once
            if normalize is None:
                rec = render(text, env, use_cache)
                rec["mode"] = "BASELINE — transform unavailable, raw text rendered"
                rec["baseline"] = True
            else:
                final = normalize(text)
                with on_path_transform_disabled() as patched:
                    rec = render(final, env, use_cache)
                rec["mode"] = f"transform ON (applied by harness; patched: " \
                              f"{', '.join(patched) or 'nothing on path'})"
        rec["id"], rec["source_text"], rec["why"] = cid, text, why
        rec["pause_ms"] = env["sentence_pause"] * 1000.0
        out[cid] = rec
    return out


def print_findings(clips: dict, env: dict) -> None:
    """Attribute every measured silence to a mechanism, and say so when that contradicts the spec.

    This block exists because the spec's Measured ground truth section states flatly that "there
    is no silence to remove". That is true of the clips it measured and false of two clips here,
    and the difference is not noise — it is a rule the spec had not isolated. Reported, not
    resolved: choosing between the spec's table and this measurement is not a harness's job.
    """
    pause_ms = env["sentence_pause"] * 1000.0
    rows = []
    for cid, _text, _tr, _why in CLIPS:
        rec = clips[cid]
        n = split_count(rec["final_text"])
        gaps = [s for s in rec["silences"] if s["ms"] >= MIN_SILENCE_MS]
        if n or gaps:
            rows.append((cid, rec, n, gaps))
    if not rows:
        return
    print("\nFINDINGS — where every measured silence came from")
    print(f"  The kokoro backend splits on {SENTENCE_SPLIT.pattern} and joins the pieces with "
          f"exactly {pause_ms:.0f} ms")
    print("  of digital zeros (tts._ResidentKokoro.synthesize -> tts.quiet). So the rule is a dot "
          "FOLLOWED BY")
    print("  WHITESPACE, not a dot. A dot with no space after it is elided by the voice model and "
          "makes no gap.")
    for cid, rec, n, gaps in rows:
        pred = n * pause_ms
        got = sum(g["ms"] for g in gaps)
        print(f"    {cid}  {rec['final_text']!r}")
        print(f"         predicted {n} x {pause_ms:.0f} = {pred:.0f} ms   measured "
              f"{got:.0f} ms in {len(gaps)} run(s) >= {MIN_SILENCE_MS:.0f} ms   "
              f"heard {rec['transcript']!r}")


def run_checks(clips: dict, normalize, env: dict) -> list:
    """Every acceptance criterion this slice can judge, with the measured number beside it."""
    blocked = normalize is None
    checks = []

    # -- AC7 first: every number below is worthless if the wrong engine answered ----------------
    # This is the trap that already cost one measurement on this spec. `config` does not read the
    # settings file on import, so a harness that forgets `load_env_file()` measures piper at 1.18
    # while the listener is on kokoro at 1.2 — and prints entirely plausible numbers. Asserted,
    # not narrated: the value in use must be the value IN THE FILE.
    fv = env["file_values"]
    want_speed = fv.get("COMMAND_BRIDGE_SPEECH_SPEED")
    want_tts = fv.get("COMMAND_BRIDGE_TTS")
    ac7 = (want_speed is not None and abs(float(want_speed) - env["speech_speed"]) < 1e-9
           and want_tts is not None and want_tts.lower() == env["tts_backend"])
    checks.append(check(
        verdict(ac7), "AC7",
        "the run used the LIVE settings file, not the module defaults (TC3)",
        f"file says COMMAND_BRIDGE_TTS={want_tts} COMMAND_BRIDGE_SPEECH_SPEED={want_speed}; "
        f"in use backend={env['tts_backend']} speed={env['speech_speed']} "
        f"(module default would have been piper-era {env['module_default_speed']}); "
        f"voice={env['voice']} sentence_pause={env['sentence_pause']}s"))

    # -- the instrument itself, before any verdict about the product ---------------------------
    # AC8 asks for a silence where there is none today. Before believing a 0 ms reading, the
    # detector has to be shown finding a silence of a KNOWN length: `tts` injects exactly
    # `sentence_pause` seconds of digital zeros between sentences, so Pb carries a ruler.
    expect_ms = env["sentence_pause"] * 1000.0
    pb = longest_silence_ms(clips["Pb"])
    checks.append(check(
        verdict(pb >= expect_ms * 0.9), "INSTR",
        f"the detector recovers the {expect_ms:.0f} ms of silence tts.quiet() demonstrably injects "
        f"between sentences",
        f"measured {pb:.1f} ms vs {expect_ms:.0f} ms injected "
        f"({pb - expect_ms:+.1f} ms of the voice's own lead-in/tail-off)"))

    g1 = clips["G1"]["transcript"]
    checks.append(check(
        verdict("0.2.6" in g1), "INSTR",
        "the recognizer writes '0.2.6' when the dots are voiced (spec ground truth)",
        f"heard {g1!r}"))
    g2 = clips["G2"]["transcript"]
    checks.append(check(
        verdict("config.py" in g2.lower()), "INSTR",
        "the recognizer writes 'config.py' when the dot is voiced (spec ground truth)",
        f"heard {g2!r}"))

    # -- AC5 first: the negative arm is what gives AC4 its meaning -----------------------------
    r1 = clips["R1"]["transcript"]
    checks.append(check(
        verdict("026" in r1 and "0.2.6" not in r1), "AC5",
        "un-normalised '0.2.6' still arrives as '026' through the identical path",
        f"heard {r1!r}"))

    r2 = clips["R2"]["transcript"]
    checks.append(check(
        verdict("0.2.6" in r2, blocked), "AC4",
        "normalised 'The version is 0.2.6 and it is ready.' round-trips containing '0.2.6'",
        f"heard {r2!r}" + ("  [baseline: transform unavailable]" if blocked else "")))

    r3 = clips["R3"]["transcript"]
    checks.append(check(
        verdict("config.py" in r3.lower(), blocked), "AC6a",
        "a file extension round-trips with its dot",
        f"heard {r3!r}" + ("  [baseline: transform unavailable]" if blocked else "")))

    r4 = clips["R4"]["transcript"]
    checks.append(check(
        verdict(".config." in r4.lower(), blocked), "AC6b",
        "a dotted identifier round-trips with its dots ('.config.' present)",
        f"heard {r4!r}" + ("  [baseline: transform unavailable]" if blocked else "")))

    # -- TC2: over-normalising is silent, so it only surfaces as a failing check ---------------
    for cid, label in (("O1", "an ellipsis"), ("O2", "a sentence-final period"),
                       ("O3", "the abbreviation 'e.g.'"), ("Pc", "ordinary prose, no dotted term")):
        t = clips[cid]["transcript"]
        spurious = WORD_POINT.findall(t) + WORD_DOT.findall(t)
        note = "" if not blocked or cid == "Pc" else "  [control: transform unavailable]"
        checks.append(check(
            verdict(not spurious, blocked and cid != "Pc"), f"TC2/{cid}",
            f"{label} gains no spoken 'point' or 'dot'",
            f"heard {t!r}; spurious={spurious or 'none'}{note}"))

    # -- pacing, measured off the rendered PCM -------------------------------------------------
    comma = longest_silence_ms(clips["Pa"])
    sentence = longest_silence_ms(clips["Pb"])
    neither = longest_silence_ms(clips["Pc"])

    checks.append(check(
        verdict(comma >= MIN_SILENCE_MS), "AC8",
        f"a comma inside a sentence produces a silence >= {MIN_SILENCE_MS:.0f} ms",
        f"comma gap = {comma:.1f} ms   (all runs: {clips['Pa']['silences'] or 'none'})"))

    checks.append(check(
        verdict(sentence > comma and comma >= MIN_SILENCE_MS), "AC9",
        "the same break as a sentence boundary is LONGER than the comma",
        f"sentence gap = {sentence:.1f} ms vs comma {comma:.1f} ms   "
        f"(all runs: {clips['Pb']['silences'] or 'none'})"))

    checks.append(check(
        verdict(neither < MIN_SILENCE_MS), "AC10",
        f"the same words with neither gain no internal silence >= {MIN_SILENCE_MS:.0f} ms",
        f"longest internal run = {neither:.1f} ms   (all runs: {clips['Pc']['silences'] or 'none'})"))

    return checks


# --------------------------------------------------------------------------------------------
# Reporting — sharpness.py's habit: say what was measured, not just the verdict
# --------------------------------------------------------------------------------------------

def print_header(env: dict, normalize, why_missing: str | None) -> None:
    print("speechcheck — spec 008 acceptance measurement (no server started; TC4)")
    print("-" * 96)
    print(f"  settings file  : {env['settings_file']}  "
          f"(exists={env['settings_exists']}, {env['settings_applied']} keys applied"
          + (f", shadowed by env: {env['settings_shadowed']}" if env["settings_shadowed"] else "")
          + ")")
    print(f"  TTS            : backend={env['tts_backend']}  voice={env['voice']}  "
          f"speed={env['speech_speed']}  sentence_pause={env['sentence_pause']}s  "
          f"deess={env['deess']}  consonant_boost={env['consonant_boost']}")
    print(f"  ASR            : engine={env['asr_engine']}  model={env['asr_model']}")
    if normalize is None:
        print(f"  TRANSFORM      : NOT PRESENT — {why_missing}")
        print("                   Slice A has not landed. Positive arms report the UN-NORMALISED")
        print("                   baseline and are marked BLOCKED, never PASS.")
    else:
        print("  TRANSFORM      : command_bridge.speech.normalize_for_speech  AVAILABLE")
    print(f"  silence detect : {WINDOW_MS:.0f} ms windows, {SILENCE_REL_DB:.0f} dB below the "
          f"clip's peak window, runs >= {MIN_SILENCE_MS:.0f} ms count, "
          f">= {REPORT_SILENCE_MS:.0f} ms reported, padding trimmed")
    print("-" * 96)


def print_clips(clips: dict) -> None:
    print("\nCLIPS")
    for cid, _text, _tr, _why in CLIPS:
        rec = clips[cid]
        tag = "cache" if rec.get("cached") else f"{rec['synth_s']}s"
        print(f"  {cid}  [{tag:>6}] {rec['why']}")
        print(f"        source    {rec['source_text']!r}")
        if rec["final_text"] != rec["source_text"]:
            print(f"        spoken    {rec['final_text']!r}")
        print(f"        mode      {rec['mode']}")
        print(f"        heard     {rec['transcript']!r}")
        d = rec["diag"]
        print(f"        audio     {rec['duration_s']}s @ {rec['rate']}Hz, "
              f"speech span {d.get('speech_span_s')}s, "
              f"pad {d.get('lead_pad_ms')}/{d.get('trail_pad_ms')}ms trimmed")
        print(f"        levels    peak win rms {d.get('peak_window_rms')}, "
              f"threshold {d.get('threshold_rms')}, "
              f"in-speech p10 {d.get('p10_inside_rms')}, quietest {d.get('quietest_inside_rms')}, "
              f"quiet headroom {d.get('quiet_headroom_db')} dB")
        n = split_count(rec["final_text"])
        print(f"        splits    the engine sees {n + 1} sentence(s) -> "
              f"{n} x {rec.get('pause_ms', 0):.0f} ms injected by tts.quiet()")
        none = f"none >= {REPORT_SILENCE_MS:.0f} ms"
        print(f"        silences  {rec['silences'] or none}")


def print_checks(checks: list) -> int:
    print("\nCHECKS")
    for c in checks:
        print(f"  {c['status']:<7} {c['ac']:<8} {c['claim']}")
        print(f"                   {c['measured']}")
    n_pass = sum(c["status"] == "PASS" for c in checks)
    n_fail = sum(c["status"] == "FAIL" for c in checks)
    n_block = sum(c["status"] == "BLOCKED" for c in checks)
    print("-" * 96)
    print(f"  {n_pass} PASS   {n_fail} FAIL   {n_block} BLOCKED   "
          f"({_STATS['synthesized']} clips synthesized, {_STATS['cached']} from cache)")
    return 0 if (n_fail == 0 and n_block == 0) else 1


# --------------------------------------------------------------------------------------------
# `prove` — watch the instrument go red on a claim known to be false
# --------------------------------------------------------------------------------------------

def prove(env: dict, use_cache: bool) -> int:
    print("\nPROVE — feeding the checks two claims that are KNOWN TO BE FALSE.")
    print("Both lines below MUST read FAIL. A check that cannot go red is not a check.\n")

    with on_path_transform_disabled():
        r1 = render("The version is 0.2.6 and it is ready.", env, use_cache)
    pc = render("We shipped it and then we tested it.", env, use_cache)

    inverted = [
        check(verdict("0.2.6" in r1["transcript"]), "INV1",
              "[FALSE CLAIM] the UN-normalised string round-trips containing '0.2.6'",
              f"heard {r1['transcript']!r}"),
        check(verdict(longest_silence_ms(pc) >= MIN_SILENCE_MS), "INV2",
              f"[FALSE CLAIM] a clip with no comma and no sentence break has a silence "
              f">= {MIN_SILENCE_MS:.0f} ms",
              f"longest internal run = {longest_silence_ms(pc):.1f} ms "
              f"(all runs: {pc['silences'] or 'none'})"),
    ]
    for c in inverted:
        print(f"  {c['status']:<7} {c['ac']:<8} {c['claim']}")
        print(f"                   {c['measured']}")

    fired = [c for c in inverted if c["status"] == "FAIL"]
    print("-" * 96)
    if len(fired) == len(inverted):
        print(f"  INSTRUMENT FIRES: {len(fired)}/{len(inverted)} false claims went red, as required.")
        print(f"  ({_STATS['synthesized']} clips synthesized, {_STATS['cached']} from cache)")
        return 0
    print(f"  INSTRUMENT DID NOT FIRE: only {len(fired)}/{len(inverted)} false claims went red.")
    print("  The checks above cannot be trusted — a green run from them would mean nothing.")
    return 1


DESCRIBE = {
    "tool": "scripts/speechcheck.py",
    "one_line": "Spec 008 acceptance: does a dotted term SURVIVE synthesis, and does a comma BREATHE?",
    "why_it_exists": (
        "Spec 008's audible half cannot live in tests/ — that directory holds no mic and no model "
        "(AGENTS.md Layout). The recognizer is the judge for FR2 and the rendered PCM is the judge "
        "for FR3, because the agent that produced the audio cannot hear it (TC1)."
    ),
    "measures": [
        "Round trip: synthesize -> transcribe -> assert on the returned STRING (AC4, AC5, AC6).",
        "Internal silence per clip: 10 ms RMS windows, -50 dB below the clip's own peak window, "
        "leading/trailing pad trimmed, runs >= 60 ms counted (AC8, AC9, AC10).",
        "Over-normalisation counter-cases: no spoken 'point'/'dot' appears where none belongs (TC2).",
    ],
    "does_NOT_measure": [
        "The transform's STRING behaviour — that is AC1/AC2/AC3 and belongs in tests/.",
        "AC11-AC13 (the CLI inspector and the per-clip record) — those are unit-testable and are "
        "not this harness's slice.",
        "Whether the on-path stage is wired up (AC3). The round-trip clips deliberately PATCH IT "
        "OUT so the negative arm stays separable; that is a different question from FR5.",
        "Anything a listener would call quality. It answers 'did the term arrive' and 'is there a "
        "gap', nothing about how pleasant either is.",
        "END-TO-END retrieval through a running `say` — no server may be started (TC4).",
    ],
    "choices_i_made": {
        "silence_threshold": f"{SILENCE_REL_DB:.0f} dB below the clip's own peak 10 ms window, not "
                             f"an absolute floor. tts.pad prepends 100 ms and appends 200 ms of "
                             f"zeros for Bluetooth sinks; an absolute floor finds that padding and "
                             f"calls it prosody.",
        "min_silence_ms": f"{MIN_SILENCE_MS:.0f} ms to count, {REPORT_SILENCE_MS:.0f} ms to report. "
                          f"60 ms is the spec's own threshold, kept so these numbers are comparable "
                          f"with the 2026-08-19 ground truth.",
        "pacing_triple": "Pa/Pb/Pc are the SAME WORDS differing only in punctuation, rather than one "
                         "clip holding both breaks. Attributing gaps inside a combined clip by index "
                         "breaks exactly when a gap is 0 ms, which is the state being measured.",
        "both_arms_patched": "The round-trip clips disable the on-path transform and the harness "
                             "applies it to exactly one arm. Once FR1 puts normalisation on the "
                             "path, an unpatched negative arm is normalised too and AC5 becomes a "
                             "second copy of AC4 that passes and means nothing.",
        "no_local_transform": "If command_bridge.speech.normalize_for_speech is missing, the positive "
                              "arms report the un-normalised BASELINE and are marked BLOCKED. This "
                              "harness never implements the thing it measures.",
        "cache": "Measurements are cached under LOCALAPPDATA keyed on the FINAL STRING plus the full "
                 "engine identity (backend/voice/speed/pause/deess/boost/ASR). Every clip costs CPU "
                 "on a machine that may be carrying a live conversation (TC4). --no-cache disables it.",
    },
    "commands": {
        "describe": "python scripts/speechcheck.py describe",
        "run": "python scripts/speechcheck.py run [--no-cache] [--json]",
        "prove": "python scripts/speechcheck.py prove [--no-cache]   # the checks must go RED",
    },
    "exit_codes": {"0": "every check passed", "1": "a check failed or is blocked", "2": "bad setup"},
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="speechcheck.py",
        description="Spec 008 acceptance measurement — round trip through the recognizer plus "
                    "internal-silence measurement. Starts no server.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("describe", help="what this measures and what it does NOT measure")
    sp = sub.add_parser("run", help="the full acceptance run")
    sp.add_argument("--no-cache", action="store_true", help="re-synthesize every clip")
    sp.add_argument("--json", action="store_true", help="also dump the raw measurements as JSON")
    sp = sub.add_parser("prove", help="feed the checks a false claim and watch them go red")
    sp.add_argument("--no-cache", action="store_true", help="re-synthesize every clip")

    args = p.parse_args(argv)
    cmd = args.cmd or "describe"

    if cmd == "describe":
        print(json.dumps(DESCRIBE, indent=2))
        return 0

    try:
        env = engine_settings()
        if env["tts_backend"] == "none":
            raise SpeechCheckError(
                "COMMAND_BRIDGE_TTS is 'none' — every clip would be silence and every transcript "
                "empty, and the run would look plausible. Remedy:\n"
                "    command-bridge config set COMMAND_BRIDGE_TTS kokoro")
        normalize, why_missing = load_transform()
        use_cache = not args.no_cache

        if cmd == "prove":
            print_header(env, normalize, why_missing)
            rc = prove(env, use_cache)
            cache_save()
            return rc

        print_header(env, normalize, why_missing)
        clips = build_clips(env, normalize, use_cache)
        checks = run_checks(clips, normalize, env)
        print_clips(clips)
        print_findings(clips, env)
        rc = print_checks(checks)
        if args.json:
            print("\nRAW\n" + json.dumps({"engine": env, "clips": clips, "checks": checks},
                                         indent=2, default=str))
        cache_save()
        return rc
    except SpeechCheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
