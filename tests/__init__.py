"""Makes this directory a REGULAR package, which is the only reason `from tests.x import y` works.

🔴 **Not cosmetic — the suite stops collecting without it.** Several tests here import each other
(`from tests.test_cli_surface import run`). Without this file `tests` is only a NAMESPACE portion,
and Python's rule is that a namespace package is created **only when no regular package of that
name is found anywhere on the path** — so a regular `tests/` package sitting in site-packages wins
no matter how early this repo appears on `sys.path`.

`kaleido` 1.2.0 ships exactly that: a top-level `tests` package, installed straight into
site-packages. Anything that pulls kaleido in — plotly image export, on this machine — silently
takes over the name for every project on the interpreter. Diagnosed 2026-08-26, when eight modules
failed to collect with `ModuleNotFoundError: No module named 'tests.test_cli_surface'` while the
files sat right there.

One empty-ish file makes this package regular, and a regular package at `sys.path[0]` beats one
further down.
"""
