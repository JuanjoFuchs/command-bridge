"""The saving is a number, and the number is a gate. Spec 011, FR4, AC22-AC26.

**A measurement is only worth having if it can come out badly**, and this file spends most of its
assertions on that: the gate is driven to FAIL on a payload that does not save (AC24), the honest
scenario is asserted not to have been improved into a fiction (AC25), and the harness is asserted
to touch no server and no live session directory (AC26).

The subject is `scripts/contextcost.py`, which computes BOTH columns from the working tree — the
`before` by resetting the two memories spec 011 added (`TunnelState.last_refusal` for FR1, the
`next_branch` map for FR3), the `after` by leaving them alone. That is what keeps the number from
rotting into a stale constant: there is no recorded "before" anywhere for a later payload change to
falsify.

🔴 NO SERVER IS STARTED OR CONTACTED HERE, and a live voice session was running on `dev` while this
was written (TC1). The script isolates its own session directory and installs tripwires; this file
asserts that it does rather than trusting it.
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "contextcost.py")

# Loaded by path rather than imported: `scripts/` is not a package, and making it one to satisfy a
# test would change how every other harness in it is invoked.
_spec = importlib.util.spec_from_file_location("contextcost", SCRIPT)
contextcost = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contextcost)


@pytest.fixture(scope="module")
def rows():
    """The real measurement, once. Every assertion below reads the same numbers, so a test cannot
    pass against a measurement a different test would not have got."""
    return contextcost.measure_all()


def row_for(rows, needle):
    matches = [r for r in rows if needle in r.name]
    assert len(matches) == 1, f"expected exactly one {needle!r} row, got {[r.name for r in rows]}"
    return matches[0]


def gated_refusal(rows):
    return row_for(rows, f"@ {contextcost.GATED_TURN_CHARS}-char")


# ------------------------------------------------------------------ AC22: all three, all sizes


def test_the_report_covers_every_exchange_the_requirement_names(rows):
    """AC22. Three exchanges, and the refused batch at three turn sizes.

    The sizes are not a sweep for its own sake. **The turn size dominates this number** — a repeat
    refusal costs the same whatever was said, so the saving is a large fraction of a big payload
    and a small fraction of a small one. Reporting only the flattering size would describe a
    different fix from the one that shipped."""
    names = [r.name for r in rows]

    assert len(rows) == 5, names
    for chars in contextcost.REFUSED_TURN_SIZES:
        assert any(f"@ {chars}-char" in n for n in names), f"no refused batch at {chars}: {names}"
    assert any("clip answer" in n for n in names), names
    assert any("quiet watch" in n for n in names), names
    assert contextcost.REFUSED_TURN_SIZES == (74, 258, 702), (
        "the median, the p90 and the observed turn, from spec 011's measured ground truth"
    )


def test_every_row_carries_a_before_an_after_and_a_reduction(rows):
    """AC22's actual demand: characters before, characters after, and the reduction — per row."""
    for row in rows:
        assert row.before > 0, row.name
        assert row.after > 0, row.name
        assert row.after <= row.before, f"{row.name}: {row.before} -> {row.after} is not a saving"
        assert row.reduction == pytest.approx((row.before - row.after) / row.before)
        assert row.saved == row.before - row.after


def test_the_weak_sizes_are_printed_rather_than_hidden(rows):
    """AC22, the half that is easy to skip. **The sizes that do not clear 40% carry the finding.**

    A repeat refusal is a flat cost, so the median turn saves little and the observed one saves a
    lot. Printing only the gated size would let a reader conclude the fix is uniformly large; the
    truth is that it is concentrated exactly where FR1 said the defect hurt."""
    printed = contextcost.render(rows)

    for chars in contextcost.REFUSED_TURN_SIZES:
        assert f"@ {chars}-char" in printed, f"the {chars}-char row is missing from the report"
    ungated = [r for r in rows if "refused batch" in r.name and r.floor is None]
    assert len(ungated) == 2, "the median and the p90 are reported, not gated"
    assert all(r.reduction < contextcost.REFUSED_BATCH_FLOOR for r in ungated), (
        "if the small turns ever clear the gated floor, the flat-cost finding has changed and the "
        "report's explanation of it is no longer true"
    )


def test_report_mode_never_gates(monkeypatch, capsys):
    """The two modes are different jobs. A report that exited non-zero could not be used to look at
    a number, which is what FR4 asks for first."""
    monkeypatch.setattr(contextcost, "measure_all", lambda: [
        contextcost.Exchange(name="nothing saved at all", before=100, after=100, floor=0.40),
    ])

    assert contextcost.main([]) == 0
    assert contextcost.main(["--gate"]) == 1
    assert "GATE FAILED" in capsys.readouterr().out


# ------------------------------------------------------------------ AC23: the floors, pre-registered


def test_the_floors_are_the_ones_the_spec_pre_registered():
    """AC23. **The numbers live here as constants so that changing one is a visible edit.**

    702 is the size FR1 was written about — the metaspec's own words are "each refusal carried the
    full text of the same ~700-character turn" — and it is pinned for that reason rather than
    because of how the three sizes came out. A floor picked after seeing the results measures
    nothing at all, which is the whole reason the spec states its reasoning before the figures."""
    assert contextcost.GATED_TURN_CHARS == 702
    assert contextcost.REFUSED_BATCH_FLOOR == 0.40
    assert contextcost.THREE_CLIP_FLOOR == 0.25


def test_only_the_observed_turn_size_is_gated(rows):
    """AC23. The median and the p90 are reported; a gate on either would fail on a correct
    implementation, because a flat repeat cost cannot be 40% of a small payload."""
    gated = [r for r in rows if r.floor is not None]

    assert {r.name for r in gated} == {gated_refusal(rows).name, row_for(rows, "clip answer").name}
    assert gated_refusal(rows).floor == contextcost.REFUSED_BATCH_FLOOR
    assert row_for(rows, "clip answer").floor == contextcost.THREE_CLIP_FLOOR


def test_the_gate_returns_zero_when_every_floor_is_met(monkeypatch, capsys):
    """AC23's other direction, and it needs synthetic rows to be a real test.

    Driven from constructed measurements rather than from the live ones on purpose: this asserts
    the GATE's arithmetic — that meeting a floor exits 0 — and wiring it to the current payloads
    would make it a test of the payloads instead, passing or failing for reasons that have nothing
    to do with the code under test."""
    monkeypatch.setattr(contextcost, "measure_all", lambda: [
        contextcost.Exchange(name="refused batch @ 702-char turn", before=1000, after=550,
                             floor=contextcost.REFUSED_BATCH_FLOOR),
        contextcost.Exchange(name="3-clip answer", before=1000, after=700,
                             floor=contextcost.THREE_CLIP_FLOOR),
        contextcost.Exchange(name="quiet watch", before=100, after=100, must_not_grow=True),
    ])

    assert contextcost.main(["--gate"]) == 0
    assert "GATE PASSED" in capsys.readouterr().out


def test_a_floor_missed_by_one_character_is_a_miss(capsys):
    """The boundary, asserted, because a percentage rounds and a gate must not.

    A 25% floor on a 1,000-character exchange permits an `after` of exactly 750 and refuses 751.
    Reported in characters as well as in percent for the same reason: 25.0% and 24.96% print the
    same and are opposite verdicts."""
    exactly = contextcost.Exchange(name="on the line", before=1000, after=750, floor=0.25)
    one_over = contextcost.Exchange(name="one character short", before=1000, after=751, floor=0.25)

    assert not exactly.failed and exactly.margin_chars == pytest.approx(0.0)
    assert one_over.failed and one_over.margin_chars == pytest.approx(-1.0)
    assert "1 characters short" in contextcost.failures([one_over])[0]
    assert contextcost.failures([exactly]) == []


# ------------------------------------------------------------------ AC24: the gate is proven to fire


def test_the_gated_refusal_clears_its_floor_on_the_shipped_code(rows):
    """**The negative control for the test below**, and it has to come first.

    Showing a gate fail on broken code proves nothing unless the same gate passes on the real
    code — otherwise it might be a gate that fails on everything."""
    gated = gated_refusal(rows)

    assert not gated.failed, (
        f"the refused batch at {contextcost.GATED_TURN_CHARS} characters saves "
        f"{gated.reduction:.1%}, below FR4's {contextcost.REFUSED_BATCH_FLOOR:.0%} floor"
    )
    assert gated.margin_chars > 0


def test_the_gate_fires_on_a_refusal_that_does_not_save(monkeypatch, capsys):
    """**AC24. A gate whose failure has never been observed is indistinguishable from one that
    cannot fail**, and this repo has shipped a denylist that refused nothing.

    The broken build is made through FR1's own documented reset seam: `state.last_refusal = None`
    before every call means the memo never matches, so every refusal in the batch is a FIRST and
    carries the turn text in full — which is exactly what the tool did before spec 011. Nothing is
    faked; the real builder runs, against a real turn log, and simply never remembers.

    The assertion names the refused-batch row specifically rather than checking the exit code
    alone. **An exit code is a weak assertion when another row can also fail** — it would pass
    while this arm was perfectly healthy and something unrelated was red."""
    real = contextcost.server._unread_refusal

    def never_remembers(state, unread):
        state.last_refusal = None
        return real(state, unread)

    monkeypatch.setattr(contextcost.server, "_unread_refusal", never_remembers)
    broken = contextcost.measure_all()
    gated = gated_refusal(broken)

    assert gated.failed, (
        f"a batch in which every refusal carries the full turn saved {gated.reduction:.1%}; the "
        f"gate did not fire on a payload with no FR1 saving in it at all"
    )
    assert any(f"@ {contextcost.GATED_TURN_CHARS}-char" in line
               for line in contextcost.failures(broken)), contextcost.failures(broken)

    monkeypatch.setattr(contextcost, "measure_all", lambda: broken)
    assert contextcost.main(["--gate"]) != 0, "the gate must exit non-zero, not merely complain"
    printed = capsys.readouterr().out
    assert "GATE FAILED" in printed
    assert "characters short" in printed, "a failure has to say by how much"


def test_the_broken_build_is_broken_in_the_way_the_test_claims(monkeypatch):
    """The arm above is only a control if the neutering did what it says.

    Asserted on the per-clip sizes rather than on the total: with FR1 disabled every clip of the
    refused batch costs the same as the first, which is the shape of the defect the requirement
    was written about — four copies of one turn to report one event."""
    real = contextcost.server._unread_refusal

    def never_remembers(state, unread):
        state.last_refusal = None
        return real(state, unread)

    monkeypatch.setattr(contextcost.server, "_unread_refusal", never_remembers)
    with contextcost.isolated() as tmp:
        broken = contextcost._refused_run(tmp, contextcost.GATED_TURN_CHARS, 4,
                                          full_every_time=False)
        monkeypatch.setattr(contextcost.server, "_unread_refusal", real)
        healthy = contextcost._refused_run(tmp, contextcost.GATED_TURN_CHARS, 4,
                                           full_every_time=False)

    assert len(set(broken["server_sizes"])) == 1, (
        "with the memo cleared every refusal is a first, so all four server payloads are equal"
    )
    assert healthy["server_sizes"][0] > healthy["server_sizes"][1], "the shipped code trims"
    assert len(set(healthy["server_sizes"][1:])) == 1, (
        "and every repeat costs the same — the price of being refused does not grow with the count"
    )


# ------------------------------------------------------------------ AC25: the honest scenario


def test_the_quiet_watch_does_not_grow(rows):
    """**AC25, and the criterion is deliberately weaker than the others because the finding is that
    there is nothing to win here.**

    Spec 011 measured a quiet `watch` at 278 characters, 51 of them `next`, and all 51 of those are
    the command itself — there is no rationale to cut, so FR3's short form would be LONGER than the
    full one and the marker-cost guard declines to emit it. Asserting a saving here would have
    required inventing one; asserting no growth protects the real property, and growth is a real
    risk: the first cut of the repeat rule made the `muted` payload 9 characters BIGGER than the
    call it was repeating."""
    quiet = row_for(rows, "quiet watch")

    assert quiet.must_not_grow and quiet.floor is None
    assert quiet.after <= quiet.before, (
        f"the quiet watch grew from {quiet.before} to {quiet.after} characters"
    )
    assert quiet.detail["per_call_after"] == quiet.detail["per_call_before"], (
        "a repeat of the cheapest branch in the tool must be byte-for-byte what the first call was"
    )


def test_a_growing_quiet_watch_fails_the_gate():
    """AC25 with teeth. Measuring a regression and then saying nothing about it would make the
    harness a decoration, so the no-growth check is gated alongside AC23's two floors."""
    grew = contextcost.Exchange(name="quiet watch", before=300, after=309, must_not_grow=True)

    assert grew.failed
    assert "may never cost more than the call it repeats" in contextcost.failures([grew])[0]


# ------------------------------------------------------------------ AC26: no server, CI floor


def test_the_measurement_contacts_no_server(rows):
    """**AC26, asserted rather than claimed.** `rows` was produced with these tripwires installed —
    the fixture is the proof — and this checks that the tripwires are real rather than decorative.

    It matters more here than anywhere else in the suite: a live voice session was running on `dev`
    while this was written, and a harness that opened a socket to it would interrupt a person
    mid-sentence (TC1)."""
    assert rows, "the measurement produced nothing, so it proved nothing"
    real_urlopen = contextcost.urllib.request.urlopen
    real_connect = contextcost.socket.create_connection

    with contextcost.isolated():
        assert contextcost.urllib.request.urlopen is not real_urlopen, "no tripwire was installed"
        with pytest.raises(contextcost._ContactedAServer):
            contextcost.urllib.request.urlopen("http://127.0.0.1:8765/status")
        with pytest.raises(contextcost._ContactedAServer):
            contextcost.socket.create_connection(("127.0.0.1", 8765))

    assert contextcost.urllib.request.urlopen is real_urlopen, "the tripwire outlived its scope"
    assert contextcost.socket.create_connection is real_connect, (
        "a harness that leaves the network broken for the rest of the suite is worse than one that "
        "never guarded it"
    )


def test_the_live_session_directory_is_closed_to_the_measurement():
    """AC26's other half, and the one the hard limit is about. `sessions/` holds a conversation a
    person is in the middle of; the harness may not read it, let alone write it."""
    live = os.path.join(contextcost.ROOT, "sessions", "dev.jsonl")

    with contextcost.isolated() as tmp:
        assert os.path.abspath(contextcost.config.session_dir()) == os.path.abspath(tmp), (
            "every path the measurement resolves has to land in the temp directory"
        )
        with pytest.raises(contextcost._ContactedAServer):
            open(live, encoding="utf-8")

    assert contextcost.LIVE_SESSIONS == os.path.join(contextcost.ROOT, "sessions")


def test_the_measurement_runs_on_the_ci_floor():
    """AC26 / TC6. **A gate that cannot run in CI is a gate nobody runs**, and CI installs core
    dependencies only — no piper, no kokoro, no sherpa-onnx, no onnxruntime, no microphone.

    Asserted by watching what the measurement imports, which is the only version of this claim that
    stays true: a check on what is installed would pass on a developer machine that happens to have
    the extras and say nothing about the CI floor."""
    heavy = ("piper", "kokoro_onnx", "sherpa_onnx", "onnxruntime", "transformers", "torch",
             "sounddevice", "playwright")
    before = set(sys.modules)

    contextcost.measure_all()

    arrived = [name for name in heavy if name not in before and name in sys.modules]
    assert not arrived, f"the measurement pulled in optional dependencies CI does not have: {arrived}"


def test_the_harness_cannot_start_a_server_because_it_cannot_spawn_one():
    """AC26. The strongest available statement that nothing is started: the script has no way to.

    Read out of the source rather than inferred from behaviour — a run that happened not to start a
    server and a script that cannot start one are different guarantees, and only the second one
    holds for the next person to edit it."""
    with open(SCRIPT, encoding="utf-8") as fh:
        source = fh.read()

    for forbidden in ("import subprocess", "import aiohttp", "os.system", "Popen"):
        assert forbidden not in source, f"contextcost.py must not be able to {forbidden}"


# ------------------------------------------------ the accounting the numbers depend on


def test_the_marker_the_short_form_pays_for_is_counted(rows):
    """**Where a materially larger figure comes from, pinned so it cannot be reported by accident.**

    FR3's short form costs a `next_repeated` key on every payload it shortens, and on an exchange
    as small as a three-clip answer that marker is a real share of the saving. Leaving it out
    reports a cut the agent does not get — so it is counted in `after`, and `after_without_marker`
    is printed beside it so the two can never be mistaken for one another.

    `cli.NEXT_REPEAT_MARKER_COST` is read from the CLI rather than restated, so renaming the field
    moves this number instead of silently invalidating it."""
    answer = row_for(rows, "clip answer")
    repeats = contextcost.ANSWER_CLIPS - 1

    assert answer.detail["marker_cost"] == contextcost.cli.NEXT_REPEAT_MARKER_COST * repeats
    assert answer.detail["after_without_marker"] + answer.detail["marker_cost"] == answer.after
    assert answer.detail["marker_cost"] > 0, (
        "a marker that costs nothing would mean the field is not in the payload at all"
    )


def test_both_columns_come_from_the_live_code_and_not_from_a_recorded_number(rows):
    """FR4's design constraint, asserted structurally: there is no historical constant to rot.

    The `before` column is the same code with the two memos reset, so a payload change moves BOTH
    columns and the gate keeps asserting the property — a repeat costs materially less than a
    first — rather than a figure somebody measured once."""
    batch = gated_refusal(rows)

    assert len(set(batch.detail["per_clip_before"])) == 1, (
        "every clip of the `before` run pays full price, which is what the tool did before FR1"
    )
    assert batch.detail["per_clip_after"][0] == batch.detail["per_clip_before"][0], (
        "the FIRST refusal is unchanged by this spec and must measure identically in both columns"
    )
    assert len(set(batch.detail["per_clip_after"][1:])) == 1, (
        "and every repeat after it costs the same flat amount"
    )
    assert batch.detail["server_only_before"] > batch.detail["server_only_after"], (
        "FR1's share of the saving, reported on its own because it is the basis the 40% floor was "
        "pre-registered against"
    )
