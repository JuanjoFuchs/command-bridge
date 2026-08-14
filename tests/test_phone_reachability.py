"""The last mile, which is the only step that fails silently.

An audit followed the documented loop end to end, did everything right, and handed over
`http://127.0.0.1:8795/?token=…` as the URL to open on a phone. Nothing had gone wrong; the tool
simply never said that the address it had just produced was unusable.

The warnings existed and named the wrong things. `the_loop` warned about `192.168.*`. The serve
banner warned about "a LAN IP". The address actually printed was loopback — not mic-less but
unreachable from another device entirely — and `status`, the one command whose job is to hand that
URL over, carried no caveat at all.
"""
import io
import json

import pytest

from tests.test_cli_surface import run
from voice_tunnel import cli

REAL_NGROK_FRONTS = cli._ngrok_fronts
"""Captured at import, before `no_live_tunnel` stubs it out. The tests below that exercise the
PARSER need the real function; everything else wants the stub, and reaching for the original
through a fixture's undo order is a dependency nobody should have to know about."""


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.10.0.2"])
def test_loopback_with_no_forwarder_is_not_phone_ready(host):
    """THE FIRST REGRESSION, and it is the DEFAULT bind — so this is the normal first answer, not
    an edge case. `no_live_tunnel` (conftest) guarantees no forwarder is in the picture."""
    verdict = cli._phone_reachability(host)
    assert verdict["ready"] is False
    assert "loopback" in verdict["why"]
    assert verdict["remedy"]


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


# ------------------------------------------------- the verdict that could never become true
#
# THE SECOND REGRESSION, and it is the one that mattered. `ready` was derived from the bind host
# alone — and ngrok, `tailscale serve`, cloudflared and every other way to reach a phone forward
# from LOOPBACK. So after following the remedy exactly, the field still said false and printed the
# same remedy again. Observed live on 2026-08-14 with a phone connected through ngrok and audio
# flowing both ways: `phone.ready: false`, `remedy: run tailscale serve`.
#
# A remedy that survives being followed teaches the reader to stop reading remedies, which costs
# more than the original gap did.


def front(monkeypatch, port=8765, url="https://x.ngrok-free.app"):
    """Pretend an ngrok tunnel is up for `port`."""
    monkeypatch.setattr(cli, "_ngrok_fronts",
                        lambda p: {"via": "ngrok", "public_url": url, "detected": True,
                                   "how": "test"} if int(p) == port else None)


def test_a_forwarded_loopback_bind_is_phone_ready(monkeypatch):
    """The whole point: the bind is loopback, and the phone works anyway."""
    front(monkeypatch)

    verdict = cli._phone_reachability("127.0.0.1", 8765)

    assert verdict["ready"] is True, "a fronted port IS reachable from a phone"
    assert verdict["url"] == "https://x.ngrok-free.app"
    assert verdict["remedy"] is None, "there is nothing left to do, so do not prescribe anything"


def test_a_forwarder_on_a_different_port_does_not_count(monkeypatch):
    """A tunnel to something else on this machine says nothing about our server."""
    front(monkeypatch, port=9999)

    assert cli._phone_reachability("127.0.0.1", 8765)["ready"] is False


def test_the_port_is_required_to_see_the_forwarder(monkeypatch):
    """Called without a port — as the older callers did — there is nothing to match a tunnel
    against, and the answer falls back to the bind host."""
    front(monkeypatch)

    assert cli._phone_reachability("127.0.0.1")["ready"] is False


def test_a_proxy_can_be_asserted_when_it_cannot_be_detected(monkeypatch):
    """`tailscale serve`, cloudflared and reverse proxies have no local API to ask. The operator
    always knows; the tool never can. So it can be said, once, and persisted."""
    monkeypatch.setenv("VOICE_TUNNEL_PUBLIC_URL", "https://box.tail1234.ts.net")

    verdict = cli._phone_reachability("127.0.0.1", 8765)

    assert verdict["ready"] is True
    assert verdict["url"] == "https://box.tail1234.ts.net"
    assert "NOT VERIFIED" in verdict["why"], (
        "an asserted fact must not be reported in the same voice as a measured one"
    )


def test_an_unverifiable_answer_says_so_instead_of_prescribing(monkeypatch):
    """THE INSTRUCTION THAT MATTERS when detection fails: say what was checked. A reader whose
    forwarder is not on that list can then tell a blind spot from a verdict about their setup."""
    verdict = cli._phone_reachability("127.0.0.1", 8765)

    why = verdict["why"]
    assert "ngrok" in why and "VOICE_TUNNEL_PUBLIC_URL" in why, "name what was checked"
    assert "INVISIBLE" in why, "and admit what could not be"
    assert "tailscale serve" in why or "tailscale" in why.lower(), (
        "the ones it cannot see should be named, not left as 'something'"
    )


def test_the_remedy_does_not_make_tailscale_the_only_answer(monkeypatch):
    """`tailscale serve` takes over the device's DNS through MagicDNS, system-wide, which can
    break a corporate VPN on the same machine. Prescribing it as THE fix — with no alternative
    and no warning — is a remedy that can cost more than the problem."""
    remedy = cli._phone_reachability("127.0.0.1", 8765)["remedy"]

    assert "ngrok" in remedy, "the option that needs no DNS change should be offered"
    assert remedy.index("ngrok") < remedy.index("tailscale"), (
        "and offered first — it is the one with no side effects"
    )
    assert "DNS" in remedy and "VPN" in remedy, (
        "the cost of the tailscale option must travel with it, not be discovered afterwards"
    )
    assert "VOICE_TUNNEL_PUBLIC_URL" in remedy, "and the escape hatch for anything else"


# ------------------------------------------------------------------ the undocumented exposure
#
# ngrok forwards from localhost, so every request it relays passes the CIDR allowlist
# unconditionally and the token in the URL becomes the only gate. Nothing in `doctor`, `status` or
# `describe` said so, and `status` printed the tokenised URL with no caveat at all.


def test_a_fronted_port_reports_that_the_allowlist_is_inert(monkeypatch):
    front(monkeypatch)

    exposure = cli._phone_reachability("127.0.0.1", 8765)["exposure"]

    assert exposure["public"] is True
    assert exposure["allowlist_effective"] is False, "THE FACT: the allowlist filters nothing"
    assert exposure["gates"] == ["the token in the URL"]
    assert "VOICE_TUNNEL_ALLOW_CIDRS" in exposure["why"]
    assert "credential" in exposure["treat_the_url_as"]


def test_an_unfronted_port_says_the_allowlist_is_doing_its_job(monkeypatch):
    exposure = cli._phone_reachability("127.0.0.1", 8765)["exposure"]

    assert exposure["public"] is False
    assert exposure["allowlist_effective"] is True
    assert "VOICE_TUNNEL_ALLOW_CIDRS" in exposure["gates"]


def test_status_carries_the_exposure_beside_the_url(capsys, tmp_sessions, monkeypatch):
    """Beside the URL, because that is the moment somebody is about to hand it to a phone."""
    cli.write_runtime("dev", "127.0.0.1", 8765, "t")
    front(monkeypatch)
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"session": "dev"})

    _, payload, _ = run(["status", "--session", "dev"], capsys)

    assert payload["phone"]["ready"] is True
    assert payload["phone"]["exposure"]["public"] is True
    assert payload["phone"]["exposure"]["allowlist_effective"] is False


def serving(payload):
    """Stand in for ngrok's agent API returning `payload`."""
    def fake(url, timeout=None):
        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R(json.dumps(payload).encode())
    return fake


@pytest.mark.parametrize("addr", ["8765", "localhost:8765", "http://127.0.0.1:8765"])
def test_ngrok_detection_matches_the_port_however_the_address_was_typed(addr, monkeypatch):
    """ngrok records `addr` the way it was typed, and the port is the only part of it that
    identifies OUR server — the host half is loopback in every spelling."""
    monkeypatch.setattr(cli.urllib.request, "urlopen", serving({"tunnels": [
        {"public_url": "https://a.ngrok-free.app", "config": {"addr": addr}},
    ]}))

    assert REAL_NGROK_FRONTS(8765)["public_url"] == "https://a.ngrok-free.app"
    assert REAL_NGROK_FRONTS(8766) is None, "a tunnel to another port is not ours"


def test_a_plain_http_tunnel_is_not_accepted(monkeypatch):
    """ngrok publishes an http and an https tunnel for the same port. Only the https one gives a
    phone a microphone, so only that one may make `ready` true."""
    monkeypatch.setattr(cli.urllib.request, "urlopen", serving({"tunnels": [
        {"public_url": "http://a.ngrok-free.app", "config": {"addr": "8765"}},
    ]}))

    assert REAL_NGROK_FRONTS(8765) is None


def test_a_missing_ngrok_never_becomes_an_error(monkeypatch):
    """`status` is on the watchdog's path. A forwarder the user has never installed must not turn
    a status call into a diagnostic about it — every failure here means the same thing."""
    def boom(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(cli.urllib.request, "urlopen", boom)

    assert REAL_NGROK_FRONTS(8765) is None


# ------------------------------------------------------------------------- doctor's opinion


def test_doctor_warns_when_a_public_port_is_gated_only_by_a_generated_token(
        capsys, tmp_sessions, monkeypatch):
    """The combination that turns a defensible design into an accident: publicly reachable, and
    the only gate is a secret nobody chose and that changes on every restart."""
    cli.write_runtime("dev", "127.0.0.1", 8765, "generated-at-serve-time")
    front(monkeypatch)
    monkeypatch.setattr(cli, "_live_server_on", lambda c: c[0])
    monkeypatch.delenv("VOICE_TUNNEL_TOKEN", raising=False)

    _, payload, _ = run(["doctor"], capsys)
    check = next(c for c in payload["checks"] if c["name"] == "exposure")

    assert check["status"] == "degraded", "this is a warning, not a footnote"
    assert "exposure" in payload["degraded"]
    assert "VOICE_TUNNEL_ALLOW_CIDRS" in check["detail"], "say WHY the allowlist is not helping"
    assert "VOICE_TUNNEL_TOKEN" in check["remedy"]
    assert "PUBLIC" in payload["next"], "the first line an agent reads must carry it"


def test_a_deliberately_set_token_is_reported_but_not_scolded(capsys, tmp_sessions, monkeypatch):
    """The exposure is still real and still stated; there is simply nothing left that the tool
    can call misconfigured."""
    cli.write_runtime("dev", "127.0.0.1", 8765, "chosen-by-a-person")
    front(monkeypatch)
    monkeypatch.setattr(cli, "_live_server_on", lambda c: c[0])
    monkeypatch.setenv("VOICE_TUNNEL_TOKEN", "chosen-by-a-person")

    _, payload, _ = run(["doctor"], capsys)
    check = next(c for c in payload["checks"] if c["name"] == "exposure")

    assert check["status"] == "info"
    assert check["detail"].startswith(cli.PUBLIC_EXPOSURE_PREFIX)
    assert "PUBLIC" in payload["next"], "still worth the first line — the microphone is still open"


def test_a_local_only_server_is_not_reported_as_exposed(capsys, tmp_sessions, monkeypatch):
    cli.write_runtime("dev", "127.0.0.1", 8765, "t")

    _, payload, _ = run(["doctor"], capsys)
    check = next(c for c in payload["checks"] if c["name"] == "exposure")

    assert check["status"] == "info"
    assert "not publicly fronted" in check["detail"]
    assert "INVISIBLE" in check["detail"], "'nothing found' is not 'nothing there'"
    assert "PUBLIC" not in payload["next"]


def test_stale_runtime_files_do_not_multiply_one_open_microphone(
        capsys, tmp_sessions, monkeypatch):
    """A runtime file outlives its server unless `stop` removed it, and twenty-six of them had
    piled up against one running process on this machine — all naming the default port. Counting
    files as servers reported twenty-six exposures for one microphone."""
    for name in ("dev", "old1", "old2", "old3"):
        cli.write_runtime(name, "127.0.0.1", 8765, "t")
    front(monkeypatch)
    monkeypatch.setattr(cli, "_live_server_on",
                        lambda c: next((x for x in c if x[0] == "dev"), None))

    _, payload, _ = run(["doctor"], capsys)
    check = next(c for c in payload["checks"] if c["name"] == "exposure")

    assert check["detail"].count("port 8765") == 1, "one port is one exposure"
    assert "'dev'" in check["detail"], "and it names the session actually answering"
    assert "old1" not in check["detail"]
