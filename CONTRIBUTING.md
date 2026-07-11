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

### Maintainer (repo-authoritative)

The repo is canonical. Author the skill **here** and deploy it to the live tree(s):

1. Edit the skill in the repo (`<skill>/SKILL.md`, `scripts/`, `overlays/<target>/`, …).
2. Deploy and verify:

   ```bash
   python scripts/sync.py --deploy   # repo -> live, for each skill's declared targets
   python scripts/sync.py --check    # confirm live matches the repo-materialized output
   ```

3. Exercise the skill, iterate on the repo copy, redeploy.
4. Commit on a `sync/<topic>` branch and open a PR.

If you edited a *live* copy directly (e.g. while debugging in a session), pull it
back with the target-explicit capture and run the cruft/secret pre-scan:

```bash
python scripts/sync.py --capture --target claude   # or --target codex
```

`--capture` requires an explicit `--target`, never touches git (staging stays in
your hands so every change lands as a reviewed PR), and **refuses to silently
resolve a divergence**: if two live copies of a dual-target skill have conflicting
edits, it prints the conflicting files and writes nothing. Reconcile in the repo,
redeploy, then re-capture if needed. Address any pre-scan warnings (secret-like
strings, project-coupled absolute paths, scratch/`seed_*` cruft).

### Adding a portable (dual-target) skill

1. Set `targets: [claude, codex]` in the `SKILL.md` frontmatter (omit the key for
   Claude-only; `[codex]` for Codex-only).
2. Keep everything **shared** by default. Put files that belong to only one product
   under `<skill>/overlays/<target>/` (e.g. `overlays/codex/agents/openai.yaml`).
   Overlays are **additive-only** — an overlay path must not shadow a shared file.
3. For a path that differs per product, write the `{{SKILL_HOME}}` token in the
   shared file; it expands to that target's install dir at deploy time. Do **not**
   write a literal `$HOME/.<product>/skills/<name>` in a shared file.
4. `python scripts/sync.py --check --target both` (or `--deploy --target both`) to
   materialize and verify each install contains only its intended files.

## Run the tests before opening a PR

Tests are install-free (no `pip install`, no `npm install`).

```bash
# Node (zero-dependency)
node --test review-loop/*.test.cjs chat-arch-thrash-detect/*.test.cjs

# Python (stdlib only)
python -m unittest discover -s gemini-image/tests -p 'test_*.py'
python -m unittest discover -s scripts/tests -p 'test_*.py'                 # sync engine
python -m unittest discover -s monitor-agent-thread/tests -p 'test_*.py'    # monitor + privacy
python -m unittest discover -s hooks/tests -p 'test_*.py'                   # drift hook
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

## Migration

The engine moved from **live-authoritative** (edit the live copy, `--capture` it
back) to **repo-authoritative** (author here, `--deploy` out). Two things to know:

- **Nothing breaks for existing skills.** With no `targets:` key a skill defaults to
  `[claude]`, so `--check`/`--deploy` behave exactly as before and never touch
  `~/.codex`.
- **`--capture` now takes a target.** Replace bare `python scripts/sync.py --capture`
  with `python scripts/sync.py --capture --target claude`. The drift hook's nudge
  already emits the correctly-targeted command. Prefer editing the repo and
  redeploying over capturing; for a portable skill, capturing after editing two live
  copies can hit the divergence guard by design.

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
