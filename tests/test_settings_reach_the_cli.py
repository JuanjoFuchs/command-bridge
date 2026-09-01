"""The five variables the code read and the CLI had never heard of (spec 010, AC1-AC8).

`VOICE_TUNNEL_BARGE_IN`, `..._BARGE_IN_THRESHOLD`, `..._CONSONANT_BOOST`, `..._WATCH_MAX_S` and
`..._WATCH_DISCONNECTED_MAX_S` were live, honoured, and absent from `config.SETTINGS` — so
`config get` called each of them an unknown setting, `describe` listed none of them, and
`.env.example` documented none of them. The proof that this is a cost and not a tidiness
complaint is sitting in the owner's own `.env`: `VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S=540`,
hand-written with a paragraph of explanation, because editing the file was the only way to set a
value the tool refused to write.

**The test that carries the most weight here is AC7**, and it is not about discoverability at all.
A registry entry resolves a number and shows it to whoever asks what the tool is doing; the watch
reads its own. Those are two readers of one setting, and a registry that describes a value the
code does not use is worse than the silence it replaced — it is a confident wrong answer, in the
document people open precisely when they already suspect the code. So the assertions below compare
the registry's number to the RUNNING one rather than each to a literal.

Registration is deliberately not universal. `VOICE_TUNNEL_WAKE_BARE` stays out because nothing
reads it (see `test_wake.py`), and `VOICE_TUNNEL_HOME` stays out because it decides where the
settings file lives and so could never be read from inside it — `test_cli_surface.py` pins that
one's behaviour.
"""
import json
import os

import pytest

from command_bridge import cli, config

FIVE = (
    "VOICE_TUNNEL_BARGE_IN",
    "VOICE_TUNNEL_BARGE_IN_THRESHOLD",
    "VOICE_TUNNEL_CONSONANT_BOOST",
    "VOICE_TUNNEL_WATCH_MAX_S",
    "VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S",
)


def run(argv, capsys):
    """Invoke the CLI the way the shim does, and return (exit code, parsed stdout, stderr)."""
    code = cli.main(argv)
    out = capsys.readouterr()
    try:
        return code, json.loads(out.out or "{}"), out.err
    except json.JSONDecodeError:
        return code, None, out.err


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """A disposable settings file and none of the five set. Returns the file's path.

    Explicitly deleting them rather than trusting the environment to be clean: this suite runs on
    the machine that hosts the live tunnel, whose `.env` sets one of the five, and a default-value
    assertion that passes only when nobody is mid-conversation is not an assertion.
    """
    path = tmp_path / ".env"
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(path))
    for key in FIVE:
        monkeypatch.delenv(key, raising=False)
    return path


# ------------------------------------------------------ AC1: reachable through the CLI


@pytest.mark.parametrize("key", FIVE)
def test_config_get_answers_for_each_of_the_five(key, hermetic, capsys):
    """AC1. Driven through the CLI entry point, not by inspecting the SETTINGS tuple — the tuple
    being right is the mechanism; `config get` answering is the thing an agent actually hits."""
    code, payload, _ = run(["config", "get", key], capsys)

    assert code == cli.EXIT_OK, f"{key} still reads as an unknown setting"
    assert "error" not in payload
    assert payload["value"] != "", f"{key} resolved to nothing at all"
    assert payload["what"], f"{key} is registered with no description of what it does"


def test_a_key_the_code_does_not_read_stays_unknown(hermetic, capsys):
    """The inverse of AC1, and the reason `VOICE_TUNNEL_WAKE_BARE` was left out of the five.

    The per-name bare-wake opt-in was DELETED; the name survives in a docstring recording that and
    in a test that sets it to prove it inert. Registering it would publish a knob that cannot be
    honoured — strictly worse than an unregistered one, because a documented setting that silently
    does nothing is harder to recognise as broken than an undocumented one.
    """
    code, payload, _ = run(["config", "get", "VOICE_TUNNEL_WAKE_BARE"], capsys)

    assert code == cli.EXIT_USAGE
    assert payload["code"] == "invalid_input"


# ---------------------------------------------------- AC2/AC3/AC4: the three consumers


def test_describe_lists_all_five(hermetic):
    """AC2. `describe`'s env block is generated from SETTINGS, so this is really an assertion that
    the registration reached the contract an agent reads instead of the README."""
    documented = set(cli.DESCRIBE["env"])
    assert set(FIVE) <= documented, f"missing from describe.env: {sorted(set(FIVE) - documented)}"


def test_config_show_reports_all_five_as_defaults_in_a_clean_environment(hermetic, capsys):
    """AC3. `source` is the field that makes `config show` worth reading — a value being wrong is
    easy, a value being wrong because a stale process env shadows the file you just edited is the
    one that costs an hour."""
    code, payload, _ = run(["config", "show"], capsys)

    assert code == cli.EXIT_OK
    by_key = {row["key"]: row for row in payload["settings"]}
    for key in FIVE:
        assert key in by_key, f"{key} is absent from `config show`"
        assert by_key[key]["source"] == "default", f"{key} is not reading as a default"


def test_env_example_documents_all_five():
    """AC4, named. `test_config_file.py::test_env_example_documents_every_setting` is the general
    drift check and passes unmodified; this one names the five, so deleting a block from
    `.env.example` fails with the key in the message rather than as a diff in a list."""
    with open(os.path.join(config.ROOT, ".env.example"), encoding="utf-8") as fh:
        text = fh.read()
    missing = [key for key in FIVE if key not in text]
    assert not missing, f"add these to .env.example: {missing}"


# ------------------------------------------------------------------ AC5: round trip

ROUND_TRIP = [
    ("VOICE_TUNNEL_BARGE_IN", "0", "0"),
    ("VOICE_TUNNEL_BARGE_IN_THRESHOLD", "0.25", 0.25),
    ("VOICE_TUNNEL_CONSONANT_BOOST", "0.4", 0.4),
    ("VOICE_TUNNEL_WATCH_MAX_S", "300", 300.0),
    ("VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S", "540", 540.0),
]


@pytest.mark.parametrize("key,written,expected", ROUND_TRIP)
def test_config_set_then_get_round_trips_each_of_the_five(
    key, written, expected, hermetic, capsys, monkeypatch
):
    """AC5, through the CLI both ways. Writing and reading are separate code paths — the writer
    validates the key against this tool's namespace, the reader looks it up in the registry — and
    a setting is only settable when both agree about it."""
    code, result, _ = run(["config", "set", key, written], capsys)
    assert code == cli.EXIT_OK and result["created"] is True

    monkeypatch.delenv(key, raising=False)
    code, got, _ = run(["config", "get", key], capsys)

    assert code == cli.EXIT_OK and "error" not in got
    assert got["source"] == "file", "the value was written and then not read back from the file"
    if isinstance(expected, str):
        assert got["value"] == expected
    else:
        assert float(got["value"]) == pytest.approx(expected)


# -------------------------------------------------- AC6: nothing set means the default
#
# NFR1: this spec makes five settings visible and must not retune one of them. Every assertion
# below is against the module constant rather than a number typed here, so the test states
# "unchanged" rather than restating the value and freezing a second copy of it.


def test_nothing_set_resolves_to_the_module_default(hermetic):
    """AC6, all five at once, because the property is the same property five times."""
    assert config.barge_in_enabled() is config.BARGE_IN
    assert config.barge_in_threshold() == config.BARGE_IN_THRESHOLD
    assert config.consonant_boost() == config.CONSONANT_BOOST
    assert config.watch_backoff_max_s() == cli.WATCH_BACKOFF_MAX_S
    assert config.watch_disconnected_max_s() == cli.WATCH_DISCONNECTED_MAX_S


def test_the_defaults_the_registry_publishes_are_the_ones_the_code_ships(hermetic, capsys):
    """AC6 again, one layer out: the numbers `config show` prints with source `default` must be
    the module constants. A resolver that quietly substituted its own default would satisfy the
    assertion above and still lie to every reader of `config show`."""
    _, payload, _ = run(["config", "show"], capsys)
    by_key = {row["key"]: row["value"] for row in payload["settings"]}

    assert by_key["VOICE_TUNNEL_BARGE_IN"] == ("1" if config.BARGE_IN else "0")
    assert float(by_key["VOICE_TUNNEL_BARGE_IN_THRESHOLD"]) == config.BARGE_IN_THRESHOLD
    assert float(by_key["VOICE_TUNNEL_CONSONANT_BOOST"]) == config.CONSONANT_BOOST
    assert float(by_key["VOICE_TUNNEL_WATCH_MAX_S"]) == cli.WATCH_BACKOFF_MAX_S
    assert float(by_key["VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S"]) == cli.WATCH_DISCONNECTED_MAX_S


# ------------------------------- AC7: one number, two readers, and they must not diverge


def test_the_registry_reports_the_disconnected_ceiling_the_watch_actually_waits(hermetic, monkeypatch):
    """AC7, at the value that is live in the owner's `.env` right now.

    540 rather than an invented number on purpose: this is the setting whose only route in was
    hand-editing the file, so the first thing registration must not do is describe it wrong. The
    two sides are asserted EQUAL TO EACH OTHER before either is compared to 540 — a pair of
    literals would pass just as happily with a resolver that reads nothing at all.
    """
    monkeypatch.setenv("VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S", "540")

    published = config.watch_disconnected_max_s()
    actual = cli._disconnected_ceiling()

    assert published == actual, "the registry publishes a ceiling the watch does not use"
    assert published == 540.0
    assert actual != cli.WATCH_DISCONNECTED_MAX_S, (
        "the override did nothing, so this proved only that two defaults agree"
    )


def test_the_registry_reports_the_backoff_cap_the_ladder_actually_tops_out_at(hermetic, monkeypatch):
    """AC7's other half, and the stronger form of it: the comparison is not against another
    resolver but against the LADDER'S OWN OUTPUT at a streak nothing can exceed.

    `_backoff_ceiling` is what `cmd_watch` calls; if the registry's number and the ladder's last
    rung ever differ, `config show` is describing a wait that does not happen.
    """
    monkeypatch.setenv("VOICE_TUNNEL_WATCH_MAX_S", "300")

    published = config.watch_backoff_max_s()
    ladder_top = cli._backoff_ceiling(cli.WATCH_BASE_S, 99, reachable=True)

    assert published == ladder_top
    assert published == 300.0
    assert published != cli.WATCH_BACKOFF_MAX_S, "the override did nothing"


def test_an_override_moves_the_whole_published_ladder(hermetic, monkeypatch, capsys):
    """The end-to-end version, through the CLI: set the cap, and both the value `config get`
    returns and the last rung `describe` publishes move together. This is the failure the
    delegation exists to prevent — `_human_seconds` exists because three hand-written copies of
    this one cap drifted three different ways."""
    monkeypatch.setenv("VOICE_TUNNEL_WATCH_MAX_S", "120")

    _, got, _ = run(["config", "get", "VOICE_TUNNEL_WATCH_MAX_S"], capsys)

    assert float(got["value"]) == 120.0
    assert cli._backoff_ladder(cli.WATCH_BASE_S)[-1] == 120.0


# ---------------------------------------------- AC8: the fallbacks the backoff already had
#
# `tests/test_watch_backoff.py` owns the statement that behaviour is unchanged and passes
# unmodified. What is added here is the same tolerance asserted of the REGISTRY's reader, because
# `config show` calls every resolver at once — one of them raising on a hand-edited value takes
# out the command that exists to tell you the file is wrong.


@pytest.mark.parametrize("garbage", ["not a number", "9min", " "])
def test_a_hand_edited_ceiling_falls_back_instead_of_raising(garbage, hermetic, monkeypatch):
    """A settings file is hand-edited, so every resolver has to survive nonsense in it."""
    monkeypatch.setenv("VOICE_TUNNEL_WATCH_MAX_S", garbage)
    monkeypatch.setenv("VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S", garbage)

    assert config.watch_backoff_max_s() == cli.WATCH_BACKOFF_MAX_S
    assert config.watch_disconnected_max_s() == cli.WATCH_DISCONNECTED_MAX_S
    assert cli._backoff_ceiling(30.0, 99, reachable=True) == cli.WATCH_BACKOFF_MAX_S


def test_every_resolver_survives_a_settings_file_full_of_nonsense(hermetic, capsys):
    """`config show` is the command you run when something is already wrong. It calls all five new
    resolvers in one pass, so it must not be the thing that breaks."""
    hermetic.write_text(
        "VOICE_TUNNEL_BARGE_IN=maybe\n"
        "VOICE_TUNNEL_BARGE_IN_THRESHOLD=loud\n"
        "VOICE_TUNNEL_CONSONANT_BOOST=lots\n"
        "VOICE_TUNNEL_WATCH_MAX_S=nine minutes\n"
        "VOICE_TUNNEL_WATCH_DISCONNECTED_MAX_S=overnight\n",
        encoding="utf-8",
    )

    code, payload, _ = run(["config", "show"], capsys)

    assert code == cli.EXIT_OK
    assert {row["key"] for row in payload["settings"]} >= set(FIVE)
