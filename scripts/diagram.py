"""The README architecture diagram, as code. Run it to regenerate both themes.

    python scripts/diagram.py        # writes docs/architecture-{light,dark}.svg

WHY THIS IS GENERATED AND NOT DRAWN. Two hand-maintained SVGs — one per theme — drift the
first time a label changes, and the one that drifts is the one you are not looking at,
because you only ever see your own theme. One description, two files, no drift.

WHY IT IS HAND-BUILT SVG AND NOT A DIAGRAM LIBRARY. Evaluated AntV Infographic
(github.com/antvis/Infographic), which renders this class of picture from a short DSL and
even derives a dark palette from the background colour on its own. Its SSR output puts every
label inside <foreignObject>, and a browser does not process foreignObject when an SVG is
loaded through <img> — which is how GitHub renders one. The diagram would arrive with all of
its text missing, and it would look fine locally. Plain <text> is the constraint that decides
this; the ideas below are borrowed from that project anyway.

WHAT IS BORROWED FROM IT:
  - Derive the second theme from one source rather than maintaining two.
  - Check text contrast with a number instead of an eye (their theme generator runs a WCAG
    contrast test to pick text colour). `check()` below fails the build rather than shipping
    a label nobody can read — the same lesson the phone client learned the hard way when a
    label inherited --fg and went dark-on-dark, invisible to everyone whose machine was set
    the other way.
  - A three-colour palette applied by role, and a title/label/caption type scale.

The colours are the phone client's own (`voice_tunnel/web/index.html`): warm is "a human has
the floor", cool is "the machine has it". The README and the thing it documents should not
disagree about what a colour means.
"""
from __future__ import annotations

import pathlib
import sys

# 880 is deliberate. GitHub's README column is ~830-880px and scales an image down to fit, so
# authoring wider than the column shrinks every label by the same ratio — at 920 the 12px
# captions landed near 10.9px. AntV's own layout guidance puts an infographic canvas at
# "around 800px" for the same reason. Author at the size it will be read at.
W, H = 880, 262

# `card` and `bg` differ so the panels read as objects on a ground. `faint` is deliberately
# lighter than `dim` but still has to clear the contrast check below — it carries real words.
THEMES = {
    "light": dict(bg="#ffffff", card="#fbfbfa", edge="#0f172a26", fg="#101519",
                  dim="#55606d", faint="#697078", warm="#8f5006", cool="#2f51ab"),
    "dark": dict(bg="#0d1117", card="#161b22", edge="#ffffff1f", fg="#e7eaf1",
                 dim="#9aa4b5", faint="#8d95a3", warm="#ffb35e", cool="#8fb4ff"),
}

FONT = "system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace"

YOU = (0, 188)          # x, width
TUN = (240, 380)
AGT = (674, 206)
PANEL_Y, PANEL_H = 14, 202
WARM_Y, COOL_Y = 112, 180


def _luminance(hex_color: str) -> float:
    """WCAG relative luminance. Alpha is ignored — only opaque colours are checked."""
    h = hex_color.lstrip("#")[:6]
    out = 0.0
    for weight, i in ((0.2126, 0), (0.7152, 2), (0.0722, 4)):
        c = int(h[i:i + 2], 16) / 255
        out += weight * (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return out


def contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    lo, hi = sorted((a, b))
    return (hi + 0.05) / (lo + 0.05)


def check(theme: str, t: dict) -> list[str]:
    """Every colour that carries words, measured against the surface it sits on.

    4.5:1 is the WCAG AA floor for text at these sizes. This returns problems rather than
    printing them so `main` can fail the whole build on the first one.
    """
    problems = []
    for role in ("fg", "dim", "faint", "warm", "cool"):
        ratio = contrast(t[role], t["card"])
        if ratio < 4.5:
            problems.append(f"{theme}: {role} {t[role]} on card {t['card']} "
                            f"is {ratio:.2f}:1, needs 4.5:1")
    return problems


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fits(s: str, size: float, width: float) -> bool:
    """Rough advance-width estimate, because nothing here can measure a real font.

    0.62em per character over-estimates a proportional face, which is what we want: the
    check should complain early rather than let a label run past its panel on someone
    else's font stack.
    """
    return len(s) * size * 0.62 <= width


def text(x, y, s, t, *, size=14, fill="fg", weight=None, mono=False):
    attrs = (f'x="{x:g}" y="{y:g}" font-family="{MONO if mono else FONT}" '
             f'font-size="{size}" fill="{t[fill]}" text-anchor="middle"')
    if weight:
        attrs += f' font-weight="{weight}"'
    return f"  <text {attrs}>{esc(s)}</text>"


def rail(x1, x2, y, color, marker):
    """A rail runs between two panels and stops short of both, so the arrow has air."""
    return (f'  <path d="M{x1:g},{y} H{x2:g}" stroke="{color}" stroke-width="2" '
            f'fill="none" marker-end="url(#{marker})"/>')


def build(theme: str) -> tuple[str, list[str]]:
    t = THEMES[theme]
    warnings: list[str] = []
    o: list[str] = []

    o.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
        f'height="{H}" role="img" aria-label="One spoken turn. You speak into a phone '
        f'browser; voice-tunnel runs a wake gate and speech recognition and writes a line '
        f'to a log; your agent reads that line, reasons, and calls say; voice-tunnel '
        f'synthesizes the reply and you hear it. Everything runs on your machine.">'
    )
    o.append(f'  <rect width="{W}" height="{H}" fill="{t["bg"]}"/>')

    o.append("  <defs>")
    for name in ("warm", "cool"):
        o.append(f'    <marker id="a-{name}" viewBox="0 0 10 10" refX="9" refY="5" '
                 f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                 f'<path d="M0,1 L9,5 L0,9 z" fill="{t[name]}"/></marker>')
    o.append("  </defs>")

    panels = (
        (YOU, "You", "no app · one tap"),
        (TUN, "voice-tunnel", "holds no model · decides nothing"),
        (AGT, "Your agent", "all the intelligence"),
    )
    for (x, w), *_ in panels:
        o.append(f'  <rect x="{x}" y="{PANEL_Y}" width="{w}" height="{PANEL_H}" rx="14" '
                 f'fill="{t["card"]}" stroke="{t["edge"]}" stroke-width="1"/>')
    for (x, w), label, sub in panels:
        o.append(text(x + w / 2, PANEL_Y + 32, label, t, size=17, weight="600"))
        o.append(text(x + w / 2, PANEL_Y + 52, sub, t, size=13, fill="faint"))
        for s, size in ((label, 17), (sub, 13)):
            if not fits(s, size, w - 16):
                warnings.append(f"{theme}: {s!r} may overflow a {w}px panel")

    gap1 = (YOU[0] + YOU[1] + TUN[0]) / 2
    gap2 = (TUN[0] + TUN[1] + AGT[0]) / 2

    # Warm rail, left to right: he talks, and it becomes a line the agent can read.
    o.append(rail(YOU[0] + YOU[1] - 22, TUN[0] + 22, WARM_Y, t["warm"], "a-warm"))
    o.append(text(gap1, WARM_Y - 12, "your voice", t, size=12, fill="warm"))
    o.append(rail(TUN[0] + TUN[1] - 22, AGT[0] + 22, WARM_Y, t["warm"], "a-warm"))
    o.append(text(gap2, WARM_Y - 12, "a line in the log", t, size=12, fill="warm"))

    # Cool rail, right to left: the agent answers and the tunnel speaks it.
    o.append(rail(AGT[0] + 22, TUN[0] + TUN[1] - 22, COOL_Y, t["cool"], "a-cool"))
    o.append(text(gap2, COOL_Y + 22, 'say "…"', t, size=12, fill="cool"))
    o.append(rail(TUN[0] + 22, YOU[0] + YOU[1] - 22, COOL_Y, t["cool"], "a-cool"))
    o.append(text(gap1, COOL_Y + 22, "spoken reply", t, size=12, fill="cool"))

    o.append(text(YOU[0] + YOU[1] / 2, WARM_Y + 5, "you speak", t, size=14, fill="warm"))
    o.append(text(YOU[0] + YOU[1] / 2, COOL_Y + 5, "you hear", t, size=14, fill="cool"))

    o.append(text(TUN[0] + TUN[1] / 2, WARM_Y + 5, "wake gate  ›  speech recognition", t,
                  size=13, fill="warm"))
    o.append(text(TUN[0] + TUN[1] / 2, COOL_Y + 5, "speech synthesis", t,
                  size=13, fill="cool"))

    o.append(text(AGT[0] + AGT[1] / 2, WARM_Y + 5, "watch --since", t, size=13,
                  fill="warm", mono=True))
    o.append(text(AGT[0] + AGT[1] / 2, (WARM_Y + COOL_Y) / 2 + 5, "reason · run tools", t,
                  size=12, fill="dim"))
    o.append(text(AGT[0] + AGT[1] / 2, COOL_Y + 5, "say", t, size=13, fill="cool",
                  mono=True))

    o.append(text(W / 2, H - 14,
                  "Runs entirely on your machine. No GPU, no account, no speech API.",
                  t, size=12, fill="dim"))
    o.append("</svg>")
    return "\n".join(o) + "\n", warnings


def main() -> int:
    out_dir = pathlib.Path(__file__).resolve().parent.parent / "docs"
    out_dir.mkdir(exist_ok=True)

    problems = [p for theme, t in THEMES.items() for p in check(theme, t)]
    if problems:
        for p in problems:
            print(f"CONTRAST FAIL  {p}", file=sys.stderr)
        return 1

    warnings: list[str] = []
    for theme in THEMES:
        svg, warns = build(theme)
        warnings += warns
        path = out_dir / f"architecture-{theme}.svg"
        path.write_text(svg, encoding="utf-8")
        print(f"wrote {path.relative_to(out_dir.parent)}  {len(svg)} bytes")
    for w in warnings:
        print(f"WARN  {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
