#!/usr/bin/env bash
#
# command-bridge installer for macOS and Linux (and Git Bash / WSL on Windows).
#
# There is no PyPI, npm or WinGet release yet, so this installs from the Git repository: it finds
# Python, clones (or updates) the repo, builds a private virtualenv beside it, installs
# command-bridge with all of its neural engines, and prints how to run it. Nothing touches the
# system Python or any active environment — everything lives under the checkout and is removed by
# deleting that one directory.
#
#   Pipe it:       curl -fsSL https://raw.githubusercontent.com/JuanjoFuchs/command-bridge/main/install.sh | bash
#   From a clone:  ./install.sh
#
# Override with environment variables:
#   CB_DIR=<path>   where to clone       (default: ~/command-bridge)
#   CB_REPO=<url>   which remote to use  (default: the GitHub repo)
#
set -euo pipefail

REPO_URL="${CB_REPO:-https://github.com/JuanjoFuchs/command-bridge.git}"
DEST="${CB_DIR:-$HOME/command-bridge}"

say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

# 1. A Python 3.10+ interpreter. `python3` first (the POSIX convention), then bare `python` — which
#    on some systems is still Python 2 or a shim that does nothing, hence last.
find_python() {
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; then
      printf '%s' "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_python)" || die "needs Python 3.10+ on PATH — install it from https://python.org and re-run."

# 2. The repository: reuse this checkout if we are already inside one, update it if it is already
#    cloned at CB_DIR, otherwise clone it fresh.
if [ -f pyproject.toml ] && grep -q 'name = "command-bridge"' pyproject.toml 2>/dev/null; then
  DEST="$(pwd)"
  say "Using this checkout: $DEST"
elif [ -d "$DEST/.git" ]; then
  say "Updating existing checkout: $DEST"
  git -C "$DEST" pull --ff-only
else
  command -v git >/dev/null 2>&1 || die "git is required to fetch the repo (or run this from a checkout)."
  say "Cloning into: $DEST"
  git clone --depth 1 "$REPO_URL" "$DEST"
fi

# 3. A private virtualenv beside the checkout. The shim in bin/ looks for exactly venv/, so this is
#    where it must go. `.[all]` pulls the engines (a neural voice, the fast recognizer, the turn
#    model) in one step — required here because, without a PyPI release, `command-bridge setup`
#    cannot install them later; it can only download the model assets they use.
# The interpreter lives at bin/python on POSIX and Scripts/python.exe on Windows (this script also
# runs under Git Bash, where `python -m venv` produces the Windows layout) — resolve whichever the
# venv actually created, the same way the bin/ shim does.
VENV="$DEST/venv"
venv_python() {
  if   [ -x "$VENV/bin/python" ];        then printf '%s' "$VENV/bin/python"
  elif [ -x "$VENV/Scripts/python.exe" ]; then printf '%s' "$VENV/Scripts/python.exe"
  else return 1; fi
}
VPY="$(venv_python)" || { "$PY" -m venv "$VENV"; VPY="$(venv_python)"; } \
  || die "virtualenv creation did not produce a python interpreter in $VENV"

say "Installing command-bridge and its neural engines (native wheels — this takes a minute)..."
"$VPY" -m pip install --quiet --upgrade pip
( cd "$DEST" && "$VPY" -m pip install --quiet -e ".[all]" )

chmod +x "$DEST/bin/command-bridge" "$DEST/bin/command-bridge-run.py" 2>/dev/null || true

# 4. Report, and the exact next steps. The models (a neural voice, fast ASR, the voiceprint and the
#    turn model) are a separate ~1 GB download kept behind an explicit command, so the install stays
#    fast and you choose what to pull.
BIN="$DEST/bin/command-bridge"
cat <<EOF

  command-bridge is installed at $DEST

  Put it on your PATH (add this to ~/.bashrc or ~/.zshrc):
      export PATH="$DEST/bin:\$PATH"

  Then:
      command-bridge setup                 # download the neural voice, fast ASR, voiceprint, turn model
      command-bridge serve --wake claude   # start it under YOUR agent's name, and open the URL it prints

  Or run it in place without touching PATH:
      $BIN doctor
EOF
