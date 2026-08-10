"""Replies said to a phone that had gone to sleep.

Android suspends a backgrounded tab, so the socket drops every time the screen locks — and
`/say` used to answer 409 and throw the reply away. Over one live session that silently ate
several answers: the owner asked a question, got nothing back, and asked it again. Twice he asked the
same question three times.

Live, 2026-08-01: "whenever the client is disconnected or you detect that my phone is locked
or whatever, you should queue up your reply back to me. So whenever I reconnect, they all send
down to me."

The bounds are as much the feature as the queue is — see UNDELIVERED_MAX / _MAX_AGE_S.
"""
import asyncio
import time

import pytest

from voice_tunnel import config, server


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    return server.TunnelState("t", token=None)


def queue(state, n, at=None):
    for i in range(n):
        state.undelivered.append({
            "header": {"id": f"clip-{i}", "type": "audio_header"},
            "pcm": b"\x00\x00",
            "at": time.time() if at is None else at,
        })


def test_nothing_is_queued_while_someone_is_listening(state):
    """The queue is a fallback, not the normal path."""
    assert state.undelivered == []


def test_pruning_drops_the_oldest_beyond_the_cap(state):
    queue(state, config.UNDELIVERED_MAX + 4)

    server._prune_undelivered(state)

    assert len(state.undelivered) == config.UNDELIVERED_MAX
    # The NEWEST survive. Reconnecting and hearing the four stalest answers, with the four most
    # recent dropped, would be the worst possible truncation.
    assert state.undelivered[-1]["header"]["id"] == f"clip-{config.UNDELIVERED_MAX + 3}"


def test_anything_older_than_the_window_is_dropped(state):
    queue(state, 3, at=time.time() - config.UNDELIVERED_MAX_AGE_S - 30)
    queue(state, 1)

    server._prune_undelivered(state)

    assert len(state.undelivered) == 1, "stale replies must not survive the window"


def test_the_bounds_are_tight_enough_to_be_a_conversation(state):
    """Guards the *reason* for the numbers. Unbounded, the fix is worse than the bug: come back
    after an hour and get read a stack of answers to questions you stopped caring about."""
    assert config.UNDELIVERED_MAX <= 10
    assert config.UNDELIVERED_MAX_AGE_S <= 900


def test_flushing_delivers_oldest_first_and_empties_the_queue(state, monkeypatch):
    sent = []

    async def fake_send(_state, header, pcm):
        sent.append(header["id"])

    monkeypatch.setattr(server, "_send_clip", fake_send)
    queue(state, 3)

    # asyncio.run rather than a pytest-asyncio marker: this suite has no async plugin, and adding
    # a dependency to await two coroutines is a worse trade than three lines here.
    delivered = asyncio.run(server._flush_undelivered(state))

    assert delivered == 3
    assert sent == ["clip-0", "clip-1", "clip-2"], "replies must arrive in the order they were said"
    assert state.undelivered == [], "a delivered clip must not be delivered twice"


def test_a_flushed_clip_says_how_long_it_waited(state, monkeypatch):
    """A reply arriving 90 seconds after the question, with no sign that time passed, reads as the
    agent being slow rather than the phone having been asleep."""
    sent = []

    async def fake_send(_state, header, pcm):
        sent.append(header)

    monkeypatch.setattr(server, "_send_clip", fake_send)
    queue(state, 1, at=time.time() - 45)

    asyncio.run(server._flush_undelivered(state))

    assert sent[0]["delayed_s"] >= 44
