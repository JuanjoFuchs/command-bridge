"""`next` is emitted when it changes something, not on every call. Spec 011, FR3.

**The field is load-bearing and this is not about removing it.** On 2026-08-19 `next` named the
exact command to run at a moment the agent's own reasoning was wrong, and following it was the only
thing that worked. What repeats is the RATIONALE around that command, and the measurement is what
makes it a defect rather than a preference:

| branch | chars | repeats while… |
|---|---|---|
| turns delivered, verbose off | 406 | every turn of every conversation |
| `say --now` succeeded | 226 | every clip of every answer |
| quiet watch | 51 | — already cheap, and left alone |

A three-clip answer spent 996 characters, 678 of them `next`, and 452 of those 678 were two
byte-identical copies of the same string arriving seconds apart.

So: the branch is what repeats, not the call. The first time a command takes a branch it says
everything; while the branch does not move it says the command alone. **Every `next` still carries
a runnable invocation with the session and the cursor substituted — that property is why the field
works and it is the one thing here that is never cut.**

🔴 EVERY TEST IN THIS FILE ISOLATES THE SESSION DIRECTORY, and not merely for hygiene. The repeat
memo is persisted (every CLI invocation is a fresh process, so it has to be), and it shares a file
with the watch backoff in the session dir — which on a developer machine is the LIVE session's.
A test that skipped this would reach into a conversation a person is holding.
"""
import json
import types

import pytest

import voice_tunnel.cli as cli

SESSION = "s"
CURSOR = 1354


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """No test here reads or writes the real sessions/ directory. See the module docstring."""
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))


# --------------------------------------------------------------------------- the states

def _live(**kw):
    """A `/status` snapshot from a healthy server with nobody talking."""
    base = {"clients": 1, "capturing": True, "muted": False, "channel_open": True,
            "verbose": False, "running": True, "watch_open": False,
            "user_speaking": False, "speech_active": False, "speech_pending": 0}
    base.update(kw)
    return base


def _turn(tid=CURSOR, text="hello"):
    return {"id": tid, "session": SESSION, "text": text, "addressed": True, "final": True}


# Every branch `_next_branch` can select, as (id, turns, live). The sweep in AC18 walks this, so a
# branch added without a row here is a branch nobody checked.
WATCH_BRANCHES = [
    ("no_server", [], None),
    ("no_clients", [], _live(clients=0)),
    ("channel_closed", [], _live(channel_open=False)),
    ("orb_off", [], _live(capturing=False)),
    ("muted", [], _live(muted=True)),
    ("turns_quiet", [_turn()], _live(verbose=False)),
    ("turns_verbose", [_turn()], _live(verbose=True)),
    ("quiet", [], _live()),
]

# And every branch `cmd_say`'s next-builder can select, as (id, what the server returned, --now).
_CLIP = {"queued": True, "id": "clip-1", "seconds": 1.0, "held_for": 0.9,
         "held_for_speech": False, "delivered": True, "reason": None,
         "unread": [], "unread_count": 0, "cursor": CURSOR, "running": True}

SAY_BRANCHES = [
    ("refused", {"error": "refusing", "code": cli.config.UNREAD_REFUSAL_CODE, "spoke": False,
                 "unread": [{"id": CURSOR, "text": "wait"}], "unread_count": 1,
                 "since": CURSOR - 1, "last_turn_id": CURSOR}, False),
    ("unread_race", {**_CLIP, "unread_count": 1, "held_for": 3.4, "held_for_speech": True}, False),
    ("unread_skipped", {**_CLIP, "unread_count": 1}, False),
    ("async", {**_CLIP, "async": True}, True),
    ("undelivered", {**_CLIP, "delivered": False, "reason": "no_client"}, False),
    ("held_speech", {**_CLIP, "held_for": 3.4, "held_for_speech": True}, False),
    ("clean", dict(_CLIP), False),
]


# --------------------------------------------------------------------------- the drivers

def _say(monkeypatch, server_says, now=False, session=SESSION):
    """One `say`, against a server that returns exactly `server_says`. No server is contacted."""
    monkeypatch.setattr(cli, "_request", lambda *a, **k: dict(server_says))
    return cli.cmd_say(types.SimpleNamespace(
        session=session, text="hi", voice=None, now=now))


def _watch(monkeypatch, live, turns=(), session=SESSION):
    """One `watch`, against a scripted `/status` and a scripted log. No server is contacted.

    The log hands its turns over on the first poll and nothing after, which is the shape every
    real quiet-then-speaking wait has; `timeout=0.0` keeps the whole thing in microseconds.
    """
    monkeypatch.setattr(cli, "_request",
                        lambda s, path, payload=None: dict(live) if path == "/status" else {})
    handed = {"done": False}

    def log(sess, cursor, timeout=0.0, addressed_only=True):
        if turns and not handed["done"]:
            handed["done"] = True
            return list(turns), turns[-1]["id"]
        return [], cursor

    monkeypatch.setattr(cli.store, "watch", log)
    return cli.cmd_watch(types.SimpleNamespace(
        session=session, since=CURSOR - 1, timeout=0.0, force=False, all_turns=False))


# ----------------------------------------------------------------- AC15: the first call is whole

def test_the_first_call_of_a_command_emits_the_full_guidance(monkeypatch):
    """AC15. Nothing is suppressed until something has actually been said once.

    Both commands, because the memo is per command and a rule that only ever ran on `watch` would
    pass this while `say` repeated itself all day.
    """
    watched = _watch(monkeypatch, _live(), turns=[_turn()])
    said = _say(monkeypatch, {**_CLIP, "async": True}, now=True)

    assert watched["next"] == cli._next_action([_turn()], _live(), SESSION, watched["cursor"]), (
        "the first watch of a session must say exactly what it said before spec 011"
    )
    assert "next_repeated" not in watched, "a first call is not a repeat"
    assert "fire-and-forget" in said["next"], "and neither is a first say"
    assert "next_repeated" not in said


# ------------------------------------------------------- AC16: the second call is the command

@pytest.mark.parametrize("branch, turns, live", [
    ("turns_quiet", [_turn()], _live(verbose=False)),
    ("channel_closed", [], _live(channel_open=False)),
])
def test_a_second_watch_on_the_same_branch_costs_the_command_alone(monkeypatch, branch, turns,
                                                                   live):
    """AC16. Asserted on CHARACTER COUNT and on the marker, because either one alone is weak.

    A marker with no saving is a field that lies; a saving with no marker is an agent that cannot
    tell a short answer from a changed one.
    """
    first = _watch(monkeypatch, live, turns=turns)
    second = _watch(monkeypatch, live, turns=turns)
    _, literal, _full = cli._next_branch(turns, live, SESSION, second["cursor"])

    assert "next_repeated" not in first
    assert second["next_repeated"] is True
    assert len(second["next"]) < len(first["next"]) / 2, (
        f"{branch}: {len(first['next'])} -> {len(second['next'])} is not materially shorter"
    )
    assert second["next"].startswith(literal), (
        "the short form still LEADS with the literal command rather than with an apology for "
        "being short"
    )
    assert second["next"].split()[0].islower(), (
        "and still with an imperative verb — a noun fragment reads as a label, not an order"
    )


def test_a_second_now_clip_costs_the_command_alone(monkeypatch):
    """AC16, on the branch that repeats hardest: every clip of every multi-clip answer."""
    first = _say(monkeypatch, {**_CLIP, "async": True}, now=True)
    second = _say(monkeypatch, {**_CLIP, "async": True}, now=True)
    third = _say(monkeypatch, {**_CLIP, "async": True}, now=True)

    assert "next_repeated" not in first
    assert second["next_repeated"] is True and third["next_repeated"] is True
    assert second["next"] == third["next"], "a repeat is a repeat, not a decaying series"
    assert len(second["next"]) < len(first["next"])
    spent = len(first["next"]) + len(second["next"]) + len(third["next"])
    assert spent < 3 * len(first["next"]) * 0.75, (
        f"a three-clip answer still spends {spent} characters on `next`"
    )


# --------------------------------------------------- AC17: a branch that moved is whole again

def test_a_second_call_on_a_different_branch_gets_the_reasoning_back(monkeypatch):
    """AC17, THE NEGATIVE CONTROL. Without it, AC16 passes on an implementation that simply
    shortened everything after the first call — which would be a different feature, and a worse
    one: the branch changing is the whole signal that the reasoning is new."""
    first = _watch(monkeypatch, _live(), turns=[_turn()])
    moved = _watch(monkeypatch, _live(muted=True))

    assert "next_repeated" not in moved, "the branch moved; nothing about this is a repeat"
    assert moved["next"] == cli._next_action([], _live(muted=True), SESSION, moved["cursor"])
    assert len(moved["next"]) > 120, "the full muted guidance, not a stub"
    assert first["next"] != moved["next"]


def test_the_verbose_toggle_is_its_own_branch(monkeypatch):
    """The same negative control at the resolution that actually bit. Both `turns` branches say
    "do the work first, then wait" and then say OPPOSITE things about narrating — so treating
    them as one branch would leave an agent that just flipped the toggle following the rule it
    was using before he flipped it."""
    quiet = _watch(monkeypatch, _live(verbose=False), turns=[_turn()])
    loud = _watch(monkeypatch, _live(verbose=True), turns=[_turn()])

    assert "next_repeated" not in loud
    assert "stay quiet" in quiet["next"] and "stay quiet" not in loud["next"]


# ------------------------------------- AC18: the command survives, on every branch, in both forms

def _emitted_forms(branch, literal, full):
    """The two things a branch can ever put in `next`, both through the real emitter."""
    first: dict = {}
    cli._emit_next(first, SESSION, "sweep", branch, literal, full)
    second: dict = {}
    cli._emit_next(second, SESSION, "sweep", branch, literal, full)
    return first["next"], second["next"]


def test_every_next_this_tool_emits_carries_a_runnable_command(monkeypatch):
    """AC18, THE PROPERTY THAT MUST NOT BE LOST — asserted by SWEEPING the branches.

    Checking one branch would have passed on the defect this sweep found: the `turns` branch, the
    single most-emitted string in the tool, spelled `--since <cursor>` while the function's own
    docstring promised "session and cursor already substituted" and `describe` promised "session
    and cursor filled in". The cursor was known and simply was not interpolated.
    """
    offenders = []
    for branch, turns, live in WATCH_BRANCHES:
        got, literal, full = cli._next_branch(turns, live, SESSION, CURSOR)
        assert got == branch, f"branch id drifted: expected {branch}, got {got}"
        for form, text in zip(("full", "short"), _emitted_forms(branch, literal, full),
                              strict=True):
            if f"--session {SESSION}" not in text:
                offenders.append(f"watch/{branch}/{form}: no session-substituted command")
            if "<" in text and ">" in text:
                offenders.append(f"watch/{branch}/{form}: unresolved placeholder in {text!r}")
            if "`voice-tunnel " not in text:
                offenders.append(f"watch/{branch}/{form}: nothing runnable")

    for branch, server_says, now in SAY_BRANCHES:
        for call in (1, 2):
            out = _say(monkeypatch, server_says, now=now)
            text = out["next"]
            form = "short" if out.get("next_repeated") else "full"
            if f"--session {SESSION}" not in text:
                offenders.append(f"say/{branch}/{form}: no session-substituted command")
            if "<" in text and ">" in text:
                offenders.append(f"say/{branch}/{form}: unresolved placeholder in {text!r}")
            if "`voice-tunnel " not in text:
                offenders.append(f"say/{branch}/{form}: nothing runnable")
            del call

    assert not offenders, "\n  ".join([""] + offenders)


def test_the_two_exception_payloads_also_carry_their_command(monkeypatch):
    """AC18 reaches the two `watch` exits that deliberately stay OUT of the repeat rule.

    `watch_open` says "do nothing" and `ceiling` says "this is NOT permission to reply" — both
    warn AGAINST the obvious action rather than restating the loop, so cutting their prose down to
    the bare command would leave a `next` saying the opposite of what the branch means. They are
    exceptions, not repeats; what they still owe is a runnable command, and that is checked here.
    """
    monkeypatch.setattr(cli, "_request",
                        lambda s, path, payload=None: _live(watch_open=True)
                        if path == "/status" else {})
    busy = cli.cmd_watch(types.SimpleNamespace(
        session=SESSION, since=CURSOR, timeout=0.0, force=False, all_turns=False))

    assert busy["reason"] == "watch_open"
    assert f"`voice-tunnel watch --session {SESSION} --since {CURSOR} --force`" in busy["next"]
    assert "next_repeated" not in busy

    monkeypatch.setattr(cli, "WATCH_SPEECH_MAX_S", 0.0)
    monkeypatch.setattr(cli, "_request",
                        lambda s, path, payload=None: _live(watch_open=False, speech_active=True)
                        if path == "/status" else {})
    monkeypatch.setattr(cli.store, "watch", lambda *a, **k: ([], CURSOR))
    ceiling = cli.cmd_watch(types.SimpleNamespace(
        session=SESSION, since=CURSOR, timeout=0.0, force=False, all_turns=False))

    assert ceiling["reason"] == "ceiling"
    assert f"`voice-tunnel watch --session {SESSION} --since {CURSOR}`" in ceiling["next"]
    assert "NOT permission to reply" in ceiling["next"], (
        "the one warning in this payload is the reason it exists; it may never be suppressed"
    )
    assert "next_repeated" not in ceiling


def test_the_one_placeholder_left_is_the_value_that_cannot_be_known(monkeypatch):
    """The single permitted exception, pinned so it stays the only one.

    `say` never reads the turn log, so on a payload that came back without a `cursor` there is no
    number to substitute — the code says so in its own comment, and the placeholder is the honest
    answer rather than a fabricated one. Everywhere the value IS knowable it is interpolated,
    which is what the sweep above enforces.
    """
    out = _say(monkeypatch, {**_CLIP, "cursor": None})

    assert "--since <cursor>" in out["next"]
    assert f"`voice-tunnel watch --session {SESSION} --since <cursor>`" in out["next"], (
        "even here the session is filled in and the invocation is otherwise complete"
    )


# ----------------------------------------------------------------- AC19: keyed per command

def test_the_memo_is_kept_per_command_and_not_globally(monkeypatch):
    """AC19, THE CASE A GLOBAL KEY GETS WRONG.

    A conversation alternates `watch` -> `say` -> `watch` -> `say`. One global "last branch" would
    see a change on literally every call and suppress nothing at all, so the optimisation would
    measure as a no-op in exactly the traffic it was written for.
    """
    w1 = _watch(monkeypatch, _live(), turns=[_turn()])
    s1 = _say(monkeypatch, {**_CLIP, "async": True}, now=True)
    w2 = _watch(monkeypatch, _live(), turns=[_turn()])
    s2 = _say(monkeypatch, {**_CLIP, "async": True}, now=True)

    assert "next_repeated" not in w1 and "next_repeated" not in s1
    assert w2["next_repeated"] is True, "the second watch saw the same branch as the first watch"
    assert s2["next_repeated"] is True, "and the second say the same branch as the first say"

    memo = json.loads((cli._backoff_path(SESSION))
                      and open(cli._backoff_path(SESSION), encoding="utf-8").read())
    assert memo["next_branch"] == {"watch": "turns_quiet", "say": "async"}, (
        "one slot per command, not one slot per session"
    )


# --------------------------------------------------------- AC20 / TC5: the shared file

def test_writing_the_branch_memo_preserves_the_backoff_streak():
    """AC20, direction one. The backoff paces an eight-hour quiet night; resetting it to 30 s
    because a `next` was emitted would put the tunnel back to waking every half minute, and
    nothing anywhere would report it."""
    cli._set_empty_streak(SESSION, 7)
    cli._remember_next_branch(SESSION, "watch", "quiet")

    assert cli._empty_streak(SESSION) == 7
    assert cli._last_next_branch(SESSION, "watch") == "quiet"


def test_writing_the_backoff_streak_preserves_the_branch_memo():
    """AC20, direction two — and it is the direction the old code would have failed, because
    `_set_empty_streak` wrote the file WHOLE. Every empty watch would have wiped the memo, so the
    saving would have quietly evaporated on the one command that watches most."""
    cli._remember_next_branch(SESSION, "say", "clean")
    cli._remember_next_branch(SESSION, "watch", "turns_quiet")
    cli._set_empty_streak(SESSION, 3)

    assert cli._last_next_branch(SESSION, "say") == "clean"
    assert cli._last_next_branch(SESSION, "watch") == "turns_quiet"
    assert cli._empty_streak(SESSION) == 3


def test_an_empty_watch_does_not_forget_what_say_already_said(monkeypatch):
    """AC20 at the call site rather than in the helpers, because that is where it would bite: an
    empty watch advances the streak, and the streak write used to take the whole file with it."""
    _say(monkeypatch, {**_CLIP, "async": True}, now=True)
    _watch(monkeypatch, _live())                      # quiet: advances the backoff streak
    repeat = _say(monkeypatch, {**_CLIP, "async": True}, now=True)

    assert repeat["next_repeated"] is True, "the streak write clobbered the say memo"
    assert cli._empty_streak(SESSION) == 1, "and the memo write clobbered the streak"


def test_a_garbled_file_still_reads_as_no_state(tmp_path):
    """Bookkeeping must never break a watch, and now there are two tenants that could break it."""
    (tmp_path / f"{SESSION}.watch.json").write_text("{not json", encoding="utf-8")

    assert cli._empty_streak(SESSION) == 0
    assert cli._last_next_branch(SESSION, "watch") is None


# ------------------------------------------------------ AC21: the short form is not a dead end

def test_the_short_form_says_where_the_reasoning_went(monkeypatch):
    """AC21. An agent whose context was compacted between the first call and the fifth has never
    seen the full form and cannot go back for it — so the short form has to name a command that
    still has it, not merely assert that nothing changed."""
    first = _say(monkeypatch, {**_CLIP, "async": True}, now=True)
    repeat = _say(monkeypatch, {**_CLIP, "async": True}, now=True)
    short = repeat["next"]

    assert "`voice-tunnel describe`" in short, "name the command that carries the reasoning"
    assert "see `voice-tunnel describe`" in short, (
        "with a VERB. This field is read to be executed, and a second backticked command sitting "
        "beside the first with nothing to say which one to run invites `describe` to be run as "
        "the next action, which is exactly what it is not"
    )
    assert repeat["next_repeated"] is True and "next_repeated" not in first


def test_the_tail_does_not_restate_in_prose_what_the_marker_already_says(monkeypatch):
    """THE PHRASE THAT COST A GATE, kept out by a test rather than by everyone remembering.

    The tail used to open with "same reasoning;" — sixteen characters of English saying exactly
    what `next_repeated: true` says beside it as a boolean. That is this spec's own subject
    appearing in this spec's own fix: words that buy nothing on the second occurrence, arriving on
    every second occurrence. It measured: the three-clip answer came in at 22.5% against FR4's
    pre-registered 25% floor, twenty-four characters short, and the phrase was thirty-two of them.

    So the tail may name the recovery and nothing else. Anything a machine-readable field on the
    same payload already states belongs in that field.
    """
    _say(monkeypatch, {**_CLIP, "async": True}, now=True)
    repeat = _say(monkeypatch, {**_CLIP, "async": True}, now=True)
    tail = repeat["next"].split(" — ")[-1]

    assert repeat["next_repeated"] is True, "the fact IS on the payload, in one boolean"
    assert len(tail) <= 30, f"the tail is back up to {len(tail)} characters: {tail!r}"
    for redundant in ("same reasoning", "unchanged", "has not changed", "repeat"):
        assert redundant not in tail.lower(), (
            f"the tail says {redundant!r} in prose while `next_repeated` says it as a boolean"
        )


def test_describe_is_actually_where_the_short_form_points(monkeypatch):
    """The pointer has to be true. `describe` is the contract (convention 3), so both commands
    document the field, the marker, and the rule that decides between them."""
    for command in ("watch", "say"):
        returns = cli.DESCRIBE["commands"][command]["returns"]
        assert "next_repeated" in returns, f"`{command}` can return it and describe omits it"
        assert "next_repeated" in returns["next"], "the field must point at its own marker"
        for text in (returns["next"], returns["next_repeated"]):
            assert "branch" in text, f"`{command}` documents the marker without the rule"
        assert "PER COMMAND" in returns["next"], (
            "the per-command keying is the part an agent would otherwise guess wrong"
        )


# ------------------------------------------------- FR4: the branches that were already cheap

def test_the_quiet_watch_is_left_exactly_as_it_was(monkeypatch):
    """The honest finding, protected rather than improved. A quiet `watch` spends 51 characters on
    `next` and all 51 of them are the command — there is no rationale to cut, so the short form
    would be LONGER than the full one. It must not grow, and it must not be marked."""
    first = _watch(monkeypatch, _live())
    second = _watch(monkeypatch, _live())

    assert first["next"] == second["next"] == f"run `voice-tunnel watch --session {SESSION} " \
                                              f"--since {CURSOR - 1}`"
    assert "next_repeated" not in second
    assert len(second["next"]) <= 51


def test_a_branch_whose_guidance_is_already_bare_never_pays_the_tail(monkeypatch):
    """The same rule, generalised, so the next branch somebody adds is handled without anybody
    remembering to add it to a list of "cheap" ones: the short form is emitted only when it is
    actually shorter."""
    for branch in ("orb_off", "quiet"):
        turns, live = next((t, lv) for b, t, lv in WATCH_BRANCHES if b == branch)
        _, literal, full = cli._next_branch(turns, live, SESSION, CURSOR)
        first, second = _emitted_forms(f"bare-{branch}", literal, full)

        assert first == second == full, f"{branch} grew a tail it cannot afford"


def test_no_repeat_ever_makes_a_payload_bigger(monkeypatch):
    """FOUND BY MEASURING THE PAYLOAD RATHER THAN THE STRING, which is the only place it shows.

    The first cut of this rule shortened whenever the string got shorter. On the `muted` branch
    that saved 14 characters and then spent 23 on `next_repeated`, so a payload the change existed
    to shrink came back 9 characters BIGGER — a saving smaller than its own bookkeeping. Every
    branch is swept, in serialized bytes, because the branch it happened on was not one anybody
    would have thought to check.
    """
    grew = []
    for branch, turns, live in WATCH_BRANCHES:
        first = _watch(monkeypatch, live, turns=turns) if live else None
        if first is None:               # `no_server` cannot be driven through a live `/status`
            continue
        second = _watch(monkeypatch, live, turns=turns)
        if len(json.dumps(second)) > len(json.dumps(first)):
            grew.append(f"watch/{branch}: {len(json.dumps(first))} -> {len(json.dumps(second))}")

    for branch, server_says, now in SAY_BRANCHES:
        first = _say(monkeypatch, server_says, now=now, session=f"{SESSION}-{branch}")
        second = _say(monkeypatch, server_says, now=now, session=f"{SESSION}-{branch}")
        if len(json.dumps(second)) > len(json.dumps(first)):
            grew.append(f"say/{branch}: {len(json.dumps(first))} -> {len(json.dumps(second))}")

    assert not grew, "a repeat cost MORE than the call it repeats:\n  " + "\n  ".join(grew)
