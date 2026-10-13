"""Working vs waiting, from the session log (JJ, 2026-09-29).

    "we instruct agents that are busy implementing something to background their watch. And that
     shows them as idle when in reality they are actually working … Is there a better way to detect
     if an agent is truly idle or not?"
    "But I want to make sure that that will not conflict with the listening status."

A `command-bridge watch` call is WAITING; every other tool call and result is WORK. The fleet reports
`last_work_epoch` and `work_pending` so the page can show a busy agent as thinking even while a
backgrounded watch is open — and an agent parked in a foreground watch still reads as listening.
"""
import datetime as dt
import json

from command_bridge import fleet


def _use(tid, name, inp, ts):
    return json.dumps({"timestamp": ts, "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})


def _result(tid, ts):
    return json.dumps({"timestamp": ts, "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}})


def _read(tmp_path, lines):
    log = tmp_path / "s.jsonl"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fleet._LOG_STATE.pop(str(log), None)
    return fleet._read_claude_log(str(log), dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))


def _now(offset_s=0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_s)).isoformat()


def test_a_foreground_watch_alone_is_not_work(tmp_path):
    act = _read(tmp_path, [_use("w1", "Bash", {"command": "command-bridge watch --lane magnus --since 5"}, _now())])
    assert act["work_pending"] is False          # the running watch is waiting, not working
    assert act["last_work_epoch"] is None


def test_a_running_tool_call_is_work_even_with_a_watch_open(tmp_path):
    act = _read(tmp_path, [
        _use("w1", "Bash", {"command": "command-bridge watch --lane magnus --since 5"}, _now(-30)),
        _use("r1", "Read", {"file_path": "/x/a.py"}, _now()),
    ])
    assert act["work_pending"] is True
    assert act["last_work_epoch"] is not None


def test_finished_work_is_recent_but_no_longer_pending(tmp_path):
    act = _read(tmp_path, [_use("e1", "Edit", {"file_path": "/x/a.py", "old_string": "a", "new_string": "b"}, _now(-2)),
                           _result("e1", _now(-1))])
    assert act["work_pending"] is False
    assert act["last_work_epoch"] is not None     # the page reads "recent" off this timestamp


def test_a_backgrounded_job_is_work_until_its_notification(tmp_path):
    """JJ: "Kepler is just executing long running scripts and you show it as idle." A backgrounded
    call returns at once; the job is still running until its task-notification names its id."""
    started = _read(tmp_path, [
        _use("toolu_B1", "Bash", {"command": "python long_eval.py", "run_in_background": True}, _now(-60)),
        json.dumps({"timestamp": _now(-59), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_B1",
             "content": "Command running in background with ID: bx1."}]}}),
    ])
    assert started["work_pending"] is True
    done = _read(tmp_path, [
        _use("toolu_B1", "Bash", {"command": "python long_eval.py", "run_in_background": True}, _now(-60)),
        json.dumps({"timestamp": _now(-59), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_B1",
             "content": "Command running in background with ID: bx1."}]}}),
        json.dumps({"timestamp": _now(), "type": "attachment", "attachment": {"type": "queued_command",
                    "prompt": "<task-notification>\n<task-id>bx1</task-id>\n<tool-use-id>toolu_B1</tool-use-id>\n<status>completed</status>\n</task-notification>"}}),
    ])
    assert done["work_pending"] is False


def test_a_foreground_call_whose_output_mentions_background_is_not_a_job(tmp_path):
    """2026-09-29: grepping a session log for "running in background" made that grep read as a
    background job that never ended. Only the call's own run_in_background flag makes it a job."""
    act = _read(tmp_path, [
        _use("toolu_G1", "Bash", {"command": "grep -o 'running in background' log.jsonl"}, _now(-60)),
        json.dumps({"timestamp": _now(-59), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_G1",
             "content": "Command running in background with ID: bx9."}]}}),
    ])
    assert act["work_pending"] is False


def test_an_async_subagent_is_work_until_it_reports_back(tmp_path):
    """JJ: "Also, how about subagents?" An async agent returns at once; it is working until the
    task-notification naming its tool-use id arrives."""
    launch = [
        _use("toolu_A1", "Agent", {"description": "trace camera", "prompt": "..."}, _now(-120)),
        json.dumps({"timestamp": _now(-119), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_A1",
             "content": [{"type": "text", "text": "Async agent launched successfully."}]}]}}),
    ]
    assert _read(tmp_path, launch)["work_pending"] is True
    done = launch + [json.dumps({"timestamp": _now(), "type": "attachment", "attachment": {
        "prompt": "<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>toolu_A1</tool-use-id>\n<status>completed</status>"}})]
    assert _read(tmp_path, done)["work_pending"] is False


def test_a_subagents_files_and_commands_count_for_its_parent_lane(tmp_path):
    """JJ: "what about the activities of those agents? like the files they touch, the commands they
    execute … without asking the sub agents to register." Claude Code writes subagent transcripts to
    `<session>/subagents/agent-*.jsonl`; the lane's reader folds them in."""
    parent = tmp_path / "sess.jsonl"
    parent.write_text(_use("p1", "Read", {"file_path": "/x/parent.py"}, _now(-30)) + "\n", encoding="utf-8")
    subs = tmp_path / "sess" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-abc.jsonl").write_text("\n".join([
        _use("s1", "Edit", {"file_path": "/x/child.py", "old_string": "a", "new_string": "b"}, _now(-10)),
        _use("s2", "Bash", {"command": "git status"}, _now(-5)),
    ]) + "\n", encoding="utf-8")
    for p in (str(parent), str(subs / "agent-abc.jsonl")):
        fleet._LOG_STATE.pop(p, None)
    act = fleet._read_claude_session(str(parent), dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
    assert "/x/parent.py" in act["files"] and "/x/child.py" in act["files"]
    assert act["files"]["/x/child.py"]["edit"] == 1
    assert "git status" in act["commands"]
    assert act["work_pending"] is True               # the subagent's git status has no result yet


def test_a_backgrounded_server_is_not_work(tmp_path):
    act = _read(tmp_path, [
        _use("toolu_S1", "Bash", {"command": "/d/x/bin/command-bridge serve 2>&1", "run_in_background": True}, _now(-60)),
        json.dumps({"timestamp": _now(-59), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_S1",
             "content": "Command running in background with ID: bs1."}]}}),
    ])
    assert act["work_pending"] is False


def test_the_watch_result_does_not_count_as_work(tmp_path):
    act = _read(tmp_path, [_use("w1", "Bash", {"command": "command-bridge watch --since 5"}, _now(-5)),
                           _result("w1", _now())])
    assert act["last_work_epoch"] is None
