"""The acknowledgement cue means acknowledgement. Spec 007, FR3, AC11-AC14.

JJ, 2026-08-19: *"Whenever you hear something that you are not going to acknowledge, it doesn't
make sense to reproduce the sound as if you heard me, as if you are acknowledging me."*

**THE CUE WAS NOT WHERE ANYONE THOUGHT IT WAS.** `heard` did not fire when a turn was read. It
fired at the end of the turn-logging path — the instant ASR finished — **before any agent had seen
the turn and whether or not one was even listening.** So its real meaning was "your words were
captured and written down", and it asserted acknowledgement for every utterance in the room,
including the ones no agent would ever answer.

It now follows the agent's INTENT TO RESPOND, carried on the read signal. The point is not the
sound; it is the SILENCE. A cue that fires on capture cannot mean anything by not firing, so the
one case JJ actually reported — heard, and deliberately not answered — was unexpressible.

The cues are driven through the real `_push_cue` and the real renderer, with only the socket
stubbed, so a cue that stopped rendering would fail here rather than passing as "not sent".

NO SERVER IS STARTED (spec 007, TC5).
"""
import asyncio
import types

import numpy as np
import pytest

from command_bridge import cues, server, store


class _Client:
    """A socket that records instead of sending. `_push_cue` refuses to render with no client
    connected — correctly, since a cue nobody can hear is wasted work — so a test about which cues
    fire has to have somebody listening."""

    def __init__(self):
        self.json = []
        self.bytes = []

    async def send_json(self, payload):
        self.json.append(payload)

    async def send_bytes(self, data):
        self.bytes.append(data)


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None, gate_enabled=False)
    st.consumed_cursor = -1
    st.clients.add(_Client())
    return st


def cues_played(state):
    """Every cue that reached the wire, in order. Read off the audio headers, which is what the
    page itself branches on — not off a stubbed `_push_cue`, so the renderer stays in the path."""
    client = next(iter(state.clients))
    return [m["cue"] for m in client.json if m.get("cue")]


class _Req:
    def __init__(self, state, body):
        self.app = {"state": state}
        self._body = body
        self.remote = "127.0.0.1"
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


def transcribed(st, monkeypatch, text):
    """Put one utterance through the REAL transcribe-gate-log path, with ASR and the voiceprint
    stubbed out. This is the path that used to end in an acknowledgement cue.

    The loop is taken from inside the coroutine, not built beside it: `_emit` hands work to an
    executor through the loop it is given, and a loop that is not the running one fails with
    "attached to a different loop" — which says nothing about cues and everything about plumbing.
    """
    monkeypatch.setattr(st.recognizer, "transcribe", lambda samples: text)
    # Swapped wholesale rather than patching `available`, which is a read-only property derived
    # from whether a 600 MB checkpoint is on this machine — so on a developer box with the model
    # installed this path would run the real embedder, and on CI it would not. Neither answer is
    # about cues.
    monkeypatch.setattr(st, "embedder", types.SimpleNamespace(available=False))

    async def _drive():
        await server._emit(st, (np.zeros(1600, dtype=np.float32), 0.0, 1.0),
                           asyncio.get_running_loop())

    asyncio.run(_drive())


def read_to(st, cursor, **body):
    """The agent's read signal: `/consumed`, optionally carrying an intent.

    The parameter is `st`, not `state`, because the intent field on the wire is itself called
    `state` — `read_to(st, 5, state="idle")` has to reach the body, not the fixture."""
    return asyncio.run(server.handle_consumed(_Req(st, {"cursor": cursor, **body})))


# ------------------------------------------------------- AC11: not on capture


def test_no_acknowledgement_when_a_turn_is_merely_logged(state, monkeypatch):
    """AC11. **This is the line that was wrong.** The cue fired here, one statement after the turn
    was appended, so it went out before any agent had seen the turn — and went out just the same
    when no agent was listening at all.

    Driven through the real transcribe-gate-log path rather than asserted on the source, because
    the requirement is about what reaches the phone."""
    transcribed(state, monkeypatch, "hey claude, are you there")

    assert store.last_turn_id(state.session) == 0, "the turn must still be logged — only the sound moved"
    assert cues_played(state) == [], (
        "capture is not acknowledgement: nobody has read this yet, and nobody may ever answer it"
    )


def test_the_turn_still_reaches_the_page_so_the_confirmation_is_not_lost(state, monkeypatch):
    """WHAT PAYS FOR AC11. Removing the capture cue removes his only AUDIBLE confirmation that the
    tunnel heard him, and that cost is real — on a phone in a pocket there is now silence between
    his last word and the agent deciding.

    What makes it acceptable is that the visual confirmation is untouched: the turn is broadcast
    into the transcript the moment it is logged, over the same socket. This pins that, because if
    it ever stopped being true the trade would silently become a bad one."""
    transcribed(state, monkeypatch, "can you hear me")

    painted = [m for m in next(iter(state.clients)).json if m.get("type") == "turn"]
    assert [m["text"] for m in painted] == ["can you hear me"]


# --------------------------------------- AC12/AC13: the intent, both arms


def test_it_sounds_when_the_read_signal_says_an_answer_is_coming(state):
    """AC12. The read signal has always carried a `state` — what the agent is doing now that it
    has the turn — and `thinking` is an answer being composed. That is the acknowledgement."""
    read_to(state, 3)

    assert "heard" in cues_played(state)


def test_it_is_silent_when_the_agent_reads_and_will_not_respond(state):
    """**AC13, THE NEGATIVE CONTROL, and the case JJ actually reported.**

    Without an arm that fires ONLY here, AC11 and AC12 both pass with the cue simply moved from
    "every capture" to "every read" — which would be the same defect one layer along, since he
    would still hear acknowledgement for turns nobody is going to answer.

    `state: "idle"` is the agent saying it has read this and is going back to listening. No sound:
    the silence is the message, and it is the thing the old cue could never say."""
    read_to(state, 3, state="idle")

    assert cues_played(state) == [], "a sound here claims an answer that is not coming"


def test_the_absence_is_the_information(state):
    """The two arms in one place, because the property is a CONTRAST and neither half states it
    alone: the same command, the same cursor, one field apart, and he can tell them by ear."""
    responding = server.TunnelState(state.session, token=None)
    responding.clients.add(_Client())
    silent = server.TunnelState(state.session, token=None)
    silent.clients.add(_Client())

    read_to(responding, 5)
    read_to(silent, 5, state="idle")

    assert "heard" in cues_played(responding)
    assert "heard" not in cues_played(silent)


def test_a_read_that_moves_nothing_acknowledges_nothing(state):
    """A repeated `consumed` at a cursor already reached has no turn behind it, and a cue for it
    would be a sound with nothing to acknowledge — the capture defect wearing a different hat."""
    read_to(state, 3)
    assert cues_played(state).count("heard") == 1

    read_to(state, 3)

    assert cues_played(state).count("heard") == 1, (
        "nothing new was read, so there is nothing new to acknowledge"
    )


# ------------------------------------------------- AC14: the other three


def test_the_thinking_cue_is_unaffected(state):
    """TC4. `thinking` answers a different question — not "did you get that" but "are you still
    working" — and this spec changes the acknowledgement cue only."""
    read_to(state, 1)

    assert "thinking" in cues_played(state)


def test_the_speaking_cue_still_fires_from_the_say_path(state, monkeypatch):
    """TC4. It is the one that tells him to stop talking because a reply is starting, so it is the
    cue whose loss would be least visible and most costly.

    ⚠ **THE CLIP HAS TO BE DELIVERABLE NOW.** The cue moved next to the send on 2026-08-25: it
    announces that audio is STARTING, and `_speak` does not always start any — a clip for a lane he
    is not on is held, and one with nobody connected is queued. This fixture previously had no
    client at all, so it was asserting that a sound plays into a room with no listener, and it kept
    passing only because the cue fired before the branch that decides.
    """
    monkeypatch.setattr(server.tts, "synthesize",
                        lambda text, voice=None, speed=1.0, pause=0.0: (b"\x00\x00" * 100, 22050))
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.0)
    # ⚠ `_send_clip` IS NOT STUBBED, and must not be: a cue is itself sent through it, so replacing
    # it silences the very thing this test measures. `_Client` receives real sends.
    #
    # ONLY THE CHANNEL. The fixture already put a `_Client` in the set, and `cues_played` reads
    # `next(iter(state.clients))` — so adding a second one made the assertion read whichever the
    # set happened to yield first, which is not the one the cue went to. What was actually missing
    # is `channel_open`: `deliverable` is `clients AND channel_open AND not off_lane`, and this
    # fixture never opened the channel because nothing used to depend on it.
    state.channel_open = True

    asyncio.run(server._speak(state, "here you go", None))

    assert "speaking" in cues_played(state)


def test_the_tool_cue_is_untouched_and_the_vocabulary_is_still_four(state):
    """TC4 froze the vocabulary at four sounds. FR3 moves one of them; it adds none, and a fifth
    (a quieter capture tick, to buy back the liveness signal) is his call, not the implementer's."""
    asyncio.run(server.handle_cue(_Req(state, {"name": "tool"})))

    assert cues_played(state) == ["tool"]
    assert set(cues.CUES) == {"heard", "thinking", "tool", "speaking"}


def test_the_documented_meaning_matches_when_it_now_fires(state):
    """A sound nobody can explain is noise, and the meaning table said "arrived and was read" for a
    cue that fired on neither event an agent controls. It has to say what the sound now means, or
    the next person to touch this reasons from the wrong premise exactly as this spec's own
    metaspec did."""
    meaning = cues.CUE_MEANING["heard"]

    assert "answering" in meaning
    assert "arrived" not in meaning
