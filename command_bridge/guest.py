"""Spec 042: guest mode — on a guest lane, a voice that is not his still reaches the agent.

Observed 2026-10-09 with a second person talking to an agent on one lane: "they just spoke on that
turn, but Command Bridge did not recognize them because it's tuned to my voice." Every one of the
guest's turns was logged `addressed:false` (`not-owner:0.00` / `not-addressed`), so the lane's
`watch` never woke and the agent missed what they said.

So a lane can be marked a GUEST lane: there, a final turn the voiceprint refused as "not him" is
addressed anyway and tagged `speaker: guest`, so the agent knows it is not JJ. Nothing else moves:
an agent's own clip coming back (`echo`) and a refused summons stay refused, his own turns are
untouched, and every other lane behaves exactly as before. Off by default, persisted per session.
"""
from __future__ import annotations

import json
import os

from . import config

ADMITTED_REASONS = ("not-owner", "not-addressed")
"""The refusals that only mean "the voiceprint does not know this voice" — the ones a guest turns
into "addressed". Anything else (echo, an ambiguous or bare summons) stays exactly as decided."""


def admit(addressed: bool, reason: str, lane: str | None, guest_lanes) -> tuple[bool, str, str]:
    """`(addressed, reason, speaker)` for a final turn. `speaker` is "guest" when guest mode let it
    through, else "" (his turns keep their shape)."""
    if addressed or not lane or lane not in guest_lanes:
        return addressed, reason, ""
    if not any((reason or "").startswith(r) for r in ADMITTED_REASONS):
        return addressed, reason, ""
    return True, "guest", "guest"


def path(session: str) -> str:
    return os.path.join(config.session_dir(), f"{session}.guests.json")


def load(session: str) -> set[str]:
    try:
        with open(path(session), encoding="utf-8") as f:
            lanes = json.load(f).get("lanes", [])
        return {str(x) for x in lanes if isinstance(x, str)}
    except (OSError, ValueError, AttributeError):
        return set()


def save(session: str, lanes: set[str]) -> None:
    p = path(session)
    if not lanes:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"lanes": sorted(lanes)}, f)
    os.replace(tmp, p)
