"""One wait, gated on speech. `watch` is its only name — spec 007 removed the second.

The two commands this replaces were one job with two hard-coded schedules — `watch` backing off
30s->9min, `drain` collapsing 5/3/2 — and **both were clocks standing in for a signal the server
already publishes**. Choosing between them under time pressure is a decision an agent gets wrong;
it happened twice in the session that produced spec 005, costing a 30-second rung each time.

Every test here pins one of three things:

* **the gate** — the wait returns ONLY at a moment when he is not speaking, and the two speech
  signals are not interchangeable: `speech_active` (the server's segmenter, late and
  authoritative) may END the wait, while `user_speaking` (the client's microphone level, early and
  noisy, and it drops during gaps INSIDE a sentence) may only EXTEND it. Returning on the first
  `false` from either is the naive version and it cuts him off mid-sentence;
* **the collapse** — ONE implementation under ONE name. It was three names, then two kept "for a
  release", and the tests here pinned that the aliases could not drift. They now pin that the
  aliases are GONE and that a caller holding an old spelling is handed the new invocation rather
  than argparse's `invalid choice`;
* **the honest exits** — a ceiling, a dead server and a concurrent waiter are each reported as
  themselves and never as permission to speak.

Deliberately NOT here any more: the collapsing ladder, `--waits`, `--max-seconds` and
`_parse_waits`. They configured a schedule that no longer exists. See
specs/005-one-wait-gated-on-speech.md and specs/007-the-tool-enforces-the-loop.md.
"""
import argparse
import json
import time
import types

import pytest

import command_bridge.cli as cli


@pytest.fixture(autouse=True)
def _isolated_streak(tmp_path, monkeypatch):
    """The backoff streak is persisted to disk, so without this the suite writes into the repo's
    real sessions/ directory — and one test's leftover streak changes the next test's ceiling."""
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))


def _args(**kw):
    base = {"session": "s", "since": 5, "force": False, "timeout": 0.3, "all_turns": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _quiet(**kw):
    """A status snapshot from a healthy server with nobody talking."""
    base = {"clients": 1, "capturing": True, "muted": False, "channel_open": True,
            "verbose": False, "watch_open": False, "user_speaking": False,
            "speech_active": False, "speech_pending": 0}
    base.update(kw)
    return base


def _turn(tid, text="hello"):
    return {"id": tid, "session": "s", "text": text, "addressed": True, "final": True}


class Fake:
    """A scripted `/status` timeline and a scripted turn log.

    `cmd_wait` reads `/status` twice before the loop — once for the concurrent-waiter guard and
    once for the control baseline — and then once per poll. So `statuses[0]` is the guard,
    `statuses[1]` is the baseline, and everything after is one per iteration. The LAST entry
    repeats forever, which is what lets a test say "and he is quiet from here on".

    `polls` and `timeouts` are the interesting record: asserting only on the RETURN value would
    pass for an implementation that waited thirty seconds before answering.
    """

    def __init__(self, statuses, turns_at=None):
        self.statuses = list(statuses)
        self.turns_at = dict(turns_at or {})
        self.polls = 0
        self.timeouts = []
        self.paths = []

    def request(self, session, path, payload=None):
        self.paths.append(path)
        if path != "/status":
            return {}
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def watch(self, session, cursor, timeout=0.0, addressed_only=True,
              lane=None, default_lane=None):
        self.timeouts.append(round(timeout, 3))
        turns = self.turns_at.get(self.polls, [])
        self.polls += 1
        if turns:
            return turns, turns[-1]["id"]
        return [], cursor

    def install(self, monkeypatch):
        monkeypatch.setattr(cli, "_request", self.request)
        monkeypatch.setattr(cli.store, "watch", self.watch)
        return self


# ----------------------------------------------------------------- the gate


def test_it_returns_on_the_first_quiet_poll_with_no_rung(monkeypatch):
    """FR2/AC7. Nothing is added on top of the segmenter's own end-of-utterance delay.

    The old drain spent 5+3+2 seconds re-deriving a fact the speech signals already carry. Here a
    turn lands and he is quiet, so the wait hands it over on that same poll.
    """
    fake = Fake([_quiet()], turns_at={0: [_turn(6)]}).install(monkeypatch)
    out = cli.cmd_watch(_args())

    assert out["finished"] is True and out["reason"] == "turns"
    assert [t["id"] for t in out["turns"]] == [6]
    assert fake.polls == 1, "one poll — a rung here would be a ladder by another name"
    assert out["elapsed_s"] < 1.0


def test_the_leading_signal_alone_may_not_end_the_wait(monkeypatch):
    """TC1/AC5. `user_speaking` is the CLIENT's microphone level with a 700 ms hangover, and it
    drops during gaps inside a sentence — so a return authorised by it going false is a return
    authorised by a breath. It may only ever extend."""
    fake = Fake(
        [_quiet(),                              # guard
         _quiet(user_speaking=True),            # baseline: he is talking
         _quiet(user_speaking=True),            # poll 1: still talking -> hold
         _quiet(user_speaking=True),            # poll 2: still talking -> hold
         _quiet()],                             # poll 3: quiet -> return
        turns_at={0: [_turn(6)]},
    ).install(monkeypatch)
    out = cli.cmd_watch(_args())

    assert fake.polls == 3, "it must not return while the client says he is talking"
    assert out["finished"] is True
    assert out["user_speaking"] is False


def test_the_lagging_signal_alone_also_holds_the_wait(monkeypatch):
    """TC1/AC6. `speech_active` is the server's segmenter — it goes false only after the full
    end-of-utterance silence AND the turn model agreeing he sounded finished. It is the
    authoritative one, and it holds the wait on its own too."""
    fake = Fake(
        [_quiet(), _quiet(speech_active=True), _quiet(speech_active=True), _quiet()],
        turns_at={0: [_turn(6)]},
    ).install(monkeypatch)
    out = cli.cmd_watch(_args())

    assert fake.polls == 2
    assert out["finished"] is True


def test_pending_transcription_holds_the_wait_though_both_signals_are_quiet(monkeypatch):
    """AC8, and it is the failure with no visible symptom.

    Between the segmenter closing an utterance and the turn reaching the log — 1-2 s, up to ~13 s
    on long dictation — both booleans read false while he has in fact just spoken and nobody has
    the words. Returning there is not interrupting him; it is answering without having heard him.
    """
    fake = Fake(
        [_quiet(), _quiet(speech_pending=1), _quiet(speech_pending=1), _quiet()],
        turns_at={0: [_turn(6)]},
    ).install(monkeypatch)
    out = cli.cmd_watch(_args())

    assert fake.polls == 2, "speech already spoken and not yet transcribed is still speech"
    assert out["finished"] is True


def test_a_muted_microphone_does_not_wedge_the_wait():
    """The prerequisite the whole gate rests on. Under a speaking-gated wait a signal that lies
    about muting does not merely mis-narrate — it HANGS the tunnel, because the wait never sees
    him stop. Fixed at the source on the server; this pins the CLI's own compatibility guard, for
    an older server that still reports the flag stuck true."""
    assert cli._still_talking({"muted": True, "speech_active": True}) is False
    assert cli._still_talking({"muted": True, "user_speaking": True}) is False
    assert cli._still_talking({"muted": False, "speech_active": True}) is True
    assert cli._still_talking({"muted": False, "user_speaking": True}) is True
    assert cli._still_talking({"muted": False, "speech_pending": 2}) is True
    assert cli._still_talking({"muted": False}) is None
    assert cli._still_talking(None) is None


def test_amplitude_false_gates_on_the_segmenter_not_the_microphone_level():
    """The pre-say gate (2026-09-04). `amplitude=False` drops `user_speaking` — the client's raw
    microphone LEVEL, which trips on any room sound — and keeps only the segmenter's judgement,
    so steady noise no longer holds a reply. On a CURRENT server both segmenter fields are present,
    so amplitude-only sound reads as not-talking."""
    live = {"user_speaking": True, "speech_active": False, "speech_pending": 0}
    assert cli._still_talking(live) is True, "default still counts the mic level"
    assert cli._still_talking(live, amplitude=False) is False, "the gate ignores it"
    assert cli._still_talking({"user_speaking": False, "speech_active": True, "speech_pending": 0},
                              amplitude=False) is True, "real segmented speech still holds"
    assert cli._still_talking({"user_speaking": False, "speech_active": False, "speech_pending": 3},
                              amplitude=False) is True, "speech still transcribing still holds"


def test_amplitude_false_falls_back_to_the_microphone_on_a_pre_segmenter_server():
    """NEVER GO BLIND. A server too old to publish `speech_active`/`speech_pending` would give the
    gate nothing, and reading that as quiet could let the pre-say talk over him. So when no
    segmenter signal exists at all, the gate falls back to amplitude — exactly the old behaviour.
    A current server always carries the segmenter fields, so the fallback never fires there."""
    assert cli._still_talking({"user_speaking": True}, amplitude=False) is True
    assert cli._still_talking({"user_speaking": False}, amplitude=False) is False
    assert cli._still_talking({}, amplitude=False) is None


def test_turns_arriving_do_not_end_the_wait_while_he_is_still_going(monkeypatch):
    """RULE_2: one thought arrives as several turns, and returning on the first answers the wrong
    question. Every round comes back, not just the last — nothing else will replay the earlier
    ones."""
    Fake(
        [_quiet(), _quiet(user_speaking=True), _quiet(user_speaking=True),
         _quiet(user_speaking=True), _quiet()],
        turns_at={0: [_turn(6)], 1: [_turn(7), _turn(8)]},
    ).install(monkeypatch)
    out = cli.cmd_watch(_args())

    assert [t["id"] for t in out["turns"]] == [6, 7, 8]
    assert out["count"] == 3
    assert out["cursor"] == 8, "the next call must resume from the cursor it was handed"
    assert out["rounds"] == 2


def test_it_polls_fast_while_holding_and_slowly_while_idle(monkeypatch):
    """NFR2. The 200 ms poll is a SAMPLING INTERVAL, not a rung: it does not grow, it does not
    depend on history, and it bounds the measurement error rather than the wait. Polling that fast
    through an eight-hour disconnected wait would be 144,000 requests, so idle keeps its 1 s."""
    fake = Fake([_quiet(), _quiet(user_speaking=True), _quiet(user_speaking=True), _quiet()],
                turns_at={0: [_turn(6)]}).install(monkeypatch)
    cli.cmd_watch(_args(timeout=30.0))
    assert all(t == cli.WATCH_POLL_SPEECH_S for t in fake.timeouts), fake.timeouts

    idle = Fake([_quiet()]).install(monkeypatch)
    cli.cmd_watch(_args(timeout=30.0))
    assert idle.timeouts[0] == cli.WATCH_POLL_IDLE_S


# --------------------------------------------------------------- honest exits


def test_the_ceiling_is_reported_as_itself(monkeypatch):
    """`finished: false` when he was STILL TALKING as time ran out. The ceiling exists so the
    command written to stop an agent interrupting cannot become the hang that stops it answering —
    but a wait that ended because the clock did is not the same fact as silence."""
    monkeypatch.setattr(cli, "WATCH_SPEECH_MAX_S", 0.05)
    Fake([_quiet(), _quiet(user_speaking=True)],
         turns_at={0: [_turn(6)]}).install(monkeypatch)
    out = cli.cmd_watch(_args(timeout=30.0))

    assert out["reason"] == "ceiling" and out["finished"] is False
    assert out["count"] == 1, "the turns it already collected must survive the ceiling"
    assert "NOT permission to reply" in out["next"]


def test_a_button_moving_comes_back_with_whatever_was_collected(monkeypatch):
    """Mute, the channel, the orb, a page arriving or dying. The old code returned the button
    INSTEAD of the turns; a button can move in the same wait that carried speech."""
    Fake([_quiet(), _quiet(), _quiet(muted=True)],
         turns_at={0: [_turn(6)]}).install(monkeypatch)
    out = cli.cmd_watch(_args())

    assert out["event"] == "control" and out["changed"] == {"muted": True}
    assert [t["id"] for t in out["turns"]] == [6], "the turns must not be dropped for a button"


def test_a_dead_server_is_never_permission_to_speak(monkeypatch):
    """THE ONE PLACE THE TWO OLD CONTRACTS DISAGREED, and the merge keeps both halves.

    `watch` returned quietly with exit 0, because a caller that treats a quiet tunnel as normal
    must not see a failure; `drain` failed loudly, because an empty payload from it read as "go
    ahead and speak". So: the exit code stays 0, and `finished` goes false — which is the field
    that actually gates the agent's mouth.
    """
    Fake([{"running": False, "error": "no server", "code": "no_server"}]).install(monkeypatch)
    out = cli.cmd_watch(_args())

    assert out["reason"] == "no_server" and out["finished"] is False
    assert out.get("running") is not False, "exit code 3 would break every existing watchdog"
    assert out["listening"] is False
    assert "command-bridge serve" in out["hint"]


def test_a_second_waiter_is_refused(monkeypatch):
    """Concurrent waits on one log race for the same turns, so one cursor silently falls behind.
    Refused rather than warned: the caller is normally a watchdog executing a rule, and a rule
    that returns a warning gets followed anyway."""
    Fake([_quiet(watch_open=True)]).install(monkeypatch)
    out = cli.cmd_watch(_args())

    assert out["error"] == "a wait is already open on this session"
    assert out["reason"] == "watch_open" and out["finished"] is False
    assert "--force" in out["next"]


def test_force_gets_through_the_refusal(monkeypatch):
    Fake([_quiet(watch_open=True), _quiet()],
         turns_at={0: [_turn(6)]}).install(monkeypatch)
    assert cli.cmd_watch(_args(force=True))["finished"] is True


def test_an_absent_speech_signal_is_admitted_not_assumed(monkeypatch):
    """ABSENT IS NOT FALSE. A server predating these fields cannot answer the question, and
    silently reading that as "he is quiet" would turn the gate into a no-op that always agrees
    with the agent — worse than not checking, because the payload would still say `finished`."""
    old = {"clients": 1, "capturing": True, "muted": False, "channel_open": True,
           "watch_open": False, "verbose": False}
    Fake([old], turns_at={0: [_turn(6)]}).install(monkeypatch)
    out = cli.cmd_watch(_args())

    assert out["user_speaking"] is None
    assert "neither" in out["hint"] and "serve" in out["hint"]


# ------------------------------------------------------- the ladder that remains


def test_speech_resets_the_idle_backoff(monkeypatch):
    """The streak means "nothing has happened for a long time", and he just spoke."""
    Fake([_quiet()], turns_at={0: [_turn(6)]}).install(monkeypatch)
    cli._set_empty_streak("s", 4)
    cli.cmd_watch(_args())
    assert cli._empty_streak("s") == 0


def test_an_empty_heartbeat_advances_the_idle_backoff(monkeypatch):
    """The ladder still paces the HEARTBEAT — the wait when nothing at all is happening. What it
    no longer does is decide whether he has finished speaking."""
    Fake([_quiet()]).install(monkeypatch)
    cli._set_empty_streak("s", 2)
    out = cli.cmd_watch(_args())

    assert out["reason"] == "quiet" and out["finished"] is True
    assert cli._empty_streak("s") == 3
    assert out["quiet_rounds"] == 3


# ------------------------------------------------------------ the collapse


def test_the_waiting_command_reaches_its_handler_through_main(monkeypatch, capsys):
    """THE TEST THAT WOULD HAVE CAUGHT THE OUTAGE.

    Renaming `cmd_watch` to `cmd_wait` left the dispatch table pointing at a name that no longer
    existed. The module still IMPORTED — the table is built inside `main()` — so every test in
    this suite passed while every real invocation raised NameError, and the scheduled watchdog
    firing `command-bridge watch` every minute erred each time.

    The gap was that nothing in the suite ever called `main()` with a real argv. This does.

    It used to loop over `("watch", "drain")` and assert both names reached the same function
    object. Spec 007 removed the second name outright, so there is one name to check and the
    identity half of the assertion has nothing left to compare — what replaced it is
    `test_the_retired_name_is_not_dispatchable_at_all` below, which pins the ABSENCE.
    """
    seen = set()
    monkeypatch.setattr(cli, "cmd_watch", lambda args: seen.add(args.cmd) or {})
    assert cli.main(["watch", "--session", "s", "--since", "1"]) == cli.EXIT_OK
    capsys.readouterr()

    assert seen == {"watch"}, "the waiting command must reach its implementation THROUGH main()"


def test_every_subcommand_the_parser_offers_has_a_handler(monkeypatch):
    """The general form of the same failure. A command that parses and then cannot dispatch is a
    KeyError at the moment of use, which no import check and no unit test would have seen."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction))
    missing = []
    for name in sub.choices:
        try:
            cli.main([name, "--nonexistent-flag-to-abort-early"])
        except SystemExit:
            pass                      # argparse rejected the flag: the command exists and routes
        except KeyError:
            missing.append(name)
        except Exception:
            pass                      # anything else means it dispatched, which is the point
    assert not missing, f"parser offers commands `main` cannot dispatch: {missing}"


def test_the_watchdogs_literal_invocation_still_parses():
    """AC11. A scheduled job in the harness emits exactly this, and it cannot be updated
    atomically with the code — so renaming the waiting command is a live-traffic migration."""
    args = cli.build_parser().parse_args(["watch", "--session", "dev", "--since", "3"])
    assert args.cmd == "watch" and args.session == "dev" and args.since == 3


def test_the_wait_has_no_flag_that_changes_when_it_returns():
    """AC12/AC19/NFR1. `--timeout` bounds the IDLE heartbeat and is a fact about the caller's
    harness; nothing an agent can pass changes when the wait decides he has stopped talking.
    Adding one would recreate the defect being removed."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    flags = {o for a in sub.choices["watch"]._actions for o in a.option_strings}

    assert "--waits" not in flags and "--max-seconds" not in flags
    # `--lane` (spec 012) changes WHICH turns come back, never WHEN the wait returns. It is the
    # same kind of flag as `--all-turns`, which this set already allows for the same reason: both
    # filter the result, and neither touches the speech signals the return is gated on. The defect
    # this guard exists to prevent is a caller being able to tune the stopped-talking decision,
    # and no value of `--lane` reaches it.
    assert flags == {"-h", "--help", "--session", "--since", "--timeout", "--force",
                     "--all-turns", "--lane"}


def test_the_retired_name_is_not_dispatchable_at_all(capsys):
    """AC15/AC17. The second waiting command is GONE, not deprecated and not aliased.

    REPLACES TWO TESTS THAT PINNED ITS SURVIVAL: one asserted the retired name still parsed
    `--waits 5,3,2` and reported them as ignored, the other that an unpassed flag was not
    reported. Both were correct while the alias was being kept "for one release" so invocations
    already in circulation would not crash — and that kindness is exactly what kept the hazard
    alive. The two names ran the same code, and the second one is what led an operating guide to
    write them up as two instruments with two waiting strategies and ship a wrong rule.

    What must survive is not the command but the MIGRATION: one failed call that names its own
    replacement, rather than argparse's `invalid choice` and a guess."""
    code = cli.main(["drain", "--session", "dev", "--since", "42"])
    err = capsys.readouterr().err
    payload = json.loads(err)

    assert code == cli.EXIT_USAGE, "a command that does not exist is a usage error"
    assert payload["code"] == "unknown_command"
    assert payload["replaced_by"] == "watch"
    # THE WHOLE POINT: the same call, respelled and runnable, not a list to choose from.
    assert payload["remedy"] == "command-bridge watch --since 42"
    assert "drain" not in payload["commands"]


def test_the_retired_flags_cannot_be_typed_on_the_waiting_command(capsys):
    """They only ever parsed on the command that is gone. Accepting them on `watch` now would be
    the ladder coming back under the surviving name."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["watch", "--session", "s", "--since", "5", "--waits", "5,3,2"])


# ------------------------------------------------------------- the contract


def test_describe_offers_exactly_one_waiting_command():
    """AC16. The contract is where an agent looks for what it may run, so a retired name surviving
    ANYWHERE in this payload — as a command, an alias, an `alias_of`, a `deprecated` note — reads
    as a command that still works.

    REPLACES the test that asserted the alias entry shared `watch`'s own string objects. That was
    a good answer to the wrong question: two entries that can never drift are still two entries,
    and the defect was never that the second description went stale. It was that a second name
    existed to be chosen."""
    cmds = cli.DESCRIBE["commands"]

    assert "wait" not in cmds, "the third name he rejected"
    assert "drain" not in cmds, "and the second one, which is what this spec removed"
    assert "watch" in cmds
    # Not just absent as a KEY — absent from the whole document, including any `alias_of` or
    # `deprecated` field that would name it while claiming to retire it.
    assert "drain" not in json.dumps(cli.DESCRIBE).lower()


def test_the_rules_no_longer_ask_an_agent_to_choose_a_command():
    """AC15. Four sections of the guide existed only to teach the watch/drain distinction, and the
    distinction is what agents got wrong. A tool that cannot be used wrongly beats a rule an agent
    has to remember."""
    assert "`watch`" in cli.DESCRIBE["RULE_1"]
    assert "ONE WAITING COMMAND" in cli.DESCRIBE["RULE_2"]
    assert "GATES SPEAKING, NOT STARTING" in cli.DESCRIBE["RULE_3"]
    for rule in ("RULE_1", "RULE_2", "RULE_3"):
        assert "command-bridge drain" not in cli.DESCRIBE[rule]


def test_the_loop_runs_the_same_command_before_and_after_the_work():
    """The collapse, expressed in the loop an agent actually follows: `wait`, work, `wait`, `say`,
    `wait`. There is no second command to pick."""
    lines = cli.DESCRIBE["the_loop"]
    waits = [i for i, ln in enumerate(lines) if "command-bridge watch" in ln]
    say = next(i for i, ln in enumerate(lines) if "command-bridge say" in ln)

    assert len(waits) >= 3, "before the work, before the say, and after it"
    assert any(i < say for i in waits) and any(i > say for i in waits)
    assert not any("command-bridge drain" in ln for ln in lines)


def test_the_watchdog_prompt_emits_the_new_command():
    """AC14. The prompt is a thing to EXECUTE, not a thing to read, so it has to carry the
    spelling the tool wants to be running a release from now."""
    prompt = cli.WATCHDOG_PROMPT.format(session="dev")

    assert "command-bridge watch --since" in prompt
    assert "command-bridge drain" not in prompt
    assert "command-bridge wait" not in prompt, "the third name he rejected must not be taught"
    # WAS: `assert "deprecated alias" in prompt`, so the prompt named the old command as the one
    # on its way out. There is nothing on its way out any more — it is gone — and a scheduled
    # prompt that mentions a command which does not exist is the tool teaching a dead spelling.
    assert "drain" not in prompt.lower(), "a retired name must not survive in a prompt to EXECUTE"
    assert "no second waiting command" in prompt, (
        "say there is only one, since the reason this prompt exists is agents picking wrong"
    )


# ------------------------------------ the pre-reply check must not have a ladder either


def test_a_pre_reply_check_returns_immediately_when_he_is_quiet(monkeypatch):
    """THE CORRECTION. Removing the rungs from the SPEAKING path and leaving them on the SILENT
    path made the pause before every reply WORSE than the drain it replaced — 30 s against 10.5 s.

    Live, 2026-08-17: *"I don't like that this wait. If I say nothing, this waits for 30 seconds.
    That's slow."* And his original design statement is the acceptance test: *"the watch was going
    to hold if the client detected that I was sending audio. And if not, it was going to resolve
    immediately."*
    """
    fake = Fake([_quiet(agent_holds_turns=True)]).install(monkeypatch)
    t0 = time.monotonic()
    out = cli.cmd_watch(_args(timeout=30.0))
    elapsed = time.monotonic() - t0

    assert elapsed < 0.1, f"a pre-reply check took {elapsed:.2f}s — he will feel anything slower"
    assert fake.timeouts == [0.0], "it must not even ask the log to block"
    assert out["reason"] == "quiet" and out["finished"] is True
    assert "next_wait" not in out, "there is no next rung; nothing is being scheduled"


def test_a_pre_reply_check_holds_while_the_segmenter_hears_speech(monkeypatch):
    """Immediate means immediate ONLY when he is quiet. The whole point of checking before
    speaking is the case where he is not — and for a pre-reply check "not quiet" means the
    SEGMENTER hears speech (`speech_active`/`speech_pending`), not that the microphone caught a
    sound. The room-noise test below pins the other half."""
    fake = Fake([_quiet(agent_holds_turns=True),
                 _quiet(agent_holds_turns=True, speech_active=True),
                 _quiet(agent_holds_turns=True, speech_active=True),
                 _quiet(agent_holds_turns=True)]).install(monkeypatch)
    out = cli.cmd_watch(_args(timeout=30.0))

    assert fake.polls >= 2, "it must hold while the segmenter hears him, however urgent the reply"
    assert fake.timeouts[1] == cli.WATCH_POLL_SPEECH_S
    assert out["finished"] is True


def test_a_pre_reply_check_ignores_room_noise(monkeypatch):
    """The fix, 2026-09-04. `user_speaking` is the client's raw MICROPHONE LEVEL — it trips on a
    fan, a door, someone else talking. A pre-reply check that held on it made him *"sit here
    waiting for the agent to say whatever it needs to say"* while the room was noisy but he was
    silent. So the pre-reply gate drops amplitude and trusts the segmenter; barge-in (his
    voiceprint alone) covers the sub-second the segmenter may lag behind him. Amplitude with NO
    segmented speech now resolves as instantly as silence does.

    Every OTHER watch keeps amplitude — `test_the_leading_signal_alone_may_not_end_the_wait` is a
    plain (not holding-a-reply) watch and still holds on `user_speaking`, unchanged."""
    fake = Fake([_quiet(agent_holds_turns=True, user_speaking=True)]).install(monkeypatch)
    t0 = time.monotonic()
    out = cli.cmd_watch(_args(timeout=30.0))
    elapsed = time.monotonic() - t0

    assert elapsed < 0.1, "amplitude-only noise must not delay a held reply"
    assert fake.timeouts == [0.0], "it takes the same fast path as silence — no blocking poll"
    assert out["reason"] == "quiet" and out["finished"] is True
    assert out["user_speaking"] is True, "the noise is still REPORTED honestly, just not held on"


def test_listening_still_blocks_rather_than_spinning(monkeypatch):
    """The other half, and why this cannot simply always return immediately. An agent with
    nothing in hand is required by RULE_1 to sit in a blocking call; an instant empty return would
    make that loop a hot spin, burning a turn per iteration.

    The distinction is DERIVED from whether the agent holds unanswered turns — never a flag, which
    is a decision an agent makes wrong under time pressure."""
    fake = Fake([_quiet(agent_holds_turns=False)]).install(monkeypatch)
    out = cli.cmd_watch(_args(timeout=0.4))

    assert fake.timeouts[0] > 0, "listening BLOCKS on the log; it must not poll with timeout 0"
    assert out["reason"] == "quiet"
    assert out["next_wait"] > 0, "and the heartbeat ladder still paces this case"
    assert out["quiet_rounds"] == 1


def test_an_empty_check_ends_the_batch_so_the_next_call_blocks(monkeypatch):
    """Without this the instant-return mode latches on for an agent that consumed turns and then
    never replied. The agent asked "anything more before I speak?", the answer was no, and the
    check is done — exactly the drain loop, where turns arriving keep it going and the first empty
    round ends it."""
    fake = Fake([_quiet(agent_holds_turns=True)]).install(monkeypatch)
    cli.cmd_watch(_args(timeout=30.0))

    closes = [p for p in fake.paths if p == "/watching"]
    assert len(closes) >= 2, "it announces itself on the way in and on the way out"


def test_a_check_that_found_turns_does_not_end_the_batch(monkeypatch):
    """"Fold them in and wait again" has to stay fast. Turns arriving mean he is still going, so
    the batch continues and the next check is instant too."""
    sent = []
    fake = Fake([_quiet(agent_holds_turns=True)], turns_at={0: [_turn(6)]})
    fake.install(monkeypatch)
    real = fake.request

    def spy(session, path, payload=None):
        if path == "/watching":
            sent.append(payload)
        return real(session, path, payload)

    monkeypatch.setattr(cli, "_request", spy)
    out = cli.cmd_watch(_args(timeout=30.0))

    assert out["count"] == 1
    assert sent[-1] == {"open": False, "empty": False}, (
        "turns came back, so the agent is still holding a reply and the next check stays instant"
    )


# ------------------- what `say` does about turns the wait never picked up
#
# THESE NOW PIN A COMPATIBILITY PATH, NOT THE DESIGN. Under spec 007 a live server REFUSES rather
# than speaking and reporting afterwards, so `unread_count > 0` beside a queued clip can only come
# back from a server that predates the refusal — and one is always running somewhere, because the
# CLI is reloaded from disk on every invocation while a server keeps the code it started with.
# The branch is kept for exactly that caller, so it is kept tested. The refusal itself is pinned
# in tests/test_say_refusal.py.


def _say(monkeypatch, **server_says):
    payload = {"queued": True, "id": "clip-1", "seconds": 1.0, "held_for": 0.9,
               "held_for_speech": False, "delivered": True, "reason": None,
               "unread": [], "unread_count": 0, "cursor": 7}
    payload.update(server_says)
    monkeypatch.setattr(cli, "_request", lambda *a, **k: dict(payload))
    return cli.cmd_say(types.SimpleNamespace(session="dev", text="hi", voice=None, now=False))


def test_speaking_hands_back_what_was_never_read(monkeypatch):
    """His own proposal, 2026-08-17: *"the say command resolves, it should include in its response
    whatever I said that wasn't drained before."*

    **This makes the tunnel's central rule structural instead of remembered.** "No speech may be
    pending when you speak" used to depend on an agent choosing to run the wait first — a
    discipline, violated repeatedly in live sessions. Now the act of speaking hands back what was
    missed, so at worst the agent finds out immediately afterwards instead of never."""
    out = _say(monkeypatch, unread=[_turn(6, "and one more thing")], unread_count=1)

    assert "READ THE 1 TURN(S) IN `unread` NOW" in out["next"]
    assert "stale" in out["next"], "the reply that just went out may answer a question he left"
    assert "--since 7" in out["next"], "resume exactly where the log is, without tracking it"


def test_it_separates_a_race_from_a_skipped_check(monkeypatch):
    """`held_for` tells the two apart and they call for different reflection. Non-zero means he
    carried on talking WHILE this was composed — a race. Zero means the turns were already sitting
    there before it started — the check was skipped."""
    race = _say(monkeypatch, unread=[_turn(6)], unread_count=1, held_for=3.4,
                held_for_speech=True)
    skipped = _say(monkeypatch, unread=[_turn(6)], unread_count=1, held_for=0.9,
                   held_for_speech=False)

    assert "STILL TALKING" in race["next"]
    assert "3.4" in race["next"]
    assert "check before speaking was skipped" in skipped["next"]


def test_unread_outranks_every_other_branch(monkeypatch):
    """The other branches are about the fate of the CLIP. This one is about the agent having
    spoken without knowing what it was answering, which is the worse fact."""
    out = _say(monkeypatch, unread=[_turn(6)], unread_count=1, delivered=False,
               reason="no_client")

    assert "READ THE 1 TURN(S)" in out["next"]


def test_a_fire_and_forget_say_carries_them_too(monkeypatch):
    """`--now` is exactly the path an agent takes when it is in a hurry, which is when it skips
    the check — so it is the path that most needs this. The server samples `unread` before
    synthesis for that reason."""
    out = _say(monkeypatch, unread=[_turn(6)], unread_count=1, **{"async": True})

    assert "READ THE 1 TURN(S)" in out["next"]


def test_a_clean_reply_says_so_without_raising_the_alarm(monkeypatch):
    """A warning that fires on every reply is one nobody reads."""
    out = _say(monkeypatch)

    assert "READ THE" not in out["next"]
    assert "command-bridge watch --since 7" in out["next"]


def test_describe_documents_the_new_fields():
    """`describe` is the tie-break, so a field an agent is told to branch on has to be in it.

    THE TWO ASSERTIONS THIS DROPPED WERE ABOUT A WARNING THAT NO LONGER EXISTS. They required
    `unread` to explain why IGNORING it was safe, and `unread_count` to be the field to branch on
    before any other — both correct while `say` spoke first and reported afterwards. Under spec
    007 there is nothing to ignore and nothing to branch on: a non-empty unread set is refused
    rather than returned beside a clip, so the branch is the `code` on a failed call."""
    returns = cli.DESCRIBE["commands"]["say"]["returns"]

    for field in ("unread", "unread_count", "cursor"):
        assert field in returns, f"`say` returns {field} and describe does not mention it"
    assert "REFUSAL" in returns, "the refusal is the fact an agent most needs from this command"
    assert "NOT the cursor to use after a refusal" in returns["cursor"], (
        "the two cursors are the trap: resuming from the head of the log recovers nothing"
    )


def test_the_grace_pass_is_not_reported_as_him_talking(monkeypatch):
    """A PRE-EXISTING DEFECT, found by running a real `say` against a live server.

    The hold loop always spends SPEAK_GRACE_S re-checking before it commits, so `held_for` comes
    back at ~0.9 s on a completely clean reply — and every branch downstream tested `held_for > 0`.
    The result was that "he kept talking while you composed this, go and read what he said" fired
    on EVERY SINGLE REPLY. A warning that always fires is one nobody reads, which is the same
    defect class as a health check that can never be emptied.
    """
    clean = _say(monkeypatch, held_for=0.9, held_for_speech=False)
    real = _say(monkeypatch, held_for=0.9, held_for_speech=True)

    assert "NOW" not in clean["next"], "0.9s of grace is not evidence of anything"
    assert "clean" in clean["next"]
    assert "NOW" in real["next"], "but an observed hold still raises the alarm"


def test_describe_says_which_field_to_branch_on():
    text = cli.DESCRIBE["commands"]["say"]["returns"]["held_for_speech"]

    assert "never on `held_for > 0`" in text, "name the trap, not just the replacement"
