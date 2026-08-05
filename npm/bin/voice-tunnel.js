#!/usr/bin/env node
/**
 * Hand every argument to the real CLI in the private venv, and get out of the way.
 *
 * Deliberately thin. This program's entire contract is "`voice-tunnel <cmd>` behaves the same
 * however you installed it", and every line of cleverness here is a way for the npm path to
 * behave differently from the pip path — which is the one bug this wrapper must not have.
 *
 * `stdio: 'inherit'` matters more than it looks. `serve` is long-running and prints a banner
 * carrying the token, which is the only way to open the page; `watch` BLOCKS until someone
 * speaks. Buffering or re-encoding either would break the tool's actual use.
 */
'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');

const { venvBin, findPython, MIN_PYTHON } = require('../scripts/postinstall.js');

const REPAIR = 'npm rebuild @juanjofuchs/voice-tunnel';

function fail(message) {
  console.error(`\nvoice-tunnel: ${message}\n`);
  process.exit(1);
}

const exe = venvBin('voice-tunnel');
if (!fs.existsSync(exe)) {
  // Distinguish the two causes, because they need different fixes and a bare "not found" sends
  // people to the wrong one. No Python at all is a prerequisite problem; Python present but no
  // console script means the install stage failed or was interrupted.
  if (!findPython()) {
    fail(
      `Python ${MIN_PYTHON.join('.')}+ is required and was not found.\n` +
      `This package is a launcher around a Python program; it does not bundle an interpreter.\n` +
      `Install Python from https://python.org, then run:  ${REPAIR}`
    );
  }
  fail(
    `the private environment is missing or incomplete.\n` +
    `Run:  ${REPAIR}\n` +
    `Or install directly, which needs no npm at all:  pip install voice-tunnel`
  );
}

const result = spawnSync(exe, process.argv.slice(2), { stdio: 'inherit' });
if (result.error) {
  fail(`could not start ${exe}: ${result.error.message}\nTry:  ${REPAIR}`);
}
// Preserve the exit code exactly — `describe` documents 0/1/2/3 and agents branch on them, so
// collapsing them here would break the contract this whole CLI is built around.
process.exit(result.status === null ? 1 : result.status);
