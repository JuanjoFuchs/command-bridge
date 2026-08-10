"""The last mile, which is the only step that fails silently.

An audit followed the documented loop end to end, did everything right, and handed over
`http://127.0.0.1:8795/?token=…` as the URL to open on a phone. Nothing had gone wrong; the tool
simply never said that the address it had just produced was unusable.

The warnings existed and named the wrong things. `the_loop` warned about `192.168.*`. The serve
banner warned about "a LAN IP". The address actually printed was loopback — not mic-less but
unreachable from another device entirely — and `status`, the one command whose job is to hand that
URL over, carried no caveat at all.
"""
import pytest

from tests.test_cli_surface import run
from voice_tunnel import cli


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.10.0.2"])
def test_loopback_is_not_phone_ready(host):
    """THE REGRESSION, and it is the DEFAULT bind — so this is the normal first answer, not an
    edge case."""
    verdict = cli._phone_reachability(host)
    assert verdict["ready"] is False
    assert "loopback" in verdict["why"]
    assert "tailscale" in verdict["remedy"].lower()


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "172.16.4.4", "172.31.9.9"])
def test_a_lan_address_over_http_is_not_phone_ready(host):
    """Not broken — mic-less. The page connects, looks right, and hears nothing, which is far
    harder to diagnose than a failure."""
    verdict = cli._phone_reachability(host)
    assert verdict["ready"] is False
    assert "microphone" in verdict["why"]


@pytest.mark.parametrize("host", ["my-box.tail1234.ts.net", "voice.example.com", "100.64.1.2"])
def test_a_routable_host_is_allowed_to_be_ready(host):
    """A Tailscale name is the whole point of the remedy; it must not be condemned too."""
    assert cli._phone_reachability(host)["ready"] is True


def test_status_carries_the_verdict_beside_the_url(capsys, tmp_sessions, monkeypatch):
    """Beside it, not two commands away. The caveat lived in prose in `the_loop` and on the serve
    banner — neither of which is what an agent reads at the moment it hands over a link."""
    cli.write_runtime("dev", "127.0.0.1", 8765, "t")
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"session": "dev"})

    _, payload, _ = run(["status", "--session", "dev"], capsys)

    assert payload["url"], "the URL is still reported"
    assert payload["phone"]["ready"] is False, "and it is reported as unusable, in the same object"
    assert payload["phone"]["remedy"]


def test_describe_tells_the_agent_to_read_it_before_handing_the_url_over():
    """A field nobody is told to read is a field nobody reads."""
    loop = " ".join(cli.DESCRIBE["the_loop"])
    assert "phone.ready" in loop
    returns = str(cli.DESCRIBE["commands"]["status"]["returns"])
    assert "phone" in returns and "secure context" in returns


def test_the_backoff_text_comes_from_the_backoff_constant():
    """Three hand-written copies of one number, no two agreeing: 15min in `watchdog.backoff`,
    30min in `watch --timeout`, thirty minutes in the constant's own docstring, and 540 seconds in
    the constant. Two of them contradicted each other inside a single `describe` payload."""
    expected = cli._human_seconds(cli.WATCH_BACKOFF_MAX_S)
    assert expected in cli.DESCRIBE["watchdog"]["backoff"]
    assert expected in cli.DESCRIBE["commands"]["watch"]["args"]["--timeout"]


@pytest.mark.parametrize("seconds,text", [(540.0, "9min"), (3600.0, "1h"), (30.0, "30s"),
                                          (90.0, "1.5min")])
def test_human_seconds_reads_the_way_a_person_would_say_it(seconds, text):
    assert cli._human_seconds(seconds) == text
