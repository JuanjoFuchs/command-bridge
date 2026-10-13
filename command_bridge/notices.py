"""Spec 036: a heads-up on his phone for a clip an agent marked important, even with the orb off.

JJ, 2026-10-09: *"I would like you to be able to send me notifications for important stuff even when
the orb is off."* · *"But just as a notification, right? So that when I turn the orb on, I get to hear
it."* So a notice is NOT the message: the clip stays a held clip exactly as before, and the notice only
says it is waiting. Its `played` receipt resolves the notice, and the phone clears the notification.

DUMB like the rest of the tool: a JSON file per session and a sequence number. Every change (a new
notice, a notice resolved) takes the next `seq`, so a phone that was offline asks for everything after
the last `seq` it saw and gets each change once, in order. Persisted, so a notice survives a restart.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from . import config

KEEP = 100
"""The newest notices kept. A phone offline for longer than this many marked messages misses the oldest
heads-ups, never a clip: the clips themselves are held by the server as always."""

TEXT_MAX = 300
"""A notification shows a line or two; the clip carries the whole message."""

_lock = threading.Lock()


def path(session: str) -> str:
    return os.path.join(config.session_dir(), f"{session}.notices.json")


def _load(session: str) -> dict[str, Any]:
    try:
        with open(path(session), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"seq": 0, "next_id": 1, "items": []}


def _save(session: str, data: dict[str, Any]) -> None:
    p = path(session)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def add(session: str, clip: str, lane: str, text: str) -> dict[str, Any]:
    """Record a heads-up for `clip`. Returns the notice."""
    with _lock:
        data = _load(session)
        data["seq"] = int(data.get("seq", 0)) + 1
        item = {"id": int(data.get("next_id", 1)), "seq": data["seq"], "clip": clip, "lane": lane,
                "text": text.strip()[:TEXT_MAX], "at": round(time.time(), 1), "resolved": False}
        data["next_id"] = item["id"] + 1
        data["items"] = (data["items"] + [item])[-KEEP:]
        _save(session, data)
    return item


def resolve(session: str, clip: str) -> bool:
    """He heard `clip`: its notice is resolved (the phone clears it). False when it had no notice."""
    if not clip:
        return False
    with _lock:
        data = _load(session)
        hit = [i for i in data["items"] if i.get("clip") == clip and not i.get("resolved")]
        if not hit:
            return False
        for i in hit:
            data["seq"] = int(data.get("seq", 0)) + 1
            i["resolved"] = True
            i["seq"] = data["seq"]
        _save(session, data)
    return True


def since(session: str, seq: int) -> tuple[list[dict[str, Any]], int]:
    """Every notice changed after `seq`, oldest change first, and the latest `seq`. A `seq` ahead of
    the server's (its file was reset) is read as 0, so the phone resynchronises instead of waiting
    for numbers that will never come."""
    data = _load(session)
    latest = int(data.get("seq", 0))
    if seq > latest:
        seq = 0
    items = sorted((i for i in data["items"] if int(i.get("seq", 0)) > seq), key=lambda i: i["seq"])
    return items, latest
