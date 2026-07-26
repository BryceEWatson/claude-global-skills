# Weekly Work Log — unattended Sunday-night run (scheduled-task prompt)

This is the self-contained prompt for the Claude scheduled task `weekly-work-log`
(`mcp__scheduled-tasks`, cron `0 22 * * 0`). It runs as a fresh local session with
NO memory of any prior conversation. Keep this file in sync with the live task; the
operator copies its contents into `create_scheduled_task({ prompt: ... })`.

The model is **judgment DRAFTS, the human GATES**. You curate EVERY interactive
Claude Code session of the week into the page, then open a PR. You never publish:
the PR is the approval gate (Bryce reviews + merges; CI deploys the merge).

---

You are the unattended Weekly Work Log runner for brycewatson.com. The user's checkout at
`C:\Users\Bryce\Projects\brycewatson.com` is READ-ONLY coordination state. Do all report
work in the dedicated worktree `C:\Users\Bryce\Projects\brycewatson.com-weekly-work-log`.
Follow `~/.claude/skills/weekly-work-log/SKILL.md` rules exactly. Do these steps in order.

## 0. Preflight (isolated, visible, bounded)
- Record the run immediately:
  `node "{{SKILL_HOME}}/run-state.cjs" start --worktree "C:/Users/Bryce/Projects/brycewatson.com-weekly-work-log"`
- Treat `C:\Users\Bryce\Projects\brycewatson.com` as `BASE` and the dedicated sibling
  path above as `WORKTREE`. Confirm BASE is a git repo and `gh auth status` is OK. Do NOT
  require BASE to be on `main` or clean. Never checkout, reset, stash, commit, or clean BASE.
- From BASE, run `git fetch origin main`. If it fails for a transient network reason,
  retry it exactly ONCE, then stop. Never loop.
- Check for an already-open PR whose head starts `work-log/weekly-`. If one exists and its
  changed files include BOTH `src/data/work-log.source.json` and
  `src/data/work-log.json`, record success with outcome `PR_ALREADY_OPEN` and its URL,
  report it, and STOP without creating a duplicate.
- Prepare WORKTREE from `origin/main` without disturbing BASE:
  1. Inspect `git -C BASE worktree list --porcelain`.
  2. If WORKTREE is registered, inspect `git -C WORKTREE status --porcelain`. If dirty,
     STOP rather than deleting work. If clean, remove it with
     `git -C BASE worktree remove --force WORKTREE`.
  3. If WORKTREE exists but is not registered, STOP. Never recursively delete an
     unregistered directory.
  4. Run `git -C BASE worktree prune`, then
     `git -C BASE worktree add --detach WORKTREE origin/main`.
  5. If the add fails, prune and retry the add exactly ONCE. Then stop on failure.
- `cd` to WORKTREE. Confirm it is clean and `HEAD` equals `origin/main`. All remaining
  commands in this task run in WORKTREE.
- On EVERY hard-stop path, first persist the failure:
  `node "{{SKILL_HOME}}/run-state.cjs" fail --reason-code <CODE> --message "<one-line public-safe reason>"`
  Then report the same reason. The durable state is what makes a failed Sunday run visible
  to Monday's preview routine.

## 1. Discover (deterministic, redacted, no LLM)
- `node scripts/draft-work-log-from-handoffs.mjs` — handoff claims/reversals/open-threads
  digest, written to `src/data/work-log.handoffs.json`.
- `node scripts/draft-work-log-sessions.mjs` (the per-session digest — the unit of
  curation). This writes the redacted, bounded `src/data/work-log.drafts.json`: one
  entry per interactive Claude Code session of the week (id, date, project, repo,
  `isPrivate`, redacted `userPrompts` steers, the assistant's own redacted `assistantNotes`
  reasoning, `toolCounts`, redacted `candidateCommits`).
- The two digests are SEPARATE files (they used to share one path and the second run
  clobbered the first). Both are already scrubbed; read ONLY these digests, never raw
  transcripts. Distil from BOTH: the handoff digest carries the tagged claims, reversals,
  and decisions; the session digest carries the per-session steers + assistant reasoning.

## 2. Curate EVERY session (the distillation — in-context, NOT subagents)
Distil **every** session in the digest into `src/data/work-log.source.json` so the page
shows the whole week. Work in-context (no fan-out subagents — keeps voice + scrub under
direct control). For each digest session **not already represented** in `source.json`
(idempotency — never duplicate an existing item, never touch a hand-authored one):
- **Public item (has a `primaryCommit`):** match on `primaryCommit` (stable across weeks).
- **Private / display-role item (no `primaryCommit`):** match on the session `id` ALONE.
  Do **not** fall back to `date+project` for these — display-role collapse maps many
  distinct real projects onto one label (all client work → `Akaya`, all finance →
  `Personal`), so two different private sessions on the same day share `date+project` and
  would wrongly dedupe (one dropped) or overwrite each other. Always write `id` = the
  digest session id so a re-run (catch-up double-fire, manual+cron overlap, a Step 5 retry)
  matches the existing item instead of drafting a duplicate.

- Write an `items[]` entry: `id` (the digest session id), `project`, `status`, `tier`,
  `title`, `summary`, and `"drafted": "auto-<today>"`.
- **`objectiveId` (goal link):** set it to the SPECIFIC registry goal the work advances
  (read `src/data/objectives.public.json`). Do NOT leave it off for a multi-goal project
  like Command — without it, every item falls through to that project's catch-all/parent
  goal and the goal lens collapses to one bucket. Client/private items (Akaya) get NO
  `objectiveId` (they must stay off `/goals`).
- **Voice (load-bearing):** plain, professional, **subject-led** titles (lead with the
  work, NOT "I"/"My"); narrative summary; **no em dashes, no " -- "**; **never announce
  the page's own honesty, and never narrate the withholding** (no "keeping it sealed",
  "surfaced here as its own thread", "belongs in an honest log"). The
  "steer→work→catch" arc is REJECTED — title + summary only.
- **Status (honest badge):** `shipped` (built + verified), `in progress`, or
  `designed, not proven` (machinery exists, no real result yet). Map a handoff `[verified]`
  claim → shipped; `[assumed]`/`[unverified]` → designed-not-proven. Mixed session → lead
  with the **frontier** status (the least-finished, framed as the frontier, never a deficit).
- **Tier:** `headline` for the few proof-moment / most-significant sessions (full
  badge+summary+detail); `routine` for the rest (compact one-line). Most are routine.
- **PUBLIC / featured sessions** (Command, DemandForge, claude-global-skills,
  brycewatson.com): set `primaryCommit` to the session's strongest candidate commit so the
  build git-verifies it (date derives from the commit). Optionally add `snippets` curated
  from the digest's steers/commit subjects (verbatim, already redacted).
- **PRIVATE / sensitive sessions — summarize through the privacy filter, do NOT drop or
  stub** (the decided requirement: *"Every session should be curated, without exception.
  Sensitive sessions should be redacted to avoid anything private or embarrassing."* and
  *"Talking about personal finance is fine, and good for the site content. We just need iron
  clad rules around it."*). For a display-role project (`Akaya` = the client codename
  `dropKnowledge`; `Personal` = `Finances`; `ShopForge`): write a generalized entry
  describing only the KIND of work (e.g. "Built a trustworthy evaluation harness for the
  question-answering system"; "Worked through personal financial planning"). **Write it
  like any other entry: say what the work WAS, never that you are withholding it.** NO
  "keeping the specifics sealed", "recording only the kind of work", "surfaced here as its
  own thread", "keeping it generic here", "belongs in an honest log", or "not
  public-facing". **Accuracy is the hard floor even here.** NO `primaryCommit`, NO `repo`,
  NO `snippets.verify` — these repos are NEVER git-read. Use the session `date`. Never name
  the client, niche, people, product, accounts, amounts, or codenames.
- **Per-project goal lines:** for any project new to `source.json`, add a `projects[]` entry
  with a durable `mission` (from the project's CHARTER/README, not one week) + a this-week
  `frontier` (derived). Display-role projects get a generalized mission too.

Then GATE your own drafting before anything builds:
- `node scripts/work-log-validate-source.mjs` — it fails LOUDLY on an em/en dash, a " -- "
  in authored prose, a denylisted token surviving your prose, a bad status/tier, or a
  display-role item carrying a git reference. FIX every flagged item and re-run until clean.
- Claim-falsification self-check: read `~/.claude/skills/review-loop/agents/claim-falsification.md`
  and `claim-calibration.md` and apply those lenses VERBATIM (do not fork them) to each
  drafted item — does the badge overstate the evidence? Downgrade any overstated badge.

## 3. Build (deterministic backstop; must pass or NO PR)
- `node scripts/work-log-via-honestweek.mjs` (the honestweek engine). It re-derives every
  date/number from git, verify-or-aborts every cited commit (resolves + is Bryce's), runs a
  numeric fact-fence over the output, redacts, and writes `work-log.json` +
  `reports/<week>.json` + `reports/index.json` + `goals.json`. If it aborts (a commit you
  cited did not resolve / is not Bryce's, or a number does not trace to a verified value),
  FIX that item (correct or remove the commit) and re-run. If it cannot be made to pass,
  STOP and open NO PR.
- Do NOT pass `--week` (no backfill from the cron). Do NOT hand-edit `work-log.json`,
  `reports/*`, or `goals.json` — only the build writes them.

## 4. Fail-open advisory (judgment that ASSISTS, never gates)
Write advisory notes to the gitignored `src/data/.local-state/advisory.md`. Wrap each check
so one failure degrades to a one-line note; if the whole layer fails, leave the sidecar
empty (step 5 prints a single "advisory unavailable" line). The advisory may only
DOWNGRADE/FLAG. Now that the feed is populated, these check your OWN distillation:
#2 badge-vs-prose (reuse the claim lenses), #3 coverage (any session you failed to curate),
#5 badge-vs-git reconciliation, #6 privacy (leak-by-meaning), #9 reversal coverage. Then run
`node scripts/work-log-harvest-nouns.mjs` and surface ONLY the count.

**Advisory is leak-safe by construction** (it is spliced into a PR body that goes LIVE on
GitHub the moment Step 5 opens the PR — BEFORE Bryce reviews it, so it never gets the human
gate the source items get): counts + high-level ONLY. When a check flags a PRIVATE /
display-role item, name the CHECK and the generic label + count, NEVER the item's content —
no client name, codename, repo, amount, or quoted prose. If you cannot phrase a note without
referencing private content, omit that note. (`work-log-weekly.mjs` also scrubs the sidecar
through the shared redactor before splicing and drops it to "advisory unavailable" on any
denylist/PII hit — but do not rely on that; the constraint above is yours to honor.)

## 5. Open exactly ONE PR (run LAST — mandatory UNLESS Step 0 or Step 3 told you to STOP)
- `node scripts/work-log-weekly.mjs --advisory src/data/.local-state/advisory.md`. It
  rebuilds + re-verifies and, if the committed data changed, opens ONE PR on a fresh branch
  (never `main`, never deploy), splicing the advisory into the body. The PR carries the
  curated week + your `drafted`-marked items for Bryce to review and merge.
- **"Unconditional" scopes to the ADVISORY (Step 4) only:** you MUST reach this step even if
  an advisory check failed — the PR is mandatory, the advisory is optional. It does NOT
  override the Step 0 / Step 3 hard-stops: if preflight aborted or the build could not be
  made to verify, you already halted and never reach this step. A build/verification abort
  is a DATA failure — never retry it into a PR. "Retry once WITHOUT `--advisory`" applies
  ONLY to a non-data `work-log-weekly.mjs` failure (e.g. a transient `gh`/network error).
  Retry exactly ONCE, never more. If the retry fails, persist `PR_OPEN_FAILED` before
  stopping.

## 6. Report
Before reporting, persist the terminal result. For an opened PR:
`node "{{SKILL_HOME}}/run-state.cjs" success --outcome PR_OPENED --pr-url "<url>" --pr-number "<number>" --week-start "<YYYY-MM-DD>" --week-end "<YYYY-MM-DD>"`
If verified data produced no change, use outcome `NO_CHANGE`. Report: PR opened (URL) or why
not; the build verification result; how many sessions were curated (public vs
private-redacted); which advisory checks ran vs degraded. Do not deploy. Do not merge.
Remove the clean dedicated WORKTREE after recording the result; if cleanup fails, report it
without changing the successful run status. Leave the user's BASE checkout untouched. The
PR is the human approval gate.
