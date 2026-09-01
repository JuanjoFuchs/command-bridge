"""The screenshot harness — the parts that need no browser.

The picture itself (full-page, viewport, lane) is verified live against a running server, because
a screenshot is only meaningful against a real render. What is unit-testable — and what actually
regressed things before — is the parsing and the failure paths: a `shot` that crashes instead of
returning a remedy takes the agent's whole turn with it.
"""
import argparse

from command_bridge import cli, shot


def test_parse_viewport_reads_wxh():
    assert shot.parse_viewport("390x844") == (390, 844)
    assert shot.parse_viewport("1280X800") == (1280, 800)   # case-insensitive on the x


def test_parse_viewport_falls_back_on_garbage():
    # A bad size is still a picture — the wrong size beats no shot, so it defaults rather than raises.
    assert shot.parse_viewport("nonsense") == (390, 844)
    assert shot.parse_viewport("") == (390, 844)


def _args(**kw):
    base = dict(session="no-such-session", out="x.png", viewport="390x844",
                lane="", url="", settle=4000)
    base.update(kw)
    return argparse.Namespace(**base)


def test_shot_without_a_server_returns_a_remedy_not_a_crash():
    """AC-4: with no running server and no --url, shot names how to start one — and it never
    launches a browser to discover the server is absent."""
    out = cli.cmd_shot(_args())
    assert out.get("code") == "no_server"
    assert "serve" in out.get("remedy", "")
    assert "error" in out


def test_shot_reports_an_unreachable_url_rather_than_raising():
    """The other server-down shape (AC-4): a URL was given but nothing answered. capture() must
    come back with page_unreachable (or a browser-availability error), never an exception."""
    # Port 1 is never a Command Bridge server; goto fails fast.
    result = shot.capture("http://127.0.0.1:1/?token=x", "x.png", settle_ms=500)
    assert "ok" not in result
    assert result.get("code") in {"page_unreachable", "no_browser", "no_playwright", "shot_failed"}
    assert "remedy" in result or "detail" in result
