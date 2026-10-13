"""A laneless `turns` read is REFUSED on a multi-lane session.

The mirror of `watch`/`say`'s `no_lane` guard, on the read-from-disk `turns`. `turns` reads the WHOLE
log, so on a multi-agent session a laneless read hands one agent every OTHER lane's turns — the exact
context pollution the post-compaction re-read rule would otherwise cause (an agent runs `turns` to
recover ITS words and gets all lanes'). Require `--lane <yours>`; `--lane all` is the explicit
whole-session escape. Keyed off the lanes present in the LOG (not the server), so `turns` stays
server-independent.
"""
import types

import command_bridge.cli as cli

TURNS = [
    {"id": 1, "lane": "magnus", "text": "m1"},
    {"id": 2, "lane": "atlas", "text": "a1"},
    {"id": 3, "lane": "magnus", "text": "m2"},
    {"id": 4, "lane": "atlas", "text": "a2"},
    {"id": 5, "lane": "magnus", "text": "m3"},
]


def _args(**kw):
    base = {"session": "dev", "limit": 0, "lane": None}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _log(turns, monkeypatch):
    monkeypatch.setattr(cli.store, "read_turns", lambda s: list(turns))
    monkeypatch.setattr(cli.store, "log_path", lambda s: "/tmp/dev.jsonl")


def test_a_named_lane_returns_only_that_lanes_turns(monkeypatch):
    _log(TURNS, monkeypatch)
    result = cli.cmd_turns(_args(lane="magnus"))
    assert [t["id"] for t in result["turns"]] == [1, 3, 5]
    assert result["count"] == 3
    assert result["lane"] == "magnus"
    assert result.get("code") != "no_lane"


def test_laneless_read_is_refused_on_a_multilane_log(monkeypatch):
    """The whole point: it must REFUSE, not silently hand back every lane's turns."""
    _log(TURNS, monkeypatch)
    result = cli.cmd_turns(_args())
    assert result["code"] == "no_lane"
    assert result["reason"] == "no_lane"
    assert result["turns"] == []
    assert result["count"] == 0
    assert "--lane" in result["remedy"]
    assert set(result["lanes"]) == {"magnus", "atlas"}


def test_lane_all_returns_every_lane(monkeypatch):
    """The explicit whole-session escape hatch: `--lane all` opts back into all lanes."""
    _log(TURNS, monkeypatch)
    result = cli.cmd_turns(_args(lane="all"))
    assert result["count"] == 5
    assert {t["lane"] for t in result["turns"]} == {"magnus", "atlas"}


def test_a_single_lane_log_allows_a_laneless_read(monkeypatch):
    """One lane is one place the turns could go, so demanding the flag would be ceremony."""
    single = [t for t in TURNS if t["lane"] == "magnus"]
    _log(single, monkeypatch)
    result = cli.cmd_turns(_args())
    assert result.get("code") != "no_lane"
    assert result["count"] == 3


def test_a_pre_lanes_log_allows_a_laneless_read(monkeypatch):
    """Turns with no `lane` field are a log predating lanes — nothing to pollute, so no refusal."""
    old = [{"id": 1, "text": "x"}, {"id": 2, "text": "y"}]
    _log(old, monkeypatch)
    result = cli.cmd_turns(_args())
    assert result.get("code") != "no_lane"
    assert result["count"] == 2


def test_limit_applies_after_the_lane_filter(monkeypatch):
    """`--limit N` is the last N of THAT lane, not the last N overall then filtered."""
    _log(TURNS, monkeypatch)
    result = cli.cmd_turns(_args(lane="magnus", limit=2))
    assert [t["id"] for t in result["turns"]] == [3, 5]
    assert result["count"] == 2
