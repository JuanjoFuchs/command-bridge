"""A live-lane reply streams piece by piece; a held one is still sent whole (JJ, 2026-10-07).

The whole clip used to wait on its last synthesized piece — 1-6 s — before he heard its first,
which alone takes ~0.2 s. These drive `handle_say`, the real path, and read what went on the wire.
"""
import asyncio
import json

import pytest

from command_bridge import server


class _Sock:
    def __init__(self):
        self.frames = []

    async def send_json(self, payload):
        self.frames.append(("json", payload))

    async def send_bytes(self, data):
        self.frames.append(("bytes", data))


class _Req:
    def __init__(self, state, body):
        self.app = {"state": state}
        self.headers = {}
        self.query = {}
        self.remote = "127.0.0.1"
        self._body = body

    async def json(self):
        return self._body


PIECES = [b"\x01\x00" * 10, b"\x02\x00" * 20, b"\x03\x00" * 30]


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("kepler")
    st.channel_open = True
    st.cues_enabled = False
    st.sock = _Sock()
    st.clients.add(st.sock)
    st.stream_clients.add(st.sock)   # this page's `hello` said `streams: true`
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.0, raising=False)
    calls = []

    def stream(text, voice=None, speed=None, pause=None):
        calls.append(text)
        for p in PIECES:
            yield p, 24000

    monkeypatch.setattr(server.tts, "synthesize_stream", stream)
    monkeypatch.setattr(server.tts, "synthesize",
                        lambda text, voice=None, speed=None, pause=None: (b"".join(PIECES), 24000))
    st.stream_calls = calls
    return st


def say(state, lane, text="Hello there. How are you?"):
    resp = asyncio.run(server.handle_say(_Req(state, {"text": text, "lane": lane})))
    return json.loads(resp.body.decode("utf-8"))


def _audio_frames(sock):
    """The frames of the reply clip only (cues are off in this fixture)."""
    out = []
    for kind, payload in sock.frames:
        if kind == "json" and payload.get("type") not in ("audio_header", "audio_end"):
            continue
        out.append((kind, payload))
    return out


def test_a_live_reply_goes_out_header_then_each_piece_then_end(state):
    out = say(state, "magnus")
    assert out["delivered"] is True
    frames = _audio_frames(state.sock)
    kinds = [k for k, _ in frames]
    assert kinds == ["json", "bytes", "bytes", "bytes", "json"]
    header, end = frames[0][1], frames[-1][1]
    assert header["type"] == "audio_header" and header["streamed"] is True
    assert [p for k, p in frames if k == "bytes"] == PIECES
    assert end == {"type": "audio_end", "id": header["id"]}


def test_the_in_flight_record_holds_the_whole_clip(state):
    """A clip handed back on reconnect or barge is re-sent from here, so it must be complete."""
    out = say(state, "magnus")
    clip = state.lane_inflight["magnus"][-1]
    assert clip["header"]["id"] == out["id"]
    assert clip["pcm"] == b"".join(PIECES)


def test_the_spoken_log_records_the_streamed_reply(state, tmp_path):
    say(state, "magnus", "Streamed and logged.")
    said = (tmp_path / "t.said.jsonl").read_text(encoding="utf-8")
    assert "Streamed and logged." in said


def test_a_page_that_never_declared_streams_gets_the_whole_clip(state):
    """An older shell still open in a tab would play only the first piece of a streamed clip, so
    one undeclared page anywhere sends every clip whole.

    **Verify by mutation:** drop the `stream_clients` check in `_speak_now` and this goes red."""
    old = _Sock()
    state.clients.add(old)
    say(state, "magnus")
    frames = _audio_frames(state.sock)
    assert [k for k, _ in frames] == ["json", "bytes"]
    assert not frames[0][1].get("streamed")
    assert frames[1][1] == b"".join(PIECES)


def test_an_off_lane_reply_is_held_whole_and_never_streamed(state):
    out = say(state, "kepler")
    assert out["held_off_lane"] is True
    assert state.stream_calls == [], "a reply he will not hear now gains nothing from streaming"
    held = state.lane_held["kepler"][-1]
    assert held["pcm"] == b"".join(PIECES)
    assert not held["header"].get("streamed")
