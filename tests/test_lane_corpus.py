"""Spec 012 Slice A — the lane layer replayed against real recorded speech.

**AC-2 (NFR3)** — a single-lane session must behave EXACTLY as it does today, over every turn
ever logged here rather than over examples chosen by the author.
**AC-3** — with a real two-lane set, no turn may switch to a lane its token did not name exactly.

The corpus is JJ's own speech and is deliberately NOT in the repo, so these **skip** rather than
fail when it is absent — a fresh clone and CI stay green. Point them at it explicitly with
`COMMAND_BRIDGE_CORPUS_DIR`; they read it and never write to it.

⚠ Read read-only and never through `config.session_dir()`. The suite's autouse fixture
redirects `COMMAND_BRIDGE_DIR` at a temp path to keep tests off the developer's real sessions, and
that isolation is correct and must not be undone here — so the corpus is found by its own
variable instead.
"""
import json
import os
import re

import pytest

from command_bridge import config
from command_bridge.lanes import resolve
from command_bridge.wake import WakeGate, normalize

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _corpus_dir() -> str | None:
    for candidate in (os.environ.get("COMMAND_BRIDGE_CORPUS_DIR"), os.path.join(_REPO, "sessions")):
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def _turns() -> list[dict]:
    base = _corpus_dir()
    if not base:
        return []
    out = []
    for name in sorted(os.listdir(base)):
        if not name.endswith(".jsonl") or name.endswith(".timing.jsonl"):
            continue
        with open(os.path.join(base, name), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    turn = json.loads(line)
                except json.JSONDecodeError:
                    continue          # a half-written final line must never break a read
                if isinstance(turn, dict) and turn.get("text"):
                    out.append(turn)
    return out


CORPUS = _turns()
needs_corpus = pytest.mark.skipif(
    not CORPUS,
    reason="no recorded corpus — set COMMAND_BRIDGE_CORPUS_DIR to a sessions/ directory to run "
           "the replay guards (they are read-only)",
)


@needs_corpus
def test_a_single_lane_session_routes_every_recorded_turn_exactly_as_today():
    """**AC-2, and the guard that makes this feature safe to ship into a live daily driver.**

    With one registered lane the TC2 table's switch and refuse rows cannot fire, so the routing
    layer must be a total no-op: every turn stays on the default lane and none is withheld. A
    single refusal here would mean a turn that reaches the agent today would stop reaching it.
    """
    stays = refusals = switches = 0
    for turn in CORPUS:
        r = resolve(turn["text"], ["claude"], "claude")
        if r.action == "refuse":
            refusals += 1
        elif r.action == "switch":
            switches += 1
        else:
            stays += 1
            assert r.lane == "claude"
    assert (refusals, switches) == (0, 0), (
        f"{refusals} refusals and {switches} switches on a SINGLE-lane session over "
        f"{len(CORPUS)} recorded turns — NFR3 says there must be none of either"
    )
    assert stays == len(CORPUS)


@needs_corpus
def test_the_wake_gate_itself_is_untouched_by_the_lane_layer():
    """AC-2's other half. NFR3 is only true if the *addressed* verdict is unchanged, so replay
    every turn through the gate as configured today and confirm the lane layer never contradicts
    it: nothing the gate addressed may be withheld by routing when there is one lane."""
    gate = WakeGate(config.wake_phrases())
    for turn in CORPUS:
        text = turn["text"]
        granted = gate._find_phrase(normalize(text)) is not None
        routed = resolve(text, ["claude"], "claude")
        assert routed.action != "refuse", (
            f"routing withheld a turn the gate accepted (granted={granted}): {text[:70]!r}"
        )


@needs_corpus
def test_no_recorded_turn_switches_to_a_lane_it_did_not_name_exactly():
    """**AC-3.** Mis-routes must be structurally zero, not merely rare. Every switch in the whole
    corpus has to be one where the spoken token IS the lane name, character for character."""
    lanes = ["claude", "codex"]
    switches = []
    for turn in CORPUS:
        r = resolve(turn["text"], lanes, "claude")
        if r.action != "switch":
            continue
        words = normalize(turn["text"]).split()
        token = words[1] if len(words) > 1 else ""
        switches.append((token, r.lane, turn["text"][:60]))
        assert token == r.lane, (
            f"MIS-ROUTE: token {token!r} switched to lane {r.lane!r} — only an exact name may "
            f"switch a lane (TC2). Turn: {turn['text'][:70]!r}"
        )
    # Every switch that did happen was exact; report how many so the guard is not silently empty.
    assert all(t == lane for t, lane, _ in switches)


@needs_corpus
def test_the_corpus_actually_exercises_the_switch_path():
    """A guard that passes because nothing reached it is the quietest failure available, and this
    repo has been bitten by exactly that (a sweep that timed out while reporting 18 of 18 clean).
    So assert the corpus contains real summons rather than trusting that it does."""
    greeting_led = [t for t in CORPUS
                    if len(normalize(t["text"]).split()) >= 2
                    and normalize(t["text"]).split()[0] in config.GREETINGS]
    assert len(greeting_led) >= 50, (
        f"only {len(greeting_led)} greeting-led utterances in the corpus — the replay guards are "
        f"not measuring what they claim to"
    )


@needs_corpus
def test_the_measured_cost_of_the_ruling_is_reported(capsys):
    """Not an assertion about a threshold — a measurement printed with `-s`, so the numbers in
    spec 012 can be re-derived rather than trusted. The only assertion is the one that matters:
    mis-routes are zero for every lane set tried."""
    rows = []
    for lane_set in (["claude", "codex"], ["claude", "codex", "grok"]):
        outcome = {"switch": 0, "stay": 0, "refuse": 0}
        misroutes = 0
        for turn in CORPUS:
            r = resolve(turn["text"], lane_set, "claude")
            outcome[r.action] += 1
            if r.action == "switch":
                words = normalize(turn["text"]).split()
                if (words[1] if len(words) > 1 else "") != r.lane:
                    misroutes += 1
        assert misroutes == 0, f"{misroutes} mis-routes with lanes={lane_set}"
        rows.append((lane_set, outcome, misroutes))

    # Report the refusal rate over GREETING-LED utterances, not over all turns. Both denominators
    # are true and they differ by 20x, so printing only the flattering one would read as
    # contradicting the figures in spec 012. A refusal can only happen on a greeting-led turn, so
    # that is the population it belongs to.
    summonses = max(sum(1 for t in CORPUS
                        if len(normalize(t["text"]).split()) >= 2
                        and normalize(t["text"]).split()[0] in config.GREETINGS), 1)
    print(f"\ncorpus: {len(CORPUS)} turns, of which {summonses} are greeting-led")
    for lane_set, outcome, misroutes in rows:
        print(f"  lanes={'+'.join(lane_set):24s} "
              f"switch={outcome['switch']:4d} stay={outcome['stay']:5d} "
              f"refuse={outcome['refuse']:3d} "
              f"({100 * outcome['refuse'] / summonses:.1f}% of summonses) "
              f"misroutes={misroutes}")


def test_the_corpus_loader_survives_a_corrupt_line(tmp_path):
    """Runs with or without the corpus. A partially-written final line is normal in an append-only
    log that a live server is still writing to, and it must never make the whole log unreadable —
    the same rule `store.read_turns` follows."""
    log = tmp_path / "x.jsonl"
    log.write_text(
        '{"id":0,"text":"hey claude"}\n'
        "\n"
        "{not json at all\n"
        '{"id":1,"text":"hey codex"}\n'
        '{"id":2,"text":',                 # truncated mid-write, as a live tail really looks
        encoding="utf-8",
    )
    os.environ["COMMAND_BRIDGE_CORPUS_DIR"] = str(tmp_path)
    try:
        turns = _turns()
    finally:
        os.environ.pop("COMMAND_BRIDGE_CORPUS_DIR", None)
    assert [t["text"] for t in turns] == ["hey claude", "hey codex"]


def test_the_skip_reason_names_the_variable_that_enables_it():
    """A skip nobody can act on is a silent hole. If these guards are skipping, the message has to
    say exactly how to turn them on."""
    assert re.search(r"COMMAND_BRIDGE_CORPUS_DIR", needs_corpus.kwargs["reason"])
