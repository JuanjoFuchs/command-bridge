"""The verbatim command-audit log (JJ, 2026-09-15: "logs where we can see verbatim what commands
the agents are using"). These pin the two things that make it safe and useful: it captures the argv
as typed, and it NEVER writes the token to disk."""
from __future__ import annotations

from command_bridge import audit


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(audit.config, "session_dir", lambda: str(tmp_path))


def test_it_records_the_verbatim_argv_with_verb_and_lane(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    audit.record(["watch", "--lane", "magnus", "--since", "42", "--reply"],
                 session="dev", verb="watch", lane="magnus")
    entries = audit.read("dev")
    assert len(entries) == 1
    e = entries[0]
    assert e["verb"] == "watch" and e["lane"] == "magnus"
    assert e["argv"] == ["watch", "--lane", "magnus", "--since", "42", "--reply"]
    assert "wall" in e
    # The whole point of the flag fix: a reader can SEE --reply was passed.
    assert "--reply" in e["argv"]


def test_the_token_is_redacted_and_never_hits_disk(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    secret = "s3cr3t-do-not-log-me"
    audit.record(["serve", "--session", "dev", "--token", secret], session="dev", verb="serve")
    # Redacted in the parsed entry ...
    assert audit.read("dev")[0]["argv"] == ["serve", "--session", "dev", "--token", "***"]
    # ... and, the load-bearing assertion, the secret is nowhere in the raw file.
    assert secret not in audit.path("dev") or True  # path itself never holds it
    with open(audit.path("dev"), encoding="utf-8") as fh:
        assert secret not in fh.read(), "a credential must never land in the audit log"


def test_the_equals_form_of_the_token_is_redacted_too(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    audit.record(["serve", "--token=s3cr3t"], session="dev", verb="serve")
    assert audit.read("dev")[0]["argv"] == ["serve", "--token=***"]
    with open(audit.path("dev"), encoding="utf-8") as fh:
        assert "s3cr3t" not in fh.read()


def test_a_large_canvas_blob_is_summarised_not_dumped(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    blob = "x" * 5000
    audit.record(["set", "--lane", "magnus", "--html", blob], session="dev", verb="set", lane="magnus")
    argv = audit.read("dev")[0]["argv"]
    # The flag and the command shape survive; the megabyte body does not.
    assert argv[:4] == ["set", "--lane", "magnus", "--html"]
    assert argv[4].startswith("x" * 160) and "<5000 chars>" in argv[4]
    assert len(argv[4]) < 250, "the blob value must be summarised, not stored whole"


def test_a_short_value_is_left_verbatim(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    audit.record(["cue", "--text", "look here"], session="dev", verb="cue")
    assert audit.read("dev")[0]["argv"] == ["cue", "--text", "look here"]


def test_report_tallies_by_verb_and_filters(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    audit.record(["watch", "--lane", "magnus", "--reply"], session="dev", verb="watch", lane="magnus")
    audit.record(["watch", "--lane", "atlas"], session="dev", verb="watch", lane="atlas")
    audit.record(["say", "--lane", "magnus", "hi"], session="dev", verb="say", lane="magnus")
    rep = audit.report("dev")
    assert rep["total"] == 3
    assert rep["by_verb"] == {"watch": 2, "say": 1}
    # A filter narrows `commands` but the tally stays over the whole log.
    only_magnus = audit.report("dev", lane="magnus")
    assert only_magnus["shown"] == 2 and only_magnus["total"] == 3
    only_say = audit.report("dev", verb="say")
    assert only_say["shown"] == 1 and only_say["commands"][0]["verb"] == "say"


def test_a_missing_log_reads_empty_not_fatal(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert audit.read("never-ran") == []
    rep = audit.report("never-ran")
    assert rep["total"] == 0 and rep["commands"] == []


def test_a_malformed_line_is_skipped(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    audit.record(["status"], session="dev", verb="status")
    with open(audit.path("dev"), "a", encoding="utf-8") as fh:
        fh.write("{ this is not json\n")
    audit.record(["turns"], session="dev", verb="turns")
    verbs = [e["verb"] for e in audit.read("dev")]
    assert verbs == ["status", "turns"], "the bad line is skipped, the good ones survive"


def test_recording_never_raises(monkeypatch):
    """An audit trail that can crash the command it records is worse than none."""
    def boom():
        raise OSError("disk full")
    monkeypatch.setattr(audit.config, "session_dir", boom)
    audit.record(["watch"], session="dev", verb="watch")  # must not raise
