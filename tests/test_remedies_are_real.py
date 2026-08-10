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
    return sorted((ROOT / "voice_tunnel").rglob("*.py"))


def test_pyproject_declares_the_extras_we_expect():
    extras = declared_extras()
    assert {"piper", "parakeet", "turn", "all"} <= extras, extras


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
    dist = {"sherpa_onnx": "sherpa-onnx", "piper": "piper-tts"}.get(module, module)
    assert dist in line, f"`{module}` is imported optionally but [{extra}] does not install {dist}"


def test_the_all_extra_is_the_union_of_the_others():
    """`all` has to mean all, or `voice-tunnel setup` silently leaves a feature broken."""
    text = PYPROJECT.read_text(encoding="utf-8")
    block = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    all_line = next(ln for ln in block.splitlines() if ln.strip().startswith("all"))
    for dist in ("piper-tts", "sherpa-onnx", "onnxruntime", "transformers"):
        assert dist in all_line, f"[all] is missing {dist}"


def test_setup_installs_the_all_extra():
    """`setup` is the one command that must leave nothing on a fallback."""
    src = (ROOT / "voice_tunnel" / "cli.py").read_text(encoding="utf-8")
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
