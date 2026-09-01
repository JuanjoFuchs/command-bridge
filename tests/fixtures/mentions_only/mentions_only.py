"""FALSE-POSITIVE CONTROL: a name that only appears in prose is not a read.

`COMMAND_BRIDGE_MENTIONED_NEVER_READ` is named in this docstring the way `config.py` names
`COMMAND_BRIDGE_WAKE_BARE` — a record that a knob was *deleted*, kept so the next person does not
re-invent it. It is also named in a comment and used as a dict key below. None of the three is a
read, and a guard that cannot tell a mention from a read fails on correct code. A guard that
fails on correct code gets switched off, and then it guards nothing.

This is why the walk is an AST walk rather than a regex over string literals: a regex sees four
occurrences here and cannot rank them.
"""
import os

# COMMAND_BRIDGE_MENTIONED_NEVER_READ was honoured here once; this comment is all that survives.
DOCUMENTED = {
    # A dict key is documentation — `cli.py` names COMMAND_BRIDGE_HOME exactly this way inside the
    # `describe` payload, in addition to genuinely reading it somewhere else entirely.
    "COMMAND_BRIDGE_MENTIONED_NEVER_READ": "named as data, never passed to the environment",
}


def tts_backend() -> str:
    """One real, registered read — so a silent verdict cannot mean the file went unparsed."""
    return os.environ.get("COMMAND_BRIDGE_TTS", "")
