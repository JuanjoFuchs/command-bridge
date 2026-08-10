"""voice_tunnel.timing — where did the time actually go.

**Why this exists.** Twice in one live session the answer to "why is this slow" had to be
reconstructed by hand, from clip IDs and log wall-clocks, after the fact. Both times the guess
was wrong in the same direction: the owner blamed the network, and the network was under a tenth of a
second — the tool spent ~2 s and the AGENT spent 39 s and 60 s. A stopwatch in someone's head is
not an instrument, and a conversation is exactly the kind of thing you cannot re-run to measure.

So every stage stamps itself as it happens, into `sessions/<session>.timing.jsonl`. Reading it
back with `voice-tunnel timing` turns "it feels slow" into a number next to a stage name.

Two clocks per event, deliberately:

  * ``mono`` — ``time.monotonic()``. What deltas are computed from. Immune to clock adjustment,
    which matters because a laptop that sleeps and resumes mid-session would otherwise produce
    negative durations and a very confusing bug report.
  * ``wall`` — local ISO-8601. What a human correlates against the turn log, git, and their own
    memory of "it was around then". Useless for arithmetic, essential for orientation.

Append-only, one JSON object per line, and **never allowed to break a session**: an instrument
that can take down the thing it measures is worse than no instrument. Every write is wrapped.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
from typing import Any

from . import config

SUFFIX = ".timing.jsonl"

_LOCK = threading.Lock()
"""One writer at a time. The server stamps from the event loop AND from executor threads, and an
interleaved half-line would corrupt the file for every later read."""

STAGES = (
    "utterance_end",   # the VAD closed a turn; the clock the user experiences starts here
    "transcribed",     # ASR returned text
    "turn_logged",     # the turn hit the JSONL and the client was told
    "consumed",        # the agent reported it had read that far  <- agent think time starts
    "say_requested",   # the agent called /say                    <- agent think time ends
    "synthesized",     # TTS produced audio
    "spoken",          # audio was pushed to the client
    "played",          # the client reported playback finished
)
"""The stages of one exchange, in order.

`consumed` -> `say_requested` is the gap that mattered most and was invisible: it is the agent
thinking, and it dwarfed everything the tool does. Naming it as a stage is the point — a
breakdown that only measures the tool would have confirmed the owner's wrong hypothesis instead of
correcting it."""


def path(session: str) -> str:
    return os.path.join(config.session_dir(), f"{session}{SUFFIX}")


def stamp(session: str, stage: str, **fields: Any) -> None:
    """Record one event. Never raises — diagnostics must not be able to break a conversation."""
    try:
        record = {
            "stage": stage,
            "mono": round(time.monotonic(), 4),
            "wall": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
        }
        record.update(fields)
        os.makedirs(config.session_dir(), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _LOCK:
            with open(path(session), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def read(session: str, limit: int = 0) -> list[dict[str, Any]]:
    """Every event, oldest first. A malformed line is skipped rather than fatal."""
    events: list[dict[str, Any]] = []
    try:
        with open(path(session), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events[-limit:] if limit else events


def _exchanges(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group events into exchanges, one per turn, each with its stage-to-stage deltas.

    An exchange starts at `utterance_end` and runs to the next one. Stages that never fired are
    simply absent — a turn the agent chose not to answer has no `say_requested`, and that is
    information, not a gap to paper over.
    """
    grouped: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for ev in events:
        if ev.get("stage") == "utterance_end":
            if current:
                grouped.append(current)
            current = {"started": ev.get("wall"), "events": [ev]}
        elif current is not None:
            current["events"].append(ev)
    if current:
        grouped.append(current)

    for ex in grouped:
        evs = ex.pop("events")
        ex["turn"] = next((e.get("turn") for e in evs if e.get("turn") is not None), None)
        ex["text"] = next((e.get("text") for e in evs if e.get("text")), None)
        first, last = evs[0], evs[-1]
        ex["total_s"] = round(last["mono"] - first["mono"], 2)
        steps = []
        # strict=False is correct here: evs[1:] is deliberately one shorter, which is what
        # makes this pair CONSECUTIVE events rather than requiring equal lengths.
        for prev, nxt in zip(evs, evs[1:], strict=False):
            steps.append({
                "from": prev["stage"],
                "to": nxt["stage"],
                "seconds": round(nxt["mono"] - prev["mono"], 2),
            })
        ex["steps"] = steps
        ex["slowest"] = max(steps, key=lambda s: s["seconds"]) if steps else None
    return grouped


def report(session: str, limit: int = 0) -> dict[str, Any]:
    """The `voice-tunnel timing` payload: recent exchanges, their slowest step, and the overall culprit."""
    events = read(session)
    if not events:
        return {
            "session": session,
            "exchanges": [],
            "note": "no timing recorded yet — the log is written as a session runs",
            "file": path(session),
        }
    exchanges = _exchanges(events)
    if limit:
        exchanges = exchanges[-limit:]

    # Which STAGE TRANSITION costs the most across the whole session. One slow exchange is
    # anecdote; the same transition topping every exchange is the thing to go fix.
    totals: dict[str, list[float]] = {}
    for ex in exchanges:
        for step in ex["steps"]:
            totals.setdefault(f"{step['from']} -> {step['to']}", []).append(step["seconds"])
    by_stage = sorted(
        (
            {
                "step": name,
                "count": len(xs),
                "total_s": round(sum(xs), 2),
                "avg_s": round(sum(xs) / len(xs), 2),
                "max_s": round(max(xs), 2),
            }
            for name, xs in totals.items()
        ),
        key=lambda r: r["total_s"],
        reverse=True,
    )
    return {
        "session": session,
        "file": path(session),
        "exchanges": exchanges,
        "by_step": by_stage,
        "worst_step": by_stage[0]["step"] if by_stage else None,
        "note": "`consumed -> say_requested` is the AGENT thinking; everything else is the tool",
    }
