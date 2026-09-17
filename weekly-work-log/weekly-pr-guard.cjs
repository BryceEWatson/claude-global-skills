#!/usr/bin/env node
'use strict';

/**
 * Preflight for the unattended Sunday weekly work-log run: is a weekly PR already open,
 * and if so, is it fresh or stuck?
 *
 * Before 2026-09-17 an open weekly PR always recorded success (PR_ALREADY_OPEN) and stopped
 * the run. PR 112 stayed open from 17 August, so four Sunday runs "succeeded" in seconds,
 * nothing was drafted, and the Monday preview had no failure to surface. Now an open weekly
 * PR aged seven or more Pacific calendar days is a failure the Monday preview raises.
 *
 * Verdicts (exit code):
 *   CLEAR  (0)  no open weekly PR; the run continues
 *   FRESH  (10) an open weekly PR younger than the limit; the run stops quietly (success)
 *   STALE  (11) an open weekly PR at or over the limit; the run stops as a failure
 *   ERROR  (2)  could not read the open PRs; the caller persists a failure
 *
 * Usage:
 *   node weekly-pr-guard.cjs [--record] [--now ISO] [--max-age-days 7] [--prs-json FILE] [--state-dir DIR]
 *
 * --record writes the terminal run state through run-state.cjs (FRESH -> success
 * PR_ALREADY_OPEN, STALE -> fail STALE_WEEKLY_PR, ERROR -> fail PR_LIST_FAILED).
 * --prs-json and --state-dir exist for tests; without --prs-json it calls gh.
 */

const { execFileSync, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const REPO = 'BryceEWatson/brycewatson.com';
const HEAD_PREFIXES = ['work-log/weekly-', 'work-log/backfill-'];
const REQUIRED_FILES = ['src/data/work-log.source.json'];
const TIME_ZONE = 'America/Los_Angeles';

function option(name) {
  const index = process.argv.indexOf(`--${name}`);
  if (index === -1) return null;
  const value = process.argv[index + 1];
  if (!value || value.startsWith('--')) throw new Error(`--${name} requires a value`);
  return value;
}

// YYYY-MM-DD of an instant as a Pacific wall-calendar date.
function pacificDate(instant) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit' })
    .formatToParts(new Date(instant));
  const get = (type) => parts.find((p) => p.type === type).value;
  return `${get('year')}-${get('month')}-${get('day')}`;
}

// Whole calendar days between two Pacific dates. Calendar days, not elapsed hours: a PR opened
// minutes after last Sunday's 22:00 start is six days and twenty-three hours old at this
// Sunday's start, and an hour-based limit would let it through for another week.
function calendarDaysBetween(fromInstant, toInstant) {
  const from = Date.parse(`${pacificDate(fromInstant)}T00:00:00Z`);
  const to = Date.parse(`${pacificDate(toInstant)}T00:00:00Z`);
  return Math.round((to - from) / 86400000);
}

function isWeeklyPr(pr) {
  const head = String(pr.headRefName || '');
  if (!HEAD_PREFIXES.some((prefix) => head.startsWith(prefix))) return false;
  const files = (pr.files || []).map((f) => f.path || f);
  return REQUIRED_FILES.every((f) => files.includes(f));
}

function evaluate(prs, { now = new Date().toISOString(), maxAgeDays = 7 } = {}) {
  const weekly = (prs || []).filter(isWeeklyPr)
    .map((pr) => ({ number: pr.number, url: pr.url, headRefName: pr.headRefName, createdAt: pr.createdAt, ageDays: calendarDaysBetween(pr.createdAt, now) }))
    .sort((a, b) => b.ageDays - a.ageDays);
  if (weekly.length === 0) return { verdict: 'CLEAR', prs: [] };
  const oldest = weekly[0];
  if (oldest.ageDays >= maxAgeDays) {
    return {
      verdict: 'STALE',
      pr: oldest,
      prs: weekly,
      message: `Weekly work-log PR ${oldest.number} has been open ${oldest.ageDays} days (since ${pacificDate(oldest.createdAt)}), so this week was not drafted. Land or close it.`,
    };
  }
  return { verdict: 'FRESH', pr: oldest, prs: weekly, message: `Weekly work-log PR ${oldest.number} is open (${oldest.ageDays} days old); not opening a duplicate.` };
}

function listOpenPrs() {
  const out = execFileSync('gh', ['pr', 'list', '--repo', REPO, '--state', 'open', '--limit', '100', '--json', 'number,url,headRefName,createdAt,files'], { encoding: 'utf8' });
  return JSON.parse(out);
}

function record(result, stateDir) {
  const runState = path.join(__dirname, 'run-state.cjs');
  const extra = stateDir ? ['--state-dir', stateDir] : [];
  let args;
  if (result.verdict === 'FRESH') {
    args = ['success', '--outcome', 'PR_ALREADY_OPEN', '--pr-url', result.pr.url, '--pr-number', String(result.pr.number)];
  } else if (result.verdict === 'STALE') {
    args = ['fail', '--reason-code', 'STALE_WEEKLY_PR', '--message', result.message, '--pr-url', result.pr.url, '--pr-number', String(result.pr.number)];
  } else if (result.verdict === 'ERROR') {
    args = ['fail', '--reason-code', 'PR_LIST_FAILED', '--message', result.message];
  } else {
    return;
  }
  const r = spawnSync(process.execPath, [runState, ...args, ...extra], { encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`run-state.cjs failed: ${r.stderr || r.stdout}`);
}

function main() {
  const shouldRecord = process.argv.includes('--record');
  const stateDir = option('state-dir');
  let result;
  try {
    const prsJson = option('prs-json');
    const prs = prsJson ? JSON.parse(fs.readFileSync(prsJson, 'utf8')) : listOpenPrs();
    const maxAge = option('max-age-days');
    result = evaluate(prs, { now: option('now') || new Date().toISOString(), maxAgeDays: maxAge == null ? 7 : Number(maxAge) });
  } catch (error) {
    result = { verdict: 'ERROR', message: `Could not list open pull requests: ${String(error.message).split('\n')[0]}` };
  }
  if (shouldRecord) record(result, stateDir);
  console.log(JSON.stringify(result, null, 2));
  process.exit({ CLEAR: 0, FRESH: 10, STALE: 11, ERROR: 2 }[result.verdict]);
}

module.exports = { evaluate, calendarDaysBetween, pacificDate, isWeeklyPr };

if (require.main === module) main();
