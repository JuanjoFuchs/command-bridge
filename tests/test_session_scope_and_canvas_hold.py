"""Two live fixes from the 2026-10-13 demo prep, both in the page.

1. The transcript is scoped to the server's SESSION. JJ, after a restart onto `serve --session demo`:
   *"The transcript panel is not filtered to the session. It's showing the previous session."* The
   server's `/history` was already per-session; the page replayed its per-browser localStorage log and
   kept it whenever `/history` came back empty — and the old rows' turn ids (the new session restarts
   at 0) made `addRow`'s dedup swallow the new session's turns.

2. A camera move holds the canvas until his ruling. JJ: *"When you show me something in the canvas and
   I have to make a ruling on that, maybe we shouldn't hide the canvas as soon as your turn ends, but
   until I give you that ruling."* A frame brought on screen with `point --look` stays open past the
   clip end and closes on his next turn on that lane; a plain edit still hides after the clip.

The page's own functions are pulled out of `index.html` and run under node (skipped without node).
"""
import json
import pathlib
import shutil
import subprocess

import pytest

PAGE = pathlib.Path(__file__).resolve().parents[1] / "command_bridge" / "web" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_source(name: str) -> str:
    start = HTML.index(f"function {name}(")
    end = HTML.index("\n  }\n", start) + len("\n  }")
    return HTML[start:end]


def _node(prog: str):
    assert NODE is not None
    out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=20, check=True)
    return json.loads(out.stdout)


# --- 1. transcript scoped to the session ---------------------------------------------------------

@needs_node
@pytest.mark.parametrize("saved,session,belongs", [
    ("demo", "demo", True),
    ("dev", "demo", False),     # the reported bug: the old session's rows on the new session
    (None, "demo", False),      # an older build never recorded a session: drop, /history rebuilds
    ("dev", None, True),        # a ready with no session (mock mode, older server) keeps the log
])
def test_a_saved_log_belongs_only_to_its_own_session(saved, session, belongs):
    src = _function_source("logBelongsTo")
    prog = src + f"\nprocess.stdout.write(JSON.stringify(logBelongsTo({json.dumps(saved)}, {json.dumps(session)})));"
    assert _node(prog) is belongs


def test_ready_adopts_the_session_before_history_rebuilds():
    ready = HTML[HTML.index('} else if (msg.type === "ready") {'):]
    assert ready.index("adoptLogSession(msg.session)") < ready.index("loadHistory()")
    adopt = _function_source("adoptLogSession")
    assert "logBelongsTo(saved, session)" in adopt and 'querySelectorAll(".row")' in adopt


def test_an_empty_history_still_clears_the_saved_rows():
    load = _function_source("loadHistory")
    assert "!rows.length" not in load   # empty used to return early and keep another session's rows
    assert load.index("if (!Array.isArray(rows)) return;") < load.index('querySelectorAll(".row")')


# --- 2. a camera move holds the canvas until his turn --------------------------------------------

_SIM = r"""
let autoCanvas = null, canvasHeld = null, autoCanvasT = 0, liveLane = "atlas", defaultLane = "atlas";
let playingLane = null, clipQueue = [], open = false;
const canvasNews = {}, canvasLook = {};
const document = { body: { classList: { contains: () => open } } };
function openCanvas() { autoCanvas = null; open = true; }
function closeCanvas() { autoCanvas = null; canvasHeld = null; open = false; }
const AUTO_CANVAS_LINGER_MS = 0;
const timers = [];
function setTimeout(fn) { timers.push(fn); return timers.length; }
function clearTimeout() {}
""" + "\n".join(_function_source(n) for n in (
    "canvasEventIsLook", "autoCanvasOnClip", "autoCanvasOnTurn", "autoCanvasAfterClip"))


def _run(steps: str):
    return _node(_SIM + "\n" + steps + "\nprocess.stdout.write(JSON.stringify(trace));")


@needs_node
def test_canvas_event_is_look_only_for_an_explicit_camera_move():
    prog = _SIM + """
process.stdout.write(JSON.stringify([
  canvasEventIsLook({kind: "point", selector: "#f1", look: true}),
  canvasEventIsLook({kind: "look", id: "f1"}),
  canvasEventIsLook({kind: "point", selector: "#f1", look: false}),
  canvasEventIsLook({kind: "point", selector: "", look: true}),
  canvasEventIsLook({kind: "frame", id: "f1"}),
]));"""
    assert _node(prog) == [True, True, False, False, False]


@needs_node
def test_a_point_look_keeps_the_canvas_open_past_the_clip_until_his_turn():
    trace = _run("""
const trace = [];
canvasNews.atlas = 1; canvasLook.atlas = true;          // agent ran `point f1 --look`, then `say`
autoCanvasOnClip({lane: "atlas"}); trace.push(open);    // clip starts: canvas opens
autoCanvasAfterClip({lane: "atlas"}); timers.forEach(f => f()); trace.push(open);  // clip ends: still open
autoCanvasOnTurn("vega"); trace.push(open);             // a turn on another lane is not his ruling
autoCanvasOnTurn("atlas"); trace.push(open);            // his ruling on this lane: it closes
""")
    assert trace == [True, True, True, False]


@needs_node
def test_a_plain_edit_still_hides_when_the_clip_ends():
    trace = _run("""
const trace = [];
canvasNews.atlas = 1;                                   // a frame edit, no camera move
autoCanvasOnClip({lane: "atlas"}); trace.push(open);
autoCanvasAfterClip({lane: "atlas"}); timers.forEach(f => f()); trace.push(open);
""")
    assert trace == [True, False]


def test_the_point_event_carries_its_look_flag_to_the_page():
    assert 'tell("point", { lane: d.lane, selector: d.selector, look: Boolean(d.look || d.zoom) })' in HTML


def test_his_final_turn_releases_the_hold():
    turn = HTML[HTML.index('if (msg.type === "turn") {'):HTML.index('} else if (msg.type === "ready") {')]
    assert "autoCanvasOnTurn(" in turn
