"""The watcher: the whole ingestion engine of the file-backed canvas (spec 022 FR3).

A file created in a lane folder becomes a frame, a changed file re-renders just that frame, a deleted
file removes it — and no command mediates any of it. The watcher does nothing the old `set` did not;
it just does it for the agent, off a file the agent already wrote with its normal tools.

**Polling, on purpose.** Native filesystem events do not fire reliably on every path this runs on
(WSL's /mnt/c, network drives, some container mounts), and a silently dead watcher is
indistinguishable from "no edits" (NFR5). A lane folder holds a handful of files, so a `stat` of each
every 200 ms costs nothing measurable — and polling is then the ONLY engine, with no second
event-driven path to diverge from it. It also adds no dependency.

**The burst is coalesced three ways** (TC2 / AC4):
  1. DEBOUNCE — a path is ingested only once its (mtime, size) has held still for `SETTLE` seconds,
     so an atomic save's temp + rename + chmod churn is one ingest, not three.
  2. HASH-DIFF — a settled file whose bytes hash to what is already on screen is dropped, so a `touch`
     or a no-op rewrite re-renders nothing, and a file the TOOL itself just wrote (its hash already
     recorded by `FolderStore.write_frame`) never loops back as a second render (TC3).
  3. IGNORE — an editor's temp/swap/backup file and the index itself are never frames (`folder.ignored`).

**Boot is not N new frames.** The first scan only SEEDS the stat table from what `FolderStore.load`
already rendered, so starting the server does not re-announce every file as freshly drawn (TC2).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import folder as fs

POLL = 0.2
SETTLE = 0.15


class Watcher:
    """Scans `store.root/<lane>/` and calls `on_upsert(lane, frame)` / `on_delete(lane, fid, name)`.

    `scan(now)` is one pass and is what the tests drive, with an explicit clock; `start()` runs it on a
    daemon thread. The callbacks run on that thread — the canvas state they touch is lock-guarded and
    thread-agnostic, the same contract the voice-lane follower already relies on."""

    def __init__(self, store: fs.FolderStore,
                 on_upsert: Callable[[str, dict], None],
                 on_delete: Callable[[str, str, str], None]) -> None:
        self.store = store
        self.on_upsert = on_upsert
        self.on_delete = on_delete
        # (lane, name) -> (mtime_ns, size) as last seen, and when it last changed.
        self._seen: dict[tuple[str, str], tuple[int, int]] = {}
        self._changed: dict[tuple[str, str], float] = {}
        self._seeded = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ingested = 0          # counters `status` can surface; also what the tests assert on
        self.removed = 0

    # ---- one pass ---------------------------------------------------------

    def _listing(self) -> dict[tuple[str, str], tuple[int, int]]:
        out: dict[tuple[str, str], tuple[int, int]] = {}
        for lane in self.store.lanes_on_disk():
            d = self.store.lane_dir(lane)
            try:
                entries = list(d.iterdir())
            except OSError:
                continue
            for p in entries:
                if fs.ignored(p.name) or not fs.kind_of(p.name):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not p.is_file():
                    continue
                out[(lane, p.name)] = (st.st_mtime_ns, st.st_size)
        return out

    def scan(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        cur = self._listing()
        if not self._seeded:
            self._seen = cur
            self._seeded = True
            return
        # Anything whose stat moved (or appeared) restarts its settle clock.
        for key, sig in cur.items():
            if self._seen.get(key) != sig:
                self._changed[key] = now
        # Deletions are immediate: there is nothing to settle about a file that is gone.
        for key in [k for k in self._seen if k not in cur]:
            self._changed.pop(key, None)
            lane, name = key
            with self.store.known_lock:
                was = self.store.known.get(lane, {}).pop(name, None)
            if was is not None:
                kk = fs.kind_of(name)
                if kk:
                    self.removed += 1
                    self.on_delete(lane, kk[1], name)
        self._seen = cur
        # Ingest every changed path that has held still long enough.
        for key in [k for k, t in self._changed.items() if now - t >= SETTLE]:
            self._changed.pop(key, None)
            lane, name = key
            self._ingest(lane, self.store.lane_dir(lane) / name)

    def _ingest(self, lane: str, path: Path) -> None:
        got = fs.read_frame(path, lane)
        if not got:
            return                                   # partial / empty / unreadable: keep the last good
        frame, h = got
        with self.store.known_lock:
            known = self.store.known.setdefault(lane, {})
            if known.get(path.name) == h:
                return                               # same bytes as on screen: a touch, or our own write
            known[path.name] = h
        self.ingested += 1
        self.on_upsert(lane, frame)

    # ---- the thread -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="canvas-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(POLL):
            try:
                self.scan()
            except Exception:                        # noqa: BLE001 — a bad pass must not kill the engine
                pass

    def status(self) -> dict:
        return {"engine": "poll", "poll_s": POLL, "alive": bool(self._thread and self._thread.is_alive()),
                "ingested": self.ingested, "removed": self.removed}
