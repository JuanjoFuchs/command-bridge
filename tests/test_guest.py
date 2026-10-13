"""Spec 042 — guest mode: on a guest lane, a voice that is not his still reaches the agent.

Observed 2026-10-09 with a second person talking to an agent on the `genie` lane: "they just spoke on
that turn, but Command Bridge did not recognize them because it's tuned to my voice." Their turns
were all `addressed:false` (`not-owner:0.00` / `not-addressed`), so `watch --lane genie` never woke.
"""
import asyncio
import json
import pathlib
from typing import Any

import pytest

from command_bridge import guest, server, store

SERVER = pathlib.Path(server.__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("reason", ["not-owner:0.00", "not-addressed"])
def test_a_non_owner_turn_on_a_guest_lane_is_addressed_and_tagged(reason):
    assert guest.admit(False, reason, "genie", {"genie"}) == (True, "guest", "guest")


def test_other_lanes_are_unchanged():
    assert guest.admit(False, "not-owner:0.00", "magnus", {"genie"}) == (False, "not-owner:0.00", "")


@pytest.mark.parametrize("reason", ["echo", "ambiguous", "summons"])
def test_echo_and_refusals_stay_refused(reason):
    """An agent's own clip coming back is still dropped, and a refused summons still goes nowhere."""
    assert guest.admit(False, reason, "genie", {"genie"}) == (False, reason, "")


def test_his_own_turns_are_untouched():
    assert guest.admit(True, "voice:0.81", "genie", {"genie"}) == (True, "voice:0.81", "")


def test_guest_lanes_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    assert guest.load("t") == set()
    guest.save("t", {"genie"})
    assert guest.load("t") == {"genie"}
    guest.save("t", set())
    assert guest.load("t") == set()


def test_a_guest_turn_is_logged_with_its_speaker(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    t = store.append_turn("t", "I wish for diamonds", 0, 1, True, reason="guest", lane="genie",
                          stamp_lane=True, speaker="guest")
    assert t["speaker"] == "guest"
    plain = store.append_turn("t", "hello", 0, 1, True, reason="wake")
    assert "speaker" not in plain              # his turns keep their old shape


def test_the_server_applies_it_after_routing_and_logs_the_speaker():
    i_route = SERVER.index("lane = routing.lane")
    i_guest = SERVER.index("guest.admit(", i_route)
    assert i_guest < SERVER.index("turn = store.append_turn(", i_route)
    assert "speaker=guest_speaker" in SERVER[i_guest:SERVER.index("state.turns_logged += 1", i_guest)]
    # ...and a guest saying an agent's name never moves his conversation.
    assert "moved = state.lanes.apply(routing) if (addressed and not guest_speaker) else False" in SERVER


class _Req:
    def __init__(self, state, body):
        self.app = {"state": state}
        self.remote = "127.0.0.1"
        self.headers, self.query, self.cookies = {}, {"token": "tok"}, {}
        self._body = body

    async def json(self):
        return self._body


def test_guest_on_off_through_the_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token="tok")
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("genie")

    def post(body) -> Any:
        r: Any = asyncio.run(server.handle_guest(_Req(st, body)))   # type: ignore[arg-type]
        return r.status, json.loads(r.body)

    assert post({"lane": "genie", "on": True}) == (200, {"guest_lanes": ["genie"]})
    assert st.snapshot()["guest_lanes"] == ["genie"]
    assert server.TunnelState("t", token="tok").guest_lanes == {"genie"}
    assert post({"lane": "nobody", "on": True})[0] == 400
    assert post({"lane": "genie", "on": False}) == (200, {"guest_lanes": []})
