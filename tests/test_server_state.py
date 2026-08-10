"""Mute and the channel are HIS state; agent_state is the AGENT's. Neither may publish the other.

Regression for a defect found live on 2026-08-07. `_on_control` ran
`_set_agent_state(state, "idle" if state.muted else state.agent_state)` on every mute toggle, so
muting while the agent was mid-thought overwrote what it was doing with "idle" — the orb dropped
from Thinking to Listening and the elapsed-seconds counter vanished, while the agent carried on
working. The owner: "the thinking status had the timer, and then the counter disappeared while it was in
thinking status."

The browser half is covered in scripts/orbstate.py. This is the half that can be checked without
a browser: **the string that caused it must not come back.** A rule that only exists in a
comment is one the next edit re-introduces.
"""
import pathlib
import re

SERVER = pathlib.Path(__file__).resolve().parents[1] / "voice_tunnel" / "server.py"


def _control_handler() -> str:
    """The body of `_on_control`, where both toggles are handled."""
    src = SERVER.read_text(encoding="utf-8")
    start = src.index("async def _on_control")
    end = src.index(chr(10) + "async def ", start + 10)
    return src[start:end]


def test_a_mute_toggle_never_publishes_agent_state():
    body = _control_handler()
    mute = body[body.index('kind == "muted"'):body.index('kind == "channel"')]
    assert "_set_agent_state" not in mute, (
        "muting must not touch agent_state — it erases what the agent is doing, which is what "
        "made the orb's clock disappear mid-Thinking"
    )


def test_a_channel_toggle_never_publishes_agent_state():
    body = _control_handler()
    channel = body[body.index('kind == "channel"'):body.index('kind == "speaking"')]
    assert "_set_agent_state" not in channel, (
        "closing the channel is his state, not the agent's; the client decides what to show"
    )


def test_both_toggles_still_broadcast_themselves():
    """The fix must not go too far the other way: the client needs to hear about both, or a
    second device never learns the tunnel was muted."""
    body = _control_handler()
    assert re.search(r'"type": "muted"', body), "mute is still broadcast on its own channel"
    assert re.search(r'"type": "channel"', body), "channel is still broadcast on its own channel"
