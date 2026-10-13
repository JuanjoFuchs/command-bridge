"""command_bridge.audit — a verbatim audit log of every CLI invocation.

**Why this exists.** JJ, 2026-09-15: "I would like us to have logs where we can see verbatim what
commands the agents are using." None of the existing logs answer that: the turn log holds the
CONVERSATION, the timing log holds STAGES, and `watch` polls the log from disk while `--reply` is a
CLI-side flag — so which command an agent actually ran, with which flags, was nowhere on disk. This
one records it: one line per `command-bridge` invocation, the verb and every flag exactly as typed,
so "is the agent using --reply?" is a grep, not an inference.

Append-only, one JSON object per line, in `sessions/<session>.commands.jsonl` beside the turn and
timing logs, and NEVER allowed to break an invocation — an audit trail that can take down the command
it records is worse than none. Every write is wrapped and silent on failure.

REDACTION is the one thing done to the verbatim argv, and it is not optional:
  * the value of a SECRET flag (`--token`) is replaced with `***` — a credential must never land in
    a log that exists to be read and shared;
  * a very long VALUE — a base64 canvas blob passed to `set --content/--html/--svg/...` — is
    truncated to a head plus its length, so the log stays readable while the command SHAPE stays
    exact (the flag is still there, only the megabyte body is summarised).
"""
from __future__ import annotations

import datetime
import json
import os
import threading
from typing import Any

from . import config

SUFFIX = ".commands.jsonl"

_LOCK = threading.Lock()
"""One writer at a time. Agents on several lanes invoke the CLI concurrently, and an interleaved
half-line would corrupt the file for every later read."""

# The value of these flags is a secret and must never be written to disk.
_SECRET_FLAGS = frozenset({"--token"})
# The value of these flags can be a large blob (canvas content); keep the flag, summarise the body.
_BLOB_FLAGS = frozenset({"--content", "--html", "--svg", "--markdown", "--text", "--mermaid"})
_BLOB_HEAD = 160  # characters of a blob value kept verbatim before the length note


def path(session: str) -> str:
    return os.path.join(config.session_dir(), f"{session}{SUFFIX}")


def _shorten(value: str) -> str:
    """A long value becomes a readable head plus its true length; a short one is left verbatim."""
    return value if len(value) <= _BLOB_HEAD else f"{value[:_BLOB_HEAD]}…<{len(value)} chars>"


def redact(argv: list[str]) -> list[str]:
    """The verbatim argv with the token hidden and any large blob value summarised.

    Value-bearing flags are written `--flag value` in this CLI, so the secret or blob is the token
    AFTER the flag; the `--flag=value` form is handled defensively too.
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        flag, eq, inline = tok.partition("=")
        if flag in _SECRET_FLAGS:
            out.append(f"{flag}=***" if eq else flag)
            if not eq and i + 1 < len(argv):
                out.append("***")
                i += 2
                continue
        elif flag in _BLOB_FLAGS:
            if eq:
                out.append(f"{flag}={_shorten(inline)}")
            else:
                out.append(flag)
                if i + 1 < len(argv):
                    out.append(_shorten(argv[i + 1]))
                    i += 2
                    continue
        else:
            out.append(tok)
        i += 1
    return out


def record(argv: list[str], *, session: str, verb: str | None = None,
           lane: str | None = None) -> None:
    """Append one invocation. Never raises — the audit trail must not break the command it records."""
    try:
        entry = {
            "wall": datetime.datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "verb": verb,
            "lane": lane,
            "argv": redact(list(argv)),
        }
        os.makedirs(config.session_dir(), exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with _LOCK:
            with open(path(session), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def read(session: str, limit: int = 0) -> list[dict[str, Any]]:
    """Every invocation, oldest first. A malformed line is skipped rather than fatal."""
    entries: list[dict[str, Any]] = []
    try:
        with open(path(session), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries[-limit:] if limit else entries


def report(session: str, *, limit: int = 0, verb: str | None = None,
           lane: str | None = None) -> dict[str, Any]:
    """The `command-bridge audit` payload: recent invocations plus a per-verb tally.

    `by_verb` is counted over the WHOLE log (before `limit`/filters) so the shape of what agents run
    is honest even when only the last N lines are shown; `commands` is the filtered, limited slice.
    """
    entries = read(session)
    tally: dict[str, int] = {}
    for e in entries:
        key = e.get("verb") or "?"
        tally[key] = tally.get(key, 0) + 1
    shown = entries
    if verb:
        shown = [e for e in shown if e.get("verb") == verb]
    if lane:
        shown = [e for e in shown if e.get("lane") == lane]
    if limit:
        shown = shown[-limit:]
    return {
        "session": session,
        "file": path(session),
        "total": len(entries),
        "by_verb": dict(sorted(tally.items(), key=lambda kv: kv[1], reverse=True)),
        "shown": len(shown),
        "commands": shown,
    }
