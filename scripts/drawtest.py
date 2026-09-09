"""End-to-end acceptance for spec 015 — a canvas he can draw on.

Drives a **real browser** against a **real server**, so the path under test is the genuine one:
open `/canvas`, select the pen, draw pointer strokes OVER an agent's frame, hit `send`, and prove
that (1) the strokes rendered live, (2) one `ink` frame landed on the live lane, (3) it renders as a
transparent overlay sitting on top of the frame it annotates, (4) a `source:"canvas"` turn was
surfaced to the agent's log, and (5) `shot` captures the sketch.

This is the browser half the pytest suite deliberately leaves out (a screenshot needs a real
render). It mirrors scripts/e2e.py: a subprocess server, Playwright input, HTTP assertions.

Run:  venv/Scripts/python.exe scripts/drawtest.py [--headed] [--keep]
Exit: 0 = every assertion held; non-zero = the first failure, printed.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from command_bridge import security, shot, store  # noqa: E402

PY = sys.executable
PASSES: list[str] = []


class Failure(RuntimeError):
    pass


def ok(label: str) -> None:
    PASSES.append(label)
    print(f"  [PASS] {label}", flush=True)


def check(condition: bool, label: str, detail: str = "") -> None:
    if not condition:
        raise Failure(f"{label}{(' — ' + detail) if detail else ''}")
    ok(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"} if data else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def wait_for_server(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if http_json(f"http://127.0.0.1:{port}/health", timeout=2).get("ok"):
                return
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            time.sleep(0.25)
    raise Failure("server did not become healthy in time")


def boxes_overlap(a: dict, b: dict) -> bool:
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


def run(headed: bool, keep: bool) -> int:
    from playwright.sync_api import sync_playwright

    workdir = tempfile.mkdtemp(prefix="command-bridge-draw-")
    session = "drawtest"
    token = security.generate_token()
    port = free_port()
    lane = "draw"     # a deterministic live lane so the frame, the ink and the page all agree
    # Isolate BOTH stores: COMMAND_BRIDGE_DIR moves the turn log; HOME/USERPROFILE moves the canvas
    # store (`~/.command-bridge-canvas.json`), so this test neither restores the developer's real
    # canvas nor writes ink into it. (Models still resolve to the source checkout, not HOME.)
    env = dict(os.environ, COMMAND_BRIDGE_DIR=workdir, COMMAND_BRIDGE_TOKEN=token,
               COMMAND_BRIDGE_TTS="sapi", HOME=workdir, USERPROFILE=workdir)

    base = f"http://127.0.0.1:{port}"
    print(f"\n== start the tunnel ==  (port {port}, session {session})")
    proc = subprocess.Popen(
        [PY, "-c",
         f"import sys; sys.path.insert(0, r'{ROOT}'); from command_bridge.cli import main; "
         f"raise SystemExit(main(['serve','--session','{session}','--port','{port}']))"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    browser = pw = None
    try:
        wait_for_server(port)
        ok("server is healthy")

        # Put a known lane on the floor, then place an agent frame on it to draw over. `/switch` and
        # `/canvas/frame` are the same ops the CLI and page use — no back door.
        http_json(f"{base}/switch", {"to": lane})
        http_json(f"{base}/canvas/frame", {
            "id": "watch-logic", "kind": "html", "lane": lane,
            "content": "<div style='width:320px;height:180px;"
                       "display:grid;place-items:center'>watch-logic</div>"})
        ok("placed an agent frame on the live lane")

        pw = sync_playwright().start()
        launch_args = ["--autoplay-policy=no-user-gesture-required"]
        try:
            browser = pw.chromium.launch(headless=not headed, channel="chrome", args=launch_args)
        except Exception:
            browser = pw.chromium.launch(headless=not headed, args=launch_args)
        page = browser.new_page(viewport={"width": 900, "height": 700})
        page.on("console", lambda m: print(f"    [console:{m.type}] {m.text}")
                if m.type == "error" else None)

        # The drawing view needs NO microphone and NO token (FR4/TC3): a plain GET over http.
        page.goto(f"{base}/canvas", wait_until="load")
        page.wait_for_selector('[data-frame="watch-logic"]', timeout=15000)
        ok("the /canvas drawing view loaded and rendered the frame")

        # AC7-ish smoke: drawing must not reach for the microphone.
        used_mic = page.evaluate(
            "() => !!(navigator.mediaDevices && navigator.mediaDevices.__called)")
        check(used_mic is False, "the drawing view never requested the microphone")

        target = page.locator('[data-frame="watch-logic"]').bounding_box()
        check(bool(target), "the frame has a screen box to draw over")

        # ---- AC3: draw two strokes ON the frame with the pen ---------------------
        print("\n== draw over the frame ==")
        page.click("#tool-pen")
        check(page.locator("#tool-pen.on").count() == 1, "the pen tool activated")

        def stroke(x0, y0, x1, y1, steps=8):
            page.mouse.move(x0, y0)
            page.mouse.down()
            for i in range(1, steps + 1):
                page.mouse.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
            page.mouse.up()

        cx, cy = target["x"] + target["width"] / 2, target["y"] + target["height"] / 2
        stroke(cx - 70, cy - 40, cx + 70, cy + 40)      # a slash across the frame
        stroke(cx - 70, cy + 40, cx + 70, cy - 40)      # the crossing slash
        live_paths = page.locator("#ink-live path").count()
        check(live_paths >= 2, "the live preview drew the strokes", f"{live_paths} paths")

        # ---- eraser removes a stroke --------------------------------------------
        # Aim at ONE slash's far end, away from the crossing, so only that stroke goes.
        page.click("#tool-erase")
        before = page.locator("#ink-live path").count()
        ex, ey = cx - 70, cy - 40
        page.mouse.move(ex, ey)
        page.mouse.down()
        page.mouse.move(ex + 3, ey + 3)
        page.mouse.up()
        after = page.locator("#ink-live path").count()
        check(after < before, "the eraser removed a stroke", f"{before} -> {after}")

        # draw one more so there is definitely ink to hand over, then send
        page.click("#tool-pen")
        stroke(cx - 60, cy, cx + 60, cy)      # a horizontal line across the frame
        check(page.locator("#ink-live path").count() >= 1, "there is ink to send")

        # ---- AC3/AC4: send lands one ink frame + a canvas turn -------------------
        print("\n== send ==")
        page.click("#tool-send")

        def ink_frames():
            st = http_json(f"{base}/canvas/status?lane={lane}")
            return [f for f in st.get("lanes", {}).get(lane, []) if f.get("kind") == "ink"]

        deadline = time.time() + 8
        inks = []
        while time.time() < deadline:
            inks = ink_frames()
            if inks:
                break
            time.sleep(0.15)
        check(len(inks) == 1, "exactly one ink frame landed on the live lane", json.dumps(inks))
        ink_id = inks[0]["id"]

        # AC4: the agent's watch returns a source:canvas turn naming the frame.
        turns, _ = store.turns_since(session, -1, base=workdir)
        canvas_turns = [t for t in turns if t.get("source") == "canvas"]
        check(len(canvas_turns) == 1, "a source:canvas turn was surfaced to the log",
              json.dumps(canvas_turns))
        t = canvas_turns[0]
        check(ink_id in t["text"] and lane in t["text"],
              "the canvas turn names the lane and the ink frame", t["text"])
        check(t.get("addressed") is True and t.get("lane") == lane,
              "the canvas turn is addressed to the live lane", json.dumps(t))

        # ---- AC5: the ink renders as an overlay ON the frame ---------------------
        print("\n== the annotation lands on the frame ==")
        page.wait_for_selector('.card[data-kind="ink"] svg path', timeout=8000)
        ink_paths = page.locator('.card[data-kind="ink"] svg path').count()
        check(ink_paths >= 1, "the ink frame renders its strokes", f"{ink_paths} paths")
        ink_box = page.locator('.card[data-kind="ink"]').bounding_box()
        frame_box = page.locator('[data-frame="watch-logic"]').bounding_box()
        check(boxes_overlap(ink_box, frame_box),
              "the ink overlay sits on top of the frame it annotates (AC5)",
              f"ink={ink_box} frame={frame_box}")

        # the local buffer cleared on send (draw-then-submit), and the tool returned to pan
        check(page.locator("#ink-live path").count() == 0, "the local drawing buffer cleared on send")
        check(page.locator("#tool-pen.on").count() == 0, "the tool returned to pan after send")

        # ---- AC1/AC2: shot captures the strokes ---------------------------------
        # Close the DRIVER's browser first: `shot` spins its own sync Playwright, and the sync API
        # cannot nest inside the one already running here. The server stays up, so the ink frame is
        # still there to capture.
        print("\n== shot ==")
        browser.close(); browser = None
        pw.stop(); pw = None
        out = os.path.join(workdir, "ink.png")
        result = shot.capture(f"{base}/canvas", out, lane=lane, settle_ms=3000, viewport=(900, 700))
        check(result.get("ok") is True, "shot rendered the canvas", json.dumps(result)[:200])
        check(result.get("bytes", 0) > 2000, "the screenshot is a real image (AC2)",
              f"{result.get('bytes')} bytes")

        print("\n== summary ==")
        print(f"    ink frame        {ink_id} on lane {lane}")
        print(f"    canvas turn      id={t['id']} {t['text']!r}")
        print(f"    shot             {result.get('bytes')} bytes -> {out}")
        return 0
    finally:
        try:
            if browser:
                browser.close()
            if pw:
                pw.stop()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if keep:
            print(f"\nworkdir kept: {workdir}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="show the browser")
    ap.add_argument("--keep", action="store_true", help="keep the temp workdir")
    args = ap.parse_args()

    print("=" * 70)
    print("command-bridge draw acceptance (spec 015)")
    print("=" * 70)
    started = time.time()
    try:
        rc = run(args.headed, args.keep)
    except Failure as exc:
        print(f"\n  [FAIL] {exc}", flush=True)
        print(f"\n{len(PASSES)} passed before the failure. FAILED in {time.time()-started:.1f}s")
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"\n  [ERROR] {type(exc).__name__}: {exc}")
        return 2
    print(f"\nALL {len(PASSES)} CHECKS PASSED in {time.time()-started:.1f}s")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
