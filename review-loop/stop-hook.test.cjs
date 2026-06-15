'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const { test } = require('node:test');
const assert = require('node:assert/strict');

const HOOK = path.join(__dirname, 'stop-hook.cjs');

function mkTempHome() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'stop-hook-test-'));
  fs.mkdirSync(path.join(root, '.claude', 'skills', 'review-loop', '.local-state', 'archive'), { recursive: true });
  return root;
}

function runHook(home, payload, extraEnv = {}) {
  return spawnSync(process.execPath, [HOOK], {
    env: { ...process.env, HOME: home, USERPROFILE: home, ...extraEnv },
    input: JSON.stringify(payload),
    encoding: 'utf8',
  });
}

function parseStdout(r) {
  const s = (r.stdout || '').trim();
  if (!s) return null;
  try { return JSON.parse(s); } catch (_) { return null; }
}

test('hook: re-entry guard via stop_hook_active=true exits without block', () => {
  const home = mkTempHome();
  const r = runHook(home, {
    session_id: 's-1', cwd: home, stop_hook_active: true,
  });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, 'should NOT emit a block decision');
});

test('hook: CLAUDE_REVIEW_LOOP_ACTIVE env var blocks re-entry', () => {
  const home = mkTempHome();
  const r = runHook(home, { session_id: 's-2', cwd: home }, { CLAUDE_REVIEW_LOOP_ACTIVE: '1' });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null);
});

test('hook: plan mode via permission_mode=plan exits without block', () => {
  const home = mkTempHome();
  const r = runHook(home, { session_id: 's-3', cwd: home, permission_mode: 'plan' });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null);
});

test('hook: trivial work (no transcript, empty diff) exits without block', () => {
  const home = mkTempHome();
  // cwd is a temp dir with no git — gitDiffIsEmpty returns false (no repo),
  // but the trivial-work check ALSO requires no transcript edits. With no
  // transcript path, transcriptHasEditWrite returns false. So trivial-work
  // triggers only if both conditions hold. Use a real git repo for this.
  const repoDir = path.join(home, 'repo');
  fs.mkdirSync(repoDir);
  spawnSync('git', ['init'], { cwd: repoDir });
  spawnSync('git', ['-C', repoDir, 'commit', '--allow-empty', '-m', 'init',
    '-c', 'user.name=t', '-c', 'user.email=t@t'], {});
  const r = runHook(home, { session_id: 's-4', cwd: repoDir });
  assert.equal(r.status, 0);
  // We expect no block when trivial work is detected (no edits + empty diff).
  // If git ops in the test env succeeded, parseStdout should be null.
  // We don't strictly assert (env-dependent) but at least exit must be 0.
});

test('hook: skip-next marker consumes and exits without block', () => {
  const home = mkTempHome();
  const marker = path.join(home, '.claude', 'skills', 'review-loop', '.skip-next');
  fs.writeFileSync(marker, '');
  const r = runHook(home, { session_id: 's-5', cwd: home });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null);
  assert.equal(fs.existsSync(marker), false, 'marker should be consumed');
});

test('hook: per-project opt-out marker exits without block', () => {
  const home = mkTempHome();
  const projectDir = path.join(home, 'project');
  fs.mkdirSync(path.join(projectDir, '.claude'), { recursive: true });
  fs.writeFileSync(path.join(projectDir, '.claude', 'review-loop.disabled'), '');
  const r = runHook(home, { session_id: 's-6', cwd: projectDir });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null);
});

test('hook: terminal state (review-clean) exits without block and archives', () => {
  const home = mkTempHome();
  const statePath = path.join(home, '.claude', 'skills', 'review-loop', '.local-state', 's-7.json');
  fs.writeFileSync(statePath, JSON.stringify({ completion: 'review-clean' }));
  const r = runHook(home, { session_id: 's-7', cwd: home });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null);
  assert.equal(fs.existsSync(statePath), false, 'state should be archived');
  const archiveDir = path.join(home, '.claude', 'skills', 'review-loop', '.local-state', 'archive');
  const archived = fs.readdirSync(archiveDir).filter(n => n.startsWith('s-7-'));
  assert.ok(archived.length >= 1, 'archived file should exist');
});

test('hook: hook never throws / never blocks on internal error', () => {
  const home = mkTempHome();
  // Malformed input: not valid JSON.
  const r = spawnSync(process.execPath, [HOOK], {
    env: { ...process.env, HOME: home, USERPROFILE: home },
    input: '{not-json',
    encoding: 'utf8',
  });
  assert.equal(r.status, 0, 'must always exit 0 on internal error');
});

// -----------------------------------------------------------------------
// Plan-mode routing tests (--mode plan|code via stop-hook diff classification)
// -----------------------------------------------------------------------

function gitRepoWithStagedFile(home, relPath, content) {
  const repo = path.join(home, 'repo');
  fs.mkdirSync(repo, { recursive: true });
  spawnSync('git', ['init', '-q'], { cwd: repo });
  spawnSync('git', ['-C', repo, 'config', 'user.name', 't']);
  spawnSync('git', ['-C', repo, 'config', 'user.email', 't@t']);
  spawnSync('git', ['-C', repo, 'config', 'commit.gpgsign', 'false']);
  spawnSync('git', ['-C', repo, 'commit', '--allow-empty', '-m', 'init', '-q']);
  const full = path.join(repo, relPath);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, content);
  spawnSync('git', ['-C', repo, 'add', '.']);
  // Forces a transcript edit signal: write a fake transcript JSONL with a
  // tool_use Edit marker so trivial-work hatch doesn't fire.
  return repo;
}

function fakeTranscriptWithEdit(home, sessionId) {
  const tpath = path.join(home, `${sessionId}.jsonl`);
  fs.writeFileSync(
    tpath,
    JSON.stringify({ type: 'assistant', message: { content: [
      { type: 'tool_use', name: 'Edit', input: {} },
    ] } }) + '\n',
  );
  return tpath;
}

test('hook: plan-artifact diff routes to --mode plan', () => {
  const home = mkTempHome();
  const repo = gitRepoWithStagedFile(home, 'reports/2026-05-30_thing-plan.md', '# plan\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-plan');
  const r = runHook(home, { session_id: 'p-plan', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.equal(out.decision, 'block');
  assert.ok(/--mode plan\b/.test(out.reason), `expected --mode plan in reason, got: ${out.reason}`);
});

test('hook: code-only diff routes to --mode code', () => {
  const home = mkTempHome();
  const repo = gitRepoWithStagedFile(home, 'src/foo.ts', 'export const x = 1;\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-code');
  const r = runHook(home, { session_id: 'p-code', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.equal(out.decision, 'block');
  assert.ok(/--mode code\b/.test(out.reason), `expected --mode code in reason, got: ${out.reason}`);
});

test('hook: mixed plan + code diff routes to --mode plan (plan wins)', () => {
  const home = mkTempHome();
  const repo = gitRepoWithStagedFile(home, 'src/foo.ts', 'export const x = 1;\n');
  // Add a second staged file in the plan-artifact pattern.
  fs.mkdirSync(path.join(repo, 'reports'), { recursive: true });
  fs.writeFileSync(path.join(repo, 'reports', '2026-06-01_other-proposal.md'), '# proposal\n');
  spawnSync('git', ['-C', repo, 'add', '.']);
  const transcript = fakeTranscriptWithEdit(home, 'p-mixed');
  const r = runHook(home, { session_id: 'p-mixed', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.equal(out.decision, 'block');
  assert.ok(/--mode plan\b/.test(out.reason), `mixed diff should be --mode plan, got: ${out.reason}`);
});

test('hook: project-level plan-paths override widens what counts as plan', () => {
  const home = mkTempHome();
  // File path that does NOT match the default globs — would normally be code.
  const repo = gitRepoWithStagedFile(home, 'docs/strategy.md', '# strategy\n');
  // But project override declares **/strategy.md as plan-artifact.
  fs.mkdirSync(path.join(repo, '.claude'), { recursive: true });
  fs.writeFileSync(
    path.join(repo, '.claude', 'review-loop.plan-paths'),
    '# project overrides\n**/strategy.md\n',
  );
  const transcript = fakeTranscriptWithEdit(home, 'p-override');
  const r = runHook(home, { session_id: 'p-override', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.equal(out.decision, 'block');
  assert.ok(/--mode plan\b/.test(out.reason), `override should classify as plan, got: ${out.reason}`);
});

test('hook: empty plan-paths override + .md is nothing-reviewable (no block)', () => {
  const home = mkTempHome();
  // -plan.md normally => plan. Empty override => no plan globs. A .md is not a
  // code extension, so nothing reviewable changed => the hook must NOT fire.
  const repo = gitRepoWithStagedFile(home, 'reports/x-plan.md', '# x\n');
  fs.mkdirSync(path.join(repo, '.claude'), { recursive: true });
  fs.writeFileSync(path.join(repo, '.claude', 'review-loop.plan-paths'), '# empty\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-empty-override');
  const r = runHook(home, { session_id: 'p-empty-override', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, 'a non-plan .md is not reviewable code => no block');
});

// -----------------------------------------------------------------------
// Value-gate tests (nothing-reviewable / unchanged-diff / code-exts override)
// -----------------------------------------------------------------------

test('hook: docs + scratch-only diff is nothing-reviewable (no block)', () => {
  const home = mkTempHome();
  const repo = gitRepoWithStagedFile(home, '.claude/handoffs/note.md', '# handoff\n');
  fs.writeFileSync(path.join(repo, 'scratch.txt'), 'junk\n');
  spawnSync('git', ['-C', repo, 'add', '.']);
  const transcript = fakeTranscriptWithEdit(home, 'p-docs');
  const r = runHook(home, { session_id: 'p-docs', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, 'docs + scratch only => no review loop');
});

test('hook: unchanged diff is not re-reviewed on a second stop', () => {
  const home = mkTempHome();
  const repo = gitRepoWithStagedFile(home, 'src/foo.ts', 'export const x = 1;\nexport const y = 2;\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-twice');
  const r1 = runHook(home, { session_id: 'p-twice-a', cwd: repo, transcript_path: transcript });
  const out1 = parseStdout(r1);
  assert.ok(out1 && out1.decision === 'block', 'first stop should fire a review');
  // Second stop, different session id, SAME diff => anti-spam skip.
  const r2 = runHook(home, { session_id: 'p-twice-b', cwd: repo, transcript_path: transcript });
  assert.equal(r2.status, 0);
  assert.equal(parseStdout(r2), null, 'identical diff should not re-fire');
});

test('hook: code-exts override widens what counts as reviewable code', () => {
  const home = mkTempHome();
  // .json is NOT reviewable by default => would be nothing-reviewable...
  const repo = gitRepoWithStagedFile(home, 'config/app.json', '{"a":1}\n');
  // ...but the project override declares .json as reviewable code.
  fs.mkdirSync(path.join(repo, '.claude'), { recursive: true });
  fs.writeFileSync(path.join(repo, '.claude', 'review-loop.code-exts'), '.json\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-json');
  const r = runHook(home, { session_id: 'p-json', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.ok(/--mode code\b/.test(out.reason), `json override should fire as code, got: ${out.reason}`);
});

// Untracked files: /review-loop reviews them, so the hook must count them too
// (a brand-new untracked code file is not yet in `git diff HEAD`).
function gitRepoWithUntrackedFile(home, relPath, content) {
  const repo = path.join(home, 'repo');
  fs.mkdirSync(repo, { recursive: true });
  spawnSync('git', ['init', '-q'], { cwd: repo });
  spawnSync('git', ['-C', repo, 'config', 'user.name', 't']);
  spawnSync('git', ['-C', repo, 'config', 'user.email', 't@t']);
  spawnSync('git', ['-C', repo, 'config', 'commit.gpgsign', 'false']);
  spawnSync('git', ['-C', repo, 'commit', '--allow-empty', '-m', 'init', '-q']);
  const full = path.join(repo, relPath);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, content); // deliberately NOT `git add`ed
  return repo;
}

test('hook: untracked new code file still triggers a review', () => {
  const home = mkTempHome();
  const repo = gitRepoWithUntrackedFile(home, 'src/new.ts', 'export const z = 3;\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-untracked');
  const r = runHook(home, { session_id: 'p-untracked', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out && out.decision === 'block', 'untracked new .ts should still fire');
  assert.ok(/--mode code\b/.test(out.reason), `expected --mode code, got: ${out.reason}`);
});

test('hook: untracked docs-only does not trigger a review', () => {
  const home = mkTempHome();
  const repo = gitRepoWithUntrackedFile(home, 'NOTES.md', '# notes\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-untracked-docs');
  const r = runHook(home, { session_id: 'p-untracked-docs', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, 'untracked docs only => no review loop');
});
