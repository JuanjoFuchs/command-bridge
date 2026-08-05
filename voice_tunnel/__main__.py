"""`python -m voice_tunnel` — the same CLI as the `voice-tunnel` console script.

Exists so the tool is reachable without the console script being on PATH. That happens more often
than it sounds: a `pip install --user` on a machine whose user Scripts directory is not on PATH, a
CI job that installs into a virtualenv it never activates, and a Windows install where PATH needs
a new shell to pick up. In every one of those `python -m voice_tunnel` works immediately, which
turns "the command is not found" from a dead end into a detour.

It must stay a pure delegation. Two entry points that can drift are two behaviours to keep in
step, and `describe` is a contract an agent reads — a difference between the two invocations
would be a contract that depends on how you were called.

**The import below is ABSOLUTE, not `from .cli import main`, and that is not a style choice.**
PyInstaller takes this file as its entry SCRIPT and runs it as `__main__` with no parent package,
so a relative import dies with `ImportError: attempted relative import with no known parent
package` — the build succeeds and the binary fails at first run. Absolute satisfies both callers:
`python -m voice_tunnel` has the package importable by definition, and the frozen bundle has it
collected.
"""
from voice_tunnel.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
