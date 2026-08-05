"""voice_tunnel.download — fetch the models the tool needs but must never ship.

**Why this exists.** Nothing else in the codebase downloads anything. `voices` LISTS what is
already on disk, and every model here arrived by hand, which was invisible while the only
installation was a checkout somebody had already populated. The moment this is `pip install`-able
that becomes the first thing a new user hits: a tunnel that transcribes nothing and speaks in the
default system voice, with no command anywhere that fixes it.

**Why not vendor them.** A Parakeet checkpoint is ~600 MB and a Piper voice 60-120 MB. Putting
either in a wheel makes the package unusable on a slow connection to save one command, and
forces a release for every voice anyone wants. They are a cache the user owns — see
`config.models_dir()`.

Three families, three upstreams, all verified reachable:

    piper voices  huggingface.co/rhasspy/piper-voices   <name>.onnx + <name>.onnx.json
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
            f"e.g. en_GB-alan-medium. `voice-tunnel download --list` shows a starting set."
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
    req = urllib.request.Request(url, headers={"User-Agent": "voice-tunnel"})
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
        return {"voice": name, "path": onnx, "already_present": True, "bytes": 0}

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

    return {"voice": name, "path": onnx, "already_present": False, "bytes": written}


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
        return {"asr": which, "path": target, "already_present": True, "bytes": 0}

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
        return {"asr": which, "path": target, "already_present": False, "bytes": written}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def download_voiceprint(force: bool = False, on_progress=None) -> dict:
    """Fetch the speaker-embedding model that makes the wake phrase optional."""
    dest = os.path.join(config.models_dir(), VOICEPRINT_MODEL["file"])
    if _looks_like_a_model(dest) and not force:
        return {"voiceprint": VOICEPRINT_MODEL["file"], "path": dest,
                "already_present": True, "bytes": 0}
    written = _fetch(VOICEPRINT_MODEL["url"], dest, on_progress)
    if not _looks_like_a_model(dest):
        os.unlink(dest)
        raise RuntimeError("voiceprint model downloaded but is too small — removed")
    return {"voiceprint": VOICEPRINT_MODEL["file"], "path": dest,
            "already_present": False, "bytes": written}


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
        "voiceprint": [{
            "name": "titanet", "note": VOICEPRINT_MODEL["note"],
            "installed": _looks_like_a_model(
                os.path.join(config.models_dir(), VOICEPRINT_MODEL["file"])),
        }],
        "models_dir": config.models_dir(),
        "note": "Any piper voice name works, not only the ones listed — the URL is derived from "
                "the name. Full set: https://huggingface.co/rhasspy/piper-voices",
    }
