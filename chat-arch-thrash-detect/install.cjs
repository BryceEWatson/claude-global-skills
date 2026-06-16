#!/usr/bin/env node
/**
 * Install the chat-arch thrash-detector PostToolUse hook into
 * ~/.claude/settings.json.
 *
 * Sidecar-manifest pattern (mirrors review-loop): managed identity is
 * exact-string match on the hook command. Manifest at
 *   ~/.claude/skills/chat-arch-thrash-detect/.local-state/install-manifest.json
 *
 * Idempotent: running twice yields exactly one managed PostToolUse entry.
 *
 * Safety:
 *   - Parses + validates settings.json before any write.
 *   - Backs up to settings.json.bak-<unix-ts> before writing (keep last 3).
 *   - Atomic write via tmp + rename.
 *   - Aborts non-zero on malformed JSON; original untouched.
 *
 * Usage:
 *   node ~/.claude/skills/chat-arch-thrash-detect/install.cjs
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const SETTINGS_PATH = path.join(os.homedir(), '.claude', 'settings.json');
const MANIFEST_DIR = path.join(os.homedir(), '.claude', 'skills', 'chat-arch-thrash-detect', '.local-state');
const MANIFEST_PATH = path.join(MANIFEST_DIR, 'install-manifest.json');
const HOOK_COMMAND = `node ${path.join('${HOME}', '.claude', 'skills', 'chat-arch-thrash-detect', 'post-tool-use-hook.cjs')}`;
const HOOK_EVENT = 'PostToolUse';
const HOOK_MATCHER = '*';
const MANAGED_VERSION = 1;
const MAX_BACKUPS = 3;

function fail(msg, code = 1) {
  process.stderr.write(`install-thrash-hook: ERROR: ${msg}\n`);
  process.exit(code);
}
function info(msg) { process.stdout.write(`install-thrash-hook: ${msg}\n`); }

function ensureDir(p) { fs.mkdirSync(p, { recursive: true }); }

function readSettings() {
  if (!fs.existsSync(SETTINGS_PATH)) return { existed: false, json: {} };
  const raw = fs.readFileSync(SETTINGS_PATH, 'utf8');
  let parsed;
  try { parsed = JSON.parse(raw); }
  catch (e) { fail(`malformed settings.json (${e.message}); leaving original untouched`); }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    fail('settings.json is not a JSON object; leaving original untouched');
  }
  return { existed: true, json: parsed, raw };
}

function backupSettings(raw) {
  if (raw === undefined) return null;
  const ts = Date.now();
  const dest = `${SETTINGS_PATH}.bak-${ts}`;
  fs.writeFileSync(dest, raw, 'utf8');
  rotateBackups();
  return dest;
}

function rotateBackups() {
  const dir = path.dirname(SETTINGS_PATH);
  const base = path.basename(SETTINGS_PATH);
  const re = new RegExp(`^${base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\.bak-(\\d+)$`);
  const backups = fs.readdirSync(dir)
    .map(name => { const m = re.exec(name); return m ? { name, ts: Number(m[1]) } : null; })
    .filter(Boolean)
    .sort((a, b) => b.ts - a.ts);
  for (const b of backups.slice(MAX_BACKUPS)) {
    try { fs.unlinkSync(path.join(dir, b.name)); } catch (_) { /* ignore */ }
  }
}

function findManagedIndex(settings) {
  const hooks = settings.hooks && Array.isArray(settings.hooks[HOOK_EVENT]) ? settings.hooks[HOOK_EVENT] : null;
  if (!hooks) return -1;
  for (let i = 0; i < hooks.length; i++) {
    const entry = hooks[i];
    if (!entry || !Array.isArray(entry.hooks)) continue;
    if (entry.hooks.some(h => h && h.type === 'command' && h.command === HOOK_COMMAND)) return i;
  }
  return -1;
}

function writeAtomic(targetPath, text) {
  ensureDir(path.dirname(targetPath));
  const tmp = `${targetPath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmp, text, 'utf8');
  fs.renameSync(tmp, targetPath);
}

function buildManagedEntry() {
  return {
    matcher: HOOK_MATCHER,
    hooks: [{ type: 'command', command: HOOK_COMMAND }],
  };
}

function writeManifest(hookIndex, settingsPath) {
  ensureDir(MANIFEST_DIR);
  const data = {
    hookEvent: HOOK_EVENT,
    hookIndex,
    installedAt: new Date().toISOString(),
    version: MANAGED_VERSION,
    settingsPath,
    hookCommand: HOOK_COMMAND,
  };
  writeAtomic(MANIFEST_PATH, JSON.stringify(data, null, 2) + '\n');
}

function main() {
  ensureDir(path.dirname(SETTINGS_PATH));
  const { existed, json: settings, raw } = readSettings();

  const existingIdx = findManagedIndex(settings);
  settings.hooks = settings.hooks && typeof settings.hooks === 'object' ? settings.hooks : {};
  if (!Array.isArray(settings.hooks[HOOK_EVENT])) settings.hooks[HOOK_EVENT] = [];

  if (existingIdx >= 0) {
    settings.hooks[HOOK_EVENT].splice(existingIdx, 1);
    info(`removed prior managed entry at index ${existingIdx}`);
  }

  settings.hooks[HOOK_EVENT].push(buildManagedEntry());
  const newIndex = settings.hooks[HOOK_EVENT].length - 1;

  let backupPath = null;
  if (existed) backupPath = backupSettings(raw);
  try {
    writeAtomic(SETTINGS_PATH, JSON.stringify(settings, null, 2) + '\n');
  } catch (e) {
    if (backupPath) {
      try { fs.copyFileSync(backupPath, SETTINGS_PATH); info(`restored from ${backupPath}`); }
      catch (_) { /* ignore */ }
    }
    fail(`failed to write settings.json (${e.message})`);
  }

  writeManifest(newIndex, SETTINGS_PATH);
  info(`installed ${HOOK_EVENT} hook at hooks.${HOOK_EVENT}[${newIndex}]`);
  info(`manifest at ${MANIFEST_PATH}`);
  if (backupPath) info(`backup at ${backupPath}`);
  info(`hook command: ${HOOK_COMMAND}`);
  info('NOTE: hook is gated on env CHATARCH_THRASH_DETECT=1 — set it to enable firing.');
  info('done.');
}

if (require.main === module) {
  try { main(); } catch (e) { fail(e.stack || String(e)); }
}

module.exports = {
  main, findManagedIndex, buildManagedEntry,
  HOOK_COMMAND, HOOK_EVENT, HOOK_MATCHER,
  MANIFEST_PATH, SETTINGS_PATH,
};
