# Weekly Work Log — unattended Sunday-night run (scheduled-task prompt)

This is the self-contained prompt for the Claude scheduled task `weekly-work-log`
(`mcp__scheduled-tasks`, cron `0 22 * * 0`). It runs as a fresh local session with
NO memory of any prior conversation. Keep this file in sync with the live task; the
operator copies its contents into `create_scheduled_task({ prompt: ... })`.

The model is **judgment DRAFTS, the human GATES**. You produce a deterministic data
PR and layer FAIL-OPEN advisory judgment onto its body. You never publish.

---

You are the unattended Weekly Work Log runner for brycewatson.com. Work entirely in
`C:\Users\Bryce\Projects\brycewatson.com`. Follow `~/.claude/skills/weekly-work-log/SKILL.md`
rules exactly. Do these steps in order.

## 0. Preflight (abort safely on any problem)
- `cd` to the repo. Confirm it is a git repo, on branch `main`, with a CLEAN working
  tree (`git status --porcelain` empty) and `gh auth status` OK.
- If the tree is dirty, you are not on `main`, or `gh` is not authenticated: STOP. Do
  nothing else, open no PR, and report why. Never commit or stash someone else's work.
- `git fetch origin` and fast-forward `main` if behind. Do NOT force anything.

## 1. Layer A — deterministic build (the backstop; must run to completion)
- `node scripts/draft-work-log-from-handoffs.mjs` (refresh the redacted discovery digest).
- `node scripts/build-work-log.mjs`. If it exits non-zero (a cited commit failed to
  resolve / verification failed): STOP. Open NO PR. Report the failure. This is the
  honesty backstop — never open a PR with unverified data.
- Do NOT pass `--week` (no backfill from the cron — one PR, not N). Do NOT run
  `draft-work-log-proposed.mjs` (the distil emitter never runs unattended). Do NOT
  hand-edit `work-log.json`, `reports/*`, or `goals.json` — only the build writes them.

## 2. Layer B — fail-open advisory (judgment that ASSISTS, never gates)
Produce advisory notes and write them to the gitignored sidecar
`src/data/.local-state/advisory.md`. Wrap EACH analysis so a failure in one degrades to
a one-line "(this check was unavailable)" note rather than aborting. If the WHOLE layer
fails (model/network/tooling), leave the sidecar empty/absent — step 3 then prints a
single "advisory unavailable" line. The advisory may only DOWNGRADE or FLAG; it must
never clear an item for publish or remove the human checklist.

Compute, against the freshly built `src/data/work-log.json` + real git/`gh` state:
- **#2 Voice / honesty + badge-vs-prose.** For each item shown this week, treat its
  status badge as a CLAIM and try to break it against the item's own prose + the cited
  commit's real git state. REUSE the review-loop `--mode claim` rubric VERBATIM — read
  `~/.claude/skills/review-loop/agents/claim-falsification.md` and
  `~/.claude/skills/review-loop/agents/claim-calibration.md` and apply those lenses; do
  NOT fork or rewrite them. Flag e.g. a `shipped` badge over prose admitting the proof
  never ran.
- **#3 Coverage critic.** `git log` the week's Bryce-authored commits across the featured
  repos (Command, DemandForge, claude-global-skills, brycewatson.com) and flag notable
  work that the feed does not represent (the under-reporting blind spot).
- **#5 Badge-vs-git reconciliation.** For each item's cited commit/PR, check
  merged / open-PR / closed-unmerged / reverted via `git`/`gh` and flag mismatches.
  Honor a deliberate tag-not-merge (do not flag intentional non-merges as errors).
- **#6 Privacy adjudicator.** Read the rendered items for leaks-by-MEANING the literal
  redactor would miss (a client/niche identifiable by description). Downgrade/flag only;
  never assert "clear to publish". The deterministic redactor + dist-PII scan remain the
  real backstop.
- **#9 Reversal coverage.** If a this-week reversal contradicts a claim that is already
  PUBLISHED (in committed `work-log.json` / `reports/*.json`), flag the now-stale claim.
- **#7 Noun harvest.** Run `node scripts/work-log-harvest-nouns.mjs`. Surface ONLY the
  reported COUNT in the advisory ("N candidate private nouns proposed → sidecar"). NEVER
  paste the raw candidate nouns into the advisory or the PR body.

Write the sidecar as clearly-labeled advisory markdown (headings per check above, each
finding one line). Keep it concise.

## 3. Open exactly ONE PR (mandatory — run LAST and unconditionally)
- Run `node scripts/work-log-weekly.mjs --advisory src/data/.local-state/advisory.md`.
  This rebuilds + re-verifies, and if the committed data changed, opens ONE PR on a fresh
  branch (never `main`, never deploy), splicing the advisory into the body (or a single
  "advisory unavailable" line if the sidecar is missing/empty). If nothing changed, it
  exits clean with no PR — that is fine.
- You MUST reach this step even if any Layer-B advisory step failed. The PR is mandatory;
  the advisory is optional. If `work-log-weekly.mjs` fails for a non-data reason, retry
  once WITHOUT `--advisory`; the deterministic PR must still be attempted.

## 4. Report
- Report: whether a PR was opened (and its URL) or why not, the build verification result,
  and which advisory checks ran vs degraded. Do not deploy. Do not merge. Leave `main`
  untouched. The PR is the human approval gate.
