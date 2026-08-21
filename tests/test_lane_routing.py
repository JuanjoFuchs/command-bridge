"""Spec 012 Slice A — routing end to end, across the server and the CLI.

**AC-4** the turn carrying the wake word belongs to the NEW lane, instruction and all.
**AC-5** a turn with no wake phrase is stamped with the CURRENT lane and does not move it.
**AC-6** an ambiguous summons is addressed to nobody, stamped `lane: null`, and reaches no watch.
**AC-9** two waits on one lane are refused; two waits on DIFFERENT lanes both run (TC7).
**AC-10** `say --lane` into a lane that is not live refuses before synthesis.
**AC-11** a lane change ends the wait of the agent that just lost the conversation.

NO SERVER IS STARTED HERE, for the same reason spec 007's suite starts none: a live voice session
was running on `dev` throughout this work. The handlers are driven in-process against a
TunnelState of this test's own, on a turn log under tmp_path.
"""
import argparse
import asyncio
import json

import pytest

from voice_tunnel import cli, server, store
from voice_tunnel.lanes import BROADCAST


class _Req:
    """Only what the handlers touch — the same stub shape spec 007's suite uses."""

    def __init__(self, state, body):
        self.app = {"state": state}
        self._body = body
        self.remote = "127.0.0.1"
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "claude")
    st = server.TunnelState("t", token=None)
    st.consumed_cursor = -1
    # Rebuild the registry under the wake name this test pins; TunnelState read it at construction
    # time, and the fixture's monkeypatch may have landed after that in some orders.
    st.lanes = server.lanes_mod.LaneRegistry("claude")
    st.lanes.add("codex")
    # Identity is a separate gate with its own suite, so it is switched off here and these tests
    # measure routing only. `available` is a read-only property over the model path, so the path
    # is what gets pointed at nothing — deterministic whether or not a voiceprint is installed.
    st.embedder.model_path = str(tmp_path / "no-such-voiceprint.onnx")
    return st


async def _drive(state, completed):
    # The loop `_emit` runs ASR on must be the one it is awaited in, or `run_in_executor` builds a
    # future on a loop nobody is driving. Take the running one rather than making a second.
    await server._emit(state, completed, asyncio.get_running_loop())


def speak(state, text, t_start=0.0, t_end=1.0, session="t"):
    """Drive one utterance through the real gate-and-log path."""
    state.recognizer.transcribe = lambda _samples: text
    asyncio.run(_drive(state, (None, t_start, t_end)))
    return store.read_turns(session)[-1]


# ------------------------------------------------------------------ AC-4 / AC-5 / AC-6

def test_the_turn_carrying_the_wake_word_belongs_to_the_new_lane(state):
    """AC-4, FR3. One turn: it switches the lane AND delivers the instruction, and the
    instruction goes to the new agent. Switching on turn N and routing on N+1 would lose the
    sentence he actually cared about."""
    turn = speak(state, "hey codex run the tests")

    assert turn["lane"] == "codex"
    assert turn["addressed"] is True
    assert state.lanes.current == "codex"
    # The text is returned exactly as spoken — the wake phrase is never stripped.
    assert turn["text"] == "hey codex run the tests"


def test_a_continuation_stays_on_the_live_lane_without_moving_it(state):
    """AC-5, FR2. Stickiness is the whole feature: he names an agent about as often as he changes
    subject, not once per sentence."""
    speak(state, "hey codex run the tests", 0.0, 1.0)
    turn = speak(state, "and then deploy it", 2.0, 3.0)

    assert turn["lane"] == "codex"
    assert state.lanes.current == "codex", "a continuation must not move the conversation"


def test_an_ambiguous_summons_reaches_nobody(state):
    """AC-6, TC2. `cloud` is 0.73 against `claude` and 0.60 against `codex` — close to both,
    exactly nobody. The safe answer is to route it to NO ONE: delivering it to the live lane is
    the mis-route the refusal exists to prevent, since a summons is only ambiguous when it looks
    like an attempt to LEAVE that lane."""
    state.lanes.switch("codex")
    turn = speak(state, "hey cloud run the tests")

    assert turn["lane"] is None
    assert turn["addressed"] is False
    assert turn["reason"].startswith("ambiguous:")
    assert state.lanes.current == "codex", "a refusal must not move the conversation either"
    # And it reaches no lane's watch, which is the fact that matters.
    for who in ("claude", "codex"):
        kept, _ = store.turns_since("t", -1, lane=who, default_lane="claude")
        assert turn["id"] not in [t["id"] for t in kept]


def test_an_ambiguous_summons_wakes_the_live_lane_to_ask(state):
    """The refusal is announced by an AGENT, not by a sound — the repo froze the cue vocabulary at
    four and recorded that a fifth is his call. So the tunnel raises a counter the live lane's
    wait is watching, and that agent can ask which he meant."""
    before = state.ambiguous
    state.lanes.switch("codex")
    speak(state, "hey cloud run the tests")

    assert state.ambiguous == before + 1
    assert "claude" in state.last_ambiguous


def test_a_refused_summons_does_not_hold_the_conversation_window_open(state):
    """A turn nobody was given must not keep the window alive on their behalf, or the NEXT
    sentence would be addressed by a summons that was never resolved."""
    state.lanes.switch("codex")
    speak(state, "hey cloud run the tests", 0.0, 1.0)
    later = speak(state, "so anyway", 100.0, 101.0)      # far outside the 30 s window

    assert later["addressed"] is False


def test_a_single_lane_server_stamps_the_default_and_never_refuses(monkeypatch, tmp_path):
    """NFR3 at the server level. With one lane the switch and refuse rows cannot fire, so every
    one of these — including the tokens that would be ambiguous with two lanes — is addressed."""
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "claude")
    st = server.TunnelState("solo", token=None)
    st.embedder.model_path = str(tmp_path / "no-such-voiceprint.onnx")
    st.lanes = server.lanes_mod.LaneRegistry("claude")

    for i, text in enumerate(["hey claude status", "hey cloud status", "hey can you status",
                              "hey go ahead", "and then deploy"]):
        st.recognizer.transcribe = lambda _s, t=text: t
        asyncio.run(_drive(st, (None, i * 2.0, i * 2.0 + 1.0)))

    turns = store.read_turns("solo")
    assert [t["lane"] for t in turns] == ["claude"] * 5
    assert all(t["addressed"] for t in turns), "a single-lane session must never refuse"
    assert st.ambiguous == 0


def test_a_broadcast_is_stamped_for_everyone(state):
    turn = speak(state, "hey everyone stand down")

    assert turn["lane"] == BROADCAST
    for who in ("claude", "codex"):
        kept, _ = store.turns_since("t", -1, lane=who, default_lane="claude")
        assert turn["id"] in [t["id"] for t in kept]


# ------------------------------------------------------------------ AC-10: say

async def _spoke(*_a, **_k):
    """Stands in for synthesis on the paths that are SUPPOSED to reach it."""
    return {"queued": True, "id": "x"}

def test_say_into_a_lane_that_is_not_live_is_held_rather_than_played(state, monkeypatch):
    """AC-10, FR9 — **and this assertion was deliberately REVERSED between the two slices.**

    Slice A refused an off-lane `say` outright, which was correct while there was nowhere safe to
    put the audio: the one thing that must never happen is an agent talking over the conversation
    he is actually having. Slice B built the hold, and he asked for that in as many words — *"what
    it is saying would be queued up"*. A refusal makes the waiting agent's answer HIS problem to
    ask for again, which is the opposite of the point.

    What survives the reversal unchanged is the guarantee that actually matters: **it does not
    play.** That is asserted on the wire rather than on the return value.
    """
    monkeypatch.setattr(server.tts, "synthesize", lambda text, **k: (b"\x00\x01" * 64, 22050))
    sent = []
    monkeypatch.setattr(server, "_send_clip",
                        lambda *a, **k: sent.append(a) or asyncio.sleep(0))
    state.lanes.switch("codex")

    resp = asyncio.run(server.handle_say(_Req(state, {"text": "all green", "lane": "claude"})))
    payload = json.loads(resp.body.decode())

    assert resp.status == 200, "it is held, not an error — the agent did nothing wrong"
    assert payload["delivered"] is False
    assert payload["held_off_lane"] is True
    assert payload["reason"] == "off_lane"
    assert sent == [], "nothing may reach the wire while he is talking to somebody else"
    assert len(state.lane_held["claude"]) == 1, "and it is waiting, not discarded"


def test_say_into_the_live_lane_is_not_refused(state, monkeypatch):
    """The refusal must not go too far the other way: the agent he IS talking to can speak."""
    monkeypatch.setattr(server, "_speak", _spoke)
    state.lanes.switch("codex")

    resp = asyncio.run(server.handle_say(_Req(state, {"text": "all green", "lane": "codex"})))
    assert resp.status == 200


def test_say_with_no_lane_still_works(state, monkeypatch):
    """Every existing single-agent caller passes no lane and must be untouched."""
    monkeypatch.setattr(server, "_speak", _spoke)
    resp = asyncio.run(server.handle_say(_Req(state, {"text": "all green"})))
    assert resp.status == 200


def test_say_into_an_unknown_lane_names_the_real_ones(state, monkeypatch):
    monkeypatch.setattr(server, "_speak", _spoke)
    resp = asyncio.run(server.handle_say(_Req(state, {"text": "hi", "lane": "nobody"})))

    assert resp.status == 400
    assert '"code": "unknown_lane"' in resp.body.decode()


# ------------------------------------------------------------------ AC-9: one wait per lane

def _watch_args(**kw):
    ns = argparse.Namespace(session="t", since=-1, timeout=None, force=False,
                            all_turns=False, lane=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_two_waits_on_the_same_lane_are_refused(monkeypatch):
    """The guard is right and stays: two waits on one log race for the same turns, so one cursor
    silently falls behind."""
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {
        "running": True, "watch_open": True, "watching_lanes": ["claude"], "lane": "claude",
    })
    out = cli.cmd_watch(_watch_args(lane="claude"))

    assert out["reason"] == "watch_open"
    assert out["lane"] == "claude"
    assert "--lane claude" in out["next"], "the escape hatch must name the lane it applies to"


def test_two_waits_on_different_lanes_both_run(monkeypatch):
    """**TC7, and the reason the guard had to change at all.** N agents watching N lanes is the
    normal case here; a session-wide flag would refuse every agent after the first and the
    feature would not work for its second user."""
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {
        "running": True, "watch_open": True, "watching_lanes": ["claude"], "lane": "claude",
        "clients": 1, "consumed_cursor": -1,
    })
    monkeypatch.setattr(cli.store, "watch",
                        lambda *a, **k: ([], k.get("cursor", -1) if "cursor" in k else -1))
    out = cli.cmd_watch(_watch_args(lane="codex", timeout=0.01))

    assert out["reason"] != "watch_open", "codex must not be refused because claude is watching"


def test_a_server_too_old_to_report_lanes_falls_back_to_the_session_flag(monkeypatch):
    """ABSENT IS NOT FALSE. A server that predates lanes reports no `watching_lanes` at all, and
    reading that as 'nobody is watching' would let a second wait race the first — the exact
    mistake `watch_open` itself had to learn about."""
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {
        "running": True, "watch_open": True,      # no watching_lanes key at all
    })
    out = cli.cmd_watch(_watch_args(lane="codex"))

    assert out["reason"] == "watch_open"


# ------------------------------------------------------------------ AC-11: told it went off-lane

def test_a_lane_switch_ends_the_wait_of_the_agent_that_lost_it(monkeypatch):
    """AC-11. An agent that simply stopped receiving turns could not tell 'he is not talking' from
    'he is talking to someone else', and the whole operating discipline here is that an agent must
    never leave him talking to nobody."""
    polls = {"n": 0}

    def fake_request(_session, path, _payload=None):
        if path != "/status":
            return {}
        polls["n"] += 1
        # He starts on claude, then says "hey codex" and the live lane moves.
        return {"running": True, "clients": 1, "consumed_cursor": -1, "watch_open": False,
                "watching_lanes": [], "default_lane": "claude", "ambiguous": 0,
                "capturing": True, "channel_open": True, "muted": False,
                "lane": "claude" if polls["n"] < 3 else "codex"}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli.store, "watch",
                        lambda _s, cursor, **k: ([], cursor))
    out = cli.cmd_watch(_watch_args(lane="claude", timeout=5.0))

    assert out["reason"] == "lane"
    assert out["event"] == "lane"
    assert out["live_lane"] == "codex"
    assert out["on_lane"] is False, "he is talking to somebody else"
    assert out["listening"] is True, (
        "`listening` is about the MICROPHONE and it is still fine — losing the lane must not be "
        "reported as him being unhearable, or the agent tells him to check his orb"
    )
    assert "codex" in out["hint"]


def test_an_unroutable_summons_ends_the_live_lanes_wait(monkeypatch):
    """The other half: he said a name, nobody matched, and the agent he was talking to is the one
    who can sensibly ask which he meant."""
    polls = {"n": 0}

    def fake_request(_session, path, _payload=None):
        if path != "/status":
            return {}
        polls["n"] += 1
        return {"running": True, "clients": 1, "consumed_cursor": -1, "watch_open": False,
                "watching_lanes": [], "default_lane": "claude", "lane": "claude",
                "capturing": True, "channel_open": True, "muted": False,
                "ambiguous": 0 if polls["n"] < 3 else 1,
                "last_ambiguous": ["claude", "codex"]}

    monkeypatch.setattr(cli, "_request", fake_request)
    monkeypatch.setattr(cli.store, "watch", lambda _s, cursor, **k: ([], cursor))
    out = cli.cmd_watch(_watch_args(lane="claude", timeout=5.0))

    assert out["reason"] == "ambiguous"
    assert out["candidates"] == ["claude", "codex"]
    assert "which he meant" in out["hint"]


def test_a_single_agent_wait_never_sees_a_lane_event(monkeypatch):
    """NFR3 again, at the CLI. A caller that passes no lane must behave exactly as it does today,
    including when the server reports lane fields it has no opinion about."""
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {
        "running": True, "clients": 1, "consumed_cursor": -1, "watch_open": False,
        "watching_lanes": [], "default_lane": "claude", "lane": "claude", "ambiguous": 0,
    })
    monkeypatch.setattr(cli.store, "watch", lambda _s, cursor, **k: ([], cursor))
    out = cli.cmd_watch(_watch_args(timeout=0.01))

    assert out["reason"] == "quiet"
    assert "event" not in out
