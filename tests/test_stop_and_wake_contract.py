"""Two gaps a cold-start audit had to work around rather than report.

  * The tool could START a detached server and could not stop one. `describe` tells you to run
    `serve` in the background and never says how it ends, so the auditor kept the OS handle from
    `Start-Process` and killed the PID itself — which works exactly once, in the session that
    launched it, and not at all in a new one. `status` reported thirty-odd fields and not the pid,
    though the pid has been written to the runtime file since the beginning.
  * `wake` in read mode returned a different shape than `describe` documents for it, with no
    `wake` key at all, and reported `persisted: {"name": "assistant"}` while `config path` said
    that settings file did not exist. A default presented as a saved value is how somebody
    concludes a setting is already applied and stops looking at it.
"""
import json
import os

import pytest

from tests.test_cli_surface import run
from voice_tunnel import cli


@pytest.fixture()
def no_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / "settings.env"))
    monkeypatch.delenv("VOICE_TUNNEL_WAKE_NAME", raising=False)
    return monkeypatch


# ------------------------------------------------------------------------ stop


def test_stop_is_documented_and_dispatchable():
    """It was absent from `describe`'s command table AND from `--help`, so there was no path to
    discovering it short of reading source."""
    import argparse

    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert "stop" in sub.choices, "`stop` must be a real command"
    assert "stop" in cli.DESCRIBE["commands"], "and it must be documented"


def test_stopping_nothing_is_not_an_error(capsys, tmp_sessions):
    """Called against a session that was never served. An agent recovering from an unknown state
    should be able to run this without having to check first."""
    _, payload, _ = run(["stop", "--session", "dev"], capsys)
    assert payload["stopped"] is False
    assert payload["reason"] == "no_runtime_file"


def test_stop_asks_the_server_before_signalling_it(capsys, tmp_sessions, monkeypatch):
    """Order matters: an orderly shutdown flushes the turn log, and the log is the only durable
    artifact a conversation leaves behind."""
    cli.write_runtime("dev", "127.0.0.1", 1, "t")
    calls = []
    monkeypatch.setattr(cli, "_request",
                        lambda s, path, body=None: calls.append(path) or {"stopping": True})
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda *a: killed.append(a))

    _, payload, _ = run(["stop", "--session", "dev"], capsys)

    assert calls == ["/shutdown"], "it must ask first"
    assert not killed, "and must not signal a server that answered"
    assert payload["stopped"] and payload["how"] == "graceful"
    assert not os.path.exists(cli.runtime_path("dev")), (
        "a runtime file outliving its server makes `status` report a host and port for something "
        "that is gone"
    )


def test_stop_falls_back_to_the_recorded_pid(capsys, tmp_sessions, monkeypatch):
    """The case that matters: a wedged server that no longer answers HTTP is exactly the one you
    most need to be able to kill."""
    cli.write_runtime("dev", "127.0.0.1", 1, "t")
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"error": "server_unreachable"})
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append(pid))

    _, payload, _ = run(["stop", "--session", "dev"], capsys)

    assert killed == [os.getpid()], "the pid in the runtime file is the one written at serve time"
    assert payload["stopped"] and payload["how"] == "signal"


def test_status_reports_the_pid(capsys, tmp_sessions, monkeypatch):
    """It has been in the runtime file all along and surfaced nowhere."""
    cli.write_runtime("dev", "127.0.0.1", 1, "t")
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"running": True, "session": "dev"})
    _, payload, _ = run(["status", "--session", "dev"], capsys)
    assert payload["pid"] == os.getpid()


def test_the_shutdown_route_is_behind_auth():
    """The one route whose whole purpose is to end the process."""
    src = open(os.path.join(os.path.dirname(cli.__file__), "server.py"), encoding="utf-8").read()
    body = src.split("async def handle_shutdown")[1].split("\nasync def ")[0]
    assert "_check(request, state)" in body, "shutdown must authenticate like every other route"
    assert "status=403" in body


# ------------------------------------------------------------------------ wake


def test_reading_wake_returns_the_documented_shape(capsys, tmp_sessions, no_settings, monkeypatch):
    """`describe` promises one shape for this command; read mode returned another, and a caller
    parsing the response cannot know which branch produced it."""
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"running": False, "error": "no server"})
    _, payload, _ = run(["wake", "--session", "dev"], capsys)

    documented = set(json.loads(json.dumps(cli.DESCRIBE["commands"]["wake"]["returns"])))
    assert documented <= set(payload), (
        f"read mode is missing {sorted(documented - set(payload))} — the keys `describe` promises"
    )


def test_an_unsaved_default_is_not_reported_as_persisted(capsys, tmp_sessions, no_settings,
                                                         monkeypatch):
    """THE REGRESSION. A fresh install answered `persisted: {"name": "assistant"}` pointing at a
    settings file that `config path` reported, in the same session, as not existing."""
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"running": False, "error": "no server"})
    _, payload, _ = run(["wake", "--session", "dev"], capsys)

    assert payload["source"] == "default"
    assert payload["persisted"] is None, "nothing has been saved; do not claim it has"
    assert payload["wake"], "the effective name still has to be reported"
