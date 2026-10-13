"""The fleet payload keeps each lane's FRESHEST registry row, not its last one (JJ, 2026-09-28).

"I have noticed other agents are also editing files, but I don't see them drawn in the repo canvas."
The registry keeps one file per herdr PANE, and a lane relaunched into a new pane leaves its old pane
files behind (26 rows for 6 lanes on 2026-09-28). `_fleet_activity` used plain last-write-wins, so a
dead duplicate — an old session with nothing in the window — overwrote the live row, and kepler's 29
edited files reached the page as none.
"""
from command_bridge import fleet, server


def _row(lane, pane, files, epoch):
    return {"lane": lane, "pane": pane, "harness": "claude-code", "registered": True,
            "activity": {"files": {f"f{i}.py": {} for i in range(files)},
                         "last_activity_epoch": epoch,
                         "last_activity": None if epoch is None else "t"}}


def test_a_dead_duplicate_listed_last_does_not_blank_the_live_row(monkeypatch):
    board = {"server_epoch": 1.0, "lanes": [
        _row("kepler", "w6:p2F", 29, 1790611624.0),   # the live session
        _row("magnus", "w6:p1", 13, 1790611700.0),
        _row("kepler", "w6:pT", 0, None),             # a stale pane file, listed LAST
    ]}
    monkeypatch.setattr(fleet, "cached_board", lambda *a, **k: board)
    monkeypatch.setattr(server, "_canvas_frame_lanes", lambda: set())
    lanes = server._fleet_activity()["lanes"]
    assert len(lanes["kepler"]["files"]) == 29
    assert len(lanes["magnus"]["files"]) == 13


def test_the_newer_of_two_live_rows_wins_whatever_the_order(monkeypatch):
    board = {"server_epoch": 1.0, "lanes": [
        _row("atlas", "w6:p2E", 6, 200.0),
        _row("atlas", "w6:p1A", 2, 100.0),
    ]}
    monkeypatch.setattr(fleet, "cached_board", lambda *a, **k: board)
    monkeypatch.setattr(server, "_canvas_frame_lanes", lambda: {"atlas"})
    lanes = server._fleet_activity()["lanes"]
    assert len(lanes["atlas"]["files"]) == 6
    assert lanes["atlas"]["has_frame"] is True
