"""An error from one subsystem must never bury the one you are debugging.

Both defects here come from a 25-minute diagnosis on 2026-08-10 that spent most of its time
reading true statements about the wrong things.

  * `state.last_error` had twelve writers sharing one slot, so the last failure won. A
    `voiceprint: No module named 'sherpa_onnx'` — an optional subsystem nobody had asked about —
    occupied the field while the TTS failure being actively investigated had been overwritten.
  * A failing `say` came back as a bare `HTTP 500`. The server had sent `SAPI produced no audio`;
    the client read that body, failed to find the shape it expected, and discarded it.

Both are the same mistake: an error path that loses information at exactly the moment somebody
needs it.
"""
import json
import urllib.error

import pytest

from voice_tunnel import cli
from voice_tunnel.server import TunnelState


@pytest.fixture()
def state():
    return TunnelState(session="t", token=None)


def test_one_subsystem_failing_does_not_hide_another(state):
    """The exact sequence from the incident, in the order it happened."""
    state.fail("tts", "SAPI produced no audio")
    state.fail("voiceprint", "No module named 'sherpa_onnx'")

    assert state.errors["tts"] == "SAPI produced no audio", (
        "the failure being diagnosed must survive an unrelated one arriving after it"
    )
    assert state.errors["voiceprint"] == "No module named 'sherpa_onnx'"


def test_last_error_still_reports_the_most_recent(state):
    """Kept for anything already reading it — the point is that it is no longer the only view."""
    state.fail("tts", "SAPI produced no audio")
    state.fail("voiceprint", "sherpa missing")
    assert state.last_error == "sherpa missing"


def test_clearing_one_subsystem_leaves_the_others(state):
    """Reconnecting proves the transport works. It proves nothing about synthesis."""
    state.fail("tts", "SAPI produced no audio")
    state.fail("transport", "flush failed")
    state.clear_error("transport")

    assert "transport" not in state.errors
    assert state.errors["tts"] == "SAPI produced no audio"


def test_the_snapshot_exposes_both_views(state):
    state.fail("tts", "SAPI produced no audio")
    snap = state.snapshot()
    assert snap["errors"]["tts"] == "SAPI produced no audio"
    assert snap["last_error"] == "SAPI produced no audio"


# --------------------------------------------------------------------- say errors


class _FakeHTTPError(urllib.error.HTTPError):
    """An HTTPError whose body can be read exactly once, like the real thing.

    The single-read behaviour is load-bearing: the bug being pinned here was a body that got
    consumed by a failed parse and was then unavailable to report.
    """

    def __init__(self, code: int, body: bytes):
        super().__init__("http://x", code, "err", {}, None)  # type: ignore[arg-type]
        self._body = body
        self._read = False

    def read(self, *_args, **_kwargs):
        if self._read:
            return b""
        self._read = True
        return self._body


def _request_against(monkeypatch, error):
    monkeypatch.setattr(cli, "read_runtime",
                        lambda _s: {"host": "127.0.0.1", "port": 1, "token": "t"})

    def boom(*_a, **_k):
        raise error

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)
    return cli._request("t", "/say", {"text": "hi"})


def test_a_server_error_message_survives(monkeypatch):
    """The shape the server actually sends."""
    err = _FakeHTTPError(500, json.dumps({"error": "SAPI produced no audio"}).encode())
    out = _request_against(monkeypatch, err)
    assert out["error"] == "SAPI produced no audio"
    assert out["status"] == 500


def test_an_unexpected_body_is_kept_rather_than_discarded(monkeypatch):
    """THE REGRESSION. A body that is not the expected JSON used to become a bare `HTTP 500`.

    The message was read off the wire and thrown away, which is worse than never reading it: the
    information existed, reached the client, and was deleted on the way to the person who needed
    it.
    """
    err = _FakeHTTPError(500, b"SAPI produced no audio")
    out = _request_against(monkeypatch, err)
    assert out["status"] == 500
    assert "SAPI produced no audio" in out.get("body", ""), (
        "an unparseable body must still be shown; it is the only clue there is"
    )


def test_an_empty_body_does_not_invent_one(monkeypatch):
    """No body is honest. A fabricated one would be worse than a bare status."""
    out = _request_against(monkeypatch, _FakeHTTPError(500, b""))
    assert out["error"] == "HTTP 500"
    assert "body" not in out


def test_a_giant_body_is_truncated(monkeypatch):
    """A proxy's HTML error page is not worth 40 KB of an agent's context — but its first lines
    still say which proxy and why."""
    out = _request_against(monkeypatch, _FakeHTTPError(502, b"<html>" + b"x" * 40000))
    assert len(out["body"]) <= 500
