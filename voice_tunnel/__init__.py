"""voice-tunnel — a voice tunnel between a phone browser and the agent that started it."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("voice-tunnel")
except PackageNotFoundError:      # a source tree that was never installed
    __version__ = "0.0.0+source"

__all__ = ["__version__"]

# READ FROM PACKAGE METADATA, never hardcoded. This was a literal string, and it drifted the
# first time it could: `pip install voice-tunnel` fetched 0.1.1 while `describe` reported 0.1.0,
# so the tool misreported itself to the one caller that parses it. `describe` is a contract an
# agent reads, and a wrong version there is worse than a missing one — it is a confident answer
# that sends someone to the wrong changelog.
#
# pyproject.toml is now the single source (NFR3), and the release workflow already refuses to
# build when the git tag disagrees with it. One number, checked at the one place it can be wrong.
#
# The fallback covers running from a clone that was never `pip install -e`'d. It is deliberately
# not a guess at the real version: `0.0.0+source` is obviously not a release, where a stale
# hardcoded number looks exactly like one.
