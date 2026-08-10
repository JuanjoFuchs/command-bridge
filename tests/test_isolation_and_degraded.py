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


def test_a_shim_belonging_to_another_install_is_not_a_clean_pass(capsys, tmp_sessions,
                                                                 tmp_path, monkeypatch):
    """`voice-tunnel` being on PATH says nothing about *which* voice-tunnel is on PATH.

    A cold-start audit built an isolated copy, configured it end to end, and this check reported
    `ok` throughout — naming a console script from a different installation that still carried
    another agent's wake name. Every subsequent bare `voice-tunnel` would have run that one, which
    is the same class of mistake as the incident the isolation work exists to prevent.
    """
    from tests.test_cli_surface import run

    stranger = tmp_path / "somebody-else" / "Scripts"
    stranger.mkdir(parents=True)
    shim = stranger / "voice-tunnel.exe"
    shim.write_text("", encoding="utf-8")
    monkeypatch.setattr(config.shutil, "which",
                        lambda name: str(shim) if name == "voice-tunnel" else None)
    monkeypatch.setattr(config.sys, "executable", str(tmp_path / "mine" / "Scripts" / "python.exe"))
    monkeypatch.setattr(config, "_in_source_checkout", lambda: False)

    _, payload, _ = run(["doctor"], capsys)
    check = next(c for c in payload["checks"] if c["name"] == "shim_on_path")

    assert check["status"] == "info", "a stranger's shim is worth saying, and is not a fallback"
    assert "somebody-else" in check["detail"] and "DIFFERENT" in check["detail"].upper()
    assert check["remedy"], "and it must say how to reach this copy instead"
    assert "shim_on_path" in payload["advisory"]
    assert "shim_on_path" not in payload["degraded"], (
        "an unclearable warning in `degraded` is how that list stops being read"
    )


def test_an_advisory_alone_does_not_make_the_runtime_sound_broken(capsys, tmp_sessions,
                                                                  tmp_path, monkeypatch):
    """THE REGRESSION, and it is about `next` rather than the check.

    An audit reached a state where `shim_on_path` was the only non-ok item and watched `doctor`
    answer, every time: "RUNS, BUT NOT AS CONFIGURED — shim_on_path is on a fallback.
    `voice-tunnel setup` installs the optional engines and downloads every model." `setup` does
    not touch PATH. It had already run. The correct advice was sitting in that check's own
    `remedy` one level down, and the top-level line — the one thing an agent reads first — was a
    hardcoded template that ignored the diagnosis and produced a loop.
    """
    from tests.test_cli_surface import run

    stranger = tmp_path / "elsewhere" / "Scripts"
    stranger.mkdir(parents=True)
    (stranger / "voice-tunnel.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(config.shutil, "which",
                        lambda name: str(stranger / "voice-tunnel.exe")
                        if name == "voice-tunnel" else None)
    monkeypatch.setattr(config, "_in_source_checkout", lambda: False)

    _, payload, _ = run(["doctor"], capsys)
    if payload["degraded"] or payload["failed"]:
        pytest.skip("this machine has real gaps; the advisory-only state cannot be isolated here")

    assert "setup" not in payload["next"], (
        "nothing here is fixed by setup — do not prescribe it"
    )


def test_this_install_s_own_shim_is_a_clean_pass(capsys, tmp_sessions, tmp_path, monkeypatch):
    """The other side of it — the check must not cry wolf on a correct install.

    Compared by directory, because pip's console script and the interpreter that owns it are
    siblings and on Windows the case and the `.EXE` suffix both vary.
    """
    from tests.test_cli_surface import run

    scripts = tmp_path / "mine" / "Scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(config.shutil, "which",
                        lambda name: str(scripts / "VOICE-TUNNEL.EXE") if name == "voice-tunnel"
                        else None)
    monkeypatch.setattr(config.sys, "executable", str(scripts / "python.exe"))
    monkeypatch.setattr(config, "_in_source_checkout", lambda: False)

    _, payload, _ = run(["doctor"], capsys)
    check = next(c for c in payload["checks"] if c["name"] == "shim_on_path")
    assert check["status"] == "ok", f"false alarm on a correct install: {check['detail']}"


def test_downloads_say_bytes_fetched_not_bytes(tmp_path, monkeypatch):
    """`bytes: 0` read as 'this file is empty'; it meant 'nothing was downloaded'."""
    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", str(tmp_path))
    src = (config.ROOT and os.path.join(config.ROOT, "voice_tunnel", "download.py"))
    text = open(src, encoding="utf-8").read()
    assert '"bytes":' not in text, "the ambiguous key is back"
    assert '"bytes_fetched":' in text
