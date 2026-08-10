"""One watch per session, and the cursor comes from the log.

Both of these shipped as fixes for failures that had already happened, and neither had a test —
which is how the second one shipped against a server that could not even report the field it
depended on.

The failures, for whoever changes this next:

* FOUR concurrent watches accumulated over one quiet afternoon. A watchdog fired every minute,
  could not tell "nobody is listening" from "somebody is listening in another process", and
  started a new one each time. Concurrent watches on one log race for the same turns: a turn goes
  to whichever wakes first, and the other agent's cursor silently falls behind.

* A watchdog took its cursor from `turns_logged`, which counts turns the SERVER has written since
  it started — 26, against a real last id of 367. Following it would have replayed three hundred
  old turns as if they had just been spoken.
"""
import types

import voice_tunnel.cli as cli
from voice_tunnel import store


def _args(**kw):
    base = {"session": "s", "since": -1, "timeout": 1.0, "force": False}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_a_second_watch_is_refused_while_one_is_open(monkeypatch):
    """The refusal is the whole point: a warning would be followed anyway.

    The caller here is normally a watchdog executing a rule, not a person weighing advice.
    """
    monkeypatch.setattr(cli, "_request", lambda s, path, payload=None:
                        {"watch_open": True} if path == "/status" else {})
    result = cli.cmd_watch(_args())
    assert result["error"] == "a watch is already open on this session"
    assert result["watch_open"] is True
    assert "--force" in result["next"]


def test_force_overrides_the_refusal(monkeypatch):
    """A watch whose process died leaves the flag set, so there has to be a way through."""
    seen = []
    monkeypatch.setattr(cli, "_request", lambda s, path, payload=None:
                        seen.append(path) or ({"watch_open": True} if path == "/status" else {}))
    monkeypatch.setattr(cli.store, "watch", lambda *a, **k: ([], 5))
    result = cli.cmd_watch(_args(force=True, timeout=0.0))
    assert "error" not in result
    assert "/watching" in seen, "it must announce itself even when forced"


def test_an_absent_flag_does_not_block(monkeypatch):
    """ABSENT IS NOT TRUE either. A server predating the field must not lock every watch out."""
    monkeypatch.setattr(cli, "_request", lambda s, path, payload=None: {"clients": 0})
    monkeypatch.setattr(cli.store, "watch", lambda *a, **k: ([], 5))
    assert "error" not in cli.cmd_watch(_args(timeout=0.0))


def test_last_turn_id_reads_the_log_not_a_counter(tmp_path):
    """The number a fresh watcher should start from, and the reason it is published."""
    monkey = str(tmp_path)
    for i in (0, 1, 2):
        store.append_turn(session="s", text=f"t{i}", t_start=float(i), t_end=i + 1.0,
                          addressed=True, reason="wake", base=monkey)
    assert store.last_turn_id("s", base=monkey) == 2


def test_an_empty_log_is_minus_one(tmp_path):
    """-1, not 0: `watch --since -1` means 'from the beginning', so an empty log and a request
    for everything are the same request."""
    assert store.last_turn_id("nothing-here", base=str(tmp_path)) == -1
