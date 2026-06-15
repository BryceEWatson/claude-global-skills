#!/usr/bin/env node
'use strict';
/**
 * Remove the weekly-work-log Sunday-night scheduled task.
 * Usage:  node ~/.claude/skills/weekly-work-log/uninstall.cjs
 */

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const TASK = 'ClaudeWeeklyWorkLog';
const MANIFEST = path.join(os.homedir(), '.claude', 'skills', 'weekly-work-log', '.local-state', 'install-manifest.json');

if (process.platform !== 'win32') {
  console.error('Nothing to do: the scheduled task is Windows-only (schtasks). Remove any cron entry you added manually.');
  process.exit(0);
}

try {
  execFileSync('schtasks', ['/Delete', '/TN', TASK, '/F'], { stdio: 'inherit' });
} catch (e) {
  console.error(`schtasks /Delete failed (task '${TASK}' may not exist).`);
}
try { if (fs.existsSync(MANIFEST)) fs.unlinkSync(MANIFEST); } catch { /* ignore */ }
console.log(`Removed scheduled task '${TASK}'.`);
