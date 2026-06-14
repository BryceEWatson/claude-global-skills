#!/usr/bin/env node
/**
 * Uninstall the chat-arch thrash-detector PostToolUse hook from
 * ~/.claude/settings.json.
 *
 * Surgical removal: reads the sidecar manifest for the index, falls
 * back to exact-command-string scan if absent. Never reassigns or
 * empties the hooks.PostToolUse array.
 *
 * Idempotent: a second run with no managed entry is a no-op exit.
 * Same atomic-write + .bak rotation as install.cjs.
 *
 * Usage:
 *   node ~/.claude/skills/chat-arch-thrash-detect/uninstall.cjs
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
const MAX_BACKUPS = 3;

function info(msg) { process.stdout.write(`uninstall-thrash-hook: ${msg}\n`); }
function fail(msg, code = 1) {
  process.stderr.write(`uninstall-thrash-hook: ERROR: ${msg}\n`);
  process.exit(code);
}

function readSettings() {
  if (!fs.existsSync(SETTINGS_PATH)) return null;
  const raw = fs.readFileSync(SETTINGS_PATH, 'utf8');
  let parsed;
  try { parsed = JSON.parse(raw); }
  catch (e) { fail(`malformed settings.json (${e.message}); leaving original untouched`); }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    fail('settings.json is not a JSON object; leaving original untouched');
  }
  return { json: parsed, raw };
}

function readManifest() {
  if (!fs.existsSync(MANIFEST_PATH)) return null;
  try { return JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8')); }
  catch (_) { return null; }
}

function findManagedIndexByCommand(settings) {
  const hooks = settings.hooks && Array.isArray(settings.hooks[HOOK_EVENT]) ? settings.hooks[HOOK_EVENT] : null;
  if (!hooks) return -1;
  for (let i = 0; i < hooks.length; i++) {
    const entry = hooks[i];
    if (!entry || !Array.isArray(entry.hooks)) continue;
    if (entry.hooks.some(h => h && h.type === 'command' && h.command === HOOK_COMMAND)) return i;
  }
  return -1;
}

function backupSettings(raw) {
  if (raw === undefined || raw === null) return null;
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

function writeAtomic(targetPath, text) {
  const tmp = `${targetPath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmp, text, 'utf8');
  fs.renameSync(tmp, targetPath);
}

function deleteManifest() {
  try { fs.unlinkSync(MANIFEST_PATH); } catch (_) { /* ignore */ }
}

function main() {
  const readResult = readSettings();
  if (!readResult) { info('no settings.json present; nothing to uninstall.'); return; }
  const { json: settings, raw } = readResult;

  const manifest = readManifest();
  let hookIndex = -1;
  let foundVia = null;

  if (manifest && Number.isInteger(manifest.hookIndex)) {
    const hooks = settings.hooks && Array.isArray(settings.hooks[HOOK_EVENT]) ? settings.hooks[HOOK_EVENT] : null;
    if (hooks && manifest.hookIndex < hooks.length) {
      const candidate = hooks[manifest.hookIndex];
      const matches = candidate && Array.isArray(candidate.hooks)
        && candidate.hooks.some(h => h && h.type === 'command' && h.command === HOOK_COMMAND);
      if (matches) { hookIndex = manifest.hookIndex; foundVia = 'manifest'; }
    }
  }
  if (hookIndex < 0) {
    hookIndex = findManagedIndexByCommand(settings);
    if (hookIndex >= 0) foundVia = 'command-scan';
  }

  if (hookIndex < 0) {
    info(`no managed ${HOOK_EVENT} hook entry found; nothing to remove.`);
    deleteManifest();
    return;
  }

  settings.hooks[HOOK_EVENT].splice(hookIndex, 1);

  const backupPath = backupSettings(raw);
  try {
    writeAtomic(SETTINGS_PATH, JSON.stringify(settings, null, 2) + '\n');
  } catch (e) {
    if (backupPath) {
      try { fs.copyFileSync(backupPath, SETTINGS_PATH); info(`restored from ${backupPath}`); }
      catch (_) { /* ignore */ }
    }
    fail(`failed to write settings.json (${e.message})`);
  }

  deleteManifest();
  info(`removed managed ${HOOK_EVENT} hook (via ${foundVia}, index ${hookIndex})`);
  if (backupPath) info(`backup at ${backupPath}`);
  info('done.');
}

if (require.main === module) {
  try { main(); } catch (e) { fail(e.stack || String(e)); }
}

module.exports = {
  main, findManagedIndexByCommand,
  HOOK_COMMAND, HOOK_EVENT,
  MANIFEST_PATH, SETTINGS_PATH,
};
