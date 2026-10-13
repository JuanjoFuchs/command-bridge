"""Spec 034 — home-network mode: bind only the Wi-Fi, and nothing anonymous on it.

In-process: the LAN listener is told apart by the LOCAL address a request was accepted on, so these
fakes carry a transport whose `sockname` is the LAN address or loopback. No server is started.
"""
import asyncio
import json
from typing import Any

import pytest
from aiohttp import web

from command_bridge import lan, pairing, server

LAN = {"ip": "192.168.1.20", "subnet": "192.168.1.0/24", "port": 8765, "url": "http://192.168.1.20:8765"}

# A synthetic multi-adapter laptop (Get-NetAdapter shape), the case the rule exists for.
THIS_PC = [
    {"ip": "169.254.10.1", "prefix": 16, "alias": "Local Area Connection* 4", "medium": 0, "status": ""},
    {"ip": "172.26.80.1", "prefix": 20, "alias": "vEthernet (Default Switch)", "medium": 0, "status": "Up"},
    {"ip": "169.254.10.2", "prefix": 16, "alias": "Bluetooth Network Connection", "medium": 10,
     "status": "Disconnected"},
    {"ip": "169.254.10.3", "prefix": 16, "alias": "Tailscale", "medium": 0, "status": "Up"},
    {"ip": "169.254.10.4", "prefix": 16, "alias": "Ethernet", "medium": 14, "status": "Disconnected"},
    {"ip": "192.168.1.20", "prefix": 24, "alias": "Wi-Fi", "medium": 9, "status": "Up"},
    {"ip": "10.8.0.35", "prefix": 27, "alias": "VPN Adapter", "medium": 0,
     "status": "Up"},
]


def test_only_the_wifi_adapter_is_chosen():                                     # AC1
    got = lan.choose(THIS_PC)
    assert got == {"ip": "192.168.1.20", "prefix": 24, "subnet": "192.168.1.0/24", "alias": "Wi-Fi"}


def test_no_wifi_means_nothing_never_another_adapter():                        # AC1
    without = [a for a in THIS_PC if a["alias"] != "Wi-Fi"]
    assert lan.choose(without) is None
    # Docked on the corporate network: Ethernet up with a private address is still not the Wi-Fi.
    docked = without + [{"ip": "10.20.30.40", "prefix": 24, "alias": "Ethernet", "medium": 14, "status": "Up"}]
    assert lan.choose(docked) is None
    # A Wi-Fi that is down, or on a link-local / public / CGNAT address, is not chosen either.
    for ip, status in (("192.168.1.5", "Disconnected"), ("169.254.1.1", "Up"), ("8.8.8.8", "Up"),
                       ("100.101.1.2", "Up")):
        assert lan.choose([{"ip": ip, "prefix": 24, "alias": "Wi-Fi", "medium": 9, "status": status}]) is None


class _Transport:
    def __init__(self, local):
        self.local = local

    def get_extra_info(self, name):
        return (self.local, 8765) if name == "sockname" else None


class _Req:
    def __init__(self, state, *, local, remote, path="/", headers=None, query=None, cookies=None, lan_cfg=LAN):
        self.app = {"state": state, "lan": lan_cfg}
        self.transport = _Transport(local)
        self.remote = remote
        self.path = path
        self.headers = headers or {}
        self.query = query or {}
        self.cookies = cookies or {}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    return tmp_path


async def _ok(request):
    return web.Response(text="served")


def _gate(req) -> Any:
    return asyncio.run(server._lan_gate(req, _ok))


def _json(resp: Any) -> Any:
    return json.loads(resp.body)


def test_nothing_anonymous_on_the_lan(env):                                     # AC2
    state = server.TunnelState("t", token="tok")
    phone = "192.168.1.50"
    assert _gate(_Req(state, local=LAN["ip"], remote=phone)).status == 403
    assert _gate(_Req(state, local=LAN["ip"], remote=phone, path="/ui")).status == 403
    assert _gate(_Req(state, local=LAN["ip"], remote=phone, path="/canvas/status")).status == 403
    assert _gate(_Req(state, local=LAN["ip"], remote=phone, query={"token": "wrong"})).status == 403


def test_the_token_or_a_device_key_is_served_on_the_lan(env):                  # AC2
    state = server.TunnelState("t", token="tok")
    phone = "192.168.1.50"
    with_token = _gate(_Req(state, local=LAN["ip"], remote=phone, query={"token": "tok"}))
    assert with_token.status == 200
    # ...and the page's later requests carry it as the LAN cookie.
    assert with_token.cookies[server.LAN_TOKEN_COOKIE].value == "tok"
    assert with_token.cookies[server.LAN_TOKEN_COOKIE]["httponly"]
    assert _gate(_Req(state, local=LAN["ip"], remote=phone,
                      cookies={server.LAN_TOKEN_COOKIE: "tok"}, path="/ui")).status == 200
    code, _ = state.pairing.mint()
    key = state.pairing.exchange(code, "pixel")[1]["device_key"]
    assert _gate(_Req(state, local=LAN["ip"], remote=phone, headers={"X-CB-Device": key})).status == 200
    assert _gate(_Req(state, local=LAN["ip"], remote=phone, cookies={"cb_device": key})).status == 200


def test_a_peer_outside_the_wifi_subnet_is_refused_even_with_the_token(env):   # AC2
    state = server.TunnelState("t", token="tok")
    for outsider in ("10.0.0.7", "10.8.0.40", "127.0.0.1"):
        assert _gate(_Req(state, local=LAN["ip"], remote=outsider, query={"token": "tok"})).status == 403


def test_the_lan_cookie_means_nothing_off_the_lan(env):
    state = server.TunnelState("t", token="tok")
    loop = _Req(state, local="127.0.0.1", remote="127.0.0.1", cookies={server.LAN_TOKEN_COOKIE: "tok"})
    assert server._request_token(loop) is None   # type: ignore[arg-type]


def test_pair_exchange_needs_no_token_but_a_subnet_peer(env):                  # AC2
    state = server.TunnelState("t", token="tok")
    assert _gate(_Req(state, local=LAN["ip"], remote="192.168.1.50", path="/pair/exchange")).status == 200

    class _Ex(_Req):
        async def json(self):
            return {"code": "nope"}

    outsider: Any = _Ex(state, local=LAN["ip"], remote="10.0.0.7", path="/pair/exchange")
    assert asyncio.run(server.handle_pair_exchange(outsider)).status == 403
    insider: Any = _Ex(state, local=LAN["ip"], remote="192.168.1.50", path="/pair/exchange")
    assert asyncio.run(server.handle_pair_exchange(insider)).status == 403   # a bad CODE, not the peer
    assert _json(asyncio.run(server.handle_pair_exchange(insider)))["code"] == "pair_code_invalid"


def test_the_loopback_listener_is_unchanged(env):                              # AC2
    state = server.TunnelState("t", token="tok")
    # The same server with --lan: a loopback request reaches the handler with no token, as before.
    assert _gate(_Req(state, local="127.0.0.1", remote="127.0.0.1")).status == 200
    # And a server without --lan never gates at all.
    assert _gate(_Req(state, local="127.0.0.1", remote="127.0.0.1", lan_cfg=None)).status == 200


def test_the_gate_is_installed_on_the_app(env):
    app = server.build_app("t", token="tok", lan=LAN)
    assert server._lan_gate in app.middlewares
    assert app["lan"] == LAN


class _Mint(_Req):
    def __init__(self, state, body, **kw):
        super().__init__(state, **kw)
        self._body = body
        self.scheme = "http"
        self.host = "192.168.1.20:8765"

    async def json(self):
        return self._body


def test_pairing_carries_the_lan_address(env):                                 # AC3
    state = server.TunnelState("t", token="tok")
    # Minted from the tunnel page: both addresses.
    via_tunnel: Any = _Mint(state, {"base": "https://abc.ngrok-free.app"}, local="127.0.0.1",
                            remote="127.0.0.1", query={"token": "tok"})
    body = _json(asyncio.run(server.handle_pair_new(via_tunnel)))
    assert body["lan"] == LAN["url"] and body["base"] == "https://abc.ngrok-free.app"
    assert "base=https%3A%2F%2Fabc.ngrok-free.app&lan=http%3A%2F%2F192.168.1.20%3A8765&code=" in body["link"]
    # Minted from a page opened over the LAN: no https origin, so the LAN address alone.
    via_lan: Any = _Mint(state, {"base": "http://192.168.1.20:8765"}, local=LAN["ip"],
                         remote="192.168.1.50", query={"token": "tok"})
    body = _json(asyncio.run(server.handle_pair_new(via_lan)))
    assert body["base"] is None and body["link"].startswith("commandbridge://pair?lan=http%3A%2F%2F192.168.1.20")


ANDROID = __import__("pathlib").Path(__file__).resolve().parents[1] / "android" / "app"


def test_cleartext_only_to_loopback_and_the_lan_hosts():                       # AC6
    script = (ANDROID / "build.gradle.kts").read_text(encoding="utf-8")
    assert 'require(isPrivateV4(it))' in script
    assert '<base-config cleartextTrafficPermitted="false" />' in script
    assert not (ANDROID / "src" / "main" / "res" / "xml" / "network_security_config.xml").exists()
    gen = ANDROID / "build" / "generated" / "nsc" / "res" / "xml" / "network_security_config.xml"
    if not gen.exists():
        pytest.skip("the Android app has not been built here")
    import re
    xml = gen.read_text(encoding="utf-8")
    assert '<base-config cleartextTrafficPermitted="false" />' in xml
    domains = re.findall(r"<domain[^>]*>([^<]+)</domain>", xml)
    assert domains[:3] == ["localhost", "127.0.0.1", "10.0.2.2"]
    assert all(lan.is_private_v4(d) for d in domains[3:])


def test_links_without_a_lan_are_as_before():
    links = pairing.pair_links("https://a.app", "c")
    assert links["link"] == "commandbridge://pair?base=https%3A%2F%2Fa.app&code=c"
