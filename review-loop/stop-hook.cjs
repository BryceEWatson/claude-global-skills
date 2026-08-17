#!/usr/bin/env node
/**
 * Stop-event hook for the auto-review-loop skill.
 *
 * Fires on every Claude Code session exit. Decides whether the
 * /review-loop skill should auto-trigger and, if so, returns a
 * `decision: "block"` JSON to the harness with a re-invocation prompt.
 *
 * Cross-platform: uses os.homedir() / path.join() exclusively, no shell
 * variable expansion at runtime. Invoked as `node <this-script>` by the
 * Stop hook in ~/.claude/settings.json.
 *
 * Stdin: JSON from Claude Code with fields:
 *   { session_id, transcript_path, cwd, permission_mode, hook_event_name,
 *     stop_hook_active?, agent_id?, agent_type? }
 *
 * Stdout: either nothing (exit 0, no block) or a JSON object:
 *   { decision: "block", reason: "<re-invoke prompt>", systemMessage: "..." }
 *
 * Exit codes:
 *   0 — allow stop (with or without `decision` JSON output)
 *   non-0 — error; harness will log but allow stop
 *
 * Gating: after the SAFETY exits (re-entry, plan mode, trivial/empty diff,
 * skip-next, per-project opt-out, terminal state, concurrent-repo) the hook
 * applies cheap, no-LLM VALUE exits and logs the decision to hook.log either
 * way — it is an always-on gate, not an always-on review:
 *   - nothing-reviewable: only docs/handoffs/lockfiles/scratch/generated changed
 *   - diff-unchanged-since-last-review: this exact diff was already dispatched
 * Per-project overrides under <repo>/.claude/:
 *   review-loop.disabled          — opt out entirely
 *   review-loop.plan-paths        — globs that count as plan artifacts
 *   review-loop.code-exts         — extensions that count as reviewable code
 *   review-loop.deliverable-paths — globs that count as reader-facing deliverables
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const STATE_ROOT = path.join(os.homedir(), '.claude', 'skills', 'review-loop', '.local-state');
const ARCHIVE_DIR = path.join(STATE_ROOT, 'archive');
const SKIP_NEXT_MARKER = path.join(os.homedir(), '.claude', 'skills', 'review-loop', '.skip-next');
const HOOK_LOG = path.join(STATE_ROOT, 'hook.log');

const STALE_LOCK_MS = 30 * 60 * 1000; // 30 minutes

function ensureDirs() {
  for (const d of [STATE_ROOT, ARCHIVE_DIR]) {
    try { fs.mkdirSync(d, { recursive: true }); } catch (_) { /* ignore */ }
  }
}

function logLine(msg) {
  ensureDirs();
  const ts = new Date().toISOString();
  try {
    fs.appendFileSync(HOOK_LOG, `${ts} ${msg}\n`);
  } catch (_) { /* swallow log errors */ }
}

function readStdinSync() {
  // Hook receives JSON on stdin. Node has no built-in sync stdin reader
  // in all environments — read from fd 0 in a loop.
  try {
    const chunks = [];
    const buf = Buffer.alloc(65536);
    for (;;) {
      let n;
      try {
        n = fs.readSync(0, buf, 0, buf.length, null);
      } catch (e) {
        if (e.code === 'EAGAIN') continue;
        break;
      }
      if (!n) break;
      chunks.push(Buffer.from(buf.subarray(0, n)));
    }
    return Buffer.concat(chunks).toString('utf8');
  } catch (_) {
    return '';
  }
}

function safeJsonParse(s) {
  if (!s) return {};
  try { return JSON.parse(s); } catch (_) { return {}; }
}

function isPidAlive(pid) {
  if (!pid || pid <= 0) return false;
  try { process.kill(pid, 0); return true; }
  catch (e) { return e.code === 'EPERM'; }
}

function sha1Short(s) {
  return crypto.createHash('sha1').update(s).digest('hex').slice(0, 16);
}

function gitTopLevel(cwd) {
  try {
    return execFileSync('git', ['-C', cwd, 'rev-parse', '--show-toplevel'], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch (_) { return null; }
}

function gitDiffIsEmpty(cwd) {
  try {
    execFileSync('git', ['-C', cwd, 'diff', '--quiet', 'HEAD'], { stdio: 'ignore' });
    return true;
  } catch (_) { return false; }
}

function gitDiffChangedFiles(cwd) {
  // Returns array of changed-file paths, or null if git failed (no repo,
  // git missing, etc.). Caller must distinguish null (failure - conservative,
  // do NOT treat as empty diff) from [] (genuinely empty diff - relaxed).
  try {
    const out = execFileSync('git', ['-C', cwd, 'diff', 'HEAD', '--name-only'], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    });
    return out.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  } catch (_) { return null; }
}

function gitBranch(cwd) {
  try {
    return execFileSync('git', ['-C', cwd, 'rev-parse', '--abbrev-ref', 'HEAD'], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch (_) { return null; }
}

function gitUntrackedFiles(cwd) {
  // Untracked, non-ignored files — /review-loop reviews these too (via
  // `git status --porcelain`), so the hook must count them when deciding
  // whether there is anything reviewable. Returns [] on failure.
  try {
    const out = execFileSync('git', ['-C', cwd, 'ls-files', '--others', '--exclude-standard'], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    });
    return out.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  } catch (_) { return []; }
}

function gitReviewStateSha(cwd) {
  // sha256 of `git diff HEAD` PLUS `git status --porcelain` — identifies the
  // exact reviewable state (tracked content changes AND added/removed untracked
  // files) so an unchanged state is not re-dispatched across sessions. If the
  // full diff is too large to buffer, fall back to a coarser numstat identity so
  // the anti-spam gate keeps working (degrade, don't silently disable). Returns
  // null only if git itself is unavailable.
  const porcelain = () => execFileSync('git', ['-C', cwd, 'status', '--porcelain'], {
    encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'],
  });
  try {
    const diff = execFileSync('git', ['-C', cwd, 'diff', 'HEAD'], {
      encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'],
    });
    return crypto.createHash('sha256').update(diff + '\n--\n' + porcelain()).digest('hex');
  } catch (_) {
    // Full diff unavailable (e.g. > maxBuffer). numstat is one line per file, so
    // it stays small even for huge diffs — coarser (line counts, not content)
    // but stable per state.
    try {
      const stat = execFileSync('git', ['-C', cwd, 'diff', 'HEAD', '--numstat'], {
        encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'],
      });
      return crypto.createHash('sha256').update('numstat\n' + stat + '\n--\n' + porcelain()).digest('hex');
    } catch (_) { return null; }
  }
}

function lastFiredPath(repoSha) {
  // Per-repo pointer to the review-state sha the hook last dispatched for.
  return path.join(STATE_ROOT, `lastfired-${repoSha}.json`);
}

const DEFAULT_PLAN_GLOBS = [
  '**/*-plan.md',
  '**/*-proposal.md',
  '**/*-spec.md',
  '**/*-retrospective.md',
  '**/*-research/**/*.md',
];

function globToRegex(glob) {
  let r = '';
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === '*') {
      if (glob[i + 1] === '*') { r += '.*'; i++; }
      else { r += '[^/]*'; }
    } else if (c === '?') {
      r += '[^/]';
    } else if ('.+^$()|[]{}\\'.includes(c)) {
      r += '\\' + c;
    } else {
      r += c;
    }
  }
  return new RegExp('^' + r + '$', 'i');
}

// Reader-facing deliverables: FINISHED work a human is handed to read or use
// (a report, a published content page, a brief). These changed nothing before —
// they fell into `skip` below and the session got no review at all — so the
// deliverable branch only ADDS review where there was none. Kept deliberately
// narrow: `docs/**` is the plan lens's genre, README/CHANGELOG are project
// furniture, `.html` is usually generated or a template, and anything under
// `.claude/**` is session machinery. Widen per-project with
// .claude/review-loop.deliverable-paths (one glob per line; REPLACES this set).
// NOTE: globToRegex above maps `**` to `.*` without absorbing the slash that
// follows it, so a `**/` prefix REQUIRES at least one directory: `reports/**/*.md`
// matches `reports/a/b.md` but not `reports/b.md`, and `**/*-report.md` matches
// `docs/x-report.md` but not a repo-root `x-report.md`. Every entry below that
// could name a top-level file therefore lists both depths. (Fixing globToRegex
// itself would retire the duplication, but it would also widen the existing plan
// globs — `**/*-plan.md` has the same root-level hole — so that belongs in its
// own change, not this one.)
const DEFAULT_DELIVERABLE_GLOBS = [
  'reports/*.md',
  'reports/*.mdx',
  'reports/**/*.md',
  'reports/**/*.mdx',
  'research/*.md',
  'research/*.mdx',
  'research/**/*.md',
  'research/**/*.mdx',
  'content/*.md',
  'content/*.mdx',
  'content/**/*.md',
  'content/**/*.mdx',
  'src/content/*.md',
  'src/content/*.mdx',
  'src/content/**/*.md',
  'src/content/**/*.mdx',
  '*-report.md',
  '**/*-report.md',
  '*-brief.md',
  '**/*-brief.md',
  '*-summary.md',
  '**/*-summary.md',
];

// Session machinery is never a reader-facing deliverable. Enforced structurally
// rather than by careful globbing, because the suffix globs above would
// otherwise swallow a `session-end` handoff named `..._thing-summary.md` and
// fire a review loop on every close-out. (The plan globs carry the same trap and
// handle it by asking handoff slugs to avoid `-plan`/`-spec`/`-retrospective`;
// this branch does not rely on that.) Case-insensitive to match globToRegex,
// which compiles every glob with the `i` flag — otherwise a `.Claude/` directory
// on a case-insensitive filesystem slips past the exclusion and matches a glob.
const DELIVERABLE_EXCLUDE = /(^|\/)\.claude\//i;

// An override that exists but cannot be read is LOGGED rather than swallowed.
// The old bare `catch (_)` silently fell back to the full default set, which
// turns a deliberate opt-out (an empty override file) into a full opt-in the
// moment that file becomes unreadable — a directory of the same name, a locked
// file, a permissions change — and hook.log showed an ordinary dispatch with no
// trace of why. `failClosedOnError` is for a set whose failure mode is FIRING a
// review rather than skipping one: there an unreadable override yields an empty
// set, so a broken opt-out cannot become a surprise opt-in.
function loadGlobsWithOverride(cwd, overrideName, defaults, failClosedOnError = false) {
  const overridePath = path.join(cwd, '.claude', overrideName);
  if (fs.existsSync(overridePath)) {
    try {
      const lines = fs.readFileSync(overridePath, 'utf8')
        .split(/\r?\n/).map(s => s.trim()).filter(s => s && !s.startsWith('#'));
      return lines.map(globToRegex);
    } catch (e) {
      const code = (e && e.code) ? e.code : 'unknown';
      const fallback = failClosedOnError ? 'using empty set' : 'using defaults';
      logLine(`override-unreadable: ${overridePath} (${code}) — ${fallback}`);
      if (failClosedOnError) return [];
    }
  }
  return defaults.map(globToRegex);
}

function loadPlanGlobs(cwd) {
  return loadGlobsWithOverride(cwd, 'review-loop.plan-paths', DEFAULT_PLAN_GLOBS);
}

function loadDeliverableGlobs(cwd) {
  return loadGlobsWithOverride(
    cwd, 'review-loop.deliverable-paths', DEFAULT_DELIVERABLE_GLOBS, true,
  );
}

// A session that changes ONLY non-code, non-plan files (handoffs, notes, logs,
// lockfiles, generated output, scratch) does not warrant a multi-agent review
// loop. A file counts as reviewable code when its extension is in this set (or
// its basename is in CODE_BASENAMES). Plan artifacts are matched separately by
// the plan globs above. Override per-project with .claude/review-loop.code-exts
// (one extension per line, e.g. ".ts"; the file REPLACES this default set).
const DEFAULT_CODE_EXTENSIONS = new Set([
  '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs',
  '.py', '.go', '.rs', '.java', '.kt', '.rb', '.php',
  '.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.cs', '.m', '.mm',
  '.swift', '.scala', '.sh', '.bash', '.zsh', '.ps1',
  '.sql', '.css', '.scss', '.sass', '.less',
  '.vue', '.svelte', '.astro', '.sol',
]);
const CODE_BASENAMES = new Set(['Dockerfile', 'Makefile']);

function loadCodeExtensions(cwd) {
  const overridePath = path.join(cwd, '.claude', 'review-loop.code-exts');
  if (fs.existsSync(overridePath)) {
    try {
      const exts = fs.readFileSync(overridePath, 'utf8')
        .split(/\r?\n/).map(s => s.trim()).filter(s => s && !s.startsWith('#'))
        .map(s => (s.startsWith('.') ? s : '.' + s).toLowerCase());
      return new Set(exts);
    } catch (_) { /* fall through to defaults */ }
  }
  return DEFAULT_CODE_EXTENSIONS;
}

function classifyDiff(files, planRegexes, codeExts, deliverableRegexes) {
  // 4-way split of the changed files:
  //   - plan        : matches a plan-artifact glob   -> review in plan mode
  //   - code        : reviewable source by extension -> review in code mode
  //   - deliverable : matches a deliverable glob     -> review in deliverable mode
  //   - skip        : everything else (docs, handoffs, lockfiles, scratch, generated)
  //
  // Precedence: plan > code > deliverable.
  //   plan first, because the plan-tuned lenses include 'architecture', so a
  //   mixed plan+code diff routes to plan (pre-existing behavior);
  //   code before deliverable, because the deliverable lens set does NOT review
  //   source — letting a stray content file demote a code diff would silently
  //   drop the code review. The cost of that ordering is the honest gap: a diff
  //   that mixes code and a deliverable gets code lenses only, and the
  //   deliverable rides along unreviewed (run /review-loop --mode deliverable
  //   manually for those). Deliverable-only diffs previously matched nothing and
  //   exited at Gate A, so this branch can only add review, never replace it.
  const planFiles = [], codeFiles = [], deliverableFiles = [], skipFiles = [];
  for (const f of (files || [])) {
    // Normalize Windows paths to forward slashes for glob matching.
    const norm = f.replace(/\\/g, '/');
    if (planRegexes.some(re => re.test(norm))) { planFiles.push(f); continue; }
    const base = norm.slice(norm.lastIndexOf('/') + 1);
    const dot = base.lastIndexOf('.');
    const ext = dot > 0 ? base.slice(dot).toLowerCase() : '';
    if ((ext && codeExts.has(ext)) || CODE_BASENAMES.has(base)) codeFiles.push(f);
    else if (deliverableRegexes.some(re => re.test(norm))) {
      // The `.claude/` exclusion is unconditional — it outranks the project
      // override too, so a glob cannot re-include session machinery. Log when it
      // suppresses a file that a glob DID match, so the no-op is visible rather
      // than mysterious to whoever wrote that glob.
      if (DELIVERABLE_EXCLUDE.test(norm)) {
        logLine(`deliverable-excluded: ${norm} (.claude/ is never a deliverable)`);
        skipFiles.push(f);
      } else deliverableFiles.push(f);
    } else skipFiles.push(f);
  }
  let mode = null;
  if (planFiles.length) mode = 'plan';
  else if (codeFiles.length) mode = 'code';
  else if (deliverableFiles.length) mode = 'deliverable';
  return { mode, planFiles, codeFiles, deliverableFiles, skipFiles };
}

function readTranscriptTail(transcriptPath, maxBytes = 200000) {
  if (!transcriptPath) return '';
  try {
    const stat = fs.statSync(transcriptPath);
    const start = Math.max(0, stat.size - maxBytes);
    const fd = fs.openSync(transcriptPath, 'r');
    const buf = Buffer.alloc(stat.size - start);
    fs.readSync(fd, buf, 0, buf.length, start);
    fs.closeSync(fd);
    return buf.toString('utf8');
  } catch (_) { return ''; }
}

function transcriptHasEditWrite(tail) {
  if (!tail) return false;
  // Heuristic — look for tool_use blocks where the name is Edit/Write/MultiEdit.
  // The JSONL records nest the tool name as "name":"Edit" within a content block.
  return /"name"\s*:\s*"(Edit|Write|MultiEdit|NotebookEdit)"/.test(tail);
}

function transcriptHasPlanModeMarker(tail) {
  if (!tail) return false;
  return /Plan mode is active/.test(tail);
}

function archiveAndExit(sessionStatePath) {
  try {
    if (sessionStatePath && fs.existsSync(sessionStatePath)) {
      const base = path.basename(sessionStatePath);
      const ts = Date.now();
      const dest = path.join(ARCHIVE_DIR, `${base.replace(/\.json$/, '')}-${ts}.json`);
      fs.renameSync(sessionStatePath, dest);
    }
  } catch (_) { /* ignore */ }
}

function sweepStaleLocks() {
  try {
    const entries = fs.readdirSync(STATE_ROOT, { withFileTypes: true });
    const now = Date.now();
    for (const e of entries) {
      if (!e.isFile() || !e.name.endsWith('.lock')) continue;
      const lockPath = path.join(STATE_ROOT, e.name);
      try {
        const stat = fs.statSync(lockPath);
        if (now - stat.mtimeMs < STALE_LOCK_MS) continue;
        const contents = fs.readFileSync(lockPath, 'utf8').trim();
        const [pidStr] = contents.split(/\s+/);
        const pid = Number(pidStr);
        if (!isPidAlive(pid)) {
          fs.unlinkSync(lockPath);
          logLine(`sweep: removed stale lock ${e.name} (pid=${pid})`);
        }
      } catch (_) { /* skip this entry */ }
    }
  } catch (_) { /* ignore */ }
}

function emitAllowStop(reason) {
  if (reason) logLine(`allow: ${reason}`);
  process.exit(0);
}

function emitBlock({ skillCommand, systemMessage }) {
  const out = {
    decision: 'block',
    reason: skillCommand,
    systemMessage: systemMessage || 'Review-loop iteration triggered',
  };
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}

function main() {
  ensureDirs();

  const stdinRaw = readStdinSync();
  const payload = safeJsonParse(stdinRaw);
  const sessionId = payload.session_id || `unknown-${Date.now()}`;
  const cwd = payload.cwd || process.cwd();
  const permissionMode = payload.permission_mode || null;
  const stopHookActive = payload.stop_hook_active === true;
  const transcriptPath = payload.transcript_path || null;

  logLine(`fire: session=${sessionId} cwd=${cwd} mode=${permissionMode} stopActive=${stopHookActive}`);

  // Escape hatch 1: re-entry guard via stop_hook_active.
  // This is the de-facto loop guard exposed by Claude Code (undocumented in
  // some recent releases but still present per anthropics/claude-code#55754).
  // When the harness re-invokes Claude as a result of a prior block, this
  // flag is true on the subsequent Stop — we must not block again.
  if (stopHookActive) emitAllowStop('stop_hook_active=true (re-entry guard)');

  // Escape hatch 1b: env-var guard set by the skill before each iteration.
  if (process.env.CLAUDE_REVIEW_LOOP_ACTIVE === '1') {
    emitAllowStop('CLAUDE_REVIEW_LOOP_ACTIVE=1 (re-entry env guard)');
  }

  // Escape hatch 2: plan mode. Prefer documented permission_mode field;
  // fall back to transcript marker for older Claude Code versions.
  if (permissionMode === 'plan') emitAllowStop('plan mode (permission_mode=plan)');

  const tail = readTranscriptTail(transcriptPath);
  if (transcriptHasPlanModeMarker(tail) && !transcriptHasEditWrite(tail)) {
    emitAllowStop('plan-mode marker in transcript, no Edit/Write');
  }

  // Escape hatch 3: trivial work. No edits + no diff = nothing to review.
  // gitDiffChangedFiles returns null when git fails (no repo, git missing) —
  // preserve original gitDiffIsEmpty semantics (failure means "we don't know
  // if it's empty, so be conservative and don't bail on trivial-work alone").
  const noTranscriptEdits = !transcriptHasEditWrite(tail);
  const changedFiles = gitDiffChangedFiles(cwd);
  const diffEmpty = changedFiles !== null && changedFiles.length === 0;
  if (noTranscriptEdits && diffEmpty) {
    emitAllowStop('trivial work: no Edit/Write in transcript and git diff is empty');
  }

  // Escape hatch 4: user override (one-shot).
  if (fs.existsSync(SKIP_NEXT_MARKER)) {
    try { fs.unlinkSync(SKIP_NEXT_MARKER); } catch (_) {}
    emitAllowStop('skip-next marker consumed');
  }

  // Escape hatch 5: per-project opt-out.
  const projectDisable = path.join(cwd, '.claude', 'review-loop.disabled');
  if (fs.existsSync(projectDisable)) {
    emitAllowStop(`per-project opt-out: ${projectDisable}`);
  }

  // Escape hatch 6: state terminal.
  const sessionStatePath = path.join(STATE_ROOT, `${sessionId}.json`);
  if (fs.existsSync(sessionStatePath)) {
    try {
      const st = JSON.parse(fs.readFileSync(sessionStatePath, 'utf8'));
      if (st.completion === 'review-clean' || st.completion === 'review-exhausted' || st.completion === 'stalled') {
        archiveAndExit(sessionStatePath);
        emitAllowStop(`state terminal: ${st.completion}`);
      }
    } catch (_) { /* corrupt state — proceed as fresh */ }
  }

  // Escape hatch 7: stale-lockfile sweep (PID-liveness based, not mtime only).
  sweepStaleLocks();

  // Concurrent-repo guard.
  const top = gitTopLevel(cwd);
  if (top) {
    const repoLockPath = path.join(STATE_ROOT, `repo-${sha1Short(top)}.lock`);
    if (fs.existsSync(repoLockPath)) {
      try {
        const [pidStr] = fs.readFileSync(repoLockPath, 'utf8').trim().split(/\s+/);
        if (isPidAlive(Number(pidStr))) {
          emitAllowStop(`concurrent-loop-in-repo (lock=${repoLockPath}, pid=${pidStr})`);
        }
        // Else: stale; sweepStaleLocks above should have caught it but didn't (mtime fresh, pid dead).
        fs.unlinkSync(repoLockPath);
      } catch (_) { /* proceed */ }
    }
  }

  // ---- Value gates: does this change actually warrant a review? ----
  // Everything above is a SAFETY exit. The checks below are VALUE exits — a
  // cheap, no-LLM look that records in hook.log exactly why the loop did or did
  // not escalate. The hook is an always-on gate, not an always-on review.

  // Git unavailable: we cannot compute a diff, and /review-loop needs one.
  if (changedFiles === null) {
    emitAllowStop('git-unavailable: cannot compute diff (no repo or git missing)');
  }

  // Reviewable set = tracked changes + untracked non-ignored files (the same
  // set /review-loop inspects). Untracked-only new code must still trigger.
  const untracked = gitUntrackedFiles(cwd);
  const files = changedFiles.concat(untracked.filter(f => !changedFiles.includes(f)));
  const planRegexes = loadPlanGlobs(cwd);
  const codeExts = loadCodeExtensions(cwd);
  const deliverableRegexes = loadDeliverableGlobs(cwd);
  const { mode, planFiles, codeFiles, deliverableFiles, skipFiles } =
    classifyDiff(files, planRegexes, codeExts, deliverableRegexes);

  // Gate A — nothing reviewable changed (only docs / handoffs / lockfiles /
  // scratch / generated). The most common low-value session; skip it.
  if (mode === null) {
    const why = files.length === 0
      ? 'empty diff'
      : `${skipFiles.length} non-reviewable file(s) [${skipFiles.slice(0, 5).join(', ')}${skipFiles.length > 5 ? ', …' : ''}]`;
    emitAllowStop(`nothing-reviewable: ${why}`);
  }

  // Gate B — we already dispatched a review for this exact diff. Changing any
  // reviewed line changes the sha and re-arms the loop; an identical diff is
  // not re-reviewed across sessions (run /review-loop manually to force).
  const repoSha = top ? sha1Short(top) : null;
  const diffSha = gitReviewStateSha(cwd);
  const firedPath = repoSha ? lastFiredPath(repoSha) : null;
  if (diffSha && firedPath && fs.existsSync(firedPath)) {
    try {
      const prev = JSON.parse(fs.readFileSync(firedPath, 'utf8'));
      if (prev && prev.diff_sha === diffSha) {
        emitAllowStop(`diff-unchanged-since-last-review (sha=${diffSha.slice(0, 8)})`);
      }
    } catch (_) { /* corrupt pointer — fall through and re-fire */ }
  }

  // Warranted — record that we DISPATCHED a review for this state, then
  // re-invoke the /review-loop skill, routed to the right reviewer lens set.
  // Note: this records dispatch, not completion — if the review is interrupted,
  // the same state won't auto-re-fire (run /review-loop manually to force).
  if (firedPath && diffSha) {
    try {
      fs.writeFileSync(firedPath, JSON.stringify({
        diff_sha: diffSha, branch: gitBranch(cwd), at: new Date().toISOString(),
      }));
    } catch (_) { /* best-effort */ }
  }
  const iteration = 0; // first invocation; skill will increment internally.
  const reviewable = planFiles.length + codeFiles.length + deliverableFiles.length;
  const skillCommand = `/review-loop --auto --session-id ${sessionId} --iteration ${iteration} --mode ${mode}`;
  logLine(`block: invoke skill iter=${iteration} mode=${mode} files=${files.length} reviewable=${reviewable} skipped=${skipFiles.length} sha=${diffSha ? diffSha.slice(0, 8) : 'n/a'}`);
  emitBlock({
    skillCommand,
    systemMessage: `Review-loop iter ${iteration + 1} starting in ${mode} mode (skip next: touch ${SKIP_NEXT_MARKER})`,
  });
}

try {
  main();
} catch (err) {
  logLine(`ERROR: ${err && err.stack ? err.stack : String(err)}`);
  // Never block stop on an internal hook error.
  process.exit(0);
}
