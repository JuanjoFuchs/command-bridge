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


def test_each_cue_is_distinguishable_by_contour_not_just_timbre():
    """Rising/flat/falling survives cheap earbuds and a noisy room; timbre does not."""
    contours = {}
    for name in cues.names():
        (f0, f1), _dur = cues.CUES[name]
        contours[name] = (f1 > f0) - (f1 < f0)      # +1 rising, 0 flat, -1 falling
    assert len(set(contours.values())) == len(contours), f"contours collide: {contours}"


def test_every_cue_has_a_documented_meaning():
    """A sound nobody can explain is noise; the meaning table is part of the feature."""
    assert set(cues.CUE_MEANING) == set(cues.CUES)
