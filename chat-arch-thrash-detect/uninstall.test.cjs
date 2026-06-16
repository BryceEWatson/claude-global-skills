/**
 * Tests for the uninstall.cjs round-trip.
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const { test } = require('node:test');
const assert = require('node:assert/strict');

const INSTALL = path.join(__dirname, 'install.cjs');
const UNINSTALL = path.join(__dirname, 'uninstall.cjs');

function mkTempHome() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'thrash-detect-uninstall-test-'));
  fs.mkdirSync(path.join(root, '.claude', 'skills', 'chat-arch-thrash-detect', '.local-state'), { recursive: true });
  return root;
}

function runScript(scriptPath, home) {
  return spawnSync(process.execPath, [scriptPath], {
    env: { ...process.env, HOME: home, USERPROFILE: home },
    encoding: 'utf8',
  });
}

function readSettings(home) {
  const p = path.join(home, '.claude', 'settings.json');
  if (!fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

test('uninstall: no settings.json ⇒ silent success', () => {
  const home = mkTempHome();
  const r = runScript(UNINSTALL, home);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /nothing to uninstall/);
});

test('uninstall: full install → uninstall round-trip removes the managed entry', () => {
  const home = mkTempHome();
  assert.equal(runScript(INSTALL, home).status, 0);
  const beforeSettings = readSettings(home);
  assert.equal(beforeSettings.hooks.PostToolUse.length, 1);
  const r = runScript(UNINSTALL, home);
  assert.equal(r.status, 0, `uninstall failed: ${r.stderr}`);
  const afterSettings = readSettings(home);
  assert.equal(afterSettings.hooks.PostToolUse.length, 0);
  const manifestPath = path.join(
    home, '.claude', 'skills', 'chat-arch-thrash-detect', '.local-state', 'install-manifest.json',
  );
  assert.equal(fs.existsSync(manifestPath), false);
});

test('uninstall: preserves unrelated PostToolUse entries', () => {
  const home = mkTempHome();
  // Pre-existing user entry.
  fs.writeFileSync(
    path.join(home, '.claude', 'settings.json'),
    JSON.stringify({
      hooks: { PostToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: 'echo bash' }] }] },
    }, null, 2),
  );
  assert.equal(runScript(INSTALL, home).status, 0);
  let settings = readSettings(home);
  assert.equal(settings.hooks.PostToolUse.length, 2);
  assert.equal(runScript(UNINSTALL, home).status, 0);
  settings = readSettings(home);
  assert.equal(settings.hooks.PostToolUse.length, 1);
  assert.equal(settings.hooks.PostToolUse[0].hooks[0].command, 'echo bash');
});

test('uninstall: idempotent (second run is a no-op)', () => {
  const home = mkTempHome();
  assert.equal(runScript(INSTALL, home).status, 0);
  assert.equal(runScript(UNINSTALL, home).status, 0);
  const r = runScript(UNINSTALL, home);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /nothing to remove/);
});

test('uninstall: falls back to command-scan when manifest is missing', () => {
  const home = mkTempHome();
  assert.equal(runScript(INSTALL, home).status, 0);
  // Delete the manifest sidecar.
  const manifestPath = path.join(
    home, '.claude', 'skills', 'chat-arch-thrash-detect', '.local-state', 'install-manifest.json',
  );
  fs.unlinkSync(manifestPath);
  const r = runScript(UNINSTALL, home);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /command-scan/);
  const settings = readSettings(home);
  assert.equal(settings.hooks.PostToolUse.length, 0);
});
