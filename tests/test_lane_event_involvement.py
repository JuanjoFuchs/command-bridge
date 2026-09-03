"""A lane switch wakes only the lanes it CROSSES — not every bystander (2026-09-03).

The live-lane signal moves on every switch, and the watch used to break on all of them, so each time
JJ moved between two lanes every OTHER agent's watch resolved too — one re-armed watch per idle agent
per switch. Measured live: with JJ bouncing between magnus and atlas, the kepler agent's watch
resolved every 1-2 minutes though nothing about kepler had changed. `_lane_event_for` gates it: a
switch only wakes a lane it left or reached (or an ambiguous summons on the lane it was holding); a
switch between two other lanes leaves an off-lane agent off-lane, so it does not wake.
"""
import time
import types

import command_bridge.cli as cli


# --- the pure involvement gate ----------------------------------------------

def _sig(lane, amb=0):
    return {"lane": lane, "ambiguous": amb}


def test_a_switch_to_my_lane_wakes_me():
    assert cli._lane_event_for(_sig("magnus"), _sig("kepler"), "kepler") == {"lane": "kepler"}


def test_a_switch_away_from_my_lane_wakes_me():
    assert cli._lane_event_for(_sig("kepler"), _sig("magnus"), "kepler") == {"lane": "magnus"}


def test_a_bystander_switch_does_not_wake_me():
    """The whole point: magnus -> atlas is nothing to kepler; it must not wake."""
    assert cli._lane_event_for(_sig("magnus"), _sig("atlas"), "kepler") is None


def test_a_single_agent_wakes_on_any_switch():
    """No `my_lane` means the one agent — every change is relevant, exactly as before."""
    assert cli._lane_event_for(_sig("magnus"), _sig("atlas"), None) == {"lane": "atlas"}


def test_no_change_is_no_event():
    assert cli._lane_event_for(_sig("kepler"), _sig("kepler"), "kepler") is None


def test_absent_signal_is_no_event():
    assert cli._lane_event_for(None, _sig("kepler"), "kepler") is None
    assert cli._lane_event_for(_sig("kepler"), None, "kepler") is None


def test_an_ambiguous_summons_on_my_held_lane_wakes_me():
    """He stayed on my lane and a summons matched nobody — the lane he was on is the one told."""
    assert cli._lane_event_for(_sig("kepler", 0), _sig("kepler", 1), "kepler") == {"ambiguous": 1}


def test_an_ambiguous_summons_on_someone_elses_lane_does_not_wake_me():
    assert cli._lane_event_for(_sig("magnus", 0), _sig("magnus", 1), "kepler") is None


# --- the loop, at the call site ---------------------------------------------

_IDLE = {"clients": 1, "channel_open": True, "capturing": True, "muted": False, "user_speaking": False}


def _run(monkeypatch, tmp_path, first, later, my_lane, timeout=0.6):
    """`first` answers the two setup /status reads (fixing the lane baseline), `later` every poll
    (the switch). A real-sleeping `store.watch` advances the loop."""
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))
    calls = {"n": 0}

    def request(session, path, payload=None):
        if path != "/status":
            return {}
        calls["n"] += 1
        return first if calls["n"] <= 2 else later
    monkeypatch.setattr(cli, "_request", request)

    def watch(session, cursor, timeout=0.0, addressed_only=True, lane=None, default_lane=None):
        time.sleep(min(timeout, 0.15))
        return [], cursor
    monkeypatch.setattr(cli.store, "watch", watch)

    return cli.cmd_watch(types.SimpleNamespace(
        session="s", since=6, timeout=timeout, force=False, all_turns=False, lane=my_lane))


def test_a_bystander_switch_does_not_resolve_an_off_lane_watch(monkeypatch, tmp_path):
    """kepler's watch, JJ switching magnus -> atlas: kepler is off-lane before and after, so its
    watch must NOT resolve on `lane` — it backs off. (Pre-fix this returned reason `lane`.)"""
    on_magnus = {**_IDLE, "lane": "magnus"}
    to_atlas = {**_IDLE, "lane": "atlas"}
    result = _run(monkeypatch, tmp_path, on_magnus, to_atlas, "kepler")
    assert result["reason"] != "lane", "a bystander switch woke an off-lane watch"
    assert result["reason"] == "quiet"


def test_a_switch_to_my_lane_still_resolves_my_watch(monkeypatch, tmp_path):
    """The necessary case must survive: he comes to kepler, kepler's watch resolves so it can take
    the floor."""
    on_magnus = {**_IDLE, "lane": "magnus"}
    to_kepler = {**_IDLE, "lane": "kepler"}
    result = _run(monkeypatch, tmp_path, on_magnus, to_kepler, "kepler", timeout=5.0)
    assert result["reason"] == "lane"


def test_a_switch_away_from_my_lane_still_resolves_my_watch(monkeypatch, tmp_path):
    """The other necessary case: he leaves kepler, so kepler wakes to raise a hand."""
    on_kepler = {**_IDLE, "lane": "kepler"}
    to_magnus = {**_IDLE, "lane": "magnus"}
    result = _run(monkeypatch, tmp_path, on_kepler, to_magnus, "kepler", timeout=5.0)
    assert result["reason"] == "lane"
    assert result.get("on_lane") is False, "he left my lane — I should be told I am off-lane"
