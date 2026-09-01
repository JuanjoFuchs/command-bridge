"""Every `pip install voice-tunnel[...]` this CLI prints must name an extra that exists.

WHY THIS FILE EXISTS. Turn detection shipped in 0.2.0 and could not work for anybody who
installed from PyPI. It imports `onnxruntime` and `transformers`; neither was declared in any
extra or requirement, so a clean install could never load it. Meanwhile `doctor`, `download` and
the runtime error all advised `pip install voice-tunnel[parakeet]` — an extra that installs
sherpa-onnx and neither of the two packages actually needed.

Anyone following that advice stayed exactly as broken, and reasonably concluded the feature was
unsupported on their machine. It went unnoticed because it works in a checkout that already
carries both packages for other reasons, which is the only place it was ever run.

Found by handing a fresh agent nothing but `voice-tunnel describe` and asking it to reach the
best configuration: it ran the remedy verbatim, inspected the installed metadata, and reported
that the command could not possibly do what the tool claimed.

A remedy is a promise the tool makes. This checks the promise is keepable — statically, without
network access, so it fails in CI rather than in someone's first session.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"

# Every string the CLI can print that suggests installing an extra.
EXTRA_PATTERN = re.compile(r"voice-tunnel\[([a-z0-9_,\- ]+)\]")


def declared_extras() -> set[str]:
    """Extra names from pyproject, parsed without importing a TOML library."""
    text = PYPROJECT.read_text(encoding="utf-8")
    block = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    return {m.group(1) for m in re.finditer(r"(?m)^([a-zA-Z0-9_-]+)\s*=", block)}


def sources() -> list[pathlib.Path]:
    return sorted((ROOT / "command_bridge").rglob("*.py"))


def test_pyproject_declares_the_extras_we_expect():
    extras = declared_extras()
    assert {"piper", "kokoro", "parakeet", "turn", "all"} <= extras, extras


def test_every_extra_the_cli_recommends_actually_exists():
    extras = declared_extras()
    offenders = []
    for path in sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in EXTRA_PATTERN.finditer(line):
                for name in (n.strip() for n in match.group(1).split(",")):
                    if name and name not in extras:
                        offenders.append(f"{path.name}:{lineno} suggests [{name}]")
    assert not offenders, (
        "the CLI prints install commands for extras that do not exist:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("module, extra", [
    ("piper", "piper"),
    # kokoro_onnx shipped with NO extra behind it at all — the turn bug repeated verbatim, one
    # backend later. `tts._ResidentKokoro._load` has imported it since the backend landed, so
    # `COMMAND_BRIDGE_TTS=kokoro` could only ever work in a checkout whose venv already carried the
    # package for some other reason. That is precisely the environment this test suite runs in,
    # which is why the gap had to be closed by declaring the pair rather than by noticing it.
    ("kokoro_onnx", "kokoro"),
    ("sherpa_onnx", "parakeet"),
    ("onnxruntime", "turn"),
    ("transformers", "turn"),
])
def test_each_optional_import_is_covered_by_an_extra(module, extra):
    """Every optional import the package makes has an extra that supplies it.

    The turn-detection bug was exactly this invariant being violated: two imports with no extra
    behind them. Checking the mapping by hand is how it survived a release.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    block = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    line = next((ln for ln in block.splitlines() if ln.strip().startswith(f"{extra} ")
                 or ln.strip().startswith(f"{extra}=")), "")
    dist = {"sherpa_onnx": "sherpa-onnx", "piper": "piper-tts",
            "kokoro_onnx": "kokoro-onnx"}.get(module, module)
    assert dist in line, f"`{module}` is imported optionally but [{extra}] does not install {dist}"


def test_every_optional_import_in_the_package_is_in_the_table_above():
    """THE HOLE THE TABLE ABOVE CANNOT SEE, and the one that let kokoro through.

    The parametrized test checks each pair someone remembered to type. It cannot fail for an
    import nobody added a row for — which is exactly how a backend ships with no extra, twice.
    So this walks the source instead: every `from <mod> import` / `import <mod>` that sits inside
    a function (an import deferred to call time IS the optional-dependency idiom in this package,
    because a module-level one would break the floor install) must appear in the table.

    Deliberately a whitelist of known-stdlib/first-party names rather than a clever AST pass: the
    list is short, and a check whose failure mode is "add the module you just made optional" is
    worth more than one nobody can read.
    """
    import ast

    covered = {m for m, _ in test_each_optional_import_is_covered_by_an_extra.pytestmark[0].args[1]}
    # Deferred imports that are NOT optional third-party runtimes: stdlib pulled in late to keep
    # CLI startup cheap, and this package's own modules imported lazily to avoid an import cycle.
    exempt = {
        "argparse", "array", "ast", "asyncio", "base64", "collections", "ctypes", "dataclasses",
        "datetime", "difflib", "functools", "glob", "gzip", "hashlib", "hmac", "html", "http",
        "importlib", "inspect", "io", "ipaddress", "json", "logging", "math", "os", "pathlib",
        "platform", "queue", "random", "re", "secrets", "shlex", "shutil", "signal", "socket",
        "ssl", "statistics", "string", "struct", "subprocess", "sys", "tarfile", "tempfile",
        "textwrap", "threading", "time", "traceback", "types", "typing", "unicodedata",
        "urllib", "uuid", "wave", "webbrowser", "zipfile",
        # Hard dependencies in [project.dependencies], deferred only to keep CLI startup cheap —
        # they need no extra because a floor install already has them.
        "numpy", "aiohttp", "faster_whisper",
        "command_bridge",                            # first-party, lazy for cycle reasons
    }
    offenders = []
    for path in sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for inner in ast.walk(node):
                names = []
                if isinstance(inner, ast.Import):
                    names = [a.name.split(".")[0] for a in inner.names]
                elif isinstance(inner, ast.ImportFrom) and inner.level == 0 and inner.module:
                    names = [inner.module.split(".")[0]]
                for name in names:
                    if name not in exempt and name not in covered:
                        offenders.append(f"{path.name}:{inner.lineno} imports {name!r}")
    assert not offenders, (
        "optional imports with no extra mapped to them — add a row to "
        "test_each_optional_import_is_covered_by_an_extra (and an extra to pyproject):\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_the_all_extra_is_the_union_of_the_others():
    """`all` has to mean all, or `voice-tunnel setup` silently leaves a feature broken."""
    text = PYPROJECT.read_text(encoding="utf-8")
    block = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    all_line = next(ln for ln in block.splitlines() if ln.strip().startswith("all"))
    for dist in ("piper-tts", "kokoro-onnx", "sherpa-onnx", "onnxruntime", "transformers"):
        assert dist in all_line, f"[all] is missing {dist}"


def test_setup_installs_the_all_extra():
    """`setup` is the one command that must leave nothing on a fallback."""
    src = (ROOT / "command_bridge" / "cli.py").read_text(encoding="utf-8")
    body = src.split("def cmd_setup", 1)[1].split("\ndef ", 1)[0]
    assert "voice-tunnel[all]" in body
    for module in ("piper", "sherpa_onnx", "onnxruntime", "transformers"):
        assert module in body, f"setup does not check for {module}"


def test_doctor_never_recommends_a_nonexistent_extra(capsys, tmp_sessions):
    """The same guarantee, through the command a person actually runs."""
    from tests.test_cli_surface import run

    _, payload, _ = run(["doctor"], capsys)
    extras = declared_extras()
    for check in payload["checks"]:
        for field in ("detail", "remedy"):
            for match in EXTRA_PATTERN.finditer(str(check.get(field) or "")):
                for name in (n.strip() for n in match.group(1).split(",")):
                    assert name in extras, f"{check['name']}.{field} names missing extra [{name}]"
