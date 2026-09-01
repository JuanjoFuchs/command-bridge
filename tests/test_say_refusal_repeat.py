"""A refused BATCH carries the unread turn once, not once per clip. Spec 011, FR1, AC1-AC7.

Measured 2026-08-19: four `say --now` clips were fired back to back, all four were refused, and
**each refusal carried the full text of the same ~700-character turn.** Four copies of one turn to
report one event — 6,268 characters, of which 2,625 were re-sends of something already delivered.

**The cost scales with the wrong thing.** An answer is split into clips because it is long, and
every clip after the first refusal is refused too, on the same turn. So the longer the thought the
agent was trying to deliver, the more it is charged for being interrupted — which is backwards, in
a repo whose whole premise is that the agent's context window is the budget.

Carrying the text ONCE is not the problem and is not being removed: it saves a round trip, and the
agent can usually re-compose without another call. What is removed is the second, third and fourth
copy, and the thing that decides is the pair **`(since, last_turn_id)`** — where the agent has read
to, and the head of the log. Matching the previous refusal means nothing the agent could act on has
changed. Any new turn moves the head, any successful read moves the cursor, and either one makes the
next refusal full again.

**The omission is safe only because it is never unrecoverable**, and that is what the tests below
spend most of their assertions on: the ids are still listed, `remedy` is byte-identical and still
delivers the turns in full, and the refusal VERDICT is untouched (spec 007, TC4).

NO SERVER IS STARTED HERE. A live voice session was running on session `dev` while this was written
(spec 011, TC1): the handlers and the refusal builder are driven in-process against a TunnelState of
this test's own, on a turn log under tmp_path.
"""
import asyncio
import json

import pytest

from command_bridge import config, server, store

# The population these payloads carry, measured against the live `dev` log on 2026-08-19 and
# recorded in spec 011. AC7 requires the saving at MEDIAN length, not at the flattering one — a
# repeat that only pays off on a 700-character turn would be a saving on the rare case.
MEDIAN_TURN = "so what I was thinking is we should probably just ship the smaller one now"
MEASURED_TURN = ("I have noticed now that I am using my voice tunnel, I am saving on context "
                 "window a lot and I am getting more value out of talking than by reading what "
                 "it writes, and it has been a very long back and forth, and my context window "
                 "is not growing, which is the whole point of the thing, so the question I have "
                 "for you is what about the ergonomics of the CLI for you, because I did notice "
                 "that in the rejections I think my turns were duplicated, and I do not know if "
                 "JSON is the right output, you tell me, and also make sure that when a clip "
                 "gets rejected you are not paying for the same turn four times over, because "
                 "that is the wrong incentive, and I would like a number out of this rather "
                 "than a feeling")


class _Req:
    """The four attributes `handle_say` touches, and nothing else.

    Same stub as `test_say_refusal.py` and deliberately not shared with it: a fixture module would
    let a change made for one spec's tests silently redefine the other spec's harness, and these
    two files are asserting different guarantees about the same handler.
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
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    st = server.TunnelState("t", token=None)
    st.consumed_cursor = -1
    return st


class _Synth:
    """A stand-in for `tts.synthesize` that RECORDS whether it was called.

    AC6 is "it did not speak", on the repeat as well as on the first, and the only honest way to
    assert that is on the synthesizer. A refusal that composed the audio and then declined to send
    it would return an identical payload and would still have run the engine and the hold loop for
    words that must never be made."""

    def __init__(self):
        self.calls = []

    def __call__(self, text, voice=None, speed=1.0, pause=0.0):
        self.calls.append(text)
        return b"\x00\x00" * 1000, 22050


@pytest.fixture
def synth(monkeypatch):
    fake = _Synth()
    monkeypatch.setattr(server.tts, "synthesize", fake)
    # 0.8 s of real sleeping on the path that is SUPPOSED to speak. Zeroed so the negative
    # controls cost nothing; it gates nothing this module tests.
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


def size(payload):
    """What the payload actually costs on the wire, which is the only unit AC7 accepts.

    Field presence is not the requirement — a repeat that dropped `text` and grew an explanation
    longer than the text would satisfy every structural assertion and save nothing."""
    return len(json.dumps(payload, ensure_ascii=False))


# ------------------------------------------------------- AC1: the first refusal is unchanged


def test_the_first_refusal_carries_the_turns_with_their_text(state, synth):
    """AC1. **The valuable half, and it is not being touched.**

    The turn text riding along on the refusal is what lets the agent re-compose without a round
    trip, and JJ's ruling is the reason it is there: *"It should say the operator did not hear you
    because there was this turn — process it, and if you want to restate your message, do so."*
    This spec cuts the second copy, never the first."""
    turn = spoke(state.session, MEDIAN_TURN)

    status, payload = said(state, "here is your answer")

    assert status == 428
    assert payload["code"] == config.UNREAD_REFUSAL_CODE
    assert [t["id"] for t in payload["unread"]] == [turn["id"]]
    assert [t["text"] for t in payload["unread"]] == [MEDIAN_TURN], (
        "the text, in full, on the first refusal — an agent cannot recognise a turn from a number"
    )
    assert payload["refusal_repeat"] == 0, "0 is the first, and 0 is the one that carries text"
    assert "unread_text_omitted" not in payload, (
        "nothing was omitted, so the flag must be absent rather than false — an agent branching on "
        "`.get('unread_text_omitted')` reads both the same way, but one of them is a lie"
    )


# ------------------------------------------------------- AC2: the repeat drops the text


def test_a_repeat_refusal_carries_the_ids_without_the_text(state, synth):
    """AC2. Same `(since, last_turn_id)` — the agent has read nothing and he has said nothing — so
    the text it is about to be handed is the text it was handed a moment ago.

    The ids stay, because they are what the agent recognises the turn BY on the recovery `watch`,
    and they are three characters each."""
    turn = spoke(state.session, MEDIAN_TURN)

    _, first = said(state, "clip one of my answer")
    _, second = said(state, "clip two of my answer")

    assert second["code"] == config.UNREAD_REFUSAL_CODE, "still a refusal, not a downgrade"
    assert [t["id"] for t in second["unread"]] == [turn["id"]], "the same turn, still named"
    assert all("text" not in t for t in second["unread"]), "the text is what a repeat drops"
    assert second["unread"] == [{"id": turn["id"]}], "ids alone — no half-emptied turn objects"
    assert second["refusal_repeat"] == 1
    assert second["unread_text_omitted"] is True, "the payload must SAY the text was omitted"
    assert "omitted" in second["error"], (
        "and say it in prose too: an agent meeting `unread: [{'id': 5}]` with no explanation has "
        "to guess whether the tool lost the text or withheld it, and those call for opposite moves"
    )
    assert "`remedy` still delivers it" in second["error"], (
        "the way back has to be in the same sentence as the omission"
    )
    assert first["unread"][0]["text"] == MEDIAN_TURN, "the first is unchanged by there being a second"


def test_the_repeat_counter_keeps_counting(state, synth):
    """0, 1, 2, 3 — not a boolean.

    **A rising number is itself the diagnosis**: it means the agent is retrying `say` against a
    turn it still has not read, which is the exact loop spec 011's FR2 was written about. A flag
    could not say how deep in it the caller is."""
    spoke(state.session, MEDIAN_TURN)

    seen = [said(state, f"clip {i}")[1]["refusal_repeat"] for i in range(4)]

    assert seen == [0, 1, 2, 3]
    assert all(t.get("text") is None
               for payload in [said(state, "clip 5")[1]] for t in payload["unread"])


# ------------------------------------------------------- AC3: the negative control that matters


def test_a_new_turn_between_two_refusals_brings_the_text_back(state, synth):
    """**AC3, THE NEGATIVE CONTROL, and without it AC2 proves nothing.**

    Every assertion above is satisfied by an implementation that simply stopped sending turn text
    on refusals. This is the arm that fails on one: he said something NEW between the two clips, so
    the head of the log moved, so the second refusal is about a set the agent has never been handed
    — and it must arrive in full.

    This is not a hypothetical ordering. A refused batch is refused precisely because he is
    talking, which is the state in which another turn is most likely to land mid-batch."""
    first_turn = spoke(state.session, MEDIAN_TURN)

    _, first = said(state, "clip one")
    second_turn = spoke(state.session, "actually, hang on, one more thing")
    _, second = said(state, "clip two")

    assert second["refusal_repeat"] == 0, "a new turn is a new unread set, so this is a FIRST"
    assert "unread_text_omitted" not in second
    assert [t["id"] for t in second["unread"]] == [first_turn["id"], second_turn["id"]]
    assert [t["text"] for t in second["unread"]] == [MEDIAN_TURN, "actually, hang on, one more thing"], (
        "the text of BOTH — including the one already sent, because the agent is being handed a "
        "set it has not seen and splitting it would be a worse contract than repeating it"
    )
    assert second["last_turn_id"] == second_turn["id"], "the half of the identity that moved"
    assert second["since"] == first["since"], "and the half that did not"


def test_reading_part_of_the_backlog_also_brings_the_text_back(state, synth):
    """The other half of the identity, exercised on its own.

    He said nothing new; the agent read SOME of what was outstanding. `since` moved, so the unread
    set is different from the one it was handed, so the next refusal is full again. Neither half of
    `(since, last_turn_id)` is decoration."""
    turns = [spoke(state.session, MEDIAN_TURN) for _ in range(3)]

    _, first = said(state, "clip one")
    state.consumed_cursor = turns[0]["id"]           # a partial read: one of three
    _, second = said(state, "clip two")

    assert first["refusal_repeat"] == 0 and second["refusal_repeat"] == 0
    assert len(second["unread"]) == 2, "only what is still outstanding"
    assert all("text" in t for t in second["unread"]), "a different set arrives in full"


# ------------------------------------------------------- AC4: the memo cannot be what saves it


def test_once_the_turns_are_read_there_is_no_refusal_at_all(state, synth):
    """**AC4, THE SECOND NEGATIVE CONTROL.** The repeat state must never be the thing that lets a
    `say` through.

    Refuse, refuse again (so the memo is populated and the repeat counter is up), then actually run
    the recovery — and the next `say` must not be a cheaper refusal, it must be SPEECH. A bug that
    let a high `refusal_repeat` decay into permission would be indistinguishable, from the
    payload's shape alone, from this working."""
    turn = spoke(state.session, MEDIAN_TURN)

    said(state, "clip one")
    _, repeat = said(state, "clip two")
    assert repeat["refusal_repeat"] == 1, "the memo is populated before the recovery runs"

    # What `watch` does, in-process: read from the remedy's cursor, then report having read it.
    delivered, cursor = store.watch(state.session, repeat["since"], timeout=0.0)
    assert [t["id"] for t in delivered] == [turn["id"]], (
        "the remedy delivers the turn whose text the repeat omitted — this is what makes the "
        "omission safe rather than lossy"
    )
    assert delivered[0]["text"] == MEDIAN_TURN
    asyncio.run(server.handle_consumed(_Req(state, {"cursor": cursor})))

    status, payload = said(state, "my reply, folded in")

    assert status == 200, "having done what the tool asked, the agent must be able to speak"
    assert synth.calls == ["my reply, folded in"], "and it must actually reach the synthesizer"
    assert payload["queued"] is True
    assert "code" not in payload and "refusal_repeat" not in payload, (
        "a reply that went out is not a refusal and must carry none of a refusal's fields"
    )


# ------------------------------------------------------- AC5: the recovery path is untouched


def test_a_repeat_carries_the_same_recovery_facts_as_the_first(state, synth):
    """AC5. **Only the text is dropped. Everything the agent recovers BY is byte-identical.**

    ⚠ **The spec and the output contract disagree about `error`, and this test follows the output
    contract.** Spec 011's AC5 lists `error` among the fields carrying "the same values as the
    first"; FR1's output contract requires the repeat's `error` to state that the text was
    delivered on the first refusal and that `remedy` still delivers it. They cannot both hold
    literally. Resolved in favour of the contract, because an agent handed ids with no text and no
    explanation has to guess — and the refusal SENTENCE is preserved verbatim as the prefix, so a
    caller matching on it still matches. Every field an agent recovers by is exactly equal."""
    spoke(state.session, MEDIAN_TURN)

    _, first = said(state, "clip one")
    _, second = said(state, "clip two")

    for field in ("spoke", "code", "remedy", "since", "last_turn_id", "unread_count"):
        assert second[field] == first[field], f"{field} must not move — the recovery is unchanged"
    assert second["error"].startswith(first["error"]), (
        "the refusal sentence itself is verbatim; the repeat only appends why the text is missing"
    )
    assert second["unread_count"] == len(second["unread"]) == 1, (
        "the count is of TURNS, so trimming their text must not make it disagree with the list"
    )
    assert second["remedy"] == f"voice-tunnel watch --session {state.session} --since -1"


# ------------------------------------------------------- AC6: TC4, nothing was spoken


def test_neither_a_first_nor_a_repeat_reaches_the_synthesizer(state, synth):
    """**AC6, and spec 007's TC4: FR1 changes what a repeat CARRIES, never whether it refuses.**

    Asserted on the mechanism, not on the return value. A refusal that synthesised first and threw
    the audio away would satisfy every payload assertion in this file while being exactly the bug —
    the words were still made, the hold loop still ran, and on `--now` the clip would already have
    been broadcast.

    All three of spec 007's guarantees, on both refusals: no synthesis, nothing queued, no cursor
    movement. The repeat is the one at risk, because it is the path this spec added."""
    spoke(state.session, MEDIAN_TURN)
    before = list(state.undelivered)

    for clip, expected_repeat in (("clip one", 0), ("clip two", 1), ("clip three", 2)):
        status, payload = said(state, clip)

        assert status == 428, f"{clip}: refusing is the verdict, and it does not decay"
        assert payload["refusal_repeat"] == expected_repeat
        assert synth.calls == [], f"{clip}: refuse BEFORE synthesis, never discard afterwards"
        assert state.undelivered == before, f"{clip}: nothing was made, so nothing can be waiting"
        assert state.consumed_cursor == -1, f"{clip}: refusing is not reading"

    still_there, _ = store.watch(state.session, state.consumed_cursor, timeout=0.0)
    assert [t["text"] for t in still_there] == [MEDIAN_TURN], (
        "and the turn whose text was omitted is still in the log, in full, for the next watch"
    )


def test_the_repeat_covers_fire_and_forget_too(state, synth):
    """The measured failure was four `say --now` clips, so `--now` is the path this exists for.

    Spec 007's TC2 ruling put the refusal on `--now` precisely because the hurried path is the one
    that skips checks; the repeat trimming has to follow it there or it saves nothing on the only
    case anyone has actually observed."""
    spoke(state.session, MEASURED_TURN)

    status_a, first = said(state, "clip one", **{"async": True})
    status_b, second = said(state, "clip two", **{"async": True})

    assert status_a == status_b == 428
    assert synth.calls == [], "a backgrounded synthesis is still a synthesis"
    assert "queued" not in first and "queued" not in second
    assert first["unread"][0]["text"] == MEASURED_TURN
    assert second["unread_text_omitted"] is True


# ------------------------------------------------------- AC7: measured, not merely structural


@pytest.mark.parametrize("text,label", [(MEDIAN_TURN, "median"), (MEASURED_TURN, "the measured one")])
def test_a_repeat_costs_measurably_less_than_the_first(state, synth, text, label):
    """**AC7, asserted on serialized character count rather than on field presence.**

    The whole requirement is a number. A repeat that dropped `text` and grew an explanation longer
    than the text would pass every structural assertion in this file and cost MORE, and that is not
    a theoretical failure mode — the repeat has to explain itself, and explanations are prose.

    Run at MEDIAN length as well as at the length that was measured, because a saving that only
    appears on a 700-character turn is a saving on the rare case. The median turn on the live `dev`
    log is 74 characters."""
    assert len(MEDIAN_TURN) == 74, "spec 011's measured median, and AC7's floor"
    spoke(state.session, text)

    _, first = said(state, "clip one")
    _, second = said(state, "clip two")

    assert size(second) < size(first), (
        f"{label}: a repeat that does not cost less is the whole requirement unmet — "
        f"first {size(first)} chars, repeat {size(second)}"
    )
    if text is MEASURED_TURN:
        assert size(second) < 0.6 * size(first), (
            f"on a long turn the cut has to be structural, not a rounding win — "
            f"first {size(first)}, repeat {size(second)}"
        )


def test_the_saving_scales_with_the_batch_and_not_against_it(state, synth):
    """The shape of the defect, pinned: **four clips used to cost four copies.**

    Measured 2026-08-19 at 6,268 characters for one refusal event on a ~700-character turn. The
    property that has to hold afterwards is that clips two, three and four are all cheap — not just
    clip two — because an answer long enough to need four clips is exactly when the old cost was
    worst."""
    spoke(state.session, MEASURED_TURN)

    sizes = [size(said(state, f"clip {i}")[1]) for i in range(4)]

    assert sizes[0] > sizes[1], "the first pays for the text; the rest must not"
    assert sizes[1] == sizes[2] == sizes[3], (
        "every repeat costs the same — the price of being refused must not grow with the count"
    )
    was = 4 * sizes[0]        # what four identical full refusals cost, which is what happened
    assert sum(sizes) < 0.6 * was, (
        f"spec 011's FR4 floor for a refused batch is a 40% reduction and this half of it has to "
        f"clear that on its own: {sizes} totals {sum(sizes)} against {was} before"
    )


# ------------------------------------------------- the seam, driven with no server and no HTTP


def test_the_builder_alone_produces_first_then_repeat(state):
    """**THE SEAM FR1's MEASUREMENT HARNESS DRIVES, pinned so it cannot quietly move.**

    The memo lives on the STATE OBJECT passed to the refusal builder — not in a module global and
    not in a file — so two calls with the same state and the same unread set produce first-then-
    repeat in process, with no server, no HTTP and no session directory. A global could not be
    driven by a test and would be shared by every session in the process; a file would make the
    measurement depend on the filesystem it was run against.

    This is also the reset contract: clearing `state.last_refusal` makes the next call a first
    again, which is how a harness measures the same payload both ways."""
    # `since` rides on every payload `_unread_turns` produces (spec 017 FR3) — the cursor the count
    # was measured against, so the remedy cannot be built from a different number. A hand-built
    # fixture that omits it is drifting from its producer, which is how a test starts asserting a
    # shape nothing emits.
    unread = {"unread": [{"id": 7, "text": MEDIAN_TURN}], "unread_count": 1, "cursor": 7,
              "since": 6}

    first = server._unread_refusal(state, unread)
    repeat = server._unread_refusal(state, unread)

    assert first["refusal_repeat"] == 0 and first["unread"][0]["text"] == MEDIAN_TURN
    assert repeat["refusal_repeat"] == 1 and repeat["unread"] == [{"id": 7}]
    # KEYED BY LANE (spec 018 FR4) and measured from THIS LANE'S read cursor (spec 017 FR3). It was
    # a bare tuple against the session cursor; both halves moved for the same reason, which is that
    # a refusal belongs to one agent and has to be recoverable by that agent.
    assert state.last_refusal[state.lanes.default] == (unread["since"], 7), (
        "the identity, on the state object, under the lane that was refused"
    )

    state.last_refusal = None
    assert server._unread_refusal(state, unread)["refusal_repeat"] == 0, (
        "clearing the memo makes the next call a first again"
    )


def test_the_memo_is_per_state_and_not_shared_between_sessions(state, monkeypatch, tmp_path):
    """A module-level memo would make one session's refusal silence another session's.

    Two tunnels in one process is not exotic — the test suite itself is that — and a repeat counter
    that crossed between them would omit text an agent had never been sent."""
    other = server.TunnelState("other", token=None)
    other.consumed_cursor = -1
    # `since` rides on every payload `_unread_turns` produces (spec 017 FR3) — the cursor the count
    # was measured against, so the remedy cannot be built from a different number. A hand-built
    # fixture that omits it is drifting from its producer, which is how a test starts asserting a
    # shape nothing emits.
    unread = {"unread": [{"id": 7, "text": MEDIAN_TURN}], "unread_count": 1, "cursor": 7,
              "since": 6}

    server._unread_refusal(state, unread)
    first_for_other = server._unread_refusal(other, unread)

    assert first_for_other["refusal_repeat"] == 0
    assert first_for_other["unread"][0]["text"] == MEDIAN_TURN


# ------------------------------------------------- TC3: describe is the contract


def test_the_new_fields_are_documented_in_describe():
    """AC27 for FR1's share of it, convention 3. **An agent reads `describe`, not this file.**

    A field it is never told about is a field it will not branch on — and `unread_text_omitted` is
    exactly the kind of field that has to be branched on, because ignoring it means reading a turn
    object with no `text` as a turn that was said in silence."""
    from command_bridge import cli

    say = cli.DESCRIBE["commands"]["say"]

    assert "refusal_repeat" in say["returns"]
    assert "unread_text_omitted" in say["returns"]
    assert "refusal_repeat" in say["returns"]["REFUSAL"], (
        "the payload shape a reader copies from must list it, or the shape is a lie"
    )
    assert "unread_text_omitted" in say["returns"]["REFUSAL"]
    assert "text" in say["returns"]["unread"] and "first" in say["returns"]["unread"], (
        "`unread` itself has to say that only the first refusal carries text"
    )
    registry = cli.DESCRIBE["error_codes"][config.UNREAD_REFUSAL_CODE]
    assert "refusal_repeat" in registry and "remedy" in registry, (
        "the code an agent branches on has to explain the trimming AND the way back from it"
    )
