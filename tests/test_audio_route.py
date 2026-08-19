"""The output route is re-applied on reconnect, and neither picker ever shows a request.

WHY THIS FILE EXISTS. Reported live 2026-08-15: *"whenever you restart the server, the client is
still up. For some reason, it stops using my Bluetooth device and starts outputting through the
earpiece. And however, the drop-down didn't change. It still says Bluetooth."*

Two symptoms, and the code owned only one of them. **The audio moving is the platform**: Android
does not let a page choose an output device — that is `setSinkId`'s documented Android limitation
— and an active capture with echo cancellation pulls the audio session into Android's
communication mode, whose fallback output with no headset route up is the earpiece. Nothing in
this repo asked for that and nothing in this repo can override it.

**The picker was ours, and it was the worse half.** There was no output picker at all; playback
has always gone to `ctx.destination`, wherever the OS puts that. The drop-down he was reading is
the MICROPHONE, and it "still said Bluetooth" because the microphone had not moved. It read as
broken rather than irrelevant for two compounding reasons, and both are the shape of a bug:
a pill showing a bare device name makes no claim about direction, and **re-selecting the option
already selected fires no `change` event at all** — so the one gesture a person makes to repair a
stale control is the one gesture that cannot do anything.

The rule that came out of it, which is what these tests hold:

    A PICKER SHOWS WHAT IS IN USE, NEVER WHAT WAS REQUESTED.

A request is right up until the moment it matters, and then stays confidently wrong, because
nothing ever contradicts it. So the input list is painted from the live track's own settings and
the output list from the sink the `AudioContext` reports back — and the route is re-applied on
every reconnect, since a socket coming back is exactly the moment the page can no longer assume
what it set up before the drop is still in force.

Each of those is one line in a 1600-line file with no obvious reason to be there, which is the
precise shape of a fix that gets tidied away by the next person.

WHAT THIS DOES NOT PROVE, stated plainly because the gap is the interesting part. There is no
JavaScript runtime in this suite, so these are structural assertions over the page source: they
prove the wiring exists and that the truthful path is the ONLY path, not that a browser routes
audio anywhere in particular. Browser-level proof needs a live server and lives in
`scripts/e2e.py` and `scripts/uitest.py`. The layer under that — whether Android hands Chrome the
Bluetooth sink back at all — is not reachable from any harness this project owns, and is the
reason the page records `window.__voiceTunnel.sink` instead of guessing: `supported` says whether
the platform ever offered a choice, `inUse` versus `wanted` says whether it was kept, and `why`
names the failure. One live phone session answers what no amount of reasoning here can.
"""
import pathlib
import re

import pytest

PAGE = pathlib.Path(__file__).resolve().parents[1] / "voice_tunnel" / "web" / "index.html"


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def script() -> str:
    """Only the `<script>` block.

    Scoped deliberately. The blanker below understands JavaScript quoting and nothing else, and
    the HTML comments in this page are full of prose apostrophes ("the transcript's OWN header")
    which it would read as an unterminated string and blank half the file behind.
    """
    raw = page()
    start = raw.index("<script>") + len("<script>")
    return raw[start : raw.index("</script>", start)]


def blanked(js: str) -> str:
    """Comments and string bodies replaced with spaces, so brace counting sees only code.

    Comments have to go because they quote the report verbatim and name every function they
    discuss — an assertion that a call site exists would match the paragraph explaining why it
    exists. Strings have to go for the same reason, and they have to go in the SAME pass rather
    than after: the socket URL is a template literal containing `//`, so a comment-first stripper
    eats the rest of that line, braces included, and every function extracted afterwards comes
    back unbalanced. `test_the_extractor_is_reading_balanced_code` is the tripwire for that.

    Newlines survive so reported line numbers still mean something.
    """
    out = list(js)
    i, n = 0, len(js)

    def blank(lo: int, hi: int) -> None:
        for k in range(lo, min(hi, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = js[i]
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            end = js.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif c == "/" and i + 1 < n and js[i + 1] == "/":
            end = js.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif c in "\"'`":
            j = i + 1
            while j < n:
                if js[j] == "\\":
                    j += 2
                    continue
                if js[j] == c:
                    break
                j += 1
            blank(i + 1, j)
            i = min(j, n) + 1
        else:
            i += 1
    return "".join(out)


def span_of(code: str, name: str) -> tuple[int, int]:
    """Offsets of one function's body, opening brace to matching close.

    THE BODY BRACE IS THE FIRST ONE AFTER THE PARAMETER LIST CLOSES, not simply the first one
    after the `(`. A destructured or defaulted parameter — `function stop({ keepSocket = false }
    = {})` — puts braces inside the parens, and taking the first of those extracted the PARAMETER
    LIST as the body. Every assertion about the function then failed with a message about the
    function no longer containing something it contains three lines later, which sends the reader
    to the wrong file entirely.
    """
    m = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\(", code)
    assert m, f"{PAGE.name} no longer declares {name}()"
    depth, i = 1, m.end()
    while i < len(code) and depth:
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
        i += 1
    start = code.index("{", i)
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
    raise AssertionError(f"unbalanced braces extracting {name}()")


def body_of(name: str) -> str:
    code = blanked(script())
    lo, hi = span_of(code, name)
    return code[lo:hi]


def test_the_extractor_is_reading_balanced_code():
    """If this fails every other test here is meaningless rather than merely wrong.

    A blanker that swallows a brace does not report an error — it silently returns a function body
    that stops early, and an assertion that some call is "inside applySink" then passes or fails on
    where the truncation happened to land. Check the premise before trusting anything built on it.
    """
    code = blanked(script())
    assert code.count("{") == code.count("}"), (
        "the page script does not brace-balance after blanking, so the blanker has eaten code — "
        "look for a new quoting shape (a regex literal, a nested template) before trusting the "
        "rest of this file"
    )


def test_the_output_route_has_exactly_one_door():
    """`setSinkId` may only be called from `applySink`.

    The failure being fixed is a route that changed without the interface hearing about it. A
    second call site is a second way for that to happen, and it would be invisible: audio would
    simply be somewhere the picker does not say.
    """
    code = blanked(script())
    lo, hi = span_of(code, "applySink")
    calls = [m.start() for m in re.finditer(r"\.setSinkId\s*\(", code)]
    assert calls, "nothing sets the output sink any more — the page cannot honour a chosen speaker"
    stray = [code[:c].count("\n") + 1 for c in calls if not lo <= c < hi]
    assert not stray, (
        f".setSinkId is called outside applySink() at script line(s) {stray}; every route change "
        "must go through the one function that repaints the picker afterwards"
    )


def test_the_reconnect_path_re_applies_the_route():
    """THE line the 2026-08-15 report is about.

    Everything else on this path is already re-asserted after a drop — mute, capturing, channel,
    speaking — because the page cannot assume a server that went away kept anything. The output
    route was the one fact still assuming it had survived.
    """
    assert "refreshDevices" in body_of("reconnect"), (
        "reconnect() no longer re-reads the devices or re-applies the output route; a server "
        "restart will silently leave audio wherever the OS moved it while the socket was down"
    )


def test_the_output_picker_is_painted_from_the_context_not_from_the_request():
    """The selection must be a readout of the routing, never a memory of an instruction."""
    body = body_of("paintSink")
    assert "currentSink()" in body, (
        "paintSink() no longer reads the sink back off the AudioContext, so the picker is once "
        "again showing what was asked for rather than what is happening"
    )
    assert ".selected" in body, "paintSink() no longer chooses an option; it reports nothing"
    # The invariant is not "the .selected line avoids sinkWanted" — that version passed while the
    # option was matched against `sinkWanted` one line above and only ASSIGNED on the next. The
    # real rule is broader and simpler: paintSink may RECORD the request as diagnostics and may
    # not otherwise consult it. Nothing it decides is allowed to depend on what was asked for.
    stray = [
        ln.strip() for ln in body.splitlines()
        if "sinkWanted" in ln and "diag.sink.wanted" not in ln
    ]
    assert not stray, (
        "paintSink() consults the REQUEST somewhere other than the diagnostics line, so the "
        "picker can once again show a device the audio is not going to: " + " | ".join(stray)
    )


def test_the_visible_control_follows_the_platform_capability():
    """A picker that cannot move audio must not be on screen.

    Offering a dead control is the same defect as the reported one, built on purpose: he taps it,
    nothing changes, and the interface has told him something untrue about itself.

    THE ASSERTION MOVED WITH THE RULE, spec 009. It used to read `$spkpick.hidden = !sinkSupported`
    inside `paintSink`, which was one of three places that assigned a pill's visibility — and three
    writers of one rule is the orb defect's exact shape. The rule now lives in `pillsView`, so the
    invariant is asserted where it lives; what is being held is unchanged, and the single-writer
    property it was half of is held by `tests/test_device_pills.py` instead.
    """
    body = body_of("pillsView")
    assert re.search(r"show\.spk\s*=\s*sinkSupported\s*&&", body), (
        "the output picker's visibility is no longer tied to whether this platform can actually "
        "select an output device"
    )


def test_the_input_picker_shows_the_live_track_and_not_a_snapshot():
    """`diag.settings` is taken once, at start(). A value that cannot change cannot report a move.

    This is what made the microphone pill unfalsifiable: it was correct at the first tap and
    incapable of ever being anything else, which is indistinguishable from a control that works.
    """
    body = body_of("refreshDevices")
    assert "getSettings()" in body, (
        "refreshDevices() no longer reads the device off the live track, so the input list is "
        "back to reporting an intention"
    )
    assert "diag.settings" not in body, (
        "the input list is selecting from the start()-time snapshot again; that value cannot "
        "change, so the picker can never show a microphone that moved"
    )


def test_a_device_change_rebuilds_both_lists():
    """The only event that can tell the page a sink came back."""
    m = re.search(r'addEventListener\(\s*"devicechange"\s*,(.{0,160})', page(), re.S)
    assert m, "nothing listens for devicechange; a headset that reconnects is invisible to the page"
    assert "refreshDevices" in m.group(1), (
        "the devicechange listener no longer refreshes the devices, so a sink that comes back is "
        "never routed to and never shown"
    )


def test_the_chosen_speaker_survives_a_reload():
    """A server restart already costs him the thread of the conversation.

    Making it cost the speaker choice as well stacks a second annoyance on the first — and he has
    to notice the second one before he can fix it, which is the whole complaint again.
    """
    raw = page()
    assert 'localStorage.setItem("voice-tunnel.sinkId"' in raw, "the chosen speaker is not saved"
    assert 'localStorage.getItem("voice-tunnel.sinkId")' in raw, "the saved speaker is never read back"


def test_a_failed_route_change_forgets_the_device_it_could_not_reach():
    """A remembered device that no longer exists must not be retried forever.

    Same reasoning as the microphone fallback a few lines up in the page: a preference that
    outlives its hardware turns every subsequent session into the same failure, and the page
    apologises for something the user cannot see the cause of.
    """
    body = body_of("applySink")
    assert 'removeItem' in body, (
        "applySink() no longer clears the stored sink when it cannot be reached, so an unpaired "
        "device is re-requested on every reconnect and every reload"
    )
    assert "setError" in body, (
        "a speaker change that failed is now silent; reverting to the default without saying so "
        "is exactly how the interface and the audio came to disagree"
    )


def test_the_session_closes_its_audio_context():
    """`start()` builds a new AudioContext every time; `stop()` has to close the old one.

    An abandoned context keeps its output stream — so a leaked one holds a route, and two contexts
    on two sinks is a disagreement the picker cannot describe, let alone win. Browsers also cap how
    many a page may hold, and that ceiling is reached inside `start()` OUTSIDE the try/catch that
    reports microphone failures: the orb sticks on "Starting" and says nothing at all.
    """
    body = body_of("stop")
    assert "ctx.close()" in body, "stop() leaks the AudioContext again"
    assert re.search(r"ctx\s*=\s*null", body), (
        "stop() closes the context but leaves the handle, so the next arriving clip reaches a "
        "closed context instead of waiting for the new one"
    )


def test_the_route_readout_is_never_derived_from_a_request():
    """THE SAME RULE AS THE PICKER, applied to the text that replaces it (spec 009 FR3).

    Withdrawing a picker that can choose nothing removes the only thing on screen that ever named
    the output — so where the page can know the route it says so in words instead. That readout
    inherits the whole reason the picker was rewritten on 2026-08-15: *"the drop-down didn't change.
    It still says Bluetooth."* A remembered preference, a `sinkWanted` and the `start()`-time
    snapshot are each a thing that was true once and is never contradicted, which is exactly how a
    stale label survives being wrong.

    So the negative control is structural and total: the function that decides the route text
    cannot even SEE those three. It is handed the sink read back off the live context and the
    enumerated devices, and nothing else.
    """
    body = body_of("pillsView")
    stray = [ln.strip() for ln in body.splitlines()
             if any(bad in ln for bad in ("sinkWanted", "localStorage", "diag.settings"))]
    assert not stray, (
        "pillsView() consults a REQUEST rather than the live route, so the page can once again "
        "name a device the audio is not going to: " + " | ".join(stray)
    )
    assert "m.liveSink" in body, (
        "pillsView() no longer reads the sink back off the live context, so the route readout is "
        "derived from something other than what is actually happening"
    )


@pytest.mark.parametrize("name", ["applySink", "paintSink", "refreshDevices", "currentSink", "normSink"])
def test_the_route_functions_are_all_still_here(name):
    """A cheap tripwire: these five are referenced by every test above, and a rename that misses
    one of them should fail as a rename rather than as five confusing assertion errors."""
    assert re.search(r"\b" + name + r"\b", blanked(script())), f"{name} is gone from the page"
