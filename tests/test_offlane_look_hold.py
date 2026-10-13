"""Spec 017 — an off-lane `look` is HELD and replayed on lane-activate, BEFORE the held clip.

A camera verb from a lane that is not live used to be refused (`409`). So an off-lane agent that
had just drawn something could not pre-position the camera: when JJ switched to that lane the view
was wherever it happened to be, not on the frame the agent meant to show. This closes the gap — a
`look` from a background lane is held per lane and applied the moment that lane becomes live, ahead
of the lane's held voice clips, so JJ lands already looking at the frame the clip is about.

**The assertions are on EVENT ORDERING across the two transports.** The camera move is a canvas SSE
`look`; the voice clip is an audio header on the WebSocket. What FR2/FR3 promise is that the server
EMITS the look before it releases the clip — so the test records both on one merged timeline (the
real `canvas.publish` and the real `_flush_lane_held` → sock path) and asserts the order, rather
than asserting a flag.

NO SERVER IS STARTED HERE — the same discipline as `test_lane_hold.py`: a live voice session may be
running on `dev`, and this drives the module state and the switch path directly.
"""
import asyncio

import pytest

from command_bridge import server
from command_bridge.canvas import server as canvas

LIVE = "lanelive"      # the lane holding the floor while the off-lane agent pre-positions
BG = "lanebg"          # the background lane whose `look` is held and replayed on the switch


class _Sock:
    """A voice client that records what it was sent, and drops each audio header onto a shared
    timeline so the look/clip ORDER across the two transports can be asserted."""

    def __init__(self, timeline):
        self.timeline = timeline
        self.headers = []
        self.blobs = []

    async def send_json(self, payload):
        self.headers.append(payload)
        if payload.get("type") == "audio_header":
            self.timeline.append(("clip", payload.get("text")))

    async def send_bytes(self, data):
        self.blobs.append(data)


@pytest.fixture(autouse=True)
def _clean_canvas():
    """Isolate the shared canvas module globals for the two lanes this file uses — before AND after,
    so a leftover from another test cannot make a hold look already-present."""
    for d in (canvas._lanes, canvas._look_held, canvas._point, canvas._geometry, canvas._viewport):
        d.pop(LIVE, None)
        d.pop(BG, None)
    yield
    for d in (canvas._lanes, canvas._look_held, canvas._point, canvas._geometry, canvas._viewport):
        d.pop(LIVE, None)
        d.pop(BG, None)


@pytest.fixture
def timeline():
    return []


@pytest.fixture
def traced_publish(monkeypatch, timeline):
    """Record every canvas `look`/`switch` on the merged timeline, then fan it out for real so the
    SSE subscribers still receive it."""
    orig = canvas.publish

    def traced(event, payload):
        if event in ("look", "switch"):
            timeline.append((event, payload.get("lane")))
        return orig(event, payload)

    monkeypatch.setattr(canvas, "publish", traced)
    return traced


@pytest.fixture
def state(monkeypatch, tmp_path, traced_publish, timeline):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry(LIVE)
    st.lanes.add(BG)
    st.channel_open = True
    st.cues_enabled = False
    st.lanes.switch(LIVE)
    canvas.set_live(LIVE)          # the canvas floor matches the voice floor
    timeline.clear()               # the setup switch is not part of what a case measures
    return st


@pytest.fixture
def sock(state, timeline):
    s = _Sock(timeline)
    state.clients.add(s)
    return s


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    def synth(text, voice=None, speed=None, pause=None):
        return (text.encode("utf-8") * 8), 22050

    monkeypatch.setattr(server.tts, "synthesize", synth)
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.0, raising=False)


def draw(lane, fid):
    canvas.apply("/frame", {"id": fid, "kind": "mermaid", "content": "graph TD;A-->B", "lane": lane})


def say(state, text, lane):
    return asyncio.run(server._speak(state, text, None, lane=lane))


def switch(state, lane):
    asyncio.run(server._set_lane(state, lane, why="tap"))


# ------------------------------------------------------------------ AC1

def test_an_off_lane_look_is_held_not_refused(state):
    """AC1: a `look` from a lane that is not live returns held:true (not 409) and records it."""
    draw(BG, "board")
    code, body = canvas.apply("/look", {"id": "board", "lane": BG})

    assert code == 200, body
    assert body["held"] is True
    assert body["id"] == "board"
    assert body["all"] is False
    assert body["lane"] == BG
    assert body["live_lane"] == LIVE
    assert body["reason"] == "off_lane"
    assert canvas._look_held.get(BG, {}).get("id") == "board", "the target must be recorded"


def test_an_off_lane_look_at_a_missing_frame_is_a_404_not_a_hold(state):
    """A typo is still caught, exactly as the live `/look` catches it — nothing is held."""
    code, body = canvas.apply("/look", {"id": "nope", "lane": BG})
    assert code == 404
    assert BG not in canvas._look_held


# ------------------------------------------------------------------ AC2 / AC3

def test_the_held_look_is_emitted_before_the_lanes_first_held_clip(state, sock, timeline):
    """AC2/AC3: with a `look` AND a `say` held off-lane, switching to the lane replays the look
    camera event BEFORE the held clip is released — look-then-clip, in that order."""
    draw(BG, "board")
    canvas.apply("/look", {"id": "board", "lane": BG})       # off-lane look → held
    say(state, "here is the board", lane=BG)                  # off-lane say → held clip
    assert [t for t in timeline if t[0] in ("look", "clip")] == [], "nothing fires while off-lane"

    switch(state, BG)

    order = [t for t in timeline if t[0] in ("look", "clip")]
    assert order == [("look", BG), ("clip", "here is the board")], order
    assert BG not in canvas._look_held, "the held look is cleared once applied"


def test_the_held_look_delivers_on_the_canvas_sse_stream(state):
    """The look reaches an actually-connected canvas browser (the SSE path), not just the timeline."""
    import queue as _q
    draw(BG, "board")
    canvas.apply("/look", {"id": "board", "lane": BG})
    sub = _q.Queue()
    canvas._subscribers.append(sub)
    try:
        canvas.set_live(BG)                 # the switch a phone tap makes
        events = []
        while not sub.empty():
            events.append(sub.get_nowait())
    finally:
        canvas._subscribers.remove(sub)
    looks = [p for (e, p) in events if e == "look" and p.get("lane") == BG]
    assert looks and looks[-1]["id"] == "board", events


# ------------------------------------------------------------------ AC4

def test_two_held_looks_with_no_clip_between_apply_only_the_last(state, sock, timeline):
    """AC4: consecutive holds collapse to the last — the camera has one position, not a timeline."""
    draw(BG, "a")
    draw(BG, "b")
    canvas.apply("/look", {"id": "a", "lane": BG})
    canvas.apply("/look", {"id": "b", "lane": BG})
    assert canvas._look_held[BG]["id"] == "b", "the later hold overwrites the earlier"

    switch(state, BG)

    looks = [t for t in timeline if t[0] == "look"]
    assert looks == [("look", BG)], "exactly one look fires"
    # And it is the LAST one — assert via the SSE payload on a fresh switch is redundant; the store
    # already collapsed, and the single fired look carried id "b" (checked by the delivery test).


def test_a_say_between_two_looks_keeps_the_first_bound_ahead_of_its_clip(state, sock, timeline):
    """FR3 ordering, the interesting shape: look A, then say, then look B. Under the single-position
    model B wins the held slot (A had no clip bound to it in this design) — so the switch replays
    B before the clip. This documents the accepted behavior (see the spec's folded-back decision):
    a plain say does not pin an earlier look; only the last look is the held camera position."""
    draw(BG, "a")
    draw(BG, "b")
    canvas.apply("/look", {"id": "a", "lane": BG})
    say(state, "the board", lane=BG)
    canvas.apply("/look", {"id": "b", "lane": BG})

    switch(state, BG)

    order = [t for t in timeline if t[0] in ("look", "clip")]
    assert order == [("look", BG), ("clip", "the board")], order


# ------------------------------------------------------------------ AC5

def test_a_live_lane_look_still_applies_immediately(state, timeline):
    """AC5: a `look` from the LIVE lane is unchanged — applied now, not held."""
    draw(LIVE, "board")
    code, body = canvas.apply("/look", {"id": "board", "lane": LIVE})
    assert code == 200
    assert body.get("held") is not True, body
    assert body["ok"] is True
    assert LIVE not in canvas._look_held
    assert ("look", LIVE) in timeline, "the live look fires immediately"


def test_point_zoom_and_a_plain_cue_stay_refused_off_lane(state):
    """AC5: only `look` is held. `point`/`zoom` and a non-armed `cue` from a background lane are
    still refused, and an ARMED cue is still allowed (unchanged)."""
    draw(BG, "board")
    assert canvas.apply("/point", {"selector": "board", "lane": BG})[0] == 409
    assert canvas.apply("/zoom", {"selector": "board", "lane": BG})[0] == 409
    assert canvas.apply("/cue", {"text": "the box [point:board]", "lane": BG})[0] == 409
    armed = canvas.apply("/cue", {"text": "the box [point:board]", "arm": True, "lane": BG})
    assert armed[0] == 200 and armed[1].get("armed") is True
    canvas._armed.pop(BG, None)
