"""`watch` resumes from the LOWER of `--since` and the server's `consumed_cursor`. Spec 011, FR2.

**THE DEFECT THIS PINS IS A DEADLOCK, NOT A DUPLICATION.** Observed live 2026-08-19: `say` refused
because one addressed turn was unread, and named `--since 1353` as the remedy. The agent was
holding cursor `1354`. It ran `watch --since 1354`, which returned `quiet`, consumed nothing, and
left the refusal exactly where it was — so the next `say` refused identically, and an agent that
trusted its own cursor over the remedy string could not reason its way out.

The mechanism is that TWO cursors track ONE log and only one of them gates the refusal. The
server's `consumed_cursor` decides whether `say` refuses; the caller's `--since` decides what
`watch` delivers; and the CLI posts `/consumed` only inside `if turns:`, so a wait that hands back
nothing advances the caller and moves the server not at all.

A remedy string cannot close that, because in two of the three routes the agent behaves perfectly
and still ends up ahead — so the fix is in the tool and it is at the CURSOR rather than at the
message. `min()` is what makes it safe to ship into a running conversation (TC1/TC2): lowering the
resume point can only ever deliver MORE turns, never fewer. It cannot skip one and cannot suppress
one.

Two levels are asserted here and both are load-bearing:

* **the arithmetic** (AC10-AC14) — driven through `cmd_watch` with a scripted `/status` and a
  scripted log, so what is asserted is the cursor the command ACTUALLY resumed from, not the
  return value of a helper;
* **the loop** (AC8, AC9) — the observed sequence re-entered end to end against a temp session:
  refuse, `watch` at the agent's own higher cursor, `say`. The requirement is *"the agent can leave
  this state"*, and only a test that re-enters it can show that.

**NO SERVER IS STARTED OR CONTACTED HERE** (TC1). A live voice session was running on session `dev`
while this was written: the `/status`, `/consumed` and `/watching` calls are routed into the real
aiohttp handlers in-process, against a `TunnelState` of this test's own, on a turn log under
`tmp_path`.
"""
import asyncio
import json
import types

import pytest

from command_bridge import cli, config, server, store

# --------------------------------------------------------------------------- shared fixtures

SESSION = "clamptest"
"""**DELIBERATELY NOT `dev`.** A live conversation runs on `dev`, and `spoke()` below appends to a
real turn log on disk. `_isolated` redirects that log into `tmp_path` and pytest does order autouse
fixtures first — but a name that cannot collide is a guarantee rather than an ordering assumption,
and the failure mode of getting it wrong is test fixtures appearing in a human's conversation."""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every turn log, consumed-cursor file and backoff file this module writes lands in tmp_path.

    Not tidiness. Without it these tests write into the repo's real sessions/ directory, which is
    where a LIVE conversation's log lives — and one test's leftover backoff streak silently changes
    the next test's ceiling.
    """
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))


def _args(session=SESSION, since=0, **kw):
    base = {"session": session, "since": since, "force": False, "timeout": 0.3,
            "all_turns": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


# ------------------------------------------------------- level one: the arithmetic, scripted


def _turn(tid, text="hello", addressed=True):
    return {"id": tid, "session": SESSION, "text": text, "addressed": addressed, "final": True}


def _quiet(**kw):
    """A `/status` from a healthy server with nobody talking, carrying both cursors."""
    base = {"running": True, "clients": 1, "capturing": True, "muted": False,
            "channel_open": True, "verbose": False, "watch_open": False,
            "user_speaking": False, "speech_active": False, "speech_pending": 0,
            "agent_holds_turns": False, "consumed_cursor": 1353, "last_turn_id": 1354}
    base.update(kw)
    return base


NO_SERVER = {
    "running": False,
    "error": "no server registered for session 'dev'",
    "code": "no_server",
    "remedy": "command-bridge serve --session dev",
}
"""Exactly what `cli._request` synthesises when nothing is registered — NFR2's condition."""


class Fake:
    """A scripted `/status` and a scripted log.

    `cursors` is the record that matters: it is every cursor `store.watch` was actually asked to
    resume from, so `cursors[0]` IS the resume point. Asserting on the returned payload alone
    would pass for an implementation that reported a clamp it never applied.

    Pass `log` instead of `turns_at` to get a reader that HONOURS the cursor — every turn with a
    higher id comes back. That is what makes a full-log replay show up as turns in the payload
    rather than as a number in an assertion nobody reads.
    """

    def __init__(self, status, turns_at=None, log=None):
        self.status = status
        self.turns_at = dict(turns_at or {})
        self.log = list(log) if log is not None else None
        self.polls = 0
        self.cursors = []
        self.paths = []

    def request(self, session, path, payload=None):
        self.paths.append(path)
        if path != "/status":
            return {}
        return dict(self.status)

    def watch(self, session, cursor, timeout=0.0, addressed_only=True,
              lane=None, default_lane=None):
        # The new lane parameters are named EXPLICITLY rather than swallowed by a **kwargs
        # catch-all. This stub's value is that it fails loudly when the real signature moves under
        # it — which is exactly what it just did — and a catch-all would trade that for silence.
        self.cursors.append(cursor)
        self.lanes_seen = getattr(self, "lanes_seen", [])
        self.lanes_seen.append(lane)
        self.polls += 1
        if self.log is not None:
            fresh = [t for t in self.log if t["id"] > cursor]
            return (fresh, fresh[-1]["id"]) if fresh else ([], cursor)
        turns = self.turns_at.get(self.polls - 1, [])
        if turns:
            return turns, turns[-1]["id"]
        return [], cursor

    def install(self, monkeypatch):
        monkeypatch.setattr(cli, "_request", self.request)
        monkeypatch.setattr(cli.store, "watch", self.watch)
        return self


BEHAVIOUR = [
    # since, status,                        resumes from, clamp reported
    (1354, _quiet(consumed_cursor=1353), 1353, True),
    (1353, _quiet(consumed_cursor=1353), 1353, False),
    (1350, _quiet(consumed_cursor=1353), 1350, False),
    (-1, _quiet(consumed_cursor=1353), -1, False),
    (1354, NO_SERVER, 1354, False),
    (1354, _quiet(consumed_cursor=-1), 1354, False),      # the downward bound, AC30
    (1354, _quiet(consumed_cursor=-999), 1354, False),    # the bound is the class, not the literal
    (1354, _quiet(consumed_cursor=0), 0, True),           # zero is a REAL read position, AC32
    (-1, _quiet(consumed_cursor=-1), -1, False),          # AC30's negative control, AC31
]
"""Spec 011's FR2 test-case table, VERBATIM — nine rows, in the spec's order.

One row per line so a change to any of them is a change to a line of the spec. Anything this file
wants to cover *beyond* the spec goes in `BEHAVIOUR_EXTRA`, so "verbatim" stays a fact rather than
a claim that quietly stopped being true."""

BEHAVIOUR_EXTRA = [
    # since, status,                    resumes from, clamp reported
    (0, _quiet(consumed_cursor=0), 0, False),
]
"""Rows this file carries that the spec's table does not name.

**Equal cursors at the boundary.** Row two of the spec's table pins `--since == consumed_cursor`
at 1353; this pins the same shape at **zero**, where the downward bound lives. It is the arm that
fails if a future guard turns "zero is a real read position" into "zero always clamps to zero and
announces it" — the resume point is right either way, so only the *silence* distinguishes them."""


@pytest.mark.parametrize(("since", "status", "resumes", "reported"),
                         BEHAVIOUR + BEHAVIOUR_EXTRA)
def test_the_resume_point_is_the_lower_of_the_two_cursors(monkeypatch, since, status, resumes,
                                                          reported):
    """AC10, AC11, AC12, AC13, AC30, AC31, AC32 — the whole behaviour table, asserted on the cursor
    the command actually read from AND on whether it said so.

    Eight of the ten rows are NEGATIVE CONTROLS and they are the reason this is a table rather
    than one test: an implementation that clamped everything to `consumed_cursor` would satisfy
    row one and break rows three, four, six and seven, and one that reported the pair
    unconditionally would satisfy row one and add noise to every other call.

    **Row eight is the edge the bound was widened past** (AC32). `consumed_cursor: 0` means the
    first turn has been read, which is a real position and must still clamp — a guard written
    `<= 0` rather than `< 0` would swallow it and leave a genuine unread turn undeliverable, while
    every other row in this table stayed green.
    """
    fake = Fake(status).install(monkeypatch)

    out = cli.cmd_watch(_args(since=since))

    assert fake.cursors[0] == resumes, "the cursor it RESUMED FROM, not the one it reported"
    if reported:
        assert out["since_requested"] == since
        assert out["resumed_from"] == resumes
    else:
        assert "since_requested" not in out, "the clamp must be silent on the normal path"
        assert "resumed_from" not in out


def test_from_the_beginning_is_never_altered(monkeypatch):
    """AC11, on its own because it is a different KIND of value.

    `-1` is not a cursor that happens to be low — it is the sentinel for "replay the log", and it
    is already below every `consumed_cursor` a server can hold. Running it through `min()` would
    be harmless today and wrong the moment a server reports a cursor of `-1` of its own, which a
    fresh session does.
    """
    fake = Fake(_quiet(consumed_cursor=-1)).install(monkeypatch)

    out = cli.cmd_watch(_args(since=-1))

    assert fake.cursors[0] == -1
    assert "resumed_from" not in out and "since_requested" not in out


def test_a_server_reporting_no_read_position_does_not_replay_the_whole_log(monkeypatch):
    """**AC30 — the downward bound.** `min()` overruled, in the one place it had to be.

    A `consumed_cursor` of `-1` is not "the server has read nothing and you should catch up on
    everything". It is the sentinel `read_consumed_cursor` returns for TWO facts it cannot tell
    apart — nothing was ever read, and the cursor file was missing or unreadable — and in the
    second the server's belief is the wrong one. Honouring it would resume from the head of the
    log and pour the entire session into the caller's context, which is the exact resource spec
    011 exists to protect.

    Asserted twice over, because "expensive" is not a test failure: on the cursor `store.watch`
    was called with, and on the TURNS that came back. With a cursor-honouring reader a replay is
    not a slow pass — it is a payload carrying turns the caller had already read, and that fails.
    """
    log = [_turn(i, f"turn {i}") for i in range(1355)]
    fake = Fake(_quiet(consumed_cursor=-1), log=log).install(monkeypatch)

    out = cli.cmd_watch(_args(since=1354))

    assert fake.cursors[0] == 1354, "a server with no read position must not drag the caller back"
    assert out["turns"] == [], "and therefore nothing is replayed"
    assert "since_requested" not in out and "resumed_from" not in out, (
        "no clamp fired, so there is no correction to report"
    )


def test_from_the_beginning_survives_the_downward_bound(monkeypatch):
    """**AC31 — the negative control for AC30**, and it is the one that stops the bound from being
    implemented as "never resume from -1".

    The bound is about a `consumed_cursor` of `-1` overriding a caller who asked for something
    else. A caller who ASKED for `-1` is not being overridden — it is stating an intent, and that
    intent still has to work, or `watch --since -1` (the first call of every session, and the
    remedy a genuinely fresh refusal names) silently stops replaying anything.
    """
    log = [_turn(i, f"turn {i}") for i in range(3)]
    fake = Fake(_quiet(consumed_cursor=-1), log=log).install(monkeypatch)

    out = cli.cmd_watch(_args(since=-1))

    assert fake.cursors[0] == -1
    assert [t["id"] for t in out["turns"]] == [0, 1, 2], "from the beginning still means all of it"
    assert "since_requested" not in out and "resumed_from" not in out


def test_a_server_too_old_to_publish_the_cursor_changes_nothing(monkeypatch):
    """AC12, second arm, NFR2. ABSENT IS NOT ZERO.

    A `/status` that answers but predates `consumed_cursor` is a live server with no second
    opinion to offer. Reading the missing key as `0` would clamp every caller to the head of the
    log and replay entire conversations — the exact failure `turns_logged` already caused once.
    """
    status = _quiet()
    status.pop("consumed_cursor")
    fake = Fake(status).install(monkeypatch)

    out = cli.cmd_watch(_args(since=1354))

    assert fake.cursors[0] == 1354, "no cursor to compare against means use what you were given"
    assert "resumed_from" not in out and "since_requested" not in out


def test_both_numbers_are_published_under_names_that_cannot_be_confused(monkeypatch):
    """AC13. The correction is VISIBLE rather than magic.

    An agent whose cursor was wrong has to be able to see that it was, and by how much — otherwise
    turns arrive it did not expect and the only available explanation is that the tool is
    duplicating them. Two names, two meanings, neither of them the bare word `cursor`.
    """
    fake = Fake(_quiet(consumed_cursor=1353), turns_at={0: [_turn(1354)]}).install(monkeypatch)

    out = cli.cmd_watch(_args(since=1354))

    assert out["since_requested"] == 1354, "what you asked for"
    assert out["resumed_from"] == 1353, "what it did"
    assert out["cursor"] == 1354, "and `cursor` still means 'resume from here next time'"
    assert fake.cursors[0] == 1353
    assert out["since_requested"] != out["resumed_from"], (
        "they are published only when they differ, so equal values would mean the pair is noise"
    )


def test_the_clamp_costs_no_round_trip(monkeypatch):
    """NFR1. It reads `status_pre` — the `/status` this command already fetches for the
    concurrent-waiter guard — so the fix cannot make the hot loop chattier.

    Asserted by counting: exactly ONE `/status` is made before the wait is even announced, and it
    is the same one the concurrent-waiter guard has always made. A clamp that fetched its own
    would show up here as a second.
    """
    fake = Fake(_quiet(consumed_cursor=1353)).install(monkeypatch)

    cli.cmd_watch(_args(since=1354))

    before_announcing = fake.paths[:fake.paths.index("/watching")]
    assert before_announcing == ["/status"], (
        "one /status answers both the guard and the clamp; a second would be a new round trip"
    )


def test_describe_documents_both_new_fields(monkeypatch):
    """TC3, convention 3 — `describe` is the contract, and a field an agent is not told about is a
    field it will never branch on. The `--since` doc has to say it is a CEILING, because an agent
    reading only "cursor" has no reason to expect a different one back."""
    returns = cli.DESCRIBE["commands"]["watch"]["returns"]

    assert "since_requested" in returns and "resumed_from" in returns
    assert "consumed_cursor" in returns["resumed_from"], "name the number it is compared against"
    assert "ONLY WHEN" in returns["resumed_from"], "say that absence is the normal case"
    since_doc = cli.DESCRIBE["commands"]["watch"]["args"]["--since"]
    assert "LOWER" in since_doc and "consumed_cursor" in since_doc
    assert "resumed_from" in since_doc


# ------------------------------------------------- level two: the loop, against a real session


class _Req:
    """The four attributes the handlers touch, and nothing else.

    Deliberately not `aiohttp.test_utils.make_mocked_request`, for the reason
    tests/test_say_refusal.py gives: a stub offering exactly what the handler reads makes it
    obvious what the handler is allowed to depend on.
    """

    def __init__(self, state, body):
        self.app = {"state": state}
        self._body = body
        self.remote = "127.0.0.1"          # loopback is in the default allowlist
        self.query = {}
        self.headers = {}

    async def json(self):
        return self._body


class _Synth:
    """A stand-in for `tts.synthesize` that RECORDS whether it was called.

    AC9 ends in "and then it SPEAKS", and the only honest place to assert that is the synthesizer:
    a payload-shaped success that never reached the engine would pass a return-value check and
    still be the bug.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, text, voice=None, speed=1.0, pause=0.0):
        self.calls.append(text)
        return b"\x00\x00" * 1000, 22050


@pytest.fixture
def live(monkeypatch, _isolated):
    """A real `TunnelState` on a temp log, with the CLI's HTTP calls routed into the real handlers.

    NO SERVER IS STARTED AND NONE IS CONTACTED (TC1). `posted` records every call the CLI made, so
    "no `/consumed` was posted" — the mechanical heart of the trap — is assertable rather than
    inferred.

    Depends on `_isolated` EXPLICITLY even though it is autouse: this fixture appends to a turn log
    on disk, so the redirection into `tmp_path` has to be a stated dependency rather than a
    property of how pytest happens to order autouse fixtures.
    """
    synth = _Synth()
    monkeypatch.setattr(server.tts, "synthesize", synth)
    # The hold loop sleeps for SPEAK_GRACE_S before committing, on the path that is SUPPOSED to
    # speak. Zeroed so AC9's success arm costs nothing; it gates nothing this module tests.
    monkeypatch.setattr(config, "SPEAK_GRACE_S", 0.0)
    state = server.TunnelState(SESSION, token=None)
    state.consumed_cursor = -1
    posted: list[tuple[str, dict]] = []

    def request(session, path, payload=None):
        posted.append((path, dict(payload or {})))
        if path == "/status":
            return json.loads(asyncio.run(server.handle_status(_Req(state, {}))).text)
        if path == "/consumed":
            return json.loads(asyncio.run(server.handle_consumed(_Req(state, payload))).text)
        if path == "/watching":
            return json.loads(asyncio.run(server.handle_watching(_Req(state, payload))).text)
        return {}

    monkeypatch.setattr(cli, "_request", request)
    return types.SimpleNamespace(state=state, synth=synth, posted=posted)


def spoke(session, text, addressed=True):
    """Put one turn in the log, as if it had been said into the microphone."""
    return store.append_turn(session=session, text=text, t_start=0.0, t_end=1.0,
                             addressed=addressed,
                             reason="wake" if addressed else "not-addressed")


def said(state, text="my reply", **body):
    """Drive `/say` in-process and return `(status, payload)`."""
    resp = asyncio.run(server.handle_say(_Req(state, {"text": text, **body})))
    return resp.status, json.loads(resp.text)


def test_a_watch_ahead_of_the_server_still_delivers_the_unread_turn(live):
    """**AC8 — THE DEFECT, end to end.**

    The state: the server's `consumed_cursor` is behind an addressed turn nobody read, and the
    caller is holding a cursor AHEAD of that turn. Before the clamp this is terminal — `--since`
    at the turn's own id means `id > cursor` is false for it, so the wait returns `quiet`, no
    `/consumed` is posted, and the server's cursor never moves.

    Asserted through the watch path against a real log, not on a helper: what has to be true is
    that the TURN comes back and the READ CURSOR moves past it.
    """
    first = spoke(SESSION, "the thing he said earlier")
    live.state.consumed_cursor = first["id"]
    missed = spoke(SESSION, "wait, one more thing")          # addressed, and nobody read it

    # The caller's own cursor is already past it — the divergence, however it arose.
    ahead = missed["id"]
    stranded, _ = store.watch(SESSION, ahead, timeout=0.0, addressed_only=True)
    assert stranded == [], "precondition: the caller's own cursor delivers NOTHING. That is the trap"

    out = cli.cmd_watch(_args(since=ahead))

    assert [t["id"] for t in out["turns"]] == [missed["id"]], (
        "the turn the server is refusing over must come back, whatever cursor the caller believed"
    )
    assert out["turns"][0]["text"] == "wait, one more thing"
    assert out["cursor"] == missed["id"]
    assert live.state.consumed_cursor == missed["id"], "and the READ cursor moved past it"
    assert out["since_requested"] == ahead and out["resumed_from"] == first["id"]


def test_the_observed_loop_ends_in_a_say_that_succeeds(live):
    """**AC9 — THE LOOP ITSELF.** Refuse, `watch` at the agent's OWN cursor, `say`.

    Every assertion in AC8 is about one call. This one is about the property the spec actually
    demands — *"the agent can leave this state"* — and the only way to show that is to re-enter
    the state and walk out of it using the commands an agent would actually run: not the remedy
    string copied verbatim, but the cursor the agent is holding, which is the thing that used to
    loop forever.
    """
    read = spoke(SESSION, "earlier, and read")
    live.state.consumed_cursor = read["id"]
    missed = spoke(SESSION, "hang on, one more thing")

    status, refusal = said(live.state, "here is your answer")
    assert status == 428, "precondition: it refuses, because one addressed turn is unread"
    assert refusal["since"] == read["id"], "and the remedy names the SERVER's cursor"
    assert live.synth.calls == [], "nothing was synthesised"

    # The agent does NOT copy the remedy. It runs the wait from the cursor it is holding, which is
    # ahead of the server's — the exact move that used to return `quiet` and change nothing.
    out = cli.cmd_watch(_args(since=missed["id"]))
    assert [t["text"] for t in out["turns"]] == ["hang on, one more thing"]

    status, payload = said(live.state, "here is your answer, folded in")

    assert status == 200, "having read what it missed, the agent must be able to speak"
    assert live.synth.calls == ["here is your answer, folded in"]
    assert payload["queued"] is True


def test_route_a_advances_the_callers_cursor_while_delivering_nothing(live):
    """**AC14, first half — the reproduction, pinned.**

    This is the route that needs no mistake by anyone, which is why it must not regress. Turns the
    wake gate judged were not for the agent are CONSUMED rather than deferred (`store.turns_since`
    computes the new cursor from the full slice, before the addressed filter), so a wait whose
    only new turns are other people talking in the room comes back empty with an ADVANCED cursor —
    and posts no `/consumed`, because nothing was delivered.

    Spec 011's measured reproduction, to the number: one addressed turn and two unaddressed ones,
    `watch --since 0` returns `[]` with cursor `2` while the server stays at `0`.
    """
    mine = spoke(SESSION, "the thing I said", addressed=True)          # id 0
    live.state.consumed_cursor = mine["id"]
    spoke(SESSION, "someone else entirely", addressed=False)           # id 1
    last_room = spoke(SESSION, "and someone else again", addressed=False)   # id 2

    out = cli.cmd_watch(_args(since=mine["id"]))

    assert out["turns"] == [], "the room talking must not end the wait"
    assert out["cursor"] == last_room["id"], "but those turns ARE consumed — they never come back"
    assert live.state.consumed_cursor == mine["id"], "while the SERVER's cursor did not move"
    assert not any(path == "/consumed" for path, _ in live.posted), (
        "no `/consumed` is posted, because none were delivered — this is the whole mechanism"
    )
    assert out["cursor"] != live.state.consumed_cursor, "the divergence, with nobody at fault"


def test_route_a_divergence_stops_deciding_what_gets_read(live, monkeypatch):
    """**AC14, second half — the divergence is recovered by the clamp.**

    Route (a) leaves the caller holding a cursor the server has never heard of. The clamp does not
    reconcile the two numbers — nothing can, when the turns between them were unaddressed and were
    genuinely consumed — it makes the stale one STOP MATTERING: the next wait resumes from the
    server's cursor, so nothing the server still counts as unread can hide underneath the caller's.

    Asserted on the cursor the log was actually read from, and on the pair being published, so an
    agent can see that its own state had drifted.
    """
    mine = spoke(SESSION, "the thing I said", addressed=True)          # id 0
    live.state.consumed_cursor = mine["id"]
    spoke(SESSION, "someone else entirely", addressed=False)           # id 1
    room = spoke(SESSION, "and someone else again", addressed=False)   # id 2

    diverged = cli.cmd_watch(_args(since=mine["id"]))
    assert diverged["cursor"] == room["id"] and live.state.consumed_cursor == mine["id"]

    reads = []
    original = store.watch

    def spy(session, cursor, **kw):
        reads.append(cursor)
        return original(session, cursor, **kw)

    monkeypatch.setattr(cli.store, "watch", spy)
    out = cli.cmd_watch(_args(since=diverged["cursor"]))

    assert reads[0] == mine["id"], (
        "the second wait reads from the SERVER's cursor, not from the one route (a) handed back"
    )
    assert out["since_requested"] == room["id"]
    assert out["resumed_from"] == mine["id"]
