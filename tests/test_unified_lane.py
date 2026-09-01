"""Spec 004 — the voice lane and the canvas lane are ONE lane.

Before the merge the canvas kept a shadow copy of the live lane and a `Follower` polled the voice
server's `/status.lane` to keep it in step. Now that spec 003 put both halves in one server, the
switch drives the canvas directly, in-process, and the poll is off. These tests pin that:

- **FR1** — the ONE place the voice live lane moves (`_set_lane`, reached by the tap and the wake
  gate alike) also sets the canvas live lane, synchronously, in the same call. No poll interval
  elapses, so a test with no `sleep` proves it.
- **FR2** — the merged server starts NO `/status`-polling follower. The `Follower` code stays for
  the standalone-canvas case, but `init_canvas(follow=False)` must not spawn its thread.

FR3 (a frame drawn to lane X shows when the voice switches to X) is verified live with
`command-bridge shot` — a real browser renders the frame — per AC3; it is the atlas-frame capture in
the spec's evidence, not a unit test.

NO SERVER IS STARTED HERE. The handlers are driven in-process against a TunnelState of this test's
own, the same way `test_lane_endpoint.py` does.
"""
import asyncio

import pytest

from command_bridge import server
from command_bridge.canvas import aio as canvas_aio
from command_bridge.canvas import server as canvas


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
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("atlas")
    return st


def test_a_voice_lane_switch_drives_the_canvas_in_process(state):
    """FR1 / AC1. The voice switch and the canvas move in the SAME call. The canvas is parked on a
    third lane first, so a pass proves the switch MOVED it — not that it was already there — and
    there is no `sleep`, so it proves no poll interval was needed."""
    canvas.set_live("_somewhere_else")
    assert canvas.live_lane() == "_somewhere_else"

    asyncio.run(server.handle_lane(_Req(state, {"action": "switch", "name": "atlas"})))

    assert state.lanes.current == "atlas"          # the voice lane moved
    assert canvas.live_lane() == "atlas"           # and the canvas moved WITH it, in-process


def test_a_wake_switch_drives_the_canvas_too(state):
    """FR1 / TC2. `_set_lane` is the ONE writer, so a wake-reason switch reaches the canvas by the
    exact same path a tap does — there is no second route that could set a different lane."""
    canvas.set_live("_somewhere_else")

    asyncio.run(server._set_lane(state, "atlas", why="wake"))

    assert state.lanes.current == "atlas"
    assert canvas.live_lane() == "atlas"


def test_the_merged_server_starts_no_polling_follower(monkeypatch, tmp_path):
    """FR2 / AC2. In one process the poll is a cross-process detour to memory it already shares.
    `init_canvas(follow=False)` must NOT spawn the follower thread, and status must say so."""
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setattr(canvas, "_store", None, raising=False)

    canvas_aio.init_canvas(session="t", fresh=True, follow=False)

    assert canvas._follower is not None
    assert canvas._follower.enabled is False
    assert canvas._follower._thread is None, "no /status-polling thread may run in the merged server"
    assert canvas._follower.status()["following"] is False


def test_the_follower_code_survives_for_the_standalone_canvas():
    """FR2's other half / TC-note. Retiring the poll for the embedded case must not delete the
    Follower — a standalone canvas (no voice server in-process) still needs it. Enabled but with no
    voice tunnel to find, it stays soft: it reports not-following and never raises."""
    from command_bridge.canvas.follow import Follower

    f = Follower(session="_no_such_session_", enabled=True)
    f.start(canvas.set_live, canvas.live_lane)
    assert f.status()["following"] is False  # soft: nothing found, no error
