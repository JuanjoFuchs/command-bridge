# Command Bridge — one-page UI backlog

Running list of UI issues JJ is dictating over voice (2026-09-02), tackled **systematically** on top
of the mock-render harness. He keeps adding to it. Items are checked off as each is built and
**screenshot-verified** against the mock.

**Status as of turn 112 (2026-09-02 ~13:00):** everything dictated so far is built and pushed. Two
threads stay open on *his* judgment, not on code: the **lane-switch flicker** fix is in but wants his
live eyes to confirm it's gone, and the **match-the-vault-renders** check is subjective (his call on
whether live now matches what he liked). Latest commit `fe1a7dc`.

## 0. Foundation (do FIRST) — mock backend / full-render harness
- [x] The UI is **fully drivable by a fake backend** (`?mock=meeting` / `?mock=solo`): fake agents,
      canvas, populated transcript, orb states — the whole page renders and screenshots with **no mic
      and no live connection**. Prerequisite for iterating on everything below. *(turns 17–21)*

## Layout
- [x] **Transcript column: less wide** — space given to the canvas. *(turn 22)*
- [x] **Orb area: less height** — more room for the canvas. *(turn 22)*

## Orbs — reworked to match the wireframe (`command-bridge-wireframe.html`)
- [x] **Smaller** orbs. *(turns 23–25)*
- [x] **Name BELOW the orb**, not inside it. *(turns 25–26)*
- [x] **Status + seconds BELOW the name**, not inside the orb (with a reserved timer line so the
      seconds counter can't shift the layout). *(turns 25–26)*
- [x] The **emoji / radial-gradient look** from the wireframe render (filled lane-colour discs). *(turn 27)*
- [x] **The three right-side buttons (verbose, mic, speaker): the EMOJIS + COLORS from the wireframe** —
      `#mute` → 🎤, `#verbose` → 💬, `#spkpick` → 🔊, `#devpick` (grouped) → 🎧. *(turns 28, 74–75, 88)*
      State still lives in the button tint (red = muted, amber = verbose on). Muted mic (turn 88, "style
      it to show it's muted"): the emoji can't carry the old SVG slash, so a red diagonal + faded glyph
      overlay it. Renders monochrome in headless capture; full colour on his Segoe-UI-Emoji browser.
- [x] The single voice-orb became the wireframe's **per-agent participant orbs**. *(turn 24)*

## Interaction model  (REFINED 2026-09-02, turns 72–75 — my first summary was wrong)
- [x] **Power button = the two-way CHANNEL toggle**, NOT a mic control. Off → the UI plays nothing
      back, but the agent's turns stay **queued**; on → the queued turns play, **per the lane he's in**.
      *(turn 72)* — verified already correct in the code.
- [x] **Mic toggle = MUTE the mic + select the mic device**; the speaker picker selects the output.
      **Muting only — neither turns the channel off.** *(turn 72)* — verified already correct.
- [x] **Tapping an orb switches lanes**, nothing else. *(turn 30)*
- [x] Verify the current power/mute behaviour matches the above. Done — it did.

## Canvas
- [x] **Canvas bordered in the live agent's color** (`--canvas-c` set from the live lane's hue), so he
      knows **which agent's canvas** is shown. *(turn 30)*

## Iteration
- [x] **Live-reload / push updates**: no manual browser refresh to see a UI change — `POST /reload` →
      SSE `reload` → the page re-fetches itself. *(turns 31–33)* — see the live-channel guard in Bugs.

## Bugs
- [x] Interaction model was ALREADY correct in the code (verified 2026-09-02): `#power` owns the
      two-way channel with queued replies, `#mute` mutes the mic (he still hears replies), the pickers
      select devices. My earlier summary was wrong; no behaviour change needed.
- [x] **Only the orb is clickable, not the whole row cell.** The `.laneorb` cell is `pointer-events:none`;
      only the disc + its label take the pointer and the hand cursor. *(turns 80–81)*
- [x] **"Channel off" is obvious.** The whole room (orbs, canvas, transcript) desaturates + dims when
      the channel is off, and the power button stays lit as the one way back on. *(turns 82–83)*
- [x] **Live-reload dropped the LIVE channel** (turns 92–94: "the power button was toggled … because I
      was chatting with another agent"). A pushed `/reload` did an unconditional `location.reload()`,
      which destroys the AudioContext + mic — a browser won't restart either without a fresh tap — so it
      silently dropped him mid-turn and reset the power button. FIXED: the reload handler HOLDS while
      `data-off="false"` (running && channelOpen) and applies the pending reload the instant he next
      toggles off (MutationObserver on `data-off`); a warm dot on the power button (`data-update-waiting`)
      flags a held update. He refreshed manually (turn 106) so his page now runs the fix.
- [ ] **Flicker on lane switch** — switching agents briefly flashes "the vision tunnel lanes below
      them, just while it's switching". *(turns 78–79)* A CSS fix is in (`#orbs:not([hidden]) ~ #lanes
      { display:none !important }` — the most likely cause, the legacy `#lanes` strip flashing in a
      JS-timing gap), but it was **not statically reproducible**, so it needs **his live eyes** to
      confirm it's actually gone. ← open on his confirmation, not on code.

## The reference — check every fix against these
- [ ] **Match the renders in the project notes project note** `Command Bridge.md` — the wireframe and the
      `command-bridge-meeting-*.png` screenshots. JJ: "I like what I see there and it's not what I'm
      seeing live." The wireframe-matching orb design (`command_bridge/meeting.py` `_orb`) is now in the
      live `index.html`. ← subjective; **his call** on whether live finally matches what he liked. *(turn 34)*

---
_Source: `sessions/dev.jsonl` turns 17–112, 2026-09-02. More coming._
