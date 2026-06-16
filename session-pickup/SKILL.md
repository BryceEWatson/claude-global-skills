---
name: session-pickup
description: Start a continued Claude Code session by safely rehydrating from a prior session's handoff — the inverse of session-end. Finds the latest handoff in .claude/handoffs/, reads it + the docs it points to (tiered, not everything), RECONCILES it against current git/file state to catch anything that drifted since it was written, rebuilds the todo list + the settled constraints + open verification debts, then presents where you are + the immediate next action and confirms before acting. Invoke at the start of a session that continues prior work.
allowed-tools: Bash, Read, Grep, Glob, TodoWrite
---

# session-pickup

Resume a prior session with zero loss and zero false confidence. Pairs with `session-end` (which
wrote the snapshot). Your job is to rehydrate *and reconcile* — then hand control back to the operator
at the right decision point, not to barrel ahead.

## Operating principle: a handoff is a SNAPSHOT — trust, then verify

The handoff was true when written. Between then and now, things may have moved: commits landed, the
branch changed, another session advanced or abandoned the in-progress work, files were renamed. **Never
resume blindly on the handoff's word — reconcile it against current reality first.** This is the inverse
of session-end's "ground in artifacts": there you grounded the summary; here you ground the resume.

## Step 1 — Locate the handoff

- Default: the newest file in `<project-root>/.claude/handoffs/` (prefer the full handoff doc; also read
  its companion continuation-prompt file if one exists). Use `git rev-parse --show-toplevel` to find root.
- Accept an optional arg: a path or a slug to pick a specific handoff.
- **If none found:** say so plainly. Offer to (a) proceed cold from a stated goal, or (b) check
  `chat-history-search` for a prior session. Do not fabricate a handoff.

## Step 2 — Read tiered (honor the handoff's own tiers; don't over-read)

Read the handoff in full first. Then read only what it marks **must-read**; respect its
**read-on-demand** list (pull those only if Step 3 or the task implicates them). Over-reading here
re-creates the exact context-bloat the handoff existed to prevent.

## Step 3 — Reconcile with current reality (the safety core)

Gather and compare against the handoff:
- `git log --oneline -20` (+ since the handoff's date) — what landed since it was written?
- `git status --short` and current branch — does the branch match the handoff's? Uncommitted work?
- For each **artifact / in-progress item** the handoff cites: does the file still exist? has it changed
  since the handoff (mtime / diff)? was the "in-progress" work since committed, advanced, or abandoned?
- Re-check each **open verification debt / assumption** — is it still open, or was it resolved?

Produce a short **drift report**: `unchanged` (handoff still accurate) · `advanced` (work moved forward —
adjust the next step) · `conflicts` (reality contradicts the handoff — STOP and surface it). Never
silently resume past a `conflicts`.

## Step 4 — Re-establish working state

- Rebuild the **TodoWrite** list from the handoff's in-progress + next-step, adjusted for any drift.
- Restate the **settled constraints to HONOR** and the **open verification debts** (carry assumptions as
  assumptions — do not promote them to fact just because a prior session wrote them down).
- Note explicitly **what NOT to redo** (the handoff's done/settled items).

## Step 5 — Orient + confirm (don't auto-execute)

Present a tight orientation: **where we are · what changed since the handoff · the immediate next
action**. Then:
- If the handoff's next action carries a **STOP / approval gate** (e.g. "deliver review findings, then
  wait"), honor it — stop at that gate.
- Before any side-effecting work, **confirm with the operator** ("resume from here?"). For a clean
  `unchanged` drift report and a read-only next step, you may begin immediately and say so.

## Safety + quality gate

- **Read-only to the repo** (+ TodoWrite). Pickup orients; it does not change files or commit.
- Distinguish **handoff-claims** from **pickup-verified** in your orientation — label what you confirmed
  against current state vs. what you're taking on the handoff's word.
- A `conflicts` drift finding always halts for the operator — resuming on stale state is the failure mode
  this skill exists to prevent.

## Complements
`session-end` writes the snapshot this reads. `chat-history-search` recovers older sessions with no
handoff file. `pattern-retrospective` mines many sessions; `session-pickup` resumes exactly one.
