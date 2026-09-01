"""command_bridge.download — fetch the models the tool needs but must never ship.

**Why this exists.** Nothing else in the codebase downloads anything. `voices` LISTS what is
already on disk, and every model here arrived by hand, which was invisible while the only
installation was a checkout somebody had already populated. The moment this is `pip install`-able
that becomes the first thing a new user hits: a tunnel that transcribes nothing and speaks in the
default system voice, with no command anywhere that fixes it.

**Why not vendor them.** A Parakeet checkpoint is ~600 MB and a Piper voice 60-120 MB. Putting
either in a wheel makes the package unusable on a slow connection to save one command, and
forces a release for every voice anyone wants. They are a cache the user owns — see
`config.models_dir()`.

Four families, four upstreams, all verified reachable:

    piper voices  huggingface.co/rhasspy/piper-voices   <name>.onnx + <name>.onnx.json
    kokoro        github.com/thewh1teagle/kokoro-onnx   one .onnx + one voice pack, all voices
    parakeet ASR  github.com/k2-fsa/sherpa-onnx         a .tar.bz2 of a model directory
    titanet       github.com/k2-fsa/sherpa-onnx         one .onnx, for the voiceprint

STDLIB ONLY. Adding `requests` to a tool whose whole install story is "it is small" would be a
poor trade for a progress bar.
"""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from . import config

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
SHERPA_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

DEFAULT_VOICE = "en_GB-alan-medium"
"""What `download voice` picks with no argument — the voice this project settled on by ear."""

VOICE_CATALOG: dict[str, str] = {
    # A curated shortlist, not the whole of piper-voices (~100 voices). Any valid piper name
    # works — the URL is derived from the name — so this is a starting point, not a whitelist.
    "en_GB-alan-medium": "British male, measured. The default here.",
    "en_GB-alba-medium": "Scottish female.",
    "en_GB-cori-high": "British female, higher fidelity and slower to synthesize.",
    "en_GB-northern_english_male-medium": "Northern English male.",
    "en_US-ryan-high": "American male, low register.",
    "en_US-amy-medium": "American female.",
    "en_US-lessac-high": "American female, the LJSpeech-style reference voice.",
}

ASR_MODELS: dict[str, dict[str, str]] = {
    "parakeet": {
        "dir": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
        "url": f"{SHERPA_BASE}/asr-models/"
               f"sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2",
        "note": "NVIDIA Parakeet TDT 0.6B, int8. RTF 0.077 on a desktop CPU — 8x faster than "
                "whisper small.en from a more accurate model. ~600 MB.",
    },
}

TURN_MODEL = {
    "file": "smart-turn-v3.2-cpu.onnx",
    "url": "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx",
    "note": "Smart Turn v3.2 (BSD-2-Clause, 8.3 MB int8). Decides whether an utterance actually "
            "SOUNDED finished instead of waiting out a fixed timer, so he is not cut off "
            "mid-thought and a short question does not wait 1.5 s for nothing.",
}

KOKORO_TIMESTAMPED_URL = (
    "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX-timestamped"
    "/resolve/main/onnx/model.onnx"
)
"""The same weights, re-exported with a second output: one duration per token.

🎯 **Fetched INSTEAD OF the plain export, not alongside it, and that is the whole argument for
the swap.** Measured 2026-08-26 on identical inputs: `max abs diff 0.0` against
`kokoro-v1.0.onnx`, and the same 325,532,171 bytes on the wire. Same size, same audio, plus the
schedule `say --timings` reports (spec 020) — so downloading the other one costs a caller 325 MB
to get strictly less.

An install that already has `kokoro-v1.0.onnx` keeps working untouched: `kokoro_installed` accepts
either model, and the backend simply reports timings as unavailable on the older one."""

KOKORO_MODELS: tuple[str, ...] = (config.KOKORO_TIMESTAMPED_MODEL, "kokoro-v1.0.onnx")
"""Model filenames that count as "kokoro is installed", best first. Order matches
`config.kokoro_model`'s preference so the check and the loader cannot disagree."""

KOKORO_FILES: tuple[dict[str, str], ...] = (
    {"file": config.KOKORO_TIMESTAMPED_MODEL, "url": KOKORO_TIMESTAMPED_URL},
    {"file": "voices-v1.0.bin", "url": f"{KOKORO_BASE}/voices-v1.0.bin"},
)
"""BOTH files, because either one alone is useless.

Kokoro is a different shape from piper: ONE model plus ONE voice pack holding all 54 voices,
rather than one 60-120 MB file per voice. So there is no name to pass and no catalog to choose
from — `download kokoro` is the whole thing, and switching voices afterwards costs nothing.

Model-without-pack is a real state and the reason these are fetched as a pair: the `.onnx` alone
synthesizes nothing, since a voice is a style vector looked up in the `.bin`. `config.kokoro_model`
and `config.kokoro_voices_bin` check for them separately for the same reason, and
`_ResidentKokoro._load` names whichever is missing.

Upstream is the kokoro-onnx project's own release assets, which is where the `kokoro_onnx`
package's README sends you — Hugging Face hosts the PyTorch weights, not these ONNX exports."""

KOKORO_NOTE = ("Kokoro v1.0 (Apache-2.0): one 325 MB model plus a 28 MB pack of all 54 voices, "
               "at 24 kHz. Measurably less harsh than the piper voices — `bm_daniel` and "
               "`bm_lewis` each score ~0.65 acum lower on DIN 45692 sharpness than "
               "en_GB-alan-medium, a larger move than any de-esser setting achieves. The model "
               "fetched is the TIMESTAMPED export: identical audio (measured bit-for-bit) and "
               "identical size, plus a duration per token, which is what lets `say --timings` "
               "report when each word is spoken. Note its speed ceiling is 2.0, below this "
               "tool's 2.5 — and measured, it saturates near 2.3x whatever you ask for, because "
               "a token cannot be shorter than one 25 ms unit.")

VOICEPRINT_MODEL = {
    "file": "nemo_en_titanet_large.onnx",
    # "recongition" is upstream's typo in the release tag, not one here. Correcting it 404s.
    "url": f"{SHERPA_BASE}/speaker-recongition-models/nemo_en_titanet_large.onnx",
    "note": "NeMo TitaNet-large speaker embeddings, for recognising the owner's voice so the "
            "wake phrase stops being mandatory. ~100 MB.",
}


def piper_voice_urls(name: str) -> tuple:
    """(onnx_url, json_url) for a piper voice name.

    The repository lays voices out as `<lang>/<locale>/<speaker>/<quality>/<name>.onnx`, and the
    name already carries every component: `en_GB-alan-medium` -> `en/en_GB/alan/medium/`. Deriving
    it means any voice upstream publishes works without this file knowing about it.
    """
    parts = name.split("-")
    if len(parts) != 3:
        raise ValueError(
            f"{name!r} is not a piper voice name — expected <locale>-<speaker>-<quality>, "
            f"e.g. en_GB-alan-medium. `command-bridge download --list` shows a starting set."
        )
    locale, speaker, quality = parts
    lang = locale.split("_")[0]
    stem = f"{PIPER_BASE}/{lang}/{locale}/{speaker}/{quality}/{name}.onnx"
    return stem, stem + ".json"


def _fetch(url: str, dest: str, on_progress: Callable | None = None) -> int:
    """Stream `url` to `dest`, atomically. Returns bytes written.

    **Atomic because a partial model is worse than a missing one.** A truncated .onnx does not
    announce itself: it fails at load, deep inside onnxruntime, with an error about protobuf
    parsing that reads as a bug in this tool. Downloading to a sibling temp file and renaming
    means the model at the destination path is either whole or absent.
    """
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "command-bridge"})
    tmp = None
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest) or ".", suffix=".part")
            written = 0
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(written, total)
        os.replace(tmp, dest)
        tmp = None
        return written
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}") from exc
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _looks_like_a_model(path: str, min_bytes: int = 1 << 20) -> bool:
    """Guard against saving an error page as a model.

    A CDN that answers a bad path with 200 and an HTML body produces a file that only fails much
    later, at load, with an unrecognisable error. Size alone catches it: no real checkpoint here
    is under a megabyte and no error page is over one.
    """
    return os.path.exists(path) and os.path.getsize(path) >= min_bytes


def voice_installed(name: str) -> bool:
    d = config.models_dir()
    return (_looks_like_a_model(os.path.join(d, f"{name}.onnx"))
            and os.path.exists(os.path.join(d, f"{name}.onnx.json")))


def download_voice(name: str, force: bool = False, on_progress=None) -> dict:
    """Fetch a piper voice (the model and its config sidecar, which is required to load it)."""
    models = config.models_dir()
    onnx = os.path.join(models, f"{name}.onnx")
    meta = os.path.join(models, f"{name}.onnx.json")

    if voice_installed(name) and not force:
        return {"voice": name, "path": onnx, "already_present": True, "bytes_fetched": 0}

    onnx_url, json_url = piper_voice_urls(name)
    written = _fetch(onnx_url, onnx, on_progress)
    _fetch(json_url, meta, None)

    if not _looks_like_a_model(onnx):
        os.unlink(onnx)
        raise RuntimeError(f"{name} downloaded but is too small to be a model — removed")
    try:
        with open(meta, encoding="utf-8") as fh:
            json.load(fh)
    except Exception as exc:
        raise RuntimeError(f"{name}.onnx.json is not valid JSON: {exc}") from exc

    return {"voice": name, "path": onnx, "already_present": False, "bytes_fetched": written}


def _safe_extract(archive: str, dest: str) -> None:
    """Extract with member paths constrained to `dest`.

    A tar member is free to name `../../etc/whatever`, and `extractall` obeyed that for most of
    Python's history (CVE-2007-4559). `filter="data"` is the modern fix; the manual check is the
    fallback on interpreters that predate the backport, because a model download that quietly
    writes outside its directory is not a tradeoff worth making for tidier code.
    """
    with tarfile.open(archive, "r:*") as tar:
        try:
            tar.extractall(dest, filter="data")
            return
        except TypeError:
            pass
        base = os.path.abspath(dest)
        for member in tar.getmembers():
            target = os.path.abspath(os.path.join(dest, member.name))
            if not target.startswith(base + os.sep) and target != base:
                raise RuntimeError(f"refusing tar member outside the target dir: {member.name}")
        tar.extractall(dest)


def download_asr(which: str = "parakeet", force: bool = False, on_progress=None) -> dict:
    """Fetch and unpack an ASR model directory."""
    if which not in ASR_MODELS:
        raise ValueError(f"unknown ASR model {which!r}; known: {', '.join(ASR_MODELS)}")
    spec = ASR_MODELS[which]
    models = config.models_dir()
    target = os.path.join(models, spec["dir"])

    if os.path.isdir(target) and os.listdir(target) and not force:
        return {"asr": which, "path": target, "already_present": True, "bytes_fetched": 0}

    os.makedirs(models, exist_ok=True)
    staging = tempfile.mkdtemp(dir=models, prefix=".unpack-")
    try:
        archive = os.path.join(staging, "model.tar.bz2")
        written = _fetch(spec["url"], archive, on_progress)
        _safe_extract(archive, staging)
        os.unlink(archive)

        # The archive contains one top-level directory; move it into place rather than merging,
        # so a re-download cannot leave files from two versions mixed together.
        entries = os.listdir(staging)
        root = os.path.join(staging, entries[0]) if len(entries) == 1 else staging
        if os.path.isdir(target):
            shutil.rmtree(target)
        shutil.move(root, target)
        return {"asr": which, "path": target, "already_present": False, "bytes_fetched": written}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def kokoro_installed() -> bool:
    """True only when BOTH halves are on disk — `download kokoro` skips nothing on a half install.

    ⚠ **EITHER model counts.** An install predating spec 020 has `kokoro-v1.0.onnx` and is not
    broken by the switch to the timestamped export — it speaks exactly as before and only reports
    timings as unavailable. Checking for the new filename alone would tell those installs that
    kokoro is missing when it is loaded and working, which is the kind of false alarm that sends
    someone to re-download 325 MB they already have.
    """
    d = config.models_dir()
    has_model = any(_looks_like_a_model(os.path.join(d, m)) for m in KOKORO_MODELS)
    return has_model and _looks_like_a_model(os.path.join(d, "voices-v1.0.bin"))


def download_kokoro(force: bool = False, on_progress=None) -> dict:
    """Fetch the Kokoro model AND its voice pack — the warmer voice, measured rather than felt.

    Two files, so unlike the single-asset targets the already-present check is per file: an
    interrupted run that got the 325 MB model and not the 28 MB pack must fetch the pack, and
    would otherwise be skipped as done and fail at load with a missing-voice error instead.
    """
    models = config.models_dir()
    fetched, present = 0, []
    for spec in KOKORO_FILES:
        dest = os.path.join(models, spec["file"])
        if _looks_like_a_model(dest) and not force:
            present.append(spec["file"])
            continue
        # An older install already has a working model under the previous name. Re-fetching 325 MB
        # to gain durations is a real cost and a real choice, so it is offered rather than taken:
        # `--force` is how someone says yes. Without this an upgrade would silently re-download on
        # the next `download kokoro` anybody ran for an unrelated reason.
        if spec["file"] == config.KOKORO_TIMESTAMPED_MODEL and not force and any(
            _looks_like_a_model(os.path.join(models, m)) for m in KOKORO_MODELS[1:]
        ):
            present.append(f"{KOKORO_MODELS[1]} (older export; `--force` to get word timings)")
            continue
        fetched += _fetch(spec["url"], dest, on_progress)
        if not _looks_like_a_model(dest):
            os.unlink(dest)
            raise RuntimeError(f"{spec['file']} downloaded but is too small to be a model — removed")
    return {
        "kokoro": [f["file"] for f in KOKORO_FILES],
        "path": models,
        "already_present": len(present) == len(KOKORO_FILES),
        "bytes_fetched": fetched,
    }


def download_voiceprint(force: bool = False, on_progress=None) -> dict:
    """Fetch the speaker-embedding model that makes the wake phrase optional."""
    dest = os.path.join(config.models_dir(), VOICEPRINT_MODEL["file"])
    if _looks_like_a_model(dest) and not force:
        return {"voiceprint": VOICEPRINT_MODEL["file"], "path": dest,
                "already_present": True, "bytes_fetched": 0}
    written = _fetch(VOICEPRINT_MODEL["url"], dest, on_progress)
    if not _looks_like_a_model(dest):
        os.unlink(dest)
        raise RuntimeError("voiceprint model downloaded but is too small — removed")
    return {"voiceprint": VOICEPRINT_MODEL["file"], "path": dest,
            "already_present": False, "bytes_fetched": written}


def download_turn(force: bool = False, on_progress=None) -> dict:
    """Fetch the turn-detection model — the escape from a fixed end-of-utterance timeout."""
    dest = os.path.join(config.models_dir(), TURN_MODEL["file"])
    if _looks_like_a_model(dest) and not force:
        return {"turn": TURN_MODEL["file"], "path": dest, "already_present": True, "bytes_fetched": 0}
    written = _fetch(TURN_MODEL["url"], dest, on_progress)
    if not _looks_like_a_model(dest):
        os.unlink(dest)
        raise RuntimeError("turn model downloaded but is too small — removed")
    return {"turn": TURN_MODEL["file"], "path": dest, "already_present": False, "bytes_fetched": written}


def catalog() -> dict[str, Any]:
    """What can be fetched and what is already here — answerable with no network."""
    return {
        "voices": [
            {"name": n, "note": note, "installed": voice_installed(n),
             "default": n == DEFAULT_VOICE}
            for n, note in VOICE_CATALOG.items()
        ],
        "asr": [
            {"name": k, "note": v["note"],
             "installed": os.path.isdir(os.path.join(config.models_dir(), v["dir"]))}
            for k, v in ASR_MODELS.items()
        ],
        "kokoro": [{
            "name": "kokoro", "note": KOKORO_NOTE, "installed": kokoro_installed(),
            "files": [f["file"] for f in KOKORO_FILES],
        }],
        "turn": [{
            "name": "smart-turn", "note": TURN_MODEL["note"],
            "installed": _looks_like_a_model(
                os.path.join(config.models_dir(), TURN_MODEL["file"])),
        }],
        "voiceprint": [{
            "name": "titanet", "note": VOICEPRINT_MODEL["note"],
            "installed": _looks_like_a_model(
                os.path.join(config.models_dir(), VOICEPRINT_MODEL["file"])),
        }],
        "models_dir": config.models_dir(),
        "note": "Any piper voice name works, not only the ones listed — the URL is derived from "
                "the name. Full set: https://huggingface.co/rhasspy/piper-voices",
    }
