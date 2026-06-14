#!/usr/bin/env node
/**
 * Install the auto-review-loop Stop hook into ~/.claude/settings.json.
 *
 * Cross-platform: uses os.homedir() / path.join() exclusively.
 *
 * Sentinel-via-sidecar pattern: a top-level `_reviewLoopManaged` key
 * inside settings.json would be REJECTED by Claude Code's strict
 * top-level schema validator (anthropics/claude-code#5886). Instead,
 * we store the install manifest in a sidecar at
 *   ~/.claude/skills/review-loop/.local-state/install-manifest.json
 * and identify "our" entry by exact-string match on the command string.
 *
 * Idempotent: running twice yields exactly one managed Stop entry.
 *
 * Safety:
 *   - Parses + validates settings.json before any write.
 *   - Backs up to settings.json.bak-<unix-ts> before writing (rotate; keep last 3).
 *   - Atomic write via tmp + rename.
 *   - Aborts non-zero with diagnostic on malformed JSON; original untouched.
 *
 * Usage:
 *   node ~/.claude/skills/review-loop/install.cjs
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const SETTINGS_PATH = path.join(os.homedir(), '.claude', 'settings.json');
const MANIFEST_DIR = path.join(os.homedir(), '.claude', 'skills', 'review-loop', '.local-state');
const MANIFEST_PATH = path.join(MANIFEST_DIR, 'install-manifest.json');
const HOOK_COMMAND = `node ${path.join('${HOME}', '.claude', 'skills', 'review-loop', 'stop-hook.cjs')}`;
// Build the exact command the user's settings.json will contain. Claude
// Code substitutes ${HOME} at hook-execution time; we keep the literal
// string in settings.json so the file is portable.
const MANAGED_VERSION = 1;
const MAX_BACKUPS = 3;

function fail(msg, code = 1) {
  process.stderr.write(`install-review-hook: ERROR: ${msg}\n`);
  process.exit(code);
}

function info(msg) {
  process.stdout.write(`install-review-hook: ${msg}\n`);
}

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function readSettings() {
  if (!fs.existsSync(SETTINGS_PATH)) {
    return { existed: false, json: {} };
  }
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
  const stop = settings.hooks && Array.isArray(settings.hooks.Stop) ? settings.hooks.Stop : null;
  if (!stop) return -1;
  for (let i = 0; i < stop.length; i++) {
    const entry = stop[i];
    if (!entry || !Array.isArray(entry.hooks)) continue;
    const match = entry.hooks.some(h => h && h.type === 'command' && h.command === HOOK_COMMAND);
    if (match) return i;
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
    matcher: '*',
    hooks: [
      { type: 'command', command: HOOK_COMMAND },
    ],
  };
}

function writeManifest(stopIndex, settingsPath) {
  ensureDir(MANIFEST_DIR);
  const data = {
    stopIndex,
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

  // Identify any existing managed entry (idempotent re-install / version upgrade).
  const existingIdx = findManagedIndex(settings);
  settings.hooks = settings.hooks && typeof settings.hooks === 'object' ? settings.hooks : {};
  if (!Array.isArray(settings.hooks.Stop)) settings.hooks.Stop = [];

  if (existingIdx >= 0) {
    settings.hooks.Stop.splice(existingIdx, 1);
    info(`removed prior managed entry at index ${existingIdx}`);
  }

  // Append a fresh managed entry.
  const newEntry = buildManagedEntry();
  settings.hooks.Stop.push(newEntry);
  const newIndex = settings.hooks.Stop.length - 1;

  // Backup existing settings (if any) then atomic-write the new one.
  let backupPath = null;
  if (existed) backupPath = backupSettings(raw);
  try {
    writeAtomic(SETTINGS_PATH, JSON.stringify(settings, null, 2) + '\n');
  } catch (e) {
    if (backupPath) {
      try { fs.copyFileSync(backupPath, SETTINGS_PATH); info(`restored from ${backupPath}`); }
      catch (_) { /* ignore */ }
    }
    fail(`failed to write settings.json (${e.message}); restored from backup if available`);
  }

  // Manifest written AFTER settings.json (manifest is derived state).
  writeManifest(newIndex, SETTINGS_PATH);

  info(`installed Stop hook at hooks.Stop[${newIndex}]`);
  info(`manifest at ${MANIFEST_PATH}`);
  if (backupPath) info(`backup at ${backupPath}`);
  info(`hook command: ${HOOK_COMMAND}`);
  info('done.');
}

if (require.main === module) {
  try { main(); }
  catch (e) { fail(e.stack || String(e)); }
}

module.exports = { main, findManagedIndex, buildManagedEntry, HOOK_COMMAND, MANIFEST_PATH, SETTINGS_PATH };
