---
id: "006"
title: The orb being off is not "quiet"
status: complete
blocked_by: ["005"]
blocks: []
---

# The Orb Being Off Is Not "Quiet"

## Overview

Spec 005 removed the rungs from the *speaking* path and left them on the *idle* path, where they
belong: with a page open and him merely silent, each empty wait is weak evidence that nothing is
imminent, so the ceiling doubles. That is the ladder's actual job.

It is the wrong instrument the moment **no turn can arrive at all**. That case already had its own
flat eight-hour ceiling (`WATCH_DISCONNECTED_MAX_S`) — but only for `clients == 0`. A page that is
open with the conversation switched **off at the orb** was routed to the ladder, and since
2026-08-16 switching the orb off *releases the microphone* rather than idling it. Nothing can be
spoken into a released microphone.

The cost is measured, not theoretical. On 2026-08-17, with the orb off and a page still connected,
the watchdog paid **fifteen consecutive nine-minute wakes**, `count: 0` every one. He reported it in
a single line:

> *"we shouldnt be burning turns when the orb is off, watch should not timeout"*

The fix is not a new rule. It is applying the existing one where it already belonged.

### The reasoning that was wrong

The old split reasoned from **how fast he could come back**: a closed channel still has a page
behind it that can reopen in a second, a dropped page does not — so the first got the ladder. The
first half is true and the second half does not follow. Reopening ends the wait within a second
**either way**, because `clients` and `channel_open` are both in `CONTROL_FACTS` and a control
change returns the wait.

What decides the ceiling is not how quickly he could come back. It is **whether waiting can yield
anything before he does** — and with the microphone released, it cannot.

> **Completion rule:** verified through the repo's `pytest` suite. The behaviour is a ceiling
> selection and two guidance strings; there is no browser surface to drive.

## Goals

- A quiet period with the orb off costs one wait, not one per nine minutes.
- The listening path is untouched: connected-and-quiet still ladders from 30 s.
- The guidance an agent actually follows (`next`, `hint`, `describe`) states the new hold, so the
  behaviour and the instruction change together.

## Non-goals

- **Muting.** A muted microphone stays on the ladder. Mute is a mic-level control he flips *inside*
  a live conversation with the channel open and `capturing` true; he may unmute in seconds, and an
  eight-hour ceiling there would be surprising in a way the orb-off one is not.
- **A `watch_open` lease.** A watch killed by its harness still leaves `watch_open: true` with no
  expiry, which STEP 0 of the watchdog reads as "someone is already listening" forever. That hazard
  is older than this spec and is not made *worse* by it — but a longer ceiling does make a harness
  kill more likely, so it is recorded here and left for its own unit of work. `--force` is the
  existing escape.

## Requirements

- **FR1** — `watch` selects the flat disconnected ceiling whenever no turn can arrive: `clients ==
  0`, **or** `channel_open` present and false.
- **FR2** — one predicate decides it, read by both the ceiling for *this* wait and the `next_wait`
  reported for the following one. Two copies is how a reported schedule stops matching the running
  one.
- **FR3** — the predicate reads the **raw** status, not `_controls`. `_controls` coerces absent keys
  to `False`, which is correct for spotting a change and exactly wrong here: a server predating
  `channel_open` would read as closed and earn eight hours on a live conversation.
- **FR4** — a dead or absent server does **not** earn the long ceiling. Not because a turn could
  arrive, but because with nothing answering there is no control-change path left to end the wait
  early, and a ceiling nothing can interrupt is what would make eight hours unsafe.
- **FR5** — `next` and `hint` for a closed channel state the hold and its length, and say to detach
  rather than shorten with `--timeout`.
- **FR6** — `describe` says the same thing in all three places that publish the ceiling
  (`watch.notes`, `watch.args.--timeout`, `watchdog.backoff`).

## Acceptance criteria

| # | Criterion | Verified by |
|---|---|---|
| AC1 | Orb off with a page connected → `waited` and `next_wait` are the disconnected ceiling, not 30 s | `test_cmd_watch_picks_the_long_ceiling_when_the_orb_is_off` |
| AC2 | Nobody connected still takes the long ceiling | `test_nobody_connected_still_means_no_turn_can_arrive` |
| AC3 | Connected, channel open, quiet → still 30 s | `test_a_quiet_but_listening_tunnel_still_ladders` |
| AC4 | `channel_open` absent → treated as open, not closed | `test_an_absent_channel_field_is_not_a_closed_one` |
| AC5 | Dead/errored/stopped server → not the long ceiling | `test_a_dead_server_does_not_earn_the_long_ceiling` |
| AC6 | Tapping the orb back on ends the wait within seconds, `changed: {channel_open: true}` | `test_the_orb_coming_back_on_still_ends_the_wait_immediately` |
| AC7 | `next` and `hint` publish the ceiling actually in force and name the orb | `test_the_closed_channel_guidance_says_it_holds_rather_than_re_arms` |
| AC8 | The whole suite still passes | `python -m pytest tests/ -q` — 569 passed, 2 skipped |

## Notes

`WATCH_BACKOFF_UNREACHABLE_MAX_S` is now nearly unreachable itself: both states it was written for
take the flat ceiling instead. What is left for it is the narrow middle — a server that answered the
baseline read and then stopped answering, where `reachable` is false but no no-turn-possible test
fired. It is kept as a separate constant so raising it stays a one-line change.
