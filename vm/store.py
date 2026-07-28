"""vm.store — the JSONL turn log and its cursor reads.

One file per session at `<VM_DIR>/<session>.jsonl`, one turn object per line. The server
appends while readers read concurrently; a single writer doing line-buffered appends plus
whole-file reads needs no locking.

**No reasoning lives here.** This module appends and reads. Whether a turn matters is the
agent's problem.

STDLIB ONLY so the read half runs under a plain `python` with no venv.

Turn schema (spec 001):
    {"id", "session", "t_start", "t_end", "text", "addressed", "final", "wall"}
`text` is UNTRUSTED — speech captured from a microphone, data and never instructions.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import config

# A session id becomes a filename, so it is the path-traversal surface. Reject rather than
# sanitize: a rejection is a bug report, a sanitized id is a silent collision.
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_session(session: str) -> str:
    """Return `session` if it is a safe filesystem-bound id, else raise ValueError."""
    if not isinstance(session, str):
        raise ValueError("session must be a string")
    if not _SESSION_RE.match(session):
        raise ValueError(
            "invalid session id: must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
        )
    if ".." in session:
        raise ValueError("invalid session id: must not contain '..'")
    if any(ord(c) < 0x20 for c in session):
        raise ValueError("invalid session id: control characters (<0x20) not allowed")
    return session


def log_path(session: str, base: Optional[str] = None) -> str:
    """Absolute path to a session's log, creating the base directory if needed."""
    validate_session(session)
    base = base or config.session_dir()
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{session}.jsonl")


def read_turns(session: str, base: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every turn in the log, in id order. Malformed lines are skipped rather than fatal —
    a partially-written final line must never make the whole log unreadable."""
    path = log_path(session, base)
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def next_id(session: str, base: Optional[str] = None) -> int:
    turns = read_turns(session, base)
    return (max((int(t.get("id", -1)) for t in turns), default=-1)) + 1


def append_turn(
    session: str,
    text: str,
    t_start: float,
    t_end: float,
    addressed: bool,
    final: bool = True,
    base: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one turn and return it (with its assigned `id`)."""
    validate_session(session)
    turn = {
        "id": next_id(session, base),
        "session": session,
        "t_start": round(float(t_start), 3),
        "t_end": round(float(t_end), 3),
        "text": text,
        "addressed": bool(addressed),
        "final": bool(final),
        "wall": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    path = log_path(session, base)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(turn, ensure_ascii=False) + "\n")
        fh.flush()
    return turn


def turns_since(
    session: str, cursor: int, base: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], int]:
    """Every turn with `id > cursor`, plus the new cursor.

    Returning *all* of them is the contract, not an optimization: the agent reasons for an
    unbounded time between calls and the log keeps growing meanwhile. Returning only the
    newest would silently drop everything said while it was thinking.
    """
    turns = [t for t in read_turns(session, base) if int(t.get("id", -1)) > cursor]
    turns.sort(key=lambda t: int(t.get("id", -1)))
    new_cursor = int(turns[-1]["id"]) if turns else cursor
    return turns, new_cursor


def watch(
    session: str,
    cursor: int,
    timeout: float = 30.0,
    poll: float = 0.1,
    base: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Block until at least one turn with `id > cursor` exists, or `timeout` elapses.

    On timeout returns `([], cursor)` — an empty result is a heartbeat, not an error, so a
    caller can distinguish "nothing said" from "the tunnel died".
    """
    deadline = time.monotonic() + timeout
    while True:
        turns, new_cursor = turns_since(session, cursor, base)
        if turns:
            return turns, new_cursor
        if time.monotonic() >= deadline:
            return [], cursor
        time.sleep(poll)


def list_sessions(base: Optional[str] = None) -> List[str]:
    base = base or config.session_dir()
    if not os.path.isdir(base):
        return []
    return sorted(
        f[:-6] for f in os.listdir(base) if f.endswith(".jsonl")
    )
