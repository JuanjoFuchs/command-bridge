"""Wake gating — spec 001 AC-4, AC-5, AC-6.

The assistant's name is configurable (`VM_WAKE_NAME`) and is no longer "claude". The tests below
still exercise the ORIGINAL name on purpose: they encode behaviour discovered against real
speech — the greeting-prefix rule, the two fuzzy thresholds, mid-sentence mentions that must not
be stripped — and rewriting them for a new name would throw that history away to prove nothing.
The autouse fixture pins the name so they keep testing behaviour rather than configuration.

`test_configurable_name` and the greeting-required tests at the bottom cover the new setting.
"""
import pytest

from vm.wake import WakeGate


@pytest.fixture(autouse=True)
def _classic_name(monkeypatch):
    """Pin the historical name and allow the bare form, which is what these tests were written
    against. Without this every assertion below would be testing today's default instead."""
    monkeypatch.setenv("VM_WAKE_NAME", "claude")
    monkeypatch.setenv("VM_WAKE_BARE", "1")


def test_turn_without_wake_phrase_is_not_addressed():
    """AC-4"""
    g = WakeGate()
    addressed, text = g.evaluate("so then I told him it was fine", now=100.0)
    assert addressed is False
    assert text == "so then I told him it was fine"


def test_wake_phrase_addresses_and_is_stripped():
    """AC-5 — the agent should get the request, not the summons."""
    g = WakeGate()
    addressed, text = g.evaluate("Hey Claude, what is the status?", now=100.0)
    assert addressed is True
    assert text == "what is the status?"


def test_wake_phrase_matching_is_punctuation_and_case_insensitive():
    for utterance in ["hey claude what's up", "Hey, Claude! what's up", "HEY CLAUDE what's up"]:
        addressed, text = WakeGate().evaluate(utterance, now=1.0)
        assert addressed is True
        assert "claude" not in text.lower()


def test_follow_up_inside_the_window_stays_addressed_without_the_phrase():
    """AC-6 — a back-and-forth must not require the phrase every single time."""
    g = WakeGate(window_s=30.0)
    assert g.evaluate("hey claude what is running", now=100.0)[0] is True
    addressed, text = g.evaluate("and what about the other one", now=110.0)
    assert addressed is True
    assert text == "and what about the other one"   # nothing stripped, nothing was said


def test_conversation_window_expires():
    g = WakeGate(window_s=30.0)
    g.evaluate("hey claude hello", now=100.0)
    assert g.evaluate("unrelated chatter", now=200.0)[0] is False


def test_each_exchange_extends_the_window():
    g = WakeGate(window_s=30.0)
    g.evaluate("hey claude hello", now=100.0)
    assert g.evaluate("follow up one", now=125.0)[0] is True     # extends to 155
    assert g.evaluate("follow up two", now=150.0)[0] is True     # still inside
    assert g.evaluate("much later", now=200.0)[0] is False


def test_bare_claude_still_wakes_but_longest_phrase_wins_for_stripping():
    addressed, text = WakeGate().evaluate("claude status please", now=1.0)
    assert addressed is True
    assert text == "status please"

    addressed, text = WakeGate().evaluate("okay claude status please", now=1.0)
    assert addressed is True
    assert text == "status please"


def test_mid_sentence_mention_wakes_but_is_not_stripped():
    """Detection is generous, stripping is conservative.

    Regression for a live bug (JJ, 2026-07-29): "and do I need to say hey Claude every time?"
    became "and do I need to say every time?" — he was REFERRING to the phrase, not using it,
    and stripping changed what he asked. Leaving a stray phrase costs the agent nothing;
    removing one the speaker meant to keep corrupts the request.
    """
    addressed, text = WakeGate().evaluate("so hey claude can you check that", now=1.0)
    assert addressed is True
    assert text == "so hey claude can you check that"


def test_referring_to_the_phrase_does_not_mangle_the_sentence():
    addressed, text = WakeGate().evaluate(
        "and do I need to say hey Claude every time?", now=1.0
    )
    assert addressed is True
    assert text == "and do I need to say hey Claude every time?"


def test_leading_phrase_is_still_stripped():
    addressed, text = WakeGate().evaluate("hey claude do I need to say it every time?", now=1.0)
    assert addressed is True
    assert text == "do I need to say it every time?"


def test_disabled_gate_addresses_everything():
    """Push-to-talk mode: the operator already signalled intent by holding the button."""
    g = WakeGate(enabled=False)
    addressed, text = g.evaluate("no wake phrase here at all", now=1.0)
    assert addressed is True
    assert text == "no wake phrase here at all"


def test_empty_transcript_is_never_addressed():
    assert WakeGate().evaluate("   ", now=1.0)[0] is False


def test_reset_clears_the_conversation_window():
    g = WakeGate(window_s=30.0)
    g.evaluate("hey claude hi", now=100.0)
    g.reset()
    assert g.evaluate("follow up", now=105.0)[0] is False


def test_a_long_monologue_does_not_time_itself_out():
    """Regression (JJ, live 2026-07-29): a ~60 s continuous turn came back addressed=False.

    The window was measured from the previous turn's END to this turn's END, so talking for a
    long time looked identical to staying silent for a long time. The gap that matters is the
    SILENCE between turns: previous end -> this start.
    """
    g = WakeGate(window_s=30.0)
    assert g.evaluate("hey claude here is the situation", now=0.0, ended=5.0)[0] is True
    # Starts 2 s after the last one ended, but runs for a full minute.
    addressed, _ = g.evaluate("a very long continuous thought", now=7.0, ended=67.0)
    assert addressed is True, "talking for a long time is not the same as going quiet"
    # And the next turn, starting soon after that one ended, is still in the conversation.
    assert g.evaluate("still going", now=69.0, ended=72.0)[0] is True


def test_a_real_silence_gap_still_closes_the_window():
    g = WakeGate(window_s=30.0)
    g.evaluate("hey claude hello", now=0.0, ended=5.0)
    assert g.evaluate("much later", now=90.0, ended=93.0)[0] is False


def test_ended_defaults_to_now_for_simple_callers():
    g = WakeGate(window_s=30.0)
    assert g.evaluate("hey claude hi", now=10.0)[0] is True
    assert g.evaluate("follow up", now=20.0)[0] is True


# --- robustness to mistranscribed names --------------------------------------
# Live 2026-07-29: Parakeet rendered JJ's "hey Claude" as "hey grab" / "hey grub", so the gate
# correctly matched nothing and he spent a whole session talking to nobody.

def test_mangled_name_after_a_greeting_still_wakes():
    for utterance in ["hey grab I have a question",
                      "hey grub what is the status",
                      "hi clod are you there",
                      "okay cloud lets keep going"]:
        addressed, _ = WakeGate().evaluate(utterance, now=1.0)
        assert addressed is True, f"should wake on {utterance!r}"


def test_a_near_miss_on_the_name_is_stripped_but_a_wild_miss_is_not():
    # "clod" is plausibly the name -> safe to remove.
    _, text = WakeGate().evaluate("hey clod what is the status", now=1.0)
    assert text == "what is the status"
    # "grab" is not recoverable as the name -> wake, but leave the words alone.
    _, text = WakeGate().evaluate("hey grab what is the status", now=1.0)
    assert text == "hey grab what is the status"


def test_a_greeting_alone_does_not_mangle_an_ordinary_sentence():
    addressed, text = WakeGate().evaluate("hey look at everything in this list", now=1.0)
    assert addressed is True          # generous: a greeting opens an address
    assert text == "hey look at everything in this list"   # but nothing is edited


def test_ordinary_words_are_not_mistaken_for_the_name():
    for word in ["cloud9", "close", "could", "loud", "code", "clown"]:
        g = WakeGate()
        addressed, _ = g.evaluate(f"the {word} is over there", now=1.0)
        assert addressed is False, f"{word!r} should not wake mid-sentence"


# ------------------------------------------------- a configurable, non-Claude name


def test_the_name_is_configurable(monkeypatch):
    """The tool holds no model, so nothing should tie it to one vendor's name.

    JJ, live 2026-08-03: "my goal is to be able to use this with any AI agent, not just cloud."
    """
    monkeypatch.setenv("VM_WAKE_NAME", "thursday")
    monkeypatch.setenv("VM_WAKE_BARE", "0")

    addressed, text = WakeGate().evaluate("Hey Thursday, what is the status?", now=100.0)

    assert addressed is True
    assert text == "what is the status?"


def test_a_common_word_name_does_not_fire_bare(monkeypatch):
    """The whole reason a day of the week is usable at all.

    "Let's ship it Thursday" must NOT wake the agent, while "hey Thursday" must. JJ spotted this
    himself when the objection was that days are words you say constantly: the wake phrase is a
    GREETING PLUS A NAME, and nobody says "hey Thursday" by accident.
    """
    monkeypatch.setenv("VM_WAKE_NAME", "thursday")
    monkeypatch.setenv("VM_WAKE_BARE", "0")

    for said in ["let's ship it thursday",
                 "see you thursday",
                 "I'll look at it thursday morning",
                 "thursday works for me"]:
        addressed, text = WakeGate().evaluate(said, now=1.0)
        assert addressed is False, f"{said!r} should not wake the agent"
        assert text == said, "an unaddressed turn must not be edited"


def test_the_greeting_form_still_wakes_a_common_word_name(monkeypatch):
    monkeypatch.setenv("VM_WAKE_NAME", "thursday")
    monkeypatch.setenv("VM_WAKE_BARE", "0")

    for said in ["hey thursday what's the status",
                 "Hi Thursday, are you there?",
                 "okay thursday go ahead"]:
        addressed, text = WakeGate().evaluate(said, now=1.0)
        assert addressed is True, f"{said!r} should wake the agent"
        assert "thursday" not in text.lower(), "the summons should be stripped"


def test_bare_is_opt_in_per_name_not_a_hard_rule(monkeypatch):
    """An uncommon name is safe bare; a common one is not. The constraint belongs to the NAME."""
    monkeypatch.setenv("VM_WAKE_NAME", "thursday")
    monkeypatch.setenv("VM_WAKE_BARE", "1")

    addressed, _ = WakeGate().evaluate("thursday what's the status", now=1.0)

    assert addressed is True
