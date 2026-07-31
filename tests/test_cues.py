"""Audio cues — Phase 6b. Pure synthesis, no device needed."""
import struct

import pytest

from vm import config, cues


def _samples(pcm):
    return struct.unpack(f"<{len(pcm)//2}h", pcm)


@pytest.mark.parametrize("name", ["heard", "thinking", "speaking"])
def test_every_cue_renders(name):
    pcm, sr = cues.render(name)
    assert sr == cues.CUE_SR
    assert len(pcm) > 0


def test_unknown_cue_raises_rather_than_returning_silence():
    """A silent 'cue' is indistinguishable from a broken one, and would be debugged as audio."""
    with pytest.raises(KeyError):
        cues.render("nope")


@pytest.mark.parametrize("name", ["heard", "thinking", "speaking"])
def test_cues_are_padded_for_bluetooth(name):
    """Same reason as speech: Bluetooth sinks sleep between clips and swallow the first ~100ms.
    An unpadded cue is an inaudible cue on the hardware this is actually used with."""
    pcm, sr = cues.render(name)
    lead = int(sr * config.CHIME_LEADING_SILENCE_S) * 2
    trail = int(sr * config.CHIME_TRAILING_SILENCE_S) * 2
    assert pcm[:lead] == b"\x00" * lead
    assert pcm[-trail:] == b"\x00" * trail


@pytest.mark.parametrize("name", ["heard", "thinking", "speaking"])
def test_cues_stay_well_below_speech_level(name):
    """A cue competing with speech becomes the thing that interrupts — the exact problem cues
    exist to solve."""
    peak = max(abs(s) for s in _samples(cues.render(name)[0])) / 32768
    assert peak <= cues.CUE_AMPLITUDE + 0.02
    assert peak > 0.02, "but it must still be audible"


@pytest.mark.parametrize("name", ["heard", "thinking", "speaking"])
def test_cues_are_short(name):
    pcm, sr = cues.render(name)
    body = len(pcm) / 2 / sr - config.CHIME_LEADING_SILENCE_S - config.CHIME_TRAILING_SILENCE_S
    assert body < 0.25, "punctuation, not a statement"


def test_cues_start_and_end_at_silence_so_they_do_not_click():
    """A hard edge clicks, and a click reads as a fault rather than as information."""
    for name in cues.names():
        s = _samples(cues.render(name)[0])
        assert abs(s[0]) < 200 and abs(s[-1]) < 200


def test_each_cue_is_distinguishable_without_relying_on_timbre():
    """Contour survives cheap earbuds and a noisy room; timbre does not.

    There are only three contours (rising/flat/falling) and more than three cues, so contour
    alone cannot separate them — the honest discriminator is contour PLUS register. Two flat
    cues are fine if one is clearly lower than the other; two flat cues at the same pitch are
    not, because nothing distinguishes them by ear.
    """
    sigs = {}
    for name in cues.names():
        (f0, f1), _dur = cues.CUES[name]
        contour = (f1 > f0) - (f1 < f0)             # +1 rising, 0 flat, -1 falling
        register = round(f0 / 150)                  # ~coarse pitch band
        sigs[name] = (contour, register)
    assert len(set(sigs.values())) == len(sigs), f"cues are not distinguishable: {sigs}"


def test_the_tool_cue_is_the_least_intrusive():
    """It fires repeatedly while the agent works, so a run of them must read as background
    activity rather than as being paged once per tool call."""
    durations = {n: cues.CUES[n][1] for n in cues.names()}
    assert durations["tool"] == min(durations.values())
    pitches = {n: cues.CUES[n][0][0] for n in cues.names()}
    assert pitches["tool"] == min(pitches.values())


def test_every_cue_has_a_documented_meaning():
    """A sound nobody can explain is noise; the meaning table is part of the feature."""
    assert set(cues.CUE_MEANING) == set(cues.CUES)
