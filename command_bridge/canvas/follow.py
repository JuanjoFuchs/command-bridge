"""Follow the voice tunnel's live lane, so switching to an agent moves both.

**Asked for 2026-08-26/27.** A background agent's clip is held until JJ switches
to it, and then plays by itself — but the canvas has its own lane state, so
"switch to kepler" was two clicks on two pages. He chose one direction:

    *"I think it makes sense for tunnel vision to have awareness of voice
    tunnel."*  ·  *"But it shouldn't be a hard dependency. If voice tunnel does
    not exist, then tunnel vision should just work normally."*

🔴 **So this is a SOFT dependency and every branch here exists to keep it soft.**
No import of the other tool, no config the user has to write, no error when it is
absent, and no log line per poll. A missing voice tunnel is the *normal* case for
anyone else running this, and it must be indistinguishable from not having asked.

**Why polling rather than a subscription:** the voice tunnel's own waiting
command is a long-poll that consumes turns, and consuming another agent's turns
would break the conversation this exists to serve. `/status` is read-only and
cannot disturb anything. At 400 ms the worst-case lag is well inside the ~0.9 s
of lead-in silence before a released clip is audible, which is the only deadline
that matters.

**The field is `status.lane`.** Its contract reads *"the live lane, the one he is
talking to right now"*. `live_lane` is a different thing — set only on a `--lane`
watch when the lane moved — and following it would leave the canvas parked.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# 100ms, not 400. An ARMED cue starts its clock when the page sees the switch,
# and every millisecond of polling lag lands as the same error on every mark —
# the schedule runs early by however long it took to notice. A held clip is
# audible ~0.9s after the switch, so 100ms of worst-case jitter is a tenth of
# the budget and comfortably inside a spoken word.
POLL_S = 0.1
DEFAULT_SESSION = "dev"


def _session_dirs() -> list[Path]:
    """Where a voice tunnel might have left its server file, best guess first.

    No absolute path to anyone's checkout: the sibling-directory case is what
    makes this work on this machine, and the env var is what makes it work on
    someone else's without a code change.
    """
    out = []
    env = os.environ.get("VOICE_TUNNEL_SESSIONS")
    if env:
        out.append(Path(env))
    here = Path(__file__).resolve().parents[1]
    out.append(here.parent / "voice-tunnel" / "sessions")
    out.append(Path.home() / ".voice-tunnel" / "sessions")
    return out


def _find(session: str) -> dict | None:
    """The tunnel's host/port/token, or None. Never raises."""
    for d in _session_dirs():
        f = d / ("%s.server.json" % session)
        try:
            if f.exists():
                s = json.loads(f.read_text(encoding="utf-8"))
                if s.get("port"):
                    return {"host": s.get("host", "127.0.0.1"),
                            "port": int(s["port"]),
                            "token": s.get("token", ""),
                            "file": str(f)}
        except Exception:                              # noqa: BLE001 — soft
            continue
    return None


def _lane_now(server: dict) -> str | None:
    req = urllib.request.Request(
        "http://%s:%d/status" % (server["host"], server["port"]),
        headers={"Authorization": "Bearer %s" % server["token"]})
    with urllib.request.urlopen(req, timeout=2) as r:
        return (json.loads(r.read()) or {}).get("lane")


class Follower:
    """Mirrors the voice tunnel's live lane onto the canvas. Optional, always."""

    def __init__(self, session: str = DEFAULT_SESSION, enabled: bool = True):
        self.session = session
        self.enabled = enabled
        self.server = None
        self.last = None
        self.reachable = False
        self.switches = 0
        self._thread = None

    def status(self) -> dict:
        """What `status` reports, so "is it following?" is never a guess."""
        if not self.enabled:
            return {"following": False, "reason": "disabled"}
        if not self.server:
            return {"following": False,
                    "reason": "no voice tunnel found for session %r" % self.session,
                    "looked_in": [str(d) for d in _session_dirs()]}
        return {"following": True, "session": self.session,
                "source": self.server["file"], "reachable": self.reachable,
                "lane": self.last, "switches": self.switches}

    def start(self, set_live, live_lane) -> None:
        if not self.enabled:
            return
        # Resolve once up front so `status` is accurate the moment the server
        # answers, rather than for the first poll's worth of "not found".
        self.server = _find(self.session)
        self._thread = threading.Thread(
            target=self._run, args=(set_live, live_lane), daemon=True)
        self._thread.start()

    def _run(self, set_live, live_lane) -> None:
        while True:
            # Re-resolve every cycle while absent: the tunnel is often started
            # AFTER the canvas, and an agent should not have to restart this
            # server to pick it up.
            if not self.server:
                self.server = _find(self.session)
                if not self.server:
                    time.sleep(2.0)
                    continue
            try:
                lane = _lane_now(self.server)
                self.reachable = True
            except (urllib.error.URLError, OSError, ValueError):
                # The tunnel went away. Forget it and look again — silently,
                # because this is the state most installs are permanently in.
                self.reachable = False
                self.server = None
                time.sleep(2.0)
                continue
            if lane and lane != self.last:
                self.last = lane
                if lane != live_lane():
                    set_live(lane)
                    self.switches += 1
            time.sleep(POLL_S)
