"""Specs 037/038: liaison mode — the liaisons speak to him and hold the camera; the others work quietly.

JJ, 2026-10-09: *"there are two layers: a liaison, an agent that I speak to, versus the rest of agents
that only speak through my liaisons."* A second liaison can manage its own group of workers
(e.g. phoenix manages atlas and vega).

The mode is a map: each liaison lane → the worker lanes it manages explicitly. A lane no liaison claims
belongs to the FIRST liaison (the one he set up first, magnus), so a newly started agent always has
someone to speak for it. While the map is non-empty:

- a file edit never moves the camera (only a liaison's explicit `point --look` does);
- a worker's `say` and camera moves are refused, naming ITS liaison;
- a worker's held clips stay held; he cannot switch to a worker's lane.

Persisted per session, so a restart keeps it. DUMB like the rest of the tool: one JSON file.
"""
from __future__ import annotations

import json
import os

from . import config

Liaisons = dict[str, list[str]]


def path(session: str) -> str:
    return os.path.join(config.session_dir(), f"{session}.liaison.json")


def load(session: str) -> Liaisons:
    try:
        with open(path(session), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("liaisons")
    if isinstance(raw, dict):
        return {str(k): [str(w) for w in (v or []) if isinstance(w, str)]
                for k, v in raw.items() if isinstance(k, str) and k}
    lane = data.get("lane")   # spec 037's single-liaison file
    return {lane: []} if isinstance(lane, str) and lane else {}


def save(session: str, liaisons: Liaisons) -> None:
    p = path(session)
    if not liaisons:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"liaisons": liaisons}, f)
    os.replace(tmp, p)


def parse(specs: list[str]) -> Liaisons:
    """`["magnus", "phoenix=atlas,vega"]` → `{"magnus": [], "phoenix": ["atlas", "vega"]}`, in order.
    Raises ValueError on an empty name or a lane claimed twice."""
    out: Liaisons = {}
    seen: set[str] = set()
    for spec in specs:
        lead, _, rest = str(spec).partition("=")
        lead = lead.strip()
        workers = [w.strip() for w in rest.split(",") if w.strip()]
        if not lead:
            raise ValueError(f"empty liaison name in {spec!r}")
        for name in [lead, *workers]:
            if name in seen:
                raise ValueError(f"lane {name!r} appears twice")
            seen.add(name)
        out[lead] = workers
    return out


def liaison_for(liaisons: Liaisons, lane: str) -> str | None:
    """Who speaks for `lane`: itself if it is a liaison, the liaison that claims it, else the first
    liaison. None when the mode is off."""
    if not liaisons:
        return None
    if lane in liaisons:
        return lane
    for lead, workers in liaisons.items():
        if lane in workers:
            return lead
    return next(iter(liaisons))


def is_worker(liaisons: Liaisons, lane: str) -> bool:
    return bool(liaisons) and lane not in liaisons


def groups(liaisons: Liaisons, lanes: list[str]) -> list[dict]:
    """Every registered lane, grouped under its liaison, in the liaisons' order: `[{lead, workers}]`.
    A liaison that is not a registered lane is still listed (its card is simply absent)."""
    out = [{"lead": lead, "workers": []} for lead in liaisons]
    index = {g["lead"]: g for g in out}
    for lane in lanes:
        if lane in liaisons:
            continue
        lead = liaison_for(liaisons, lane)
        if lead is not None:
            index[lead]["workers"].append(lane)
    return out


def refusal(session: str, liaisons: Liaisons, lane: str) -> dict:
    """The answer a worker lane's `say` gets: who speaks for it, and how to reach him through them."""
    lead = liaison_for(liaisons, lane) or ""
    return {
        "spoke": False,
        "error": f"liaison mode: {lane} speaks to him through {lead}. Nothing was synthesized or queued.",
        "code": "liaison_mode",
        "liaison": lead,
        "liaisons": list(liaisons),
        "lane": lane,
        "remedy": f"command-bridge brief --session {session} --lane {lead} --file <your report>",
        "next": f"Write what he needs to know (start it '[from {lane}]', voice-ready) and brief "
                f"{lead}, who decides what reaches him. Keep working; keep your watch armed.",
    }
