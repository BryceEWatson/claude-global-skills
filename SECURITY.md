# Security Policy

This repository is a collection of Claude Code skills. Some of them read your
private local Claude chat history, and skills run as executable code inside your
Claude Code environment. That makes both **data handling** and **what a skill is
allowed to do** security-relevant. Please read this file before contributing or
installing.

## Reporting a vulnerability

If you find a security or privacy issue, report it **privately**:

- **Preferred:** GitHub's private vulnerability reporting (Security Advisories) —
  on the repo, go to the **Security** tab → **Report a vulnerability**. This
  keeps the report and discussion private until a fix ships.
- **Fallback:** email **bryceewatson@gmail.com**.

Please do **not**:

- Open a **public issue or pull request** for a security or privacy report. A
  public issue discloses the problem before there is a fix.
- Paste **raw chat transcripts**, mined `.local-state/` output, or **API keys /
  tokens** into a report. Describe the issue and include the minimum redacted
  snippet needed to reproduce it. If a real secret has been exposed, rotate it
  immediately and say so in the report — do not include the live value.

A useful report names the skill, the file/line, and what an attacker (or an
honest accident) could cause. Proof-of-concept steps are welcome as long as they
contain no real private data.

## Data handling

Several skills (`chat-history-search`, `transcript-analysis`,
`pattern-retrospective`, `global-review-loop`) **mine your private local Claude
history** — your past prompts and conversations. How that data is contained:

- **Mined data stays under each skill's `.local-state/`**, which is **git-ignored**
  (`.gitignore` covers `**/.local-state/`). It is local scratch, never tracked
  content, and is never copied between the repo and the live tree —
  `scripts/sync.py` excludes `.local-state/` in both `--capture` and `--deploy`.
- **Writes are gated by a fail-closed guard.** `global-review-loop/lib/_guards.py`
  exposes `assert_safe_out()`, which validates every output destination before a
  write. It **refuses** to write:
  - **anywhere inside the `~/.claude` config tree** (your `CLAUDE.md`,
    `settings.json`, `skills/`, memories) — the **only** sanctioned exception is
    this skill's own `~/.claude/skills/global-review-loop/.local-state/` scratch;
  - **anywhere inside a git working tree** (it walks up looking for a `.git`
    entry), so mined private data cannot be committed by accident.
  - The one exempt path — the skill's own `.local-state/` — still **warns** if an
    ancestor is a git working tree, so the exemption is never a silent leak path.
  - On **any error** while evaluating safety, it **refuses** rather than
    proceeding. A boundary check that cannot prove safety must not assume it.
- **Net rule:** mined data must never reach a **tracked git tree** or the
  **`~/.claude` config**. If you write a new skill that touches private history,
  route every write through `assert_safe_out()` and keep the output under that
  skill's `.local-state/`.

If you find any path by which mined history escapes `.local-state/` — into a
tracked file, into `~/.claude` config, or into terminal/log output that gets
captured — treat it as a vulnerability and report it privately.

## Secret scanning

`scripts/sync.py --capture` runs a **warn-only** secret pre-scan over new and
changed files before you commit them. It flags common token families —
`sk-ant-`, `ghp_`, `github_pat_`, `glpat-`, `AKIA…`, `AIza…`,
`-----BEGIN … PRIVATE KEY-----`, and `xox[baprs]-` Slack tokens — plus
project-coupled absolute paths and obvious scratch/seed cruft.

This scan is **best-effort, not a gate**:

- It only **prints warnings**; it does **not** block the capture, and `sync.py`
  does not touch git — staging, commit, and PR stay operator-driven.
- It matches only the listed patterns. A novel token format, a base64-wrapped
  secret, or a credential split across lines will pass through silently.

**Contributors must still eyeball every diff before committing.** Do not rely on
the pre-scan to catch a leaked secret for you. Treat a `SECRET-LIKE:` warning as
a stop-and-verify trigger, never as noise to scroll past.

## Autonomous-execution threat model

Skills in this repo are **executable code that runs inside your Claude Code
environment**, not passive config:

- Some skills install **`PostToolUse` / `Stop` hooks** that fire automatically as
  you work.
- The **`review-loop`** machinery can **auto-apply fixes** to a working tree.

Together that means **a malicious or careless skill PR is a code-execution
vector** — merging one is equivalent to running its code on contributors' and
maintainers' machines, with the ability to read local files and mutate
`~/.claude` configuration.

**Mitigation — the mandatory review gate.** Every change is reviewed before it
lands: a PR is not considered done until a **`/review-loop` verdict** is recorded
on it (pinned to the reviewed commit). That human-plus-automated gate is the
primary defense against a hostile contribution. Do not merge skill code that has
not passed it.

**Advice for anyone installing these skills:** **read a skill's code before you
install it.** Deploying copies the skill into `~/.claude/skills/<name>/`, where
its hooks and scripts can run automatically. At minimum, inspect the skill's
`SKILL.md`, any `scripts/`, and any `install.cjs` / hook registration, and
confirm it only does what it claims. If you do not understand what a skill will
execute, do not deploy it.
