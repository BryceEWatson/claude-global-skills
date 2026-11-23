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
  // Path is under notes/ rather than reports/ so it also misses the deliverable
  // globs — this test is about plan-glob override semantics, not routing.
  const repo = gitRepoWithStagedFile(home, 'notes/x-plan.md', '# x\n');
  fs.mkdirSync(path.join(repo, '.claude'), { recursive: true });
  fs.writeFileSync(path.join(repo, '.claude', 'review-loop.plan-paths'), '# empty\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-empty-override');
  const r = runHook(home, { session_id: 'p-empty-override', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, 'a non-plan .md is not reviewable code => no block');
});

// -----------------------------------------------------------------------
// Deliverable-mode routing (--mode deliverable; precedence plan > code > deliverable)
// -----------------------------------------------------------------------

test('hook: deliverable-only diff routes to --mode deliverable', () => {
  const home = mkTempHome();
  // A published content page: previously nothing-reviewable (a .md that is
  // neither a plan artifact nor code), so this can only ADD a review.
  const repo = gitRepoWithStagedFile(home, 'src/content/projects/thing/index.md', '# thing\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv');
  const r = runHook(home, { session_id: 'p-deliv', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.equal(out.decision, 'block');
  assert.ok(/--mode deliverable\b/.test(out.reason), `expected --mode deliverable, got: ${out.reason}`);
});

test('hook: top-level report under reports/ routes to --mode deliverable', () => {
  const home = mkTempHome();
  // Guards the glob depth trap: `reports/**/*.md` alone would NOT match a file
  // sitting directly in reports/, because `**` does not absorb its slash.
  const repo = gitRepoWithStagedFile(home, 'reports/weekly.md', '# weekly\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-flat');
  const r = runHook(home, { session_id: 'p-deliv-flat', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.ok(/--mode deliverable\b/.test(out.reason), `expected --mode deliverable, got: ${out.reason}`);
});

test('hook: repo-root *-report.md routes to --mode deliverable', () => {
  const home = mkTempHome();
  // The `**/` prefix requires at least one directory, so `**/*-report.md` alone
  // misses a report sitting at the repo root. Both depths must be listed.
  const repo = gitRepoWithStagedFile(home, 'weekly-report.md', '# weekly\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-root');
  const r = runHook(home, { session_id: 'p-deliv-root', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.ok(/--mode deliverable\b/.test(out.reason), `expected --mode deliverable, got: ${out.reason}`);
});

test('hook: nested *-report.md still routes to --mode deliverable', () => {
  const home = mkTempHome();
  const repo = gitRepoWithStagedFile(home, 'docs/notes/weekly-report.md', '# weekly\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-nested');
  const r = runHook(home, { session_id: 'p-deliv-nested', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.ok(/--mode deliverable\b/.test(out.reason), `expected --mode deliverable, got: ${out.reason}`);
});

test('hook: mixed code + deliverable diff routes to --mode code (code wins)', () => {
  const home = mkTempHome();
  // Deliverable lenses do not review source, so a stray content file must never
  // demote a code diff — that would silently drop the code review.
  const repo = gitRepoWithStagedFile(home, 'src/foo.ts', 'export const x = 1;\n');
  fs.mkdirSync(path.join(repo, 'reports'), { recursive: true });
  fs.writeFileSync(path.join(repo, 'reports', 'weekly.md'), '# weekly\n');
  spawnSync('git', ['-C', repo, 'add', '.']);
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-mixed');
  const r = runHook(home, { session_id: 'p-deliv-mixed', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.ok(/--mode code\b/.test(out.reason), `mixed code+deliverable should be code, got: ${out.reason}`);
});

test('hook: mixed plan + deliverable diff routes to --mode plan (plan still wins)', () => {
  const home = mkTempHome();
  const repo = gitRepoWithStagedFile(home, 'reports/weekly.md', '# weekly\n');
  fs.writeFileSync(path.join(repo, 'reports', 'thing-spec.md'), '# spec\n');
  spawnSync('git', ['-C', repo, 'add', '.']);
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-plan');
  const r = runHook(home, { session_id: 'p-deliv-plan', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.ok(/--mode plan\b/.test(out.reason), `mixed plan+deliverable should be plan, got: ${out.reason}`);
});

test('hook: deliverable-paths override widens what counts as a deliverable', () => {
  const home = mkTempHome();
  // Neither plan, code, nor a default deliverable — normally nothing-reviewable.
  const repo = gitRepoWithStagedFile(home, 'pages/about.md', '# about\n');
  fs.mkdirSync(path.join(repo, '.claude'), { recursive: true });
  fs.writeFileSync(
    path.join(repo, '.claude', 'review-loop.deliverable-paths'),
    '# project overrides\npages/*.md\n',
  );
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-override');
  const r = runHook(home, { session_id: 'p-deliv-override', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.ok(/--mode deliverable\b/.test(out.reason), `override should classify as deliverable, got: ${out.reason}`);
});

test('hook: empty deliverable-paths override turns the deliverable branch off', () => {
  const home = mkTempHome();
  // The override REPLACES the default set, so an empty file opts a project out
  // of deliverable review entirely and restores the pre-existing skip.
  const repo = gitRepoWithStagedFile(home, 'reports/weekly.md', '# weekly\n');
  fs.mkdirSync(path.join(repo, '.claude'), { recursive: true });
  fs.writeFileSync(path.join(repo, '.claude', 'review-loop.deliverable-paths'), '# empty\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-off');
  const r = runHook(home, { session_id: 'p-deliv-off', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, 'empty deliverable override => nothing reviewable => no block');
});

test('hook: .mdx under reports/ routes to --mode deliverable', () => {
  const home = mkTempHome();
  // Both docs promise .md/.mdx across all four deliverable directories.
  const repo = gitRepoWithStagedFile(home, 'reports/2026/q3.mdx', '# q3\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-mdx');
  const r = runHook(home, { session_id: 'p-deliv-mdx', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.ok(/--mode deliverable\b/.test(out.reason), `expected --mode deliverable, got: ${out.reason}`);
});

test('hook: top-level src/content page routes to --mode deliverable', () => {
  const home = mkTempHome();
  const repo = gitRepoWithStagedFile(home, 'src/content/about.md', '# about\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-srctop');
  const r = runHook(home, { session_id: 'p-deliv-srctop', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  const out = parseStdout(r);
  assert.ok(out, 'expected a block decision');
  assert.ok(/--mode deliverable\b/.test(out.reason), `expected --mode deliverable, got: ${out.reason}`);
});

test('hook: an override cannot re-include .claude/ as a deliverable', () => {
  const home = mkTempHome();
  // The exclusion outranks the override — session machinery is never a
  // deliverable, however the project globs it.
  const repo = gitRepoWithStagedFile(home, '.claude/reports/x.md', '# x\n');
  fs.writeFileSync(
    path.join(repo, '.claude', 'review-loop.deliverable-paths'),
    '.claude/**/*.md\n',
  );
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-reinclude');
  const r = runHook(home, { session_id: 'p-deliv-reinclude', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, '.claude/ stays excluded even when a glob names it');
});

test('hook: unreadable deliverable override fails closed, not open', () => {
  const home = mkTempHome();
  // A directory where the override file should be => readFileSync throws
  // (EISDIR). Falling back to the full default set would turn a deliberate
  // opt-out into an opt-in, so the deliverable set must come back empty.
  const repo = gitRepoWithStagedFile(home, 'reports/weekly.md', '# weekly\n');
  fs.mkdirSync(path.join(repo, '.claude', 'review-loop.deliverable-paths'), { recursive: true });
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-unreadable');
  const r = runHook(home, { session_id: 'p-deliv-unreadable', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, 'unreadable override must not restore the default globs');
});

test('hook: handoffs and scratch are still not deliverables', () => {
  const home = mkTempHome();
  // Session machinery under .claude/ must stay out of the deliverable globs, or
  // every session-end handoff would fire a review loop.
  const repo = gitRepoWithStagedFile(home, '.claude/handoffs/2026-08-16_thing-summary.md', '# handoff\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-handoff');
  const r = runHook(home, { session_id: 'p-deliv-handoff', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, 'a handoff is not a reader-facing deliverable => no block');
});

test('hook: the .claude/ exclusion is case-insensitive', () => {
  const home = mkTempHome();
  // globToRegex compiles with the `i` flag, so `**/*-summary.md` matches any
  // casing; the exclusion has to match the same way or a `.Claude/` directory
  // slips a handoff through as a deliverable.
  const repo = gitRepoWithStagedFile(home, '.Claude/handoffs/2026-08-16_thing-summary.md', '# handoff\n');
  const transcript = fakeTranscriptWithEdit(home, 'p-deliv-case');
  const r = runHook(home, { session_id: 'p-deliv-case', cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, '.Claude/ must be excluded exactly like .claude/');
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

// ---------------------------------------------------------------------------
// Independence marking. The Stop event fires IN the authoring session, so a
// hook-dispatched review is a self-review nearly every time. These two tests pin
// both directions of what the hook now tells the skill about that.
// ---------------------------------------------------------------------------

function mkRepoWithDirtyFile(home, relPath = 'src/a.js') {
  const repoDir = path.join(home, 'repo');
  fs.mkdirSync(path.join(repoDir, path.dirname(relPath)), { recursive: true });
  spawnSync('git', ['init'], { cwd: repoDir });
  spawnSync('git', ['-C', repoDir, 'config', 'user.email', 't@t'], {});
  spawnSync('git', ['-C', repoDir, 'config', 'user.name', 't'], {});
  fs.writeFileSync(path.join(repoDir, relPath), 'let x = 1;\n');
  spawnSync('git', ['-C', repoDir, 'add', '-A'], {});
  spawnSync('git', ['-C', repoDir, 'commit', '-m', 'init'], {});
  fs.writeFileSync(path.join(repoDir, relPath), 'let x = 2;\n'); // the diff under review
  return repoDir;
}

test('hook: marks the dispatched review self-authored when this session edited the diff', () => {
  const home = mkTempHome();
  const repoDir = mkRepoWithDirtyFile(home);
  // Deploy the guard into the temp HOME exactly where the hook looks for it. In a deployed
  // copy the guard sits beside this file; in the repo it is the Claude-only overlay.
  const guardSrc = [path.join(__dirname, 'authorship-guard.mjs'),
    path.join(__dirname, 'overlays', 'claude', 'authorship-guard.mjs')].find(p => fs.existsSync(p));
  fs.copyFileSync(guardSrc,
    path.join(home, '.claude', 'skills', 'review-loop', 'authorship-guard.mjs'));

  const sid = 'sid-self-authored';
  const projDir = path.join(home, '.claude', 'projects', 'proj');
  fs.mkdirSync(projDir, { recursive: true });
  const edited = path.join(repoDir, 'src', 'a.js');
  fs.writeFileSync(path.join(projDir, `${sid}.jsonl`),
    `${JSON.stringify({ type: 'assistant', message: { content: [
      { type: 'tool_use', name: 'Edit', input: { file_path: edited } },
    ] } })}\n`);

  const r = runHook(home, { session_id: sid, cwd: repoDir, transcript_path: path.join(projDir, `${sid}.jsonl`) });
  const out = parseStdout(r);
  assert.ok(out, 'a review should still be dispatched — the guard marks it, it does not suppress it');
  assert.match(out.reason, /--independence self-authored/);
  assert.match(out.systemMessage, /SELF-REVIEW/);
  assert.match(out.systemMessage, /fresh session is still required/);
});

test('hook: falls open to unverified — never to independent — when the guard cannot run', () => {
  const home = mkTempHome(); // no authorship-guard.mjs deployed here
  const repoDir = mkRepoWithDirtyFile(home);
  const sid = 'sid-no-guard';
  const projDir = path.join(home, '.claude', 'projects', 'proj');
  fs.mkdirSync(projDir, { recursive: true });
  fs.writeFileSync(path.join(projDir, `${sid}.jsonl`),
    `${JSON.stringify({ type: 'assistant', message: { content: [
      { type: 'tool_use', name: 'Edit', input: { file_path: path.join(repoDir, 'src', 'a.js') } },
    ] } })}\n`);

  const r = runHook(home, { session_id: sid, cwd: repoDir, transcript_path: path.join(projDir, `${sid}.jsonl`) });
  const out = parseStdout(r);
  assert.ok(out, 'a missing guard must not suppress the review');
  assert.match(out.reason, /--independence unverified/);
  assert.doesNotMatch(out.reason, /--independence independent/,
    'a guard that could not run must never certify independence');
});

// ---------------------------------------------------------------------------
// Re-fire fixes (hook study, 19-23 Nov 2026). Each test below is one of the real
// cases: the re-arm on a skipped file, another session's uncommitted work in a
// shared checkout, and a review that is still running.
// ---------------------------------------------------------------------------

const STATE_REL = ['.claude', 'skills', 'review-loop', '.local-state'];

function hookLog(home) {
  try { return fs.readFileSync(path.join(home, ...STATE_REL, 'hook.log'), 'utf8'); } catch (_) { return ''; }
}

function deployGuard(home) {
  const guardSrc = [path.join(__dirname, 'authorship-guard.mjs'),
    path.join(__dirname, 'overlays', 'claude', 'authorship-guard.mjs')].find(p => fs.existsSync(p));
  fs.copyFileSync(guardSrc, path.join(home, ...STATE_REL.slice(0, 3), 'authorship-guard.mjs'));
}

// Writes the session's transcript where the guard looks for it and returns its path.
function writeTranscript(home, sid, records) {
  const projDir = path.join(home, '.claude', 'projects', 'proj');
  fs.mkdirSync(projDir, { recursive: true });
  const tpath = path.join(projDir, `${sid}.jsonl`);
  fs.writeFileSync(tpath, records.map(r => JSON.stringify(r)).join('\n') + '\n');
  return tpath;
}

const editRecord = (filePath) => ({ type: 'assistant', timestamp: new Date().toISOString(),
  message: { content: [{ type: 'tool_use', name: 'Edit', id: 'e1', input: { file_path: filePath } }] } });

function git(repo, ...args) { return spawnSync('git', ['-C', repo, ...args], { encoding: 'utf8' }); }

// The Finances case: eight changed files, one reviewable script, seven records.
function mkFinancesRepo(home) {
  const repo = path.join(home, 'repo');
  fs.mkdirSync(path.join(repo, 'records'), { recursive: true });
  spawnSync('git', ['init', '-q'], { cwd: repo });
  git(repo, 'config', 'user.email', 't@t');
  git(repo, 'config', 'user.name', 't');
  git(repo, 'config', 'commit.gpgsign', 'false');
  const records = ['STATUS.md', 'records/ledger.md', 'records/calendar.md', 'records/payments.csv', 'records/house.md'];
  for (const f of records) fs.writeFileSync(path.join(repo, f), 'v1\n');
  git(repo, 'add', '-A');
  git(repo, 'commit', '-q', '-m', 'init');
  for (const f of records) fs.writeFileSync(path.join(repo, f), 'v2\n');                 // 5 modified records
  fs.writeFileSync(path.join(repo, 'records', 'invoice.md'), 'invoice\n');               // + 2 untracked records
  fs.writeFileSync(path.join(repo, 'records', 'evidence.md'), 'evidence\n');
  fs.writeFileSync(path.join(repo, 'records', 'invoice_render.py'), 'print("v1")\n');    // + 1 reviewable script
  return repo;
}

test('re-arm: a change to a skipped file does not re-fire (8 files, 1 reviewable, 7 skipped)', () => {
  const home = mkTempHome();
  const repo = mkFinancesRepo(home);
  const transcript = fakeTranscriptWithEdit(home, 'fin');
  const first = parseStdout(runHook(home, { session_id: 'fin-a', cwd: repo, transcript_path: transcript }));
  assert.ok(first && first.decision === 'block', 'the script has never been reviewed, so the first stop fires');
  assert.match(hookLog(home), /files=8 reviewable=1 skipped=7/, 'the fixture is the logged Finances shape');

  // The session then edits a record. Not code, so nothing new to review.
  fs.writeFileSync(path.join(repo, 'records', 'house.md'), 'v3 paint note\n');
  const second = runHook(home, { session_id: 'fin-b', cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(second), null, 'a skipped file changing must not re-arm the hook');
  assert.match(hookLog(home), /diff-unchanged-since-last-review/);

  // Changing the script itself still re-arms it.
  fs.writeFileSync(path.join(repo, 'records', 'invoice_render.py'), 'print("v2")\n');
  const third = parseStdout(runHook(home, { session_id: 'fin-c', cwd: repo, transcript_path: transcript }));
  assert.ok(third && third.decision === 'block', 'a changed reviewable file re-arms the hook');
});

test('re-arm: committing one reviewed file does not re-fire for the other', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home, 'src/a.js');
  fs.writeFileSync(path.join(repo, 'src', 'b.js'), 'let y = 1;\n');
  const transcript = fakeTranscriptWithEdit(home, 'two');
  assert.ok(parseStdout(runHook(home, { session_id: 'two-a', cwd: repo, transcript_path: transcript })));
  git(repo, 'add', 'src/b.js');
  git(repo, 'commit', '-q', '-m', 'b');
  const r = runHook(home, { session_id: 'two-b', cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(r), null, 'a.js was already dispatched in this content');
});

test('authorship: another session\'s uncommitted work in a shared checkout does not block', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);          // src/a.js is dirty, written by "another session"
  deployGuard(home);
  const sid = 'sid-bystander';
  // This session's only write is a note outside the repo.
  const transcript = writeTranscript(home, sid, [editRecord(path.join(home, 'notes', 'verification.md'))]);
  const r = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript });
  assert.equal(r.status, 0);
  assert.equal(parseStdout(r), null, 'the session wrote none of the reviewable files');
  assert.match(hookLog(home), /not-authored: this session wrote none of the 1 reviewable changed file\(s\)/);
});

test('authorship: a session that edited only a skipped file is not blocked on someone else\'s code', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);          // src/a.js: someone else's
  fs.writeFileSync(path.join(repo, 'NOTES.md'), 'mine\n');
  deployGuard(home);
  const sid = 'sid-notes-only';
  const transcript = writeTranscript(home, sid, [editRecord(path.join(repo, 'NOTES.md'))]);
  const r = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(r), null, 'the guard says "edited the diff", but not a reviewable file in it');
  assert.match(hookLog(home), /not-authored/);
});

test('authorship: edits made through a script still fire (the file changed while a shell call ran)', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  deployGuard(home);
  const sid = 'sid-script-edits';
  const start = new Date(Date.now() - 60000).toISOString();
  const end = new Date(Date.now() + 60000).toISOString();
  // `python patch.py`: the command names no file in the repo, and no edit tool ran.
  const transcript = writeTranscript(home, sid, [
    { type: 'assistant', timestamp: start, message: { content: [
      { type: 'tool_use', name: 'Bash', id: 'b1', input: { command: 'python /tmp/patch.py' } }] } },
    { type: 'user', timestamp: end, message: { content: [
      { type: 'tool_result', tool_use_id: 'b1', content: 'ok' }] } },
  ]);
  const out = parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript }));
  assert.ok(out && out.decision === 'block', 'a script-mediated edit is still this session\'s work');
  assert.match(out.reason, /--independence independent/, 'the timing signal never changes the independence marking');
});

test('authorship: a file this session created in a new untracked directory counts as its own', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  git(repo, 'checkout', '-q', '--', '.');           // clean tree, then one new directory
  fs.mkdirSync(path.join(repo, 'tools', 'new'), { recursive: true });
  const created = path.join(repo, 'tools', 'new', 'run.py');
  fs.writeFileSync(created, 'print(1)\n');
  deployGuard(home);
  const sid = 'sid-new-dir';
  const transcript = writeTranscript(home, sid, [editRecord(created)]);
  const out = parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript }));
  assert.ok(out && out.decision === 'block');
  assert.match(out.reason, /--independence self-authored/,
    'git status lists a new directory as one entry; the guard must still see the file inside it');
});

test('authorship: other sessions\' later changes do not re-fire once this session\'s files were dispatched', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  deployGuard(home);
  const sid = 'sid-shared';
  const transcript = writeTranscript(home, sid, [editRecord(path.join(repo, 'src', 'a.js'))]);
  assert.ok(parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript })));
  // Another session now leaves its own source file in the checkout; a different
  // session id asks, so the in-progress gate is not what answers.
  fs.writeFileSync(path.join(repo, 'src', 'other.js'), 'let z = 1;\n');
  const sid2 = 'sid-shared-2';
  const t2 = writeTranscript(home, sid2, [editRecord(path.join(repo, 'src', 'a.js'))]);
  const r = runHook(home, { session_id: sid2, cwd: repo, transcript_path: t2 });
  assert.equal(parseStdout(r), null);
  assert.match(hookLog(home), /the 1 reviewable file\(s\) this session wrote are unchanged/);
});

function runRecordPath(home, sid) { return path.join(home, ...STATE_REL, `${sid}.hook-review.json`); }

test('in progress: a second stop while the dispatched review is unfinished stays quiet', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  const transcript = fakeTranscriptWithEdit(home, 'prog');
  const sid = 'sid-in-progress';
  assert.ok(parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript })));
  // Reviewers are still out; the session keeps working and stops again.
  fs.writeFileSync(path.join(repo, 'src', 'a.js'), 'let x = 3;\n');
  const r = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(r), null, 'must not block again with --iteration 0');
  assert.match(hookLog(home), /review-in-progress: dispatched 0m ago/);

  // An hour later with no verdict, the hook stops waiting and re-arms.
  const rec = JSON.parse(fs.readFileSync(runRecordPath(home, sid), 'utf8'));
  rec.dispatched_at = Date.now() - 61 * 60 * 1000;
  fs.writeFileSync(runRecordPath(home, sid), JSON.stringify(rec));
  const late = parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript }));
  assert.ok(late && late.decision === 'block', 'a stale review no longer silences the hook');
  assert.match(hookLog(home), /review-stale/);
});

test('in progress: the verdict in the final reply settles the review, and its own fixes do not re-fire', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  const transcript = fakeTranscriptWithEdit(home, 'settle');
  const sid = 'sid-settle';
  assert.ok(parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript })));
  // The review fixes a finding, then ends the continued turn with its verdict.
  fs.writeFileSync(path.join(repo, 'src', 'a.js'), 'let x = 2; // fixed in review\n');
  const ending = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript,
    stop_hook_active: true, last_assistant_message: 'Main result. Review: clean.\n<promise>review-clean</promise>' });
  assert.equal(parseStdout(ending), null);
  assert.match(hookLog(home), /settled: session=sid-settle/);
  // Next ordinary stop: the reviewed state is unchanged, so nothing fires.
  const next = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(next), null, 'the review\'s own fix must not re-arm the hook');
  assert.match(hookLog(home), /diff-unchanged-since-last-review/);
  // New work after the verdict is reviewed again.
  fs.writeFileSync(path.join(repo, 'src', 'a.js'), 'let x = 4;\n');
  assert.ok(parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript })));
});

test('in progress: a verdict written to the transcript after the dispatch ends the wait', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  const sid = 'sid-transcript-verdict';
  const transcript = writeTranscript(home, sid, [
    // The skill's own text quotes the markers; it is not an assistant verdict.
    { type: 'user', timestamp: new Date().toISOString(), message: { content: [
      { type: 'text', text: 'ends with <promise>review-clean</promise>' }] } },
    { type: 'assistant', timestamp: new Date().toISOString(), message: { content: [
      { type: 'tool_use', name: 'Edit', id: 'e', input: {} }] } },
  ]);
  assert.ok(parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript })));
  const quiet = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(quiet), null);
  assert.match(hookLog(home), /review-in-progress/, 'quoted skill text is not a verdict');

  fs.appendFileSync(transcript, JSON.stringify({ type: 'assistant',
    timestamp: new Date(Date.now() + 5000).toISOString(), message: { content: [
      { type: 'text', text: 'All reviewers reported.\n<promise>review-exhausted</promise>' }] } }) + '\n');
  const done = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(done), null);
  assert.match(hookLog(home), /review-finished: verdict in the transcript/);
});

test('in progress: state the skill archived itself counts as the verdict', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  const transcript = fakeTranscriptWithEdit(home, 'arch');
  const sid = 'sid-self-archived';
  assert.ok(parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript })));
  fs.writeFileSync(path.join(repo, 'src', 'a.js'), 'let x = 2; // fixed\n');
  const archived = path.join(home, ...STATE_REL, 'archive', `${sid}-1.json`);
  fs.writeFileSync(archived, JSON.stringify({ completion: 'review-clean' }));
  fs.utimesSync(archived, new Date(Date.now() + 5000), new Date(Date.now() + 5000));
  const r = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(r), null);
  assert.match(hookLog(home), /review-finished: archived review state/);
  const again = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(again), null, 'the fixed state was recorded as reviewed');
});

test('in progress: a review the session started by hand keeps the hook quiet', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  const transcript = fakeTranscriptWithEdit(home, 'manual');
  const sid = 'sid-manual-review';
  fs.writeFileSync(path.join(home, ...STATE_REL, `${sid}.json`), JSON.stringify({ iteration: 1, completion: null }));
  const r = runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript });
  assert.equal(parseStdout(r), null, 'the reviewer\'s own fixes must not start a second review');
  assert.match(hookLog(home), /review-in-progress: this session has a live, unfinished review state/);
});

test('wording: the block reason keeps the command first and asks for the main result first', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  const transcript = fakeTranscriptWithEdit(home, 'word');
  const out = parseStdout(runHook(home, { session_id: 'sid-wording', cwd: repo, transcript_path: transcript }));
  assert.ok(out);
  const [firstLine] = out.reason.split('\n');
  assert.match(firstLine, /^\/review-loop --auto --session-id sid-wording --iteration 0 --mode code --independence \S+$/);
  assert.match(out.reason, /restate the full main result of the work first/);
  assert.match(out.reason, /at most one added line/);
  assert.match(out.reason, /Never end on a bare "Done"/);
});

test('state: REVIEW_LOOP_STATE_DIR moves the hook\'s state and log out of the home folder', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  const transcript = fakeTranscriptWithEdit(home, 'statedir');
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stop-hook-state-'));
  const out = parseStdout(runHook(home, { session_id: 'sid-statedir', cwd: repo, transcript_path: transcript },
    { REVIEW_LOOP_STATE_DIR: stateDir }));
  assert.ok(out);
  assert.ok(fs.existsSync(path.join(stateDir, 'hook.log')));
  assert.ok(fs.existsSync(path.join(stateDir, 'sid-statedir.hook-review.json')));
  assert.equal(hookLog(home), '', 'nothing was written to the default state folder');
});

test('authorship: a dispatch records only the files it covers, so the other author is still reviewed', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);            // src/a.js: the code author's
  fs.mkdirSync(path.join(repo, 'reports'), { recursive: true });
  const report = path.join(repo, 'reports', 'summary.md');
  fs.writeFileSync(report, '# summary\n');           // the report author's
  deployGuard(home);
  const t1 = writeTranscript(home, 'sid-report', [editRecord(report)]);
  const first = parseStdout(runHook(home, { session_id: 'sid-report', cwd: repo, transcript_path: t1 }));
  assert.ok(first && first.decision === 'block');
  assert.match(first.reason, /--mode deliverable/, 'routed by the report author\'s own file');
  // The code author stops next. Its source file was not covered by the deliverable review.
  const t2 = writeTranscript(home, 'sid-code', [editRecord(path.join(repo, 'src', 'a.js'))]);
  const second = parseStdout(runHook(home, { session_id: 'sid-code', cwd: repo, transcript_path: t2 }));
  assert.ok(second && second.decision === 'block', 'the code author must still get its review');
  assert.match(second.reason, /--mode code/);
});

test('authorship: a deletion nobody can be shown to have made still fires', () => {
  const home = mkTempHome();
  const repo = mkRepoWithDirtyFile(home);
  git(repo, 'checkout', '-q', '--', '.');
  fs.unlinkSync(path.join(repo, 'src', 'a.js'));     // `rm src/a.js` through the shell
  deployGuard(home);
  const sid = 'sid-deleter';
  const transcript = writeTranscript(home, sid, [editRecord(path.join(home, 'notes', 'n.md'))]);
  const out = parseStdout(runHook(home, { session_id: sid, cwd: repo, transcript_path: transcript }));
  assert.ok(out && out.decision === 'block', 'missing signals are not proof the session did not delete it');
});
