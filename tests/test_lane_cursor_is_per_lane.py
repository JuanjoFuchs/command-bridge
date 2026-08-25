"""Spec 017 — every question of the form "where have you read to" answers per lane.

**Asked for 2026-08-25, after spec 016 landed:** *"What about the cursor? Is the cursor a single
number for the full server, or is now the cursor an independent number per lane?"*

**The answer was "both", and that was the bug.** Spec 015 gave each lane its own cursor and pointed
the transcript's tick marks at it. Everything else that asks the same question — the unread count,
the refusal's remedy, the resume hint the watchdog reads — kept asking `consumed_cursor`, which is
one number for the whole session that every lane's `watch` overwrites.

**Measured live, three lanes on his phone:** `consumed_cursor` 2604 while `lane_consumed` held
`{magnus: 2604, atlas: 2597, kepler: 2589}`. A kepler agent asking where to resume was told 2604.

⚠ **Turn ids are GLOBAL** — one sequence for the whole log, whatever lane a turn is for — so a
lane's cursor is a position on one shared ruler, not a private count. Two lanes never hold the same
id. That is what makes a cursor from the wrong lane silently plausible instead of obviously wrong.
"""
import asyncio

import pytest

from voice_tunnel import server, store


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "magnus")
    st = server.TunnelState("t", token=None)
    st.consumed_cursor = -1
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("kepler")
    return st


def log(state, text, lane):
    # `stamp_lane` is what actually writes the key. Without it `lane` is accepted and DROPPED,
    # because omission is a meaningful state of its own — a turn from before lanes existed. The
    # first version of this helper left it off and every turn logged as lane-less, which made the
    # per-lane filter reject all of them and the assertion fail for the right number and the
    # wrong reason.
    return store.append_turn(state.session, t_start=0.0, t_end=1.0, text=text,
                             addressed=True, final=True, lane=lane, stamp_lane=True)


# ------------------------------------------------------------------ AC-1: the cursor itself

def test_a_lane_that_has_never_reported_falls_back_to_the_session_cursor(state):
    """AC-1 — FR1, NFR2. The session number is the FALLBACK and not the rival: thousands of turns
    predate lanes, and a lane with no cursor of its own has to start somewhere honest."""
    state.consumed_cursor = 40
    assert state.lane_cursor("kepler") == 40
    assert state.lane_cursor(None) == 40, "no lane named means the session, unchanged"

    state.lane_consumed["kepler"] = 12
    assert state.lane_cursor("kepler") == 12, "once a lane has reported, its own answer wins"
    assert state.lane_cursor("magnus") == 40, "and it says nothing about anybody else"


# ------------------------------------------------------------------ AC-2: the unread count

def test_the_unread_count_is_measured_against_the_asking_lanes_cursor(state):
    """AC-2 — FR2. HIS SCENARIO, SHRUNK. Two turns for kepler that kepler has not read, then magnus
    reads past both. Before spec 017 the count was measured against the session cursor, so magnus
    reading marked kepler caught up on turns kepler never received."""
    # kepler has read up to here and no further — the state his own lane was actually in.
    state.lane_consumed["kepler"] = -1
    log(state, "hey kepler, first thing", "kepler")
    log(state, "hey kepler, second thing", "kepler")
    last = log(state, "hey magnus, something else", "magnus")

    # magnus reads everything; the session cursor moves to the head of the log.
    state.consumed_cursor = int(last["id"])
    state.lane_consumed["magnus"] = int(last["id"])

    unread = server._unread_turns(state, lane="kepler")

    assert unread["unread_count"] == 2, (
        "kepler is two turns behind; measuring against the session cursor reported zero because "
        "magnus had read past them"
    )
    assert [t["text"] for t in unread["unread"]] == [
        "hey kepler, first thing", "hey kepler, second thing"]


def test_the_refusal_resumes_from_the_cursor_it_complained_about(state):
    """AC-3 — FR3. The remedy and the complaint must be computed from ONE number. A remedy built
    from a different cursor delivers turns the complaint was not about, so the cursor never reaches
    the turns being complained about and the refusal repeats forever."""
    log(state, "hey kepler, first thing", "kepler")
    last = log(state, "hey magnus, something else", "magnus")
    state.consumed_cursor = int(last["id"])
    state.lane_consumed["magnus"] = int(last["id"])
    state.lane_consumed["kepler"] = -1

    unread = server._unread_turns(state, lane="kepler")
    payload = server._unread_refusal(state, unread)

    assert payload["since"] == -1, "the refusal resumes from kepler's cursor, not the session's"
    assert f"--since {unread['since']}" in payload["remedy"]


# ------------------------------------------------------------------ AC-4: solo is unchanged

def test_a_single_lane_session_answers_exactly_as_before(monkeypatch, tmp_path):
    """AC-4 — NFR1. With one lane the two numbers are the same number, so every answer here
    collapses to the session cursor and nothing about a solo session changes."""
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "magnus")
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.consumed_cursor = 7

    assert st.lane_cursor("magnus") == 7
    st.lane_consumed["magnus"] = 7
    assert st.lane_cursor("magnus") == 7


# ------------------------------------------------------------------ AC-5: the watchdog prompt

def test_the_watchdog_prompt_names_the_per_lane_cursor():
    """AC-5 — FR4. The prompt in `describe` is the text JJ actually schedules, so a fix that leaves
    it saying `consumed_cursor` ships the bug to every agent that follows it. `describe` outranks
    every prose copy of this instruction, which is exactly why it has to be the one that is right.
    """
    from voice_tunnel import cli

    prompt = cli.WATCHDOG_PROMPT if hasattr(cli, "WATCHDOG_PROMPT") else cli.describe()[
        "watchdog"]["prompt"]
    assert "lane_consumed" in prompt, (
        "the scheduled prompt still sends every agent to the session-wide cursor"
    )
    assert "turns_logged" in prompt, "the older warning must survive alongside the new one"


def _unused_asyncio_guard():
    # `asyncio` is imported for parity with the sibling suites that drive coroutines; this file
    # asserts on pure functions only. Named rather than deleted so the next person adding an
    # async case does not re-add the import and wonder why it was missing.
    return asyncio
