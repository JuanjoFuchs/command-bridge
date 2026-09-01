"""Turn a marked-up sentence into a highlight schedule.

`[point:<selector>]` marks sit inline in the sentence the agent is speaking.
Each one fires when the speech reaches it, so one clip can walk through three
parts of a diagram instead of degenerating into "highlight, talk, highlight".

The whole module is a pure function on purpose: the schedule is the part worth
testing, and it can be tested without a browser, a clip or a clock.

THREE SOURCES OF TIME, AND THEY ARE NOT INTERCHANGEABLE:

    measured   `words` from `voice-tunnel say --timings` -- when each word is
               ACTUALLY spoken, decided by the synthesizer that made the audio.
    estimated  `seconds` alone -- the clip's duration split by character offset.
               Clause-accurate at best.
    immediate  neither -- every mark resolves at once, in order.

`timing` says which one produced the schedule, and that field is not decoration:
this project's standing rule is that an estimate must never be presented in the
same shape as a measurement. A caller that cannot tell them apart will describe
a proportional guess as word-synchronised, which is a precision claim the tool
has not earned.
"""

from __future__ import annotations

import re

MARK = re.compile(r"\[point:([^\]]*)\]")


def parse(text: str) -> tuple[str, list[dict]]:
    """Split marked-up text into the spoken sentence and its marks.

    A mark's position is recorded as the number of WORDS that precede it, which
    is the unit a word schedule is indexed by, and its character offset, which
    is what the estimated path needs. Both are computed against the STRIPPED
    text -- the marks are not spoken, so they cannot occupy time.
    """
    marks: list[dict] = []
    spoken, at = [], 0
    for m in MARK.finditer(text):
        spoken.append(text[at:m.start()])
        prefix = "".join(spoken)
        marks.append({"selector": m.group(1).strip(),
                      "word": len(prefix.split()),
                      "char": len(prefix)})
        at = m.end()
    spoken.append(text[at:])

    # Collapse the whitespace the removed marks left behind, and shift every
    # offset that sat after the gap. Without this a mark between two words
    # lands a character or two late and, worse, the echoed sentence has a
    # double space where the agent never wrote one.
    joined = "".join(spoken)
    clean, prev_end = [], 0
    for m in re.finditer(r"\s{2,}", joined):
        clean.append(joined[prev_end:m.start()])
        clean.append(" ")
        for mk in marks:
            if mk["char"] > m.start():
                mk["char"] -= len(m.group(0)) - 1
        prev_end = m.end()
    clean.append(joined[prev_end:])
    out = "".join(clean).strip()
    lead = len(joined) - len(joined.lstrip())
    for mk in marks:
        mk["char"] = max(0, min(len(out), mk["char"] - lead))
    return out, marks


def schedule(text: str, words: list[dict] | None = None,
             seconds: float | None = None) -> dict:
    """Build the cue schedule. Never raises on a shape it cannot time."""
    spoken, marks = parse(text)
    total = len(spoken) or 1

    if words:
        timing = "measured"
        for mk in marks:
            k = mk["word"]
            if k < len(words):
                mk["at"] = float(words[k].get("t", 0.0))
            else:
                # A mark after the last word fires as the last word begins --
                # there is no later moment to schedule it at, and dropping it
                # would silently lose a highlight the agent asked for.
                mk["at"] = float(words[-1].get("t", 0.0)) if words else 0.0
    elif seconds:
        timing = "estimated"
        for mk in marks:
            mk["at"] = round(float(seconds) * mk["char"] / total, 3)
    else:
        timing = "immediate"
        for mk in marks:
            mk["at"] = 0.0

    for mk in marks:
        mk.pop("char", None)
    return {"text": spoken, "marks": marks, "timing": timing,
            "seconds": float(seconds) if seconds else None}
