"""Build the wheel and provision a cold install from it — without publishing anything.

WHY THIS EXISTS. Versions 0.2.1 through 0.2.4 were each found by handing an agent a fresh
virtualenv, the published wheel and nothing but `voice-tunnel describe`. That worked — every round
found real defects — but the loop ran through PyPI, so four public releases in ninety minutes were
really four test iterations, and PyPI has no unpublish. The artifact under test was never the
reason to publish; it was the only cold install lying around.

`python -m build` produces the same wheel the release workflow uploads, from the same tree. Pip
installs it into a throwaway virtualenv exactly as it would from the index. The only things this
cannot exercise are the upload itself and the npm/WinGet shims, and neither has ever been the
thing under test.

ISOLATION IS THE POINT, NOT A CONVENIENCE. A fresh virtualenv is not a fresh installation: 0.2.2
came out of an audit that built one, believed it was pristine, and inherited another agent's wake
name through the machine-wide settings file. So this points VOICE_TUNNEL_HOME at a directory
inside the sandbox before anything runs. Models are the one deliberate exception — a Parakeet
checkpoint is ~600 MB and re-downloading it per iteration is worse than the sharing — and
`--no-share-models` turns even that off when the download path is what you are testing.

    python scripts/coldstart.py                 # build, install [all], print the environment
    python scripts/coldstart.py --extras ""     # the bare floor a plain `pip install` lands on
    python scripts/coldstart.py --no-share-models --verify
    python scripts/coldstart.py --brief         # + a paste-ready prompt for a blind agent

Nothing here writes to the repository or to the machine-wide settings file.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = "Scripts" if os.name == "nt" else "bin"
EXE = ".exe" if os.name == "nt" else ""


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a step and fail loudly. A half-built sandbox is worse than none: the agent that gets
    handed it will report on whatever it finds, and attribute the gaps to the tool."""
    proc = subprocess.run(cmd, text=True, capture_output=True, **kw)
    if proc.returncode != 0:
        sys.stderr.write(f"\n$ {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}\n")
        raise SystemExit(f"failed: {cmd[0]} exited {proc.returncode}")
    return proc


def build_wheel(outdir: str) -> str:
    """Build from the working tree — including uncommitted edits, which is the whole point.

    Deliberately NOT `pip install -e .`: an editable install resolves back to the checkout, so
    every path question this is meant to answer ("does the wheel ship the client page", "does it
    find its own piper.exe", "is this a source checkout") gets the checkout's answer instead of
    the shipped one. That difference is exactly where the last four releases' bugs lived.
    """
    _run([sys.executable, "-m", "build", "--wheel", "--outdir", outdir], cwd=ROOT)
    wheels = sorted(glob.glob(os.path.join(outdir, "*.whl")), key=os.path.getmtime)
    if not wheels:
        raise SystemExit("build produced no wheel")
    return wheels[-1]


def provision(root: str, wheel: str, extras: str, share_models: bool) -> dict:
    venv = os.path.join(root, "venv")
    _run([sys.executable, "-m", "venv", venv])
    python = os.path.join(venv, SCRIPTS, "python" + EXE)

    spec = f"{wheel}[{extras}]" if extras else wheel
    _run([python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    _run([python, "-m", "pip", "install", "--quiet", spec])

    home = os.path.join(root, "home")
    os.makedirs(home, exist_ok=True)
    env = {"VOICE_TUNNEL_HOME": home}
    if share_models:
        # The one path shared on purpose. `doctor` reports it under runtime.shared, so the agent
        # can see it rather than discover it — which is the lesson 0.2.2 was named after.
        env["VOICE_TUNNEL_MODELS_DIR"] = os.path.join(
            os.path.expanduser("~"), ".voice-tunnel", "models")

    return {
        "root": root,
        "wheel": os.path.basename(wheel),
        "extras": extras or "(none — the bare floor)",
        "python": python,
        "shim": os.path.join(venv, SCRIPTS, "voice-tunnel" + EXE),
        "env": env,
    }


def verify(sandbox: dict) -> dict:
    """Not "did it install" — did it install something that answers.

    `describe` is the contract an agent reads first, and `doctor` is the one that has been wrong
    in four consecutive releases, so both run before anything is handed over.
    """
    env = dict(os.environ, **sandbox["env"])
    out = {}
    for cmd in ("describe", "doctor", "download --list"):
        proc = subprocess.run([sandbox["shim"], *cmd.split()],
                              text=True, capture_output=True, env=env)
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            payload = None
        out[cmd] = {
            "exit": proc.returncode,
            "parsed": payload is not None,
            "stderr": (proc.stderr or "").strip()[:400] or None,
        }
    doctor = subprocess.run([sandbox["shim"], "doctor"], text=True, capture_output=True, env=env)
    try:
        d = json.loads(doctor.stdout or "{}")
        out["doctor_summary"] = {k: d.get(k) for k in ("ok", "failed", "degraded", "next")}
        out["doctor_summary"]["runtime.version"] = d.get("runtime", {}).get("version")
    except json.JSONDecodeError:
        pass
    return out


BRIEF = """\
You are testing a CLI you have never seen. Do not read its source, its README, or any note about \
it — the point is to find out what the tool alone can tell you.

Run `{shim} describe` and go from there. Your goal: reach the best configuration the tool \
supports on this machine, using only commands it tells you about. These are already set for you \
and must stay set:

{envlines}

Report, concretely: every command you ran and what it returned; anything that told you something \
untrue, incomplete, or that you could not act on; anything you had to guess, infer by analogy, or \
learn by trial and error; and the state you finished in. Where you got stuck, say what output \
would have unstuck you.

Do not fix the tool. Do not edit any file outside the sandbox at {root}.
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the wheel and provision a cold install from it, without publishing.")
    ap.add_argument("--extras", default="all",
                    help="extras to install ('all', 'piper,turn', or '' for the bare floor)")
    ap.add_argument("--dir", help="where to build the sandbox (default: a new temp directory)")
    ap.add_argument("--no-share-models", action="store_true",
                    help="download models into the sandbox instead of reusing the user's cache")
    ap.add_argument("--verify", action="store_true", help="run describe/doctor and report")
    ap.add_argument("--brief", action="store_true",
                    help="print a paste-ready prompt for a blind agent")
    ap.add_argument("--keep-dist", action="store_true", help="leave the built wheel on disk")
    args = ap.parse_args()

    root = args.dir or tempfile.mkdtemp(prefix="voice-tunnel-cold-")
    os.makedirs(root, exist_ok=True)
    dist = os.path.join(root, "dist")

    wheel = build_wheel(dist)
    sandbox = provision(root, wheel, args.extras, share_models=not args.no_share_models)
    if args.verify:
        sandbox["verify"] = verify(sandbox)
    if not args.keep_dist and not args.dir:
        shutil.rmtree(dist, ignore_errors=True)
        sandbox["wheel"] += " (removed; pass --keep-dist to retain)"

    print(json.dumps(sandbox, indent=2))
    if args.brief:
        envlines = "\n".join(f"  {k}={v}" for k, v in sandbox["env"].items())
        print("\n" + "-" * 70 + "\n")
        print(BRIEF.format(shim=sandbox["shim"], envlines=envlines, root=root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
