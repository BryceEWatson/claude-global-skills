---
name: review-loop
description: |
  Dispatches a multi-agent review team over the work in this session,
  runs an execution-grounded check (lint/test/build), validates findings
  through a falsifier stage, addresses load-bearing issues, and re-reviews
  until clean or budget hits. On the terminal verdict, always records a
  commit-pinned verdict comment on the branch's open PR (if one exists), so
  the review leaves a trail. Invoked manually as /review-loop or
  automatically by the Stop hook after real work outside plan mode. Use
  `--mode claim` to review a non-code analytical conclusion (a research
  finding or comparison verdict) by sending reviewers back to its primary
  sources to falsify it.
allowed-tools: Read, Grep, Glob, Bash, Task, Edit, Write
---

# /review-loop

You are running the auto-review-loop skill. The Stop hook installed at
`~/.claude/skills/review-loop/stop-hook.cjs` invoked you after Claude
finished a session with real code changes outside plan mode. (Or the user
invoked you manually.)

## Argument parsing

Parse from the slash-command arguments:

- `--session-id <id>` — required; uniquely identifies this session
- `--iteration <n>` — 0-indexed iteration count; defaults to 0
- `--max-iter <n>` — defaults to 3 (per `THRESHOLDS.reviewLoop.maxIterations`)
- `--cost-ceiling-tokens <n>` — combined input+output token budget; default 300000
- `--scope <git-diff|head-1>` — default `git-diff` (uncommitted changes)
- `--auto` — set when hook-invoked; suppresses preamble, writes structured exit
- `--mode <code|plan|claim>` — default `code`. Controls which reviewer lens set
  fires in Step 4. The Stop hook auto-selects: any changed file matching
  the plan-artifact globs (`**/*-plan.md`, `**/*-proposal.md`, `**/*-spec.md`,
  `**/*-retrospective.md`, `**/*-research/**/*.md`, or a project override at
  `.claude/review-loop.plan-paths`) → `--mode plan`. Otherwise → `--mode code`.
  `--mode claim` reviews an **analytical conclusion / research finding** (not a
  diff): the "artifact" is a set of load-bearing claims plus the primary sources
  they cite, and reviewers are sent back to those sources to try to break the
  conclusion. Manual-invocation only (the hook never auto-selects it); pass the
  claims under review — and where their evidence lives — as the slash-command
  arguments. Manual invocation may override the mode.

Treat missing args defensively: any arg can be omitted, all have defaults.

## Workflow

### Step 1 — State load

State file: `~/.claude/skills/review-loop/.local-state/<session-id>.json`.

Shape:

```json
{
  "session_id": "...",
  "started_at": "<unix-ms>",
  "iteration": 0,
  "max_iterations": 3,
  "cost_spent_input_tokens": 0,
  "cost_spent_output_tokens": 0,
  "cost_ceiling_input_tokens": 300000,
  "last_diff_sha": null,
  "findings_history": [],
  "completion": null,
  "scope": { "branch": "...", "files": [] }
}
```

Read the file if it exists; otherwise initialize.

### Step 2 — Concurrent-run locks

Two locks needed:

- **Per-session lock** `.local-state/<session-id>.lock` — use `fs.openSync(path, 'wx')` (O_EXCL).
- **Per-repo lock** `.local-state/repo-<sha1(toplevel)>.lock` — same pattern. Hash of `git rev-parse --show-toplevel`.

Lock files store `<pid>` + `<unix-ts>`. If either fails to acquire:
- Read the existing lock's PID; check liveness via `process.kill(pid, 0)`.
- If lock's mtime > 30 min ago AND PID is dead → remove stale lock, retry once.
- Otherwise → log `skip: concurrent-loop-in-repo` to `.local-state/<session-id>.log` and exit cleanly.

### Step 3 — Gather scope

**Claim mode:** skip the git commands below — there is no diff. Scope = the
verbatim claims under review (from the invocation args, or a named artifact
file) + the primary sources they cite. Use the claims' combined text as the
`diff_sha` equivalent for the stall check and as the reference text for the
Step 6 drift-guard. Skip the "Leave a trail on the PR" section unless the
analysis lives in a file tied to an open PR.

Run via Bash:
- `git status --porcelain` — list of changed files
- `git diff HEAD` — full diff text
- `git rev-parse --abbrev-ref HEAD` — current branch
- SHA the diff via Bash `git diff HEAD | sha256sum` (or compute in Node).

If `state.last_diff_sha === current_diff_sha` AND `state.iteration > 0`:
- Stall: same diff after attempted fixes means we're not making progress. Write `completion: 'stalled'` to state; emit `<promise>review-stalled</promise>` + unresolved checklist; exit.

### Step 4 — Dispatch reviewers (parallel)

The reviewer lens set depends on `--mode`:

#### Mode `code` (default)

Read the 6 agent files:
- `agents/simplicity.md`
- `agents/design-coherence.md`
- `agents/adversarial.md`
- `agents/ship-readiness.md`
- `agents/statistical-rigor.md` (skip if project has `.claude/review-loop.disabled-roles` listing `statistical-rigor`)
- `agents/execution-grounded.md` (special — see below)

#### Mode `plan`

The code-tuned lenses misfire on planning documents (`ship-readiness` has nothing to say about a markdown spec; `execution-grounded` has no `pnpm lint` to run on a `.md` file; `simplicity` flags prose redundancy as if it were duplicated code). Use this lens set instead:

- `agents/vision-coherence.md` — does the plan realize each non-negotiable / falsifier / stated goal of the source-of-truth doc the plan derives from (VISION.md, CLAUDE.md, charter, RFC)?
- `agents/operator-empathy.md` — will the human who acts on this plan actually be able to act on it under realistic constraints (touch-time, mobile parity, anxiety surfaces, decision fatigue)?
- `agents/architecture.md` — does the plan respect declared layer separations, introduce silent state, bloat scope beyond a referenced scope-table, or imply Tier-1 file edits without surfacing them?
- `agents/completeness.md` — cross-reference the plan against every spec section it claims to cover; flag missing surfaces, untestable done-criteria, dangling Tier-1 asks.

Do NOT load `execution-grounded.md` in plan mode — there's nothing to execute.
Do NOT load `ship-readiness.md`, `simplicity.md`, or `statistical-rigor.md` in plan mode — they assume code-shape artifacts and produce category-error findings on prose.

#### Mode `claim`

The artifact is **not a diff** — it is one or more load-bearing analytical
claims (a research finding, a comparison verdict, a recommendation) plus the
primary sources they rest on. The failure modes are overstatement, confirmation
bias, missed disconfirming evidence, and method holes — not bugs. Use this lens
set:

- `agents/claim-falsification.md` — go to the cited primary sources and try to
  DISPROVE each load-bearing claim; hunt specifically for disconfirming evidence
  the analysis omitted.
- `agents/claim-method.md` — is the method sound enough to support the claim
  (do comparable data actually exist; is the sample non-degenerate; is
  attribution by an authoritative field, not inference; is an under-powered null
  being sold as a finding)?
- `agents/claim-calibration.md` — do stated confidence and the headline match
  the evidence? Flag overstatement, proxy-as-fact, absence-of-evidence-vs-
  evidence-of-absence, and caveat-then-ignore.
- `agents/claim-coverage.md` — did the analysis examine the RIGHT evidence?
  Selection/sampling bias, missed sources, over-aggressive filtering.

Do NOT load `execution-grounded.md` — there is nothing to run.
Do NOT load any code/plan lens — they category-error on an analytical claim.

#### Dispatch (all modes)

For each enabled agent file, dispatch a `Task` (subagent_type: `general-purpose`) with:
- The agent file's full body as the system instruction
- The diff + file list + branch name (plan mode: include the full content of changed plan files, not just the diff — review needs the surrounding doc context; **claim mode: include the verbatim claims under review + pointers to the primary sources/transcripts/data they cite, and instruct each reviewer to read those sources directly — its job is to break the claim against ground truth, not critique prose**)
- For iteration ≥ 2: the prior iteration's findings injected as a **Reflexion-style verbal reflection** (prepend: *"In the prior iteration you flagged: [list]. The developer applied fixes. Your new findings should reflect what's now true rather than re-litigate prior decisions."*)
- An instruction to return JSON findings only, no preamble
- **Plan mode only — hard PLAN-VS-CODE DISCIPLINE block**: "This document is a PLAN, not implementation. Do NOT emit findings of the form 'code at X does not implement Y' against a forward-looking spec. Valid findings: internal contradiction in the plan, missing required section, untestable done-criterion, constraint violation, Tier-1 ask missing for an implied file edit. Cap at 5 findings; quality over quantity."
- **Claim mode only — hard CLAIM DISCIPLINE block**: "You are reviewing an analytical CONCLUSION, not code. Ground every finding in the cited primary source (read it; do not trust the analysis's own summary of it). Valid findings: a load-bearing claim contradicted or unsupported by the source, disconfirming evidence the analysis omitted, a method flaw that makes the claim unsupportable, overstatement vs. stated confidence. A failed falsification (you tried and could NOT break a claim) is a valid, useful result — report it. Cap at 6 findings; quality over quantity."

All LLM reviewers dispatch in parallel (one message, multiple `Task` calls). Code mode: 5 lenses. Plan mode: 4 lenses. Claim mode: 4 lenses.

The **execution-grounded** reviewer (code mode only): not a Task dispatch. Invoke Bash directly to run the project's `pnpm lint`, `pnpm test`, `pnpm build` (or `npm run lint/test/build` if no pnpm-lock.yaml exists). Capture exit codes and stderr. Each non-zero exit → one finding with severity `high`, confidence `100`, `load_bearing: true`, category `execution-failure`, claim describing the failure. Timeout: 180s.

Accumulate token usage from each Task's response into `state.cost_spent_*_tokens`.

### Step 5 — Falsifier stage (NEW per industry research)

For each LLM-reviewer finding (NOT execution-grounded), dispatch a short-context falsifier `Task`:

> "A reviewer flagged this issue: [finding.claim] at [finding.file]:[finding.line] (category: [finding.category], severity: [finding.severity]). The actual diff at that location is: [diff snippet ±10 lines around the cited line]. Try to disprove the claim. If the finding refers to nonexistent code, misreads the change, attacks a strawman, or relies on hallucinated context, return `{falsified: true, evidence: '<short reason>'}`. Otherwise return `{falsified: false}`."

Findings with `falsified: true` are dropped before aggregation. Token budget per falsifier: ~8000 tokens.

**Claim mode:** the falsifier receives the relevant **primary-source excerpt**
(not a diff snippet) and tries to disprove the finding against it — i.e. confirm
the finding is itself grounded in the source before it's used to overturn a
conclusion. A finding that misreads or misquotes its source is dropped.

### Step 6 — Aggregate + drift-guard + dedup

After falsifier:

1. **Drift-guard.** Compute cosine similarity between each finding's `claim` text and the diff text. Drop findings with similarity < 0.3 (off-topic / drift, per CodeAgent QA-Checker pattern). Use a simple TF-IDF approximation if no embedding model available.
2. **Cross-iteration dedup.** For iteration ≥ 2: drop findings whose `claim` is cosine-similar (≥0.85) to any finding already in `state.findings_history[]` that was addressed.
3. **Confidence filter.** For iteration ≥ 2: drop LLM findings with `confidence < 70` (execution-grounded always kept).
4. **Classify.** `actionable` = severity ≥ medium AND `load_bearing: true`. `speculative` = the rest.

### Step 7 — Decide

- **All clean** (0 actionable findings): write `completion: 'review-clean'` to state; emit `<promise>review-clean</promise>` with a one-paragraph summary; release locks; exit.
- **More iterations available** (`iter + 1 < max_iterations` AND cost-spent < ceiling AND ≥1 actionable):
  - Apply fixes for `actionable` findings using `Edit` / `Write` tools (one fix per actionable finding; if a fix is non-obvious, write a tracking-only comment in the affected file with a `// REVIEW-LOOP-iter<N>:` prefix).
  - Increment `state.iteration`; append the current iteration's full findings to `state.findings_history`.
  - Save state.
  - Release locks.
  - Exit with stdout JSON: `{"decision": "block", "reason": "/review-loop --auto --session-id <id> --iteration <next> --max-iter <max> --mode <mode>", "systemMessage": "Review-loop iter <next>/<max>"}` — preserve the `--mode` value across iterations; the mode does not flip mid-loop.
- **Exhausted** (iter cap reached OR cost ceiling crossed):
  - Write `completion: 'review-exhausted'`.
  - Emit `<promise>review-exhausted</promise>` followed by an exit checklist listing unresolved `actionable` findings + the full `speculative` set.
  - Release locks; exit.

**Claim mode:** an `actionable` finding means a load-bearing claim is wrong or
overstated. The "fix" is to correct the conclusion — if the analysis lives in a
file, `Edit` it; if it lives only in the conversation, emit the corrected claim
in your summary. Re-running reviewers on a purely deflationary correction
(claiming *less*) is usually unnecessary — prefer a single pass unless a
correction introduces a NEW load-bearing claim.

## Output format

Every run produces a final assistant text block ending with exactly one of:
- `<promise>review-clean</promise>`
- `<promise>review-stalled</promise>`
- `<promise>review-exhausted</promise>`
- (no promise marker if iteration continues — the hook re-invokes you)

The Stop hook reads stdout for the JSON `{decision, reason, systemMessage}` block; the user sees the assistant text.

## Leave a trail on the PR (ALWAYS, when one exists)

On a **terminal verdict** (`review-clean` / `review-exhausted` / `review-stalled`) — i.e. the same point
you'd archive state — record the verdict ON the open PR for this branch, so "was this reviewed?" is
answerable from the PR itself, not from a chat transcript. This is **not optional**: a review without a
trail is treated as incomplete. It is self-gating — most loop runs are on uncommitted WIP with no PR, and
those post nothing.

1. **Detect the PR (read-only):** `gh pr view --json number,headRefOid,headRefName,state` for the current
   branch. If it errors, returns no PR, or `state != "OPEN"` → **skip silently** (no PR = nothing to do).
2. **Compose a commit-pinned verdict** from the template below. Pin to the PR's `headRefOid`. The verdict
   is valid only for the commit it ran against.
3. **Be honest about staleness:** if the reviewed diff is uncommitted WIP, or local `HEAD` differs from the
   PR's `headRefOid`, say so in the comment — the verdict covers the reviewed state, not necessarily the
   PR head. Never let a review of one state read as covering another.
4. **Idempotent — never spam:** if the PR's most recent comment already carries the
   `<!-- review-loop:<sha> -->` marker, update it with `gh pr comment <n> --edit-last --body-file <tmp>`;
   otherwise add one with `gh pr comment <n> --body-file <tmp>` (temp file — the body has markdown/newlines).
5. Posting to GitHub is the loop's only outbound action. It writes a comment to the operator's own PR
   (reversible — editable/deletable) and is the standing instruction, so post it as part of completing the
   review; do **not** block the terminal exit waiting for a separate confirm.

```
## 🔬 review-loop verdict — pinned to `<headRefOid short>`

**Reviewed:** `<scope/range>` on `<headRefName>` · **iterations:** <n> · **method:** automated multi-agent
review-loop — <K> lens subagents (<names>) in independent contexts + an execution-grounded lint/test/build
check. Findings falsified against the diff; not a human review.

**Verdict:** <review-clean ✅ | exhausted — N unresolved | stalled> <one line>

### Fixed in-loop
- **[load-bearing]** `path:line` — <issue> → <fix>

### Surfaced, not fixed
- `path:line` — <issue> (<why deferred>)

<!-- review-loop:<headRefOid> --> _Re-run after new commits to refresh._
```

## State archival

On terminal exit (clean / stalled / exhausted), move `state/<session-id>.json` to `state/archive/<session-id>-<unix-ts>.json` and the `.lock` file is removed. The hook checks for terminal `completion` field and exits 0 if present (per its escape-hatch list).

## Cost discipline

If at any point `cost_spent_input + cost_spent_output > cost_ceiling_input_tokens`:
- Skip remaining reviewer dispatches in this iteration.
- Treat current accumulated findings as the iteration's output.
- Move to Step 6 / Step 7 (decide).
- The `review-exhausted` branch will be taken in Step 7.

## When invoked manually (no `--auto` flag)

Print a one-line preamble: *"Running /review-loop on <branch>, iter <N>/<max>, scope=<scope>."* before Step 4. Otherwise identical.

## Notes

- Per memory entry `feedback-confidence-per-step`: each reviewer's prompt instructs it to **label `load_bearing` honestly** — in-iter fixes are limited to load-bearing items; speculative items surface as the exit checklist.
- Per memory entry `feedback-claude-code-not-api`: when surfacing cost to the user, label as "plan usage (API-equivalent)" not "$X spent."
- The Stop hook handles the infinite-loop guard (env var `CLAUDE_REVIEW_LOOP_ACTIVE=1` is set before each iteration's re-invocation); the skill itself does not need to check this — if you're running, you've been invited.
- The Stop hook is a cheap **always-on gate, not an always-on review**: after the safety exits it also skips when **nothing reviewable changed** (only docs / handoffs / lockfiles / scratch / generated files) or when the **exact diff was already dispatched** for review, logging the decision to `.local-state/hook.log` either way. Per-project knobs under `<repo>/.claude/`: `review-loop.disabled` (opt out entirely), `review-loop.plan-paths` (globs that count as plan artifacts), `review-loop.code-exts` (extensions that count as reviewable code; the file replaces the built-in default set).
