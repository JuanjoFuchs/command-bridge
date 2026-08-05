"""Where runtime state lands — the difference between a checkout and an installed package.

Every path used to hang off `config.ROOT`: `<root>/.env`, `<root>/sessions`, `<root>/models`.
That is correct in a checkout and silently wrong once installed, because ROOT is then
`site-packages` — turn logs and a ~600 MB ASR model written into the interpreter's library
directory, destroyed by the next `pip install --upgrade`, and outright failing wherever
site-packages is read-only (a system Python, a container, most managed environments).

**No test could have caught it, because the suite only ever runs from a checkout.** These tests
force the other branch instead of hoping to be run in it.
"""
import os
import sys

import pytest

from voice_tunnel import config


@pytest.fixture
def installed(monkeypatch):
    """Pretend this is an installed package rather than a checkout."""
    monkeypatch.setattr(config, "_in_source_checkout", lambda: False)
    # The real ones must not leak in and make an assertion pass for the wrong reason.
    for var in ("VOICE_TUNNEL_DIR", "VOICE_TUNNEL_MODELS_DIR", "VOICE_TUNNEL_ENV_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def checkout(monkeypatch):
    monkeypatch.setattr(config, "_in_source_checkout", lambda: True)
    for var in ("VOICE_TUNNEL_DIR", "VOICE_TUNNEL_MODELS_DIR", "VOICE_TUNNEL_ENV_FILE"):
        monkeypatch.delenv(var, raising=False)


def test_a_checkout_keeps_its_state_local(checkout):
    """Unchanged behaviour, asserted so the install work cannot quietly move a developer's data.

    Repo-local matters for a reason beyond tidiness: two working trees must not fight over one
    turn log, and a dev run must leave nothing behind in $HOME.
    """
    assert config.session_dir() == os.path.join(config.ROOT, "sessions")
    assert config.models_dir() == os.path.join(config.ROOT, "models")
    assert config.env_file_path() == os.path.join(config.ROOT, ".env")


def test_an_installed_package_never_writes_next_to_its_own_code(installed):
    """THE regression. site-packages is not a data directory."""
    for path in (config.session_dir(), config.models_dir(), config.env_file_path()):
        assert not path.startswith(config.ROOT), (
            f"{path} is inside the package directory; an upgrade would delete it and a "
            f"read-only install would refuse to write it"
        )
        assert "voice-tunnel" in path, f"{path} should be namespaced to this tool"


def test_installed_paths_are_under_one_user_directory(installed):
    """Sessions and models share a root, so 'where is my data' has ONE answer."""
    assert os.path.dirname(config.session_dir()) == os.path.dirname(config.models_dir())


def test_the_env_var_overrides_win_in_both_modes(installed, monkeypatch):
    """An explicit setting is always the last word — that is what makes the suite able to run
    against disposable directories, and what lets a user relocate a 600 MB model cache."""
    monkeypatch.setenv("VOICE_TUNNEL_DIR", os.path.join("X:", "turns"))
    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", os.path.join("X:", "models"))
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", os.path.join("X:", "settings.env"))

    assert config.session_dir() == os.path.join("X:", "turns")
    assert config.models_dir() == os.path.join("X:", "models")
    assert config.env_file_path() == os.path.join("X:", "settings.env")


# Each platform asserts ITS OWN convention rather than one test skipping everywhere else. The
# earlier pair skipped XDG only on Windows and therefore ran on macOS, where the code correctly
# ignores XDG in favour of ~/Library/Application Support — a green suite on two platforms and a
# red one on the third, for a defect that was in the test. A three-OS matrix is only worth its
# cost if each OS checks something the others cannot.

@pytest.mark.skipif(os.name != "nt", reason="Windows convention")
def test_windows_uses_local_appdata_not_roaming(installed, monkeypatch):
    """Local, deliberately. Roaming profiles sync to a network share on managed machines, and a
    600 MB model in there is somebody's login taking ten minutes."""
    monkeypatch.setenv("LOCALAPPDATA", os.path.join("C:", "Users", "someone", "AppData", "Local"))
    assert "Local" in config.models_dir()
    assert "Roaming" not in config.models_dir()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS convention")
def test_macos_uses_application_support_and_ignores_xdg(installed, monkeypatch):
    """XDG is a freedesktop convention; a Mac user looking for their data looks in
    ~/Library/Application Support, and honouring XDG there would hide it from them."""
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")

    assert "Library/Application Support/voice-tunnel" in config.session_dir()
    assert not config.session_dir().startswith("/tmp/xdg-data")


@pytest.mark.skipif(os.name == "nt" or sys.platform == "darwin", reason="XDG is a Linux/BSD convention")
def test_xdg_variables_are_honoured(installed, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-config")
    assert config.session_dir().startswith("/tmp/xdg-data")
    assert config.env_file_path().startswith("/tmp/xdg-config")


def test_pyproject_marks_a_checkout(monkeypatch, tmp_path):
    """`pyproject.toml` beside the package, not `.git`, because an sdist or a zip download is
    still a checkout and has no `.git` at all."""
    monkeypatch.setattr(config, "ROOT", str(tmp_path))
    assert config._in_source_checkout() is False

    (tmp_path / "pyproject.toml").write_text("[project]\nname='voice-tunnel'\n", encoding="utf-8")
    assert config._in_source_checkout() is True
