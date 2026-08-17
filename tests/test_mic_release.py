"""Switching the orb off releases the microphone — without closing the tab.

WHY THIS FILE EXISTS. Reported live 2026-08-16: *"whenever I tap the orb and I turn it off, I would
like you to stop recording on the microphone, because that does not allow me to watch other media
on the phone, or use the Bluetooth or whatever, right — because you're hugging the microphone but
the orb is off, so there's no point in that."*

**The orb stopped LISTENING without stopping CAPTURING.** Those are different states and only one
of them was visible to him: the `MediaStream` stayed open, so the OS microphone indicator stayed
lit and the phone stayed in capture. Confirmed against the live server the same day — after he
switched the orb off, `status` reported `channel_open: false, muted: true, capturing: true`.

**The second half of the requirement is the part that constrains the fix.** Closing the tab already
releases the microphone; he found that himself and rejected it. *"What I did is I closed the tab,
but I would like to keep the tab open so that I can just come back and hit the orb again without
having to reopen the link, scroll up and go find the link."* The tunnel is fronted by a URL that
changes on every restart, so "just reopen it" costs a scroll through chat history for a link that
may no longer work. So the tracks end and everything else — socket, page, transcript, URL — stays.

WHAT THESE TESTS ARE AND ARE NOT. There is no JavaScript runtime here, so these are structural
assertions over the page source: they prove the wiring is the wiring that was argued for. The
BEHAVIOUR is proven in `scripts/orbstate.py`, which is the only harness that drives a real
`getUserMedia` — every other one stubs it, and a stubbed stream has no tracks to end. That harness
taps the orb off, reads the track states off the stream itself, checks the server was told, and
taps back on.

**The one claim nothing here can settle:** whether a real phone re-prompts for permission on the
re-acquire. Chrome's `--use-fake-ui-for-media-stream` auto-grants silently, so a green re-acquire
says the path works and says nothing about the prompt. The permission belongs to the origin rather
than to the stream, which is why this is expected to be silent — but it is reasoning, not a
measurement, and it wants one tap on his phone.
"""
import re

import pytest

from tests.test_audio_route import blanked, body_of, page, script, span_of
from tests.test_device_group import listener_body


def source_of(name: str) -> str:
    """One function's body UNBLANKED — for the few assertions that are about a string literal.

    Spans are computed on the blanked copy (so a brace inside a comment cannot end a function) and
    sliced out of the raw one. `blanked` substitutes character-for-character and keeps newlines, so
    the two are interchangeable by offset.
    """
    lo, hi = span_of(blanked(script()), name)
    return script()[lo:hi]


def test_the_orb_off_path_goes_through_a_release_rather_than_a_flag_flip():
    """`setChannel` used to flip `channelOpen` and leave capture running. It is gone rather than
    kept beside the new path, because two functions that both mean "close the conversation" is
    exactly how one of them comes to mean something else."""
    code = blanked(script())
    assert "closeChannel" in code, "nothing releases the microphone when the orb goes off"
    assert "setChannel" not in code, (
        "the old flip-a-flag path is back alongside the release; whichever one the orb calls, the "
        "other is a second definition of 'off' waiting to be wired up by mistake"
    )


def test_the_orb_calls_it():
    body = body_of("orbTapped")
    assert "closeChannel" in body, (
        "tapping a running orb no longer releases capture, so the microphone stays held while the "
        "interface says it is off"
    )


def test_closing_the_channel_stops_the_tracks():
    """`.stop()` on every track is what actually clears the OS microphone indicator. Disconnecting
    the audio graph is not enough — the capture session belongs to the track, not to the node."""
    body = body_of("stop")
    assert re.search(r"getTracks\(\)\.forEach\(\s*t\s*=>\s*t\.stop\(\)\s*\)", body), (
        "stop() no longer ends the capture tracks, so the microphone is never released"
    )


def test_the_stream_handle_is_dropped_and_not_merely_stopped():
    """An ended track still answers `getSettings()` with the device it used to be on, and
    `refreshDevices` paints the input picker from exactly that call. Left in place, a released
    microphone keeps naming a device it is not holding — the "shows what was requested" failure
    this page spent 2026-08-15 removing, now in a state he SITS in rather than passes through."""
    body = body_of("stop")
    assert re.search(r"micStream\s*=\s*null", body), (
        "stop() stops the tracks but keeps the stream, so the picker can report a device that was "
        "released minutes ago"
    )


def test_the_socket_survives_the_release():
    """THE WHOLE POINT, and the half that separates this from closing the tab. He keeps the page,
    the session and the URL; only the microphone goes."""
    body = body_of("stop")
    assert "keepSocket" in body, "stop() can no longer be asked to keep the socket"
    assert re.search(r"if\s*\(\s*!\s*keepSocket\s*\)\s*\{[^}]*ws\.close\(\)", body), (
        "the socket is closed unconditionally again, so switching the orb off costs him the "
        "session and the URL — which is the workaround he explicitly asked to stop needing"
    )
    assert "keepSocket" in body_of("closeChannel"), (
        "the orb-off path no longer asks for the socket to be kept"
    )


def test_the_release_drops_the_queue_before_it_tears_down():
    """`stop()` deliberately KEEPS the clip queue, because its other caller is a device change
    where those replies are still wanted. This caller is the opposite intention. Reported
    2026-08-07: "I hit the orb to turn off because I was not hearing you... However, you spoke
    anyway." A switch whose effect waits for the current sentence is not an off switch."""
    body = body_of("closeChannel")
    silence = body.index("silencePlayback")
    assert "stop(" in body, "closeChannel no longer tears capture down at all"
    assert silence < body.index("stop("), (
        "the teardown now runs before the queue is dropped, so a reply already in flight can still "
        "reach him after he switched the conversation off"
    )


def test_the_agent_state_is_not_forgotten_while_he_is_away():
    """The agent's state is the SERVER's fact. With the socket still open the agent is still
    working and still pushing `agent_state`, so clearing it on release would tell him — the instant
    he taps back on — that the work he stepped away from had finished."""
    body = body_of("stop")
    assert re.search(r"if\s*\(\s*!\s*keepSocket\s*\)\s*agentState\s*=", body), (
        "the agent's state is cleared even when the session is being kept, so reopening reports "
        "idle over an agent that is still thinking"
    )


def test_off_and_never_started_stay_distinguishable():
    """Releasing capture clears `running`, which is what the orb used to read as "Tap to start".
    Dropping back to that label after he presses the switch reads as the session having died,
    rather than as the thing he just did."""
    body = body_of("orbView")
    assert "everStarted" in body, (
        "orbView can no longer tell a page he switched off from one that was never started, so "
        "the orb reports a dead session every time he uses the switch"
    )
    assert re.search(r"everStarted\s*\?\s*\"Off\"\s*:\s*\"Tap to start\"", source_of("orbView")), (
        "the two labels are no longer chosen by that fact"
    )


def test_the_never_started_default_is_preserved_for_an_older_caller():
    """`orbView` is a pure reducer that harnesses sweep over the whole state matrix. A model
    without the new field must keep behaving exactly as it did, or every existing sweep starts
    asserting against a state that did not exist when it was written."""
    body = body_of("orbView")
    assert "m.everStarted" in body, "the field is read off the model rather than a closure"
    assert not re.search(r"everStarted\s*===\s*(false|undefined)", body), (
        "an explicit comparison makes absent and false different; absent must read as "
        "never-started, which is the previous behaviour exactly"
    )


def test_the_flag_is_never_cleared():
    """It answers "has this page ever held the microphone", which cannot become false again."""
    code = blanked(script())
    assignments = re.findall(r"everStarted\s*=\s*(\w+)", code)
    assert "true" in assignments, "nothing ever sets it, so the orb can never say Off"
    assert "false" in assignments and assignments.count("false") == 1, (
        "expected exactly one `= false`, the declaration; a second one is a reset, and a reset "
        "puts the orb back to 'Tap to start' on a page that plainly has started"
    )


def test_whether_the_microphone_is_held_is_read_off_the_tracks():
    """The defect was that "the orb is off" and "the microphone is released" had drifted apart
    while every status the page published said they had not. A boolean set by the same function
    that does the releasing would be that same lie one layer down — so the diagnostic reads the
    tracks. Same discipline as `diag.sink` reading the AudioContext."""
    raw = page()
    m = re.search(r"micState:\s*\(\)\s*=>(.{0,160})", raw, re.S)
    assert m, "window.__voiceTunnel.micState is gone; nothing can check the release from outside"
    assert "getAudioTracks" in m.group(1) and "readyState" in m.group(1), (
        "micState no longer reads the tracks themselves, so it reports a claim rather than a fact"
    )


def test_a_broadcast_channel_close_does_not_release_capture():
    """Releasing is the answer to HIS tap on THIS device. A `channel` broadcast is another page or
    a reconnect echo, and letting a message tear down a live capture means the microphone can drop
    mid-sentence for a reason nobody at this device can see."""
    raw = script()
    m = re.search(r'msg\.type === "channel"\)\s*\{', raw)
    assert m, "the channel broadcast branch has moved; re-check what it does to capture"
    # Read the branch as CODE. Every one of these names appears in the comment explaining why the
    # branch does not do it, so a raw search finds the explanation and calls it the offence.
    code = blanked(raw)
    end = code.index("} else if", m.end())
    branch = code[m.end():end]
    assert "silencePlayback" in branch, "a remote close must still silence what is playing here"
    assert "closeChannel" not in branch and "stop(" not in branch, (
        "a broadcast now tears down local capture; a message from elsewhere can cut his microphone "
        "mid-sentence"
    )


@pytest.mark.parametrize("name", ["closeChannel", "orbTapped", "stop", "start", "orbView"])
def test_the_lifecycle_functions_are_all_still_here(name):
    span_of(blanked(script()), name)


def test_the_device_picker_still_tears_the_socket_down(monkeypatch=None):
    """The grouped picker restarts capture through the DEFAULT `stop()`, not the keep-socket one.
    A device change is a genuine session rebuild; borrowing the release path here would leave a
    socket bound to a context that no longer exists."""
    body = listener_body("$dev")
    assert "keepSocket" not in body, (
        "the device picker is now using the orb's release path, which keeps a socket across a "
        "full AudioContext rebuild"
    )
