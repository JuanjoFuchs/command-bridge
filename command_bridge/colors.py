"""Lane colours that belong to the agent, not to its position (spec 029 FR5-FR7).

A roster agent's colour is the roster's (`agents.<name>.hue`). Any other lane gets a hue ASSIGNED
once, by name, from Command Bridge's own wide palette — the candidate farthest from every roster and
previously assigned hue — and keeps it across restarts and re-adds. JJ, 2026-10-08: *"unique to each
agent … even though I know we might be constrained on colours in Claude Code, but that should not
constrain Command Bridge."*

Pure functions over hex strings; no I/O and no model (NFR1). The server persists `assigned`.
"""
from __future__ import annotations

import colorsys
import itertools
from collections.abc import Iterable


def _palette() -> list[str]:
    """Command Bridge's candidate hues: 36 hues round the wheel at two lightness levels, all
    saturated enough to read as an identity on the dark page. Generated, not hand-picked, so the
    palette is as wide as the wheel rather than the six hues the positional fallback had."""
    out: list[str] = []
    for light in (0.55, 0.68):
        for i in range(36):
            r, g, b = colorsys.hls_to_rgb(i / 36, light, 0.62)
            out.append("#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255)))
    return out


PALETTE = _palette()


def _lab(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    r, g, b = lin
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    """CIE76 distance between two hex colours. Raises ValueError on a non-hex string."""
    la, lb = _lab(a), _lab(b)
    return sum((p - q) ** 2 for p, q in zip(la, lb, strict=True)) ** 0.5


def _is_hex(value: str) -> bool:
    try:
        _lab(value)
        return True
    except (ValueError, IndexError):
        return False


def resolve(lanes: Iterable[str], roster: dict[str, str],
            assigned: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Every lane's colour, and the (possibly grown) assigned map to persist.

    Roster agents keep the roster hue (KD3). A lane already assigned keeps its hue, so adding a lane
    never repaints another. A new unrostered lane takes the palette entry whose nearest neighbour
    among ALL roster and assigned hues — up or not — is farthest away."""
    assigned = dict(assigned)
    colours: dict[str, str] = {}
    for name in lanes:
        if roster.get(name):
            colours[name] = roster[name]
            continue
        if name in assigned and _is_hex(assigned[name]):
            colours[name] = assigned[name]
            continue
        taken = [c for c in list(roster.values()) + list(assigned.values()) if _is_hex(c)]
        best = max(PALETTE, key=lambda c: min((delta_e(c, t) for t in taken), default=1e9))
        assigned[name] = best
        colours[name] = best
    return colours, assigned


def clashes(colours: dict[str, str], min_de: float) -> list[dict]:
    """Pairs of lanes whose colours are closer than `min_de`, closest first (FR7). Reported, never
    repainted: a roster clash is fixed in the roster."""
    out = []
    for (a, ca), (b, cb) in itertools.combinations(colours.items(), 2):
        if not (_is_hex(ca) and _is_hex(cb)):
            continue
        d = delta_e(ca, cb)
        if d < min_de:
            out.append({"lanes": [a, b], "colors": [ca, cb], "delta_e": round(d, 1)})
    return sorted(out, key=lambda p: p["delta_e"])
