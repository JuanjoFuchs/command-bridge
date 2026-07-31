"""vm.cues — short non-speech sounds that make the agent's state audible.

JJ's original 2026-06-18 capture asked for "sounds for whenever it is typing, running stuff", and
the reason resurfaced on 2026-07-31: a pause feels dangerous because silence is ambiguous. You
cannot tell *listening* from *thinking* from *about to talk over me*, and the on-screen state
only helps someone already looking at the page — which defeats a hands-free, eyes-free tool.

A cue reaches you while you are looking at something else. That is the whole point.

**Design constraints, each load-bearing:**

* **Synthesized, not sampled.** No asset files to ship, license, or lose; a cue is a few hundred
  bytes of arithmetic. It also means a cue can never be the thing that fails to load.
* **Distinct by PITCH CONTOUR, not just tone.** Rising = arriving, falling = finishing, flat =
  working. Contour survives cheap earbuds and a noisy room, where timbre does not.
* **Quiet and short.** ~120 ms at low amplitude. A cue competing with speech becomes the thing
  that interrupts, which is the exact problem cues exist to solve.
* **Padded like speech** (:func:`vm.tts.pad`) because Bluetooth sinks sleep between clips and
  swallow the first ~100 ms — an unpadded cue is an inaudible cue on the target hardware.
"""
from __future__ import annotations

import math
import struct
from typing import Dict, List, Tuple

from . import config, tts

CUE_SR = 22050
CUE_AMPLITUDE = 0.18
"""Deliberately well below speech level — a cue is punctuation, not a statement."""

# name -> (frequency ramp in Hz, duration seconds)
CUES: Dict[str, Tuple[Tuple[float, float], float]] = {
    "heard": ((660.0, 990.0), 0.11),
    "thinking": ((520.0, 520.0), 0.09),
    "speaking": ((880.0, 590.0), 0.13),
}

CUE_MEANING = {
    "heard": "rising — your turn arrived and was read",
    "thinking": "flat — working on it",
    "speaking": "falling — about to talk, so stop if you were not finished",
}


def names() -> List[str]:
    return sorted(CUES)


def render(name: str) -> Tuple[bytes, int]:
    """Return `(padded mono 16-bit PCM, sample_rate)` for a cue. Raises KeyError if unknown."""
    (f0, f1), seconds = CUES[name]
    n = int(CUE_SR * seconds)
    out = bytearray()
    phase = 0.0
    for i in range(n):
        t = i / n
        freq = f0 + (f1 - f0) * t
        phase += 2.0 * math.pi * freq / CUE_SR
        # Raised-cosine envelope: a hard edge clicks, and a click reads as a fault rather than
        # as information.
        env = 0.5 - 0.5 * math.cos(2.0 * math.pi * min(t, 1.0))
        out += struct.pack("<h", int(max(-1.0, min(1.0, math.sin(phase) * env * CUE_AMPLITUDE)) * 32767))
    return tts.pad(bytes(out), CUE_SR), CUE_SR
