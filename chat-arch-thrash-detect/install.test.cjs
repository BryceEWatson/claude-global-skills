/**
 * Tests for install.cjs / uninstall.cjs round-trips of the
 * chat-arch-thrash-detect PostToolUse hook.
 *
 * Run: node --test install.test.cjs uninstall.test.cjs post-tool-use-hook.test.cjs
 *
 * Scripts are spawned as subprocesses with HOME overridden to a
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
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'thrash-detect-install-test-'));
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

function readManifest(home) {
  const p = path.join(home, '.claude', 'skills', 'chat-arch-thrash-detect', '.local-state', 'install-manifest.json');
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
  assert.ok(Array.isArray(settings.hooks.PostToolUse));
  assert.equal(settings.hooks.PostToolUse.length, 1);
  assert.equal(settings.hooks.PostToolUse[0].matcher, '*');
  assert.equal(settings.hooks.PostToolUse[0].hooks[0].type, 'command');
  assert.match(settings.hooks.PostToolUse[0].hooks[0].command, /post-tool-use-hook\.cjs/);
  const manifest = readManifest(home);
  assert.equal(manifest.hookEvent, 'PostToolUse');
  assert.equal(manifest.hookIndex, 0);
});

test('install: preserves prior unrelated hooks', () => {
  const home = mkTempHome();
  writeSettings(home, {
    effortLevel: 'high',
    hooks: {
      Stop: [{ matcher: '*', hooks: [{ type: 'command', command: 'echo stop' }] }],
      PostToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command', command: 'echo bash' }] }],
    },
  });
  const r = runScript(INSTALL, home);
  assert.equal(r.status, 0, `install failed: ${r.stderr}`);
  const settings = readSettings(home);
  assert.equal(settings.effortLevel, 'high');
  assert.equal(settings.hooks.Stop.length, 1);
  assert.equal(settings.hooks.PostToolUse.length, 2);
  const preExisting = settings.hooks.PostToolUse.find(e => e.matcher === 'Bash');
  assert.ok(preExisting);
  assert.equal(preExisting.hooks[0].command, 'echo bash');
});

test('install: idempotent (running twice produces one managed entry)', () => {
  const home = mkTempHome();
  runScript(INSTALL, home);
  runScript(INSTALL, home);
  const settings = readSettings(home);
  const managed = settings.hooks.PostToolUse.filter(e =>
    Array.isArray(e.hooks) && e.hooks.some(h => /post-tool-use-hook\.cjs/.test(h.command || ''))
  );
  assert.equal(managed.length, 1);
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

test('install: prints a hint about the CHATARCH_THRASH_DETECT env-gate', () => {
  const home = mkTempHome();
  const r = runScript(INSTALL, home);
  assert.equal(r.status, 0);
  assert.match(r.stdout, /CHATARCH_THRASH_DETECT=1/);
});
