#!/usr/bin/env node
'use strict';
/**
 * Register the weekly-work-log Sunday-night scheduled task (Windows).
 *
 * Creates a `schtasks` job that runs weekly-run.cmd every Sunday at 22:00. That
 * launcher discovers last week's work, refreshes + verifies the data, and opens
 * a review PR. It NEVER pushes main or deploys.
 *
 * Idempotent (`/F` overwrites). Records a sidecar manifest in .local-state/.
 *
 * Usage:  node ~/.claude/skills/weekly-work-log/install.cjs
 *         node ~/.claude/skills/weekly-work-log/install.cjs --time 21:30
 */

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const SKILL_DIR = path.join(os.homedir(), '.claude', 'skills', 'weekly-work-log');
const STATE_DIR = path.join(SKILL_DIR, '.local-state');
const MANIFEST = path.join(STATE_DIR, 'install-manifest.json');
const LAUNCHER = path.join(SKILL_DIR, 'weekly-run.cmd');
const TASK = 'ClaudeWeeklyWorkLog';

const timeIdx = process.argv.indexOf('--time');
const TIME = timeIdx > -1 ? process.argv[timeIdx + 1] : '22:00';

if (process.platform !== 'win32') {
  console.error('This installer registers a Windows scheduled task (schtasks).');
  console.error('On another OS, add a weekly cron entry that runs the equivalent of weekly-run.cmd.');
  process.exit(1);
}
if (!fs.existsSync(LAUNCHER)) {
  console.error('Launcher not found: ' + LAUNCHER);
  process.exit(1);
}
fs.mkdirSync(STATE_DIR, { recursive: true });

const tr = `cmd /c "${LAUNCHER}"`;
try {
  execFileSync('schtasks', ['/Create', '/TN', TASK, '/TR', tr, '/SC', 'WEEKLY', '/D', 'SUN', '/ST', TIME, '/F'], { stdio: 'inherit' });
} catch (e) {
  console.error('\nschtasks /Create failed. Register it manually (one line):');
  console.error(`  schtasks /Create /TN ${TASK} /TR "${tr}" /SC WEEKLY /D SUN /ST ${TIME} /F`);
  process.exit(1);
}

fs.writeFileSync(MANIFEST, JSON.stringify({
  task: TASK,
  launcher: LAUNCHER,
  schedule: `WEEKLY SUN ${TIME}`,
  installedBy: 'weekly-work-log/install.cjs',
}, null, 2) + '\n');

console.log(`\nRegistered scheduled task '${TASK}' (Sundays ${TIME}).`);
console.log('It prepares a review PR and never publishes. Test it now with:');
console.log(`  cmd /c "${LAUNCHER}"`);
console.log(`Disable with:  node ${path.join(SKILL_DIR, 'uninstall.cjs')}`);
console.log('\nNote: it activates once the /log data files are committed to the repo;');
console.log('until then it harmlessly no-ops (untracked files show no diff).');
