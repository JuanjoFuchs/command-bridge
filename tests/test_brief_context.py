"""`brief` + `watch --context`: the thinking agent's channel to a fast voice agent (2026-10-06).

JJ: *"the watch should resolve with new context … a facade for both watch and new context. That way
we don't have to rely on Herdr."* Claude Code delivers a message to a busy agent only between tool
calls, and a voice agent is always inside a blocking watch, so context sat queued until he spoke.

Server endpoints are driven directly; the watch loop is driven against a stubbed `_request`.
"""
import asyncio
import types

from command_bridge import cli, server


class _Req:
    def __init__(self, state, body):
        self.app = {"state": state}
        self._body = body
        self.remote = "127.0.0.1"
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    return server.TunnelState("t", token=None, gate_enabled=False)


def _call(handler, state, body):
    async def go():
        resp = await handler(_Req(state, body))
        import json
        return json.loads(resp.body)
    return asyncio.run(go())


def test_a_brief_is_held_per_lane_and_taken_exactly_once(monkeypatch, tmp_path):
    st = _state(monkeypatch, tmp_path)
    assert _call(server.handle_brief, st, {"lane": "magnus", "text": "kepler shipped"})["pending"] == 1
    _call(server.handle_brief, st, {"lane": "magnus", "text": "tests pass"})
    assert st.snapshot()["lane_briefs_pending"] == {"magnus": 2}
    taken = _call(server.handle_briefs_take, st, {"lane": "magnus"})
    assert [b["text"] for b in taken["context"]] == ["kepler shipped", "tests pass"]
    assert _call(server.handle_briefs_take, st, {"lane": "magnus"})["context"] == []
    assert st.snapshot()["lane_briefs_pending"] == {}


def test_an_empty_brief_is_refused(monkeypatch, tmp_path):
    st = _state(monkeypatch, tmp_path)
    assert _call(server.handle_brief, st, {"lane": "magnus", "text": "  "})["code"] == "empty"


def test_briefs_pending_reads_only_this_lane():
    live = {"lane_briefs_pending": {"magnus": 2}, "default_lane": "magnus"}
    assert cli._briefs_pending(live, "magnus") == 2
    assert cli._briefs_pending(live, "kepler") == 0
    assert cli._briefs_pending(live, None) == 2          # a laneless call means the default lane
    assert cli._briefs_pending(None, "magnus") == 0


def test_brief_requires_text(capsys):
    args = types.SimpleNamespace(text=None, file=None, session="dev", lane="magnus")
    assert cli.cmd_brief(args)["code"] == "empty"
