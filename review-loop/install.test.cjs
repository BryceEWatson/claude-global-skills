/**
 * Tests for install.cjs / uninstall.cjs round-trips.
 *
 * Run: node --test install.test.cjs uninstall.test.cjs stop-hook.test.cjs
 *
 * Tests spawn the scripts as subprocesses with HOME overridden to a
 * temp dir — exercising them exactly as the user will.
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

function readManifest(home) {
  const p = path.join(home, '.claude', 'skills', 'review-loop', '.local-state', 'install-manifest.json');
  if (!fs.existsSync(p)) return null;
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

function writeSettings(home, json) {
  const p = path.join(home, '.claude', 'settings.json');
  fs.writeFileSync(p, JSON.stringify(json, null, 2));
}

test('install: empty settings.json (file absent)', () => {
  const home = mkTempHome();
  const r = runScript(INSTALL, home);
  assert.equal(r.status, 0, `install failed: ${r.stderr}`);
  const settings = readSettings(home);
  assert.ok(settings.hooks);
  assert.ok(Array.isArray(settings.hooks.Stop));
  assert.equal(settings.hooks.Stop.length, 1);
  assert.equal(settings.hooks.Stop[0].matcher, '*');
  assert.equal(settings.hooks.Stop[0].hooks[0].type, 'command');
  assert.match(settings.hooks.Stop[0].hooks[0].command, /stop-hook\.cjs/);
  const manifest = readManifest(home);
  assert.equal(manifest.stopIndex, 0);
});

test('install: preserves prior unrelated hooks', () => {
  const home = mkTempHome();
  writeSettings(home, {
    effortLevel: 'high',
    theme: 'dark',
    hooks: {
      PostToolUse: [{ matcher: '*', hooks: [{ type: 'command', command: 'echo foo' }] }],
      Stop: [{ matcher: 'Bash', hooks: [{ type: 'command', command: 'echo bar' }] }],
    },
  });
  const r = runScript(INSTALL, home);
  assert.equal(r.status, 0, `install failed: ${r.stderr}`);
  const settings = readSettings(home);
  assert.equal(settings.effortLevel, 'high');
  assert.equal(settings.theme, 'dark');
  assert.equal(settings.hooks.PostToolUse.length, 1);
  assert.equal(settings.hooks.PostToolUse[0].hooks[0].command, 'echo foo');
  assert.equal(settings.hooks.Stop.length, 2);
  // Pre-existing Stop entry survives.
  const preExisting = settings.hooks.Stop.find(e => e.matcher === 'Bash');
  assert.ok(preExisting);
  assert.equal(preExisting.hooks[0].command, 'echo bar');
});

test('install: idempotent (running twice produces one managed entry)', () => {
  const home = mkTempHome();
  runScript(INSTALL, home);
  runScript(INSTALL, home);
  const settings = readSettings(home);
  const managedEntries = settings.hooks.Stop.filter(e =>
    Array.isArray(e.hooks) && e.hooks.some(h => /stop-hook\.cjs/.test(h.command || ''))
  );
  assert.equal(managedEntries.length, 1);
});

test('install: writes a .bak when settings.json exists', () => {
  const home = mkTempHome();
  writeSettings(home, { theme: 'dark' });
  const r = runScript(INSTALL, home);
  assert.equal(r.status, 0);
  const claudeDir = path.join(home, '.claude');
  const baks = fs.readdirSync(claudeDir).filter(n => /^settings\.json\.bak-\d+$/.test(n));
  assert.ok(baks.length >= 1, 'expected at least one .bak file');
});

test('install: malformed settings.json aborts with non-zero, leaves original untouched', () => {
  const home = mkTempHome();
  const settingsPath = path.join(home, '.claude', 'settings.json');
  fs.writeFileSync(settingsPath, '{not valid json}');
  const r = runScript(INSTALL, home);
  assert.notEqual(r.status, 0);
  assert.equal(fs.readFileSync(settingsPath, 'utf8'), '{not valid json}');
});
