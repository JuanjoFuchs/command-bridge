"""command_bridge.fleet — OBJECTIVE per-lane activity, derived from git + the harness session logs.

The turn log (`store.py`) records what each lane SAID; it has no field for what each lane DID —
the files it read, edited, wrote, and whether that work is committed. That is recoverable, but only
from the harness's own transcript, and only by something that can join a lane to its session id.
Each agent knows both its lane (its `HERDR_PANE_ID` / the lane it drives) and its session id
(`CLAUDE_CODE_SESSION_ID`) from its own environment, so it writes that one-line bridge into the
registry (`config.fleet_registry_dir()`, one file per pane so concurrent lanes never race). This
module joins that registry to a per-harness transcript reader into per-lane file activity.

Nothing here is self-reported: the activity comes from parsing the harness's own transcript, not
from an agent describing itself — the property that makes the fleet view trustworthy as the home
screen (spec 021, KD5). Folded in from an earlier standalone `fleet.py` prototype; the herdr
dependency is dropped because Command Bridge already knows its own lanes and their live state.

STDLIB ONLY, like `config` — this is imported by the server and must add no dependency.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import config

# File-touching tools -> the action they represent. Everything else (Bash, Glob, Grep, Task, ...)
# is counted separately, not as a file touch.
FILE_TOOLS = {
    "Read": ("read", "file_path"),
    "Edit": ("edit", "file_path"),
    "MultiEdit": ("edit", "file_path"),
    "Write": ("write", "file_path"),
    "NotebookEdit": ("edit", "notebook_path"),
}


def _now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _sanitize(pane: str) -> str:
    return pane.replace(":", "_").replace("/", "_").replace("\\", "_")


# ---- per-harness transcript readers -----------------------------------------
# Each returns a normalized activity dict. Add a reader per harness; the registry and board stay
# generic. These transcripts run to HUNDREDS of MB, so re-reading them whole on every poll is
# hopeless (measured 42s). Instead we TAIL incrementally: seed from a bounded tail on first read,
# then on each later read consume only the bytes appended since. State per log path — which is why
# it lives on this long-lived module rather than in a request (server) or a fresh process (CLI).
_TAIL_BYTES = 6_000_000
_LOG_STATE: dict[str, dict] = {}
# ONE READER AT A TIME (JJ, 2026-10-05: "I don't see their interactions for Rigel and Sirius"). The
# server builds the board from executor THREADS — the periodic broadcast and every page connect — and
# two of them tailing the same log at once both read from the same saved offset and both ADVANCE it,
# pushing it past the end of the file. Nothing from that log was read again until it grew past the
# overshoot: rigel and sirius showed no files for hours while their fresh-process numbers were fine.
_LOG_LOCK = threading.Lock()


def _blank_counts() -> dict:
    return {"read": 0, "edit": 0, "write": 0, "bash": 0, "search": 0, "commits": 0}


def _detect_harness() -> str:
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude-code"
    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_SESSION_ID"):
        return "codex"
    return "unknown"


def _resolve_claude_log(config_dir: str | None, session_id: str | None) -> str | None:
    if not (config_dir and session_id):
        return None
    hits = glob.glob(os.path.join(config_dir, "projects", "**", f"{session_id}.jsonl"),
                     recursive=True)
    return hits[0] if hits else None


def _input_len(name: str, inp: dict) -> int:
    """Characters WRITTEN by a file tool, straight from its input (a token proxy: ~chars/4)."""
    if name == "Write":
        return len(inp.get("content") or "")
    if name == "Edit":
        return len(inp.get("new_string") or "")
    if name == "MultiEdit":
        return sum(len(e.get("new_string") or "") for e in (inp.get("edits") or []) if isinstance(e, dict))
    return 0


def _result_len(content) -> int:
    """Characters READ, taken from a tool_result block (str or list-of-text-blocks)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(x.get("text") or "") for x in content if isinstance(x, dict))
    return 0


# ---- shell commands as fleet activity (JJ, 2026-09-28) -----------------------------------------
# "I was thinking about diagramming the other commands like we do the files … git would be the box,
# and inside of git we would have status, commit." Every Bash/PowerShell tool call is split into
# (command, subcommand) so the fleet can draw a box per command and a dot per subcommand, the same
# way it draws repos and files. The split is heuristic by necessity — a command line is free text —
# so it errs toward dropping noise (shell keywords, inline code bodies, trivial read-only utilities)
# rather than inventing commands.
_SUBCOMMAND_TOOLS = {"git", "command-bridge", "herdr", "gh", "az", "npm", "pip", "uv", "docker",
                     "kubectl", "sb", "agent-mail", "claude",
                     "codex", "winget", "choco", "ngrok", "tailscale", "cargo", "go", "dotnet"}
# Plain utilities are how an agent LOOKS at things, not a command it runs; charting every `cat` and
# `ls` would bury the tools that matter under the noise of inspection.
_TRIVIAL = {"cd", "echo", "sleep", "set-location", "write-host", "write-output", "cat", "ls", "dir",
            "head", "tail", "grep", "rg", "sed", "awk", "find", "wc", "sort", "uniq", "cut", "tr",
            "mkdir", "rm", "cp", "mv", "touch", "pwd", "true", "false", "test", "which", "where",
            "get-content", "get-childitem", "select-object", "select-string", "out-string",
            "convertfrom-json", "foreach-object", "where-object", "measure-object", "start-sleep", "type",
            "tee", "xargs", "diff", "printf", "export", "source", "chmod", "stat", "file", "date"}
_KEYWORDS = {"for", "if", "else", "elif", "import", "from", "print", "def", "return", "foreach", "try",
             "catch", "while", "with", "fi", "done", "do", "then", "param", "function", "end", "begin",
             "process", "exit", "break", "continue", "null", "the", "a", "an", "and", "to", "of", "this"}
_INTERPRETERS = {"python", "python3", "py", "pwsh", "powershell", "node", "bash", "sh"}
_CMDNAME = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
_ENVSET = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def normalize_command(line: str) -> list[tuple[str, str | None]]:
    """A shell command line -> the (command, subcommand) pairs it invokes, noise dropped.

    Chained segments (`&&`, `||`, `;`, newlines) each count; an inline code body (`python -c "…"`,
    a heredoc, `pwsh -Command …`) is cut off first, since its `;` and newlines are code, not command
    boundaries. An interpreter running a script is charted as the SCRIPT (`fleet.py register`), and
    `python -c` as `python` / `-c (inline)`. A subcommand is only kept for tools that have them."""
    out: list[tuple[str, str | None]] = []
    inline = bool(re.search(r"\b(python3?|py)\s+-c\s", line))
    head_part = re.split(r"\s-c\s|<<|\s-Command\s", line, maxsplit=1)[0]
    for seg in re.split(r"&&|\|\||;|\n|\|", head_part):
        toks = [t for t in seg.replace('"', " ").replace("'", " ").split() if t not in ("&",)]
        while toks and (_ENVSET.match(toks[0]) or toks[0].startswith("$env:")):
            toks.pop(0)
        if not toks:
            continue
        head = os.path.basename(toks[0].replace("\\", "/")).lower()
        for suf in (".exe", ".cmd", ".bat"):
            if head.endswith(suf):
                head = head[: -len(suf)]
        rest = toks[1:]
        if head in _INTERPRETERS:
            args = [t for t in rest if not t.startswith("-")]
            script = next((t for t in args if re.search(r"\.(py|ps1|js|sh)$", t, re.I)), None)
            if script:
                head = os.path.basename(script.replace("\\", "/")).lower()
                rest = rest[rest.index(script) + 1:]
            elif inline and head in ("python", "python3", "py"):
                out.append((head, "-c (inline)"))
                continue
            elif "-m" in rest and rest.index("-m") + 1 < len(rest):
                i = rest.index("-m")
                head, rest = rest[i + 1].lower(), rest[i + 2:]
        if head == "sb.py":
            head = "sb"
        if head in _KEYWORDS or head in _TRIVIAL or not _CMDNAME.match(head):
            continue
        sub = None
        if head in _SUBCOMMAND_TOOLS or re.search(r"\.(py|ps1|js|sh)$", head):
            for t in rest:
                if t.startswith(("-", "$")) or any(ch in t for ch in "/\\:>&=") or not _CMDNAME.match(t.lower()):
                    continue
                sub = t.lower()
                break
        out.append((head, sub))
    return out


def _ingest_line(line: str, st: dict, since_utc: dt.datetime) -> None:
    # A background job's END: the harness logs a task-notification naming the tool-use id that
    # started it. Close it here (bg_pending, see _track_work) — notifications carry no tool blocks.
    if "task-notification" in line:
        for tid in re.findall(r"<tool-use-id>(toolu_[A-Za-z0-9]+)</tool-use-id>", line):
            st.get("bg_pending", {}).pop(tid, None)
    if '"tool_use"' not in line and '"tool_result"' not in line:
        return
    try:
        o = json.loads(line)
    except Exception:
        return
    ts = None
    ts_raw = o.get("timestamp")
    if ts_raw:
        try:
            ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except Exception:
            ts = None
    if ts is not None and ts < since_utc:
        return
    msg = o.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        _track_work(b, btype, st, ts)
        if btype == "tool_result":                          # chars READ land here, one message after the Read
            tid = b.get("tool_use_id")
            # A command's OUTPUT — the tokens it poured back into the agent (JJ, 2026-09-28: "tokens in
            # the invocation and tokens out in the tool result"). Credited to the command it answers.
            keys = st.get("pending_cmd", {}).pop(tid, None) if tid else None
            if keys:
                out = _result_len(b.get("content"))
                for k in keys:
                    rec = st.get("cmds", {}).get(k)
                    if rec is not None:
                        rec["out_chars"] = rec.get("out_chars", 0) + out // len(keys)
                        rec["last_out_chars"] = out // len(keys)   # THIS run's result — times the return flow
                        # WHEN the result landed: a long command (a `watch`) returns minutes after the
                        # call, so the return flow is anchored here, and the command reads as active again.
                        if ts is not None:
                            rec["last_out_t"] = ts.timestamp()
                            rec["t"] = max(rec.get("t", 0.0), ts.timestamp())
                continue
            path = st.get("pending", {}).pop(tid, None) if tid else None
            if path:
                rec = st["files"].get(path)
                if rec is not None:
                    rec["chars"] = rec.get("chars", 0) + _result_len(b.get("content"))
            continue
        if btype != "tool_use":
            continue
        name = b.get("name")
        inp = b.get("input") or {}
        if name in FILE_TOOLS:
            action, key = FILE_TOOLS[name]
            path = inp.get(key)
            if path:
                rec = st["files"].setdefault(path, {"read": 0, "edit": 0, "write": 0, "t": 0.0, "chars": 0, "last_op": None})
                rec[action] += 1
                st["counts"][action] += 1
                if action in ("edit", "write"):             # chars WRITTEN are in the tool input, right now
                    rec["chars"] = rec.get("chars", 0) + _input_len(name, inp)
                elif action == "read" and b.get("id"):       # chars READ come later in the result -> remember the id
                    pend = st.setdefault("pending", {})
                    pend[b["id"]] = path
                    if len(pend) > 600:                      # safety: drop stale unmatched reads
                        st["pending"] = {}
                if ts is not None:
                    tsf = ts.timestamp()
                    if tsf >= rec.get("t", 0.0):            # newest touch OWNS the live arrow direction (spec 021)
                        rec["last_op"] = action            # read/edit/write of the most recent op, per lane per file
                    rec["t"] = max(rec.get("t", 0.0), tsf)  # last-touch, for the live layer
                if ts is not None and (st["last"] is None or ts > st["last"]):
                    st["last"] = ts
                if ts is not None and (st["since"] is None or ts < st["since"]):
                    st["since"] = ts
        elif name in ("Bash", "PowerShell"):
            st["counts"]["bash"] += 1
            cmd = inp.get("command") or ""
            if "git commit" in cmd or "commit-unit apply" in cmd:  # a commit event (live "committing" state)
                if ts is not None:
                    st["last_commit"] = max(st.get("last_commit") or 0.0, ts.timestamp())
                st["counts"]["commits"] = st["counts"].get("commits", 0) + 1
            # Each command/subcommand it ran, for the fleet's command boxes (JJ, 2026-09-28), with the
            # tokens IN (the invocation's text) now and the tokens OUT (its result) when that lands.
            pairs = normalize_command(cmd)
            keys = []
            for c, s in pairs:
                key = c + (" " + s if s else "")
                rec = st.setdefault("cmds", {}).setdefault(
                    key, {"cmd": c, "sub": s, "n": 0, "t": 0.0, "in_chars": 0, "out_chars": 0})
                rec["n"] += 1
                rec["in_chars"] = rec.get("in_chars", 0) + len(cmd) // max(1, len(pairs))
                # THIS run's call, and a reset of its result until that lands — the fleet times one
                # line's in-then-out animation off the LATEST run, not the window's totals.
                rec["last_in_chars"] = len(cmd) // max(1, len(pairs))
                rec["last_out_chars"] = 0
                if ts is not None:
                    rec["last_in_t"] = ts.timestamp()
                keys.append(key)
                if ts is not None:
                    rec["t"] = max(rec["t"], ts.timestamp())
                    if st["last"] is None or ts > st["last"]:
                        st["last"] = ts
            if keys and b.get("id"):
                pend = st.setdefault("pending_cmd", {})
                pend[b["id"]] = keys
                if len(pend) > 600:                          # safety: drop stale unmatched calls
                    st["pending_cmd"] = {}
        elif name in ("Glob", "Grep"):
            st["counts"]["search"] += 1


def _is_wait(name: str, inp: dict) -> bool:
    """A `command-bridge watch` call is the agent WAITING for JJ, not working."""
    if name not in ("Bash", "PowerShell"):
        return False
    cmd = str(inp.get("command") or "")
    if "command-bridge" in cmd and re.search(r"\b(watch|serve)\b", cmd) is not None:
        # `serve` too: it is a SERVICE the agent keeps running in the background, not a job it is
        # waiting on — counted as work, it would mark the agent that started the server as busy forever.
        return True
    return re.search(r"\bhttp\.server\b", cmd) is not None


def _track_work(b: dict, btype: str | None, st: dict, ts: dt.datetime | None) -> None:
    """IS THE AGENT WORKING, as opposed to waiting? (JJ, 2026-09-29: "we instruct agents that are busy
    implementing something to background their watch. And that shows them as idle when in reality
    they are actually working … Is there a better way to detect if an agent is truly idle?")

    The watch alone cannot say: a busy agent keeps one open in the background to stay reachable. The
    session log can. Every tool call and result is work EXCEPT the watch itself — a foreground watch
    is a tool call still running, which must read as listening, not working (JJ: "make sure that that
    will not conflict with the listening status"). Records `last_work` (newest non-wait call or
    result) and `work_pending` (non-wait calls still waiting on their result)."""
    if btype == "tool_use":
        tid = b.get("id")
        if _is_wait(b.get("name") or "", b.get("input") or {}):
            if tid:
                st.setdefault("wait_ids", set()).add(tid)
            return
        if tid and ts is not None:
            pend = st.setdefault("work_pending", {})
            pend[tid] = ts.timestamp()
            if len(pend) > 200:                              # safety: a dead session's strays
                st["work_pending"] = {}
        # Remember which calls were LAUNCHED in the background, by the call's own flag — its output
        # text is no evidence (a foreground grep of a log that mentions "running in background" read
        # as a job that never ended, 2026-09-29).
        if tid and (b.get("input") or {}).get("run_in_background"):
            st.setdefault("bg_launched", set()).add(tid)
        # SUBAGENTS (JJ, 2026-09-29: "Also, how about subagents?"). A foreground one is simply a call
        # still waiting on its result, which the pending set above already counts. An ASYNC one
        # returns at once ("Async agent launched …") and ends with a task-notification naming this
        # id, like a background job — so remember the id and decide on its result.
        if tid and (b.get("name") or "") in ("Agent", "Task"):
            st.setdefault("agent_calls", set()).add(tid)
    elif btype == "tool_result":
        tid = b.get("tool_use_id")
        if tid and tid in st.get("wait_ids", set()):
            st["wait_ids"].discard(tid)
            return
        if tid:
            st.get("work_pending", {}).pop(tid, None)
            # A BACKGROUNDED JOB IS STILL WORK (JJ, 2026-09-29: "what about long running things? … Kepler
            # is just executing long running scripts and you show it as idle"). A run_in_background call
            # returns at once, so its result closes nothing; the job runs on until a task-notification
            # naming this tool-use id lands (see _ingest_line). Held as pending until then.
            if ts is not None and tid in st.get("bg_launched", set()):
                st["bg_launched"].discard(tid)
                st.setdefault("bg_pending", {})[tid] = ts.timestamp()
            elif tid in st.get("agent_calls", set()):
                st["agent_calls"].discard(tid)
                # the harness's own launch receipt (not command output), so its wording is evidence
                if ts is not None and "Async agent launched" in str(b.get("content") or ""):
                    st.setdefault("bg_pending", {})[tid] = ts.timestamp()
    else:
        return
    if ts is not None:
        st["last_work"] = max(st.get("last_work") or 0.0, ts.timestamp())


# A call waiting this long on its result is a stuck or abandoned one, not current work.
_WORK_PENDING_MAX_S = 600
# A background job is expected to run long, but one with no end notice after two hours is more
# likely orphaned (the session died) than still going.
_BG_PENDING_MAX_S = 7200


def _read_claude_log(log_path: str, since_utc: dt.datetime) -> dict:
    """Incrementally tail a Claude Code JSONL transcript into cumulative file activity.

    First call seeds from the last _TAIL_BYTES (recent window, cheap); later calls read only bytes
    appended since the saved offset. Accumulates across calls, so a live dashboard shows activity
    growing as agents work."""
    if not (log_path and os.path.exists(log_path)):
        return {"error": "log not found", "log_path": log_path}
    with _LOG_LOCK:
        return _read_claude_log_locked(log_path, since_utc)


def _read_claude_log_locked(log_path: str, since_utc: dt.datetime) -> dict:
    size = os.path.getsize(log_path)
    st = _LOG_STATE.get(log_path)
    # first read, truncated/rotated, or an offset past the end (the old race left these) -> reseed
    if st is None or size < st.get("size", 0) or st.get("offset", 0) > size:
        start = max(0, size - _TAIL_BYTES)
        st = {"offset": start, "counts": _blank_counts(), "files": {},
              "last": None, "since": None, "size": 0, "drop_first": start > 0}
        _LOG_STATE[log_path] = st
    with open(log_path, "rb") as fh:
        fh.seek(st["offset"])
        data = fh.read()
    nl = data.rfind(b"\n")
    if nl != -1:
        chunk = data[:nl + 1]
        st["offset"] += nl + 1
        lines = chunk.decode("utf-8", "replace").split("\n")
        if st.pop("drop_first", False):
            lines = lines[1:]  # first line after a mid-file seek is partial
        for line in lines:
            if line:
                _ingest_line(line, st, since_utc)
    st["size"] = size
    return {
        "counts": st["counts"],
        "files_touched": len(st["files"]),
        "files": st["files"],
        "commands": st.get("cmds", {}),
        "last_activity": st["last"].astimezone().isoformat(timespec="seconds") if st["last"] else None,
        "last_activity_epoch": st["last"].timestamp() if st["last"] else None,
        "last_commit_epoch": st.get("last_commit"),
        # WORKING vs WAITING (see _track_work): the newest non-watch tool activity, and whether a
        # non-watch call is still running (bounded, so a stuck call does not read as work forever).
        "last_work_epoch": st.get("last_work"),
        "work_pending": any(time.time() - t < _WORK_PENDING_MAX_S
                            for t in st.get("work_pending", {}).values())
                        or any(time.time() - t < _BG_PENDING_MAX_S
                               for t in st.get("bg_pending", {}).values()),
        "since": st["since"].astimezone().isoformat(timespec="seconds") if st["since"] else None,
    }


def _merge_activity(into: dict, sub: dict) -> dict:
    """Fold one subagent's activity into its parent's (new dicts; the cached log state is untouched)."""
    files = {p: dict(r) for p, r in (into.get("files") or {}).items()}
    for p, r in (sub.get("files") or {}).items():
        mine = files.get(p)
        if mine is None:
            files[p] = dict(r)
            continue
        for k in ("read", "edit", "write", "chars"):
            mine[k] = mine.get(k, 0) + r.get(k, 0)
        if r.get("t", 0.0) >= mine.get("t", 0.0):
            mine["t"], mine["last_op"] = r.get("t", 0.0), r.get("last_op") or mine.get("last_op")
    cmds = {k: dict(r) for k, r in (into.get("commands") or {}).items()}
    for k, r in (sub.get("commands") or {}).items():
        mine = cmds.get(k)
        if mine is None:
            cmds[k] = dict(r)
            continue
        for f in ("n", "in_chars", "out_chars"):
            mine[f] = mine.get(f, 0) + r.get(f, 0)
        mine["t"] = max(mine.get("t", 0.0), r.get("t", 0.0))
        if r.get("last_in_t", 0) >= mine.get("last_in_t", 0):
            for f in ("last_in_t", "last_in_chars", "last_out_t", "last_out_chars"):
                if f in r:
                    mine[f] = r[f]
    counts = dict(into.get("counts") or {})
    for k, v in (sub.get("counts") or {}).items():
        counts[k] = counts.get(k, 0) + v
    out: dict = dict(into, files=files, commands=cmds, counts=counts, files_touched=len(files))
    last = max(into.get("last_activity_epoch") or 0.0, sub.get("last_activity_epoch") or 0.0)
    if last:
        out["last_activity_epoch"] = last
        out["last_activity"] = dt.datetime.fromtimestamp(last).astimezone().isoformat(timespec="seconds")
    lw = max(into.get("last_work_epoch") or 0.0, sub.get("last_work_epoch") or 0.0)
    out["last_work_epoch"] = lw or None
    out["work_pending"] = bool(into.get("work_pending")) or bool(sub.get("work_pending"))
    return out


def _read_claude_session(log_path: str, since_utc: dt.datetime) -> dict:
    """The lane's activity = its session log PLUS its subagents' transcripts (JJ, 2026-09-29: "But what
    about the activities of those agents? like the files they touch, the commands they execute …
    without asking the sub agents to register with the fleet, of course").

    Claude Code writes each subagent's transcript to `<session>/subagents/agent-*.jsonl`, beside the
    parent's log, so no registration is needed: whatever a lane's subagents read, edit and run is
    credited to that lane. Only transcripts touched inside the window are read."""
    act = _read_claude_log(log_path, since_utc)
    if act.get("error") or not log_path or not log_path.endswith(".jsonl"):
        return act
    sub_dir = Path(log_path[: -len(".jsonl")]) / "subagents"
    if not sub_dir.is_dir():
        return act
    cutoff = since_utc.timestamp()
    for sub in sorted(sub_dir.glob("agent-*.jsonl")):
        try:
            if sub.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        sub_act = _read_claude_log(str(sub), since_utc)
        if not sub_act.get("error"):
            act = _merge_activity(act, sub_act)
    return act


READERS = {"claude-code": _read_claude_session}


# ---- git awareness ----------------------------------------------------------
# Per-file: is it inside a git repo, and does it have uncommitted changes? Lets the fleet view
# color a file by whether it is still in JJ's review queue (dirty) or already committed (clean),
# and hide non-repo noise (scratchpad / Downloads / temp).
_GITROOT_CACHE: dict[str, str | None] = {}
_DIRTY_CACHE: dict[str, tuple] = {}


def _git_root(path: str) -> str | None:
    d = os.path.dirname(path)
    if d in _GITROOT_CACHE:
        return _GITROOT_CACHE[d]
    cur, root = d, None
    while cur and cur != os.path.dirname(cur):
        if os.path.exists(os.path.join(cur, ".git")):
            root = cur
            break
        cur = os.path.dirname(cur)
    _GITROOT_CACHE[d] = root
    return root


def _repo_dirty(root: str, ttl: float = 8.0) -> set:
    now = time.time()
    hit = _DIRTY_CACHE.get(root)
    if hit and now - hit[0] < ttl:
        return hit[1]
    dirty = set()
    try:
        out = subprocess.run(["git", "-C", root, "status", "--porcelain", "-z"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10).stdout
        for entry in out.split("\0"):
            if len(entry) > 3:
                rel = entry[3:]
                dirty.add(os.path.normcase(os.path.normpath(os.path.join(root, rel))))
    except Exception:
        pass
    _DIRTY_CACHE[root] = (now, dirty)
    return dirty


def _annotate_git(files: dict) -> dict:
    """Return a copy of the per-file dict with repo/in_repo/dirty added (cache untouched)."""
    ann = {}
    for p, cnt in files.items():
        root = _git_root(p)
        in_repo = root is not None
        repo = os.path.basename(root) if root else None
        dirty = (os.path.normcase(os.path.normpath(p)) in _repo_dirty(root)) if root else False
        ann[p] = {**cnt, "repo": repo, "in_repo": in_repo, "dirty": dirty}
    return ann


# ---- the lane<->session registry --------------------------------------------


def register_lane(pane: str | None = None, lane: str | None = None, session: str | None = None,
                  harness: str | None = None, config_dir: str | None = None) -> dict:
    """Write THIS lane's one-line bridge (per-pane file, race-free). Env fills any blank:
    pane from HERDR_PANE_ID, session from CLAUDE_CODE_SESSION_ID, config_dir from
    CLAUDE_CONFIG_DIR, harness auto-detected. Returns the record (or an {error}) — the only party
    that can make this join is the agent itself, from its own environment."""
    pane = pane or os.environ.get("HERDR_PANE_ID")
    if not pane:
        return {"error": "no pane: pass pane= or run inside a herdr pane (HERDR_PANE_ID)",
                "code": "no_pane"}
    harness = harness or _detect_harness()
    session = session or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_SESSION_ID")
    config_dir = config_dir or os.environ.get("CLAUDE_CONFIG_DIR")
    log_path = _resolve_claude_log(config_dir, session) if harness == "claude-code" else None
    rec = {
        "pane": pane, "lane": lane, "harness": harness, "session_id": session,
        "config_dir": config_dir, "log_path": log_path, "cwd": os.getcwd(),
        "registered_at": _now_iso(),
    }
    reg_dir = Path(config.fleet_registry_dir())
    reg_dir.mkdir(parents=True, exist_ok=True)
    dest = reg_dir / f"{_sanitize(pane)}.json"
    dest.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return {"ok": True, "registered": rec, "file": str(dest)}


def _load_registry() -> list:
    reg_dir = Path(config.fleet_registry_dir())
    if not reg_dir.exists():
        return []
    recs = []
    for f in sorted(reg_dir.glob("*.json")):
        try:
            recs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return recs


# ---- per-lane working / idle state (spec 025) ---------------------------------


def lane_states(activity: dict, lanes: list, now: float, recent_s: float) -> dict:
    """Each lane's state for `watch --fleet`, from the activity map the server already derives.

    PURE (spec 025 TC1). `activity` is `{lane: {"work_pending", "last_work_epoch", ...}}` (the shape
    `server._fleet_activity()["lanes"]` returns). Only lanes in `lanes` are reported, in that order:
    `working` when a tool call is pending or the last work is younger than `recent_s` (the page's
    `laneBusy` rule); `idle` with `idle_s` (whole seconds since the last work) when older; `unknown`
    when there is no activity row or no work seen yet."""
    out: dict = {}
    for name in lanes:
        act = (activity or {}).get(name)
        if not isinstance(act, dict):
            out[name] = {"state": "unknown"}
            continue
        if act.get("work_pending"):
            out[name] = {"state": "working"}
            continue
        last = act.get("last_work_epoch")
        try:
            since = float(now) - float(last)
        except (TypeError, ValueError):
            out[name] = {"state": "unknown"}
            continue
        if since < float(recent_s):
            out[name] = {"state": "working"}
        else:
            out[name] = {"state": "idle", "idle_s": int(max(0.0, since))}
    return out


# ---- the board --------------------------------------------------------------


def build_board(hours: int = 24) -> dict:
    """Join the registry (session<->lane) + the per-harness log reader into per-lane file activity.

    Registry-driven (not herdr-driven): Command Bridge already knows its own lanes and their live
    state, so this contributes only the OBJECTIVE work — files read/edited/written, git status,
    last commit — keyed to a lane by the registry. Pure data; the CLI prints it and the server
    (spec 021) merges it onto the ready frame / a `lane_activity` broadcast."""
    since_utc = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    lanes = []
    for reg in _load_registry():
        reader = READERS.get(reg.get("harness"))
        activity: dict = (reader(reg.get("log_path"), since_utc) if reader
                          else {"error": f"no reader for harness {reg.get('harness')}"})
        files = activity.get("files")
        if isinstance(files, dict):
            activity["files"] = _annotate_git(files)
        lanes.append({
            "lane": reg.get("lane"),
            "pane": reg.get("pane"),
            "harness": reg.get("harness"),
            "registered": True,
            "activity": activity,
        })
    lanes.sort(key=lambda r: (r.get("activity", {}).get("counts", {}) or {}).get("edit", 0)
               + (r.get("activity", {}).get("counts", {}) or {}).get("write", 0), reverse=True)
    return {"window_hours": hours, "since_utc": since_utc.isoformat(timespec="seconds"),
            "generated": _now_iso(), "server_epoch": time.time(), "lanes": lanes}


# ---- hidden repos (spec 045) --------------------------------------------------
# JJ, voice, 2026-10-13, before a public demo: the repo canvas shows every repo the agents touched,
# private work repos included — "just a way to ask you to clean the ribbon, say which boxes to remove.
# Just that." A persisted set of repo names (as the box shows them) that the server drops from the
# page's fleet payload. The board itself stays whole, so `fleet` still reports the truth and `hide`
# can tell a real repo from a mis-heard name.


def hidden_path() -> str:
    """Beside the fleet registry's default home, in the session dir, so it moves with COMMAND_BRIDGE_DIR."""
    return os.path.join(config.session_dir(), ".agent-fleet", "hidden.json")


def load_hidden() -> list[str]:
    """The hidden repo names, in the order hidden. Never raises: a missing or bad file hides nothing."""
    try:
        with open(hidden_path(), encoding="utf-8") as fh:
            repos = (json.load(fh) or {}).get("repos") or []
        return [str(r) for r in repos if isinstance(r, str) and r]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _save_hidden(repos: list[str]) -> None:
    path = Path(hidden_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"repos": repos}, indent=1), encoding="utf-8")
    os.replace(tmp, path)   # atomic: the server reads this every broadcast


def known_repos(board: dict) -> list[str]:
    """Every repo name a box can show for this board (files inside a git repo), sorted."""
    names = set()
    for row in board.get("lanes", []):
        for f in ((row.get("activity") or {}).get("files") or {}).values():
            if isinstance(f, dict) and f.get("in_repo") and f.get("repo"):
                names.add(str(f["repo"]))
    return sorted(names, key=str.lower)


def hide_repos(names: list[str], known: list[str]) -> dict:
    """Hide each name that matches a known (or already hidden) repo, case-insensitively, stored in
    the box's own spelling. Names matching nothing are REPORTED in `unknown`, never stored — a
    dictated name is fuzzy, and a typo persisted would hide nothing while reading as done."""
    hidden = load_hidden()
    canon = {r.lower(): r for r in list(known) + hidden}
    have = {r.lower() for r in hidden}
    added, unknown = [], []
    for n in names:
        real = canon.get(n.strip().lower())
        if real is None:
            unknown.append(n)
        elif real.lower() not in have:
            hidden.append(real)
            have.add(real.lower())
            added.append(real)
    if added:
        _save_hidden(hidden)
    out: dict = {"hidden": hidden, "added": added}
    if unknown:
        out["unknown"] = unknown
        out["known"] = known
        if len(unknown) == len(names):   # nothing matched at all: a failure, not a partial success
            out.update(error=f"no repo box is named {', '.join(unknown)}",
                       code="unknown_repo",
                       remedy="command-bridge fleet hide <one of `known`> — names match the box "
                              "label, case-insensitively")
    return out


def unhide_repos(names: list[str], everything: bool = False) -> dict:
    """Show hidden repos again: the named ones (case-insensitive) or, with `everything`, all."""
    hidden = load_hidden()
    if everything:
        removed, not_hidden = hidden, []
        hidden = []
    else:
        want = {n.strip().lower() for n in names}
        removed = [r for r in hidden if r.lower() in want]
        hidden = [r for r in hidden if r.lower() not in want]
        not_hidden = [n for n in names if n.strip().lower() not in {r.lower() for r in removed}]
    if removed:
        _save_hidden(hidden)
    out: dict = {"hidden": hidden, "removed": removed}
    if not_hidden:
        out["not_hidden"] = not_hidden
    return out


def filter_hidden(lanes: dict, hidden: list[str]) -> dict:
    """PURE: the page's `{lane: activity}` map with every file in a hidden repo dropped (new dicts)."""
    if not hidden:
        return lanes
    gone = {h.lower() for h in hidden}
    out = {}
    for name, act in lanes.items():
        files = act.get("files")
        if isinstance(files, dict):
            kept = {p: f for p, f in files.items()
                    if not (isinstance(f, dict) and str(f.get("repo") or "").lower() in gone)}
            act = {**act, "files": kept, "files_touched": len(kept)}
        out[name] = act
    return out


_CACHE = {"ts": 0.0, "hours": None, "data": None}


def cached_board(hours: int = 24, ttl: float = 1.0) -> dict:
    """`build_board` behind a short TTL, so a ~1s poll and several near-simultaneous readers do not
    each re-run the git + log pass. Incremental tailing makes the uncached call cheap too (measured
    42s full re-read -> ~0.1s tail); this just collapses a burst."""
    now = time.time()
    if _CACHE["data"] is not None and _CACHE["hours"] == hours and (now - _CACHE["ts"]) < ttl:
        return _CACHE["data"]
    data = build_board(hours)
    _CACHE.update(ts=now, hours=hours, data=data)
    return data


def board_markdown(result: dict, top: int = 3) -> str:
    """The board as a compact table — what `command-bridge fleet --markdown` prints."""
    out = ["# Agent Fleet — objective activity", "",
           f"*From the session logs + git. No self-reporting. "
           f"Last {result['window_hours']}h · {result['generated'][:16].replace('T', ' ')}*", "",
           "| lane | edits | writes | reads | bash | commits | files | top files |",
           "|---|--:|--:|--:|--:|--:|--:|---|"]
    for r in result["lanes"]:
        act = r.get("activity", {})
        c = act.get("counts") or {}
        files = act.get("files") or {}
        ranked = sorted(files.items(),
                        key=lambda kv: (kv[1].get("edit", 0) + kv[1].get("write", 0), kv[1].get("read", 0)),
                        reverse=True)[:top]
        short = ", ".join(Path(p).name for p, _ in ranked) or "—"
        out.append(f"| {r.get('lane') or '?'} | {c.get('edit', 0)} | {c.get('write', 0)} | "
                   f"{c.get('read', 0)} | {c.get('bash', 0)} | {c.get('commits', 0)} | "
                   f"{act.get('files_touched', 0)} | {short} |")
    return "\n".join(out)
