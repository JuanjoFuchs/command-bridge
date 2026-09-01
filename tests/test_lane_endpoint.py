"""Spec 012 Slice A — the `/lane` endpoint and the lane fields on `/status`.

Separate from `test_lane_routing.py` because these are about the SURFACE — is it wired up, does a
refusal carry what an agent needs — rather than about which agent a turn reaches.

NO SERVER IS STARTED HERE. A live voice session was running on `dev` throughout this work, so the
handlers are driven in-process against a TunnelState of this test's own.
"""
import asyncio

import pytest

from command_bridge import server
from command_bridge.lanes import BROADCAST


class _Req:
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
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry("claude")
    st.lanes.add("codex")
    return st


def test_the_lane_route_is_registered(monkeypatch, tmp_path):
    """The handler being right is worth nothing if nothing reaches it. Asserted on the route
    table, because every other test here calls the handler directly and would pass just as well
    with the endpoint never wired up."""
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    app = server.build_app("t", token=None)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}

    assert "/lane" in paths


def test_the_endpoint_returns_the_whole_registry_on_every_call(state):
    """A caller that has to accumulate deltas to know the current lane set is a caller that will
    eventually disagree with the server."""
    resp = asyncio.run(server.handle_lane(_Req(state, {"action": "add", "name": "grok"})))
    body = resp.body.decode()

    assert resp.status == 200
    for expected in ('"lane"', '"lanes"', '"default_lane"', '"broadcast"', '"watching_lanes"'):
        assert expected in body


def test_the_endpoint_refuses_with_a_code_and_a_remedy(state):
    """AGENTS.md convention 8: an agent branches on `code` and runs `remedy`. A bare 400 makes it
    parse prose, which it cannot do reliably."""
    resp = asyncio.run(server.handle_lane(_Req(state, {"action": "add", "name": BROADCAST})))
    body = resp.body.decode()

    assert resp.status == 400
    assert '"code": "lane_exists"' in body
    assert '"remedy": "command-bridge ' in body


def test_an_unknown_lane_is_named_in_the_remedy(state):
    resp = asyncio.run(server.handle_lane(_Req(state, {"action": "switch", "name": "nobody"})))
    body = resp.body.decode()

    assert resp.status == 400
    assert '"code": "unknown_lane"' in body
    assert "claude" in body and "codex" in body, "the remedy must list the lanes that DO exist"


def test_switching_by_tap_moves_the_live_lane(state):
    """FR6's half that needs no page: he refused being REQUIRED to tap, not the tap itself."""
    asyncio.run(server.handle_lane(_Req(state, {"action": "switch", "name": "codex"})))
    assert state.lanes.current == "codex"


def test_a_tap_can_reach_the_broadcast_lane(state):
    asyncio.run(server.handle_lane(_Req(state, {"action": "switch", "name": BROADCAST})))
    assert state.lanes.current == BROADCAST


def test_removing_a_watched_lane_clears_its_wait(state):
    """A lane that is gone must not still hold the single-waiter slot, or its name could never be
    registered and watched again."""
    state.watching_lanes.add("codex")
    state.watch_open = True
    asyncio.run(server.handle_lane(_Req(state, {"action": "remove", "name": "codex"})))

    assert "codex" not in state.watching_lanes
    assert state.watch_open is False


def test_removing_the_live_lane_leaves_exactly_one_live_lane(state):
    """FR1 has to hold DURING a removal, not only before and after one."""
    state.lanes.switch("codex")
    asyncio.run(server.handle_lane(_Req(state, {"action": "remove", "name": "codex"})))

    assert state.lanes.current == "claude"


def test_an_unknown_action_is_refused_rather_than_treated_as_a_list(state):
    resp = asyncio.run(server.handle_lane(_Req(state, {"action": "destroy", "name": "codex"})))
    assert resp.status == 400
    assert state.lanes.names == ("claude", "codex")


def test_the_status_snapshot_always_carries_the_lane_fields(state):
    """Published even in a single-lane session. A field that appears only once a feature is in use
    is a field nothing can rely on — and `watch` has to tell 'no lanes here' from 'a server too
    old to have them', the same absent-versus-false distinction `watch_open` was bitten by."""
    snap = state.snapshot()

    for key in ("lane", "lanes", "default_lane", "watching_lanes", "ambiguous"):
        assert key in snap, f"{key} must be published unconditionally"
    assert snap["lane"] == "claude"
    assert snap["default_lane"] == "claude"


def test_a_watch_announcement_records_which_lane_is_waiting(state):
    """TC7 needs real per-lane data, and this is where it comes from."""
    asyncio.run(server.handle_watching(_Req(state, {"open": True, "lane": "codex"})))
    assert state.watching_lanes == {"codex"}
    assert state.watch_open is True

    asyncio.run(server.handle_watching(_Req(state, {"open": False, "lane": "codex"})))
    assert state.watching_lanes == set()
    assert state.watch_open is False


def test_a_watch_that_names_no_lane_is_recorded_against_the_default(state):
    """The single-agent caller passes nothing and is in fact watching the default lane, so that is
    what it gets recorded as — not a nameless slot that the per-lane guard cannot reason about."""
    asyncio.run(server.handle_watching(_Req(state, {"open": True})))
    assert state.watching_lanes == {"claude"}
