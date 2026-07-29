"""vm.wake — the wake-phrase gate.

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
from typing import Iterable, Optional, Tuple

from . import config


def _norm(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — so 'Hey, Claude!' matches."""
    t = text.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


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
            (_norm(p) for p in (phrases if phrases is not None else config.WAKE_PHRASES)),
            key=len,
            reverse=True,
        )
        self.window_s = float(window_s)
        self.enabled = bool(enabled)
        self._last_addressed_at: Optional[float] = None

    def reset(self) -> None:
        self._last_addressed_at = None

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
        for p in self.phrases:
            if normalized == p or normalized.startswith(p + " "):
                return p, True
        for p in self.phrases:
            if (" " + p + " ") in (" " + normalized + " "):
                return p, False
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
