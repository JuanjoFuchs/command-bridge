"""The transcript lives on the server, so every device shows the same conversation (JJ, 2026-09-25).

The page used to rebuild its transcript from its own localStorage, i.e. only what THAT browser had
heard live — on the phone after the laptop: "the turns that I did while in the computer are not
showing up here, why". His turns were already in the turn log; the agents' replies existed nowhere
on disk. `_send_clip` now appends each spoken reply to `<session>.said.jsonl`, and
`transcript_history` merges the two in wall-clock order for `GET /history`.
"""
import asyncio
import json

from command_bridge import server, store


class _State:
    session = "hist"
    clients: set = set()

    def __init__(self):
        self._clip_lock = asyncio.Lock()


def test_a_spoken_reply_is_logged_and_a_cue_is_not(tmp_sessions):
    st = _State()

    async def go():
        await server._send_clip(st, {"type": "audio_header", "id": "clip-1", "text": "hello",
                                     "lane": "magnus"}, b"\x00\x00")
        await server._send_clip(st, {"type": "audio_header", "id": "cue-heard", "text": "",
                                     "cue": "heard"}, b"\x00\x00")

    asyncio.run(go())
    lines = open(server._said_path("hist"), encoding="utf-8").read().splitlines()
    assert [json.loads(x)["id"] for x in lines] == ["clip-1"]


def test_history_merges_his_turns_and_the_replies_in_order(tmp_sessions):
    store.append_turn(session="hist", text="first from him", t_start=0, t_end=1, addressed=True,
                      lane="magnus", stamp_lane=True)
    server._log_said("hist", {"id": "clip-9", "lane": "magnus", "text": "the reply"})
    rows = server.transcript_history("hist")
    assert [r["from"] for r in rows] == ["you", "agent"]
    assert rows[0]["text"] == "first from him" and rows[0]["lane"] == "magnus"
    assert rows[1]["key"] == "c:clip-9" and rows[1]["text"] == "the reply"


def test_history_is_empty_not_an_error_for_a_new_session(tmp_sessions):
    assert server.transcript_history("nothing-here") == []
