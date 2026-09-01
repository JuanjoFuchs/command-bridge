"""command_bridge.speech — the string an agent WROTE against the string a listener NEEDS.

Two different jobs live here, both of them string work sitting between the caller's text and
whichever engine answers, and neither of them a property of the voice model:

  * :func:`normalize_for_speech` — say technical terms as terms (spec 008, FR2).
  * :func:`segments` — break a reply where its MEANING breaks, not only where a full stop
    lands (spec 008, FR3).

**The defect this module exists for, measured rather than assumed (2026-08-19).** The dot in a
technical term is not turned into a pause. It is **DROPPED**, and the digits either side run
together into a different number:

    synthesized `0.2.6`   -> the recognizer hears `026`
    synthesized `1.0.0`   -> the recognizer hears `100`  ("one hundred")
    synthesized `config.py` -> `Config Py`

There is no silence to remove — a real sentence boundary measured 990 ms and a dotted term
measured **none at all**. That makes this worse than a pacing problem: a pause is a term arriving
awkwardly, an elision is a term arriving **wrong**, with nothing in the sound to say anything was
lost. So the fix is to make the separator AUDIBLE, which is the opposite of what the original
report's diagnosis implied.

**Why words and not digits.** `0 point 2 point 6` would probably read correctly too — but it still
hands three number tokens to the engine's own text frontend, and that frontend is precisely the
black box that is mangling them (TC5 forbids modifying it). The word form hands it no numbers at
all, and it is the form the spec MEASURED round-tripping back to `0.2.6`. Measured evidence beat
a plausible shortcut.

**The danger here is the opposite rule firing, and it is silent (TC2).** A rule that rewrites an
ellipsis, an abbreviation like `e.g.`, or a sentence-final period changes what he hears with
nothing to signal that it did. Every pattern below is therefore written to be NARROW, and every
one of them has a counter-case in `tests/test_speech_normalize.py` proving where it does not fire.
A rule with no counter-case is a rule you cannot tell is over-firing.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- the separators

POINT = " point "
"""How a dotted NUMBER is separated. `0.2.6` is "zero point two point six" and `3.5` is "three
point five" — the same construction, read the same way, which is why they must not diverge."""

DOT = " dot "
"""How a dotted IDENTIFIER, file extension or domain is separated — `config.py` is "config dot
py", because that is how an engineer says it out loud."""


# ------------------------------------------------------------------- numbers, spelled out

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")

_CARDINAL_MAX_DIGITS = 6
"""Above six digits a version segment is not a version segment, and a "one hundred twenty three
million…" reading is more likely to be wrong than the digits were. Longer runs fall back to
digit-by-digit, which is always sayable and never mis-groups."""


def _cardinal(n: int) -> str:
    """`26` -> "twenty six". Only ever called for values inside `_CARDINAL_MAX_DIGITS`."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, rest = divmod(n, 10)
        return _TENS[tens] + (f" {_ONES[rest]}" if rest else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {_cardinal(rest)}" if rest else "")
    thousands, rest = divmod(n, 1000)
    return f"{_cardinal(thousands)} thousand" + (f" {_cardinal(rest)}" if rest else "")


def _digit_by_digit(part: str) -> str:
    """`14` -> "one four". The reading a DECIMAL fraction gets, and the safe fallback everywhere."""
    return " ".join(_ONES[int(c)] for c in part)


def _number_part(part: str, *, spell_digits: bool) -> str:
    """One dot-separated numeric component, as words.

    A leading zero is read digit-by-digit whatever the caller asked for: `1.02.3` read as
    "one point two point three" has silently lost a digit, which is the same class of failure as
    the elision this module exists to fix.
    """
    if spell_digits or len(part) > _CARDINAL_MAX_DIGITS or (len(part) > 1 and part[0] == "0"):
        return _digit_by_digit(part)
    return _cardinal(int(part))


def _say_number(match: re.Match) -> str:
    """`0.2.6` -> "zero point two point six"; `3.14` -> "three point one four".

    THE TWO SHAPES ARE READ DIFFERENTLY BY PEOPLE, so they are read differently here:

    * **three or more parts** is a version, and every part is a whole number —
      `1.10.2` is "one point ten point two", never "one point one zero point two".
    * **two parts** is a decimal, and English reads the fraction digit by digit —
      `3.14` is "three point one four", not "three point fourteen". `3.5` is a single digit, so
      both readings agree and it comes out "three point five" as the spec requires.
    """
    parts = match.group(1).split(".")
    decimal = len(parts) == 2
    said = [
        _number_part(part, spell_digits=decimal and i > 0 and len(part) > 1)
        for i, part in enumerate(parts)
    ]
    # A SPACE WHEN A LETTER FOLLOWS, because the trailing guard deliberately allows one. `1.5x`
    # is a real thing to say and is heard as "fifteen ex" today — but "five" + "x" glued together
    # is "fivex", a word no engine can pronounce, so the seam has to be reopened here.
    end = match.end()
    glued = end < len(match.string) and (match.string[end].isalpha() or match.string[end] == "_")
    return POINT.join(said) + (" " if glued else "")


# -------------------------------------------------------------------------------- the rules

_DOTTED_NUMBER = re.compile(r"(?<![\w.$£€¥])(\d+(?:\.\d+)+)(?!\d)")
"""A version or a decimal: digits, then at least one `.digits` group.

The lookbehind is doing three jobs at once and each one is a counter-case in the tests:

* `\\w` stops it reaching into an identifier — the `1.2` inside `python3.11.2` is not a version;
* `.` stops it re-entering a token this pass already stepped over;
* the currency symbols stop it firing on `$3.50`, which the engine ALREADY reads correctly as
  money. Rewriting that to "$three point five zero" would take a correct reading and break it,
  which is exactly the silent over-normalisation TC2 forbids.

The trailing guard is `(?!\\d)` and not `(?!\\w)` on purpose: `1.5x` should be voiced (it is
currently heard as "fifteen ex"), and a sentence-final period after `0.2.6.` must still be
allowed to follow.
"""

_DOTTED_WORD = re.compile(r"(?<![\w.])([A-Za-z_][\w-]*(?:\.[a-z][a-z0-9_-]+)+)(?!\w)")
"""An identifier, a file extension or a domain: `config.py`, `example.com`,
`command_bridge.config.speech_speed`.

**Every component after a dot must start LOWERCASE and be at least two characters**, and that
one clause is what keeps this rule off the things AC2 forbids:

* `e.g.` / `i.e.` — the tail is a single character, so it never matches. "e dot g" would be
  gibberish where the engine reads the abbreviation correctly today;
* `Ph.D` / `U.S.A` — the tail is uppercase, so it never matches;
* `ready. It is ready now.` — a sentence-final period is followed by WHITESPACE, and this
  pattern allows none, so a real sentence boundary is invisible to it;
* `Wait... really?` — the character after the first dot of an ellipsis is another dot, not a
  letter, so an ellipsis cannot start a match either.

An uppercase tail is rejected rather than accepted because the far more likely reading of
`Done.Next` is a missing space between two sentences, not an identifier.
"""

_LEADING_DOT = re.compile(r"(?<![\w.])\.([a-z][a-z0-9_-]+)(?!\w)")
"""A dotfile or a bare extension with nothing on its left: `.env`, `.gitignore`.

Same shape as `_DOTTED_WORD`'s tail for the same reasons. The lookbehind excludes `.` so the
second and third dots of an ellipsis can never start a match, and excludes `\\w` so `1.` in a
numbered list and the `.` closing a sentence are both out of reach.
"""


def _say_dotted_word(match: re.Match) -> str:
    """`command_bridge.config.speech_speed` -> "command bridge dot config dot speech speed".

    The underscore goes too, and only inside a token this rule already matched. An underscore is
    not a sound — it is either read out as the word "underscore" or dropped, and neither is what
    an engineer says. Scoping it to the matched token is what keeps it from touching a bare
    `speech_speed` in prose, which is a counter-case in the tests.
    """
    return match.group(1).replace(".", DOT).replace("_", " ")


def normalize_for_speech(text: str) -> str:
    """The string the ENGINE should be handed, given the string the agent WROTE.

    Pure, deterministic and engine-independent (FR1, FR5) — both installed engines were measured
    dropping the dot, so this is not a workaround for one of them. It is called from exactly two
    places: `tts.synthesize`, above the backend dispatch, and `command-bridge pronounce`, which is
    how you ask what the engine is about to be handed WITHOUT starting a server. They call the
    same function so the inspector cannot drift away from the thing it inspects (FR4a).

    Order matters and is narrow-to-broad: the numeric rule runs first so `0.2.6` is claimed as a
    version before the identifier rule could see `2.6` as anything, and the replacements it emits
    contain no dots, so no later rule can fire on this pass's own output.
    """
    if not text:
        return text
    out = _DOTTED_NUMBER.sub(_say_number, text)
    out = _DOTTED_WORD.sub(_say_dotted_word, out)
    out = _LEADING_DOT.sub(lambda m: DOT.lstrip() + m.group(1), out)
    return out


# ------------------------------------------------------------------------ phrase-level pacing

CLAUSE_PAUSE_RATIO = 0.35
"""How long a clause break is, as a FRACTION of the sentence pause the owner has tuned.

Measured 2026-08-19: a sentence boundary yields 990 ms of silence and a comma yields **0 ms** —
the reply only breathes where a full stop happens to fall. FR3 asks for a break that is
measurably present AND measurably shorter, which is the pair of arms that separates "pacing"
from simply "slower".

A ratio rather than a number of seconds, deliberately, and this is the reason `rate --pause`
keeps working the way its own note promises ("raise --pause when reading a list aloud"): one
knob still moves the whole prosody, and a clause break can never overtake the sentence break it
is supposed to sit inside. 0.35 puts it at ~300 ms against his 0.85 s sentence pause, which is
where an unhurried speaker's comma actually lands and is far above the 60 ms floor the silence
measurement can resolve.

**Not a setting.** It introduces no `COMMAND_BRIDGE_*` variable to register, because spec 008
closes a gap rather than widening the surface — and the thing worth tuning by ear, the sentence
pause, is already tunable.
"""

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
"""Where a full stop ends a thought. Unchanged from what the kokoro backend already did, so
comma-free text segments EXACTLY as it did before — which is what makes AC10 (a clip with no
comma and no sentence break gains no new silence) true by construction rather than by luck."""

_CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+")
"""Where a thought turns a corner without ending. The punctuation is KEPT on the piece before the
break, because the engine's own intonation depends on it: a fragment ending in a comma is read
with continuation, a fragment ending in nothing is read as a finished sentence, and stripping it
would buy a gap at the price of a falling tone in the middle of every clause."""


def segments(text: str) -> list[tuple[str, float]]:
    """Speakable pieces, each with the pause that FOLLOWS it as a multiple of the sentence pause.

    ``[("Alpha,", 0.35), ("beta.", 1.0), ("Gamma.", 0.0)]`` — the last piece always scores 0.0,
    so a reply can never end on dead air, which reads from the phone as the tunnel having hung.

    Splitting here rather than inside a backend is what makes the pacing engine-independent: both
    resident backends already joined their own pieces with silence, and they now join the same
    pieces the same way. Returns ``[]`` for text with nothing speakable in it, so the caller keeps
    whatever it already does about empty input.
    """
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if not sentences:
        return []
    out: list[tuple[str, float]] = []
    for i, sentence in enumerate(sentences):
        clauses = [c for c in _CLAUSE_SPLIT.split(sentence) if c.strip()]
        for j, clause in enumerate(clauses):
            if j < len(clauses) - 1:
                gap = CLAUSE_PAUSE_RATIO      # a corner inside the sentence
            elif i < len(sentences) - 1:
                gap = 1.0                     # the full stop
            else:
                gap = 0.0                     # the end of the reply
            out.append((clause.strip(), gap))
    return out
