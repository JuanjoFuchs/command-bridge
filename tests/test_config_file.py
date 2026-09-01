"""The persisted settings file — parsing, precedence, and safe writes.

These exist because the failure they guard against is invisible: a settings file that silently
does not load, or that loads and silently overrides an intentional one-off export, produces a
tool that behaves differently depending on which directory you were standing in. Every test
below is a statement about which value wins.
"""
import os

import pytest

from command_bridge import config

# ------------------------------------------------------------------- parsing


def test_parses_comments_blanks_and_export_prefixes():
    text = """
    # a comment
    COMMAND_BRIDGE_TTS=piper

    export COMMAND_BRIDGE_DIR=/tmp/sessions
    """
    assert config.parse_env_text(text) == {"COMMAND_BRIDGE_TTS": "piper", "COMMAND_BRIDGE_DIR": "/tmp/sessions"}


def test_quoted_values_keep_their_spaces_and_hashes():
    """A Windows path is the common case — `C:\\Program Files\\...` has a space in it, and an
    unquoted reader would truncate the setting at the space and then fail to find the binary."""
    parsed = config.parse_env_text(
        'COMMAND_BRIDGE_PIPER_BIN="C:/Program Files/piper/piper.exe"\n'
        "COMMAND_BRIDGE_OWNER='a # b'\n"
    )
    assert parsed["COMMAND_BRIDGE_PIPER_BIN"] == "C:/Program Files/piper/piper.exe"
    assert parsed["COMMAND_BRIDGE_OWNER"] == "a # b"


def test_unquoted_value_drops_a_trailing_comment():
    assert config.parse_env_text("COMMAND_BRIDGE_TTS=piper  # the good one")["COMMAND_BRIDGE_TTS"] == "piper"


def test_a_malformed_line_is_skipped_not_fatal():
    """The file is hand-edited. Refusing to start because line 2 is a stray word would be a
    worse failure than ignoring line 2 — and `config show` reports what was ignored."""
    assert config.parse_env_text("COMMAND_BRIDGE_TTS=piper\nnonsense\nCOMMAND_BRIDGE_CUES=0") == {
        "COMMAND_BRIDGE_TTS": "piper", "COMMAND_BRIDGE_CUES": "0"
    }


def test_a_value_may_contain_equals_signs():
    assert config.parse_env_text("COMMAND_BRIDGE_TOKEN=ab=cd=ef")["COMMAND_BRIDGE_TOKEN"] == "ab=cd=ef"


def test_crlf_line_endings_parse(tmp_path):
    """The file is edited on Windows; splitlines handles \\r\\n but a naive split on \\n would
    leave a trailing \\r glued to every value."""
    p = tmp_path / ".env"
    p.write_bytes(b"COMMAND_BRIDGE_TTS=piper\r\nCOMMAND_BRIDGE_OWNER=jj\r\n")
    assert config.read_env_file(str(p)) == {"COMMAND_BRIDGE_TTS": "piper", "COMMAND_BRIDGE_OWNER": "jj"}


def test_the_last_duplicate_wins():
    assert config.parse_env_text("COMMAND_BRIDGE_TTS=sapi\nCOMMAND_BRIDGE_TTS=piper")["COMMAND_BRIDGE_TTS"] == "piper"


# ---------------------------------------------------------------- precedence


def test_the_file_fills_in_what_the_environment_has_not_set(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("COMMAND_BRIDGE_TTS=piper\n", encoding="utf-8")
    monkeypatch.delenv("COMMAND_BRIDGE_TTS", raising=False)

    report = config.load_env_file(str(p))

    assert os.environ["COMMAND_BRIDGE_TTS"] == "piper"
    assert report["applied"] == ["COMMAND_BRIDGE_TTS"]


def test_the_environment_beats_the_file(tmp_path, monkeypatch):
    """The whole contract. scripts/e2e.py hands its child COMMAND_BRIDGE_DIR/COMMAND_BRIDGE_TOKEN/COMMAND_BRIDGE_TTS and must win
    over whatever the developer has persisted, or the acceptance run reads the wrong turn log."""
    p = tmp_path / ".env"
    p.write_text("COMMAND_BRIDGE_TTS=piper\n", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_TTS", "sapi")

    report = config.load_env_file(str(p))

    assert os.environ["COMMAND_BRIDGE_TTS"] == "sapi"
    assert report["shadowed"] == ["COMMAND_BRIDGE_TTS"]
    assert report["applied"] == []


def test_loading_is_idempotent(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("COMMAND_BRIDGE_TTS=piper\n", encoding="utf-8")
    monkeypatch.delenv("COMMAND_BRIDGE_TTS", raising=False)

    config.load_env_file(str(p))
    second = config.load_env_file(str(p))

    # The second pass sees its own work already in the environment, so it shadows rather than
    # re-applies. That is what makes calling it from main() on every command harmless.
    assert second["shadowed"] == ["COMMAND_BRIDGE_TTS"]
    assert os.environ["COMMAND_BRIDGE_TTS"] == "piper"


def test_a_missing_file_is_not_an_error(tmp_path):
    report = config.load_env_file(str(tmp_path / "nope.env"))
    assert report["exists"] is False and report["applied"] == []


def test_a_bogus_variable_name_is_reported_not_applied(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("9BAD=x\nCOMMAND_BRIDGE_OWNER=jj\n", encoding="utf-8")
    monkeypatch.delenv("COMMAND_BRIDGE_OWNER", raising=False)

    report = config.load_env_file(str(p))

    assert report["ignored"] == ["9BAD"]
    assert "9BAD" not in os.environ


# -------------------------------------------------------------------- writing


def test_set_creates_the_file_with_a_header(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(p))

    result = config.write_setting("COMMAND_BRIDGE_TTS", "piper")

    assert result["created"] is True
    assert config.read_env_file(str(p)) == {"COMMAND_BRIDGE_TTS": "piper"}
    assert p.read_text(encoding="utf-8").startswith("#")


def test_set_replaces_in_place_and_keeps_comments_and_neighbours(tmp_path, monkeypatch):
    """A config file that loses its comments the first time a tool touches it is a config file
    people stop letting tools touch."""
    p = tmp_path / ".env"
    p.write_text("# keep me\nCOMMAND_BRIDGE_TTS=sapi\nCOMMAND_BRIDGE_OWNER=jj\n", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(p))

    config.write_setting("COMMAND_BRIDGE_TTS", "piper")

    text = p.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert config.read_env_file(str(p)) == {"COMMAND_BRIDGE_TTS": "piper", "COMMAND_BRIDGE_OWNER": "jj"}


def test_set_collapses_duplicate_keys(tmp_path, monkeypatch):
    """Leaving a stale duplicate behind would mean the file says one thing and the process
    does another — the reader keeps the last occurrence."""
    p = tmp_path / ".env"
    p.write_text("COMMAND_BRIDGE_TTS=sapi\nCOMMAND_BRIDGE_TTS=none\n", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(p))

    result = config.write_setting("COMMAND_BRIDGE_TTS", "piper")

    assert result["replaced"] == 2
    assert p.read_text(encoding="utf-8").count("COMMAND_BRIDGE_TTS=") == 1


def test_values_needing_quotes_round_trip(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(p))

    config.write_setting("COMMAND_BRIDGE_PIPER_BIN", "C:/Program Files/piper/piper.exe")

    assert config.read_env_file(str(p))["COMMAND_BRIDGE_PIPER_BIN"] == "C:/Program Files/piper/piper.exe"


def test_unset_removes_the_key(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("COMMAND_BRIDGE_TTS=piper\nCOMMAND_BRIDGE_OWNER=jj\n", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(p))

    config.write_setting("COMMAND_BRIDGE_TTS", None)

    assert config.read_env_file(str(p)) == {"COMMAND_BRIDGE_OWNER": "jj"}


# ----------------------------------------------------------------- validation
#
# "Agents hallucinate. Build like it." — the input hardening these tests pin down is aimed at a
# confused caller, not a hostile one, which is why every rejection names the remedy.


@pytest.mark.parametrize("key", ["PATH", "command_bridge_tts", "TTS", "", "VOICE_TUNNEL tts", "COMMAND_BRIDGE_TTS=x"])
def test_only_this_tools_namespace_can_be_written(key, tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / ".env"))
    with pytest.raises(ValueError, match="settable key"):
        config.write_setting(key, "x")


def test_a_newline_in_a_value_is_refused(tmp_path, monkeypatch):
    """Otherwise `command-bridge config set COMMAND_BRIDGE_OWNER $'jj\\nCOMMAND_BRIDGE_TOKEN=stolen'` forges a second setting."""
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / ".env"))
    with pytest.raises(ValueError, match="newline or control character"):
        config.write_setting("COMMAND_BRIDGE_OWNER", "jj\nCOMMAND_BRIDGE_TOKEN=stolen")


def test_a_quote_in_a_value_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / ".env"))
    with pytest.raises(ValueError, match="quote character"):
        config.write_setting("COMMAND_BRIDGE_OWNER", 'j"j')


# -------------------------------------------------------------- piper defaults
#
# The point of these: `COMMAND_BRIDGE_TTS=piper` should be the ONLY setting a piper session needs. Before
# this, the binary and the voice had to be exported on every single invocation.


def test_the_piper_binary_is_found_in_the_repo_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMAND_BRIDGE_PIPER_BIN", raising=False)
    fake_root = tmp_path / "repo"
    scripts = fake_root / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "piper.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", str(fake_root))
    # The running interpreter is searched first now, so point it somewhere empty to isolate the
    # behaviour this test is actually about.
    monkeypatch.setattr(config.sys, "executable", str(tmp_path / "nowhere" / "python.exe"))

    assert config.piper_bin() == str(scripts / "piper.exe")


def test_this_interpreter_s_piper_beats_the_repo_venv(tmp_path, monkeypatch):
    """`pip install command-bridge[piper]` puts piper.exe beside the interpreter that installed it.

    That copy is by definition the one this process's packages were installed with, and it used
    to lose to a checkout path and then to PATH. A cold-start audit built a fully isolated
    installation and found it still resolving a `piper.exe` belonging to a different Python —
    a cross-installation dependency inside a copy that had isolated everything else.
    """
    monkeypatch.delenv("COMMAND_BRIDGE_PIPER_BIN", raising=False)
    mine = tmp_path / "myenv" / "Scripts"
    mine.mkdir(parents=True)
    (mine / "piper.exe").write_text("", encoding="utf-8")

    other = tmp_path / "repo" / "venv" / "Scripts"
    other.mkdir(parents=True)
    (other / "piper.exe").write_text("", encoding="utf-8")

    monkeypatch.setattr(config, "ROOT", str(tmp_path / "repo"))
    monkeypatch.setattr(config.sys, "executable", str(mine / "python.exe"))

    assert config.piper_bin() == str(mine / "piper.exe")


def test_an_explicit_piper_binary_wins_over_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_PIPER_BIN", "/somewhere/else/piper")
    assert config.piper_bin() == "/somewhere/else/piper"


def test_only_onnx_files_with_a_sidecar_count_as_voices(tmp_path, monkeypatch):
    """The voiceprint gallery's speaker model shares the models dir. Listing it as a voice
    offered a selection that then failed at synthesis time."""
    models = tmp_path / "models"
    models.mkdir()
    for name in ("en_GB-alan-medium", "en_US-amy-medium"):
        (models / f"{name}.onnx").write_text("", encoding="utf-8")
        (models / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
    (models / "nemo_en_titanet_large.onnx").write_text("", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_MODELS_DIR", str(models))

    assert config.piper_voices() == ["en_GB-alan-medium", "en_US-amy-medium"]


def test_the_default_voice_is_chosen_when_several_are_installed(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMAND_BRIDGE_PIPER_VOICE", raising=False)
    models = tmp_path / "models"
    models.mkdir()
    for name in (config.DEFAULT_PIPER_VOICE, "en_US-amy-medium"):
        (models / f"{name}.onnx").write_text("", encoding="utf-8")
        (models / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_MODELS_DIR", str(models))

    assert config.piper_voice() == str(models / f"{config.DEFAULT_PIPER_VOICE}.onnx")


def test_a_sole_installed_voice_is_taken_even_if_it_is_not_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMAND_BRIDGE_PIPER_VOICE", raising=False)
    models = tmp_path / "models"
    models.mkdir()
    (models / "en_US-amy-medium.onnx").write_text("", encoding="utf-8")
    (models / "en_US-amy-medium.onnx.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_MODELS_DIR", str(models))

    assert config.piper_voice() == str(models / "en_US-amy-medium.onnx")


def test_no_voice_is_chosen_when_the_choice_would_be_a_guess(tmp_path, monkeypatch):
    """Several installed and none of them the default: guessing which voice someone wants to
    hear is worse than saying "name one", which is what `command-bridge doctor` then does."""
    monkeypatch.delenv("COMMAND_BRIDGE_PIPER_VOICE", raising=False)
    models = tmp_path / "models"
    models.mkdir()
    for name in ("en_US-amy-medium", "en_US-ryan-high"):
        (models / f"{name}.onnx").write_text("", encoding="utf-8")
        (models / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_MODELS_DIR", str(models))

    assert config.piper_voice() == ""


# ------------------------------------------------------------------- registry


def test_env_example_documents_every_setting():
    """A deterministic check beats a convention nobody enforces: the previous `describe` block
    listed 8 of the 17 variables the code read, and the omitted ones were exactly the piper
    settings a session could not start without."""
    text = (
        open(os.path.join(config.ROOT, ".env.example"), encoding="utf-8").read()
    )
    undocumented = [s["key"] for s in config.SETTINGS if s["key"] not in text]
    assert not undocumented, f"add these to .env.example: {undocumented}"


def test_every_setting_resolves_without_blowing_up():
    """`config show` calls all of them at once, so one raising resolver takes out the command
    that exists to tell you what is wrong."""
    for row in config.effective():
        assert isinstance(row["value"], str)
        assert row["source"] in ("env", "file", "default")


# ------------------------------------------------------- back-compat (spec 001 FR4)


def test_a_legacy_voice_tunnel_var_is_honored_under_the_new_prefix(monkeypatch):
    """The rename kept the old prefix working: a COMMAND_BRIDGE_* read falls back to the
    VOICE_TUNNEL_* twin when the new name is unset, so a `.env` written before the rename — the
    pinned token above all — is not silently invalidated."""
    monkeypatch.delenv("COMMAND_BRIDGE_TOKEN", raising=False)
    monkeypatch.setenv("VOICE_TUNNEL_TOKEN", "legacy-secret")
    assert config._env("COMMAND_BRIDGE_TOKEN") == "legacy-secret"


def test_the_new_prefix_wins_over_the_legacy_twin(monkeypatch):
    """A value under the new name is preferred; the fallback only fills a gap."""
    monkeypatch.setenv("COMMAND_BRIDGE_TOKEN", "new-secret")
    monkeypatch.setenv("VOICE_TUNNEL_TOKEN", "legacy-secret")
    assert config._env("COMMAND_BRIDGE_TOKEN") == "new-secret"


def test_load_env_file_maps_a_legacy_key_to_the_new_prefix(tmp_path):
    """A pre-rename .env with only VOICE_TUNNEL_* keys populates the COMMAND_BRIDGE_* twins at load
    time, so even the readers that hit os.environ directly (the token) see them. The conftest
    scrubs COMMAND_BRIDGE_*; the legacy key is cleaned here."""
    for k in ("COMMAND_BRIDGE_TOKEN", "VOICE_TUNNEL_TOKEN"):
        os.environ.pop(k, None)
    env = tmp_path / ".env"
    env.write_text("VOICE_TUNNEL_TOKEN=pinned\n", encoding="utf-8")
    try:
        config.load_env_file(str(env))
        assert os.environ.get("COMMAND_BRIDGE_TOKEN") == "pinned"
    finally:
        os.environ.pop("VOICE_TUNNEL_TOKEN", None)
        os.environ.pop("COMMAND_BRIDGE_TOKEN", None)
