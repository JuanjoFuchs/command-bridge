"""Turn log and cursor semantics — spec 001 AC-2, AC-3."""
import pytest

from command_bridge import store


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


# ------------------------------------------------------- turns that were not for you (2026-08-15)


def test_unaddressed_turns_are_consumed_but_not_returned(tmp_sessions):
    """Reported live on the move, family talking around him: *"we need a better way to ignore what
    isn't classified as me, and it doesn't waste turns resolving the watch."*

    Both halves matter. Not returned, so the agent does not wake for someone else's sentence —
    and CONSUMED, so the same sentence is not re-read on the next call and does not wake it then.
    """
    store.append_turn("s", "hey claude, do the thing", 0, 1, True)
    store.append_turn("s", "pass the water", 1, 2, False)
    store.append_turn("s", "what's for lunch", 2, 3, False)

    turns, cursor = store.turns_since("s", -1, addressed_only=True)

    assert [t["text"] for t in turns] == ["hey claude, do the thing"]
    assert cursor == 2, "the cursor must clear the unaddressed turns, not stall behind them"

    again, cursor = store.turns_since("s", cursor, addressed_only=True)
    assert again == [], "consumed means consumed — they must not come back"


def test_all_turns_still_sees_everything(tmp_sessions):
    """The filter is a default, not a deletion: an audit of what the gate rejected must remain
    possible, and `--all-turns` is that audit."""
    store.append_turn("s", "mine", 0, 1, True)
    store.append_turn("s", "someone else", 1, 2, False)

    turns, _ = store.turns_since("s", -1, addressed_only=False)
    assert [t["text"] for t in turns] == ["mine", "someone else"]


def test_a_watch_does_not_end_on_a_turn_that_was_not_for_you(tmp_sessions):
    """The wall-clock version of the bug: the watch RETURNED, which is what burned his turns.

    A room talking must look identical to silence from the agent's side — an empty heartbeat
    after the full timeout, with the cursor advanced past what it consumed.
    """
    store.append_turn("s", "someone else entirely", 0, 1, False)

    turns, cursor = store.watch("s", -1, timeout=0.3, poll=0.05, addressed_only=True)

    assert turns == [], "an unaddressed turn must not end the wait"
    assert cursor == 0, "but it must still be marked read"
