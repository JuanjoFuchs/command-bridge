---
id: "045"
title: Hide repo boxes from the fleet view on request
status: done
blocked_by: []
blocks: []
---

# Hide repo boxes from the fleet view on request

## Overview

The fleet view (spec 021) draws a box for every repo the agents touched, private work repos
included. JJ, voice, 2026-10-13, before a public demo: *"just a way to ask you to clean the ribbon,
say which boxes to remove. Just that."* The liaison agent hides named boxes deterministically, and
shows them again, through the CLI. No model, no page control: a persisted list of names the server
drops from the page's fleet payload.

## Functional requirements

- **FR1 — Verbs.** `fleet hide <repo>...`, `fleet unhide <repo>... | --all`, `fleet hidden`. Bare
  `fleet [--hours]` is unchanged (plus a `hidden` field). Results are JSON; a failure is
  `{error, code, remedy}`.
- **FR2 — Match the box label.** A name matches a repo name a box can show (the git root's basename),
  case-insensitively, stored in the box's spelling. A name matching nothing is reported in `unknown`
  with the `known` names and never stored (dictated names are fuzzy). All names unknown ->
  `unknown_repo`, exit 2. A name already hidden still matches, so a repeat is a no-op.
- **FR3 — Persists.** `<session dir>/.agent-fleet/hidden.json`, written atomically; survives a page
  reload and a restart.
- **FR4 — Live.** The server drops every file in a hidden repo from `_fleet_activity` (the ready
  frame, the 2s `lane_activity` broadcast and `/fleet/state`), re-reading the file each time, so a
  hide lands on the open page within one broadcast — no refresh. The page rebuilds its node set from
  each payload, so a box with no files left is gone.
- **FR5 — Only the page is filtered.** Bare `fleet` still reports every repo.

## Acceptance criteria

- **AC1** hide / hidden / unhide round-trip through the CLI, persisted to disk (test).
- **AC2** Unknown names are reported, not stored; all-unknown is `unknown_repo`, exit 2 (test).
- **AC3** `_fleet_activity` omits a hidden repo's files, case-insensitively, and keeps the rest (test).
- **AC4** `describe` documents the verbs; the describe contract tests stay green.
