"""Slice a markdown document by heading, so a summary is an EXTRACT.

The rule this exists to enforce, stated by JJ on 2026-08-25 while reviewing a
summary an agent had written of its own draft issue:

    "what I get rendered in summary … shouldn't never be your interpretation of
    the thing. It should be an objective representation of it … you can use your
    judgment to determine what to render, but it shouldn't be what you render.
    It shouldn't be an interpretation that you're hard coding into this. It
    should be what you're picking from that source."

So: **selection is judgment, wording is not.** An agent may decide which
sections matter; it may not restate them. Every character rendered by this
module came out of the source file, which is what makes the short version
auditable against the long one instead of asking to be trusted.

Anything left out is reported, so the omission is visible rather than silent —
a summary that hides what it dropped is an interpretation wearing an extract's
clothes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A setext-style underline (=== / ---) also opens a section, but only the ATX
# form is honoured here: agents write `#`, and guessing at underlines would
# start interpreting the document, which is the thing this module refuses to do.
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


@dataclass
class Section:
    level: int
    title: str
    start: int          # line index of the heading
    end: int            # exclusive
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip("\n")


def split(md: str) -> list[Section]:
    """Every ATX-heading section, in document order, with its verbatim body.

    Text before the first heading is returned as a level-0 section titled ''
    so a preamble is addressable rather than silently unreachable.
    """
    lines = md.splitlines()
    marks: list[tuple[int, int, str]] = []
    fenced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            # A `#` inside a fenced block is a comment, not a heading. Getting
            # this wrong silently truncates every extract after the first shell
            # snippet, which is the kind of bug that looks like a content
            # problem for a long time.
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(line)
        if m:
            marks.append((i, len(m.group(1)), m.group(2)))

    out: list[Section] = []
    if not marks or marks[0][0] > 0:
        end = marks[0][0] if marks else len(lines)
        if "".join(lines[:end]).strip():
            out.append(Section(0, "", 0, end, lines[:end]))
    for n, (start, level, title) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(lines)
        out.append(Section(level, title, start, end, lines[start:end]))
    return out


def _matches(section: Section, want: str) -> bool:
    """Case-insensitive, punctuation-tolerant title match.

    Deliberately forgiving on the *reference* and exact on the *content*: an
    agent should not have to reproduce a heading's punctuation to quote it, and
    a near-miss that silently extracts nothing is worse than a loose match.
    """
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    a, b = norm(section.title), norm(want)
    return bool(b) and (a == b or b in a)


def extract(md: str, wants: list[str]) -> tuple[str, list[str], list[str], list[str]]:
    """Return (extracted markdown, included, omitted, missing).

    Sections come back in DOCUMENT order regardless of the order asked for, so
    the extract cannot reorder an argument into saying something the source did
    not. A requested title that matches nothing is reported in `missing` rather
    than dropped, because an extract that quietly skipped a section you asked
    for is exactly the silent interpretation this module exists to prevent.
    """
    sections = split(md)
    if not wants:
        return md, [s.title for s in sections if s.title], [], []

    chosen: list[Section] = []
    missing: list[str] = []
    for want in wants:
        hit = next((s for s in sections if _matches(s, want)), None)
        if hit is None:
            missing.append(want)
        elif hit not in chosen:
            chosen.append(hit)

    chosen.sort(key=lambda s: s.start)
    body = "\n\n".join(s.text for s in chosen)
    included = [s.title for s in chosen]
    omitted = [s.title for s in sections if s.title and s not in chosen]
    return body, included, omitted, missing
