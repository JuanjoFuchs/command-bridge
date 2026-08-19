"""Frames stopping means speech stopped — the prerequisite the speaking-gated wait rests on.

**Under the old drain, a signal that lied about muting produced a wrong narration**: the agent
announcing it was waiting for someone whose microphone was switched off. Live, 2026-08-01:
*"whenever I mute, you say that you're listening and you're waiting for me to finish, but I'm
muted."* Annoying, and self-correcting the moment he unmuted.

**Under spec 005's wait the same lie HANGS THE TUNNEL**, because the wait never observes him
stopping. That is why the owner refused to route around it — *"It seems that we should fix the
flag, right?"* — and why the fix is at the source rather than in the CLI.

It is in two halves, because there are two failure shapes:

* **events flush** — mute, orb off / capture released, channel close and a dropped socket each end
  the utterance, so nothing said before them is silently eaten;
* **the published flag is guarded on frame recency** — for everything nobody tells us about:
  frames still in flight when a mute lands and re-opening the buffer after the flush, an Android
  tab suspended in the background with its socket alive, a wedged page holding a live connection
  and sending nothing.

The first alone leaves races; the second alone loses audio.
"""
import pathlib
import time

import numpy as np
import pytest

from voice_tunnel import config, server

SERVER = pathlib.Path(__file__).resolve().parents[1] / "voice_tunnel" / "server.py"


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    # No turn model and no recognizer work: this file is about the SIGNALS, and loading an 8 MB
    # ONNX session here would make every one of these tests depend on a download.
    monkeypatch.setattr(config, "turn_detect_enabled", lambda: False)
    return server.TunnelState(session="sig", token=None, gate_enabled=True)


def _speech(seconds=0.5, sr=None):
    """Loud enough to clear the measured noise floor — quiet audio reads as silence by design."""
    sr = sr or config.TARGET_SR
    t = np.arange(int(sr * seconds), dtype=np.float32) / sr
    return (0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


def _talking(state):
    """Put the state into "a page is open, capturing, and an utterance is in progress"."""
    state.clients.add(object())
    state.capturing = True
    state.last_audio_at = time.monotonic()
    state.buffer.feed(_speech())
    assert state.buffer.speech_active, "fixture is wrong: no utterance is open"
    return state


# ------------------------------------------------------------------ AC1: muted


def test_a_muted_client_reports_not_speaking(state):
    """AC1. The buffer still holds an open utterance — that is the point. Nothing further will
    arrive to close it, so the flag would stick true forever if it were published raw."""
    _talking(state)
    state.user_speaking = True
    state.muted = True

    signals = state.speaking_now()
    assert signals["speech_active"] is False
    assert signals["user_speaking"] is False
    assert state.talking() is False
    assert state.snapshot()["speech_active"] is False


# --------------------------------------------------------- AC2: frames stopped


def test_frames_stopping_reads_as_speech_stopping(state):
    """AC2. The window is END_OF_UTTERANCE_MS and is NOT a new constant: it is exactly the silence
    that would have closed the utterance had frames kept coming. So the rule is the segmenter's
    own rule applied to the case where the audio simply stopped."""
    _talking(state)
    state.user_speaking = True

    assert state.talking() is True, "with frames current he IS talking"

    state.last_audio_at = time.monotonic() - (config.END_OF_UTTERANCE_MS / 1000.0) - 0.5
    signals = state.speaking_now()
    assert signals["speech_active"] is False
    assert signals["user_speaking"] is False


def test_a_frame_gap_shorter_than_the_window_still_reads_as_speaking(state):
    """The guard must not fire between ordinary audio frames, which arrive tens of times a second.
    Half the window is still comfortably inside a live stream."""
    _talking(state)
    state.last_audio_at = time.monotonic() - (config.END_OF_UTTERANCE_MS / 2000.0)

    assert state.speaking_now()["speech_active"] is True


# ------------------------------------------- AC3: the events that stop frames flush


@pytest.mark.parametrize("kind", ["capturing", "channel", "muted"])
def test_every_event_that_stops_frames_flushes_the_buffer(kind):
    """AC3. Since 2026-08-16 switching the orb off RELEASES the microphone, so releasing capture
    became the most common way to stop talking — and it was the one path that left a half-utterance
    open with no further frame able to close it.

    Asserted against the source rather than by driving asyncio, because what must not regress is
    that each branch CALLS the flush; the flush itself is covered by its own tests.
    """
    src = SERVER.read_text(encoding="utf-8")
    body = src[src.index("async def _on_control"):src.index("\nasync def ", src.index(
        "async def _on_control") + 10)]
    branch = body[body.index(f'kind == "{kind}"'):]
    end = branch.find('elif kind ==', 10)
    branch = branch[:end] if end > 0 else branch

    assert "_flush(" in branch, (
        f"the `{kind}` branch stops audio arriving, so it must end the utterance — otherwise the "
        f"words spoken just before it are held forever and the flag sticks true"
    )


def test_a_flush_empties_the_buffer_so_the_flag_goes_false(state):
    """The mechanism the branches above rely on. `flush` resets in BOTH of its paths — the one
    that emits a turn and the one that discards a too-short fragment — which is what makes
    "flush on the way out" a complete answer rather than a usual-case one."""
    _talking(state)
    state.buffer.flush()

    assert state.buffer.speech_active is False


# -------------------------------------------------- AC4: nobody there is not speaking


def test_no_client_means_nobody_is_speaking(state):
    """AC4. `user_speaking` is set only by a client message, and a socket that dies mid-word never
    sends the matching `speaking: false` — so the flag stayed true for the life of the server.
    Harmless while it only drove a hold loop a reconnect would clear; fatal once a wait gates on
    it, because the wait would hold forever on a page that no longer exists."""
    _talking(state)
    state.user_speaking = True
    state.clients.clear()

    signals = state.speaking_now()
    assert signals["speech_active"] is False
    assert signals["user_speaking"] is False
    assert signals["speech_pending"] == 0


def test_capture_released_means_nobody_is_speaking(state):
    """A page can be connected with the microphone released — that is what the orb being off now
    means — and a connected client is not a listening one."""
    _talking(state)
    state.capturing = False

    assert state.talking() is False


def test_the_disconnect_path_clears_the_client_signal():
    """The other half of AC4, asserted where it lives: the websocket handler's `finally`."""
    src = SERVER.read_text(encoding="utf-8")
    tail = src[src.index("        state.clients.discard(ws)"):]
    tail = tail[:tail.index("async def _on_control")]

    assert "state.user_speaking = False" in tail, (
        "the last page going away must clear the client's speaking flag, or it stays true forever"
    )


# ------------------------------------------------- speech_pending, the third signal


def test_speech_pending_counts_and_is_published(state):
    """The invariant made observable — his words: *"the gist is making sure that there's any
    speech drained before you speak."* Non-zero means he has spoken and nobody has the words yet,
    which both booleans read as quiet."""
    state.clients.add(object())
    state.capturing = True
    state.last_audio_at = time.monotonic()

    assert state.speaking_now()["speech_pending"] == 0
    assert state.talking() is False

    state.speech_pending = 1
    assert state.talking() is True, "an utterance in transcription is speech nobody has heard"
    assert state.snapshot()["speech_pending"] == 1


def test_pending_is_raised_before_the_first_await():
    """The ordering is the whole point. `_emit` awaits the recognizer, so `/status` IS served
    between the buffer emptying and the turn reaching the log; counting after the await would
    leave the window it exists to close wide open."""
    src = SERVER.read_text(encoding="utf-8")
    body = src[src.index("async def _on_audio"):]
    raise_at = body.index("state.speech_pending += 1")
    emit_at = body.index("await _emit(")

    assert raise_at < emit_at, "increment before the await, not after it"


def test_the_status_snapshot_publishes_all_three_from_one_place(state):
    """One place, one answer. `_speak`'s hold loop and `status` used to compute this separately —
    the first knowing about mute and the second not — which is why the CLI carried a caveat
    telling every reader to apply the exception by hand."""
    state.clients.add(object())
    state.capturing = True
    state.last_audio_at = time.monotonic()
    snap = state.snapshot()

    for field in ("speech_active", "user_speaking", "speech_pending"):
        assert field in snap
        assert snap[field] == state.speaking_now()[field]


def test_the_hold_loop_reads_the_same_answer_as_status():
    """The two copies are what produced the caveat. `_speak` must go through `state.talking()`."""
    src = SERVER.read_text(encoding="utf-8")
    speak = src[src.index("    waited = 0.0"):src.index('await _set_agent_state(state, "speaking")')]

    assert "state.talking()" in speak
    assert "state.user_speaking or state.buffer.speech_active" not in speak, (
        "that is the second copy of the test, and it is the one that did not know about mute"
    )


# ------------------------------- what `say` hands back, and what it must not touch


def test_unread_turns_are_those_past_the_read_cursor(state, tmp_path):
    """The set is defined by the READ CURSOR, not by anything the caller passes — so an agent
    cannot ask the wrong question, and there is no flag to get wrong."""
    from voice_tunnel import server as srv
    from voice_tunnel import store

    for i in range(4):
        store.append_turn(session=state.session, text=f"t{i}", t_start=float(i), t_end=i + 1.0,
                          addressed=True, reason="wake")
    state.consumed_cursor = 1

    got = srv._unread_turns(state)
    assert [t["id"] for t in got["unread"]] == [2, 3]
    assert got["unread_count"] == 2
    assert got["cursor"] == 3, "the caller can resume from here without tracking it"


def test_unread_never_advances_the_read_cursor(state):
    """THE DESIGN DECISION WORTH DEFENDING. `watch` consumes what it delivers, because delivering
    IS the acknowledgement. `say` is not the reading loop — it is a WARNING that the reading loop
    was skipped, so the next `watch` must still return these turns.

    Two reasons: an agent that ignores the field then loses nothing (the failure mode is a
    re-read, where consuming here would silently drop words), and the page's "read to here"
    divider would otherwise jump on a REPLY — he has not been read, he has been answered."""
    from voice_tunnel import server as srv
    from voice_tunnel import store

    for i in range(3):
        store.append_turn(session=state.session, text=f"t{i}", t_start=float(i), t_end=i + 1.0,
                          addressed=True, reason="wake")
    state.consumed_cursor = -1

    srv._unread_turns(state)
    assert state.consumed_cursor == -1, "reading it must not move the boundary"
    assert srv._unread_turns(state)["unread_count"] == 3, "and it is still there next time"


def test_unread_is_bounded_so_a_reply_cannot_carry_the_whole_log(state):
    """It rides on the reply path. An agent that was away while he kept talking must not be handed
    an hour of log inside a `say` response."""
    from voice_tunnel import config as cfg
    from voice_tunnel import server as srv
    from voice_tunnel import store

    for i in range(cfg.UNREAD_ON_SAY_MAX + 12):
        store.append_turn(session=state.session, text=f"t{i}", t_start=float(i), t_end=i + 1.0,
                          addressed=True, reason="wake")
    state.consumed_cursor = -1

    got = srv._unread_turns(state)
    assert got["unread_count"] == cfg.UNREAD_ON_SAY_MAX
    assert got["unread"][-1]["text"] == f"t{cfg.UNREAD_ON_SAY_MAX + 11}", "newest kept, not oldest"


def test_turns_the_wake_gate_rejected_are_not_handed_back(state):
    """A room talking around him must not surface as "you spoke without reading this" — the same
    reason `--all-turns` is off by default on the wait."""
    from voice_tunnel import server as srv
    from voice_tunnel import store

    store.append_turn(session=state.session, text="for him", t_start=0.0, t_end=1.0,
                      addressed=True, reason="wake")
    store.append_turn(session=state.session, text="someone else", t_start=1.0, t_end=2.0,
                      addressed=False, reason="not-owner:0.04")
    state.consumed_cursor = -1

    got = srv._unread_turns(state)
    assert [t["text"] for t in got["unread"]] == ["for him"]


def test_say_samples_unread_before_synthesis():
    """The ordering is what puts it on the `--now` path, which is the one an agent takes when it
    is in a hurry — which is when the check gets skipped."""
    src = SERVER.read_text(encoding="utf-8")
    body = src[src.index("async def handle_say"):src.index("async def _speak")]

    assert body.index("_unread_turns(state)") < body.index("if fire_and_forget:"), (
        "sample it before the early return, or the hurried path is the one path without it"
    )
    fire = body[body.index("if fire_and_forget:"):]
    assert "**unread" in fire, "the fire-and-forget response must carry it too"
