"""An empty watch is not permission to speak.

`drain` exists because the three steps between a turn landing and a reply being spoken were prose,
and prose is exactly as reliable as the next agent's memory. Live on 2026-08-14 the owner was
interrupted four times, twice while the previous interruption was being fixed. The steps:

* re-watch on SHORT, COLLAPSING ceilings rather than the 30s-doubling backoff, which is right for
  waiting on a conversation to start and wrong inside one;
* check whether he is speaking after EVERY empty round, because an empty watch says only that he
  had not started the next sentence — a breath between clauses looks the same from the log;
* look once more in the gap that opens while the reply is composed, which is what the shortest
  final rung buys.

Every test here is one of those, or one of the ways the loop must refuse to lie about them.
"""
import time
import types

import pytest

import voice_tunnel.cli as cli


def _args(**kw):
    base = {"session": "s", "since": 5, "waits": "8,4,2", "max_seconds": 30.0, "force": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


def _quiet(**kw):
    """A status snapshot from a healthy server with nobody talking."""
    base = {"clients": 1, "capturing": True, "muted": False, "channel_open": True,
            "verbose": False, "watch_open": False, "user_speaking": False,
            "speech_active": False}
    base.update(kw)
    return base


def _empty_watch(**kw):
    base = {"turns": [], "cursor": 5, "count": 0, "listening": True, "verbose": False}
    base.update(kw)
    return base


def _turn(tid, text="hello"):
    return {"id": tid, "session": "s", "text": text, "addressed": True, "final": True}


class Fake:
    """A scripted `watch` and a scripted `/status`, plus the timeouts drain asked for.

    The timeouts are the interesting record: the ladder is the entire mechanism, and asserting on
    the RETURN value alone would pass for an implementation that waited thirty seconds a rung.
    """

    def __init__(self, watches, statuses, sleep=0.0):
        self.watches = list(watches)
        self.statuses = list(statuses)
        self.timeouts = []
        self.sleep = sleep

    def watch(self, args):
        self.timeouts.append(round(args.timeout, 3))
        if self.sleep:
            time.sleep(self.sleep)
        return self.watches.pop(0) if self.watches else _empty_watch()

    def request(self, session, path, payload=None):
        if path != "/status":
            return {}
        return self.statuses.pop(0) if self.statuses else _quiet()

    def install(self, monkeypatch):
        monkeypatch.setattr(cli, "cmd_watch", self.watch)
        monkeypatch.setattr(cli, "_request", self.request)
        return self


# --------------------------------------------------------------- the ladder


def test_it_collapses_and_returns_only_after_the_whole_sequence(monkeypatch):
    """Three rungs, shortening, and nothing said until all three come back empty.

    A single empty watch used to be the whole test an agent applied before replying. It is the
    weakest evidence available: it means he had not started the next sentence inside that window.
    """
    fake = Fake([_empty_watch()] * 3, [_quiet()] * 4).install(monkeypatch)
    out = cli.cmd_drain(_args())
    assert fake.timeouts == [8.0, 4.0, 2.0]
    assert out["finished"] is True and out["reason"] == "finished"
    assert out["rounds"] == 3
    assert "voice-tunnel say" in out["next"]


def test_the_ladder_ends_on_its_shortest_rung(monkeypatch):
    """The last look must be the freshest one — that gap is where the interruptions happened."""
    fake = Fake([_empty_watch()] * 3, [_quiet()] * 4).install(monkeypatch)
    cli.cmd_drain(_args())
    assert fake.timeouts == sorted(fake.timeouts, reverse=True)
    assert fake.timeouts[-1] == min(cli.DRAIN_WAITS_S)


def test_still_speaking_resets_the_collapse(monkeypatch):
    """AN EMPTY WATCH IS NOT PERMISSION TO SPEAK — this is the check that makes that true.

    The watch comes back empty every single time here. The only thing separating "he paused" from
    "he is mid-sentence" is the speech signal, and finding it set must start the ladder over
    rather than let it keep collapsing towards a reply.
    """
    fake = Fake(
        [_empty_watch()] * 5,
        [_quiet(),                       # the pre-flight guard check
         _quiet(user_speaking=True),     # after rung 1: he is talking -> reset
         _quiet(), _quiet(), _quiet()],
    ).install(monkeypatch)
    out = cli.cmd_drain(_args())
    assert fake.timeouts == [8.0, 8.0, 4.0, 2.0], "the ladder must restart, not continue"
    assert out["finished"] is True
    assert out["user_speaking"] is False


def test_either_speech_signal_holds_the_reply(monkeypatch):
    """The server's own VAD counts too. It lags the client's microphone reading by a buffer and a
    hop, and that lag is what let a reply land on top of him — so the two are additive."""
    fake = Fake(
        [_empty_watch()] * 5,
        [_quiet(), _quiet(speech_active=True), _quiet(), _quiet(), _quiet()],
    ).install(monkeypatch)
    assert cli.cmd_drain(_args())["finished"] is True
    assert fake.timeouts == [8.0, 8.0, 4.0, 2.0]


def test_turns_restart_the_ladder_instead_of_ending_the_drain(monkeypatch):
    """One thought arrives as several turns (RULE_2). Returning on the first answers the wrong
    question, and turns arriving are proof the evidence for silence was wrong."""
    fake = Fake(
        [_empty_watch(turns=[_turn(6)], count=1, cursor=6)] + [_empty_watch(cursor=6)] * 3,
        [_quiet()] * 5,
    ).install(monkeypatch)
    out = cli.cmd_drain(_args())
    assert fake.timeouts == [8.0, 8.0, 4.0, 2.0]
    assert out["count"] == 1 and out["turns"][0]["id"] == 6
    assert out["cursor"] == 6, "the next round must resume from the cursor it was handed"


def test_every_round_of_turns_comes_back_not_just_the_last(monkeypatch):
    """The drain is the only holder of the earlier rounds — nothing else will replay them."""
    Fake(
        [_empty_watch(turns=[_turn(6)], count=1, cursor=6),
         _empty_watch(turns=[_turn(7), _turn(8)], count=2, cursor=8)] + [_empty_watch(cursor=8)] * 3,
        [_quiet()] * 5,
    ).install(monkeypatch)
    out = cli.cmd_drain(_args())
    assert [t["id"] for t in out["turns"]] == [6, 7, 8]
    assert out["count"] == 3


# ------------------------------------------------------------- honest exits


def test_the_ceiling_is_reported_as_itself(monkeypatch):
    """`finished: false` when time ran out. The ceiling exists so the command written to stop an
    agent interrupting cannot become the hang that stops it answering — but a drain that ended
    because the clock did is NOT the same fact as silence, and must never read as permission."""
    Fake([_empty_watch(turns=[_turn(6)], count=1, cursor=6)] + [_empty_watch(cursor=6)] * 9,
         [_quiet()] * 10, sleep=0.06).install(monkeypatch)
    out = cli.cmd_drain(_args(max_seconds=0.1))
    assert out["reason"] == "ceiling" and out["finished"] is False
    assert out["count"] == 1, "the turns it already collected must survive the ceiling"
    assert "NOT permission to reply" in out["next"]


def test_a_button_moving_comes_straight_back(monkeypatch):
    """Mute, the channel, the orb, a page arriving or dying — none of them are answered by
    waiting another two seconds, and `watch` already returns for them."""
    Fake([_empty_watch(event="control", changed={"muted": True})],
         [_quiet(), _quiet(muted=True)]).install(monkeypatch)
    out = cli.cmd_drain(_args())
    assert out["reason"] == "control" and out["finished"] is False
    assert out["changed"] == {"muted": True}
    assert "voice-tunnel" in out["next"]


def test_a_second_watch_is_refused_in_the_same_words_watch_uses(monkeypatch):
    """A drain IS a run of watches, so it races a concurrent watcher for the same turns — and
    discovering that three rungs in means turns the other watcher will never see."""
    Fake([], [_quiet(watch_open=True)]).install(monkeypatch)
    out = cli.cmd_drain(_args())
    assert out["error"] == "a watch is already open on this session"
    assert out["reason"] == "watch_open" and out["finished"] is False
    assert "drain" in out["next"] and "--force" in out["next"]


def test_force_gets_through_the_refusal(monkeypatch):
    Fake([_empty_watch()] * 3, [_quiet(watch_open=True)] + [_quiet()] * 4).install(monkeypatch)
    assert cli.cmd_drain(_args(force=True))["finished"] is True


def test_a_dead_server_fails_instead_of_reporting_silence(monkeypatch):
    """Unlike `watch`, which treats a quiet tunnel as normal. An empty payload from THIS command
    reads as "he has finished, go ahead" — saying that on zero seconds of evidence is the exact
    failure it was written to prevent. `running: False` exits 3."""
    Fake([], [{"running": False, "error": "no server registered for session 's'",
               "code": "no_server", "remedy": "start it"}]).install(monkeypatch)
    out = cli.cmd_drain(_args())
    assert out["reason"] == "no_server" and out["finished"] is False
    assert out["running"] is False and out["code"] == "no_server"
    assert out["remedy"]


def test_an_absent_speech_signal_is_admitted_not_assumed(monkeypatch):
    """ABSENT IS NOT FALSE. A server predating these fields cannot answer the question, and
    silently reading that as "he is quiet" would turn the whole command into a no-op that always
    agrees with the agent — worse than not checking, because the payload would still say
    `finished`."""
    old = {"clients": 1, "capturing": True, "muted": False, "channel_open": True,
           "watch_open": False}
    Fake([_empty_watch()] * 3, [old] * 4).install(monkeypatch)
    out = cli.cmd_drain(_args())
    assert out["user_speaking"] is None
    assert "neither" in out["hint"] and "serve" in out["hint"]


def test_muted_is_not_mid_sentence():
    """Muting stops frames arriving, so nothing closes the open utterance and `speech_active`
    stays stuck true from the last frame before the mute. A drain that believed it would sit out
    its whole ceiling while he watched with his microphone off. Live, 2026-08-01: "whenever I
    mute, you say that you're listening and you're waiting for me to finish, but I'm muted."
    """
    assert cli._still_talking({"muted": True, "speech_active": True}) is False
    assert cli._still_talking({"muted": False, "speech_active": True}) is True
    assert cli._still_talking({"muted": False, "user_speaking": True}) is True
    assert cli._still_talking({"muted": False}) is None
    assert cli._still_talking(None) is None


# ---------------------------------------------------------------- plumbing


def test_the_drains_short_rungs_do_not_inflate_the_watch_backoff(tmp_path, monkeypatch):
    """The backoff streak means "nothing has happened for a long time". A drain's empty rungs are
    the tail of a thought he is in the middle of, and left alone three of them would tell the next
    `watch` to open with a four-minute ceiling one second after he stopped talking.
    """
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))
    Fake([_empty_watch()] * 3, [_quiet()] * 4).install(monkeypatch)
    cli._set_empty_streak("s", 4)
    cli.cmd_drain(_args())
    assert cli._empty_streak("s") == 4, "a drain must leave the streak as it found it"


def test_a_turn_clears_the_streak(tmp_path, monkeypatch):
    """He spoke, so the tunnel is demonstrably not idle."""
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))
    Fake([_empty_watch(turns=[_turn(6)], count=1, cursor=6)] + [_empty_watch(cursor=6)] * 3,
         [_quiet()] * 5).install(monkeypatch)
    cli._set_empty_streak("s", 4)
    cli.cmd_drain(_args())
    assert cli._empty_streak("s") == 0


@pytest.mark.parametrize("bad", ["", "8,x,2", "8,0,2", "8,-4"])
def test_a_bad_ladder_is_rejected_with_the_spelling(bad):
    """Rejected rather than repaired: every plausible repair is wrong in a way the caller cannot
    see, and an empty ladder is a drain that returns instantly — the "he must be finished"
    mistake this command exists to stop."""
    with pytest.raises(ValueError) as exc:
        cli._parse_waits(bad)
    assert "--waits" in str(exc.value)


def test_the_default_ladder_comes_from_the_constant():
    assert cli._parse_waits(None) == list(cli.DRAIN_WAITS_S)
    assert cli._parse_waits("8,4,2") == [8.0, 4.0, 2.0]


# ------------------------------------------------------------ the contract


def test_describe_carries_the_rule_and_points_at_the_command():
    """RULE_3 is the only one of the three that could be enforced by code, so it is — and the rule
    exists to point at the command rather than to be remembered."""
    assert "drain" in cli.DESCRIBE["RULE_3"]
    assert "not permission to speak" in cli.DESCRIBE["RULE_3"].lower()
    assert "finished" in str(cli.DESCRIBE["commands"]["drain"]["returns"])


def test_the_loop_puts_drain_between_watch_and_say():
    """It is the step between a turn landing and a reply being spoken; anywhere else in the list
    describes a different loop from the one that works."""
    lines = cli.DESCRIBE["the_loop"]
    first = next(i for i, ln in enumerate(lines) if "voice-tunnel watch" in ln)
    drain = next(i for i, ln in enumerate(lines) if "voice-tunnel drain" in ln)
    say = next(i for i, ln in enumerate(lines) if "voice-tunnel say" in ln)
    assert first < drain < say
