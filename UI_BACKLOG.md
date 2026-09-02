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
- [ ] Use the **color of the verbose button** he liked. *(turn 28)*
- [ ] "**What you're showing me is different**" — the current single voice-orb must become the
      wireframe's per-agent participant orbs. *(turn 24)*

## Interaction model
- [ ] **Tapping the orb no longer turns the mic on/off** — that responsibility is the **power button's
      only**. *(turn 29)*
- [ ] **Tapping an orb switches lanes**, nothing else. *(turn 30)*

## Canvas
- [ ] **Border the canvas in the live agent's color** (each agent has a wireframe color), so he knows
      **which agent's canvas** is being shown. *(turn 30)*

## Iteration (PRIORITIZED — speeds up all the above)
- [ ] **Live-reload / push updates**: he should NOT have to refresh the browser to see a UI change —
      push it like the canvas pushes frames. The `/events` SSE already carries a `version` event and
      the page now subscribes to it (the one-page merge), so wire the page to reload/hot-apply on a
      pushed version bump. *(turns 31–33)*

## The reference — check every fix against these
- [ ] **Match the renders in the project notes project note** `Command Bridge.md` — the wireframe
      (`command-bridge-wireframe.png`) and the meeting-page screenshots (`command-bridge-meeting-*.png`).
      JJ: "I like what I see there and it's not what I'm seeing live." NB: the wireframe-matching orb
      design already exists in `command_bridge/meeting.py` (`_orb`: colored disc, name below, status
      below, `LANE_HUES`) — bring THAT into the live `index.html`. *(turn 34)*

---
_Source: `sessions/dev.jsonl` turns 17–34, 2026-09-02. More coming._
