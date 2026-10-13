"""Spec 025: a returning watch shows which lanes are working and which are idle.

The liaison (JJ talks only to magnus) missed an agent that had gone idle — JJ spotted it first. `watch
--fleet` puts each other lane's state on every return, from the same session-log activity the page's
fleet view reads. `fleet.lane_states` is pure; the CLI wrapper is driven with a stubbed wait and a
stubbed snapshot; the endpoint is driven in-process (no server is started — a live session runs).
"""
import asyncio
import json
import types

import pytest

import command_bridge.cli as cli
from command_bridge import fleet, server

NOW = 1_800_000_000.0
RECENT = 20.0


# ------------------------------------------------------------------ the pure states


def test_pending_work_is_working():
    """AC2."""
    out = fleet.lane_states({"kepler": {"work_pending": True, "last_work_epoch": NOW - 900}},
                            ["kepler"], NOW, RECENT)
    assert out == {"kepler": {"state": "working"}}


def test_recent_work_is_working():
    out = fleet.lane_states({"kepler": {"last_work_epoch": NOW - 5}}, ["kepler"], NOW, RECENT)
    assert out["kepler"] == {"state": "working"}


def test_old_work_is_idle_with_whole_seconds():
    out = fleet.lane_states({"kepler": {"last_work_epoch": NOW - 312.7}}, ["kepler"], NOW, RECENT)
    assert out["kepler"] == {"state": "idle", "idle_s": 312}


def test_no_work_seen_is_unknown():
    out = fleet.lane_states({"kepler": {"last_work_epoch": None}}, ["kepler"], NOW, RECENT)
    assert out["kepler"] == {"state": "unknown"}


def test_only_lanes_in_the_list_are_reported_and_missing_rows_are_unknown():
    """AC3."""
    out = fleet.lane_states({"ghost": {"last_work_epoch": NOW - 1}}, ["atlas", "vega"], NOW, RECENT)
    assert out == {"atlas": {"state": "unknown"}, "vega": {"state": "unknown"}}


# ------------------------------------------------------------------ the watch wrapper


def _args(**kw):
    base = {"session": "s", "since": 5, "lane": "magnus", "fleet": True}
    base.update(kw)
    return types.SimpleNamespace(**base)


SNAP = {"server_epoch": NOW, "lanes": {"magnus": {"state": "working"},
                                         "kepler": {"state": "idle", "idle_s": 312},
                                         "atlas": {"state": "working"}}}


def test_the_snapshot_rides_on_the_return_without_my_own_lane(monkeypatch):
    """AC4."""
    monkeypatch.setattr(cli, "_watch_once", lambda a: {"reason": "turns", "turns": [{"id": 6}]})
    monkeypatch.setattr(cli, "_fleet_snapshot", lambda s: SNAP)
    out = cli.cmd_watch(_args())
    assert out["reason"] == "turns" and out["turns"] == [{"id": 6}]
    assert out["fleet"] == {"kepler": {"state": "idle", "idle_s": 312},
                            "atlas": {"state": "working"}}


def test_without_the_flag_no_field_is_added(monkeypatch):
    """AC1."""
    monkeypatch.setattr(cli, "_watch_once", lambda a: {"reason": "quiet"})
    called = []
    monkeypatch.setattr(cli, "_fleet_snapshot", lambda s: called.append(s) or SNAP)
    out = cli.cmd_watch(_args(fleet=False))
    assert out == {"reason": "quiet"} and not called


def test_an_unreadable_snapshot_reports_why_and_leaves_the_return_alone(monkeypatch):
    """AC5."""
    monkeypatch.setattr(cli, "_watch_once", lambda a: {"reason": "idle", "idle_s": 15.2})
    monkeypatch.setattr(cli, "_fleet_snapshot", lambda s: {"error": "server unreachable (URLError)"})
    out = cli.cmd_watch(_args())
    assert out == {"reason": "idle", "idle_s": 15.2,
                   "fleet_unavailable": "server unreachable (URLError)"}


def test_the_fetch_never_raises(monkeypatch):
    """AC5: no server registered, or the call blows up, is an error dict — never an exception."""
    monkeypatch.setattr(cli, "read_runtime", lambda s: None)
    assert "error" in cli._fleet_snapshot("s")
    monkeypatch.setattr(cli, "read_runtime", lambda s: {"port": 1, "token": "x"})

    def boom(*a, **k):
        raise OSError("refused")
    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    assert cli._fleet_snapshot("s")["error"].startswith("server unreachable")


def test_an_idle_rearm_carries_the_flag(monkeypatch, tmp_path):
    """AC7: the self-built re-arm command in an `idle` return keeps `--fleet`."""
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location(
        "_tw_idle", pathlib.Path(__file__).with_name("test_watch_idle.py"))
    assert spec and spec.loader
    tw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tw)
    tw.Fake([tw._status()]).install(monkeypatch)
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cli, "_fleet_snapshot", lambda s: SNAP)
    out = cli.cmd_watch(tw._args(fleet=True))
    assert out["reason"] == "idle"
    assert "--idle 15 --fleet" in out["next"]
    assert "magnus" not in out["fleet"] and out["fleet"]["kepler"]["state"] == "idle"


def test_describe_documents_the_flag_and_fields(capsys):
    """AC8."""
    assert cli.main(["describe"]) == 0
    text = capsys.readouterr().out
    assert "--fleet" in text and "fleet_unavailable" in text and "idle_s" in text


# ------------------------------------------------------------------ the endpoint


class _Req:
    def __init__(self, state, query=None):
        self.app = {"state": state}
        self.remote = "127.0.0.1"
        self.query = query or {}
        self.headers = {}


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token="s3cret")
    st.lanes = server.lanes_mod.LaneRegistry("magnus")
    st.lanes.add("kepler")
    return st


def test_the_route_is_registered(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    app = server.build_app("t", token=None)
    assert "/fleet/state" in {r.resource.canonical for r in app.router.routes() if r.resource}


def test_the_endpoint_needs_the_token(state):
    """AC6."""
    resp = asyncio.run(server.handle_fleet_state(_Req(state)))
    assert resp.status == 403


def test_the_endpoint_returns_shaped_states(state, monkeypatch):
    """AC6."""
    monkeypatch.setattr(server, "_fleet_activity", lambda: {
        "server_epoch": NOW, "lanes": {"kepler": {"last_work_epoch": NOW - 100}}})
    resp = asyncio.run(server.handle_fleet_state(_Req(state, {"token": "s3cret"})))
    body = json.loads(resp.body.decode())
    assert resp.status == 200
    assert body["lanes"] == {"magnus": {"state": "unknown"},
                             "kepler": {"state": "idle", "idle_s": 100}}
