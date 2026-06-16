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
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'review-loop-test-'));
  fs.mkdirSync(path.join(root, '.claude', 'skills', 'review-loop', '.local-state'), { recursive: true });
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

function writeSettings(home, json) {
  fs.writeFileSync(path.join(home, '.claude', 'settings.json'), JSON.stringify(json, null, 2));
}

test('uninstall: no settings.json is a no-op', () => {
  const home = mkTempHome();
  const r = runScript(UNINSTALL, home);
  assert.equal(r.status, 0, `uninstall failed: ${r.stderr}`);
});

test('uninstall: removes managed entry, preserves unrelated Stop hooks', () => {
  const home = mkTempHome();
  // First install.
  let r = runScript(INSTALL, home);
  assert.equal(r.status, 0);
  // Add an unrelated Stop hook AFTER the managed one.
  const settings = readSettings(home);
  settings.hooks.Stop.push({ matcher: 'Bash', hooks: [{ type: 'command', command: 'echo bar' }] });
  writeSettings(home, settings);

  r = runScript(UNINSTALL, home);
  assert.equal(r.status, 0, `uninstall failed: ${r.stderr}`);
  const after = readSettings(home);
  assert.equal(after.hooks.Stop.length, 1);
  assert.equal(after.hooks.Stop[0].matcher, 'Bash');
  assert.equal(after.hooks.Stop[0].hooks[0].command, 'echo bar');
});

test('uninstall: idempotent (second uninstall is a no-op)', () => {
  const home = mkTempHome();
  runScript(INSTALL, home);
  const r1 = runScript(UNINSTALL, home);
  assert.equal(r1.status, 0);
  const r2 = runScript(UNINSTALL, home);
  assert.equal(r2.status, 0);
});

test('uninstall: install + uninstall + install = clean reinstall', () => {
  const home = mkTempHome();
  runScript(INSTALL, home);
  runScript(UNINSTALL, home);
  runScript(INSTALL, home);
  const settings = readSettings(home);
  const managedEntries = settings.hooks.Stop.filter(e =>
    Array.isArray(e.hooks) && e.hooks.some(h => /stop-hook\.cjs/.test(h.command || ''))
  );
  assert.equal(managedEntries.length, 1);
});

test('uninstall: command-string fallback when manifest is missing', () => {
  const home = mkTempHome();
  runScript(INSTALL, home);
  // Delete the manifest to force fallback path.
  const manifestPath = path.join(home, '.claude', 'skills', 'review-loop', '.local-state', 'install-manifest.json');
  fs.unlinkSync(manifestPath);
  const r = runScript(UNINSTALL, home);
  assert.equal(r.status, 0, `uninstall failed: ${r.stderr}`);
  const settings = readSettings(home);
  const managedEntries = (settings.hooks && settings.hooks.Stop || []).filter(e =>
    Array.isArray(e.hooks) && e.hooks.some(h => /stop-hook\.cjs/.test(h.command || ''))
  );
  assert.equal(managedEntries.length, 0);
});
