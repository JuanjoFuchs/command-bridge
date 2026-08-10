---
id: "002"
title: Packaging and Distribution
status: in_progress
blocked_by: ["001"]
blocks: ["003"]
---

# Packaging and Distribution

## Overview

Publish the voice tunnel from spec 001 as a Python distribution on PyPI, release artifacts on
GitHub Releases, and a WinGet package for Windows. npm and `npx` distribution is deliberately
separate and covered by spec 003.

The distribution name, the import name, and the command are all `voice-tunnel` / `voice_tunnel`.
`voice-mode` — the name this project shipped under internally — is taken on PyPI by mbailey's
VoiceMode, which is the closest prior art and is cited in the project note. `voice-tunnel` was
verified free on both PyPI and npm before the rename landed.

> **Completion rule:** This spec is not complete until all acceptance criteria are verified
> through the testing approach below, including a real install from the real publish channel:
> `pipx install voice-tunnel` on a clean machine, and `winget install JuanjoFuchs.voice-tunnel`
> on a clean Windows machine after Microsoft approval. Build-only and CI-only verification are
> insufficient. The agent must iterate until verification passes.

## Goals

- Make `voice-tunnel` runnable on a fresh machine through `pip`, `pipx`, and WinGet.
- Preserve every spec 001 behaviour when invoked through an installed package rather than a
  checkout.
- Publish reproducible artifacts from GitHub Actions, never from a developer machine.
- Establish the release artifacts that spec 003 consumes for npm.
- Hold the actual 0.1.0 publish behind an explicit human go, so the launch video and the
  published version describe the same tool.

## Requirements

### Functional Requirements

- **FR1**: `pip install voice-tunnel`, `pipx install voice-tunnel`, and
  `pipx run voice-tunnel describe` work on Linux, macOS and Windows for Python 3.10+.
- **FR2**: Optional extras install the better backends: `voice-tunnel[piper]`,
  `voice-tunnel[parakeet]`, `voice-tunnel[all]`.
- **FR3**: `python -m voice_tunnel` runs the same CLI as the console script.
- **FR4**: Each tagged release attaches a wheel and an sdist to the GitHub Release.
- **FR5**: `winget install JuanjoFuchs.voice-tunnel` installs a working `voice-tunnel` on Windows
  after the WinGet manifest is approved.
- **FR6**: An installed package writes no runtime state inside its own installation directory.
  Settings, turn logs, voiceprints and models resolve to per-user directories.
- **FR7**: The client page is served correctly from an installed package, not only from a
  checkout.
- **FR8**: A release is produced only by pushing a `vX.Y.Z` tag, and only when that tag matches
  the version in package metadata.

### Non-Functional Requirements

- **NFR1**: All published artifacts are built and uploaded by GitHub Actions.
- **NFR2**: After the first release, PyPI uploads use Trusted Publishing (OIDC). Long-lived PyPI
  API tokens are not used for steady-state publishing.
- **NFR3**: The version is authored in exactly one place, and a tag that disagrees with it fails
  the release before any artifact is created or uploaded.
- **NFR4**: CI runs the unit suite on every push and pull request, on all three operating
  systems, without requiring a microphone, a GPU, or a downloaded model.

### Technical Constraints

- **TC1**: PyPI distribution name `voice-tunnel`; import name `voice_tunnel`; console command
  `voice-tunnel`. All three agree, unlike agent-mail-cli where PyPI rejected the short name.
- **TC2**: Python 3.10+.
- **TC3**: WinGet package identifier is `JuanjoFuchs.voice-tunnel`.
- **TC4**: Core dependencies are `aiohttp`, `numpy`, `faster-whisper`. `piper-tts` and
  `sherpa-onnx` are extras, because each carries a large native runtime and a first install
  should not pay for backends the user has not chosen.
- **TC5**: The client page ships as package data inside `voice_tunnel/web/`.
- **TC6**: **No GPU is required, and no code path uses one.** Every model in the stack is CPU
  inference: Parakeet is int8 via onnxruntime, Piper is ONNX, faster-whisper uses CTranslate2,
  TitaNet is onnxruntime. This is a documented property, not an accident — see the hardware
  measurements below.
- **TC7**: Models are downloaded at runtime by `voice-tunnel download`, never vendored into any
  artifact. A Parakeet checkpoint is 631 MB.

### Requirement Traceability

| Requirement | Acceptance Criteria |
|---|---|
| FR1 | AC5, AC6, AC7 |
| FR2 | AC8 |
| FR3 | AC4 |
| FR4 | AC3 |
| FR5 | AC12, AC13, AC14 |
| FR6 | AC9, AC10 |
| FR7 | AC11 |
| FR8 | AC15 |
| NFR1 | AC3, AC16 |
| NFR2 | AC16, AC17 |
| NFR3 | AC15 |
| NFR4 | AC1 |
| TC1 | AC2, AC5 |
| TC2 | AC5 |
| TC3 | AC12, AC13 |
| TC4 | AC8 |
| TC5 | AC11 |
| TC6 | AC18 |
| TC7 | AC19 |

## Pre-requisites (Human Required)

**These block publishing and nothing else.** Every implementation task below can be completed and
verified without them; only the final publish steps are gated. Each names exactly what to do and
what to hand back.

### 1. GitHub repository

- [ ] Create `https://github.com/JuanjoFuchs/voice-tunnel` (public, no README/LICENSE/gitignore —
      the local repo has all three and an unrelated-history merge is avoidable pain).
- [ ] Confirm so the local `main` can be pushed. **History was rewritten this session**, so this
      must be the first push to a repository that has never had content.

### 2. GitHub `release` environment

- [ ] Create an environment named `release` in the new repository's settings. The release
      workflow requests OIDC credentials from it; PyPI Trusted Publishing binds to the
      environment name, so it must match exactly.

### 3. PyPI

- [ ] Confirm the PyPI account is the same one that owns `agent-mail-cli` and `ccburn`.
- [ ] Reserve `voice-tunnel` via **Pending Trusted Publisher** at
      https://pypi.org/manage/account/publishing/ with:
      owner `JuanjoFuchs`, repository `voice-tunnel`, workflow `release.yml`,
      environment `release`.
- [ ] Create a PyPI API token scoped to the project and add it as repository secret
      `PYPI_API_TOKEN`.

  > **Known gotcha, carried over from ccburn and re-confirmed by agent-mail-cli:** the
  > pending-publisher flow fails on a first upload. The proven path is a one-time API token for
  > the first release, then a **normal** Trusted Publisher added to the now-existing project. A
  > pending publisher does not activate once a token-created project exists — agent-mail-cli hit
  > exactly this and its v0.1.2 release failed with
  > `invalid-pending-publisher: valid token, but project already exists`.

### 4. WinGet

- [ ] Generate a GitHub Personal Access Token with `public_repo` scope and add it as repository
      secret `WINGET_TOKEN`. This is what opens the PR against `microsoft/winget-pkgs`.

### 5. npm — trusted publishing, and one manual first publish

**No `NPM_TOKEN`, deliberately.** The account has 2FA set to `auth-and-writes`, which a
token-based CI publish cannot satisfy; automation tokens used to bypass that and npm is
deprecating the bypass. The workflow authenticates by OIDC instead, so there is no secret.

- [x] npm CLI logged in as `juanjofuchs`.
- [ ] **After** PyPI `0.1.0` is live, publish npm `0.1.0` once by hand — a trusted publisher is
      configured on a package settings page and npm has no pending-publisher equivalent, so the
      name must exist before it can be authorized:

      cd npm && npm version 0.1.0 --no-git-tag-version && npm publish --access public

      It will ask for an OTP. Order matters: the postinstall pins `voice-tunnel==0.1.0`, so
      publishing npm before PyPI ships a package that installs cleanly and then fails for
      everyone.
- [ ] On https://www.npmjs.com/package/@juanjofuchs/voice-tunnel — Settings -> Trusted Publisher,
      add: owner `JuanjoFuchs`, repository `voice-tunnel`, workflow `npm-publish.yml`.
- [ ] Every release after 0.1.0 is then fully automated.

### 6. The go signal

- [ ] Say go. Nothing is published until a `vX.Y.Z` tag is pushed, and the tag is the last step.

## Key Decisions

### The distribution name matches the command, which agent-mail could not manage

PyPI rejected `agent-mail` as too similar to an existing project, so that tool carries a
`agent-mail-cli` distribution with an `agent-mail` command — a permanent explanation in its
README. `voice-tunnel` was verified free on PyPI and npm before the rename, so all three names
agree and nothing needs explaining.

### A Windows `--onedir` bundle, not the `--onefile` binaries the prior art uses

agent-mail-cli and ccburn attach four `--onefile` PyInstaller binaries to each release, and
WinGet installs the Windows one as a portable. **`--onefile` cannot be used here, and the reason
is a measurement, not a size concern** — a onefile bootloader unpacks the whole archive to a temp
directory on *every invocation*, and this CLI is invoked several times per conversational turn.
Measured below: 4.5-14.9 s per command against 0.25-0.59 s for the pip console script.

`--onedir` does not unpack, starts in 485 ms, and carries the complete stack. WinGet's `zip`
installer type takes a nested portable, so the Windows bundle ships as a zip.

Binaries are **Windows-only**. They exist to serve the one audience pip does not — Windows users
without Python, which is who WinGet is for. macOS and Linux users get `pipx`, and a per-platform
binary matrix for them would be build cost with no audience behind it.

### Extras rather than a fat default install

`faster-whisper` and SAPI give a working tunnel with no extra native runtime. Parakeet and Piper
are each a large native dependency, and a first `pip install` that pulls both for backends the
user has not chosen is a worse introduction than one that works immediately and upgrades on
request. `voice-tunnel download` tells the user when a model needs an extra it does not have.

### Runtime state is per-user, not per-install

Established in the packaging work already committed: `.env`, `sessions/` and `models/` resolve to
platform user directories once installed, and stay repo-local in a checkout. Writing a 631 MB
model into `site-packages` would be destroyed by the next upgrade and refused outright on a
read-only install.

### Publishing is gated on a human go

The launch video and the published artifact must describe the same tool. Everything up to the tag
is automated and verifiable; the tag is manual and deliberate.

## Findings — implementer

### Hardware requirements, measured on this machine

Measured with `psutil` RSS on an Intel 20-core desktop CPU, models resident in one process.

| Configuration | Resident memory | Disk (models) | Cold load |
|---|---|---|---|
| Minimum — faster-whisper `base.en` + Windows SAPI | **219 MB** | ~150 MB | 10.3 s |
| Recommended — Parakeet + Piper + voiceprint | **~1.0 GB** | **788 MB** | 5.8 s / 6.7 s |

Per-model disk: Parakeet int8 631 MB, TitaNet 96.7 MB, a `medium` Piper voice 60.3 MB, a `high`
voice 109-115 MB.

**No GPU, and there is no code path that would use one.** Every model is CPU inference by
construction. Speech recognition real-time factor on this machine, warm, best of five over a
7.4 s clip: **RTF 0.114** — 7.4 s of audio transcribed in 0.85 s.

> **A measurement error worth recording, because the repo already has the lesson and it still
> caught me.** The first RTF measurement read **0.229**, taken while a PyInstaller build was
> saturating the CPU in the background. Re-measured on an idle machine it is **0.114** — a 2×
> error, in the direction that would have put a wrong minimum-hardware claim into the README.
> `scripts/uitest.py` already carries "don't instrument the signal you're measuring" as a code
> comment about audio; it applies to CPU-bound timings just as completely.

### PyInstaller: measured, and the first two conclusions were wrong

Three assumptions were written into this spec before anything was built. Two of them did not
survive contact with a build, which is recorded here because the wrong version is the intuitive
one and someone will re-derive it.

**Assumption 1 — "the bundle will be enormous." False.** `--collect-all piper` genuinely does
drag `piper.train` -> `torch` -> `pytorch_lightning` into the analysis, and the first build
walked `piper.train.export_onnx` and `piper.train.infer_torch` before it was killed. But that is
one exclude away: `--exclude-module piper.train --exclude-module torch` produces a **56 MB**
onefile, or **304 MB** as a onedir.

**Assumption 2 — "a binary has no pip, so extras are unreachable and users are stuck on whisper
and SAPI." False.** The bundle carries every backend. Verified by running it against a populated
models directory:

```
tts_backend        piper (resident)     <- the ONNX voice loaded inside the frozen binary
asr                parakeet             <- sherpa-onnx importable, model selected
voiceprint_available  True              <- TitaNet embedder works
asr (no models)    whisper base.en      <- the clean-machine default path also works
```

**Assumption 3 — startup. This is the one that actually decides it, and it was not on the list.**

| Build | Size | Startup (cold / warm) |
|---|---|---|
| `--onefile` | 56 MB | 14.9 s / 4.5 s |
| `--onedir` | 304 MB | 5.3 s / **0.49 s** |
| pip console script | — | 0.59 s / **0.25 s** |

A onefile bootloader unpacks its whole archive to a temp directory **on every invocation**. For a
tool invoked once that is a shrug; **this CLI is invoked several times per conversational turn**
— `watch`, `consumed`, `say`, `watch` again — so a 4.5 s floor would add seconds to every spoken
reply. Latency to the agent is already the dominant cost in this system, measured repeatedly at
15-30 s per turn; adding a fixed 4.5 s tax per CLI call would be the single worst regression
available. `--onedir` does not unpack and lands within a quarter-second of the pip path.

**What survives from the original objections:** unsigned PyInstaller output is flagged by Windows
Defender's ML heuristic — `@juanjofuchs/agent-mail`'s launcher carries a hardcoded
`Program:Win32/Wacapew.A!ml` quarantine hint — so that support burden is inherited and the README
must mention it.

### A `doctor` defect surfaced by the binary, unrelated to packaging

Running `doctor` from the bundle with `VOICE_TUNNEL_TTS=piper` reported
`FAIL tts :: piper bin=(not found)` while `voices` simultaneously reported
`backend: piper (resident, not yet loaded)` and synthesis worked. The check requires
`piper_bin`, which the **resident in-process path does not use at all** and which has been the
default since the TTS latency work. The check fails a working configuration, and it would do so
for any pip user who installed `voice-tunnel[piper]` without a `piper.exe` on PATH — which is
every one of them.

## Implementation Tasks

### Packaging

- [x] `pyproject.toml` with core dependencies, `[piper]` / `[parakeet]` / `[all]` extras, the
      `voice-tunnel` console script, and `voice_tunnel/web/*.html` as package data.
- [x] MIT `LICENSE`.
- [x] Runtime paths resolve per-user when installed and repo-local in a checkout.
- [x] The client page ships inside the package.
- [ ] `python -m voice_tunnel` entry point.
- [ ] Version authored in exactly one place, readable by the release workflow.

### CI/CD workflows

- [ ] `ci.yml` — lint, unit tests on Linux/macOS/Windows, and a build check on every push and PR.
- [ ] `release.yml` — validate the tag against package metadata, build wheel and sdist, publish
      to PyPI, and create the GitHub Release with both artifacts attached.
- [ ] `winget-init.yml` — one-time submission of `JuanjoFuchs.voice-tunnel` to
      `microsoft/winget-pkgs`.
- [ ] `winget-publish.yml` — subsequent version submissions on each release.

### Documentation

- [ ] Rewrite `README.md` for the end user who wants to run this, following the agent-mail-cli
      shape: badges, one-command hook, why, installation per channel, usage, hardware
      requirements.
- [ ] State the measured hardware requirements and that no GPU is needed.
- [ ] Update `AGENTS.md` and `PROJECT_UNDERSTANDING.md` for the packaging layout.

### Release

- [ ] Everything above verified locally and in CI, with no tag pushed.
- [ ] **HOLD.** Await the explicit go.
- [ ] Push `v0.1.0`, verify PyPI and the GitHub Release, then run the WinGet submission.
- [ ] Complete the PyPI Trusted Publishing cleanup: add a normal Trusted Publisher, remove the
      token fallback from the workflow, delete the `PYPI_API_TOKEN` secret.

## Acceptance Criteria

### Build artifacts

- [ ] **AC1**: `ruff check` and `pytest` pass in CI on Linux, macOS and Windows without a
      microphone, a GPU, or any downloaded model. `integration`
- [ ] **AC2**: `python -m build` produces `voice_tunnel-X.Y.Z-py3-none-any.whl` and
      `voice_tunnel-X.Y.Z.tar.gz`. `integration`
- [ ] **AC3**: A tag push creates a GitHub Release carrying the wheel and the sdist. `manual` —
      requires a real tag against the real repository.

### Installed behaviour

- [ ] **AC4**: `python -m voice_tunnel describe` and `voice-tunnel describe` produce identical
      output. `integration`
- [ ] **AC5**: `pip install voice-tunnel` on a clean Python 3.10+ environment puts
      `voice-tunnel` on PATH and `describe` returns the documented schema. `integration` for a
      local wheel; `manual` from PyPI.
- [ ] **AC6**: `pipx install voice-tunnel && voice-tunnel describe` works. `manual`
- [ ] **AC7**: `pipx run voice-tunnel describe` works with no persistent install. `manual`
- [ ] **AC8**: A core install reports `sapi`/`whisper`; after `pip install voice-tunnel[all]` and
      the corresponding downloads, it reports `piper`/`parakeet`. `integration`
- [ ] **AC9**: An installed package resolves `.env`, sessions and models to per-user directories,
      and writes nothing inside its own installation directory. `unit`
- [ ] **AC10**: `voice-tunnel doctor` on a clean install passes every check except
      `shim_on_path`, and no check names a path inside the package. `integration`
- [ ] **AC11**: An installed `voice-tunnel serve` returns the client page over HTTP with status
      200 and a non-trivial body. `integration`

### WinGet

- [ ] **AC12**: The WinGet submission workflow opens a PR to `microsoft/winget-pkgs` for
      `JuanjoFuchs.voice-tunnel`. `manual`
- [ ] **AC13**: After Microsoft approval, `winget install JuanjoFuchs.voice-tunnel` on a clean
      Windows machine makes `voice-tunnel describe` work from any directory. `manual`
- [ ] **AC14**: A later release does not leave duplicate entries; `winget list voice-tunnel`
      returns exactly one row. `manual`

### Release integrity

- [ ] **AC15**: Pushing `vX.Y.Z` where `X.Y.Z` differs from package metadata fails the workflow
      before any artifact is built or uploaded. `integration`
- [ ] **AC16**: The first PyPI release publishes with `PYPI_API_TOKEN` and
      `https://pypi.org/project/voice-tunnel/` is live. `manual`
- [ ] **AC17**: After the token secret is deleted and the fallback removed, the next release
      publishes through OIDC with no password field in the publish step. `manual`

### Documented claims

- [ ] **AC18**: The README states the measured memory, disk and CPU requirements and that no GPU
      is required. `manual`
- [ ] **AC19**: No published artifact contains a model file; the wheel and sdist are each under
      1 MB. `integration`

## Testing Approach

### Local validation

```bash
venv/Scripts/python -m ruff check voice_tunnel/ tests/ scripts/
venv/Scripts/python -m pytest tests/ -q
venv/Scripts/python scripts/layout.py
venv/Scripts/python -m build
venv/Scripts/python -m twine check dist/*
```

### Clean-install validation

A wheel built locally, installed into a fresh virtualenv outside the repository, exercised for
`describe`, `doctor`, `config path`, `serve`, and an HTTP `GET /`. This is what caught the
`WEB_DIR` defect that every in-repo test passed over.

### Release validation

After a tag push: the GitHub Release carries both artifacts, PyPI shows the version, and a clean
`pipx run voice-tunnel describe` returns the schema.

### Test Cases

| Input | Expected |
|---|---|
| `python -m build` | wheel + sdist, both under 1 MB |
| wheel installed in a clean venv, `voice-tunnel describe` | documented schema, exit 0 |
| clean install, `voice-tunnel config path` | a path under the per-user config dir, never site-packages |
| clean install, `GET /?token=…` | 200, the client page |
| tag `v9.9.9` against metadata `0.1.0` | workflow fails before build |
| core install, `voice-tunnel download voice` | succeeds, and says piper-tts is also needed |

## Out of Scope

- PyInstaller binaries and any zero-Python install path. Rejected with reasons in Findings.
- Homebrew, Scoop, Chocolatey, AUR, Docker.
- Code signing and notarization.
- Publishing the models anywhere; they are fetched from their existing upstreams at runtime.
- npm and `npx`, which is spec 003.
- Any change to spec 001 tunnel behaviour.

## References

- `specs/001-voice-tunnel.md` — the behaviour being packaged
- `agent-mail-cli`'s own packaging spec — the prior art this borrows from
- `agent-mail-cli`'s workflows — the proven implementations
- `ai-docs/reference/security.md` — transport trust, which the README must not overstate
