"""Spec 044 — the app is a dumb wrapper; behaviour ships from the server.

The two acceptance tests that matter most are AC1 and AC2: spec 043's orb-off hang-up and spec 040's
"Heard you" were each an APK release, and here each is a server-side policy driven through the real
`_on_control` / `_broadcast_json` path to a fake app socket.
"""
import asyncio
import json
import pathlib

import pytest

from command_bridge import native, server

SHELL = pathlib.Path(server.WEB_DIR, "shell.html").read_text(encoding="utf-8")
OPS = ["audio", "notify", "notify_cancel", "awake"]


class _Sock:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)

    def directives(self, op=None):
        return [m for m in self.sent if m.get("type") == "native" and (op is None or m.get("op") == op)]


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    return server.TunnelState("t", token=None)


def _ctl(state, ws, msg):
    asyncio.run(server._on_control(state, json.dumps(msg), ws))  # type: ignore[arg-type]


def _app(state, foreground=False, devices=(native.SPEAKER, native.BLUETOOTH_SCO)):
    ws = _Sock()
    state.clients.add(ws)
    _ctl(state, ws, {"type": "hello", "sample_rate": 16000, "streams": True,
                     "native": {"protocol": 1, "ops": OPS, "app": "0.8.0"}})
    _ctl(state, ws, {"type": "native_state", "foreground": foreground, "devices": list(devices),
                     "mic": True, "call": True, "device": native.BLUETOOTH_SCO})
    ws.sent.clear()
    return ws


# --- AC3: the handshake ---------------------------------------------------------------------------

def test_a_native_hello_is_acknowledged_with_the_protocol(state):
    ws = _Sock()
    state.clients.add(ws)
    _ctl(state, ws, {"type": "hello", "sample_rate": 16000, "native": {"protocol": 1, "ops": OPS}})
    ack = next(m for m in ws.sent if m["type"] == "hello_ack")
    assert ack["native"] == {"protocol": 1}
    assert ws in state.native_clients


def test_an_old_app_or_a_browser_never_gets_a_directive(state):
    old = _Sock()
    state.clients.add(old)
    _ctl(state, old, {"type": "hello", "sample_rate": 16000, "streams": True})
    ack = next(m for m in old.sent if m["type"] == "hello_ack")
    assert "native" not in ack
    state.channel_open = False
    asyncio.run(server._broadcast_json(state, {"type": "channel", "value": False}))
    asyncio.run(server._broadcast_json(state, {"type": "turn", "text": "hi", "addressed": True, "lane": "magnus"}))
    assert not [m for m in old.sent if m.get("type") == "native"]


def test_a_future_protocol_is_driven_at_ours():
    c = native.from_hello({"native": {"protocol": 7, "ops": ["audio"]}})
    assert c is not None and c.protocol == native.PROTOCOL


# --- AC1: the orb-off fix, as server policy -------------------------------------------------------

def test_closing_the_channel_hangs_up_the_call(state):
    ws = _app(state)
    state.channel_open = True
    _ctl(state, _Sock(), {"type": "channel", "value": False})
    audio = ws.directives("audio")
    assert audio == [{"type": "native", "op": "audio", "mic": False, "call": False, "device": None}]
    # Sent BEFORE the channel broadcast itself, so the call drops with the orb.
    assert ws.sent.index(audio[0]) < ws.sent.index({"type": "channel", "value": False})


def test_opening_the_channel_takes_the_call_back_on_the_preferred_output(state):
    ws = _app(state)
    state.channel_open = False
    _ctl(state, _Sock(), {"type": "channel", "value": True})
    assert ws.directives("audio")[-1] == {"type": "native", "op": "audio", "mic": True, "call": True,
                                          "device": native.BLUETOOTH_SCO}
    assert ws.directives("awake")[-1]["on"] is True


def test_the_hold_rule_is_one_pure_function():
    c = native.NativeClient(protocol=1, ops=set(OPS), devices=[native.SPEAKER])
    assert native.wanted_audio(False, c, None) == {"mic": False, "call": False, "device": None}
    assert native.wanted_audio(True, c, None) == {"mic": True, "call": True, "device": native.SPEAKER}


# --- AC4: reports converge, never loop ------------------------------------------------------------

def test_a_report_that_matches_the_policy_sends_nothing(state):
    ws = _app(state)
    state.channel_open = True
    _ctl(state, ws, {"type": "native_state", "mic": True, "call": True, "device": native.BLUETOOTH_SCO})
    assert ws.directives("audio") == []


def test_a_report_that_differs_is_corrected_once(state):
    ws = _app(state)
    state.channel_open = False
    _ctl(state, ws, {"type": "native_state", "mic": True, "call": True, "device": native.BLUETOOTH_SCO})
    assert len(ws.directives("audio")) == 1
    ws.sent.clear()
    _ctl(state, ws, {"type": "native_state", "mic": False, "call": False, "device": None})
    assert ws.directives("audio") == []


def test_the_route_rule_never_picks_the_earpiece_by_itself():
    assert native.choose_route([native.EARPIECE, native.SPEAKER], None) == native.SPEAKER
    assert native.choose_route([native.SPEAKER, native.WIRED_HEADSET], None) == native.WIRED_HEADSET
    assert native.choose_route([native.SPEAKER, native.EARPIECE], native.EARPIECE) == native.EARPIECE
    assert native.choose_route([native.SPEAKER], native.BLUETOOTH_SCO) == native.SPEAKER


# --- AC2: "Heard you", as server policy -----------------------------------------------------------

def _turn(**kw):
    return {"type": "turn", "text": "turn the oven off", "addressed": True, "lane": "magnus", **kw}


def test_heard_you_is_sent_while_the_app_is_behind(state):
    ws = _app(state, foreground=False)
    asyncio.run(server._broadcast_json(state, _turn()))
    assert ws.directives("notify") == [{"type": "native", "op": "notify", "key": "heard", "style": "quiet",
                                        "title": "Heard you → Magnus", "text": "turn the oven off"}]


@pytest.mark.parametrize("turn,foreground", [
    (_turn(), True), (_turn(addressed=False), False), (_turn(text="  "), False)])
def test_heard_you_is_not_sent_in_front_unaddressed_or_empty(state, turn, foreground):
    ws = _app(state, foreground=foreground)
    asyncio.run(server._broadcast_json(state, turn))
    assert ws.directives("notify") == []


def test_a_report_moving_the_app_in_front_stops_heard_you(state):
    ws = _app(state, foreground=False)
    _ctl(state, ws, {"type": "native_state", "foreground": True})
    asyncio.run(server._broadcast_json(state, _turn()))
    assert ws.directives("notify") == []


def test_a_spoken_clip_and_a_raised_hand_notify_from_the_server(state):
    ws = _app(state)
    asyncio.run(server._broadcast_json(state, {"type": "audio_header", "id": "c1", "lane": "magnus",
                                               "text": "build is green"}))
    asyncio.run(server._broadcast_json(state, {"type": "audio_header", "id": "cue-x", "cue": "ack", "text": "x"}))
    asyncio.run(server._broadcast_json(state, {"type": "lane_waiting", "lane": "ada", "waiting": 1}))
    asyncio.run(server._broadcast_json(state, {"type": "lane_waiting", "lane": "ada", "waiting": 0}))
    titles = [d["title"] for d in ws.directives("notify")]
    assert titles == ["Magnus", "Ada raised a hand"]


def test_an_op_the_app_did_not_list_is_never_sent(state):
    ws = _Sock()
    state.clients.add(ws)
    _ctl(state, ws, {"type": "hello", "native": {"protocol": 1, "ops": ["awake"]}})
    ws.sent.clear()
    asyncio.run(server._broadcast_json(state, _turn()))
    _ctl(state, _Sock(), {"type": "channel", "value": False})
    assert {d["op"] for d in ws.directives()} == {"awake"}


# --- AC5: his output pick lives on the server -----------------------------------------------------

def test_a_route_pick_is_stored_reroutes_the_app_and_repaints_the_caret(state, tmp_path):
    ws = _app(state)
    page = _Sock()
    state.clients.add(page)
    state.channel_open = True
    _ctl(state, page, {"type": "route_pick", "value": native.SPEAKER})
    assert ws.directives("audio")[-1]["device"] == native.SPEAKER
    routes = [m for m in page.sent if m.get("type") == "native_routes"][-1]
    assert {r["type"] for r in routes["available"]} == {native.SPEAKER, native.BLUETOOTH_SCO}
    assert native.load_pick(str(tmp_path)) == native.SPEAKER
    # A restarted server starts from the stored pick.
    assert server.TunnelState("t2", token=None).native_pick == native.SPEAKER


def test_status_names_the_driven_app(state):
    _app(state)
    s = state.snapshot()
    assert s["native_clients"][0]["app"] == "0.8.0"
    assert s["native_clients"][0]["ops"] == sorted(OPS)


# --- AC6: the page's half -------------------------------------------------------------------------

def test_the_shell_with_prims_drives_the_server_not_the_app():
    assert 'const PRIMS = NATIVE && params.has("prims");' in SHELL
    i = SHELL.index("async function orbTapped()")
    block = SHELL[i:i + 900]
    assert 'tellServer("channel", !channelOpen)' in block
    assert 'nativeCall("orbTapped")' in block                       # not running, or an old app
    assert 'tellServer("route_pick", Number(type))' in SHELL
    assert 'msg.type === "native_routes"' in SHELL
