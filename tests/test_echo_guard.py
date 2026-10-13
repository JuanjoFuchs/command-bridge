"""The echo guard (echo.py): an agent's own clip coming back through his desk mic must not become
his turn — and his own short replies must never be mistaken for one (JJ, 2026-09-30)."""

from command_bridge import echo


DEXTER = ("This one's complete. It's bigger because it scanned the whole glass, not just the main "
          "diagram, so I cropped it.")


def test_a_fragment_of_a_just_played_clip_is_echo():
    """The live sighting: two of dexter's sentences logged as his turns at 12:33. The mic caught a
    fragment, not the clip — which is exactly his question: "what if the mic doesn't pick up your
    full text?" """
    assert echo.match("It's bigger because it scanned the whole glass, not just the main",
                      [DEXTER]) == DEXTER


def test_a_mangled_word_or_two_still_matches():
    assert echo.match("its bigger cause it scanned the whole class not just the main", [DEXTER])


def test_his_short_replies_are_never_echo():
    """'Yes', 'go ahead', 'do it' appear inside almost any clip; dropping them is the worse error."""
    for reply in ("yes", "go ahead", "do it"):
        assert echo.match(reply, ["Yes, go ahead and do it now."]) is None


def test_his_own_sentence_is_not_echo():
    assert echo.match("Alright, let's build it inside of a feature toggle", [DEXTER]) is None


def test_his_short_command_is_never_echo_of_anything():
    """14:08, 2026-09-30: his "ship it" (heard as "Shape it") was dropped by a cue rule since removed.
    Two words can never be judged echo here, whatever was just played."""
    assert echo.match("Shape it", ["ship it now, then shape it up"]) is None


def test_only_clips_still_playing_or_just_finished_are_candidates():
    now = 1000.0
    recent = [
        {"text": "playing now", "sent_at": now - 5, "played_at": None},
        {"text": "just finished", "sent_at": now - 20, "played_at": now - 4},
        {"text": "long finished", "sent_at": now - 60, "played_at": now - 40},
        {"text": "ancient, never played", "sent_at": now - 600, "played_at": None},
    ]
    assert echo.candidates(recent, now) == ["playing now", "just finished"]
