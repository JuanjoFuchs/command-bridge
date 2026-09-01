"""The canvas on disk, so a restart does not erase a session.

**One file, every lane.** A file per lane would introduce partial-restore states
— lane A back, lane B corrupt — and FR5 says the server always starts. One
document has one answer to "is this readable", which is the only failure mode
worth having.

**Saving never blocks drawing.** `set` marks the canvas dirty and returns; a
single background thread coalesces bursts and writes at most every `DEBOUNCE`
seconds. A batch of twelve frames is one write, not twelve, and no HTTP handler
ever waits on a disk.

**Writes are atomic** — temp file in the same directory, then `os.replace`. A
crash mid-write leaves the previous good canvas intact rather than a truncated
one, which matters more here than usual: the thing being protected IS the
session's record, and there is no second copy anywhere.

What is deliberately NOT persisted:

    the camera      the page's own rule is that a reconnect must not move the
                    view; restoring a viewport would break it on the one path
                    where the user has no say.
    raised hands    attention state belongs to a running agent. If the server
                    restarted, so did the agent that raised it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

CANVAS_FILE = Path.home() / ".command-bridge-canvas.json"
DEBOUNCE = 1.0
VERSION = 1


class Store:
    """Holds the save thread and the last-known-good path. One per server."""

    def __init__(self, path: Path = CANVAS_FILE, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._snapshot = None
        self._dirty = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    # ---- loading ----------------------------------------------------------

    def load(self) -> tuple[dict[str, dict[str, dict]], dict[str, dict], str | None]:
        """Return (lanes, geometry, live). Never raises. Never refuses to start.

        A canvas that cannot be read is reported and discarded. Refusing to boot
        because the history is malformed would fail at the only job this tool
        has — and the history is exactly the thing most likely to be malformed,
        since it is written continuously by a process that can be killed.
        """
        if not self.enabled or not self.path.exists():
            return {}, {}, None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            lanes = raw["lanes"]
            if not isinstance(lanes, dict):
                raise ValueError("lanes is not an object")
            # Rebuild rather than trust: a hand-edited file should not be able
            # to put a non-frame into a canvas the page will try to render.
            clean: dict[str, dict[str, dict]] = {}
            for lane, frames in lanes.items():
                if not isinstance(frames, dict):
                    continue
                keep = {fid: f for fid, f in frames.items()
                        if isinstance(f, dict) and f.get("kind")}
                if keep:
                    clean[str(lane)] = keep
            geo = raw.get("geometry")
            geo = geo if isinstance(geo, dict) else {}
            live = raw.get("live")
            return clean, geo, str(live) if live else None
        except Exception as exc:                      # noqa: BLE001 — see docstring
            print("command-bridge: ignoring unreadable canvas at %s (%s)"
                  % (self.path, exc), file=sys.stderr)
            return {}, {}, None

    # ---- saving -----------------------------------------------------------

    def start(self, snapshot) -> None:
        """`snapshot` is called on the writer thread to get what to persist."""
        if not self.enabled:
            return
        self._snapshot = snapshot
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def touch(self) -> None:
        """The canvas changed. Returns immediately — this is the whole NFR1."""
        self._dirty.set()

    def _run(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        while True:
            self._dirty.wait()
            # Coalesce the burst that follows a `batch` or a page reconnect
            # before touching the disk at all.
            time.sleep(DEBOUNCE)
            self._dirty.clear()
            try:
                self.write(snapshot())
            except Exception as exc:                  # noqa: BLE001
                print("command-bridge: canvas save failed (%s)" % exc,
                      file=sys.stderr)

    def write(self, payload: dict) -> None:
        """Atomic: same-directory temp, fsync, then replace."""
        if not self.enabled:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            body = json.dumps({"version": VERSION, "saved_at": time.time(),
                               **payload}, ensure_ascii=False, indent=1)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)

    def discard(self) -> None:
        """`clear` on the last lane must not leave a copy that reappears."""
        if not self.enabled:
            return
        with self._lock:
            self.path.unlink(missing_ok=True)
