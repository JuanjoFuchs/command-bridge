"""Spec 022 — a frame is a file, the folder is the canvas.

Pure halves (the folder store, the index, the ignore rules, the doctor, migration) and the
integration half (a real temp folder -> the watcher -> the same frame ops and SSE events the old
`set` produced). No browser: the render and the sticky layout's geometry are checked by `shot`.
"""
from __future__ import annotations

import json
import os
import queue
import time
from pathlib import Path

import pytest

from command_bridge.canvas import folder as fs
from command_bridge.canvas import server as canvas
from command_bridge.canvas.watcher import SETTLE, Watcher

# ---- fixtures -----------------------------------------------------------------------------------

@pytest.fixture()
def root(tmp_path) -> Path:
    return tmp_path / "dev.canvas"


@pytest.fixture()
def live_canvas(root, monkeypatch):
    """The module canvas pointed at a temp folder, with an SSE subscriber recording events. The
    watcher is built but NOT threaded — each test drives `scan(now)` with an explicit clock."""
    store = fs.FolderStore(root)
    monkeypatch.setattr(canvas, "_store", store)
    saved = {k: dict(v) for k, v in canvas._lanes.items()}
    for lane in ("_fa", "_fb"):
        canvas._lanes.pop(lane, None)
        canvas._look_held.pop(lane, None)
        canvas._geometry.pop(lane, None)
    monkeypatch.setattr(canvas, "_live", "_fa")
    q: queue.Queue = queue.Queue()
    canvas._subscribers.append(q)
    w = Watcher(store, canvas._on_file_upsert, canvas._on_file_delete)
    w.scan(0.0)                     # seed (boot) — an empty folder, nothing to announce
    yield store, w, q
    canvas._subscribers.remove(q)
    for lane in ("_fa", "_fb"):
        canvas._lanes.pop(lane, None)
        canvas._look_held.pop(lane, None)
        canvas._geometry.pop(lane, None)
    canvas._lanes.update({k: v for k, v in saved.items() if k not in canvas._lanes})


def events(q) -> list[tuple[str, dict]]:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def settle(w: Watcher, t: float) -> float:
    """One scan to notice, one after SETTLE to ingest. Returns the clock."""
    w.scan(t)
    t += SETTLE + 0.01
    w.scan(t)
    return t


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---- pure: kinds, ignore, index ---------------------------------------------------------------

def test_the_extension_is_the_render_kind_and_the_stem_is_the_id():
    assert fs.kind_of("plan.mmd") == ("mermaid", "plan")
    assert fs.kind_of("notes.md") == ("markdown", "notes")
    assert fs.kind_of("board.html") == ("html", "board")
    assert fs.kind_of("ink-0.ink.svg") == ("ink", "ink-0")      # compound suffix wins
    assert fs.kind_of("arch.svg") == ("svg", "arch")
    assert fs.kind_of("costs.vl.json") == ("vega", "costs")
    assert fs.kind_of("shot.png") == ("image", "shot")
    assert fs.kind_of("data.csv") is None                         # not a render kind
    assert fs.kind_of(".mmd") is None


@pytest.mark.parametrize("name", ["plan.mmd~", "plan.mmd.tmp", ".plan.mmd.swp", ".hidden.md",
                                  "index.json", "index.json.tmp", "x.md.tmp.123", "#plan.md#"])
def test_editor_and_writer_scratch_is_never_a_frame(name):
    assert fs.ignored(name)


def test_the_index_holds_ids_files_and_positions_and_never_content(root):
    store = fs.FolderStore(root)
    frames = {"plan": {"id": "plan", "kind": "mermaid", "content": "graph TD; SECRET-->X",
                       "file": "plan.mmd", "at": [940, 0]},
              "notes": {"id": "notes", "kind": "markdown", "content": "# SECRET",
                        "file": "notes.md"}}
    geometry = {"notes": {"x": 0, "y": 0, "w": 900, "h": 520}}
    (root / "_fa").mkdir(parents=True)
    store.write({"lanes": {"_fa": frames}, "geometry": {"_fa": geometry}})
    raw = (root / "_fa" / "index.json").read_text(encoding="utf-8")
    assert "SECRET" not in raw and "content" not in raw and "render" not in raw   # AC3 / TC4
    idx = json.loads(raw)["frames"]
    assert {"id": "plan", "file": "plan.mmd", "x": 940, "y": 0} in idx
    assert {"id": "notes", "file": "notes.md", "x": 0, "y": 0, "w": 900, "h": 520} in idx


def test_load_reads_content_from_the_file_and_position_from_the_index(root):
    write(root / "_fa" / "plan.mmd", "graph TD; A-->B")
    write(root / "_fa" / "index.json",
          json.dumps({"version": 1, "frames": [{"id": "plan", "file": "plan.mmd", "x": 7, "y": 9}]}))
    lanes, _, _ = fs.FolderStore(root).load()
    f = lanes["_fa"]["plan"]
    assert f["content"] == "graph TD; A-->B" and f["kind"] == "mermaid"
    assert f["at"] == [7, 9]                                      # AC6: sticky across a restart


def test_an_unparseable_index_keeps_the_files_and_reports_rather_than_emptying_the_board(root):
    write(root / "_fa" / "plan.mmd", "graph TD; A-->B")
    write(root / "_fa" / "index.json", '{"version": 1, "frames": [')      # a crash mid-write
    lanes, _, _ = fs.FolderStore(root).load()
    assert "plan" in lanes["_fa"]                                 # AC11: the board survives


def test_a_zero_length_file_is_not_trusted(root):
    write(root / "_fa" / "plan.mmd", "")
    assert fs.read_frame(root / "_fa" / "plan.mmd", "_fa") is None


# ---- integration: the watcher is the whole loop -----------------------------------------------

def test_creating_a_file_renders_a_frame_with_no_command(live_canvas, root):
    store, w, q = live_canvas
    write(root / "_fa" / "plan.mmd", "graph TD; A-->B")
    settle(w, 1.0)
    evs = [e for e in events(q) if e[0] == "frame"]
    assert len(evs) == 1                                          # AC1
    ev = evs[0][1]
    assert ev["id"] == "plan" and ev["kind"] == "mermaid" and ev["lane"] == "_fa"
    assert ev["content"] == "graph TD; A-->B" and ev["file"] == "plan.mmd"
    assert canvas._lanes["_fa"]["plan"]["file"] == "plan.mmd"


def test_editing_re_renders_only_that_frame_and_deleting_removes_it(live_canvas, root):
    store, w, q = live_canvas
    write(root / "_fa" / "a.md", "# A")
    write(root / "_fa" / "b.md", "# B")
    t = settle(w, 1.0)
    events(q)
    write(root / "_fa" / "a.md", "# A, edited")
    os.utime(root / "_fa" / "a.md", ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    t = settle(w, t + 1)
    frames = [e[1] for e in events(q) if e[0] == "frame"]
    assert [f["id"] for f in frames] == ["a"] and frames[0]["content"] == "# A, edited"   # AC2
    (root / "_fa" / "b.md").unlink()
    settle(w, t + 1)
    assert [e[1]["id"] for e in events(q) if e[0] == "remove"] == ["b"]
    assert "b" not in canvas._lanes["_fa"]


def test_a_save_burst_is_one_render_and_a_touch_is_none(live_canvas, root):
    store, w, q = live_canvas
    p = root / "_fa" / "plan.mmd"
    # An atomic save: temp written, renamed over, chmod — all inside one settle window.
    write(root / "_fa" / "plan.mmd.tmp", "graph TD; A-->B")
    w.scan(1.0)
    os.replace(root / "_fa" / "plan.mmd.tmp", p)
    w.scan(1.05)
    os.chmod(p, 0o644)
    w.scan(1.1)
    w.scan(1.1 + SETTLE + 0.01)
    assert len([e for e in events(q) if e[0] == "frame"]) == 1   # AC4: debounced to one
    # A touch: new mtime, identical bytes.
    later = time.time_ns() + 5 * 10**9
    os.utime(p, ns=(later, later))
    settle(w, 3.0)
    assert [e for e in events(q) if e[0] == "frame"] == []        # AC4: hash-diff, zero renders


def test_a_file_the_tool_itself_wrote_does_not_loop_back(live_canvas, root):
    store, w, q = live_canvas
    code, body = canvas.apply("/frame", {"id": "costs", "kind": "vega", "content": '{"mark":"bar"}',
                                         "lane": "_fa"})
    assert code == 200 and body["file"] == "costs.vl.json"
    assert (root / "_fa" / "costs.vl.json").read_text(encoding="utf-8") == '{"mark":"bar"}'
    events(q)
    settle(w, 1.0)
    assert [e for e in events(q) if e[0] == "frame"] == []        # TC3: idempotent per hash


def test_remove_deletes_the_file_so_it_does_not_come_back(live_canvas, root):
    store, w, q = live_canvas
    canvas.apply("/frame", {"id": "gone", "kind": "markdown", "content": "# x", "lane": "_fa"})
    assert (root / "_fa" / "gone.md").exists()
    canvas.apply("/remove", {"id": "gone", "lane": "_fa"})
    assert not (root / "_fa" / "gone.md").exists()
    events(q)
    settle(w, 1.0)
    assert [e for e in events(q) if e[0] == "frame"] == []


def test_boot_does_not_announce_existing_files_as_new(root):
    write(root / "_fa" / "old.md", "# already here")
    store = fs.FolderStore(root)
    store.load()
    seen: list = []
    w = Watcher(store, lambda lane, f: seen.append(f), lambda *a: seen.append(a))
    w.scan(0.0)
    w.scan(1.0)
    assert seen == []                                             # TC2


# ---- sticky layout ------------------------------------------------------------------------------

def test_a_placed_frame_is_pinned_and_a_re_render_keeps_its_spot(live_canvas, root):
    store, w, q = live_canvas
    write(root / "_fa" / "a.md", "# A")
    t = settle(w, 1.0)
    canvas.apply("/placed", {"lane": "_fa", "frames": {"a": {"x": 120, "y": 40, "w": 300, "h": 200}},
                             "viewport": {}})
    assert canvas._lanes["_fa"]["a"]["at"] == [120, 40]
    write(root / "_fa" / "a.md", "# A again")
    os.utime(root / "_fa" / "a.md", ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    settle(w, t + 1)
    assert canvas._lanes["_fa"]["a"]["at"] == [120, 40]            # FR6: an edit never moves it
    store.write(canvas._canvas_snapshot())
    lanes, _, _ = fs.FolderStore(root).load()
    assert lanes["_fa"]["a"]["at"] == [120, 40]                   # AC6: survives a restart


def test_relayout_forgets_the_lanes_positions(live_canvas, root):
    store, w, q = live_canvas
    canvas.apply("/frame", {"id": "a", "kind": "markdown", "content": "# A", "lane": "_fa",
                            "at": [5, 5]})
    events(q)
    code, _ = canvas.apply("/relayout", {"lane": "_fa"})
    assert code == 200 and "at" not in canvas._lanes["_fa"]["a"]
    assert [e[0] for e in events(q)] == ["relayout"]


# ---- auto-focus follows the latest edit -----------------------------------------------------------

def test_an_edit_on_the_live_lane_focuses_that_frame(live_canvas, root):
    store, w, q = live_canvas
    write(root / "_fa" / "plan.mmd", "graph TD; A-->B")
    settle(w, 1.0)
    ev = [e[1] for e in events(q) if e[0] == "frame"][0]
    assert ev.get("focus") is True                                # AC7 (live half)
    assert "focus" not in canvas._lanes["_fa"]["plan"]            # never replayed on reconnect


def test_off_lane_edits_coalesce_to_the_latest_and_replay_on_switch(live_canvas, root):
    store, w, q = live_canvas
    write(root / "_fb" / "a.md", "# A")
    t = settle(w, 1.0)
    write(root / "_fb" / "b.md", "# B")
    settle(w, t + 1)
    assert canvas._look_held["_fb"]["id"] == "b"                   # latest wins
    events(q)
    canvas.set_live("_fb")
    evs = events(q)
    assert [e[0] for e in evs][:2] == ["switch", "look"]           # AC7: the look lands first
    assert evs[1][1]["id"] == "b"
    canvas.set_live("_fa")


def test_an_underscore_file_updates_without_taking_focus(live_canvas, root):
    store, w, q = live_canvas
    write(root / "_fa" / "_status.md", "# working on T3")
    write(root / "_fb" / "_status.md", "# working")
    settle(w, 1.0)
    frames = [e[1] for e in events(q) if e[0] == "frame"]
    assert frames and not any(f.get("focus") for f in frames)     # AC8
    assert "_fb" not in canvas._look_held


# ---- doctor + migration ---------------------------------------------------------------------------

def test_the_doctor_finds_and_repairs_the_four_drifts(root):
    d = root / "_fa"
    write(d / "orphan.md", "# not in the index")
    write(d / "plan.mmd", "graph TD; A-->B")
    write(d / "plan.md", "# same id, other kind")
    write(d / "notes.docx", "binary-ish")
    write(d / "index.json", json.dumps({"version": 1, "frames": [
        {"id": "plan", "file": "plan.mmd", "x": 0, "y": 0},
        {"id": "ghost", "file": "ghost.md", "x": 1, "y": 1}]}))
    rep = fs.doctor(root)
    lane = rep["lanes"]["_fa"]
    assert "orphan.md" in lane["orphan"]                           # AC10
    assert lane["dangling"] == ["ghost.md"]
    assert set(lane["duplicate"]["plan"]) == {"plan.mmd", "plan.md"}
    assert lane["unknown"] == ["notes.docx"]
    assert not rep["ok"]
    fixed = fs.doctor(root, fix=True)
    assert fixed["lanes"]["_fa"].get("renamed")
    again = fs.doctor(root)
    assert set(again["lanes"]["_fa"]) == {"unknown"}               # only the not-ours finding left
    files = {e["file"] for e in json.loads((d / "index.json").read_text())["frames"]}
    assert "ghost.md" not in files and "orphan.md" in files


def test_migration_splits_the_single_json_into_lane_folders(tmp_path, root):
    legacy = tmp_path / "old-canvas.json"
    legacy.write_text(json.dumps({"version": 1, "lanes": {
        "magnus": {"plan": {"id": "plan", "kind": "mermaid", "content": "graph TD; A-->B",
                            "at": [10, 20]},
                   "ink-0": {"id": "ink-0", "kind": "ink", "content": "<svg></svg>"}},
        "atlas": {"notes": {"id": "notes", "kind": "markdown", "content": "# N"}}},
        "geometry": {"atlas": {"notes": {"x": 3, "y": 4, "w": 10, "h": 10}}}}), encoding="utf-8")
    out = fs.migrate(legacy, root)
    assert out and out["frames"] == 3                               # AC12: no frame lost
    assert (root / "magnus" / "plan.mmd").read_text() == "graph TD; A-->B"
    assert (root / "magnus" / "ink-0.ink.svg").exists()
    assert (root / "atlas" / "notes.md").exists()
    assert not legacy.exists() and legacy.with_name(legacy.name + ".migrated").exists()
    lanes, _, _ = fs.FolderStore(root).load()
    assert lanes["magnus"]["plan"]["at"] == [10, 20] and lanes["atlas"]["notes"]["at"] == [3, 4]
    # A lane that already has frames drawn the new way is never overwritten — and it does not stop
    # the OTHER lanes from migrating (caught live: one lane's early file blocked the whole upgrade).
    legacy.write_text(json.dumps({"lanes": {
        "magnus": {"plan": {"id": "plan", "kind": "mermaid", "content": "OLD"}},
        "orion": {"o": {"id": "o", "kind": "markdown", "content": "# O"}}}}), encoding="utf-8")
    again = fs.migrate(legacy, root)
    assert again["skipped"] == ["magnus"] and again["lanes"] == {"orion": 1}
    assert (root / "magnus" / "plan.mmd").read_text() == "graph TD; A-->B"
    assert (root / "orion" / "o.md").exists()
