"""The pre-speech grace counts his silence from his LAST SPEECH, not from the moment a clip is ready.

JJ, 2026-10-07: *"What do you mean deliberate pause? I don't want to have a deliberate pause."*
The grace (`SPEAK_GRACE_S`, 0.8 s) exists so a mid-sentence pause is not talked over. But a reply
is ready seconds after his turn closed, so a fresh 0.8 s wait on every clip was pure latency:
`held_for` read 0.9 on clean replies where he had been quiet for five seconds.

🔴 THESE DRIVE `handle_say`, the real path, as `tests/test_duplicate_hold.py` does.
"""
import asyncio
import json
import time

import pytest

from command_bridge import server


class _Sock:
    async def send_json(self, payload):
        pass

    async def send_bytes(self, data):
        pass


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
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.channel_open = True
    st.cues_enabled = False
    st.clients.add(_Sock())
    monkeypatch.setattr(server.tts, "synthesize",
                        lambda text, voice=None, speed=None, pause=None: (b"\0\0" * 100, 22050))
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.8, raising=False)
    return st


def say(state):
    resp = asyncio.run(server.handle_say(_Req(state, {"text": "hello", "lane": "magnus"})))
    return json.loads(resp.body.decode("utf-8"))


def test_long_silence_means_no_grace_wait(state):
    """He stopped talking five seconds ago, so the clip goes out without the 0.8 s wait.

    **Verify by mutation:** start `grace` at 0.0 unconditionally and this goes red."""
    state.last_speech_at = time.monotonic() - 5.0
    t0 = time.monotonic()
    out = say(state)
    assert out["delivered"] is True
    assert out["held_for"] < 0.2
    assert time.monotonic() - t0 < 0.5


def test_recent_speech_waits_only_the_remainder(state):
    """He spoke 0.5 s ago: wait out the remaining ~0.3 s, not a fresh 0.8 s."""
    state.last_speech_at = time.monotonic() - 0.5
    t0 = time.monotonic()
    say(state)
    elapsed = time.monotonic() - t0
    assert 0.2 <= elapsed < 0.7


def test_no_speech_ever_seen_keeps_the_full_grace(state):
    """No audio yet: nothing to measure silence from, so the old full wait stands."""
    assert state.last_speech_at == 0.0
    out = say(state)
    assert out["held_for"] >= 0.8
