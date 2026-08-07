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
from collections.abc import Iterable
from difflib import SequenceMatcher

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
        phrases: Iterable[str] | None = None,
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
        self._last_addressed_at: float | None = None
        self.last_grant: str | None = None
        """HOW the most recent `evaluate` addressed the turn: 'phrase' | 'window' | None.

        Valid only for the call that just returned — it is a detail of the last verdict, not
        state. The caller needs it because the two grants carry different authority: a spoken
        phrase is an unambiguous instruction from a person, while the window is an INFERENCE that
        whoever is talking now is the same person who was talking a moment ago. Only the second
        one is safe for another signal to overrule."""

    def reset(self) -> None:
        self._last_addressed_at = None
        self.last_grant = None

    def set_phrases(self, phrases: Iterable[str]) -> None:
        """Swap the accepted summons without ending the conversation in progress.

        Renaming mid-session is a real case — the agent that started the tunnel hands off, or the
        user finds that their ASR mangles the name they chose. Rebuilding the whole gate would
        drop `_last_addressed_at`, so the very next sentence would come back unaddressed and they
        would have to say the (new) phrase to resume something they never stopped doing.
        """
        self.phrases = sorted((_norm(p) for p in phrases), key=len, reverse=True)

    def _find_phrase(self, normalized: str) -> str | None:
        """Return the wake phrase that woke this utterance, or None.

        Detection only — **the phrase is never removed from the text.** Where a phrase sits in
        the sentence used to decide whether it was safe to strip; now that nothing is stripped,
        position carries no consequence and the distinction is gone.
        """
        # 1. An exact configured phrase, leading — the clean case.
        for p in self.phrases:
            if normalized == p or normalized.startswith(p + " "):
                return p

        words = normalized.split()

        # 2. A greeting followed by something close enough to "claude" that ASR probably
        #    mangled the name. The intent is unambiguous.
        if (
            len(words) >= 2
            and words[0] in GREETINGS
            and _is_nameish(words[1], _RATIO_AFTER_GREETING)
        ):
            return " ".join(words[:2])

        # 3. A greeting opening the utterance, whatever follows.
        #    Parakeet turned "hey Claude" into "hey grab" and "hey grub", which no fuzzy match
        #    on the name can recover — the sound simply isn't there any more. In a session the
        #    user deliberately opened and tapped into, an utterance that opens with a greeting
        #    is addressed to the only other participant. Waking wrongly costs a wasted read;
        #    staying silent costs him talking to nobody, which is far worse.
        if len(words) >= 2 and words[0] in GREETINGS:
            return words[0]

        # 4. A full greeting-plus-name anywhere else.
        for p in self.phrases:
            if (" " + p + " ") in (" " + normalized + " "):
                return p

        # There is deliberately no rule for the BARE name. It used to be rule 5, gated on
        # `config.wake_allows_bare()`, and it is the reason "let's ship it Thursday" kept waking
        # the agent after the bare form had already been removed from `wake_phrases()` — the
        # phrase list and the matcher were two separate paths and only one of them was fixed.
        # Now the greeting is mandatory (see config.GREETINGS), so a name alone is just a word.
        return None

    @property
    def window_anchor(self) -> float | None:
        """When the conversation window last restarted. Read it BEFORE `evaluate` if the caller
        may reject the turn: `evaluate` extends the window as it grants, and a turn that is
        rejected downstream must not leave the window it opened behind."""
        return self._last_addressed_at

    def mark_addressed(self, ended: float | None) -> None:
        """Open or extend the conversation window from OUTSIDE this gate.

        The voiceprint can grant attention to a turn with no wake phrase in it. When it does, the
        conversation is exactly as live as if the phrase had been spoken — but only this class
        holds the window, so without this call a voice-addressed turn started no conversation and
        the NEXT sentence had to qualify entirely on its own.

        **The turns that fell out were the short ones, and that is not a coincidence.** A one- or
        two-word follow-up is too short for the embedder to score confidently, so it cannot pass
        the voice gate either, and it landed as `not-addressed` in the middle of a conversation
        already in progress. Reported live by JJ, 2026-08-07: "I have said a few things to you
        directing myself to you, but I see they're being categorized as [not addressed]" — his
        "But", "Yeah, yeah", "Just one thing" and "That's my taste" all went unheard between
        turns that were heard.

        The rule this restores: **whatever grants attention also extends the window.** A gate
        that can start a conversation but not continue one is not a gate, it is a doorbell.

        Passing None winds the window back — used to undo the extension `evaluate` applied to
        a turn that another gate then rejected.
        """
        self._last_addressed_at = ended

    def evaluate(self, text: str, now: float, ended: float | None = None) -> tuple[bool, str]:
        """Return `(addressed, text_for_the_agent)`.

        **The text is returned exactly as spoken — the wake phrase is never removed.**
        JJ, 2026-08-07: "when I initially said hey clot [Claude], you removed that hay clot
        from the transcription. I think we should stop doing that. It's fine that it's a wake
        word or two words, but we shouldn't remove it."

        *Why this beats the old strip-the-leading-summons rule:* the turn log is a record of
        what a person said, and an agent that can read "hey claude what is the status" can
        ignore two words without help. Editing costs something real — it made the log disagree
        with his memory of the sentence, and the failure mode was silent. The earlier fix
        narrowed stripping to leading phrases only, after "do I need to say hey Claude every
        time?" came back with the phrase deleted; this removes the class of bug rather than
        another instance of it.

        `now` is when this utterance STARTED and `ended` when it finished, both on one monotonic
        clock. The distinction is load-bearing: measuring the conversation window to the *end*
        of an utterance means a long monologue looks like a long silence, and the speaker gets
        dropped out of the conversation for talking too much. Reported live by JJ, 2026-07-29:
        a ~60 s continuous turn came back `addressed: false` while he was still mid-flow.
        The gap that matters is silence between turns — previous end to this start.
        """
        ended = now if ended is None else ended
        self.last_grant = None
        if not self.enabled:
            self._last_addressed_at = ended
            self.last_grant = "phrase"     # push-to-talk: the operator signalled intent by hand
            return True, text.strip()

        normalized = _norm(text)
        if not normalized:
            return False, text.strip()

        if self._find_phrase(normalized) is not None:
            self._last_addressed_at = ended
            self.last_grant = "phrase"
            return True, text.strip()

        # No phrase — still addressed if this utterance BEGAN within the window of the last
        # one ending. Comparing starts-to-ends is what stops a long turn from timing itself out.
        if (
            self._last_addressed_at is not None
            and (now - self._last_addressed_at) <= self.window_s
        ):
            self._last_addressed_at = ended  # each exchange extends the window
            self.last_grant = "window"
            return True, text.strip()

        return False, text.strip()
