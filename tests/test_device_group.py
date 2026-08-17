"""One device picker, not two — and the order in which it moves the two halves.

WHY THIS FILE EXISTS. Reported live 2026-08-16, on seeing the output picker for the first time:
*"right now I see there are two dropdowns, one for the microphone and one for the speaker. But I
think the microphone and speaker should be just one dropdown that controls both as a group
together."*

**He is describing the thing he owns; the page was describing the thing the browser exposes.** A
Bluetooth headset is one object — paired once, expected to be both ears and mouth — and
`enumerateDevices()` returns it as two `MediaDeviceInfo` entries that happen to share a `groupId`.
Mirroring that split faithfully is exactly the defect: two dropdowns means two ways to be
half-connected, and half-connected is what the 2026-08-15 report already was.

Three properties are worth holding, and each one is a way this can quietly go wrong:

1. **`groupId` is the mechanism.** It is already on every enumerated device, so this is a rewrite
   of how the lists are rendered rather than new capability. Anything that pairs devices by
   guessing at labels instead is a heuristic that will pair two unrelated devices one day.

2. **The sink moves BEFORE the capture restarts.** The two single-purpose handlers are
   deliberately asymmetric — the microphone tears capture down because swapping a live source
   breeds subtle bugs, the speaker does not because `setSinkId` re-routes a live context and
   tearing capture down to move a speaker *"would drop his turn to fix his ears"*. A combined
   handler inherits both reasons, and getting the order backwards makes every device change cost a
   dropped turn.

3. **Grouping is all-or-nothing, and it falls back.** Some platforms return an empty or unstable
   `groupId`, and Android enumerates no `audiooutput` at all. Where the halves cannot be paired,
   the two original pickers come back. The recorded lesson from the last picker is that offering a
   dead switch is worse than offering nothing, and a combined control that reaches only one half is
   a dead switch with better manners.

WHAT THIS DOES NOT PROVE, same gap as `test_audio_route.py` and for the same reason: there is no
JavaScript runtime in this suite, so these are structural assertions over the page source. They
prove the wiring exists and that the ordering is the one that was argued for — not that a browser
groups any particular headset. `window.__voiceTunnel.group` is what answers that on a real device:
`supported` says whether the halves could be paired at all, `count` how many physical devices the
page believes exist, and `why` names which check failed when they could not.
"""
import re

import pytest

from tests.test_audio_route import blanked, body_of, page, script, span_of


def listener_span(selector: str) -> tuple[int, int]:
    """Offsets of a `change` listener's body, e.g. `$dev`.

    Located in the RAW script and brace-walked in the BLANKED copy. `blanked` replaces characters
    one-for-one and keeps newlines, so the two are interchangeable by offset — which is the only
    reason this can find `"change"` (blanked away as a string body) and still count real braces.
    """
    raw = script()
    code = blanked(raw)
    m = re.search(re.escape(selector) + r"\.addEventListener\(\s*[\"']change[\"']", raw)
    assert m, f"{selector} no longer has a change handler"
    start = code.index("{", m.end())
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
    raise AssertionError(f"unbalanced braces extracting the {selector} change handler")


def listener_body(selector: str) -> str:
    """Code only — comments and string bodies blanked, so an assertion cannot match a comment."""
    lo, hi = listener_span(selector)
    return blanked(script())[lo:hi]


def listener_source(selector: str) -> str:
    """The same span UNBLANKED, for the handful of assertions that are about a string literal."""
    lo, hi = listener_span(selector)
    return script()[lo:hi]


def test_the_page_offers_a_single_grouped_picker():
    raw = page()
    assert 'id="devpick"' in raw and 'id="dev"' in raw, (
        "the grouped picker is gone; he is back to choosing his headset twice"
    )


def test_the_grouped_pill_inherits_the_touch_target_the_others_have():
    """`scripts/layout.py` measures the split layout, which is the WIDER of the two and therefore
    the one that decides whether the control row fits — a single pill cannot wrap a row that two
    pills already fit into. What that harness does NOT cover is this pill's own touch height, so
    assert the cheap structural reason it is the same: it wears the same class, and `.pick select`
    carries the 44px minimum."""
    raw = page()
    assert re.search(r'<label class="pick" id="devpick"', raw), (
        "the grouped pill no longer uses the shared .pick class, so it no longer inherits the "
        "44px touch minimum that `layout.py` enforces on its siblings"
    )
    assert re.search(r"\.pick select\s*\{[^}]*min-height:44px", raw, re.S), (
        "the 44px minimum is gone from .pick select, so nothing gives any pill a touch target"
    )


def test_the_grouping_is_done_by_groupid_and_not_by_guesswork():
    """`groupId` is the browser's own statement that two entries are one physical device.

    Anything softer — matching on label text, on ordering, on a name prefix — is a heuristic that
    will one day pair a webcam microphone with a monitor's speakers and offer that as one device.
    """
    body = body_of("pairDevices")
    assert "groupId" in body, "devices are no longer paired by the platform's own grouping"


def test_a_group_needs_both_halves_before_it_is_offered():
    """A group with only an input cannot be driven by a control that claims to set both, and
    offering it anyway is the dead-switch defect with the labels swapped."""
    body = body_of("pairDevices")
    assert re.search(r"g\.in\s*!==\s*null\s*&&\s*g\.out\s*!==\s*null", body), (
        "pairDevices() no longer requires both halves, so a one-sided device can reach the "
        "combined picker and silently move only one route"
    )


def test_the_combined_handler_moves_the_sink_before_it_restarts_capture():
    """THE ORDERING ARGUMENT, pinned. Sink first is instant and non-destructive; capture first
    would pay the microphone's price — a torn-down session — for a gesture that is usually about
    the speaker. Backwards, every device change costs him a dropped turn."""
    body = listener_body("$dev")

    sink = body.index("applySink")
    assert re.search(r"\bstop\(\)", body), "the combined picker no longer restarts capture at all"
    restart = body.index("stop()")
    assert sink < restart, (
        "the capture restart now runs before the output route is applied, so changing device tears "
        "the session down before his ears are fixed — the exact cost the asymmetry avoided"
    )
    assert "start()" in body[restart:], "capture is stopped and never started again"


def test_the_combined_handler_writes_the_same_preferences_the_single_pickers_do():
    """Reusing both keys is what keeps persistence, `start()`'s restore path and the fallback
    layout working unchanged. A third stored preference is a third thing to go stale."""
    body = listener_source("$dev")
    assert 'voice-tunnel.sinkId' in body and 'voice-tunnel.micId' in body, (
        "the grouped picker stores its choice somewhere else, so a reload or a fallback to the "
        "two-picker layout will not see it"
    )


def test_grouping_requires_the_platform_to_be_able_to_route_output():
    """Android is the case: no `audiooutput` enumerated and no `setSinkId`, so a combined control
    would move the microphone and quietly do nothing about the speaker."""
    body = body_of("refreshDevices")
    assert re.search(r"grouped\s*=\s*Boolean\(\s*sinkSupported", body), (
        "the grouped picker is no longer gated on this platform being able to select an output "
        "device; on Android it would claim to control a route it cannot touch"
    )


def test_an_ungrouped_device_falls_back_instead_of_becoming_unreachable():
    """ALL-OR-NOTHING is the deliberate part. One device outside a usable group and the combined
    pill silently cannot select it; a second dropdown he has seen before is the cheaper failure."""
    body = body_of("refreshDevices")
    assert "covered(ins)" in body and "covered(outs)" in body, (
        "grouping no longer checks that EVERY device is reachable through a group, so a device "
        "that groups badly becomes unselectable rather than falling back"
    )


@pytest.mark.parametrize("pill,expected", [
    ("$micpick", r"\$micpick\.hidden\s*=\s*grouped"),
    ("$spkpick", r"\$spkpick\.hidden\s*=\s*!\s*sinkSupported\s*\|\|\s*grouped"),
])
def test_exactly_one_layout_is_on_screen(pill, expected):
    """Three pills is worse than two. The single-purpose pair is the fallback, so it hides when
    the grouped pill is up and returns the moment grouping stops working."""
    assert re.search(expected, blanked(script())), (
        f"{pill}'s visibility is no longer tied to whether the grouped picker is showing"
    )


def test_the_fallback_pickers_are_still_built_while_hidden():
    """`grouped` is recomputed on every enumeration, so an unpairing headset can put these back on
    screen at any moment — and a list that only populates while visible arrives empty exactly
    then."""
    body = body_of("refreshDevices")
    mic = body.index("$mic.innerHTML")
    hide = body.index("$micpick.hidden")
    assert mic < hide, (
        "the input list is now built only when it is visible, so the fallback layout can appear "
        "with an empty dropdown"
    )


def test_the_grouped_pill_is_painted_from_what_is_in_use():
    """A PICKER SHOWS WHAT IS IN USE, NEVER WHAT WAS REQUESTED — the rule the 2026-08-15 fix
    established, and it bites harder here: one control standing for two routes has two ways to
    become a lie."""
    body = body_of("paintGroup")
    assert "getSettings()" in body, (
        "paintGroup() no longer reads the live capture track, so the pill reports an intention"
    )
    assert "currentSink()" in body, (
        "paintGroup() no longer reads the sink back off the AudioContext"
    )
    stray = [ln.strip() for ln in body.splitlines() if "sinkWanted" in ln]
    assert not stray, (
        "paintGroup() consults the REQUESTED sink, so the pill can name a device the audio is not "
        "going to: " + " | ".join(stray)
    )


def test_the_pill_can_say_the_two_halves_disagree():
    """The state a single control makes possible and a pair of controls could not: his microphone
    on one device and his speaker on another. Left unnamed, the pill picks one of them and reads
    as confident and wrong, which is the whole failure class this section exists to remove."""
    body = body_of("paintGroup")
    assert "split" in body, "paintGroup() no longer distinguishes a split route from an unknown one"
    assert "different devices" in page(), (
        "nothing tells him the mic and the speaker are on different devices; the pill will name "
        "one of them and imply both"
    )


def test_the_placeholder_option_never_moves_audio():
    """The synthetic option names a STATE, not a choice. Selecting it is not a request, and acting
    on it would route audio to an empty device id."""
    body = listener_body("$dev")
    assert re.search(r"if\s*\(\s*!\s*group\s*\)", body), (
        "the combined handler no longer guards against the placeholder option, so choosing "
        "'current devices' would try to select a device that does not exist"
    )


def test_the_synthetic_option_does_not_accumulate():
    """`paintGroup` runs on every clip, every reconnect and every devicechange. An option appended
    each time turns the dropdown into a list of identical placeholders."""
    body = body_of("paintGroup")
    assert "remove()" in body, (
        "paintGroup() no longer clears the placeholder it appends, so they pile up"
    )


@pytest.mark.parametrize("name", ["pairDevices", "paintGroup", "cleanLabel", "groupOfDevice"])
def test_the_grouping_machinery_is_all_still_here(name):
    """A cheap tripwire, same as `test_audio_route.py`'s: a rename that misses one of these should
    fail as a rename rather than as a scatter of confusing assertion errors."""
    assert re.search(r"\b" + name + r"\b", blanked(script())), f"{name} is gone from the page"


@pytest.mark.parametrize("name", ["pairDevices", "paintGroup"])
def test_the_grouping_functions_are_extractable(name):
    """The tests above read these bodies. If one becomes an arrow const the extractor stops finding
    it and every assertion built on it fails for a reason that has nothing to do with the audio."""
    span_of(blanked(script()), name)
