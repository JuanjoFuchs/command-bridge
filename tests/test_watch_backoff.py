"""A quiet watch waits longer each round, and anything at all resets it.

JJ, 2026-08-08: "whenever I take longer you also stop watching... we need to design the watch
timeouts with a back off period, an exponential backoff, so that whenever I stop talking for
quite a while and you're still watching you stop wasting turns in silence."

The property that makes this safe is that the timeout governs ONLY how long the call is willing
to return empty. Detection latency is unchanged — turns at `store.watch`'s 0.1 s poll, controls at
the 1 s status check — so a longer ceiling costs nothing and saves a turn.
"""
import voice_tunnel.cli as cli


def test_the_wait_doubles_while_nothing_happens():
    waits = [cli._backoff_ceiling(30.0, s, reachable=True) for s in range(5)]
    assert waits == [30.0, 60.0, 120.0, 240.0, 480.0]


def test_the_wait_is_capped():
    """Unbounded doubling would eventually block for a day, and a session left open overnight
    should still notice him in the morning without being restarted."""
    assert cli._backoff_ceiling(30.0, 99, reachable=True) == cli.WATCH_BACKOFF_MAX_S
    assert cli._backoff_ceiling(30.0, 99, reachable=False) == cli.WATCH_BACKOFF_UNREACHABLE_MAX_S


def test_the_ceiling_stays_under_a_harness_tool_timeout():
    """The ceiling is set by the CALLER'S HARNESS, not by the situation.

    Found by hitting it: a 16-minute wait exceeded Claude Code's 10-minute maximum tool timeout,
    so the harness moved the call to the background — at which point it is not a blocking watch
    any more, the turn ends, and the failure this mechanism exists to prevent happens again with
    a longer fuse. A watch that outlives its harness's tool timeout stops being a watch.
    """
    assert cli.WATCH_BACKOFF_MAX_S <= 600.0
    assert cli.WATCH_BACKOFF_UNREACHABLE_MAX_S <= 600.0


def test_the_ceiling_is_overridable(monkeypatch):
    """A harness with a longer limit should be able to use it."""
    monkeypatch.setenv("VOICE_TUNNEL_WATCH_MAX_S", "1200")
    assert cli._backoff_ceiling(30.0, 99, reachable=True) == 1200.0


def test_the_caller_s_timeout_is_still_the_base():
    """A harness asking for 2 seconds must get 2 seconds on the first call, not 30. The backoff
    multiplies what was asked for; it does not replace it."""
    assert cli._backoff_ceiling(2.0, 0, reachable=True) == 2.0
    assert cli._backoff_ceiling(2.0, 1, reachable=True) == 4.0


def test_the_streak_round_trips_through_disk(tmp_path, monkeypatch):
    """It has to survive the process, because every invocation is a new one. Held in memory the
    backoff would reset on every call and do nothing at all."""
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))
    assert cli._empty_streak("s") == 0
    cli._set_empty_streak("s", 3)
    assert cli._empty_streak("s") == 3
    cli._set_empty_streak("s", 0)
    assert cli._empty_streak("s") == 0


def test_a_missing_or_corrupt_file_reads_as_zero(tmp_path, monkeypatch):
    """Bookkeeping must never break a watch. A garbled file means start over, not crash."""
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))
    (tmp_path / "s.watch.json").write_text("{not json", encoding="utf-8")
    assert cli._empty_streak("s") == 0
