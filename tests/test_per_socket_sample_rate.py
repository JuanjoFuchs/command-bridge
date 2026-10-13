"""Spec 030 review MUST 1 — the sample rate belongs to the SOCKET, not the session.

The Android app announces 16 kHz; a browser page (even a `?spectate=1` canvas) announces 48 kHz. With
one session-wide rate, whichever said `hello` last decided how EVERY client's frames were resampled,
so opening the canvas played the app's audio to ASR at a third of its speed. Driven in-process, no
server started.
"""
import asyncio

import numpy as np
import pytest

from command_bridge import asr as asr_mod
from command_bridge import config, server


class _Sock:
    """Enough of a WebSocketResponse for `hello`: weak-referenceable and able to ack."""

    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


class _Seen(Exception):
    pass


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    return server.TunnelState("t", token=None)


def _hello(state, ws, rate):
    asyncio.run(server._on_control(state, '{"type": "hello", "sample_rate": %d, "streams": true}' % rate, ws))


def test_each_socket_keeps_the_rate_it_announced(state):
    app, page = _Sock(), _Sock()
    _hello(state, app, 16000)
    _hello(state, page, 48000)      # the canvas opening after the app connected
    assert server._client_rate(state, app) == 16000
    assert server._client_rate(state, page) == 48000


def test_a_frame_is_resampled_from_ITS_socket_rate(state, monkeypatch):
    app, page = _Sock(), _Sock()
    _hello(state, app, 16000)
    _hello(state, page, 48000)
    calls = []

    def spy(samples, src, dst):
        calls.append((src, dst))
        raise _Seen()

    monkeypatch.setattr(asr_mod, "resample_linear", spy)
    raw = np.zeros(640, dtype=np.int16).tobytes()

    async def run():
        loop = asyncio.get_running_loop()
        # The app's 16 kHz frame needs no resampling at all: it must reach past the resample step.
        monkeypatch.setattr(server, "_maybe_barge", _raise_seen)
        with pytest.raises(_Seen):
            await server._on_audio(state, raw, loop, app)
        # The page's 48 kHz frame is resampled from 48 kHz, not from the app's 16 kHz.
        with pytest.raises(_Seen):
            await server._on_audio(state, raw, loop, page)

    asyncio.run(run())
    assert calls == [(48000, config.TARGET_SR)]


async def _raise_seen(*_a, **_k):
    raise _Seen()
