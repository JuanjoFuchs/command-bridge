"""Spec 012 Slice A — the turn log's `lane` stamp and the filtered cursor read.

**AC-7** — a lane's watch returns its own turns and broadcasts, never another lane's, and the
cursor still advances past the ones it filtered out.
**AC-8** — a turn with no `lane` key at all reaches the DEFAULT lane and nobody else, which is
what keeps the thousands of turns already on disk working.
"""
import json

import pytest

from voice_tunnel import store
from voice_tunnel.lanes import BROADCAST


@pytest.fixture()
def log(tmp_path):
    """A log written the way the server writes one, one turn per line."""
    def write(*turns):
        path = tmp_path / "dev.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i, turn in enumerate(turns):
                row = {"id": i, "session": "dev", "t_start": 0.0, "t_end": 1.0,
                       "text": f"turn {i}", "addressed": True, "final": True, "wall": "now"}
                row.update(turn)
                fh.write(json.dumps(row) + "\n")
        return str(tmp_path)
    return write


# ------------------------------------------------------------------ AC-8: the legacy turn

def test_a_turn_with_no_lane_key_belongs_to_the_default_lane():
    assert store.lane_of({"text": "x"}, "claude") == "claude"


def test_a_turn_with_an_explicit_null_lane_belongs_to_nobody():
    """Absent and null are DIFFERENT and the difference is load-bearing. Null is a turn the wake
    gate refused to route; answering `default` for it would deliver it to the lane he was already
    talking to, which is the exact mis-route the refusal exists to prevent."""
    assert store.lane_of({"text": "x", "lane": None}, "claude") is None
    assert store.turn_is_for({"lane": None}, "claude", "claude") is False
    assert store.turn_is_for({"lane": None}, "codex", "claude") is False


def test_the_legacy_turns_reach_the_default_lane_and_no_other(log):
    base = log({}, {}, {"lane": "codex"})
    kept, _ = store.turns_since("dev", -1, base, lane="claude", default_lane="claude")
    assert [t["id"] for t in kept] == [0, 1]

    kept, _ = store.turns_since("dev", -1, base, lane="codex", default_lane="claude")
    assert [t["id"] for t in kept] == [2]


# ------------------------------------------------------------------ AC-7: filtering

def test_a_lane_sees_its_own_turns_and_broadcasts_only(log):
    base = log(
        {"lane": "claude"},
        {"lane": "codex"},
        {"lane": BROADCAST},
        {"lane": None},
        {"lane": "codex"},
    )
    claude, _ = store.turns_since("dev", -1, base, lane="claude", default_lane="claude")
    codex, _ = store.turns_since("dev", -1, base, lane="codex", default_lane="claude")

    assert [t["id"] for t in claude] == [0, 2]
    assert [t["id"] for t in codex] == [1, 2, 4]
    # The refused turn reached nobody at all.
    assert 3 not in [t["id"] for t in claude + codex]


def test_the_cursor_advances_past_another_lanes_turns(log):
    """The same rule `addressed_only` follows: a turn this agent will not receive is CONSUMED,
    not deferred. Otherwise every agent wakes on every other agent's turns and re-reads them
    forever — the 'don't waste turns resolving the watch' complaint, multiplied by the number of
    agents in the room."""
    base = log({"lane": "claude"}, {"lane": "codex"}, {"lane": "codex"})
    kept, cursor = store.turns_since("dev", -1, base, lane="claude", default_lane="claude")
    assert [t["id"] for t in kept] == [0]
    assert cursor == 2, "the cursor must come from the FULL slice, before lane filtering"


def test_a_watch_on_a_quiet_lane_times_out_while_another_lane_talks(log):
    """The point of the cursor rule, stated as behaviour: codex talking must not end claude's
    wait, and must not leave claude's cursor behind either."""
    base = log({"lane": "codex"}, {"lane": "codex"})
    turns, cursor = store.watch("dev", -1, timeout=0.05, poll=0.01, base=base,
                                lane="claude", default_lane="claude")
    assert turns == []
    assert cursor == 1, "consumed, not deferred — a timeout still advances past them"


def test_a_broadcast_wakes_every_lane(log):
    base = log({"lane": BROADCAST})
    for who in ("claude", "codex", "somebodyelse"):
        turns, _ = store.watch("dev", -1, timeout=0.05, poll=0.01, base=base,
                               lane=who, default_lane="claude")
        assert [t["id"] for t in turns] == [0], f"{who} missed a broadcast"


def test_no_lane_argument_means_no_filtering_at_all(log):
    """The existing single-agent callers pass nothing and must keep seeing every turn, including
    ones stamped for a lane they have never heard of."""
    base = log({"lane": "claude"}, {"lane": "codex"}, {}, {"lane": None})
    kept, cursor = store.turns_since("dev", -1, base)
    assert [t["id"] for t in kept] == [0, 1, 2, 3]
    assert cursor == 3


def test_lane_and_addressed_filters_compose(log):
    base = log(
        {"lane": "claude", "addressed": True},
        {"lane": "claude", "addressed": False},
        {"lane": "codex", "addressed": True},
    )
    kept, cursor = store.turns_since("dev", -1, base, addressed_only=True,
                                     lane="claude", default_lane="claude")
    assert [t["id"] for t in kept] == [0]
    assert cursor == 2


# ------------------------------------------------------------------ writing the stamp

def test_a_turn_is_only_stamped_when_the_caller_says_so(tmp_sessions):
    """`None` is a meaningful lane and omission is a different meaningful state, so the value
    cannot decide whether the key is written. A caller with no opinion must leave the log exactly
    as it was — otherwise every existing tool that appends a turn silently starts claiming a lane
    it never chose."""
    plain = store.append_turn("dev", "hello", 0.0, 1.0, True, base=tmp_sessions)
    assert "lane" not in plain

    refused = store.append_turn("dev", "hey cloud", 0.0, 1.0, False, base=tmp_sessions,
                                lane=None, stamp_lane=True)
    assert "lane" in refused and refused["lane"] is None

    routed = store.append_turn("dev", "hey codex", 0.0, 1.0, True, base=tmp_sessions,
                               lane="codex", stamp_lane=True)
    assert routed["lane"] == "codex"


def test_the_stamp_survives_the_round_trip_to_disk(tmp_sessions):
    store.append_turn("dev", "a", 0.0, 1.0, True, base=tmp_sessions, lane="codex",
                      stamp_lane=True)
    store.append_turn("dev", "b", 0.0, 1.0, True, base=tmp_sessions)
    store.append_turn("dev", "c", 0.0, 1.0, False, base=tmp_sessions, lane=None,
                      stamp_lane=True)

    on_disk = store.read_turns("dev", tmp_sessions)
    assert [t.get("lane", "<absent>") for t in on_disk] == ["codex", "<absent>", None]
    assert [store.lane_of(t, "claude") for t in on_disk] == ["codex", "claude", None]
