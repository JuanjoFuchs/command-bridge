"""Two threads tailing ONE log at once must not lose it (JJ, 2026-10-05).

    "I have noticed that my other agents I don't see their interactions for Rigel and Sirius.
     In the repo canvas, I only see interactions from Magnus and Polaris. Why is that?"

The server builds the fleet board from executor threads (the periodic broadcast and every page
connect). Both read a log from the same saved offset and both advanced it, so the offset ran past the
end of the file and nothing from that log was read again until it grew past the overshoot.
"""
import datetime as dt
import json
import threading

from command_bridge import fleet


def _edit(path, ts):
    return json.dumps({"timestamp": ts, "message": {"content": [
        {"type": "tool_use", "id": path, "name": "Edit", "input": {"file_path": path}}]}})


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def test_concurrent_readers_never_push_the_offset_past_the_file(tmp_path):
    log = tmp_path / "s.jsonl"
    log.write_text("\n".join(_edit(f"/x/f{i}.md", _now()) for i in range(3000)) + "\n", encoding="utf-8")
    fleet._LOG_STATE.pop(str(log), None)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    go = threading.Barrier(4)

    def read():
        go.wait()
        fleet._read_claude_log(str(log), since)

    threads = [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = fleet._LOG_STATE[str(log)]
    assert st["offset"] <= log.stat().st_size
    assert len(st["files"]) == 3000, "every edit counted exactly once"
    assert st["counts"]["edit"] == 3000

    # and the next appended line is still picked up
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(_edit("/x/new.md", _now()) + "\n")
    act = fleet._read_claude_log(str(log), since)
    assert "/x/new.md" in act["files"]


def test_an_offset_left_past_the_end_reseeds(tmp_path):
    """A state already broken by the old race heals on the next read instead of staying blind."""
    log = tmp_path / "s.jsonl"
    log.write_text(_edit("/x/a.md", _now()) + "\n", encoding="utf-8")
    fleet._LOG_STATE[str(log)] = {"offset": 10_000_000, "counts": fleet._blank_counts(), "files": {},
                                  "last": None, "since": None, "size": 1}
    act = fleet._read_claude_log(str(log), dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
    assert "/x/a.md" in act["files"]
