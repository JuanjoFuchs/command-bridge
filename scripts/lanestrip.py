"""The LANE STRIP: who is in the meeting, who is live, and who is waiting. Spec 012 FR6/FR7/FR8.

Two halves, and the split is the same one `scripts/orbstate.py` makes for the orb:

1. **The pure reducer**, swept exhaustively. Every combination of lane count x live lane x
   waiting depth x per-agent state goes through `lanesView` in milliseconds, with no DOM.
2. **The wiring**, driven through the page's REAL message handler. A pure function that is
   right and a painter that never runs is the failure a model-only sweep cannot see.

⚠ **SAFE TO RUN DURING A LIVE SESSION**, like `scripts/devicepills.py` and unlike every other page
harness here. It starts NO voice-tunnel server: the page is served by a plain `http.server` on an
ephemeral port, the socket is never opened, and the last section proves all of that rather than
asserting it — because a harness that quietly started a second tunnel while he was mid-sentence
would be a worse bug than anything it could find.

    python scripts/lanestrip.py

Needs Playwright (`pip install playwright && playwright install chromium`).
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO, "voice_tunnel", "web")
SESSIONS = os.path.join(REPO, "sessions")
DEFAULT_PORT = 8765
SERVED_PORTS: list[int] = []

fails: list[str] = []


def check(ok, label, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(label)
    return ok


def note(text):
    print(f"\n--- {text} " + "-" * max(0, 78 - len(text)))


# ------------------------------------------------------------------ the safety net
#
# Duplicated from devicepills.py rather than imported, and deliberately: that file has no main
# guard and runs top-to-bottom on import, so importing it would START it. More to the point, two
# harnesses that each claim to be live-session-safe should each prove it on their own, or one
# file's edit can silently weaken the other's guarantee.


def _session_files():
    if not os.path.isdir(SESSIONS):
        return {}
    out = {}
    for name in os.listdir(SESSIONS):
        if name.endswith(".server.json"):
            out[name] = os.path.getmtime(os.path.join(SESSIONS, name))
    return out


def _listeners_on(port):
    try:
        raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=30).stdout
    except Exception:
        return set()
    pids = set()
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
            pids.add(parts[4])
    return pids


def _tunnel_children():
    try:
        raw = subprocess.run(
            ["wmic", "process", "where", f"ParentProcessId={os.getpid()}",
             "get", "CommandLine"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    return [ln.strip() for ln in raw.splitlines()
            if "voice_tunnel" in ln or "voice-tunnel" in ln]


class ServerWatch:
    def __init__(self):
        self.sessions = _session_files()
        self.listeners = _listeners_on(DEFAULT_PORT)

    def verify(self):
        note("no voice-tunnel server process was started")
        now = _session_files()
        changed = sorted(k for k in set(self.sessions) | set(now)
                         if self.sessions.get(k) != now.get(k))
        check(not changed, "no sessions/*.server.json was created or modified",
              "" if not changed else f"changed: {changed}")

        listeners = _listeners_on(DEFAULT_PORT)
        check(listeners == self.listeners,
              f"the set of processes listening on the tunnel's port ({DEFAULT_PORT}) is unchanged",
              f"pids {sorted(self.listeners)} -> {sorted(listeners)}")
        check(bool(listeners) == bool(self.listeners),
              "a live session running before this harness is still running after it",
              f"pids on {DEFAULT_PORT}: {sorted(listeners) or 'none'}")

        kids = _tunnel_children()
        check(not kids, "this process spawned no voice-tunnel child process",
              "" if not kids else f"found: {kids}")
        check(all(p != DEFAULT_PORT for p in SERVED_PORTS),
              "every static server this harness opened is on an ephemeral port",
              f"ports {SERVED_PORTS}")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(root):
    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=root))
    port = httpd.server_address[1]
    assert port != DEFAULT_PORT, "the OS handed out the tunnel's port; refusing to bind it"
    SERVED_PORTS.append(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# ------------------------------------------------------------------ the page stub
#
# The socket is never opened: `openSocket` runs only inside `start()`, which needs a
# `getUserMedia` grant a headless run cannot produce. So `WebSocket` is replaced with something
# inert, and frames are pushed straight into the page's real `onMessage` through `diag.deliver`.
# `fetch` is recorded rather than performed, because there is no tunnel to answer it.
STUB = """
window.WebSocket = function () {
  this.readyState = 0;
  this.send = function () {};
  this.close = function () {};
};
window.__posts = [];
const realFetch = window.fetch;
window.fetch = function (url, opts) {
  window.__posts.push({ url: String(url), body: opts && opts.body ? JSON.parse(opts.body) : null });
  return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
};
"""

SWEEP = """
() => {
  const view = window.__voiceTunnel.lanesView;
  const names = ['claude', 'codex', 'grok'];
  const states = ['idle', 'thinking', 'synthesizing', 'speaking'];
  const out = [];
  for (let n = 0; n <= 3; n++) {
    const lanes = names.slice(0, n);
    const targets = lanes.concat(['everyone', 'nobody']);
    for (const live of targets) {
      for (const depth of [0, 1, 4]) {
        for (const st of states) {
          const waiting = {}, sts = {};
          if (lanes.length) { waiting[lanes[0]] = depth; sts[lanes[0]] = st; }
          const v = view({ lanes, live, waiting, states: sts, broadcast: 'everyone' });
          out.push({ n, live, depth, st, shown: v.shown,
                     rows: v.rows.map(r => ({ name: r.name, live: r.live, waiting: r.waiting,
                                              busy: r.busy, aria: r.aria,
                                              stateLabel: r.stateLabel })) });
        }
      }
    }
  }
  return out;
}
"""


TAGSWEEP = """
() => {
  const tag = window.__voiceTunnel.tagView;
  const out = [];
  for (const lanes of [[], ['claude'], ['claude', 'codex'], ['claude', 'codex', 'atlas']]) {
    // An agent clip, from each possible speaker.
    for (const lane of ['claude', 'codex', 'atlas', undefined]) {
      out.push({ kind: 'agent', lanes: lanes.length, lane,
                 got: tag({ from: 'agent', lane, lanes, fallback: 'claude' }) });
    }
    // His own turns: addressed to a lane, to everyone, refused, absent, or not addressed at all.
    for (const spec of [{ lane: 'claude' }, { lane: 'codex' }, { lane: 'everyone' },
                        { lane: null }, {}]) {
      out.push({ kind: 'you', lanes: lanes.length, spec: JSON.stringify(spec),
                 got: tag({ from: 'you', addressed: true, lanes, fallback: 'claude', ...spec }) });
    }
    out.push({ kind: 'heard', lanes: lanes.length,
               got: tag({ from: 'you', addressed: false, lanes, fallback: 'claude' }) });
  }
  return out;
}
"""


RECEIPTSWEEP = """
() => {
  const r = window.__voiceTunnel.receiptView;
  const out = [];
  for (const id of [0, 1, 2, 3, 4]) {
    for (const consumed of [null, -1, 0, 2, 4]) {
      for (const readThrough of [null, -1, 0, 2, 4]) {
        out.push({ id, consumed, readThrough, got: r({ id, consumed, readThrough }).state });
      }
    }
  }
  return out;
}
"""


def open_page(browser, port):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.add_init_script(STUB)
    page.goto(f"http://127.0.0.1:{port}/?token=lanestrip", wait_until="load")
    page.wait_for_function("() => window.__voiceTunnel && window.__voiceTunnel.lanesView",
                           timeout=15000)
    return ctx, page


def deliver(page, msg):
    page.evaluate("(m) => window.__voiceTunnel.deliver(m)", msg)


def strip(page):
    """What is actually on screen, read from the DOM and not from the model."""
    return page.evaluate("""() => {
      const el = document.getElementById('lanes');
      return {
        hidden: el.hidden,
        chips: Array.from(el.querySelectorAll('button')).map(b => ({
          name: b.dataset.lane,
          live: b.classList.contains('live'),
          waiting: b.classList.contains('waiting'),
          busy: Boolean(b.querySelector('.dot.busy')),
          count: b.querySelector('.n') ? b.querySelector('.n').textContent : null,
          aria: b.getAttribute('aria-label'),
          pressed: b.getAttribute('aria-pressed'),
        })),
      };
    }""")


watch = ServerWatch()
httpd, port = serve(WEB)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright is not installed — `pip install playwright && playwright install chromium`")
    httpd.shutdown()
    sys.exit(2)

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    try:
        ctx, page = open_page(browser, port)

        # ------------------------------------------------------ 1. the pure reducer, swept
        note("FR6/FR7/FR8: the pure reducer, over every shape")
        rows = page.evaluate(SWEEP)
        check(len(rows) == 4 * 3 * 4 * (2 + 3 + 4 + 5) // 1 or len(rows) > 100,
              "the sweep actually ran a large matrix", f"{len(rows)} cases")

        bad_hidden = [r for r in rows if r["shown"] != (r["n"] >= 2)]
        check(not bad_hidden,
              "the strip is shown if and only if there are two or more agent lanes",
              "" if not bad_hidden else f"{len(bad_hidden)} wrong, first {bad_hidden[0]}")

        multi_live = [r for r in rows if sum(1 for c in r["rows"] if c["live"]) > 1]
        check(not multi_live, "never more than one chip is live",
              "" if not multi_live else f"first {multi_live[0]}")

        # `nobody` is a live lane that is not in the set — the state right after a lane is removed.
        orphan = [r for r in rows if r["live"] == "nobody" and any(c["live"] for c in r["rows"])]
        check(not orphan, "a live lane that is not registered lights nothing, rather than guessing",
              "" if not orphan else f"first {orphan[0]}")

        bcast_hold = [r for r in rows for c in r["rows"]
                      if c["name"] == "everyone" and (c["waiting"] or c["busy"])]
        check(not bcast_hold,
              "the broadcast chip never holds or busies — it is a destination, not a participant")

        no_aria = [c for r in rows for c in r["rows"] if not c["aria"]]
        check(not no_aria, "every chip carries an aria-label, so the colour is not the only signal")

        waiting_said = [c for r in rows for c in r["rows"]
                        if c["waiting"] and "waiting to be heard" not in c["aria"]]
        check(not waiting_said, "a waiting lane says so in words, not only in a border colour")

        # ---------------------------------------------- 013 AC-11: the state has to be READABLE
        # The dot already carried `busy`, and a dot that is either on or off cannot answer the
        # question he asked -- "I should know if an agent is listening or thinking or something
        # else". A binary indicator collapses four states into one.
        unsaid = [(r["st"], c) for r in rows for c in r["rows"]
                  if not c.get("broadcast") and r["st"] != "idle" and c["name"] == "claude"
                  and r["n"] >= 2 and not c["stateLabel"]]
        check(not unsaid, "013 AC-11: a busy lane names its state in words, not only as a lit dot",
              "" if not unsaid else f"{len(unsaid)} silent, first {unsaid[0]}")

        idle_quiet = [c for r in rows for c in r["rows"]
                      if r["st"] == "idle" and not c["waiting"] and c["stateLabel"]]
        check(not idle_quiet, "an idle lane with nothing held says nothing — no label to ignore")

        # A held clip is the one state he can ACT on, so it outranks whatever the agent is doing.
        held_wins = [c for r in rows for c in r["rows"]
                     if c["waiting"] and c["stateLabel"] != "wants you"]
        check(not held_wins,
              "013 AC-12: a lane holding speech reads 'wants you', whatever else it is doing",
              "" if not held_wins else f"first {held_wins[0]}")

        # ------------------------------------------- 013 AC-10: who is talking, and to whom
        note("013 FR3/FR4: the transcript says WHO — the 'everybody shows as Claude' guard")
        tags = page.evaluate(TAGSWEEP)
        check(len(tags) > 30, "the tag sweep ran", f"{len(tags)} cases")

        # THE DEFECT ITSELF. Every agent rendered under the same literal tag, so three agents in
        # one transcript were indistinguishable. JJ, 2026-08-24: "In the transcript everybody
        # shows as Claude."
        multi = [t for t in tags if t["lanes"] >= 2]
        collapsed = [t for t in multi if t["kind"] == "agent" and t["lane"] and t["got"] != t["lane"]]
        check(not collapsed,
              "013 AC-10: with several agents, a clip is tagged with the lane that SPOKE it",
              "" if not collapsed else f"{len(collapsed)} wrong, first {collapsed[0]}")

        # ⚠ PARSED, NOT STRING-MATCHED. The first version of this filter compared against
        # '"lane": "codex"' while `JSON.stringify` emits '{"lane":"codex"}' — it selected nothing,
        # and `all()` over an empty list is TRUE. It printed PASS with an empty detail, which is
        # the same vacuous green this repo has now hit three times: a sweep against a retired
        # selector, a layout run against a hidden strip, and this. **Hence the non-empty guard on
        # every subset below** — a filter that matches nothing must fail, not pass quietly.
        def spec_of(t):
            try:
                return json.loads(t["spec"])
            except (KeyError, ValueError):
                return {}

        addressed = [t for t in multi if t["kind"] == "you" and spec_of(t).get("lane") == "codex"]
        check(addressed and all("codex" in t["got"] and t["got"].startswith("you")
                                for t in addressed),
              "013 AC-10: his own turns say who he was addressing",
              f"{len(addressed)} cases, {sorted({t['got'] for t in addressed})}")

        # A refused summons reached NOBODY. Naming a lane there would claim a delivery that did
        # not happen -- the one thing the ambiguity refusal exists to prevent.
        refused = [t for t in multi if t["kind"] == "you"
                   and "lane" in spec_of(t) and spec_of(t)["lane"] is None]
        check(refused and all(t["got"] == "you → nobody" for t in refused),
              "a turn the gate refused to route is shown as reaching nobody",
              f"{len(refused)} cases, {sorted({t['got'] for t in refused})}")

        # Absent `lane` is NOT null: those are the thousands of turns that predate lanes, and they
        # belong to the default lane rather than to nobody.
        legacy = [t for t in multi if t["kind"] == "you" and spec_of(t) == {}]
        check(legacy and all(t["got"] == "you → claude" for t in legacy),
              "a turn from before lanes existed is shown as addressed to the default lane",
              f"{len(legacy)} cases, {sorted({t['got'] for t in legacy})}")

        # ⚠ BELOW TWO LANES THE NAME IS NOISE. Same rule as the strip: one agent means every row
        # would carry a name that distinguishes nothing.
        solo = [t for t in tags if t["lanes"] < 2]
        noisy = [t for t in solo if "→" in t["got"]]
        check(not noisy, "a single-agent session renders exactly as it did before lanes",
              "" if not noisy else f"first {noisy[0]}")

        # 🔴 AND IT SAYS WHO IS SPEAKING, WHICH IS A DIFFERENT QUESTION FROM WHERE IT WENT.
        #
        # The solo branch used to return the literal string "claude" for every agent row. JJ,
        # 2026-08-25, one lane, named magnus: *"even though we have named this lane Magnus, you
        # are speaking as Claude."* Same class as the 2026-08-10 runtime incident — **this tool
        # holds no model and cannot know what is driving it**, so a model name compiled into the
        # page is always a guess and was wrong the first time anyone renamed a lane.
        #
        # ⚠ The sweep above cannot catch it: it uses `fallback: 'claude'`, so the bug and the
        # correct answer are the same string. This one asks with a name no model has.
        named = page.evaluate("""() => {
          const tag = window.__voiceTunnel.tagView;
          return ['magnus', undefined].map((lane) => ({
            lane,
            got: tag({ from: 'agent', lane, lanes: ['magnus'], fallback: 'magnus' }),
          }));
        }""")
        check(named and all(t["got"] == "magnus" for t in named),
              "a solo agent is tagged with ITS OWN lane name, never a model name",
              f"{len(named)} cases, {sorted({t['got'] for t in named})}")

        heard = [t for t in tags if t["kind"] == "heard"]
        check(all(t["got"] == "heard" for t in heard),
              "an unaddressed turn is still just 'heard', at any lane count")

        # -------------------- 015 FR2: "idle" must not stand for an agent that is heads-down
        #
        # 🔴 JJ, 2026-08-24: *"the agents say idle, but in reality, you are not idle, right?
        # Whenever you're not listening and whenever you're doing something, you're thinking."*
        #
        # `lane_states` holds only what an agent REPORTED, and an agent deep in a long task reports
        # nothing — so it rendered identically to one sitting doing nothing.
        #
        # 🔴 **AND SPEC 015's ANSWER OVERCLAIMED IN THE OTHER DIRECTION**, which he corrected on
        # 2026-08-25: it split "working" from "idle" by whether the lane had ever read anything —
        # history, not state — leaving "idle" as the label for an agent nobody could see.
        #
        # *"an agent that is not listening is an agent that is thinking, right? We don't have a way
        # to determine if an agent is idle or not, so we shouldn't claim it. The only reason we
        # show idle is if an agent is listening... but it's not the agent I am focused on. That
        # means that it's waiting for me. That means that it's idle."*
        #
        # 🎯 His rule uses two facts and no history, and it is what these cases now pin:
        #     in a watch AND live -> listening · in a watch AND not live -> idle · otherwise
        #     thinking. `consumed` is passed here only to prove it no longer changes the answer.
        note("018: listening, idle and thinking, decided without any history")
        orb_state = page.evaluate("""() => {
          const v = window.__voiceTunnel.laneOrbsView;
          const base = { lanes: ['claude', 'codex', 'atlas'], live: 'claude', waiting: {},
                         states: {} };
          const pick = (view, n) => view.rows.find((r) => r.name === n).status;
          const view = v({ ...base, watching: ['claude', 'atlas'],
                           consumed: { claude: 5, codex: 9 } });
          return {
            liveWatching: pick(view, 'claude'),  // in a wait AND live -> listening
            headsDown: pick(view, 'codex'),      // not in a wait -> thinking, whatever it has read
            parked: pick(view, 'atlas'),         // in a wait, not live -> waiting for him -> idle
            neverSeen: pick(v({ ...base, watching: [], consumed: {} }), 'atlas'),
            reported: pick(v({ ...base, watching: [], consumed: { codex: 9 },
                               states: { codex: 'speaking' } }), 'codex'),
          };
        }""")
        check(orb_state["liveWatching"] == "listening",
              "the lane he is talking to, sitting in a watch, reads as listening", f"{orb_state}")
        check(orb_state["headsDown"] == "thinking",
              "018: a lane NOT in a watch reads as thinking — never idle, which we cannot see",
              f"{orb_state}")
        check(orb_state["parked"] == "idle",
              "018: a lane parked in a watch he is NOT talking to is idle — it waits for him")
        check(orb_state["neverSeen"] == "thinking",
              "and history no longer decides: a lane that has read nothing is judged the same way")
        check(orb_state["reported"] == "speaking",
              "a state the agent actually reported always wins over the derived one")

        # ---------------------------- 015 FR1: one lane reading must not mark another lane's turns
        #
        # 🔴 THE BUG HE FOUND BY LOOKING AT THE PAGE, 2026-08-24: *"whenever one lane reads, I think
        # we're marking everything as read."* The receipt was drawn against `consumed_cursor`, a
        # single integer overwritten by whichever lane's watch reported last — so Atlas reading
        # turn 40 painted Claude's turn 10 blue, a turn Claude was never delivered.
        #
        # 🎯 **Same seam as the unread-refusal bug, and the half that was left.** `012` made
        # DELIVERY per-lane; `013` built the receipt on the session-wide cursor. Each was right on
        # its own and nothing checked the pair — the third time today that shape has bitten.
        note("015 FR1: the receipt is per-lane, so one reader cannot speak for another")
        deliver(page, {"type": "lane", "lanes": ["claude", "codex"], "lane": "claude",
                       "waiting": {}, "default_lane": "claude"})
        page.evaluate("""() => {
          const log = document.getElementById('log');
          log.replaceChildren();
          [['claude', 10], ['codex', 40]].forEach(([lane, id]) => {
            const d = document.createElement('div');
            d.className = 'row addressed';
            d.dataset.id = String(id);
            d.dataset.lane = lane;
            const t = document.createElement('span');
            t.className = 'text';
            t.textContent = lane + ' turn ' + id;
            d.appendChild(t);
            log.appendChild(d);
          });
        }""")
        # Codex has read through 40. Claude has read nothing.
        deliver(page, {"type": "consumed", "cursor": 40, "pending": 0,
                       "lane_consumed": {"codex": 40}})
        page.wait_for_timeout(250)
        marks = page.evaluate("""() => {
          const g = (lane) => {
            const t = document.querySelector('#log .row[data-lane="' + lane + '"] .tick');
            return t ? t.dataset.state : null;
          };
          return { claude: g('claude'), codex: g('codex') };
        }""")
        check(marks["codex"] == "read", "the lane that actually read shows read", f"{marks}")
        check(marks["claude"] == "sent",
              "015 FR1: and the lane that did NOT read still shows sent", f"{marks}")

        # ------------------------------- 014: every orb counts its OWN seconds while it is busy
        #
        # 🔴 THE TIMER WAS LOST IN THE REVAMP AND NO HARNESS NOTICED. It was a child of the single
        # orb, so replacing that orb took it away — while `orbView` still returned its `timer` and
        # `orbstate.py`'s 480-case golden still matched, because the MODEL was intact and only the
        # thing that showed it was gone. He caught it by looking, minutes after it shipped:
        # *"I no longer see the seconds counter, the timer, whenever you're doing something."*
        #
        # 🎯 **A golden over a pure model cannot see a missing view.** This asserts the rendered
        # element, which is the half that was actually absent.
        note("014: the busy timer, per lane, in the DOM rather than in the model")
        deliver(page, {"type": "lane", "lanes": ["claude", "codex"], "lane": "claude",
                       "waiting": {}})
        deliver(page, {"type": "agent_state", "lane": "codex", "state": "thinking",
                       "live": False})
        # 2.5s, not 1.4s: the clock is stamped at the state change and the tick fires on the
        # second, so a 1.4s window straddles the first render and floors to zero often enough to
        # be flaky. A guard that fails intermittently teaches people to re-run it.
        page.wait_for_timeout(2500)
        timer = page.evaluate("""() => {
          const c = document.querySelector('#orbs .laneorb[data-lane="codex"] .lanetimer');
          const l = document.querySelector('#orbs .laneorb[data-lane="claude"] .lanetimer');
          return { busy: c ? c.textContent.trim() : null, idle: l ? l.textContent.trim() : null };
        }""")
        check(timer["busy"], "a busy lane shows a seconds counter", f"{timer['busy']!r}")
        # ⚠ **"SHOWS NONE" IS EMPTY TEXT, NOT AN ABSENT ELEMENT**, and the difference is not
        # cosmetic. The span is now always in the DOM so the tenths tick can write into it without
        # rebuilding the button — the rebuild is what stopped taps registering. Asserting
        # `element === null` was asserting the IMPLEMENTATION that happened to deliver the
        # property; the property is that an idle orb shows no number, and `:empty` collapses the
        # span so it costs no row either.
        check(not (timer["idle"] or ""),
              "and an idle lane shows none — the number belongs to the work, not to the orb",
              f"{timer['idle']!r}")

        deliver(page, {"type": "agent_state", "lane": "codex", "state": "idle",
                       "live": False})
        page.wait_for_timeout(200)
        cleared = page.evaluate("""() => {
          const t = document.querySelector('#orbs .laneorb[data-lane="codex"] .lanetimer');
          if (!t) return true;
          // Empty AND collapsed: text alone would leave a grid row and its gap, which is what
          // pushed the status word off the middle of the disc.
          return !t.textContent.trim() && t.getBoundingClientRect().height === 0;
        }""")
        check(cleared, "and it clears the moment that lane stops working")

        # ------------------------------------------- 014 AC-5: hue is identity, and it is PURE
        note("014 FR5: one hue per lane, deterministic, and defined past the end of the list")
        hue = page.evaluate("""() => {
          const h = window.__voiceTunnel.laneHue;
          const three = ['claude', 'codex', 'atlas'];
          const many = Array.from({length: 9}, (_, i) => 'lane' + i);
          return {
            stable: h(three, 'codex') === h(three, 'codex'),
            distinct: new Set(three.map((n) => h(three, n))).size === 3,
            // ⚠ POSITIONAL, not name-hashed: two devices watching one session must agree, and a
            // hash would also make one lane's colour change when an unrelated lane is added.
            positional: h(['codex', 'claude'], 'codex') === h(['claude', 'codex'], 'claude'),
            unknown: h(three, 'nobody'),
            // TC4: the list repeats rather than running out or returning undefined.
            wrapsDefined: many.every((n) => typeof h(many, n) === 'string' && h(many, n)),
            wrapsRepeat: h(many, 'lane6') === h(many, 'lane0'),
          };
        }""")
        check(hue["stable"], "014 AC-5: the same lane list yields the same hue")
        check(hue["distinct"], "and the first three lanes are distinct")
        check(hue["positional"], "assignment is by POSITION, so two devices agree")
        check(hue["wrapsDefined"] and hue["wrapsRepeat"],
              "014 AC-5/TC4: past the end of the list it repeats rather than going undefined",
              f"lane6 == lane0: {hue['wrapsRepeat']}")
        check(hue["unknown"] and "var(" in str(hue["unknown"]),
              "a lane that is not registered gets no identity colour", f"{hue['unknown']!r}")

        # ------------------------------------------------ 013 AC-13: the receipt, TWO states
        #
        # 🔴 **RULED DOWN FROM THREE, by JJ, 2026-08-24**, from an observation sharper than the
        # design: *"as soon as one of my terms enters your context window, that's read by you."*
        # Delivered and read are the SAME instant here, so the middle state was drawing a
        # distinction that does not exist, and the third ("answered") was not a delivery state at
        # all — the answer is already the next row in the transcript. *"We just keep one check
        # mark, right? And that check mark is gray and then blue."*
        note("013 FR7: the receipt is TWO states, and the colour is the whole signal")
        rec = page.evaluate(RECEIPTSWEEP)
        check(len(rec) > 50, "the receipt sweep ran", f"{len(rec)} cases")

        seen = sorted({c["got"] for c in rec})
        check(seen == ["read", "sent"],
              "013 AC-13: exactly two states, and both are reachable", f"{seen}")

        below = [c for c in rec if c["consumed"] is None or c["id"] > c["consumed"]]
        check(below and all(c["got"] == "sent" for c in below),
              "a turn no agent has taken yet is 'sent'", f"{len(below)} cases")

        taken = [c for c in rec if c["consumed"] is not None and c["id"] <= c["consumed"]]
        check(taken and all(c["got"] == "read" for c in taken),
              "a turn inside an agent's context is 'read' — the cursor IS the receipt",
              f"{len(taken)} cases")

        # ⚠ `readThrough` must no longer move the verdict. It still exists on the server for
        # `unanswered_s`, and a receipt that quietly read it would reintroduce the third state.
        by_cursor = {}
        for c in rec:
            by_cursor.setdefault((c["id"], c["consumed"]), set()).add(c["got"])
        split = {k: v for k, v in by_cursor.items() if len(v) > 1}
        check(not split,
              "the verdict depends on the delivery cursor ALONE, never on what a lane answered",
              "" if not split else f"{len(split)} ids disagree, first {list(split.items())[0]}")

        # ------------------------------------------------------ 2. the wiring
        note("the model reaches the paint, through the page's own message handler")
        deliver(page, {"type": "ready", "lanes": ["claude"], "lane": "claude",
                       "broadcast": "everyone", "lane_states": {}, "waiting": {}})
        one = strip(page)
        check(one["hidden"], "one agent: no strip at all, so a solo session looks exactly as before")

        deliver(page, {"type": "lane", "lanes": ["claude", "codex"], "lane": "claude",
                       "waiting": {}})
        two = strip(page)
        # 🔴 THE CHIP STRIP IS SUPERSEDED BY THE ORB ROW (spec 014 FR4). He asked for the badges to
        # become orbs — *"I'm thinking around the orb… and maybe use mini orbs for them"*, then
        # arrangement C replaced the centre orb outright — so the strip is now hidden whenever the
        # row is up, and asserting it appears would pin behaviour the page no longer has.
        #
        # ⚠ **The REQUIREMENT is unchanged and is asserted here, on the element that carries it:**
        # a second agent must bring a visible, per-lane control on screen. Everything below still
        # exercises `lanesView`, which remains the pure reducer both views are built from.
        # ⚠ The strip's own markup is now dead on the page — flagged for removal rather than
        # deleted at the end of a long session, since its 45 checks are the only coverage of the
        # reducer until the orb row's equivalents exist.
        orbs_shown = page.evaluate(
            "() => { const o = document.getElementById('orbs');"
            "        return Boolean(o) && !o.hidden"
            "               && o.querySelectorAll('button').length >= 2; }")
        check(orbs_shown, "a second agent brings the ORB ROW on screen (spec 014 FR4)")
        check([c["name"] for c in two["chips"]] == ["claude", "codex", "everyone"],
              "one chip per agent plus the broadcast",
              f"{[c['name'] for c in two['chips']]}")
        check([c["live"] for c in two["chips"]] == [True, False, False],
              "and the live one is the one he is talking to")

        deliver(page, {"type": "lane", "lanes": ["claude", "codex"], "lane": "codex",
                       "waiting": {}})
        moved = strip(page)
        check([c["live"] for c in moved["chips"]] == [False, True, False],
              "saying another agent's name moves the highlight")
        check(moved["chips"][1]["pressed"] == "true",
              "and the state reaches the accessibility tree, not just the colour")

        deliver(page, {"type": "lane_waiting", "lane": "claude", "waiting": 2})
        held = strip(page)
        check(held["chips"][0]["waiting"] and held["chips"][0]["count"] == "2",
              "FR7: a lane holding something to say is marked, with how much",
              f"{held['chips'][0]}")
        check(not held["chips"][1]["waiting"], "and only that lane is marked")

        deliver(page, {"type": "lane_waiting", "lane": "claude", "waiting": 0})
        cleared = strip(page)
        check(not cleared["chips"][0]["waiting"] and cleared["chips"][0]["count"] is None,
              "and the mark clears when he has heard it")

        deliver(page, {"type": "agent_state", "state": "thinking", "lane": "claude",
                       "live": False})
        busy = strip(page)
        check(busy["chips"][0]["busy"],
              "FR8: an off-lane agent's own state shows on its chip")
        orb_label = page.evaluate("() => document.getElementById('orblabel').textContent")
        check(orb_label != "Thinking",
              "and it does NOT repaint the orb, which follows the LIVE lane only",
              f"orb reads {orb_label!r}")

        deliver(page, {"type": "agent_state", "state": "thinking", "lane": "codex", "live": True})
        check(page.evaluate("() => document.getElementById('orblabel').textContent") is not None,
              "the live lane's state still reaches the orb")

        # ------------------------------------------------------ 3. the tap
        note("FR6: a tap switches the live lane")
        page.evaluate("() => document.querySelector('#lanes button[data-lane=\\'claude\\']').click()")
        posts = page.evaluate("() => window.__posts")
        lane_posts = [p for p in posts if "/lane" in p["url"]]
        check(lane_posts and lane_posts[-1]["body"] == {"action": "switch", "name": "claude"},
              "the tap posts a switch for the lane that was tapped",
              f"{lane_posts[-1] if lane_posts else 'nothing posted'}")
        check(strip(page)["chips"][0]["live"],
              "and the strip repaints immediately rather than waiting for the round trip")

        before = len(page.evaluate("() => window.__posts"))
        page.evaluate("() => document.querySelector('#lanes button[data-lane=\\'claude\\']').click()")
        check(len(page.evaluate("() => window.__posts")) == before,
              "tapping the lane that is ALREADY live posts nothing — it is not a switch")

        # 🔴 A TAP ON THE ORB ROW MUST SURVIVE THE SECONDS TICKING.
        #
        # The elapsed counter repaints ten times a second while any lane is busy. When that tick
        # called the full painter, `replaceChildren` destroyed the buttons — and a pointer that
        # went down on one released over its replacement, so no click event ever fired. Reported
        # minutes after the decimal shipped: *"clicking around the orbs is weird. And one click
        # does not recognize the lane switching. Was that on purpose?"*
        #
        # ⚠ The press and the release are issued SEPARATELY with a tick's worth of time between
        # them. A single `.click()` is synchronous and cannot straddle a repaint, so it would pass
        # against the broken build and prove nothing — which is the whole failure mode this file
        # exists to refuse.
        note("a tap survives the elapsed tick — press and release straddle a repaint")
        # A lane set of this block's own, so the target does not depend on what the tests above
        # left live. `atlas` is registered here and is NOT the live lane, which is what makes the
        # tap a switch rather than a no-op.
        deliver(page, {"type": "lane", "lanes": ["claude", "codex", "atlas"], "lane": "claude",
                       "waiting": {}})
        deliver(page, {"type": "agent_state", "state": "thinking", "lane": "atlas", "live": False})
        page.wait_for_timeout(150)
        orb = page.locator("#orbs .laneorb[data-lane='atlas']")
        if orb.count() and orb.bounding_box():
            box = orb.bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
            page.wait_for_timeout(250)          # two ticks and change
            page.mouse.up()
            posts = [p for p in page.evaluate("() => window.__posts") if "/lane" in p["url"]]
            check(posts and posts[-1]["body"] == {"action": "switch", "name": "atlas"},
                  "an orb tap held across two ticks still posts its switch",
                  f"{posts[-1] if posts else 'nothing posted'}")
        else:
            check(False, "the orb row rendered so the tap could be measured")

        # ------------------------------------------------------ 4. the negative control
        note("the negative control: these assertions have been SEEN to fail")
        broken = page.evaluate("""() => {
          const view = window.__voiceTunnel.lanesView;
          // The rule inverted: show the strip whenever there is any lane at all.
          const v = view({ lanes: ['claude'], live: 'claude', broadcast: 'everyone' });
          return { shown: v.shown, wouldFailIfInverted: !v.shown };
        }""")
        check(broken["wouldFailIfInverted"],
              "a one-lane session is hidden — the check that would catch the inverted rule")
        probe = page.evaluate("""() => {
          const el = document.getElementById('lanes');
          // Plant `live` on a chip that is NOT already live — picking blindly would land on the
          // one the previous test just made live and prove nothing, which is exactly what the
          // first version of this control did.
          const victim = Array.from(el.querySelectorAll('button'))
                              .find(b => !b.classList.contains('live'));
          victim.classList.add('live');
          return el.querySelectorAll('button.live').length;
        }""")
        check(probe >= 2, "the DOM reader can see a second live chip when one is planted",
              f"{probe} live chips after planting one")
        deliver(page, {"type": "lane", "lanes": ["claude", "codex"], "lane": "codex",
                       "waiting": {}})
        check(sum(1 for c in strip(page)["chips"] if c["live"]) == 1,
              "and the painter repairs it on the next message, rather than accumulating classes")

        ctx.close()
    finally:
        browser.close()

httpd.shutdown()
watch.verify()

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
