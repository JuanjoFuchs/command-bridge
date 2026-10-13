"""Spec 032 — a clip heard on one device is not played again on another.

JJ, 2026-10-09: *"when I switched back to the browser, the two turns from you played back in the
browser again, as if I hadn't heard them in the app."* Every clip goes to every connected socket; a
backgrounded browser tab queues its copy (its AudioContext is suspended) and plays it on resume. So a
`played` receipt from ANY device is announced to the others as `clip_played`, which they drop from
their queues — and a clip already heard never comes back from the server's own stores either.
"""
import asyncio
import pathlib

import pytest

from command_bridge import server

SHELL = pathlib.Path(server.WEB_DIR, "shell.html").read_text(encoding="utf-8")


class _Sock:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    return server.TunnelState("t", token=None)


def test_a_receipt_from_the_app_is_announced_to_the_browser(state):
    app, browser = _Sock(), _Sock()
    state.clients.add(app)
    state.clients.add(browser)
    state.lane_inflight["magnus"] = [{"header": {"id": "c1"}, "pcm": b"\0\0"}]
    asyncio.run(server._on_control(state, '{"type": "played", "id": "c1"}', app))  # type: ignore[arg-type]
    assert {"type": "clip_played", "id": "c1"} in browser.sent
    # The sender already knows; it is not told about its own receipt.
    assert {"type": "clip_played", "id": "c1"} not in app.sent


def test_a_clip_heard_on_the_app_is_not_replayed_to_a_browser_that_connects_later(state):
    app = _Sock()
    state.clients.add(app)
    clip = {"header": {"id": "c2", "lane": "magnus"}, "pcm": b"\0\0", "at": 0}
    state.lane_inflight["magnus"] = [dict(clip)]
    # The same clip also sits on a hold (e.g. returned by a drop that raced the receipt).
    state.lane_held["magnus"] = [dict(clip)]
    asyncio.run(server._on_control(state, '{"type": "played", "id": "c2"}', app))  # type: ignore[arg-type]
    assert not state.lane_inflight.get("magnus")
    assert not state.lane_held.get("magnus")
    assert all((c.get("header") or {}).get("id") != "c2" for c in state.undelivered)


def test_the_native_page_never_captures_plays_or_asserts_controls():   # spec 032 AC3
    """In the app the service owns audio: the shell in ?native=1 must have no path to the mic, to
    playback, or to asserting the session controls on connect."""
    assert 'const NATIVE = params.has("native")' in SHELL
    assert "if (SPECTATE || NATIVE) return;" in SHELL                      # start(): no getUserMedia
    assert 'if (NATIVE && typeof ev.data !== "string") return;' in SHELL   # no clip bytes queued
    assert 'if (NATIVE && (msg.type === "audio_header" || msg.type === "audio_end")) return;' in SHELL
    assert 'if (!NATIVE) tellServer("muted", muted);' in SHELL
    assert 'if (!NATIVE) tellServer("speaking", false);' in SHELL
    assert "running && !SPECTATE && !NATIVE" in SHELL
    orb = SHELL[SHELL.index("async function orbTapped()"):]
    orb = orb[:orb.index("if (running) { await closeChannel(); return; }")]
    assert "if (NATIVE) {" in orb and 'nativeCall("orbTapped");' in orb   # spec 033: a web message


def test_the_shell_drops_a_queued_copy_of_a_clip_played_elsewhere():
    """The page half: `clip_played` removes that id from the local queue (source contract)."""
    i = SHELL.find('msg.type === "clip_played"')
    assert i > 0, "shell.html must handle clip_played"
    block = SHELL[i:i + 600]
    assert "clipQueue" in block
