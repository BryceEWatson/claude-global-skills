'use strict';

const assert = require('node:assert/strict');
const { execFileSync, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const script = path.resolve(__dirname, '..', 'run-state.cjs');

function run(dir, ...args) {
  execFileSync(process.execPath, [script, ...args, '--state-dir', dir], { encoding: 'utf8' });
  return JSON.parse(fs.readFileSync(path.join(dir, 'last-run.json'), 'utf8'));
}

test('records a running state and preserves its start time on success', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'weekly-run-state-'));
  try {
    const started = run(dir, 'start', '--worktree', 'C:/worktree', '--week-start', '2026-07-06', '--week-end', '2026-07-12');
    const completed = run(dir, 'success', '--outcome', 'PR_OPENED', '--pr-url', 'https://example.test/pull/1', '--pr-number', '1');

    assert.equal(started.status, 'running');
    assert.equal(completed.status, 'succeeded');
    assert.equal(completed.startedAt, started.startedAt);
    assert.equal(completed.weekStart, '2026-07-06');
    assert.equal(completed.weekEnd, '2026-07-12');
    assert.equal(completed.prNumber, '1');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('records a bounded, single-line failure message', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'weekly-run-state-'));
  try {
    run(dir, 'start');
    const failed = run(dir, 'fail', '--reason-code', 'WORKTREE_SETUP', '--message', 'first line\nsecond line');

    assert.equal(failed.status, 'failed');
    assert.equal(failed.reasonCode, 'WORKTREE_SETUP');
    assert.equal(failed.message, 'first line second line');
    assert.ok(failed.completedAt);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('rejects an incomplete failure record', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'weekly-run-state-'));
  try {
    const result = spawnSync(process.execPath, [script, 'fail', '--reason-code', 'X', '--state-dir', dir], { encoding: 'utf8' });
    assert.equal(result.status, 2);
    assert.match(result.stderr, /requires --reason-code and --message/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('a failure about a PR keeps its link, and a held merge keeps its reasons', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'weekly-run-state-'));
  try {
    run(dir, 'start');
    const stale = run(dir, 'fail', '--reason-code', 'STALE_WEEKLY_PR', '--message', 'PR 112 open 28 days', '--pr-url', 'https://example.test/pull/112', '--pr-number', '112');
    assert.equal(stale.prNumber, '112');
    assert.equal(stale.prUrl, 'https://example.test/pull/112');
    run(dir, 'start');
    const held = run(dir, 'success', '--outcome', 'HELD', '--message', 'the advisory raised 1 flag(s)', '--pr-number', '140');
    assert.equal(held.status, 'succeeded');
    assert.equal(held.outcome, 'HELD');
    assert.equal(held.message, 'the advisory raised 1 flag(s)');
    assert.equal(held.reasonCode, undefined);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
