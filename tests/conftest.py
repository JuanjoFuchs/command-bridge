import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from voice_tunnel import config


@pytest.fixture(autouse=True)
def hermetic_settings(tmp_path, monkeypatch):
    """Cut every test off from the developer's real `.env` and from the previous test's leftovers.

    Two leaks to close, both of which produce the worst kind of failure — one that depends on who
    is running the suite:

    * `voice_tunnel/config.py` now loads `<repo>/.env` into os.environ. A suite that reads it would pass or
      fail according to whatever the developer last persisted, so VOICE_TUNNEL_ENV_FILE is pointed at a
      path that does not exist.
    * `load_env_file` mutates os.environ directly, so a test that triggers a load leaves VOICE_TUNNEL_*
      variables set for every test that follows. Snapshot and restore them.
    """
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / "no-such.env"))
    before = {k: v for k, v in os.environ.items() if k.startswith("VOICE_TUNNEL_")}
    config._LOAD_REPORT = {}
    yield
    for key in [k for k in os.environ if k.startswith("VOICE_TUNNEL_")]:
        if key not in before:
            del os.environ[key]
    os.environ.update(before)
    config._LOAD_REPORT = {}


@pytest.fixture(autouse=True)
def no_live_tunnel(monkeypatch):
    """Cut every test off from whatever forwarder happens to be running on this machine.

    `status.phone.ready` now asks ngrok's local agent API whether anything is fronting the port,
    which is the fix for a verdict that could never be true — and it is also a second version of
    the leak `hermetic_settings` closes one fixture above. The suite writes runtime files on the
    DEFAULT port, so on a developer machine with a live tunnel `_phone_reachability("127.0.0.1")`
    correctly answered `ready: true` and failed a test asserting loopback is not phone-ready. The
    test was right and the environment was wrong: a result that depends on whether the author
    happens to be mid-conversation is not a test.

    Off by default, so "no tunnel" is what every test gets unless it says otherwise; the ones
    about detection patch `_ngrok_fronts` or set VOICE_TUNNEL_PUBLIC_URL themselves.
    """
    from voice_tunnel import cli

    monkeypatch.setattr(cli, "_ngrok_fronts", lambda port: None)
    monkeypatch.delenv("VOICE_TUNNEL_PUBLIC_URL", raising=False)


@pytest.fixture()
def tmp_sessions(tmp_path, monkeypatch):
    """Isolate turn logs per test so nothing leaks between them or into the repo."""
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(d))
    return str(d)
