"""Installing a neural voice should be enough to use it.

From the third cold-start audit on 2026-08-10: an agent given a fresh virtualenv and nothing but
`voice-tunnel describe` ran `setup`, watched every step report success, and ended on the robotic
Windows system voice. Nothing had failed. `tts_backend()` returned a hardcoded `"sapi"` unless a
human had exported `VOICE_TUNNEL_TTS`, while `asr_engine()` had always upgraded itself the moment
its model appeared — so the two halves of the same install behaved by opposite rules, and only one
of them was documented anywhere.

The remedy printed alongside it said "run setup", which is what the auditor had just run.
"""
import os

import pytest

from tests.test_cli_surface import run
from voice_tunnel import config


@pytest.fixture()
def clean(monkeypatch, tmp_path):
    """No inherited settings, no real models, no discoverable piper.exe."""
    for var in ("VOICE_TUNNEL_TTS", "VOICE_TUNNEL_PIPER_BIN", "VOICE_TUNNEL_PIPER_VOICE",
                "VOICE_TUNNEL_PIPER_INPROCESS", "VOICE_TUNNEL_HOME", "VOICE_TUNNEL_ENV_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / "settings.env"))
    monkeypatch.setattr(config.sys, "executable", str(tmp_path / "nowhere" / "python.exe"))
    monkeypatch.setattr(config, "ROOT", str(tmp_path / "nowhere"))
    monkeypatch.setattr(config.shutil, "which", lambda _n: None)
    return monkeypatch


def _installed(mp, tmp_path, *, module=True, voice=True):
    if voice:
        v = tmp_path / "voices" / "en_US-amy-medium.onnx"
        v.parent.mkdir(parents=True, exist_ok=True)
        v.write_text("", encoding="utf-8")
        mp.setattr(config, "piper_voice", lambda: str(v))
    else:
        mp.setattr(config, "piper_voice", lambda: "")
    mp.setattr(config, "have_module", lambda name: name == "piper" and module)


def test_a_complete_piper_install_selects_itself(clean, tmp_path):
    """THE REGRESSION. Engine plus voice present and nothing exported: this must be piper.

    Anything else means `setup` can succeed end to end and leave synthesis on the fallback, which
    is precisely what a cold-start audit reported.
    """
    _installed(clean, tmp_path)
    assert config.tts_backend() == "piper"


def test_an_explicit_setting_still_wins(clean, tmp_path):
    """Naming a backend earns you its behaviour — and its errors — rather than a substitution."""
    _installed(clean, tmp_path)
    clean.setenv("VOICE_TUNNEL_TTS", "sapi")
    assert config.tts_backend() == "sapi"


def test_a_voice_without_an_engine_does_not_select_piper(clean, tmp_path):
    """Selecting on the voice file alone moves the failure from the download to the first spoken
    word, where it is far more expensive to diagnose."""
    _installed(clean, tmp_path, module=False, voice=True)
    assert config.tts_backend() == "sapi"


def test_an_engine_without_a_voice_does_not_select_piper(clean, tmp_path):
    _installed(clean, tmp_path, module=False, voice=False)
    clean.setattr(config, "have_module", lambda name: name == "piper")
    assert config.tts_backend() == "sapi"


def test_a_spawning_install_still_counts_as_usable(clean, tmp_path):
    """No `piper` package, but a `piper.exe` beside the interpreter. Slower, and it works —
    so it is a backend, not a reason to stay on sapi."""
    _installed(clean, tmp_path, module=False, voice=True)
    exe = tmp_path / "env" / "Scripts" / "piper.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("", encoding="utf-8")
    clean.setenv("VOICE_TUNNEL_PIPER_BIN", str(exe))
    assert config.tts_backend() == "piper"


def test_the_two_engines_choose_by_the_same_rule(clean, tmp_path):
    """The asymmetry itself, pinned. Both selectors must read: explicit wins, else upgrade if
    what is needed is installed. If these ever diverge again, one half of an install will silently
    stay on a fallback while the other reports itself configured."""
    src = open(os.path.join(os.path.dirname(config.__file__), "config.py"), encoding="utf-8").read()
    body = src.split("def tts_backend")[1].split("\ndef ")[0]
    assert "forced" in body and "return forced" in body, "explicit must still win"
    assert "return \"piper\" if usable else \"sapi\"" in body, (
        "tts_backend must self-select like asr_engine, not return a constant"
    )


# ------------------------------------------------------------------ doctor's advice


def test_the_sapi_remedy_names_the_setting_when_setup_is_already_done(
        clean, tmp_path, tmp_sessions, capsys):
    """A remedy that ignores what is already installed is worse than none: it sends you to
    re-run the command that just worked, and the real gap goes unnamed.
    """
    _installed(clean, tmp_path)
    clean.setenv("VOICE_TUNNEL_TTS", "sapi")  # the only reason it is still on the fallback
    _, payload, _ = run(["doctor"], capsys)
    tts = next(c for c in payload["checks"] if c["name"] == "tts")

    assert tts["status"] == "degraded"
    assert "VOICE_TUNNEL_TTS piper" in tts["remedy"], (
        "with piper and a voice installed, the fix is the setting — not another setup run"
    )
    assert "voice-tunnel setup" not in tts["remedy"], "do not advise re-running what has run"


def test_the_sapi_remedy_still_says_setup_on_a_bare_install(clean, tmp_sessions, capsys):
    """The other branch: nothing installed, so `setup` is genuinely the answer."""
    clean.setattr(config, "piper_voice", lambda: "")
    clean.setattr(config, "have_module", lambda _n: False)
    _, payload, _ = run(["doctor"], capsys)
    tts = next(c for c in payload["checks"] if c["name"] == "tts")
    if os.name == "nt":
        assert "setup" in tts["remedy"]


def test_describe_does_not_promise_setup_makes_an_install_complete(capsys, tmp_sessions):
    """It installs engines and downloads models. Whether the result is *used* depended on a
    setting it never touched, so the claim was false in the exact case it was written for.
    """
    _, payload, _ = run(["describe"], capsys)
    text = str(payload).upper()
    assert "FULLY CAPABLE" not in text, "setup cannot promise a complete install"
