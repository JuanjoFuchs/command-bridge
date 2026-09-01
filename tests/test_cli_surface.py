"""The agent-facing surface: `describe` as the contract, exit codes, and remedial errors.

`describe` is the live source of truth (AGENTS.md convention 3), which only holds if something
fails when it drifts. That is what the first test here is for — the previous drift was silent and
cost an agent a session's worth of guessing at env vars that were never written down.
"""
import argparse
import json

import pytest

from command_bridge import cli, config, lanes


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


def test_describe_documents_every_flag_the_parser_accepts():
    """One level deeper than the command check, and found the same way it was.

    Two audits used `--help` per subcommand to discover flags `describe` never mentioned:
    `say --now`, `say --voice`, `watch --force`. `describe` calls itself the contract, and an
    agent that trusts an `args` block and finds it short has been told something false about the
    tool's capabilities — `--now` in particular is the difference between interrupting a reply and
    queueing behind it, and `describe`'s own watchdog prompt uses it.
    """
    import argparse as _ap

    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, _ap._SubParsersAction))
    missing = {}
    for name, p in sub.choices.items():
        documented = set(cli.DESCRIBE["commands"].get(name, {}).get("args") or ())
        for action in p._actions:
            if isinstance(action, _ap._HelpAction) or not action.option_strings:
                continue
            # The long form is what gets documented; `--session` is on nearly every command and
            # is documented per-command already.
            longest = max(action.option_strings, key=len)
            if longest not in documented:
                missing.setdefault(name, []).append(longest)
    assert not missing, f"undocumented flags in DESCRIBE['commands']: {missing}"


def test_describe_documents_every_setting_the_code_reads():
    assert set(cli.DESCRIBE["env"]) == {s["key"] for s in config.SETTINGS}


def test_the_variables_that_cannot_be_settings_are_still_documented():
    """COMMAND_BRIDGE_HOME scopes the settings file, the models and the sessions, so it cannot live
    in the file it locates — and it was therefore in no list at all. An audit ran a whole session
    inside it, found `config get` calling it an "unknown setting", and had to reconstruct what it
    did from one line of `doctor.runtime.isolate_with`.
    """
    process_only = cli.DESCRIBE["env_process_only"]
    assert "COMMAND_BRIDGE_HOME" in process_only
    assert set(process_only).isdisjoint({s["key"] for s in config.SETTINGS}), (
        "a variable cannot be both persistable and process-only"
    )


def test_describe_carries_exit_codes_and_the_error_shape():
    """An agent branches on the exit code before it parses anything, so the codes have to be
    part of the published contract, not folklore."""
    assert set(cli.DESCRIBE["exit_codes"]) == {"0", "1", "2", "3"}
    assert set(cli.DESCRIBE["errors"]) == {"error", "code", "remedy"}


def test_describe_registers_every_lane_error_the_registry_actually_raises():
    """Bind the published registry to the code that raises, not to a literal list.

    Spec 012 shipped `unknown_lane` and `lane_exists` as real refusals and documented neither, so
    `describe` — the thing an agent reads to learn the surface — was silent on both. A literal
    assertion would have gone stale the same way; this drives the real registry and fails if a
    code it raises is missing from the contract.
    """
    registry = lanes.LaneRegistry("claude")
    raised = set()
    for bad in ["everyone", "two words", "x"]:
        with pytest.raises(lanes.LaneError) as caught:
            registry.add(bad)
        raised.add(caught.value.code)
    registry.add("codex")
    with pytest.raises(lanes.LaneError) as caught:
        registry.add("codex")
    raised.add(caught.value.code)
    with pytest.raises(lanes.LaneError) as caught:
        registry.require("grok")
    raised.add(caught.value.code)

    assert raised == {"lane_exists", "unknown_lane"}
    undocumented = raised - set(cli.DESCRIBE["error_codes"])
    assert not undocumented, f"raised but absent from describe.error_codes: {sorted(undocumented)}"


def test_describe_carries_the_lane_field_the_turn_log_now_writes():
    """`lane` is the field that decides WHICH agent a turn reaches, so an agent that reads the
    turn schema and never sees it cannot know its watch is filtered. The absent-means-default
    rule is the load-bearing half: every turn logged before lanes existed has no `lane` key."""
    lane_doc = cli.DESCRIBE["turn_schema"]["lane"]
    assert "ABSENT MEANS THE DEFAULT LANE" in lane_doc


def test_describe_carries_the_two_things_013_added_to_the_agent_contract():
    """013 AC-8 — the refusal an agent must handle, and the number that ends its fold loop.

    Both are things an agent learns ONLY from `describe`: `no_lane` is a refusal it will meet the
    first time a second agent joins, and `unanswered_s` is the bound on a loop the same contract
    tells it to run. A contract that states the loop and omits its exit is the defect 013 FR8 was
    written for.
    """
    assert "no_lane" in cli.DESCRIBE["error_codes"]
    watch_returns = cli.DESCRIBE["commands"]["watch"]["returns"]
    assert "unanswered_s" in watch_returns
    assert "STOP FOLDING AND ANSWER" in watch_returns["unanswered_s"]


def test_describe_tells_the_caller_how_to_invoke_it():
    """The failure this documents: an agent fell back to
    `python -c "import sys; sys.path.insert(...)"` with four env-var prefixes, because nothing
    in the contract said there was a shim or a settings file."""
    invocation = cli.DESCRIBE["invocation"]
    assert "python -c" in invocation["no_python_dash_c"]
    assert "command-bridge config set" in cli.DESCRIBE["config_file"]["write_it_with"]


def test_describe_exits_zero_and_is_json(capsys):
    code, payload, _ = run(["describe"], capsys)
    assert code == cli.EXIT_OK
    assert payload["tool"] == "command-bridge"


# --------------------------------------------------------------- config command


def test_config_show_reports_the_source_of_every_value(capsys, tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("COMMAND_BRIDGE_OWNER=from-file\n", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.delenv("COMMAND_BRIDGE_OWNER", raising=False)
    monkeypatch.setenv("COMMAND_BRIDGE_TTS", "none")

    code, payload, _ = run(["config", "show"], capsys)

    assert code == cli.EXIT_OK
    by_key = {row["key"]: row for row in payload["settings"]}
    assert by_key["COMMAND_BRIDGE_OWNER"]["source"] == "file"
    assert by_key["COMMAND_BRIDGE_TTS"]["source"] == "env"
    assert by_key["COMMAND_BRIDGE_ASR_THREADS"]["source"] == "default"


def test_config_show_redacts_a_secret_but_get_reveals_it(capsys, monkeypatch, tmp_path):
    """A bulk dump lands in a transcript and an agent's context; an explicit single-key read is
    somebody actually asking for that value."""
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("COMMAND_BRIDGE_TOKEN", "s3cret-token")

    _, shown, _ = run(["config", "show"], capsys)
    token_row = [r for r in shown["settings"] if r["key"] == "COMMAND_BRIDGE_TOKEN"][0]
    assert token_row["value"] == config.REDACTED

    _, got, _ = run(["config", "get", "COMMAND_BRIDGE_TOKEN"], capsys)
    assert got["value"] == "s3cret-token"


def test_config_set_then_get_round_trips(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("COMMAND_BRIDGE_TTS", raising=False)

    code, written, _ = run(["config", "set", "COMMAND_BRIDGE_TTS", "none"], capsys)
    assert code == cli.EXIT_OK and written["created"] is True

    monkeypatch.delenv("COMMAND_BRIDGE_TTS", raising=False)
    _, got, _ = run(["config", "get", "COMMAND_BRIDGE_TTS"], capsys)
    assert got == {"key": "COMMAND_BRIDGE_TTS", "value": "none", "source": "file",
                   "what": got["what"]}


def test_config_set_warns_when_the_environment_will_shadow_the_write(capsys, tmp_path, monkeypatch):
    """Otherwise you set a value, watch the old one keep applying, and go looking in the code."""
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("COMMAND_BRIDGE_TTS", "sapi")

    _, payload, _ = run(["config", "set", "COMMAND_BRIDGE_TTS", "piper"], capsys)

    assert payload["shadowed_by_env"] is True
    assert "wins over the file" in payload["note"]


def test_config_get_on_an_unknown_key_names_the_remedy(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / "absent.env"))

    code, payload, _ = run(["config", "get", "COMMAND_BRIDGE_NOPE"], capsys)

    # EXIT_USAGE, not EXIT_ERROR. The next test asserts `config set` of a foreign key exits 2 with
    # this same `invalid_input` code; these two exited 1 and 2 for the identical class of mistake,
    # purely because one was raised and the other returned. An audit found the pair and had no way
    # to tell which document was wrong.
    assert code == cli.EXIT_USAGE
    assert payload["code"] == "invalid_input"
    assert "command-bridge config show" in payload["remedy"]


def test_the_same_code_always_means_the_same_exit_status(capsys, tmp_path, monkeypatch):
    """Raised and returned rejections must not disagree — a caller branches on one or the other,
    never both."""
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / ".env"))

    returned, payload, _ = run(["config", "get", "COMMAND_BRIDGE_NOPE"], capsys)
    raised, _, err = run(["wake", "--name", "two words", "--no-save"], capsys)

    assert payload["code"] == "invalid_input"
    assert "invalid_input" in err
    assert returned == raised == cli.EXIT_USAGE


def test_a_process_only_variable_is_explained_rather_than_disowned(capsys, tmp_path, monkeypatch):
    """`config get COMMAND_BRIDGE_HOME` answered "unknown setting" about the variable that decides
    where the settings file `config` reads actually lives."""
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / ".env"))

    code, payload, _ = run(["config", "get", "COMMAND_BRIDGE_HOME"], capsys)

    assert code == cli.EXIT_OK
    assert "error" not in payload
    assert payload["what"] and payload["note"], "say what it does and why it cannot be persisted"


def test_config_set_of_a_foreign_key_exits_usage_with_a_remedy(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(tmp_path / ".env"))

    code, _, err = run(["config", "set", "PATH", "/tmp"], capsys)

    assert code == cli.EXIT_USAGE
    payload = json.loads(err)
    assert payload["code"] == "invalid_input"
    assert "COMMAND_BRIDGE_" in payload["error"]


# --------------------------------------------------------------- exit codes


def test_a_command_needing_a_server_exits_three_not_one(capsys, tmp_sessions):
    """`no server` and `the request failed` call for different next moves — start one versus
    rephrase — so collapsing both into 1 forced the caller to string-match the message."""
    code, payload, _ = run(["status", "--session", "nothing-here"], capsys)

    assert code == cli.EXIT_NO_SERVER
    assert payload["code"] == "no_server"
    assert "command-bridge serve" in payload["remedy"]


def test_the_no_server_remedy_names_the_watch_that_must_follow(capsys, tmp_sessions):
    """AGENTS.md's rule 1: an agent told only "no server" starts one and then forgets to go
    straight back into the blocking wait, which from the user's side is a crash.

    Names `watch`, which is THE waiting command: spec 005 made it smart enough that `drain` was
    unnecessary rather than renaming it. The tool must never TEACH a spelling it is retiring —
    that is how `--waits 5,3,2` outlived the ladder it configured."""
    _, payload, _ = run(["say", "--session", "ghost", "hello"], capsys)

    assert "command-bridge watch" in payload["remedy"]


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
        assert check["status"] in {"ok", "info", "degraded", "failed"}
        if check["status"] == "ok":
            assert check["remedy"] is None, f"{check['name']} is fine; nothing to suggest"
        else:
            # `info` included deliberately. It means "nothing is wrong", not "nothing to say" —
            # it exists for facts worth surfacing that the reader may still want to act on, and a
            # note with no suggested action is the kind of line people learn to skip.
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
    monkeypatch.setenv("COMMAND_BRIDGE_TTS", "sapi")
    monkeypatch.setenv("COMMAND_BRIDGE_ASR", "whisper")
    _, payload, _ = run(["doctor"], capsys)

    assert "asr" in payload["degraded"], "whisper is a fallback and should say so"
    assert payload["next"], "a degraded runtime must carry a next step"
    if payload["degraded"]:
        assert "setup" in payload["next"], "the one-command fix should be named"


def test_doctor_fails_when_a_check_fails(capsys, tmp_sessions, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_TTS", "gibberish")
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

    import command_bridge

    try:
        installed = pkg_version("command-bridge")
    except PackageNotFoundError:
        pytest.skip("not installed; nothing to compare against")

    assert command_bridge.__version__ == installed
    assert cli.DESCRIBE["version"] == installed
