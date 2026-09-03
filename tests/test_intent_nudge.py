"""The CLI nudges `--intent` at the one moment staleness happens — a clip held off-lane (spec 012).

`--intent` was built AND documented (in the guide and in `say`'s describe), and agents still did not
reach for it. The lever is not more documentation: it is a prompt at the exact moment the staleness
it prevents occurs — a `say` that comes back HELD off-lane. So the held-off-lane branch nudges it,
but only when the clip was NOT already marked, and phrased for announcements, because a RESULT must
never be marked (it has to stand until he hears it).
"""
import json

from command_bridge import cli


_HELD = {"queued": True, "held_off_lane": True, "lane": "magnus", "seconds": 2.0,
         "superseded": 0, "unread": [], "unread_count": 0, "cursor": 10}


def _say(monkeypatch, capsys, *extra):
    monkeypatch.setattr(cli, "_request", lambda *a, **k: dict(_HELD))
    cli.main(["say", "--session", "dev", "--lane", "magnus", *extra, "on it, running the tests"])
    return json.loads(capsys.readouterr().out)


def test_a_held_off_lane_announcement_is_nudged_toward_intent(monkeypatch, capsys):
    """The whole fix: a held clip teaches `--intent` for next time, so the next announcement is
    marked and its result supersedes it instead of arriving as a stale promise."""
    payload = _say(monkeypatch, capsys)
    blob = json.dumps(payload)
    assert "--intent" in blob, "a held-off-lane clip should teach --intent"
    assert "announced" in blob.lower(), "and scope the nudge to announcements, not results"


def test_a_clip_already_marked_intent_is_not_nudged(monkeypatch, capsys):
    """Nothing to teach — it is already marked. The nudge must not fire, or it reads as a warning
    that the correct thing was wrong."""
    payload = _say(monkeypatch, capsys, "--intent")
    assert "--intent" not in json.dumps(payload), "an already-intent clip must not be nudged"


def test_the_held_guidance_still_leads_with_do_not_repeat(monkeypatch, capsys):
    """The nudge is an ADDITION, not a replacement: the original held-clip guidance — it is not
    lost, do not repeat it, keep waiting — must survive intact."""
    payload = _say(monkeypatch, capsys)
    assert "HELD" in payload["next"]
    assert "Do not repeat" in payload["next"]


def test_the_describe_prompts_when_to_use_intent():
    """An agent reads `describe`, not this file. The `--intent` entry must PROMPT its use on every
    announcement, not merely define the flag — a passive definition is what left it unused."""
    intent_doc = cli.DESCRIBE["commands"]["say"]["args"]["--intent"]
    assert "ABOUT to do" in intent_doc
    assert "never results" in intent_doc.lower() or "never a result" in intent_doc.lower() \
        or "stand until he hears" in intent_doc.lower()
