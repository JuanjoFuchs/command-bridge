#!/usr/bin/env python
"""The one entry point every `voice-tunnel` shim executes. Not meant to be typed by a human.

WHY THIS FILE EXISTS. The shims used to invoke the CLI as an inline program:

    python -c "import sys; sys.path.insert(0, r'$VOICE_TUNNEL_ROOT'); from voice_tunnel.cli import main; ..."

which fails in two ways that are only visible on Windows. First, `$VOICE_TUNNEL_ROOT` had to be spliced
into a Python raw string, so a root ending in a backslash — what `cygpath -w` returns for a drive
root, and what a symlinked shim resolved to in practice — closed the string early and produced a
SyntaxError instead of a CLI. Second, it is unreadable, so nobody could tell at a glance whether
the quoting was right.

Resolving the root from `__file__` removes the splice entirely: the shell hands over ONE path,
the path of this file, and Python resolves it natively whatever separators it arrived with. There
is no string to escape and no PYTHONPATH for MSYS to rewrite on the way through.
"""
import os
import sys

# Put the repo root FIRST so `voice-tunnel` resolves to this checkout even if another one is installed
# in the interpreter's site-packages — a shim named after a directory must mean that directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_tunnel.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
