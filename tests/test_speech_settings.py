"""Speech speed and sentence pause: the unit they are expressed in, and that they survive a restart.

Two defects live here, both already paid for once:

  * **The inverted unit.** Piper's `length_scale` means "duration", so lower is faster. Exposing
    it produced half speed when JJ asked for 2.0. Every layer above the piper call now speaks
    SPEED, and these tests pin that — including the one conversion, which is the only place the
    inversion is allowed to exist.
  * **Settings that lived only in a running process.** Speed and pause are tuned by ear during a
    live conversation, and before this they were lost on every restart, so the value that was
    right had to be rediscovered each session.
"""
import json

import pytest

from voice_tunnel import cli, config


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
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(path))
    for key in ("VOICE_TUNNEL_SPEECH_SPEED", "VOICE_TUNNEL_SENTENCE_PAUSE", "VOICE_TUNNEL_PIPER_LENGTH_SCALE"):
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


# ------------------------------------------------------------- reading settings


def test_speed_and_pause_come_from_the_settings_file(env_file, monkeypatch):
    env_file.write_text("VOICE_TUNNEL_SPEECH_SPEED=1.4\nVOICE_TUNNEL_SENTENCE_PAUSE=0.3\n", encoding="utf-8")
    config.load_env_file()

    assert config.speech_speed() == pytest.approx(1.4)
    assert config.sentence_pause() == pytest.approx(0.3)


def test_a_retired_length_scale_is_still_honoured_converted(env_file, monkeypatch):
    """Someone's .env may predate the unit change; silently reverting them to default speed
    would be a worse outcome than reading the old key once."""
    monkeypatch.setenv("VOICE_TUNNEL_PIPER_LENGTH_SCALE", "0.5")

    assert config.speech_speed() == pytest.approx(2.0)


def test_an_explicit_speed_wins_over_the_retired_key(env_file, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_PIPER_LENGTH_SCALE", "0.5")
    monkeypatch.setenv("VOICE_TUNNEL_SPEECH_SPEED", "1.1")

    assert config.speech_speed() == pytest.approx(1.1)


def test_a_garbage_value_falls_back_to_the_default_rather_than_crashing(env_file, monkeypatch):
    """A hand-edited file must never be able to stop the tunnel from speaking."""
    monkeypatch.setenv("VOICE_TUNNEL_SPEECH_SPEED", "fast please")
    monkeypatch.setenv("VOICE_TUNNEL_SENTENCE_PAUSE", "")

    assert config.speech_speed() == pytest.approx(config.SPEECH_SPEED)
    assert config.sentence_pause() == pytest.approx(config.SENTENCE_SILENCE_S)


def test_an_out_of_range_persisted_value_is_clamped_on_read(env_file, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_SPEECH_SPEED", "9000")
    monkeypatch.setenv("VOICE_TUNNEL_SENTENCE_PAUSE", "60")

    assert config.speech_speed() == config.SPEED_MAX
    assert config.sentence_pause() == config.PAUSE_MAX


# ------------------------------------------------------------------ `voice-tunnel rate`


def test_rate_persists_by_default_so_it_survives_a_restart(env_file, capsys, tmp_sessions):
    """The actual request: 'we need to persist the speed and pause parameters across restarts.'"""
    code, payload, _ = run(["rate", "--session", "nobody", "--speed", "1.35", "--pause", "0.4"],
                           capsys)

    assert code == cli.EXIT_OK
    assert payload["persisted"] == {"VOICE_TUNNEL_SPEECH_SPEED": "1.35", "VOICE_TUNNEL_SENTENCE_PAUSE": "0.4"}

    written = config.read_env_file(str(env_file))
    assert written["VOICE_TUNNEL_SPEECH_SPEED"] == "1.35"
    assert written["VOICE_TUNNEL_SENTENCE_PAUSE"] == "0.4"


def test_rate_saves_even_when_no_server_is_running(env_file, capsys, tmp_sessions):
    """'set it now, start the tunnel next' is normal. Failing the command over a missing server
    would throw away the setting — and this exits 0, because nothing actually failed."""
    code, payload, _ = run(["rate", "--session", "nobody", "--speed", "1.5"], capsys)

    assert code == cli.EXIT_OK
    assert payload["applied_live"] is False
    assert "voice-tunnel serve" in payload["note"]
    assert config.read_env_file(str(env_file))["VOICE_TUNNEL_SPEECH_SPEED"] == "1.5"


def test_no_save_leaves_the_file_untouched(env_file, capsys, tmp_sessions):
    code, payload, _ = run(["rate", "--session", "nobody", "--speed", "2.0", "--no-save"], capsys)

    assert code == cli.EXIT_OK
    assert payload["persisted"] is None
    assert "VOICE_TUNNEL_SPEECH_SPEED" not in config.read_env_file(str(env_file))


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
    assert written == {"VOICE_TUNNEL_SENTENCE_PAUSE": "0.7", "VOICE_TUNNEL_SPEECH_SPEED": "1.25"}
