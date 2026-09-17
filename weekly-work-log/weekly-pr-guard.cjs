#!/usr/bin/env node
'use strict';

/**
 * Preflight for the unattended Sunday weekly work-log run: is a weekly PR already open,
 * and if so, does it belong to this run's week or is it left over from an earlier one?
 *
 * Before 2026-09-17 an open weekly PR always recorded success (PR_ALREADY_OPEN) and stopped
 * the run. PR 112 stayed open from 17 August, so four Sunday runs "succeeded" in seconds,
 * nothing was drafted, and the Monday preview had no failure to show.
 *
 * The rule. Each run reports the week that ends on the most recent Sunday (Pacific; today, if
 * today is Sunday). An open weekly or backfill PR opened before that Sunday is left over from an
 * earlier week: that is STALE, a failure Monday raises. This covers every PR seven or more days
 * old, and also a PR opened mid-week by a Sunday run that started late, which a plain seven-day
 * limit would let through for another week. Any open backfill PR is also STALE: it blocks the
 * weekly run and is not the run's own PR. A weekly PR opened on or after that Sunday, still open
 * or already merged, means this week already ran (a second firing), so the run stops quietly
 * instead of drafting and publishing the week a second time.
 *
 * Verdicts (exit code):
 *   CLEAR  (0)  nothing blocks the run; it continues
 *   FRESH  (10) this week's weekly PR already exists (open or merged); the run stops quietly
 *   STALE  (11) an open PR from an earlier week, or an open backfill PR; the run stops as a failure
 *   ERROR  (2)  could not read the PRs, or could not record the result
 *
 * Usage:
 *   node weekly-pr-guard.cjs [--record] [--now ISO] [--prs-json FILE] [--state-dir DIR]
 *
 * --record writes the terminal run state through run-state.cjs (FRESH -> success
 * PR_ALREADY_OPEN, or MERGED when it already merged; STALE -> fail STALE_WEEKLY_PR;
 * ERROR -> fail PR_LIST_FAILED).
 * --prs-json and --state-dir exist for tests; without --prs-json it calls gh.
 */

const { execFileSync, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const REPO = 'BryceEWatson/brycewatson.com';
const HEAD_PREFIXES = ['work-log/weekly-', 'work-log/backfill-'];
const WORK_LOG_DATA = /^(src\/data\/(work-log\.source\.json|work-log\.json|goals\.json|reports\/)|data\/weekly-candidates\/)/;
const DEFAULT_STATE_DIR = path.join(os.homedir(), '.claude', 'scheduled-tasks', 'weekly-work-log');
const TIME_ZONE = 'America/Los_Angeles';
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function option(name) {
  const index = process.argv.indexOf(`--${name}`);
  if (index === -1) return null;
  const value = process.argv[index + 1];
  if (!value || value.startsWith('--')) throw new Error(`--${name} requires a value`);
  return value;
}

function pacificParts(instant) {
  const date = new Date(instant);
  if (Number.isNaN(date.getTime())) throw new Error(`not a valid time: ${instant}`);
  const parts = new Intl.DateTimeFormat('en-US', { timeZone: TIME_ZONE, year: 'numeric', month: '2-digit', day: '2-digit', weekday: 'short' })
    .formatToParts(date);
  const get = (type) => parts.find((p) => p.type === type).value;
  return { ymd: `${get('year')}-${get('month')}-${get('day')}`, weekday: WEEKDAYS.indexOf(get('weekday')) };
}

// YYYY-MM-DD of an instant as a Pacific wall-calendar date.
function pacificDate(instant) {
  return pacificParts(instant).ymd;
}

function addDays(ymd, days) {
  const d = new Date(`${ymd}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

// The Sunday (Pacific) that ends the week this run reports: today if today is Sunday.
function reportWeekEnd(now) {
  const { ymd, weekday } = pacificParts(now);
  return addDays(ymd, -weekday);
}

function calendarDaysBetween(fromInstant, toInstant) {
  const from = Date.parse(`${pacificDate(fromInstant)}T00:00:00Z`);
  const to = Date.parse(`${pacificDate(toInstant)}T00:00:00Z`);
  return Math.round((to - from) / 86400000);
}

// A weekly branch counts on its name alone (a week can change only candidates or the ledger).
// A backfill branch must touch work-log data or candidates.
function isWeeklyPr(pr) {
  const head = String(pr.headRefName || '');
  if (head.startsWith('work-log/weekly-')) return true;
  if (!HEAD_PREFIXES.some((prefix) => head.startsWith(prefix))) return false;
  return (pr.files || []).some((f) => WORK_LOG_DATA.test(f.path || f));
}

// `prs` holds every open PR plus recently merged ones (state OPEN or MERGED; a PR without a
// state counts as open). Order of precedence:
//   1. any open weekly or backfill PR opened before this run's week ended -> STALE
//   2. any open backfill PR -> STALE (it blocks the weekly run and is not this run's own PR)
//   3. a weekly PR opened for this week, open or already merged -> FRESH (a second firing; quiet)
//   4. otherwise CLEAR
function evaluate(prs, { now = new Date().toISOString() } = {}) {
  const weekEnd = reportWeekEnd(now);
  const weekly = (prs || []).filter(isWeeklyPr)
    .map((pr) => ({
      number: pr.number, url: pr.url, headRefName: pr.headRefName, createdAt: pr.createdAt,
      state: String(pr.state || 'OPEN').toUpperCase(),
      openedOn: pacificDate(pr.createdAt), ageDays: calendarDaysBetween(pr.createdAt, now),
    }))
    .sort((a, b) => a.openedOn.localeCompare(b.openedOn));
  const open = weekly.filter((pr) => pr.state === 'OPEN');
  const oldOpen = open.find((pr) => pr.openedOn < weekEnd);
  if (oldOpen) {
    return {
      verdict: 'STALE', weekEnd, pr: oldOpen, prs: weekly,
      message: `Weekly work-log PR ${oldOpen.number} has been open since ${oldOpen.openedOn} (${oldOpen.ageDays} days), before this run's week ended on ${weekEnd}, so this week was not drafted. Land or close it.`,
    };
  }
  const openBackfill = open.find((pr) => pr.headRefName.startsWith('work-log/backfill-'));
  if (openBackfill) {
    return {
      verdict: 'STALE', weekEnd, pr: openBackfill, prs: weekly,
      message: `Backfill PR ${openBackfill.number} is open, so this week was not drafted on top of it. Land or close it, then rerun.`,
    };
  }
  const thisWeek = weekly.find((pr) => pr.headRefName.startsWith('work-log/weekly-') && pr.openedOn >= weekEnd && ['OPEN', 'MERGED'].includes(pr.state));
  if (thisWeek) {
    return {
      verdict: 'FRESH', weekEnd, pr: thisWeek, prs: weekly,
      message: `Weekly work-log PR ${thisWeek.number} was already ${thisWeek.state === 'MERGED' ? 'merged' : 'opened'} for this week; not running again.`,
    };
  }
  return { verdict: 'CLEAR', weekEnd, prs: weekly };
}

function listPrs() {
  const fields = 'number,url,headRefName,createdAt,state,files';
  const open = JSON.parse(execFileSync('gh', ['pr', 'list', '--repo', REPO, '--state', 'open', '--limit', '200', '--json', fields], { encoding: 'utf8' }));
  const merged = JSON.parse(execFileSync('gh', ['pr', 'list', '--repo', REPO, '--state', 'merged', '--limit', '30', '--json', fields], { encoding: 'utf8' }));
  return [...open, ...merged];
}

// The previous finished run's HELD or PR_OPENED verdict on this same open PR, if any.
function carriedVerdict(stateDir, pr) {
  if (pr.state !== 'OPEN') return null;
  try {
    const prev = JSON.parse(fs.readFileSync(path.join(stateDir, 'last-run.json'), 'utf8')).previous;
    if (prev && String(prev.prNumber) === String(pr.number) && ['HELD', 'PR_OPENED'].includes(prev.outcome)) return prev;
  } catch { /* no record: nothing to carry */ }
  return null;
}

function record(result, stateDir) {
  const runState = path.join(__dirname, 'run-state.cjs');
  const extra = stateDir ? ['--state-dir', stateDir] : [];
  let args;
  if (result.verdict === 'FRESH') {
    // A second firing must not erase the first firing's verdict on the same PR: a HELD or
    // PR_OPENED result is what makes Monday ask Bryce, so carry it forward unchanged.
    const carried = carriedVerdict(stateDir || DEFAULT_STATE_DIR, result.pr);
    const outcome = carried ? carried.outcome : result.pr.state === 'MERGED' ? 'MERGED' : 'PR_ALREADY_OPEN';
    args = ['success', '--outcome', outcome, ...(carried?.message ? ['--message', carried.message] : []), '--pr-url', result.pr.url, '--pr-number', String(result.pr.number)];
  } else if (result.verdict === 'STALE') {
    args = ['fail', '--reason-code', 'STALE_WEEKLY_PR', '--message', result.message, '--pr-url', result.pr.url, '--pr-number', String(result.pr.number)];
  } else if (result.verdict === 'ERROR') {
    args = ['fail', '--reason-code', 'PR_LIST_FAILED', '--message', result.message];
  } else {
    return;
  }
  const r = spawnSync(process.execPath, [runState, ...args, ...extra], { encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`run-state.cjs failed: ${(r.stderr || r.stdout || '').trim()}`);
}

function main() {
  const shouldRecord = process.argv.includes('--record');
  let result;
  let stateDir = null;
  try {
    stateDir = option('state-dir');
    const prsJson = option('prs-json');
    const prs = prsJson ? JSON.parse(fs.readFileSync(prsJson, 'utf8')) : listPrs();
    result = evaluate(prs, { now: option('now') || new Date().toISOString() });
  } catch (error) {
    result = { verdict: 'ERROR', message: `Could not list open pull requests: ${String(error.message).split('\n')[0]}` };
  }
  if (shouldRecord) {
    try {
      record(result, stateDir);
    } catch (error) {
      // Could not persist the verdict: say so on stdout and exit ERROR, never a success code.
      console.log(JSON.stringify({ ...result, recordError: String(error.message).split('\n')[0] }, null, 2));
      process.exit(2);
    }
  }
  console.log(JSON.stringify(result, null, 2));
  process.exit({ CLEAR: 0, FRESH: 10, STALE: 11, ERROR: 2 }[result.verdict]);
}

module.exports = { evaluate, calendarDaysBetween, pacificDate, reportWeekEnd, isWeeklyPr };

if (require.main === module) main();
