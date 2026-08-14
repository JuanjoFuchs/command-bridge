"""`pending_turns` — the field whose obvious use was a no-op.

It was `turns_logged - 1 - consumed_cursor`, and `turns_logged` counts what THIS PROCESS has
written since it started. So on any server that has been restarted — which is every long-lived
session and no author's test — the arithmetic goes negative and clamps to zero, and the field
reads "you are caught up" no matter how far behind the reader is.

Measured live on 2026-08-14, mid-conversation: `last_turn_id` 209, `turns_logged` 133,
`consumed_cursor` 209. Five more turns would have moved the real backlog to five and left this
field at zero.

WHY IT IS WORSE THAN A MISSING FIELD. The obvious way to mechanise "drain until nothing is left"
is to loop until pending is zero, and a field that is already zero turns that loop into a single
pass — the precise "he must have finished" mistake the drain discipline exists to stop. It also
drives the page's own "read to here — N more below" divider, so the person on the phone was told
he was caught up while he was not. The source itself calls `turns_logged` its impostor and the
watchdog prompt says never to use it; the status payload was using it anyway.
"""
import pytest

from voice_tunnel import server, store


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    return server.TunnelState("t", token=None)


def say(session, n):
    """Append `n` turns the way the server does, so ids are assigned by the log."""
    for i in range(n):
        store.append_turn(session=session, text=f"turn {i}", t_start=0.0, t_end=1.0,
                          addressed=True)


def test_a_restarted_server_still_counts_the_backlog(state):
    """THE REGRESSION, with the shape it had live: a log far ahead of this process's counter."""
    say("t", 210)                    # ids 0..209, written by a server that has since exited
    state.turns_logged = 0           # this process has written nothing yet
    state.consumed_cursor = 204      # and five of them are unread

    assert state.pending_turns() == 5, (
        "pending must come from the log; `turns_logged` belongs to this process, not to the "
        "conversation"
    )


def test_the_old_arithmetic_would_have_said_zero(state):
    """Pins the bug itself, so the formula cannot quietly come back."""
    say("t", 210)
    state.turns_logged = 0
    state.consumed_cursor = 204

    assert max(0, state.turns_logged - 1 - state.consumed_cursor) == 0
    assert state.pending_turns() > 0


def test_caught_up_is_zero(state):
    say("t", 10)
    state.consumed_cursor = 9
    assert state.pending_turns() == 0


def test_nothing_said_yet_is_zero(state):
    """An empty log is `last_turn_id == -1` and a fresh cursor is -1: not -1 - -1 = 0 by
    accident, but genuinely nothing pending."""
    assert state.pending_turns() == 0


def test_a_reader_that_has_read_nothing_is_behind_by_everything(state):
    say("t", 3)
    assert state.consumed_cursor == -1, "the starting cursor means 'has read nothing'"
    assert state.pending_turns() == 3


def test_the_snapshot_publishes_the_honest_number(state):
    say("t", 210)
    state.turns_logged = 0
    state.consumed_cursor = 204

    snap = state.snapshot()

    assert snap["pending_turns"] == 5
    assert snap["last_turn_id"] == 209
    assert snap["turns_logged"] == 0, "the impostor stays published, so it can be seen not to be it"


def test_a_caller_can_reuse_a_last_turn_id_it_already_paid_for(state):
    """`snapshot` reads the log once for two fields. Passing the id in must give the same answer
    as looking it up."""
    say("t", 10)
    state.consumed_cursor = 4
    assert state.pending_turns(9) == state.pending_turns() == 5


def test_the_impostor_is_gone_from_every_pending_computation():
    """Three places computed this, not one. The two broadcast paths feed the page's read-lag
    divider ("read to here — N more below"), so a phone was being told it was caught up too.

    Matched on the code shape rather than the words, because the docstrings on the fix quote the
    old formula on purpose — the reason it was wrong is worth keeping next to it.
    """
    with open(server.__file__, encoding="utf-8") as fh:
        src = fh.read()
    for dead in ("max(0, self.turns_logged", "max(0, state.turns_logged"):
        assert dead not in src, (
            f"`{dead}...` is back: a backlog derived from turns_logged reads 0 on every "
            f"restarted server"
        )
    assert src.count("pending_turns(") >= 4, (
        "every pending computation should go through the one method that reads the log"
    )
