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
let autoCanvas = null, canvasHeld = null, canvasHeldAfterClip = false, agentOpened = null;
let autoCanvasT = 0, liveLane = "atlas", defaultLane = "atlas";
let playingLane = null, clipQueue = [], open = false;
const canvasNews = {}, canvasLook = {};
const document = { body: { classList: { contains: () => open } } };
function openCanvas() { autoCanvas = null; open = true; }
function closeCanvas() { autoCanvas = null; canvasHeld = null; agentOpened = null; open = false; }
const AUTO_CANVAS_LINGER_MS = 0;
const timers = [];
function setTimeout(fn) { timers.push(fn); return timers.length; }
function clearTimeout() {}
function endClip(lane) { autoCanvasAfterClip({lane}); timers.splice(0).forEach(f => f()); }
""" + "\n".join(_function_source(n) for n in (
    "canvasEventIsLook", "autoCanvasOnClip", "autoCanvasOnTurn", "canvasShow", "autoCanvasAfterClip"))


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
autoCanvasOnTurn("atlas"); trace.push(open);            // a turn landing mid-clip is not a ruling on it
endClip("atlas"); trace.push(open);                     // clip ends: still open
autoCanvasOnTurn("vega"); trace.push(open);             // a turn on another lane is not his ruling
autoCanvasOnTurn("atlas"); trace.push(open);            // his ruling on this lane: it closes
""")
    assert trace == [True, True, True, True, False]


@needs_node
def test_the_ruling_closes_it_even_after_he_clicked_into_the_canvas():
    # The 2026-10-13 live miss: a click into the frame clears `autoCanvas` (the blur rule), and the
    # first fix only closed when `autoCanvas` still named the lane — so three turns later it was up.
    trace = _run("""
const trace = [];
canvasNews.atlas = 1; canvasLook.atlas = true;
autoCanvasOnClip({lane: "atlas"});
autoCanvas = null;                                      // he clicked into the canvas to look
endClip("atlas"); trace.push(open);
autoCanvasOnTurn("atlas"); trace.push(open);
""")
    assert trace == [True, False]


@needs_node
def test_a_look_holds_a_canvas_that_was_already_open():
    trace = _run("""
const trace = [];
open = true;                                            // already showing (he opened it)
canvasNews.atlas = 1; canvasLook.atlas = true;
autoCanvasOnClip({lane: "atlas"}); endClip("atlas"); trace.push(open);
autoCanvasOnTurn("atlas"); trace.push(open);
""")
    assert trace == [True, False]


@needs_node
def test_a_plain_edit_never_opens_the_canvas_for_a_clip():
    # JJ, 2026-10-13: "whenever Phoenix speaks, its canvas opens, and there's nothing recent it
    # should show me". Only an explicit look (or `canvas open`) opens it now.
    trace = _run("""
const trace = [];
canvasNews.atlas = 1;                                   // a frame edit, no camera move
autoCanvasOnClip({lane: "atlas"}); trace.push(open);
endClip("atlas"); trace.push(open);
""")
    assert trace == [False, False]


@needs_node
def test_canvas_open_stays_until_canvas_close_and_only_on_the_live_lane():
    trace = _run("""
const trace = [];
canvasShow({lane: "vega", action: "open"}); trace.push(open);    // not his lane: ignored, no switch
canvasShow({lane: "atlas", action: "open"}); trace.push(open);
canvasNews.atlas = 1; canvasLook.atlas = true;                   // a look + clip on top of it
autoCanvasOnClip({lane: "atlas"}); endClip("atlas"); trace.push(open);
autoCanvasOnTurn("atlas"); trace.push(open);                     // his turn does not close an explicit open
canvasShow({lane: "atlas", action: "close"}); trace.push(open);
""")
    assert trace == [False, True, True, True, False]


def _canvas_fn(name: str) -> str:
    from command_bridge.canvas import page
    src = page.PAGE
    start = src.index(f"function {name}(")
    return src[start:src.index("\n  }\n", start) + len("\n  }")]


@needs_node
@pytest.mark.parametrize("target,by_agent,lands_on", [
    ("outline", True, ["zoom:outline"]),   # liaison's `point outline --look`, then the canvas opens
    ("outline", False, ["zoom:outline"]),
    (None, False, ["auto:link-1"]),        # HIS button, nothing pointed at: the latest edit, as before
    (None, True, []),                      # an agent's open with no target: never an old frame
])
def test_opening_the_canvas_lands_on_the_explicit_target_not_the_latest_edit(target, by_agent, lands_on):
    # 2026-10-13, liaison mode: "your update to the Lyra frame stole the camera away from the outline";
    # and "you were showing me an old frame".
    prog = """
let live = "magnus", pendingFocus = null, openByAgent = """ + ("true" if by_agent else "false") + """;
const calls = [];
function layout() {}
function latestCardId() { return "link-1"; }
function autoFocus(id) { calls.push("auto:" + id); }
function zoomTo(el) { calls.push("zoom:" + el.id); }
let explicitTarget = %s;
%s
focusLatestOnOpen();
process.stdout.write(JSON.stringify(calls));""" % (
        "null" if target is None
        else '{lane: "magnus", el: {id: "%s", isConnected: true}, margin: 24}' % target,
        _canvas_fn("focusLatestOnOpen"))
    assert _node(prog) == lands_on


def test_explicit_moves_are_remembered_and_only_a_focusing_edit_clears_them():
    from command_bridge.canvas import page
    src = page.PAGE
    calls = src.count("rememberExplicit(el, ") - src.count("function rememberExplicit(")
    assert calls == 3   # point --look, look, cue --look
    frame = src[src.index('es.addEventListener("frame"'):src.index('es.addEventListener("relayout"')]
    focus_branch = frame[frame.index("if (msg.focus && lane === live"):]
    assert "explicitTarget = null" in focus_branch


def test_pointing_at_a_frame_outlines_it_and_never_recolours_its_insides():
    # JJ, 2026-10-13: "you're highlighting every word and every line inside of the frame".
    import re
    from command_bridge.canvas import page
    css = page.PAGE[:page.PAGE.index("</style>")]
    descendant = re.findall(r"\.pointed[^,{]*\s+(?:rect|polygon|circle|path|text|tspan)\b", css)
    assert descendant, "the descendant recolour rules moved — update this test"
    for sel in descendant:
        assert ":not([data-frame])" in sel, sel
    # ...and a frame id still resolves to the card itself, which carries data-frame.
    assert "el.dataset.frame = id;" in page.PAGE
    assert "if (card) return card;" in page.PAGE


def test_the_show_route_relays_open_and_close_and_rejects_anything_else(monkeypatch):
    from command_bridge.canvas import server as canvas
    assert canvas.Handler.OPS["show"] == "/show"
    sent = []
    monkeypatch.setattr(canvas, "publish", lambda ev, p: sent.append((ev, p)) or 1)
    code, body = canvas.Handler.apply(None, "/show", {"lane": "magnus", "action": "open"})  # type: ignore[arg-type]
    assert code == 200 and body["delivered_to"] == 1
    assert sent == [("show", {"lane": "magnus", "action": "open"})]
    code, body = canvas.Handler.apply(None, "/show", {"lane": "magnus", "action": "toggle"})  # type: ignore[arg-type]
    assert code == 400 and body["code"] == "invalid_input"


def test_the_canvas_verb_is_in_describe():
    from command_bridge import cli
    assert "canvas" in cli.DESCRIBE["commands"]
    assert "action" in cli.DESCRIBE["commands"]["canvas"]["args"]


def test_agent_opens_tell_the_canvas_they_are_the_agents():
    assert "openCanvas(lane, true)" in _function_source("autoCanvasOnClip")
    assert "openCanvas(lane, true)" in _function_source("canvasShow")
    assert "explicit: Boolean(byAgent)" in _function_source("openCanvas")


def test_the_point_event_carries_its_look_flag_to_the_page():
    assert 'tell("point", { lane: d.lane, selector: d.selector, look: Boolean(d.look || d.zoom) })' in HTML


def test_his_final_turn_releases_the_hold():
    turn = HTML[HTML.index('if (msg.type === "turn") {'):HTML.index('} else if (msg.type === "ready") {')]
    assert "autoCanvasOnTurn(" in turn
