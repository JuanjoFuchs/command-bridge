"""Spec 030 FR9 / AC4 — the companion APK is served from the same link, token-gated.

Driven in-process (no server started), like test_lane_endpoint.py: a live session runs on `dev`.
"""
import asyncio
from typing import Any

import pytest

from command_bridge import config, server


def _get(state, query) -> Any:
    """The handler on a fake request; `Any` because the fake is duck-typed, not an aiohttp Request."""
    return asyncio.run(server.handle_android_apk(_Req(state, query)))  # type: ignore[arg-type]


class _Req:
    def __init__(self, state, query):
        self.app = {"state": state}
        self.remote = "127.0.0.1"
        self.query = query
        self.headers = {}


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    return server.TunnelState("t", token="tok")


def test_the_apk_route_is_registered(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    app = server.build_app("t", token=None)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    assert "/android/command-bridge.apk" in paths


def test_without_the_token_it_is_refused(state, monkeypatch, tmp_path):
    apk = tmp_path / "app-debug.apk"
    apk.write_bytes(b"PK\x03\x04fake")
    monkeypatch.setattr(config, "ANDROID_APK_PATH", str(apk))
    resp = _get(state, {})
    assert resp.status == 403


def test_with_the_token_it_serves_the_file_as_an_apk(state, monkeypatch, tmp_path):
    apk = tmp_path / "app-debug.apk"
    apk.write_bytes(b"PK\x03\x04fake")
    monkeypatch.setattr(config, "ANDROID_APK_PATH", str(apk))
    resp = _get(state, {"token": "tok"})
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/vnd.android.package-archive"
    assert "command-bridge.apk" in resp.headers["Content-Disposition"]
    assert str(resp._path) == str(apk)


def test_a_missing_build_says_how_to_make_one(state, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ANDROID_APK_PATH", str(tmp_path / "nope.apk"))
    resp = _get(state, {"token": "tok"})
    body = resp.body.decode()
    assert resp.status == 404
    assert '"code": "apk_not_built"' in body
    assert "assembleDebug" in body
