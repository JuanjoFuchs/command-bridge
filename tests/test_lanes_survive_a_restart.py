"""Spec 019 — a restart must not un-invite the room.

**Found 2026-08-24 answering his question** *"will restarting the tunnel kill the other lanes? Will
it impact the other agents?"* — the answer was yes on both counts. **Paid four times on 2026-08-25**,
where every restart needed `lane add atlas` and `lane add kepler` typed again by hand.

⚠ **The loss is easy to miss, which is why it survived so long.** The turn log is on disk and comes
back intact, so the CONVERSATION is all there and only the ROOM is gone. The cost is paid by the
other agents, who cannot see it happen: they get a dead socket and no reason.

NO SERVER IS STARTED HERE — `TunnelState` is constructed directly, which is exactly what `serve`
does and therefore exactly where the restore has to live.
"""
import asyncio

import pytest

from command_bridge import server, store


class _Req:
    def __init__(self, state, body):
        self.app = {"state": state}
        self._body = body
        self.remote = "127.0.0.1"
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "magnus")
    return tmp_path


def lane(state, action, name):
    return asyncio.run(server.handle_lane(_Req(state, {"action": action, "name": name})))


def restart(session="probe"):
    """What `serve` does on a fresh start — no more, no less."""
    return server.TunnelState(session, token=None)


# ------------------------------------------------------------------ AC-1

def test_a_lane_added_before_a_restart_is_still_there_after_it(env):
    """AC-1 — FR1, FR2. The whole bug, in four lines."""
    st = restart()
    lane(st, "add", "atlas")
    lane(st, "add", "kepler")
    assert list(st.lanes.names) == ["magnus", "atlas", "kepler"]

    assert list(restart().lanes.names) == ["magnus", "atlas", "kepler"], (
        "the room was un-invited by the restart; every one of those agents' watches died with the "
        "socket and nothing told them why"
    )


def test_a_removed_lane_stays_removed(env):
    """AC-2 — FR2. Persisting on ADD alone would make `lane remove` a suggestion that a restart
    silently reverses, which is the same defect pointing the other way."""
    st = restart()
    lane(st, "add", "atlas")
    lane(st, "add", "kepler")
    lane(st, "remove", "kepler")

    assert list(restart().lanes.names) == ["magnus", "atlas"]


# ------------------------------------------------------------------ AC-3

def test_the_wake_name_still_decides_the_default(env, monkeypatch):
    """AC-3 — TC1. ⚠ **The default is NOT restored, only the guests.** `--wake` is what he typed on
    THIS start; letting a persisted default override it would turn the flag into a suggestion and
    make the lane a turn belongs to depend on a file he cannot see."""
    st = restart()
    lane(st, "add", "atlas")

    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "dexter")
    after = restart()

    assert after.lanes.default == "dexter", "the flag he typed wins"
    assert "atlas" in after.lanes.names, "and the guests still come back"
    assert "magnus" in after.lanes.names, (
        "including the old default, which is now an ordinary lane rather than a lost one — turns "
        "already stamped `lane: magnus` must still reach somebody"
    )


# ------------------------------------------------------------------ AC-4

def test_a_single_agent_session_writes_nothing_and_restores_nothing(env):
    """AC-4 — NFR1. A session that never registered a second lane must behave exactly as before,
    including not growing a file beside its log."""
    st = restart()
    assert list(st.lanes.names) == ["magnus"]
    assert not (env / "probe.lanes.json").exists(), (
        "nothing was registered, so nothing should have been written"
    )
    assert list(restart().lanes.names) == ["magnus"]


def test_a_corrupt_or_stale_file_does_not_stop_the_server_starting(env):
    """AC-5 — TC2. The file is written best-effort and read on the start path, so a truncated
    write, a hand-edit or a name that is no longer valid must degrade to "no guests" rather than
    to a server that will not come up. A tunnel that refuses to start is worse than one that asks
    him to re-add a lane — which is precisely the behaviour this spec replaces."""
    (env / "probe.lanes.json").write_text("{ not json", encoding="utf-8")
    assert list(restart().lanes.names) == ["magnus"]

    store.write_lanes("probe", ["magnus", "Has Spaces", "atlas"])
    names = list(restart().lanes.names)
    assert names == ["magnus", "atlas"], f"the invalid name is skipped, not fatal: {names}"
