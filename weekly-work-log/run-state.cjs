#!/usr/bin/env node
'use strict';

/**
 * Durable, privacy-safe status for the unattended weekly work-log routine.
 *
 * The scheduled run records `running`, then one terminal state. The Monday
 * preview reads the same file so a failed Sunday run is visible instead of
 * being mistaken for "nothing to preview".
 *
 * Usage:
 *   node run-state.cjs start [--worktree PATH] [--week-start YYYY-MM-DD] [--week-end YYYY-MM-DD]
 *   node run-state.cjs success [--outcome PR_OPENED|NO_CHANGE] [--pr-url URL] [--pr-number N]
 *   node run-state.cjs fail --reason-code CODE --message TEXT
 *
 * Tests may override the destination with --state-dir PATH.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const MODES = new Set(['start', 'success', 'fail']);
const mode = process.argv[2];

function fail(message) {
  console.error(`weekly-work-log run-state: ${message}`);
  process.exit(2);
}

if (!MODES.has(mode)) {
  fail('first argument must be start, success, or fail.');
}

function option(name) {
  const index = process.argv.indexOf(`--${name}`);
  if (index === -1) return null;
  const value = process.argv[index + 1];
  if (!value || value.startsWith('--')) fail(`--${name} requires a value.`);
  return String(value).trim();
}

function clean(value, max = 500) {
  if (!value) return null;
  return value.replace(/[\r\n\t]+/g, ' ').replace(/\s{2,}/g, ' ').slice(0, max);
}

const stateDir = path.resolve(option('state-dir') || path.join(os.homedir(), '.claude', 'scheduled-tasks', 'weekly-work-log'));
const statePath = path.join(stateDir, 'last-run.json');
const now = new Date().toISOString();

let previous = null;
try {
  previous = JSON.parse(fs.readFileSync(statePath, 'utf8'));
} catch {
  previous = null;
}

const record = {
  schemaVersion: 1,
  taskId: 'weekly-work-log',
  status: mode === 'start' ? 'running' : mode === 'success' ? 'succeeded' : 'failed',
  startedAt: mode === 'start' ? now : previous?.startedAt || now,
  updatedAt: now,
};

for (const [key, flag, max] of [
  ['worktree', 'worktree', 500],
  ['weekStart', 'week-start', 10],
  ['weekEnd', 'week-end', 10],
]) {
  const value = clean(option(flag), max) || clean(previous?.[key], max);
  if (value) record[key] = value;
}

if (mode !== 'start') record.completedAt = now;

if (mode === 'success') {
  record.outcome = clean(option('outcome'), 80) || 'PR_OPENED';
  const prUrl = clean(option('pr-url'), 500);
  const prNumber = clean(option('pr-number'), 20);
  if (prUrl) record.prUrl = prUrl;
  if (prNumber) record.prNumber = prNumber;
}

if (mode === 'fail') {
  const reasonCode = clean(option('reason-code'), 80);
  const message = clean(option('message'), 500);
  if (!reasonCode || !message) fail('fail requires --reason-code and --message.');
  record.reasonCode = reasonCode;
  record.message = message;
}

fs.mkdirSync(stateDir, { recursive: true });
const temporary = `${statePath}.${process.pid}.tmp`;
fs.writeFileSync(temporary, JSON.stringify(record, null, 2) + '\n', 'utf8');
fs.renameSync(temporary, statePath);

console.log(statePath);
