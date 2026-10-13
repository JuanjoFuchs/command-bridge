"""A quiet watch waits longer each round, and anything at all resets it.

Reported 2026-08-08: "whenever I take longer you also stop watching... we need to design the watch
timeouts with a back off period, an exponential backoff, so that whenever I stop talking for
quite a while and you're still watching you stop wasting turns in silence."

The property that makes this safe is that the timeout governs ONLY how long the call is willing
to return empty. Detection latency is unchanged — turns at `store.watch`'s 0.1 s poll, controls at
the 1 s status check — so a longer ceiling costs nothing and saves a turn.
"""
import time
import types

import command_bridge.cli as cli


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


# ------------------------------------------------------- nobody connected is not "quiet"
#
# Measured 2026-08-15: staying reachable across a six-hour absence cost ~35 re-armed watches, one
# per ceiling, every one of them a guaranteed-empty result. The ladder bounds waiting on A PERSON
# WHO MIGHT SPEAK; a page that is not open cannot produce a turn at all, so there is nothing for
# the rungs to pace.


def test_a_disconnected_watch_does_not_ladder():
    """The same empty streak that would be waiting 30 s connected waits hours with nobody there."""
    quiet = cli._watch_ceiling(30.0, 0, reachable=True, unattended=False, off_lane=False, explicit=False)
    gone = cli._watch_ceiling(30.0, 0, reachable=False, unattended=True, off_lane=False, explicit=False)

    assert quiet == 30.0
    assert gone == cli.WATCH_DISCONNECTED_MAX_S
    assert gone > cli.WATCH_BACKOFF_MAX_S, "otherwise nothing changed and the ~35 wakes remain"


def test_the_disconnected_wait_is_flat_rather_than_growing():
    """There is no evidence to accumulate. Every rung would be the same guaranteed-empty result,
    so the ceiling does not depend on how many of them have already been spent."""
    waits = {cli._watch_ceiling(30.0, s, reachable=False, unattended=True, off_lane=False, explicit=False)
             for s in range(6)}

    assert waits == {cli.WATCH_DISCONNECTED_MAX_S}


def test_there_is_still_a_hard_ceiling():
    """A wait with no ceiling is indistinguishable from a hang, can never report
    `listening: false`, and would hold a socket for an abandoned session until the machine
    restarted. It was eight hours until 2026-10-01; it is now 110 minutes, inside Claude Code's
    two-hour background limit, so the watch returns and is re-armed instead of being stopped by the
    harness and abandoned (atlas, 2026-10-01)."""
    assert cli.WATCH_DISCONNECTED_MAX_S == 6600.0
    assert cli._watch_ceiling(30.0, 99, reachable=False, unattended=True,
                              off_lane=False, explicit=False) == 6600.0


def test_an_env_override_cannot_outlive_the_harness(monkeypatch):
    """JJ's `.env` asked for eight hours. A detached call that long is stopped by the harness at two
    hours, and the harness then tells the agent not to restart it — so the cap applies to the
    override too, or the `.env` brings the deaf lane straight back."""
    monkeypatch.setenv("COMMAND_BRIDGE_WATCH_DISCONNECTED_MAX_S", "28800")
    assert cli._disconnected_ceiling() == cli.WATCH_HARNESS_MAX_S
    assert cli.WATCH_HARNESS_MAX_S < 7200.0
    assert cli._watch_ceiling(30.0, 0, reachable=True, unattended=False, off_lane=False,
                              explicit=False, background=True) == cli.WATCH_HARNESS_MAX_S


def test_an_explicit_timeout_still_wins_over_the_long_wait():
    """A caller that names a number knows something the tool does not — usually its harness's
    maximum tool timeout. Silently handing it eight hours is the same defect as silently handing
    it nine minutes when it asked for eight, which broke the caller twice in ten minutes."""
    assert cli._watch_ceiling(45.0, 3, reachable=False, unattended=True, off_lane=False, explicit=True) == 45.0


def test_the_ceiling_is_overridable_without_editing_the_code(monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_WATCH_DISCONNECTED_MAX_S", "600")
    assert cli._watch_ceiling(30.0, 0, reachable=False, unattended=True, off_lane=False, explicit=False) == 600.0
    monkeypatch.setenv("COMMAND_BRIDGE_WATCH_DISCONNECTED_MAX_S", "not a number")
    assert cli._disconnected_ceiling() == cli.WATCH_DISCONNECTED_MAX_S


def test_describe_states_the_long_wait_where_an_agent_reads_it():
    """`describe` is the tie-break, so a behaviour it does not mention is one that does not exist
    as far as the next agent is concerned — and the ladder it DOES publish would then be a
    confident, wrong answer to "how long will this block"."""
    published = cli._human_seconds(cli.WATCH_DISCONNECTED_MAX_S)
    assert published == "110min"

    notes = cli.DESCRIBE["commands"]["watch"]["notes"]
    background = cli.DESCRIBE["commands"]["watch"]["args"]["--background"]

    for text in (notes, background):
        assert published in text, (
            f"this copy does not publish the disconnected ceiling: {text[:90]}"
        )
    assert "--background" in notes, (
        "the ceiling exceeds every harness tool timeout on purpose; say to run it with --background "
        "where it is stated, or the next agent re-arms short waits and re-creates the ~35 wakes"
    )


# ------------------------------------------------ and the escapes, which are what make it safe


def _run(monkeypatch, tmp_path, first, later, ceiling, lane=None):
    """Drive `cmd_watch` against a scripted `/status`: `first` answers the two setup reads, `later`
    every poll inside the loop. The disconnected ceiling is shrunk to seconds — the branch under
    test is which ceiling gets chosen, not how long eight hours is. `lane` sets --lane, so the
    off-lane branch can be exercised by scripting a status whose live `lane` is something else."""
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))
    monkeypatch.setenv("COMMAND_BRIDGE_WATCH_DISCONNECTED_MAX_S", str(ceiling))
    calls = {"n": 0}

    def request(session, path, payload=None):
        if path != "/status":
            return {}
        calls["n"] += 1
        return first if calls["n"] <= 2 else later

    monkeypatch.setattr(cli, "_request", request)
    # Honours its own timeout, so the loop advances in real time instead of spinning. A stub that
    # returned instantly would busy-wait the whole ceiling and time nothing.
    def watch(session, cursor, timeout=0.0, addressed_only=True, lane=None, default_lane=None):
        time.sleep(min(timeout, 1.0))
        return [], cursor

    monkeypatch.setattr(cli.store, "watch", watch)
    return cli.cmd_watch(types.SimpleNamespace(
        session="s", since=6, timeout=None, force=False, all_turns=False, lane=lane))


def _capture_ceiling(monkeypatch):
    """Record the flags `cmd_watch` hands `_watch_ceiling`, and make the wait tiny.

    The ceiling it CHOSE used to be readable off the payload (`waited`/`next_wait`), but those
    fields were informational-only — the agent re-arms `watch` per `next` regardless of the number
    — so they were pruned (JJ, 2026-09-15). The ceiling SELECTION is still the thing under test, so
    verify it at the source: which (reachable, unattended, off_lane) flags cmd_watch computed. This
    is more direct than the old field read and returns instantly instead of waiting the ceiling out."""
    seen: dict = {}
    real = cli._watch_ceiling

    def spy(base, streak, *, reachable, unattended, off_lane, explicit, background=False):
        seen.update(reachable=reachable, unattended=unattended, off_lane=off_lane, explicit=explicit,
                    background=background)
        real(base, streak, reachable=reachable, unattended=unattended,
             off_lane=off_lane, explicit=explicit, background=background)   # keep the real arithmetic exercised
        return 0.1

    monkeypatch.setattr(cli, "_watch_ceiling", spy)
    return seen


# ------------------------------------------------- off-lane is a long-wait state too (2026-09-03)
#
# Connected, orb on, but he is talking to ANOTHER agent: nothing addressed to THIS lane can arrive
# until he switches back, and a switch wakes the poll within ~1s regardless of the ceiling. So the
# 9-minute ladder has nothing to pace — the wait holds long and detached and resolves the instant he
# returns, exactly like the disconnected case. Before this, an off-lane agent re-armed every ~9 min.


def test_a_foreground_watch_keeps_its_short_ceiling_when_the_orb_goes_off(monkeypatch, tmp_path):
    """magnus, 2026-10-05 and -06: a blocking watch opened with the orb ON took the short ceiling,
    then the orb went off mid-wait and the off-branch re-armed it to the hours-long hold — so the
    call outlived the 10-minute tool timeout it was sized for. It must return inside its window."""
    monkeypatch.setattr(cli, "_backoff_ceiling", lambda base, streak, reachable: 1.5)
    on = {"clients": 1, "channel_open": True, "capturing": True, "muted": False, "lane": "magnus"}
    off = dict(on, channel_open=False, capturing=False)
    t0 = time.monotonic()
    _run(monkeypatch, tmp_path, on, off, 30.0, lane="magnus")
    assert time.monotonic() - t0 < 6, "the orb going off stretched a foreground watch past its ceiling"


def test_the_ceiling_picks_the_long_hold_when_off_lane():
    """The arithmetic: off-lane reuses the disconnected ceiling, not the ladder's 30 s."""
    off = cli._watch_ceiling(30.0, 0, reachable=True, unattended=False, off_lane=True, explicit=False)
    on = cli._watch_ceiling(30.0, 0, reachable=True, unattended=False, off_lane=False, explicit=False)
    assert off == cli._disconnected_ceiling()
    assert on == 30.0, "on-lane and quiet is still the ladder"


def test_cmd_watch_holds_long_when_he_is_on_another_lane(monkeypatch, tmp_path):
    """The report, at the call site: magnus is off-lane (he is on kepler), so its watch takes the
    long hold, not the 30 s rung it would take if this were its own quiet lane."""
    seen = _capture_ceiling(monkeypatch)
    on_kepler = {"clients": 1, "channel_open": True, "capturing": True, "muted": False,
                 "lane": "kepler"}
    _run(monkeypatch, tmp_path, on_kepler, on_kepler, 0.5, lane="magnus")
    assert seen["off_lane"] is True and seen["unattended"] is False, (
        "off-lane must take the long hold, not the 30 s ladder")


def test_a_switch_back_to_my_lane_ends_the_long_off_lane_wait_immediately(monkeypatch, tmp_path):
    """THE PATH THAT MAKES THE LONG OFF-LANE HOLD SAFE — the analogue of a page reconnecting. He is on
    kepler, then switches to magnus, and magnus's long wait returns within a poll rather than sitting
    out its ceiling."""
    on_kepler = {"clients": 1, "channel_open": True, "capturing": True, "muted": False,
                 "lane": "kepler"}
    to_magnus = {**on_kepler, "lane": "magnus"}

    started = time.monotonic()
    result = _run(monkeypatch, tmp_path, on_kepler, to_magnus, 20.0, lane="magnus")

    assert result["reason"] == "lane"
    assert time.monotonic() - started < 5.0, "it waited out the ceiling instead of waking on the switch"


def test_a_dead_server_ends_a_long_off_lane_wait_too(monkeypatch, tmp_path):
    """The same escape the disconnected wait relies on: with nothing answering there is no
    lane-switch path left to end the long hold early, so it must end here rather than hold for hours."""
    on_kepler = {"clients": 1, "channel_open": True, "capturing": True, "muted": False,
                 "lane": "kepler"}

    started = time.monotonic()
    result = _run(monkeypatch, tmp_path, on_kepler, None, 20.0, lane="magnus")

    assert time.monotonic() - started < 5.0, "it blocked on a dead server"
    assert result["listening"] is False


def test_cmd_watch_picks_the_long_ceiling_when_nobody_is_connected(monkeypatch, tmp_path):
    """The whole point, at the call site rather than in the arithmetic: an empty streak of zero
    would wait 30 s if a page were open, and this session's page is not."""
    gone = {"clients": 0, "channel_open": True, "capturing": True, "muted": False}
    seen = _capture_ceiling(monkeypatch)

    _run(monkeypatch, tmp_path, gone, gone, 0.5)

    assert seen["unattended"] is True and seen["reachable"] is False, (
        "nobody connected must take the long hold, not the ladder")


def test_a_page_reconnecting_still_ends_the_wait_immediately(monkeypatch, tmp_path):
    """THE PATH THAT MUST NOT REGRESS. Holding for hours is only acceptable because this returns
    within a second of him coming back; without it the long wait becomes the very unreachability
    it was meant to stop paying for."""
    gone = {"clients": 0, "channel_open": True, "capturing": True, "muted": False}
    back = {**gone, "clients": 1}

    started = time.monotonic()
    result = _run(monkeypatch, tmp_path, gone, back, 20.0)

    assert result["event"] == "control"
    assert result["changed"] == {"clients": True}
    assert time.monotonic() - started < 5.0, "it waited out the ceiling instead of waking on him"


def test_a_server_that_dies_mid_wait_ends_it_rather_than_holding_for_hours(monkeypatch, tmp_path):
    """The wait is held open only because the server promised to report a page arriving. When the
    server stops answering that promise is gone, and without this the agent would learn eight
    hours later. Under the old ceiling the same mistake cost nine minutes, which is why it was
    survivable and why it is not any more."""
    gone = {"clients": 0, "channel_open": True, "capturing": True, "muted": False}

    started = time.monotonic()
    result = _run(monkeypatch, tmp_path, gone, None, 20.0)

    assert time.monotonic() - started < 5.0, "it blocked on a dead server"
    assert result["listening"] is False
    assert "serve" in result["hint"], "say what to do about it, not merely that it happened"


# ------------------------------------------------- the orb being OFF is the same state, not a
# ------------------------------------------------- milder one
#
# Reported 2026-08-17, after fifteen consecutive nine-minute wakes with `count: 0` every time:
# "we shouldnt be burning turns when the orb is off, watch should not timeout."
#
# The old split reasoned from HOW FAST HE COULD COME BACK — a closed channel still has a page
# behind it, a dropped page does not — and picked the ladder for the first. That is the wrong
# question. Coming back ends the wait in a second EITHER WAY, because `clients` and `channel_open`
# are both control facts. What decides the ceiling is whether waiting can yield anything BEFORE he
# comes back, and switching the orb off RELEASES the microphone, so it cannot.


def test_the_orb_being_off_means_no_turn_can_arrive():
    off = {"clients": 1, "channel_open": False, "capturing": False, "muted": False}
    assert cli._no_turn_possible(off) is True


def test_nobody_connected_still_means_no_turn_can_arrive():
    """The case that already worked, asserted through the new predicate so a refactor of it
    cannot quietly drop the original one."""
    assert cli._no_turn_possible(
        {"clients": 0, "channel_open": True, "capturing": True, "muted": False}) is True


def test_a_live_listening_tunnel_is_left_on_the_ladder():
    """The ladder is not dead — connected-and-quiet is still the case it was designed for, and
    routing that to eight hours would be a far worse bug than the one being fixed."""
    live = {"clients": 1, "channel_open": True, "capturing": True, "muted": False}
    assert cli._no_turn_possible(live) is False


def test_an_absent_channel_field_is_not_a_closed_one():
    """ABSENT IS NOT FALSE. A server predating `channel_open` reports nothing, and reading that as
    closed would hand an eight-hour ceiling to a live conversation — the worst available failure,
    because it is silent and it is on the listening path."""
    old = {"clients": 1, "capturing": True, "muted": False}
    assert cli._no_turn_possible(old) is False


def test_a_dead_server_does_not_earn_the_long_ceiling():
    """Not because a turn could arrive, but because with nothing answering there is no
    control-change path left to end a long wait early, and a ceiling nothing can interrupt is the
    one thing that would make eight hours unsafe."""
    assert cli._no_turn_possible(None) is False
    assert cli._no_turn_possible({"error": "connection refused"}) is False
    assert cli._no_turn_possible({"running": False, "clients": 0}) is False


def test_cmd_watch_picks_the_long_ceiling_when_the_orb_is_off(monkeypatch, tmp_path):
    """The report, at the call site. An empty streak of zero waited 30 s and a streak of fifteen
    waited nine minutes; both were guaranteed empty."""
    off = {"clients": 1, "channel_open": False, "capturing": False, "muted": False}
    seen = _capture_ceiling(monkeypatch)

    _run(monkeypatch, tmp_path, off, off, 0.5)

    assert seen["unattended"] is True, "orb off must take the long hold, not the ladder"


def test_the_orb_coming_back_on_still_ends_the_wait_immediately(monkeypatch, tmp_path):
    """THE PATH THAT MAKES THE LONG HOLD SAFE, and the one the old design doubted existed. He taps
    the orb, `channel_open` moves, and the wait returns — no different from a page reconnecting."""
    off = {"clients": 1, "channel_open": False, "capturing": False, "muted": False}
    back = {**off, "channel_open": True, "capturing": True}

    started = time.monotonic()
    result = _run(monkeypatch, tmp_path, off, back, 20.0)

    assert result["event"] == "control"
    assert result["changed"] == {"channel_open": True, "capturing": True}
    assert time.monotonic() - started < 5.0, "it waited out the ceiling instead of waking on him"


def test_a_quiet_but_listening_tunnel_still_ladders(monkeypatch, tmp_path):
    """The regression guard for the fix itself: with the orb ON and him merely silent, the very
    next wait must still be the 30 s rung, because now an empty result IS evidence."""
    live = {"clients": 1, "channel_open": True, "capturing": True, "muted": False}
    seen = _capture_ceiling(monkeypatch)

    _run(monkeypatch, tmp_path, live, live, 0.5)

    assert seen == {"reachable": True, "unattended": False, "off_lane": False, "explicit": False,
                    "background": False}, (
        "a listening-but-quiet tunnel must take the ladder, not the orb-off/long hold")


# ------------------------------------------------ 2026-10-01: no ladder, and `--background`
#
# JJ: "maybe just have it be 9 minutes without that build up, and then we want a way for the agents
# to pass a flag to declare the intent that they will background the command so that it holds
# longer, and then maybe we retire --timeout."


def test_an_idle_wait_is_a_flat_nine_minutes_from_the_first_one():
    """The ladder's first rung was 30 s and every event reset it, so a quiet agent woke every half
    minute after each clip. The default base now IS the cap."""
    assert cli.WATCH_BASE_S == cli.WATCH_BACKOFF_MAX_S
    assert cli._backoff_ladder() == [cli.WATCH_BACKOFF_MAX_S]
    assert cli._watch_ceiling(cli._backoff_cap(), 0, reachable=True, unattended=False,
                              off_lane=False, explicit=False) == cli.WATCH_BACKOFF_MAX_S


def test_background_takes_the_long_hold():
    """The caller declared it will not block on the call, so the 10-minute tool-timeout reason for
    the 9-minute cap does not apply."""
    assert cli._watch_ceiling(cli._backoff_cap(), 0, reachable=True, unattended=False, off_lane=False,
                              explicit=False, background=True) == cli._disconnected_ceiling()


def test_cmd_watch_passes_background_through(monkeypatch, tmp_path):
    live = {"clients": 1, "channel_open": True, "capturing": True, "muted": False}
    seen = _capture_ceiling(monkeypatch)
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cli, "_request", lambda s, p, payload=None: live if p == "/status" else {})
    monkeypatch.setattr(cli.store, "watch", lambda *a, **k: ([], 6))
    cli.cmd_watch(types.SimpleNamespace(session="s", since=6, timeout=None, force=False,
                                        all_turns=False, lane=None, background=True))
    assert seen["background"] is True


def test_the_timeout_flag_is_retired_with_a_pointed_error():
    """Passing `--timeout` returns `retired_flag` naming `--background`, not argparse's bare error."""
    args = cli.build_parser().parse_args(["watch", "--since", "3", "--timeout", "30"])
    out = cli.cmd_watch(args)
    assert out["code"] == "retired_flag"
    assert "--background" in out["remedy"]


def test_the_closed_channel_guidance_says_it_holds_rather_than_re_arms(monkeypatch, tmp_path):
    """An agent follows `next`, not the source. If that field still reads as a re-armed series the
    behaviour changed and the instruction did not, which is the shape of the original bug."""
    off = {"clients": 1, "channel_open": False, "capturing": False, "muted": False}

    result = _run(monkeypatch, tmp_path, off, off, 0.5)

    # Read through `_disconnected_ceiling` rather than off the constant: `_run` shrinks the
    # ceiling by env so the test finishes in half a second, and these fields must publish the
    # ceiling ACTUALLY in force. Asserting the constant would pass while the code published a
    # different number — the exact drift the fields exist to prevent.
    published = cli._human_seconds(cli._disconnected_ceiling())
    assert published in result["next"], "the command to run next understates how long it holds"
    assert published in result["hint"], "and so does the explanation beside it"
    assert "orb" in result["hint"], (
        "a closed channel is a decision he made; the hint must not tell him his microphone broke"
    )
