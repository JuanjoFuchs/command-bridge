"""Spec 034: which address is the home Wi-Fi, so `serve --lan` binds that and nothing else.

A typical laptop has several networks at once: Wi-Fi, a docked Ethernet port, a VPN adapter,
Hyper-V and Tailscale virtual adapters. Binding 0.0.0.0 would put the
microphone on every one of them. So the address is FOUND, by a rule narrow enough to name exactly one
adapter: up, physical medium native 802.11, and an RFC 1918 address. Anything else is never chosen,
and no match means no LAN listener rather than a guess.

DUMB like the rest of the tool: one PowerShell query, then a pure function over its rows.
"""
from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
from typing import Any

WIFI_MEDIUM = 9
"""NDIS_PHYSICAL_MEDIUM NativeIEEE802_11, as `Get-NetAdapter` reports `NdisPhysicalMedium`."""

PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
"""RFC 1918. Link-local (169.254/16) and CGNAT (100.64/10, Tailscale) are deliberately absent."""


def is_private_v4(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and any(a in n for n in PRIVATE_NETS)


def choose(adapters: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The Wi-Fi adapter's private IPv4 address, or None. Each row: ip, prefix, medium, status, alias.

    Pure, so the rule is tested against every adapter this PC actually has (spec 034 AC1)."""
    picks = [a for a in adapters
             if str(a.get("status")) == "Up"
             and a.get("medium") == WIFI_MEDIUM
             and is_private_v4(str(a.get("ip") or ""))]
    if not picks:
        return None
    a = sorted(picks, key=lambda r: str(r.get("ip")))[0]
    prefix = int(a.get("prefix") or 24)
    subnet = str(ipaddress.ip_network(f"{a['ip']}/{prefix}", strict=False))
    return {"ip": str(a["ip"]), "prefix": prefix, "subnet": subnet, "alias": a.get("alias")}


_PS = (
    "Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { "
    "$n = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue; "
    "[pscustomobject]@{ ip=$_.IPAddress; prefix=$_.PrefixLength; alias=$_.InterfaceAlias; "
    "medium=[int]$n.NdisPhysicalMedium; status=[string]$n.Status } } | ConvertTo-Json -Compress"
)


def adapters() -> list[dict[str, Any]]:
    """This machine's IPv4 adapters (Windows). Elsewhere, or on any failure, an empty list: the
    caller then finds no Wi-Fi and refuses, which is the safe answer."""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
                             capture_output=True, text=True, timeout=20).stdout
        rows = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return rows if isinstance(rows, list) else [rows]


def detect() -> dict[str, Any] | None:
    return choose(adapters())
