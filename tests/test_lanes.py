"""Spec 012 Slice A — the lane resolver and the registry.

Covers **AC-1** (the TC2 table is a pure total function), **AC-12** (registration refuses what it
must and, critically, does NOT refuse `codex` alongside `claude`) and **AC-13** (NFR1: resolution
performs no I/O, so a switch cannot cost a round trip).
"""
import builtins
import socket

import pytest

from command_bridge import lanes
from command_bridge.lanes import BROADCAST, LaneError, LaneRegistry, resolve

TWO = ["claude", "codex"]


# --------------------------------------------------------------- AC-1: the TC2 table

@pytest.mark.parametrize(
    "text, current, action, lane",
    [
        # Row 1 — an exact name that is not the live one is the ONLY thing that switches.
        ("hey codex run the tests", "claude", "switch", "codex"),
        ("hey claude run the tests", "codex", "switch", "claude"),
        # Row 2 — an exact name that IS the live one changes nothing.
        ("hey claude run the tests", "claude", "stay", "claude"),
        # Row 3 — below threshold against every lane: just the next word of a sentence. This is
        # 51% of his real summons, so getting it wrong would be the loudest possible regression.
        ("hey can you run the tests", "claude", "stay", "claude"),
        ("hey i was thinking", "claude", "stay", "claude"),
        ("hey let us ship it", "codex", "stay", "codex"),
        # Row 4 — a mangled form of the lane already live. Costs him nothing, so accept it.
        ("hey cloud run the tests", "claude", "stay", "claude"),
        ("hey club run the tests", "claude", "stay", "claude"),
        # Row 5 — the same mangled token while a DIFFERENT lane is live is a switch attempt that
        # names nobody exactly. Refuse rather than guess: this is the whole of TC2.
        ("hey cloud run the tests", "codex", "refuse", None),
        # No greeting at all — continuation speech, sticky by definition.
        ("run the tests", "claude", "stay", "claude"),
        ("run the tests", "codex", "stay", "codex"),
        # The broadcast lane is addressable by name and is sticky like any other lane.
        ("hey everyone stand down", "claude", "switch", BROADCAST),
        ("hey everyone stand down", BROADCAST, "stay", BROADCAST),
    ],
)
def test_the_tc2_table_decides_every_documented_case(text, current, action, lane):
    r = resolve(text, TWO, current)
    assert (r.action, r.lane) == (action, lane)


def test_only_a_leading_greeting_can_switch():
    """A greeting buried mid-sentence is weaker evidence, and weaker evidence may not cause the
    one irreversible act. Measured at ~2% of real summons, and they become a stay, not a loss."""
    buried = resolve("so anyway hey claude what is up", TWO, "codex")
    assert (buried.action, buried.lane) == ("stay", "codex")


def test_a_refusal_names_what_it_was_torn_between():
    """The refusal has to be diagnosable from the log alone. A bare 'ambiguous' would leave the
    reader unable to tell a near-miss from a wild one."""
    r = resolve("hey cloud run the tests", TWO, "codex")
    assert r.action == "refuse" and r.lane is None
    assert r.reason.startswith("ambiguous:")
    assert "claude" in r.candidates
    # It is torn between the lanes it scored, and it says so rather than picking the best.
    assert len(r.candidates) >= 1


def test_a_resolution_that_is_not_a_refusal_persists_no_reason():
    assert resolve("hey codex go", TWO, "claude").reason is None
    assert resolve("carry on", TWO, "claude").reason is None


@pytest.mark.parametrize("text", ["", "   ", "hey", "hey ", "!!!", "hey ...", "hey été",
                                  "hey 你好", "你好 hey claude"])
def test_the_resolver_is_total_and_never_raises(text):
    """A wake gate that can throw is a wake gate that can drop a turn. Empty, punctuation-only and
    non-ASCII input all have to produce a verdict rather than an exception."""
    r = resolve(text, TWO, "claude")
    assert r.action in {"switch", "stay", "refuse"}


def test_a_single_lane_session_can_never_refuse_and_can_never_switch():
    """NFR3 as a property rather than a hope: with one lane, rows 1 and 5 of the table cannot
    fire, so the whole feature collapses to today's behaviour. Proven here over the tokens that
    actually appear in his speech, and again over the full corpus in test_lane_corpus.py."""
    for token in ["can", "i", "let", "so", "cloud", "club", "go", "got", "sorry", "again",
                  "claude", "grab", "grub", "god", "joe", "clock"]:
        r = resolve(f"hey {token} run the tests", ["claude"], "claude")
        assert r.action == "stay", f"{token!r} must not move a single-lane session"
        assert r.lane == "claude"


# --------------------------------------------------- AC-13 / NFR1: resolution touches nothing

def test_resolution_performs_no_io(monkeypatch):
    """NFR1. A lane switch happens on state already in memory. Poison every door out of the
    process and resolve anyway — if a switch ever needs a file or a socket, this fails loudly
    rather than showing up as a pause he can feel on every subject change."""
    def forbidden(*_a, **_k):
        raise AssertionError("lane resolution must not perform I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)

    reg = LaneRegistry("claude")
    reg.add("codex")
    assert reg.resolve("hey codex run the tests").action == "switch"
    assert resolve("hey can you", ["claude", "codex"], "claude").action == "stay"


# ------------------------------------------------------------ AC-12: registration

def test_codex_registers_alongside_claude():
    """**The specific regression this test exists to prevent.** An earlier draft of spec 012 said
    `lane add` should refuse a name that collides with an existing lane. `claude` and `codex`
    score 0.55 against each other — exactly that draft's threshold — so the rule would have
    rejected this feature's primary use case at registration, and it would have bought nothing:
    a mis-route is impossible by construction, and this pair produced zero false refusals across
    109 real summons."""
    reg = LaneRegistry("claude")
    assert reg.add("codex") == "codex"
    assert reg.names == ("claude", "codex")


def test_the_broadcast_name_cannot_be_registered_as_an_agent():
    reg = LaneRegistry("claude")
    with pytest.raises(LaneError) as exc:
        reg.add(BROADCAST)
    assert exc.value.code == "lane_exists"


def test_a_duplicate_lane_is_refused():
    reg = LaneRegistry("claude")
    reg.add("codex")
    with pytest.raises(LaneError) as exc:
        reg.add("codex")
    assert exc.value.code == "lane_exists"


@pytest.mark.parametrize("bad", ["code x", "code-x", "CODEX!", "", "x", "a" * 33, "9lives",
                                 "co_dex", "hey codex"])
def test_a_name_that_is_not_one_token_is_refused(bad):
    """A lane name is matched as the single word after the greeting, and normalization splits on
    every non-word character. So `code-x` is two tokens and can never be said at all — refusing it
    at registration is the difference between an error and a name that silently never works."""
    reg = LaneRegistry("claude")
    with pytest.raises(LaneError):
        reg.add(bad)


def test_every_lane_error_carries_a_code_and_a_remedy():
    """AGENTS.md convention 8. An agent cannot infer a fix from a message, so a refusal that does
    not carry its remedy makes it guess."""
    reg = LaneRegistry("claude")
    for call in (lambda: reg.add(BROADCAST),
                 lambda: reg.add("code-x"),
                 lambda: reg.remove("claude"),
                 lambda: reg.remove("nobody"),
                 lambda: reg.switch("nobody")):
        with pytest.raises(LaneError) as exc:
            call()
        assert exc.value.code in {"lane_exists", "unknown_lane"}
        assert exc.value.remedy.startswith("voice-tunnel ")


def test_the_default_lane_cannot_be_removed():
    """It is the lane every unlabelled turn on disk belongs to. Removing it would orphan 1,866
    turns that an agent is still reading."""
    reg = LaneRegistry("claude")
    with pytest.raises(LaneError) as exc:
        reg.remove("claude")
    assert exc.value.code == "lane_exists"


def test_removing_the_live_lane_falls_back_to_the_default():
    """FR1 says there is exactly one live lane. That has to stay true during a removal, not only
    before and after one."""
    reg = LaneRegistry("claude")
    reg.add("codex")
    reg.switch("codex")
    assert reg.current == "codex"
    reg.remove("codex")
    assert reg.current == "claude"


def test_switching_to_an_unknown_lane_is_refused_and_the_remedy_lists_the_real_ones():
    reg = LaneRegistry("claude")
    with pytest.raises(LaneError) as exc:
        reg.switch("codex")
    assert exc.value.code == "unknown_lane"
    assert "claude" in exc.value.remedy and BROADCAST in exc.value.remedy


def test_apply_reports_whether_the_conversation_actually_moved():
    """The server broadcasts on this and it is what wakes an off-lane agent's watch, so a switch
    to the lane already live must NOT report movement — or every repeat of the same name would
    wake every agent for nothing."""
    reg = LaneRegistry("claude")
    reg.add("codex")
    assert reg.apply(reg.resolve("hey codex go")) is True
    assert reg.current == "codex"
    assert reg.apply(reg.resolve("hey codex again")) is False
    assert reg.apply(reg.resolve("hey can you")) is False
    assert reg.current == "codex"


def test_confusability_is_reported_and_never_enforced():
    """Proximity to ordinary speech is what predicted every measured false refusal — `grok`
    against `go`, `got`, `god`. Proximity to another LANE predicted nothing. So the number is
    surfaced and the choice stays his."""
    ordinary = ["go", "got", "god", "can", "i", "let", "so", "did", "all"]
    assert lanes.confusability("grok", ordinary)          # grok is the expensive name
    assert not lanes.confusability("claude", ordinary)    # claude is not
    # And it is a report, not a gate: registering the expensive name still succeeds.
    reg = LaneRegistry("claude")
    assert reg.add("grok") == "grok"
