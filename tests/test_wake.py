"""Wake gating — spec 001 AC-4, AC-5, AC-6.

The name is configurable (`VOICE_TUNNEL_WAKE_NAME`) and defaults to a role, not a product. The
tests below still exercise "claude" on purpose: they encode behaviour discovered against real
speech — the greeting-prefix rule and the fuzzy threshold — and rewriting them for a new name
would throw that history away to prove nothing. The autouse fixture pins the name so they keep
testing behaviour rather than configuration.

**Nothing is ever removed from the transcript** (Reported 2026-08-07). Several tests below assert
that directly, because the previous rule stripped a leading summons and the class of bug it
produced — the log disagreeing with what he remembers saying — was silent every time.

The tests at the bottom cover the rule that a GREETING IS ALWAYS REQUIRED, which replaced the
per-name `VOICE_TUNNEL_WAKE_BARE` opt-in.
"""
import pytest

from voice_tunnel import config
from voice_tunnel.wake import WakeGate


@pytest.fixture(autouse=True)
def _classic_name(monkeypatch):
    """Pin the historical name, which is what these tests were written against. Without this
    every assertion below would be testing today's default instead."""
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "claude")


def test_turn_without_wake_phrase_is_not_addressed():
    """AC-4"""
    g = WakeGate()
    addressed, text = g.evaluate("so then I told him it was fine", now=100.0)
    assert addressed is False
    assert text == "so then I told him it was fine"


def test_wake_phrase_addresses_and_the_words_are_left_alone():
    """AC-5 — the phrase wakes the agent and STAYS in the transcript.

    Reported 2026-08-07: "you removed that hay clot [hey Claude] from the transcription. I think we
    should stop doing that." The turn log records what a person said; an agent can ignore two
    words on its own, and editing them made the log disagree with his memory of the sentence.
    """
    g = WakeGate()
    addressed, text = g.evaluate("Hey Claude, what is the status?", now=100.0)
    assert addressed is True
    assert text == "Hey Claude, what is the status?"


def test_wake_phrase_matching_is_punctuation_and_case_insensitive():
    """Matching normalizes; the text handed on does not."""
    for utterance in ["hey claude what's up", "Hey, Claude! what's up", "HEY CLAUDE what's up"]:
        addressed, text = WakeGate().evaluate(utterance, now=1.0)
        assert addressed is True
        assert text == utterance


def test_follow_up_inside_the_window_stays_addressed_without_the_phrase():
    """AC-6 — a back-and-forth must not require the phrase every single time."""
    g = WakeGate(window_s=30.0)
    assert g.evaluate("hey claude what is running", now=100.0)[0] is True
    addressed, text = g.evaluate("and what about the other one", now=110.0)
    assert addressed is True
    assert text == "and what about the other one"   # nothing stripped, nothing was said


def test_the_window_can_be_opened_by_another_gate():
    """The voiceprint grants attention to turns with no phrase in them, and the window has to
    follow — otherwise a voice-addressed turn starts no conversation and the next short
    follow-up, too brief for the embedder to score, arrives unaddressed mid-exchange.

    Live, 2026-08-07: "I have said a few things to you directing myself to you, but I see
    they're being categorized as [not addressed]."
    """
    g = WakeGate(window_s=30.0)

    # No phrase, so this gate alone would not address it. The server did, on the voiceprint.
    assert g.evaluate("each state gets its own count", now=100.0, ended=104.0)[0] is False
    g.mark_addressed(104.0)

    # The follow-up now rides the window, exactly as if a phrase had opened it.
    assert g.evaluate("just one thing", now=106.0, ended=107.0)[0] is True
    # ...and continuing extends it, so a long exchange never times itself out.
    assert g.evaluate("that's my taste", now=130.0, ended=131.0)[0] is True
    # The window is still a window: leave for long enough and it closes.
    assert g.evaluate("unrelated chatter", now=200.0)[0] is False


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


def test_a_bare_name_never_wakes_however_uncommon_it_is(monkeypatch):
    """The rule that replaced a setting. The owner: "I would make it a default to always require the
    hey, and the only thing the agent or the user can choose is the wake word."

    This used to be opt-in per name, on the theory that "claude" is uncommon enough to say bare
    while "thursday" is not. Deleting the option deleted a class of bug: it was the tool asking
    every user to predict whether their agent's name collides with ordinary speech, a judgment
    they can only get wrong, and whose failure is an agent interrupting a conversation it was
    never part of. `grok` is a verb, `cursor` and `gemini` are words — none of it matters now.
    """
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "claude")
    addressed, text = WakeGate().evaluate("claude status please", now=1.0)
    assert addressed is False
    assert text == "claude status please", "an unaddressed turn must not be edited"


def test_the_greeting_form_wakes_and_keeps_every_word():
    addressed, text = WakeGate().evaluate("okay claude status please", now=1.0)
    assert addressed is True
    assert text == "okay claude status please"


def test_mid_sentence_mention_wakes_and_survives_intact():
    """Regression for the live bug that started this (Reported 2026-07-29): "and do I need to say hey
    Claude every time?" became "and do I need to say every time?" — he was REFERRING to the
    phrase, not using it. That fix spared mid-sentence mentions; 2026-08-07 spared all of them.
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


def test_a_leading_phrase_is_kept_too():
    """The case that used to be the one exception. There are no exceptions now."""
    addressed, text = WakeGate().evaluate("hey claude do I need to say it every time?", now=1.0)
    assert addressed is True
    assert text == "hey claude do I need to say it every time?"


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
    """Regression (Live, 2026-07-29): a ~60 s continuous turn came back addressed=False.

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
# Live, 2026-07-29: Parakeet rendered the owner's "hey Claude" as "hey grab" / "hey grub", so the gate
# correctly matched nothing and he spent a whole session talking to nobody.

def test_mangled_name_after_a_greeting_still_wakes():
    for utterance in ["hey grab I have a question",
                      "hey grub what is the status",
                      "hi clod are you there",
                      "okay cloud lets keep going"]:
        addressed, _ = WakeGate().evaluate(utterance, now=1.0)
        assert addressed is True, f"should wake on {utterance!r}"


def test_a_near_miss_on_the_name_wakes_and_still_changes_nothing():
    """Both of these wake the agent — "clod" by fuzzy match, "grab" by the greeting rule — and
    the difference between them no longer shows up in the text, because neither is edited."""
    for said in ["hey clod what is the status", "hey grab what is the status"]:
        addressed, text = WakeGate().evaluate(said, now=1.0)
        assert addressed is True
        assert text == said


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

    Live, 2026-08-03: "my goal is to be able to use this with any AI agent, not just cloud."
    """
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "thursday")

    addressed, text = WakeGate().evaluate("Hey Thursday, what is the status?", now=100.0)
    assert addressed is True
    assert text == "Hey Thursday, what is the status?"   # woken, and not edited

    # A LEADING greeting wakes whatever the name is, so it cannot prove the name was configured.
    # A mid-sentence phrase can: it matches the configured phrase verbatim or not at all.
    mid = "so I said hey thursday and he laughed"
    assert WakeGate().evaluate(mid, now=200.0)[0] is True
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "claude")
    assert WakeGate().evaluate(mid, now=300.0)[0] is False


def test_a_common_word_name_does_not_fire_bare(monkeypatch):
    """The whole reason a day of the week is usable at all.

    "Let's ship it Thursday" must NOT wake the agent, while "hey Thursday" must. The owner spotted this
    himself when the objection was that days are words you say constantly: the wake phrase is a
    GREETING PLUS A NAME, and nobody says "hey Thursday" by accident.
    """
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "thursday")

    for said in ["let's ship it thursday",
                 "see you thursday",
                 "I'll look at it thursday morning",
                 "thursday works for me"]:
        addressed, text = WakeGate().evaluate(said, now=1.0)
        assert addressed is False, f"{said!r} should not wake the agent"
        assert text == said, "an unaddressed turn must not be edited"


def test_the_greeting_form_still_wakes_a_common_word_name(monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "thursday")

    for said in ["hey thursday what's the status",
                 "Hi Thursday, are you there?",
                 "okay thursday go ahead"]:
        addressed, text = WakeGate().evaluate(said, now=1.0)
        assert addressed is True, f"{said!r} should wake the agent"
        assert text == said, "the summons is kept, like every other word"


def test_a_greeting_is_mandatory_and_cannot_be_switched_off(monkeypatch):
    """The old escape hatch is gone, and setting it must not resurrect the bare-name path.

    `VOICE_TUNNEL_WAKE_BARE=1` was the opt-in. A user carrying it in an old `.env`, or copying it
    from a stale README, must not silently get a gate that fires on an ordinary word.
    """
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "thursday")
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_BARE", "1")   # the setting no longer exists

    assert WakeGate().evaluate("thursday what's the status", now=1.0)[0] is False
    assert WakeGate().evaluate("hey thursday what's the status", now=1.0)[0] is True


def test_every_greeting_builds_a_phrase(monkeypatch):
    """The phrase list and the matcher must come from ONE list.

    They were two separate paths once, and that is precisely how removing the bare form from
    `wake_phrases()` failed to stop a bare "thursday" from waking the agent — the matcher had its
    own rule. `config.GREETINGS` is now the single source for both.
    """
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "codex")
    for greeting in config.GREETINGS:
        said = f"{greeting} codex what is the status"
        addressed, text = WakeGate().evaluate(said, now=1.0)
        assert addressed is True, f"{greeting!r} should open a summons"
        assert text == said, f"{greeting!r} must wake without editing the words"


def test_an_ordinary_word_name_is_safe_because_the_greeting_is_mandatory(monkeypatch):
    """The payoff. A user can name their agent after a verb and nothing breaks."""
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "grok")

    for said in ["I finally grok the cursor problem",
                 "did you grok that",
                 "grok is the model we tested"]:
        assert WakeGate().evaluate(said, now=1.0)[0] is False, f"{said!r} must not wake"
    assert WakeGate().evaluate("hey grok what is the status", now=1.0)[0] is True


def test_renaming_does_not_end_the_conversation_in_progress(monkeypatch):
    """`set_phrases` exists instead of rebuilding the gate, and this is why.

    Renaming mid-session is real — the agent hands off, or the user finds their ASR cannot
    recover the name they picked. A fresh WakeGate would start with no `_last_addressed_at`, so
    the very next sentence would come back unaddressed and they would have to say the new phrase
    to resume something they never stopped doing.
    """
    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "claude")
    g = WakeGate(window_s=30.0)
    assert g.evaluate("hey claude what is running", now=100.0)[0] is True

    monkeypatch.setenv("VOICE_TUNNEL_WAKE_NAME", "codex")
    g.set_phrases(config.wake_phrases())

    # Mid-conversation, so no phrase is needed at all.
    assert g.evaluate("and the other one", now=110.0)[0] is True

    # A MID-SENTENCE phrase is what proves the list actually changed. A leading greeting wakes
    # whoever is listening — that is rule 3 working as designed, because Parakeet renders the
    # name as grab / grub / Crawley and no fuzzy match can recover a sound that is not there —
    # so only rule 4, which needs the configured phrase verbatim, can tell old from new:
    assert g.evaluate("I told them hey codex is running", now=500.0)[0] is True
    assert g.evaluate("I told them hey claude is running", now=900.0)[0] is False

    # Both still wake from the front, and neither is edited.
    assert g.evaluate("hey codex status", now=1000.0) == (True, "hey codex status")
    assert g.evaluate("hey claude status", now=1100.0) == (True, "hey claude status")

    # The bare old name, with no greeting, is now simply a word.
    assert g.evaluate("claude status", now=1300.0)[0] is False
