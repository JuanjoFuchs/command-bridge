"""voice_tunnel.security — who may reach the socket, and who may open a session.

Read ai-docs/reference/security.md before changing anything here. The short version:

* The allowlist decides on the **direct TCP peer**. ``X-Forwarded-For`` is honored only when
  the peer is itself a configured trusted proxy, and the trusted-proxy list is empty by
  default. This is VoiceMode's post-CVE rule (GHSA-2qvv-vjq9-g5r4, CVSS 8.6), where the naive
  version let a spoofed header reach microphone recording.
* Auth is checked on the **WebSocket handshake**, not in HTTP middleware. ASGI middleware
  conventionally passes non-HTTP scopes through untouched, which would leave the audio channel
  unauthenticated while looking protected.

STDLIB ONLY — pure, and unit-tested including the spoof case.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
from collections.abc import Iterable, Sequence

from . import config


def generate_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def ip_in_cidrs(ip_str: str, cidrs: Iterable[str]) -> bool:
    """True if `ip_str` falls in any CIDR. Malformed input is False, never an exception —
    an unparseable address must fail closed, not crash the server."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def allowed_cidrs() -> tuple[str, ...]:
    """Loopback always; Tailscale and anything else strictly opt-in via VOICE_TUNNEL_ALLOW_CIDRS."""
    return tuple(config.LOOPBACK_CIDRS) + tuple(config.extra_allow_cidrs())


def client_ip(
    peer_ip: str,
    forwarded_for: str | None = None,
    trusted_proxies: Sequence[str] | None = None,
) -> str:
    """Resolve the real client IP.

    ``X-Forwarded-For`` is attacker-controllable, so it is consulted **only** when the direct
    peer is inside `trusted_proxies`. With the default empty list the header is ignored and the
    peer address is returned — which is what makes a spoofed header useless (AC-8).

    When the peer *is* trusted, walk the chain right-to-left skipping trusted hops; the first
    untrusted address is the real client. The leftmost entries are the ones an attacker writes.
    """
    proxies = tuple(trusted_proxies if trusted_proxies is not None else config.trusted_proxies())
    if not proxies or not ip_in_cidrs(peer_ip, proxies):
        return peer_ip
    if not forwarded_for:
        return peer_ip
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):
        if not ip_in_cidrs(hop, proxies):
            return hop
    return peer_ip


def peer_allowed(
    peer_ip: str,
    forwarded_for: str | None = None,
    trusted_proxies: Sequence[str] | None = None,
    cidrs: Sequence[str] | None = None,
) -> bool:
    resolved = client_ip(peer_ip, forwarded_for, trusted_proxies)
    return ip_in_cidrs(resolved, cidrs if cidrs is not None else allowed_cidrs())


def token_ok(provided: str | None, expected: str | None) -> bool:
    """Constant-time token check. A plain `!=` on a secret is timing-attack shaped (AC-9).

    An unset expected token means auth is disabled — only legitimate for loopback dev, and the
    server refuses to bind a non-loopback interface in that state.
    """
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


class Gate:
    """Bundles the two checks so a connection handler cannot accidentally do only one."""

    def __init__(self, token: str | None, cidrs: Sequence[str] | None = None) -> None:
        self.token = token
        self.cidrs = tuple(cidrs) if cidrs is not None else allowed_cidrs()

    def check(
        self,
        peer_ip: str,
        provided_token: str | None,
        forwarded_for: str | None = None,
    ) -> tuple[bool, str]:
        """Return `(ok, reason)`. Reason is safe to log and safe to send to the client —
        it never echoes the expected token."""
        if not peer_allowed(peer_ip, forwarded_for, cidrs=self.cidrs):
            return False, f"peer {peer_ip} not in allowlist"
        if not token_ok(provided_token, self.token):
            return False, "invalid or missing token"
        return True, "ok"
