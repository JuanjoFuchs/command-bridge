"""`store.read_turns` reads only what was appended since its last call (JJ, 2026-10-07).

The whole-file re-parse on every `/status` held the server's event loop at 92% busy with seven
agents polling a 10,000-turn log, so every reply reached him ~2.5 s late. These pin the cache's
contract: same answers as a full read, through appends, a half-written line, and a rewrite.
"""
import json

from command_bridge import store


def _row(i, **extra):
    row = {"id": i, "session": "dev", "text": f"turn {i}", "addressed": True, "final": True}
    row.update(extra)
    return json.dumps(row) + "\n"


def _ids(base):
    return [t["id"] for t in store.read_turns("dev", base)]


def test_appended_turns_are_picked_up(tmp_path):
    path = tmp_path / "dev.jsonl"
    path.write_text(_row(0) + _row(1), encoding="utf-8")
    assert _ids(str(tmp_path)) == [0, 1]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_row(2))
    assert _ids(str(tmp_path)) == [0, 1, 2]


def test_a_half_written_line_is_skipped_then_read_once_complete(tmp_path):
    path = tmp_path / "dev.jsonl"
    path.write_text(_row(0), encoding="utf-8")
    line = _row(1)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line[:10])
    assert _ids(str(tmp_path)) == [0]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line[10:])
    assert _ids(str(tmp_path)) == [0, 1]


def test_a_complete_final_line_without_a_newline_is_returned(tmp_path):
    path = tmp_path / "dev.jsonl"
    path.write_text(_row(0) + _row(1).rstrip("\n"), encoding="utf-8")
    assert _ids(str(tmp_path)) == [0, 1]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n" + _row(2))
    assert _ids(str(tmp_path)) == [0, 1, 2]


def test_a_rewritten_log_is_reread_from_scratch(tmp_path):
    path = tmp_path / "dev.jsonl"
    path.write_text(_row(0) + _row(1), encoding="utf-8")
    assert _ids(str(tmp_path)) == [0, 1]
    path.write_text(_row(7, text="other") + _row(8) + _row(9), encoding="utf-8")
    assert _ids(str(tmp_path)) == [7, 8, 9]
    path.write_text(_row(5), encoding="utf-8")
    assert _ids(str(tmp_path)) == [5]


def test_malformed_lines_are_skipped(tmp_path):
    path = tmp_path / "dev.jsonl"
    path.write_text(_row(0) + "{not json\n\n" + _row(1), encoding="utf-8")
    assert _ids(str(tmp_path)) == [0, 1]


def test_each_call_returns_a_new_list(tmp_path):
    (tmp_path / "dev.jsonl").write_text(_row(0), encoding="utf-8")
    first = store.read_turns("dev", str(tmp_path))
    first.append({"id": 99})
    assert _ids(str(tmp_path)) == [0]


def test_a_missing_log_is_empty(tmp_path):
    assert store.read_turns("dev", str(tmp_path)) == []
