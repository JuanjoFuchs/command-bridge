"""NEGATIVE CONTROL for the settings-registry guard. This module reads a variable nobody registered.

It is never imported and never executed — the guard *parses* it, the same way it parses
`command_bridge/`. It exists because a walker that returns zero findings and a walker that parses
nothing at all are indistinguishable from the outside, and this repo has shipped a denylist that
refused nothing and three diagnostics that never populated. A green check is a hypothesis until
something has been seen to turn it red.

The variable below is deliberately fictional: registering it would defeat the control.
"""
import os


def imaginary_backend_voice() -> str:
    """A plausible-looking read of a setting the CLI has never heard of."""
    return os.getenv("COMMAND_BRIDGE_NOT_A_REAL_SETTING", "")
