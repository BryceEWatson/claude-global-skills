/**
 * Tests for the thrash-detector PostToolUse hook.
 *
 * Run: node --test post-tool-use-hook.test.cjs install.test.cjs uninstall.test.cjs
 *
 * The trigger detectors are pure functions — most assertions hit those
 * directly. The processEvent() integration test exercises the full
 * stdin → state-file → stderr → fires-log path with HOME pinned to a
 * temp dir and `now` injected for deterministic cooldown math.
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');
const assert = require('node:assert/strict');

const hook = require('./post-tool-use-hook.cjs');
const t = hook.THRESHOLDS;

// ---- fixture helpers ----

function mkEvent(over = {}) {
  return {
    ts: 0,
    tool: 'Edit',
    file: null,
    command: '',
    errored: false,
    output: '',
    stderr: '',
    ...over,
  };
}

function mkTempStateRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'thrash-detect-test-'));
  return root;
}

function makePayload(over = {}) {
  return {
    session_id: 'test-session',
    tool_name: 'Edit',
    tool_input: {},
    tool_response: {},
    hook_event_name: 'PostToolUse',
    ...over,
  };
}

// ---- THRESHOLDS pinned-by-value ----

test('THRESHOLDS mirrors the canonical chat-arch values', () => {
  assert.equal(t.rollingWindow, 30);
  assert.equal(t.editThrashMinSameFile, 4);
  assert.equal(t.editThrashWindow, 10);
  assert.equal(t.readLoopMinSameFile, 6);
  assert.equal(t.readLoopWindow, 12);
  assert.equal(t.testLoopMinConsecutive, 3);
  assert.equal(t.toolFlailDistinctTools, 5);
  assert.equal(t.toolFlailWindow, 6);
  assert.equal(t.cooldownMinutes, 5);
});

// ---- detectEditThrash ----

test('detectEditThrash fires on 4 Edits to same file with 1 errored', () => {
  const f = '/x/y/foo.ts';
  const events = [
    mkEvent({ tool: 'Edit', file: f }),
    mkEvent({ tool: 'Edit', file: f, errored: true }),
    mkEvent({ tool: 'Edit', file: f }),
    mkEvent({ tool: 'Edit', file: f }),
  ];
  const fire = hook.detectEditThrash(events, t);
  assert.ok(fire, 'expected fire');
  assert.equal(fire.kind, 'edit-thrash');
  assert.equal(fire.file, f);
});

test('detectEditThrash does NOT fire when no edit errored', () => {
  const f = '/x/y/foo.ts';
  const events = [
    mkEvent({ tool: 'Edit', file: f }),
    mkEvent({ tool: 'Edit', file: f }),
    mkEvent({ tool: 'Edit', file: f }),
    mkEvent({ tool: 'Edit', file: f }),
  ];
  assert.equal(hook.detectEditThrash(events, t), null);
});

test('detectEditThrash does NOT fire when edits are split across files', () => {
  const events = [
    mkEvent({ tool: 'Edit', file: '/a.ts', errored: true }),
    mkEvent({ tool: 'Edit', file: '/b.ts' }),
    mkEvent({ tool: 'Edit', file: '/c.ts' }),
    mkEvent({ tool: 'Edit', file: '/d.ts' }),
  ];
  assert.equal(hook.detectEditThrash(events, t), null);
});

// ---- detectReadLoop ----

test('detectReadLoop fires on 6 Reads of same file with no intervening Edit/Bash', () => {
  const f = '/x/y/foo.ts';
  const events = Array.from({ length: 6 }, () => mkEvent({ tool: 'Read', file: f }));
  const fire = hook.detectReadLoop(events, t);
  assert.ok(fire);
  assert.equal(fire.kind, 'read-loop');
  assert.equal(fire.file, f);
});

test('detectReadLoop does NOT fire if an Edit intervenes', () => {
  const f = '/x/y/foo.ts';
  const events = [
    mkEvent({ tool: 'Read', file: f }),
    mkEvent({ tool: 'Read', file: f }),
    mkEvent({ tool: 'Edit', file: f }),
    mkEvent({ tool: 'Read', file: f }),
    mkEvent({ tool: 'Read', file: f }),
    mkEvent({ tool: 'Read', file: f }),
    mkEvent({ tool: 'Read', file: f }),
  ];
  assert.equal(hook.detectReadLoop(events, t), null);
});

// ---- detectTestLoop ----

test('detectTestLoop fires on 3 consecutive errored test runs with no Edit between', () => {
  const events = [
    mkEvent({ tool: 'Bash', command: 'pnpm test', errored: true }),
    mkEvent({ tool: 'Bash', command: 'pnpm test', errored: true }),
    mkEvent({ tool: 'Bash', command: 'pnpm test', errored: true }),
  ];
  const fire = hook.detectTestLoop(events, t);
  assert.ok(fire);
  assert.equal(fire.kind, 'test-loop');
  assert.equal(fire.count, 3);
});

test('detectTestLoop does NOT fire when an Edit intervenes', () => {
  const events = [
    mkEvent({ tool: 'Bash', command: 'pnpm test', errored: true }),
    mkEvent({ tool: 'Edit', file: '/x.ts' }),
    mkEvent({ tool: 'Bash', command: 'pnpm test', errored: true }),
    mkEvent({ tool: 'Bash', command: 'pnpm test', errored: true }),
  ];
  assert.equal(hook.detectTestLoop(events, t), null);
});

test('detectTestLoop does NOT fire when most recent test passed', () => {
  const events = [
    mkEvent({ tool: 'Bash', command: 'pnpm test', errored: true }),
    mkEvent({ tool: 'Bash', command: 'pnpm test', errored: true }),
    mkEvent({ tool: 'Bash', command: 'pnpm test', errored: false }),
  ];
  assert.equal(hook.detectTestLoop(events, t), null);
});

// ---- detectToolFlail ----

test('detectToolFlail fires on 5 distinct tools in last 6 with no success', () => {
  const events = [
    mkEvent({ tool: 'Read', file: '/a' }),
    mkEvent({ tool: 'Grep' }),
    mkEvent({ tool: 'Glob' }),
    mkEvent({ tool: 'Bash', command: 'ls', errored: true }),
    mkEvent({ tool: 'WebFetch' }),
    mkEvent({ tool: 'TodoWrite' }),
  ];
  const fire = hook.detectToolFlail(events, t);
  assert.ok(fire);
  assert.equal(fire.kind, 'tool-flail');
});

test('detectToolFlail does NOT fire if a successful Edit is in the window', () => {
  const events = [
    mkEvent({ tool: 'Read', file: '/a' }),
    mkEvent({ tool: 'Grep' }),
    mkEvent({ tool: 'Glob' }),
    mkEvent({ tool: 'Edit', file: '/x.ts' }),  // success
    mkEvent({ tool: 'WebFetch' }),
    mkEvent({ tool: 'TodoWrite' }),
  ];
  assert.equal(hook.detectToolFlail(events, t), null);
});

// ---- classifyBashCommand ----

test('classifyBashCommand recognizes common test/build/commit forms', () => {
  assert.equal(hook.classifyBashCommand('pnpm test'), 'test');
  assert.equal(hook.classifyBashCommand('npm run test'), 'test');
  assert.equal(hook.classifyBashCommand('vitest run'), 'test');
  assert.equal(hook.classifyBashCommand('pytest -q'), 'test');
  assert.equal(hook.classifyBashCommand('go test ./...'), 'test');
  assert.equal(hook.classifyBashCommand('node --test foo.test.cjs'), 'test');
  assert.equal(hook.classifyBashCommand('pnpm build'), 'build');
  assert.equal(hook.classifyBashCommand('tsc -p .'), 'build');
  assert.equal(hook.classifyBashCommand('git commit -m wip'), 'commit');
  assert.equal(hook.classifyBashCommand('ls -la'), 'other');
});

// ---- processEvent integration ----

test('processEvent: env-gate off ⇒ skipped', () => {
  const root = mkTempStateRoot();
  const payload = makePayload();
  const r = hook.processEvent(JSON.stringify(payload), {}, { stateRoot: root, now: 1 });
  assert.equal(r.skipped, 'env-gate');
});

test('processEvent: env-gate on but no thrash ⇒ no fire, state file written', () => {
  const root = mkTempStateRoot();
  const r = hook.processEvent(
    JSON.stringify(makePayload({ tool_name: 'Read', tool_input: { file_path: '/x.ts' } })),
    { CHATARCH_THRASH_DETECT: '1' },
    { stateRoot: root, now: 1 },
  );
  assert.equal(r.fired, null);
  assert.ok(fs.existsSync(path.join(root, 'test-session.json')));
});

test('processEvent: edit-thrash sequence fires and writes fires.jsonl', () => {
  const root = mkTempStateRoot();
  const env = { CHATARCH_THRASH_DETECT: '1' };
  const file = '/x/y/foo.ts';
  // First 3 Edits — no fire yet.
  for (let i = 0; i < 3; i++) {
    hook.processEvent(
      JSON.stringify(makePayload({
        tool_name: 'Edit',
        tool_input: { file_path: file },
        tool_response: { is_error: i === 1 },
      })),
      env, { stateRoot: root, now: 100 + i },
    );
  }
  // 4th Edit — fires.
  const r = hook.processEvent(
    JSON.stringify(makePayload({
      tool_name: 'Edit',
      tool_input: { file_path: file },
      tool_response: {},
    })),
    env, { stateRoot: root, now: 200 },
  );
  assert.ok(r.fired, 'expected fire');
  assert.equal(r.fired.kind, 'edit-thrash');
  const firesLog = fs.readFileSync(path.join(root, 'fires.jsonl'), 'utf8');
  assert.match(firesLog, /"kind":"edit-thrash"/);
});

test('processEvent: cooldown suppresses second fire within 5 minutes', () => {
  const root = mkTempStateRoot();
  const env = { CHATARCH_THRASH_DETECT: '1' };
  const file = '/x/y/foo.ts';

  // Fire once.
  for (let i = 0; i < 3; i++) {
    hook.processEvent(
      JSON.stringify(makePayload({
        tool_name: 'Edit', tool_input: { file_path: file },
        tool_response: { is_error: i === 0 },
      })),
      env, { stateRoot: root, now: 1000 + i },
    );
  }
  const first = hook.processEvent(
    JSON.stringify(makePayload({ tool_name: 'Edit', tool_input: { file_path: file } })),
    env, { stateRoot: root, now: 2000 },
  );
  assert.ok(first.fired);

  // Second fire attempt 1 minute later — should be skipped by cooldown.
  const second = hook.processEvent(
    JSON.stringify(makePayload({
      tool_name: 'Edit', tool_input: { file_path: file },
      tool_response: { is_error: true },
    })),
    env, { stateRoot: root, now: 2000 + 60_000 },
  );
  assert.equal(second.skipped, 'cooldown');
});

test('processEvent: cooldown elapses after 5 minutes', () => {
  const root = mkTempStateRoot();
  const env = { CHATARCH_THRASH_DETECT: '1' };
  // Manually seed state with a fire 6 minutes ago and a built-up window.
  const state = {
    events: Array.from({ length: 4 }, (_, i) => ({
      ts: 0, tool: 'Edit', file: '/foo.ts',
      command: '', errored: i === 0, output: '', stderr: '',
    })),
    lastFireAt: 1_000_000,
    suppressed: false,
  };
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, 'test-session.json'), JSON.stringify(state));
  const r = hook.processEvent(
    JSON.stringify(makePayload({ tool_name: 'Edit', tool_input: { file_path: '/foo.ts' } })),
    env, { stateRoot: root, now: 1_000_000 + 6 * 60_000 },
  );
  // Cooldown over and 4 same-file edits — should re-fire.
  assert.ok(r.fired, 'expected re-fire after cooldown');
});

test('processEvent: successful test pass clears the window', () => {
  const root = mkTempStateRoot();
  const env = { CHATARCH_THRASH_DETECT: '1' };
  // Build up a near-firing window.
  for (let i = 0; i < 3; i++) {
    hook.processEvent(
      JSON.stringify(makePayload({
        tool_name: 'Edit', tool_input: { file_path: '/foo.ts' },
        tool_response: { is_error: i === 0 },
      })),
      env, { stateRoot: root, now: 10 + i },
    );
  }
  // A passing test should clear.
  const clear = hook.processEvent(
    JSON.stringify(makePayload({
      tool_name: 'Bash', tool_input: { command: 'pnpm test' },
      tool_response: {},
    })),
    env, { stateRoot: root, now: 20 },
  );
  assert.equal(clear.cleared, 'test');
  const saved = JSON.parse(fs.readFileSync(path.join(root, 'test-session.json'), 'utf8'));
  assert.equal(saved.events.length, 0);
  assert.equal(saved.lastFireAt, 0);
});

test('processEvent: successful commit clears the window', () => {
  const root = mkTempStateRoot();
  const env = { CHATARCH_THRASH_DETECT: '1' };
  hook.processEvent(
    JSON.stringify(makePayload({
      tool_name: 'Edit', tool_input: { file_path: '/foo.ts' },
      tool_response: { is_error: true },
    })),
    env, { stateRoot: root, now: 1 },
  );
  const r = hook.processEvent(
    JSON.stringify(makePayload({
      tool_name: 'Bash', tool_input: { command: 'git commit -m wip' },
      tool_response: {},
    })),
    env, { stateRoot: root, now: 2 },
  );
  assert.equal(r.cleared, 'commit');
});

test('processEvent: user-override marker in transcript suppresses for rest of session', () => {
  const root = mkTempStateRoot();
  const env = { CHATARCH_THRASH_DETECT: '1' };
  // Write a fake transcript containing an "I know" user turn.
  const transcript = path.join(root, 'transcript.jsonl');
  fs.writeFileSync(
    transcript,
    JSON.stringify({ type: 'user', message: { role: 'user', content: 'I know what I am doing here' } }) + '\n',
    'utf8',
  );
  // Build up a thrash window.
  for (let i = 0; i < 4; i++) {
    const r = hook.processEvent(
      JSON.stringify(makePayload({
        tool_name: 'Edit', tool_input: { file_path: '/foo.ts' },
        tool_response: { is_error: i === 0 },
        transcript_path: transcript,
      })),
      env, { stateRoot: root, now: 100 + i },
    );
    if (i === 3) {
      assert.equal(r.skipped, 'user-override', `expected user-override at i=${i}, got ${JSON.stringify(r)}`);
    }
  }
});

test('processEvent: bad JSON stdin does not throw', () => {
  const root = mkTempStateRoot();
  const r = hook.processEvent('not-json', { CHATARCH_THRASH_DETECT: '1' }, { stateRoot: root, now: 1 });
  assert.equal(r.skipped, 'bad-json');
});

// ---- isClearEvent / pushEvent / cooldownActive (unit) ----

test('isClearEvent: errored Bash test is NOT a clear', () => {
  assert.equal(hook.isClearEvent(mkEvent({ tool: 'Bash', command: 'pnpm test', errored: true })), false);
});

test('isClearEvent: passing Bash test IS a clear', () => {
  assert.equal(hook.isClearEvent(mkEvent({ tool: 'Bash', command: 'pnpm test', errored: false })), true);
});

test('pushEvent caps the rolling window at THRESHOLDS.rollingWindow', () => {
  const state = { events: [] };
  for (let i = 0; i < t.rollingWindow + 10; i++) {
    hook.pushEvent(state, mkEvent({ tool: 'Read', file: `/${i}.ts` }), t);
  }
  assert.equal(state.events.length, t.rollingWindow);
});

test('cooldownActive: true within cooldown, false after', () => {
  assert.equal(hook.cooldownActive({ lastFireAt: 1_000_000 }, 1_000_000 + 60_000, t), true);
  assert.equal(hook.cooldownActive({ lastFireAt: 1_000_000 }, 1_000_000 + 5 * 60_000 + 1, t), false);
  assert.equal(hook.cooldownActive({ lastFireAt: 0 }, 9999, t), false);
});
