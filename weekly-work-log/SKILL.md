---
name: weekly-work-log
description: Build and refresh the brycewatson.com "Weekly Work Log" report page (/weekly-report, archived under /log) for the last completed week. Discovers the week's work from session-end handoffs + git, distils it into honest public-voice items, re-derives and verifies every number, and (unattended, every Sunday night via a Claude scheduled task) opens a judgment-assisted review PR. Never publishes unattended.
user-invocable: true
argument-hint: "[--weekly] [--days N] [--week YYYY-MM-DD]"
---

# Weekly Work Log

Builds and refreshes the **Weekly Work Log** at `brycewatson.com/weekly-report`: a per-project feed of the specific work Bryce did **last week**, each item with an honest status badge and an expandable evidence drawer, a git-derived commits/day chart for the week, and per-project archive pages. Each week's report is snapshotted; `brycewatson.com/log` is the index of all past weekly reports.

Routes: `/weekly-report` (live, current week) · `/weekly-report/<project>` (per-project commit-history archive) · `/log` (index of weekly reports) · `/log/<week>` (an archived week).

**Target project:** `C:\Users\Bryce\Projects\brycewatson.com`. The page + build scripts live there; this skill is the reusable orchestrator + the weekly scheduler.

## The window: the last completed week

The report always covers the most recently **completed Monday–Sunday week** (a week completes on Sunday). Run on a Sunday night, that is the week that just ended; run any other day, it is the previous full week. Discovery (handoffs + git), the chart, and the per-project metrics all use this same week, resolved by the single shared `resolveWeek()` in `scripts/lib/work-log-sessions.mjs`. It is anchored on the calendar week, never a rolling window walking back from today.

**Backfill (`--week YYYY-MM-DD`)** regenerates a PAST completed week's `/log` archive (snapshot + index + re-aggregated goals) and **leaves the live `work-log.json` untouched**. Any day inside the target week resolves to that whole Mon–Sun window; a future date or the current in-progress week aborts. Backfill is **opt-in / operator-confirmed** and regenerates **exactly one completed week per invocation** (blast radius bounded to one PR); it is **never** fired by the scheduled run (the unattended agent always targets the last completed week — one PR, not N).

## What runs where

Everything runs LOCALLY (it reads `~/.claude` handoffs + the sibling repos under `C:\Users\Bryce\Projects\`); GitHub Pages CI cannot. The committed JSON is what CI builds, exactly like `art.json`/`books.json`.

- `<repo>/src/data/work-log.source.json` — AUTHORED, approval-gated content (the human gate).
- `<repo>/src/data/work-log.json` — GENERATED + committed (every number re-derived + git-verified).
- `<repo>/src/data/reports/<weekStart>.json` + `reports/index.json` — committed per-week snapshots (the `/log` archive series); the generator writes these.
- `<repo>/src/data/goals.json` — GENERATED + committed (the `/goals` cross-week aggregation).
- `<repo>/src/components/ReportPanel.astro` — the report panel, shared by `/weekly-report` and `/log/<week>`.
- `<repo>/src/pages/weekly-report.astro` + `weekly-report/[project].astro` + `log.astro` + `log/[week].astro` + `goals.astro` — the pages.
- `<repo>/scripts/draft-work-log-from-handoffs.mjs` — discover (handoffs → redacted digest).
- `<repo>/scripts/draft-work-log-sessions.mjs` — mine the week's interactive sessions → redacted digest.
- `<repo>/scripts/draft-work-log-proposed.mjs` — ranked `.proposed` distil scaffold (interactive; the builder NEVER reads it).
- `<repo>/scripts/work-log-harvest-nouns.mjs` — propose redactor denylist additions → gitignored sidecar (count surfaced, never raw nouns).
- `<repo>/scripts/work-log-validate-source.mjs` — voice/leak gate on the DRAFTED source prose (fails loud on a dash / denylisted token / bad badge / display-role git-leak), run after distillation and before the build (also chained in `pnpm build`).
- `<repo>/scripts/build-work-log.mjs` — build (re-derive + verify every commit, redact).
- `<repo>/scripts/work-log-weekly.mjs` — open the review PR (`--advisory <path>` splices the fail-open advisory; never main, never deploy).
- `<repo>/scripts/lib/work-log-redact.mjs` — shared secret/leak scrubber (the ONE canonical redactor; extend its term lists, never duplicate).

## Modes

- **`/weekly-work-log`** (interactive, a human in the loop): the full build — discover → distil candidates → build → preview. New items get WRITTEN here (judgment). The optional `.proposed` scaffold (`draft-work-log-proposed.mjs`) gives you a ranked, mirror-shaped starting point to fill in the public voice; you then MOVE accepted items by hand into `source.json` (the gate).
- **Unattended (the Sunday-night Claude scheduled task)** — curates the whole week, then opens one PR:
  - **Curate every session (distillation):** distil EVERY interactive Claude Code session of the week into `source.json` items so the page shows the full week (the original "curate every session" directive, automated). Done in-context from the redacted digest (not subagents). Private/sensitive sessions are **summarized through the privacy filter, not dropped or stubbed** (display roles: Akaya/Personal/ShopForge — generalized to the kind of work, never git-read; "personal finance is fine, we just need iron clad rules"). The drafted prose is gated by `work-log-validate-source.mjs` (fails loud on a dash/leak/bad-badge) + a claim-falsification self-check BEFORE the build.
  - **Deterministic backstop:** `build-work-log.mjs` git-verifies every number (aborts on any unresolved/non-Bryce commit, so NO PR on bad data). Items the job drafts carry `"drafted": "auto-<date>"`.
  - **Fail-open advisory:** judgment that ASSISTS the reviewer (badge-vs-prose, coverage gaps, badge-vs-git, a downgrade-only privacy adjudicator, reversal-coverage) spliced into the PR body, plus the noun-harvester (count only). Any LLM/network/`gh` failure → a single "advisory unavailable" line; never blocks the PR. Advisory may only DOWNGRADE/FLAG.
  - **The PR is the gate:** auto-DRAFT, human-APPROVE. The run never pushes `main`, never deploys, opens exactly one PR.
- **`--week YYYY-MM-DD`**: backfill a past completed week's archive (see above). Manual only.
- **`--days N`**: override the window to a rolling N-day scan (manual broader discovery).

## The full flow (interactive)

1. **Discover** — `node scripts/draft-work-log-from-handoffs.mjs` (and/or `draft-work-log-sessions.mjs`). Scans the week's session-end handoffs / interactive sessions across the allowlisted repos, extracts tagged claims + reversals + cited commits, redacts, writes the gitignored `src/data/work-log.drafts.json` digest.
2. **Curate every session** — `node scripts/draft-work-log-sessions.mjs` for the redacted per-session digest, then distil EVERY session into `source.json` items in the plain public voice (private/sensitive sessions summarized through the privacy filter, never dropped — see the rules + the unattended prompt `scheduled-task-prompt.md`). `draft-work-log-proposed.mjs` can give a ranked scaffold to fill. Then `node scripts/work-log-validate-source.mjs` (fails loud on a dash/leak/bad-badge) before building. Obey every rule below; accuracy is the floor even for private.
3. **Build** — `node scripts/build-work-log.mjs`. Re-derives every date/number from git, rewrites commit snippets to the real subject, verifies all cited commits resolve (aborts if any do not), redacts. Writes `work-log.json` (+ snapshot + `goals.json`).
4. **Preview** — `pnpm dev`, open `http://localhost:4321/weekly-report` (archive index at `/log`, goals at `/goals`).
5. **Ship** — commit the data files. A merge to `main` deploys via CI. (Unattended: `node scripts/work-log-weekly.mjs --advisory <sidecar>` opens the PR instead of committing to main.)

## The rules (load-bearing — every run, especially unattended, must honor these)

- **Voice**: plain, concrete, no marketing flourishes. **Subject-led headlines** — lead with the work or the finding, NOT "I"/"My" (headlines must not all start the same way or read self-focused). First-person belongs in the body. **No em dashes.** **Never announce the page's own honesty** ("honest", "keeping myself honest", "proof I don't fake it") — show it through the badges + receipts.
- **Honest status badges**: `shipped` (built, merged, verified), `in progress`, `designed, not proven` (machinery exists, no real result yet). The most interesting work is often the least finished; let it wear the badge openly. Map a handoff's `[verified]` claim → shipped; `[assumed]`/`[unverified]`/`[handoff-claimed]` → designed-not-proven.
- **Every number git-verified**: re-derived at build, never hardcoded. A cited commit that does not resolve (or is not Bryce's: `bryceewatson@gmail.com` / `bryceewatson@users.noreply.github.com`) aborts the build. Item dates come from the commit, not the prose.
- **Privacy** (default-deny): only Bryce's OWN repos are read (Command, DemandForge, claude-global-skills, brycewatson.com). Client / no-remote repos (dropKnowledge, etc.) are never read or named. The shared redactor scrubs codenames (plumagedispatch), client names (dropKnowledge / castgryff), vendor + niche names, session UUIDs, home paths, and secrets. **Draft-and-distil, never lift**: handoff prose is operational and leaky; distil it into plain public-voice items, never paste it verbatim. The `.proposed` scaffold and the harvester sidecar are gitignored; never commit them, and never route raw harvested nouns into the PR body (count only).
- **Structure**: the feed is grouped by project into containers; each project is a card with git-derived metrics (commits this week, active days, entries) + an `archive →` link to `/weekly-report/<project>` (6-month commit history). Dark console aesthetic; the shared site header uses `headerVariant="dark-canvas"`.
- **The approval gate is the PR**: the unattended run never pushes to `main` and never deploys. It only opens a PR. Bryce reviews + merges; CI deploys the merge. Advisory judgment never gates — it only assists.

## Scheduling (every Sunday night) — Claude scheduled task

The unattended weekly run is a **Claude scheduled task** (`mcp__scheduled-tasks`), NOT a Windows Task Scheduler job. It runs as a local Claude agent with full filesystem access and **catches up on next app launch** if the app/machine was closed at the scheduled time — fixing the old `schtasks` job's silent-drop failure (it had `StartWhenAvailable=False` / `WakeToRun=False` / `DisallowStartIfOnBatteries=True` and no catch-up).

- **The task**: `taskId: weekly-work-log`, `cronExpression: "0 22 * * 0"` (Sunday 22:00 local). Its prompt is authored in `scheduled-task-prompt.md` (this skill dir) — keep that file in sync with the live task.
- **Create / update it** (the runtime swap; done by an operator session after the repo seams are on `main`):
  ```
  mcp__scheduled-tasks__create_scheduled_task({
    taskId: "weekly-work-log",
    cronExpression: "0 22 * * 0",
    description: "Sunday-night Weekly Work Log: deterministic build + verify, fail-open advisory, opens one review PR (never deploys)",
    prompt: <contents of scheduled-task-prompt.md>
  })
  ```
  Confirm with `mcp__scheduled-tasks__list_scheduled_tasks`.
- **Retire the legacy Windows task** at the same swap: `node uninstall.cjs` (or `schtasks /Delete /TN ClaudeWeeklyWorkLog /F`). `install.cjs` / `weekly-run.cmd` are the **legacy** schtasks launchers, retired at the swap; do not run `install.cjs` going forward.
- **Verification debt**: the run-on-next-launch catch-up is documented, not yet behaviorally proven here. Worth a one-time controlled catch-up test (close the app over a due time, relaunch, confirm fire) before fully trusting it — the `demandforge-verify-cron-first-firing` task is precedent.
- It **activates once the page is shipped** (the data files are committed). Requires: `gh` authenticated; git push credentials; the repo on a clean `main`.

## Safety invariants

- Never push to `main`; never deploy; only ever open a PR. The unattended run opens **exactly one** PR.
- Abort (no PR) if the generator fails verification — never open a PR with unverified data.
- **Advisory is fail-open**: any LLM/network/`gh` failure → a single "advisory unavailable" line; the PR still opens with deterministic data + the human checklist intact. Advisory may only DOWNGRADE/FLAG — never clear-for-publish, never suppress the human leak/voice checklist.
- The cron NEVER fires `--week` backfill; the harvester's raw nouns NEVER enter the PR body (count only).
- Reuse the review-loop `--mode claim` engine/rubric verbatim for the badge-vs-prose checks — read its `agents/claim-*.md` lenses at runtime; do not fork them.
- **Curate every session, redacted — not dropped, not stubbed.** Private/sensitive sessions are summarized through the privacy filter (display roles never git-read; client→"Akaya", finances→"Personal"; generalized to the kind of work; accuracy is the hard floor even for private). A session you cannot distil confidently gets the most generic HONEST entry, never a fabricated one.
- **The drafted prose is gated before it builds:** `work-log-validate-source.mjs` fails loud on a dash/leak/bad-badge/git-leak; the iron-clad redactor + the dist-PII scan are the deterministic backstop; the PR is the human gate. Auto-DRAFT, human-APPROVE — never auto-publish in Bryce's voice.
- The unattended run writes ONLY `work-log.source.*` (its drafted items) + gitignored sidecars; it NEVER hand-writes `work-log.json` / `reports/*` / `goals.json` (the build does, deterministically), never `main`, never deploy.
- The drafts digest and harvester sidecar are gitignored and redacted; never commit them.
