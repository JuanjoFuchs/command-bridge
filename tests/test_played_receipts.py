"""The page's `played` receipt reaches the AGENT, not just the server (JJ, 2026-09-29).

    "It played." · "And I think the client should have a way of telling you that, right?"

The page has always sent a `played` receipt per clip; the server used it only to free its held copy.
So an agent could never tell a clip he HEARD from one still waiting — and the voice rules told it to
re-say, which he heard as duplicates. Now each receipt is recorded per lane (`lane_played`, with the
clip's text) and handed to that lane's agent on its next watch-open, cleared on delivery.

Drives the real `_speak` and the real `played` handler, like test_two_lanes_speaking.py.
"""
import asyncio
import json

import pytest

from command_bridge import config, server


class _Sock:
    def __init__(self):
        self.headers = []

    async def send_json(self, payload):
        self.headers.append(payload)

    async def send_bytes(self, data):
        pass


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("atlas")
    st.channel_open = True
    st.cues_enabled = False
    st.clients.add(_Sock())
    return st


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    monkeypatch.setattr(server.tts, "synthesize", lambda text, voice=None, speed=None, pause=None:
                        (text.encode("utf-8") * 8, 22050))
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.0, raising=False)


def _played(state, clip_id):
    sock = next(iter(state.clients))
    asyncio.run(server._on_control(state, json.dumps({"type": "played", "id": clip_id}), sock))


def test_a_played_receipt_is_recorded_for_the_lane_that_spoke_it(state):
    state.lanes.switch("atlas")
    clip = asyncio.run(server._speak(state, "tests are green", None, lane="atlas"))
    _played(state, clip["id"])
    assert state.lane_played["atlas"] == [
        {"clip": clip["id"], "text": "tests are green", "played_at": state.lane_played["atlas"][0]["played_at"]}]
    assert "magnus" not in state.lane_played            # another lane is told nothing
    assert clip["id"] not in state.clip_text             # the text is handed over, not kept forever


def test_a_clip_he_cut_off_is_reported_as_cut_with_how_much_he_heard(state):
    """JJ, 2026-10-05: *"an agent should be aware of whether or not their turn played or not or was
    interrupted by me speaking"*. Stopping a clip fires the same end event as finishing it, so the
    page says HOW it ended, and the agent is told it was cut and how far in."""
    state.lanes.switch("atlas")
    clip = asyncio.run(server._speak(state, "here is the long answer", None, lane="atlas"))
    sock = next(iter(state.clients))
    asyncio.run(server._on_control(state, json.dumps(
        {"type": "played", "id": clip["id"], "cut": "barge_in", "heard_s": 1.84, "duration_s": 9.3}), sock))
    rec = state.lane_played["atlas"][0]
    assert rec["cut"] == "barge_in" and rec["heard_s"] == 1.8 and rec["duration_s"] == 9.3


def test_a_clip_that_played_to_the_end_carries_no_cut(state):
    state.lanes.switch("atlas")
    clip = asyncio.run(server._speak(state, "short", None, lane="atlas"))
    _played(state, clip["id"])
    assert "cut" not in state.lane_played["atlas"][0]


def test_the_watch_next_leads_with_the_cut_clip():
    from command_bridge import cli
    result = {"next": "run `command-bridge watch --since 5`",
              "played": [{"clip": "c1", "text": "x", "cut": "barge_in", "heard_s": 1.8, "duration_s": 9.3}]}
    cli._lead_next_with_reissue(result, "atlas")
    assert result["next"].startswith("NOTE — 1 clip of yours WAS CUT OFF (he talked over it after 1.8s of 9.3s")
    assert result["next"].endswith("run `command-bridge watch --since 5`")


def test_the_shell_marks_a_barge_in_before_it_stops_the_clip():
    """The mark has to be on the source BEFORE `stop()`, because `stop()` is what fires the end
    event that sends the receipt."""
    import pathlib, re
    shell = (pathlib.Path(server.__file__).parent / "web" / "shell.html").read_text(encoding="utf-8")
    assert re.search(r'playingSource\.__cut = "barge_in";\s*\n\s*try \{ if \(playingSource\) playingSource\.stop', shell)
    assert re.search(r'playingSource\.__cut = "channel_off";\s*\n\s*try \{ if \(playingSource\) playingSource\.stop', shell)
    assert 'cut, heard_s:' in shell


def test_the_record_is_bounded_like_the_held_queue(state):
    state.lanes.switch("magnus")
    for i in range(config.UNDELIVERED_MAX + 3):
        clip = asyncio.run(server._speak(state, f"clip {i}", None, lane="magnus"))
        _played(state, clip["id"])
    assert len(state.lane_played["magnus"]) == config.UNDELIVERED_MAX
    assert state.lane_played["magnus"][-1]["text"] == f"clip {config.UNDELIVERED_MAX + 2}"
