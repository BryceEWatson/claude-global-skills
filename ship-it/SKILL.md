---
name: ship-it
description: >-
  Take a build task from first read to a shipped, reviewed PR — the full
  research → plan → review-the-plan → implement → test → PR → review-the-PR
  chain, looping each review checkpoint until the definition-of-done holds. Use
  whenever you state an end-to-end build intent: an issue to take all the way
  ("for issue 27", "read issue 21", "understand issue 42") or a freeform goal
  stated WITH the take-it-all-the-way chain ("implement and PR all of these
  changes", "build out the rest of the P1 work and review-loop it"). The
  headline upgrade over typing the chain by hand: it adversarially reviews the
  PLAN before any code is written, gates plan-approval through the operator,
  exercises the REAL app (not just unit tests), and never silently de-scopes.
  Triggers (verbatim phrasings it must fire on): "The goal is to fully research,
  plan, implement, test, PR, and review-loop the PR for issue 27", "Our goal is
  to fully research, plan, implement, test, PR, and review-loop the PR for issue
  N", "The goal is to read, research, plan + review-loop the plan, implement,
  test, PR, review-loop the PR issue 35", "understand issue 42, then research,
  plan, implement, test, PR, and review-loop until the issue is fully solved
  properly", "research and plan ... then fully implement it, test it, and
  review-loop the final PR", "fully research, plan, implement, test (visually),
  PR and then finally review-loop that", "loop until the issue is fully solved
  properly", "ship issue 27", "take issue 42 all the way / end to end",
  "/ship-it".
argument-hint: "[issue-number | goal description]"
---

# ship-it

Drive one goal end to end — research → plan → **review the plan** → implement → test for real → PR →
**review the PR** — and **loop each review checkpoint until done**, not until first pass. This is the
build chain you already type from memory, made durable, portable, and gate-disciplined. Optimize for
*verified completion against a pinned spec*, not for sounding finished.

## Operating principle: pin a spec, gate the chain, loop until the gate is clean

The value here is the gates, not the verbs. Externalize the discipline the freehand prompt trusts you
to hold in your head: review the PLAN adversarially **before** a line is built (when fixing it is a doc
edit, not a rebuild), block edits behind operator approval, exercise the real app, and gate "done" on
the global `/review-loop`'s explicit `<promise>review-clean</promise>` marker plus an in-scope audit.
Compose only capabilities that resolve in **any** project on **any** machine — the GLOBAL `/review-loop`,
native plan mode + `ExitPlanMode`, native `Agent` subagents, `AskUserQuestion`, `session-end`. Everything
project-local (its own test/lint/build, visual-QA, PR status) is **discovered or described generically**.

**Operates on the CURRENT repo (cwd) only** — it builds and ships the checked-out project, never another.
This skill is global, so it also loads in a Command operator session, where the delegate-the-WHAT rule still
binds: if the goal's work belongs in a *different* repo, file it there as a requirements issue rather than
implementing it here.

## Step 0 — Understand first (issue or freeform)

Detect the target from the argument, then comprehend before researching.
- **Issue-grounded** (`27`, `#35`, "issue 42", "read issue 21"): read the GH issue **in full** with native
  `gh issue view <n> --comments`; restate the problem, outcome, and acceptance criteria as the spec seed.
- **Freeform** ("all of these changes", "the rest", "this", or a named plan/spec file): take the conversation
  — and any named source-of-truth doc, **read IN FULL** ("read research/…-plan.md first; it is the spec") — as
  the seed. No argument → read the latest stated intent as the goal.
- Orient in the codebase: discover the **default branch**, the files the change will touch, and existing
  conventions. Pin **in-scope vs out-of-scope** from the start. The argument is a *seed*, never final scope.

## Step 1 — Clarify gate (only when genuinely ambiguous)

If scope boundaries, target environment (PC-local vs deployed), or what "done/tested" means are ambiguous,
**STOP and ask** via `AskUserQuestion` — structured options, **one marked "(Recommended)"**, one batched
round. This is the literal mechanism behind "ask me intelligent questions to maintain rock-solid alignment."
Don't interrogate; proceed unasked only when unambiguous, and never guess on a scope or environment fork.

## Step 2 — Research (subagent fan-out, read-only)

Fan out parallel `Agent` subagents (`subagent_type` `general-purpose` is the workhorse, `Explore` for
codebase mapping) to gather what implementation needs: repo patterns, relevant APIs/types, prior art,
failure modes, the concrete files that will change. Match depth to size — a one-file fix gets one pass; a
multi-system feature gets 3-5 agents. Return grounded findings (paths, signatures, constraints), tagged
*measured / derived / assumed* — not opinions, not edits. `Agent` is the guaranteed, portable fan-out
primitive — lean on it; a bare `research`/`deep-research` command may resolve to a non-portable
Command-local/plugin variant on some machines, so treat it as an opportunistic accelerator, never a
dependency. Blocking unknowns route back to Step 1, not to a guess.

## Step 3 — Write the plan to a tracked file (load-bearing)

Distill the spec seed + research + Step-1 answers into a full implementation plan, **written to a file in
the working tree** named with a plan-glob suffix (`<slug>-plan.md`; `*-proposal.md`/`*-spec.md` also work).
It MUST live on disk as a **tracked/changed** `.md` — not held only in conversation — because
`/review-loop --mode plan` still gathers a git diff to put the plan in scope. The plan must contain:
- the **source of truth** it derives from,
- explicit **in-scope** and **out-of-scope / Deferred** lists,
- a concrete, **testable definition-of-done** (incl. how the *real app* will be exercised, and the PR target).

This file is the authority the rest of the run keys on.

## Step 4 — Review the plan adversarially (the headline upgrade)

Before building anything, run the **GLOBAL** `/review-loop --mode plan --session-id <id>` against the plan
file (use a fresh, unique `--session-id` per checkpoint — e.g. `ship-<slug>-plan` here and `ship-<slug>-pr`
in Step 9; the plan and PR loops must **not** share an id, since `/review-loop`'s state file + per-session
lock key on it). Plan mode is first-class: 4 prose lenses (vision-coherence, operator-empathy, architecture,
completeness), a hard PLAN-VS-CODE discipline block, cap 5 findings/reviewer, **no** execution-grounded /
ship-readiness / simplicity / statistical-rigor lenses (they category-error on prose). Pass `--mode plan`
explicitly — **never `--mode claim`** (that's for analytical conclusions, not forward-looking plans).
- **Gate on the terminal marker:** `<promise>review-clean</promise>` → proceed. Re-run after each plan
  revision until clean. `review-exhausted`/`review-stalled` = unresolved findings remain → surface them to
  the operator; don't proceed silently. The mode never flips mid-loop.

## Step 5 — Plan-approval gate (ExitPlanMode, HARD)

Present the reviewed plan for operator approval. Call **`ExitPlanMode`** with the finished plan (`planFilePath`,
and `allowedPrompts` to pre-authorize follow-on commands like the project's test/build). **No `Edit`/`Write`
before approval.** Plan mode is operator-entered (Shift+Tab) — **never call `EnterPlanMode`**; the assistant
only *exits* through this gate. If the session isn't in plan mode, degrade to an explicit in-chat
confirm-before-implement. If the operator amends the plan, return to Step 4 to re-review the change.
(Behavioral fact: `/review-loop` is suppressed in plan mode and only auto-fires after you exit into real
edits — so the manual Step-4 run is the *only* plan review, and exiting here re-arms the Stop-hook backstop.)

## Step 6 — Implement on a fresh branch (scope-disciplined)

Create a **fresh branch off the default branch** (never whatever's checked out — a worktree may carry an
unrelated branch), then implement to the plan, fanning parallelizable work across `Agent` subagents. Hold
the line both ways: build **every in-scope item** (Build-Complete — never silently de-scope) and **nothing**
under Deferred/out-of-scope (no gold-plating). Keep the tree clean (no debug prints, no secrets, type safety).
- **Scope-cut flag (HARD):** if any in-scope item must be cut/deferred, or any scope must expand, **STOP and
  flag it** as an explicit, contestable decision for consent — never absorb it silently.

## Step 7 — Test for real (not just green checks)

Two required layers.
- **Automated:** discover and run **the project's own** test/lint/build (read `package.json` scripts /
  Makefile / `pyproject` / CI config — do **not** hard-name a skill) until green.
- **Exercise the real app:** actually run it and observe the change working — "verified so we can merge with
  confidence." For a UI, do **visual QA** at desktop + mobile and drive the live app (*use a visual-QA skill
  if one is present in this project, else drive the running UI / preview directly*); for a CLI/server, run it
  and observe real behavior. The native `run`/`verify` commands are portable fallbacks.

Design at least one **failure-path** check per success path. Inspect the actual output/screenshots — don't
claim "verified" from a mental model. Failures loop back to Step 6, then re-test.

## Step 8 — PR + publish wall (HARD)

Behind the publish wall: verify new files are tracked (`git ls-files`) and the build passes from clean, then
**confirm target + title + body with the operator BEFORE any push or PR creation** (outbound publish). Push
the fresh branch and open the PR with native `gh pr create` only after consent. Surface the PR URL. The open
PR is what lets the next step leave a durable, commit-pinned trail.

## Step 9 — Review the PR + loop until done

On the branch with the open PR checked out, run the **GLOBAL** `/review-loop --session-id <id>` (default
`--mode code`; force code mode for a code-dominant diff): 5 lenses + execution-grounded lint/test/build,
falsifier stage, in-iter fixes for load-bearing findings, re-review until clean or the budget/iter cap.
Because an open PR exists, it **always posts a commit-pinned verdict comment** (pinned to `headRefOid`,
idempotent) — **that comment IS the required trail** ("a review without a trail is incomplete"). The global
Stop hook is also armed as a forward backstop — it re-reviews only if new reviewable edits land that this
manual run didn't already cover (it skips an already-dispatched diff), so the trail stays current.
- **Definition-of-done gate:** declare done **only** when `<promise>review-clean</promise>` **AND** the
  commit-pinned PR comment is posted **AND** every in-scope plan item is implemented+tested **AND** docs are
  updated. `review-exhausted`/`review-stalled` or any unbuilt in-scope item → keep going / surface to the
  operator; never declare done at the budget wall. Close with the plain-language recap (lead with the
  concrete subject), the ship decision, and ranked next actions.

## Long-build survival

If context fills at **any** step, invoke `session-end` in mid-flight mode → it writes a durable handoff to
`.claude/handoffs/` (exact resumable state: last step → next, files in play, pinned scope/plan/done-state,
verification debts) + a paste-ready continuation prompt, so the build resumes losslessly in a fresh session.
`session-pickup` is the inverse. A multi-hour build hands off rather than dying half-done.

## Gates / safety

- **Clarify (Step 1):** ambiguity on scope/environment/done → `AskUserQuestion`, "(Recommended)" marked, one round.
- **Plan-review (Step 4):** don't proceed until `/review-loop --mode plan` emits `<promise>review-clean</promise>` over the written plan file.
- **Plan-approval (Step 5, HARD):** operator approves via `ExitPlanMode` (or, if not in plan mode, an explicit in-chat approval) before ANY edit; never `EnterPlanMode`.
- **Scope-cut (Step 6, HARD):** any cut/defer/expand of scope is surfaced for consent — never silent (Build-Complete).
- **Publish wall (Step 8, HARD):** confirm fresh-branch-off-default + target/title/body before any push or PR.
- **Definition-of-done (Step 9):** `review-clean` + commit-pinned PR comment + every in-scope item done+tested + docs updated. Treat `exhausted`/`stalled` as unresolved, not done.
- **Portability:** name only the GLOBAL `/review-loop`, native plan mode/`ExitPlanMode`/`Agent`/`AskUserQuestion`, `session-end`/`session-pickup`. Discover/describe everything else generically.
- **Match rigor to size:** a one-file fix needn't fan out 5 agents or run two full loops — scale the ceremony to the change. Concurrent builds in one repo collide on `/review-loop`'s per-repo lock; use a worktree per session.

## Complements (not duplicates)

`/review-loop` is one **segment** (review only) — `ship-it` invokes it at two checkpoints (plan + PR) and
loops on its verdict. `session-end`/`session-pickup` are session **lifecycle** (close/resume), used here only
as the mid-build escape hatch. The analysis/mining skills (`pattern-retrospective`, `transcript-analysis`,
`chat-history-search`, `global-review-loop`) are **backward-looking**. `ship-it` is the forward-looking
**driver** that chains research → PR → reviewed-done — the one thing none of the others do end to end.
