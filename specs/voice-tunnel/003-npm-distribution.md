---
id: "003"
title: npm Distribution
status: in_progress
blocked_by: ["002"]
blocks: []
---

# npm Distribution

## Overview

Publish `@juanjofuchs/voice-tunnel` on npm so the tool installs the way most agent tooling does,
alongside the PyPI and WinGet channels in spec 002.

**This package carries no binary.** It creates a private virtualenv inside the package directory
at postinstall and `pip install voice-tunnel==<same version>` into it; the `voice-tunnel` command
is a launcher that passes every argument through. That is a real divergence from
`@juanjofuchs/agent-mail`, which ships PyInstaller executables and downloads them per platform —
and the reason is in spec 002's findings.

> **Completion rule:** This spec is not complete until `npm install -g @juanjofuchs/voice-tunnel`
> on a clean machine produces a working `voice-tunnel describe`, and the failure paths have been
> exercised by removing Python from PATH. A `node --check` pass is insufficient. The agent must
> iterate until verification passes.

## Goals

- `npm install -g @juanjofuchs/voice-tunnel` yields a working `voice-tunnel` command.
- The npm and PyPI versions cannot drift.
- A machine without Python gets a message naming the cause and the fix, not a stack trace.
- Nothing outside the package directory is modified, and uninstall leaves nothing behind.

## Requirements

### Functional Requirements

- **FR1**: `npm install -g @juanjofuchs/voice-tunnel` installs and `voice-tunnel describe`
  returns the documented schema.
- **FR2**: The postinstall creates a virtualenv **inside the package directory** and installs
  `voice-tunnel==<package.json version>` into it.
- **FR3**: The launcher forwards every argument and preserves the exit code exactly.
- **FR4**: With no suitable Python, install and invocation both explain the cause and name the
  fix; the install does not fail the whole `npm install`.
- **FR5**: `npm uninstall -g @juanjofuchs/voice-tunnel` removes the virtualenv with the package.

### Non-Functional Requirements

- **NFR1**: The npm publish fails if the matching PyPI version is not live.
- **NFR2**: No global Python environment is modified.

### Technical Constraints

- **TC1**: npm package name `@juanjofuchs/voice-tunnel`; command `voice-tunnel`. Scoped because
  the unscoped name is somebody's to lose and the scope is already established by
  `@juanjofuchs/agent-mail`.
- **TC2**: Node 16+.
- **TC3**: Python 3.10+ on PATH is a prerequisite, discovered as `py -3` on Windows and
  `python3` / `python` elsewhere.
- **TC4**: The pip install is version-pinned to the npm package version.
- **TC5**: stdio is inherited by the child process, never piped.

### Requirement Traceability

| Requirement | Acceptance Criteria |
|---|---|
| FR1 | AC1, AC2 |
| FR2 | AC3, AC4 |
| FR3 | AC5, AC6 |
| FR4 | AC7, AC8 |
| FR5 | AC9 |
| NFR1 | AC10 |
| NFR2 | AC3 |
| TC1 | AC1 |
| TC3 | AC7 |
| TC4 | AC4 |
| TC5 | AC6 |

## Key Decisions

### A launcher, not a bundled binary

`@juanjofuchs/agent-mail` is pure stdlib, so PyInstaller turns it into a ~10 MB executable per
platform and npm ships those. Here the equivalent is a 304 MB `--onedir` tree per platform, and
a `--onefile` costs 4.5-14.9 s of unpacking on **every invocation** — measured in spec 002, and
disqualifying for a CLI invoked several times per conversational turn. Requiring a real Python is
the honest trade, and the README says so rather than implying npm makes Python unnecessary.

### A private virtualenv, never the ambient one

`npm install -g` must not mutate a user's Python environment. Everything lives under the package
directory and disappears with `npm uninstall`.

### The pip install is pinned

Unpinned, `npm install @juanjofuchs/voice-tunnel@0.1.0` fetches whatever PyPI has latest, so the
version asked for and the version received can differ — and a bad PyPI release reaches backwards
into every npm version already published. Pinned, the two registries carry the same number or the
install fails loudly.

### Trusted publishing, and why the FIRST publish cannot use it

npm now supports OIDC trusted publishing from GitHub Actions, and it is the right mechanism here
for a reason specific to this account: **2FA is set to `auth-and-writes`**, which a token-based
CI publish cannot satisfy. Automation tokens used to bypass that check, and npm is deprecating
exactly that bypass — the web UI now steers you to trusted publishing when you try to create a
granular token. OIDC leaves no secret to leak, rotate, or forget to delete.

Requirements, confirmed from npm's documentation: **npm >= 11.5.1, Node >= 22.14.0,
`id-token: write`, and `registry-url` set on `setup-node`.** No `NODE_AUTH_TOKEN`.

**The constraint that shapes the release sequence: a trusted publisher is configured on a
PACKAGE's settings page, and npm has no equivalent of PyPI's pending publisher.** There is no way
to pre-authorize a name that has never been published. So `@juanjofuchs/voice-tunnel@0.1.0` must
be published once, interactively, with an OTP; every version after that is automated.

This is the same shape as the PyPI problem agent-mail-cli recorded — first release by hand,
steady state by OIDC — arrived at from the opposite direction. Worth stating plainly rather than
discovering during a launch.

### A postinstall failure is not fatal

npm renders a postinstall failure as a red block, and every likely cause here — no Python, an old
Python, no network — is a one-minute fix. The postinstall reports and exits 0; the launcher
re-checks and repeats the guidance at the moment it is actually needed, which is when the user
runs a command rather than when they walked away from an install.

## Implementation Tasks

- [x] `npm/package.json` with the scoped name, `bin`, `files`, and the postinstall hook.
- [x] `npm/scripts/postinstall.js` — interpreter discovery, private venv, pinned pip install.
- [x] `npm/bin/voice-tunnel.js` — pass-through launcher with inherited stdio and exit-code
      preservation.
- [x] `npm/README.md` stating the Python prerequisite plainly.
- [x] `npm/LICENSE`.
- [x] Version pinning between `package.json` and the pip install.
- [x] `npm-publish.yml` with the PyPI-exists precondition.
- [ ] Verify a real `npm install -g` from a packed tarball.
- [ ] Verify the no-Python path by making the interpreter unfindable.

## Acceptance Criteria

- [ ] **AC1**: `npm install -g @juanjofuchs/voice-tunnel` then `voice-tunnel describe` returns
      the documented schema. `manual` — needs a published package.
- [ ] **AC2**: The same, from `npm pack` output installed locally. `integration`
- [ ] **AC3**: After install, a virtualenv exists inside the package directory and no global
      `pip list` entry for `voice-tunnel` appears. `integration`
- [ ] **AC4**: The version installed into the private venv equals the npm package version.
      `integration`
- [ ] **AC5**: `voice-tunnel doctor` through the launcher matches the same command run directly
      against the venv's console script. `integration`
- [ ] **AC6**: Exit codes are preserved: a command that exits 3 through the launcher exits 3.
      `integration`
- [ ] **AC7**: With no Python 3.10+ discoverable, both the postinstall and the launcher print a
      message naming Python as the cause and `npm rebuild` as the fix. `integration`
- [ ] **AC8**: A postinstall failure leaves `npm install` exiting 0. `integration`
- [ ] **AC9**: `npm uninstall -g` removes the package directory including the venv. `manual`
- [ ] **AC10**: The publish workflow refuses to publish when the matching PyPI version is absent.
      `integration` — assert the check's logic against a version that does not exist.

## Testing Approach

### Local validation

```bash
node --check npm/bin/voice-tunnel.js
node --check npm/scripts/postinstall.js
cd npm && npm pack
npm install -g ./juanjofuchs-voice-tunnel-*.tgz
voice-tunnel describe
```

### Failure-path validation

Run the postinstall and the launcher with `PATH` stripped of every Python, and confirm both name
Python as the cause. This is the path most likely to be hit by a real user and the least likely
to be exercised by accident.

### Test Cases

| Input | Expected |
|---|---|
| `npm install -g <tarball>` on a machine with Python | working `voice-tunnel describe` |
| the same with no Python on PATH | install exits 0, prints the Python prerequisite |
| `voice-tunnel` with no venv present | names the cause, suggests `npm rebuild`, exits 1 |
| a command that exits 3 | the launcher exits 3 |
| publish workflow, PyPI version absent | fails before `npm publish` |

## Out of Scope

- Bundling a Python interpreter or any PyInstaller artifact.
- `npx` one-shot execution. Each invocation would build a virtualenv and pip-install several
  hundred megabytes of native wheels, which is not a one-shot experience anyone wants. This is
  a real divergence from agent-mail-cli, whose `npx -y @juanjofuchs/agent-mail describe` is its
  headline and works because that package is a 10 MB self-contained binary.
- Publishing an unscoped `voice-tunnel` npm name.
- Windows Defender submission for the WinGet bundle.

## References

- `specs/002-packaging.md` — PyPI, GitHub Releases, WinGet, and the measurements behind the
  no-binary decision
- `agent-mail-cli`'s own npm spec — the prior art
- `npm/README.md` — the user-facing statement of the Python prerequisite
