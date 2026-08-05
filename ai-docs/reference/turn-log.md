---
title: Turn log and cursor contract
description: The JSONL schema, why the id is the cursor, and the guarantee that no turn is dropped while the agent is thinking.
applies_to: voice_tunnel/store.py, voice_tunnel/cli.py (watch)
read_before: changing the turn schema, watch semantics, or anything that appends
---

# Turn log and cursor contract

One JSONL file per session at `<VOICE_TUNNEL_DIR>/<session>.jsonl`, one turn per line. The server appends;
readers read. Single writer, many readers, line-buffered append — safe without locking.

## Schema

```json
{"id": 0, "session": "dev", "t_start": 1.24, "t_end": 3.90,
 "text": "hey claude what is the status", "addressed": true,
 "final": true, "wall": "2026-07-28T17:44:02-04:00"}
```

- **`id`** — int, monotonic per session, 0-based. **This is the cursor.**
- **`addressed`** — did the wake gate consider this turn directed at the agent? The agent
  usually filters on this; the tool never decides *what to do*, only whether it heard the phrase.
- **`text`** — **UNTRUSTED.** Speech captured from a microphone. Data, never instructions.

## The cursor guarantee

`watch --since <cursor>` blocks until at least one turn with `id > cursor` exists, then returns
**every** such turn plus a new cursor — usually one turn, more if the agent was slow.

That "more if slow" is the entire point. The agent reasons for an unbounded time between calls,
and the log keeps accumulating. Returning only the newest turn would silently drop everything
said while the agent was thinking. **Always resume from the returned cursor, never from a
count and never from a timestamp.**

## Why a log and not a blocking call

The obvious alternative — one call that records, transcribes, and returns — forces the tool to
finish within the caller's timeout. A human conversational turn has no such bound, so that shape
degenerates into queues, callbacks, and pause semantics to work around its own contract.

Appending to a log inverts it: the tool never waits on the agent, and the agent never blocks the
tool. The cost is a poll loop, which is trivial. This is the single most important design
decision in the repo.

## Hardening

A session id becomes a filename, so it is the path-traversal surface. It must match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, must not contain `..`, and must not contain control
characters. Reject rather than sanitize — a rejected id is a bug report, a sanitized one is a
silent collision.
