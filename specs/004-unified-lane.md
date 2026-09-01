---
id: "004"
title: One lane — the voice lane and the canvas lane become the same thing
status: complete
blocked_by: ["003"]
blocks: ["008"]
---

# One lane — the voice lane and the canvas lane become the same thing

## Overview

Voice and canvas each track a "live lane" of their own. The voice server owns the real one —
`state.lanes.current`, moved by a wake word or a tap. The canvas has a second copy, kept in step by
a `Follower` that POLLS the voice server's `/status.lane` every fraction of a second and mirrors it.
That poll was the only bridge available while they were two processes. Now that spec 003 put them in
one server, the poll is a cross-process round trip to reach a value sitting in the same memory. This
spec collapses the two into ONE lane: a voice lane switch drives the canvas lane directly, in-process,
and the poll retires.

> **Completion rule:** This spec is not complete until every acceptance criterion below is verified
> by the method named on it (a unit/integration test, or the spec 002 shot). Build-only verification
> is insufficient. Iterate until verification passes.

## What he said, verbatim

> *"Whenever I drive the voice tunnel, it drives the tunnel vision."*
>
> *"the names for the lanes and the color for the n lanes should be deterministic … the first one is
> always by default Magnus … the next one we open is always Atlas … and so on."*
>
> (voice-dictated, 2026-08-26 / 08-31; records in the gitignored `sessions/dev.jsonl`. The roadmap
> row: *"collapse voice-lane and canvas-lane into one lane concept; retire the 100 ms poll-follow now
> both live in one server."*)

## Findings — the two lane truths today

Grounded in the code:

- **The voice lane is authoritative.** `command_bridge/lanes.py`'s `Lanes` object holds `current`
  (the live lane, the one he is addressing), moved by the wake-name switch and the tap. Turns are
  stamped with it; `/status` publishes it as `lane`.
- **The canvas keeps a shadow copy.** `command_bridge/canvas/server.py` has its own `_live`, and
  `canvas/follow.py`'s `Follower` reads the voice server's `/status.lane` on a short interval and
  calls `set_live` to mirror it. It polls, not subscribes, because the voice server's only wait
  command consumes turns — a reader must not.
- **In one process the poll is a detour.** `init_canvas` starts that `Follower` against the SAME
  server it now lives in. The value it fetches over HTTP is `state.lanes.current`, in the same memory.
  A direct call is both faster and impossible to desync.

## Goals

- **One live lane, one source of truth.** The voice lane is it; the canvas reflects it with no second
  copy that can drift.
- **A voice lane switch moves the canvas lane in the same breath** — in-process, not via a poll.
- **The poll-follow is retired** for the in-process case (it stays only as the cross-process fallback
  it was built for, and is off when the canvas is embedded).

## Requirements

### Functional Requirements

- **FR1** — When the voice server's live lane changes (`state.lanes.current` moves, by wake or tap),
  the canvas's live lane is set to the SAME lane, synchronously, in-process — the canvas `look`s at
  that lane's frames.
- **FR2** — The cross-process poll (`Follower` reading `/status.lane`) is **not running** when the
  canvas is embedded in the voice server; the in-process call replaces it. (The `Follower` code stays
  for the standalone-canvas case, but the merged server does not start it.)
- **FR3** — A lane NAME means the same thing on both halves: a frame an agent draws to lane `X` lives
  on the same lane the voice side calls `X`, and switching to `X` by voice shows `X`'s canvas.
- **FR4** — The one-way direction is preserved: the **voice lane drives the canvas**, never the
  reverse (a canvas `switch` still exists for a canvas-only caller, but the human's voice is the
  authority, as it is today).

### Non-Functional Requirements

- **NFR1** — **Lower latency, never higher.** The in-process call lands before the next turn; the
  ~100 ms poll lag that put highlight marks a beat behind is gone.
- **NFR2** — **The voice contract is unchanged.** The turn log, the cursor, `/status`, and the lane
  switch behave exactly as before; the voice suite stays green.

### Technical Constraints

- **TC1** — The canvas stays a **dumb surface** — it is told which lane is live; it decides nothing.
- **TC2** — One writer for the live lane. The voice `Lanes.switch` (or the single place the live lane
  moves) is where the canvas is driven, so there is no second path that could set a different lane.

## Implementation Tasks

- [x] Drive `canvas.set_live` from the one place the voice live lane changes (`_set_lane`, reached by
      the wake gate, the tap and an explicit switch), synchronously. — `command_bridge/server.py:944`.
- [x] Stop `init_canvas` from starting the polling `Follower` in the embedded server (keep it for the
      standalone canvas), and confirm nothing else starts a second live-lane writer. — `run()` calls
      `init_canvas(session, follow=False)`; `_set_lane` is the sole live-lane writer.
- [x] Reconcile the default lane so a fresh canvas and a fresh voice session agree on the same first
      lane rather than `main` vs the first wake name. — `set_live` is called on every switch including
      the first wake, so the canvas adopts the voice default rather than holding its own `main`.

## Acceptance Criteria

- [x] **AC1** `integration` — **FR1.** Switching the voice live lane (through the server's switch
      path) sets the canvas live lane to the same value, in the same call — asserted without any
      polling interval elapsing. — `tests/test_unified_lane.py::test_a_voice_lane_switch_drives_the_canvas_in_process`
      (and `::test_a_wake_switch_drives_the_canvas_too`, the wake path).
- [x] **AC2** `integration` — **FR2.** The merged server starts **no** `/status`-polling follower: a
      voice lane switch is reflected on the canvas even with the poll disabled/absent, and no follower
      thread is polling. — `tests/test_unified_lane.py::test_the_merged_server_starts_no_polling_follower`
      (asserts `_follower._thread is None`), plus `::test_the_follower_code_survives_for_the_standalone_canvas`.
- [x] **AC3** `shot` — **FR3.** Draw a frame on lane `X`, switch the voice lane to `X`, and a
      `command-bridge shot` of the canvas shows `X`'s frame (not another lane's). — verified live
      2026-09-01: a frame was drawn to lane `atlas` while the live lane was elsewhere, a voice switch
      to `atlas` was issued through `/lane`, and the shot of `/canvas` showed the `atlas` chip with
      `atlas`'s frame ("ATLAS frame — the canvas followed the voice lane"). Method tag corrected from
      `kittest-snapshot` to `shot` to name what actually validates it (a real browser render, spec 002).
- [x] **AC4** `command:python -m pytest tests/` — **NFR2.** The full suite passes; the voice lane and
      turn-log tests are unchanged. — no regression against the recorded baseline (the 4 pre-existing
      `test_word_timings` failures from the missing `kokoro_onnx` package are unchanged; no NEW failures).

## Testing Approach

### Validation Steps

1. Drive the voice switch in a test; assert the canvas `_live` moved with no poll.
2. Live: `serve`, draw on lane `X`, switch to `X` by the lane API, shot the canvas.
3. Run the full suite.

### Test Cases

| Situation | Expected |
|---|---|
| voice switches to lane `X` | canvas live lane is `X`, in-process |
| canvas embedded in the voice server | no `/status` poll running |
| frame on `X`, then switch to `X` | the shot shows `X`'s frame |
| voice turn round-trip | turn log + cursor unchanged |

## Out of Scope

- **The orb/chip strip and per-lane state UI** — one transcript, orbs as participants is spec 008.
- **Deterministic lane name/colour/voice slots** — a separate roadmap item; this spec unifies the
  live-lane *pointer*, not the naming scheme.
- **The combined page layout** — spec 006.

## References

- `command_bridge/lanes.py` (the authoritative voice lane), `command_bridge/canvas/follow.py` (the
  poll being retired), `command_bridge/canvas/server.py` (`set_live` / `_live`).
- `specs/003-one-server-one-page.md` — the merge this builds on.
