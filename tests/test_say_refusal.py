"""`say` refuses to speak over a turn nobody read. Spec 007, FR1/FR2/FR5, AC1-AC10.

JJ, 2026-08-18: *"The `say` command should exit with an error and not stream what you're saying if
there was a turn from me that you didn't see. It should say the operator did not hear you because
there was this turn — process it, and if you want to restate your message, do so."*

**THE STATE WAS ALREADY THERE; THE VERDICT IS WHAT CHANGED.** `_unread_turns` has always been
computed before synthesis, on both branches, filtered to turns actually addressed to the agent.
What the tool did with it was hand it back attached to a reply that had already gone out — a
report of the failure rather than a control against it. Three rewrites of the operating guide did
not stop agents speaking over him; the fourth restatement would not have either.

So the tests here are mostly about what does NOT happen: no synthesis, no clip, no queue entry, no
cursor movement. Every one of those is asserted on the mechanism rather than on the return value,
because a refusal that synthesised first and threw the audio away would satisfy every
return-value-only check while being exactly the bug (the words were still made, and the 15-second
hold loop still ran, and on the `--now` path the clip would already have been broadcast).

NO SERVER IS STARTED HERE. A live voice session was running on session `dev` while this was
written (spec 007, TC5): the handlers are driven in-process against a TunnelState of this test's
own, on a turn log under tmp_path.
"""
import asyncio
import json

import pytest

from command_bridge import config, server, store


class _Req:
    """The four attributes `handle_say` touches, and nothing else.

    Deliberately not `aiohttp.test_utils.make_mocked_request`: the handler reads `request.app`,
    `request.remote`, `request.query`, `request.headers` and awaits `request.json()`, and a stub
    that offers exactly those makes it obvious what the handler is allowed to depend on. A test
    that fails because the handler started reading something else is a test doing its job.
    """

    def __init__(self, state, body):
        self.app = {"state": state}
        self._body = body
        self.remote = "127.0.0.1"          # loopback is in the default allowlist
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    # No token, so `security.Gate` passes on a loopback peer. The auth path has its own suite;
    # threading a token through here would test that one twice and this one not at all.
    st = server.TunnelState("t", token=None)
    st.consumed_cursor = -1
    return st


class _Synth:
    """A stand-in for `tts.synthesize` that RECORDS whether it was called.

    AC1 is "did not speak", and the only honest way to assert that is on the synthesizer. A
    refusal that returned an error-shaped payload after synthesising would pass an assertion about
    the payload and fail the requirement.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, text, voice=None, speed=1.0, pause=0.0):
        self.calls.append(text)
        return b"\x00\x00" * 1000, 22050


@pytest.fixture
def synth(monkeypatch):
    fake = _Synth()
    monkeypatch.setattr(server.tts, "synthesize", fake)
    # The hold loop re-checks for SPEAK_GRACE_S before committing, which is 0.8 s of real sleeping
    # on the path that is SUPPOSED to speak. Zeroed so the negative control costs nothing; it
    # gates nothing this module is testing.
    monkeypatch.setattr(config, "SPEAK_GRACE_S", 0.0)
    return fake


def said(state, text="hello", **body):
    """Drive `/say` in-process and return `(status, payload)`."""
    resp = asyncio.run(server.handle_say(_Req(state, {"text": text, **body})))
    return resp.status, json.loads(resp.text)


def spoke(session, text, addressed=True):
    """Put one turn in the log, as if he had said it."""
    return store.append_turn(session=session, text=text, t_start=0.0, t_end=1.0,
                             addressed=addressed, reason="wake")


# --------------------------------------------------------------- AC1, the refusal


def test_it_refuses_and_never_reaches_the_synthesizer(state, synth):
    """AC1. Asserted on the SYNTHESIS CALL, not on the return value.

    "Did not speak" is the requirement. A refusal that still synthesised — composing the audio and
    then declining to send it — would return an identical payload and would still have run the
    engine, the 15-second hold loop and the normalisation, for words that must never be made."""
    spoke(state.session, "wait, one more thing")

    status, payload = said(state, "here is your answer")

    assert synth.calls == [], "it must refuse BEFORE synthesis, not discard afterwards"
    assert status == 428, "precondition required: read him, then retry"
    assert payload["spoke"] is False
    assert payload["code"] == config.UNREAD_REFUSAL_CODE


def test_the_refusal_carries_the_turns_and_a_runnable_watch(state, synth):
    """AC2. `{error, code, remedy}` per convention 8, plus the turns themselves so recovering
    costs no extra round trip."""
    turn = spoke(state.session, "and one more thing")

    _, payload = said(state)

    assert set(payload) >= {"error", "code", "remedy", "unread", "unread_count"}
    assert payload["unread_count"] == 1
    assert [t["id"] for t in payload["unread"]] == [turn["id"]]
    assert [t["text"] for t in payload["unread"]] == ["and one more thing"], (
        "the text, not just the ids — an agent cannot recognise a turn from a number"
    )
    assert payload["remedy"] == f"command-bridge watch --session {state.session} --since -1"
    assert "not read" in payload["error"]


def test_the_remedy_resumes_from_the_read_cursor_not_the_head_of_the_log(state, synth):
    """**THE ONE DETAIL THAT DECIDES WHETHER THE REFUSAL IS ESCAPABLE AT ALL.**

    `--since` must be the READ cursor. Resuming from `last_turn_id` — the number `_unread_turns`
    reports and the success path hands back — returns NOTHING, so nothing is marked read, so the
    cursor never moves, so the retried `say` refuses on the same turns forever. The tool would be
    handing out a remedy that cannot work.

    Verified by RUNNING the remedy's cursor through the same reader `watch` uses, and requiring
    the turns back."""
    state.consumed_cursor = 3
    for _ in range(4):
        spoke(state.session, "old")                       # ids 0..3, already read
    fresh = [spoke(state.session, "new one"), spoke(state.session, "and another")]

    _, payload = said(state)

    assert payload["since"] == 3, "the cursor he has been READ to"
    assert payload["last_turn_id"] == fresh[-1]["id"], "and the head of the log, named separately"
    assert payload["since"] != payload["last_turn_id"], "the two must not be conflated"
    assert f"--since {payload['since']}" in payload["remedy"]

    delivered, cursor = store.watch(state.session, payload["since"], timeout=0.0)
    assert [t["id"] for t in delivered] == [t["id"] for t in fresh], (
        "the remedy must actually deliver the turns it is complaining about"
    )
    assert cursor == fresh[-1]["id"]


def test_the_cli_exits_one_with_the_documented_code(monkeypatch, capsys):
    """AC3. Non-zero, and the exit code documented for it: 1, the operation failed with .error and
    .remedy in the payload. The slug is what an agent branches on; the exit code is what it can
    branch on before parsing anything."""
    from command_bridge import cli

    refusal = {"spoke": False, "error": "refusing to speak: he said 1 thing(s) you have not read.",
               "code": config.UNREAD_REFUSAL_CODE,
               "remedy": "command-bridge watch --session dev --since 4",
               "unread": [{"id": 5, "text": "wait"}], "unread_count": 1,
               "since": 4, "last_turn_id": 5}
    monkeypatch.setattr(cli, "_request", lambda *a, **k: dict(refusal))

    code = cli.main(["say", "--session", "dev", "hello"])
    payload = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_ERROR
    assert payload["code"] == config.UNREAD_REFUSAL_CODE
    assert payload["remedy"] == refusal["remedy"]
    assert "restated" in payload["next"], "his ruling: process it, then restate if you still want to"


def test_a_refusal_does_not_get_the_clip_branches(monkeypatch, capsys):
    """The branches after a `say` are all about the fate of a CLIP — held, delivered, stale. There
    is no clip, so describing one would be inventing an event."""
    from command_bridge import cli

    monkeypatch.setattr(cli, "_request", lambda *a, **k: {
        "spoke": False, "error": "refusing", "code": config.UNREAD_REFUSAL_CODE,
        "remedy": "command-bridge watch --session dev --since 4", "next": "read them",
        "unread": [{"id": 5, "text": "wait"}], "unread_count": 1, "since": 4, "last_turn_id": 5})

    cli.main(["say", "--session", "dev", "hello"])
    payload = json.loads(capsys.readouterr().out)

    assert "you spoke without them" not in payload.get("next", "")
    assert "delivered" not in payload and "held_for" not in payload


def test_the_code_slug_is_in_the_documented_code_list_and_the_commands_own_docs():
    """AC4, convention 3. An agent reads `describe`, not this file — a slug it is told to branch
    on that appears nowhere in the contract is a slug it will never write a branch for."""
    from command_bridge import cli

    assert config.UNREAD_REFUSAL_CODE in cli.DESCRIBE["error_codes"]
    assert "Exit 1" in cli.DESCRIBE["error_codes"][config.UNREAD_REFUSAL_CODE]
    say = cli.DESCRIBE["commands"]["say"]
    assert config.UNREAD_REFUSAL_CODE in say["returns"]["REFUSAL"]
    assert config.UNREAD_REFUSAL_CODE in say["notes"]
    assert "unread_turns" in cli.DESCRIBE["exit_codes"]["1"], (
        "the exit code has to say that a refusal lands here, or `1` reads as a malfunction"
    )


def test_no_flag_anywhere_on_say_disables_the_check():
    """AC5. **A prohibition with no test can only ever be observed failing.**

    Asserted over the PARSER, so adding the flag later fails here rather than in a live session.
    FR2's reasoning: a bypass would be reached for under exactly the conditions the check exists
    for, which is why this repo has already deleted one such flag from another queue."""
    import argparse as _argparse

    from command_bridge import cli
    sub = next(a for a in cli.build_parser()._actions
               if isinstance(a, _argparse._SubParsersAction))
    flags = {o for a in sub.choices["say"]._actions for o in a.option_strings}

    # `--lane` (spec 012) was added and given exactly the deliberate look this guard demands.
    # It is NOT a way around the refusal, and the reason is structural rather than a promise:
    # every branch it opens on the server RETURNS AN ERROR — `unknown_lane` or `off_lane` — so it
    # can only ever ADD a way to be refused. No value of it reaches synthesis, and the unread
    # check below it still runs on every path that does. It makes `say` strictly harder to use,
    # which is the opposite of the thing this test is guarding against.
    # `--timings` (spec 020) was given the same look. It is NOT a bypass, and again structurally:
    # it is read into a local at the top of the handler beside `async`, is never consulted by any
    # guard, and only chooses WHICH synthesis function runs once every refusal has already been
    # passed. It cannot reach a branch the check does not sit above, and it adds no branch of its
    # own. What it changes is the SHAPE OF THE RESPONSE after a clip exists.
    assert flags == {"-h", "--help", "--session", "--voice", "--now", "--lane", "--timings"}, (
        "a new flag on `say` needs a deliberate look: is it a way around the refusal?"
    )
    for banned in ("--force", "--anyway", "--no-check", "--skip-unread", "--ignore-unread",
                   "--yes", "--override"):
        assert banned not in flags


def test_fire_and_forget_is_refused_on_the_same_condition(state, synth):
    """AC6, and the TC2 ruling. `--now` returns before synthesis on purpose, which makes it the
    path an agent takes when it is in a hurry — which is when the check gets skipped. The unread
    set was ALREADY sampled before this branch, with a comment saying exactly that; exempting it
    would put the hole where the code says it is most likely to be used."""
    spoke(state.session, "hang on")

    status, payload = said(state, "the answer", **{"async": True})

    assert status == 428
    assert payload["code"] == config.UNREAD_REFUSAL_CODE
    assert synth.calls == [], "a backgrounded synthesis is still a synthesis"
    assert "queued" not in payload


def test_nothing_is_queued_and_the_cursor_does_not_move(state, synth):
    """AC7, TC3. The undelivered queue is load-bearing — a reply said to a locked phone is held
    until he is back — and the refusal must not become a new way for words to enter it, nor a new
    way for a turn to be marked read without anybody reading it."""
    spoke(state.session, "wait")
    before = list(state.undelivered)

    said(state)

    assert state.undelivered == before, "nothing was made, so nothing can be waiting"
    assert state.consumed_cursor == -1, "refusing is not reading"
    still_there, _ = store.watch(state.session, state.consumed_cursor, timeout=0.0)
    assert len(still_there) == 1, "the next watch must still return the turn"


def test_a_refused_say_leaves_the_agent_holding_its_turns(state, synth):
    """Speaking is what ends a batch — the agent has answered, so its next wait is a LISTEN. A
    refusal answered nothing, so it must not clear the flag: the next wait is still the instant
    pre-reply check, which is what the agent needs after reading what it missed."""
    state.agent_holds_turns = True
    spoke(state.session, "wait")

    said(state)

    assert state.agent_holds_turns is True


def test_running_the_remedy_clears_the_refusal(state, synth):
    """**THE LOOP HAS TO CLOSE.** Every other test here checks that the tool says no; this one
    checks that its own instructions get to yes.

    A control an agent cannot escape by following the remedy is not a control, it is a deadlock —
    and this one is a plausible deadlock rather than a theoretical one, because the obvious cursor
    to put in the remedy (`last_turn_id`, which is what the success path returns) delivers nothing
    and therefore moves nothing.

    So: refuse, run exactly what the payload said to run, then speak."""
    spoke(state.session, "one more thing before you answer")

    status, refusal = said(state, "my reply")
    assert status == 428 and synth.calls == []

    # What `watch` does, in-process: read from the remedy's cursor, then report having read it.
    delivered, cursor = store.watch(state.session, refusal["since"], timeout=0.0)
    assert delivered, "the remedy's cursor must actually hand the turns over"
    asyncio.run(server.handle_consumed(_Req(state, {"cursor": cursor})))

    status, payload = said(state, "my reply, folded in")

    assert status == 200, "having done what it asked, the agent must be able to speak"
    assert synth.calls == ["my reply, folded in"]
    assert payload["queued"] is True


# ------------------------------------------------- the negative controls


def test_with_nothing_unread_it_speaks(state, synth):
    """**AC8, THE NEGATIVE CONTROL, and without it AC1 proves nothing.**

    Every refusal assertion above is satisfied by a `say` path that is simply broken — one that
    never synthesises under any condition would pass all of them. This is the arm that fails if
    the tool has stopped talking."""
    turn = spoke(state.session, "already read this")
    state.consumed_cursor = turn["id"]

    status, payload = said(state, "here is your answer")

    assert status == 200
    assert synth.calls == ["here is your answer"], "it must actually speak when it may"
    assert payload["queued"] is True
    assert "code" not in payload


def test_muted_with_nothing_unread_still_speaks(state, synth):
    """AC9, first arm, TC1. He asked: *"if I am muted, it means that there is no turn from me
    that's pending to be read, right?"* — and structurally yes: a muted microphone sends no frames,
    so no utterance closes and no turn is appended. Muting cannot MANUFACTURE an unread turn, and
    must not be mistaken for one."""
    turn = spoke(state.session, "said before the mute, and read")
    state.consumed_cursor = turn["id"]
    state.muted = True

    status, payload = said(state, "answering you")

    assert status == 200 and synth.calls == ["answering you"]
    assert payload["queued"] is True


def test_muting_does_not_forgive_a_turn_said_before_it(state, synth):
    """AC9, second arm, and the half that would rot silently. Muting cannot manufacture an unread
    turn — but it must not FORGIVE one either. Those words were still said and are still unread,
    and the microphone being off since then changes nothing about that."""
    spoke(state.session, "said while the mic was live")
    state.muted = True

    status, payload = said(state, "talking over him anyway")

    assert status == 428, "a muted microphone is not permission to speak over what he already said"
    assert synth.calls == []
    assert payload["unread_count"] == 1


def test_room_speech_that_never_passed_the_wake_gate_causes_no_refusal(state, synth):
    """AC10. Reported on the move, 2026-08-15, with other people talking around him: turns the gate
    judged were not for the agent already carry `addressed: false`. They advance the cursor and
    end no wait — and they must not be able to gag the agent either, or a conversation in the room
    becomes a mute button nobody pressed."""
    spoke(state.session, "someone else entirely", addressed=False)

    status, payload = said(state, "answering the person I am talking to")

    assert status == 200, "an unaddressed turn is not a turn you owe him an answer for"
    assert synth.calls == ["answering the person I am talking to"]
    assert payload["unread_count"] == 0
