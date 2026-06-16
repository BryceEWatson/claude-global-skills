---
name: weekly-work-log
description: Build and refresh the brycewatson.com "Weekly Work Log" report page (/report, archived under /log) for the last completed week. Discovers the week's work from session-end handoffs + git, distils it into honest public-voice items, re-derives and verifies every number, and (unattended, every Sunday night) opens a review PR. Never publishes unattended.
user-invocable: true
argument-hint: "[--weekly] [--days N]"
---

# Weekly Work Log

Builds and refreshes the **Weekly Work Log** at `brycewatson.com/report`: a per-project feed of the specific work Bryce did **last week**, each item with an honest status badge and an expandable evidence drawer, a git-derived commits/day chart for the week, and per-project archive pages. Each week's report is snapshotted; `brycewatson.com/log` is the index of all past weekly reports.

Routes: `/report` (live, current week) · `/report/<project>` (per-project commit-history archive) · `/log` (index of weekly reports) · `/log/<week>` (an archived week).

**Target project:** `%USERPROFILE%\Projects\brycewatson.com`. The page + build scripts live there; this skill is the reusable orchestrator + the weekly scheduler.

## The window: the last completed week

The report always covers the most recently **completed Monday–Sunday week** (a week completes on Sunday). Run on a Sunday night, that is the week that just ended; run any other day, it is the previous full week. Discovery (handoffs + git), the chart, and the per-project metrics all use this same week. It is anchored on the calendar week, never a rolling window walking back from today.

## What runs where

Everything runs LOCALLY (it reads `~/.claude` handoffs + the sibling repos under `%USERPROFILE%\Projects\`); GitHub Pages CI cannot. The committed JSON is what CI builds, exactly like `art.json`/`books.json`.

- `<repo>/src/data/work-log.source.json` — AUTHORED, approval-gated content (the human gate).
- `<repo>/src/data/work-log.json` — GENERATED + committed (every number re-derived + git-verified).
- `<repo>/src/data/reports/<weekStart>.json` + `reports/index.json` — committed per-week snapshots (the `/log` archive series); the generator writes these.
- `<repo>/src/components/ReportPanel.astro` — the report panel, shared by `/report` and `/log/<week>`.
- `<repo>/src/pages/report.astro` + `report/[project].astro` + `log.astro` + `log/[week].astro` — the pages.
- `<repo>/scripts/draft-work-log-from-handoffs.mjs` — discover (handoffs → redacted digest).
- `<repo>/scripts/build-work-log.mjs` — build (re-derive + verify every commit, redact).
- `<repo>/scripts/work-log-weekly.mjs` — open the review PR (never main, never deploy).
- `<repo>/scripts/lib/work-log-redact.mjs` — shared secret/leak scrubber.

## Modes

- **`/weekly-work-log`** (interactive, a human in the loop): the full build — discover → distil new items into `source.json` → build → preview. This is where new items get WRITTEN (judgment).
- **Unattended (the Sunday-night scheduled run)**: DETERMINISTIC — refresh the data + discover the week's candidates + open a PR. It does NOT write new item prose unattended; that is the gated judgment step. The PR carries the refreshed, re-verified data and the week's candidate digest in its body for Bryce to distil on review.
- **`--days N`**: override the window to a rolling N-day scan (manual broader discovery).

## The full flow (interactive)

1. **Discover** — `node scripts/draft-work-log-from-handoffs.mjs` (from `<repo>`). Scans the week's session-end handoffs across the allowlisted repos, extracts tagged claims + reversals + cited commits, redacts, writes the gitignored `src/data/work-log.drafts.json` digest.
2. **Distil** — read the digest + current `source.json`. For each strong, git-verifiable, leak-clean candidate not already present, write a distilled item into `source.json`. Conservative: when unsure, leave it out. Obey every rule below.
3. **Build** — `node scripts/build-work-log.mjs`. Re-derives every date/number from git, rewrites commit snippets to the real subject, verifies all cited commits resolve (aborts if any do not), redacts. Writes `work-log.json`.
4. **Preview** — `pnpm dev`, open `http://localhost:4321/report` (archive index at `/log`).
5. **Ship** — commit `work-log.json` (+ `source.json` if edited). A merge to `main` deploys via CI. (Unattended: `node scripts/work-log-weekly.mjs` opens the PR instead of committing to main.)

## The rules (load-bearing — every run, especially unattended, must honor these)

- **Voice**: plain, concrete, no marketing flourishes. **Subject-led headlines** — lead with the work or the finding, NOT "I"/"My" (headlines must not all start the same way or read self-focused). First-person belongs in the body. **No em dashes.** **Never announce the page's own honesty** ("honest", "keeping myself honest", "proof I don't fake it") — show it through the badges + receipts.
- **Honest status badges**: `shipped` (built, merged, verified), `in progress`, `designed, not proven` (machinery exists, no real result yet). The most interesting work is often the least finished; let it wear the badge openly. Map a handoff's `[verified]` claim → shipped; `[assumed]`/`[unverified]`/`[handoff-claimed]` → designed-not-proven.
- **Every number git-verified**: re-derived at build, never hardcoded. A cited commit that does not resolve (or is not Bryce's: `bryceewatson@gmail.com` / `bryceewatson@users.noreply.github.com`) aborts the build. Item dates come from the commit, not the prose.
- **Privacy** (default-deny): only your OWN repos are read. Client / no-remote repos are never read or named. The shared redactor scrubs codenames, client names, vendor + niche names, session UUIDs, home paths, and secrets. **Draft-and-distil, never lift**: handoff prose is operational and leaky; distil it into plain public-voice items, never paste it verbatim.
- **Structure**: the feed is grouped by project into containers; each project is a card with git-derived metrics (commits this week, active days, entries) + an `archive →` link to `/report/<project>` (6-month commit history). Dark console aesthetic; the shared site header uses `headerVariant="dark-canvas"`. No "projects these came from" strip (its content lives in the project cards).
- **The approval gate is the PR**: the unattended run never pushes to `main` and never deploys. It only opens a PR. Bryce reviews + merges; CI deploys the merge.

## Scheduling (every Sunday night)

A local Windows scheduled task runs the deterministic weekly flow.

- **Install**: `node ~/.claude/skills/weekly-work-log/install.cjs` — registers a `schtasks` job `ClaudeWeeklyWorkLog` for Sundays at 22:00 that runs `weekly-run.cmd` (cd to the repo → `draft-work-log-from-handoffs.mjs` → `work-log-weekly.mjs` → PR), logging to `.local-state/weekly.log`.
- **Test now**: `cmd /c "%USERPROFILE%\.claude\skills\weekly-work-log\weekly-run.cmd"` — runs the flow once; opens a PR only if the committed data changed.
- **Disable**: `node ~/.claude/skills/weekly-work-log/uninstall.cjs` (or `schtasks /Delete /TN ClaudeWeeklyWorkLog /F`).
- It **activates once the page is shipped** (the data files are committed). Until then it harmlessly no-ops, because untracked data files show no diff against `HEAD`.
- Requires: the machine on Sunday night; `gh` authenticated; git push credentials for the user; the repo on a clean `main`.

## Safety invariants

- Never push to `main`; never deploy; only ever open a PR.
- Abort (no PR) if the generator fails verification — never open a PR with unverified data.
- Conservative distillation: skip any candidate you cannot stand behind; note the skips in the PR body.
- The drafts digest is gitignored and redacted; never commit it.
