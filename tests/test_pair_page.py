"""Spec 035 — pairing is its own link, not a button on the bridge."""
import asyncio
import pathlib
from typing import Any

from command_bridge import cli, server

WEB = pathlib.Path(server.WEB_DIR)
PAIR = (WEB / "pair.html").read_text(encoding="utf-8")
PAGE = (WEB / "index.html").read_text(encoding="utf-8")


def test_pair_is_a_route_serving_its_own_page():                          # AC1
    app = server.build_app("t", token="tok")
    paths = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource}
    assert ("GET", "/pair") in paths
    resp: Any = asyncio.run(server.handle_pair_page(None))   # type: ignore[arg-type]
    assert resp.status == 200 and "Pair the phone app" in resp.text


def test_the_page_mints_with_its_token_and_opens_the_app_from_the_top():  # AC1
    assert "fetch(`/pair/new?token=${encodeURIComponent(token)}`" in PAIR
    assert 'id="open" href="#" target="_top"' in PAIR
    assert "$open.href = j.intent_link;" in PAIR
    assert "innerHTML" not in PAIR


def test_the_bridge_has_no_pair_button():                                  # AC2
    assert 'id="pairbtn"' not in PAGE
    assert "/pair/new" not in PAGE


RT = {"host": "127.0.0.1", "port": 8765, "token": "tok/+=", "pid": 1,
      "lan": {"ip": "192.168.1.20", "url": "http://192.168.1.20:8765"}}


def test_status_gives_a_pairing_link_per_path():                           # AC3
    links = cli._pair_links(RT, "https://abc.ngrok-free.app/?token=x")
    assert links == {"lan": "http://192.168.1.20:8765/pair?token=tok%2F%2B%3D",
                     "tunnel": "https://abc.ngrok-free.app/pair?token=tok%2F%2B%3D"}


def test_an_absent_path_has_no_link():                                     # AC3
    assert cli._pair_links({**RT, "lan": None}, None) == {}
    assert set(cli._pair_links(RT, None)) == {"lan"}
    assert set(cli._pair_links({k: v for k, v in RT.items() if k != "lan"}, "https://a.app")) == {"tunnel"}
