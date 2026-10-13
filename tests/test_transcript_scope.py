"""The transcript shows only the lane he is on, with a dropdown to show every lane (spec 026).

JJ, 2026-10-08, by voice: *"I would like to make it less wide. And I would like it to filter to the
agent that I am on. Instead of showing every transcript for every agent."* · *"like a drop down next to
the [transcript] button like we do with the microphone and the speaker."*

The page's own `logScopeView` is pulled out of `index.html` and run under node when node is on PATH (so
the test exercises the shipped function, not a port); without node those cases skip and the structural
assertions still run.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

PAGE = pathlib.Path(__file__).resolve().parents[1] / "command_bridge" / "web" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    start = HTML.index(f"function {name}(")
    # The function body ends at the first line that is exactly two-space-indented `}`.
    end = HTML.index("\n  }\n", start) + len("\n  }")
    return HTML[start:end]


NODE = shutil.which("node")


def _scope(**m) -> bool:
    src = _function_source("logScopeView")
    prog = src + "\nprocess.stdout.write(JSON.stringify(logScopeView(" + json.dumps(m) + ")));"
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")
BASE = {"live": "atlas", "scope": "lane", "broadcast": "everyone", "laneCount": 5}


@needs_node
@pytest.mark.parametrize("row,shown", [("atlas", True), ("vega", False), (None, True), ("everyone", True)])
def test_ac1_lane_scope_shows_the_live_lane_unlaned_and_broadcast_rows(row, shown):
    assert _scope(rowLane=row, **BASE) is shown


@needs_node
def test_ac2_all_scope_live_broadcast_and_no_live_show_everything():
    assert _scope(rowLane="vega", **{**BASE, "scope": "all"}) is True
    assert _scope(rowLane="vega", **{**BASE, "live": "everyone"}) is True
    assert _scope(rowLane="vega", **{**BASE, "live": None}) is True


@needs_node
def test_ac3_below_two_lanes_every_row_shows():
    assert _scope(rowLane="vega", **{**BASE, "laneCount": 1}) is True


def test_ac3_the_dropdown_hides_below_two_lanes():
    painter = _function_source("applyLogScope")
    assert "pick.hidden = lanes.length < 2" in painter


def test_ac4_the_dropdown_sits_right_after_the_transcript_button():
    after = HTML[HTML.index('<button id="collapse"'):]
    after = after[after.index("</button>") + len("</button>"):]
    first_element = re.search(r"<(label|button|div|select)\b[^>]*>", after).group(0)
    assert 'id="logpick"' in first_element and 'class="pick"' in first_element
    pick = after[: after.index("</label>")]
    assert 'id="logscope"' in pick and "aria-label=" in pick
    assert 'value="lane"' in pick and 'value="all"' in pick


def test_ac5_scope_persists_under_its_key_and_defaults_to_lane():
    getter = _function_source("getLogScope")
    assert '"command-bridge.logscope"' in getter
    assert '=== "all" ? "all" : "lane"' in getter   # anything but "all" (incl. empty/throwing) -> lane
    assert 'LOGSCOPE_KEY = "command-bridge.logscope"' in HTML


def test_ac6_the_fleet_transcript_column_is_narrower():
    assert "body.sharing.fleet { grid-template-columns:1fr minmax(210px,20%); }" in HTML


def test_rows_are_hidden_not_removed_and_scoped_on_arrival():
    assert ".row.offscope { display:none; }" in HTML
    add_row = _function_source("addRow")
    assert "scopeRow(d)" in add_row
    assert "applyLogScope()" in _function_source("applyLanes")


def test_exactly_one_function_toggles_offscope():
    # The orb/lanes/pills single-writer rule: only scopeRow paints the class, only applyLogScope calls it
    # over the whole log.
    assert len(re.findall(r'classList\.toggle\("offscope"', HTML)) == 1
