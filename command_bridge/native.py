"""Spec 044: the server's half of the Android app's primitive protocol — every POLICY decision the
app used to make in Kotlin, made here so changing it needs no APK.

The app keeps only what Android forces to be native (mic, playback, audio mode/route, notifications,
the foreground service) and does what a `{"type": "native", "op": ...}` directive says. This module
is pure: it turns the session's facts and the app's reports into directives. `server.py` only wires
it to the socket. Two of the rules here were APK releases before this existed — spec 043's "the orb
off hangs up the call" and spec 040's "Heard you" — and the spec's acceptance test is that each
would now be an edit to this file alone.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

PROTOCOL = 1
"""The primitive protocol this server drives. An app advertising a higher one is driven at this one;
the ops it lists are the contract, so a newer app's extra ops are simply never sent."""

# android.media.AudioDeviceInfo.TYPE_* — the app reports raw types, the policy names them.
EARPIECE, SPEAKER, WIRED_HEADSET, WIRED_HEADPHONES, BLUETOOTH_SCO, USB_HEADSET, BLE_HEADSET = 1, 2, 3, 4, 7, 22, 26

ROUTE_PREFERENCE = (BLUETOOTH_SCO, BLE_HEADSET, WIRED_HEADSET, USB_HEADSET, WIRED_HEADPHONES, SPEAKER)
"""Spec 032/033: a headset if one is connected, else the loudspeaker — NEVER the earpiece by default
(JJ, 2026-10-09: "now it's using the headset as if I were speaking on the phone"). The earpiece is
reachable only as his explicit pick."""

ROUTE_LABELS = {
    SPEAKER: "Speaker", EARPIECE: "Phone (earpiece)", BLUETOOTH_SCO: "Bluetooth",
    BLE_HEADSET: "Bluetooth LE", WIRED_HEADSET: "Wired headset", WIRED_HEADPHONES: "Wired headphones",
    USB_HEADSET: "USB headset",
}
"""The caret's choices, in the order it lists them."""


@dataclass
class NativeClient:
    """One connected app that declared the primitive protocol, and what it last reported."""
    protocol: int
    ops: set[str]
    app: str = ""
    foreground: bool = False
    devices: list[int] = field(default_factory=list)
    mic: bool = False
    call: bool = False
    device: int | None = None
    mic_error: str | None = None
    hands: dict[str, int] = field(default_factory=dict)

    def as_status(self) -> dict[str, Any]:
        return {"protocol": self.protocol, "app": self.app, "ops": sorted(self.ops),
                "foreground": self.foreground, "devices": list(self.devices), "mic": self.mic,
                "call": self.call, "device": self.device, "mic_error": self.mic_error}


def from_hello(msg: dict) -> NativeClient | None:
    """The `native` block of a `hello`, or None for a browser or an app before the protocol."""
    n = msg.get("native")
    if not isinstance(n, dict):
        return None
    try:
        protocol = int(n.get("protocol") or 0)
    except (TypeError, ValueError):
        return None
    if protocol < 1:
        return None
    ops = {str(o) for o in (n.get("ops") or []) if isinstance(o, str)}
    return NativeClient(protocol=min(protocol, PROTOCOL), ops=ops, app=str(n.get("app") or ""))


def apply_report(client: NativeClient, msg: dict) -> None:
    """Adopt a `native_state` report. Missing fields keep their last value."""
    if "foreground" in msg:
        client.foreground = bool(msg["foreground"])
    if isinstance(msg.get("devices"), list):
        client.devices = [int(d) for d in msg["devices"] if isinstance(d, (int, float))]
    if "mic" in msg:
        client.mic = bool(msg["mic"])
    if "call" in msg:
        client.call = bool(msg["call"])
    if "device" in msg:
        d = msg["device"]
        client.device = int(d) if isinstance(d, (int, float)) else None
    if "mic_error" in msg:
        client.mic_error = str(msg["mic_error"]) if msg["mic_error"] else None


def choose_route(devices: list[int], picked: int | None) -> int | None:
    """His pick when that output is present, else the automatic rule."""
    if picked is not None and picked in devices:
        return picked
    return next((t for t in ROUTE_PREFERENCE if t in devices), None)


def wanted_audio(channel_open: bool, client: NativeClient, picked: int | None) -> dict[str, Any]:
    """SPEC 043, AS POLICY. A VOICE_COMMUNICATION mic and call mode are a phone CALL to Android and
    to a car over Bluetooth HFP, so both are held only while the channel is open. JJ, 2026-10-10:
    "when I turn the orb off, what I want is to close all those channels. So that I can continue
    listening to music or doing other stuff in the car." """
    hold = bool(channel_open)
    return {"mic": hold, "call": hold, "device": choose_route(client.devices, picked) if hold else None}


def audio_directive(want: dict[str, Any]) -> dict[str, Any]:
    return {"type": "native", "op": "audio", **want}


def audio_if_needed(channel_open: bool, client: NativeClient, picked: int | None) -> list[dict]:
    """The `audio` directive when what the app reports differs from what the policy wants, else
    nothing — so a report that only confirms the last directive does not start a loop. A mic that
    failed to open is not retried on every report: the app reports again only on a change."""
    if "audio" not in client.ops:
        return []
    want = wanted_audio(channel_open, client, picked)
    have = {"mic": client.mic, "call": client.call, "device": client.device if client.call else None}
    return [] if have == want else [audio_directive(want)]


def routes_payload(client: NativeClient | None, picked: int | None) -> dict[str, Any]:
    """What the page's output caret shows: the outputs the phone has, and the one in use (or that
    would be once the channel opens)."""
    devices = client.devices if client else []
    available = [{"type": t, "label": label} for t, label in ROUTE_LABELS.items() if t in devices]
    current = (client.device if client and client.call and client.device is not None
               else choose_route(devices, picked))
    return {"type": "native_routes", "available": available, "current": current}


def _lane_title(name: str) -> str:
    return name[:1].upper() + name[1:] if name else name


def _notify(client: NativeClient, key: str, style: str, title: str, text: str) -> list[dict]:
    if "notify" not in client.ops:
        return []
    return [{"type": "native", "op": "notify", "key": key, "style": style, "title": title, "text": text}]


def on_broadcast(payload: dict[str, Any], client: NativeClient, *, channel_open: bool,
                 picked: int | None) -> list[dict]:
    """The directives one driven app gets alongside a broadcast, sent BEFORE the payload itself.
    Every notification rule the app used to hold is here."""
    kind = payload.get("type")
    if kind == "channel":
        out = []
        if "awake" in client.ops:
            out.append({"type": "native", "op": "awake", "on": bool(channel_open)})
        if "audio" in client.ops:
            # Always sent on a channel change, not only on a difference: the app may not have
            # reported yet since its last change, and the directive is idempotent.
            out.append(audio_directive(wanted_audio(channel_open, client, picked)))
        return out
    if kind == "turn":
        # SPEC 040 (JJ: "whenever you do recognize me when I say something, I would like a
        # notification with what has been transcribed from me"). `turn` is only ever a FINAL turn.
        text = str(payload.get("text") or "")
        if payload.get("addressed") and text.strip() and not client.foreground:
            lane = _lane_title(str(payload.get("lane") or ""))
            return _notify(client, "heard", "quiet", f"Heard you → {lane}" if lane else "Heard you", text)
        return []
    if kind == "audio_header":
        # A spoken clip while he is in another app: who said it, and what (a cue is punctuation).
        text = str(payload.get("text") or "")
        if client.foreground or payload.get("cue") or not text:
            return []
        who = _lane_title(str(payload.get("lane") or payload.get("agent") or "Agent"))
        return _notify(client, "speak", "alert", who, text)
    if kind == "lane_waiting":
        # A heads-up only when a lane's hand count goes UP (spec 032 review NICE 6).
        lane = str(payload.get("lane") or "an agent")
        now = int(payload.get("waiting") or 0)
        before = client.hands.get(lane, 0)
        client.hands[lane] = now
        if now > before and not client.foreground:
            return _notify(client, "speak", "alert", f"{_lane_title(lane)} raised a hand",
                           "Switch to that lane to hear it.")
        return []
    return []


# --- his output pick, kept across restarts -----------------------------------------------------
def pick_path(session_dir: str) -> str:
    return os.path.join(session_dir, "native.json")


def load_pick(session_dir: str) -> int | None:
    try:
        with open(pick_path(session_dir), encoding="utf-8") as f:
            v = json.load(f).get("route")
        return int(v) if isinstance(v, (int, float)) else None
    except (OSError, ValueError, AttributeError):
        return None


def save_pick(session_dir: str, route: int | None) -> None:
    os.makedirs(session_dir, exist_ok=True)
    with open(pick_path(session_dir), "w", encoding="utf-8") as f:
        json.dump({"route": route}, f)
