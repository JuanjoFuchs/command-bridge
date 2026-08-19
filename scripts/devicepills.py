"""The pill rule, in a real browser — and an instrument that has been seen to fail.

**Why this harness exists, in one sentence:** a rule that is right in the model and wrong in the
DOM is the orb bug in a new place, and no amount of pure sweeping can see it.

Reported live 2026-08-18, on a headset, with the speaker list offering only `default`: *"It seems
we need to be smarter than that, right? If there is no speaker to choose, why would we show a
drop-down to choose a speaker?"* Spec 009 answers that by counting distinct devices, and
`tests/test_device_pills.py` sweeps the rule as pure logic. This file is the other half: the same
enumeration shapes driven through the REAL page, in a real Chromium, so that "the model says
hidden" and "the pill is off the screen" are two separate claims that must both hold.

WHAT IT DOES NOT DO, and this constraint shaped everything below (TC4). **A live voice session is
running on `dev`, port 8765.** No verification for this spec may start, restart or stop a
`voice-tunnel` server, and `layout.py`, `channel.py`, `orbstate.py`, `bargein.py` and `e2e.py` each
start one. So this harness serves `voice_tunnel/web/index.html` over a plain `http.server` on an
ephemeral loopback port — `http://127.0.0.1` is a secure context, so `getUserMedia`,
`enumerateDevices` and `AudioContext` are all available — and never speaks to a tunnel at all. The
page's WebSocket 404s, `openSocket` resolves on `onerror`, and the device path runs regardless;
`assert_no_server_started()` below checks that claim rather than asserting it, and says in its own
output exactly what it does and does not cover.

THE NEGATIVE CONTROL IS THE POINT (AC14). "No output pill" and "the harness never found the pill,
never loaded the page, or asserted on an element that no longer exists" produce identical output.
A harness that has never been seen to fail is indistinguishable from one that cannot fail, and this
repo has already shipped a denylist that refused nothing and three diagnostics that never
populated. So two deliberately broken copies of the page are built in a temp directory and run
through the SAME assertions, which must report them:

  * `rule`   — the visibility rule inverted at source (`outputs > 1` becomes `outputs > 0`), so the
               model and the DOM are broken together and agree with each other perfectly.
  * `desync` — the model left correct and the DOM forced to show the pill anyway. This is the orb
               defect's exact shape, and it is the one a model-only assertion cannot see.

Run: `python scripts/devicepills.py [-v]`
"""
import functools
import http.server
import os
import pathlib
import re
import socketserver
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "voice_tunnel", "web")
PAGE = os.path.join(WEB, "index.html")
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

# The reference model and the enumeration fixtures are IMPORTED from the pytest sweep, not
# re-stated. Two copies of the rule is two rules, and the case that only one of them covers is
# exactly the case nobody looks at.
sys.path.insert(0, ROOT)
from tests.test_device_pills import CASES, dev, pill_model  # noqa: E402

# Read out of the source rather than imported. This harness deliberately does not import any part
# of `voice_tunnel` — the strongest available guarantee that it cannot start one is that it never
# loads the code that could.
DEFAULT_PORT = int(re.search(
    r"^DEFAULT_PORT\s*=\s*(\d+)",
    pathlib.Path(ROOT, "voice_tunnel", "config.py").read_text(encoding="utf-8"),
    re.M).group(1))

fails = []
notes = []


def check(ok, label, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))
    if not ok:
        fails.append(label)
    return ok


def note(text):
    notes.append(text)
    print(f"      {text}")


# ------------------------------------------------------------------ AC15: nothing was started
#
# Asserted, not intended. Three independent checks, each with a different blind spot, because no
# single one of them covers the question on its own.

def _session_files():
    d = pathlib.Path(ROOT, "sessions")
    if not d.is_dir():
        return {}
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in d.glob("*.server.json")}


def _listeners_on(port):
    import psutil
    out = set()
    for c in psutil.net_connections(kind="inet"):
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
            out.add(c.pid)
    return out


def _descendants():
    import psutil
    me = psutil.Process(os.getpid())
    out = {}
    for p in me.children(recursive=True):
        try:
            out[p.pid] = " ".join(p.cmdline() or [])
        except Exception:
            out[p.pid] = "<unreadable>"
    return out


TUNNEL_ENTRYPOINTS = ("voice-tunnel-run.py", "-m voice_tunnel", "voice_tunnel.cli",
                      "bin/voice-tunnel", "bin\\voice-tunnel", "voice-tunnel.cmd")


def tunnel_children():
    """Descendants of THIS process that look like a voice-tunnel invocation.

    Matched on the entry point, not on the path: this repository lives in a directory called
    `voice-tunnel`, so every python process started from it carries that string and a substring
    match on the cwd would flag the harness itself.
    """
    hits = {}
    for pid, cmd in _descendants().items():
        low = cmd.lower()
        if any(e.lower() in low for e in TUNNEL_ENTRYPOINTS):
            hits[pid] = cmd
    return hits


class ServerWatch:
    def __init__(self):
        self.sessions = _session_files()
        self.listeners = _listeners_on(DEFAULT_PORT)

    def verify(self):
        print()
        print("--- AC15: no voice-tunnel server process was started ---------------------------")
        now_sessions = _session_files()
        changed = sorted(k for k in set(self.sessions) | set(now_sessions)
                         if self.sessions.get(k) != now_sessions.get(k))
        check(not changed,
              "no sessions/*.server.json was created or modified",
              "" if not changed else f"changed: {changed}")

        now_listeners = _listeners_on(DEFAULT_PORT)
        check(now_listeners == self.listeners,
              f"the set of processes listening on the tunnel's default port ({DEFAULT_PORT}) "
              "is unchanged",
              f"pids {sorted(self.listeners)} -> {sorted(now_listeners)}")
        check(bool(now_listeners) == bool(self.listeners),
              "the live session that was running before this harness is still running after it",
              f"pids on {DEFAULT_PORT}: {sorted(now_listeners) or 'none'}")

        kids = tunnel_children()
        check(not kids, "this process spawned no voice-tunnel child process",
              "" if not kids else f"found: {kids}")

        check(all(p != DEFAULT_PORT for p in SERVED_PORTS),
              "every static server this harness opened is on an ephemeral port, not the tunnel's",
              f"ports {SERVED_PORTS}")

        print("      WHAT THIS COVERS: a server started by this process (any port, any session "
              "dir) shows up as a child; a server writing to <repo>/sessions shows up as a file; "
              "anything binding 8765 shows up as a listener.")
        print("      WHAT IT DOES NOT COVER: a server started by some OTHER process while this "
              "ran, a detached server that reparented away before the check, or a server on a "
              "non-default port with an isolated VOICE_TUNNEL_DIR started outside this tree. "
              "The structural guarantee behind those is that this file imports no part of the "
              "`voice_tunnel` package and spawns no subprocess except Playwright's browser.")


SERVED_PORTS = []


# ------------------------------------------------------------------ a plain static server


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(root):
    """A static file server on an ephemeral loopback port. NOT a voice-tunnel process: it has no
    `/ws` route, no session directory and no token — it hands out one HTML file."""
    httpd = socketserver.TCPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=root))
    port = httpd.server_address[1]
    assert port != DEFAULT_PORT, "the OS handed out the tunnel's port; refusing to bind it"
    SERVED_PORTS.append(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# ------------------------------------------------------------------ the platform stub
#
# Installed BEFORE the page's own scripts run (Playwright's add-init-script). Later is too late:
# the page enumerates on load, and a stub arriving afterwards would be compared against a first
# paint taken from the real machine's devices.
#
# `navigator.mediaDevices` is patched in place rather than replaced, because the page registers its
# `devicechange` listener behind `"ondevicechange" in navigator.mediaDevices` — a plain object
# substituted for the real MediaDevices fails that test, the listener is never registered, and the
# whole harness then measures a page that cannot react to anything.

STUB = r"""
(() => {
  window.__loadStamp = String(Math.random());   // proves a check ran without a reload
  window.__stubDevices = [];
  window.__enumCalls = 0;
  window.__gumCalls = 0;
  window.__sinkCalls = [];
  window.__fakeSink = "";
  // When set, `setSinkId` records the request and the sink does NOT move — a platform that
  // accepts the call and routes audio somewhere else, which is the 2026-08-15 report exactly and
  // the only way to stage "stored preference A, live sink B" for FR3.
  window.__sinkPinned = false;

  const md = navigator.mediaDevices;
  md.enumerateDevices = async () => {
    window.__enumCalls++;
    return window.__stubDevices.map((d) => ({
      deviceId: d.deviceId, kind: d.kind, label: d.label || "", groupId: d.groupId || "",
    }));
  };
  // A silent oscillator, exactly as scripts/orbstate.py does it: `start()` needs a real stream to
  // reach `ctx = new AC()`, and this harness needs the CONTEXT, never the audio.
  md.getUserMedia = async () => {
    window.__gumCalls++;
    const c = new (window.AudioContext || window.webkitAudioContext)();
    if (c.state === "suspended") await c.resume();
    const dest = c.createMediaStreamDestination();
    const osc = c.createOscillator(), gain = c.createGain();
    gain.gain.value = 0.0; osc.frequency.value = 180;
    osc.connect(gain); gain.connect(dest); osc.start();
    return dest.stream;
  };

  const AC = window.AudioContext || window.webkitAudioContext;
  if (window.__noSetSinkId) {
    // The platform axis: Chrome for Android reports `setSinkId` present and cannot honour it, but
    // a platform that does not expose it at all is the other half of the sweep.
    try { delete AC.prototype.setSinkId; } catch (e) {}
  } else {
    // The sink is stubbed at the READBACK, which is the seam FR3 turns on. Routing itself is out
    // of scope (TC1 — Android cannot route from the page and this spec does not try), so what has
    // to be controllable is what the page learns the sink IS, not where audio really goes.
    Object.defineProperty(AC.prototype, "sinkId", {
      configurable: true, get() { return window.__fakeSink; },
    });
    AC.prototype.setSinkId = async function (v) {
      const want = (v && typeof v === "object") ? (v.type || "") : (v || "");
      window.__sinkCalls.push(want);
      if (!window.__sinkPinned) window.__fakeSink = want;
    };
  }

  // ONE PROBE, used against the real page and against every broken copy. If the negative control
  // ran through a different reader it would prove the reader, not the assertions.
  window.__vtProbe = () => {
    const vis = (id) => {
      const e = document.getElementById(id);
      return !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
    };
    const opts = (id) => {
      const e = document.getElementById(id);
      return e ? Array.from(e.options).map((o) => [o.value, o.textContent]) : null;
    };
    // ROUTE TEXT, FOUND BY WHAT IT LOOKS LIKE RATHER THAN BY AN ID. Everything visible inside
    // <main> that is not the orb, the error line, a toggle or a <select>. An id-based read would
    // pass if the route were painted into some other element, and "the page says nothing" has to
    // mean the page says nothing.
    const skip = new Set(["orbwrap", "orb", "orblabel", "orbtimer", "err",
                          "mute", "verbose", "dev", "mic", "spk"]);
    const walk = (n) => {
      if (n.nodeType === 3) return n.textContent;
      if (n.nodeType !== 1) return "";
      if (skip.has(n.id)) return "";
      const tag = (n.tagName || "").toLowerCase();
      if (tag === "select" || tag === "option" || tag === "svg") return "";
      if (n.hidden) return "";
      const cs = getComputedStyle(n);
      if (cs.display === "none" || cs.visibility === "hidden") return "";
      let s = "";
      for (const c of n.childNodes) s += walk(c);
      return s;
    };
    const vt = window.__voiceTunnel || {};
    return {
      dom: { dev: vis("devpick"), mic: vis("micpick"), spk: vis("spkpick") },
      opts: { dev: opts("dev"), mic: opts("mic"), spk: opts("spk") },
      model: vt.pills || null,
      hasPureFn: typeof vt.pillsView === "function",
      sink: vt.sink || null,
      routeText: walk(document.querySelector("main")).replace(/\s+/g, " ").trim(),
      routeVisible: vis("route"),
      loadStamp: window.__loadStamp,
      label: (document.getElementById("orblabel") || {}).textContent,
      errors: (vt.errors || []).slice(),
    };
  };
})();
"""

# Swapping the enumeration and letting the page react. Waits for the stub to actually be consulted
# rather than for a fixed delay — `refreshDevices` is async and awaits `applySink` inside it, so a
# read taken on a timer lands mid-update often enough to be flaky and never often enough to notice.
APPLY = r"""
async (devs) => {
  window.__stubDevices = devs;
  const before = window.__enumCalls;
  navigator.mediaDevices.dispatchEvent(new Event("devicechange"));
  const t0 = Date.now();
  while (window.__enumCalls === before && Date.now() - t0 < 3000)
    await new Promise((r) => setTimeout(r, 4));
  await new Promise((r) => setTimeout(r, 40));   // let the awaited applySink pass settle
  return window.__enumCalls > before;
}
"""


def open_page(browser, port, *, set_sink_id=True, storage=None):
    ctx = browser.new_context()
    page = ctx.new_page()
    pre = []
    if not set_sink_id:
        pre.append("window.__noSetSinkId = true;")
    if storage:
        pre.append("try {" + "".join(
            f"localStorage.setItem({k!r}, {v!r});" for k, v in storage.items()) + "} catch(e){}")
    if pre:
        page.add_init_script("\n".join(pre))
    page.add_init_script(STUB)
    page.goto(f"http://127.0.0.1:{port}/?token=devicepills", wait_until="load")
    page.wait_for_function("() => window.__voiceTunnel && window.__vtProbe", timeout=15000)
    return ctx, page


def apply_devices(page, ins, outs):
    ran = page.evaluate(APPLY, list(ins) + list(outs))
    if not ran:
        raise AssertionError("a synthetic devicechange never reached enumerateDevices — the page "
                             "is not listening, so nothing below measures anything")
    return page.evaluate("() => window.__vtProbe()")


# ------------------------------------------------------------------ the reusable assertions
#
# One function, run against the real page and against every broken copy. AC14 is only worth
# anything if the mutant is judged by the identical instrument.

def android_shape_failures(probe):
    """Everything wrong with the page on the Android shape. Empty list means it is correct."""
    bad = []
    if probe["dom"]["spk"]:
        bad.append("the output pill is ON SCREEN with one unlabelled `default` output "
                   f"(options: {probe['opts']['spk']})")
    if probe["dom"]["dev"]:
        bad.append("the grouped pill is on screen with nothing to pair")
    if probe["routeText"]:
        bad.append(f"the page names a route it cannot know: {probe['routeText']!r}")
    m = probe["model"]
    if m is None:
        bad.append("window.__voiceTunnel.pills is absent — there is no model to check the DOM "
                   "against")
    else:
        if m["show"]["spk"]:
            bad.append("the model itself says the output pill should show")
        if m["outputs"] != 1:
            bad.append(f"the Android shape counted as {m['outputs']} distinct outputs, not 1")
        for pill in ("dev", "mic", "spk"):
            if m["show"][pill] != probe["dom"][pill]:
                bad.append(f"MODEL/DOM DISAGREEMENT on {pill}: model says "
                           f"{'show' if m['show'][pill] else 'hide'}, the DOM says "
                           f"{'shown' if probe['dom'][pill] else 'hidden'}")
        if m["route"] is not None:
            bad.append(f"the model carries a route it cannot know: {m['route']!r}")
    return bad


ANDROID_INS = [dev("default", "audioinput", "", "")]
ANDROID_OUTS = [dev("default", "audiooutput", "", "")]

SWEEP_CASES = [{"id": cid, "inputs": ins, "outputs": outs,
                "sinkSupported": bool(sink and outs), "liveSink": None}
               for cid, ins, outs, sink in CASES]


def sweep_violations(page):
    """Every combination the spec names, through the page's OWN pure function.

    Returns (views, violations). Run against the real page it must come back empty; run against
    the inverted-rule build it must not, which is what makes the empty list a measurement.
    """
    views = page.evaluate("(cs) => cs.map(c => window.__voiceTunnel.pillsView(c))", SWEEP_CASES)
    bad = []
    for c, v in zip(SWEEP_CASES, views):
        if v["show"]["spk"] and v["outputs"] < 2:
            bad.append(f"{c['id']}: output pill with {v['outputs']} choice(s)")
        if v["show"]["mic"] and v["inputs"] < 2:
            bad.append(f"{c['id']}: input pill with {v['inputs']} choice(s)")
        if v["show"]["dev"] and (v["inputs"] < 2 or v["outputs"] < 2):
            bad.append(f"{c['id']}: device pill with {v['inputs']}in/{v['outputs']}out")
    return views, bad


# ------------------------------------------------------------------ the broken copies

RULE_LITERAL = "show.spk = sinkSupported && outputs > 1;"
RULE_INVERTED = "show.spk = sinkSupported && outputs > 0;"

DESYNC_SCRIPT = """
<script>
/* DELIBERATELY BROKEN BUILD — the DOM painted behind the model's back. This is the orb defect's
   exact shape: the model is right, one more writer paints the element anyway, and a check that
   only reads the model reports everything fine. */
setInterval(() => {
  const el = document.getElementById('spkpick');
  if (el) { el.hidden = false; el.style.display = ''; }
}, 5);
</script>
"""

STALE_ROUTE_SCRIPT = """
<script>
/* DELIBERATELY BROKEN BUILD — the exact bug that started this section, restored. A route readout
   painted from a memory rather than from the live sink: confident, specific and wrong.
   Without this mutant, "the page shows no route text" and "the harness never looked for route
   text" are the same green line. */
setInterval(() => {
  const el = document.getElementById('route');
  if (el) { el.textContent = 'Bluetooth (WH-1000XM4)'; el.hidden = false; }
}, 5);
</script>
"""


def build_mutants():
    """Broken copies of the page in a temp dir. The real `index.html` is never touched."""
    src = pathlib.Path(PAGE).read_text(encoding="utf-8")
    out = {}

    tmp = tempfile.mkdtemp(prefix="devicepills-mutant-")

    # The rule inverted at source: model and DOM broken together, agreeing with each other.
    if RULE_LITERAL not in src:
        raise AssertionError(
            "the source mutation target is gone from index.html — expected the literal\n"
            f"    {RULE_LITERAL}\n"
            "in pillsView(). The negative control cannot invert a rule it cannot find, and a "
            "negative control that quietly stops mutating is exactly the failure mode it exists "
            "to catch. Update RULE_LITERAL to the current spelling.")
    rule_dir = os.path.join(tmp, "rule")
    os.makedirs(rule_dir)
    pathlib.Path(rule_dir, "index.html").write_text(
        src.replace(RULE_LITERAL, RULE_INVERTED, 1), encoding="utf-8")
    out["rule"] = rule_dir

    # The DOM lying about a correct model.
    assert "</body>" in src, "the page has no </body> to inject a broken build into"
    for name, injected in (("desync", DESYNC_SCRIPT), ("stale-route", STALE_ROUTE_SCRIPT)):
        d = os.path.join(tmp, name)
        os.makedirs(d)
        pathlib.Path(d, "index.html").write_text(
            src.replace("</body>", injected + "</body>", 1), encoding="utf-8")
        out[name] = d

    return out


# ------------------------------------------------------------------ run

watch = ServerWatch()
httpd, port = serve(WEB)
mutants = build_mutants()
mutant_servers = {name: serve(d) for name, d in mutants.items()}

try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])

        # ============================================================ the pure rule, in the page
        print("--- the page's own rule, swept (AC6) ------------------------------------------")
        ctx, page = open_page(browser, port)
        probe0 = page.evaluate("() => window.__vtProbe()")
        if not check(probe0["hasPureFn"],
                     "the page exposes its visibility rule as a pure function (pillsView)",
                     "without it the sweep can only drive real transitions"):
            note("window.__voiceTunnel.pillsView is absent; the source sweep below is skipped.")

        if probe0["hasPureFn"]:
            views, viol = sweep_violations(page)
            check(len(views) == len(CASES) == 64,
                  "every combination produced a view", f"{len(views)} cases")

            # THE INVARIANT, on the page's own model. Stated in terms of the counts the page
            # itself reports, so it holds however the counting is implemented.
            check(not viol, "NO PILL IS VISIBLE WITH FEWER THAN TWO CHOICES, in all 64",
                  "" if not viol else f"{len(viol)} violations, first: {viol[0]}")

            layout = [c["id"] for c, v in zip(SWEEP_CASES, views)
                      if v["show"]["dev"] and (v["show"]["mic"] or v["show"]["spk"])]
            check(not layout, "the grouped layout never shares the screen with the fallback pair",
                  "" if not layout else f"{len(layout)} cases, first: {layout[0]}")

            nosink = [c["id"] for c, v in zip(SWEEP_CASES, views)
                      if not c["sinkSupported"] and (v["show"]["spk"] or v["show"]["dev"])]
            check(not nosink, "a platform that cannot route output is offered no routing control",
                  "" if not nosink else f"first: {nosink[0]}")

            # And the page's rule against the reference model in tests/test_device_pills.py.
            # THIS IS WHAT THE PORT COSTS, paid off: a reference model that drifted from the page
            # would make the pytest sweep green and meaningless, and this line is where that shows.
            drift = []
            for (cid, ins, outs, sink), v in zip(CASES, views):
                ref = pill_model(ins, outs, sink)
                for field in ("inputs", "outputs", "grouped", "show", "route"):
                    if v[field] != ref[field]:
                        drift.append(f"{cid} .{field}: page={v[field]!r} reference={ref[field]!r}")
            check(not drift,
                  "the page's rule agrees with the reference model in tests/test_device_pills.py",
                  "" if not drift else f"{len(drift)} disagreements, first: {drift[0]}")

            unreasoned = [c["id"] for c, v in zip(SWEEP_CASES, views)
                          if any((v["why"][p] is None) == (not v["show"][p])
                                 for p in ("dev", "mic", "spk"))]
            check(not unreasoned, "every hidden pill records why, and no visible pill does",
                  "" if not unreasoned else f"first: {unreasoned[0]}")

        # ============================================================ real transitions
        # The same 64 fixtures, driven through the real `devicechange` path, asserting the DOM
        # against the model each time. The sweep above proves the RULE; this proves it is WIRED.
        print()
        print("--- the same 64 shapes driven through the real page (FR4/FR5) -----------------")
        mismatches, invariant, missing_model = [], [], []
        for want_sink in (True, False):
            sctx, spage = open_page(browser, port, set_sink_id=want_sink)
            for cid, ins, outs, sink in CASES:
                if sink != want_sink:
                    continue
                p = apply_devices(spage, ins, outs)
                m = p["model"]
                if m is None:
                    missing_model.append(cid)
                    continue
                for pill in ("dev", "mic", "spk"):
                    if m["show"][pill] != p["dom"][pill]:
                        mismatches.append(
                            f"{cid} {pill}: model={'show' if m['show'][pill] else 'hide'} "
                            f"dom={'shown' if p['dom'][pill] else 'hidden'}")
                if m["show"]["spk"] and m["outputs"] < 2:
                    invariant.append(f"{cid}: spk shown with {m['outputs']}")
                if m["show"]["mic"] and m["inputs"] < 2:
                    invariant.append(f"{cid}: mic shown with {m['inputs']}")
                if p["dom"]["dev"] and (p["dom"]["mic"] or p["dom"]["spk"]):
                    invariant.append(f"{cid}: two layouts on screen at once")
                if bool(p["routeText"]) != bool(m["route"]):
                    mismatches.append(f"{cid} route: model={m['route']!r} dom={p['routeText']!r}")
            sctx.close()
        check(not missing_model, "every real transition published a model",
              "" if not missing_model else f"{len(missing_model)} without one")
        check(not mismatches,
              "THE DOM AGREES WITH THE MODEL IN ALL 64 SHAPES, driven through devicechange",
              "" if not mismatches else f"{len(mismatches)} disagreements, first: {mismatches[0]}")
        check(not invariant, "and the invariant holds on the rendered page, not only in the model",
              "" if not invariant else f"first: {invariant[0]}")

        # ============================================================ named criteria
        print()
        print("--- AC7 / AC11: the Android shape ---------------------------------------------")
        p = apply_devices(page, ANDROID_INS, ANDROID_OUTS)
        bad = android_shape_failures(p)
        check(not bad, "AC7+AC11 the real page renders NO output pill and NO route text on the "
                       "Android shape", "" if not bad else " | ".join(bad))
        if VERBOSE:
            note(f"model={p['model']}")

        print()
        print("--- AC8: two distinct outputs -------------------------------------------------")
        two_outs = [dev("default", "audiooutput", "Speakers (Realtek)", "ga"),
                    dev("hs-1", "audiooutput", "Headset (WH-1000XM4)", "gb")]
        p = apply_devices(page, [], two_outs)
        opts = p["opts"]["spk"] or []
        check(p["dom"]["spk"], "AC8 the output pill is on screen with two distinct outputs",
              f"model.outputs={p['model'] and p['model']['outputs']}")
        check(len(opts) == 2, "AC8 and it offers exactly two options",
              f"options={opts}")
        check(not p["routeText"],
              "AC8 no route text beside a picker that already names the route",
              f"routeText={p['routeText']!r}")

        print()
        print("--- AC9: two outputs become one, with no reload -------------------------------")
        stamp_before = p["loadStamp"]
        p2 = apply_devices(page, [], [dev("default", "audiooutput", "Speakers (Realtek)", "ga")])
        check(not p2["dom"]["spk"],
              "AC9 the pill is REMOVED when the second device goes away",
              f"model={p2['model'] and p2['model']['show']}")
        check(p2["loadStamp"] == stamp_before,
              "AC9 and the page was never reloaded to do it",
              f"loadStamp {stamp_before} -> {p2['loadStamp']}")
        p3 = apply_devices(page, [], two_outs)
        check(p3["dom"]["spk"], "AC9 and it comes back when the device returns (FR5/TC2)")
        ctx.close()

        # ============================================================ the route readout
        print()
        print("--- AC12: one labelled output, live sink matching it ---------------------------")
        rctx, rpage = open_page(browser, port)
        headset = [dev("default", "audiooutput", "Headset (WH-1000XM4)", "g1")]
        headset_in = [dev("default", "audioinput", "Headset Mic (WH-1000XM4)", "g1")]
        apply_devices(rpage, headset_in, headset)
        rpage.click("#orb")
        rpage.wait_for_function(
            "() => ['Warming up','Listening','Off','No mic'].includes("
            "document.getElementById('orblabel').textContent)", timeout=25000)
        p = apply_devices(rpage, headset_in, headset)
        # THE PRECONDITION, checked rather than assumed: if there were no live AudioContext, the
        # page would report the sink as unknowable and AC12 would "pass" by saying nothing.
        live_ok = check(p["sink"] and p["sink"]["inUse"] == "",
                        "AC12 precondition: a LIVE AudioContext is reporting its sink back",
                        f"sink={p['sink']}")
        check(p["model"] and p["model"]["route"] == "Headset (WH-1000XM4)",
              "AC12 the model names the device the audio is going to",
              f"route={p['model'] and p['model']['route']!r}")
        check("Headset (WH-1000XM4)" in p["routeText"],
              "AC12 and the DOM says so too", f"routeText={p['routeText']!r}")
        check(not p["dom"]["spk"] and not p["dom"]["dev"],
              "AC12 with no picker on screen — one device is not a choice")
        if not live_ok:
            note("the route assertions above are unsound without a live context; treat as unmet.")

        # AC13, the negative control. Stored preference names device A; the live sink reports B.
        print()
        print("--- AC13: a stored preference must never become the route text -----------------")
        dctx, dpage = open_page(browser, port,
                                storage={"voice-tunnel.sinkId": "spk-A"})
        # devA's LABEL is reachable — it is enumerated, as an input — so a page that resolved the
        # remembered id against the enumeration could produce it. Without that the control would
        # be trivially unfailable: the page could not name A even if it wanted to.
        a_and_b = [dev("spk-B", "audiooutput", "Device B Speakers", "gb")]
        a_input = [dev("spk-A", "audioinput", "Device A Speakers", "ga")]
        apply_devices(dpage, a_input, a_and_b)
        dpage.click("#orb")
        dpage.wait_for_function(
            "() => ['Warming up','Listening','Off','No mic'].includes("
            "document.getElementById('orblabel').textContent)", timeout=25000)
        # Pin the live sink to B and leave the stored preference pointing at A: `applySink` will
        # keep asking for A and keep not getting it, which is the 2026-08-15 report reproduced.
        dpage.evaluate("() => { window.__fakeSink = 'spk-B'; window.__sinkPinned = true; }")
        p = apply_devices(dpage, a_input, a_and_b)
        check(p["sink"] and p["sink"]["inUse"] == "spk-B" and p["sink"]["wanted"] == "spk-A",
              "AC13 precondition: the page WANTS device A and the live sink reports device B",
              f"wanted={p['sink'] and p['sink']['wanted']!r} "
              f"inUse={p['sink'] and p['sink']['inUse']!r}")
        said = (p["model"] or {}).get("route")
        check("Device A" not in (p["routeText"] or "") and "spk-A" not in (p["routeText"] or ""),
              "AC13 the route text never names the REMEMBERED device",
              f"routeText={p['routeText']!r}")
        check(said in (None, "Device B Speakers"),
              "AC13 it names the live device or says nothing — never the request",
              f"model.route={said!r}")
        dctx.close()
        rctx.close()

        # ============================================================ AC14
        print()
        print("--- AC14: the negative control — this harness has been SEEN to fail ------------")
        for name, (_h, mport) in mutant_servers.items():
            mctx, mpage = open_page(browser, mport)
            mp = apply_devices(mpage, ANDROID_INS, ANDROID_OUTS)
            bad = android_shape_failures(mp)
            check(bool(bad),
                  f"AC14 the broken build '{name}' is REPORTED by the same assertions",
                  f"{len(bad)} failure(s): " + " | ".join(bad) if bad else
                  "THE HARNESS PASSED A DELIBERATELY BROKEN PAGE — every green above is worthless")
            if bad and VERBOSE:
                note(f"[{name}] " + " | ".join(bad))
            # AND THE SWEEP ITSELF, against the build whose rule is wrong. AC6's value is that
            # "we handled the cases" becomes a measurement, and a measurement that cannot come
            # back negative measures nothing. Only the source mutant breaks the rule; the injected
            # ones leave `pillsView` correct and lie downstream of it, which is the point of having
            # both.
            if name == "rule":
                _views, sweep_bad = sweep_violations(mpage)
                check(bool(sweep_bad),
                      "AC14 the 64-case SWEEP also reports the inverted rule",
                      f"{len(sweep_bad)} violations, first: {sweep_bad[0]}" if sweep_bad else
                      "THE SWEEP PASSED AN INVERTED RULE — AC6 is measuring nothing")
            mctx.close()

        browser.close()
finally:
    httpd.shutdown()
    for _h, _p in mutant_servers.values():
        _h.shutdown()

watch.verify()

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
