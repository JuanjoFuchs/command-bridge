"""FALSE-POSITIVE CONTROL: a read whose name is computed cannot be resolved, and must not fail.

`voice_tunnel/` contains several of these and every one is legitimate — the `_env` helper itself,
`config get` / `config set` resolving the key a user typed, and the `effective()` walk over
`SETTINGS`. That is the generic plumbing which makes a registry a registry: it reads whatever
name it is handed, so there is no literal for a source walk to find and nothing for it to check.

The guard records these and reports none of them. Reporting them would mean the registry's own
machinery fails the registry's own guard, which is the shape of a check that gets deleted rather
than satisfied.
"""
import os


def _env(name: str, default: str = "") -> str:
    """The helper, mirrored from `config.py`: one non-literal read, deliberately."""
    return (os.environ.get(name) or default).strip()


def read_whatever_the_caller_named(key: str) -> str:
    """Two more: a helper call and a subscript, neither with a literal to resolve."""
    return _env(key) or os.environ[key]
