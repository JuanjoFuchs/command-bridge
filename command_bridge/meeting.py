"""The meeting page (spec 006): one page where JJ's agents are participant orbs, the live one draws
on the shared canvas, and a transcript runs down the side. It arranges itself like a video call.

A DUMB view over state the tool already keeps — the lane registry (the participants), the turn log
(the transcript), the live lane (who has the floor, spec 004), and the canvas frame store (whether a
screen is being shared). It embeds the working canvas from `/canvas` rather than re-implementing it.
It holds no model and decides nothing (spec 006 TC1).

The layout is a pure function of three inputs (spec 006 FR2): how many agents are in the bridge,
whether a canvas is being shared, and whether he wants to see it. This module renders the state that
matches; live transitions between states are driven on the page from the same SSE the halves use.
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
    """One participant orb: a lane-hued disc, its name, and a status line. `size` is the base disc
    diameter (bigger in the centre when nobody is sharing); the live lane is enlarged and lit."""
    d = size + 10 if live else size
    disc = (
        f"width:{d}px;height:{d}px;border-radius:50%;margin:0 auto;"
        f"background:radial-gradient(circle at 35% 30%,{colour},#14141b);border:1px solid {colour};"
        + (f"box-shadow:0 0 0 3px {colour}44,0 0 16px {colour}88;" if live else "")
    )
    label_c = colour if live else "#8a8a97"
    weight = "600" if live else "400"
    status = "live" if live else "idle"
    label_sz = 13 if size > 60 else 11
    return (
        f'<div style="text-align:center;flex:0 1 auto">'
        f'<div style="{disc}"></div>'
        f'<div style="font-size:{label_sz}px;margin-top:6px;letter-spacing:1px;color:{label_c};'
        f'font-weight:{weight}">{_html.escape(name.upper())}</div>'
        f'<div style="font-size:9px;color:{"#c9c9d4" if live else "#7a7a86"};margin-top:2px">'
        f'{status}</div>'
        f'</div>'
    )


def _transcript_rows(session: str, lanes: list[str]) -> str:
    turns = store.read_turns(session)[-14:]
    if not turns:
        return ('<div style="color:#6f6f7d;font-style:italic">no turns yet — one transcript, '
                'every lane on it</div>')
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
    return "".join(rows)


def render(state: Any) -> str:
    """The meeting page for `state`. Wireframe arrangement when a canvas is shared; the agent(s)
    hold the centre when it is not."""
    lanes = list(state.lanes.names)
    live = state.lanes.current
    shared = _canvas_is_shared()

    orbs = "".join(_orb(n, hue(lanes, n), n == live) for n in lanes)
    # The top orb row belongs to the SHARED state (the wireframe): the canvas holds the centre, so
    # the participants sit in a row across the top, like a video call sharing a screen. When nothing
    # is shared the agent(s) ARE the centre, so the top row would just repeat them — omit it and keep
    # the grid's three tracks with a zero-height spacer.
    orb_row = (
        '<div style="display:flex;justify-content:space-around;align-items:center;'
        'padding:6px 24px 12px;gap:8px">' + orbs + '</div>'
        if shared else '<div style="height:0"></div>'
    )
    transcript = (
        '<div style="background:#17171e;padding:14px;display:flex;flex-direction:column;'
        'min-height:0;overflow:hidden">'
        '<div style="font-size:11px;letter-spacing:.6px;color:#7a7a86;text-transform:uppercase;'
        'margin-bottom:12px">Transcript</div>'
        '<div style="font-size:12.5px;line-height:1.5;overflow:auto">'
        + _transcript_rows(state.session, lanes) + '</div></div>'
    )

    if shared:
        # SHARED: the validated wireframe — orbs top, canvas full-bleed centre, transcript right.
        body = (
            '<div id="body" data-state="shared" style="display:grid;'
            'grid-template-columns:1fr 6px minmax(200px,var(--tw,258px));min-height:0;'
            'border-top:1px solid #2a2a36">'
            '<iframe src="/canvas" title="shared canvas" style="border:0;width:100%;height:100%;'
            'background:#191921"></iframe>'
            '<div id="grip" style="background:#20202a;display:flex;align-items:center;'
            'justify-content:center;cursor:col-resize;color:#55555f;font-size:14px">⋮</div>'
            + transcript + '</div>'
        )
    else:
        # NOT SHARING: the agent(s) hold the centre (a 1:1 call / a gallery); the transcript is a
        # side panel he can collapse. No canvas full-bleed until someone shares.
        centre_orbs = "".join(
            _orb(n, hue(lanes, n), n == live, size=110)  # bigger — they hold the centre
            for n in lanes
        )
        body = (
            '<div id="body" data-state="solo" style="display:grid;'
            'grid-template-columns:1fr 6px minmax(180px,var(--tw,240px));min-height:0;'
            'border-top:1px solid #2a2a36">'
            '<div style="background:#191921;display:flex;align-items:center;justify-content:center;'
            'gap:28px;min-height:0">' + centre_orbs + '</div>'
            '<div id="grip" style="background:#20202a;display:flex;align-items:center;'
            'justify-content:center;cursor:col-resize;color:#55555f;font-size:14px">⋮</div>'
            + transcript + '</div>'
        )

    header = (
        '<div style="display:flex;align-items:center;padding:11px 18px">'
        '<div style="width:34px;height:34px;border-radius:9px;border:1px solid #3a5a8f;'
        'background:#1a2740;display:flex;align-items:center;justify-content:center;color:#5b9bff;'
        'font-size:16px">⏻</div>'
        '<div style="margin-left:12px;font-size:13px;letter-spacing:3px;color:#8a8a97;'
        'font-weight:600">COMMAND&#8202;BRIDGE</div>'
        f'<div style="margin-left:auto;font-size:11px;color:#6f6f7d">'
        f'{len(lanes)} in the bridge · live: '
        f'<span style="color:{hue(lanes, live)}">{_html.escape(live.upper())}</span></div>'
        '</div>'
    )

    grip_js = (
        "<script>(function(){var g=document.getElementById('grip'),b=document.getElementById('body');"
        "if(!g||!b)return;var d=0,w=0,x=0;g.addEventListener('mousedown',function(e){d=1;x=e.clientX;"
        "w=b.getBoundingClientRect().width;e.preventDefault();});"
        "window.addEventListener('mousemove',function(e){if(!d)return;var col=b.getBoundingClientRect()"
        ".right-e.clientX;col=Math.max(160,Math.min(560,col));b.style.setProperty('--tw',col+'px');});"
        "window.addEventListener('mouseup',function(){d=0;});})();</script>"
    )

    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Command Bridge — meeting</title>'
        '<style>html,body{margin:0;height:100%;background:#0e0e14}'
        "body{font-family:'Segoe UI',system-ui,sans-serif}"
        '#stage{height:100vh;box-sizing:border-box;background:#14141b;color:#e9e9ef;'
        'display:grid;grid-template-rows:auto auto 1fr;overflow:hidden}'
        'iframe{display:block}</style></head><body>'
        '<div id="stage">' + header + orb_row + body + '</div>' + grip_js +
        '</body></html>'
    )
