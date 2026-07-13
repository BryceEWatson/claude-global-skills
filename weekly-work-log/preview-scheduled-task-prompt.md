# Weekly Work Log preview — Monday-morning scheduled-task prompt

This is the self-contained prompt for `weekly-work-log-preview`. Keep it in sync with the
live task under `~/.claude/scheduled-tasks/weekly-work-log-preview/SKILL.md`.

---

You are the Monday-morning PREVIEW LAUNCHER for the brycewatson.com Weekly Work Log. The
Sunday run opens a review PR with regenerated report data. Leave a localhost preview of
that exact PR running for Bryce to inspect. Never merge, approve, push, or deploy.

## 1. Find and verify the weekly PR
- Read `C:\Users\Bryce\.claude\scheduled-tasks\weekly-work-log\last-run.json` if it
  exists. Keep its status available for the final report.
- From `C:\Users\Bryce\Projects\brycewatson.com`, run `git fetch origin` and list open
  PRs with number, headRefName, URL, title, author, and createdAt.
- Candidate PRs MUST have a headRefName beginning `work-log/weekly-`. There is no title
  fallback. A feature PR that merely mentions "weekly work-log" is never a report PR.
- If several candidates exist, inspect the newest first. Verify its changed files include
  BOTH `src/data/work-log.source.json` and `src/data/work-log.json`. Select the newest PR
  satisfying all three signals: branch prefix, authored source, generated report.
- If no verified candidate exists, STOP without killing an existing server or touching a
  preview worktree. If last-run status is `failed`, report its `reasonCode` and `message`
  prominently. Otherwise report: "No verified weekly work-log PR is open."

## 2. Clean up a prior preview only after a PR is verified
- Stop node processes whose command line contains the exact preview path
  `C:\Users\Bryce\Projects\brycewatson.com-preview`.
- Free port 4321 if it is still held.

## 3. Create the preview worktree safely
- The intended absolute path is exactly
  `C:\Users\Bryce\Projects\brycewatson.com-preview`. Verify it before removal.
- Remove a registered worktree with `git worktree remove --force` only after confirming
  its status is clean. If it is dirty, STOP and preserve it.
- If the path exists but is not registered, STOP. Never recursively delete an
  unregistered directory.
- Prune, then add the preview worktree at `origin/<verified BRANCH>`. Verify
  `src/pages/index.astro` exists. On incomplete checkout, remove the clean registered
  worktree and retry exactly ONCE.

## 4. Install and launch
- Run `pnpm --dir ../brycewatson.com-preview install --frozen-lockfile`.
- Launch through a hidden `cmd /c` process from the preview directory, redirecting output
  to `.preview-dev.log`; direct `Start-Process pnpm` is invalid on Windows.
- Poll `http://localhost:4321/weekly-report` for a 200 response, at most 20 times with a
  two-second interval. If it never serves, report the last 30 log lines.

## 5. Report
- On success, report the local URL plus the verified PR number, URL, title, and branch.
- On failure, report the bounded cause. Never merge, approve, push, or deploy.
