#!/usr/bin/env node
/**
 * PostToolUse hook — detects four runtime-thrash patterns and emits a
 * single stderr nudge per fire.
 *
 *   1. Edit-thrash:   >=4 Edits to same file in last 10 calls AND >=1 errored.
 *   2. Read-loop:     >=6 Reads of same file with no intervening Edit/Bash in last 12.
 *   3. Test-loop:     >=3 consecutive errored Bash test runs with no source Edit between.
 *   4. Tool-flail:    >=5 distinct tool types in last 6 with no successful state-change.
 *
 * Output on fire: stderr line
 *     [chat-arch] thrash:<kind> — consider <hint>
 * plus an append to ~/.claude/cache/chat-arch-thrash/fires.jsonl for
 * calibration audit.
 *
 * Cooldown: 5 minutes per session (any-trigger).
 *
 * Clear conditions (drop the rolling window down to zero, reset cooldown):
 *   - successful test command (test pass detected in tool output)
 *   - successful build command (build pass detected)
 *   - successful git commit
 *
 * User-override: if any user turn in the session transcript since the
 * last fire contains "I know" / "intentional" (case-insensitive), the
 * session is suppressed for the rest of its lifetime.
 *
 * Env-var gate: only fires when CHATARCH_THRASH_DETECT=1 (else exits 0).
 *
 * Hermetic: no chat-arch repo import. Thresholds duplicated in
 * `./thresholds.cjs`. Canonical source documented there.
 *
 * Stdin payload (Claude Code PostToolUse event):
 *   { session_id, transcript_path, cwd, tool_name, tool_input,
 *     tool_response?, tool_use_id?, hook_event_name, ... }
 *
 * The hook always exits 0 — a failure here must NEVER block the user's
 * tool-call stream. All errors are swallowed silently except in
 * --verbose mode (used by the test harness).
 */

'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const THRESHOLDS = require('./thresholds.cjs');

const STATE_ROOT = path.join(os.homedir(), '.claude', 'cache', 'chat-arch-thrash');
const FIRES_LOG = path.join(STATE_ROOT, 'fires.jsonl');

const ENV_GATE = 'CHATARCH_THRASH_DETECT';

// ---- module-level helpers (pure, tested) ----

function nowMs(injected) { return typeof injected === 'number' ? injected : Date.now(); }

function ensureDir(dir) {
  try { fs.mkdirSync(dir, { recursive: true }); } catch (_) { /* ignore */ }
}

function readJsonSafe(p) {
  if (!fs.existsSync(p)) return null;
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); }
  catch (_) { return null; }
}

function writeAtomic(targetPath, text) {
  ensureDir(path.dirname(targetPath));
  const tmp = `${targetPath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmp, text, 'utf8');
  fs.renameSync(tmp, targetPath);
}

function readStdinSync() {
  try {
    const chunks = [];
    const buf = Buffer.alloc(65536);
    for (;;) {
      let n;
      try { n = fs.readSync(0, buf, 0, buf.length, null); }
      catch (e) { if (e.code === 'EAGAIN') continue; break; }
      if (!n) break;
      chunks.push(Buffer.from(buf.subarray(0, n)));
    }
    return Buffer.concat(chunks).toString('utf8');
  } catch (_) { return ''; }
}

// ---- event normalization ----

/**
 * Normalize a Claude Code PostToolUse payload to the minimal shape
 * the trigger functions consume. The payload schema is loose across
 * Claude Code versions; we accept either `tool_response.is_error` or
 * a `tool_response.error` string. Bash output is sniffed with
 * regex-only heuristics — no shelling out.
 */
function normalizeEvent(payload, nowOverride) {
  const toolName = String(payload.tool_name || '').trim();
  const input = payload.tool_input || {};
  const response = payload.tool_response || {};
  const errored = Boolean(
    response.is_error === true ||
    response.error ||
    (typeof response.stderr === 'string' && response.stderr.length > 0 && response.exit_code && response.exit_code !== 0)
  );
  // `file_path` is the Edit/Read/Write convention; fall back to common synonyms.
  const filePath = input.file_path || input.path || input.notebook_path || null;
  const command = typeof input.command === 'string' ? input.command : '';
  return {
    ts: nowMs(nowOverride),
    tool: toolName,
    file: filePath ? String(filePath) : null,
    command,
    errored,
    output: typeof response.stdout === 'string' ? response.stdout : '',
    stderr: typeof response.stderr === 'string' ? response.stderr : '',
  };
}

/**
 * Recognize a Bash invocation as one of:
 *   - 'test'    — npm/pnpm test, vitest, jest, pytest, go test, cargo test, node --test
 *   - 'build'   — npm/pnpm build, tsc, cargo build, go build
 *   - 'commit'  — git commit
 *   - 'other'
 *
 * Used by both the test-loop trigger and the clear-condition check.
 */
function classifyBashCommand(cmd) {
  if (!cmd) return 'other';
  const s = cmd.toLowerCase();
  if (/\bgit\s+commit\b/.test(s)) return 'commit';
  if (/\b(npm|pnpm|yarn)\s+(run\s+)?(test|vitest|jest)\b/.test(s) ||
      /\bvitest\b/.test(s) || /\bjest\b/.test(s) || /\bpytest\b/.test(s) ||
      /\bgo\s+test\b/.test(s) || /\bcargo\s+test\b/.test(s) ||
      /\bnode\s+--test\b/.test(s)) {
    return 'test';
  }
  if (/\b(npm|pnpm|yarn)\s+(run\s+)?build\b/.test(s) ||
      /\btsc\b/.test(s) || /\bgo\s+build\b/.test(s) || /\bcargo\s+build\b/.test(s)) {
    return 'build';
  }
  return 'other';
}

/**
 * Was the most recent event a "successful state change"? Used by the
 * tool-flail clearer.
 *   - Edit/Write/MultiEdit that didn't error
 *   - Bash 'commit' that didn't error
 *   - Bash 'test' that didn't error (output looks like pass)
 *   - Bash 'build' that didn't error
 */
function isSuccessfulStateChange(ev) {
  if (ev.errored) return false;
  if (ev.tool === 'Edit' || ev.tool === 'Write' || ev.tool === 'MultiEdit' || ev.tool === 'NotebookEdit') return true;
  if (ev.tool === 'Bash') {
    const kind = classifyBashCommand(ev.command);
    return kind === 'commit' || kind === 'test' || kind === 'build';
  }
  return false;
}

// ---- triggers ----

/** >=4 Edits to same file in last `editThrashWindow` AND >=1 errored. */
function detectEditThrash(events, t) {
  const window = events.slice(-t.editThrashWindow);
  const edits = window.filter(e => e.tool === 'Edit' || e.tool === 'MultiEdit');
  if (edits.length < t.editThrashMinSameFile) return null;
  // Count per-file.
  const perFile = new Map();
  for (const e of edits) {
    if (!e.file) continue;
    perFile.set(e.file, (perFile.get(e.file) || 0) + 1);
  }
  for (const [file, count] of perFile) {
    if (count >= t.editThrashMinSameFile) {
      const fileEvents = edits.filter(e => e.file === file);
      const anyErrored = fileEvents.some(e => e.errored);
      if (anyErrored) {
        return {
          kind: 'edit-thrash',
          file,
          count,
          hint: `step back and re-read ${path.basename(file)} before the next edit`,
        };
      }
    }
  }
  return null;
}

/** >=6 Reads of same file with no intervening Edit/Bash in last `readLoopWindow`. */
function detectReadLoop(events, t) {
  const window = events.slice(-t.readLoopWindow);
  const perFile = new Map();
  // Track for each file: contiguous read run ending at the most recent event.
  // We walk left-to-right; whenever we see an Edit/Bash, reset all files'
  // counters; whenever we see a Read of file X, increment X's counter.
  let lastEditOrBashAt = -1;
  for (let i = 0; i < window.length; i++) {
    const ev = window[i];
    if (ev.tool === 'Edit' || ev.tool === 'MultiEdit' || ev.tool === 'Write' || ev.tool === 'Bash') {
      lastEditOrBashAt = i;
      perFile.clear();
    } else if (ev.tool === 'Read' && ev.file) {
      perFile.set(ev.file, (perFile.get(ev.file) || 0) + 1);
    }
  }
  for (const [file, count] of perFile) {
    if (count >= t.readLoopMinSameFile) {
      return {
        kind: 'read-loop',
        file,
        count,
        hint: `${path.basename(file)} has been read ${count}x without an Edit or Bash — try a Grep or a different file`,
      };
    }
  }
  // Suppress unused-variable lint
  void lastEditOrBashAt;
  return null;
}

/**
 * >=3 consecutive errored Bash test runs with no source Edit between.
 * Walks from the most recent event backward.
 */
function detectTestLoop(events, t) {
  let consecutive = 0;
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.tool === 'Edit' || ev.tool === 'MultiEdit' || ev.tool === 'Write') break;
    if (ev.tool !== 'Bash') continue;
    const kind = classifyBashCommand(ev.command);
    if (kind !== 'test') continue;
    if (!ev.errored) { consecutive = 0; break; }
    consecutive += 1;
    if (consecutive >= t.testLoopMinConsecutive) {
      return {
        kind: 'test-loop',
        count: consecutive,
        hint: `${consecutive} consecutive test failures without an Edit — read the failure first`,
      };
    }
  }
  return null;
}

/**
 * >=5 distinct tool types in last `toolFlailWindow` AND no successful
 * state-change in that window.
 */
function detectToolFlail(events, t) {
  const window = events.slice(-t.toolFlailWindow);
  if (window.length < t.toolFlailWindow) return null;
  const tools = new Set(window.map(e => e.tool).filter(Boolean));
  if (tools.size < t.toolFlailDistinctTools) return null;
  const anySuccess = window.some(isSuccessfulStateChange);
  if (anySuccess) return null;
  return {
    kind: 'tool-flail',
    distinctTools: tools.size,
    hint: `${tools.size} distinct tools in ${window.length} calls with no state change — pick a single path`,
  };
}

const TRIGGERS = [detectEditThrash, detectReadLoop, detectTestLoop, detectToolFlail];

/** Returns the first firing trigger or null. */
function detectAny(events, thresholds) {
  for (const fn of TRIGGERS) {
    const hit = fn(events, thresholds);
    if (hit) return hit;
  }
  return null;
}

// ---- clear conditions ----

/**
 * Did the most recent event clear the window? A successful test pass,
 * build pass, or commit clears.
 */
function isClearEvent(ev) {
  if (ev.errored) return false;
  if (ev.tool !== 'Bash') return false;
  const kind = classifyBashCommand(ev.command);
  return kind === 'test' || kind === 'build' || kind === 'commit';
}

// ---- session-state machinery ----

function sessionStatePath(sessionId) {
  return path.join(STATE_ROOT, `${sessionId}.json`);
}

function loadSessionState(sessionId) {
  const p = sessionStatePath(sessionId);
  const data = readJsonSafe(p);
  if (data && Array.isArray(data.events)) return data;
  return { events: [], lastFireAt: 0, suppressed: false, lastUserOverrideCheckedAt: 0 };
}

function saveSessionState(sessionId, state) {
  writeAtomic(sessionStatePath(sessionId), JSON.stringify(state) + '\n');
}

function pushEvent(state, ev, t) {
  state.events.push(ev);
  // Cap the window at THRESHOLDS.rollingWindow.
  if (state.events.length > t.rollingWindow) {
    state.events.splice(0, state.events.length - t.rollingWindow);
  }
  return state;
}

function clearWindow(state) {
  state.events = [];
  state.lastFireAt = 0;
}

function cooldownActive(state, now, t) {
  if (!state.lastFireAt) return false;
  return (now - state.lastFireAt) < t.cooldownMinutes * 60_000;
}

// ---- user-override (transcript-grep) ----

/**
 * Inspect the tail of the session transcript for "I know" / "intentional"
 * inside a user message. Returns true if the user has signaled
 * acknowledgement since the session started. Bounded read (last 200 KB).
 */
function userOverrideMarkerFound(transcriptPath) {
  if (!transcriptPath || !fs.existsSync(transcriptPath)) return false;
  try {
    const stat = fs.statSync(transcriptPath);
    const start = Math.max(0, stat.size - 200_000);
    const fd = fs.openSync(transcriptPath, 'r');
    const buf = Buffer.alloc(stat.size - start);
    fs.readSync(fd, buf, 0, buf.length, start);
    fs.closeSync(fd);
    const tail = buf.toString('utf8');
    // Scan user-message lines only — assistant messages might quote
    // these phrases incidentally.
    const re = /"role"\s*:\s*"user"[\s\S]{0,2000}?\b(I know|intentional)\b/i;
    return re.test(tail);
  } catch (_) { return false; }
}

// ---- fire bookkeeping ----

function logFire(fire, sessionId) {
  ensureDir(STATE_ROOT);
  const line = JSON.stringify({
    ts: new Date().toISOString(),
    sessionId,
    ...fire,
  }) + '\n';
  try { fs.appendFileSync(FIRES_LOG, line, 'utf8'); }
  catch (_) { /* ignore */ }
}

function emitFire(fire) {
  process.stderr.write(`[chat-arch] thrash:${fire.kind} — consider ${fire.hint}\n`);
}

// ---- main ----

/**
 * Exported core for testing. Pure relative to `(stdin, env, now,
 * stateRoot)` — never throws.
 */
function processEvent(stdinRaw, env, opts) {
  const t = THRESHOLDS;
  const now = nowMs(opts && opts.now);
  const stateRoot = (opts && opts.stateRoot) || STATE_ROOT;
  const firesLog = path.join(stateRoot, 'fires.jsonl');
  ensureDir(stateRoot);

  if (env[ENV_GATE] !== '1') return { skipped: 'env-gate' };

  let payload;
  try { payload = JSON.parse(stdinRaw); }
  catch (_) { return { skipped: 'bad-json' }; }

  const sessionId = payload.session_id || `unknown-${Date.now()}`;
  const statePath = path.join(stateRoot, `${sessionId}.json`);
  const state = (function load() {
    const data = readJsonSafe(statePath);
    if (data && Array.isArray(data.events)) return data;
    return { events: [], lastFireAt: 0, suppressed: false };
  })();

  // User-override check (transcript grep). Cheap-skip when transcript_path absent.
  if (!state.suppressed && payload.transcript_path) {
    if (userOverrideMarkerFound(payload.transcript_path)) {
      state.suppressed = true;
    }
  }

  const ev = normalizeEvent(payload, now);

  // Clear conditions short-circuit — drop window, exit without firing.
  if (isClearEvent(ev)) {
    state.events = [];
    state.lastFireAt = 0;
    writeAtomic(statePath, JSON.stringify(state) + '\n');
    return { cleared: ev.tool === 'Bash' ? classifyBashCommand(ev.command) : ev.tool };
  }

  pushEvent(state, ev, t);

  if (state.suppressed) {
    writeAtomic(statePath, JSON.stringify(state) + '\n');
    return { skipped: 'user-override' };
  }
  if (cooldownActive(state, now, t)) {
    writeAtomic(statePath, JSON.stringify(state) + '\n');
    return { skipped: 'cooldown' };
  }

  const fire = detectAny(state.events, t);
  if (!fire) {
    writeAtomic(statePath, JSON.stringify(state) + '\n');
    return { fired: null };
  }

  state.lastFireAt = now;
  writeAtomic(statePath, JSON.stringify(state) + '\n');

  // Side effects: stderr + fires.jsonl append.
  process.stderr.write(`[chat-arch] thrash:${fire.kind} — consider ${fire.hint}\n`);
  try {
    fs.appendFileSync(
      firesLog,
      JSON.stringify({ ts: new Date(now).toISOString(), sessionId, ...fire }) + '\n',
      'utf8',
    );
  } catch (_) { /* ignore */ }
  return { fired: fire };
}

function main() {
  try {
    const stdinRaw = readStdinSync();
    processEvent(stdinRaw, process.env, {});
  } catch (_) { /* always exit 0 */ }
  process.exit(0);
}

if (require.main === module) main();

module.exports = {
  // Pure helpers (tested)
  normalizeEvent,
  classifyBashCommand,
  isSuccessfulStateChange,
  detectEditThrash,
  detectReadLoop,
  detectTestLoop,
  detectToolFlail,
  detectAny,
  isClearEvent,
  pushEvent,
  cooldownActive,
  userOverrideMarkerFound,
  // High-level (tested via fixtures + state-root override)
  processEvent,
  // Internals exposed for tests
  STATE_ROOT,
  FIRES_LOG,
  ENV_GATE,
  THRESHOLDS,
};

// Suppress an unused-fn lint warning while keeping the helper alive for
// the integration test of the standalone main() path.
void emitFire;
void logFire;
void loadSessionState;
void saveSessionState;
void clearWindow;
void sessionStatePath;
