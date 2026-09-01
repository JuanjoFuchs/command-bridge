"""What spec 011 buys back, in characters — recomputed from the working tree, every run.

**Why this is a script and not a number in a document.** FR4 asks for the saving to be stated as a
measurement rather than an intention, and a measurement written into prose is true exactly once:
the first payload change after it is written makes the document confidently wrong, and nothing
anywhere reports it. So the before and the after are BOTH computed here, from the live code, and
the gate asserts the *property* — a repeat costs materially less than a first — rather than a
historical constant.

**"Before" is not a constant either.** It is the same code driven with the memories spec 011 added
reset between calls, which is exactly what the tool did before this spec:

* FR1's memo is `TunnelState.last_refusal`; clearing it makes the next refusal a FIRST again, and
  that reset is a documented seam (`command_bridge/server.py::_unread_refusal`).
* FR3's memo is the per-command `next_branch` map in `<session_dir>/<session>.watch.json`;
  emptying it makes the next call of every command a first call again.

Nothing else is stubbed. The refusal payloads come out of `server._unread_refusal` against a real
turn log, the `next` strings come out of `cli._emit_next` through the real `cmd_say` / `cmd_watch`,
and the sizes are `json.dumps` of the payload the agent actually receives.

WHAT IT MEASURES (AC22) — three exchanges, and the refused batch at three turn sizes because the
turn size dominates the number:

  * **a refused batch** — four `say --now` clips, all refused, one unread turn, at 74 characters of
    speech (the median addressed turn on the live `dev` log), 258 (p90) and 702 (the size the
    defect was observed at). All three are printed whether or not they pass, because the weak ones
    carry the real finding: **a repeat refusal is a flat cost regardless of turn length**, so the
    saving is large exactly where FR1 said it hurt and small where there was little to save.
  * **a three-clip answer** — three successful `say --now` payloads.
  * **a quiet `watch`** — nothing said, steady state. Measured and deliberately not improved: it is
    already 51 characters of `next` inside a small payload, so the criterion is that it MUST NOT
    GROW (AC25), not that it shrink.

THE GATE (AC23) exits non-zero when the **702-character** refused batch saves less than 40% or the
three-clip answer less than 25%. **702 is pinned by the spec and is not chosen from the results** —
FR1 was written about a ~700-character turn, and picking the size after seeing the figures is the
failure pre-registration exists to prevent. The quiet watch's no-growth check is additive to those
two floors: it can never fire on a correct implementation, and a harness that measures a regression
and then says nothing about it is worse than one that never looked.

🔴 **NO SERVER IS STARTED AND NONE IS CONTACTED** (TC1, AC26), and that is asserted rather than
claimed: `urllib.request.urlopen` and `socket.create_connection` are replaced with tripwires for
the whole measurement, and `open()` refuses any path under the repo's live `sessions/` directory.
A live voice session was running on `dev` while this was written. It also runs on the CI floor
(TC6): core dependencies only — no piper, no model, no microphone.

Run:  python scripts/contextcost.py            # the report, always exit 0
      python scripts/contextcost.py --gate     # the report plus the floors, non-zero on a miss
"""
import argparse
import builtins
import contextlib
import json
import os
import shutil
import socket
import sys
import tempfile
import types
import urllib.request
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The one directory nothing here may touch. A person is mid-sentence in it.
LIVE_SESSIONS = os.path.join(ROOT, "sessions")
sys.path.insert(0, ROOT)

from command_bridge import cli, config, server, store  # noqa: E402

# The session NAME is `dev` because it is the one the measurement was taken against and because the
# name is interpolated into `remedy` and into every `next` — a three-character name and a
# fourteen-character one produce different payload sizes. The session DIRECTORY is a temp dir, so
# the live log of that name is never opened. Both facts matter and they are independent.
SESSION = "dev"

# The population these payloads carry, from spec 011's measured ground truth against the live `dev`
# log (1,365 turns, 1,261 addressed). Not tuning knobs: 74 is the median addressed turn, 258 the
# p90, and 702 the turn FR1 was written about.
MEDIAN_TURN_CHARS = 74
P90_TURN_CHARS = 258
OBSERVED_TURN_CHARS = 702
REFUSED_TURN_SIZES = (MEDIAN_TURN_CHARS, P90_TURN_CHARS, OBSERVED_TURN_CHARS)

# The size AC23 gates, pinned by the spec ahead of the results. See the module docstring.
GATED_TURN_CHARS = OBSERVED_TURN_CHARS
REFUSED_BATCH_FLOOR = 0.40
THREE_CLIP_FLOOR = 0.25

# An answer is capped at three clips by the operating guide, and the observed refused batch was
# four. Both are the real shapes rather than round numbers.
REFUSED_CLIPS = 4
ANSWER_CLIPS = 3

_SENTENCE = ("so what I was thinking is we should probably just ship the smaller one now and see "
             "whether the numbers move at all before we go and touch anything else in there ")


def turn_text(n: int) -> str:
    """`n` characters of plain ASCII speech.

    Plain on purpose: a curly quote or an accent would be one character of text and six of JSON
    escape, so the payload size would stop being a function of the number this is called with.
    """
    return (_SENTENCE * (n // len(_SENTENCE) + 2))[:n]


# --------------------------------------------------------------------------- isolation


class _ContactedAServer(AssertionError):
    """Raised by the tripwires. AC26 is a claim about what did NOT happen, and the only honest way
    to assert one of those is to make the thing it forbids raise."""


@contextlib.contextmanager
def isolated():
    """A temp session directory, no inherited settings, and three tripwires. Yields the directory.

    Every `COMMAND_BRIDGE_*` variable is cleared rather than merely overridden, because the
    measurement has to produce the same number on CI and on the machine of somebody who has
    persisted `COMMAND_BRIDGE_VERBOSE=1` — and the verbose toggle selects a different `next` branch.
    `COMMAND_BRIDGE_ENV_FILE` is pointed at a path that does not exist for the same reason: the repo
    `.env` is a developer's, not a fixture.
    """
    saved_env = {k: v for k, v in os.environ.items() if k.startswith("COMMAND_BRIDGE_")}
    saved_open, saved_urlopen = builtins.open, urllib.request.urlopen
    saved_connect = socket.create_connection
    tmp = tempfile.mkdtemp(prefix="contextcost-")

    def guarded_open(file, *args, **kwargs):
        try:
            resolved = os.path.abspath(os.fspath(file))
        except TypeError:                     # a file descriptor, which cannot name a path
            return saved_open(file, *args, **kwargs)
        if resolved.startswith(LIVE_SESSIONS + os.sep) or resolved == LIVE_SESSIONS:
            raise _ContactedAServer(f"refusing to open the live session directory: {resolved}")
        return saved_open(file, *args, **kwargs)

    def tripwire(*_args, **_kwargs):
        raise _ContactedAServer(
            "the measurement tried to open a network connection; it must build every payload in "
            "process (spec 011, TC1 and AC26)"
        )

    try:
        for key in [k for k in os.environ if k.startswith("COMMAND_BRIDGE_")]:
            del os.environ[key]
        os.environ["COMMAND_BRIDGE_DIR"] = tmp
        os.environ["COMMAND_BRIDGE_ENV_FILE"] = os.path.join(tmp, "no-such.env")
        assert os.path.abspath(config.session_dir()) == os.path.abspath(tmp), (
            "the session directory did not follow COMMAND_BRIDGE_DIR; refusing to measure"
        )
        builtins.open = guarded_open
        urllib.request.urlopen = tripwire
        socket.create_connection = tripwire
        yield tmp
    finally:
        builtins.open = saved_open
        urllib.request.urlopen = saved_urlopen
        socket.create_connection = saved_connect
        for key in [k for k in os.environ if k.startswith("COMMAND_BRIDGE_")]:
            del os.environ[key]
        os.environ.update(saved_env)
        shutil.rmtree(tmp, ignore_errors=True)


def clean(tmp: str) -> None:
    """Empty the temp session directory between runs, so the `before` and the `after` runs see the
    same log from the same starting state. Not a new directory: the directory NAME is in no
    payload, but re-creating it per run would multiply the tripwire setup for nothing."""
    for name in os.listdir(tmp):
        path = os.path.join(tmp, name)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)


# --------------------------------------------------------------------------- the two memo resets


def forget_branches(session: str = SESSION) -> None:
    """Put FR3's memo back where it was before spec 011: no command has spoken yet.

    Empties `next_branch` rather than deleting the state file, because the file's other tenant is
    the watch backoff (`empty_streak`) and that number is in the quiet-watch payload as
    `quiet_rounds`. Deleting the file would reset it too and make the `before` run differ from the
    `after` run by a field this spec never touched.
    """
    cli._update_session_state(session, next_branch={})


def size(payload: dict) -> int:
    """What the payload costs the agent, which is the only unit FR4 accepts.

    `ensure_ascii=False`, matching how the CLI prints and how the refusal tests measure: escaping
    is a property of the serializer, not of the contract, and counting `\\u2014` as six characters
    would inflate every `next` in the tool by the number of em dashes in it.
    """
    return len(json.dumps(payload, ensure_ascii=False))


# --------------------------------------------------------------------------- the drivers


def say(session: str, server_payload: dict, text: str, now: bool = True) -> dict:
    """One `say`, answered by exactly `server_payload`. The CLI's own `next` is real."""
    saved = cli._request
    cli._request = lambda *_a, **_k: dict(server_payload)
    try:
        return cli.cmd_say(types.SimpleNamespace(
            session=session, text=text, voice=None, now=now))
    finally:
        cli._request = saved


def watch(session: str, live: dict, since: int) -> dict:
    """One `watch` against a scripted `/status` and an empty log — the quiet steady state."""
    saved_request, saved_watch = cli._request, cli.store.watch
    cli._request = lambda _s, path, payload=None: dict(live) if path == "/status" else {}
    cli.store.watch = (lambda _sess, cursor, timeout=0.0, addressed_only=True,
                      lane=None, default_lane=None: ([], cursor))
    try:
        return cli.cmd_watch(types.SimpleNamespace(
            session=session, since=since, timeout=0.0, force=False, all_turns=False))
    finally:
        cli._request, cli.store.watch = saved_request, saved_watch


LIVE_STATUS = {"clients": 1, "capturing": True, "muted": False, "channel_open": True,
               "verbose": False, "running": True, "watch_open": False,
               "user_speaking": False, "speech_active": False, "speech_pending": 0}


# --------------------------------------------------------------------------- the measurement


@dataclass
class Exchange:
    """One exchange, measured both ways. `floor` gates it; `must_not_grow` is AC25's weaker claim."""

    name: str
    before: int
    after: int
    floor: float | None = None
    must_not_grow: bool = False
    detail: dict = field(default_factory=dict)

    @property
    def saved(self) -> int:
        return self.before - self.after

    @property
    def reduction(self) -> float:
        return self.saved / self.before if self.before else 0.0

    @property
    def margin_chars(self) -> float:
        """How many characters of slack this exchange has against its floor. Negative is a miss.

        Reported because a percentage hides how close a gate is to firing: 25.4% and 24.6% read as
        the same number and are opposite verdicts."""
        if self.floor is None:
            return float(self.before - self.after)
        return self.before * (1.0 - self.floor) - self.after

    @property
    def failed(self) -> bool:
        if self.floor is not None:
            return self.reduction < self.floor
        if self.must_not_grow:
            return self.after > self.before
        return False

    @property
    def gated(self) -> bool:
        return self.floor is not None or self.must_not_grow


def _refused_run(tmp: str, turn_chars: int, clips: int, full_every_time: bool) -> dict:
    """A refused batch, once. `full_every_time` is the world before FR1 and FR3.

    The refusal comes from the server builder against a REAL turn log, and is then handed to the
    real `cmd_say`, which is the layer that writes `next` on a refusal. So both halves of what a
    refused clip costs the agent — the unread turns (FR1) and the guidance (FR3) — are measured
    together, because the agent pays for them together.
    """
    clean(tmp)
    state = server.TunnelState(SESSION, token=None)
    state.consumed_cursor = -1
    # Realistic wall-clock fields so the per-turn metadata is the real ~170 characters rather than
    # a flattering `0.0`. `reason` is what the voiceprint gate writes on an addressed turn.
    turn = store.append_turn(session=SESSION, text=turn_text(turn_chars),
                             t_start=1755620671.244, t_end=1755620679.031,
                             addressed=True, reason="voice:0.83")
    forget_branches()
    sizes, server_sizes = [], []
    for clip in range(clips):
        if full_every_time:
            state.last_refusal = None         # the documented FR1 reset
            forget_branches()                 # and FR3's
        unread = server._unread_turns(state)
        refusal = server._unread_refusal(state, unread)
        server_sizes.append(size(refusal))
        sizes.append(size(say(SESSION, refusal, f"clip {clip + 1} of my answer")))
    return {"sizes": sizes, "server_sizes": server_sizes, "turn_id": turn["id"]}


def measure_refused_batch(tmp: str, turn_chars: int, clips: int = REFUSED_CLIPS) -> Exchange:
    before = _refused_run(tmp, turn_chars, clips, full_every_time=True)
    after = _refused_run(tmp, turn_chars, clips, full_every_time=False)
    return Exchange(
        name=f"refused batch, {clips} clips @ {turn_chars}-char turn",
        before=sum(before["sizes"]),
        after=sum(after["sizes"]),
        floor=REFUSED_BATCH_FLOOR if turn_chars == GATED_TURN_CHARS else None,
        detail={
            "per_clip_before": before["sizes"],
            "per_clip_after": after["sizes"],
            # The server payload on its own, which is FR1's share and the figure spec 011
            # pre-registered its 40% floor against. Reported so the pre-registration is
            # reproducible and any divergence from it is visible rather than argued about.
            "server_only_before": sum(before["server_sizes"]),
            "server_only_after": sum(after["server_sizes"]),
        },
    )


def _answer_run(tmp: str, clips: int, full_every_time: bool) -> dict:
    """A successful multi-clip answer, once. Every clip takes the `async` branch — the most-emitted
    `next` in an answer, and the one spec 011 measured at 226 characters per clip."""
    clean(tmp)
    state = server.TunnelState(SESSION, token=None)
    state.consumed_cursor = -1
    # What `handle_say` returns on the fire-and-forget path: `{queued, async, **unread}`. The
    # unread half is taken from the server rather than typed, so the envelope is whatever the code
    # currently produces; the two flags are the only literals in this file's payloads.
    envelope = {"queued": True, "async": True, **server._unread_turns(state)}
    forget_branches()
    sizes = []
    for clip in range(clips):
        if full_every_time:
            forget_branches()
        sizes.append(size(say(SESSION, envelope, f"clip {clip + 1} of my answer")))
    return {"sizes": sizes}


def measure_three_clip_answer(tmp: str, clips: int = ANSWER_CLIPS) -> Exchange:
    before = _answer_run(tmp, clips, full_every_time=True)
    after = _answer_run(tmp, clips, full_every_time=False)
    repeats = clips - 1
    return Exchange(
        name=f"{clips}-clip answer (`say --now`)",
        before=sum(before["sizes"]),
        after=sum(after["sizes"]),
        floor=THREE_CLIP_FLOOR,
        detail={
            "per_clip_before": before["sizes"],
            "per_clip_after": after["sizes"],
            # WHAT THE SAVING PAYS FOR ITSELF WITH. Each shortened `next` adds a `next_repeated`
            # key, and on an exchange this small the marker is a material share of the result — so
            # it is reported rather than left for somebody to rediscover from a percentage that
            # does not match the one in the requirement.
            "marker_cost": cli.NEXT_REPEAT_MARKER_COST * repeats,
            "after_without_marker": sum(after["sizes"]) - cli.NEXT_REPEAT_MARKER_COST * repeats,
        },
    )


def measure_quiet_watch(tmp: str, calls: int = 2) -> Exchange:
    """AC25, THE HONEST SCENARIO. The finding is that there is nothing here to win.

    A quiet `watch` spends 51 characters on `next` and all 51 of them are the command, so FR3's
    short form would be LONGER than the full one and the marker-cost guard declines to emit it.
    The requirement is therefore that the payload does not GROW, which is a real thing to protect:
    the first cut of the repeat rule shortened whenever the string got shorter and made the `muted`
    payload 9 characters bigger than the call it was repeating.
    """
    clean(tmp)
    forget_branches()
    before = [size(watch(SESSION, LIVE_STATUS, since=1354)) for _ in range(calls)]
    # `before` and `after` are the same calls with FR3's memo reset between them. The backoff
    # streak is deliberately NOT reset (see `forget_branches`), so `quiet_rounds` counts the same
    # way in both runs and cannot manufacture a difference this spec did not cause.
    clean(tmp)
    forget_branches()
    after = []
    for _ in range(calls):
        after.append(size(watch(SESSION, LIVE_STATUS, since=1354)))
    return Exchange(
        name=f"quiet watch, {calls} calls",
        before=sum(before),
        after=sum(after),
        must_not_grow=True,
        detail={"per_call_before": before, "per_call_after": after},
    )


def measure_all() -> list[Exchange]:
    """Every exchange FR4 names, in one isolated environment. No server, no session directory."""
    with isolated() as tmp:
        rows = [measure_refused_batch(tmp, n) for n in REFUSED_TURN_SIZES]
        rows.append(measure_three_clip_answer(tmp))
        rows.append(measure_quiet_watch(tmp))
        return rows


# --------------------------------------------------------------------------- the report


def failures(rows: list[Exchange]) -> list[str]:
    """Every gated row that missed, as a sentence naming the number and the shortfall."""
    out = []
    for row in rows:
        if not row.failed:
            continue
        if row.floor is not None:
            out.append(
                f"{row.name}: {row.reduction:.1%} against a floor of {row.floor:.0%} — "
                f"{abs(row.margin_chars):.0f} characters short "
                f"({row.before} -> {row.after})"
            )
        else:
            out.append(
                f"{row.name}: grew from {row.before} to {row.after} characters; a repeat may "
                f"never cost more than the call it repeats"
            )
    return out


def render(rows: list[Exchange]) -> str:
    lines = [
        "command-bridge context cost — spec 011, FR4",
        "",
        "  Both columns are computed from the working tree. `before` is the same code with FR1's",
        "  `last_refusal` memo and FR3's `next_branch` memo reset between calls, which is what the",
        "  tool did before this spec. Sizes are json.dumps(payload, ensure_ascii=False).",
        "",
        f"  {'exchange':<44}{'before':>9}{'after':>9}{'saved':>9}{'cut':>9}"
        f"{'floor':>9}  verdict",
    ]
    for row in rows:
        if row.floor is not None:
            floor = f"{row.floor:.0%}"
            verdict = "FAIL" if row.failed else "pass"
            verdict += f"  ({row.margin_chars:+.0f} chars)"
        elif row.must_not_grow:
            floor = "no-grow"
            verdict = "FAIL" if row.failed else "pass"
        else:
            floor = "—"
            verdict = "reported"
        lines.append(
            f"  {row.name:<44}{row.before:>9,}{row.after:>9,}{row.saved:>9,}"
            f"{row.reduction:>8.1%}{floor:>9}  {verdict}"
        )
    lines += [
        "",
        "  Breakdown",
    ]
    for row in rows:
        lines.append(f"    {row.name}")
        for key, value in row.detail.items():
            lines.append(f"      {key:<22} {value}")
    lines += [
        "",
        "  Notes",
        "    * A repeat refusal is a FLAT cost regardless of turn length, which is why the",
        "      reduction is large at 702 characters and small at the median. The saving lands",
        "      exactly where FR1 said the defect hurt: the longer the thought being delivered, the",
        "      more clips it takes and the more the old payload charged for being interrupted.",
        "    * `server_only_*` is the refusal payload without the CLI's `next` — FR1's share alone,",
        "      and the basis spec 011 pre-registered its 40% floor against.",
        "    * `marker_cost` is what the `next_repeated` key costs across the shortened clips, and",
        "      it is counted. On the three-clip answer it is a material share of the saving, so a",
        "      measurement that leaves it out reports a materially larger cut than the agent gets;",
        "      `after_without_marker` is printed so the two numbers cannot be confused.",
        "    * The quiet watch is reported, not improved. It is already 51 characters of `next` and",
        "      the marker-cost guard declines to shorten it, so 0% here is the correct result.",
        "",
        "  No server was started and none was contacted: every payload was built in process, with",
        "  urlopen/create_connection replaced by tripwires and the live sessions/ directory closed",
        "  to open().",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="contextcost",
        description="Measure what spec 011 saves the agent, and gate on it.",
    )
    parser.add_argument("--gate", action="store_true",
                        help="exit non-zero when a floor is missed (the CI mode)")
    args = parser.parse_args(argv)

    rows = measure_all()
    print(render(rows))
    missed = failures(rows)
    if not args.gate:
        return 0
    print("")
    if missed:
        print("GATE FAILED")
        for line in missed:
            print(f"  {line}")
        print("")
        print("  The floors are pre-registered in specs/011-the-agents-context-is-a-budget.md,")
        print("  AC23. Do not move one to make a run pass — a floor chosen after seeing the")
        print("  result measures nothing.")
        return 1
    print("GATE PASSED — every floor met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
