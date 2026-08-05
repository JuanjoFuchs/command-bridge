"""voice_tunnel.wake — the wake-phrase gate.

Decides one narrow thing: **was this turn directed at the agent?** It never decides what to do
about it — that is the agent's job (AGENTS.md rule 1).

Gating happens on the transcript rather than acoustically. The ASR already runs on every
buffer, so a text match costs nothing extra, needs no model download, and no per-phrase
training. The tradeoff is that we cannot save power by skipping transcription — which does not
matter here, because the machine transcribing is a desktop, not the phone.

Deliberately NOT addressivity: we do not infer from context whether you were talking to the
agent. You say the phrase, or you are inside the conversation window. See the project note —
addressivity was the one real research risk and it is out of scope.

STDLIB ONLY — pure logic, unit-testable with no audio.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Optional, Tuple

from . import config


GREETINGS = config.GREETINGS
"""Openers that mark an utterance as a summons even when the name that follows was garbled.

Lives in config alongside the reason a greeting is mandatory — see `config.GREETINGS`. Aliased
here because this module is where it is read, and one list has to build the phrases and match
them or the two drift apart."""

def _name() -> str:
    """The assistant's name, read live rather than frozen at import.

    A module-level constant would have baked "claude" into every fuzzy comparison, so renaming
    would have silently kept matching the old name. Read through config so `VOICE_TUNNEL_WAKE_NAME` is real.
    """
    return config.wake_name()

_RATIO_AFTER_GREETING = 0.55
"""How close a token has to be to the name — and the ONLY threshold, because a greeting always
precedes it.

Loose on purpose: "hey ___" already establishes that someone is being addressed, so the only
question left is whether the garbled token is the name. Accepts clod / cloud / claud, which is
what makes the gate survive an ASR that renders "claude" as grab, grub, God, Joe or Crawley.

There used to be a second, stricter threshold (0.80) for a bare token standing alone
mid-sentence, where "the cloud is over there" and "cloud9 is down" would otherwise wake the
agent constantly. **Requiring a greeting deleted that path, and the threshold with it.** Context
is what buys leniency; now there is always context."""


def _norm(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — so 'Hey, Claude!' matches."""
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _is_nameish(word: str, threshold: float = _RATIO_AFTER_GREETING) -> bool:
    """True if `word` is plausibly a mangled form of the assistant's name."""
    if not word:
        return False
    name = _name()
    if word == name:
        return True
    return SequenceMatcher(None, word, name).ratio() >= threshold


class WakeGate:
    """Tracks whether we are mid-conversation, so a back-and-forth doesn't require the wake
    phrase on every single turn.

    `enabled=False` makes every turn addressed — the push-to-talk mode, where the operator has
    already signalled intent by holding a button.
    """

    def __init__(
        self,
        phrases: Optional[Iterable[str]] = None,
        window_s: float = config.CONVERSATION_WINDOW_S,
        enabled: bool = True,
    ) -> None:
        # Longest first so "hey claude" wins over the bare "claude" and strips fully.
        self.phrases = sorted(
            (_norm(p) for p in (phrases if phrases is not None else config.wake_phrases())),
            key=len,
            reverse=True,
        )
        self.window_s = float(window_s)
        self.enabled = bool(enabled)
        self._last_addressed_at: Optional[float] = None

    def reset(self) -> None:
        self._last_addressed_at = None

    def set_phrases(self, phrases: Iterable[str]) -> None:
        """Swap the accepted summons without ending the conversation in progress.

        Renaming mid-session is a real case — the agent that started the tunnel hands off, or the
        user finds that their ASR mangles the name they chose. Rebuilding the whole gate would
        drop `_last_addressed_at`, so the very next sentence would come back unaddressed and they
        would have to say the (new) phrase to resume something they never stopped doing.
        """
        self.phrases = sorted((_norm(p) for p in phrases), key=len, reverse=True)

    def _find_phrase(self, normalized: str) -> Tuple[Optional[str], bool]:
        """Return `(phrase, at_start)`. Detection is generous; stripping is not.

        A phrase anywhere wakes the agent, but only a phrase at the START is a summons we may
        safely remove. Mid-sentence, the speaker is usually *referring* to the phrase rather
        than using it — JJ, live, 2026-07-29: "and do I need to say hey Claude every time?"
        became "and do I need to say every time?", which changes what he asked.

        The asymmetry is deliberate: leaving a stray wake phrase in the text costs the agent
        nothing, while removing one the speaker meant to keep corrupts the request. When in
        doubt, do not edit the user's words.
        """
        # 1. An exact configured phrase, leading — the clean case, safe to strip.
        for p in self.phrases:
            if normalized == p or normalized.startswith(p + " "):
                return p, True

        words = normalized.split()

        # 2. A greeting followed by something close enough to "claude" that ASR probably
        #    mangled the name. Still safe to strip: the intent is unambiguous.
        if (
            len(words) >= 2
            and words[0] in GREETINGS
            and _is_nameish(words[1], _RATIO_AFTER_GREETING)
        ):
            return " ".join(words[:2]), True

        # 3. A greeting opening the utterance, whatever follows. WAKE but DO NOT STRIP.
        #    Parakeet turned "hey Claude" into "hey grab" and "hey grub", which no fuzzy match
        #    on the name can recover — the sound simply isn't there any more. In a session the
        #    user deliberately opened and tapped into, an utterance that opens with a greeting
        #    is addressed to the only other participant. Waking wrongly costs a wasted read;
        #    staying silent costs him talking to nobody, which is far worse.
        if len(words) >= 2 and words[0] in GREETINGS:
            return words[0], False

        # 4. A full greeting-plus-name anywhere else — wake, but never edit their words.
        for p in self.phrases:
            if (" " + p + " ") in (" " + normalized + " "):
                return p, False

        # There is deliberately no rule for the BARE name. It used to be rule 5, gated on
        # `config.wake_allows_bare()`, and it is the reason "let's ship it Thursday" kept waking
        # the agent after the bare form had already been removed from `wake_phrases()` — the
        # phrase list and the matcher were two separate paths and only one of them was fixed.
        # Now the greeting is mandatory (see config.GREETINGS), so a name alone is just a word.
        return None, False

    def evaluate(self, text: str, now: float, ended: Optional[float] = None) -> Tuple[bool, str]:
        """Return `(addressed, text_for_the_agent)`.

        A **leading** wake phrase is stripped, because the agent should receive the request
        ("what is the status") and not the summons ("hey claude what is the status"). A
        mid-sentence mention still wakes but is left intact — see :meth:`_find_phrase`.

        `now` is when this utterance STARTED and `ended` when it finished, both on one monotonic
        clock. The distinction is load-bearing: measuring the conversation window to the *end*
        of an utterance means a long monologue looks like a long silence, and the speaker gets
        dropped out of the conversation for talking too much. Reported live by JJ, 2026-07-29:
        a ~60 s continuous turn came back `addressed: false` while he was still mid-flow.
        The gap that matters is silence between turns — previous end to this start.
        """
        ended = now if ended is None else ended
        if not self.enabled:
            self._last_addressed_at = ended
            return True, text.strip()

        normalized = _norm(text)
        if not normalized:
            return False, text.strip()

        phrase, at_start = self._find_phrase(normalized)
        if phrase is not None:
            self._last_addressed_at = ended
            # Strip only a leading summons; leave a mid-sentence mention intact.
            return True, (_strip_phrase(text, phrase) if at_start else text.strip())

        # No phrase — still addressed if this utterance BEGAN within the window of the last
        # one ending. Comparing starts-to-ends is what stops a long turn from timing itself out.
        if (
            self._last_addressed_at is not None
            and (now - self._last_addressed_at) <= self.window_s
        ):
            self._last_addressed_at = ended  # each exchange extends the window
            return True, text.strip()

        return False, text.strip()


def _strip_phrase(original: str, normalized_phrase: str) -> str:
    """Remove the wake phrase from the ORIGINAL text, preserving its casing and punctuation
    elsewhere. Falls back to the original string if the phrase can't be located verbatim —
    better to hand the agent a slightly noisy request than to mangle it."""
    words = normalized_phrase.split()
    pattern = r"[^\w]*".join(re.escape(w) for w in words)
    out = re.sub(
        r"^\s*[^\w]*" + pattern + r"[^\w]*\s*",
        "",
        original,
        count=1,
        flags=re.IGNORECASE,
    )
    if out != original:
        return out.strip()
    out = re.sub(pattern, " ", original, count=1, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip() or original.strip()
