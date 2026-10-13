"""The watchdog is retired (2026-09-17), and nothing in the agent-facing contract says otherwise.

For most of this tool's life the contract's LOUDEST instruction was "register a watchdog": a
~1/min scheduled job in the harness that re-armed `watch`, shipped as a ready-to-paste
`WATCHDOG_PROMPT` and attached to the first `watch` of every session. It was retired because a
blocking `watch` and a scheduler are the same mechanism from two sides — the scheduler kept waking
to find a watch already running and spawning a duplicate, and at seven live lanes it littered the
machine with per-minute jobs. The blocking foreground watch, re-invoked by the harness when a long
one is backgrounded, is the whole net now.

This is the mirror of `test_drain_is_gone.py`. `describe` is the contract (convention 3): every
string in it is read as a live claim, so there is no such thing as a historical mention INSIDE it.
The history belongs in the source comments and in `specs/`, and those are deliberately left alone —
which is why this test reads the live `describe` payload and the live module symbols, not the
source text. If it fails, an agent is once again being told to schedule a job that no longer
belongs in the contract.
"""
import argparse
import json

from command_bridge import cli


def _walk(node, path="describe"):
    """Every key and every leaf value in the payload, with the path that reached it. Recursive
    because the watchdog block used to be attached, and a shallow top-level check would miss a
    nested reattachment."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}", str(key)
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple, set)):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")
    elif node is not None:
        yield path, str(node)


def _describe():
    return cli.cmd_describe(argparse.Namespace(session="dev", human=False))


def test_the_describe_payload_has_no_watchdog_block():
    """The top-level key that carried the whole section — and the per-session prompt substituted
    into it at call time — is gone from the contract."""
    payload = _describe()
    assert "watchdog" not in payload, "the `watchdog` section is still in the `describe` contract"


def test_the_word_watchdog_appears_nowhere_in_the_describe_payload():
    """Zero tolerance, and only here. Asserted on the payload `cmd_describe` actually returns —
    the prompt used to be substituted in at call time, and that prompt was the single most
    instruction-shaped string the tool emitted."""
    hits = [(path, text) for path, text in _walk(_describe()) if "watchdog" in text.lower()]
    assert not hits, "the retired watchdog is still named in the `describe` contract at:\n" + "\n".join(
        f"  {path}: {text[:160]}" for path, text in hits
    )


def test_the_describe_payload_tells_no_one_to_schedule_a_recurring_job():
    """The BEHAVIOUR, not just the name: the contract must not instruct the agent to register a
    cron / scheduled job / recurring reminder to re-arm the watch. Named by property because the
    old block was deliberately worded to survive a rename ('cron job, a scheduled prompt, a
    reminder, a timer, a notification')."""
    blob = json.dumps(_describe()).lower()
    for banned in ("cron", "recurring job", "recurring reminder", "scheduled prompt",
                   "1/min", "~1 min", "every minute", "re-arms `watch`"):
        assert banned not in blob, (
            f"the contract still teaches a scheduled re-arm ({banned!r}); the blocking watch is the "
            "net now"
        )


def test_the_watchdog_machinery_is_not_importable():
    """The code symbols an old caller or test might reach for are gone, so a reattachment cannot be
    a one-liner that quietly compiles."""
    assert not hasattr(cli, "WATCHDOG_PROMPT"), "the ready-to-schedule prompt is still in the module"
    assert not hasattr(cli, "_watchdog_block"), "the per-session block builder is still in the module"


def test_the_re_entry_lesson_survived_the_removal():
    """Retiring the net must not delete the reason it existed. The one thing the watchdog taught
    that still holds — end every turn in `watch`, because that is what re-enters you — has to be
    somewhere an agent reads, or the retirement re-opens the failure it once covered."""
    payload = _describe()
    loop_text = " ".join(payload["the_loop"]).lower()
    assert "watch" in loop_text and "re-invoke" in loop_text, (
        "the loop must still say the blocking watch is the way back"
    )
