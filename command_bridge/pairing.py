"""Spec 031: pair the phone app once, with a one-time code, and know it afterwards by its device key.

DUMB like the rest of the tool: random strings, hashes, a JSON file, a clock. No model, no network.

- A **pairing code** is minted for a page that already holds the token, lives in memory only, expires
  after `PAIR_CODE_TTL_S`, and is consumed by its first successful exchange.
- A **device key** is what the exchange returns. Only its SHA-256 is stored, in the session directory,
  so a copy of the file cannot be replayed as a key. The file is re-read on every check, which is what
  makes `command-bridge devices --revoke` take effect on the very next request with no server call.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from . import config

PAIR_CODE_TTL_S = 600
"""How long a minted code stays valid: long enough to walk from laptop to phone, short enough that a
code left on a screen is soon worthless."""

BAD_CODE_LIMIT = 10
BAD_CODE_WINDOW_S = 600
LOCKOUT_S = 600
"""After BAD_CODE_LIMIT wrong codes inside BAD_CODE_WINDOW_S, exchange refuses everything for LOCKOUT_S.
A 128-bit code cannot be guessed anyway; the lockout stops a client hammering the open endpoint."""

DEVICE_HEADER = "X-CB-Device"
DEVICE_COOKIE = "cb_device"
"""The same key as a cookie, for the app's WebView (spec 032)."""

_lock = threading.Lock()


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def devices_path(session: str) -> str:
    return os.path.join(config.session_dir(), f"{session}.devices.json")


def _load(session: str) -> list[dict[str, Any]]:
    try:
        with open(devices_path(session), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(session: str, devices: list[dict[str, Any]]) -> None:
    path = devices_path(session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(devices, f, indent=1)
    os.replace(tmp, path)


class Pairing:
    """One per server session: the live codes and the bad-code counter (both in memory)."""

    def __init__(self, session: str, clock: Callable[[], float] = time.time) -> None:
        self.session = session
        self.clock = clock
        self.codes: dict[str, float] = {}       # code hash -> expiry
        self.bad: list[float] = []              # times of recent bad codes
        self.locked_until = 0.0

    def mint(self) -> tuple[str, int]:
        code = secrets.token_urlsafe(16)        # 128 bits
        now = self.clock()
        with _lock:
            self.codes = {h: exp for h, exp in self.codes.items() if exp > now}
            self.codes[_hash(code)] = now + PAIR_CODE_TTL_S
        return code, PAIR_CODE_TTL_S

    def exchange(self, code: str, name: str = "") -> tuple[int, dict[str, Any]]:
        """`(status, body)`. 200 with the new key, 403 for a bad code, 429 while locked out."""
        now = self.clock()
        with _lock:
            if now < self.locked_until:
                return 429, {"error": "too many bad pairing codes; pairing is locked for now",
                             "code": "pair_locked",
                             "remedy": f"wait {int(self.locked_until - now)} s, then mint a new code"}
            h = _hash(code or "")
            exp = self.codes.pop(h, None)       # consumed whether or not it is still valid
            if exp is None or exp <= now:
                self.bad = [t for t in self.bad if t > now - BAD_CODE_WINDOW_S] + [now]
                if len(self.bad) >= BAD_CODE_LIMIT:
                    self.locked_until = now + LOCKOUT_S
                    self.bad = []
                return 403, {"error": "pairing code is invalid, used or expired",
                             "code": "pair_code_invalid",
                             "remedy": "tap 'Pair phone app' on the page for a fresh code"}
            key = secrets.token_urlsafe(32)     # 256 bits
            device_id = secrets.token_hex(4)
            devices = _load(self.session)
            devices.append({"id": device_id, "name": (name or "phone")[:60],
                            "key_sha256": _hash(key), "created": int(now), "last_seen": None})
            _save(self.session, devices)
        return 200, {"device_id": device_id, "device_key": key}


def device_ok(session: str, key: str | None) -> bool:
    """Constant-time check of a presented device key against every stored hash."""
    if not key:
        return False
    h = _hash(key)
    ok = False
    for d in _load(session):
        if hmac.compare_digest(str(d.get("key_sha256", "")), h):
            ok = True
    return ok


def list_devices(session: str) -> list[dict[str, Any]]:
    """Paired devices WITHOUT their key hashes (nothing secret-shaped leaves this function)."""
    return [{k: v for k, v in d.items() if k != "key_sha256"} for d in _load(session)]


def revoke(session: str, device_id: str) -> bool:
    with _lock:
        devices = _load(session)
        kept = [d for d in devices if d.get("id") != device_id]
        if len(kept) == len(devices):
            return False
        _save(session, kept)
    return True


def pair_links(base: str | None, code: str, lan: str | None = None) -> dict[str, str]:
    """The custom-scheme link and Chrome's intent:// form of the same link (Chrome on Android opens
    the intent form more reliably than a bare custom scheme). `base` is the https tunnel, `lan` the
    home-Wi-Fi address (spec 034 FR4); either may be absent, and the app needs at least one."""
    from urllib.parse import quote
    parts = []
    if base:
        parts.append(f"base={quote(base, safe='')}")
    if lan:
        parts.append(f"lan={quote(lan, safe='')}")
    parts.append(f"code={quote(code, safe='')}")
    q = "&".join(parts)
    return {
        "link": f"commandbridge://pair?{q}",
        "intent_link": f"intent://pair?{q}#Intent;scheme=commandbridge;"
                       f"package=dev.commandbridge.companion;end",
    }
