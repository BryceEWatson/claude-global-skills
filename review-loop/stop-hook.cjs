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
 *     stop_hook_active?, last_assistant_message?, agent_id?, agent_type? }
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
 *   - review-in-progress: a review this session started has not reached a verdict
 *   - diff-unchanged-since-last-review: every reviewable file was already dispatched
 *     in its current content (a change to a skipped file does not re-arm the hook)
 *   - not-authored: this session wrote none of the reviewable changed files
 * State lives under ~/.claude/skills/review-loop/.local-state, or under
 * REVIEW_LOOP_STATE_DIR when that is set (the authorship guard reads the same
 * variable), so a replay or a test can keep its state out of the live folder.
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

const STATE_ROOT = process.env.REVIEW_LOOP_STATE_DIR
  || path.join(os.homedir(), '.claude', 'skills', 'review-loop', '.local-state');
const ARCHIVE_DIR = path.join(STATE_ROOT, 'archive');
const SKIP_NEXT_MARKER = path.join(os.homedir(), '.claude', 'skills', 'review-loop', '.skip-next');
const HOOK_LOG = path.join(STATE_ROOT, 'hook.log');

const STALE_LOCK_MS = 30 * 60 * 1000; // 30 minutes
// How long a dispatched review may run without a verdict before the hook stops
// waiting for it. The longest hook-dispatched review measured over 19-23 Nov 2026
// took 61 minutes from the block to its verdict, so the window is 90.
const REVIEW_STALE_MS = 90 * 60 * 1000;
// Per-session run records older than this are deleted by the stale sweep.
const RUN_RECORD_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
// A reviewable file larger than this is identified by size and modification time
// instead of content, so the hook never buffers a huge file just to compare it.
const MAX_HASH_BYTES = 16 * 1024 * 1024;
// The clock. REVIEW_LOOP_NOW_MS lets a replay of logged stops run each one at the
// time it happened; nothing sets it in normal use.
const nowMs = () => Number(process.env.REVIEW_LOOP_NOW_MS) || Date.now();
const TERMINAL_COMPLETIONS = new Set(['review-clean', 'review-exhausted', 'stalled']);
const PROMISE_RE = /<promise>review-(clean|stalled|exhausted)<\/promise>/;

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

function gitAddedFiles(cwd) {
  // Files that exist now but not in HEAD (staged additions and rename targets).
  try {
    const out = execFileSync('git', ['-C', cwd, 'diff', 'HEAD', '--name-only', '--diff-filter=A'], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    });
    return out.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  } catch (_) { return []; }
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
  // whether there is anything reviewable. --full-name keeps the paths relative to
  // the repo root, like `git diff --name-only`, when the session runs from a
  // subdirectory. Returns [] on failure.
  try {
    const out = execFileSync('git', ['-C', cwd, 'ls-files', '--others', '--exclude-standard', '--full-name'], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    });
    return out.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  } catch (_) { return []; }
}

function fileFingerprint(absPath) {
  // Identity of one reviewable file's current content: sha256 of its bytes. A file
  // too large to buffer degrades to size + modification time (coarser, but stable
  // per state, so the anti-spam gate keeps working). A deleted file is 'absent'.
  try {
    const st = fs.statSync(absPath);
    if (!st.isFile()) return 'not-a-file';
    if (st.size > MAX_HASH_BYTES) return `stat:${st.size}:${Math.round(st.mtimeMs)}`;
    return crypto.createHash('sha256').update(fs.readFileSync(absPath)).digest('hex');
  } catch (_) { return 'absent'; }
}

function relKey(f) {
  // One spelling per file for the dispatch record and the authorship comparison.
  const k = String(f).replace(/\\/g, '/');
  return process.platform === 'win32' ? k.toLowerCase() : k;
}

function fingerprintFiles(root, files) {
  // The re-arm key. It is built ONLY from files the hook classifies as plan, code
  // or deliverable, one entry per file, so a change to a skipped file (a note, a
  // ledger, a lockfile) cannot re-arm the hook. Measured 19-23 Nov 2026: the old
  // key hashed the whole tree's diff, and one session was blocked five times for
  // one unchanged script while it edited records beside it.
  const out = {};
  for (const f of files) out[relKey(f)] = fileFingerprint(path.resolve(root, f));
  return out;
}

function readDispatched(firedPath) {
  // { relKey: fingerprint } of the reviewable files already handed to a review.
  if (!firedPath) return {};
  try {
    const prev = JSON.parse(fs.readFileSync(firedPath, 'utf8'));
    return (prev && prev.files && typeof prev.files === 'object') ? prev.files : {};
  } catch (_) { return {}; } // missing, corrupt or pre-per-file format: re-fire once
}

function allDispatched(files, fingerprints, dispatched) {
  return files.every((f) => {
    const k = relKey(f);
    return dispatched[k] !== undefined && dispatched[k] === fingerprints[k];
  });
}

function recordDispatched(firedPath, fingerprints, changedFiles, branch) {
  // Merge, so two sessions sharing a checkout do not erase each other's record,
  // and prune entries for files that are no longer changed, so the record cannot
  // grow and a file that is committed and later changed again is reviewed again.
  if (!firedPath) return;
  try {
    const still = new Set(changedFiles.map(relKey));
    const merged = {};
    for (const [k, v] of Object.entries(readDispatched(firedPath))) if (still.has(k)) merged[k] = v;
    Object.assign(merged, fingerprints);
    fs.writeFileSync(firedPath, JSON.stringify({ files: merged, branch, at: new Date().toISOString() }));
  } catch (_) { /* best-effort */ }
}

function lastFiredPath(repoSha) {
  // Per-repo record of the reviewable files the hook last dispatched a review for.
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
      if (e.isFile() && e.name.endsWith('.hook-review.json')) {
        try {
          const p = path.join(STATE_ROOT, e.name);
          if (now - fs.statSync(p).mtimeMs > RUN_RECORD_MAX_AGE_MS) fs.unlinkSync(p);
        } catch (_) { /* skip this entry */ }
        continue;
      }
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

// ---- "A review this session started is still running" ----------------------
// The hook used to forget that it had dispatched a review: it blocked again, with
// `--iteration 0`, at every later stop while the reviewers were still out. It now
// keeps one small record per session and stays quiet until that review reaches a
// verdict or goes stale.

function runRecordPath(sessionId) {
  return path.join(STATE_ROOT, `${sessionId}.hook-review.json`);
}

function readRunRecord(sessionId) {
  try {
    const r = JSON.parse(fs.readFileSync(runRecordPath(sessionId), 'utf8'));
    return (r && typeof r === 'object') ? r : null;
  } catch (_) { return null; }
}

function writeRunRecord(sessionId, record) {
  try { fs.writeFileSync(runRecordPath(sessionId), JSON.stringify(record)); } catch (_) { /* best-effort */ }
}

function seenUntil(run) {
  // The moment up to which this session's stops have already been checked for a verdict.
  return Math.max(run.dispatched_at || 0, run.last_seen_at || 0);
}

function isPending(run) {
  return !!(run && run.dispatched_at && !(run.settled_at && run.settled_at >= run.dispatched_at));
}

function reviewEndedSince(sessionId, sinceMs, payload, tail, readPromise) {
  // Evidence that this session's review reached a verdict after `sinceMs`.
  // Returns a short description, or null. Every stop of a session passes through
  // here, and the caller moves `sinceMs` forward each time, so a verdict is seen
  // at the stop that ends the turn it arrived in, never credited to a later turn.
  //  1. An archived state file. The skill archives its own state on a terminal
  //     verdict, so the live file is usually already gone when the hook looks.
  try {
    for (const name of fs.readdirSync(ARCHIVE_DIR)) {
      if (!name.startsWith(`${sessionId}-`) || !name.endsWith('.json')) continue;
      if (fs.statSync(path.join(ARCHIVE_DIR, name)).mtimeMs > sinceMs) return 'archived review state';
    }
  } catch (_) { /* no archive is not evidence either way */ }
  if (!readPromise) return null;
  //  2. The verdict marker in the reply that is ending this turn.
  const last = payload && typeof payload.last_assistant_message === 'string' ? payload.last_assistant_message : '';
  if (PROMISE_RE.test(last)) return 'verdict in the final reply';
  //  3. The verdict marker in an assistant message written after the dispatch.
  //     Parsed per record, because the raw transcript also holds the skill's own
  //     text, which quotes the markers.
  for (const line of String(tail || '').split('\n')) {
    if (!line.includes('<promise>review-')) continue;
    let rec;
    try { rec = JSON.parse(line); } catch (_) { continue; }
    if (!rec || rec.type !== 'assistant' || !(Date.parse(rec.timestamp) > sinceMs)) continue;
    const content = rec.message && rec.message.content;
    if (!Array.isArray(content)) continue;
    if (content.some(b => b && b.type === 'text' && PROMISE_RE.test(b.text || ''))) return 'verdict in the transcript';
  }
  return null;
}

function reviewStarted(sessionId, run, tail) {
  // A block is a request, not a review. Measured 19-23 Nov 2026: 23 of 44 blocks
  // were not followed by one (the session declined in a sentence, or went on with
  // other work). Staying quiet is only right once a review is really under way:
  // the skill was invoked, a reviewer agent was dispatched, or the skill's own
  // state or lock file exists. Remembered in the run record once seen, because the
  // evidence can scroll out of the transcript tail.
  if (run.started) return true;
  for (const name of [`${sessionId}.json`, `${sessionId}.lock`]) {
    try { if (fs.statSync(path.join(STATE_ROOT, name)).mtimeMs >= run.dispatched_at) return true; } catch (_) { /* absent */ }
  }
  for (const line of String(tail || '').split('\n')) {
    if (!line.includes('"tool_use"')) continue;
    let rec;
    try { rec = JSON.parse(line); } catch (_) { continue; }
    if (!rec || rec.type !== 'assistant' || !(Date.parse(rec.timestamp) >= run.dispatched_at)) continue;
    const content = rec.message && rec.message.content;
    if (!Array.isArray(content)) continue;
    for (const b of content) {
      if (!b || b.type !== 'tool_use') continue;
      if (b.name === 'Agent' || b.name === 'Task') return true;
      if (b.name === 'Skill' && /review-loop/.test(String((b.input && b.input.skill) || ''))) return true;
    }
  }
  return false;
}

function reviewableNow(cwd) {
  // The changed files and their classification, or null when git cannot say.
  const changed = gitDiffChangedFiles(cwd);
  if (changed === null) return null;
  const untracked = gitUntrackedFiles(cwd);
  const files = changed.concat(untracked.filter(f => !changed.includes(f)));
  const cls = classifyDiff(files, loadPlanGlobs(cwd), loadCodeExtensions(cwd), loadDeliverableGlobs(cwd));
  return { files, ...cls, reviewable: cls.planFiles.concat(cls.codeFiles, cls.deliverableFiles) };
}

function settleReview(sessionId, cwd, run, why) {
  // The review ended, so the state it left is the reviewed state: record it as
  // dispatched. Without this the review's own fixes change the files and re-arm
  // the hook the moment the review finishes.
  try {
    const top = gitTopLevel(cwd);
    const now = reviewableNow(cwd);
    if (top && now) {
      // A hook-dispatched review covered the files listed in its run record, and
      // only those are recorded as reviewed: another session's files beside them
      // still get their own review. A review started by hand has no list and
      // covered the whole diff.
      const covered = run && Array.isArray(run.files) ? new Set(run.files) : null;
      const reviewed = covered ? now.reviewable.filter(f => covered.has(relKey(f))) : now.reviewable;
      recordDispatched(lastFiredPath(sha1Short(top)), fingerprintFiles(top, reviewed), now.files, gitBranch(cwd));
    }
  } catch (_) { /* best-effort: a failed settle only costs one extra review */ }
  writeRunRecord(sessionId, { ...(run || {}), settled_at: nowMs(), last_seen_at: nowMs() });
  logLine(`settled: session=${sessionId} review ended (${why}); the state it left is recorded as reviewed`);
}

function archiveTerminalState(sessionId) {
  const p = path.join(STATE_ROOT, `${sessionId}.json`);
  try {
    const st = JSON.parse(fs.readFileSync(p, 'utf8'));
    if (st && TERMINAL_COMPLETIONS.has(st.completion)) archiveAndExit(p);
  } catch (_) { /* absent or corrupt: nothing to archive */ }
}

function emitAllowStop(reason) {
  if (reason) logLine(`allow: ${reason}`);
  process.exit(0);
}


/**
 * Independence check — does the session we are about to hand the review to also
 * happen to be the session that wrote the diff?
 *
 * For the hook path the answer is very nearly always YES by construction: the Stop
 * event fires in the authoring session, so this is where "don't review your own work"
 * was being broken automatically, every time, with nothing recording that it happened.
 * The hook therefore does NOT suppress the review (its findings still have value, and
 * an unattended run has nobody to dispatch a replacement) — it MARKS the run, and the
 * skill withholds the `review-clean` token so a fresh session is still required to land.
 *
 * The same answer also decides whether the hook fires at all. `authoredFiles`,
 * `weakFiles` and `timingFiles` together are the changed files this session wrote
 * (by an edit tool, a write-shaped shell command naming the file, or a shell command
 * that was running when the file was last modified). If none of the reviewable files
 * is among them, the work belongs to someone else and the hook lets the stop through.
 *
 * `orphanedEdits` lists files the session wrote in the repo that are no longer there
 * (moved or deleted); when it is not empty, new files count as possibly its own.
 *
 * Returns { independence, authoredFiles, weakFiles, timingFiles, orphanedEdits } or
 * null when it could not be determined.
 */
function checkAuthorship(sessionId, cwd) {
  const guard = path.join(os.homedir(), '.claude', 'skills', 'review-loop', 'authorship-guard.mjs');
  if (!fs.existsSync(guard)) return null;
  try {
    const out = execFileSync(process.execPath, [guard, '--session-id', sessionId, '--repo', cwd, '--json'], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 20000,
    });
    const parsed = JSON.parse(out);
    return {
      independence: parsed.independence,
      authoredFiles: parsed.authoredFiles || [],
      weakFiles: parsed.weakFiles || [],
      timingFiles: parsed.timingFiles || [],
      orphanedEdits: parsed.orphanedEdits || [],
    };
  } catch (err) {
    // A guard that cannot run must not block the review; an unproven claim of
    // independence is the only thing it is allowed to refuse.
    try {
      const parsed = JSON.parse((err && err.stdout) || '');
      return {
      independence: parsed.independence,
      authoredFiles: parsed.authoredFiles || [],
      weakFiles: parsed.weakFiles || [],
      timingFiles: parsed.timingFiles || [],
      orphanedEdits: parsed.orphanedEdits || [],
    };
    } catch (_) { return null; }
  }
}

// What the session is told about its closing reply. A hook reply lands under the
// reply the user was about to read, so the last thing on screen was often "the
// review fixed X" with the main result scrolled away above it (Bryce, 2026-11-23:
// "the hook replies push the conversation up so I'm always scrolling back").
const CLOSING_REPLY_RULE = [
  'When this review ends, your final reply replaces the reply you had just written as the last',
  'thing the user reads, and they may not have read that one. So the final reply must stand on',
  'its own: restate the full main result of the work first, as you had reported it, and only',
  'then give the review outcome. If the review had nothing to add, resend your previous reply',
  'with at most one added line about the review. Never end on a bare "Done", or on a reply that',
  'only says what the review fixed.',
  'Once the review is under way, later stops stay quiet until it reaches a verdict, so do not expect another prompt.',
].join(' ');

function emitBlock({ skillCommand, systemMessage }) {
  const out = {
    decision: 'block',
    reason: `${skillCommand}\n\n${CLOSING_REPLY_RULE}`,
    systemMessage: systemMessage || 'Review-loop iteration triggered',
  };
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}

function main() {
  ensureDirs();

  const stdinRaw = readStdinSync();
  const payload = safeJsonParse(stdinRaw);
  const sessionId = payload.session_id || `unknown-${nowMs()}`;
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
  if (stopHookActive) {
    // The turn our own block continued is ending. If the review it ran has reached
    // its verdict, record the state it left as reviewed now, before any later work
    // in this session can be mistaken for part of it.
    const activeRun = readRunRecord(sessionId);
    if (isPending(activeRun)) {
      const activeTail = readTranscriptTail(transcriptPath);
      const ended = reviewEndedSince(sessionId, seenUntil(activeRun), payload, activeTail, true);
      if (ended) {
        settleReview(sessionId, cwd, activeRun, ended);
        // A terminal state file the skill left live belongs to this verdict. Archive
        // it now, or the next ordinary stop would settle a second time and record
        // work done after the review as reviewed.
        archiveTerminalState(sessionId);
      } else if (reviewStarted(sessionId, activeRun, activeTail)) {
        writeRunRecord(sessionId, { ...activeRun, started: true, last_seen_at: nowMs() });
      } else {
        // The turn our block continued ended without starting a review: declined.
        // Stop waiting. The dispatch record stays, so this same state does not ask
        // again, and new work does.
        writeRunRecord(sessionId, { ...activeRun, dispatched_at: null, last_seen_at: nowMs() });
        logLine(`review-not-started: session=${sessionId} ended the continued turn without starting a review; no longer waiting`);
      }
    }
    emitAllowStop('stop_hook_active=true (re-entry guard)');
  }

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

  // Escape hatch 2b: a review this session started. Terminal: archive its state,
  // record the state it left as reviewed, and allow. Still running: allow, quietly.
  // This sits before the cheaper exits below on purpose: every stop must pass
  // through it, so the verdict is seen at the stop that ends the review's own turn.
  const sessionStatePath = path.join(STATE_ROOT, `${sessionId}.json`);
  let run = readRunRecord(sessionId);
  let liveState = null;
  let liveStateMtime = 0;
  if (fs.existsSync(sessionStatePath)) {
    try {
      liveState = JSON.parse(fs.readFileSync(sessionStatePath, 'utf8'));
      liveStateMtime = fs.statSync(sessionStatePath).mtimeMs;
    } catch (_) { liveState = null; /* corrupt state — proceed as fresh */ }
  }
  if (liveState && TERMINAL_COMPLETIONS.has(liveState.completion)) {
    archiveAndExit(sessionStatePath);
    // Settle once per verdict. If this state was written before the last settle,
    // its verdict is already recorded, and what changed since is new work.
    const alreadySettled = !!(run && run.settled_at && liveStateMtime <= run.settled_at);
    if (!alreadySettled) {
      settleReview(sessionId, cwd, run, `state ${liveState.completion}`);
      emitAllowStop(`state terminal: ${liveState.completion}`);
    }
    liveState = null;
  }
  {
    const pending = isPending(run);
    // A hook-dispatched review is looked for since its dispatch. A review the
    // session started by hand leaves no dispatch record, so look back one stale
    // window, and never before the last verdict already recorded.
    const since = pending
      ? seenUntil(run)
      : Math.max((run && run.settled_at) || 0, nowMs() - REVIEW_STALE_MS);
    const ended = reviewEndedSince(sessionId, since, payload, tail, pending);
    if (ended) {
      settleReview(sessionId, cwd, run, ended);
      emitAllowStop(`review-finished: ${ended}`);
    }
    if (pending) {
      const ageMin = Math.round((nowMs() - run.dispatched_at) / 60000);
      const started = reviewStarted(sessionId, run, tail);
      if (started && nowMs() - run.dispatched_at < REVIEW_STALE_MS) {
        writeRunRecord(sessionId, { ...run, started: true, last_seen_at: nowMs() });
        emitAllowStop(`review-in-progress: dispatched ${ageMin}m ago for this session, no verdict yet`);
      }
      // Never started (declined or interrupted), or no verdict within the window:
      // stop waiting, and let the gates below decide on what has changed since.
      logLine(started
        ? `review-stale: session=${sessionId} dispatched ${ageMin}m ago with no verdict; re-arming`
        : `review-not-started: session=${sessionId} was asked ${ageMin}m ago and started no review; no longer waiting`);
      run = { ...run, dispatched_at: null };
      writeRunRecord(sessionId, run);
    }
    if (liveState && nowMs() - liveStateMtime < REVIEW_STALE_MS) {
      emitAllowStop('review-in-progress: this session has a live, unfinished review state');
    }
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

  // Escape hatch 6: stale-lockfile sweep (PID-liveness based, not mtime only).
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
  const classified = classifyDiff(files, planRegexes, codeExts, deliverableRegexes);
  const { planFiles, codeFiles, deliverableFiles, skipFiles } = classified;
  let { mode } = classified;

  // Gate A — nothing reviewable changed (only docs / handoffs / lockfiles /
  // scratch / generated). The most common low-value session; skip it.
  if (mode === null) {
    const why = files.length === 0
      ? 'empty diff'
      : `${skipFiles.length} non-reviewable file(s) [${skipFiles.slice(0, 5).join(', ')}${skipFiles.length > 5 ? ', …' : ''}]`;
    emitAllowStop(`nothing-reviewable: ${why}`);
  }

  // Gate B — every reviewable file was already dispatched for review in exactly
  // its current content. The key is per reviewable file, so changing a reviewed
  // file re-arms the loop and changing a skipped file does not (run /review-loop
  // manually to force).
  const reviewableFiles = planFiles.concat(codeFiles, deliverableFiles);
  const root = top || cwd;
  const firedPath = top ? lastFiredPath(sha1Short(top)) : null;
  const fingerprints = fingerprintFiles(root, reviewableFiles);
  const dispatched = readDispatched(firedPath);
  if (firedPath && allDispatched(reviewableFiles, fingerprints, dispatched)) {
    emitAllowStop(`diff-unchanged-since-last-review (${reviewableFiles.length} reviewable file(s) unchanged)`);
  }

  // Gate C — whose work is it? Asked only now, because the guard reads whole
  // transcripts and every cheaper exit has already had its chance.
  // Independence: the Stop event fires IN the authoring session, so a hook-dispatched
  // review is a self-review unless the tree was already dirty when the session began.
  // Pass what was measured rather than assuming either way.
  // A bystander stops many times beside the same foreign work. The guard reads
  // whole transcripts, so its "none of these are yours" answer is kept for as long
  // as the reviewable files stay byte-identical: this session cannot have written
  // a file whose content has not changed.
  const stateKey = crypto.createHash('sha256').update(JSON.stringify(
    reviewableFiles.map(f => [relKey(f), fingerprints[relKey(f)]]).sort())).digest('hex');
  if (run && run.not_authored_key === stateKey) {
    emitAllowStop(`not-authored: the ${reviewableFiles.length} reviewable changed file(s) are unchanged since the guard last found none of them this session's`);
  }
  const authorship = checkAuthorship(sessionId, cwd);
  const independence = authorship ? authorship.independence : 'unverified';
  // The files this dispatch covers. When the guard can name this session's files,
  // only those are recorded as dispatched, so the session that wrote the other
  // changed files still gets a review of its own (its review may run other lenses).
  let covered = reviewableFiles;
  if (authorship && independence !== 'unverified') {
    // The guard could read the transcripts and the tree, so it can say which
    // changed files this session wrote. A shared checkout holds other sessions'
    // uncommitted work too, and blocking this session on that work only produced
    // "these changes are not mine" replies (several sessions, 19-23 Nov 2026).
    const wrote = new Set(
      authorship.authoredFiles.concat(authorship.weakFiles, authorship.timingFiles).map(relKey),
    );
    // A deleted file has no modification time and `rm` names no write, so the
    // guard has no way to attribute a deletion. Missing signals are not proof the
    // session did not make it: a deleted reviewable file counts as possibly its own.
    // A file the session wrote and then moved sits here under a path no tool call
    // names. If the session wrote files in this repo that are now gone, every new
    // file (untracked, or added since HEAD) counts as possibly its own.
    const newFiles = authorship.orphanedEdits.length
      ? new Set(untracked.concat(gitAddedFiles(cwd)).map(relKey))
      : new Set();
    const mine = reviewableFiles.filter(f => wrote.has(relKey(f))
      || fingerprints[relKey(f)] === 'absent' || newFiles.has(relKey(f)));
    if (mine.length === 0) {
      writeRunRecord(sessionId, { ...(run || {}), not_authored_key: stateKey });
      emitAllowStop(`not-authored: this session wrote none of the ${reviewableFiles.length} reviewable changed file(s) `
        + `[${reviewableFiles.slice(0, 5).join(', ')}${reviewableFiles.length > 5 ? ', …' : ''}] (guard=${independence})`);
    }
    if (firedPath && allDispatched(mine, fingerprints, dispatched)) {
      emitAllowStop(`diff-unchanged-since-last-review (the ${mine.length} reviewable file(s) this session wrote are unchanged; `
        + `${reviewableFiles.length - mine.length} other changed file(s) are not its work)`);
    }
    // Route by this session's own files, so its report is not sent to the code
    // lenses because another session left source files dirty beside it.
    const own = classifyDiff(mine, planRegexes, codeExts, deliverableRegexes);
    if (own.mode) mode = own.mode;
    covered = mine;
  }
  // When the guard cannot tell (no transcript found, git unreadable, or the guard
  // is missing), the hook still fires, on every reviewable file, marked
  // `unverified`. An unreadable transcript is not evidence that the work is someone
  // else's, and a skipped review is the costlier mistake.

  // Warranted — record that we DISPATCHED a review for this state, then
  // re-invoke the /review-loop skill, routed to the right reviewer lens set.
  // Note: this records dispatch, not completion — if the review is interrupted,
  // the same state won't auto-re-fire (run /review-loop manually to force).
  const coveredPrints = {};
  for (const f of covered) coveredPrints[relKey(f)] = fingerprints[relKey(f)];
  recordDispatched(firedPath, coveredPrints, files, gitBranch(cwd));
  writeRunRecord(sessionId, { ...(run || {}), dispatched_at: nowMs(), started: false, mode, files: covered.map(relKey) });
  const iteration = 0; // first invocation; skill will increment internally.
  const reviewable = planFiles.length + codeFiles.length + deliverableFiles.length;
  const selfAuthored = independence === 'refused' || independence === 'self-acknowledged';
  const independenceFlag = ` --independence ${selfAuthored ? 'self-authored' : independence}`;

  const skillCommand = `/review-loop --auto --session-id ${sessionId} --iteration ${iteration} --mode ${mode}${independenceFlag}`;
  logLine(`block: invoke skill iter=${iteration} mode=${mode} files=${files.length} reviewable=${reviewable} skipped=${skipFiles.length} independence=${independence}${selfAuthored ? ` authored=${authorship.authoredFiles.length}` : ''}`);
  emitBlock({
    skillCommand,
    systemMessage: selfAuthored
      ? `Review-loop iter ${iteration + 1} (${mode} mode) — SELF-REVIEW: this session edited ${authorship.authoredFiles.length} file(s) under review, so its verdict cannot be signed as independent. A fresh session is still required to land this.`
      : `Review-loop iter ${iteration + 1} starting in ${mode} mode (skip next: touch ${SKIP_NEXT_MARKER})`,
  });
}

try {
  main();
} catch (err) {
  logLine(`ERROR: ${err && err.stack ? err.stack : String(err)}`);
  // Never block stop on an internal hook error.
  process.exit(0);
}
