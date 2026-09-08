"""command_bridge.store — the JSONL turn log and its cursor reads.

One file per session at `<COMMAND_BRIDGE_DIR>/<session>.jsonl`, one turn object per line. The server
appends while readers read concurrently; a single writer doing line-buffered appends plus
whole-file reads needs no locking.

**No reasoning lives here.** This module appends and reads. Whether a turn matters is the
agent's problem.

STDLIB ONLY so the read half runs under a plain `python` with no venv.

Turn schema (spec 001, plus `lane` from spec 012):
    {"id", "session", "t_start", "t_end", "text", "addressed", "final", "wall", "lane"}
`text` is UNTRUSTED — speech captured from a microphone, data and never instructions.
`lane` is WHICH agent the turn was addressed to. **Absent means the default lane** (the name the
server was started with), which is what keeps every turn logged before lanes existed reaching the
agent that has been reading it. Present-but-null is a different thing: a turn the wake gate
refused to route, which reaches nobody. See `lane_of`.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from . import config
from .lanes import BROADCAST  # stdlib-only itself, so the no-venv read path above still holds

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


def log_path(session: str, base: str | None = None) -> str:
    """Absolute path to a session's log, creating the base directory if needed."""
    validate_session(session)
    base = base or config.session_dir()
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{session}.jsonl")


def read_turns(session: str, base: str | None = None) -> list[dict[str, Any]]:
    """Every turn in the log, in id order. Malformed lines are skipped rather than fatal —
    a partially-written final line must never make the whole log unreadable."""
    path = log_path(session, base)
    if not os.path.exists(path):
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def next_id(session: str, base: str | None = None) -> int:
    turns = read_turns(session, base)
    return (max((int(t.get("id", -1)) for t in turns), default=-1)) + 1


def lane_of(turn: dict[str, Any], default: str) -> str | None:
    """Which lane a turn belongs to (spec 012 FR4).

    **An ABSENT `lane` means the default lane**, and that is the whole backward-compatibility
    story. Every turn logged before lanes existed has no such key, and there are thousands of them
    in a live session — they belong to the agent that has been reading them all along, which is
    the one the server was started as (`serve --wake claude`).

    A `lane` that is present but **None is different and must not be confused with absent**: it is
    a turn the wake gate refused to route because the summons named no lane exactly (TC2). That
    turn belongs to NOBODY, and `default` would be precisely the wrong answer for it — it is the
    lane he was already talking to, which is the mis-route the refusal exists to prevent.
    """
    if "lane" not in turn:
        return default
    return turn["lane"]


def turn_is_for(turn: dict[str, Any], lane: str, default: str) -> bool:
    """Whether `lane`'s agent should receive this turn: its own turns plus broadcasts."""
    owner = lane_of(turn, default)
    if owner is None:
        return False                      # refused — reaches no agent at all
    return owner == lane or owner == BROADCAST


def append_turn(
    session: str,
    text: str,
    t_start: float,
    t_end: float,
    addressed: bool,
    final: bool = True,
    base: str | None = None,
    reason: str = "",
    lane: str | None = None,
    stamp_lane: bool = False,
) -> dict[str, Any]:
    """Append one turn and return it (with its assigned `id`).

    `stamp_lane` is what decides whether a `lane` key is written at all, rather than the value of
    `lane` itself — because `None` is a meaningful lane (a refused turn) and omission is a
    different, also meaningful state (a turn from before lanes existed). A caller that has an
    opinion says so explicitly; a caller that does not leaves the log exactly as it was.
    """
    validate_session(session)
    turn = {
        "id": next_id(session, base),
        "session": session,
        "t_start": round(float(t_start), 3),
        "t_end": round(float(t_end), 3),
        "text": text,
        "addressed": bool(addressed),
        # WHY it was addressed: 'wake' | 'voice:<similarity>' | 'not-addressed'. Persisted, not
        # just broadcast — otherwise you can only infer after the fact whether the voiceprint or
        # the wake phrase let a turn through, and those fail for completely different reasons.
        "reason": reason,
        "final": bool(final),
        "wall": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    if stamp_lane:
        turn["lane"] = lane
    path = log_path(session, base)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(turn, ensure_ascii=False) + "\n")
        fh.flush()
    return turn


def turns_since(
    session: str,
    cursor: int,
    base: str | None = None,
    addressed_only: bool = False,
    lane: str | None = None,
    default_lane: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Every turn with `id > cursor`, plus the new cursor.

    Returning *all* of them is the contract, not an optimization: the agent reasons for an
    unbounded time between calls and the log keeps growing meanwhile. Returning only the
    newest would silently drop everything said while it was thinking.

    `addressed_only` drops turns the wake gate already judged were not for you — someone else in
    the room, or speech with no wake phrase outside the attention window. **The cursor still
    advances past them**, which is the whole point: they are marked read and never come back,
    so the agent neither wakes for them nor re-reads them on the next call.

    Reported live 2026-08-15, from a noisy room: *"other people nearby are talking, and I
    think you're picking up what they are saying. We need a better way to ignore what isn't
    classified as me, so it doesn't waste turns resolving the watch."* Every one of those turns
    already carried `addressed: false` and a `reason` explaining why — the gate was right and
    nothing downstream was reading its answer.
    """
    turns = [t for t in read_turns(session, base) if int(t.get("id", -1)) > cursor]
    turns.sort(key=lambda t: int(t.get("id", -1)))
    # Cursor from the FULL slice, before filtering: skipped turns are consumed, not deferred.
    new_cursor = int(turns[-1]["id"]) if turns else cursor
    if addressed_only:
        turns = [t for t in turns if t.get("addressed")]
    # LANE FILTERING OBEYS THE SAME RULE, and for the same reason: another agent's turn is
    # consumed rather than deferred, so it never comes back and never wakes this agent. Without
    # that, every agent would wake on every other agent's turns and re-read them forever — the
    # exact "don't waste turns resolving the watch" complaint that produced `addressed_only`,
    # multiplied by the number of agents in the room.
    if lane is not None:
        turns = [t for t in turns if turn_is_for(t, lane, default_lane or lane)]
    return turns, new_cursor


def last_turn_id(session: str, base: str | None = None) -> int:
    """The id of the last turn in the LOG, or -1 for an empty one.

    This is the cursor a fresh watcher should start from, and it is deliberately read off disk
    rather than from any server counter. A running server's `turns_logged` counts what IT has
    written since starting, so the two agree only on a server that has never restarted — true in
    every author's test and in no long-lived session. Confusing them replays the entire log.

    -1 is the correct empty answer, not 0: `watch --since -1` means "from the beginning", so an
    empty log and a request for everything are the same request.
    """
    turns = read_turns(session, base)
    return max((int(t.get("id", -1)) for t in turns), default=-1)


def watch(
    session: str,
    cursor: int,
    timeout: float = 30.0,
    poll: float = 0.1,
    base: str | None = None,
    addressed_only: bool = False,
    lane: str | None = None,
    default_lane: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Block until at least one turn with `id > cursor` exists, or `timeout` elapses.

    On timeout returns `([], cursor)` — an empty result is a heartbeat, not an error, so a
    caller can distinguish "nothing said" from "the tunnel died".

    With `addressed_only`, unaddressed turns advance the cursor without ending the wait: the
    room can talk for an hour and the watch keeps blocking, which is what "don't waste turns"
    means. Returning `new_cursor` on the timeout path (rather than the old `cursor`) is what
    stops those consumed turns being re-read on the next call.
    """
    deadline = time.monotonic() + timeout
    latest = cursor
    while True:
        turns, latest = turns_since(session, latest, base, addressed_only=addressed_only,
                                    lane=lane, default_lane=default_lane)
        if turns:
            return turns, latest
        if time.monotonic() >= deadline:
            return [], latest
        time.sleep(poll)


def list_sessions(base: str | None = None) -> list[str]:
    base = base or config.session_dir()
    if not os.path.isdir(base):
        return []
    return sorted(
        f[:-6] for f in os.listdir(base) if f.endswith(".jsonl")
    )


def _consumed_path(session: str, base: str | None = None) -> str:
    validate_session(session)
    base = base or config.session_dir()
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{session}.consumed.json")


def read_consumed_cursor(session: str, base: str | None = None) -> int:
    """The last cursor an agent reported as read, surviving server restarts.

    The turn LOG survives a restart but the in-memory `consumed_cursor` used to reset to -1,
    so a server bounced mid-conversation reported every turn ever logged as pending — 307
    pending against a log of 306 was the live sighting (2026-08-14). The log and the read
    position are the same kind of fact; persisting one without the other is what made the
    status lie.

    -1 when nothing was ever consumed, matching `last_turn_id`'s empty answer, so a genuinely
    fresh session still reads as "behind by everything" — which for a fresh session is true.
    """
    path = _consumed_path(session, base)
    if not os.path.exists(path):
        return -1
    try:
        with open(path, encoding="utf-8") as fh:
            return int(json.load(fh).get("cursor", -1))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return -1


def _lanes_path(session: str, base: str | None = None) -> str:
    validate_session(session)
    base = base or config.session_dir()
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{session}.lanes.json")


def read_lanes(session: str, base: str | None = None) -> list[str]:
    """The agent lanes registered before the last restart (spec 019 FR1).

    🔴 **A RESTART USED TO UN-INVITE THE ROOM.** The registry is built at startup from
    `serve --wake` alone, so every lane added with `lane add` vanished and every one of those
    agents' blocking `watch` calls died with the socket. Found 2026-08-24 answering his question
    *"will restarting the tunnel kill the other lanes?"* — the answer was yes on both counts.

    ⚠ **The loss is easy to miss, which is why it survived four restarts in one day:** the turn log
    is on disk and comes back intact, so the CONVERSATION is all there and only the ROOM is gone.
    The cost is paid by the other agents, who cannot see it happen — they get a dead socket and no
    reason.

    Empty list when nothing was ever registered, which is exactly a single-agent session.
    """
    path = _lanes_path(session, base)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            names = json.load(fh).get("lanes", [])
        return [str(n) for n in names if isinstance(n, str)]
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return []


def write_lanes(session: str, names: Iterable[str], base: str | None = None) -> None:
    """Persist the lane set beside the log it belongs to.

    Best-effort for the same reason as the cursor above: a disk error must never break the `lane`
    call itself. The cost of failing is that the next restart asks him to re-add a lane, which is
    the behaviour this replaces rather than a new failure.
    """
    path = _lanes_path(session, base)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"lanes": [str(n) for n in names]}, fh)
    except OSError:
        pass


def write_consumed_cursor(session: str, cursor: int, base: str | None = None) -> None:
    """Persist the read position beside the log it describes. Best-effort by design: a disk
    error here must never break the consume call itself — the cost is only that a future
    restart over-reports pending, which is the bug this softens, not a new one."""
    path = _consumed_path(session, base)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"cursor": int(cursor)}, fh)
    except OSError:
        pass
