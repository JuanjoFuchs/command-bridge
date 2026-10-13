"""Lane colours come from the agent roster by NAME (JJ, 2026-10-01: *"I can clearly see blue and think
magnus and think command bridge"*), not from a lane's position in the list."""
import json

from command_bridge import config


def test_colours_are_read_by_name_from_the_roster(monkeypatch, tmp_path):
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"agents": {
        "magnus": {"hue": "#3559bd"}, "sirius": {"hue": "#9a7b12"}, "nohue": {"role": "x"}}}),
        encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_ROSTER", str(roster))
    assert config.lane_colors() == {"magnus": "#3559bd", "sirius": "#9a7b12"}


def test_roles_are_read_by_name_from_the_roster(monkeypatch, tmp_path):
    """The card shows each agent's role (JJ, 2026-10-05), from the same roster file."""
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"agents": {
        "rigel": {"hue": "#7fae2e", "role": "reviewer"}, "nobody": {"hue": "#000"}}}), encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_ROSTER", str(roster))
    assert config.lane_roles() == {"rigel": "reviewer"}
    roster.write_text("{broken", encoding="utf-8")
    assert config.lane_roles() == {}


def test_the_roster_can_live_in_a_markdown_note(monkeypatch, tmp_path):
    """JJ keeps the roster written down once, in a markdown note, as a ```json block."""
    note = tmp_path / "example-roster.md"
    note.write_text("# Roster\n\nprose\n\n```json\n"
                    + json.dumps({"agents": {"magnus": {"hue": "#3b6fe0"}}})
                    + "\n```\n\nmore prose\n", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_ROSTER", str(note))
    assert config.lane_colors() == {"magnus": "#3b6fe0"}
    note.write_text("# no block here\n", encoding="utf-8")
    assert config.lane_colors() == {}


def test_no_roster_or_a_broken_one_means_positional_colours(monkeypatch, tmp_path):
    monkeypatch.delenv("COMMAND_BRIDGE_ROSTER", raising=False)
    assert config.lane_colors() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("COMMAND_BRIDGE_ROSTER", str(bad))
    assert config.lane_colors() == {}, "a bad roster must never stop the server"
