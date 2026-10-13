"""The echo guard: drop a transcript that is an agent's own clip coming back through his microphone.

JJ, 2026-09-30, on a desk mic with a loud external speaker: *"sometimes when you
speak it bleeds through my microphone"* · *"when I increase it, it definitely picks you up."* The
logs showed it: at 12:33 two of dexter's own sentences were logged as his turns. Chrome's software
echo canceller loses to the speaker at high volume, and current Chrome offers no OS canceller to
switch to (`?ec=system` came back `echoCancellationType: None`).

**The one advantage the tool has over any canceller: it knows exactly what it just said.** So the
guard compares the transcript's WORDS against the clips played in the last few seconds, and an
utterance whose words are almost all found, IN ORDER, inside one of them is an echo — even when the
mic caught only a fragment (his question, 2026-09-30: *"what if the mic doesn't pick up your full
text?"*). Subsequence, not equality, is what answers that.

⚠ **Behind a toggle** (`COMMAND_BRIDGE_ECHO_GUARD`, off by default) at his direction: *"let's build
it inside of a feature toggle so that we can properly test this."* The cost it risks is him quoting an
agent's words back within seconds of hearing them; `MIN_WORDS` keeps short replies ("yes, do it")
out of reach entirely.
"""

from __future__ import annotations

import re

MIN_WORDS = 3
"""Utterances shorter than this are never judged echo. "Yes", "go ahead", "do it" appear inside
almost any clip, and dropping his short answers is far worse than letting a short echo through."""

MATCH_RATIO = 0.75
"""The share of the heard words that must appear, in order, in one clip. ASR mangles a word or two
of a muffled echo, so demanding every word would miss the case this exists for."""

WINDOW_S = 10.0
"""How long after a clip finished playing its echo can still be arriving: the utterance may run on
past the clip, then transcription takes a few seconds more."""

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    """Lowercased word tokens, apostrophes dropped so "one's" and "ones" compare equal."""
    return _WORD.findall((text or "").lower().replace("'", "").replace("’", ""))


def _lcs(a: list[str], b: list[str]) -> int:
    """Length of the longest common subsequence of two word lists (in-order, gaps allowed)."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def match(heard: str, spoken: list[str]) -> str | None:
    """The clip text `heard` is an echo of, or None. Pure, so the rule is asserted without audio."""
    h = words(heard)
    if len(h) < MIN_WORDS:
        return None
    for clip in spoken:
        c = words(clip)
        if c and _lcs(h, c) / len(h) >= MATCH_RATIO:
            return clip
    return None


# CUE SOUNDS are handled on the PAGE, not here: it ships silence while a cue plays (shell.html CUE
# GATE). A text rule for them (`cue_blip`: drop a ≤2-word turn overlapping a cue) lived here for an
# hour on 2026-09-30 and was removed after it dropped his real "ship it" — a short command and a cue
# blip look identical to a word count.


def candidates(recent: list[dict], now: float, window_s: float = WINDOW_S,
               max_age_s: float = 180.0) -> list[str]:
    """The clip texts an utterance ending `now` could be echoing: sent recently, and either still
    playing (no receipt yet) or finished within `window_s`."""
    out = []
    for r in recent:
        if now - float(r.get("sent_at") or 0) > max_age_s:
            continue
        played = r.get("played_at")
        if played is None or now - float(played) <= window_s:
            out.append(str(r.get("text") or ""))
    return out
