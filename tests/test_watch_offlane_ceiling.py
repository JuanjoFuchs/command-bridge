"""The speech ceiling asks "is he talking TO ME", not "is anyone talking" (2026-09-03).

`_still_talking` reads a speech signal the server publishes COMBINED across every lane, so on a
multi-lane session an off-addressed lane's watch treated his conversation with ANOTHER agent as
"talking": it never reached the not-talking branch, sat out the `WATCH_SPEECH_MAX_S` ceiling, and
re-armed every ~2 min — a wasted turn per idle agent while he worked with one of them.

The fix narrows the ceiling (and the fast poll) to `_talking_to_me`: the live lane IS who he is
talking to, so when it is not mine his speech does not hold my wait — it falls through to the idle
backoff instead. There is no latency cost, because the loop still polls at the same rate and catches
a switch TO me on the next poll; only the RETURN ceiling moved.
"""
import time
import types

import command_bridge.cli as cli


# --- the pure narrowing ------------------------------------------------------

def test_on_my_lane_his_speech_is_to_me():
    assert cli._talking_to_me(True, {"lane": "magnus"}, "magnus") is True


def test_on_another_lane_his_speech_is_not_to_me():
    """The bug in one line: he is talking, but to kepler, so a magnus watch must not treat it as
    talking-to-magnus and burn the ceiling."""
    assert cli._talking_to_me(True, {"lane": "kepler"}, "magnus") is False


def test_a_single_agent_watch_counts_all_speech():
    """No lane to be off of — `my_lane` None means the one agent, so any speech is to it. This is
    what keeps the single-agent workflow unchanged."""
    assert cli._talking_to_me(True, {"lane": "kepler"}, None) is True


def test_a_server_without_a_lane_field_counts_all_speech():
    """ABSENT IS NOT OFF-LANE. A server predating lanes publishes no `lane`; treat his speech as to
    me, exactly as before — never narrow on missing evidence."""
    assert cli._talking_to_me(True, {}, "magnus") is True


def test_not_talking_is_never_talking_to_me():
    assert cli._talking_to_me(False, {"lane": "magnus"}, "magnus") is False


def test_unknown_speech_coerces_to_false():
    """`_still_talking` returns None when the server publishes no speech fields; it must read as
    not-talking-to-me, exactly as `not talking` treated None before."""
    assert cli._talking_to_me(None, {"lane": "magnus"}, "magnus") is False


# --- the loop, at the call site ---------------------------------------------

def _run(monkeypatch, tmp_path, status, my_lane, *, speech_max, timeout):
    """Drive `cmd_watch` against a CONSTANT `/status` (so no lane_event fires and the branch under
    test is the ceiling gate, not a switch), with the speech ceiling shrunk so the test finishes in
    a fraction of a second. A real-sleeping `store.watch` stub advances the loop in real time."""
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cli, "WATCH_SPEECH_MAX_S", speech_max)

    def request(session, path, payload=None):
        return status if path == "/status" else {}
    monkeypatch.setattr(cli, "_request", request)

    def watch(session, cursor, timeout=0.0, addressed_only=True, lane=None, default_lane=None):
        time.sleep(min(timeout, 0.2))
        return [], cursor
    monkeypatch.setattr(cli.store, "watch", watch)

    return cli.cmd_watch(types.SimpleNamespace(
        session="s", since=6, timeout=timeout, force=False, all_turns=False, lane=my_lane))


_TALKING_TO_KEPLER = {"clients": 1, "channel_open": True, "capturing": True, "muted": False,
                      "user_speaking": True, "lane": "kepler"}
_TALKING_TO_MAGNUS = {**_TALKING_TO_KEPLER, "lane": "magnus"}


def test_an_off_lane_watch_does_not_hit_the_speech_ceiling(monkeypatch, tmp_path):
    """The whole fix, at the call site: he is talking to kepler, so the magnus watch must NOT burn
    the speech ceiling on it. It falls through to the idle path and returns `quiet`, never
    `ceiling`. (On the pre-fix code this returned `ceiling`.)"""
    result = _run(monkeypatch, tmp_path, _TALKING_TO_KEPLER, "magnus",
                  speech_max=0.05, timeout=0.6)
    assert result["reason"] != "ceiling", "an off-lane watch burned the speech ceiling"
    assert result["reason"] == "quiet"


def test_an_on_lane_watch_still_hits_the_speech_ceiling(monkeypatch, tmp_path):
    """The regression guard: when he is talking to ME and does not stop, the ceiling must still
    fire — that is the interrupt-guard it was written for, and the fix must not remove it."""
    started = time.monotonic()
    result = _run(monkeypatch, tmp_path, _TALKING_TO_MAGNUS, "magnus",
                  speech_max=0.05, timeout=5.0)
    assert result["reason"] == "ceiling", "the on-lane interrupt-guard stopped firing"
    assert result["finished"] is False, "the ceiling is not permission to reply"
    assert time.monotonic() - started < 2.0, "it waited well past the shrunk ceiling"


# --- the RETURNED payload is lane-scoped too, not just the hold gate (2026-09-08) --------------
#
# The ceiling fix above narrowed the loop's HOLD decision. But the payload it hands back still
# reported the raw session-wide `user_speaking`, so an off-lane agent that returned for any reason
# read "he is speaking" and could not tell the speech was not its own — and re-held a watch it never
# needed. JJ: *"Dexter found there was a user_speaking flag ... so it's holding another watch
# unnecessarily"* while he was talking to magnus. The flag an agent branches on must mean "to ME".

def test_an_off_lane_return_reports_user_speaking_scoped_to_my_lane(monkeypatch, tmp_path):
    """He is talking to kepler; a magnus watch that returns must report `user_speaking: False`. The
    status it read said the mic was hot (session-wide), but that speech was not on magnus's lane."""
    result = _run(monkeypatch, tmp_path, _TALKING_TO_KEPLER, "magnus",
                  speech_max=0.05, timeout=0.6)
    assert result["user_speaking"] is False, \
        "an off-lane watch reported the session-wide flag as if the user were talking to it"


def test_an_on_lane_return_still_reports_user_speaking_true(monkeypatch, tmp_path):
    """The other half: when he IS talking to me, the payload must still say so — the scope narrows it
    to my lane, it does not blind the lane he is actually addressing."""
    result = _run(monkeypatch, tmp_path, _TALKING_TO_MAGNUS, "magnus",
                  speech_max=0.05, timeout=5.0)
    assert result["user_speaking"] is True, "the lane he is addressing lost its own speech signal"


def test_a_single_agent_return_reports_the_unscoped_flag(monkeypatch, tmp_path):
    """No lane to be off of: a single-agent watch (`my_lane` None) reports the combined flag exactly
    as before, so the scoping is invisible to the workflow it does not apply to."""
    result = _run(monkeypatch, tmp_path, _TALKING_TO_KEPLER, None,
                  speech_max=0.05, timeout=0.6)
    assert result["user_speaking"] is True
