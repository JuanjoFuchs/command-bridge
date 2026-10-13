"""`describe` is the tie-break, so `describe` has to be the one that is right.

The guide written against this tool states, as its rule for resolving disagreements, that
`describe` is authoritative. An audit on 2026-08-14 found that claim false in three places and
undefined in a fourth — which is worse than the guide being wrong on its own, because the rule
tells the reader to believe whichever copy has drifted.

The repair is not to weaken the tie-break. It is to fix the CLI copies, for the reason
`_next_action`'s own docstring already gives: *"`describe` is read once, at the start of a
session, and by then it is a manual… Guidance keyed to state beats guidance keyed to memory."* A
guide is one level further from the moment than `describe` is.

These tests pin the four repairs, so the next edit cannot quietly re-open one.
"""
import argparse

import pytest

from tests.test_cli_surface import run
from command_bridge import cli

# ------------------------------------------------------------------- `say`'s return shape


def test_describe_documents_every_field_say_actually_returns():
    """It listed `{queued, id, seconds}` and returned seven. The two it omitted are the two that
    decide what to do next: `delivered` (did anyone hear it) and `held_for` (was he still
    talking while you wrote it). An agent obeying the tie-break checked neither."""
    documented = cli.DESCRIBE["commands"]["say"]["returns"]

    for field in ("queued", "id", "held_for", "delivered", "reason", "next"):
        assert field in documented, f"`say` returns {field} and describe does not mention it"


def test_the_hold_is_documented_as_a_reason_to_watch_again():
    """`held_for` is the final-wait rule keyed to a fact the tool has already measured: the server
    held this clip because he was STILL SPEAKING while it was being composed.

    Says WAIT AGAIN since spec 005 — the command it used to name no longer exists as a separate
    thing, and a document that sends the reader to a retired name is how `--waits 5,3,2` outlived
    the ladder it configured."""
    text = cli.DESCRIBE["commands"]["say"]["returns"]["held_for"]

    assert "WAIT AGAIN" in text.upper()
    assert "command-bridge watch" in text, "name the command, do not describe it"
    assert "moved past" in text, "say WHY: the reply may answer a question he has left behind"


def test_delivered_is_documented_as_not_an_error():
    text = cli.DESCRIBE["commands"]["say"]["returns"]["delivered"]
    assert "queued" in text and "not an error" in text.lower()


def _say(monkeypatch, **server_says):
    """Run `say` against a server that returns exactly `server_says`."""
    payload = {"queued": True, "id": "clip-1", "seconds": 1.0, "held_for": 0.9,
               "held_for_speech": False, "delivered": True, "reason": None,
               "unread": [], "unread_count": 0, "cursor": 7}
    payload.update(server_says)
    monkeypatch.setattr(cli, "_request", lambda *a, **k: dict(payload))
    return cli.cmd_say(argparse.Namespace(session="dev", text="hi", voice=None, now=False))


def test_a_held_reply_is_told_to_watch_before_it_is_trusted(monkeypatch):
    """THE BRANCH THAT DID NOT EXIST. The server measured the overlap and said nothing about it."""
    out = _say(monkeypatch, held_for=3.4, held_for_speech=True)

    assert "watch" in out["next"], "the hold is evidence he kept talking; go and read what he said"
    assert "3.4" in out["next"], "state the measurement, not a vague warning"
    assert "moved past" in out["next"]


def test_an_unheld_reply_just_goes_back_to_listening(monkeypatch):
    """One command now, so this can no longer be "watch, not drain". What it still has to check
    is that nothing URGENT is implied when the server did not have to hold the clip."""
    out = _say(monkeypatch)

    assert "command-bridge watch" in out["next"]
    assert "NOW" not in out["next"], "nothing suggests he kept talking; do not raise the alarm"
    assert "moved past" not in out["next"]


def test_nobody_listening_outranks_the_hold(monkeypatch):
    """There is no stale reply to worry about when there was no listener to hear it."""
    out = _say(monkeypatch, held_for=9.0, held_for_speech=True, delivered=False,
               reason="no_client")

    assert "unreachable" in out["next"]


# ----------------------------------------------------------------------------- verbose OFF


def test_verbose_off_means_silence_is_the_default():
    """The two documents said opposite things. `describe` said "confirm the order out loud…
    never go quiet on your own initiative"; the guide said "stay quiet until he asks, silence is
    the default". The guide had his actual stated preference, so the CLI moved to it."""
    notes = cli.DESCRIBE["commands"]["verbose"]["notes"]

    assert "SILENCE IS THE DEFAULT" in notes
    assert "never go quiet on your own initiative" not in notes, (
        "that is the instruction that contradicted the owner's stated preference"
    )


def test_verbose_off_keeps_the_part_describe_had_right():
    """One coherent instruction, not two. The handshake at the START of an order is what makes
    the silence that follows readable — a silence he was warned about is not one he has to
    interrupt to check."""
    notes = cli.DESCRIBE["commands"]["verbose"]["notes"]

    assert "CONFIRM" in notes.upper(), "an accepted order is still acknowledged"
    assert "a while" in notes, "and a long job is still flagged before going heads-down"


def test_every_verbose_off_instruction_in_the_payload_agrees():
    """Three places describe this mode. They have to say the same thing, because an agent reads
    whichever one reaches it first."""
    for text in (cli.DESCRIBE["commands"]["verbose"]["notes"],
                 cli.DESCRIBE["commands"]["watch"]["notes"],
                 cli.DESCRIBE["commands"]["watch"]["returns"]["verbose"]):
        lowered = text.lower()
        assert ("stay quiet" in lowered or "speak only when he elicits" in lowered), (
            f"this copy does not say silence is the default: {text[:120]}"
        )


def test_the_next_action_for_a_quiet_session_tells_you_to_stay_quiet():
    """`next` is the copy that arrives at the moment it applies, so it is the one that matters
    most — and it used to prescribe the opposite of the guide."""
    nxt = cli._next_action(
        [{"id": 1, "text": "do the thing"}],
        {"clients": 1, "channel_open": True, "capturing": True, "muted": False, "verbose": False},
        "dev", 4,
    )

    assert "stay quiet" in nxt
    assert "confirm" in nxt


# --------------------------------------------------------------------- detaching the watch


def test_the_background_flag_resolves_its_own_argument_about_detaching():
    """The detaching rule lives in ONE place — since 2026-10-01 the `--background` arg, which
    replaced `--timeout` — and must name both halves: a LISTEN run in the background holds long and
    is fine, and the PRE-SAY wait, which gates your own mouth, must never be backgrounded —
    foreground and last. One home, so there is nothing left to drift against."""
    bg = cli.DESCRIBE["commands"]["watch"]["args"]["--background"]

    assert "BACKGROUND" in bg.upper(), "say it is the flag for a watch you run in the background"
    assert "PRE-SAY" in bg and "FOREGROUND" in bg and "LAST" in bg, (
        "and say the wait that gates your own mouth must never be backgrounded — foreground and last"
    )


# ------------------------------------------------------------------------ the backoff ladder


def test_the_published_ladder_is_the_one_the_code_computes():
    """Since 2026-10-01 there is no ramp: the idle wait is the cap from the first call, and the
    document states that number from the constant rather than a typed copy."""
    assert cli._backoff_ladder() == [540.0]
    assert cli._backoff_ladder_text() == "9min"
    assert cli._human_seconds(cli.WATCH_BACKOFF_MAX_S) in cli.DESCRIBE["commands"]["watch"]["args"]["--background"]


def test_the_ladder_is_generated_rather_than_typed():
    """The arithmetic still clamps to the cap, so an explicit smaller base would still ramp — the
    published flat wait is a property of the default base, not a hard-coded string."""
    assert cli._backoff_ladder(30.0) == [30.0, 60.0, 120.0, 240.0, 480.0, 540.0]


def test_each_rung_is_what_a_watch_would_actually_wait(monkeypatch):
    """The ladder must be the arithmetic `cmd_watch` runs, not a parallel description of it."""
    for streak, expected in enumerate(cli._backoff_ladder()):
        assert cli._backoff_ceiling(cli.WATCH_BASE_S, streak, True) == expected


# --------------------------------------------------------- the signals the drain rests on


@pytest.mark.parametrize("field", ["user_speaking", "speech_active", "pending_turns",
                                   "last_turn_id", "consumed_cursor", "watch_open"])
def test_status_documents_the_fields_the_discipline_rests_on(field):
    """`status.returns` listed `url`, `phone`, `pid` and "...". `user_speaking` and
    `speech_active` are the only things that can tell a breath between clauses apart from the end
    of a thought — and they appeared in NO CLI-facing document at all."""
    returns = cli.DESCRIBE["commands"]["status"]["returns"]
    assert field in returns, f"`status` returns {field} and describe does not mention it"


def test_the_speech_signals_are_documented_as_additive():
    """THREE signals now, not two, and the muted caveat is INVERTED rather than dropped.

    It used to read "a MUTED microphone leaves speech_active stuck true, check muted first" — a
    true statement about the old server and a dangerous one about this one, because under a
    speaking-gated wait a reader who believes the flag lies will write a second workaround for a
    bug that is fixed. Spec 005 fixed it at the source, so the document has to say the OPPOSITE:
    frames stopping IS speech stopping."""
    returns = cli.DESCRIBE["commands"]["status"]["returns"]
    note = returns["_speaking_note"]

    assert "ANY" in note, "he is talking if any of the three says so"
    assert "no longer applies" in note, "the muted caveat is retired, not silently deleted"
    assert "frames stopping" in note.lower(), "say what replaced it"
    assert "lag" in returns["speech_active"].lower(), "say which one is late and why"
    assert "immediate" in returns["user_speaking"].lower()
    assert "transcribed" in returns["speech_pending"], "and say what the third one counts"


def test_pending_turns_is_documented_with_the_trap_it_used_to_be():
    returns = cli.DESCRIBE["commands"]["status"]["returns"]

    assert "turns_logged" in returns["pending_turns"], (
        "the field it used to be derived from is the reason to trust this one"
    )
    assert "NOT a cursor" in returns["turns_logged"], (
        "the impostor must be labelled where it is published"
    )


def test_status_still_returns_what_describe_now_claims(capsys, tmp_sessions, monkeypatch):
    """A contract is only worth anything if the payload actually carries it."""
    cli.write_runtime("dev", "127.0.0.1", 8765, "t")
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {
        "session": "dev", "user_speaking": False, "speech_active": False,
        "pending_turns": 0, "last_turn_id": 3, "consumed_cursor": 3, "watch_open": False,
    })

    _, payload, _ = run(["status", "--session", "dev"], capsys)

    for field in ("user_speaking", "speech_active", "pending_turns", "last_turn_id"):
        assert field in payload
