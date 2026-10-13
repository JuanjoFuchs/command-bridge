"""Spec 045: hide repo boxes from the fleet view on request.

JJ, voice, 2026-10-13, before a public demo: "just a way to ask you to clean the ribbon, say which
boxes to remove. Just that." The liaison hides named repos through the CLI; the server drops them
from the page's fleet payload; the list persists in the session dir.
"""
import json
import os

from command_bridge import cli, fleet, server
from tests.test_cli_surface import run as _run_cli


def run(argv, capsys) -> tuple[int, dict, str]:
    code, out, err = _run_cli(argv, capsys)
    assert isinstance(out, dict), err                             # every fleet result is a JSON object
    return code, out, err


def _board():
    def f(repo, in_repo=True):
        return {"read": 1, "edit": 0, "write": 0, "t": 1.0, "repo": repo, "in_repo": in_repo,
                "dirty": False}
    return {"server_epoch": 1.0, "window_hours": 24, "lanes": [
        {"lane": "magnus", "pane": "p1", "harness": "claude-code", "registered": True,
         "activity": {"last_activity_epoch": 10.0, "files": {
             "D:/w/acme-billing/a.py": f("acme-billing"),
             "D:/w/command-bridge/b.py": f("command-bridge"),
             "C:/tmp/x.txt": f(None, in_repo=False)}}},
        {"lane": "rigel", "pane": "p2", "harness": "claude-code", "registered": True,
         "activity": {"last_activity_epoch": 9.0, "files": {
             "D:/w/acme-billing/c.py": f("acme-billing"),
             "D:/w/team-notes/d.md": f("team-notes")}}},
    ]}


def _fake_board(monkeypatch):
    monkeypatch.setattr(fleet, "cached_board", lambda *a, **k: _board())


def test_hide_list_unhide_round_trip_persists(tmp_sessions, monkeypatch, capsys):
    _fake_board(monkeypatch)
    code, out, _ = run(["fleet", "hide", "ACME-billing", "team-notes"], capsys)
    assert code == 0
    assert out["added"] == ["acme-billing", "team-notes"]     # the box's spelling, not the typed one
    assert "unknown" not in out

    # on disk, beside the session state, so a reload or a restart keeps it
    path = os.path.join(tmp_sessions, ".agent-fleet", "hidden.json")
    assert json.load(open(path, encoding="utf-8"))["repos"] == ["acme-billing", "team-notes"]

    code, out, _ = run(["fleet", "hidden"], capsys)
    assert code == 0 and out["hidden"] == ["acme-billing", "team-notes"]

    code, out, _ = run(["fleet", "hide", "acme-billing"], capsys)   # a repeat is a no-op
    assert code == 0 and out["added"] == [] and out["hidden"] == ["acme-billing", "team-notes"]

    code, out, _ = run(["fleet", "unhide", "Team-Notes", "nope"], capsys)
    assert code == 0
    assert out["removed"] == ["team-notes"] and out["hidden"] == ["acme-billing"]
    assert out["not_hidden"] == ["nope"]

    code, out, _ = run(["fleet", "unhide", "--all"], capsys)
    assert code == 0 and out["hidden"] == [] and out["removed"] == ["acme-billing"]
    assert fleet.load_hidden() == []


def test_unknown_names_are_reported_and_never_stored(tmp_sessions, monkeypatch, capsys):
    _fake_board(monkeypatch)
    code, out, _ = run(["fleet", "hide", "command-bridge", "acme billing"], capsys)
    assert code == 0                                              # partial: one real box hidden
    assert out["added"] == ["command-bridge"] and out["unknown"] == ["acme billing"]
    assert "acme-billing" in out["known"]
    assert fleet.load_hidden() == ["command-bridge"]

    code, out, _ = run(["fleet", "hide", "north-oregon"], capsys)  # nothing matched: a failure
    assert code == 2
    assert out["code"] == "unknown_repo" and out["error"] and "fleet hide" in out["remedy"]
    assert fleet.load_hidden() == ["command-bridge"]


def test_unhide_with_no_names_and_no_all_is_bad_input(tmp_sessions, capsys):
    code, out, _ = run(["fleet", "unhide"], capsys)
    assert code == 2 and out["code"] == "invalid_input" and "--all" in out["remedy"]


def test_bare_fleet_still_reports_every_repo(tmp_sessions, monkeypatch, capsys):
    _fake_board(monkeypatch)
    fleet.hide_repos(["acme-billing"], fleet.known_repos(_board()))
    code, out, _ = run(["fleet", "--hours", "3"], capsys)
    assert code == 0 and out["hidden"] == ["acme-billing"]
    assert "D:/w/acme-billing/a.py" in out["lanes"][0]["activity"]["files"]


def test_the_page_payload_drops_hidden_repos_case_insensitively(tmp_sessions, monkeypatch):
    _fake_board(monkeypatch)
    monkeypatch.setattr(server, "_canvas_frame_lanes", lambda: set())
    before = server._fleet_activity()["lanes"]
    assert "D:/w/acme-billing/c.py" in before["rigel"]["files"]

    fleet.hide_repos(["Acme-Billing"], fleet.known_repos(_board()))
    lanes = server._fleet_activity()["lanes"]               # re-read from disk: no restart
    assert set(lanes["magnus"]["files"]) == {"D:/w/command-bridge/b.py", "C:/tmp/x.txt"}
    assert set(lanes["rigel"]["files"]) == {"D:/w/team-notes/d.md"}
    assert lanes["rigel"]["files_touched"] == 1

    # the filter itself compares case-insensitively, whatever spelling was stored
    assert fleet.filter_hidden(before, ["TEAM-NOTES"])["rigel"]["files"] == {
        "D:/w/acme-billing/c.py": before["rigel"]["files"]["D:/w/acme-billing/c.py"]}

    fleet.unhide_repos([], everything=True)
    assert "D:/w/acme-billing/c.py" in server._fleet_activity()["lanes"]["rigel"]["files"]


def test_a_corrupt_hidden_file_hides_nothing(tmp_sessions):
    os.makedirs(os.path.dirname(fleet.hidden_path()), exist_ok=True)
    with open(fleet.hidden_path(), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert fleet.load_hidden() == []


def test_describe_lists_the_new_verbs():
    args = cli.DESCRIBE["commands"]["fleet"]["args"]
    for verb in ("hide", "unhide", "hidden"):
        assert verb in args
