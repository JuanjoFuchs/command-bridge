"""Spec 036 — a heads-up on his phone for important clips, even with the orb off.

Drives the real `_speak`, the real control handler (`played` receipts) and the real `/notify/wait`
handler. The notice is a heads-up only: the clip is held and played exactly as before.
"""
import asyncio
import json
from typing import Any

import pytest

from command_bridge import notices, server


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token="tok")
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("rigel")
    st.channel_open = True
    st.cues_enabled = False
    server._NOTICE_WAKE.clear()
    return st


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    monkeypatch.setattr(server.tts, "synthesize", lambda text, voice=None, speed=None, pause=None:
                        (text.encode("utf-8") * 8, 22050))
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.0, raising=False)


def _speak(state, text, lane="rigel", notify=False) -> Any:
    return asyncio.run(server._speak(state, text, None, lane=lane, notify=notify))


def test_a_notified_say_records_a_notice_for_its_clip(state):          # AC1
    out = _speak(state, "The deploy is blocked on your approval.", notify=True)
    items, seq = notices.since("t", 0)
    assert seq == 1 and len(items) == 1
    assert items[0]["clip"] == out["id"] and items[0]["lane"] == "rigel" and not items[0]["resolved"]
    assert "deploy is blocked" in items[0]["text"]


def test_a_plain_say_records_nothing(state):                            # AC1
    _speak(state, "just chatting")
    assert notices.since("t", 0) == ([], 0)


class _Req:
    def __init__(self, state, body=None, query=None, headers=None):
        self.app = {"state": state}
        self.remote = "127.0.0.1"
        self.headers = headers or {}
        self.query = query or {}
        self.cookies = {}
        self._body = body

    async def json(self):
        return self._body


def test_notify_with_intent_is_refused(state):                          # AC1
    req: Any = _Req(state, {"text": "about to deploy", "lane": "rigel", "notify": True, "intent": True},
                    query={"token": "tok"})
    resp: Any = asyncio.run(server.handle_say(req))
    assert resp.status == 400 and json.loads(resp.body)["code"] == "bad_request"
    assert notices.since("t", 0) == ([], 0)


class _Ws:
    async def send_json(self, _p):
        pass


def _played(state, clip, **extra):
    asyncio.run(server._on_control(state, json.dumps({"type": "played", "id": clip, **extra}), _Ws()))  # type: ignore[arg-type]


def test_only_an_uncut_receipt_resolves_the_notice(state):              # AC2
    clip = _speak(state, "Your approval is needed.", notify=True)["id"]
    _played(state, clip, cut="channel_off", heard_s=1.0, duration_s=4.0)
    assert not notices.since("t", 0)[0][0]["resolved"]       # switched off halfway: not heard
    _played(state, clip)
    items, seq = notices.since("t", 0)
    assert items[0]["resolved"] and seq == 2


def _wait(state, since=0, wait=0.0, token: "str | None" = "tok") -> Any:
    req: Any = _Req(state, query={"since": str(since), "wait": str(wait), **({"token": token} if token else {})})
    resp: Any = asyncio.run(server.handle_notify_wait(req))
    return resp.status, (json.loads(resp.body) if resp.status == 200 else None)


def test_changes_after_since_oldest_first_each_once(state):            # AC3
    a = notices.add("t", "clip-1", "rigel", "first")
    notices.add("t", "clip-2", "magnus", "second")
    notices.resolve("t", "clip-1")
    status, body = _wait(state, since=0)
    assert status == 200 and body["seq"] == 3
    # clip-1 changed again (resolved), so it is reported once, at its latest change: after clip-2.
    assert [(i["clip"], i["resolved"]) for i in body["items"]] == [("clip-2", False), ("clip-1", True)]
    assert _wait(state, since=3)[1]["items"] == []
    assert _wait(state, since=2)[1]["items"][0]["id"] == a["id"]


def test_a_future_since_resynchronises(state):                         # AC3
    notices.add("t", "clip-1", "rigel", "first")
    assert len(_wait(state, since=99)[1]["items"]) == 1


def test_notices_survive_a_restart(state, monkeypatch, tmp_path):       # AC3 (persisted)
    notices.add("t", "clip-1", "rigel", "first")
    fresh = server.TunnelState("t", token="tok")
    assert _wait(fresh, since=0)[1]["items"][0]["clip"] == "clip-1"


def test_a_waiting_poll_wakes_when_a_notice_lands(state):              # AC3
    async def scenario():
        req: Any = _Req(state, query={"since": "0", "wait": "5", "token": "tok"})
        poll = asyncio.create_task(server.handle_notify_wait(req))
        await asyncio.sleep(0.05)
        notices.add("t", "clip-9", "rigel", "now")
        server._notices_changed(state)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        resp: Any = await poll
        return loop.time() - t0, json.loads(resp.body)
    took, body = asyncio.run(scenario())
    assert took < 1.0 and body["items"][0]["clip"] == "clip-9"


def test_the_poll_needs_the_key_or_token(state):                       # AC3
    assert _wait(state, token=None)[0] == 403
    assert _wait(state, token="nope")[0] == 403


class _Sock:
    def __init__(self):
        self.headers = []

    async def send_json(self, p):
        self.headers.append(p)

    async def send_bytes(self, _b):
        pass

    def clips(self):
        return [h["id"] for h in self.headers if h.get("type") == "audio_header"]


def test_orb_off_holds_even_through_a_reconnect_then_plays_once_on(state):   # the 13:10 bug
    """2026-10-09: the orb was off, the app's socket reconnected, the connect-time flush sent the held
    clip and it played. JJ: "I had the orb turned off and you still spoke to me." """
    phone = _Sock()
    state.clients.add(phone)
    state.lanes.switch("magnus")
    asyncio.run(server._on_control(state, json.dumps({"type": "channel", "value": False}), _Ws()))  # type: ignore[arg-type]
    clip = _speak(state, "Enjoy lunch.", lane="magnus", notify=True)["id"]
    assert phone.clips() == [] and len(state.undelivered) == 1
    # The reconnect: a new socket runs the same flush `handle_ws` runs on connect.
    assert asyncio.run(server._flush_undelivered(state)) == 0
    assert phone.clips() == [] and len(state.undelivered) == 1
    # The orb on: it plays, once.
    asyncio.run(server._on_control(state, json.dumps({"type": "channel", "value": True}), _Ws()))  # type: ignore[arg-type]
    assert phone.clips() == [clip]
    asyncio.run(server._flush_undelivered(state))
    assert phone.clips() == [clip]
    # And hearing it clears the heads-up.
    _played(state, clip)
    assert notices.since("t", 0)[0][-1]["resolved"]


def test_the_route_is_registered(state):
    app = server.build_app("t", token="tok")
    assert ("GET", "/notify/wait") in {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource}
