"""An OBJECTIVE ear: score a WAV for the "piercing / harsh / fatiguing" percept.

WHY THIS EXISTS
---------------
Three de-esser implementations shipped in one sitting (see `_deess` in command_bridge/tts.py and
the 2026-08-15 entry in the project note). Each was measured as working. Every one landed on the
owner as *"I didn't feel a difference."* The failure was not in the DSP:

    A metric authored by the same pass that authors the fix is not evidence, it is the fix
    restating itself.

Every check those passes ran -- high-band energy share, per-band dB, a band-ratio test -- was a
proxy chosen alongside the change it was there to judge, so it could only agree. The listener was
the only real instrument in the loop, and he was being spent one clip at a time.

This module does not invent a metric. It runs a PUBLISHED one, standardised decades before this
project existed, against reference signals whose correct answers were fixed by listening tests
this repository had nothing to do with.

WHAT IT MEASURES
----------------
**Sharpness, in acum, per DIN 45692:2009**, computed from ISO 532-1 (Zwicker) specific loudness.
Sharpness is the psychoacoustic literature's name for exactly the percept being complained about:
the sensation that a sound is piercing/shrill, driven by where its loudness sits on the critical
band scale rather than by how loud it is.

    S = 0.11 * integral(0..24 Bark) N'(z) g(z) z dz / N

with N'(z) the specific loudness [sone/Bark], N the total loudness [sone], and g(z) the DIN 45692
weighting that grows exponentially above 15.8 Bark (~3.15 kHz):

    g(z) = 1                                  for z <= 15.8 Bark
    g(z) = 0.15 * exp(0.42 * (z - 15.8)) + 0.85   for z >  15.8 Bark

1 acum is defined as a 1 kHz narrow-band noise (bandwidth < 150 Hz) at 60 dB.

REFERENCES
----------
* DIN 45692:2009-08, "Measurement technique for the simulation of the auditory sensation of
  sharpness" -- the model, its weighting function, the acum reference, and the 41 reference
  signals used by `validate`.
* ISO 532-1:2017, "Acoustics -- Methods for calculating loudness -- Part 1: Zwicker method" --
  supplies the specific loudness N'(z) that sharpness is computed from.
* E. Zwicker and H. Fastl, "Psychoacoustics: Facts and Models", 3rd ed., Springer 2007 --
  the underlying model, the N5 percentile convention, and the psychoacoustic annoyance model
  whose sharpness term only becomes non-zero above S = 1.75 acum.
* MOSQITO (Eomys, Apache-2.0) -- the implementation actually called here.
  https://github.com/Eomys/MoSQITo
* JND values quoted by `describe`: Pedrielli, Carletti & Casazza, "Just noticeable differences
  of loudness and sharpness for earth moving machines" (0.04 acum); You & Jeon, refrigerator
  noise (0.08 acum).

WHAT IT DOES *NOT* MEASURE -- read `describe`, this list is the honest half of the tool.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------------------------
# Choices the published model leaves open. Every one of these is a decision I made, not
# something DIN 45692 or ISO 532-1 dictates, and each is exposed as a flag so it can be argued
# with rather than discovered by reading the source.
# --------------------------------------------------------------------------------------------

# CHOICE 1 -- playback level. A WAV file carries no absolute level, and sharpness is computed
# from LOUDNESS, which is level-dependent. Something has to decide what SPL the file represents.
# Every clip is therefore normalised to the same active-speech level before scoring, which is
# also what a fair listening test would do. It matters here specifically: de-esser attempt #1
# ducked whole frames and the downstream peak normaliser handed part of the level back, so a
# metric that let level in would have scored a pure loudness change as a timbre change.
# `score --spl-sweep` reports how load-bearing this choice actually is.
DEFAULT_SPL_DB = 65.0

# CHOICE 2 -- bandwidth matching. g(z) grows exponentially in the top Barks, so a recording that
# physically cannot contain 8-11 kHz will always score lower than one that can, for reasons that
# have nothing to do with timbre. The owner's own speech is captured at 16 kHz (8 kHz ceiling);
# Piper renders at 22.05 kHz (11 kHz ceiling). Comparing them raw would credit the TTS with
# sharpness it earned from bandwidth alone. So everything is low-passed to a common ceiling.
DEFAULT_BAND_HZ = 8000.0

# CHOICE 3 -- which number is "the" sharpness of a time-varying signal. DIN 45692 defines
# sharpness for a STATIONARY sound; speech is not stationary. The convention borrowed here is
# Zwicker & Fastl's percentile form (N5 = loudness exceeded 5% of the time), applied to
# sharpness: S05 is the value exceeded in 5% of frames. It is reported as the headline because
# it tracks the fricatives that are actually being complained about, while the mean is dragged
# toward silence. Mean-over-active-frames is reported beside it so the choice is visible.
DEFAULT_NPERSEG = 4096  # ~85 ms at 48 kHz, ~43 ms hop

# NOT a choice -- mosqito/ISO 532-1 declare specific loudness invalid below this, and mosqito
# itself zeroes sharpness there. Used as the speech-active gate so the gate is the model's, not
# mine.
LOUDNESS_VALID_SONE = 0.1

# Published just-noticeable differences for sharpness. These are what turn "the metric moved"
# into "he could have heard it", and they are the reason this tool can contradict its own
# author. Two independent studies, kept separate rather than averaged.
JND_ACUM_SMALL = 0.04   # Pedrielli, Carletti & Casazza (earth moving machines)
JND_ACUM_LARGE = 0.08   # You & Jeon (refrigerator noise)

# Zwicker & Fastl's psychoacoustic annoyance model gives sharpness ZERO weight below this, i.e.
# below 1.75 acum sharpness is not considered to contribute to annoyance at all.
PA_SHARPNESS_KNEE = 1.75

DIN_REF_BASE = ("https://raw.githubusercontent.com/Eomys/MoSQITo/master/"
                "validations/sq_metrics/sharpness_din/input")

# DIN 45692:2009-08 chapter 6 reference signals and their standard-specified sharpness [acum].
# These values come from the standard, NOT from this repository and NOT from this tool. They are
# the whole point: an instrument that reproduces them is answering to something it did not write.
DIN_BROADBAND = {
    "broadband_250.wav": 2.70, "broadband_350.wav": 2.74, "broadband_450.wav": 2.78,
    "broadband_570.wav": 2.85, "broadband_700.wav": 2.91, "broadband_840.wav": 2.96,
    "broadband_1000.wav": 3.05, "broadband_1170.wav": 3.12, "broadband_1370.wav": 3.20,
    "broadband_1600.wav": 3.30, "broadband_1850.wav": 3.42, "broadband_2150.wav": 3.53,
    "broadband_2500.wav": 3.69, "broadband_2900.wav": 3.89, "broadband_3400.wav": 4.12,
    "broadband_4000.wav": 4.49, "broadband_4800.wav": 5.04, "broadband_5800.wav": 5.69,
    "broadband_7000.wav": 6.47, "broadband_8500.wav": 7.46,
}
DIN_NARROWBAND = {
    "narrowband_250.WAV": 0.38, "narrowband_350.wav": 0.49, "narrowband_450.wav": 0.60,
    "narrowband_570.wav": 0.71, "narrowband_700.wav": 0.82, "narrowband_840.wav": 0.93,
    "narrowband_1000.wav": 1.00, "narrowband_1170.wav": 1.13, "narrowband_1370.wav": 1.26,
    "narrowband_1600.wav": 1.35, "narrowband_1850.wav": 1.49, "narrowband_2150.wav": 1.64,
    "narrowband_2500.wav": 1.78, "narrowband_2900.wav": 2.06, "narrowband_3400.wav": 2.40,
    "narrowband_4000.wav": 2.82, "narrowband_4800.wav": 3.48, "narrowband_5800.wav": 4.43,
    "narrowband_7000.wav": 5.52, "narrowband_8500.wav": 6.81, "narrowband_10500.wav": 8.55,
}

P_REF = 20e-6  # Pa, the 0 dB SPL reference


class SharpnessError(RuntimeError):
    """Raised with a runnable remedy, matching this repo's `doctor`/`remedy` convention."""


def _require_mosqito():
    """Import mosqito, or fail with the command that fixes it.

    Kept as a function rather than a module-level import so `describe` and `--help` work on a
    machine that has never installed it -- the one moment a reader most needs to be told what
    to install.
    """
    try:
        from mosqito.sq_metrics import loudness_zwst_perseg, sharpness_din_from_loudness
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SharpnessError(
            f"mosqito is required and did not import ({exc}). Remedy:\n"
            f"    {sys.executable} -m pip install mosqito matplotlib\n"
            "  (mosqito hard-imports matplotlib and pyuff at package import time but declares "
            "only pyuff, so matplotlib must be installed explicitly.)"
        ) from exc
    return loudness_zwst_perseg, sharpness_din_from_loudness


# --------------------------------------------------------------------------------------------
# Signal conditioning
# --------------------------------------------------------------------------------------------

def read_wav_mono(path: str) -> tuple[np.ndarray, int]:
    """Return (float64 samples in [-1, 1], sample_rate). Channel 0 only.

    Uses the stdlib `wave` module rather than scipy so the instrument has one fewer thing that
    can disagree with the rest of the repo about what a WAV is. Session recordings here are
    16-bit mono PCM; Piper renders 16-bit mono PCM; that is the whole surface needed.
    """
    with wave.open(path, "rb") as w:
        n_ch, width, rate, n_frames = (
            w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())
        raw = w.readframes(n_frames)
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    else:
        raise SharpnessError(f"{path}: unsupported sample width {width * 8}-bit")
    if n_ch > 1:
        # Mic-on-the-LEFT is this project's recording convention (see the voiceprint work), so
        # channel 0 is the speaker by construction. Mixing channels would average his voice with
        # whatever the other channel holds.
        data = data.reshape(-1, n_ch)[:, 0]
    return np.ascontiguousarray(data), rate


def band_limit(x: np.ndarray, rate: int, ceiling_hz: float) -> np.ndarray:
    """Zero-phase low-pass so two clips can be compared over the same spectrum. No-op if the
    signal already stops below the ceiling.

    Zero-phase (filtfilt) rather than a causal filter because any group delay here would shift
    frame boundaries between the two clips being compared, and the comparison is the product.
    """
    if ceiling_hz <= 0 or ceiling_hz >= rate / 2.0 * 0.999:
        return x
    from scipy.signal import butter, sosfiltfilt
    sos = butter(8, ceiling_hz / (rate / 2.0), btype="low", output="sos")
    return sosfiltfilt(sos, x)


def resample_to(x: np.ndarray, rate: int, target: int) -> np.ndarray:
    """ISO 532-1 / DIN 45692 want >= 48 kHz. mosqito resamples internally with a warning; doing
    it here instead keeps the band-limit and the level calibration on a known rate."""
    if rate == target:
        return x
    from fractions import Fraction

    from scipy.signal import resample_poly
    f = Fraction(target, rate).limit_denominator(1000)
    return resample_poly(x, f.numerator, f.denominator)


def active_rms(x: np.ndarray, rate: int, frame_ms: float = 20.0) -> tuple[float, float]:
    """Active-speech RMS and the fraction of frames counted as active.

    CHOICE: this is a deliberate simplification of ITU-T P.56 ("Objective measurement of active
    speech level"). P.56 iterates an activity threshold against a margin; this takes frames
    within 30 dB of the 95th-percentile frame and calls those speech. Silence and inter-sentence
    pauses must be excluded or the level calibration would be set by how much dead air a clip
    happens to contain, which is not a property of the voice.
    """
    n = max(1, int(rate * frame_ms / 1000.0))
    usable = (x.size // n) * n
    if usable < n:
        return float(np.sqrt(np.mean(x ** 2)) or 1e-12), 1.0
    frames = x[:usable].reshape(-1, n)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    loud = np.percentile(rms, 95)
    if loud <= 0:
        return 1e-12, 0.0
    active = rms > loud * (10 ** (-30.0 / 20.0))
    if not active.any():
        return float(np.sqrt(np.mean(x ** 2))), 0.0
    return float(np.sqrt(np.mean(frames[active] ** 2))), float(active.mean())


def calibrate_to_spl(x: np.ndarray, rate: int, spl_db: float) -> tuple[np.ndarray, float]:
    """Scale to Pascals such that the ACTIVE speech level equals `spl_db` dB SPL.

    Returns (signal in Pa, the gain applied). See CHOICE 1.
    """
    rms, _ = active_rms(x, rate)
    target_pa = P_REF * (10 ** (spl_db / 20.0))
    gain = target_pa / max(rms, 1e-12)
    return x * gain, gain


def hf_energy_share(x: np.ndarray, rate: int, split_hz: float = 4000.0) -> float:
    """The OLD, self-authored proxy: fraction of energy above 4 kHz.

    Reported deliberately, and deliberately labelled `legacy_proxy` in the output. It is here so
    the two can be watched disagreeing: this is the number the failed de-esser passes optimised,
    and the project's ground-truth claim ("this voice puts ~55% of its energy above 4 kHz where
    natural speech is under 20%") is stated in these terms, so reproducing it is how this
    pipeline proves it is looking at the same audio everyone else was.

    It is NOT the metric. Energy share knows nothing about the ear.

    MEASURED CAVEAT, and it matters for reading the project's ground-truth claim. "55% of its
    energy above 4 kHz" does not reproduce under this definition: `en_GB-alan-medium` measures
    18.7% full-band and 22.8% energy-weighted per frame. 55% is recoverable only by summing
    MAGNITUDES rather than power, unweighted, across all frames including silence -- and 64% of
    the FFT bins of a 22.05 kHz signal lie above 4 kHz, so a flat spectrum scores ~64% on that
    definition before any brightness exists. The same definition rates natural speech at 43.7%,
    which would make natural speech nearly as "bright" as the TTS. So the number that was
    described as the measurement that finally explained the failure is itself definition-
    dependent, and the shipped detector's 0.55 threshold is a magnitude ratio, not an energy
    share. Power, energy-weighted, is used here.
    """
    if x.size < 2:
        return float("nan")
    win = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * win)) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / rate)
    total = spec.sum()
    if total <= 0:
        return float("nan")
    return float(spec[freqs >= split_hz].sum() / total)


# --------------------------------------------------------------------------------------------
# The measurement
# --------------------------------------------------------------------------------------------

def sharpness_of_signal(x: np.ndarray, rate: int, *, spl_db: float = DEFAULT_SPL_DB,
                        band_hz: float = DEFAULT_BAND_HZ, nperseg: int = DEFAULT_NPERSEG,
                        field_type: str = "free", weighting: str = "din") -> dict:
    """Score an in-memory signal. Returns the same dict shape `score` prints."""
    loudness_zwst_perseg, sharpness_din_from_loudness = _require_mosqito()

    native_rate = rate
    x = band_limit(x, rate, band_hz)
    x = resample_to(x, rate, 48000)
    rate = 48000
    x, gain = calibrate_to_spl(x, rate, spl_db)

    if x.size < nperseg * 2:
        raise SharpnessError(
            f"signal too short: {x.size} samples at {rate} Hz, need at least {nperseg * 2}")

    N, N_specific, _bark, time_axis = loudness_zwst_perseg(
        x, rate, nperseg=nperseg, noverlap=nperseg // 2, field_type=field_type)
    # Silent segments give N = 0 and mosqito divides by it before zeroing the result. The
    # warning is expected and the frames are dropped by the validity gate two lines down, so
    # silence it here rather than letting every run print a scary divide-by-zero.
    with np.errstate(divide="ignore", invalid="ignore"):
        S = np.atleast_1d(sharpness_din_from_loudness(N, N_specific, weighting=weighting))
    N = np.atleast_1d(N)
    S = np.nan_to_num(S, nan=0.0, posinf=0.0, neginf=0.0)

    # The gate is the MODEL's validity threshold, not a number I picked: mosqito/ISO 532-1
    # treat specific loudness below 0.1 sone as invalid and mosqito zeroes sharpness there.
    active = N >= LOUDNESS_VALID_SONE
    S_active = S[active]
    if S_active.size == 0:
        raise SharpnessError(
            "no frames above the ISO 532-1 validity threshold -- the clip is effectively silent "
            f"at {spl_db} dB SPL, or the calibration failed")

    # Energy share is measured on the band-limited, level-matched signal so it answers the same
    # question the sharpness number does.
    share = hf_energy_share(x / max(gain, 1e-12), rate)

    return {
        "sharpness_acum": {
            "s05": float(np.percentile(S_active, 95)),   # exceeded 5% of the time
            "s10": float(np.percentile(S_active, 90)),
            "mean_active": float(S_active.mean()),
            "median_active": float(np.median(S_active)),
            "max": float(S_active.max()),
        },
        "loudness_sone": {
            "n05": float(np.percentile(N[active], 95)),
            "mean_active": float(N[active].mean()),
        },
        "legacy_proxy": {
            "hf_energy_share_above_4k": share,
            "note": "the self-authored proxy the failed de-esser passes optimised; not the metric",
        },
        "frames": {
            "total": int(S.size),
            "active": int(active.sum()),
            "active_fraction": float(active.mean()),
            "duration_s": float(time_axis[-1]) if len(time_axis) else 0.0,
        },
        "settings": {
            "model": f"DIN 45692 sharpness ({weighting} weighting) over ISO 532-1 specific loudness",
            "native_sample_rate": native_rate,
            "band_limit_hz": band_hz,
            "target_spl_db": spl_db,
            "field_type": field_type,
            "nperseg": nperseg,
        },
    }


def score_file(path: str, **kw) -> dict:
    x, rate = read_wav_mono(path)
    out = sharpness_of_signal(x, rate, **kw)
    out["file"] = os.path.abspath(path)
    return out


def interpret(s05: float) -> dict:
    """Turn an acum number into something a reader can act on, using only published anchors."""
    return {
        "acum": s05,
        "vs_1khz_reference": f"{s05:.2f}x the sharpness of the 1 acum reference "
                             f"(1 kHz narrow-band noise at 60 dB)",
        "above_annoyance_knee": bool(s05 > PA_SHARPNESS_KNEE),
        "annoyance_knee_note": f"Zwicker & Fastl's psychoacoustic annoyance model gives sharpness "
                               f"zero weight at or below {PA_SHARPNESS_KNEE} acum",
        "jnd_acum": {"small_study": JND_ACUM_SMALL, "large_study": JND_ACUM_LARGE},
    }


def compare(a: dict, b: dict) -> dict:
    """Difference between two scores, judged against the PUBLISHED JND rather than against
    whether it looks like a big number.

    This is the function that lets the tool say "your fix is inaudible" to the person who wrote
    the fix, which is the entire reason the tool exists.
    """
    d = b["sharpness_acum"]["s05"] - a["sharpness_acum"]["s05"]
    mag = abs(d)
    if mag < JND_ACUM_SMALL:
        verdict = "BELOW both published JNDs -- predicted inaudible"
    elif mag < JND_ACUM_LARGE:
        verdict = (f"between the two published JNDs ({JND_ACUM_SMALL}-{JND_ACUM_LARGE} acum) -- "
                   "predicted borderline, do not claim an improvement")
    else:
        verdict = "above both published JNDs -- predicted audible"
    return {
        "delta_acum": d,
        "delta_percent": 100.0 * d / a["sharpness_acum"]["s05"],
        "verdict": verdict,
        "legacy_proxy_delta": (b["legacy_proxy"]["hf_energy_share_above_4k"]
                               - a["legacy_proxy"]["hf_energy_share_above_4k"]),
    }


# --------------------------------------------------------------------------------------------
# Validation against DIN 45692's own reference signals
# --------------------------------------------------------------------------------------------

def ref_dir() -> str:
    base = os.environ.get("SHARPNESS_REF_DIR")
    if base:
        return base
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(root, "command-bridge-sharpness", "din45692")


def fetch_reference(name: str) -> str:
    """Download one DIN 45692 reference signal into the cache. Cached outside the repo on
    purpose -- 41 WAVs is ~20 MB of binary that has no business in git history."""
    d = ref_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    url = f"{DIN_REF_BASE}/{name}"
    with urllib.request.urlopen(url, timeout=60) as r, open(path, "wb") as f:
        f.write(r.read())
    return path


def validate(tolerance: float = 0.05) -> dict:
    """Score all 41 DIN 45692 reference signals against the values printed in the standard.

    THIS IS THE POINT OF THE WHOLE FILE. The reference values were fixed by the standard's own
    listening tests; nothing in this repository influenced them. An instrument that reproduces
    them is answering to something it did not author, which is precisely what the three failed
    de-esser metrics could not do.

    Compliance is assessed per DIN 45692 chapter 6: within +/- 5%.

    Note these are scored RAW -- no band limit, no SPL normalisation, `wav_calib = 1` as the
    standard's own test harness does -- because here the goal is to reproduce the standard
    exactly, not to compare two clips.
    """
    loudness_zwst_perseg, sharpness_din_from_loudness = _require_mosqito()
    from mosqito.sq_metrics import sharpness_din_st

    rows = []
    for group, table in (("narrowband", DIN_NARROWBAND), ("broadband", DIN_BROADBAND)):
        for name, expected in sorted(table.items(), key=lambda kv: int(
                "".join(c for c in kv[0] if c.isdigit()))):
            path = fetch_reference(name)
            x, rate = read_wav_mono(path)
            got = float(sharpness_din_st(x, rate, weighting="din"))
            err = (got - expected) / expected
            rows.append({
                "signal": name, "group": group, "standard_acum": expected,
                "computed_acum": round(got, 4), "error_percent": round(100 * err, 2),
                "within_tolerance": bool(abs(err) <= tolerance),
            })
    passed = sum(r["within_tolerance"] for r in rows)
    errs = [abs(r["error_percent"]) for r in rows]
    ranked = sorted(rows, key=lambda r: abs(r["error_percent"]), reverse=True)
    return {
        "source": "DIN 45692:2009-08 chapter 6 reference signals",
        "signals": len(rows),
        "within_5_percent": passed,
        "outside_5_percent": len(rows) - passed,
        "mean_abs_error_percent": round(float(np.mean(errs)), 2),
        "max_abs_error_percent": round(float(np.max(errs)), 2),
        "worst": ranked[:5],
        "rows": rows,
    }


DESCRIBE = {
    "tool": "sharpness.py",
    "one_line": "Scores a WAV for the 'piercing/harsh' percept using DIN 45692 sharpness (acum).",
    "why_it_exists": (
        "Three de-esser implementations were each measured as working by a metric the same pass "
        "invented, and each landed on the listener as 'I didn't feel a difference'. A metric "
        "authored by the same pass that authors the fix is not evidence, it is the fix restating "
        "itself. This runs a model published in 2009 and validates against that standard's own "
        "reference signals, so it can disagree with whoever is using it."
    ),
    "measures": [
        "Sharpness in acum, DIN 45692:2009, from ISO 532-1 (Zwicker) specific loudness.",
        "S05 (the sharpness exceeded in 5% of frames) as the headline, following Zwicker & "
        "Fastl's N5 percentile convention for time-varying sounds.",
        "Loudness in sone (ISO 532-1), reported so a level change cannot masquerade as a "
        "timbre change.",
        "The legacy >4 kHz energy share, labelled as the discredited proxy, for comparison only.",
    ],
    "does_NOT_measure": [
        "AUDIBILITY on its own. A delta is only meaningful against the published JND "
        f"({JND_ACUM_SMALL}-{JND_ACUM_LARGE} acum); `compare` applies it, and will tell you your "
        "change is inaudible.",
        "Intelligibility. A clip can get less sharp and harder to understand; this cannot see "
        "that. (SII exists in mosqito and is not wired up here.)",
        "Roughness or fluctuation strength, so it does NOT compute Zwicker's full psychoacoustic "
        "annoyance -- mosqito has roughness but no fluctuation strength, so PA is unavailable.",
        "The PLAYBACK CHAIN. It scores the file. The listener hears a phone speaker or a "
        "Bluetooth codec, which has its own presence peak in the 5-8 kHz band being argued "
        "about. A clip that scores clean here can still arrive harsh on his phone.",
        "THIS LISTENER. acum is a population-average scale derived from listening tests on "
        "noise, not on speech, and not on him. It has never been calibrated against his ratings.",
        "Speech quality or MOS. NISQA/DNSMOS/PESQ predict quality; quality and harshness are "
        "different targets and a quality model can be blind to a voice that is merely fatiguing.",
        "Anything about level. Every clip is normalised to the same active-speech SPL by design.",
    ],
    "choices_i_made": {
        "playback_level": f"WAVs carry no absolute level and sharpness depends on loudness, so "
                          f"every clip is normalised to {DEFAULT_SPL_DB} dB SPL active speech "
                          f"(a simplification of ITU-T P.56). Override with --spl; test the "
                          f"sensitivity with --spl-sweep.",
        "bandwidth": f"Low-passed to {DEFAULT_BAND_HZ:.0f} Hz by default so a 16 kHz recording "
                     f"and a 22.05 kHz render are compared over the same spectrum. Without this "
                     f"the wider-band file wins on bandwidth alone. Override with --band.",
        "time_varying": "DIN 45692 defines sharpness for stationary sounds. Speech is scored "
                        "per segment and summarised as S05, with mean-over-active-frames "
                        "reported beside it.",
        "silence_gate": f"Frames below {LOUDNESS_VALID_SONE} sone are excluded -- that is "
                        f"ISO 532-1's own validity threshold, not a number chosen here.",
        "field_type": "'free' field, mosqito's default. 'diffuse' is arguably closer to a phone "
                      "at the ear; exposed as --field.",
    },
    "commands": {
        "describe": "python scripts/sharpness.py describe",
        "score": "python scripts/sharpness.py score <a.wav> [b.wav ...] [--spl 65] [--band 8000]",
        "compare": "python scripts/sharpness.py compare <before.wav> <after.wav>",
        "validate": "python scripts/sharpness.py validate   # 41 DIN 45692 reference signals",
    },
    "validated_against": (
        "DIN 45692:2009-08 chapter 6 -- 41 reference signals with sharpness values fixed by the "
        "standard. Run `validate`. This is external ground truth: the reference values predate "
        "this project and were not chosen by it."
    ),
    "references": [
        "DIN 45692:2009-08 Measurement technique for the simulation of the auditory sensation "
        "of sharpness",
        "ISO 532-1:2017 Acoustics -- Methods for calculating loudness -- Zwicker method",
        "Zwicker & Fastl, Psychoacoustics: Facts and Models, 3rd ed., Springer 2007",
        "MOSQITO (Apache-2.0) https://github.com/Eomys/MoSQITo",
        "Pedrielli, Carletti & Casazza -- JND 0.04 acum; You & Jeon -- JND 0.08 acum",
    ],
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="sharpness.py",
        description="Objective sharpness (DIN 45692, acum) for WAV files -- see `describe` for "
                    "what it does and, more importantly, what it does not measure.")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("describe", help="what this measures and what it does NOT measure")

    def add_common(sp):
        sp.add_argument("--spl", type=float, default=DEFAULT_SPL_DB,
                        help=f"active-speech level to normalise to, dB SPL (default {DEFAULT_SPL_DB})")
        sp.add_argument("--band", type=float, default=DEFAULT_BAND_HZ,
                        help=f"low-pass ceiling in Hz for fair comparison (default {DEFAULT_BAND_HZ:.0f}; "
                             f"0 disables)")
        sp.add_argument("--field", choices=["free", "diffuse"], default="free")
        sp.add_argument("--nperseg", type=int, default=DEFAULT_NPERSEG)
        sp.add_argument("--weighting", choices=["din", "aures", "bismarck", "fastl"],
                        default="din")

    sp = sub.add_parser("score", help="score one or more WAV files")
    sp.add_argument("wav", nargs="+")
    sp.add_argument("--spl-sweep", action="store_true",
                    help="also score at 55/65/75 dB SPL to show how load-bearing the level "
                         "calibration choice is")
    add_common(sp)

    sp = sub.add_parser("compare", help="score two WAVs and judge the delta against the JND")
    sp.add_argument("before")
    sp.add_argument("after")
    add_common(sp)

    sp = sub.add_parser("validate", help="reproduce DIN 45692's 41 reference signals")
    sp.add_argument("--full", action="store_true", help="print every row, not just the summary")

    args = p.parse_args(argv)
    cmd = args.cmd or "describe"

    try:
        if cmd == "describe":
            print(json.dumps(DESCRIBE, indent=2))
            return 0

        if cmd == "validate":
            out = validate()
            if not args.full:
                out.pop("rows")
            print(json.dumps(out, indent=2))
            return 0 if out["outside_5_percent"] == 0 else 0  # reported, not fatal

        kw = {"spl_db": args.spl, "band_hz": args.band, "nperseg": args.nperseg,
              "field_type": args.field, "weighting": args.weighting}

        if cmd == "score":
            results = []
            for path in args.wav:
                r = score_file(path, **kw)
                r["interpretation"] = interpret(r["sharpness_acum"]["s05"])
                if args.spl_sweep:
                    r["spl_sweep"] = {
                        f"{lvl:.0f}dB": round(
                            score_file(path, **{**kw, "spl_db": lvl})["sharpness_acum"]["s05"], 4)
                        for lvl in (55.0, 65.0, 75.0)}
                results.append(r)
            print(json.dumps(results if len(results) > 1 else results[0], indent=2))
            return 0

        if cmd == "compare":
            a = score_file(args.before, **kw)
            b = score_file(args.after, **kw)
            print(json.dumps({"before": a, "after": b, "comparison": compare(a, b)}, indent=2))
            return 0
    except SharpnessError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
