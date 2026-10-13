"""Specs 037/038 — liaison mode: the liaisons speak to him and hold the camera; workers speak through them.

Drives the real canvas module (`_on_file_upsert`, `apply`), the real `/liaison`, `/say` and `/lane`
handlers and the real `_flush_lane_held`. No server is started. The page's grouping rule is run in
node against the same cases as `liaison.groups`, so the two cannot drift.
"""
import asyncio
import json
import pathlib
import re
import shutil
import subprocess
from typing import Any

import pytest

from command_bridge import liaison, server
from command_bridge.canvas import server as canvas

LEAD = "magnuslz"
LEAD2 = "phoenixlz"
WORKER = "atlaslz"
OTHER = "siriuslz"
PAGE = pathlib.Path(canvas.__file__).with_name("page.py").read_text(encoding="utf-8")
UI = (pathlib.Path(server.WEB_DIR) / "index.html").read_text(encoding="utf-8")
ALL = (LEAD, LEAD2, WORKER, OTHER)


@pytest.fixture(autouse=True)
def _clean_canvas():
    def clear():
        for d in (canvas._lanes, canvas._look_held, canvas._point, canvas._geometry, canvas._viewport):
            for n in ALL:
                d.pop(n, None)
        canvas.set_liaisons(None)
    clear()
    yield
    clear()


@pytest.fixture(autouse=True)
def _fake_tts(monkeypatch):
    monkeypatch.setattr(server.tts, "synthesize", lambda text, voice=None, speed=None, pause=None:
                        (text.encode("utf-8") * 8, 22050))
    monkeypatch.setattr(server.config, "SPEAK_GRACE_S", 0.0, raising=False)


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    st = server.TunnelState("t", token="tok")
    st.lanes = server.lanes_mod.LaneRegistry(LEAD)
    for n in (LEAD2, WORKER, OTHER):
        st.lanes.add(n)
    st.lanes.switch(LEAD)
    st.channel_open = True
    st.cues_enabled = False
    canvas.set_live(LEAD)
    return st


class _Req:
    def __init__(self, state, body):
        self.app = {"state": state}
        self.remote = "127.0.0.1"
        self.headers = {}
        self.query = {"token": "tok"}
        self.cookies = {}
        self._body = body

    async def json(self):
        return self._body


def _post(handler, state, body) -> Any:
    resp: Any = asyncio.run(handler(_Req(state, body)))   # type: ignore[arg-type]
    return resp.status, json.loads(resp.body)


def _on(state, liaisons=None):
    return _post(server.handle_liaison, state, {"on": True, "liaisons": liaisons or {LEAD: [], LEAD2: [WORKER]}})


# ------------------------------------------------------------------ the rules (pure)

def test_parse_and_who_speaks_for_whom():                              # 038 FR1
    m = liaison.parse(["magnus", "phoenix=atlas,vega"])
    assert m == {"magnus": [], "phoenix": ["atlas", "vega"]}
    assert liaison.liaison_for(m, "vega") == "phoenix"
    assert liaison.liaison_for(m, "sirius") == "magnus"        # unclaimed → the first liaison
    assert liaison.liaison_for(m, "phoenix") == "phoenix"
    assert liaison.liaison_for({}, "sirius") is None
    with pytest.raises(ValueError):
        liaison.parse(["magnus=atlas", "phoenix=atlas"])           # claimed twice


def test_groups_cover_every_lane_once():                               # 038 FR2
    m = {"magnus": [], "phoenix": ["atlas", "vega"]}
    g = liaison.groups(m, ["magnus", "sirius", "atlas", "phoenix", "vega", "rigel"])
    assert g == [{"lead": "magnus", "workers": ["sirius", "rigel"]},
                 {"lead": "phoenix", "workers": ["atlas", "vega"]}]


def test_the_single_liaison_file_of_spec_037_still_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    pathlib.Path(liaison.path("t")).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(liaison.path("t")).write_text('{"lane": "magnus"}', encoding="utf-8")
    assert liaison.load("t") == {"magnus": []}


def _js_function(src, name):
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(src[i], 0)
        if depth == 0:
            return src[start:i + 1]
        i += 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_page_groups_exactly_like_the_server():                    # 038 FR2
    cases = [({"magnus": [], "phoenix": ["atlas", "vega"]}, ["magnus", "sirius", "atlas", "phoenix", "vega"]),
             ({"magnus": ["dexter"]}, ["dexter", "magnus", "orion"]),
             ({}, ["a", "b"])]
    js = _js_function(UI, "liaisonGroups") + "\nconst cases = " + json.dumps(cases) + ";\n" \
        "console.log(JSON.stringify(cases.map(([m, l]) => liaisonGroups(l, m))));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [liaison.groups(m, lane) for m, lane in cases]


# ------------------------------------------------------------------ the server

def test_liaisons_persist_and_are_reported(state):                      # AC1
    status, body = _on(state)
    assert status == 200 and body["liaisons"] == {LEAD: [], LEAD2: [WORKER]}
    assert body["groups"] == [{"lead": LEAD, "workers": [OTHER]}, {"lead": LEAD2, "workers": [WORKER]}]
    snap = state.snapshot()
    assert snap["liaison"] == LEAD and snap["liaisons"] == {LEAD: [], LEAD2: [WORKER]}
    assert server.TunnelState("t", token="tok").liaisons == {LEAD: [], LEAD2: [WORKER]}
    assert _post(server.handle_liaison, state, {"on": False})[1]["liaisons"] == {}
    assert liaison.load("t") == {}


def test_the_037_single_lane_form_still_works(state):
    assert _post(server.handle_liaison, state, {"on": True, "lane": LEAD})[1]["liaisons"] == {LEAD: []}


def test_an_unknown_lane_is_refused(state):                            # AC1
    status, body = _on(state, {"nobody": []})
    assert status == 400 and body["code"] == "unknown_lane"


def test_an_edit_never_moves_the_camera_in_liaison_mode(state, monkeypatch):   # 037 AC2 / 038 FR3
    sent = []
    monkeypatch.setattr(canvas, "publish", lambda ev, p: sent.append((ev, p)) or 1)
    _on(state)
    canvas._on_file_upsert(LEAD, {"id": "plan", "kind": "markdown", "content": "# x", "file": "plan.md"})
    canvas._on_file_upsert(LEAD2, {"id": "p2", "kind": "markdown", "content": "# y", "file": "p2.md"})
    frames = [p for ev, p in sent if ev == "frame"]
    assert frames and not any(p.get("focus") for p in frames)
    assert LEAD2 not in canvas._look_held


def test_outside_liaison_mode_edits_still_focus(state, monkeypatch):    # spec 022 unchanged
    sent = []
    monkeypatch.setattr(canvas, "publish", lambda ev, p: sent.append((ev, p)) or 1)
    canvas._on_file_upsert(LEAD, {"id": "plan", "kind": "markdown", "content": "# x", "file": "plan.md"})
    assert any(p.get("focus") for ev, p in sent if ev == "frame")


def test_only_liaisons_move_the_camera(state):                         # 037 AC3
    _on(state)
    canvas.apply("/frame", {"id": "plan", "kind": "markdown", "content": "# x", "lane": LEAD})
    code, body = canvas.apply("/point", {"selector": "plan", "look": True, "lane": WORKER})
    assert code == 409 and body["code"] == "liaison_mode"
    assert canvas.apply("/look", {"id": "plan", "lane": OTHER})[0] == 409
    code, body = canvas.apply("/point", {"selector": "plan", "look": True, "lane": LEAD})
    assert code == 200 and body["look"] is True


def test_a_workers_say_is_refused_naming_its_liaison(state, monkeypatch):     # 037 AC4 / 038 FR1
    called = []
    monkeypatch.setattr(server.tts, "synthesize", lambda *a, **k: called.append(1) or (b"\0\0", 22050))
    _on(state)
    status, body = _post(server.handle_say, state, {"text": "atlas here", "lane": WORKER})
    assert status == 409 and body["code"] == "liaison_mode" and body["liaison"] == LEAD2
    assert f"brief --session t --lane {LEAD2}" in body["remedy"]
    status, body = _post(server.handle_say, state, {"text": "sirius here", "lane": OTHER})
    assert status == 409 and body["liaison"] == LEAD                  # unclaimed → the first liaison
    assert called == []


def test_a_workers_held_clips_stay_held(state):                        # 037 AC4
    asyncio.run(server._speak(state, "atlas result", None, lane=WORKER))   # held: off-lane
    assert state.lane_held.get(WORKER)
    _on(state)
    assert asyncio.run(server._flush_lane_held(state, WORKER)) == 0
    assert state.lane_held.get(WORKER)


def test_he_cannot_switch_to_a_worker(state):                          # 038 FR2
    _on(state)
    status, body = _post(server.handle_lane, state, {"action": "switch", "name": WORKER})
    assert status == 409 and body["liaison"] == LEAD2
    assert state.lanes.current == LEAD
    assert _post(server.handle_lane, state, {"action": "switch", "name": LEAD2})[0] == 200
    assert state.lanes.current == LEAD2


# ------------------------------------------------------------------ the pages

def test_the_canvas_lets_an_explicit_look_win():                       # 037 AC5
    assert PAGE.count("explicitCamera();") >= 3          # point --look, look, zoom
    assert "if (lastExplicit < at) autoFocus(id, true);" in PAGE
    assert "if (lastExplicit < at) autoFocus(id, false);" in PAGE
    assert 'liveCardEl(String(selector).replace(/^[#.]/, "")) || cardEl(selector)' in PAGE
    assert "lookWhenShown = { el, margin };" in PAGE


def test_a_worker_card_is_a_name_and_working_or_idle():                # 038 FR2
    block = UI[UI.index("if (isWorker(row.name)) {"):UI.index("b.className = \"laneorb\" + (row.live")]
    assert "b.append(wn, wst);" in block and "return b;" in block
    for absent in ("wave", "laneopen", "lanetimer", "lanewait"):
        assert absent not in block
    assert "b.disabled = true;" in block
    assert "if (isWorker(name)) return;" in UI           # switchLane
    assert re.search(r"/\^\(idle\|listening\|off\|waiting\|asleep\)\$/", UI)
