---
name: session-end
description: End or close out the current Claude Code session, producing an evidence-grounded record of what happened: decisions and rationale, claims and their verification status, load-bearing assumptions, artifacts changed, reversals. When work is mid-flight it also hands off, emitting a ready-to-paste continuation prompt for a fresh session. Formerly "session-handoff"; handoff is now its mid-flight mode.
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

## Operating principle: an inference must not read as a measurement

The next session reads this doc cold and acts on it. **An untagged claim is indistinguishable from a
measurement** — a diagnosis you reasoned your way to looks exactly like a fact you checked. That is the
failure mode this rule exists to stop, and it is not hypothetical:

> A handoff stated: *"The `review-pr` skill template is the source of that divergence."*
>
> It reads as a diagnosis. It was an inference, and it was wrong — the divergence was a **deliberate
> safety separation** between a code review that runs the tests and one that doesn't. The next session
> nearly "repaired" it, which would have let an explicitly-untested review auto-merge its own changes.
> It was caught only because that session happened to open both files. The same handoff also stated a
> mechanism wrongly (a script reading the cwd's `.env`, when it actually resolves from the script's own
> location) — harmless there, same root cause.

**The rule.** Every **load-bearing** claim carries an epistemic tag, or states its evidence inline so the
reader can judge for themselves. *Load-bearing* means: **the next session is likely to act on it without
re-deriving it** — a diagnosis, a mechanism, a state-of-the-world assertion, a number, a "X is why Y".
Passing colour and narration need no tag; don't tag everything, or the tags stop carrying signal.

| Tag | Means | Requires |
|---|---|---|
| `[verified]` | You read it, ran it, or observed it. | Name *how* — the file, command, or output. |
| `[derived]` | You **inferred** it from evidence. Sound reasoning, still an inference. | Name what it's inferred *from*, so the reader can re-run the inference. |
| `[assumed]` | No source; you took it as given. | Say what would confirm or falsify it. |
| `[unverified]` | Asserted during the session, never checked. | Say what checking it would take. |

`[derived]` is the tier that was missing, and it is the one that bites: an inference is grounded enough
to *feel* verified, so it gets written in the voice of a measurement. **If you reasoned your way to it,
it is `[derived]`, however confident you are.**

Stating the evidence inline satisfies this too, and is often shorter and better:
*"`review-pr/SKILL.md` has no test step and `code-review` does"* beats a bare tag — it hands the reader
the same evidence you had.

**The gate: no claim without a source.** If you can't name what a load-bearing claim rests on, it is
`[assumed]` — write it as `[assumed]`, or leave it out. Never close the gap by writing more confidently.
This applies to *every* section below, and hardest to Steps 3 and 5, where claims travel furthest from
the evidence that produced them.

## Step 1 — Gather evidence (do this before writing anything)

- `git status --short` and `git diff --stat` (+ `git log --oneline -15`) — what actually changed / was created. Every artifact you cite must appear here or on disk.
- **An empty `git status` / `git diff` is a signal to widen the search, not a finding.** A session whose
  work landed through merged PRs has a clean working tree *by construction* — the work is real, and these
  two probes are structurally blind to it. Reporting "no artifacts" there records a productive shift as
  an empty one. When both come back empty, go looking where landed work actually shows up:
  - `git log --oneline --since=<session start>` (and on the default branch) — what landed during the shift.
  - `git show --stat <sha>` per commit, or `git diff --stat <base-branch>...HEAD` — the files those commits
    touched. (Three-dot already means "since the merge base"; don't pass a merge-base into it.)
  - `gh pr list --state merged --search "merged:>=<session start date>"` — PRs that closed during the
    shift. Bound it by date: a bare `--limit N` returns the last N merges whenever they happened.
  - Output that lives outside this checkout entirely: another worktree, a sibling repo, or a non-repo
    artifact (a published page, a scheduled task, a review verdict left on a PR).
  If it is *still* empty after widening, say so as an explicit finding — "no files changed; this shift's
  output was X" — never as a silently omitted section.
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
- **Claims, diagnoses & numbers** — each tagged per the provenance rule above: `[verified]` / `[derived]` / `[unverified]` / `[assumed]`. **Not just numbers** — a diagnosis ("X is the source of Y"), a mechanism ("this script reads Z"), and a state-of-the-world assertion are claims too, and are the ones that mislead. Never present an unverified figure or an inference as fact. Note *how* the verified ones were checked and *from what* the derived ones were inferred.
- **Assumptions** — load-bearing ones flagged explicitly, with what would confirm/falsify each.
- **Artifacts** — files created/modified (paths), one line each on what + why. Cite from `git status` —
  or, when the tree is clean because the work merged, from the widened evidence of Step 1 (the landed
  commits and the files they touched). Say which, so "merged" never reads as "nothing happened".
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

Everything here is load-bearing by construction — it exists precisely so the next session acts on it
without re-deriving it. So **tag it** (`[verified]` / `[derived]` / `[unverified]` / `[assumed]`) or
state the evidence inline. Be strictest about any claim that would justify *changing* something: "this
looks wrong / is the cause / should be repaired" is a `[derived]` claim until you have read the thing
and can say why it is that way. If it might be deliberate, say so — that sentence is what stops the
next session from "fixing" a safety property.

## Step 4 — Write the handoff to disk (durable + machine-readable)

Write the full summary to a file so the next session can READ it rather than trust pasted prose:
`<primary-checkout>/.claude/handoffs/<UTC-timestamp>_<slug>.md` (create the dir; it's additive/safe).
If not in a writable repo, skip and emit in-chat only.

**Resolve `<primary-checkout>` explicitly — it is NOT necessarily where you are standing.** If this
session is running in a linked git worktree, the obvious answers (`git rev-parse --show-toplevel`, or the
cwd) give the *worktree's* root — and worktrees are routinely deleted when the task that created them
ends, taking the one artifact this skill exists to produce with them. A handoff that dies with its
worktree is worse than no handoff: the session reports success and leaves nothing behind. Resolve it:

```bash
git worktree list --porcelain | head -1 | sed 's/^worktree //'
```

The first entry is always the primary checkout. In an ordinary clone it returns that same checkout, so
run it unconditionally rather than branching on whether you think you're in a worktree. (In a bare-repo
+ worktrees setup the first entry is the bare `.git` directory, marked `bare` in the output — that has
no checkout to prefer, and it is the one location there that outlives every worktree, so it is still the
right target. Don't be surprised by a path ending in `.git`.) **State the
absolute path you wrote to in your closing message** — when the destination is not the directory the
session has been working in, that is precisely what the operator needs to see, not a silent redirect.

**On committing: don't — and you don't need to.** Durability here comes from *location*, not from
committing: the primary checkout outlives worktree cleanup, so the read-only gate stands. Whether
handoffs are *tracked* is the project's call and genuinely varies — some repos commit their handoff
history, others gitignore `.claude/` wholesale, and a handoff written under an ignored path is still
durable, just untracked. Report which you observed (`git check-ignore -v <path>`) and leave the decision
to the project; a declared close-out contract (Step 4b) is the one thing that can authorize more.

**Stamp a provenance marker as the very FIRST line of the handoff file** (before the H1), so a later automated
review can identify the handoff THIS session wrote and never a concurrent sibling session's:
`<!-- review-loop:session:<session-id> -->`. `<session-id>` is the current Claude Code session id (the UUID for
this session — e.g. the active transcript's id for this cwd, or a `--session-id` value surfaced in this
session). If you cannot determine it with confidence, write `<!-- review-loop:session:unattributed -->` — the
review will then safely SKIP reconciling this handoff rather than risk editing the wrong one. (HTML comments
are invisible in rendered markdown.) This is the only hook the `/review-loop` "Reconcile the session's handoff"
step keys on.

**Stamp where the session ran, on the line directly after the provenance marker:**
`<!-- session-end:origin branch=<current branch> worktree=<git rev-parse --show-toplevel> -->`. Writing to
the primary checkout is what makes the handoff durable, but it also means sessions running in *different*
worktrees now share one handoff directory. "The newest file" therefore stops being a safe way for a later
`session-pickup` to tell whose handoff it is holding: end two concurrent sessions minutes apart and the
newest belongs to whichever finished last, not to the branch being resumed. This line is what lets pickup
disambiguate, and it is the reason to spend two values on it. Note this is the *worktree's* toplevel — the
one place `--show-toplevel` is the right call, because here you are recording where you ran, not where
you write.

## Step 4b — Honour the project's close-out contract (only if it declares one)

Some projects have a session **acquire** state at start that must be **released** at end: a claim on a
shared desk or role, a lock, a lease, a "session N is live" marker. This skill cannot know what those
are — but a claim left held by a session that no longer exists silently blocks the next one, and the
symptom (a live-looking claim naming a dead session) points nowhere near the cause.

Look for a project-declared contract at `<primary-checkout>/.claude/session-close-out.md`. **If it is
absent, skip this step** — never invent close-out actions a project did not ask for. If present, read it
and do what it says. **The writes it authorizes are a declared, scoped exception to the read-only gate**
— that is the contract's entire purpose, and it is bounded by what that file names and nothing wider.

**If you cannot complete it, say so loudly — that is the whole job here.** A close-out that silently
half-ran is worse than one never attempted, because the project now believes it ran. Name the exact
state left held, where it lives, and the command to clear it — in the handoff's **Open threads** section
*and* in your closing message. An unreleasable claim the operator can see is a nuisance; one they
cannot is a trap for the next session.

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
6. **Keep the tags on.** The continuation prompt is the furthest a claim travels from its evidence — a
   pasted prompt has no `git log` behind it. Stripping `[derived]` to make the prompt read cleanly is
   exactly how an inference becomes the next session's premise. Carry the tag, or the inline evidence.

Present it in a fenced block, ready to paste. Keep it tight but complete — it is the single thing that determines whether the next session continues cleanly.

## Safety + quality gate

- **Read-only to the repo** except (a) writing the one handoff file and (b) whatever a declared close-out
  contract authorizes (Step 4b). No commits. Absent such a contract, no edits to other files.
- **The handoff went to the primary checkout**, not a worktree that is about to be deleted (Step 4), and
  its absolute path is stated in the closing message.
- Every cited artifact exists in `git status`/on disk **or in the widened evidence** of Step 1; every
  load-bearing claim is tagged or evidenced. A clean working tree was investigated, not assumed empty.
- **Re-read your own draft for untagged diagnoses.** Scan for the shapes that hide inferences: *"X is the
  source of Y" · "the reason is…" · "this reads the…" · "that's a bug/divergence/leftover" · "should be
  fixed"*. For each, ask: **did I check this, or did I conclude it?** Concluded → `[derived]` (or go
  check it now). This pass is cheap and catches the review-pr failure above.
- A reader with an empty context window could resume from the continuation prompt alone.
- If you're unsure whether something was decided vs. discussed, say so — don't assert it as settled.

## Complements (not duplicates)
`session-pickup` is the inverse — it rehydrates the next session from the handoff doc this writes (invoke
it at the start of a continued session; only relevant when this ran in mid-flight mode). `pattern-retrospective`
(rigorous multi-session studies) and `chat-history-search` (find past prompts) are heavier and
backward-looking. `session-end` is the fast, forward-looking close-out for *this* session — recording it
always, and handing it off when there's work to continue.
