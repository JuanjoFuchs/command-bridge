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

    def _find_phrase(self, normalized: str) -> Optional[str]:
        for p in self.phrases:
            if normalized == p or normalized.startswith(p + " "):
                return p
            if (" " + p + " ") in (" " + normalized + " "):
                return p
        return None

    def evaluate(self, text: str, now: float) -> Tuple[bool, str]:
        """Return `(addressed, text_for_the_agent)`.

        When the wake phrase is present it is stripped, because the agent should receive the
        request ("what is the status") and not the summons ("hey claude what is the status").
        """
        if not self.enabled:
            self._last_addressed_at = now
            return True, text.strip()

        normalized = _norm(text)
        if not normalized:
            return False, text.strip()

        phrase = self._find_phrase(normalized)
        if phrase is not None:
            self._last_addressed_at = now
            return True, _strip_phrase(text, phrase)

        # No phrase — still addressed if we are inside the conversation window (AC-6).
        if (
            self._last_addressed_at is not None
            and (now - self._last_addressed_at) <= self.window_s
        ):
            self._last_addressed_at = now  # each exchange extends the window
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
