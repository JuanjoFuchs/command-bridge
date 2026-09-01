import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from command_bridge import config


@pytest.fixture(autouse=True)
def hermetic_settings(tmp_path, monkeypatch):
    """Cut every test off from the developer's real `.env`, real session directory, and from the
    previous test's leftovers.

    Three leaks to close, all of which produce the worst kind of failure — one that depends on who
    is running the suite:

    * `command_bridge/config.py` now loads `<repo>/.env` into os.environ. A suite that reads it would pass or
      fail according to whatever the developer last persisted, so VOICE_TUNNEL_ENV_FILE is pointed at a
      path that does not exist.
    * `load_env_file` mutates os.environ directly, so a test that triggers a load leaves VOICE_TUNNEL_*
      variables set for every test that follows. Snapshot and restore them.
    * **VOICE_TUNNEL_DIR, since spec 011 FR3.** Session isolation used to be OPT-IN (`tmp_sessions`
      below) and that was survivable only because `cmd_say` persisted NOTHING — a test could call it
      with `--session dev` and touch no disk. FR3 gave `cmd_say` and `cmd_watch` a per-session state
      file at `<session_dir>/<session>.watch.json`, so every unisolated test now READS AND WRITES the
      real `sessions/` directory. Both halves of that bite:

      - **Reads make tests order-dependent.** `test_say_refusal.py`'s two CLI-level tests both run
        `say --session dev`; the second one saw the `next_branch` memo the first one had just left
        behind and got FR3's shortened `next` string, so the first test's `"restated" in next`
        assertion passed only on that ordering. A test whose verdict depends on which test ran
        before it is not a test.
      - **Writes corrupt a live conversation.** `sessions/s.watch.json` was observed being rewritten
        at 15:49:54 by a suite run; its `empty_streak` survived only because FR3's writer is
        read-modify-write. A suite must never be able to reach into a session a human is mid-sentence
        in.

      Isolation therefore belongs here, autouse, next to the other two — not in a fixture a test has
      to remember to ask for. `tmp_sessions` keeps working unchanged: it points at the same
      `tmp_path / "sessions"`, so opting in now only adds the mkdir.
    """
    monkeypatch.setenv("VOICE_TUNNEL_ENV_FILE", str(tmp_path / "no-such.env"))
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path / "sessions"))
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
    from command_bridge import cli

    monkeypatch.setattr(cli, "_ngrok_fronts", lambda port: None)
    monkeypatch.delenv("VOICE_TUNNEL_PUBLIC_URL", raising=False)


@pytest.fixture()
def tmp_sessions(tmp_path, monkeypatch):
    """Isolate turn logs per test so nothing leaks between them or into the repo.

    Now a formality rather than a defence: `hermetic_settings` above points VOICE_TUNNEL_DIR at this
    same path for EVERY test, so what this adds is the mkdir and the returned path for tests that
    want to write fixture files into the directory themselves.
    """
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(d))
    return str(d)
