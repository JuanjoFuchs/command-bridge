"""The timing log — the instrument that replaces guessing about latency.

It exists because "why is this slow" was answered by hand twice in one live session, from clip
IDs and wall-clocks, and the intuition was wrong both times in the same direction: the owner blamed
Tailscale, and the network was under 0.1 s while the AGENT spent 39 s and 60 s. A breakdown that
only measured the tool would have confirmed the wrong hypothesis, which is why `consumed ->
say_requested` is a named stage rather than an unlabelled gap.
"""
import json

import pytest

from voice_tunnel import timing


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path))
    return "t"


def _write(session, rows):
    """Write events with explicit monotonic values, so durations are asserted not measured."""
    import voice_tunnel.config as config

    with open(timing.path(session), "w", encoding="utf-8") as fh:
        for stage, mono, extra in rows:
            rec = {"stage": stage, "mono": mono, "wall": "2026-07-31T19:00:00.000-04:00"}
            rec.update(extra or {})
            fh.write(json.dumps(rec) + "\n")
    assert config  # the fixture's VOICE_TUNNEL_DIR is what put the file here


# ------------------------------------------------------------------- writing


def test_stamp_writes_both_clocks(session):
    timing.stamp(session, "utterance_end", audio_s=3.2)

    events = timing.read(session)
    assert len(events) == 1
    assert events[0]["stage"] == "utterance_end"
    assert events[0]["audio_s"] == 3.2
    # Deltas come from mono; wall exists only so a human can correlate with the turn log.
    assert isinstance(events[0]["mono"], float)
    assert "T" in events[0]["wall"]


def test_stamping_never_raises_even_when_the_directory_is_impossible(monkeypatch, tmp_path):
    """An instrument that can take down the thing it measures is worse than no instrument."""
    monkeypatch.setenv("VOICE_TUNNEL_DIR", str(tmp_path / "file-not-a-dir"))
    (tmp_path / "file-not-a-dir").write_text("in the way", encoding="utf-8")

    timing.stamp("t", "utterance_end")   # must not raise


def test_a_corrupt_line_is_skipped_not_fatal(session):
    timing.stamp(session, "utterance_end")
    with open(timing.path(session), "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    timing.stamp(session, "transcribed")

    assert [e["stage"] for e in timing.read(session)] == ["utterance_end", "transcribed"]


# ------------------------------------------------------------------ reporting


def test_the_report_attributes_time_to_the_right_stage(session):
    _write(session, [
        ("utterance_end", 100.0, {"audio_s": 4.0}),
        ("transcribed",   100.3, {"chars": 40}),
        ("turn_logged",   100.4, {"turn": 7}),
        ("consumed",      100.5, {"cursor": 7}),
        ("say_requested", 130.5, {"chars": 60}),   # 30s of AGENT
        ("synthesized",   130.9, {}),
        ("spoken",        131.0, {}),
    ])

    report = timing.report(session)
    ex = report["exchanges"][0]

    assert ex["turn"] == 7
    assert ex["total_s"] == 31.0
    assert ex["slowest"] == {"from": "consumed", "to": "say_requested", "seconds": 30.0}
    assert report["worst_step"] == "consumed -> say_requested"


def test_the_agent_stage_is_named_so_it_cannot_hide(session):
    """The whole point: the tool's stages were fast and visible, the agent's was slow and had no
    name, so every discussion of latency blamed the visible thing."""
    _write(session, [
        ("utterance_end", 0.0, {}), ("consumed", 0.2, {}), ("say_requested", 40.0, {}),
    ])

    assert "consumed -> say_requested" in timing.report(session)["by_step"][0]["step"]
    assert "AGENT" in timing.report(session)["note"]


def test_exchanges_split_on_each_new_utterance(session):
    _write(session, [
        ("utterance_end", 0.0, {"audio_s": 1.0}), ("transcribed", 0.5, {}),
        ("utterance_end", 10.0, {"audio_s": 2.0}), ("transcribed", 10.4, {}),
    ])

    report = timing.report(session)
    assert len(report["exchanges"]) == 2
    assert [round(e["total_s"], 1) for e in report["exchanges"]] == [0.5, 0.4]


def test_a_turn_the_agent_did_not_answer_has_no_reply_stages(session):
    """Staying silent is correct behaviour (guide Rule 3), not a missing measurement."""
    _write(session, [("utterance_end", 0.0, {}), ("transcribed", 0.3, {}),
                     ("turn_logged", 0.4, {"turn": 1, "addressed": False})])

    ex = timing.report(session)["exchanges"][0]
    assert [s["to"] for s in ex["steps"]] == ["transcribed", "turn_logged"]


def test_aggregates_rank_by_total_not_by_worst_single_case(session):
    """One slow exchange is anecdote; the same step topping every exchange is the thing to fix."""
    _write(session, [
        ("utterance_end", 0.0, {}), ("transcribed", 9.0, {}),      # one 9s outlier
        ("utterance_end", 20.0, {}), ("consumed", 20.1, {}), ("say_requested", 26.0, {}),
        ("utterance_end", 40.0, {}), ("consumed", 40.1, {}), ("say_requested", 46.0, {}),
    ])

    top = timing.report(session)["by_step"][0]
    assert top["step"] == "consumed -> say_requested"
    assert top["count"] == 2 and top["total_s"] == 11.8


def test_an_empty_log_reports_cleanly_rather_than_erroring(session):
    report = timing.report(session)
    assert report["exchanges"] == [] and "no timing recorded" in report["note"]
