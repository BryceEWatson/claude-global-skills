#!/usr/bin/env node
/**
 * Uninstall the auto-review-loop Stop hook from ~/.claude/settings.json.
 *
 * Surgical removal: reads the install manifest sidecar to locate "our"
 * Stop entry by index, falls back to exact-command-string scan if the
 * manifest is absent. Never reassigns or empties the hooks.Stop array.
 *
 * Idempotent: a second run with no managed entry is a no-op exit.
 *
 * Same atomic-write + .bak rotation as install.cjs.
 *
 * Usage:
 *   node ~/.claude/skills/review-loop/uninstall.cjs
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const SETTINGS_PATH = path.join(os.homedir(), '.claude', 'settings.json');
const MANIFEST_DIR = path.join(os.homedir(), '.claude', 'skills', 'review-loop', '.local-state');
const MANIFEST_PATH = path.join(MANIFEST_DIR, 'install-manifest.json');
const HOOK_COMMAND = `node "${path.join(os.homedir(), '.claude', 'skills', 'review-loop', 'stop-hook.cjs')}"`;
// Command strings from installs before the absolute-path fix; uninstall removes
// these too so an old broken entry can still be cleaned up.
const LEGACY_HOOK_COMMANDS = [
  `node ${path.join('${HOME}', '.claude', 'skills', 'review-loop', 'stop-hook.cjs')}`,
];
const KNOWN_HOOK_COMMANDS = new Set([HOOK_COMMAND, ...LEGACY_HOOK_COMMANDS]);
const MAX_BACKUPS = 3;

function info(msg) { process.stdout.write(`uninstall-review-hook: ${msg}\n`); }
function fail(msg, code = 1) {
  process.stderr.write(`uninstall-review-hook: ERROR: ${msg}\n`);
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
  const stop = settings.hooks && Array.isArray(settings.hooks.Stop) ? settings.hooks.Stop : null;
  if (!stop) return -1;
  for (let i = 0; i < stop.length; i++) {
    const entry = stop[i];
    if (!entry || !Array.isArray(entry.hooks)) continue;
    const match = entry.hooks.some(h => h && h.type === 'command' && KNOWN_HOOK_COMMANDS.has(h.command));
    if (match) return i;
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
    try { fs.unlinkSync(path.join(dir, b.name)); } catch (_) {}
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

  // Try manifest first for index, fall back to command-string scan.
  const manifest = readManifest();
  let stopIndex = -1;
  let foundVia = null;

  if (manifest && Number.isInteger(manifest.stopIndex)) {
    const stop = settings.hooks && Array.isArray(settings.hooks.Stop) ? settings.hooks.Stop : null;
    if (stop && manifest.stopIndex < stop.length) {
      const candidate = stop[manifest.stopIndex];
      const matchesCommand = candidate && Array.isArray(candidate.hooks)
        && candidate.hooks.some(h => h && h.type === 'command' && KNOWN_HOOK_COMMANDS.has(h.command));
      if (matchesCommand) {
        stopIndex = manifest.stopIndex;
        foundVia = 'manifest';
      }
    }
  }
  if (stopIndex < 0) {
    stopIndex = findManagedIndexByCommand(settings);
    if (stopIndex >= 0) foundVia = 'command-scan';
  }

  if (stopIndex < 0) {
    info('no managed Stop hook entry found; nothing to remove.');
    deleteManifest(); // cleanup orphaned manifest if present
    return;
  }

  // Surgical splice.
  settings.hooks.Stop.splice(stopIndex, 1);
  // Don't delete an empty Stop array — preserves user's structure shape.

  const backupPath = backupSettings(raw);
  try {
    writeAtomic(SETTINGS_PATH, JSON.stringify(settings, null, 2) + '\n');
  } catch (e) {
    if (backupPath) {
      try { fs.copyFileSync(backupPath, SETTINGS_PATH); info(`restored from ${backupPath}`); }
      catch (_) {}
    }
    fail(`failed to write settings.json (${e.message}); restored from backup if available`);
  }

  deleteManifest();
  info(`removed managed Stop hook (via ${foundVia}, index ${stopIndex})`);
  if (backupPath) info(`backup at ${backupPath}`);
  info('done.');
}

if (require.main === module) {
  try { main(); }
  catch (e) { fail(e.stack || String(e)); }
}

module.exports = { main, findManagedIndexByCommand, HOOK_COMMAND, MANIFEST_PATH, SETTINGS_PATH };
