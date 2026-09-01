"""Speech speed and sentence pause: the unit they are expressed in, and that they survive a restart.

Two defects live here, both already paid for once:

  * **The inverted unit.** Piper's `length_scale` means "duration", so lower is faster. Exposing
    it produced half speed when the owner asked for 2.0. Every layer above the piper call now speaks
    SPEED, and these tests pin that — including the one conversion, which is the only place the
    inversion is allowed to exist.
  * **Settings that lived only in a running process.** Speed and pause are tuned by ear during a
    live conversation, and before this they were lost on every restart, so the value that was
    right had to be rediscovered each session.
"""
import json

import pytest

from command_bridge import cli, config


def run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr()
    try:
        return code, json.loads(out.out or "{}"), out.err
    except json.JSONDecodeError:
        return code, None, out.err


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """A disposable settings file. A suite that reads the developer's real .env is not a suite."""
    path = tmp_path / ".env"
    monkeypatch.setenv("COMMAND_BRIDGE_ENV_FILE", str(path))
    for key in ("COMMAND_BRIDGE_SPEECH_SPEED", "COMMAND_BRIDGE_SENTENCE_PAUSE", "COMMAND_BRIDGE_PIPER_LENGTH_SCALE"):
        monkeypatch.delenv(key, raising=False)
    return path


# ------------------------------------------------------------------ the unit


def test_speed_is_a_multiple_and_higher_means_faster():
    """The regression this exists for: 2.0 must mean twice as fast, not half."""
    assert config.length_scale_for(2.0) < config.length_scale_for(1.0)
    assert config.length_scale_for(2.0) == pytest.approx(0.5)


def test_the_default_speed_matches_the_length_scale_it_replaced():
    """0.85 was tuned by ear for en_GB-alan-medium. Changing the unit must not change the voice."""
    assert config.length_scale_for(config.SPEECH_SPEED) == pytest.approx(0.85, abs=0.005)


def test_a_speed_outside_the_range_is_clamped_not_divided_by_zero():
    """0 would be a ZeroDivisionError on the way to length_scale — in the middle of speaking."""
    assert config.length_scale_for(0.0) == pytest.approx(1.0 / config.SPEED_MIN)
    assert config.length_scale_for(99.0) == pytest.approx(1.0 / config.SPEED_MAX)


# ------------------------------------------------- kokoro's ceiling is not the project's


def test_kokoro_has_a_lower_ceiling_than_the_setting_allows():
    """THE LATENT CRASH. `kokoro_onnx.create` opens with `assert speed <= 2.0`, while this tool
    accepts and persists up to 2.5 — so a value `command-bridge rate --speed 2.5` writes without
    complaint makes every Kokoro reply raise an AssertionError from inside the package.

    The relationship is what matters, not the two numbers: the moment they are equal this test is
    pointless, and the moment SPEED_MAX drops below 2.0 someone has 'fixed' it the wrong way."""
    assert config.KOKORO_SPEED_MAX < config.SPEED_MAX


def test_a_speed_kokoro_would_refuse_is_clamped_to_its_ceiling():
    assert config.kokoro_speed(2.5) == config.KOKORO_SPEED_MAX
    assert config.kokoro_speed(99.0) == config.KOKORO_SPEED_MAX
    assert config.kokoro_speed(0.0) == config.SPEED_MIN


def test_a_speed_kokoro_accepts_is_passed_through_untouched():
    """Clamping must not become rounding. The owner's live setting is 1.2 and every value up to
    the ceiling has to reach the model exactly as configured."""
    for speed in (0.5, 1.0, 1.18, 1.2, 1.9, 2.0):
        assert config.kokoro_speed(speed) == pytest.approx(speed)


def test_piper_keeps_the_full_range_kokoro_cannot_take(monkeypatch):
    """The reason the clamp lives at the kokoro boundary instead of in SPEED_MAX: piper handles
    2.5, and the owner uses high speeds deliberately. Lowering the global maximum would take a
    working setting away from one backend to accommodate the other — so the persisted value must
    still read back at 2.5 and still reach piper as 1/2.5."""
    monkeypatch.setenv("COMMAND_BRIDGE_SPEECH_SPEED", "2.5")
    assert config.speech_speed() == pytest.approx(2.5)
    assert config.length_scale_for(config.speech_speed()) == pytest.approx(1.0 / 2.5)


# ------------------------------------------------------------- reading settings


def test_speed_and_pause_come_from_the_settings_file(env_file, monkeypatch):
    env_file.write_text("COMMAND_BRIDGE_SPEECH_SPEED=1.4\nCOMMAND_BRIDGE_SENTENCE_PAUSE=0.3\n", encoding="utf-8")
    config.load_env_file()

    assert config.speech_speed() == pytest.approx(1.4)
    assert config.sentence_pause() == pytest.approx(0.3)


def test_a_retired_length_scale_is_still_honoured_converted(env_file, monkeypatch):
    """Someone's .env may predate the unit change; silently reverting them to default speed
    would be a worse outcome than reading the old key once."""
    monkeypatch.setenv("COMMAND_BRIDGE_PIPER_LENGTH_SCALE", "0.5")

    assert config.speech_speed() == pytest.approx(2.0)


def test_an_explicit_speed_wins_over_the_retired_key(env_file, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_PIPER_LENGTH_SCALE", "0.5")
    monkeypatch.setenv("COMMAND_BRIDGE_SPEECH_SPEED", "1.1")

    assert config.speech_speed() == pytest.approx(1.1)


def test_a_garbage_value_falls_back_to_the_default_rather_than_crashing(env_file, monkeypatch):
    """A hand-edited file must never be able to stop the tunnel from speaking."""
    monkeypatch.setenv("COMMAND_BRIDGE_SPEECH_SPEED", "fast please")
    monkeypatch.setenv("COMMAND_BRIDGE_SENTENCE_PAUSE", "")

    assert config.speech_speed() == pytest.approx(config.SPEECH_SPEED)
    assert config.sentence_pause() == pytest.approx(config.SENTENCE_SILENCE_S)


def test_an_out_of_range_persisted_value_is_clamped_on_read(env_file, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_SPEECH_SPEED", "9000")
    monkeypatch.setenv("COMMAND_BRIDGE_SENTENCE_PAUSE", "60")

    assert config.speech_speed() == config.SPEED_MAX
    assert config.sentence_pause() == config.PAUSE_MAX


# ------------------------------------------------------------------ `command-bridge rate`


def test_rate_persists_by_default_so_it_survives_a_restart(env_file, capsys, tmp_sessions):
    """The actual request: 'we need to persist the speed and pause parameters across restarts.'"""
    code, payload, _ = run(["rate", "--session", "nobody", "--speed", "1.35", "--pause", "0.4"],
                           capsys)

    assert code == cli.EXIT_OK
    assert payload["persisted"] == {"COMMAND_BRIDGE_SPEECH_SPEED": "1.35", "COMMAND_BRIDGE_SENTENCE_PAUSE": "0.4"}

    written = config.read_env_file(str(env_file))
    assert written["COMMAND_BRIDGE_SPEECH_SPEED"] == "1.35"
    assert written["COMMAND_BRIDGE_SENTENCE_PAUSE"] == "0.4"


def test_rate_saves_even_when_no_server_is_running(env_file, capsys, tmp_sessions):
    """'set it now, start the tunnel next' is normal. Failing the command over a missing server
    would throw away the setting — and this exits 0, because nothing actually failed."""
    code, payload, _ = run(["rate", "--session", "nobody", "--speed", "1.5"], capsys)

    assert code == cli.EXIT_OK
    assert payload["applied_live"] is False
    assert "command-bridge serve" in payload["note"]
    assert config.read_env_file(str(env_file))["COMMAND_BRIDGE_SPEECH_SPEED"] == "1.5"


def test_no_save_leaves_the_file_untouched(env_file, capsys, tmp_sessions):
    code, payload, _ = run(["rate", "--session", "nobody", "--speed", "2.0", "--no-save"], capsys)

    assert code == cli.EXIT_OK
    assert payload["persisted"] is None
    assert "COMMAND_BRIDGE_SPEECH_SPEED" not in config.read_env_file(str(env_file))


def test_rate_with_no_arguments_reports_rather_than_writing(env_file, capsys, tmp_sessions):
    code, payload, _ = run(["rate", "--session", "nobody"], capsys)

    assert code == cli.EXIT_OK
    assert payload["persisted"]["speed"] == pytest.approx(config.speech_speed())
    assert not env_file.exists()


@pytest.mark.parametrize("flag,value", [("--speed", "0.1"), ("--speed", "5"),
                                        ("--pause", "-1"), ("--pause", "9")])
def test_an_out_of_range_argument_is_refused_before_it_reaches_the_file(
    flag, value, env_file, capsys, tmp_sessions
):
    """Validated CLI-side, not only server-side: with no server there is nothing to reject it,
    and a nonsense number would be written and then silently clamped forever after."""
    code, _, err = run(["rate", "--session", "nobody", flag, value], capsys)

    assert code == cli.EXIT_USAGE
    assert json.loads(err)["code"] == "invalid_input"
    assert not env_file.exists()


def test_only_the_named_setting_is_written(env_file, capsys, tmp_sessions):
    """Changing speed must not stamp a pause the user never asked for over their tuned value."""
    run(["rate", "--session", "nobody", "--pause", "0.7"], capsys)
    run(["rate", "--session", "nobody", "--speed", "1.25"], capsys)

    written = config.read_env_file(str(env_file))
    assert written == {"COMMAND_BRIDGE_SENTENCE_PAUSE": "0.7", "COMMAND_BRIDGE_SPEECH_SPEED": "1.25"}
