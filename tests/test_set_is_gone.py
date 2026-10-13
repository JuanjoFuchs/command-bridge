"""Spec 022: `set`, `look`, `zoom`, `remove` and `clear` are retired — a frame is a file.

Drawing through `set` copied the agent's content into the store (so it lived twice, the kept copy an
escaped JSON string with no language tooling), and `look` was the separate step agents kept
forgetting. Now an agent writes the frame as a native file into its lane folder and the watcher
renders, focuses and removes it. These tests pin the retirement the same way `test_drain_is_gone.py`
pins `drain`'s: the old names are refused with a remedy that says what to do INSTEAD, and appear
nowhere an agent reads the contract (AC9).

The wall-of-text advisory that used to ride on `set`'s return survives: `status` carries it per
frame now, and its pure rules are unchanged (the tests at the bottom are the ones that moved here).
"""
import json

import pytest

from command_bridge import cli

RETIRED = ("set", "look", "zoom", "remove", "clear")


@pytest.mark.parametrize("verb", RETIRED)
def test_a_retired_canvas_verb_is_refused_with_what_to_do_instead(verb, capsys):
    code = cli.main([verb, "--lane", "magnus"])
    assert code == cli.EXIT_USAGE
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "unknown_command"
    assert err["remedy"] and "command-bridge describe" not in err["remedy"]
    assert "spec 022" in err["why"]


def test_set_points_at_the_lane_folder():
    replacement, why, remedy = cli.RETIRED_COMMANDS["set"]
    assert "lane folder" in replacement and "canvas.folder" in remedy
    assert ".mmd" in why and "watcher" in why


@pytest.mark.parametrize("verb", RETIRED)
def test_a_retired_verb_is_gone_from_describe_and_the_parser(verb):
    assert verb not in cli.DESCRIBE["commands"]
    sub = next(a for a in cli.build_parser()._actions if a.__class__.__name__ == "_SubParsersAction")
    assert verb not in sub.choices


def test_the_agent_canvas_surface_is_point_and_shot():
    cmds = cli.DESCRIBE["commands"]
    for verb in ("point", "cue", "shot", "say", "relayout", "canvas-doctor"):
        assert verb in cmds, verb
    notes = cmds["point"]["notes"]
    assert "lane folder" in notes and ".mmd" in notes and "_<name>" in notes


# ---- the wall-of-text advisory (JJ, 2026-09-15), now carried by `status` ----------------------

def test_prose_over_the_threshold_trips_the_nudge():
    prose = "This is a long paragraph of running prose that keeps going and going. " * 6  # ~78 words
    nudge = cli._wall_of_text(prose, "markdown")
    assert nudge is not None and "mermaid" in nudge, "a prose wall must be nudged toward a diagram"


def test_a_structured_list_does_not_trip():
    listy = "\n".join(f"- item number {i} with a few words on it here" for i in range(12))
    assert cli._wall_of_text(listy, "markdown") is None, "a structured list is not a prose wall"


def test_a_headed_table_does_not_trip():
    doc = "# Title\n\n| a | b |\n| 1 | 2 |\n| 3 | 4 |\n\n" + ("more cells | and text | here | " * 4)
    assert cli._wall_of_text(doc, "markdown") is None, "headings + a table are structure, not prose"


def test_a_short_frame_does_not_trip():
    assert cli._wall_of_text("a short caption under the threshold", "text") is None


def test_diagram_kinds_are_exempt():
    prose = "word " * 100
    assert cli._wall_of_text(prose, "mermaid") is None
    assert cli._wall_of_text(prose, "svg") is None
    assert cli._wall_of_text(prose, "html") is None, "html is often media/structured; not covered"
