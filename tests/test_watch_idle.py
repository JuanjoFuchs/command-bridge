"""Spec 024: a silence after my clip wakes my watch, once.

The liaison (JJ talks only to magnus) asked for "no silences": after magnus's clip finishes and he
goes quiet, the queued next topic must not wait for him to speak first. `watch --idle <s>` reports
that silence — once — and leaves the choice of what to say to the agent.

`_idle_due` is pure, so every suppression (FR5) is driven with fabricated `/status`; a few tests
drive the whole `cmd_watch` loop through a scripted fake to pin the payload, the precedence, the
fired-once record and the `--reply` exemption.
"""
import time
import types

import pytest

import command_bridge.cli as cli


@pytest.fixture(autouse=True)
def _isolated_streak(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.config, "session_dir", lambda: str(tmp_path))


NOW = 1_800_000_000.0


def _status(**kw):
    """A healthy server, orb on, magnus live and quiet, magnus's clip played 20 s before NOW."""
    base = {"clients": 1, "capturing": True, "muted": False, "channel_open": True,
            "verbose": False, "watch_open": False, "user_speaking": False,
            "speech_active": False, "speech_pending": 0, "utterance_open": False,
            "lane": "magnus", "lanes": ["magnus", "kepler"], "default_lane": "magnus",
            "lane_last_played": {"magnus": {"clip": "clip-a", "played_at": NOW - 20}},
            "lane_idle_fired": {}, "lane_unanswered": {}, "lane_holds_turns": {},
            "lane_states": {"magnus": "idle"}, "lane_waiting": {}}
    base.update(kw)
    return base


# ------------------------------------------------------------------ the pure decision


def test_due_after_the_silence_reaches_the_threshold():
    """AC3."""
    due, el, clip = cli._idle_due(_status(), "magnus", 15, NOW)
    assert due is True and clip == "clip-a" and el == pytest.approx(20)


def test_not_due_before_the_threshold():
    due, el, _ = cli._idle_due(_status(), "magnus", 30, NOW)
    assert due is False and el == pytest.approx(20)


def test_the_same_anchor_never_fires_twice():
    """AC4 / FR4."""
    s = _status(lane_idle_fired={"magnus": "clip-a"})
    assert cli._idle_due(s, "magnus", 15, NOW)[0] is False


def test_a_new_clip_re_arms_it():
    """AC5: the fired record names the OLD anchor, so the new one is free to fire."""
    s = _status(lane_idle_fired={"magnus": "clip-a"},
                lane_last_played={"magnus": {"clip": "clip-b", "played_at": NOW - 16}})
    due, _, clip = cli._idle_due(s, "magnus", 15, NOW)
    assert due is True and clip == "clip-b"


@pytest.mark.parametrize("override", [
    {"channel_open": False},                          # orb off
    {"capturing": False},                             # mic released
    {"muted": True},
    {"clients": 0},                                   # no page
    {"lane": "kepler"},                               # he is on another lane
    {"user_speaking": True},
    {"speech_active": True},
    {"speech_pending": 1},
    {"utterance_open": True},
    {"lane_unanswered": {"magnus": 4.2}},             # he spoke after my clip; I owe him
    {"lane_holds_turns": {"magnus": True}},           # an unread / unanswered batch
    {"lane_states": {"magnus": "speaking"}},          # my clip still playing
    {"lane_states": {"magnus": "synthesizing"}},
    {"lane_waiting": {"magnus": 1}},                  # my clip held, not yet heard
], ids=lambda o: next(iter(o)))
def test_every_blocker_suppresses_it(override):
    """AC6 / FR5."""
    assert cli._idle_due(_status(**override), "magnus", 15, NOW)[0] is False


def test_no_anchor_never_fires():
    """AC7: no clip of this lane ever receipted (or a restart wiped the in-memory anchor)."""
    assert cli._idle_due(_status(lane_last_played={}), "magnus", 15, NOW)[0] is False
    assert cli._idle_due(_status(lane_last_played={"kepler": {"clip": "k", "played_at": 0}}),
                         "magnus", 15, NOW)[0] is False


def test_a_cut_clip_anchors_like_any_other():
    """AC10b / KD6: the receipt the server records for a cut clip is the same shape."""
    s = _status(lane_last_played={"magnus": {"clip": "clip-cut", "played_at": NOW - 15.5}})
    assert cli._idle_due(s, "magnus", 15, NOW)[:3:2] == (True, "clip-cut")


def test_an_unknowable_speech_state_never_fires():
    """A server that publishes no speech signal cannot prove he is quiet."""
    s = _status()
    for k in ("user_speaking", "speech_active", "speech_pending", "utterance_open"):
        s.pop(k)
    assert cli._idle_due(s, "magnus", 15, NOW)[0] is False


# ------------------------------------------------------------------ the whole watch


class Fake:
    def __init__(self, statuses, turns_at=None, briefs=None):
        self.statuses = list(statuses)
        self.turns_at = dict(turns_at or {})
        self.briefs = list(briefs or [])
        self.polls = 0
        self.posts = []

    def request(self, session, path, payload=None):
        if path == "/status":
            return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        self.posts.append((path, payload))
        if path == "/briefs/take":
            out, self.briefs = self.briefs, []
            return {"context": out}
        return {}

    def watch(self, session, cursor, timeout=0.0, addressed_only=True, lane=None, default_lane=None):
        turns = self.turns_at.get(self.polls, [])
        self.polls += 1
        return (turns, turns[-1]["id"]) if turns else ([], cursor)

    def install(self, monkeypatch):
        monkeypatch.setattr(cli, "_request", self.request)
        monkeypatch.setattr(cli.store, "watch", self.watch)
        monkeypatch.setattr(cli.time, "time", lambda: NOW)
        return self


def _args(**kw):
    base = {"session": "s", "since": 5, "force": False, "timeout": 0.5, "all_turns": False,
            "lane": "magnus", "idle": 15.0}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_the_watch_returns_idle_and_records_the_anchor(monkeypatch):
    fake = Fake([_status()]).install(monkeypatch)
    out = cli.cmd_watch(_args())
    assert out["reason"] == "idle" and out["finished"] is True
    assert out["anchor_clip"] == "clip-a" and out["idle_s"] == pytest.approx(20)
    assert ("/idle/fired", {"clip": "clip-a", "lane": "magnus"}) in fake.posts
    # Spec 028: the idle `next` says `say` directly — `say` refuses if he started talking, so the
    # pre-say `--reply` watch is gone from the hint.
    assert "--idle 15" in out["next"] and "--reply" not in out["next"]


def test_without_the_flag_nothing_changes(monkeypatch):
    """AC1."""
    Fake([_status()]).install(monkeypatch)
    out = cli.cmd_watch(_args(idle=None))
    assert out["reason"] == "quiet" and "anchor_clip" not in out


def test_too_short_is_refused_with_exit_2(monkeypatch, capsys):
    """AC2."""
    Fake([_status()]).install(monkeypatch)
    out = cli.cmd_watch(_args(idle=3.0))
    assert out["code"] == "idle_too_short" and out["remedy"]
    code = cli.main(["watch", "--session", "s", "--lane", "magnus", "--since", "5", "--idle", "3"])
    assert code == 2


def test_reply_never_returns_idle(monkeypatch):
    """AC9."""
    Fake([_status()]).install(monkeypatch)
    out = cli.cmd_watch(_args(reply=True))
    assert out["reason"] != "idle"


def test_a_turn_at_the_threshold_wins_and_leaves_the_anchor_unfired(monkeypatch):
    """AC8 / FR6."""
    turn = {"id": 6, "session": "s", "text": "hey", "addressed": True, "final": True,
            "lane": "magnus"}
    fake = Fake([_status()], turns_at={0: [turn]}).install(monkeypatch)
    out = cli.cmd_watch(_args())
    assert out["reason"] == "turns"
    assert not any(p == "/idle/fired" for p, _ in fake.posts)


def test_a_brief_at_the_threshold_wins_and_leaves_the_anchor_unfired(monkeypatch):
    """AC8 / FR8: with --context, a brief landing during the silence returns `context`."""
    monkeypatch.setattr(cli, "BRIEF_SETTLE_S", 0.0)
    fake = Fake([_status(lane_briefs_pending={"magnus": 1})],
                briefs=[{"text": "[from kepler] done", "at": "x"}]).install(monkeypatch)
    out = cli.cmd_watch(_args(context=True))
    assert out["reason"] == "context"
    assert not any(p == "/idle/fired" for p, _ in fake.posts)


def test_describe_documents_the_flag_and_the_reason(capsys):
    """AC10."""
    assert cli.main(["describe"]) == 0
    text = capsys.readouterr().out
    assert "--idle" in text and "idle_too_short" in text and "anchor_clip" in text
    assert f"Recommended: {cli.config.IDLE_RECOMMENDED_S:g}" in text
