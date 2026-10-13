"""The canvas on disk as a FOLDER OF NATIVE FILES, one folder per lane (spec 022).

    sessions/<session>.canvas/<lane>/
        plan.mmd          a mermaid frame          (its id is its stem: `plan`)
        notes.md          a markdown frame
        board.html        an html frame
        ink-0.ink.svg     a human sketch (spec 015)
        costs.vl.json     a vega-lite chart
        index.json        TOOL-OWNED: id / file / x / y — never content

**A frame is a file, and the extension is its render kind** (TC4). There is no `render` field
anywhere, because a second place that says what a frame is would be a second thing to drift. An
agent draws by writing the file with its normal Edit/Write tools — full language tooling, nothing
escaped into a JSON string — and the watcher (`watcher.py`) turns the change into a frame op.

**The id is the filename stem.** Spec 022 KD7 asked for an id stamped INSIDE each file so the index
is a rebuildable cache. The stem gives the same guarantee — identity survives the index being lost —
without the tool writing into a file an agent is in the middle of editing (a write-back races the
agent's own Edit and trips its "file changed since read" guard). So the index is rebuildable from
the directory listing alone, and two files with one stem (`plan.mmd` + `plan.md`) are the duplicate-id
finding `doctor` reports.

**The index carries position only** (FR4). Content lives in exactly one place — the file — which is
the whole point: the old store copied every frame's content into one global JSON, so it lived twice
and the surviving copy was the least legible one.

**Writes are atomic** (NFR3): same-directory temp, fsync, `os.replace` — for the index and for every
frame file the TOOL writes (a generator such as `chart`, the human's ink, migration). The index write
is debounced on a background thread exactly as the old single-file store was, so no HTTP handler ever
waits on a disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

INDEX = "index.json"
VERSION = 1
DEBOUNCE = 1.0

# The old single-document store (the "before" of spec 022). Only `migrate` reads it now.
LEGACY_FILE = Path.home() / ".command-bridge-canvas.json"

# Extension -> render kind. Checked LONGEST FIRST, so a compound suffix (`.ink.svg`, `.vl.json`)
# wins over the plain one it ends with.
KINDS: list[tuple[str, str]] = sorted([
    (".ink.svg", "ink"),
    (".vl.json", "vega"), (".vega.json", "vega"),
    (".mmd", "mermaid"), (".mermaid", "mermaid"),
    (".md", "markdown"), (".markdown", "markdown"),
    (".html", "html"), (".htm", "html"),
    (".svg", "svg"),
    (".txt", "text"),
    (".png", "image"), (".jpg", "image"), (".jpeg", "image"), (".gif", "image"),
    (".webp", "image"),
], key=lambda kv: -len(kv[0]))

# Kind -> the extension the TOOL writes it under (a generator, migration). Image has none: the tool
# never generates an image frame, it only renders one an agent dropped in.
EXT_FOR_KIND = {"mermaid": ".mmd", "markdown": ".md", "html": ".html", "svg": ".svg",
                "ink": ".ink.svg", "vega": ".vl.json", "text": ".txt"}

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}

# An editor's or a writer's scratch is not a frame (TC2): a swap file, a backup, a partial download,
# an atomic-save temp, a dotfile, and the index itself.
_IGNORE_SUFFIX = ("~", ".tmp", ".swp", ".swx", ".part", ".crdownload", ".bak")


def ignored(name: str) -> bool:
    """True for a file the watcher must never turn into a frame."""
    if not name or name.startswith(".") or name.startswith("#"):
        return True
    if name == INDEX or name.startswith(INDEX + "."):
        return True
    low = name.lower()
    return low.endswith(_IGNORE_SUFFIX) or ".tmp." in low


def kind_of(name: str) -> tuple[str, str] | None:
    """(kind, id) for a frame file, or None when the extension is not a render kind we know."""
    low = name.lower()
    for ext, kind in KINDS:
        if low.endswith(ext) and len(name) > len(ext):
            return kind, name[: -len(ext)]
    return None


def safe_name(s: str) -> str:
    """A frame id or lane name as a filesystem name: no separators, no traversal, never empty."""
    out = re.sub(r"[^A-Za-z0-9._-]", "_", str(s)).strip(".")
    return out or "frame"


def background(name: str) -> bool:
    """The auto-focus OPT-OUT (spec 022 FR7): a file whose name starts with `_` updates without
    taking the camera. A filename convention, not an index field, because agents never touch the
    index — `_status.md` is a status frame that refreshes quietly beside the work."""
    return name.startswith("_")


WALL_OF_TEXT_WORDS = 60
"""A markdown/text canvas frame past this many words with little structure reads as a wall of text
(JJ, 2026-09-15). Advisory only — `status` nudges toward a diagram; a genuine document extract is a
legitimate exception the agent judges."""


def wall_of_text(content: str, kind: str) -> str | None:
    """Advisory: does this frame read as a wall of text rather than a diagram?

    Only markdown/text — mermaid and svg ARE diagrams, and an html frame is often media or already
    structured. Trips on PROSE: many words with few STRUCTURAL lines (headings, list items, table
    rows). A long list or table is structured and does NOT trip — the target is running paragraphs,
    which is what "print walls of text in the canvas" means. Returns the nudge string, or None.
    Lived on `set` until spec 022 retired it; `status` now carries it per frame.
    """
    if kind not in ("markdown", "text"):
        return None
    words = len(content.split())
    if words < WALL_OF_TEXT_WORDS:
        return None
    structure = sum(
        1 for ln in content.splitlines()
        if re.match(r"\s*(#{1,6}\s|[-*+]\s|\d+[.)]\s)", ln) or ln.count("|") >= 2
    )
    if structure >= 3:
        return None
    return (f"this frame is {words} words of mostly prose — the canvas is for SHOWING, not text. "
            "If the idea is spatial, relational, or a sequence, a `mermaid` diagram lands far better "
            "(run the WHEN gate first). A genuine document EXTRACT is a fine exception — ignore this "
            "then; otherwise redraw it as a diagram.")


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_frame(path: Path, lane: str) -> tuple[dict, str] | None:
    """Read one frame file into a frame record (without timestamps) plus its content hash.

    None when it is not trustworthy YET (NFR3): unreadable, zero-length (a writer that has created
    the file and not filled it), or an unknown kind. The caller keeps whatever it last rendered."""
    kk = kind_of(path.name)
    if not kk:
        return None
    kind, fid = kk
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    h = digest(data)
    if kind == "image":
        # The page loads the pixels itself; the frame carries the URL, cache-busted by content so an
        # overwritten image re-renders instead of showing the browser's cached copy.
        content = "/canvas/file/%s/%s?v=%s" % (lane, path.name, h[:10])
    else:
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return {"id": fid, "kind": kind, "content": content, "title": "", "file": path.name}, h


class FolderStore:
    """The per-session canvas root. Same surface the server used on the old single-file `Store`
    (`load` / `start` / `touch` / `discard` / `path` / `enabled`), plus the file operations."""

    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = Path(root)
        self.path = self.root                  # `status` names where the canvas lives
        self.enabled = enabled
        self._snapshot = None
        self._dirty = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        # lane -> {file name: content hash} of what is on screen. Shared with the watcher so a file
        # the TOOL just wrote is recognised as already-rendered and never loops back (TC3).
        self.known: dict[str, dict[str, str]] = {}
        self.known_lock = threading.Lock()

    # ---- layout on disk ---------------------------------------------------

    def lane_dir(self, lane: str) -> Path:
        return self.root / safe_name(lane)

    def lanes_on_disk(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("."))

    def read_index(self, lane: str) -> list[dict] | None:
        """The lane's index entries, or None when there is none or it cannot be parsed.

        A parse failure is NOT an empty board (NFR3): the caller keeps its last good layout and the
        next debounced write repairs the file from memory."""
        p = self.lane_dir(lane) / INDEX
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            frames = raw.get("frames")
            if not isinstance(frames, list):
                raise ValueError("frames is not a list")
            return [f for f in frames if isinstance(f, dict) and f.get("file")]
        except Exception as exc:                          # noqa: BLE001 — see docstring
            print("command-bridge: keeping the last good layout, unreadable %s (%s)" % (p, exc),
                  file=sys.stderr)
            return None

    # ---- loading ----------------------------------------------------------

    def load(self) -> tuple[dict[str, dict[str, dict]], dict[str, dict], str | None]:
        """Return (lanes, geometry, live) from the folders. Never raises, never refuses to start.

        Frames come back in INDEX order (so the sticky layout replays in the order it was built),
        then any file the index does not name yet, oldest first. A frame's saved x/y becomes its
        `at`, which the page honours instead of re-packing — that is what makes the layout survive a
        restart (AC6)."""
        lanes: dict[str, dict[str, dict]] = {}
        if not self.enabled:
            return lanes, {}, None
        for lane in self.lanes_on_disk():
            d = self.lane_dir(lane)
            entries = self.read_index(lane) or []
            pos = {e["file"]: e for e in entries}
            files = [p for p in d.iterdir() if p.is_file() and not ignored(p.name) and kind_of(p.name)]
            rank = {e["file"]: i for i, e in enumerate(entries)}
            files.sort(key=lambda p: (rank.get(p.name, len(rank)), p.stat().st_mtime))
            frames: dict[str, dict] = {}
            known: dict[str, str] = {}
            for p in files:
                got = read_frame(p, lane)
                if not got:
                    continue
                frame, h = got
                if frame["id"] in frames:            # a duplicate stem: first one wins, doctor reports
                    continue
                st = p.stat()
                frame["created"] = st.st_ctime
                frame["updated"] = st.st_mtime
                e = pos.get(p.name)
                if e and isinstance(e.get("x"), (int, float)) and isinstance(e.get("y"), (int, float)):
                    frame["at"] = [e["x"], e["y"]]
                frames[frame["id"]] = frame
                known[p.name] = h
            if frames:
                lanes[lane] = frames
            with self.known_lock:
                self.known[lane] = known
        return lanes, {}, None

    # ---- the tool's own frame writes (generators, ink, migration) ----------

    def write_frame(self, lane: str, fid: str, kind: str, content: str) -> str:
        """Write a frame the TOOL generated as its native file, atomically, and record its hash so
        the watcher sees it as already rendered (TC3). Returns the file name.

        An existing file for the same id keeps its name (and so its extension) when the kind still
        matches, so re-sending a chart rewrites `costs.vl.json` rather than adding a second file."""
        d = self.lane_dir(lane)
        ext = EXT_FOR_KIND.get(kind, ".txt")
        name = safe_name(fid) + ext
        data = content.encode("utf-8")
        with self.known_lock:
            self.known.setdefault(lane, {})[name] = digest(data)
        _atomic_write(d / name, data)
        return name

    def delete_frame(self, lane: str, name: str) -> None:
        with self.known_lock:
            self.known.get(lane, {}).pop(name, None)
        try:
            (self.lane_dir(lane) / name).unlink()
        except OSError:
            pass

    # ---- the index ---------------------------------------------------------

    def start(self, snapshot) -> None:
        """`snapshot` is called on the writer thread to get what to persist."""
        if not self.enabled:
            return
        self._snapshot = snapshot
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def touch(self) -> None:
        """The layout changed. Returns immediately; the writer coalesces the burst."""
        self._dirty.set()

    def _run(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        while True:
            self._dirty.wait()
            time.sleep(DEBOUNCE)
            self._dirty.clear()
            try:
                self.write(snapshot())
            except Exception as exc:                  # noqa: BLE001
                print("command-bridge: canvas index save failed (%s)" % exc, file=sys.stderr)

    @staticmethod
    def index_entries(frames: dict[str, dict], geometry: dict[str, dict]) -> list[dict]:
        """What a lane's index.json holds: id, file and position — NEVER content (FR4/AC3).

        The position is the frame's pinned `at` when it has one, else where the page last measured it
        — so a frame the page placed once is written down and comes back to the same spot."""
        out = []
        for fid, f in frames.items():
            if not f.get("file"):
                continue
            e = {"id": fid, "file": f["file"]}
            at = f.get("at")
            g = geometry.get(fid) or {}
            if at:
                e["x"], e["y"] = at[0], at[1]
            elif isinstance(g.get("x"), (int, float)) and isinstance(g.get("y"), (int, float)):
                e["x"], e["y"] = g["x"], g["y"]
            if isinstance(g.get("w"), (int, float)):
                e["w"], e["h"] = g["w"], g.get("h")
            out.append(e)
        return out

    def write(self, payload: dict) -> None:
        """Write every lane's index.json from a server snapshot. Atomic per lane."""
        if not self.enabled:
            return
        lanes = payload.get("lanes") or {}
        geometry = payload.get("geometry") or {}
        with self._lock:
            for lane in set(lanes) | set(self.lanes_on_disk()):
                d = self.lane_dir(lane)
                if not d.is_dir() and not lanes.get(lane):
                    continue
                entries = self.index_entries(lanes.get(lane) or {}, geometry.get(lane) or {})
                body = json.dumps({"version": VERSION, "frames": entries}, indent=1).encode()
                _atomic_write(d / INDEX, body)

    def discard(self) -> None:
        """Kept for the server's `clear`-the-last-lane path; the files ARE the canvas, and `clear`
        already deleted them, so there is nothing further to throw away."""
        return


# ---- the doctor (spec 022 KD8 / T7) ------------------------------------------------------------

def doctor(root: Path, lane: str | None = None, fix: bool = False) -> dict:
    """Reconcile each lane folder against its index — the standing maintenance the folder shape costs.

    Findings, per lane:
      orphan      a frame file the index does not name     fix: add an entry (no position yet)
      dangling    an index entry whose file is gone          fix: drop the entry
      duplicate   two files with one id (stem)               fix: rename all but the newest `<id>-N`
      unknown     a file whose extension is no render kind   reported only — not ours to delete
      bad_index   an index.json that does not parse          fix: rebuild it from the files
    """
    st = FolderStore(root)
    lanes = [lane] if lane else st.lanes_on_disk()
    report: dict = {"root": str(root), "fixed": fix, "lanes": {}}
    total = 0
    for ln in lanes:
        d = st.lane_dir(ln)
        if not d.is_dir():
            continue
        files = sorted(p for p in d.iterdir() if p.is_file() and not ignored(p.name))
        entries = st.read_index(ln)
        bad_index = entries is None and (d / INDEX).exists()
        entries = entries or []
        named = {e["file"] for e in entries}
        frames = [p for p in files if kind_of(p.name)]
        unknown = [p.name for p in files if not kind_of(p.name)]
        orphan = [p.name for p in frames if p.name not in named]
        present = {p.name for p in frames}
        dangling = [e["file"] for e in entries if e["file"] not in present]
        by_id: dict[str, list[Path]] = {}
        for p in frames:
            by_id.setdefault(kind_of(p.name)[1], []).append(p)  # type: ignore[index]
        duplicate = {fid: [p.name for p in ps] for fid, ps in by_id.items() if len(ps) > 1}
        found: dict[str, object] = {k: v for k, v in (("orphan", orphan), ("dangling", dangling),
                                                      ("duplicate", duplicate), ("unknown", unknown))
                                    if v}
        if bad_index:
            found["bad_index"] = True
        total += sum(len(v) if isinstance(v, (list, dict)) else 1 for v in found.values())
        if fix and found:
            renamed = {}
            for fid, ps in by_id.items():
                if len(ps) < 2:
                    continue
                ps = sorted(ps, key=lambda p: p.stat().st_mtime, reverse=True)
                for n, p in enumerate(ps[1:], start=2):
                    ext = p.name[len(fid):]
                    new = d / ("%s-%d%s" % (fid, n, ext))
                    while new.exists():
                        n += 1
                        new = d / ("%s-%d%s" % (fid, n, ext))
                    p.rename(new)
                    renamed[p.name] = new.name
            keep = [e for e in entries if e["file"] in present]
            keep = [{**e, "file": renamed.get(e["file"], e["file"]),
                     "id": kind_of(renamed.get(e["file"], e["file"]))[1]}  # type: ignore[index]
                    for e in keep]
            have = {e["file"] for e in keep}
            for p in sorted(d.iterdir(), key=lambda q: q.stat().st_mtime):
                if p.is_file() and not ignored(p.name) and kind_of(p.name) and p.name not in have:
                    keep.append({"id": kind_of(p.name)[1], "file": p.name})  # type: ignore[index]
            _atomic_write(d / INDEX, json.dumps({"version": VERSION, "frames": keep},
                                                indent=1).encode())
            if renamed:
                found["renamed"] = renamed
        report["lanes"][ln] = found
    report["findings"] = total
    report["ok"] = total == 0
    return report


# ---- migration from the single JSON store (spec 022 FR10 / T8) ---------------------------------

def migrate(legacy: Path, root: Path) -> dict | None:
    """Split the old one-file store into per-lane folders, once.

    Runs when the legacy file exists, lane by lane: a lane whose folder already holds a frame is
    SKIPPED, so it can never overwrite work already drawn the new way (an agent that started writing
    files before the first restart must not cost every other lane its canvas — caught live
    2026-09-25, when one lane's early image blocked the whole migration). Every frame becomes
    `<lane>/<id><ext>` (extension from its kind), its position goes into the lane's index, and the
    old file is RENAMED to `.migrated` rather than deleted, so the upgrade is one rollback away from
    undone. Returns what it did, or None when there was no legacy file."""
    if not legacy.exists():
        return None
    st = FolderStore(root)

    def has_frames(ln: str) -> bool:
        d = st.lane_dir(ln)
        return d.is_dir() and any(p.is_file() and kind_of(p.name) and not ignored(p.name)
                                  for p in d.iterdir())
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
        lanes = raw.get("lanes") or {}
        geometry = raw.get("geometry") or {}
    except Exception as exc:                              # noqa: BLE001 — never block a start
        return {"migrated": False, "error": "unreadable legacy canvas: %s" % exc}
    done: dict[str, int] = {}
    skipped: list[str] = []
    for ln, frames in lanes.items():
        if not isinstance(frames, dict):
            continue
        if has_frames(ln):
            skipped.append(ln)
            continue
        entries = []
        for fid, f in frames.items():
            if not isinstance(f, dict) or not f.get("kind"):
                continue
            content = f.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content)
            name = st.write_frame(ln, fid, str(f["kind"]), content)
            e = {"id": kind_of(name)[1], "file": name}  # type: ignore[index]
            g = (geometry.get(ln) or {}).get(fid) or {}
            at = f.get("at")
            if at:
                e["x"], e["y"] = at[0], at[1]
            elif isinstance(g.get("x"), (int, float)):
                e["x"], e["y"] = g["x"], g.get("y", 0)
            entries.append(e)
        if entries:
            _atomic_write(st.lane_dir(ln) / INDEX,
                          json.dumps({"version": VERSION, "frames": entries}, indent=1).encode())
            done[ln] = len(entries)
    retired = legacy.with_name(legacy.name + ".migrated")
    os.replace(legacy, retired)
    return {"migrated": True, "lanes": done, "frames": sum(done.values()), "skipped": skipped,
            "retired_to": str(retired)}
