"""The meeting page (spec 006): one page where JJ's agents are participant orbs, the live one draws
on the shared canvas, and a transcript runs down the side. It arranges itself like a video call, and
it does so **live** — when a canvas is shared (or stops being shared) and when the live lane moves, the
page rearranges itself with no reload, off the same SSE stream the canvas already publishes (spec 006
FR7).

A DUMB view over state the tool already keeps — the lane registry (the participants), the turn log
(the transcript), the live lane (who has the floor, spec 004), and the canvas frame store (whether a
screen is being shared). It embeds the working canvas from `/canvas?embed=1` (the embed flag hides the
canvas's own header). It holds no model and decides nothing (spec 006 TC1).

The layout is a pure function of three inputs (spec 006 FR2): how many agents are in the bridge,
whether a canvas is being shared, and whether he wants to see it. Both states are always in the DOM;
`data-state` on the stage selects which is shown, set on the server for the first paint and moved by
the client on each SSE event. The page is also responsive — the side-by-side body stacks on a phone.
"""
from __future__ import annotations

import html as _html
from typing import Any

from . import store

# The lane identity hues, in registration order — the SAME palette the phone UI uses
# (web/index.html `LANE_HUES`), so an orb is the same colour on both pages. Deterministic by lane
# order (JJ: "the colour for the n lanes should be deterministic … first Magnus … next Atlas").
LANE_HUES = ["#3559bd", "#0f7a63", "#8a3fa8", "#a8501f", "#1f6f8a", "#7a2f4f"]


def hue(names: list[str], name: str) -> str:
    """This lane's identity hue — its position in the registry, so it never moves under it."""
    try:
        return LANE_HUES[list(names).index(name) % len(LANE_HUES)]
    except ValueError:
        return "#3a3a46"


def _canvas_is_shared() -> bool:
    """A canvas is 'being shared' when any lane has a frame on it — the visual equivalent of a
    participant sharing their screen. Read from the in-process canvas store; never raises."""
    try:
        from .canvas import server as canvas
        with canvas._lock:
            return any(frames for frames in canvas._lanes.values())
    except Exception:  # noqa: BLE001 — the canvas may not be mounted; then nothing is shared
        return False


def _orb(name: str, colour: str, live: bool, size: int = 34) -> str:
    """One participant orb: a lane-hued disc, its name, and a status line, driven by CSS off the
    `--c`/`--sz` custom properties and the `.live` class so the client can re-mark the live lane on a
    switch without re-rendering. The orb IS the count-and-who-is-live indicator (JJ, 2026-09-01:
    'no need to say [N] in the bridge and which one's live … that's understood by the orbs')."""
    cls = "orb live" if live else "orb"
    status = "live" if live else "idle"
    return (
        f'<div class="{cls}" data-lane="{_html.escape(name)}" style="--c:{colour};--sz:{size}px">'
        f'<div class="disc"></div>'
        f'<div class="nm">{_html.escape(name.upper())}</div>'
        f'<div class="st">{status}</div>'
        f'</div>'
    )


def _header() -> str:
    """The header kept from the voice-tunnel UI: the identity, and the room controls — the mic and
    speaker pickers and the verbosity toggle (JJ asked for these; they are the wireframe's header).
    No 'N in the bridge / live: X' text — the orbs carry that."""
    pill = ("display:flex;align-items:center;gap:5px;background:#20202a;border:1px solid #33333f;"
            "border-radius:20px;padding:6px 12px;font-size:13px;color:#d6d6de")
    caret = '<span style="color:#6f6f7d;font-size:10px">&#9662;</span>'
    return (
        '<div class="header">'
        '<div style="width:34px;height:34px;border-radius:9px;border:1px solid #3a5a8f;'
        'background:#1a2740;display:flex;align-items:center;justify-content:center;color:#5b9bff;'
        'font-size:16px">&#9211;</div>'
        '<div style="margin-left:12px;font-size:13px;letter-spacing:3px;color:#8a8a97;'
        'font-weight:600">COMMAND&#8202;BRIDGE</div>'
        '<div style="margin-left:auto;display:flex;gap:10px;align-items:center">'
        f'<div style="{pill}">&#127908; {caret}</div>'
        f'<div style="{pill}">&#128266; {caret}</div>'
        f'<div style="{pill}">verbose {caret}</div>'
        '<div style="width:34px;height:34px;border-radius:50%;background:#d9a441;display:flex;'
        'align-items:center;justify-content:center;color:#14141b;font-size:14px">&#9776;</div>'
        '</div></div>'
    )


def _transcript(session: str, lanes: list[str]) -> str:
    turns = store.read_turns(session)[-14:]
    if not turns:
        body = ('<div style="color:#6f6f7d;font-style:italic">no turns yet — one transcript, '
                'every lane on it</div>')
    else:
        rows = []
        for t in turns:
            lane = t.get("lane") or (lanes[0] if lanes else "")
            c = hue(lanes, lane)
            who = f"you › {lane}" if t.get("addressed") else lane
            text = _html.escape((t.get("text") or "").strip())
            rows.append(
                f'<div style="margin-bottom:9px"><span style="color:{c};font-weight:600">'
                f'{_html.escape(who)}</span><br><span style="color:#c9c9d4">{text}</span></div>'
            )
        body = "".join(rows)
    return (
        '<div class="transcript">'
        '<div style="font-size:11px;letter-spacing:.6px;color:#7a7a86;text-transform:uppercase;'
        'margin-bottom:12px">Transcript</div>'
        '<div style="font-size:12.5px;line-height:1.5;overflow:auto">' + body + '</div></div>'
    )


_STYLE = """
:root{color-scheme:dark}
html,body{margin:0;height:100%;background:#0e0e14;font-family:'Segoe UI',system-ui,sans-serif}
.stage{height:100vh;box-sizing:border-box;background:#14141b;color:#e9e9ef;display:grid;
  grid-template-rows:auto auto 1fr;overflow:hidden}
.header{display:flex;align-items:center;padding:11px 18px}
.orbrow{display:flex;justify-content:space-around;align-items:center;padding:6px 24px 12px;gap:8px}
/* SOLO: the top row is redundant (the agent IS the centre); collapse it to nothing but keep the
   grid's three tracks so the body stays in the 1fr row. */
.stage[data-state="solo"] .orbrow{height:0;padding:0;overflow:hidden}
.orb{text-align:center;flex:0 1 auto}
.orb .disc{width:var(--sz);height:var(--sz);border-radius:50%;margin:0 auto;
  background:radial-gradient(circle at 35% 30%,var(--c),#14141b);border:1px solid var(--c);
  transition:width .18s ease,height .18s ease,box-shadow .18s ease}
.orb.live .disc{width:calc(var(--sz) + 10px);height:calc(var(--sz) + 10px);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--c) 27%,transparent),
             0 0 16px color-mix(in srgb,var(--c) 53%,transparent)}
.orb .nm{font-size:11px;margin-top:6px;letter-spacing:1px;color:#8a8a97}
.orb.live .nm{color:var(--c);font-weight:600}
.orb .st{font-size:9px;margin-top:2px;color:#7a7a86}
.orb.live .st{color:#c9c9d4}
.body{min-height:0;border-top:1px solid #2a2a36;display:grid;
  grid-template-columns:1fr 6px minmax(200px,var(--tw,258px))}
.cell{min-height:0;background:#191921;display:grid}
.cell > *{grid-area:1/1;min-height:0}
.canvas{border:0;width:100%;height:100%;background:#191921}
.stage[data-state="solo"] .canvas{display:none}
.centre{display:flex;align-items:center;justify-content:center;gap:28px;flex-wrap:wrap}
.stage[data-state="shared"] .centre{display:none}
.grip{background:#20202a;display:flex;align-items:center;justify-content:center;cursor:col-resize;
  color:#55555f;font-size:14px}
.transcript{background:#17171e;padding:14px;display:flex;flex-direction:column;min-height:0;
  overflow:hidden}
iframe{display:block}
/* PHONE (JJ, 2026-09-01: validate phone-size rendering too). The side-by-side body stacks: the
   canvas over the transcript, so neither is squeezed to a sliver on a narrow screen. */
@media (max-width:640px){
  .header{padding:9px 12px;flex-wrap:wrap}
  .orbrow{padding:4px 8px 8px;gap:4px}
  .body{grid-template-columns:1fr;grid-template-rows:1fr minmax(120px,34vh)}
  .grip{display:none}
  .transcript{border-top:1px solid #2a2a36}
}
"""

# The live state machine, on the page. It subscribes to the canvas's own SSE (`/events`) — the same
# stream the canvas surface uses — and recomputes the two inputs it can see: whether ANY lane has a
# frame (a screen shared) and which lane is live. On every event it sets `data-state` and re-marks the
# live orb, so a share toggling on/off or a lane switch rearranges the page with no reload (FR7). It
# reads only; it never posts, and a missing SSE just leaves the server-rendered first paint standing.
_SCRIPT = """
(function(){
  var stage=document.querySelector('.stage');
  if(!stage) return;
  var frames={};
  function apply(){
    var shared=false; for(var k in frames){ if(frames[k]>0){ shared=true; break; } }
    stage.dataset.state = shared ? 'shared' : 'solo';
  }
  function markLive(lane){
    var orbs=stage.querySelectorAll('.orb');
    for(var i=0;i<orbs.length;i++){
      var isLive = orbs[i].getAttribute('data-lane')===lane;
      orbs[i].classList.toggle('live', isLive);
      var st=orbs[i].querySelector('.st'); if(st) st.textContent = isLive ? 'live':'idle';
    }
  }
  function on(name, fn){ es.addEventListener(name, function(e){ try{ fn(JSON.parse(e.data)); }catch(x){} }); }
  var es;
  try { es = new EventSource('/events'); } catch(x){ return; }
  on('sync', function(d){
    frames={}; if(d.lanes){ for(var k in d.lanes){ frames[k]=(d.lanes[k]||[]).length; } }
    if(d.live) markLive(d.live);
    apply();
  });
  on('switch', function(d){ if(d.lane) markLive(d.lane); });
  on('frame',  function(d){ frames[d.lane]=(frames[d.lane]||0)+1; apply(); });
  on('remove', function(d){ frames[d.lane]=Math.max(0,(frames[d.lane]||1)-1); apply(); });
  on('clear',  function(d){ frames[d.lane]=0; apply(); });
})();
"""

_GRIP = """
(function(){var g=document.querySelector('.grip'),b=document.querySelector('.body');
if(!g||!b||matchMedia('(max-width:640px)').matches)return;var d=0;
g.addEventListener('mousedown',function(e){d=1;e.preventDefault();});
window.addEventListener('mousemove',function(e){if(!d)return;var col=b.getBoundingClientRect().right
-e.clientX;col=Math.max(160,Math.min(560,col));b.style.setProperty('--tw',col+'px');});
window.addEventListener('mouseup',function(){d=0;});})();
"""


def render(state: Any) -> str:
    """The meeting page for `state`. Both states live in the DOM; `data-state` selects one for the
    first paint and the client moves it on each SSE event. Responsive: the body stacks on a phone."""
    lanes = list(state.lanes.names)
    live = state.lanes.current
    shared = _canvas_is_shared()

    top_orbs = "".join(_orb(n, hue(lanes, n), n == live) for n in lanes)
    centre_orbs = "".join(_orb(n, hue(lanes, n), n == live, size=110) for n in lanes)
    transcript = _transcript(state.session, lanes)

    stage = (
        f'<div class="stage" data-state="{"shared" if shared else "solo"}" '
        f'data-live="{_html.escape(live)}">'
        + _header()
        + f'<div class="orbrow">{top_orbs}</div>'
        + '<div class="body">'
        + '<div class="cell">'
        # The shared screen: the working canvas, its own header hidden. Present even in the solo
        # state (hidden by CSS) so a share can reveal it instantly, with no reload.
        '<iframe class="canvas" src="/canvas?embed=1" title="shared canvas"></iframe>'
        f'<div class="centre">{centre_orbs}</div>'
        + '</div>'
        + '<div class="grip">&#8942;</div>'
        + transcript
        + '</div></div>'
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Command Bridge — meeting</title>'
        '<style>' + _STYLE + '</style></head><body>'
        + stage
        + '<script>' + _SCRIPT + _GRIP + '</script>'
        + '</body></html>'
    )
