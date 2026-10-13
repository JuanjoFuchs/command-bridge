---
title: Page and audio harnesses
description: Which script verifies which part of the phone page or the audio path, why each exists, and which are safe to run during a live session.
applies_to: command_bridge/web/index.html, scripts/layout.py, scripts/bargein.py, scripts/orbstate.py, scripts/lanestrip.py, scripts/devicepills.py, scripts/channel.py
read_before: changing the page's geometry, the orb, the lane strip, the device pickers, the channel controls, or barge-in
---

# Page and audio harnesses

The unit tests and `scripts/e2e.py` cannot see most of what a person experiences on the page. Each
harness below covers one of those blind spots. **Safe during a live session:** only `lanestrip.py`
and `devicepills.py` — they start no command-bridge server and assert that they didn't.

## Size or position — `python scripts/layout.py`

For anything in `command_bridge/web/index.html` that affects SIZE or POSITION. Asserts the page
never outgrows the viewport and the newest row is on screen, at five viewports. The unit tests and
e2e both pass while the bottom of the transcript is cropped off a phone — geometry is invisible to
them.

## Barge-in and the voiceprint gate — `python scripts/bargein.py`

Drives the real socket with his recorded speech AND the agent's own TTS. Both directions matter: a
tunnel you cannot interrupt is annoying, one that stops on any noise is unusable. **Needs the real
voiceprint gallery** — it lives in the session dir, so isolating COMMAND_BRIDGE_DIR isolates his
identity and every score comes back 0.0.

## The orb — `python scripts/orbstate.py` (`--update` to re-bless the snapshot)

For what the ORB SAYS, or when its elapsed-seconds clock runs — `orbView`, `applyOrb`, the
agent-state handler, or anything on the server that publishes `agent_state`. Sweeps all 384
combinations of running x phase x channel x mute x playing x agent-state through the PURE reducer,
diffs them against `tests/golden/orb-states.json`, then drives the real transitions. **The orb is a
pure function of a model — do not write a label or a class outside `applyOrb`.** It read "Thinking"
with no clock because four handlers painted it independently, and nothing in the suite could see
that.

## The lane strip — `python scripts/lanestrip.py`

For who is in the meeting, which lane is live, the waiting mark, or per-lane state (`lanesView`,
`applyLanes`). Sweeps every shape of the PURE `lanesView` (lane count x live lane x waiting depth x
per-agent state), then drives the real page through its own message handler with a stubbed socket.
**`applyLanes` is the only thing allowed to paint a chip**, exactly as `applyOrb` is for the orb and
for the same reason: the orb once read "Thinking" with no clock because four handlers painted it
independently. **No strip is shown below two agent lanes** — one agent plus `everyone` is the same
destination twice, so a solo session's layout is unchanged. Like `devicepills.py` it starts **no
command-bridge server** and proves it, so it is safe to run during a live session.

## The device pickers — `python scripts/devicepills.py`

For which pills are on screen, the alias-collapsed device count, or the route readout beside them
(`pillsView`, `applyPills`, `distinctDevices`). Sweeps all 64 enumeration shapes through the PURE
`pillsView`, then drives the real page in a browser with a stubbed `enumerateDevices` — which is how
the ANDROID case gets tested with no phone in the room. **No pill may be on screen with fewer than
two choices, and `applyPills` is the only thing allowed to assign one's `hidden`.** Chrome lists one
physical device up to three times (`default`, `communications`, real), so a row count says two where
there is one choice — the alias collapse is the whole rule. It starts **no command-bridge server** (a
static server on an ephemeral port) and asserts that it didn't, so it is the one page harness that
is safe to run during a live session.

## Orb, mute, the channel, the speaking signal — `python scripts/channel.py`

For those controls in `command_bridge/web/index.html`. Drives them through a real socket. Every one
of them needs `running`, which needs a `getUserMedia` grant a headless run cannot produce — so
without this the controls a person actually touches are the least tested part of the page.
