# Contributing

Thanks for helping improve these skills. This is a small, personal repo, so the
process is light — but two rules are firm: **respect the privacy surface** and
**don't merge without passing tests and a `/review-loop` verdict**.

A skill is a top-level directory containing a `SKILL.md`. Skills are deployed by
*copying* the directory to `~/.claude/skills/<name>/`; `scripts/sync.py` is the
copy engine. Read [`README.md`](README.md) for the full deploy model and
[`SKILL-SPEC.md`](SKILL-SPEC.md) for the `SKILL.md` contract before you start.

## Two ways to contribute

You don't need the maintainer's machine to contribute. Pick the path that fits.

### Outside contributor (no live `~/.claude` tree)

You edit the **repo copy directly** and test against your own `~/.claude`.

1. Fork/clone, branch from `main`.
2. Edit the skill's files in the repo (`<skill>/SKILL.md`, `scripts/`, `lib/`,
   `tests/`, …).
3. Point `sync.py` at your checkout and deploy it to your own live tree to test:

   ```bash
   export CLAUDE_GLOBAL_SKILLS_REPO=/path/to/your/claude-global-skills
   python scripts/sync.py --deploy   # copies repo -> your ~/.claude/skills/
   ```

   `--deploy` never clobbers any existing `.local-state/` in your live tree, so
   it's safe to re-run. Exercise the skill, iterate on the repo copy, redeploy.
4. Run the tests (below), then open a PR.

> Don't run `python scripts/sync.py --capture` — that's the maintainer's
> live→repo path and needs a populated live tree. As an outside contributor,
> the repo *is* your source of truth; `--deploy` is how you test it.

### Maintainer (live-authoritative)

The canonical copy lives at `~/.claude/skills/<name>/` and you edit it **there**,
then capture back into the repo:

1. Edit the live skill at `~/.claude/skills/<name>/`.
2. Capture into the repo working tree and run the cruft/secret pre-scan:

   ```bash
   python scripts/sync.py --capture   # copies live -> repo, prints what changed + warnings
   ```

   `--capture` deliberately does **not** touch git — staging stays in your hands
   so every change lands as a reviewed PR.
3. Review the diff. Address any pre-scan warnings (secret-like strings,
   project-coupled absolute paths, scratch/`seed_*` cruft).
4. Commit on a `sync/<topic>` branch and open a PR.

## Run the tests before opening a PR

Tests are install-free (no `pip install`, no `npm install`).

```bash
# Node (zero-dependency)
node --test review-loop/*.test.cjs chat-arch-thrash-detect/*.test.cjs

# Python (stdlib only)
python -m unittest discover -s gemini-image/tests -p 'test_*.py'
python pattern-retrospective/lib/krippendorff_alpha.py --test
```

Notes:

- Do **not** run `python scripts/sync.py --check` as a test — it compares against
  a live `~/.claude` tree, which CI and outside contributors don't have.
- Python is stdlib-only except two **lazy, conditional** imports — `filelock`
  (in `global-review-loop/.../register_finding.py` and `ledger_store.py`, with a
  pip-install fallback message) and `anthropic` (in `dual_llm_coder.py`,
  inter-rater path only). Neither is needed for the test commands above. Keep new
  code stdlib-only unless you have a strong reason, and gate any new dependency
  behind a lazy import with a clear fallback.

## Privacy rule (non-negotiable)

Several skills (`chat-history-search`, `transcript-analysis`,
`pattern-retrospective`, `global-review-loop`) mine the user's **private local
Claude chat history**. Mined output is written only under each skill's
git-ignored `.local-state/`, behind a fail-closed guard
(`global-review-loop/lib/_guards.py::assert_safe_out()`).

- **Never commit anything under any `.local-state/`.** It's already covered by
  `.gitignore` (`**/.local-state/`); don't `git add -f` around it.
- **Never paste verbatim mined chat into a PR, issue, or commit message.** If you
  must illustrate a problem with real data, **redact** it first (no prompts,
  paths, names, or secrets).
- If you change anything in the privacy guard or the `.local-state/` write paths,
  call it out explicitly in the PR — it's a security-sensitive surface.

## The `SKILL.md` contract

Every skill needs a valid `SKILL.md` with YAML frontmatter whose `name` **equals
the directory name** (`sync.py` keys on this). The full contract — frontmatter
fields, the `description` trigger surface, directory conventions, and the
install/uninstall requirement for skills that mutate `~/.claude/settings.json` —
is in [`SKILL-SPEC.md`](SKILL-SPEC.md). Read it before adding or renaming a skill.

## Merge gate

These skills run autonomously (via PostToolUse/Stop hooks, and `review-loop` can
auto-apply fixes), so a skill PR is a code-execution path into anyone who
installs it. Before a PR merges:

- ✅ The test commands above pass.
- ✅ The PR carries a **`/review-loop` verdict** — a commit-pinned review comment
  on the PR. This is the mandatory gate that mitigates the code-execution risk.

If you're an outside contributor and can't run `/review-loop` yourself, that's
fine — open the PR with passing tests and the maintainer will run the review pass
before merge.
