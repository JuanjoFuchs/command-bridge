"""A control with nothing to choose is not rendered — swept, and kept single-writer.

WHY THIS FILE EXISTS. Reported live 2026-08-18, on a headset, with the speaker list offering only
`default`: *"It seems we need to be smarter than that, right? If there is no speaker to choose,
why would we show a drop-down to choose a speaker?"*

The page already had a rule meant to withdraw that control — `$spkpick.hidden = !sinkSupported ||
grouped` — and the reason it never fired is the interesting part. `sinkSupported` is decided by
feature-detecting `setSinkId` (present on Android Chrome, which cannot honour it) or by catching a
`NotSupportedError` at call time; and the call site returns early when the wanted sink already
equals the current one, which at page load is `"" === ""`. **Nothing calls `setSinkId`, so nothing
throws, so the picker is never withdrawn.** The only thing that could prove the control dead is
structurally unreachable on the default path.

So the fix is not to detect harder. It is to COUNT: a control offering one option is observably
useless without needing the platform to admit anything.

Counting is where this gets subtle, and it is the reason for the sweep below rather than four
hand-picked cases. **Chrome lists one physical device up to three times** — once for real and once
each as the `default` and `communications` pseudo-devices, all sharing a `groupId`. A rule that
counts rows leaves the dead control on every single-output desktop; a rule that counts devices does
not. Two entries that are the same device said twice are one choice.

THREE THINGS ARE HELD HERE, and each is a different way this goes wrong:

1. **The rule** (`pill_model`), swept over every combination of {0, 1 alias-collapsed, 1 multi-row,
   2+} inputs x the same outputs x {setSinkId present, absent} x {paired, unpaired}. In every one
   of the 64, no pill may be visible with fewer than two choices, and the grouped layout may never
   share the screen with the single-purpose pair. The orb's defect was found by sweeping 384
   combinations of a pure model; this is the same instrument.

2. **The single writer** (`test_exactly_one_function_assigns_pill_visibility`). This is the
   `orbView` lesson applied to the same page: the orb read "Thinking" with no clock because four
   handlers painted it independently and nothing in the suite could see that. A rule that lives in
   three assignments cannot be swept, and a rule that cannot be swept will be tested on one path
   and broken on another. This assertion is what keeps FR4 true after the spec closes.

3. **The route readout never reads a request** (`test_the_route_readout_never_consults_...`). A
   remembered preference is right until the moment it matters and then stays confidently wrong.

WHAT THIS FILE DOES NOT PROVE, stated plainly because the gap is the point of the sibling harness:
there is no JavaScript runtime in this suite, so `pill_model` below is a REFERENCE MODEL — a
port of the rule, not the page's own code — and the structural assertions read source text.
Neither can tell you the DOM was painted from the model. `scripts/devicepills.py` is what closes
that: it drives the real page in a real Chromium over a plain static server, sweeps these exact
fixtures through `refreshDevices`, and fails if the page's model disagrees with this reference OR
if the DOM disagrees with the page's model. The cost of the port is named there too.
"""
import itertools
import pathlib
import re

import pytest

PAGE = pathlib.Path(__file__).resolve().parents[1] / "command_bridge" / "web" / "index.html"

# ---------------------------------------------------------------------------------------------
# The reference model.
#
# Deliberately a PORT rather than an import of the page's function, and the tradeoff is real: a
# port can drift from the code it mirrors, and a green sweep over a stale port proves nothing about
# the page. That cost is paid off in `scripts/devicepills.py`, which runs these same fixtures
# through the real page and asserts the page's own model equals this one — so drift fails loudly
# in the harness instead of hiding here. What the port buys is a sweep that needs no browser, runs
# in milliseconds, and can be read as a statement of the rule rather than as a scrape of it.
# ---------------------------------------------------------------------------------------------

# Chrome's pseudo-devices. The page carries this same set as `ALIAS_IDS`.
ALIAS_IDS = frozenset({"default", "communications"})


def dev(device_id: str, kind: str, label: str = "", group_id: str = "") -> dict:
    """One `MediaDeviceInfo` as `enumerateDevices()` returns it."""
    return {"deviceId": device_id, "kind": kind, "label": label, "groupId": group_id}


def norm_sink(v) -> str:
    """`""` and `"default"` are one device said two ways — the page's `normSink`."""
    return "" if (not v or v == "default") else v


def clean_label(s) -> str:
    """"Default - Headset (WH-1000XM4)" is Chrome labelling the ALIAS, not the device."""
    return re.sub(r"^(Default|Communications)\s+-\s+", "", s or "", flags=re.I).strip()


def distinct_devices(rows) -> int:
    """How many CHOICES a list of enumerated rows actually offers.

    An alias is a POINTER, not a device: `default` names whichever real device the OS currently
    points at. So it folds into that device when the platform says which — a shared `groupId`, or
    the label Chrome copied onto it — and counts on its own only when it is all there is. That last
    case is the Android shape exactly, and it has to count as one.
    """
    keys = set()
    real = [d for d in rows if d.get("deviceId") not in ALIAS_IDS]
    aliases = [d for d in rows if d.get("deviceId") in ALIAS_IDS]
    for d in real:
        keys.add("g:" + d["groupId"] if d.get("groupId") else "d:" + d["deviceId"])
    for d in aliases:
        if d.get("groupId") and "g:" + d["groupId"] in keys:
            continue
        label = clean_label(d.get("label"))
        if label and any(clean_label(r.get("label")) == label for r in real):
            continue
        keys.add("g:" + d["groupId"] if d.get("groupId") else "l:" + (label or "default"))
    return len(keys)


def pair_devices(ins, outs) -> dict:
    """groupId -> {in, out} for the groups with BOTH halves — mirrors the page's `pairDevices`."""
    groups: dict = {}
    for kind, rows in (("in", ins), ("out", outs)):
        for d in rows:
            gid = d.get("groupId") or ""
            if not gid:
                continue
            g = groups.setdefault(gid, {"in": None, "out": None})
            better = g[kind] is None or (g[kind] in ALIAS_IDS and d["deviceId"] not in ALIAS_IDS)
            if better:
                g[kind] = d["deviceId"]
    return {k: v for k, v in groups.items() if v["in"] is not None and v["out"] is not None}


def pill_model(ins, outs, set_sink_id: bool = True, live_sink=None) -> dict:
    """WHICH PILLS ARE ON SCREEN, as a pure function of the enumeration facts.

    Shaped to the contract the page exposes on `window.__voiceTunnel.pills`, so the harness can
    compare the two field by field.
    """
    n_in, n_out = distinct_devices(ins), distinct_devices(outs)
    # The page's own capability ladder, minus the `sinkRefused` stickiness, which is a session fact
    # rather than an enumeration fact and therefore cannot be swept from here.
    sink_supported = bool(set_sink_id and outs)

    groups = pair_devices(ins, outs)
    covered = all((d.get("groupId") or "") in groups for d in list(ins) + list(outs))
    grouped = bool(sink_supported and ins and outs and covered)

    # ONE PILL PER REAL CHOICE. The grouped pill is a picker over DEVICES, so the same rule binds
    # it: one paired headset is one object, and offering a menu of one is the defect with a tidier
    # layout. When it goes, the single-purpose pair does not come back to fill the hole — each of
    # them is then a menu of one too.
    show_dev = grouped and len(groups) >= 2
    show_mic = (not show_dev) and n_in >= 2
    show_spk = (not show_dev) and sink_supported and n_out >= 2

    def why_side(show: bool, n: int, supported: bool = True) -> str | None:
        if show:
            return None
        if not supported:
            return "output not selectable on this platform"
        return "nothing enumerated" if n == 0 else "only one device"

    # FR3 — WHERE THE AUDIO IS GOING, said only where the page can genuinely know AND where no
    # picker already says it. `live_sink` is the sink read back off a LIVE AudioContext; `None`
    # means unknowable, and unknowable renders nothing. A remembered preference never reaches here.
    route = None
    if not show_dev and not show_spk and live_sink is not None:
        have = norm_sink(live_sink)
        hit = next((d for d in outs
                    if norm_sink(d.get("deviceId")) == have and clean_label(d.get("label"))), None)
        route = clean_label(hit["label"]) if hit else None

    return {
        "inputs": n_in,
        "outputs": n_out,
        "grouped": grouped,
        "show": {"dev": show_dev, "mic": show_mic, "spk": show_spk},
        "why": {
            "dev": None if show_dev else (
                "only one device" if grouped else "devices cannot be paired"),
            "mic": None if show_mic else (
                "the grouped pill covers both halves" if grouped else why_side(False, n_in)),
            "spk": None if show_spk else (
                "the grouped pill covers both halves" if grouped
                else why_side(False, n_out, sink_supported)),
        },
        "route": route,
    }


# ---------------------------------------------------------------------------------------------
# The enumeration fixtures the sweep and the browser harness BOTH use.
#
# Shared on purpose: a browser harness driving different shapes from the ones swept here would let
# a case pass in the model and never be seen in the page, which is exactly the gap this pair of
# instruments exists to close.
# ---------------------------------------------------------------------------------------------

SHAPES = ("none", "android", "three_rows", "two")


def side(shape: str, kind: str, groups: tuple) -> list:
    """One side's enumeration. `groups` supplies the two group ids this side may use."""
    ga, gb = groups
    if shape == "none":
        return []
    if shape == "android":
        # THE ANDROID SHAPE: one row, the `default` alias, no label and no groupId. One choice.
        return [dev("default", kind, "", "")]
    if shape == "three_rows":
        # ONE DESKTOP DEVICE SAID THREE TIMES. This is the case a row count gets wrong, and it is
        # not hypothetical — it is every single-sound-card desktop.
        return [dev("default", kind, f"Default - {ga} device", ga),
                dev("communications", kind, f"Communications - {ga} device", ga),
                dev(f"real-{ga}-{kind}", kind, f"{ga} device", ga)]
    if shape == "two":
        return [dev(f"real-{ga}-{kind}", kind, f"{ga} device", ga),
                dev(f"real-{gb}-{kind}", kind, f"{gb} device", gb)]
    raise ValueError(shape)


def enumeration(in_shape: str, out_shape: str, paired: bool) -> tuple:
    """(inputs, outputs). Paired means both sides draw from the SAME group ids, so a group can
    have both halves; unpaired gives each side its own namespace, so none can."""
    in_groups = ("ga", "gb") if paired else ("ia", "ib")
    out_groups = ("ga", "gb") if paired else ("oa", "ob")
    return (side(in_shape, "audioinput", in_groups),
            side(out_shape, "audiooutput", out_groups))


def sweep_cases() -> list:
    """Every combination the spec names, as (id, inputs, outputs, set_sink_id)."""
    cases = []
    for in_shape, out_shape, sink, paired in itertools.product(
            SHAPES, SHAPES, (True, False), (True, False)):
        ins, outs = enumeration(in_shape, out_shape, paired)
        case_id = (f"in={in_shape} out={out_shape} "
                   f"sinkId={'yes' if sink else 'no'} {'paired' if paired else 'unpaired'}")
        cases.append((case_id, ins, outs, sink))
    return cases


CASES = sweep_cases()
IDS = [c[0] for c in CASES]


# ---------------------------------------------------------------------------------------------
# AC1-AC5 — the rule itself, named case by case so a failure says which one broke.
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,pill", [("audiooutput", "spk"), ("audioinput", "mic")])
def test_zero_devices_hides_the_pill(kind, pill):
    """AC1/AC5. Nothing enumerated is not a choice of one; it is no choice at all."""
    rows = []
    m = (pill_model([], rows) if kind == "audiooutput" else pill_model(rows, []))
    assert m["show"][pill] is False


@pytest.mark.parametrize("kind,pill", [("audiooutput", "spk"), ("audioinput", "mic")])
def test_the_android_shape_hides_the_pill(kind, pill):
    """AC2/AC5. One unlabelled `default` — the shape he actually gets — is one choice."""
    rows = [dev("default", kind, "", "")]
    m = pill_model([], rows) if kind == "audiooutput" else pill_model(rows, [])
    assert m["show"][pill] is False, (
        "the picker survives the Android shape, which is the exact control he asked about")


@pytest.mark.parametrize("kind,pill", [("audiooutput", "spk"), ("audioinput", "mic")])
def test_one_device_shown_as_chromes_three_rows_hides_the_pill(kind, pill):
    """AC3/AC5. THE CASE A ROW COUNT GETS WRONG, and it is a desktop case.

    `enumerateDevices` returns `default`, `communications` and the real entry, all sharing one
    `groupId`. `outs.length > 1` reads that as two choices and leaves the dead control on every
    one-sound-card desktop.
    """
    rows = side("three_rows", kind, ("ga", "gb"))
    assert len(rows) == 3, "the fixture stopped being the three-row case"
    m = pill_model([], rows) if kind == "audiooutput" else pill_model(rows, [])
    assert m["outputs" if kind == "audiooutput" else "inputs"] == 1, (
        "three rows of one device now count as more than one choice — the alias collapse is gone")
    assert m["show"][pill] is False


@pytest.mark.parametrize("kind,pill", [("audiooutput", "spk"), ("audioinput", "mic")])
def test_two_distinct_devices_show_the_pill(kind, pill):
    """AC4/AC5. The desktop case NFR1 protects: a real choice keeps its control."""
    rows = side("two", kind, ("ga", "gb"))
    m = pill_model([], rows) if kind == "audiooutput" else pill_model(rows, [])
    assert m["show"][pill] is True, "a genuine choice lost its control; that is the regression NFR1 names"


# ---------------------------------------------------------------------------------------------
# AC6 — the exhaustive sweep.
# ---------------------------------------------------------------------------------------------

def test_the_sweep_covers_every_combination_the_spec_names():
    """The instrument before what it measures: 4 input shapes x 4 output shapes x setSinkId x
    pairing. If this number moves, an axis was dropped and every green below means less."""
    assert len(CASES) == 4 * 4 * 2 * 2 == 64
    assert len(set(IDS)) == 64, "two cases collided, so fewer combinations run than are reported"


@pytest.mark.parametrize("case_id,ins,outs,sink", CASES, ids=IDS)
def test_no_pill_is_visible_with_fewer_than_two_choices(case_id, ins, outs, sink):
    """AC6, THE INVARIANT. Stated against the model's own counts, so it stays true however the
    counting is implemented."""
    m = pill_model(ins, outs, sink)
    if m["show"]["spk"]:
        assert m["outputs"] >= 2, f"{case_id}: an output picker with {m['outputs']} choice(s)"
    if m["show"]["mic"]:
        assert m["inputs"] >= 2, f"{case_id}: an input picker with {m['inputs']} choice(s)"
    if m["show"]["dev"]:
        assert m["inputs"] >= 2 and m["outputs"] >= 2, (
            f"{case_id}: a device picker with {m['inputs']} input(s) and {m['outputs']} output(s)")


@pytest.mark.parametrize("case_id,ins,outs,sink", CASES, ids=IDS)
def test_exactly_one_layout_is_on_screen(case_id, ins, outs, sink):
    """AC6, the other half. The grouped pill and the single-purpose pair are two LAYOUTS, not three
    controls; mixing them was already forbidden, and "no pills at all" is the layout this spec
    adds rather than a fourth state to fall between them."""
    m = pill_model(ins, outs, sink)
    if m["show"]["dev"]:
        assert not m["show"]["mic"] and not m["show"]["spk"], (
            f"{case_id}: the grouped pill is sharing the screen with the fallback pair")


@pytest.mark.parametrize("case_id,ins,outs,sink", CASES, ids=IDS)
def test_every_hidden_pill_says_why(case_id, ins, outs, sink):
    """A hidden control with no recorded reason is the state that cost this spec its diagnosis:
    the page hid nothing and nobody could tell whether the rule had run."""
    m = pill_model(ins, outs, sink)
    for pill in ("dev", "mic", "spk"):
        if m["show"][pill]:
            assert m["why"][pill] is None, f"{case_id}: {pill} is visible and still carries a reason"
        else:
            assert m["why"][pill], f"{case_id}: {pill} is hidden for no recorded reason"


@pytest.mark.parametrize("case_id,ins,outs,sink", CASES, ids=IDS)
def test_the_platform_that_cannot_route_never_offers_an_output_control(case_id, ins, outs, sink):
    """TC1 as an invariant. Without `setSinkId` the page cannot move audio at all, so an output
    picker is a claim about a capability that is genuinely absent."""
    m = pill_model(ins, outs, sink)
    if not sink:
        assert not m["show"]["spk"] and not m["show"]["dev"], (
            f"{case_id}: a routing control on a platform with no setSinkId")


def test_a_single_paired_headset_shows_no_pill_at_all():
    """The case worth calling out, because it looks like a regression and is the spec working:
    one Bluetooth headset is one input, one output and one group. Nothing to choose on any axis."""
    ins, outs = enumeration("three_rows", "three_rows", paired=True)
    m = pill_model(ins, outs, True)
    assert m["grouped"] is True, "the fixture stopped being a paired device"
    assert m["show"] == {"dev": False, "mic": False, "spk": False}


def test_two_paired_headsets_show_the_grouped_pill_and_nothing_else():
    """NFR2: the paired layout is untouched where pairing still buys something."""
    ins, outs = enumeration("two", "two", paired=True)
    m = pill_model(ins, outs, True)
    assert m["show"] == {"dev": True, "mic": False, "spk": False}


# ---------------------------------------------------------------------------------------------
# Source-structural assertions. Everything below reads the page as text.
#
# The helpers are a deliberate copy of `tests/test_audio_route.py`'s rather than an import of them.
# Cheap duplication, bought for one reason: these two files are edited by different hands for
# different specs, and a shared extractor makes one file's refactor look like the other file's
# rule breaking. `test_the_extractor_is_reading_balanced_code` is the tripwire that says which.
# ---------------------------------------------------------------------------------------------

def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def script() -> str:
    """Only the `<script>` block — the blanker understands JavaScript quoting and nothing else,
    and the HTML comments above it are full of prose apostrophes it would read as strings."""
    raw = page()
    start = raw.index("<script>") + len("<script>")
    return raw[start:raw.index("</script>", start)]


def blanked(js: str) -> str:
    """Comments and string bodies replaced with spaces, so a search sees only code.

    Both have to go, and in the SAME pass: the comments in this page name every function they
    discuss, and the socket URL is a template literal containing `//`, which a comment-first
    stripper reads as a line comment and eats the rest of the line — braces included.
    """
    out = list(js)
    i, n = 0, len(js)

    def blank(lo: int, hi: int) -> None:
        for k in range(lo, min(hi, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = js[i]
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            end = js.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif c == "/" and i + 1 < n and js[i + 1] == "/":
            end = js.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif c in "\"'`":
            j = i + 1
            while j < n:
                if js[j] == "\\":
                    j += 2
                    continue
                if js[j] == c:
                    break
                j += 1
            blank(i + 1, j)
            i = min(j, n) + 1
        else:
            i += 1
    return "".join(out)


# `function name(`, `const name = () => {`, `const name = function (`, `const name = async ...`.
# All three spellings, because the assertion below is about WHICH function owns a write, and a
# rule that only recognises one spelling can be defeated by renaming a declaration.
_DECL = re.compile(
    r"(?:(?:async\s+)?function\s+(?P<fn>\w+)\s*\()"
    r"|(?:\b(?:const|let|var)\s+(?P<cn>\w+)\s*=\s*(?:async\s+)?"
    r"(?:function\s*\*?\s*\w*\s*\(|\([^()]*\)\s*=>|\w+\s*=>))")


def _body_span(code: str, open_from: int):
    """Brace-matched body, IF the very next thing is a brace.

    The "if" is load-bearing and was a real bug in this extractor's first draft. An
    expression-bodied arrow — `const liveSink = () => (ctx && …) ? normSink(ctx.sinkId) : null;` —
    has no block at all, and scanning forward for the next `{` handed back some LATER function's
    body. Every write inside that unrelated function was then attributed to `liveSink`, which is a
    single-writer assertion answering from the wrong scope while looking perfectly green.
    """
    start = open_from
    while start < len(code) and code[start] in " \t\r\n":
        start += 1
    if start >= len(code) or code[start] != "{":
        return None
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
    return None


def function_spans(code: str) -> list:
    """(name, lo, hi) for every named function body in the script, in source order."""
    spans = []
    for m in _DECL.finditer(code):
        name = m.group("fn") or m.group("cn")
        # For `function f(`, skip the parameter list first: a destructured or defaulted parameter
        # puts braces inside the parens and would be mistaken for the body.
        i = m.end()
        if m.group("fn"):
            depth = 1
            while i < len(code) and depth:
                if code[i] == "(":
                    depth += 1
                elif code[i] == ")":
                    depth -= 1
                i += 1
        span = _body_span(code, i)
        if span:
            spans.append((name, span[0], span[1]))
    return spans


def enclosing_function(spans: list, offset: int) -> str:
    """The INNERMOST named function containing `offset`, or 'top level'."""
    best, best_size = "top level", None
    for name, lo, hi in spans:
        if lo <= offset < hi and (best_size is None or hi - lo < best_size):
            best, best_size = name, hi - lo
    return best


# Every way a pill's visibility can be written. `hidden` is the one the page uses; the others are
# here so a future change cannot route around this assertion by picking a different mechanism.
PILL = r"\$(?:dev|mic|spk)pick"
VISIBILITY_WRITES = [
    (rf"{PILL}\s*\.\s*hidden\s*=", "hidden ="),
    (rf"{PILL}\s*\.\s*style\s*\.\s*display\s*=", "style.display ="),
    (rf"{PILL}\s*\.\s*style\s*\.\s*visibility\s*=", "style.visibility ="),
    (rf"{PILL}\s*\.\s*classList\s*\.\s*(?:add|remove|toggle)\s*\(", "classList"),
    (rf"{PILL}\s*\.\s*(?:set|remove|toggle)Attribute\s*\(", "setAttribute"),
    (rf"{PILL}\s*\.\s*remove\s*\(\s*\)", ".remove()"),
    (rf"{PILL}\s*\.\s*replaceWith\s*\(", ".replaceWith()"),
]


def visibility_writers() -> list:
    """(function name, mechanism, line) for every write to a pill's visibility."""
    code = blanked(script())
    spans = function_spans(code)
    found = []
    for pattern, mechanism in VISIBILITY_WRITES:
        for m in re.finditer(pattern, code):
            found.append((enclosing_function(spans, m.start()), mechanism,
                          code[:m.start()].count("\n") + 1))
    return sorted(found, key=lambda t: t[2])


def test_the_extractor_is_reading_balanced_code():
    """If this fails, every structural assertion below is meaningless rather than merely wrong.

    A blanker that swallows a brace does not report an error — it returns function bodies that stop
    early, and "which function owns this write" then answers from wherever the truncation landed.
    """
    code = blanked(script())
    assert code.count("{") == code.count("}"), (
        "the page script does not brace-balance after blanking, so the blanker has eaten code — "
        "look for a new quoting shape (a regex literal, a nested template) before trusting the rest")


def test_the_function_extractor_finds_the_functions_this_file_reasons_about():
    """A second tripwire, and it earns its place: a rename that the extractor misses produces
    'top level' for every write, which reads as a scattered rule when the rule is fine."""
    names = {n for n, _, _ in function_spans(blanked(script()))}
    for expected in ("refreshDevices", "pairDevices", "applySink"):
        assert expected in names, (
            f"the extractor can no longer find {expected}() — it has been renamed or respelled, "
            "and the single-writer assertion below is now reading the wrong scopes")


def test_exactly_one_function_assigns_pill_visibility():
    """AC10 / FR4 — THE ASSERTION THAT KEEPS THIS SPEC TRUE AFTER IT CLOSES.

    The orb read "Thinking" with no clock because four handlers painted it independently and
    nothing in the suite could see that. The pills had the identical shape: `paintSink`,
    `paintGroup` and `refreshDevices` each assigned `hidden`, so the rule lived in three places
    and could be correct on the path that was tested and wrong on the path that was not.

    A rule that lives in one function can be swept. That is the entire argument, and this is the
    line that stops a fourth `hidden =` being added quietly at the next call site.
    """
    writers = visibility_writers()
    assert writers, (
        "nothing assigns pill visibility any more — either the pills are gone or they are being "
        "shown and hidden by a mechanism this assertion does not recognise, which is worse")
    owners = sorted({w[0] for w in writers})
    assert len(owners) == 1, (
        "pill visibility is written from "
        + str(len(owners)) + " places: " + ", ".join(owners) + ". "
        + "Sites: " + "; ".join(f"{fn} L{ln} ({mech})" for fn, mech, ln in writers) + ". "
        "A rule spread across several writers cannot be swept, and the orb shipped a defect of "
        "exactly this shape.")
    assert owners[0] != "top level", (
        "pill visibility is assigned outside any function, so there is no pure rule to sweep")


def test_the_visibility_writer_is_not_also_the_enumeration_handler():
    """FR4 wants a PURE function of the enumeration facts. If the only writer is `refreshDevices`
    itself — the async handler that talks to the platform, awaits `applySink` and rebuilds the
    option lists — then the rule cannot be called with facts and asked what it returns, which is
    what makes a sweep possible at all."""
    writers = visibility_writers()
    assert writers, "nothing assigns pill visibility any more"
    owner = writers[0][0]
    assert owner != "refreshDevices", (
        "the only writer is refreshDevices() itself, so the visibility rule is welded to the "
        "async enumeration path and cannot be evaluated as a pure function of the facts")


def test_the_model_is_exposed_for_a_harness_to_read():
    """FR4's other half. A rule that is pure but unreachable from outside cannot be checked
    against the DOM, and 'the model is right while the DOM is wrong' is the orb bug verbatim."""
    code = blanked(script())
    assert re.search(r"\bpills\b", code), (
        "the page exposes no pill model, so `scripts/devicepills.py` can only scrape the DOM and "
        "the model-versus-DOM comparison — the whole point of FR4 — cannot run")


def route_writers() -> list:
    """(function name, line) for every place the route readout is computed or assigned.

    Located by the model field rather than by an element id, because the id is Slice A's to choose
    and this assertion is about WHERE the value comes from, not what it is painted into.
    """
    code = blanked(script())
    spans = function_spans(code)
    hits = []
    for m in re.finditer(r"\broute\s*[:=](?!=)", code):
        hits.append((enclosing_function(spans, m.start()), code[:m.start()].count("\n") + 1))
    return hits


def test_the_route_readout_never_consults_a_request_or_a_snapshot():
    """AC13, structurally. FR3: the page may say where audio is going only when it can KNOW —
    the sink read back from the LIVE `AudioContext`, matched to an enumerated device with a real
    label. `sinkWanted` is what he asked for, `localStorage` is what he asked for last time, and
    `diag.settings` is a snapshot taken at `start()` that can never report a device that moved.

    A stale label is the bug that started this whole section: the picker read "Bluetooth" while
    audio played from the earpiece. Silence is honest; a confident wrong answer is not.

    The dynamic half of AC13 — a stored preference naming device A against a live sink reporting
    device B — is in `scripts/devicepills.py`, because only a browser has a live sink.
    """
    hits = route_writers()
    assert hits, (
        "nothing computes a route readout, so FR3 is unimplemented — this test is not a pass")
    code = blanked(script())
    spans = function_spans(code)
    by_name = {name: (lo, hi) for name, lo, hi in spans}

    forbidden = ("sinkWanted", "localStorage", "diag.settings")
    for fn, line in hits:
        if fn == "top level" or fn not in by_name:
            continue
        lo, hi = by_name[fn]
        body = code[lo:hi]
        for token in forbidden:
            assert token not in body, (
                f"{fn}() computes the route readout (L{line}) and also reads `{token}` — the route "
                "can therefore name a device the audio is not going to, which is the 2026-08-15 "
                "report reproduced")


def definition_text(code: str, name: str) -> str:
    """A declaration's whole right-hand side — brace body or expression, up to the statement end.

    Needed because the interesting supplier here is an expression-bodied arrow, which has no block
    for `function_spans` to return.
    """
    m = (re.search(r"\b(?:const|let|var)\s+" + re.escape(name) + r"\s*=", code)
         or re.search(r"\b(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", code))
    if not m:
        return ""
    i, depth = m.end(), 0
    while i < len(code):
        c = code[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth < 0:
                break
        elif c == ";" and depth == 0:
            return code[m.end():i]
        i += 1
    return code[m.end():i]


def test_the_route_readout_is_derived_from_the_live_context():
    """The positive half of the same rule: a readout that consults nothing at all cannot be honest
    either. The sink has to be read back off the LIVE `AudioContext`.

    TWO HOPS, because the rule is pure and that is the point. `pillsView` cannot read `ctx` — it
    takes the sink as a FACT, which is what lets a harness sweep it. So the check follows the fact:
    the single writer of pill visibility must call something whose definition reads `ctx.sinkId`,
    and hand that to the function that computes `route`. A model fed a remembered preference would
    be just as pure and completely wrong, so purity alone proves nothing here.
    """
    hits = route_writers()
    assert hits, "nothing computes a route readout, so FR3 is unimplemented — this is not a pass"
    code = blanked(script())
    spans = function_spans(code)
    by_name = {name: (lo, hi) for name, lo, hi in spans}

    writers = visibility_writers()
    assert writers, "nothing assigns pill visibility, so the route cannot be wired to the DOM"
    owner = writers[0][0]
    assert owner in by_name, f"the single visibility writer {owner}() has no extractable body"
    lo, hi = by_name[owner]
    body = code[lo:hi]

    called = {m.group(1) for m in re.finditer(r"\b(\w+)\s*\(", body)}
    suppliers = [n for n in called if "sinkId" in definition_text(code, n)]
    assert suppliers, (
        f"{owner}() supplies the pill model from " + ", ".join(sorted(called)) + ", and none of "
        "them reads a sink back off a live AudioContext — so the route text reports something "
        "other than where audio is actually going")
