"""Model fetching — the URL derivation and the two guards, without touching the network.

The download itself is verified by running it (a voice is 63 MB and lands in three seconds). What
belongs in a unit test is the logic that decides WHERE to fetch from and what to refuse, because
both fail in ways that are silent at download time and confusing much later.
"""
import tarfile

import pytest

from voice_tunnel import download as dl


def test_a_voice_name_derives_its_own_url():
    """The repository layout is <lang>/<locale>/<speaker>/<quality>/, and the name carries all
    four. Deriving it means any voice upstream publishes works without a code change here."""
    onnx, meta = dl.piper_voice_urls("en_GB-alan-medium")
    assert onnx.endswith("/en/en_GB/alan/medium/en_GB-alan-medium.onnx")
    assert meta == onnx + ".json"


def test_an_underscored_speaker_name_still_splits_correctly():
    """`en_GB-northern_english_male-medium` — the reason the split is on '-' and not '_'."""
    onnx, _ = dl.piper_voice_urls("en_GB-northern_english_male-medium")
    assert onnx.endswith("/en/en_GB/northern_english_male/medium/"
                         "en_GB-northern_english_male-medium.onnx")


@pytest.mark.parametrize("bad", ["alan", "en_GB-alan", "en_GB-alan-medium-extra", ""])
def test_a_malformed_voice_name_is_refused_before_any_request(bad):
    """Fail on the name, not on a 404. The error can then say what a name looks like, which a
    404 from a CDN cannot."""
    with pytest.raises(ValueError, match="piper voice name"):
        dl.piper_voice_urls(bad)


def test_an_error_page_is_not_mistaken_for_a_model(tmp_path):
    """THE silent failure this guard exists for.

    A CDN that answers a bad path with 200 and an HTML body writes a file that looks downloaded
    and only fails at load, inside onnxruntime, with a protobuf parse error that reads as a bug
    in this tool. No real checkpoint here is under a megabyte; no error page is over one.
    """
    page = tmp_path / "en_GB-fake-medium.onnx"
    page.write_text("<!doctype html><title>404</title>", encoding="utf-8")
    assert dl._looks_like_a_model(str(page)) is False

    real = tmp_path / "big.onnx"
    real.write_bytes(b"\0" * (2 << 20))
    assert dl._looks_like_a_model(str(real)) is True


def test_a_voice_missing_its_sidecar_is_not_installed(tmp_path, monkeypatch):
    """Piper cannot load a voice without its .onnx.json, so 'the model file exists' is the wrong
    question — an interrupted run that got one of the two must re-download, not be skipped."""
    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", str(tmp_path))
    (tmp_path / "en_GB-alan-medium.onnx").write_bytes(b"\0" * (2 << 20))
    assert dl.voice_installed("en_GB-alan-medium") is False

    (tmp_path / "en_GB-alan-medium.onnx.json").write_text("{}", encoding="utf-8")
    assert dl.voice_installed("en_GB-alan-medium") is True


def test_a_tar_cannot_write_outside_its_target(tmp_path):
    """CVE-2007-4559. A tar member may name `../../somewhere`, and `extractall` obeyed that for
    most of Python's history. A model download that quietly writes outside its own directory is
    not a tradeoff worth making."""
    payload = tmp_path / "payload.txt"
    payload.write_text("owned", encoding="utf-8")
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(payload, arcname="../../escaped.txt")

    dest = tmp_path / "unpack"
    dest.mkdir()

    # Named exception types, not a blind `Exception`. A bare catch-all would pass if the code
    # blew up for an unrelated reason — a typo raising AttributeError reads as "the guard works".
    # Two types because the guard has two implementations: tarfile's own `filter="data"` on
    # interpreters that have it, and the explicit path check on those that do not.
    refusals = (tarfile.TarError, RuntimeError)
    with pytest.raises(refusals):
        dl._safe_extract(str(archive), str(dest))
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_a_normal_tar_still_extracts(tmp_path):
    inner = tmp_path / "model"
    inner.mkdir()
    (inner / "tokens.txt").write_text("a b c", encoding="utf-8")
    archive = tmp_path / "ok.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(inner, arcname="model")

    dest = tmp_path / "unpack"
    dl._safe_extract(str(archive), str(dest))
    assert (dest / "model" / "tokens.txt").read_text(encoding="utf-8") == "a b c"


def test_the_catalog_answers_without_a_network(tmp_path, monkeypatch):
    """`--list` has to work offline: the most likely moment someone runs it is when a download
    just failed and they are trying to find out what the name should have been."""
    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", str(tmp_path))
    cat = dl.catalog()

    assert {"voices", "asr", "voiceprint", "models_dir"} <= set(cat)
    assert all(v["installed"] is False for v in cat["voices"]), "empty dir, nothing installed"
    assert sum(1 for v in cat["voices"] if v["default"]) == 1, "exactly one default voice"
    assert cat["models_dir"] == str(tmp_path)


def test_the_sherpa_release_tag_typo_is_preserved():
    """Upstream's release tag really is spelled 'recongition'. Correcting it 404s, so this
    asserts the typo stays — a future reader will otherwise 'fix' it."""
    assert "speaker-recongition-models" in dl.VOICEPRINT_MODEL["url"]


# --- a downloaded model is only half the answer -------------------------------
# The runtime that loads it ships as an extra (`pip install voice-tunnel[parakeet]`). In a
# checkout both always arrived together, so this distinction did not exist until the tool became
# installable and the split became real.

def test_a_parakeet_model_without_sherpa_falls_back_to_whisper(tmp_path, monkeypatch):
    """THE regression. Selecting on model presence alone means `download asr` — 600 MB — flips
    the engine to an implementation that is not installed, and the failure lands at the first
    spoken word rather than at the download."""
    from voice_tunnel import config

    monkeypatch.setenv("VOICE_TUNNEL_PARAKEET_DIR", str(tmp_path))
    monkeypatch.delenv("VOICE_TUNNEL_ASR", raising=False)
    monkeypatch.setattr(config, "have_module", lambda name: name != "sherpa_onnx")

    assert config.asr_engine() == "whisper"


def test_an_explicit_engine_is_still_obeyed(tmp_path, monkeypatch):
    """Someone who names an engine deserves the error, not a silent substitution — and `doctor`
    is where the mismatch gets explained."""
    from voice_tunnel import config

    monkeypatch.setenv("VOICE_TUNNEL_ASR", "parakeet")
    monkeypatch.setattr(config, "have_module", lambda name: name != "sherpa_onnx")

    assert config.asr_engine() == "parakeet"


def test_both_halves_present_selects_parakeet(tmp_path, monkeypatch):
    from voice_tunnel import config

    monkeypatch.setenv("VOICE_TUNNEL_PARAKEET_DIR", str(tmp_path))
    monkeypatch.delenv("VOICE_TUNNEL_ASR", raising=False)
    monkeypatch.setattr(config, "have_module", lambda name: True)

    assert config.asr_engine() == "parakeet"


def test_doctor_accepts_piper_without_an_executable(monkeypatch, tmp_path, capsys):
    """The resident path needs no `piper.exe`, and `doctor` used to demand one anyway.

    That failed a working install for everyone who ran `pip install voice-tunnel[piper]` — the
    wheel ships a library, not an executable. Found by running `doctor` inside a PyInstaller
    bundle, which reported `bin=(not found)` while synthesis was demonstrably working.
    """

    from voice_tunnel import cli, config

    voice = tmp_path / "en_GB-alan-medium.onnx"
    voice.write_bytes(b"\0" * (2 << 20))
    (tmp_path / "en_GB-alan-medium.onnx.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_TUNNEL_TTS", "piper")
    monkeypatch.setattr(config, "piper_bin", lambda: None)          # no executable anywhere
    monkeypatch.setattr(config, "have_module", lambda name: name == "piper")

    tts = next(c for c in cli.cmd_doctor(None)["checks"] if c["name"] == "tts")
    assert tts["ok"] is True, f"resident piper must pass without a binary: {tts['detail']}"
    assert "resident" in tts["detail"]
