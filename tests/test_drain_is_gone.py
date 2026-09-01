"""Spec 007, FR4: the second name for the waiting command is gone, and nothing left in the repo
says otherwise.

`watch` and the retired name ran the same code under two spellings. The second spelling was not
cosmetic — it is what led an operating guide to write them up as two instruments with two waiting
strategies, and to ship a wrong rule on 2026-08-19. Removing it from the parser is the easy half.
The hard half is AC18: **no remaining occurrence of the word in the repo may tell a reader the
command exists**, in a repo whose commentary is deliberately thick with the history of how it got
here.

AC15 drives the real entry point as a subprocess. AC16 and AC17 read the live objects — the
`describe` payload, the parser, the dispatch table — rather than the source text, because those
are the surfaces an agent actually reads. AC18 is the text scan, and its rules are below.
"""
import argparse
import json
import os
import re
import subprocess
import sys

import pytest

from command_bridge import cli, config

REPO = config.ROOT
RUNNER = os.path.join(REPO, "bin", "voice-tunnel-run.py")


# --------------------------------------------------------------------------- AC15: it is unknown


def test_the_retired_name_exits_non_zero_as_an_unknown_command_and_names_watch(tmp_path):
    """AC15 (`integration`). Driven through `bin/voice-tunnel-run.py`, the file every shim execs,
    because the retirement lookup happens in `main` before `parse_args` and an in-process call to
    a handler would step straight over it.

    COMMAND_BRIDGE_DIR is pointed at an empty directory ON PURPOSE and it is not only hygiene: a
    live voice session is running on session `dev` while this spec lands (TC5), and the session
    directory is where `read_runtime` looks for a server to talk to. Isolating it means the worst
    case — a regression where the old name still dispatches — is a wait that finds no server and
    returns, rather than one that reaches into a conversation in progress and steals its cursor.

    Non-zero alone would be a weak assertion, because the wait ITSELF exits non-zero often enough
    to fake it. So this also pins the shape: an `unknown_command` code, and `watch` named as the
    replacement.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMMAND_BRIDGE_")}
    env["COMMAND_BRIDGE_DIR"] = str(tmp_path)
    env["COMMAND_BRIDGE_ENV_FILE"] = str(tmp_path / "absent.env")

    proc = subprocess.run(
        [sys.executable, RUNNER, "drain"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180,
    )
    combined = proc.stdout + proc.stderr

    assert proc.returncode != 0, (
        "the retired name still ran and exited 0:\n" + combined[:2000]
    )
    payload = _first_json(proc.stderr) or _first_json(proc.stdout) or {}
    assert payload.get("code") == "unknown_command", (
        "an unknown command must answer with the repo's error shape and a stable slug "
        f"(convention 8), got {combined[:2000]!r}"
    )
    assert "watch" in json.dumps(payload), (
        "the message must name the replacement — a list of twenty commands does not say which "
        f"one took over. Got {payload!r}"
    )


def _first_json(text: str):
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


# ------------------------------------------------------------------- AC16: absent from `describe`


def _walk(node, path="describe"):
    """Every key and every leaf value in the payload, with the path that reached it.

    Recursive on purpose. The retired entry deliberately SHARED objects with `watch` — its `args`
    dict was built from `**_WATCH_DOC["args"]` so the two could not drift — so a shallow check of
    the top-level command keys can pass while a shared nested object still carries the name.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}.{key}", str(key)
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple, set)):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")
    elif node is not None:
        yield path, str(node)


def test_the_retired_name_appears_nowhere_in_the_describe_payload():
    """AC16 (`unit`). Zero tolerance, and only here.

    `describe` is the contract (convention 3) — a statement of what the tool offers RIGHT NOW.
    Every string in it is read as a live claim, so there is no such thing as a historical mention
    inside it; the history belongs in the source comments and in the specs. That is why this one
    surface gets a flat "the word does not appear" rule while AC18 below has to reason about shape.

    Asserted on the payload `cmd_describe` actually returns, not on the `DESCRIBE` literal: the
    watchdog prompt is substituted in at call time, and that prompt is the single most
    instruction-shaped string the tool emits.
    """
    payload = cli.cmd_describe(argparse.Namespace(session="dev", human=False))
    hits = [(path, text) for path, text in _walk(payload) if WORD.search(text)]
    assert not hits, "the retired name is still in the `describe` contract at:\n" + "\n".join(
        f"  {path}: {text[:160]}" for path, text in hits
    )


# ----------------------------------------------- AC17: absent from the parser, help and dispatch


def _subparsers(parser):
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def test_the_retired_name_is_in_no_parser_no_help_and_no_dispatch_table():
    """AC17 (`unit`). Read off the live objects, which is what makes this survive a reword.

    Three separate surfaces, because the name was registered in three places and removing it from
    one is the failure mode:

    * the subparser choices — a real subparser, not an argparse `aliases=`, because it used to
      carry flags of its own;
    * every `format_help()` string, top level and per command, which is what `--help` prints;
    * the dispatch table, which is a local inside `main` and therefore invisible to `getattr`.
      Its keys compile into `main`'s code object as constants, so that is where they are read
      from. Scoped to `main` deliberately: `RETIRED_COMMANDS` at module level HOLDS the retired
      name on purpose — it is the tombstone that answers a call already in flight — and a check
      that scanned the whole module could not tell a tombstone from a live route.
    """
    parser = cli.build_parser()
    sub = _subparsers(parser)

    assert not [c for c in sub.choices if WORD.search(c)], (
        f"still a subcommand: {sorted(sub.choices)}"
    )

    helps = {"<top level>": parser.format_help()}
    for name, p in sub.choices.items():
        helps[name] = p.format_help()
    in_help = {name: text for name, text in helps.items() if WORD.search(text)}
    assert not in_help, f"the retired name is still in help text for: {sorted(in_help)}"

    assert not hasattr(cli, "cmd_drain"), "the handler is still importable"
    aliases = getattr(cli, "ALIASES", {})
    assert not any(WORD.search(str(k)) or WORD.search(str(v)) for k, v in dict(aliases).items()), (
        f"the alias table still carries it: {aliases!r}"
    )
    assert not [c for c in _consts(cli.main.__code__) if WORD.search(c)], (
        "`main` still holds the retired name as a constant — check the dispatch table"
    )


def _consts(code):
    """Every string constant in a code object, following nested code objects."""
    for const in code.co_consts:
        if isinstance(const, str):
            yield const
        elif isinstance(const, tuple):
            yield from (c for c in const if isinstance(c, str))
        elif hasattr(const, "co_consts"):
            yield from _consts(const)


# ------------------------------------------------------------------------------ AC18: the sweep

# The word, and ONLY the word. `(?<![\w-])` / `(?![\w-])` mean this never matches `drainClips`,
# `draining`, `drained`, `DRAIN_WAITS_S`, `mid-drain` or `FINAL-DRAIN` — identifiers and inflected
# verbs, none of which are the command name. That boundary does more work than it looks like: the
# browser client alone has a dozen `drainClips` references that no rule should ever have to argue
# about.
WORD = re.compile(r"(?<![\w-])drain(?![\w-])", re.I)

# 1. INVOCATION — text a reader could copy and execute. NO override, ever (see the docstring).
INVOCATION = re.compile(
    r"(?:^|[\s`\"'(\[/])(?:voice-tunnel(?:\.cmd)?|vt)\s+drain(?![\w-])"
    r"|(?<![\w-])drain\s+--\w",
    re.I,
)

# 2. RETIRED FLAGS — `--waits` and `--max-seconds` configured the collapsing ladder and existed on
#    no other command. Fires only on a line that also names the command, so the standing historical
#    sentence about how `--waits 5,3,2` outlived the ladder it configured stays sayable.
RETIRED_FLAGS = re.compile(r"--waits|--max-seconds")

# 3. IMPERATIVE — prose telling the reader to run it. The verb has to be right next to the word;
#    a wide window turns "Use `watch` ... the old name existed only because" into a false hit.
IMPERATIVE = re.compile(
    r"\b(?:run|call|invoke|execute|issue|use|try|then|first)\b[\s`\"'*_]{0,4}drain(?![\w-])",
    re.I,
)

# 4. PRESENT-TENSE EXISTENCE CLAIM — the name as the subject of a verb in the present tense, which
#    is a claim that the command is there to be used. `.md` files only; see the docstring.
PRESENT_TENSE = re.compile(
    r"(?<![\w-])drain`?\s+(?:and\s+`?[\w-]+`?\s+)?(?:still\s+|also\s+|only\s+|now\s+|always\s+)?"
    r"\b(?:is|are|runs|run|works|work|exists|exist|accepts|accept|continues|continue|does|do|"
    r"has|have|re-watches|rewatches|watches|checks|check|dispatches|dispatch|returns|return|"
    r"collapses|publishes|remains|stays|lives|takes|gives|fails|behaves|supports)\b",
    re.I,
)

# A RETIREMENT FRAME is the writer saying, in the same sentence, that the thing is in the past.
# It excuses rules 3 and 4. It NEVER excuses rules 1 and 2, which are the runnable shapes.
# Kept to explicit past/removal phrasings: "never", "before" and "old" were tried and are useless,
# because they turn up inside live instructions ("checks the speech signals `watch` never returns")
# as readily as inside history.
RETIREMENT_FRAME = re.compile(
    r"\b(used to|no longer|was removed|were removed|has been removed|have been removed|"
    r"is gone|are gone|was retired|were retired|ran the same|once said|once named|former|"
    r"no more|had to be)\b",
    re.I,
)

# Skipped, each for a stated reason — these are the two places the word must stay sayable in full.
#
# specs/    dated design records. `specs/005` is the record of the decision `specs/007` reverses,
#           and it contains the literal invocation because that is what shipped. Rewriting it
#           would delete the history that explains why the command went away, which is the same
#           reason a CHANGELOG entry is never edited: a dated record is not an instruction.
# tests/    these tests assert the ABSENCE of the command, and an assertion has to be able to name
#           what it forbids. This file alone would otherwise report itself a dozen times.
SKIP_DIRS = ("specs/", "tests/")
SKIP_SUFFIXES = (".svg", ".gif", ".png", ".ico", ".wav", ".onnx", ".bin", ".whl")


def _repo_files():
    """Every tracked text file the scan covers. `git ls-files` rather than a walk, so "the repo"
    means what git means by it and nothing in `venv/` or `build/` can wander in."""
    out = subprocess.run(
        ["git", "-C", REPO, "ls-files"], capture_output=True, text=True, timeout=120,
    )
    out.check_returncode()
    files = []
    for rel in out.stdout.splitlines():
        rel = rel.strip()
        if not rel or rel.startswith(SKIP_DIRS) or rel.lower().endswith(SKIP_SUFFIXES):
            continue
        files.append(rel)
    return files


def _sentence_at(lines, index, column):
    """The sentence the occurrence sits in, reconstructed across the line wrap.

    Prose in this repo is hard-wrapped at 100 columns and lives inside `#` comments and markdown
    as often as not, so a per-LINE test for a retirement frame gets the wrong answer constantly:
    "It used to say" routinely sits on the line above the thing it is quoting. The window is the
    line plus its two neighbours, stripped of comment and markdown furniture, and the sentence is
    then cut out of that — narrow enough that a frame two paragraphs away cannot launder an
    instruction, wide enough that a wrapped sentence is read whole.
    """
    furniture = re.compile(r"^\s*(?:#+|//|\*|>|-)?\s*")

    def clean(text):
        return " " + text.rstrip("\n")[furniture.match(text).end():]

    before = clean(lines[index - 1]) if index > 0 else ""
    here = clean(lines[index])
    after = clean(lines[index + 1]) if index + 1 < len(lines) else ""
    window = before + here + after
    # `clean` drops the furniture and puts one space back, so a character at original column
    # `column` lands at `1 + column - len(furniture)` inside `here`.
    offset = len(before) + 1 + max(column - furniture.match(lines[index]).end(), 0)
    offset = min(max(offset, 0), max(len(window) - 1, 0))

    starts = [0] + [m.end() for m in re.finditer(r"(?<=[.!?])\s+", window)]
    ends = [m.start() for m in re.finditer(r"(?<=[.!?])\s+", window)] + [len(window)]
    for start, end in zip(starts, ends, strict=True):  # same splits, so the same length
        if start <= offset <= end:
            return window[start:end]
    return window


def classify(rel_path, lines, index):
    """Verdicts for one line: a list of (rule, why) for the shapes that make it an instruction.

    THE RULE IS ABOUT THE SHAPE OF THE OCCURRENCE, NOT THE PRESENCE OF THE WORD, and both blanket
    answers were rejected on the way here.

    **A blanket ban is unmaintainable and destroys the thing this repo is built on.** The word
    appears more than a hundred times, almost all of it commentary explaining why the tool is the
    way it is: `server.py` describing the loop the wait replaced, `cli.py` quoting the rule that
    used to be printed, the queue in the browser client that drains on its own. Banning it would
    force every one of those to be deleted or contorted, and the history of WHY the second command
    went away is the single best defence against someone reintroducing it.

    **A blanket allowance is exactly how an instruction survives.** The paragraph at the top of
    AGENTS.md — the first thing an agent reads on entering this repo — told it to run the retired
    command before every reply, and it sat there through the release that deprecated the name.
    Nothing that counts occurrences or greps for the word would have made a sound.

    So each occurrence is judged by what it would do to a reader:

    * **INVOCATION** — a command line that can be copied and run. Fails everywhere, with no
      override, because a reader copies the command and not the sentence around it. A retirement
      frame is a claim the writer makes; a runnable line is runnable regardless of the claim.
    * **RETIRED_FLAGS** — a flag that only ever existed on the retired command, on a line that
      names it. Same reasoning.
    * **IMPERATIVE** — "run `x` first", "then `x` in the foreground". Yields to a retirement frame
      in the same sentence, because *quoting* an instruction to say it is dead is history and this
      repo does it deliberately: `cli.py` still carries the old rule verbatim inside "It used to
      say ...", and that sentence is the reason nobody reinvents it.
    * **PRESENT_TENSE** — the name as the subject of a present-tense verb ("`x` still runs",
      "`x` re-watches", "`x` and `y` are aliases"). **Markdown only.** A `.md` file is
      documentation and every sentence in it is a claim about the tool as it is now. Source
      comments are a narrative of how the code got here, written in the tense of the moment being
      described — policing their tense IS the unmaintainable blanket rule, and the same hazard is
      caught structurally one layer up by AC16 and AC17, which read the live `describe` payload,
      parser and dispatch table instead of the text around them.

    What this cannot see is written down in the module docstring of the test that uses it.
    """
    line = lines[index]
    match = WORD.search(line)
    if not match:
        # Every rule below is scoped to a line that names the command, which is what keeps
        # RETIRED_FLAGS from firing on the standing sentence about how `--waits 5,3,2` outlived
        # the ladder it configured — true, historical, and nowhere near the retired name.
        return []
    framed = bool(RETIREMENT_FRAME.search(_sentence_at(lines, index, match.start())))

    findings = []
    if INVOCATION.search(line):
        findings.append(("INVOCATION", "a command line a reader can copy and run"))
    if RETIRED_FLAGS.search(line):
        findings.append(("RETIRED_FLAGS", "a flag that existed only on the retired command"))
    if IMPERATIVE.search(line) and not framed:
        findings.append(("IMPERATIVE", "tells the reader to run it, nothing marks it as past"))
    if rel_path.lower().endswith(".md") and PRESENT_TENSE.search(line) and not framed:
        findings.append(("PRESENT_TENSE", "claims in the present tense that the command acts"))
    return findings


def scan(files, read=None):
    """Run `classify` over `files` and return `[(path, lineno, rule, why, text)]`."""
    read = read or (lambda rel: open(os.path.join(REPO, rel), encoding="utf-8",
                                     errors="replace").read())
    findings = []
    for rel in files:
        try:
            lines = read(rel).splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(lines):
            for rule, why in classify(rel, lines, i):
                findings.append((rel, i + 1, rule, why, line.strip()))
    return findings


# The instrument's own test table. Left column: shapes that must NOT fire, because history and the
# ordinary English verb have to survive. Right column: shapes that must fire.
ALLOWED = [
    ("AGENTS.md", "The second name, `drain`, was removed on 2026-08-19."),
    ("AGENTS.md", "This is what `drain` used to do before the wait learned to do it."),
    ("AGENTS.md", "`watch` and `drain` ran the same code under two spellings."),
    ("scripts/channel.py", "# Open the channel the way the orb does. The queue must drain on its own."),
    ("command_bridge/server.py", "# WORSE than the drain it replaced — 30 s against 10.5 s."),
    ("command_bridge/server.py", "# That is exactly the drain loop: while turns keep arriving,"),
    ("command_bridge/web/index.html", "// clip would start its own drain loop and we would overlap."),
    ("command_bridge/cli.py", "# `watch` and `drain` are aliases  <- source comment, AC17's job"),
]
FORBIDDEN = [
    ("AGENTS.md", "run `voice-tunnel drain --session <s> --since <cursor>` before you reply"),
    ("AGENTS.md", "then `drain` in the foreground before every say"),
    ("AGENTS.md", "an empty watch is not permission to speak, so run `drain` first"),
    ("AGENTS.md", "`drain` re-watches on collapsing short ceilings and checks the signals"),
    ("AGENTS.md", "`watch` and `drain` are aliases for one another"),
    ("AGENTS.md", "`drain` still runs as a deprecated alias for one release"),
    ("ai-docs/reference/turn-log.md", "voice-tunnel drain --session dev --since 42"),
    ("scripts/e2e.py", "# pass `drain --waits 5,3,2` to collapse the ladder"),
    ("README.md", "Use `voice-tunnel.cmd drain` on Windows."),
]


@pytest.mark.parametrize("path,text", ALLOWED)
def test_the_ac18_check_leaves_history_and_the_verb_alone(path, text):
    assert not classify(path, [text], 0), f"false positive on allowed prose: {text!r}"


@pytest.mark.parametrize("path,text", FORBIDDEN)
def test_the_ac18_check_fires_on_an_instruction(path, text):
    assert classify(path, [text], 0), f"missed an instruction: {text!r}"


def test_the_ac18_check_fires_on_a_planted_instruction_in_a_real_file(tmp_path):
    """The instrument shown to fail on a whole file, not a single line.

    This repo has shipped a denylist that refused nothing and three diagnostics that never
    populated, so a check nobody has watched go red is not evidence of anything. A copy of the
    swept AGENTS.md gets one instruction-shaped line planted in it; the scan must find that line
    and only that line, and must name the file and the line number.
    """
    original = open(os.path.join(REPO, "AGENTS.md"), encoding="utf-8").read()
    assert not scan(["AGENTS.md"]), "precondition: the real AGENTS.md is clean"

    planted = original.replace(
        "**You are no longer asked to remember that.**",
        "Run `voice-tunnel drain --session dev --since <cursor>` before you reply.\n\n"
        "**You are no longer asked to remember that.**",
        1,
    )
    assert planted != original, "the anchor moved; re-point the plant"
    (tmp_path / "AGENTS.md").write_text(planted, encoding="utf-8")

    findings = scan(["AGENTS.md"], read=lambda rel: (tmp_path / rel).read_text(encoding="utf-8"))
    assert len(findings) == 1, f"expected exactly the planted line, got {findings}"
    path, lineno, rule, _why, text = findings[0]
    assert path == "AGENTS.md" and rule == "INVOCATION"
    assert lineno == planted.splitlines().index(
        "Run `voice-tunnel drain --session dev --since <cursor>` before you reply.") + 1
    assert "voice-tunnel drain" in text


def test_the_ac18_scan_is_actually_reading_the_repo():
    """A scan over an empty file list passes, and so does one whose regex never matches anything.
    Both have shipped here before, so the scope is asserted before the verdict is trusted."""
    files = _repo_files()
    assert len(files) > 50, f"the file list collapsed: {len(files)} files"
    for expected in ("AGENTS.md", "README.md", "CHANGELOG.md", "command_bridge/cli.py",
                     "scripts/orbstate.py", "command_bridge/web/index.html"):
        assert expected in files, f"{expected} is not being scanned"
    assert not [f for f in files if f.startswith(SKIP_DIRS)], "a skipped directory leaked in"

    seen = sum(
        1 for rel in files
        for line in open(os.path.join(REPO, rel), encoding="utf-8", errors="replace")
        if WORD.search(line)
    )
    assert seen > 5, (
        f"the scanner found the word on only {seen} lines — either the repo has been rewritten "
        "or the pattern stopped matching, and a check that can no longer see the word cannot fail"
    )


def test_no_occurrence_in_the_repo_tells_a_reader_the_command_exists():
    """AC18. The verdict.

    WHAT THIS CANNOT SEE, stated rather than discovered later:

    * `specs/` and `tests/` are out of scope by the reasoning beside SKIP_DIRS.
    * A present-tense claim in a source comment or docstring (`.py`, `.js`, `.html`) is not
      flagged — only markdown is. The live surfaces are covered by AC16 and AC17 instead.
    * Noun apposition — "the `drain` alias", "a drain command" — is deliberately NOT a rule. The
      repo quotes its owner verbatim on exactly that phrasing ("we have had a watch command, then
      a drain command"), with a date beside it, and a rule that fires on a dated quotation is a
      rule that gets suppressed.
    * `{drain}` and similar interpolations are not prose and are not read as prose; a variable
      that merely carries the old name is invisible here.
    * A novel phrasing that instructs without any of these four shapes gets through. The shapes
      are the ones that have actually appeared in this repo, not a proof of completeness.
    """
    findings = scan(_repo_files())
    assert not findings, "these still tell a reader the command exists:\n" + "\n".join(
        f"  {path}:{lineno}  [{rule}] {why}\n      {text[:140]}"
        for path, lineno, rule, why, text in findings
    )
