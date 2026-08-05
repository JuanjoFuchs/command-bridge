"""Turn log and cursor semantics — spec 001 AC-2, AC-3."""
import pytest

from voice_tunnel import store


def test_ids_are_monotonic_from_zero(tmp_sessions):
    for i in range(3):
        t = store.append_turn("s", f"turn {i}", i, i + 1, True)
        assert t["id"] == i


def test_watch_returns_every_turn_after_cursor_not_just_the_newest(tmp_sessions):
    """AC-2 — the guarantee that nothing is dropped while the agent is thinking.

    This is THE critical test. If it ever returns only the last turn, an agent that reasoned
    for a few seconds silently loses everything said in the meantime.
    """
    for i in range(5):
        store.append_turn("s", f"turn {i}", i, i + 1, True)

    turns, cursor = store.turns_since("s", -1)
    assert [t["id"] for t in turns] == [0, 1, 2, 3, 4]
    assert cursor == 4

    # Agent processed through 1, then three more landed while it was busy.
    turns, cursor = store.turns_since("s", 1)
    assert [t["id"] for t in turns] == [2, 3, 4]
    assert cursor == 4


def test_turns_since_is_empty_and_cursor_unchanged_when_nothing_new(tmp_sessions):
    store.append_turn("s", "only", 0, 1, True)
    turns, cursor = store.turns_since("s", 0)
    assert turns == []
    assert cursor == 0


def test_watch_times_out_to_empty_heartbeat(tmp_sessions):
    turns, cursor = store.watch("s", -1, timeout=0.2, poll=0.05)
    assert turns == []
    assert cursor == -1  # an empty result is a heartbeat, not an error


@pytest.mark.parametrize(
    "bad",
    ["../escape", "a/b", "a\\b", "..", "with\x00null", "with\nnewline", "", "-leading"],
)
def test_dangerous_session_ids_are_rejected_not_sanitized(bad, tmp_sessions):
    """AC-3 — a session id becomes a filename, so it is the traversal surface."""
    with pytest.raises(ValueError):
        store.validate_session(bad)


def test_reasonable_session_ids_are_accepted():
    for good in ["dev", "s1", "meeting-2026-07-28", "a.b_c-d", "A9"]:
        assert store.validate_session(good) == good


def test_malformed_line_does_not_poison_the_log(tmp_sessions):
    store.append_turn("s", "good", 0, 1, True)
    with open(store.log_path("s"), "a", encoding="utf-8") as fh:
        fh.write("{not json\n")          # a torn write during a crash
    store.append_turn("s", "after", 1, 2, True)
    turns = store.read_turns("s")
    assert [t["text"] for t in turns] == ["good", "after"]


def test_addressed_flag_round_trips(tmp_sessions):
    store.append_turn("s", "aside", 0, 1, addressed=False)
    store.append_turn("s", "to you", 1, 2, addressed=True)
    turns = store.read_turns("s")
    assert [t["addressed"] for t in turns] == [False, True]
