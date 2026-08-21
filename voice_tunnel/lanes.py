"""voice_tunnel.lanes — WHICH agent a turn is for.

Several agents share one microphone and one transcript. Exactly one of them is **live** at a
time, and the wake name is a switch thrown once and then remembered. So this module answers one
narrow question — *does this utterance move the conversation to a different agent?* — and it
answers it with a **lookup**, never an inference.

That distinction is the whole reason spec 012 is allowed to exist. Working out who a turn is for
from its content is addressivity, which is this project's one written anti-goal. Storing which
agent he last named is not.

**THE ONE RULE: only an EXACT lane name switches a lane.** Everything weaker resolves to the lane
already live, which is free to be wrong because it changes nothing. Measured over every turn ever
logged here (2,460 turns, 2026-07-29 to 08-21) — see spec 012 for the tables:

* The recognizer renders the name exactly **56%** of the time since 2026-08-07, and **0%** before
  it, which is what makes an exact-match rule viable at all rather than merely safe.
* Scoring the token against a lane set is confidently wrong rather than uncertain: `go` scores
  **0.67** against `grok` with no competitor, so a margin rule cannot catch it. **That is why
  fuzzy matching is refused across lanes instead of retuned.**
* The matcher's "a greeting, whatever follows" rule is **51%** of real summons and the token it
  accepts is usually just the next word of a sentence (`i`, `can`, `let`). With one agent that is
  correct. With several it names nobody, and must not be allowed to guess.

STDLIB ONLY — pure logic, unit-testable with no audio, no server and no session directory (NFR1).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

from . import config
from .wake import normalize

BROADCAST = "everyone"
"""The lane that addresses every agent at once (FR5).

**One spelling, everywhere** — in what he says out loud, in `--lane`, and in the turn's `lane`
field. Spec 007 deleted a second name for one concept after it caused an operating guide to
document two instruments with two strategies; this does not add one back. It is reserved: no agent
may register it, because a lane named `everyone` and the broadcast would be indistinguishable at
the exact moment it mattered."""

NAME_RE = re.compile(r"^[a-z][a-z0-9]{1,31}$")
"""A lane name is ONE token: lowercase, alphanumeric, no spaces and no hyphens.

Not style — the matcher compares the single word after the greeting, and `normalize` turns every
non-word character into a space. So a lane called `code-x` becomes the two tokens `code x` and can
never be matched by voice at all. Rejecting it at registration is the difference between a clear
error and a name that silently never works."""

_RATIO = 0.55
"""How close a token has to be to a lane name to count as a mangled form of it.

Deliberately the SAME number the wake gate uses after a greeting (`wake._RATIO_AFTER_GREETING`),
because it answers the same question about the same token. Two thresholds for one question is how
they drift apart. Note what it is used for here: it can only ever cause a REFUSAL or a stay — it
can never cause a switch, so a wrong answer costs a repeat and never an action."""


class LaneError(ValueError):
    """A lane operation that cannot be honoured. Carries the stable `code` an agent branches on
    and the `remedy` that fixes it (AGENTS.md convention 8) — a caller must never have to parse
    this message to know what to do."""

    def __init__(self, message: str, code: str, remedy: str) -> None:
        super().__init__(message)
        self.code = code
        self.remedy = remedy


@dataclass(frozen=True)
class Resolution:
    """What an utterance did to the live lane.

    `action` is one of:

    * `switch` — he named a different agent, exactly. `lane` is the new live lane.
    * `stay`   — nothing moves. `lane` is the lane already live, and the turn belongs to it.
    * `refuse` — a summons that looks like a switch attempt but names no lane exactly. `lane` is
      None and the turn goes to NOBODY.

    **`refuse` is the only outcome that withholds a turn**, and it exists because the two errors
    are not symmetrical: a refusal costs one repeat, while a mis-switch puts an instruction into
    an agent's context where it cannot be recalled and may be acted on.
    """

    action: str
    lane: str | None
    candidates: tuple[str, ...] = ()

    @property
    def reason(self) -> str | None:
        """The `reason` to persist on a refused turn, naming what it was torn between — so the
        refusal is diagnosable from the log alone rather than only from the moment it happened."""
        if self.action != "refuse":
            return None
        return "ambiguous:" + "|".join(self.candidates)


def validate_name(name: str) -> str:
    """Return the normalized lane name, or raise LaneError.

    Rejects rather than sanitizes, for the same reason `store.validate_session` does: a rejected
    name is a bug report, a silently-adjusted one is a name he says and nothing answers to.
    """
    if not isinstance(name, str):
        raise LaneError("lane name must be a string", "lane_exists",
                        "voice-tunnel lane add <one lowercase word>")
    cleaned = name.strip().lower()
    if cleaned == BROADCAST:
        raise LaneError(
            f"'{BROADCAST}' is the broadcast lane and cannot be an agent lane",
            "lane_exists",
            "voice-tunnel lane add <another name>",
        )
    if not NAME_RE.match(cleaned):
        raise LaneError(
            "a lane name must be one lowercase word, 2-32 characters, letters and digits only "
            "-- it is matched as the single token after the greeting, so a space or a hyphen "
            "makes it unsayable",
            "lane_exists",
            "voice-tunnel lane add <one lowercase word>",
        )
    return cleaned


def confusability(name: str, words: Iterable[str]) -> list[str]:
    """Ordinary words this lane name could be mistaken for — the measured cost of choosing it.

    **REPORTED, NEVER REFUSED**, and an earlier draft of spec 012 was wrong about that. It said a
    lane should be refused for colliding with another lane; `claude` and `codex` score 0.55
    against each other, so that rule would have rejected this feature's primary pair, and it
    would have bought nothing — a mis-route is already impossible, and that same pair produced
    ZERO false refusals across 109 real summons. **Proximity to another lane predicts nothing;
    proximity to ordinary speech predicts everything.** Every measured false refusal was `grok`,
    against `go`, `got` and `god`.

    So the number is surfaced and the choice stays his. It is also un-enforceable as a rule: the
    corpus that measures it is his own speech, which a fresh install does not have.
    """
    name = (name or "").strip().lower()
    if not name:
        return []
    return sorted({w for w in words
                   if w != name and SequenceMatcher(None, w, name).ratio() >= _RATIO})


def resolve(text: str, lanes: Iterable[str], current: str) -> Resolution:
    """Decide what `text` does to the live lane. Pure: no I/O, no clock, no server (NFR1).

    `lanes` is the registered agent lanes; the broadcast lane is always addressable and does not
    need to be listed. `current` is the live lane.

    **Only a LEADING greeting can switch.** A greeting buried mid-sentence ("so anyway hey claude")
    is weaker evidence, and under the safety ordering weaker evidence may not cause the one
    irreversible act. It costs about 2% of real summons, measured, and those become a `stay`
    rather than a loss.
    """
    known = tuple(dict.fromkeys(list(lanes) + [BROADCAST]))
    words = normalize(text).split()

    # No leading greeting at all -> this is continuation speech. The conversation window and the
    # voiceprint decide whether it is addressed; the lane is simply the one already live. This is
    # the sticky case, and it is the overwhelming majority of turns.
    if len(words) < 2 or words[0] not in config.GREETINGS:
        return Resolution("stay", current)

    token = words[1]

    # 1. An exact name. The ONLY thing allowed to move the conversation.
    if token in known:
        return Resolution("stay" if token == current else "switch", token)

    # 2. Nothing close to any lane -> it is just the next word of a sentence ("hey, can you..."),
    #    which is 51% of real summons. Stay.
    scored = sorted(
        ((SequenceMatcher(None, token, lane).ratio(), lane) for lane in known),
        key=lambda pair: (-pair[0], pair[1]),
    )
    top, winner = scored[0]
    if top < _RATIO:
        return Resolution("stay", current)

    # 3. A mangled form of the lane ALREADY LIVE changes nothing, so accept it rather than
    #    charging him a repeat for a switch he never asked for. Requires a strict win: a tie is
    #    handled below, because a tie is exactly the case that cannot be resolved by lookup.
    tied = [lane for ratio, lane in scored if ratio == top]
    if winner == current and len(tied) == 1:
        return Resolution("stay", current)

    # 4. It looks like a switch attempt and names no lane exactly. REFUSE — do not guess which
    #    agent he meant. This is TC2, and it is the whole ruling in four lines.
    return Resolution("refuse", None, tuple(lane for ratio, lane in scored if ratio >= _RATIO))


class LaneRegistry:
    """Who is in the meeting, and who is being talked to (FR1).

    Pure state with no I/O, so the routing rules are testable without a server — which is also
    what makes NFR1 true rather than hoped for: a switch is a dictionary lookup on state already
    in memory, so it cannot cost a round trip.

    The **default lane** is the name the server was started with (`serve --wake claude`). It is
    special in exactly one way: it cannot be removed, because it is the lane that unlabelled turns
    already on disk belong to. Every turn logged before this feature existed has no `lane` field,
    and those 1,866 turns must keep reaching the agent that has been reading them.
    """

    def __init__(self, default: str) -> None:
        self.default = validate_name(default)
        self._lanes: list[str] = [self.default]
        self.current: str = self.default

    @property
    def names(self) -> tuple[str, ...]:
        """The registered agent lanes. `everyone` is addressable but never listed here — it is a
        destination, not a participant, and listing it would let it be removed."""
        return tuple(self._lanes)

    def knows(self, name: str) -> bool:
        return name == BROADCAST or name in self._lanes

    def require(self, name: str) -> str:
        """The lane, or a LaneError naming the ones that exist. An agent cannot guess a lane set
        from a bare refusal, so the remedy carries it."""
        if self.knows(name):
            return name
        raise LaneError(
            f"no lane named '{name}'",
            "unknown_lane",
            f"voice-tunnel lane add {name}   # or use one of: "
            + ", ".join(self._lanes + [BROADCAST]),
        )

    def add(self, name: str) -> str:
        name = validate_name(name)
        if name in self._lanes:
            raise LaneError(f"lane '{name}' is already registered", "lane_exists",
                            "voice-tunnel lane list")
        self._lanes.append(name)
        return name

    def remove(self, name: str) -> str:
        """Remove a lane. If it was live, the conversation falls back to the default lane rather
        than to nothing — there is always exactly one live lane (FR1), including right now."""
        if name == self.default:
            raise LaneError(
                f"'{name}' is this server's own lane and cannot be removed",
                "lane_exists",
                "voice-tunnel lane remove <another lane>",
            )
        if name not in self._lanes:
            raise LaneError(f"no lane named '{name}'", "unknown_lane", "voice-tunnel lane list")
        self._lanes.remove(name)
        if self.current == name:
            self.current = self.default
        return name

    def switch(self, name: str) -> str:
        self.current = self.require(name)
        return self.current

    def resolve(self, text: str) -> Resolution:
        return resolve(text, self._lanes, self.current)

    def apply(self, resolution: Resolution) -> bool:
        """Commit a resolution. Returns whether the live lane actually moved, which is what the
        server broadcasts on and what wakes an off-lane agent's `watch`."""
        if resolution.action == "switch" and resolution.lane:
            moved = resolution.lane != self.current
            self.current = resolution.lane
            return moved
        return False
