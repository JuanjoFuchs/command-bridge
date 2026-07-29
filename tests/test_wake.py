"""Wake gating — spec 001 AC-4, AC-5, AC-6."""
from vm.wake import WakeGate


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
