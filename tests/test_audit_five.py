"""What the fifth cold-start audit found, after it had already reached a working configuration.

That is the interesting part: this round produced no broken installs and no failed commands. Every
defect here is the tool being *wrong about itself* to an agent that had done everything right —
which is the only failure mode left once the thing works.
"""
import os

import pytest

from tests.test_cli_surface import run
from voice_tunnel import cli


def test_setup_is_named_by_every_remedy_it_actually_fixes(capsys, tmp_sessions, monkeypatch):
    """`next` reads the remedy strings to find what one command covers, so a remedy that names
    only its narrow pip line makes `setup` look smaller than it is.

    The audit was told `setup` covered two checks and handed pip lines for two others. `setup`
    installs `[all]` and fixed all four. Following that advice literally is three redundant
    commands, and one of them was actively wrong (see below).
    """
    monkeypatch.setenv("VOICE_TUNNEL_TTS", "piper")
    monkeypatch.setenv("VOICE_TUNNEL_PIPER_VOICE", "")
    _, payload, _ = run(["doctor"], capsys)

    for check in payload["checks"]:
        if check["name"] not in ("tts", "asr"):
            continue
        if check["status"] in ("failed", "degraded"):
            assert "voice-tunnel setup" in (check["remedy"] or ""), (
                f"{check['name']} is fixed by setup; say so, or `next` will undercount it"
            )


def test_the_asr_remedy_does_not_tell_you_to_pin_what_auto_selects(capsys, tmp_sessions,
                                                                   monkeypatch):
    """THE FOOTGUN. It ended with `config set VOICE_TUNNEL_ASR parakeet`, while `describe` warns
    in the same payload that an explicit value WINS over what is installed. Parakeet is selected
    automatically once its runtime is present, so the remedy pinned a choice that had already been
    made and took away the tool's ability to move you off it later.
    """
    monkeypatch.setenv("VOICE_TUNNEL_ASR", "whisper")
    _, payload, _ = run(["doctor"], capsys)
    asr = next(c for c in payload["checks"] if c["name"] == "asr")
    remedy = asr["remedy"] or ""
    if "sherpa" in (asr["detail"] or "") or "parakeet" in remedy:
        assert "config set VOICE_TUNNEL_ASR parakeet" not in remedy, (
            "installing the runtime is enough; pinning contradicts the tool's own warning"
        )


def test_watch_hands_over_a_watchdog_prompt_that_exists(capsys, tmp_sessions, monkeypatch):
    """It attached the static section, whose `prompt` key is null, next to a line saying "use the
    ready-made text in `prompt` below rather than paraphrasing it"."""
    monkeypatch.setattr(cli, "_request",
                        lambda s, path, body=None: {"running": False, "error": "no server"})
    _, payload, _ = run(["watch", "--session", "live", "--since", "-1", "--timeout", "0.1"],
                        capsys)
    wd = payload.get("watchdog")
    if wd is None:
        pytest.skip("no watchdog block on this path")
    assert wd.get("prompt"), "the field the sibling line points at must not be null"
    assert "live" in wd["prompt"], "and the session must already be substituted in"
    assert "{session}" not in wd["prompt"], "no unfilled placeholder may escape"


def test_a_global_flag_works_after_the_subcommand(capsys, tmp_sessions):
    """`doctor --human` is how people write it. It exited 2 with `unrecognized arguments: --human`
    while the usage line advertised `[--human]`, so the flag visibly existed and appeared broken.
    """
    code, payload, _ = run(["doctor", "--human"], capsys)
    assert code in (cli.EXIT_OK, cli.EXIT_ERROR), "must not be a usage error"
    assert payload is not None


def test_status_can_recover_the_client_url(capsys, tmp_sessions, monkeypatch):
    """`serve` prints the URL with its token exactly once, to stdout. Nothing else could produce
    it again — lose that output and the only route back to the page was restarting the server."""
    cli.write_runtime("dev", "127.0.0.1", 8765, "sekret")
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {"session": "dev"})
    _, payload, _ = run(["status", "--session", "dev"], capsys)
    assert payload["url"].startswith("http://127.0.0.1:8765/?token=")
    assert "sekret" in payload["url"]


def test_the_serve_banner_says_how_to_stop(capsys):
    """The command that starts a background process should say how it ends, in the same breath.
    This banner explained how to rename the assistant and change the speech rate."""
    src = open(os.path.join(os.path.dirname(cli.__file__), "server.py"), encoding="utf-8").read()
    assert "voice-tunnel stop --session" in src, "the banner must name the way out"


def test_the_live_wake_name_is_in_the_status_snapshot():
    """`voice-tunnel wake` compares persisted against live, and the live half read null forever
    because the snapshot never carried it — next to correct live phrases, which made it look like
    the server had a name it would not admit to."""
    from voice_tunnel.server import TunnelState

    snap = TunnelState(session="t", token=None).snapshot()
    assert snap.get("wake"), "the running gate's name belongs in status"
    assert "wake_phrases" in snap
