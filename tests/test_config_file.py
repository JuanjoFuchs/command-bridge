"""The persisted settings file — parsing, precedence, and safe writes.

These exist because the failure they guard against is invisible: a settings file that silently
does not load, or that loads and silently overrides an intentional one-off export, produces a
tool that behaves differently depending on which directory you were standing in. Every test
below is a statement about which value wins.
"""
import os

import pytest

from voice_tunnel import config


# ------------------------------------------------------------------- parsing


def test_parses_comments_blanks_and_export_prefixes():
    text = """
    # a comment
    VOICE_TUNNEL_TTS=piper

    export VOICE_TUNNEL_DIR=/tmp/sessions
    """
    assert config.parse_env_text(text) == {"VOICE_TUNNEL_TTS": "piper", "VOICE_TUNNEL_DIR": "/tmp/sessions"}


def test_quoted_values_keep_their_spaces_and_hashes():
    """A Windows path is the common case — `C:\\Program Files\\...` has a space in it, and an
    unquoted reader would truncate the setting at the space and then fail to find the binary."""
    parsed = config.parse_env_text(
        'VOICE_TUNNEL_PIPER_BIN="C:/Program Files/piper/piper.exe"\n'
        "VOICE_TUNNEL_OWNER='a # b'\n"
    )
    assert parsed["VOICE_TUNNEL_PIPER_BIN"] == "C:/Program Files/piper/piper.exe"
    assert parsed["VOICE_TUNNEL_OWNER"] == "a # b"


def test_unquoted_value_drops_a_trailing_comment():
    assert config.parse_env_text("VOICE_TUNNEL_TTS=piper  # the good one")["VOICE_TUNNEL_TTS"] == "piper"


def test_a_malformed_line_is_skipped_not_fatal():
    """The file is hand-edited. Refusing to start because line 2 is a stray word would be a
    worse failure than ignoring line 2 — and `config show` reports what was ignored."""
    assert config.parse_env_text("VOICE_TUNNEL_TTS=piper\nnonsense\nVOICE_TUNNEL_CUES=0") == {
        "VOICE_TUNNEL_TTS": "piper", "VOICE_TUNNEL_CUES": "0"
    }


def test_a_value_may_contain_equals_signs():
    assert config.parse_env_text("VOICE_TUNNEL_TOKEN=ab=cd=ef")["VOICE_TUNNEL_TOKEN"] == "ab=cd=ef"


def test_crlf_line_endings_parse(tmp_path):
    """The file is edited on Windows; splitlines handles \\r\\n but a naive split on \\n would
    leave a trailing \\r glued to every value."""
    p = tmp_path / ".env"
    p.write_bytes(b"VOICE_TUNNEL_TTS=piper\r\nVOICE_TUNNEL_OWNER=jj\r\n")
    assert config.read_env_file(str(p)) == {"VOICE_TUNNEL_TTS": "piper", "VOICE_TUNNEL_OWNER": "jj"}


def test_the_last_duplicate_wins():
    assert config.parse_env_text("VOICE_TUNNEL_TTS=sapi\nVOICE_TUNNEL_TTS=piper")["VOICE_TUNNEL_TTS"] == "piper"


# ---------------------------------------------------------------- precedence


def test_the_file_fills_in_what_the_environment_has_not_set(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("VOICE_TUNNEL_TTS=piper\n", encoding="utf-8")
    monkeypatch.delenv("VOICE_TUNNEL_TTS", raising=False)

    report = config.load_env_file(str(p))

    assert os.environ["VOICE_TUNNEL_TTS"] == "piper"
    assert report["applied"] == ["VOICE_TUNNEL_TTS"]


def test_the_environment_beats_the_file(tmp_path, monkeypatch):
    """The whole contract. scripts/e2e.py hands its child VOICE_TUNNEL_DIR/VOICE_TUNNEL_TOKEN/VOICE_TUNNEL_TTS and must win
    over whatever the developer has persisted, or the acceptance run reads the wrong turn log."""
    p = tmp_path / ".env"
    p.write_text("VOICE_TUNNEL_TTS=piper\n", encoding="utf-8")
    monkeypatch.setenv("VOICE_TUNNEL_TTS", "sapi")

    report = config.load_env_file(str(p))

    assert os.environ["VOICE_TUNNEL_TTS"] == "sapi"
    assert report["shadowed"] == ["VOICE_TUNNEL_TTS"]
    assert report["applied"] == []


def test_loading_is_idempotent(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("VOICE_TUNNEL_TTS=piper\n", encoding="utf-8")
    monkeypatch.delenv("VOICE_TUNNEL_TTS", raising=False)

    config.load_env_file(str(p))
    second = config.load_env_file(str(p))

    # The second pass sees its own work already in the environment, so it shadows rather than
    # re-applies. That is what makes calling it from main() on every command harmless.
    assert second["shadowed"] == ["VOICE_TUNNEL_TTS"]
    assert os.environ["VOICE_TUNNEL_TTS"] == "piper"


def test_a_missing_file_is_not_an_error(tmp_path):
    report = config.load_env_file(str(tmp_path / "nope.env"))
    assert report["exists"] is False and report["applied"] == []


def test_a_bogus_variable_name_is_reported_not_applied(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("9BAD=x\nVOICE_TUNNEL_OWNER=jj\n", encoding="utf-8")
    monkeypatch.delenv("VOICE_TUNNEL_OWNER", raising=False)

    report = config.load_env_file(str(p))

    assert report["ignored"] == ["9BAD"]
    assert "9BAD" not in os.environ


# -------------------------------------------------------------------- writing


def test_set_creates_the_file_with_a_header(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(p))

    result = config.write_setting("VOICE_TUNNEL_TTS", "piper")

    assert result["created"] is True
    assert config.read_env_file(str(p)) == {"VOICE_TUNNEL_TTS": "piper"}
    assert p.read_text(encoding="utf-8").startswith("#")


def test_set_replaces_in_place_and_keeps_comments_and_neighbours(tmp_path, monkeypatch):
    """A config file that loses its comments the first time a tool touches it is a config file
    people stop letting tools touch."""
    p = tmp_path / ".env"
    p.write_text("# keep me\nVOICE_TUNNEL_TTS=sapi\nVOICE_TUNNEL_OWNER=jj\n", encoding="utf-8")
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(p))

    config.write_setting("VOICE_TUNNEL_TTS", "piper")

    text = p.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert config.read_env_file(str(p)) == {"VOICE_TUNNEL_TTS": "piper", "VOICE_TUNNEL_OWNER": "jj"}


def test_set_collapses_duplicate_keys(tmp_path, monkeypatch):
    """Leaving a stale duplicate behind would mean the file says one thing and the process
    does another — the reader keeps the last occurrence."""
    p = tmp_path / ".env"
    p.write_text("VOICE_TUNNEL_TTS=sapi\nVOICE_TUNNEL_TTS=none\n", encoding="utf-8")
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(p))

    result = config.write_setting("VOICE_TUNNEL_TTS", "piper")

    assert result["replaced"] == 2
    assert p.read_text(encoding="utf-8").count("VOICE_TUNNEL_TTS=") == 1


def test_values_needing_quotes_round_trip(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(p))

    config.write_setting("VOICE_TUNNEL_PIPER_BIN", "C:/Program Files/piper/piper.exe")

    assert config.read_env_file(str(p))["VOICE_TUNNEL_PIPER_BIN"] == "C:/Program Files/piper/piper.exe"


def test_unset_removes_the_key(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("VOICE_TUNNEL_TTS=piper\nVOICE_TUNNEL_OWNER=jj\n", encoding="utf-8")
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(p))

    config.write_setting("VOICE_TUNNEL_TTS", None)

    assert config.read_env_file(str(p)) == {"VOICE_TUNNEL_OWNER": "jj"}


# ----------------------------------------------------------------- validation
#
# "Agents hallucinate. Build like it." — the input hardening these tests pin down is aimed at a
# confused caller, not a hostile one, which is why every rejection names the remedy.


@pytest.mark.parametrize("key", ["PATH", "voice_tunnel_tts", "TTS", "", "VOICE_TUNNEL tts", "VOICE_TUNNEL_TTS=x"])
def test_only_this_tools_namespace_can_be_written(key, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / ".env"))
    with pytest.raises(ValueError, match="settable key"):
        config.write_setting(key, "x")


def test_a_newline_in_a_value_is_refused(tmp_path, monkeypatch):
    """Otherwise `voice-tunnel config set VOICE_TUNNEL_OWNER $'jj\\nVOICE_TUNNEL_TOKEN=stolen'` forges a second setting."""
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / ".env"))
    with pytest.raises(ValueError, match="newline or control character"):
        config.write_setting("VOICE_TUNNEL_OWNER", "jj\nVOICE_TUNNEL_TOKEN=stolen")


def test_a_quote_in_a_value_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / ".env"))
    with pytest.raises(ValueError, match="quote character"):
        config.write_setting("VOICE_TUNNEL_OWNER", 'j"j')


# -------------------------------------------------------------- piper defaults
#
# The point of these: `VOICE_TUNNEL_TTS=piper` should be the ONLY setting a piper session needs. Before
# this, the binary and the voice had to be exported on every single invocation.


def test_the_piper_binary_is_found_in_the_repo_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICE_TUNNEL_PIPER_BIN", raising=False)
    fake_root = tmp_path / "repo"
    scripts = fake_root / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "piper.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "ROOT", str(fake_root))

    assert config.piper_bin() == str(scripts / "piper.exe")


def test_an_explicit_piper_binary_wins_over_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_PIPER_BIN", "/somewhere/else/piper")
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
    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", str(models))

    assert config.piper_voices() == ["en_GB-alan-medium", "en_US-amy-medium"]


def test_the_default_voice_is_chosen_when_several_are_installed(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICE_TUNNEL_PIPER_VOICE", raising=False)
    models = tmp_path / "models"
    models.mkdir()
    for name in (config.DEFAULT_PIPER_VOICE, "en_US-amy-medium"):
        (models / f"{name}.onnx").write_text("", encoding="utf-8")
        (models / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", str(models))

    assert config.piper_voice() == str(models / f"{config.DEFAULT_PIPER_VOICE}.onnx")


def test_a_sole_installed_voice_is_taken_even_if_it_is_not_the_default(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICE_TUNNEL_PIPER_VOICE", raising=False)
    models = tmp_path / "models"
    models.mkdir()
    (models / "en_US-amy-medium.onnx").write_text("", encoding="utf-8")
    (models / "en_US-amy-medium.onnx.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", str(models))

    assert config.piper_voice() == str(models / "en_US-amy-medium.onnx")


def test_no_voice_is_chosen_when_the_choice_would_be_a_guess(tmp_path, monkeypatch):
    """Several installed and none of them the default: guessing which voice someone wants to
    hear is worse than saying "name one", which is what `voice-tunnel doctor` then does."""
    monkeypatch.delenv("VOICE_TUNNEL_PIPER_VOICE", raising=False)
    models = tmp_path / "models"
    models.mkdir()
    for name in ("en_US-amy-medium", "en_US-ryan-high"):
        (models / f"{name}.onnx").write_text("", encoding="utf-8")
        (models / f"{name}.onnx.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VOICE_TUNNEL_MODELS_DIR", str(models))

    assert config.piper_voice() == ""


# ------------------------------------------------------------------- registry


def test_env_example_documents_every_setting():
    """A deterministic check beats a convention nobody enforces: the previous `describe` block
    listed 8 of the 17 variables the code read, and the omitted ones were exactly the piper
    settings a session could not start without."""
    text = (
        open(os.path.join(config.ROOT, ".env.example"), "r", encoding="utf-8").read()
    )
    undocumented = [s["key"] for s in config.SETTINGS if s["key"] not in text]
    assert not undocumented, f"add these to .env.example: {undocumented}"


def test_every_setting_resolves_without_blowing_up():
    """`config show` calls all of them at once, so one raising resolver takes out the command
    that exists to tell you what is wrong."""
    for row in config.effective():
        assert isinstance(row["value"], str)
        assert row["source"] in ("env", "file", "default")
