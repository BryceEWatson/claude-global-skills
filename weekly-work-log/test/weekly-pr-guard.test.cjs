'use strict';

const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { evaluate, calendarDaysBetween } = require('../weekly-pr-guard.cjs');

const script = path.resolve(__dirname, '..', 'weekly-pr-guard.cjs');
const SOURCE = { path: 'src/data/work-log.source.json' };
const REPORT = { path: 'src/data/work-log.json' };

function weeklyPr(number, createdAt, head = `work-log/weekly-${createdAt.slice(0, 10)}`) {
  return { number, url: `https://github.com/BryceEWatson/brycewatson.com/pull/${number}`, headRefName: head, createdAt, files: [SOURCE, REPORT] };
}

// Runs the CLI against a fixture PR list and a throwaway state dir; returns exit code, parsed
// stdout, and the persisted run state (or null).
function runGuard(prs, now, { record = true, extra = [] } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'weekly-pr-guard-'));
  try {
    const prsFile = path.join(dir, 'prs.json');
    fs.writeFileSync(prsFile, typeof prs === 'string' ? prs : JSON.stringify(prs));
    const args = [script, '--prs-json', prsFile, '--now', now, '--state-dir', dir, ...extra];
    if (record) args.push('--record');
    const r = spawnSync(process.execPath, args, { encoding: 'utf8' });
    const statePath = path.join(dir, 'last-run.json');
    const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, 'utf8')) : null;
    return { code: r.status, out: r.stdout ? JSON.parse(r.stdout) : null, state, stderr: r.stderr };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

test('no open weekly PR is CLEAR, records nothing, exits 0', () => {
  const r = runGuard([{ number: 5, headRefName: 'feature/x', createdAt: '2026-09-01T00:00:00Z', files: [SOURCE] }], '2026-09-21T05:00:00Z');
  assert.equal(r.code, 0);
  assert.equal(r.out.verdict, 'CLEAR');
  assert.equal(r.state, null);
});

test('success path: a weekly PR opened this week is FRESH and records PR_ALREADY_OPEN as success', () => {
  const r = runGuard([weeklyPr(140, '2026-09-18T17:00:00Z')], '2026-09-21T05:00:00Z');
  assert.equal(r.code, 10);
  assert.equal(r.out.verdict, 'FRESH');
  assert.equal(r.state.status, 'succeeded');
  assert.equal(r.state.outcome, 'PR_ALREADY_OPEN');
  assert.equal(r.state.prNumber, '140');
});

test('failure path: PR 112 on the Sunday of 14 September is STALE and persists a failure with the PR', () => {
  // PR 112 was created 2026-08-17T05:17:38Z (16 August, Pacific). The run of 13 September
  // started 2026-09-14T05:00:48Z (13 September, Pacific): 28 calendar days.
  const r = runGuard([weeklyPr(112, '2026-08-17T05:17:38Z', 'work-log/weekly-2026-08-16')], '2026-09-14T05:00:48Z');
  assert.equal(r.code, 11);
  assert.equal(r.out.verdict, 'STALE');
  assert.equal(r.state.status, 'failed');
  assert.equal(r.state.reasonCode, 'STALE_WEEKLY_PR');
  assert.equal(r.state.prNumber, '112');
  assert.match(r.state.prUrl, /\/pull\/112$/);
  assert.match(r.state.message, /open 28 days/);
});

test('a PR opened minutes after last Sunday\'s run is stale at this Sunday\'s run', () => {
  // Opened Sunday 13 Sep 22:10 Pacific; next run starts Sunday 20 Sep 22:00 Pacific. Elapsed time
  // is under seven days, but it is seven calendar days, so it must not slip through a week.
  const opened = '2026-09-14T05:10:00Z';
  const nextRun = '2026-09-21T05:00:00Z';
  assert.equal(calendarDaysBetween(opened, nextRun), 7);
  const r = runGuard([weeklyPr(141, opened)], nextRun);
  assert.equal(r.out.verdict, 'STALE');
  assert.equal(r.state.reasonCode, 'STALE_WEEKLY_PR');
});

test('six calendar days is still FRESH (the Monday-opened backfill case)', () => {
  assert.equal(evaluate([weeklyPr(142, '2026-09-15T16:00:00Z')], { now: '2026-09-21T05:00:00Z' }).verdict, 'FRESH');
});

test('the oldest of several weekly PRs decides, and backfill branches count', () => {
  const result = evaluate([
    weeklyPr(150, '2026-09-19T17:00:00Z'),
    weeklyPr(149, '2026-09-01T17:00:00Z', 'work-log/backfill-2026-08-17'),
  ], { now: '2026-09-21T05:00:00Z' });
  assert.equal(result.verdict, 'STALE');
  assert.equal(result.pr.number, 149);
});

test('a work-log branch that does not touch the authored source is not a weekly PR', () => {
  const pr = weeklyPr(151, '2026-08-01T00:00:00Z');
  pr.files = [{ path: 'README.md' }];
  assert.equal(evaluate([pr], { now: '2026-09-21T05:00:00Z' }).verdict, 'CLEAR');
});

test('unreadable PR data is ERROR and persists PR_LIST_FAILED, never a success', () => {
  const r = runGuard('not json', '2026-09-21T05:00:00Z');
  assert.equal(r.code, 2);
  assert.equal(r.out.verdict, 'ERROR');
  assert.equal(r.state.status, 'failed');
  assert.equal(r.state.reasonCode, 'PR_LIST_FAILED');
});

test('without --record the guard reports but writes no state', () => {
  const r = runGuard([weeklyPr(112, '2026-08-17T05:17:38Z')], '2026-09-14T05:00:48Z', { record: false });
  assert.equal(r.code, 11);
  assert.equal(r.state, null);
});
