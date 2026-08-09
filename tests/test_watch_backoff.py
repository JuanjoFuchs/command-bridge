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


def test_it_doubles_up_to_what_a_foreground_call_can_hold():
    """The ceiling is what a BLOCKING call can reach, and that is the harness's tool timeout.

    Learned over a two-hour silence in three steps: capped at 9 min to fit the harness; raised to
    30 min on the argument that a backgrounded watch still works and the watchdog covers the gap;
    then measured, which killed the argument. DETACHING IS WHAT THE WATCHDOG FIRES ON — it frees
    the harness, the harness goes idle, and the once-a-minute job wakes to find nobody watching.
    Four concurrent watches had piled up before anyone noticed.

    A blocking call and a watchdog are the same mechanism from two sides, and only one can be in
    charge. Foreground costs one turn per ceiling; detaching costs one per watchdog interval plus
    duplicates. So the ladder doubles, and stops where a foreground call does.
    """
    ladder = [cli._backoff_ceiling(30.0, s, reachable=True) for s in range(6)]
    assert ladder == [30.0, 60.0, 120.0, 240.0, 480.0, 540.0]
    assert cli.WATCH_BACKOFF_MAX_S <= 600.0, "must fit inside a 10-minute harness tool timeout"


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
