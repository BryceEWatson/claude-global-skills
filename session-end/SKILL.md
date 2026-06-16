---
name: session-end
description: End / close out the current Claude Code session — produce an evidence-grounded record of what happened (key decisions + rationale, claims + verification status, load-bearing assumptions, artifacts created/changed, reversals). When work is mid-flight it ALSO hands off: captures the exact resumable state and emits a ready-to-paste continuation prompt for a fresh session. Invoke whenever you're wrapping up a session — whether the work is DONE or you're passing the baton (context filling up, switching tasks). Formerly "session-handoff"; handoff is now the mid-flight mode. Triggers: end / wrap up / close out / hand off this session.
allowed-tools: Bash, Read, Grep, Glob, Write, TodoWrite
---

# session-end

Close out a session with an evidence-grounded record — and, **when work is still in flight**, hand off
so a fresh session can pick up with zero loss. Two intents, one flow: *ending* always produces the
record; *handing off* (the mid-flight mode) adds the exact resumable state + a copy-pasteable
continuation prompt **for the next session (you, with an empty context window) and for the human operator**.
Optimize for *truth and resumability*, not for sounding complete.

## Operating principle: ground in artifacts, never in memory alone

A summary written from recollection will hallucinate. **Reconstruct the session from hard evidence
first**, then narrate. This is what makes the handoff *safe*.

## Step 1 — Gather evidence (do this before writing anything)

- `git status --short` and `git diff --stat` (+ `git log --oneline -15`) — what actually changed / was created. Every artifact you cite must appear here or on disk.
- The current **TodoWrite** list — the authoritative in-progress/next-step state.
- New/modified files of substance — Read or skim the ones central to the session (specs, code, docs).
- Skim the conversation for: explicit **decisions**, **claims/numbers** asserted, **assumptions** taken, **questions answered**, and any **reversals** (things that changed mid-session).
- If the work is a long arc, reconstruct the **chronology** from commit messages + file mtimes.

## Step 2 — Synthesize a DYNAMIC summary

Include only the sections that apply to *this* session (a design session, a debugging session, and a
research session produce different shapes). Always tag epistemics — this mirrors a
verify-first standard.

- **Session arc** — 2-4 sentences: what this session was, start to now.
- **Key decisions** — each with its one-line rationale. These are the load-bearing outcomes.
- **Claims & numbers** — each tagged `[verified]` / `[unverified]` / `[assumed]`. Never present an unverified figure as fact. Note *how* the verified ones were checked.
- **Assumptions** — load-bearing ones flagged explicitly, with what would confirm/falsify each.
- **Artifacts** — files created/modified (paths), one line each on what + why. Cite from `git status`.
- **Reversals / corrections** — anything that changed during the session (a dropped claim, a re-decided choice), so the next session doesn't resurrect it.
- **Open threads / unresolved** — questions still pending, deferred items, known gaps.

Keep it scannable (ranked, terse). Match length to session size; don't pad.

## Step 3 — Mid-flight continuation block (only if work is in progress)

If a task was underway when handoff was invoked, capture the exact resumable state:
- **What was in progress** and **precisely where it stands** (last completed step → next step).
- **Files/functions in play** (paths, and what's half-done in each).
- **Constraints that bind the continuation** — decisions/assumptions the next session must honor.
- **What NOT to redo** — settled choices, so the new session doesn't relitigate or duplicate.
- Any **pending command/verification** that was about to run.

## Step 4 — Write the handoff to disk (durable + machine-readable)

Write the full summary to a file so the next session can READ it rather than trust pasted prose:
`<project-root>/.claude/handoffs/<UTC-timestamp>_<slug>.md` (create the dir; it's additive/safe).
If not in a writable repo, skip and emit in-chat only. Never commit, never modify other files.

## Step 5 — Emit the continuation prompt (ONLY if work is mid-flight)

**If the session is DONE** — nothing in flight (work shipped, parked, or merged) — skip this step.
Close with the Step 2 record plus a one-line stop marker ("Nothing in flight; clean stopping point —
no resume needed."). Don't manufacture a continuation prompt when there's nothing to continue; that's
the whole point of *ending* vs *handing off*.

**If work is mid-flight** (Step 3 applied), emit a self-sufficient Claude Code prompt to start the next
session. It MUST:
1. Orient — project, branch, and that it continues a prior session.
2. **Point to disk first** — list the authoritative artifacts to READ before acting (the handoff file + the key specs/code), so the new session rehydrates from current state, not from the prompt's prose.
3. State the **immediate next action** concretely.
4. Carry the **load-bearing constraints, assumptions, and settled decisions** to honor (and what not to redo).
5. Note the verification debts (unverified claims/assumptions to confirm).

Present it in a fenced block, ready to paste. Keep it tight but complete — it is the single thing that determines whether the next session continues cleanly.

## Safety + quality gate

- **Read-only to the repo** except writing the one handoff file. No commits, no edits to other files.
- Every cited artifact exists in `git status`/on disk; every number is tagged for verification status.
- A reader with an empty context window could resume from the continuation prompt alone.
- If you're unsure whether something was decided vs. discussed, say so — don't assert it as settled.

## Complements (not duplicates)
`session-pickup` is the inverse — it rehydrates the next session from the handoff doc this writes (invoke
it at the start of a continued session; only relevant when this ran in mid-flight mode). `pattern-retrospective`
(rigorous multi-session studies) and `chat-history-search` (find past prompts) are heavier and
backward-looking. `session-end` is the fast, forward-looking close-out for *this* session — recording it
always, and handing it off when there's work to continue.
