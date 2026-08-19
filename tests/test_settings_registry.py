"""Every VOICE_TUNNEL_* variable the running code reads is a variable the CLI can describe.

WHY THIS FILE EXISTS. Three times now the package has read a setting the CLI knew nothing about.
The piper paths went undocumented while `describe` listed 8 of the 17 variables the code read.
The three Kokoro keys were live, honoured, and sitting in the owner's own `.env` selecting the
voice he was listening to, while `config get VOICE_TUNNEL_KOKORO_VOICE` answered "unknown
setting". Then, the moment someone looked again, five more — including one the owner had
hand-written into `.env` with a comment, because editing the file was the only way to set it.

Each time, the fix was the instances. This is the fix for the class. It walks the source and
fails when the code reads a name the registry does not declare, because a table that can only be
wrong when someone forgets to update it cannot catch someone forgetting to update it.

THE FOUR BUCKETS. Every VOICE_TUNNEL_* name the package reads falls into exactly one, and a name
in none of them fails the build:

1. **registered** — in `config.SETTINGS`, which is the answer for almost everything;
2. **process-only** — declared in `cli.DESCRIBE["env_process_only"]`. Read from the live payload
   rather than re-listed here, so one declaration keeps serving both consumers;
3. **excluded** — named in `EXCLUDED` below with its reason written out. One member today;
4. **foreign** — not this tool's namespace at all (`LOCALAPPDATA`, `XDG_CONFIG_HOME`,
   `XDG_DATA_HOME`), so not this registry's business.

WHY AN AST WALK AND NOT A REGEX. A regex over string literals cannot tell a *read* from a
*mention*, and this repo deliberately keeps retired names in prose: `config.py` records that the
per-name `VOICE_TUNNEL_WAKE_BARE` opt-in was deleted, and `cli.py` names `VOICE_TUNNEL_HOME` as a
dict key in the `describe` payload as well as reading it somewhere else entirely. Both would fail
a regex, neither is a read, and a guard that fails on correct code is a guard somebody disables.

WHY THE FIXTURES. `test_the_guard_fires_on_an_unregistered_read` points this exact code path at a
constructed module that reads a fictional variable, and asserts it is reported by name, file and
line. Without it, the zero on `voice_tunnel/` is indistinguishable from a walker that parses
nothing — and this repo has shipped a denylist that refused nothing and three diagnostics that
never populated. The two false-positive fixtures cover the other direction.
"""
import ast
import pathlib
from typing import NamedTuple

import pytest

from voice_tunnel import cli, config

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

# TC5: the running tool only. `tests/` sets variables the code does not read — that is how the
# retired bare-wake opt-in is proven inert — and `scripts/` are developer harnesses. Widening the
# walk to either makes this guard fail on correct code.
PACKAGE = ROOT / "voice_tunnel"

NAMESPACE = "VOICE_TUNNEL_"

# THE EXCLUSION TABLE. One entry, one reason, written down rather than implied — a name silently
# missing from the registry reads like an oversight to the next person, which is how the last
# three of these survived review.
EXCLUDED: dict[str, str] = {
    "VOICE_TUNNEL_PIPER_LENGTH_SCALE": (
        "Deliberately retired. `speech_speed()` still honours a hand-written length_scale and "
        "converts it, so an old .env keeps working — but the unit is INVERTED (lower is faster), "
        "which is the exact confusion VOICE_TUNNEL_SPEECH_SPEED exists to remove. Registering it "
        "would re-publish the inverted unit as a supported knob; leaking it once already produced "
        "half speed when the owner asked for double."
    ),
}

# The function names that mean "read the environment". `_env` and `_int_env` are config.py's own
# helpers, matched on the attribute so `config._env(...)` from cli.py counts too. `setdefault` is
# included with `get` because it also returns the environment's value — a read that happens to
# write when the name is absent is still a read.
_ENV_HELPERS = frozenset({"_env", "_int_env", "getenv"})
_ENVIRON_METHODS = frozenset({"get", "setdefault"})


class Read(NamedTuple):
    """One place the source reaches into the environment.

    `name` is None when the walk could not resolve one — the argument was a variable rather than a
    string literal. Those are kept rather than dropped so a test can assert they exist and are
    passed over on purpose, instead of the walker quietly having no opinion about them.
    """

    name: str | None
    path: pathlib.Path
    lineno: int

    @property
    def where(self) -> str:
        try:
            rel = self.path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            rel = self.path.as_posix()
        return f"{rel}:{self.lineno}"


def _is_environ(node: ast.AST) -> bool:
    """`os.environ`, or a bare `environ` from `from os import environ`."""
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _is_env_call(func: ast.AST) -> bool:
    """Does this call expression read the environment by its first argument?"""
    if isinstance(func, ast.Name):
        return func.id in _ENV_HELPERS
    if isinstance(func, ast.Attribute):
        # `os.getenv(...)`, `config._env(...)`
        if func.attr in _ENV_HELPERS:
            return True
        # `os.environ.get(...)` — the `.get` must belong to environ, or every `.get()` in the
        # package would be treated as an environment read.
        return func.attr in _ENVIRON_METHODS and _is_environ(func.value)
    return False


class _EnvReadVisitor(ast.NodeVisitor):
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.reads: list[Read] = []

    def _record(self, arg: ast.AST, lineno: int) -> None:
        name = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
        self.reads.append(Read(name, self.path, lineno))

    def visit_Call(self, node: ast.Call) -> None:
        if _is_env_call(node.func) and node.args:
            self._record(node.args[0], node.args[0].lineno)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # A LOAD only. `os.environ["X"] = v` is the tool publishing a value for a child process,
        # not the tool depending on one, and the registry describes what can be set, not what is.
        if _is_environ(node.value) and isinstance(node.ctx, ast.Load):
            self._record(node.slice, node.lineno)
        self.generic_visit(node)


def scan(directory: pathlib.Path) -> list[Read]:
    """Every environment read under `directory`, resolved or not.

    THE PURE FUNCTION AT THE CENTRE OF THIS FILE. The real run and every fixture below go through
    it, because a guard whose fixtures exercise a different code path from the real run has tested
    nothing.
    """
    found: list[Read] = []
    for path in sorted(pathlib.Path(directory).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _EnvReadVisitor(path)
        visitor.visit(tree)
        found.extend(visitor.reads)
    return found


def registered_keys() -> set[str]:
    return {s["key"] for s in config.SETTINGS}


def process_only_keys() -> set[str]:
    """Read from the live `describe` payload, never re-listed — one declaration, two consumers."""
    return set(cli.DESCRIBE["env_process_only"])


def classify(name: str) -> str:
    """Which of the four buckets a name falls in. `unclassified` is the failure."""
    if not name.startswith(NAMESPACE):
        return "foreign"
    if name in registered_keys():
        return "registered"
    if name in process_only_keys():
        return "process_only"
    if name in EXCLUDED:
        return "excluded"
    return "unclassified"


def unclassified_reads(directory: pathlib.Path) -> list[Read]:
    """The guard's answer: reads of a VOICE_TUNNEL_* name that nothing declares."""
    return [r for r in scan(directory) if r.name and classify(r.name) == "unclassified"]


def failure_report(findings: list[Read]) -> str:
    """FR3: name, file and line, so the fix is obvious from the failure.

    A guard whose message requires an investigation is a guard that gets suppressed instead of
    obeyed, so the offending line is printed rather than the count.
    """
    lines = [
        "The code reads VOICE_TUNNEL_* variables the CLI does not declare.",
        "Each one is invisible to `describe`, to `config get` and to `.env.example`, so the only "
        "way to set it is to hand-edit a file — which is the defect this guard exists to stop "
        "happening a fourth time.",
        "",
    ]
    lines += [
        f"  {r.name}  read at {r.where}"
        for r in sorted(findings, key=lambda r: (r.name or "", r.where))
    ]
    lines += [
        "",
        "Fix exactly one of these for each name:",
        "  * register it in `config.SETTINGS` with a `what` and a live resolver — the usual answer;",
        "  * if it decides WHERE the settings file lives, declare it in "
        "`cli.DESCRIBE['env_process_only']`;",
        "  * if it is deliberately retired, add it to EXCLUDED in "
        "tests/test_settings_registry.py WITH ITS REASON.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------- the guard (AC9, AC10)


def test_the_package_declares_every_setting_it_reads():
    """AC9. Zero unclassified reads under `voice_tunnel/`."""
    findings = unclassified_reads(PACKAGE)
    assert not findings, failure_report(findings)


def test_the_walk_actually_finds_reads_in_the_package():
    """The companion to the assertion above: a walker that parses nothing also returns zero.

    This is the in-tree half of the negative control. The fixture below is the other half.
    """
    named = [r for r in scan(PACKAGE) if r.name]
    assert len(named) > 20, f"only {len(named)} literal reads found — the walk has stopped working"
    assert {"VOICE_TUNNEL_TTS", "VOICE_TUNNEL_HOME"} <= {r.name for r in named}


def test_the_failure_message_names_the_variable_the_file_and_the_line():
    """AC10, on a real finding rather than a hand-built one."""
    findings = unclassified_reads(FIXTURES / "unregistered_read")
    message = failure_report(findings)
    assert "VOICE_TUNNEL_NOT_A_REAL_SETTING" in message
    assert "tests/fixtures/unregistered_read/reads_unregistered.py" in message
    assert f":{findings[0].lineno}" in message
    # The remedy, not just the complaint.
    assert "config.SETTINGS" in message


# ------------------------------------------------------- the two documented exclusions (AC11)


def test_the_retired_length_scale_key_is_excluded_with_a_reason():
    """AC11, TC1. Asserted by name, so deleting the reason is a failure and not a quiet pass."""
    assert classify("VOICE_TUNNEL_PIPER_LENGTH_SCALE") == "excluded"
    reason = EXCLUDED["VOICE_TUNNEL_PIPER_LENGTH_SCALE"]
    assert len(reason) > 40, "an exclusion without a real reason reads as an oversight"


def test_the_home_variable_is_process_only():
    """AC11, TC4. It selects where the settings file lives, so it cannot live in that file.

    Read from the `describe` payload, which is the declaration — this asserts the payload still
    carries it, not that a copy of the list here still does.
    """
    assert classify("VOICE_TUNNEL_HOME") == "process_only"
    assert "VOICE_TUNNEL_HOME" in cli.DESCRIBE["env_process_only"]


def test_every_exclusion_names_a_variable_the_code_still_reads():
    """An exclusion for a name nobody reads any more is a graveyard entry, not a decision."""
    read_names = {r.name for r in scan(PACKAGE) if r.name}
    stale = sorted(set(EXCLUDED) - read_names)
    assert not stale, f"excluded but no longer read anywhere — delete the row: {stale}"


def test_no_name_falls_into_two_buckets():
    """The four buckets are exclusive, so a registered name must not also carry an exclusion."""
    overlap = (set(EXCLUDED) & registered_keys()) | (set(EXCLUDED) & process_only_keys())
    assert not overlap, f"declared twice — the exclusion is now wrong: {sorted(overlap)}"


@pytest.mark.parametrize("name, bucket", [
    ("LOCALAPPDATA", "foreign"),
    ("XDG_CONFIG_HOME", "foreign"),
    ("XDG_DATA_HOME", "foreign"),
    ("VOICE_TUNNEL_TTS", "registered"),
    ("VOICE_TUNNEL_HOME", "process_only"),
    ("VOICE_TUNNEL_PIPER_LENGTH_SCALE", "excluded"),
    ("VOICE_TUNNEL_NOT_A_REAL_SETTING", "unclassified"),
])
def test_each_bucket_is_reachable(name, bucket):
    """FR5: four buckets plus the failure. A classifier nothing ever falls through is a constant."""
    assert classify(name) == bucket


# ------------------------------------------------------------- the negative control (AC12)


def test_the_guard_fires_on_an_unregistered_read():
    """AC12, FR4. THE MOST IMPORTANT TEST IN THIS FILE, and it runs on every suite run.

    Pointed at a constructed module that reads a fictional variable, the same `scan` +
    `classify` path used against `voice_tunnel/` must report exactly that name, with the file it
    is read in and the line it is read on. Delete this and AC9's zero becomes unfalsifiable.
    """
    fixture = FIXTURES / "unregistered_read" / "reads_unregistered.py"
    findings = unclassified_reads(fixture.parent)

    assert [r.name for r in findings] == ["VOICE_TUNNEL_NOT_A_REAL_SETTING"]
    finding = findings[0]
    assert finding.path.resolve() == fixture.resolve()

    # The line is derived from the fixture rather than hardcoded, so editing the fixture cannot
    # leave this asserting a number that used to be right.
    source = fixture.read_text(encoding="utf-8").splitlines()
    expected = next(i for i, line in enumerate(source, 1)
                    if "VOICE_TUNNEL_NOT_A_REAL_SETTING" in line and "getenv" in line)
    assert finding.lineno == expected
    assert finding.where.endswith(f"reads_unregistered.py:{expected}")


# ------------------------------------------------------- the false-positive controls (AC13, AC14)


def test_a_name_in_a_docstring_a_comment_or_a_dict_key_is_not_a_read():
    """AC13, TC3. The reason this is an AST walk: a regex sees four hits in that fixture."""
    fixture_dir = FIXTURES / "mentions_only"
    assert unclassified_reads(fixture_dir) == []

    # And prove the file was parsed at all — the registered read in it must have been seen, or
    # this test would pass just as well against an empty directory.
    names = {r.name for r in scan(fixture_dir) if r.name}
    assert names == {"VOICE_TUNNEL_TTS"}, names
    assert "VOICE_TUNNEL_MENTIONED_NEVER_READ" not in names


def test_the_two_prose_mentions_in_the_real_tree_are_not_reads():
    """AC13 on the tree itself, not on a stand-in. These two are why the walk is an AST walk.

    Both would fail a regex over string literals, and both are correct as they stand.
    """
    reads = [r for r in scan(PACKAGE) if r.name]

    # `config.py` records in a docstring that the per-name bare-wake opt-in was DELETED. Reading
    # that as a live setting would publish a knob that cannot be honoured — worse than an
    # undocumented one, because a documented no-op is not discoverable as broken.
    assert "VOICE_TUNNEL_WAKE_BARE" not in {r.name for r in reads}

    # `cli.py` names VOICE_TUNNEL_HOME as a dict KEY in the `describe` payload. A dict key is
    # documentation; the only place the value is read is `config.home_dir()`.
    home = sorted({r.path.name for r in reads if r.name == "VOICE_TUNNEL_HOME"})
    assert home == ["config.py"], f"VOICE_TUNNEL_HOME counted as read in {home}"


def test_a_read_whose_name_is_computed_is_not_a_failure():
    """AC14, TC3. The registry's own plumbing must not fail the registry's own guard."""
    fixture_dir = FIXTURES / "non_literal_read"
    assert unclassified_reads(fixture_dir) == []

    unresolved = [r for r in scan(fixture_dir) if r.name is None]
    assert len(unresolved) >= 3, "the walk did not even see the non-literal reads it must ignore"


def test_the_package_has_non_literal_reads_and_passes_anyway():
    """The same property on the real tree, where those call sites actually live.

    `_env(name)`, `config get`/`config set` resolving `args.key`, and the `effective()` walk over
    SETTINGS all read a name held in a variable. They are why this guard reports unresolved reads
    instead of failing on them.
    """
    unresolved = [r for r in scan(PACKAGE) if r.name is None]
    assert unresolved, "no non-literal reads found — the walk is not seeing them"
    # Seen, and none of them reported: `unclassified_reads` only ever yields resolved names.
    assert all(r.name for r in unclassified_reads(PACKAGE))
