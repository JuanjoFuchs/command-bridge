"""Spec 028 — a lane's hold does not take the same sentence twice.

**Measured, and it was the agent rather than the tool.** 2026-08-27, from
`sessions/dev.timing.jsonl`: two clip ids, identical text, eighty-five seconds apart, both held for
kepler and both played when he came back to that lane.

    "The Kepler agent just spoke twice to me."
    "I don't know if it's an issue of the agent or the voice tunnel."
    "Make the same turn repeated twice."

🎯 **The tool PRINTED the rule and did not enforce it, which is the whole finding.** The off-lane
hold response says *"Do not repeat it and do not say it another way"* — unusually explicit, naming
the failure and forbidding it in one breath — and an agent did it anyway, because **a sentence in a
JSON field is advice to a model, not a constraint on a system.**

⚠ The second cost is the one that justified fixing it in the tool rather than in an agent: he could
not tell whether his tunnel or his agent was broken, which makes every future report ambiguous.

🔴 **THESE DRIVE `handle_say`, NOT `_speak`.** The refusal lives in the HTTP handler beside the
unread and no-lane refusals, and a test that called `_speak` would step straight over it. Same rule
as `tests/test_lane_hold.py`: drive the real path or do not claim to.
"""
import asyncio

import pytest

from command_bridge import server


class _Sock:
    def __init__(self):
        self.headers = []
        self.blobs = []

    async def send_json(self, payload):
        self.headers.append(payload)

    async def send_bytes(self, data):
        self.blobs.append(data)


class _Req:
    def __init__(self, state, body):
        self.app = {"state": state}
        self.headers = {}
        self.query = {}
        self.remote = "127.0.0.1"
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("kepler")
    st.lanes.add("atlas")
    st.channel_open = True
    st.cues_enabled = False
    st.clients.add(_Sock())          # somebody is listening, so an off-lane clip is HELD not queued
    return st


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    def synth(text, voice=None, speed=None, pause=None):
        return (text.encode("utf-8") * 8), 22050

    monkeypatch.setattr(server.tts, "synthesize", synth)
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.0, raising=False)


def say(state, lane, text, now=False):
    """The REAL endpoint. He is on magnus throughout, so kepler and atlas are off-lane."""
    body = {"text": text, "lane": lane}
    if now:
        body["async"] = True
    resp = asyncio.run(server.handle_say(_Req(state, body)))
    import json
    return resp.status, json.loads(resp.body.decode("utf-8"))


def held_texts(state, lane):
    return [(c.get("header") or {}).get("text") for c in state.lane_held.get(lane, [])]


# ------------------------------------------------------------------ the defect


def test_the_same_sentence_is_refused_rather_than_queued_twice(state):
    """🔴 THE BUG. Kepler said it, waited, and said it again — and he heard both.

    **Verify by mutation:** remove the `_held_duplicate` check in `handle_say` and this goes red.
    """
    line = "Here it is. You were talking to someone else, so I held this."
    status, first = say(state, "kepler", line)
    assert status == 200 and first.get("held_off_lane") is True
    assert held_texts(state, "kepler") == [line]

    status, second = say(state, "kepler", line)

    assert status == 428, "the second copy must be refused, not queued"
    assert second["code"] == "duplicate_held"
    assert held_texts(state, "kepler") == [line], "and the hold must still contain exactly one"


def test_the_refusal_names_the_clip_it_duplicates(state):
    """FR2. An agent repeats itself because it believes the first call did not land — so the
    refusal has to show the clip is real and waiting, or the agent has no reason to stop."""
    line = "The verdict is confirmed."
    _, first = say(state, "kepler", line)
    status, refusal = say(state, "kepler", line)

    assert status == 428
    assert refusal["clip"] == first["id"], "it must point at the clip already waiting"
    assert refusal["waiting_s"] >= 0
    assert refusal["lane"] == "kepler"


def test_case_whitespace_and_a_changed_full_stop_are_still_the_same_sentence(state):
    """FR3. Told not to say it another way, the cheapest variations available are exactly these."""
    say(state, "kepler", "The verdict is confirmed.")

    for variant in ["the verdict is confirmed.",
                    "  The verdict is confirmed.  ",
                    "The verdict is confirmed",
                    "The verdict is confirmed!"]:
        status, body = say(state, "kepler", variant)
        assert status == 428, f"{variant!r} should have been caught as a duplicate"
        assert body["code"] == "duplicate_held"
    assert len(state.lane_held.get("kepler", [])) == 1


def test_a_genuinely_different_sentence_still_gets_through(state):
    """⚠ The half that stops this becoming a gag. Refusing a paraphrase would swallow an agent's
    second, DIFFERENT thought — a worse failure than the repetition being fixed."""
    say(state, "kepler", "The verdict is confirmed.")
    status, _ = say(state, "kepler", "And the second file failed for a different reason.")

    assert status == 200
    assert len(state.lane_held.get("kepler", [])) == 2


# ---------------------------------------------------------- what must keep working


def test_once_the_hold_is_flushed_the_same_words_are_allowed_again(state):
    """TC1. Saying something again later is ordinary speech. The tool has no business arguing
    with it — the refusal is about a duplicate WAITING, not about ever repeating."""
    line = "The verdict is confirmed."
    say(state, "kepler", line)
    asyncio.run(server._set_lane(state, "kepler", why="tap"))   # he comes back; the hold flushes
    assert not state.lane_held.get("kepler")

    state.lanes.switch("magnus")                                # and he leaves again
    status, _ = say(state, "kepler", line)

    assert status == 200, "the hold is empty, so this is a new thing to say"


def test_another_lane_holding_the_same_words_does_not_block_this_one(state):
    """TC2. Two agents can honestly reach the same sentence, and they are different voices."""
    line = "I could not reproduce it."
    say(state, "kepler", line)
    status, _ = say(state, "atlas", line)

    assert status == 200
    assert held_texts(state, "kepler") == [line]
    assert held_texts(state, "atlas") == [line]


def test_the_hurried_path_is_refused_once_its_clip_is_actually_held(state):
    """FR4, stated as what is actually true — and the first draft of this test caught the gap.

    ⚠ **`--now` RETURNS BEFORE SYNTHESIS**, so its clip reaches the hold a moment later. Two
    fire-and-forget calls issued in the same instant therefore both pass the check: the first has
    not landed when the second is tested. **That window is left open on purpose.** The failure this
    spec exists for was eighty-five seconds wide, agents call `say` sequentially rather than
    concurrently, and closing a millisecond race would mean reserving keys before synthesis —
    defensive handling for a scenario that has not happened, which his own spec rules say to cut.

    What IS guaranteed, and what this asserts: once the clip is in the hold, the hurried path is
    refused on exactly the same terms as the ordinary one.
    """
    import json

    async def scenario():
        line = "Are you still there?"
        first = await server.handle_say(_Req(state, {"text": line, "lane": "kepler",
                                                     "async": True}))
        assert first.status == 200
        # Let the fire-and-forget task it spawned actually reach the hold.
        for _ in range(50):
            await asyncio.sleep(0)
            if state.lane_held.get("kepler"):
                break
        assert state.lane_held.get("kepler"), "the clip must be held before this proves anything"

        second = await server.handle_say(_Req(state, {"text": line, "lane": "kepler",
                                                      "async": True}))
        return second.status, json.loads(second.body.decode("utf-8"))

    status, body = asyncio.run(scenario())
    assert status == 428
    assert body["code"] == "duplicate_held"
    assert len(state.lane_held.get("kepler", [])) == 1
