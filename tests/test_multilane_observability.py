"""Spec 018 — the facts that describe ONE agent, asked and answered per agent.

**The audit he asked for, 2026-08-25:** *"what other parts of the architecture and observability
and general everything else should have changed and have not changed to support a new multi-lane
architecture."*

Four of the five findings are here (the fifth, the watchdog's first step, is prose in `describe`
and is asserted at the bottom). Each was one value shared by every lane, and each cost him
something different: a pause before an answer, a lane that never got re-armed, a ladder paced by
the wrong agent, and a first refusal missing the text it existed to carry.

NO SERVER IS STARTED HERE. The handlers are driven in-process against a TunnelState of this test's
own — a live voice session runs on `dev` throughout this work.
"""
import asyncio
import json

import pytest

from command_bridge import cli, server


class _Req:
    def __init__(self, state, body):
        self.app = {"state": state}
        self._body = body
        self.remote = "127.0.0.1"
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "magnus")
    st = server.TunnelState("t", token=None)
    st.consumed_cursor = -1
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("kepler")
    return st


def run(coro):
    return asyncio.run(coro)


def consumed(state, cursor, lane):
    return run(server.handle_consumed(_Req(state, {"cursor": cursor, "lane": lane})))


def watching(state, lane, on, empty=False):
    # ⚠ THE KEY IS `open`, NOT `watching`. The first version of this helper sent `watching`, which
    # the endpoint ignores — and `open` defaults to TRUE, so every "the watch closed" call was
    # silently a "the watch opened" call and three assertions failed for a reason that had nothing
    # to do with the code under test. A fixture that names a field the producer does not read is
    # the same class of defect as a probe that reads the model instead of the screen.
    return run(server.handle_watching(
        _Req(state, {"open": on, "lane": lane, "empty": empty})))


# ------------------------------------------------------------------ AC-1: the pre-reply flag

def test_one_lanes_empty_watch_does_not_end_another_lanes_batch(state):
    """AC-1 — FR1. HIS REPORT, AS AN ASSERTION.

    kepler takes a turn and is now composing an answer. magnus, listening on its own lane, comes
    back empty. Before spec 018 that empty watch cleared ONE session-wide boolean, so kepler's next
    wait stopped being a pre-reply check and blocked for a full backoff rung.

    *"that watch resolves immediately because I am silent. But on the other lanes, it doesn't...
    and it goes the full sixty seconds before resolving. What that costs me is time."*
    """
    consumed(state, 10, "kepler")
    assert state.lane_holds_turns["kepler"] is True

    watching(state, "magnus", on=False, empty=True)

    assert state.lane_holds_turns["kepler"] is True, (
        "kepler is still holding the turn it was handed; magnus going quiet says nothing about it"
    )
    assert state.agent_holds_turns is True, (
        "and the session-wide flag still means what it always meant — SOMEBODY is holding turns"
    )


def test_the_lane_that_answered_is_the_lane_whose_batch_ends(state):
    """AC-1 — FR1, the other direction. Speaking ends a batch, and only the speaker's."""
    consumed(state, 10, "kepler")
    consumed(state, 10, "magnus")

    watching(state, "kepler", on=False, empty=True)

    assert state.lane_holds_turns["kepler"] is False
    assert state.lane_holds_turns["magnus"] is True
    assert state.agent_holds_turns is True, "re-derived from the fan-out, not left stale"


def test_the_fan_out_is_published_so_a_lane_can_read_its_own_answer(state):
    """AC-1 — NFR2, FR1. The CLI runs in a different process and cannot see the other lanes, so
    the per-lane answer has to be on the wire rather than inferred from anything local."""
    consumed(state, 10, "kepler")
    snap = state.snapshot() if hasattr(state, "snapshot") else None
    payload = json.loads(run(server.handle_status(_Req(state, {}))).body.decode())

    assert payload["lane_holds_turns"]["kepler"] is True
    assert payload["agent_holds_turns"] is True
    assert snap is None or True


# ------------------------------------------------------------------ AC-3: the backoff ladder

def test_the_backoff_ladder_is_per_lane(monkeypatch, tmp_path):
    """AC-3 — FR3. A lane quiet for nine minutes left the shared ladder at its cap, so a DIFFERENT
    lane's first wait after he spoke opened on that rung."""
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    cli._set_empty_streak("t018a", 5, "magnus")

    assert cli._empty_streak("t018a", "magnus") == 5
    assert cli._empty_streak("t018a", "kepler") == 5, (
        "kepler has no entry of its own yet, so it inherits rather than resetting to zero (TC2)"
    )

    cli._set_empty_streak("t018a", 0, "kepler")
    assert cli._empty_streak("t018a", "kepler") == 0
    assert cli._empty_streak("t018a", "magnus") == 5, (
        "and once a lane HAS its own answer, another lane's cannot move it"
    )


def test_an_existing_session_keyed_streak_is_inherited_not_discarded(monkeypatch, tmp_path):
    """AC-3 — TC2. The streak is persisted in a file that survives the upgrade. Resetting every
    ladder to zero on first read would start a round of hot polling on every live session."""
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    cli._set_empty_streak("t018b", 4)          # the old, lane-less shape

    assert cli._empty_streak("t018b", "magnus") == 4
    assert cli._empty_streak("t018b") == 4


# ------------------------------------------------------------------ AC-4: the refusal memo

def test_two_lanes_each_get_a_first_refusal_with_the_text(state):
    """AC-4 — FR4. The memo was one pair for the session, so the SECOND lane to be refused was
    told it was repeating itself and handed ids instead of turn text — on the one occasion the
    text is what it needs to recover."""
    unread = {"unread": [{"id": 7, "text": "the whole sentence"}], "unread_count": 1,
              "cursor": 7, "since": 6}

    first_magnus = server._unread_refusal(state, unread, "magnus")
    first_kepler = server._unread_refusal(state, unread, "kepler")

    assert first_magnus["refusal_repeat"] == 0
    assert first_magnus["unread"][0]["text"] == "the whole sentence"
    assert first_kepler["refusal_repeat"] == 0, (
        "kepler has never been refused; magnus's refusal is not kepler's repeat"
    )
    assert first_kepler["unread"][0]["text"] == "the whole sentence"

    # And a genuine repeat on one lane is still a repeat.
    again = server._unread_refusal(state, unread, "magnus")
    assert again["refusal_repeat"] == 1 and again["unread"] == [{"id": 7}]


# ------------------------------------------------------------------ AC-5: the timing log

def test_the_timing_log_can_tell_two_agents_apart(state, tmp_path):
    """AC-5 — FR5. `consumed` opens the agent's thinking time and carried only a cursor, so with
    three lanes the log could show how long an answer took and never whose. The wait was not
    stamped at all, which is why the pause he timed had to be found by reading source."""
    from command_bridge import timing

    consumed(state, 10, "kepler")
    watching(state, "kepler", on=True)
    watching(state, "kepler", on=False, empty=True)

    events = timing.read(state.session) if hasattr(timing, "read") else None
    if events is None:
        path = tmp_path / f"{state.session}.timing.jsonl"
        events = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    stages = {e.get("stage"): e for e in events}
    assert "consumed" in stages, "the stamp that opens thinking time"
    assert stages["consumed"].get("lane") == "kepler", "and it says whose thinking it is"
    assert "watch_open" in stages and "watch_closed" in stages, (
        "the wait is stamped at both ends — the event the log has never had"
    )
    assert stages["watch_open"].get("lane") == "kepler"
    assert "holds" in stages["watch_open"], (
        "and it records the fact that decides fast-check versus long-listen"
    )


# ------------------------------------------------------------------ AC-2: the watchdog's step 0

def test_the_watchdog_asks_whether_THIS_lane_is_watching():
    """AC-2 — FR2. The purest instance of the whole pattern: `watching_lanes` was published by spec
    012 and the watchdog that reads it was never told. Its first step branched on `watch_open`,
    which is true while ANY lane watches — so a lane whose watch had died was never re-armed while
    somebody else was listening, and his turns piled up on it unread.

    ⚠ Asserted on `describe`, because that prompt is the text he actually schedules. A fix that
    leaves it saying `watch_open` ships the bug to every agent that follows it.
    """
    prompt = cli.WATCHDOG_PROMPT

    assert "watching_lanes" in prompt, (
        "the scheduled prompt still branches on the session-wide flag"
    )
    assert "YOUR LANE IS NOT IN IT" in prompt, (
        "and it must say explicitly that a non-empty list belonging to OTHER lanes is not a reason "
        "to stay silent — that is the exact reading that left a lane deaf"
    )
    assert "watch_open" in prompt, (
        "the fallback for a server that predates the per-lane list must survive"
    )
