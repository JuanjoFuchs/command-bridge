# Command Bridge — one-page UI backlog

Running list of UI issues JJ is dictating over voice (2026-09-02), to tackle **systematically** once
the mock-render harness exists. He will keep adding to this. Check items off as each is built and
**screenshot-verified** against the mock.

## 0. Foundation (do FIRST) — mock backend / full-render harness
- [ ] The UI must be **fully drivable by a fake backend**: fake usage, multiple agents, canvases, a
      populated transcript, orb states — so the whole page can be **fully rendered and screenshotted
      with no mic and no live connection**. This is the prerequisite for iterating on everything below
      (screenshot every state deterministically). *(turns 17–21)*

## Layout
- [ ] **Transcript column: less wide** — give the space to the canvas. *(turn 22)*
- [ ] **Orb area: less height** — same reason, more room for the canvas. *(turn 22)*

## Orbs — rework to match the wireframe (`command-bridge-wireframe.html`)
- [ ] **Smaller** orbs. *(turn 23–25)*
- [ ] **Name BELOW the orb**, not inside it. *(turn 25–26)*
- [ ] **Status + seconds BELOW the name**, not inside the orb. *(turn 25–26)*
- [ ] Use the **icons / emoji look** from the wireframe render (the colored radial-gradient orbs). *(turn 27)*
- [ ] **The three right-side buttons (verbose, mic, speaker): use the EMOJIS + COLORS from the
      wireframe render** — he liked those (the 🎤 / 🔊 / verbose pills). *(turns 28, 74–75)*
- [ ] "**What you're showing me is different**" — the current single voice-orb must become the
      wireframe's per-agent participant orbs. *(turn 24)*

## Interaction model  (REFINED 2026-09-02, turns 72–75 — my first summary was wrong)
- [ ] **Power button = the two-way CHANNEL toggle**, NOT a mic control. Off → the UI plays nothing
      back to him, but the agent's turns stay **queued**; on → the queued turns play, **per the lane
      he's in**. *(turn 72)*
- [ ] **Mic toggle (right) = MUTE the mic + select the mic device.** Speaker toggle = mute the speaker
      + select the speaker device. **Muting only — neither turns the channel off.** *(turn 72)*
- [ ] **Tapping an orb switches lanes**, nothing else. *(turn 30)* — the lane orbs already do this.
- [ ] Verify the current power/mute behaviour matches the above and align it if not.

## Canvas
- [ ] **Border the canvas in the live agent's color** (each agent has a wireframe color), so he knows
      **which agent's canvas** is being shown. *(turn 30)*

## Iteration (PRIORITIZED — speeds up all the above)
- [ ] **Live-reload / push updates**: he should NOT have to refresh the browser to see a UI change —
      push it like the canvas pushes frames. The `/events` SSE already carries a `version` event and
      the page now subscribes to it (the one-page merge), so wire the page to reload/hot-apply on a
      pushed version bump. *(turns 31–33)*

## Bugs
- [x] Interaction model was ALREADY correct in the code (verified 2026-09-02): #power owns the
      two-way channel with queued replies, #mute mutes the mic (he still hears replies), the pickers
      select devices. My earlier summary was wrong; no behaviour change needed — only the styling below.
- [ ] **Flicker on lane switch**: switching agents briefly flashes "the vision tunnel lanes … below
      them, just while it's switching" — a transient during the switch (likely the old `#lanes` chip
      strip or the embedded canvas's own lane chips showing for a beat). Find and kill it. *(turns 78–79)*
- [ ] **Only the orb should be clickable, not the whole row cell.** Hovering the orb row shows the hand
      cursor across the full width — each `.laneorb` is `flex:1 1 0` so its hit area spans its share of
      the row. Shrink the clickable/pointer area to the disc (+ its label), not the padding around it. *(turns 80–81)*
- [ ] **Make "channel off" obvious.** The power button's own style should read more clearly as off, and
      the WHOLE UI should visibly signal when the power button is off (dim/desaturate the room), so the
      off state is unmistakable at a glance. *(turns 82–83)*

## The reference — check every fix against these
- [ ] **Match the renders in the project notes project note** `Command Bridge.md` — the wireframe
      (`command-bridge-wireframe.png`) and the meeting-page screenshots (`command-bridge-meeting-*.png`).
      JJ: "I like what I see there and it's not what I'm seeing live." NB: the wireframe-matching orb
      design already exists in `command_bridge/meeting.py` (`_orb`: colored disc, name below, status
      below, `LANE_HUES`) — bring THAT into the live `index.html`. *(turn 34)*

---
_Source: `sessions/dev.jsonl` turns 17–34, 2026-09-02. More coming._
