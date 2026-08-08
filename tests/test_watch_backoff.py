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


def test_it_keeps_doubling_well_past_a_harness_tool_timeout():
    """The ceiling is set by how long he might plausibly be away, NOT by a tool timeout.

    This was briefly capped at nine minutes to stay under Claude Code's 10-minute limit, and JJ
    caught the over-correction: "you shouldn't be waiting 9 minutes always, it should be getting
    longer exponentially." Backgrounding is only fatal without a watchdog, and the contract now
    requires one — so a harness that caps blocking calls should run long waits detached rather
    than shorten them.
    """
    assert cli.WATCH_BACKOFF_MAX_S >= 1800.0
    assert cli.WATCH_BACKOFF_UNREACHABLE_MAX_S > cli.WATCH_BACKOFF_MAX_S
    ladder = [cli._backoff_ceiling(30.0, s, reachable=True) for s in range(7)]
    assert ladder == [30.0, 60.0, 120.0, 240.0, 480.0, 960.0, 1800.0]


def test_the_ceiling_is_overridable(monkeypatch):
    """A harness with a longer limit should be able to use it."""
    monkeypatch.setenv("VOICE_TUNNEL_WATCH_MAX_S", "1200")
    assert cli._backoff_ceiling(30.0, 99, reachable=True) == 1200.0


def test_an_explicit_timeout_is_a_ceiling_not_a_base():
    """`--timeout 480` must mean AT MOST 480, never 480 doubled.

    It shipped the other way and broke the caller twice in ten minutes: a caller trying to stay
    inside its own harness limit was multiplied past it. A caller that names a number knows
    something the tool does not.
    """
    assert cli._backoff_ceiling(2.0, 5, reachable=True) == 64.0   # the ladder itself still doubles
    # ...but cmd_watch only consults the ladder when --timeout was OMITTED; see `explicit`.


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
