"""Held clips survive a graceful restart (JJ, 2026-09-24; backlog 6277).

A restart used to wipe `state.lane_held` (in-memory), so JJ came back to empty lanes — "there are no
pending turns on any of the lanes". A graceful restart now pickles the held clips to a sibling of the
runtime file and the next startup reloads them, so the agents' raised hands survive. A stale leftover
(older than the restart window) is discarded, so clips from a crash long ago never resurrect.
"""
import asyncio
import os
import pickle
import time

from command_bridge import server


def _clip(cid: str, lane: str) -> dict:
    return {"header": {"id": cid, "lane": lane, "text": "done"}, "pcm": b"\x00\x01", "at": time.time()}


def test_held_clips_survive_a_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    state = server.TunnelState("dev", "tok", True)
    state.lane_held = {"atlas": [_clip("c1", "atlas")], "vega": [_clip("c2", "vega")]}
    server._persist_held(state)
    assert os.path.exists(server._held_path("dev"))

    # A fresh server comes up and restores the hands.
    fresh = server.TunnelState("dev", "tok", True)
    asyncio.run(server._restore_held_on_startup({"state": fresh}))
    assert set(fresh.lane_held) == {"atlas", "vega"}
    assert fresh.lane_held["atlas"][0]["header"]["id"] == "c1"
    # The file is consumed, so the boot after this one does not resurrect the same clips.
    assert not os.path.exists(server._held_path("dev"))


def test_clips_said_to_nobody_survive_a_restart_too(monkeypatch, tmp_path):
    """JJ, 2026-09-29: held clips should *"survive restarts or toggles"* — that includes a clip said
    while no page was connected (the `undelivered` queue), which a restart used to drop silently."""
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    state = server.TunnelState("dev", "tok", True)
    state.undelivered = [dict(_clip("u1", "magnus"), lane="magnus")]
    server._persist_held(state)                     # nothing in lane_held, still written
    assert os.path.exists(server._held_path("dev"))
    fresh = server.TunnelState("dev", "tok", True)
    asyncio.run(server._restore_held_on_startup({"state": fresh}))
    assert [c["header"]["id"] for c in fresh.undelivered] == ["u1"]


def test_a_stale_held_file_is_discarded(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    os.makedirs(server.config.session_dir(), exist_ok=True)
    with open(server._held_path("dev"), "wb") as fh:
        pickle.dump({"saved_at": time.time() - 10_000, "lane_held": {"atlas": [_clip("old", "atlas")]}}, fh)
    fresh = server.TunnelState("dev", "tok", True)
    asyncio.run(server._restore_held_on_startup({"state": fresh}))
    assert not fresh.lane_held                      # too old -> not restored
    assert not os.path.exists(server._held_path("dev"))   # and cleaned up


def test_no_held_file_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    fresh = server.TunnelState("dev", "tok", True)
    asyncio.run(server._restore_held_on_startup({"state": fresh}))
    assert not fresh.lane_held


def test_a_plain_stop_persists_nothing(monkeypatch, tmp_path):
    """`_persist_held` is only called on the restart path; with nothing held it writes no file."""
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    state = server.TunnelState("dev", "tok", True)
    server._persist_held(state)                     # empty lane_held
    assert not os.path.exists(server._held_path("dev"))
