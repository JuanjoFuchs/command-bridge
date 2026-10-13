"""Spec 029: lanes reorder live, keep a colour of their own, and persist both."""
from __future__ import annotations

from typing import Any

import pytest

from command_bridge import colors, config, lanes, store


def _reg(*names: str) -> lanes.LaneRegistry:
    reg = lanes.LaneRegistry(names[0])
    for n in names[1:]:
        reg.add(n)
    return reg


# AC1 ---------------------------------------------------------------------------------------------

def test_reorder_puts_listed_first_and_keeps_the_rest_in_order():
    reg = _reg("magnus", "sirius", "atlas", "vega", "phoenix", "lyra")
    assert reg.reorder(["magnus", "vega", "sirius"]) == (
        "magnus", "vega", "sirius", "atlas", "phoenix", "lyra")


def test_reorder_can_move_the_default_lane():
    reg = _reg("magnus", "atlas", "vega")
    reg.reorder(["vega", "magnus"])
    assert reg.names == ("vega", "magnus", "atlas")
    assert reg.default == "magnus" and reg.current == "magnus"


@pytest.mark.parametrize("names,code", [(["magnus", "ghost"], "unknown_lane"),
                                        (["atlas", "atlas"], "bad_order")])
def test_a_refused_reorder_changes_nothing(names, code):
    reg = _reg("magnus", "atlas", "vega")
    with pytest.raises(lanes.LaneError) as exc:
        reg.reorder(names)
    assert exc.value.code == code
    assert reg.names == ("magnus", "atlas", "vega")


# AC4 / AC5 ---------------------------------------------------------------------------------------

ROSTER = {"magnus": "#3b6fe0", "atlas": "#2f9e4a", "vega": "#d0508f"}


def test_roster_agents_keep_the_roster_hue():
    got, assigned = colors.resolve(["magnus", "atlas"], ROSTER, {})
    assert got == {"magnus": "#3b6fe0", "atlas": "#2f9e4a"}
    assert assigned == {}


def test_an_unrostered_lane_gets_a_distinct_hue_that_sticks():
    got, assigned = colors.resolve(["magnus", "astra"], ROSTER, {})
    hue = got["astra"]
    # Clear of EVERY roster hue, including agents that are not up.
    assert all(colors.delta_e(hue, c) >= config.LANE_COLOR_MIN_DE for c in ROSTER.values())
    again, _ = colors.resolve(["astra", "magnus", "atlas", "vega"], ROSTER, assigned)
    assert again["astra"] == hue


def test_adding_a_lane_never_repaints_another():
    first, assigned = colors.resolve(["magnus", "astra"], ROSTER, {})
    second, assigned2 = colors.resolve(["magnus", "astra", "nebula"], ROSTER, assigned)
    assert second["astra"] == first["astra"]
    assert colors.delta_e(second["nebula"], second["astra"]) >= config.LANE_COLOR_MIN_DE
    assert set(assigned2) == {"astra", "nebula"}


def test_clashes_reports_only_close_pairs():
    near = colors.clashes({"phoenix": "#e0a03a", "sirius": "#c9a227", "magnus": "#3b6fe0"},
                          config.LANE_COLOR_MIN_DE)
    assert [c["lanes"] for c in near] == [["phoenix", "sirius"]]
    assert near[0]["delta_e"] < config.LANE_COLOR_MIN_DE


def test_assigned_colours_persist(tmp_path):
    store.write_lane_colors("dev", {"astra": "#123456"}, base=str(tmp_path))
    assert store.read_lane_colors("dev", base=str(tmp_path)) == {"astra": "#123456"}
    (tmp_path / "dev.lane_colors.json").write_text("not json", encoding="utf-8")
    assert store.read_lane_colors("dev", base=str(tmp_path)) == {}


# AC2 / AC3 ---------------------------------------------------------------------------------------

def test_order_survives_a_restart_including_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    from command_bridge import server
    st = server.TunnelState("t", token=None)
    st.lanes.add("atlas")
    st.lanes.add("vega")
    st.lanes.reorder(["vega", st.lanes.default, "atlas"])
    store.write_lanes("t", st.lanes.names)
    again = server.TunnelState("t", token=None)
    assert again.lanes.names == ("vega", st.lanes.default, "atlas")


def test_the_lane_endpoint_orders_live_and_reports_colours(tmp_path, monkeypatch):
    """AC2 — `/lane order` reorders, persists, publishes, and leaves the live lane alone."""
    import asyncio
    import json

    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    from command_bridge import server

    class _Req:
        def __init__(self, state, body):
            self.app = {"state": state}
            self._body = body
            self.remote = "127.0.0.1"
            self.query = {}
            self.headers = {}

        async def json(self):
            return self._body

    st = server.TunnelState("t", token=None)
    st.lanes.add("atlas")
    st.lanes.add("vega")
    st.lanes.current = "atlas"
    st.watching_lanes.add("vega")
    sent: list = []

    async def _bc(_state, msg):
        sent.append(msg)

    monkeypatch.setattr(server, "_broadcast_json", _bc)
    req: Any = _Req(st, {"action": "order", "names": ["vega", "atlas"]})
    resp = asyncio.run(server.handle_lane(req))
    body = json.loads(resp.text or "{}")
    assert resp.status == 200
    assert body["lanes"][:2] == ["vega", "atlas"] and body["lane"] == "atlas"
    assert body["watching_lanes"] == ["vega"]
    assert set(body["colors"]) == set(body["lanes"]) and "color_clashes" in body
    assert store.read_lanes("t")[:2] == ["vega", "atlas"]
    assert any(m.get("type") == "lane" and m.get("why") == "order" for m in sent)
    bad_req: Any = _Req(st, {"action": "order", "names": ["ghost"]})
    bad = asyncio.run(server.handle_lane(bad_req))
    assert bad.status == 400 and json.loads(bad.text or "{}")["code"] == "unknown_lane"
