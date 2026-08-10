"""One root that really isolates, and checks that do not go quiet when they start passing.

All three of these came out of a cold-start audit on 2026-08-10, where an agent was given a fresh
virtualenv, the published wheel, and nothing but `voice-tunnel describe`.

  * It set `VOICE_TUNNEL_DIR`, believed the environment was pristine, and found
    `VOICE_TUNNEL_WAKE_NAME=codex` already applied — leaked through the machine-wide settings
    file, which that variable never scoped.
  * It watched the `tts` check stay "ok" while its detail changed from spawning `piper.exe` to
    resident in-process, a difference this codebase measures at 7-26x. A fallback hiding behind a
    pass is the exact failure `degraded` was introduced to end.
  * It found the `voiceprint` check present when the model was missing and GONE once installed,
    so readiness had to be confirmed through a different command entirely.
"""
import os

import pytest

from voice_tunnel import config


@pytest.fixture()
def clean_env(monkeypatch):
    for var in ("VOICE_TUNNEL_HOME", "VOICE_TUNNEL_DIR", "VOICE_TUNNEL_MODELS_DIR",
                "VOICE_TUNNEL_ENV_FILE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_home_scopes_all_three_paths(clean_env, tmp_path):
    """The switch that actually means "keep this copy to itself"."""
    clean_env.setenv("VOICE_TUNNEL_HOME", str(tmp_path))
    for path in (config.session_dir(), config.models_dir(), config.env_file_path()):
        assert str(tmp_path) in path, f"{path} escaped VOICE_TUNNEL_HOME"


def test_the_session_variable_alone_does_not_isolate_settings(clean_env, tmp_path):
    """The precise shape of the leak, pinned so nobody 'simplifies' it back.

    `VOICE_TUNNEL_DIR` scopes turn logs and nothing else. That is legitimate behaviour — it is
    named for the session directory — and it is why an audit that set it still inherited another
    installation's wake name. The fix was never to change this; it was to stop implying that it
    isolates, and to provide something that does.
    """
    clean_env.setenv("VOICE_TUNNEL_DIR", str(tmp_path / "sessions"))
    assert str(tmp_path) in config.session_dir()
    assert str(tmp_path) not in config.env_file_path(), (
        "if this ever starts passing, VOICE_TUNNEL_DIR has grown scope and the docs must follow"
    )


def test_an_explicit_variable_still_beats_home(clean_env, tmp_path):
    """Isolation must not cost the ability to override one path — a shared 600 MB model cache
    beside an isolated settings file is a reasonable thing to want."""
    clean_env.setenv("VOICE_TUNNEL_HOME", str(tmp_path / "home"))
    clean_env.setenv("VOICE_TUNNEL_MODELS_DIR", str(tmp_path / "shared-models"))
    assert config.models_dir() == str(tmp_path / "shared-models")
    assert str(tmp_path / "home") in config.session_dir()


def test_doctor_names_the_paths_other_installs_also_use(capsys, tmp_sessions, clean_env):
    """Sharing models is deliberate; discovering it by surprise is not."""
    from tests.test_cli_surface import run

    _, payload, _ = run(["doctor"], capsys)
    runtime = payload["runtime"]
    assert "shared" in runtime, "doctor must say which paths are machine-wide"
    assert "isolate_with" in runtime and "VOICE_TUNNEL_HOME" in runtime["isolate_with"]


def test_spawning_piper_is_reported_as_degraded(capsys, tmp_sessions, monkeypatch):
    """Spawning per reply costs ~3.5s of process startup the resident voice does not.

    It used to pass silently, so the only way to notice was to read `detail` and know what the
    two strings meant.
    """
    monkeypatch.setenv("VOICE_TUNNEL_TTS", "piper")
    monkeypatch.setenv("VOICE_TUNNEL_PIPER_INPROCESS", "0")
    from tests.test_cli_surface import run

    _, payload, _ = run(["doctor"], capsys)
    tts = next(c for c in payload["checks"] if c["name"] == "tts")
    if "spawning" in tts["detail"]:
        assert tts["status"] == "degraded", "a spawning engine is a fallback, not a clean pass"
        assert tts["remedy"], "and it must say how to get the resident one"


def test_the_voiceprint_check_is_always_present(capsys, tmp_sessions):
    """A check that disappears when it starts passing is a check nobody can rely on.

    Absence of a warning is not evidence of readiness, and an auditor had to go find the answer
    in `download --list` because this line had quietly gone away.
    """
    from tests.test_cli_surface import run

    _, payload, _ = run(["doctor"], capsys)
    names = [c["name"] for c in payload["checks"]]
    assert "voiceprint" in names, "voiceprint must report either way, present or missing"


def test_downloads_say_bytes_fetched_not_bytes(tmp_path, monkeypatch):
    """`bytes: 0` read as 'this file is empty'; it meant 'nothing was downloaded'."""
    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", str(tmp_path))
    src = (config.ROOT and os.path.join(config.ROOT, "voice_tunnel", "download.py"))
    text = open(src, encoding="utf-8").read()
    assert '"bytes":' not in text, "the ambiguous key is back"
    assert '"bytes_fetched":' in text
