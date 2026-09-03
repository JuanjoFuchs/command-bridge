"""A laneless watch is REFUSED once a second agent has joined.

The mirror of `say`'s `no_lane` guard (server.py, spec 013 FR1), now on `watch`. The failure it
closes was measured live 2026-09-03: a `magnus` agent ran `watch --session dev` with NO `--lane` on
a two-lane session. A laneless watch resolves the DEFAULT lane, so it returned three `atlas` turns —
another agent's conversation — and left the atlas agent's cursor behind. `say` already refused this
exact shape; `watch` did not, so the tool that hands turns out could quietly hand them to the wrong
agent.

The invariant, in JJ's words: *"watch should never allow resolving without lane when there are
multiple lanes."* The predicate is `say`'s verbatim — only when a SECOND lane exists — so a
single-agent session is untouched, and a server too old to publish `lanes` never trips it.
"""
import types

import command_bridge.cli as cli


def _args(**kw):
    base = {"session": "dev", "since": 683, "timeout": 0.0, "force": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _status(status, monkeypatch):
    """Serve `status` for /status and an empty dict for everything else, and stub the blocking
    read so an ALLOWED watch returns instead of waiting on a real log."""
    monkeypatch.setattr(cli, "_request", lambda s, path, payload=None:
                        status if path == "/status" else {})
    monkeypatch.setattr(cli.store, "watch", lambda *a, **k: ([], 5))


def test_laneless_watch_is_refused_on_a_multilane_session(monkeypatch):
    """The whole point: it must REFUSE, not warn — a warning to a watchdog is followed anyway,
    and the turns it would hand back belong to another agent."""
    _status({"lanes": ["magnus", "atlas"], "lane": "atlas"}, monkeypatch)
    result = cli.cmd_watch(_args())  # no `lane` in args -> laneless
    assert result["code"] == "no_lane"
    assert result["reason"] == "no_lane"
    # A caller branching on `finished` must not read a refusal as permission to speak.
    assert result["finished"] is False
    # No turns were resolved — the atlas turns are NOT handed to this caller.
    assert result["turns"] == []
    assert result["count"] == 0
    # The remedy names the flag that fixes it, and the payload names the lanes to choose from.
    assert "--lane" in result["remedy"]
    assert set(result["lanes"]) == {"magnus", "atlas"}


def test_a_named_lane_is_not_refused_on_a_multilane_session(monkeypatch):
    """The flag is what makes it legal: with `--lane` the wait proceeds to the log read."""
    _status({"lanes": ["magnus", "atlas"], "watching_lanes": []}, monkeypatch)
    result = cli.cmd_watch(_args(lane="magnus"))
    assert result.get("code") != "no_lane"
    assert "error" not in result


def test_a_single_lane_session_still_allows_a_laneless_watch(monkeypatch):
    """`say`'s NFR1 carve-out, verbatim: one lane is one place the turns could go, so demanding
    the flag would be ceremony. The single-agent workflow must be unchanged."""
    _status({"lanes": ["magnus"]}, monkeypatch)
    result = cli.cmd_watch(_args())
    assert result.get("code") != "no_lane"
    assert "error" not in result


def test_force_does_not_bypass_the_lane_guard(monkeypatch):
    """`--force` overrides the stale-wait LOCK, never lane routing. A forced laneless watch is the
    exact bug this closes, so force must not be a way through it."""
    _status({"lanes": ["magnus", "atlas"], "lane": "atlas"}, monkeypatch)
    result = cli.cmd_watch(_args(force=True))
    assert result["code"] == "no_lane"


def test_a_server_predating_lanes_never_trips_the_guard(monkeypatch):
    """ABSENT IS NOT MULTI-LANE. A status with no `lanes` field is an old server, not a session
    with two agents — the same absent-versus-false distinction `watch_open` had to learn."""
    _status({"clients": 0}, monkeypatch)  # no `lanes` key at all
    result = cli.cmd_watch(_args())
    assert result.get("code") != "no_lane"
    assert "error" not in result
