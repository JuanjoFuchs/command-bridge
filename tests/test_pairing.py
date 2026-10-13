"""Spec 031 — one-time pairing codes and device keys. In-process, no server started."""
import asyncio
import json
from typing import Any

import pytest

from command_bridge import cli, pairing, server


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    return tmp_path


def test_a_code_exchanges_exactly_once(env):          # AC1
    p = pairing.Pairing("t")
    code, _ = p.mint()
    status, body = p.exchange(code, "pixel")
    assert status == 200 and body["device_key"] and body["device_id"]
    again, err = p.exchange(code, "pixel")
    assert again == 403 and err["code"] == "pair_code_invalid"


def test_an_expired_code_is_refused(env):              # AC2
    clock = _Clock()
    p = pairing.Pairing("t", clock=clock)
    code, ttl = p.mint()
    clock.t += ttl + 1
    status, body = p.exchange(code)
    assert status == 403 and body["code"] == "pair_code_invalid"


def test_bad_codes_lock_exchange_then_it_lifts(env):   # AC3
    clock = _Clock()
    p = pairing.Pairing("t", clock=clock)
    good, _ = p.mint()
    for _ in range(pairing.BAD_CODE_LIMIT):
        assert p.exchange("wrong")[0] == 403
    status, body = p.exchange(good)
    assert status == 429 and body["code"] == "pair_locked"
    clock.t += pairing.LOCKOUT_S + 1
    fresh, _ = p.mint()
    assert p.exchange(fresh)[0] == 200


def test_only_a_hash_of_the_key_is_stored(env):
    p = pairing.Pairing("t")
    code, _ = p.mint()
    key = p.exchange(code)[1]["device_key"]
    raw = open(pairing.devices_path("t"), encoding="utf-8").read()
    assert key not in raw and "key_sha256" in raw


class _Req:
    def __init__(self, state, headers=None, query=None):
        self.app = {"state": state}
        self.remote = "127.0.0.1"
        self.headers = headers or {}
        self.query = query or {}


def _paired(state) -> tuple[str, str]:
    code, _ = state.pairing.mint()
    body = state.pairing.exchange(code, "pixel")[1]
    return body["device_id"], body["device_key"]


def test_a_device_key_stands_in_for_the_token(env):    # AC4
    state = server.TunnelState("t", token="tok")
    dev_id, key = _paired(state)
    assert server._check(_Req(state, {"X-CB-Device": key}), state)[0]          # type: ignore[arg-type]
    assert not server._check(_Req(state, {"X-CB-Device": "nope"}), state)[0]  # type: ignore[arg-type]
    assert not server._check(_Req(state), state)[0]                           # type: ignore[arg-type]
    # The WebSocket handler runs the same `_check`; revoking kills the key on the next request.
    assert pairing.revoke("t", dev_id)
    assert not server._check(_Req(state, {"X-CB-Device": key}), state)[0]     # type: ignore[arg-type]


class _CookieReq(_Req):
    def __init__(self, state, cookies):
        super().__init__(state)
        self.cookies = cookies


def test_a_device_cookie_stands_in_for_the_token(env):   # spec 032 AC2
    state = server.TunnelState("t", token="tok")
    dev_id, key = _paired(state)
    good: Any = _CookieReq(state, {"cb_device": key})
    bad: Any = _CookieReq(state, {"cb_device": "nope"})
    assert server._check(good, state)[0]
    assert not server._check(bad, state)[0]
    assert not asyncio.run(server.handle_ws(bad)).status == 101
    assert pairing.revoke("t", dev_id)
    assert not server._check(good, state)[0]


def test_the_ws_handler_refuses_a_bad_device_key(env):  # AC4, on the socket path itself
    state = server.TunnelState("t", token="tok")
    req: Any = _Req(state, {"X-CB-Device": "nope"})
    resp = asyncio.run(server.handle_ws(req))
    assert resp.status == 403


def test_the_routes_are_registered(env):
    app = server.build_app("t", token=None)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    assert {"/pair/new", "/pair/exchange"} <= paths


class _JsonReq(_Req):
    def __init__(self, state, body, headers=None, query=None):
        super().__init__(state, headers, query)
        self._body = body
        self.scheme = "http"
        self.host = "127.0.0.1:8765"

    async def json(self):
        return self._body


def test_mint_needs_the_token_and_returns_both_links(env):
    state = server.TunnelState("t", token="tok")
    denied: Any = asyncio.run(server.handle_pair_new(_JsonReq(state, {})))  # type: ignore[arg-type]
    assert denied.status == 403
    mint_req: Any = _JsonReq(state, {"base": "https://abc.ngrok-free.app/"}, query={"token": "tok"})
    ok: Any = asyncio.run(server.handle_pair_new(mint_req))
    body = json.loads(ok.body.decode())
    assert body["link"].startswith("commandbridge://pair?base=https%3A%2F%2Fabc.ngrok-free.app&code=")
    assert "scheme=commandbridge" in body["intent_link"]
    ex_req: Any = _JsonReq(state, {"code": body["code"], "name": "pixel"})
    ex: Any = asyncio.run(server.handle_pair_exchange(ex_req))
    assert ex.status == 200


def test_devices_cli_lists_without_keys_and_revokes(env):   # AC5
    p = pairing.Pairing("t")
    code, _ = p.mint()
    body = p.exchange(code, "pixel")[1]

    class A:
        session = "t"
        revoke: "str | None" = None

    listed = cli.cmd_devices(A())
    assert listed["devices"][0]["id"] == body["device_id"]
    assert "key_sha256" not in listed["devices"][0]
    A.revoke = body["device_id"]
    out = cli.cmd_devices(A())
    assert out["revoked"] == body["device_id"] and out["devices"] == []
    assert not pairing.device_ok("t", body["device_key"])
    A.revoke = "missing"
    assert cli.cmd_devices(A())["code"] == "device_not_found"
