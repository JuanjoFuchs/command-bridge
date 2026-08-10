"""The agent-facing surface: `describe` as the contract, exit codes, and remedial errors.

`describe` is the live source of truth (AGENTS.md convention 3), which only holds if something
fails when it drifts. That is what the first test here is for — the previous drift was silent and
cost an agent a session's worth of guessing at env vars that were never written down.
"""
import argparse
import json

import pytest

from voice_tunnel import cli, config


def run(argv, capsys):
    """Invoke the CLI the way the shim does, and return (exit code, parsed stdout)."""
    code = cli.main(argv)
    out = capsys.readouterr()
    try:
        return code, json.loads(out.out or "{}"), out.err
    except json.JSONDecodeError:
        return code, None, out.err


# ------------------------------------------------------- describe is the contract


def test_describe_documents_every_command_the_parser_accepts():
    """The mechanical version of AGENTS.md convention 3. Add a command without documenting it
    and this fails in the same commit, rather than an agent discovering the gap at runtime."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    undocumented = set(sub.choices) - set(cli.DESCRIBE["commands"])
    assert not undocumented, f"add these to DESCRIBE['commands']: {sorted(undocumented)}"


def test_describe_documents_every_setting_the_code_reads():
    assert set(cli.DESCRIBE["env"]) == {s["key"] for s in config.SETTINGS}


def test_describe_carries_exit_codes_and_the_error_shape():
    """An agent branches on the exit code before it parses anything, so the codes have to be
    part of the published contract, not folklore."""
    assert set(cli.DESCRIBE["exit_codes"]) == {"0", "1", "2", "3"}
    assert set(cli.DESCRIBE["errors"]) == {"error", "code", "remedy"}


def test_describe_tells_the_caller_how_to_invoke_it():
    """The failure this documents: an agent fell back to
    `python -c "import sys; sys.path.insert(...)"` with four env-var prefixes, because nothing
    in the contract said there was a shim or a settings file."""
    invocation = cli.DESCRIBE["invocation"]
    assert "python -c" in invocation["no_python_dash_c"]
    assert "voice-tunnel config set" in cli.DESCRIBE["config_file"]["write_it_with"]


def test_describe_exits_zero_and_is_json(capsys):
    code, payload, _ = run(["describe"], capsys)
    assert code == cli.EXIT_OK
    assert payload["tool"] == "voice-tunnel"


# --------------------------------------------------------------- config command


def test_config_show_reports_the_source_of_every_value(capsys, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("VOICE_TUNNEL_OWNER=from-file\n", encoding="utf-8")
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(env_file))
    monkeypatch.delenv("VOICE_TUNNEL_OWNER", raising=False)
    monkeypatch.setenv("VOICE_TUNNEL_TTS", "none")

    code, payload, _ = run(["config", "show"], capsys)

    assert code == cli.EXIT_OK
    by_key = {row["key"]: row for row in payload["settings"]}
    assert by_key["VOICE_TUNNEL_OWNER"]["source"] == "file"
    assert by_key["VOICE_TUNNEL_TTS"]["source"] == "env"
    assert by_key["VOICE_TUNNEL_ASR_THREADS"]["source"] == "default"


def test_config_show_redacts_a_secret_but_get_reveals_it(capsys, monkeypatch, tmp_path):
    """A bulk dump lands in a transcript and an agent's context; an explicit single-key read is
    somebody actually asking for that value."""
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("VOICE_TUNNEL_TOKEN", "s3cret-token")

    _, shown, _ = run(["config", "show"], capsys)
    token_row = [r for r in shown["settings"] if r["key"] == "VOICE_TUNNEL_TOKEN"][0]
    assert token_row["value"] == config.REDACTED

    _, got, _ = run(["config", "get", "VOICE_TUNNEL_TOKEN"], capsys)
    assert got["value"] == "s3cret-token"


def test_config_set_then_get_round_trips(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("VOICE_TUNNEL_TTS", raising=False)

    code, written, _ = run(["config", "set", "VOICE_TUNNEL_TTS", "none"], capsys)
    assert code == cli.EXIT_OK and written["created"] is True

    monkeypatch.delenv("VOICE_TUNNEL_TTS", raising=False)
    _, got, _ = run(["config", "get", "VOICE_TUNNEL_TTS"], capsys)
    assert got == {"key": "VOICE_TUNNEL_TTS", "value": "none", "source": "file",
                   "what": got["what"]}


def test_config_set_warns_when_the_environment_will_shadow_the_write(capsys, tmp_path, monkeypatch):
    """Otherwise you set a value, watch the old one keep applying, and go looking in the code."""
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("VOICE_TUNNEL_TTS", "sapi")

    _, payload, _ = run(["config", "set", "VOICE_TUNNEL_TTS", "piper"], capsys)

    assert payload["shadowed_by_env"] is True
    assert "wins over the file" in payload["note"]


def test_config_get_on_an_unknown_key_names_the_remedy(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / "absent.env"))

    code, payload, _ = run(["config", "get", "VOICE_TUNNEL_NOPE"], capsys)

    assert code == cli.EXIT_ERROR
    assert payload["code"] == "invalid_input"
    assert "voice-tunnel config show" in payload["remedy"]


def test_config_set_of_a_foreign_key_exits_usage_with_a_remedy(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / ".env"))

    code, _, err = run(["config", "set", "PATH", "/tmp"], capsys)

    assert code == cli.EXIT_USAGE
    payload = json.loads(err)
    assert payload["code"] == "invalid_input"
    assert "VOICE_TUNNEL_" in payload["error"]


# --------------------------------------------------------------- exit codes


def test_a_command_needing_a_server_exits_three_not_one(capsys, tmp_sessions):
    """`no server` and `the request failed` call for different next moves — start one versus
    rephrase — so collapsing both into 1 forced the caller to string-match the message."""
    code, payload, _ = run(["status", "--session", "nothing-here"], capsys)

    assert code == cli.EXIT_NO_SERVER
    assert payload["code"] == "no_server"
    assert "voice-tunnel serve" in payload["remedy"]


def test_the_no_server_remedy_names_the_watch_that_must_follow(capsys, tmp_sessions):
    """AGENTS.md's rule 1: an agent told only "no server" starts one and then forgets to go
    straight back into a blocking watch, which from the user's side is a crash."""
    _, payload, _ = run(["say", "--session", "ghost", "hello"], capsys)

    assert "voice-tunnel watch" in payload["remedy"]


def test_a_bad_session_name_exits_usage(capsys, tmp_sessions):
    code, _, err = run(["turns", "--session", "../escape"], capsys)
    assert code == cli.EXIT_USAGE
    assert json.loads(err)["code"] == "invalid_input"


def test_reading_the_log_of_an_idle_session_is_success_not_failure(capsys, tmp_sessions):
    """A quiet tunnel is the normal state, and an agent that treats it as an error stops
    listening."""
    code, payload, _ = run(["turns", "--session", "quiet"], capsys)
    assert code == cli.EXIT_OK and payload["count"] == 0


# ------------------------------------------------------------------- doctor


def test_doctor_gives_every_actionable_check_a_remedy(capsys, tmp_sessions):
    """Anything not fully OK carries the command that fixes it — including a DEGRADED check.

    This used to assert `ok => remedy is None`, which encoded the binary model that caused the
    2026-08-10 incident: a fresh install answered `ok: true, failed: []` while running on the
    system voice and the slow recognizer, so the fixes had to be smuggled into `detail` and the
    field a parser reads was null on every line. Passing-but-degraded is now a state, and it is
    the one state that most needs a remedy attached.
    """
    code, payload, _ = run(["doctor"], capsys)

    assert code in (cli.EXIT_OK, cli.EXIT_ERROR)
    assert {c["name"] for c in payload["checks"]} >= {
        "interpreter", "dependencies", "settings_file", "session_dir", "tts", "asr",
        "shim_on_path",
    }
    for check in payload["checks"]:
        assert check["status"] in {"ok", "degraded", "failed"}
        if check["status"] == "ok":
            assert check["remedy"] is None, f"{check['name']} is fine; nothing to suggest"
        else:
            # A failure or a fallback without a remedy is one the caller has to go read source
            # code to act on.
            assert check["remedy"], f"{check['name']} is {check['status']} with no remedy"


def test_doctor_reports_which_runtime_answered(capsys, tmp_sessions):
    """The four facts that identify an installation, in one place.

    Every one of these was individually available on 2026-08-10 and none was assembled, so half a
    session ran against a second install nobody meant to use. No single check can ask "am I even
    the runtime you provisioned?" — only the set can.
    """
    _, payload, _ = run(["doctor"], capsys)
    runtime = payload["runtime"]
    for key in ("version", "executable", "settings_file", "models_dir", "source_checkout"):
        assert key in runtime, f"runtime identity is missing {key}"
    assert payload["next"], "doctor must always say what to do next"


def test_a_degraded_runtime_is_not_reported_as_simply_fine(capsys, tmp_sessions, monkeypatch):
    """`ok: true` must not be the whole story when the tunnel is on fallbacks.

    SAPI and Whisper both work, which is why they used to pass silently. Working is not the same
    as being the setup someone configured, and the gap between those two is where the incident
    lived.
    """
    monkeypatch.setenv("VOICE_TUNNEL_TTS", "sapi")
    monkeypatch.setenv("VOICE_TUNNEL_ASR", "whisper")
    _, payload, _ = run(["doctor"], capsys)

    assert "asr" in payload["degraded"], "whisper is a fallback and should say so"
    assert payload["next"], "a degraded runtime must carry a next step"
    if payload["degraded"]:
        assert "setup" in payload["next"], "the one-command fix should be named"


def test_doctor_fails_when_a_check_fails(capsys, tmp_sessions, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_TTS", "gibberish")
    code, payload, _ = run(["doctor"], capsys)
    assert code == cli.EXIT_ERROR
    assert "tts" in payload["failed"]


# ------------------------------------------------------------- no interactive


def test_no_command_reads_from_stdin(capsys, tmp_sessions, monkeypatch):
    """A prompt is a hang when the caller is an agent with no terminal. Reading stdin at all
    would be the bug, so make it explode rather than block."""
    def explode(*_a, **_k):
        raise AssertionError("the CLI must never prompt")

    monkeypatch.setattr("builtins.input", explode)
    for argv in (["describe"], ["doctor"], ["config", "show"], ["voices"]):
        cli.main(argv)
        capsys.readouterr()


def test_describe_reports_the_installed_version_not_a_literal():
    """`__version__` was a hardcoded string and drifted the first time it could: PyPI had 0.1.1
    while `describe` still said 0.1.0.

    `describe` is a contract an agent parses, so a wrong version there is worse than a missing
    one — it is a confident answer pointing at the wrong changelog. pyproject.toml is the single
    source (NFR3) and the release workflow already refuses a tag that disagrees with it.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    import voice_tunnel

    try:
        installed = pkg_version("voice-tunnel")
    except PackageNotFoundError:
        pytest.skip("not installed; nothing to compare against")

    assert voice_tunnel.__version__ == installed
    assert cli.DESCRIBE["version"] == installed
