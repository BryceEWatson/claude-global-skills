'use strict';

const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { evaluate, calendarDaysBetween, reportWeekEnd } = require('../weekly-pr-guard.cjs');

const script = path.resolve(__dirname, '..', 'weekly-pr-guard.cjs');
const SOURCE = { path: 'src/data/work-log.source.json' };
const REPORT = { path: 'src/data/work-log.json' };

function weeklyPr(number, createdAt, head = `work-log/weekly-${createdAt.slice(0, 10)}`, files = [SOURCE, REPORT]) {
  return { number, url: `https://github.com/BryceEWatson/brycewatson.com/pull/${number}`, headRefName: head, createdAt, files };
}

// Runs the CLI against a fixture PR list and a throwaway state dir; returns exit code, parsed
// stdout, and the persisted run state (or null).
function runGuard(prs, now, { record = true, stateDir = null } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'weekly-pr-guard-'));
  try {
    const prsFile = path.join(dir, 'prs.json');
    fs.writeFileSync(prsFile, typeof prs === 'string' ? prs : JSON.stringify(prs));
    const target = stateDir || dir;
    const args = [script, '--prs-json', prsFile, '--now', now, '--state-dir', target];
    if (record) args.push('--record');
    const r = spawnSync(process.execPath, args, { encoding: 'utf8' });
    const statePath = path.join(dir, 'last-run.json');
    const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, 'utf8')) : null;
    return { code: r.status, out: r.stdout ? JSON.parse(r.stdout) : null, state, stderr: r.stderr };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

// Sunday 20 September 2026, 22:00 Pacific (daylight time, UTC-7).
const SUNDAY_RUN = '2026-09-21T05:00:00Z';

test('the reported week ends on the most recent Pacific Sunday', () => {
  assert.equal(reportWeekEnd(SUNDAY_RUN), '2026-09-20');
  assert.equal(reportWeekEnd('2026-09-21T23:00:00Z'), '2026-09-20'); // Monday afternoon catch-up
  assert.equal(reportWeekEnd('2026-09-20T06:30:00Z'), '2026-09-13'); // Saturday 19 Sep, 23:30 Pacific
  assert.equal(reportWeekEnd('2026-11-02T06:00:00Z'), '2026-11-01'); // the Sunday daylight time ends
});

test('no open weekly PR is CLEAR, records nothing, exits 0', () => {
  const r = runGuard([{ number: 5, headRefName: 'feature/x', createdAt: '2026-09-01T00:00:00Z', files: [SOURCE] }], SUNDAY_RUN);
  assert.equal(r.code, 0);
  assert.equal(r.out.verdict, 'CLEAR');
  assert.equal(r.state, null);
});

test('success path: a PR opened for this week (a second firing) is FRESH and records PR_ALREADY_OPEN', () => {
  const r = runGuard([weeklyPr(140, '2026-09-21T05:03:00Z')], '2026-09-21T05:30:00Z');
  assert.equal(r.code, 10);
  assert.equal(r.out.verdict, 'FRESH');
  assert.equal(r.state.status, 'succeeded');
  assert.equal(r.state.outcome, 'PR_ALREADY_OPEN');
  assert.equal(r.state.prNumber, '140');
});

test('a Monday catch-up run still treats the PR its own Sunday run opened as FRESH', () => {
  assert.equal(evaluate([weeklyPr(141, '2026-09-21T06:00:00Z')], { now: '2026-09-21T23:00:00Z' }).verdict, 'FRESH');
});

test('failure path: PR 112 on the Sunday of 13 September is STALE and persists a failure with the PR', () => {
  // PR 112 was created 2026-08-17T05:17:38Z (16 August, Pacific). The run of 13 September
  // started 2026-09-14T05:00:48Z (13 September, Pacific).
  const r = runGuard([weeklyPr(112, '2026-08-17T05:17:38Z', 'work-log/weekly-2026-08-16')], '2026-09-14T05:00:48Z');
  assert.equal(r.code, 11);
  assert.equal(r.out.verdict, 'STALE');
  assert.equal(r.state.status, 'failed');
  assert.equal(r.state.reasonCode, 'STALE_WEEKLY_PR');
  assert.equal(r.state.prNumber, '112');
  assert.match(r.state.prUrl, /\/pull\/112$/);
  assert.match(r.state.message, /open since 2026-08-16 \(28 days\)/);
});

test("a PR opened minutes after last Sunday's run is stale at this Sunday's run", () => {
  const opened = '2026-09-14T05:10:00Z'; // Sunday 13 Sep, 22:10 Pacific
  assert.equal(calendarDaysBetween(opened, SUNDAY_RUN), 7);
  assert.equal(evaluate([weeklyPr(142, opened)], { now: SUNDAY_RUN }).verdict, 'STALE');
});

test('a PR opened mid-week by a late run is stale at the next Sunday, though under seven days old', () => {
  const opened = '2026-09-14T15:00:00Z'; // Monday 14 Sep, 08:00 Pacific: last week's run, started late
  assert.equal(calendarDaysBetween(opened, SUNDAY_RUN), 6);
  const r = runGuard([weeklyPr(143, opened)], SUNDAY_RUN);
  assert.equal(r.out.verdict, 'STALE');
  assert.equal(r.state.reasonCode, 'STALE_WEEKLY_PR');
});

test('the oldest of several weekly PRs decides, and backfill branches count', () => {
  const result = evaluate([
    weeklyPr(150, '2026-09-21T05:05:00Z'),
    weeklyPr(149, '2026-09-01T17:00:00Z', 'work-log/backfill-2026-08-17'),
  ], { now: SUNDAY_RUN });
  assert.equal(result.verdict, 'STALE');
  assert.equal(result.pr.number, 149);
});

test('a backfill PR that only changes the report archive and goals still counts', () => {
  const pr = weeklyPr(151, '2026-09-01T17:00:00Z', 'work-log/backfill-2026-08-10', [{ path: 'src/data/reports/2026-08-10.json' }, { path: 'src/data/goals.json' }]);
  assert.equal(evaluate([pr], { now: SUNDAY_RUN }).verdict, 'STALE');
});

test('a work-log branch that touches no work-log data is not a weekly PR', () => {
  const pr = weeklyPr(152, '2026-08-01T00:00:00Z', 'work-log/weekly-2026-08-01', [{ path: 'README.md' }]);
  assert.equal(evaluate([pr], { now: SUNDAY_RUN }).verdict, 'CLEAR');
});

test('unreadable PR data is ERROR and persists PR_LIST_FAILED, never a success', () => {
  const r = runGuard('not json', SUNDAY_RUN);
  assert.equal(r.code, 2);
  assert.equal(r.out.verdict, 'ERROR');
  assert.equal(r.state.status, 'failed');
  assert.equal(r.state.reasonCode, 'PR_LIST_FAILED');
});

test('a bad --now is ERROR, not a quiet success', () => {
  const r = runGuard([weeklyPr(112, '2026-08-17T05:17:38Z')], 'someday');
  assert.equal(r.code, 2);
  assert.equal(r.out.verdict, 'ERROR');
});

test('a verdict that cannot be recorded exits 2 and says why', () => {
  const blocker = path.join(os.tmpdir(), `weekly-pr-guard-file-${process.pid}`);
  fs.writeFileSync(blocker, 'not a directory');
  try {
    const r = runGuard([weeklyPr(112, '2026-08-17T05:17:38Z')], SUNDAY_RUN, { stateDir: path.join(blocker, 'state') });
    assert.equal(r.code, 2);
    assert.equal(r.out.verdict, 'STALE');
    assert.match(r.out.recordError, /run-state\.cjs failed/);
  } finally {
    fs.rmSync(blocker, { force: true });
  }
});

test('without --record the guard reports but writes no state', () => {
  const r = runGuard([weeklyPr(112, '2026-08-17T05:17:38Z')], '2026-09-14T05:00:48Z', { record: false });
  assert.equal(r.code, 11);
  assert.equal(r.state, null);
});
