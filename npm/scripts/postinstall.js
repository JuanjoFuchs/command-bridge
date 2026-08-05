#!/usr/bin/env node
/**
 * Build a private virtualenv beside this package and pip-install voice-tunnel into it.
 *
 * WHY THIS IS NOT A BUNDLED BINARY, unlike the sibling `agent-mail` package.
 *
 * agent-mail is pure stdlib, so PyInstaller turns it into a ~10 MB executable and npm ships that.
 * voice-tunnel depends on aiohttp, numpy and faster-whisper, and optionally on onnxruntime and
 * sherpa-onnx — native wheels, per-platform, hundreds of megabytes together. A PyInstaller bundle
 * of that is enormous, slow to start, and reliably flagged by Windows Defender's ML heuristic
 * (agent-mail's own launcher carries a quarantine hint for exactly that reason). Shipping a real
 * Python install instead is the honest trade: npm becomes a convenient front door, not a way to
 * pretend Python is absent.
 *
 * A PRIVATE venv rather than installing into whatever `python` happens to be first on PATH,
 * because `npm install -g` must never mutate a user's system or active environment. Everything
 * this creates lives under the package directory and disappears with `npm uninstall`.
 *
 * FAILING HERE IS NOT FATAL. npm surfaces a postinstall failure as a scary red block, and the
 * common causes — no Python, an old Python, no network — are all things the user can fix in a
 * minute. So this reports what is wrong and exits 0; the launcher re-checks and prints the same
 * guidance at the moment it is actually needed.
 */
'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const PKG_ROOT = path.resolve(__dirname, '..');
const VENV = path.join(PKG_ROOT, '.venv');
const MIN_PYTHON = [3, 10];
const PKG_VERSION = require('../package.json').version;

/** Where a venv puts its executables, which differs by platform and is a classic silent break. */
function venvBin(name) {
  return process.platform === 'win32'
    ? path.join(VENV, 'Scripts', `${name}.exe`)
    : path.join(VENV, 'bin', name);
}

/** Candidate interpreters, best first. `py -3` is the Windows launcher and is often the only
 *  thing on PATH there; `python3` is the POSIX convention; bare `python` is last because on some
 *  systems it is still Python 2 or a Microsoft Store shim that does nothing. */
function pythonCandidates() {
  return process.platform === 'win32'
    ? [['py', ['-3']], ['python', []], ['python3', []]]
    : [['python3', []], ['python', []]];
}

function probe(cmd, prefix) {
  const r = spawnSync(cmd, [...prefix, '-c',
    'import sys; print("%d.%d" % sys.version_info[:2])'], { encoding: 'utf8' });
  if (r.error || r.status !== 0) return null;
  const [major, minor] = (r.stdout || '').trim().split('.').map(Number);
  if (!Number.isFinite(major) || !Number.isFinite(minor)) return null;
  const ok = major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1]);
  return ok ? { cmd, prefix, version: `${major}.${minor}` } : null;
}

function findPython() {
  for (const [cmd, prefix] of pythonCandidates()) {
    const found = probe(cmd, prefix);
    if (found) return found;
  }
  return null;
}

function run(cmd, args, label) {
  const r = spawnSync(cmd, args, { stdio: 'inherit' });
  if (r.error || r.status !== 0) {
    throw new Error(`${label} failed${r.error ? `: ${r.error.message}` : ` (exit ${r.status})`}`);
  }
}

function main() {
  const py = findPython();
  if (!py) {
    console.error(
      `\nvoice-tunnel needs Python ${MIN_PYTHON.join('.')}+ and could not find it.\n` +
      `This package is a launcher around a Python program — it does not bundle an interpreter.\n` +
      `Install Python from https://python.org, then re-run:  npm rebuild @juanjofuchs/voice-tunnel\n`
    );
    return; // exit 0 on purpose — see the header
  }

  try {
    if (!fs.existsSync(venvBin('python'))) {
      console.log(`voice-tunnel: creating a private environment (Python ${py.version})`);
      run(py.cmd, [...py.prefix, '-m', 'venv', VENV], 'venv creation');
    }
    console.log('voice-tunnel: installing (this pulls native wheels and takes a minute)');
    run(venvBin('python'), ['-m', 'pip', 'install', '--quiet', '--upgrade', 'pip'], 'pip upgrade');
    // PINNED to this package's own version. Unpinned, `npm install @juanjofuchs/voice-tunnel@0.1.0`
    // would fetch whatever PyPI has latest — so the version a user asked for and the version they
    // get could differ, and a bad PyPI release would reach back into every previously-published
    // npm version. The two registries carry the same number or the install fails loudly.
    run(venvBin('python'),
        ['-m', 'pip', 'install', '--quiet', `voice-tunnel==${PKG_VERSION}`], 'pip install');
    console.log(
      '\nvoice-tunnel installed. Next:\n' +
      '  voice-tunnel doctor              what is missing, and the command that fixes it\n' +
      '  voice-tunnel download --list     optional models: a neural voice, faster ASR\n' +
      '  voice-tunnel serve --wake claude use YOUR agent\'s name\n'
    );
  } catch (err) {
    console.error(
      `\nvoice-tunnel: setup did not complete — ${err.message}\n` +
      `Fix the cause and re-run:  npm rebuild @juanjofuchs/voice-tunnel\n` +
      `Or skip npm entirely:      pip install voice-tunnel\n`
    );
  }
}

module.exports = { venvBin, findPython, VENV, MIN_PYTHON };

if (require.main === module) main();
