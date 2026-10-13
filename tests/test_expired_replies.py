"""Spec 021 — a reply that expires tells the AGENT, and waits half an hour before it does.

**The two halves came from one turn.** JJ, 2026-08-26, after losing replies to a bound he had
never agreed to:

    "But why are replies expiring after ten minutes? I haven't made that call."
    "I think 10 minutes is too short. I think we should make it 30 minutes. And when it expires,
     it shouldn't tell me. It should tell the agent. The agent should be aware that their replies
     had expired and did not reach me. Maybe it helps them restate."

🔴 **The silence was the real defect, not the number.** An expiry used to be invisible in both
directions: he never learned a reply had existed, and the agent that wrote it went on believing it
had been delivered — so it never restated, and the exchange simply had a hole in it that neither
party could see. Telling HIM would be noise on a phone; telling the AGENT is actionable, because
the agent is the only party that can do anything about it.
"""
import time

import pytest

from command_bridge import config, server


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    return server.TunnelState("dev", "tok", True)


def hold(state, lane, text, age_s=0.0, clip="clip-1"):
    """Put a clip on a lane's hold queue, optionally already `age_s` seconds old."""
    state.lane_held.setdefault(lane, []).append({
        "header": {"id": clip, "text": text, "lane": lane},
        "pcm": b"\x00\x00" * 8,
        "at": time.time() - age_s,
    })


# ================================================================ the bound he actually chose


def test_a_reply_waits_a_working_day():
    """FR1. Raised to 8h by JJ, 2026-09-14: expiry was net-negative because the re-issue path never
    fired on its own, so an expired clip was simply lost until he asked for it. Thirty minutes was
    still shorter than an absence (a 45-min gap lost a report), so the hold now matches
    WATCH_DISCONNECTED_MAX_S — one duration for "the wait for him to come back". The count cap
    (UNDELIVERED_MAX) stays the stale-stack safety."""
    assert config.LANE_HELD_MAX_AGE_S == 28800.0   # 8h, matching cli.WATCH_DISCONNECTED_MAX_S


def test_a_reply_said_to_nobody_also_waits_a_working_day():
    """🔴 JJ, 2026-09-29, overruling the ten-minute `undelivered` bound (and the old test that kept
    the two bounds apart): *"we don't need a connected page for clips to be held … they can survive
    restarts or toggles."* Both of an agent's morning clips, said before any page connected, expired
    unheard — and the voice rules then told agents to re-say, which he heard as duplicates. A clip
    said to nobody now waits as long as one held for another lane; the count cap stays the
    stale-stack safety."""
    assert config.UNDELIVERED_MAX_AGE_S >= config.LANE_HELD_MAX_AGE_S


def test_a_reply_younger_than_the_bound_is_kept(state):
    hold(state, "atlas", "still good", age_s=config.LANE_HELD_MAX_AGE_S - 60)
    server._prune_lane_held(state, "atlas")

    assert len(state.lane_held.get("atlas", [])) == 1
    assert not state.lane_expired.get("atlas"), "nothing expired, so nothing to report"


def test_a_reply_older_than_the_bound_is_dropped(state):
    hold(state, "atlas", "too old", age_s=config.LANE_HELD_MAX_AGE_S + 1)
    server._prune_lane_held(state, "atlas")

    assert not state.lane_held.get("atlas"), "it must not still be waiting to play"


def test_a_reply_that_would_have_survived_the_old_bound_now_lives(state):
    """The regression guard for the number itself: 11 minutes is dead under the old 600 s and
    alive under the new one, so reverting the constant turns this red."""
    hold(state, "atlas", "eleven minutes old", age_s=660)
    server._prune_lane_held(state, "atlas")

    assert len(state.lane_held.get("atlas", [])) == 1, (
        "at 11 minutes this used to be gone; that is the loss he reported"
    )


# ==================================================== the notice, which is the half that was new


def test_an_expiry_is_recorded_against_the_lane_that_lost_it(state):
    """🔴 FR2. The whole point: the agent finds out. Before this, an expiry was recorded nowhere
    and the agent went on believing its reply had been delivered."""
    hold(state, "atlas", "the answer he never heard", age_s=config.LANE_HELD_MAX_AGE_S + 1, clip="clip-77")
    server._prune_lane_held(state, "atlas")

    notice = state.lane_expired.get("atlas") or []
    assert len(notice) == 1, "an expiry that tells nobody is the defect this spec exists to fix"
    assert notice[0]["text"] == "the answer he never heard", (
        "the TEXT is what makes it actionable — the agent has to know what to restate"
    )
    assert notice[0]["clip"] == "clip-77"
    assert notice[0]["waited_s"] >= 2000


def test_the_notice_does_not_leak_to_another_lane(state):
    hold(state, "atlas", "atlas lost this", age_s=config.LANE_HELD_MAX_AGE_S + 1)
    hold(state, "kepler", "kepler keeps this", age_s=5)
    server._prune_lane_held(state, "atlas")
    server._prune_lane_held(state, "kepler")

    assert list(state.lane_expired) == ["atlas"], "only the lane that lost a reply is told"
    assert len(state.lane_held.get("kepler", [])) == 1


def test_the_notice_is_bounded_like_the_queue_it_replaces(state):
    """A lane nobody ever visits must not grow this without limit — it is held in memory for an
    agent that may never come back."""
    for i in range(config.UNDELIVERED_MAX + 5):
        hold(state, "atlas", f"reply {i}", age_s=config.LANE_HELD_MAX_AGE_S + 1, clip=f"clip-{i}")
    server._prune_lane_held(state, "atlas")

    assert len(state.lane_expired["atlas"]) == config.UNDELIVERED_MAX
    assert state.lane_expired["atlas"][-1]["text"] == f"reply {config.UNDELIVERED_MAX + 4}", (
        "keep the most recent, which are the ones still worth restating"
    )


def test_the_text_is_read_from_the_header_not_the_hold_record(state):
    """The hold wraps `{header, pcm, at}`, so reading `text` off the top level yields an empty
    string — a notice that says something expired without saying what, which is barely better
    than the silence it replaces."""
    hold(state, "atlas", "specific words", age_s=config.LANE_HELD_MAX_AGE_S + 1)
    server._prune_lane_held(state, "atlas")

    assert state.lane_expired["atlas"][0]["text"] == "specific words"
    assert state.lane_expired["atlas"][0]["clip"] is not None


def test_a_single_agent_session_never_populates_this(state):
    """NFR1. Nothing is held off-lane when there is only one lane, so this whole mechanism stays
    invisible to the common case — including on the memory it would otherwise occupy."""
    server._prune_lane_held(state, "magnus")

    assert state.lane_expired == {}
    assert state.lane_held == {}


# ============================================= and `next` now tells the agent to reissue (2026-09-09)


def test_the_next_hint_leads_with_the_reissue_when_a_reply_expired():
    """JJ, 2026-09-09: recording the notice was not enough — the `expired` field alone was read as
    FYI and walked past (*"you haven't reissued it ... improve the next hint telling you to
    reissue"*). So when a watch carries expired replies, `next` — the field the agent acts on — LEADS
    with the restate, ahead of the wait it displaced, and still names the wait."""
    from command_bridge import cli

    result = {"next": "run `command-bridge watch --since 42`",
              "expired": [{"text": "the answer he never heard", "clip": "c1", "waited_s": 1904.3}]}
    cli._lead_next_with_reissue(result, "magnus")

    assert result["next"].startswith("FIRST, REISSUE"), "the reissue must lead, not trail"
    assert 'command-bridge say --lane magnus --now "…"' in result["next"], "with the runnable say"
    assert "run `command-bridge watch --since 42`" in result["next"], "the displaced wait still follows"


def test_the_reissue_hint_is_absent_when_nothing_expired():
    """The common case pays nothing: no `expired`, `next` is untouched."""
    from command_bridge import cli

    result = {"next": "run `command-bridge watch --since 7`"}
    cli._lead_next_with_reissue(result, "magnus")

    assert result["next"] == "run `command-bridge watch --since 7`"


def test_the_reissue_hint_counts_and_pluralises_and_names_the_lane():
    from command_bridge import cli

    result = {"next": "X", "expired": [{"text": "a"}, {"text": "b"}]}
    cli._lead_next_with_reissue(result, "atlas")

    assert "2 earlier replies" in result["next"]
    assert "--lane atlas" in result["next"]


def test_the_reissue_hint_drops_the_lane_flag_for_a_single_lane_session():
    """A single-lane session has no `--lane`; the say command must still be runnable as written."""
    from command_bridge import cli

    result = {"next": "X", "expired": [{"text": "a"}]}
    cli._lead_next_with_reissue(result, None)

    assert 'command-bridge say --now "…"' in result["next"]
    assert "--lane" not in result["next"]
