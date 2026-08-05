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


@pytest.fixture()
def tmp_sessions(tmp_path, monkeypatch):
    """Isolate turn logs per test so nothing leaks between them or into the repo."""
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(d))
    return str(d)
