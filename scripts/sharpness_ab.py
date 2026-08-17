"""Point the objective ear at THIS project's audio: the voice, the de-essers, the alternatives.

Deliberately a separate file from `scripts/sharpness.py`, and the separation is the whole design.
`sharpness.py` is the instrument: it knows nothing about Piper, about `_deess`, or about what
anyone hoped the answer would be, and it is validated against DIN 45692's own reference signals.
This file is the EXPERIMENT that points it at things. If the two ever merge, the instrument
starts being maintained by the person who wants a particular result from it, which is exactly
the failure this whole effort exists to end.

    "you need to be able to verify this yourself objectively. Because so far you have given me
     solutions, and when I hear it, it doesn't feel like there's any change at all."
     -- JJ, 2026-08-15, stopping the de-esser work

Three things get measured here:

1. **Is the voice actually sharp?** `en_GB-alan-medium` against the owner's own recorded speech.
   the project notes records a ground truth from a different pass, by a different method: this voice puts
   ~55% of its energy above 4 kHz where natural speech is under 20%. If the sharpness metric does
   not also rate it high, the metric is wrong.

2. **Were the three de-essers audible?** All three were measured as working and all three landed
   as "I didn't feel a difference". A useful metric must show them as SMALL -- if it reports big
   improvements it has reproduced the original failure and should be thrown away. Deltas are
   judged against the published JND (0.04-0.08 acum), not against whether they look impressive.

3. **Does a different voice beat the filter?** The open Next Action is "a better voice"; this
   prices that against the de-esser without anyone having to listen to ten clips.

Attempts 2 and 3 run the REAL shipped `_deess` from voice_tunnel/tts.py. Attempt 1 was replaced
and no longer exists in the tree, so it is RECONSTRUCTED here from its description -- and the
reconstruction is checked against the effect size recorded at the time (-33% high-band energy),
so a bad reconstruction shows up as a failed check rather than as a quiet wrong answer.

    <repo>/venv/Scripts/python scripts/sharpness_ab.py            # everything
    <repo>/venv/Scripts/python scripts/sharpness_ab.py --json     # machine readable
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sharpness  # noqa: E402  the instrument

# A sentence with plenty of sibilance, because the complaint was specifically about the 's'.
# Same text for every voice and every setting, so the only thing varying is the processing.
SIBILANT_TEXT = ("The session status says the six systems are stable. "
                 "She says the assistant's synthesis sounds surprisingly sharp. "
                 "It is essential to assess whether this specific setting is sufficient.")

OUT_DIR = os.path.join(sharpness.ref_dir(), "..", "ab")


def _out(name: str) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    return os.path.abspath(os.path.join(OUT_DIR, name))


def write_wav(path: str, pcm: bytes, rate: int) -> str:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


# --------------------------------------------------------------------------------------------
# 1. Natural speech reference -- his own voice, from the sessions this project already recorded
# --------------------------------------------------------------------------------------------

ARCTIC_BASE = "http://festvox.org/cmu_arctic/cmu_arctic"
ARCTIC_SPEAKERS = {"awb": "Scottish male", "bdl": "US male", "slt": "US female"}


def arctic_reference(speaker: str = "awb", n_utts: int = 10) -> str:
    """Natural speech from the CMU ARCTIC corpus -- an EXTERNAL reference this project did not
    record and cannot have tuned.

    Why this rather than his own recordings, which was the first thing tried: his sessions are
    captured through a microphone this project has already measured as muffled -- a 405 Hz
    spectral centroid against 2629 Hz through a proper capture path (vault, 2026-07-29). Scored
    against that, ANY TTS looks piercing, and the comparison would be measuring his headset.
    ARCTIC is studio-recorded, 16 kHz mono, phonetically balanced, and freely redistributable.

    `awb` (Scottish male) is the default because `en_GB-alan-medium` is a British male voice and
    comparing across sex or accent would confound the thing being measured.
    """
    import urllib.request
    d = os.path.join(sharpness.ref_dir(), "..", "natural")
    os.makedirs(d, exist_ok=True)
    chunks = None
    rate = 16000
    for i in range(1, n_utts + 1):
        name = f"{speaker}_arctic_a{i:04d}.wav"
        path = os.path.join(d, name)
        if not (os.path.exists(path) and os.path.getsize(path) > 1000):
            url = f"{ARCTIC_BASE}/cmu_us_{speaker}_arctic/wav/arctic_a{i:04d}.wav"
            with urllib.request.urlopen(url, timeout=60) as r, open(path, "wb") as f:
                f.write(r.read())
        with wave.open(path) as f:
            rate = f.getframerate()
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        chunks = pcm if chunks is None else np.concatenate([chunks, pcm])
    out = _out(f"natural_arctic_{speaker}.wav")
    write_wav(out, chunks.astype(np.int16).tobytes(), rate)
    return out


def natural_speech(session: str = "live", max_seconds: float = 60.0) -> str:
    """Concatenate the owner's ADDRESSED turns from a recorded session into one WAV.

    Why his own recordings rather than a public speech corpus: they are the only natural speech
    that went through THIS microphone and THIS capture chain, so a difference against the TTS
    cannot be blamed on the recording path. The cost is that they are 16 kHz, which is exactly
    why `sharpness.py` band-limits everything to 8 kHz by default.

    `addressed` turns only -- 25 of the last 80 turns in one session were other people talking in
    the car, and a reference contaminated with other voices measures nothing.
    """
    wav_path = os.path.join(ROOT, "sessions", f"{session}.wav")
    log_path = os.path.join(ROOT, "sessions", f"{session}.jsonl")
    if not (os.path.exists(wav_path) and os.path.exists(log_path)):
        raise SystemExit(f"no recorded session at {wav_path} / {log_path}")

    with wave.open(wav_path) as f:
        rate, n = f.getframerate(), f.getnframes()
        pcm = np.frombuffer(f.readframes(n), dtype=np.int16)

    turns = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                turns.append(json.loads(line))

    chunks, total = [], 0.0
    for t in turns:
        if not t.get("addressed"):
            continue
        s, e = t.get("t_start"), t.get("t_end")
        if s is None or e is None or e <= s or e * rate > n:
            continue
        if e - s < 1.0:            # too short to hold a stable spectrum
            continue
        chunks.append(pcm[int(s * rate):int(e * rate)])
        total += e - s
        if total >= max_seconds:
            break
    if not chunks:
        raise SystemExit(f"no usable addressed turns in {log_path}")
    out = np.concatenate(chunks)
    path = _out(f"natural_{session}.wav")
    write_wav(path, out.astype(np.int16).tobytes(), rate)
    return path


# --------------------------------------------------------------------------------------------
# 2. The three de-esser attempts
# --------------------------------------------------------------------------------------------

def _frames(x: np.ndarray, size: int, hop: int):
    starts = np.arange(0, x.size - size + 1, hop)
    win = np.hanning(size + 1)[:size].astype(np.float32)
    return starts, win, np.stack([x[s:s + size] for s in starts]) * win


def attempt1_frame_duck(pcm: bytes, rate: int, strength: float = 0.6) -> bytes:
    """RECONSTRUCTION of the first de-esser: detect sibilant frames, duck the WHOLE frame.

    From the docstring of the code that replaced it: *"The first working de-esser detected
    sibilant frames correctly and then multiplied the entire frame by a gain. Measured, it
    removed 33% of the high-band energy. Listened to, it changed nothing."*

    Detection is identical to the shipped version (framed FFT, high-to-total ratio above 4 kHz).
    Only the ACTION differs: a broadband gain in the time domain rather than an attenuation of
    the offending bins. That difference is the entire point -- ducking makes an 's' quieter while
    leaving its spectrum exactly as harsh.
    """
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    size, hop = 512, 256
    if x.size < size * 2:
        return pcm
    starts, win, frames = _frames(x, size, hop)
    mag = np.abs(np.fft.rfft(frames, axis=1))
    bins = mag.shape[1] - 1
    detect = max(1, int(4000.0 / (rate / 2.0) * bins))
    ratio = mag[:, detect:].sum(axis=1) / (mag.sum(axis=1) + 1e-9)
    over = np.clip((ratio - 0.55) / 0.45, 0.0, 1.0)
    gain = 1.0 - (0.88 * strength) * over

    out = np.zeros(x.size, dtype=np.float32)
    norm = np.zeros(x.size, dtype=np.float32)
    for i, s in enumerate(starts):
        out[s:s + size] += frames[i] * gain[i] * win / (win + 1e-9) * win
        norm[s:s + size] += win * win
    covered = norm > 1e-6
    out[covered] /= norm[covered]
    out[~covered] = x[~covered]
    return (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def attempt2_spectral_no_shelf(pcm: bytes, rate: int, strength: float = 0.6) -> bytes:
    """RECONSTRUCTION of the second de-esser: attenuate bins above 4.5 kHz, DYNAMIC ONLY.

    This is the shipped `_deess` with the unconditional shelf removed (`floor = 0`). It is what
    shipped before the measurement that "55% of this voice's energy is above 4 kHz on EVERY
    frame" showed that a dynamic-only filter leaves a voice that is fatiguing on all of them.
    Recorded effect at the time: -40% of the in-band energy above 4.5 kHz. Still inaudible.
    """
    return _spectral(pcm, rate, strength, floor_frac=0.0)


def attempt3_shipped(pcm: bytes, rate: int, strength: float = 0.6) -> bytes:
    """The de-esser CURRENTLY IN THE TREE -- calls voice_tunnel.tts._deess directly.

    Not a reconstruction. Whatever is in tts.py is what gets scored, so this stays honest if the
    file changes underneath.
    """
    from voice_tunnel import config, tts
    old = os.environ.get("VOICE_TUNNEL_DEESS")
    os.environ["VOICE_TUNNEL_DEESS"] = str(strength)
    try:
        config._ENV_CACHE = {} if hasattr(config, "_ENV_CACHE") else None
        return tts._deess(pcm, rate)
    finally:
        if old is None:
            os.environ.pop("VOICE_TUNNEL_DEESS", None)
        else:
            os.environ["VOICE_TUNNEL_DEESS"] = old


def _spectral(pcm: bytes, rate: int, strength: float, floor_frac: float) -> bytes:
    """Shared implementation of the spectral de-esser, parameterised by the shelf floor.

    Mirrors voice_tunnel/tts.py::_deess. Duplicated rather than imported because the whole point
    is to run it with a floor the shipped code does not expose -- and editing tts.py to expose it
    would mean the measuring apparatus modifying the thing being measured.
    """
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    size, hop = 512, 256
    if x.size < size * 2:
        return pcm
    starts, win, frames = _frames(x, size, hop)
    spec = np.fft.rfft(frames, axis=1)
    mag = np.abs(spec)
    nyq, bins = rate / 2.0, spec.shape[1] - 1
    detect = max(1, int(4000.0 / nyq * bins))
    cut = max(1, int(4500.0 / nyq * bins))
    ratio = mag[:, detect:].sum(axis=1) / (mag.sum(axis=1) + 1e-9)
    over = np.clip((ratio - 0.55) / 0.45, 0.0, 1.0)
    gain = 1.0 - np.maximum((0.88 * strength) * over, floor_frac * strength)

    if float(mag[:, cut:].sum()) <= 1e-3 * float(mag.sum() + 1e-9):
        return pcm
    shaped = spec.copy()
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
    covered = norm > 1e-6
    out[covered] /= norm[covered]
    out[~covered] = x[~covered]
    return (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def high_shelf(pcm: bytes, rate: int, db: float, corner_hz: float = 4500.0) -> bytes:
    """A plain, unconditional high shelf of `db` above `corner_hz`. The POSITIVE CONTROL.

    Why this matters more than it looks. The DIN 45692 validation proves the metric reproduces
    the standard on STATIONARY NOISE. It does not prove the metric has any resolution on running
    speech, which is a different signal entirely -- and an instrument that is correct on the
    calibration bench and flat on the real material would fail exactly like the proxies it
    replaces, while looking rigorous.

    So: apply a known, monotonic, physically unambiguous brightness change and check the metric
    tracks it. If sharpness does not move monotonically with shelf gain on this voice, the metric
    is not usable here and the whole approach should be abandoned rather than defended.

    The by-product is the number the de-esser work actually needed: how many dB of shelf buys one
    published JND. That converts "make it less harsh" into an engineering target.
    """
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    size, hop = 1024, 512
    if x.size < size * 2:
        return pcm
    starts, win, frames = _frames(x, size, hop)
    spec = np.fft.rfft(frames, axis=1)
    bins = spec.shape[1] - 1
    nyq = rate / 2.0
    cut = max(1, int(corner_hz / nyq * bins))
    g = 10 ** (db / 20.0)
    ramp_end = min(spec.shape[1], cut + max(1, int(1000.0 / nyq * bins)))
    shaped = spec.copy()
    if ramp_end > cut:
        ramp = np.linspace(0.0, 1.0, ramp_end - cut, dtype=np.float32)
        shaped[:, cut:ramp_end] *= (1.0 + ramp * (g - 1.0))
    shaped[:, ramp_end:] *= g
    out = np.zeros(x.size, dtype=np.float32)
    norm = np.zeros(x.size, dtype=np.float32)
    rebuilt = np.fft.irfft(shaped, n=size, axis=1).astype(np.float32) * win
    for i, s in enumerate(starts):
        out[s:s + size] += rebuilt[i]
        norm[s:s + size] += win * win
    covered = norm > 1e-6
    out[covered] /= norm[covered]
    out[~covered] = x[~covered]
    return (np.clip(out, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def band_energy_above(pcm: bytes, rate: int, hz: float) -> float:
    """Absolute energy above `hz` -- the OLD proxy, in the units the old passes reported."""
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size))) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / rate)
    return float(spec[freqs >= hz].sum())


# --------------------------------------------------------------------------------------------
# 3. Rendering
# --------------------------------------------------------------------------------------------

def render(text: str, voice: str, name: str, backend: str = "piper") -> tuple[str, bytes, int]:
    """Render with the de-esser OFF, so every variant below starts from identical raw audio."""
    os.environ["VOICE_TUNNEL_TTS"] = backend
    os.environ["VOICE_TUNNEL_DEESS"] = "0"
    from voice_tunnel import tts
    pcm, rate = tts.synthesize(text, backend=backend, voice=voice)
    return write_wav(_out(f"{name}.wav"), pcm, rate), pcm, rate


def finish(pcm: bytes, rate: int, name: str) -> str:
    """Apply the same tail of the real pipeline every reply gets: normalize, then pad.

    `normalize` matters more than it looks: it is the peak normaliser that "hands part of the
    level straight back" after a ducking de-esser, and leaving it out would flatter attempt 1.
    """
    from voice_tunnel import tts
    return write_wav(_out(f"{name}.wav"), tts.pad(tts.normalize(pcm), rate), rate)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="emit the full JSON record")
    ap.add_argument("--session", default="live", help="recorded session to take natural speech from")
    ap.add_argument("--voice", default="en_GB-alan-medium")
    ap.add_argument("--band", type=float, default=sharpness.DEFAULT_BAND_HZ)
    ap.add_argument("--repeats", type=int, default=8,
                    help="identical renders used to measure the synthesis noise floor")
    ap.add_argument("--voice-repeats", type=int, default=3,
                    help="renders averaged per alternative voice (VITS jitter, see --repeats)")
    ap.add_argument("--kokoro", nargs="*",
                    default=["bm_daniel", "bm_george", "bm_lewis", "bm_fable"],
                    help="Kokoro voices to audition (British male by default, to match "
                         "en_GB-alan-medium); pass with no values to skip")
    args = ap.parse_args()

    kw = {"band_hz": args.band}
    results, notes = {}, []

    print("Rendering and scoring. Model: DIN 45692 sharpness (acum), "
          f"band-limited to {args.band:.0f} Hz, level-matched to "
          f"{sharpness.DEFAULT_SPL_DB:.0f} dB SPL.\n")

    # -- natural speech -----------------------------------------------------------------
    # Two references, and they answer different questions. ARCTIC is the real one: external,
    # studio-recorded, untouched by this project. His own session audio is kept beside it
    # BECAUSE it is known-muffled -- it shows what happens when the reference is bad, which is
    # the mistake this experiment made on its first run.
    for spk in ARCTIC_SPEAKERS:
        try:
            results[f"natural:arctic_{spk}"] = sharpness.score_file(
                arctic_reference(spk), **kw)
        except Exception as exc:
            notes.append(f"ARCTIC {spk} unavailable ({exc}) -- natural reference degraded")
    try:
        results["natural:his_mic(muffled)"] = sharpness.score_file(
            natural_speech(args.session), **kw)
    except SystemExit as exc:
        notes.append(f"his own session audio unavailable: {exc}")

    # -- the voice, raw ------------------------------------------------------------------
    raw_path, raw_pcm, rate = render(SIBILANT_TEXT, args.voice, f"{args.voice}_raw")
    results["piper_raw"] = sharpness.score_file(finish(raw_pcm, rate, f"{args.voice}_raw_fin"), **kw)

    # -- the measurement's own noise floor, BEFORE any verdict is issued ------------------
    # Piper is VITS: it samples noise at every inference, so the same text through the same
    # voice is different audio every time. the project notes already records this ("byte-identity was
    # never achievable"). It was noticed here because piper_raw moved 0.066 acum between two
    # runs -- the same order as the published JND. An instrument whose own jitter is the size of
    # the effect it is judging will confidently report noise as a fix, which is the failure mode
    # this whole exercise exists to end. So it gets measured and every delta is compared to it.
    noise, raw_takes = [], []
    for i in range(args.repeats):
        _, pcm_i, r_i = render(SIBILANT_TEXT, args.voice, f"noise_{i}")
        raw_takes.append(pcm_i)
        noise.append(sharpness.score_file(
            finish(pcm_i, r_i, f"noise_fin_{i}"), **kw)["sharpness_acum"]["s05"])
    noise_sd = float(np.std(noise, ddof=1)) if len(noise) > 1 else float("nan")
    noise_floor = 2 * noise_sd          # ~95% band for a single-render comparison
    results["_synthesis_noise"] = {
        "renders": len(noise), "s05_values": [round(v, 4) for v in noise],
        "mean": round(float(np.mean(noise)), 4), "sd": round(noise_sd, 4),
        "resolution_limit_acum": round(noise_floor, 4),
    }
    print(f"synthesis jitter over {len(noise)} identical renders: "
          f"S05 mean {np.mean(noise):.3f}, sd {noise_sd:.4f} acum -> a single-render A/B "
          f"cannot resolve anything smaller than ~{noise_floor:.3f} acum")
    if noise_floor > sharpness.JND_ACUM_SMALL:
        print(f"  NOTE: that is LARGER than the smaller published JND "
              f"({sharpness.JND_ACUM_SMALL}) -- near-threshold changes need averaged renders, "
              f"not one clip.")
    print()

    # -- the three attempts, PAIRED against every raw take -------------------------------
    # A filter and the clip it filtered share the same VITS sample, so the synthesis jitter
    # CANCELS in that difference -- the noise floor above governs comparisons between different
    # renders (voice A vs voice B), not a processing step applied to one. Running each attempt
    # over all N takes gives the paired delta AND how much it varies from take to take, which is
    # the only form of "this filter does X" that survives a voice that is different every time.
    ops = {
        "attempt1_frame_duck": lambda p: attempt1_frame_duck(p, rate),
        "attempt2_spectral_no_shelf": lambda p: attempt2_spectral_no_shelf(p, rate),
        "attempt3_shipped_0.6": lambda p: attempt3_shipped(p, rate, 0.6),
        "attempt3_shipped_1.0": lambda p: attempt3_shipped(p, rate, 1.0),
    }
    for name, op in ops.items():
        deltas, hi_pcts, hi45_pcts, scored = [], [], [], None
        for i, take in enumerate(raw_takes):
            processed = op(take)
            scored = sharpness.score_file(finish(processed, rate, f"{name}_{i}"), **kw)
            deltas.append(scored["sharpness_acum"]["s05"] - noise[i])
            b4 = band_energy_above(take, rate, 4000.0)
            b45 = band_energy_above(take, rate, 4500.0)
            hi_pcts.append(100 * (band_energy_above(processed, rate, 4000.0) - b4) / b4)
            hi45_pcts.append(100 * (band_energy_above(processed, rate, 4500.0) - b45) / b45)
        results[name] = scored
        results[name]["paired"] = {
            "takes": len(deltas),
            "delta_s05_mean": round(float(np.mean(deltas)), 4),
            "delta_s05_sd": round(float(np.std(deltas, ddof=1)), 4),
        }
        results[name]["old_proxy_check"] = {
            "high_band_above_4k_change_pct": round(float(np.mean(hi_pcts)), 1),
            "in_band_above_4k5_change_pct": round(float(np.mean(hi45_pcts)), 1),
        }

    # -- alternative voices ---------------------------------------------------------------
    others = []
    models = os.path.join(ROOT, "models")
    if os.path.isdir(models):
        for f in sorted(os.listdir(models)):
            if f.endswith(".onnx") and f.startswith("en_") and f[:-5] != args.voice:
                others.append(f[:-5])
    # Averaged over several renders for the same reason the noise floor exists: a single render
    # of each voice put `alba` on both sides of zero across three runs of this script, which
    # would have made "switch voices" a coin flip presented as a recommendation.
    for v in others:
        try:
            vals = []
            last = None
            for i in range(max(1, args.voice_repeats)):
                _, pcm, r = render(SIBILANT_TEXT, v, f"{v}_raw{i}")
                last = sharpness.score_file(finish(pcm, r, f"{v}_fin{i}"), **kw)
                vals.append(last["sharpness_acum"])
            last["sharpness_acum"] = {
                k: float(np.mean([x[k] for x in vals])) for k in vals[0]}
            last["renders_averaged"] = len(vals)
            results[f"voice:{v}"] = last
        except Exception as exc:                       # a missing voice must not kill the run
            notes.append(f"voice {v} failed to render: {exc}")

    # -- Kokoro, the roadmap's actual alternative -----------------------------------------
    # The open Next Action is "a better voice -- audition Kokoro". The shelf ladder below shows
    # filtering cannot close the gap to natural speech on this voice, so whether ANOTHER model
    # starts closer is the question that decides the whole item. Scored here rather than left to
    # a listening session, which is the resource this exercise exists to stop spending.
    for kv in (args.kokoro or []):
        try:
            vals, last = [], None
            for i in range(max(1, args.voice_repeats)):
                _, pcm, r = render(SIBILANT_TEXT, kv, f"kokoro_{kv}_{i}", backend="kokoro")
                last = sharpness.score_file(finish(pcm, r, f"kokoro_{kv}_fin{i}"), **kw)
                vals.append(last["sharpness_acum"])
            last["sharpness_acum"] = {k: float(np.mean([x[k] for x in vals])) for k in vals[0]}
            last["renders_averaged"] = len(vals)
            results[f"kokoro:{kv}"] = last
        except Exception as exc:
            notes.append(f"kokoro voice {kv} failed: {exc}")

    # -- report ----------------------------------------------------------------------------
    # The baseline for UNPAIRED comparisons is the mean over all raw takes, not one render.
    base = float(np.mean(noise))
    base_mean = results["piper_raw"]["sharpness_acum"]["mean_active"]

    # BOTH aggregates are printed, and the reason is a real finding rather than caution: the
    # dynamic cut and the unconditional shelf move DIFFERENT ones. S05 tracks the loudest 5% of
    # frames -- the fricatives the dynamic de-esser targets -- and is almost blind to a shelf
    # that acts on every frame. mean_active is the reverse. A single headline number would have
    # reported one of the two shipped behaviours as doing nothing.
    print(f"{'clip':30s} {'S05':>7s} {'meanS':>7s} {'>4kHz':>7s} {'dS05':>8s} {'dmean':>8s}  verdict")
    print("-" * 100)
    for name, r in results.items():
        if name.startswith("_"):        # bookkeeping entries, not scored clips
            continue
        s = r["sharpness_acum"]["s05"]
        m = r["sharpness_acum"]["mean_active"]
        share = r["legacy_proxy"]["hf_energy_share_above_4k"]
        if name.startswith("natural") or name == "piper_raw":
            v = "reference"
            d1 = d2 = float("nan")
        elif "paired" in r:
            # PAIRED: the filter and its input share a VITS sample, so jitter cancels and the
            # comparison is against the JND directly, with the take-to-take sd as the error bar.
            d1, d2 = r["paired"]["delta_s05_mean"], m - base_mean
            sd = r["paired"]["delta_s05_sd"]
            mag = abs(d1)
            if mag < sharpness.JND_ACUM_SMALL:
                v = f"BELOW both JNDs -- predicted INAUDIBLE (paired, sd {sd:.3f})"
            elif mag < sharpness.JND_ACUM_LARGE:
                v = f"between the two JNDs -- borderline (paired, sd {sd:.3f})"
            else:
                v = f"above both JNDs -- predicted audible (paired, sd {sd:.3f})"
        else:
            # UNPAIRED (a different voice model): the synthesis noise floor applies, and it
            # OVERRIDES the JND verdict -- a delta smaller than the instrument's own jitter is
            # not a small effect, it is no measurement at all.
            d1, d2 = s - base, m - base_mean
            v = sharpness.compare({"sharpness_acum": {"s05": base},
                                   "legacy_proxy": {"hf_energy_share_above_4k": 0.0}},
                                  r)["verdict"]
            if abs(d1) < noise_floor:
                v = f"WITHIN synthesis noise (+/-{noise_floor:.3f}) -- not measurable"
        c1 = "       -" if d1 != d1 else f"{d1:+8.3f}"
        c2 = "       -" if d2 != d2 else f"{d2:+8.3f}"
        print(f"{name:30s} {s:7.3f} {m:7.3f} {share*100:6.1f}% {c1} {c2}  {v}")

    # -- positive control: does the metric resolve a KNOWN brightness change on this voice? ----
    print()
    print("POSITIVE CONTROL -- unconditional high shelf above 4.5 kHz on the same raw clip.")
    print("If S05 does not track shelf gain monotonically, this metric cannot be used here.")
    print(f"{'shelf dB':>9s} {'S05':>7s} {'meanS':>7s} {'dS05':>8s}  audible per published JND?")
    ladder = []
    # Paired against the UNSHELVED version of the same take, not against the multi-take mean --
    # the shelf is applied to one render, so its baseline must be that render.
    ladder_base = sharpness.score_file(
        finish(raw_pcm, rate, "shelf_base"), **kw)["sharpness_acum"]["s05"]
    for db in (-15.0, -12.0, -9.0, -6.0, -3.0, 0.0, 3.0, 6.0):
        pcm = raw_pcm if db == 0.0 else high_shelf(raw_pcm, rate, db)
        r = sharpness.score_file(finish(pcm, rate, f"shelf_{db:+.0f}"), **kw)
        d = r["sharpness_acum"]["s05"] - ladder_base
        ladder.append({"shelf_db": db, "s05": r["sharpness_acum"]["s05"],
                       "mean_active": r["sharpness_acum"]["mean_active"], "delta_s05": d})
        aud = ("no" if abs(d) < sharpness.JND_ACUM_SMALL else
               "borderline" if abs(d) < sharpness.JND_ACUM_LARGE else "yes")
        print(f"{db:+9.1f} {r['sharpness_acum']['s05']:7.3f} "
              f"{r['sharpness_acum']['mean_active']:7.3f} {d:+8.3f}  {aud}")
    mono = all(ladder[i]["s05"] <= ladder[i + 1]["s05"] + 1e-9 for i in range(len(ladder) - 1))
    print(f"monotonic with shelf gain: {'YES' if mono else 'NO -- METRIC UNUSABLE HERE'}")
    cuts = [row for row in ladder
            if row["shelf_db"] < 0 and abs(row["delta_s05"]) >= sharpness.JND_ACUM_LARGE]
    if cuts:
        print(f"smallest cut clearing the larger JND ({sharpness.JND_ACUM_LARGE} acum): "
              f"{max(row['shelf_db'] for row in cuts):+.0f} dB")
    results["_shelf_ladder"] = ladder

    print()
    for k, r in results.items():
        if k.startswith("natural"):
            print(f"{k:30s} S05 = {r['sharpness_acum']['s05']:.3f} acum   "
                  f"ratio alan/natural = {base / r['sharpness_acum']['s05']:.2f}x")
    print(f"published JND: {sharpness.JND_ACUM_SMALL}-{sharpness.JND_ACUM_LARGE} acum. "
          f"Zwicker annoyance knee: {sharpness.PA_SHARPNESS_KNEE} acum.")
    for n in notes:
        print(f"NOTE: {n}")

    if args.json:
        print()
        print(json.dumps({"results": results, "notes": notes}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
