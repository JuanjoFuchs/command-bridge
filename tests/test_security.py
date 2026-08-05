"""Security — spec 001 AC-7, AC-8, AC-9.

The X-Forwarded-For cases below are the regression test for the exact shape of
GHSA-2qvv-vjq9-g5r4 (CVSS 8.6), where a spoofed header reached microphone recording.
"""
import pytest

from voice_tunnel import security


def test_loopback_is_allowed_by_default():
    assert security.ip_in_cidrs("127.0.0.1", security.allowed_cidrs())


def test_arbitrary_public_ip_is_not_allowed_by_default():
    assert not security.ip_in_cidrs("8.8.8.8", security.allowed_cidrs())


def test_tailscale_range_is_opt_in(monkeypatch):
    assert not security.ip_in_cidrs("100.101.102.103", security.allowed_cidrs())
    monkeypatch.setenv("VOICE_TUNNEL_ALLOW_CIDRS", "100.64.0.0/10")
    assert security.ip_in_cidrs("100.101.102.103", security.allowed_cidrs())


def test_malformed_ip_fails_closed_instead_of_raising():
    assert security.ip_in_cidrs("not-an-ip", ["127.0.0.0/8"]) is False
    assert security.ip_in_cidrs("", ["127.0.0.0/8"]) is False


def test_malformed_cidr_is_skipped_not_fatal():
    assert security.ip_in_cidrs("127.0.0.1", ["garbage", "127.0.0.0/8"]) is True


# --------------------------------------------------------- the CVE regression


def test_spoofed_forwarded_for_cannot_bypass_the_allowlist():
    """AC-8 — THE regression test. With no trusted proxy configured the header is ignored
    entirely and the direct peer decides, so claiming to be localhost buys nothing."""
    assert (
        security.client_ip("8.8.8.8", forwarded_for="127.0.0.1", trusted_proxies=())
        == "8.8.8.8"
    )
    assert not security.peer_allowed(
        "8.8.8.8", forwarded_for="127.0.0.1", trusted_proxies=(), cidrs=["127.0.0.0/8"]
    )


def test_forwarded_for_is_honored_only_from_a_trusted_peer():
    assert (
        security.client_ip("10.0.0.5", forwarded_for="203.0.113.9", trusted_proxies=("10.0.0.0/8",))
        == "203.0.113.9"
    )


def test_forwarded_chain_is_walked_right_to_left_skipping_trusted_hops():
    """Leftmost entries are attacker-written; the first untrusted address from the right
    is the real client."""
    resolved = security.client_ip(
        "10.0.0.5",
        forwarded_for="1.2.3.4, 203.0.113.9, 10.0.0.7",
        trusted_proxies=("10.0.0.0/8",),
    )
    assert resolved == "203.0.113.9"


def test_entirely_trusted_chain_falls_back_to_the_direct_peer():
    resolved = security.client_ip(
        "10.0.0.5", forwarded_for="10.0.0.6, 10.0.0.7", trusted_proxies=("10.0.0.0/8",)
    )
    assert resolved == "10.0.0.5"


# ------------------------------------------------------------------ the token


def test_token_must_match():
    """AC-7"""
    assert security.token_ok("abc", "abc") is True
    assert security.token_ok("wrong", "abc") is False
    assert security.token_ok(None, "abc") is False
    assert security.token_ok("", "abc") is False


def test_unset_expected_token_disables_auth():
    assert security.token_ok(None, None) is True
    assert security.token_ok("anything", "") is True


def test_token_comparison_is_constant_time():
    """AC-9 — a plain != on a secret is timing-attack shaped."""
    import inspect

    src = inspect.getsource(security.token_ok)
    assert "compare_digest" in src


def test_generated_tokens_are_unique_and_long_enough():
    tokens = {security.generate_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 24 for t in tokens)


# -------------------------------------------------------------------- the gate


@pytest.mark.parametrize(
    "peer,token,expect_ok",
    [
        ("127.0.0.1", "secret", True),
        ("127.0.0.1", "nope", False),
        ("127.0.0.1", None, False),
        ("8.8.8.8", "secret", False),
    ],
)
def test_gate_requires_both_checks(peer, token, expect_ok):
    gate = security.Gate("secret", cidrs=["127.0.0.0/8"])
    ok, _reason = gate.check(peer, token)
    assert ok is expect_ok


def test_gate_reason_never_leaks_the_expected_token():
    gate = security.Gate("super-secret-value", cidrs=["127.0.0.0/8"])
    _ok, reason = gate.check("127.0.0.1", "wrong")
    assert "super-secret-value" not in reason
