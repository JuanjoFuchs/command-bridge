"""Graceful shutdown: a RESTART must not look like a crash to the other agents (JJ, 2026-09-22,
dev.jsonl turns 6251-6253).

The other agents don't hold a live push — they poll HTTP and read turns off disk — so the signal is
the runtime file: `handle_shutdown` stamps `restarting_until`, `_request`'s unreachable path reads it
and returns `server_restarting` (which `cmd_watch` surfaces as a re-arm hint, not "server down"), and
the incoming server's `write_runtime` overwrites the whole file so the mark clears itself. These pin
the marker logic without an actual restart (the end-to-end is verified live).
"""
import time

from command_bridge import cli


class _Talker:
    """Stands in for TunnelState: `talking` answers from a script of booleans, one per poll."""

    session = "t"

    def __init__(self, script):
        self.script = list(script)
        self.polls = 0

    def talking(self, *, amplitude=True):
        assert amplitude is False, "the shutdown wait must use the segmenter, not the mic level"
        self.polls += 1
        return self.script.pop(0) if self.script else False


def test_a_restart_waits_for_him_to_stop_speaking(monkeypatch):
    """JJ, 2026-10-05: *"Magnus restarted the server so that whole thing was lost."* The wait holds
    while he is talking and only releases after a quiet stretch, so his sentence is not cut."""
    import asyncio

    from command_bridge import config, server
    monkeypatch.setattr(config, "SHUTDOWN_QUIET_S", 0.3)
    st = _Talker([True] * 8)                       # ~0.8 s of speech, then quiet
    t0 = time.monotonic()
    asyncio.run(server._wait_for_him_to_finish(st))
    took = time.monotonic() - t0
    assert st.polls > 8, "it stopped polling while he was still talking"
    assert took >= 0.8 + 0.3 - 0.05


def test_the_wait_is_capped_so_noise_cannot_hold_a_restart_forever(monkeypatch):
    import asyncio

    from command_bridge import config, server
    monkeypatch.setattr(config, "SHUTDOWN_SPEECH_WAIT_S", 0.5)
    monkeypatch.setattr(server.timing, "stamp", lambda *a, **k: None)
    st = _Talker([True] * 10_000)
    t0 = time.monotonic()
    asyncio.run(server._wait_for_him_to_finish(st))
    assert time.monotonic() - t0 < 1.5


def test_mark_restarting_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    cli.write_runtime("dev", "127.0.0.1", 8765, "tok")
    cli.mark_restarting("dev", 20)
    rt = cli.read_runtime("dev")
    assert rt is not None
    assert rt["restarting_until"] > time.time()
    # the rest of the runtime is preserved (merge, not overwrite)
    assert rt["port"] == 8765 and rt["token"] == "tok"


def test_write_runtime_clears_the_mark(monkeypatch, tmp_path):
    """The incoming server's write_runtime is what ends the restart window — no separate cleanup."""
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    cli.write_runtime("dev", "127.0.0.1", 8765, "tok")
    cli.mark_restarting("dev", 20)
    cli.write_runtime("dev", "127.0.0.1", 8765, "tok")   # the new server coming up
    rt = cli.read_runtime("dev")
    assert rt is not None and "restarting_until" not in rt


def test_mark_restarting_without_runtime_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    cli.mark_restarting("dev", 20)          # no runtime file -> no crash, no file conjured
    assert cli.read_runtime("dev") is None


def test_request_reads_the_restart_marker(monkeypatch, tmp_path):
    """A dead port + a live mark reads as 'restarting', not 'unreachable'."""
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    cli.write_runtime("dev", "127.0.0.1", 59999, "tok")   # nothing listens there
    cli.mark_restarting("dev", 20)
    out = cli._request("dev", "/status")
    assert out.get("code") == "server_restarting", out


def test_request_unreachable_without_a_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    cli.write_runtime("dev", "127.0.0.1", 59999, "tok")
    out = cli._request("dev", "/status")
    assert out.get("code") == "server_unreachable", out


def test_request_expired_marker_is_unreachable(monkeypatch, tmp_path):
    """An old mark from a real crash-mid-restart must not mask the crash forever."""
    monkeypatch.setenv("COMMAND_BRIDGE_DIR", str(tmp_path))
    cli.write_runtime("dev", "127.0.0.1", 59999, "tok")
    cli.mark_restarting("dev", -5)          # already expired
    out = cli._request("dev", "/status")
    assert out.get("code") == "server_unreachable", out
