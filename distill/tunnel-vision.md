# tunnel-vision — code distillation

**Purpose of this document:** a factual inventory of what is *actually built* in
`tunnel-vision`, read from the code, to ground the Command Bridge project that will
absorb this canvas. Every claim below is grounded in the source; where a doc/comment disagrees with
the code, the code is trusted and the disagreement is called out. No changes or merge design are
proposed here.

**What it is (one line):** a local, loopback-only "infinite canvas" server + CLI. An agent draws
frames into a browser page and drives the camera to direct a human's attention while it talks. The
tool is deliberately *dumb* — it holds no model, models no geometry, and makes no decisions; the
agent driving it is the intelligence. It is the sibling/return-leg of `voice-tunnel` (intent out by
voice; understanding back by sight) and can follow voice-tunnel's live lane.

- **Language/runtime:** Python ≥3.10, **stdlib only** in the server (`pyproject.toml` `dependencies = []`).
  `run`'s optional extras: `pandas>=2.0`, `matplotlib>=3.7` (`[data]`); `pytest>=8.0` (`[dev]`).
- **Entry point:** `tunnel-vision = tunnel_vision.cli:main` (console script); `python -m tunnel_vision`
  (`__main__.py` → `cli.main`); `bin/tunnel-vision` (sh) and `bin/tunnel-vision.cmd` shims run from the
  checkout via `PYTHONPATH` (prefer a repo `venv/`, else `python`).
- **Modules:** `cli.py` (surface + `describe` contract), `server.py` (HTTP+SSE, canvas, lanes),
  `page.py` (the whole browser page as one string + version hash), `store.py` (disk persistence),
  `cue.py` (schedule builder, pure fn), `follow.py` (voice-lane follower), `runner.py` (`run`),
  `extract.py` (`--section` markdown slicing), `shot.py` (headless screenshot).
- **Persistence files (home dir):** `~/.tunnel-vision.json` (server pointer: port/url/started, written
  unconditionally by every `serve`), `~/.tunnel-vision-canvas.json` (the saved canvas — all lanes).
- **Network:** binds `127.0.0.1` only. Default port **8770**. The only outbound requests are CDN
  scripts pulled by the page (mermaid, highlight.js `@highlightjs/cdn-assets`, marked, vega/vega-lite/vega-embed).

---

## 1. CLI command surface

Global option (on the top-level parser, applies to every subcommand):

- `--lane <name>` — "which agent you are"; your own canvas. Only the live lane may move the camera.
  Default `""` (empty → server's default lane `main`). Held in a module global `LANE`; `_request()`
  injects `{"lane": LANE}` into any dict payload that lacks one. For `batch`, each op inherits `LANE`
  unless it names its own. Subparsers are `required=True` (a bare `tunnel-vision` errors).

Exit codes: `0` ok; `1` when the response dict contains an `error` key, no server reachable, or (for
`batch`) any op failed. `serve` returns 0. `describe` returns 0.

### 17 subcommands

**1. `describe`** — prints the `CONTRACT` dict (JSON, indented) and exits 0. No args. This is the
machine-readable contract; the code asserts it "wins over any prose." (See §6 for where the contract
text itself has drifted from the parser/defaults.)

**2. `serve`** — start the surface (long-running; loopback bind).
- `--port` (int, default `8770`)
- `--verbose` (flag) — log requests to stderr
- `--fresh` (flag) — start from an empty canvas *without deleting* the saved one (it returns on the
  next normal start; the first change here overwrites it)
- `--no-follow` (flag; `dest=follow`, `store_false`) — do not mirror voice-tunnel's live lane
- `--session <name>` (default `dev`) — which voice-tunnel session to follow
- On start: loads the saved canvas (unless `--fresh`), starts the debounced writer thread, starts the
  `Follower`, writes `~/.tunnel-vision.json`, prints a JSON line
  (`serving`, `port`, `page_version`, `canvas_file`, `restored_frames`, `restored_lanes`), then
  `serve_forever()` until KeyboardInterrupt; deletes the pointer file on exit.

**3. `set`** — place or replace a frame. Exactly one content source is **required** (mutually
exclusive group): `--mermaid` | `--markdown` | `--svg` | `--html` | `--text` | `--file` | `--content`.
- `--id <name>` (default `""` → reserved frame `main`). Re-using an id **replaces that frame in place**,
  keeping its layout slot and `created` stamp.
- `--at "x,y"` — explicit canvas coordinate (parsed via `_at`; bad input `SystemExit`s); default is
  automatic deterministic shelf-packing.
- `--scale <float>` (default 0 → none) — render this frame larger (2 = twice size, text and all);
  carries *importance*, so zoom level becomes reading order.
- `--file <path>` — read content from a file; pair with `--kind`.
- `--content <str|->` — content inline, or `-` to read stdin (routes shell out of the content path).
- `--kind <mermaid|markdown|html|svg|text>` (default `mermaid`) — used with `--file`/`--content`.
- `--section <HEADING>` (`append`, repeatable) — **markdown only**; render just that section(s)
  VERBATIM via `extract.py`. Errors if used on non-markdown or if nothing matches; reports `omitted`
  and `missing` section lists in the response.
- `--title <str>` — frame title bar text.
- POSTs `/frame`. Returns include `delivered_to` (open browsers that got it; **0 = nobody looking**),
  `frames` (count), `id`, `lane`, `kind`.

**4. `look`** — the attention verb; bring the human to a frame, fitted & centred.
- `id` (positional, optional, default `""`)
- `--all` (flag) — frame the whole canvas instead
- POSTs `/look`. Unknown id → 404 with the real frame list (exit 1). Far frames are *travelled* to
  (fly-to); near ones are a direct hop. Refused (409) off-lane.

**5. `point`** — highlight a frame or something inside one (empty selector clears).
- `selector` (positional, optional, default `""`). Grammar: `<frame>:step:<n>` (a whole sequence step —
  number+label+arrow), `<frame>:text:<needle>` (prose by content), `<frame>:<thing>` (scope to one
  frame), a bare frame id, a CSS selector, or a bare mermaid node id.
- `--look` / `--zoom` (alias; `dest=look`, flag) — move the camera there too.
- POSTs `/point`. Returns a `warning` when the target is off screen and no `--look`. Refused off-lane.

**6. `cue`** — highlights timed to a sentence being spoken (marked pointing).
- `--text` — the sentence with inline `[point:<selector>]` marks (stripped from spoken text).
- `--words <json|->` — the `words` array from `voice-tunnel say --timings` (accepts the whole response
  object too; the CLI extracts `.words`). **MEASURED** timing.
- `--seconds <float>` — clip duration; **ESTIMATED** timing (proportional split by char offset).
- `--cancel` (flag) — stop a running schedule and clear the highlight.
- `--arm` (flag) — store the schedule and start it when THIS lane goes live (held-clip case). **The one
  camera-adjacent action a background lane may take.**
- `--look <frame>` — with `--arm`: bring the camera to this frame before the first mark.
- `--lead <float>` — with `--arm`: clip lead-in seconds (`held_for` from `say`), waited before mark 1.
- POSTs `/cue`. Returns `timing` (`measured`|`estimated`|`immediate`), `marks` ([{selector, at}]),
  `text`, and `warnings` (off-screen/empty-selector marks). Refused off-lane *unless* `--arm`.

**7. `inspect`** — ask the PAGE what a selector resolves to (cheaper than a screenshot; ~50 tokens).
- `selector` (positional, required). Same grammar as `point`.
- POSTs `/inspect`; the server pushes an `inspect` SSE event to the page and blocks up to 1.5 s for the
  page to POST `/inspected` back. Returns `resolved` (count), `elements` (tag/el_id/data_id/cls/frame/
  pointed/visible/text), `focused`. 409 if no browser connected; 504 if the page doesn't answer.

**8. `remove`** — delete one frame. `id` (positional, required). POSTs `/remove`; 404 if unknown.
Scoped to the caller's lane.

**9. `clear`** — empty the canvas and reset the camera (no args). POSTs `/clear`. Scoped to the caller's
lane; clearing the last lane deletes the canvas file outright.

**10. `zoom`** — the low-level camera (prefer `look`).
- `selector` (positional, optional) — zoom to fit this element
- `--scale` — a number (1 = actual size) or `fit`
- POSTs `/zoom`. Refused off-lane.

**11. `run`** — execute a local `.py` file and render its code beside its result.
- `file` (positional, required)
- `--id <name>` — frame to render into
- `--no-code` (flag) — show only the result
- `--title <str>` — default `run: <filename>`
- Executes locally via `runner.run_file` (which returns HTML), POSTs `/frame` with `kind: html`.
  Detection order: matplotlib figure(s) as inline theme-inheriting SVG → most-likely DataFrame as a
  table (`result`→`df`→`out`→last defined) → stdout → traceback. Code shown syntax-highlighted.

**12. `raise`** — ask for attention without taking the screen.
- `--why <str>` — one line describing what you want to show
- POSTs `/raise`. Increments a per-lane hand count; the page shows a ✋ chip. If already live, returns a
  no-op note. (This is the only camera-adjacent thing that is *never* refused off-lane — it exists for
  the background lane.)

**13. `switch`** — hand the floor to a lane (a hand-off, not a grab).
- `to` (positional, required) — target lane
- POSTs `/switch` → `set_live(target)`.

**14. `chart`** — a Vega-Lite chart: send the spec once, then stream rows (the only tier whose second
update is cheaper than its first).
- `--id <name>`
- `--spec <file|->` — a Vega-Lite spec (path or stdin); POSTs `/frame` with `kind: vega`
- `--rows <json|->` — a JSON array of rows to append; POSTs `/rows` (no markup crosses the wire)
- `--replace` (flag) — with `--rows`: clear existing rows first
- `--data-name <name>` (default `table`) — the named data source in the spec rows attach to
- `--title <str>`, `--scale <float>` (default 0)
- Errors (SystemExit) if neither `--spec` nor `--rows`. `--rows` with no such chart → server 404 with a
  remedy naming `--spec`. Returns `inserted` (row count) on append.

**15. `batch`** — apply many operations from **stdin** in one call (one process launch).
- No flags. Reads a JSON array from stdin; each element `{op, ...args}` where `op` ∈
  `set|remove|clear|point|look|zoom|cue|raise|switch` (mapped to routes by `Handler.OPS`).
  **Note:** `chart`/`/rows` and `inspect` are *not* in the batch op map.
- POSTs `/batch`. Returns `{"results": [...]}` (per-op, in order; a failed op returns its error in place
  and the rest still run). Prints indented JSON and returns 1 if any op failed (adds `failed` count),
  else 0. Bad stdin JSON → error + exit 1.

**16. `shot`** — screenshot the canvas in its own throwaway headless browser (never touches anyone's
browser).
- `path` (positional, optional; default a temp file `<tmp>/tunnel-vision.png`)
- `--width` (int, default **900**), `--height` (int, default **560**)
- `--look <FRAME>` — frame this frame before the shutter (fires a real `look` into the capture page);
  else captures the fit-everything view
- `--settle <MS>` (default `4000`) — how long to let the page render before capturing
- Calls `shot.capture(url, target, width, height, look, LANE, settle)`. Honors `--lane` by pinning the
  capture page to that lane via `?lane=` (local pin, only in shot mode; never moves the server's floor).
  Uses `?shot=<ms>` one-shot mode + the `/settle` slow script to hold the load event. Never raises for a
  missing browser (returns an `error` dict).

**17. `status`** — what is on the canvas and what the human can see.
- `--of <LANE>` — inspect a lane without switching to it (`which = args.of or LANE`)
- GETs `/status` (with `?lane=` when a lane is named). Returns `clients`, `lane`, `live_lane`, `lanes`
  (summary with frame counts + raised state), `frames` (each with id/kind/title/scale/at/age_s/created/
  updated_s + page-measured x,y,w,h), `viewport` ({visible,partial,offscreen,scale}, **null when no
  browser connected**), `page_version`, `canvas_file`, `follow` (voice-lane follow status).

---

## 2. Server + rendering architecture

### The SSE contract (one multiplexed stream)

- **Transport:** `ThreadingHTTPServer` on `127.0.0.1`, `HTTP/1.1`, `daemon_threads`. One `text/event-
  stream` at `GET /events`; **not** one stream per lane or per frame — HTTP/1.1 caps a browser at 6
  connections/origin, so fan-out dies at 7. Every connected browser gets its own `queue.Queue` in
  `_subscribers`; `publish(event, payload)` fans one event to all queues and returns the count (this is
  the `delivered_to` value). A 15 s queue timeout emits a `: keep-alive` comment.
- **Connect handshake (order matters):** on a new `/events` connection the server sends, in order:
  1. `version` — `{version}` (page self-upgrade; an older page-hash tab calls `location.reload()`).
  2. `sync` — `{live, lanes:{lane:[frame ids]}, hands}`. The server is authoritative: it declares what
     it believes in *before* replaying, so a browser that survived a restart drops frames the new
     server never heard of (they'd otherwise be unaddressable).
  3. Any `_armed` cues (so a late-joining page still receives a held schedule).
  4. Each backlogged `frame` (`{...frame, lane}`), and for a vega frame with stored rows, a `rows`
     event replaying accumulated rows (`remove_all: True`).
  Then it loops publishing live events.
- **Server→page event types:** `version`, `sync`, `cue`, `frame`, `rows`, `remove`, `clear`, `raise`,
  `switch`, `look`, `point`, `zoom`, `inspect`.
- **Page→server POSTs:** `/placed` (measured geometry + viewport — inbound truth, *not* fanned out),
  `/inspected` (answer to an inspect request), `/switch` (human clicks a lane chip).

### The frame model

- A frame is `{id, kind, content, title}` plus optional `scale`, `at`, `rows`/`data_name` (vega), and
  server-stamped `created`/`updated`. Stored per lane in `_lanes: dict[lane, dict[frame_id, frame]]`
  (a lane materializes on first write). Dict insertion order is the packing/layout order.
- **Ids:** default id is `main` (`DEFAULT_FRAME`). Re-placing an id replaces in place and keeps the
  layout slot + `created`, moves `updated`.
- **Age-stamping is the server's job, not the caller's** (TC3 of spec 004): an agent that could set its
  own timestamps could make a stale frame look fresh. `status` reports `age_s`/`updated_s` (seconds);
  the page renders a relative age ("just now", "14m ago", "yesterday") on the title bar, repainted every
  30 s.
- **The server models NO geometry.** Only the browser knows a rendered thing's size or where the camera
  ended up. The page shelf-packs frames and POSTs measured `frames{x,y,w,h}` + `viewport` to `/placed`;
  the server just relays it in `status`. Positions are absent from `status.frames` until a browser has
  rendered.

### Render tiers (all client-side)

Frame `kind` values handled by the page's `paint()`:
- **`mermaid`** — cheapest tier (~16 tokens for a small flowchart); rendered by mermaid.js off-document
  then swapped in; a bad definition shows the error rather than blanking. `unshrink()` restores intrinsic
  size from the viewBox (mermaid stamps `width="100%"`).
- **`vega`** (Vega-Lite) — the chart tier; spec once, then row changesets via `view.change()`. The live
  vega view is kept in a `views` map for streaming. Forced transparent + `currentColor` chrome for both
  themes. Degrades to spec-as-text if vega fails to load.
- **`markdown`** — rendered via marked.js into `.md` (max-width 68ch). Degrades to `<pre>` if marked
  absent.
- **`html`** / **`svg`** — `innerHTML` verbatim. (`run` output arrives as `html`.)
- **`text`** — plain `<pre>` textContent.
- Highlight.js runs over `pre code` after paint. **Nothing inside a card scrolls** (the canvas is the
  scrollbar); a diagram/svg card is uncapped width, everything else caps at 1400px.

### The camera model (agent-driven `look`)

- One CSS transform on `#stage` (`translate + scale`); cards are absolutely positioned in canvas space
  and never need to know the zoom. Per-lane camera `{x,y,k}` is parked on lane switch.
- **`look`** fits & centres a frame (via `zoomTo`), or `--all` fits the whole extent. Returns
  immediately; the animation plays in the page. Unknown id → 404 (exit 1). `set` **never** moves the
  camera — attention is directed only by `look`.
- **Fly-to (spec 007):** a far move zooms out just far enough to show both endpoints, travels, and zooms
  back in (`FLIGHT_MS = 600`, scale dips on `sin(πt)` toward a bounded `low`); a near move stays a direct
  hop (`HOP_MS = 350`). Distance never enters duration.
- **Readability:** below `READABLE = 0.62` scale a tall frame is fit-to-width and parked at the top
  (reading position) rather than shrunk to fit whole.
- **The human always wins:** wheel/drag/dblclick cancel an in-flight flight (`cancelFlight`) and take the
  camera. A window resize does *not* refit (deliberate — also fixes `shot --look`). After a settle the
  page reports the viewport so `status` can answer "what can he see?".
- **Viewport in status:** `{visible, partial, offscreen, scale}`; **null when no browser connected** (an
  honest "nothing visible" beats a stale value).

### The attention channel (`point` / `cue`)

- `point`/`cue`/`look`/`zoom` travel as **separate SSE events from `frame`**, so a highlight costs a few
  bytes rather than re-sending markup.
- **Selector resolution (in the page, `findEl`/`findEls`):** frame id wins over anything inside a frame;
  `frame:selector` scopes explicitly; the last-`look` **focused** frame is searched before the rest of
  the canvas (camera takes precedence over highlighting). Resolution prefers **exactness before
  proximity** and **nodes before edges** (mermaid names a node `flowchart-FR-6` and its edge `L_AG_FR_0`,
  so a naive substring match would light the arrow). A sequence *step* is 3 siblings (number/label/arrow)
  matched by document-order index; a multi-element match draws a single `.halo` rectangle over the
  combined bounds rather than N outlines. Prose is reachable by `text:<needle>` (smallest containing
  element wins).
- **`cue` speech-sync (`cue.py`, a pure function):** `[point:<sel>]` marks are parsed out; each mark
  records the word index and char offset that precede it (against the stripped text). Three time sources,
  labelled by `timing`:
  - **`measured`** — `words` from `say --timings`; each mark fires at `words[word_index].t` (a mark after
    the last word fires at the last word's time). Word-accurate.
  - **`estimated`** — `seconds` alone; `at = seconds * char / total_chars`. Clause-accurate at best.
  - **`immediate`** — neither; every mark at 0.0 (degenerate, never errors).
  The `timing` field is load-bearing: an estimate must never be presented in a measurement's shape.
  The schedule runs entirely in the browser off ONE event (`runCue` sets local `setTimeout`s), so network
  jitter stays outside the sentence and the CLI can keep listening. A new `cue` cancels the prior; a plain
  `point` overrides a running cue.
- **Arming (built in code):** `cue --arm` stores the schedule server-side in `_armed[lane]` and the page
  keeps it in an `armed` Map; when the lane goes live, `fireArmed(lane)` brings the camera (if `--look`),
  waits `lead_ms`, then runs the marks. This is the one camera-path a non-live lane may use.

### Lanes

- Each agent gets its own canvas; exactly one lane is **live** (`_live`, default `main` = `DEFAULT_LANE`).
  `set_live()` is the single place `_live` moves (shared by the human's chip-click via `/switch` and the
  voice-follower). Switching clears that lane's raised hand and drops its armed cue (the page fires its
  own copy on switch).
- **Camera verbs are refused off-lane:** `_camera()` returns **409** for `point`/`look`/`zoom`/`cue` from
  a lane ≠ `_live` (the sole exception is `cue --arm`). The 409 names the live lane and carries the exact
  `raise` command to run instead. `set`, `remove`, `clear`, `rows`, `raise` are *not* camera verbs and are
  allowed off-lane (each scoped to the caller's own lane — a lane cannot delete another's work).
- **`raise`** puts a ✋ (with count + optional `--why` as a title tooltip) on the lane's chip without
  taking the screen. **`switch`** is the only way an agent may change the live lane (a hand-off).
- The page renders **only the live lane** (others are hidden divs, not merely scrolled off). Empty lanes
  are not shown as chips (an empty lane is "a name somebody typed"). `status --of <lane>` inspects another
  lane's frames without switching.

### Voice-lane follow (`follow.py`, one-way, read-only, soft)

- A background `Follower` thread polls the voice-tunnel's `GET /status` and mirrors its **`status.lane`**
  field onto this canvas via `set_live` (so switching agent by voice moves the canvas in the same
  gesture). **One direction only** — the canvas never writes to the voice tunnel.
- **Soft dependency:** no import of the other tool, no config required, no error/log when absent; a
  missing tunnel is indistinguishable from not having asked. `status.follow` reports whether it's
  following, from which file, reachable, current lane, switch count. Re-resolves while absent (a tunnel
  started later is picked up without restarting the canvas).
- **Discovery order:** `$VOICE_TUNNEL_SESSIONS` → sibling `../voice-tunnel/sessions` → `~/.voice-tunnel/
  sessions`; looks for `<session>.server.json` (default session `dev`). Polls `/status` (never the
  turn-consuming `watch`). Follows `status.lane`, deliberately **not** `status.live_lane`.
- **Poll interval discrepancy (code vs doc):** the actual constant is **`POLL_S = 0.1` (100 ms)** with a
  code comment explaining the change from 400 ms; the module's own top docstring, spec 009, and AGENTS.md
  still say 400 ms. Code wins: 100 ms.

### Persistence (`store.py`)

- **One file for all lanes** (`~/.tunnel-vision-canvas.json`): lanes' frames + geometry + which lane is
  live. A per-lane file was rejected (partial-restore states). Written by a single **debounced background
  thread** (`DEBOUNCE = 1.0 s`); mutations only `touch()` a dirty flag and return, so no HTTP handler ever
  waits on disk. Writes are **atomic** (same-dir temp + fsync + `os.replace`). `VERSION = 1`.
- **Deliberately NOT persisted:** the camera (a reconnect must never move the view) and raised hands
  (attention state belongs to a running agent). `clear` on the last lane `discard()`s the file. A missing/
  empty/corrupt file yields an empty canvas + a stderr warning and **always starts** (rebuild-don't-trust:
  non-dict lanes and non-frame entries are dropped).

---

## 3. Feature set by arc

Grouped by the arc the specs/code trace. All are **built** unless noted.

**Foundation**
- Single-frame surface → infinite canvas of addressable frames (`set --id`, accumulate not replace) — built.
- One multiplexed SSE stream, connect-time replay of the whole canvas, page self-upgrade by version hash — built.
- Stdlib-only server; page as one string with no build step — built.

**Attention (the load-bearing arc)**
- Agent-driven camera: `look`/`look --all`, `point --look` compose, off-screen warning, human-wins cancel — built.
- Viewport report in `status` (visible/partial/offscreen/scale; null when unconnected) — built.
- Fly-to travel vs direct hop (spec 007) — built.
- `point` selector grammar (frame id, `frame:thing`, `frame:step:N`, `frame:text:...`, mermaid nodes,
  sequence-step halo) — built.
- `cue` marked pointing with measured/estimated/immediate timing (spec 005) — built.
- Armed cue for a held clip (`--arm/--lead/--look`) — **built in code**, but has no spec of its own and
  spec 009's AC7 still records it as "not built" (see §6).
- `inspect` (page answers a selector query cheaply instead of a screenshot) — built.

**Evidence**
- `run <file.py>` → code + matplotlib-SVG / DataFrame-table / stdout / traceback, theme-inheriting — built.
- Vega-Lite chart tier: spec-once then row changesets, rows stored + replayed, both-theme chrome (spec 001) — built.
- `--section` verbatim markdown extract with reported omissions/missing (`extract.py`) — built.

**Canvas**
- Deterministic shelf packing + `--at` override; `--scale` = importance/reading-order; per-frame age
  stamps — built.
- Session persistence across restart (spec 004): all lanes, live lane, geometry; `--fresh`; corrupt-tolerant — built.

**Lanes**
- Canvas-per-agent, one live lane, camera verbs 409'd off-lane, `raise`/`switch`, per-lane camera,
  `status --of` (spec 006) — built.
- Follow voice-tunnel's live lane, soft/one-way (spec 009) — built.

**Ergonomics**
- `batch` (stdin, one process launch, per-op results, failures don't abort) + `set --content -` (spec 008) — built.
- `describe` as the generated contract; JSON-out with meaningful exit codes throughout — built.
- `bin/` shims (sh + cmd) run from the checkout via PYTHONPATH — built.

**Tooling**
- `shot` headless screenshot in a throwaway profile (`?shot=<ms>` + `/settle` hold; `--look`, `--lane`
  pin, browser auto-discovery) — built.
- Scratch verification harnesses (`scratch/verify_batch.py`, `verify_cue_timing.py`, `voice_latency.py`) — built.

**Fix (defensive/hardening, mostly documented in spec Findings)**
- Server authoritative on reconnect (`sync` drops orphaned frames after a restart) — built.
- Windows double-`serve` port trap + `~/.tunnel-vision.json` pointer-hijack — documented traps, not fixed
  in code (environmental) — open/known.
- Lane-scoped `status`/`shot` (the earlier "`--lane` ignored" defect) — **fixed** in current code (see §6).

---

## 4. Specs inventory

All spec files carry frontmatter `status`. Every one reads **complete**. (`005` frontmatter is
`complete`; its body also carries a "UNBLOCKED AND BUILT" banner.) PROJECT_UNDERSTANDING.md's older
one-liner ("003 🟡 · 004 · 005 blocked") is stale relative to the spec frontmatter and code — trust the
specs/code.

| # | Title | What (one line) | Status (frontmatter) |
|---|-------|-----------------|----------------------|
| 001 | A chart tier: send a spec once, then stream rows | Vega-Lite `chart --spec`/`--rows`; second update is cheaper than the first | complete |
| 002 | Infinite canvas — frames accumulate instead of replacing | Addressable frames, `set --id`, auto-pack, `remove`/`clear`, whole-canvas replay | complete |
| 003 | Agent-driven camera — directing attention across the canvas | `look`/`--all`, `point --look`, viewport in `status`, off-screen warning, human-wins | complete |
| 004 | Session persistence — the canvas survives a restart, says how old it is | Disk store (all lanes + live + geometry), `--fresh`, server-stamped age | complete |
| 005 | Marked pointing — the highlight fires inside the sentence | `cue` with measured/estimated/immediate timing from `[point:]` marks | complete |
| 006 | Lanes — a canvas per agent, a hand raised instead of a camera taken | Per-lane canvas, live-lane-only camera, `raise`/`switch`, `status --of` | complete |
| 007 | Fly-to — the camera travels instead of cutting | `look` zooms out/travels/zooms in for far moves; direct hop for near | complete |
| 008 | One call, many operations — a CLI an agent can think in code with | `batch` from stdin, per-op results, `set --content -` | complete |
| 009 | The canvas follows the voice lane — one switch, not two | Soft one-way follower mirroring voice-tunnel's `status.lane` | complete |

**Count: 9 specs, all marked complete/shipped.** Notable unticked acceptance criteria *within* completed
specs (structural, not regressions): 006 AC3 & AC9 (a `shot` browser can't witness "no movement" or a
restored per-lane camera — needs a page already open); 009 AC7 (arming — since built in code but never
re-ticked and never given its own spec).

---

## 5. Test surface

- **Invocation:** `python -m pytest tests` (`pyproject.toml` `[tool.pytest.ini_options] testpaths =
  ["tests"]`). The `bin/` shim / developing doc prefer a repo `venv/`; tests insert the repo root on
  `sys.path`. `data` extras (pandas/matplotlib) tests are `skipif`-guarded when absent.
- **Rough count:** **46 test functions** across 5 files (grep of `^def test_`):
  - `test_cue.py` — 13: mark parsing, measured/estimated/immediate `timing`, double-space collapse,
    measured-beats-estimated, plus two server-level regression tests that arming is allowed off-lane and a
    normal cue is refused off-lane.
  - `test_store.py` — 9: round-trip all lanes + geometry + live, corrupt/missing tolerance, non-frame
    dropping, atomic write, `discard`, disabled store, `touch` writes nothing (NFR1).
  - `test_follow.py` — 9: soft absence, corrupt/no-port server files, session name, search-order (env var
    before sibling before home; every path derived not hard-coded), disabled = nothing looked for.
  - `test_extract.py` — 8: preamble addressable, `#` inside a fence ≠ heading, verbatim extract, reported
    omissions/missing, document-order preservation, forgiving-title/exact-content.
  - `test_runner.py` — 7: HTML escaping, stdout shown, `--no-code`, traceback rendered, language from
    extension, DataFrame `result` wins (skip w/o pandas), figure → theme-inheriting SVG w/ no baked color
    (skip w/o matplotlib).
- **Explicitly NOT unit-tested (by policy):** the camera, the packer, and the pointer — an assertion over
  a computed transform proves the arithmetic ran, not that the result is legible. They are verified by
  `status` + a screenshot, and by human-in-the-loop manual ACs.
- **There is no `test_server.py`** — the server is exercised indirectly (the two arming/refusal tests in
  `test_cue.py` construct a bare `Handler` and call `_camera` directly) and via the scratch harnesses.
- **Harnesses (not pytest):** `tunnel_vision/shot.py` (the `capture` screenshot tool — its own headless
  browser, `?shot=`/`/settle`, browser auto-discovery); `scratch/verify_batch.py` (re-runnable spec-008
  acceptance, incl. the byte-identical markdown round-trip against the saved canvas); `scratch/
  verify_cue_timing.py` (differential two-shutter cue timing); `scratch/voice_latency.py` (voice/latency
  measurement behind the follow decision). There is **no** `uisim` or dedicated `layout` harness in the
  tree despite the task's hint — layout/packing lives in `page.py` and is verified only by `shot` +
  `status`. `examples/` holds `chart.py` (matplotlib) and `table.py` (pandas) exercising `run`.

---

## 6. Known open defects & code/doc disagreements

**Genuinely open / unresolved (from spec Findings + code):**
- **Windows double-`serve` port trap.** Killing the shell may leave the server running; a second `serve`
  binds the same port without erroring, and requests go to whichever won. Presents as "my change did
  nothing." Environmental — documented in `ai-docs/workflows/developing.md` and spec 002 Findings, not
  fixed in code.
- **`~/.tunnel-vision.json` pointer hijack.** Every `serve` (including a throwaway `--fresh`/other-port
  instance) rewrites the pointer unconditionally; killing that throwaway leaves `tv` pointing at a dead
  port while a healthy server runs elsewhere. Symptom names the wrong process. Spec 004 Findings; not
  fixed.
- **006 AC3 / AC9 unverifiable by `shot`.** "No camera movement when a background frame lands" and
  "per-lane camera restored on return" are properties of an already-open page; `shot` opens a fresh
  browser each time, so they remain manually unverified rather than ticked.
- **Stale-resident-model silent `say --timings`.** Documented in spec 005 Findings as an upstream
  (voice-tunnel) issue: a stale in-memory model returns no `words` and no `timings_unavailable`, so `cue`
  can't tell "cannot report" from "nothing to report." Not a tunnel-vision code defect but affects the
  `cue` measured path.

**Code/doc disagreements (code trusted):**
- **`shot` default viewport.** The `describe` CONTRACT text says `--width/--height` default **1600x1000**;
  the actual argparse defaults and `shot.capture` default are **900x560** (`cli.py` lines ~461-462;
  `shot.py`). Code wins: 900x560.
- **`shot` flags not in the contract.** The parser has `shot --look <FRAME>` and honors `--lane` (via the
  global) by pinning the capture page; the `describe` `shot` entry documents neither. `shot.py`'s error
  remedy also mentions a `--browser <path>` flag that **does not exist** in the parser.
- **`set` flags not in the contract.** The parser accepts `--content` (incl. `-` for stdin) and
  `--section` (repeatable, markdown-only, verbatim); the `describe` `set.args` block lists neither.
- **Voice follow poll interval.** Code `POLL_S = 0.1` (100 ms); the `follow.py` module docstring, spec
  009 (FR/tasks), and AGENTS.md all say 400 ms. Code wins: 100 ms.
- **Arming is built but "spec-orphaned."** `cue --arm/--lead/--look`, server `_armed`, and page
  `fireArmed` are fully implemented, yet spec 009 AC7 records arming as "not built" and lists it as
  out-of-scope needing its own spec; no spec 010 exists. The feature ships without a spec documenting it
  as done.
- **`--lane` on `status`/`shot` — previously a real defect, now fixed.** The task flagged
  `shot`/`status` ignoring `--lane`. In the *current* code both honor it: `status` uses `which = args.of
  or LANE` and appends `?lane=`; `shot` passes `LANE` into `capture`, which appends `&lane=` and the page
  pins to it in shot mode. Spec 006 Findings describe this as the bug that was found and fixed the same
  hour. So it is closed, not open.
- **`batch` op coverage.** `batch` dispatches only `set|remove|clear|point|look|zoom|cue|raise|switch`
  (`Handler.OPS`); `chart`/`/rows` and `inspect` are not batchable. Not a bug, but a real boundary worth
  noting for anything that assumes batch covers the whole surface.
- **PROJECT_UNDERSTANDING.md is stale** on spec status ("003 🟡 · 005 blocked") and on "005 marked
  pointing is blocked" — contradicted by the spec frontmatter and the shipped `cue.py`. Trust the specs
  and code.
