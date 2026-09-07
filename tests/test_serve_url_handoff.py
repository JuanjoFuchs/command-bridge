"""When the server is up, `doctor` hands the human the full client URL — WITH its token.

The gap this pins (JJ 2026-09-07): an agent restarts the server DETACHED, never sees the serve
banner (it goes to a pipe), verifies with `doctor`, and then handed the human a TOKENLESS URL from
memory — so the page loaded but the microphone could not connect and he had to ask "don't we need a
token?". `doctor` now surfaces the live, tokened URL and tells the agent to give it over whole. It
is LIVE-PROBED (`_server_alive`), because the runtime file outlives a `stop`: a URL that no longer
connects is worse than none.
"""
import command_bridge.cli as cli

_RT = {"host": "127.0.0.1", "port": 8765, "token": "SECRET-TOKEN-123"}


def test_doctor_hands_over_the_tokened_url_when_the_server_is_up(monkeypatch):
    monkeypatch.setattr(cli, "read_runtime", lambda _s: dict(_RT))
    monkeypatch.setattr(cli, "_server_alive", lambda _rt: True)

    out = cli.cmd_doctor(None)

    assert "client_url" in out, "a live server's URL must be surfaced for the agent to hand over"
    assert "token=SECRET-TOKEN-123" in out["client_url"], "the URL must carry the token"
    # the `next` must TELL the agent to give it to the human, WITH the token in it
    nxt = out["next"].lower()
    assert "give" in nxt and "human" in nxt, "next must instruct handing the URL to the human"
    assert "token" in nxt, "next must flag the token as required"
    assert "SECRET-TOKEN-123" in out["next"], "the exact URL (with token) belongs in next"


def test_doctor_does_not_hand_over_a_dead_servers_url(monkeypatch):
    """The runtime file outlives a `stop`; a URL that no longer connects is worse than none."""
    monkeypatch.setattr(cli, "read_runtime", lambda _s: dict(_RT))
    monkeypatch.setattr(cli, "_server_alive", lambda _rt: False)

    out = cli.cmd_doctor(None)

    assert "client_url" not in out
    assert "SECRET-TOKEN-123" not in out["next"]


def test_doctor_says_nothing_about_a_url_with_no_server_ever_started(monkeypatch):
    monkeypatch.setattr(cli, "read_runtime", lambda _s: None)
    monkeypatch.setattr(cli, "_server_alive", lambda _rt: True)  # must not even be consulted

    out = cli.cmd_doctor(None)

    assert "client_url" not in out


def test_server_alive_is_false_when_nothing_answers(monkeypatch):
    """The probe fails closed: any error on the loopback /health call reads as 'not up', never a
    crash and never a false 'up'."""
    def _boom(*_a, **_k):
        raise OSError("connection refused")
    monkeypatch.setattr(cli.urllib.request, "urlopen", _boom)

    assert cli._server_alive(_RT) is False
