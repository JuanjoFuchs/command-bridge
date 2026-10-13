"""Shell commands as fleet activity (JJ, 2026-09-28): "git would be the box, and inside of git we
would have status, commit." `normalize_command` turns a free-text command line into the
(command, subcommand) pairs the fleet charts, dropping noise rather than inventing commands."""
import datetime as dt
import json

from command_bridge import fleet
from command_bridge.fleet import normalize_command as nc


def test_a_tool_with_subcommands_keeps_the_subcommand():
    assert nc("git status") == [("git", "status")]
    assert nc("git -C /d/x commit -F msg.txt") == [("git", "commit")]
    assert nc("COMMAND_BRIDGE_TOKEN=abc /d/x/bin/command-bridge watch --lane magnus --since 7") == \
        [("command-bridge", "watch")]


def test_a_chain_counts_each_invocation_and_drops_trivial_utilities():
    assert nc("cd /x && git add a.py && git commit -m hi") == [("git", "add"), ("git", "commit")]
    assert nc("ls -la /tmp; cat foo | grep bar") == []


def test_a_script_is_charted_as_the_script():
    assert nc("python D:/v/scripts/fleet.py register") == [("fleet.py", "register")]
    assert nc("pwsh -NoProfile -File scripts/herdr-grid.ps1 -Relaunch") == [("herdr-grid.ps1", None)]
    assert nc("python scripts/sb.py commit-unit apply --repo x") == [("sb", "commit-unit")]


def test_inline_code_is_one_python_call_not_its_statements():
    assert nc('python -c "import json; print(1)\nfor x in y: pass"') == [("python", "-c (inline)")]


def test_the_reader_records_commands_per_subcommand(tmp_path):
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    def use(cmd, tid):
        return json.dumps({"timestamp": ts, "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}]}})

    def result(tid, text):
        return json.dumps({"timestamp": ts, "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": text}]}})
    log = tmp_path / "s.jsonl"
    log.write_text("\n".join([use("git status", "t1"), result("t1", "x" * 400),
                              use("git status", "t2"), use("git diff", "t3")]) + "\n",
                   encoding="utf-8")
    fleet._LOG_STATE.pop(str(log), None)
    act = fleet._read_claude_log(str(log), dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
    cmds = act["commands"]
    assert cmds["git status"]["n"] == 2 and cmds["git status"]["sub"] == "status"
    assert cmds["git diff"]["n"] == 1
    assert act["last_activity_epoch"] is not None
    # tokens IN = the invocation text; tokens OUT = the tool result, credited to the command it answers
    assert cmds["git status"]["in_chars"] == 2 * len("git status")
    assert cmds["git status"]["out_chars"] == 400
    assert cmds["git diff"]["out_chars"] == 0
    # the LATEST run of git status had no result yet, so its last_out resets while totals keep the first
    assert cmds["git status"]["last_in_chars"] == len("git status")
    assert cmds["git status"]["last_out_chars"] == 0
