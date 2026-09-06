#!/usr/bin/env node
/**
 * review-loop authorship guard — "did the session asking for this review write the code?"
 *
 * A session reviewing its own work carries the authoring context, and therefore the same
 * blind spots that produced the defect. The rule "don't review your own work" was written
 * down and not followed, which is the exact failure class measured in
 * research/studies/2026-08-20_forcing-functions-vs-text-rules.md: a written rule holds when
 * following it IS the thing being produced, and fails when it is a separate step that leaves
 * no trace. This script is that step's trace.
 *
 * WHAT IT MEASURES (literally): whether this session's own transcript — and the transcripts of
 * subagents it dispatched — record a file-editing tool call whose target path is also a file in
 * the diff under review. That is an observation about tool calls, not proof of authorship;
 * see the confidence tiers below for where the two come apart.
 *
 * SIGNAL TIERS
 *   strong  — an Edit/Write/MultiEdit/NotebookEdit tool_use whose `file_path` is in the diff.
 *             Near-zero false positives: the harness records the path it actually wrote.
 *   weak    — a write-shaped Bash command mentioning a diff file's path. Real false-positive
 *             rate (e.g. `git diff scripts/x.mjs > out.txt` matches), so it WARNS and never
 *             hard-refuses on its own.
 *
 * FAIL-OPEN BY DESIGN. If the transcript cannot be found or read, the verdict is `unverified`,
 * not `self-authored`. A guard that wrongly claims "you wrote this" blocks legitimate reviews
 * and teaches people to route around it, which is worse than the written rule it replaces.
 * `unverified` still refuses to CLAIM independence — it is disclosed, not silently upgraded.
 *
 * Exit codes: 0 independent · 3 self-authored (refuse) · 4 unverified · 2 usage/internal error.
 */

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export const EDIT_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit"]);

/** Bash verbs that can create or mutate a file. Read-only verbs are deliberately absent. */
const WRITE_VERB = /(?:^|[\s;&|(])(?:tee|sed\s+-i|dd\s|truncate\s|install\s|patch\s)|>>?[^>]/;

/**
 * Normalize a path for comparison. Handles the three shapes the corpus actually contains:
 * `C:\\Users\\...` (escaped backslash), `C:/Users/...`, and MSYS `/c/Users/...`.
 * Windows filesystems are case-insensitive, so comparison is lowercased there.
 */
export function normalizePath(p) {
  if (!p) return null;
  let s = String(p).replace(/\\/g, "/");
  s = s.replace(/^\/([a-zA-Z])\//, "$1:/"); // MSYS /c/... -> c:/...
  s = s.replace(/\/{2,}/g, "/").replace(/\/+$/, "");
  if (/^[a-zA-Z]:/.test(s)) s = s[0].toLowerCase() + s.slice(1);
  return process.platform === "win32" ? s.toLowerCase() : s;
}

const realCache = new Map();

/**
 * Resolve a path to its true on-disk form before comparing.
 *
 * Necessary because the two sides of the comparison come from different places and Windows will
 * happily hand back two different spellings of one file: `git rev-parse --show-toplevel` returns
 * the long form, while a transcript can record the 8.3 short form (`RUNNER~1`) — which is exactly
 * what CI's temp directory uses, and what made this guard silently report `independent` there.
 * A file that no longer exists still canonicalizes through its longest surviving ancestor, so a
 * deleted file in the diff is not a hole in the check.
 */
export function canonicalize(p) {
  const norm = normalizePath(p);
  if (!norm) return null;
  if (realCache.has(norm)) return realCache.get(norm);
  let out = norm;
  try {
    out = normalizePath(fs.realpathSync.native(norm));
  } catch {
    const parts = norm.split("/");
    const tail = [];
    while (parts.length > 1) {
      tail.unshift(parts.pop());
      const prefix = parts.join("/");
      // Stop at a bare drive ("c:") or an empty prefix. On Windows a bare drive is
      // drive-RELATIVE and resolves to the current directory, which would silently rewrite an
      // unrelated path into this process's cwd. Neither tells us anything worth having.
      if (!prefix || /^[a-z]:$/i.test(prefix)) break;
      try {
        out = [normalizePath(fs.realpathSync.native(prefix)), ...tail].join("/");
        break;
      } catch { /* keep walking up to the first ancestor that exists */ }
    }
  }
  realCache.set(norm, out);
  return out;
}

/**
 * Pull every file-editing tool call out of one transcript's JSONL text.
 * Parses each line as JSON rather than regex-matching the raw text, so a Bash command that
 * merely CONTAINS the string `"name":"Edit"` (this file's own tests do) cannot register as an edit.
 */
export function extractAuthoredPaths(jsonlText, { diffPaths = [] } = {}) {
  const editTool = new Set();
  const bashWrite = new Set();
  const diffNorm = diffPaths.map(normalizePath).filter(Boolean);

  for (const line of String(jsonlText || "").split("\n")) {
    if (!line.trim()) continue;
    let rec;
    try {
      rec = JSON.parse(line);
    } catch {
      continue; // a truncated tail line is not evidence either way
    }
    const content = rec?.message?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (block?.type !== "tool_use") continue;
      if (EDIT_TOOLS.has(block.name)) {
        const fp = block.input?.file_path || block.input?.notebook_path;
        const norm = canonicalize(fp);
        if (norm) editTool.add(norm);
      } else if (block.name === "Bash" && typeof block.input?.command === "string") {
        const cmd = block.input.command;
        if (!WRITE_VERB.test(cmd)) continue;
        const haystack = normalizePath(cmd);
        for (const d of diffNorm) {
          // Match the full repo-relative or absolute path, never a bare basename:
          // `README.md` alone would collide across directories.
          if (haystack && haystack.includes(d)) bashWrite.add(d);
        }
      }
    }
  }
  return { editTool, bashWrite };
}

/** Every transcript belonging to a session: its own, plus each subagent it dispatched. */
export function transcriptPathsForSession(sessionId, { projectsRoot } = {}) {
  const root = projectsRoot || path.join(os.homedir(), ".claude", "projects");
  const found = [];
  let dirs = [];
  try {
    dirs = fs.readdirSync(root, { withFileTypes: true }).filter((d) => d.isDirectory());
  } catch {
    return found;
  }
  for (const d of dirs) {
    const own = path.join(root, d.name, `${sessionId}.jsonl`);
    if (fs.existsSync(own)) found.push(own);
    // Subagent transcripts live in a sidecar dir keyed by the parent session id. Command's
    // charter routes substantial implementation into isolated workers, so an authoring session
    // very often has zero edits of its own and all of them here.
    const sub = path.join(root, d.name, sessionId, "subagents");
    if (fs.existsSync(sub)) {
      try {
        for (const f of fs.readdirSync(sub)) {
          if (f.endsWith(".jsonl")) found.push(path.join(sub, f));
        }
      } catch { /* unreadable sidecar is not evidence */ }
    }
  }
  return found;
}

/**
 * Files in the change under review, as absolute normalized paths.
 *
 * The scope matters more than it looks. Comparing only the working tree against `HEAD` answers
 * "what is uncommitted", which is the WRONG question the moment the work has been committed —
 * and reviewing a pull request, the case this guard exists for, is exactly that case. A session
 * could author every committed file under review and still be told it was independent.
 *
 * So the default unions two things: uncommitted changes, and the commits this branch carries that
 * its base does not. `--scope head-1` narrows to the last commit, matching /review-loop's own flag.
 *
 * Returns `{ files, probeFailed }`. A git probe that fails is reported, never quietly rendered as
 * an empty diff: "I could not look" and "there is nothing there" must not collapse into one answer.
 */
export function diffFiles(repoRoot, { scope = "auto", base = null } = {}) {
  let probeFailed = false;
  const git = (args, { optional = false } = {}) => {
    try {
      return execFileSync("git", ["-C", repoRoot, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    } catch {
      // `optional` marks a probe whose failure is ordinary (no upstream, a root commit with no
      // HEAD~1). Anything else failing means we could not read the scope at all.
      if (!optional) probeFailed = true;
      return "";
    }
  };

  const top = git(["rev-parse", "--show-toplevel"]).trim();
  if (!top) return { files: [], probeFailed: true };
  const root = canonicalize(top);
  const names = new Set();
  const add = (out) => {
    for (const l of out.split("\n")) if (l.trim()) names.add(l.trim());
  };

  if (scope === "head-1") {
    add(git(["diff", "HEAD~1", "HEAD", "--name-only"], { optional: true }));
  } else {
    add(git(["diff", "HEAD", "--name-only"]));
    for (const l of git(["status", "--porcelain"]).split("\n")) {
      const m = l.match(/^..\s+(.+)$/);
      if (m) names.add(m[1].replace(/^"|"$/g, "").trim());
    }
    // Commits this branch carries that its base does not. Three-dot compares against the
    // merge-base, so a branch that is merely behind its base does not drag in unrelated files.
    const candidates = base ? [base] : ["origin/main", "main", "origin/master", "master"];
    for (const ref of candidates) {
      const merged = git(["merge-base", ref, "HEAD"], { optional: true }).trim();
      if (!merged) continue;
      add(git(["diff", `${merged}`, "HEAD", "--name-only"], { optional: true }));
      break;
    }
  }

  const files = [...names].map((rel) => ({
    rel,
    abs: canonicalize(path.resolve(root, rel)),
    raw: normalizePath(path.resolve(root, rel)),
  }));
  return { files, probeFailed };
}

/**
 * Core decision. Pure over its inputs so the tests can drive it without a repo or a transcript.
 */
export function assessAuthorship({ sessionId, transcripts, diff, transcriptsFound, probeFailed = false }) {
  const diffAbs = diff.map((d) => d.abs);
  const relByAbs = new Map(diff.map((d) => [d.abs, d.rel]));

  const edited = new Set();
  const bashSuspect = new Set();
  for (const text of transcripts) {
    const { editTool, bashWrite } = extractAuthoredPaths(text, {
      diffPaths: [...diffAbs, ...diff.map((d) => d.raw).filter(Boolean), ...diff.map((d) => d.rel)],
    });
    for (const p of editTool) if (diffAbs.includes(p)) edited.add(p);
    for (const p of bashWrite) {
      const hit = diffAbs.find((a) => a === p || a.endsWith(`/${p}`)) || (diffAbs.includes(p) ? p : null);
      if (hit) bashSuspect.add(hit);
    }
  }

  const authoredFiles = [...edited].map((a) => relByAbs.get(a) || a).sort();
  const weakFiles = [...bashSuspect].filter((a) => !edited.has(a)).map((a) => relByAbs.get(a) || a).sort();

  if (probeFailed) {
    // A git probe that failed is not an empty diff. Reporting "independent" here would let a
    // non-checkout, or an unavailable git, certify an independence it never measured.
    return {
      verdict: "unverified",
      authored: false,
      authoredFiles: [],
      weakFiles: [],
      reason:
        "Could not read the review scope: a git probe failed, so the set of files under review is " +
        "unknown. An unreadable scope is not an empty one.",
    };
  }
  if (!transcriptsFound) {
    return {
      verdict: "unverified",
      authored: false,
      authoredFiles: [],
      weakFiles: [],
      reason:
        `No transcript found for session ${sessionId}. Independence could not be verified — ` +
        "this is not evidence of independence, and the verdict must say so.",
    };
  }
  if (authoredFiles.length) {
    return {
      verdict: "self-authored",
      authored: true,
      authoredFiles,
      weakFiles,
      reason: `This session edited ${authoredFiles.length} file(s) that are in the diff under review.`,
    };
  }
  if (weakFiles.length) {
    return {
      verdict: "independent",
      authored: false,
      authoredFiles: [],
      weakFiles,
      reason:
        `No file-editing tool call targeted the diff, but ${weakFiles.length} file(s) appear in ` +
        "write-shaped shell commands. Weak signal — reported, not enforced.",
    };
  }
  return {
    verdict: "independent",
    authored: false,
    authoredFiles: [],
    weakFiles: [],
    reason: "No file-editing tool call in this session (or its subagents) targeted a file in the diff.",
  };
}

export function refusalMessage({ authoredFiles, repoRoot, prNumber }) {
  const shown = authoredFiles.slice(0, 8);
  const more = authoredFiles.length - shown.length;
  const checkout = prNumber ? `gh pr checkout ${prNumber}` : "gh pr checkout <n>";
  return [
    `review-loop refused: this session edited ${authoredFiles.length} file(s) in the diff under review.`,
    "",
    ...shown.map((f) => `  - ${f}`),
    ...(more > 0 ? [`  … and ${more} more`] : []),
    "",
    "A session cannot independently review work it wrote: it carries the authoring context, and",
    "therefore the same blind spots that produced the defect.",
    "",
    "What to do instead:",
    `  1. Dispatch a fresh session rooted in this project (${repoRoot}).`,
    `  2. Its first act: ${checkout}`,
    "  3. Run /review-loop there. It will sign a verdict this session is not allowed to sign.",
    "",
    "If no fresh session is possible, re-run with an explicit, recorded acknowledgement:",
    '  /review-loop --self-review-ack "<why no fresh session was possible>"',
    "That runs the review, stamps the verdict SELF-REVIEW (NOT INDEPENDENT), and withholds the",
    "`review-clean` token, so the auto-merge gate keeps holding and a human decides.",
  ].join("\n");
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const out = { json: false, selfReviewAck: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--json") out.json = true;
    else if (a === "--session-id") out.sessionId = argv[++i];
    else if (a === "--repo") out.repo = argv[++i];
    else if (a === "--pr") out.pr = argv[++i];
    else if (a === "--transcript") (out.transcripts ||= []).push(argv[++i]);
    else if (a === "--projects-root") out.projectsRoot = argv[++i];
    else if (a === "--scope") out.scope = argv[++i];
    else if (a === "--base") out.base = argv[++i];
    else if (a === "--self-review-ack") out.selfReviewAck = argv[++i] ?? "";
  }
  return out;
}

export function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  if (!args.sessionId) {
    process.stderr.write(
      "usage: review-loop-authorship-guard.mjs --session-id <id> [--repo <dir>] [--scope git-diff|head-1] [--base <ref>] [--json]\n",
    );
    return 2;
  }
  if (args.selfReviewAck !== null && !String(args.selfReviewAck).trim()) {
    // A blank acknowledgement is the exact bypass this hatch exists to prevent. The hatch buys
    // nothing except a recorded reason, and an empty reason records nothing while still turning
    // a detected self-review into a clean exit.
    process.stderr.write(
      'usage: --self-review-ack needs a reason, e.g. --self-review-ack "no fresh session possible: offline".\n' +
        "It is quoted on the verdict and in the receipt so a later reader can weigh it.\n",
    );
    return 2;
  }
  const repoRoot = path.resolve(args.repo || process.cwd());
  const files = args.transcripts?.length
    ? args.transcripts
    : transcriptPathsForSession(args.sessionId, { projectsRoot: args.projectsRoot });

  const texts = [];
  for (const f of files) {
    try {
      texts.push(fs.readFileSync(f, "utf8"));
    } catch { /* skip unreadable */ }
  }

  const { files: diff, probeFailed } = diffFiles(repoRoot, { scope: args.scope, base: args.base });
  const result = assessAuthorship({
    sessionId: args.sessionId,
    transcripts: texts,
    diff,
    transcriptsFound: texts.length > 0,
    probeFailed,
  });

  const receipt = {
    sessionId: args.sessionId,
    checkedAt: new Date().toISOString(),
    repoRoot,
    transcriptsRead: files.length,
    diffFileCount: diff.length,
    ...result,
    selfReviewAck: args.selfReviewAck,
    independence:
      result.verdict === "self-authored" && args.selfReviewAck !== null
        ? "self-acknowledged"
        : result.verdict === "self-authored"
          ? "refused"
          : result.verdict === "unverified"
            ? "unverified"
            : "independent",
  };

  // Durable receipt. The merge gate reads the verdict COMMENT, so a self-review that wrongly
  // writes the `review-clean` token would still land — closing that last gap needs a change in
  // scripts/automerge/ (Owner's pen), not here. What this file buys is that the bypass is no
  // longer invisible: the receipt records what the guard measured, whatever the comment claims.
  try {
    const dir = process.env.REVIEW_LOOP_STATE_DIR
      || path.join(os.homedir(), ".claude", "skills", "review-loop", ".local-state");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, `${args.sessionId}.independence.json`), `${JSON.stringify(receipt, null, 2)}
`);
  } catch { /* a receipt we cannot write must not block the check itself */ }

  if (args.json) {
    process.stdout.write(`${JSON.stringify(receipt, null, 2)}\n`);
  } else if (result.verdict === "self-authored" && args.selfReviewAck === null) {
    process.stdout.write(`${refusalMessage({ authoredFiles: result.authoredFiles, repoRoot, prNumber: args.pr })}\n`);
  } else {
    process.stdout.write(`independence=${receipt.independence} — ${result.reason}\n`);
  }

  if (result.verdict === "self-authored") return args.selfReviewAck === null ? 3 : 0;
  if (result.verdict === "unverified") return 4;
  return 0;
}

const invokedDirectly =
  process.argv[1] && normalizePath(process.argv[1]) === normalizePath(fileURLToPath(import.meta.url));
if (invokedDirectly) process.exit(main());
