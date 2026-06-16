# SKILL-SPEC.md — the contract for adding a skill

This document defines what a skill in this repository **must** look like to be
accepted. It is the authoritative checklist for contributors and reviewers. A
skill is a single top-level directory containing a `SKILL.md` plus optional
supporting code. The deploy engine (`scripts/sync.py`) copies each skill
directory to `~/.claude/skills/<name>/`.

If you are an outside contributor, you have no live `~/.claude/skills` tree to
capture from. Edit the repo copy directly, then test with:

```bash
export CLAUDE_GLOBAL_SKILLS_REPO=/path/to/claude-global-skills
python scripts/sync.py --deploy        # repo -> ~/.claude/skills
```

Do **not** run `sync.py --check` in CI — it requires a live `~/.claude` tree.

---

## 1. Required `SKILL.md` YAML frontmatter

Every skill begins with a YAML frontmatter block delimited by `---`. The body
below it is plain Markdown documentation.

### Required keys

| Key | Rule |
|---|---|
| `name` | **MUST equal the directory name exactly.** `sync.py` keys on `name` to map the directory to its deploy target; a mismatch breaks deploy and drift detection. Use the lowercase-hyphenated directory name verbatim. |
| `description` | The **trigger surface** — this is what Claude reads to decide when to invoke the skill, so it carries real weight. Write it as: *what it does* + *when to use it* + a few *example trigger phrases* (verbatim phrasings a user might type). Be specific; vague descriptions cause mis-triggering. |

### Optional keys

| Key | Use it for |
|---|---|
| `metadata.type` | A free-form type tag (e.g. `reference`). See `gemini-image/SKILL.md`, which sets `type: reference`. |
| `allowed-tools` | Restrict the skill to a named subset of tools. Omit to allow all. |
| `disable-model-invocation` | Set `true` for a directory that exists only to host machinery (a hook script, install/uninstall code) and must **never** be auto-invoked by the model. Such a skill is reachable by its support code, not by trigger phrases. |
| `argument-hint` | A short hint string shown for skills invoked with arguments (e.g. `/signal-scan <repoId>`). |

### Minimal annotated example

```yaml
---
name: my-skill                      # MUST match the directory name (my-skill/)
description: >-                      # the trigger surface — what + when + examples
  One sentence on what this does. Use when <the concrete situation that should
  fire it>. Triggers: "do the thing", "run my-skill on X", "<verbatim phrase>".
metadata:
  type: reference                    # optional free-form type tag
allowed-tools: Bash, Read, Write     # optional — omit to allow all tools
disable-model-invocation: false      # optional — true to host-only (never auto-fires)
argument-hint: "<target>"            # optional — only for argument-taking skills
---

# my-skill

Markdown documentation of the skill goes here.
```

Use the YAML `>-` block scalar for multi-line `description` values, as the
exemplar skills do.

---

## 2. Directory layout conventions

A skill directory follows these conventions (all subdirectories are optional —
include only what the skill needs):

```
my-skill/
├── SKILL.md            # required — frontmatter + docs (see §1)
├── SPEC.md             # optional — a longer canonical contract (e.g. gemini-image/SPEC.md)
├── scripts/            # executables the skill runs (CLIs, entry points)
├── lib/                # importable helpers (not run directly)
├── tests/              # python tests (discovered by the CI matrix, see §5)
└── .local-state/       # runtime scratch — git-ignored, never committed
```

- **`scripts/`** — runnable entry points. `gemini-image/scripts/gemini_image.py`
  is the canonical example: a single zero-dependency CLI.
- **`lib/`** — shared, importable code that is *not* invoked directly (e.g.
  `global-review-loop/lib/_guards.py`).
- **`tests/`** — python unit tests, named `test_*.py` (see §5).
- **`.local-state/`** — per-skill runtime output. It is git-ignored
  (`**/.local-state/` in `.gitignore`) and is the **only** sanctioned write
  area for skills that produce local data.

### Skills that mutate `~/.claude/settings.json`

A skill that registers a hook (or otherwise edits `~/.claude/settings.json`)
**must** ship install/uninstall machinery plus matching tests:

```
my-hook-skill/
├── SKILL.md
├── install.cjs         # idempotent registration into ~/.claude/settings.json
├── uninstall.cjs       # clean removal — must fully reverse install.cjs
├── install.test.cjs    # node test for install behavior
├── uninstall.test.cjs  # node test for uninstall behavior
└── <hook>.cjs          # + <hook>.test.cjs for the hook itself
```

`review-loop/` is the reference implementation (`install.cjs`,
`uninstall.cjs`, `stop-hook.cjs`, each with a `*.test.cjs`). `install.cjs` must
be idempotent, and `uninstall.cjs` must fully reverse it. The `*.test.cjs`
files run install-free under `node --test` (see §5).

---

## 3. Dependency rule: stdlib-first, lazy third-party imports

The repository is intentionally near-zero-dependency so a skill runs on a bare
machine with only `node` and `python` present.

- **Node skills MUST be zero-dependency** — Node skills use only built-in
  modules. No `node_modules`, no `package.json` install step.
- **Python skills MUST be stdlib-only by default.** Any third-party dependency
  is allowed only as a **lazy conditional import** — imported inside the one
  function/path that needs it, not at module top level — and on `ImportError`
  it must emit a clear `pip install --user <pkg>` fallback message rather than
  crashing the whole skill.

The only two third-party Python deps currently in the repo are precedents for
this pattern:

| Dependency | Where | Why it is allowed |
|---|---|---|
| `filelock` | `global-review-loop/lib/ledger_store.py`, `register_finding.py` | Lazy-imported only on the file-locking path; falls back with a `pip install --user filelock` message. |
| `anthropic` | `global-review-loop` (`dual_llm_coder.py`) | Lazy-imported only on the inter-rater / LLM path; not needed for the common path. |

If you add a third dependency, it must follow this same lazy-import-with-
fallback shape, and you should expect to justify it in review.

---

## 4. Privacy rule for chat-mining skills

Several skills mine the user's **private local Claude chat history**
(`chat-history-search`, `transcript-analysis`, `pattern-retrospective`,
`global-review-loop`). Any skill that reads that corpus is held to a hard
privacy contract:

1. **Route every corpus/derived write through the fail-closed guard.** Use
   `global-review-loop/lib/_guards.py::assert_safe_out()` for any output path.
   It refuses to write into the `~/.claude` config tree (CLAUDE.md,
   `settings.json`, `skills/`, memories) **and** refuses any path inside a git
   working tree, and it **fails closed** — on any exception while evaluating
   safety, it refuses rather than guessing.

2. **Write only under the skill's own `.local-state/`.** That git-ignored
   directory is the single sanctioned destination for mined or derived data.
   Never write mined data into a tracked path, into the repo, or anywhere else
   under `~/.claude`.

3. **Never commit mined data.** `.gitignore` already covers `**/.local-state/`,
   `.env`, `**/.env`, `__pycache__/`, `*.pyc`, `.claude/`, and `.pytest_cache/`.
   Do not add corpus output to git, and do not weaken these ignore rules.

These skills also run autonomously via hooks and can have fixes auto-applied by
the review loop, so a careless write path is a real leak vector. Treat the
guard as mandatory, not advisory.

---

## 5. Testing expectation

Ship tests that the CI matrix can run **without any install step**. CI invokes
these commands directly:

```bash
# Node tests (built-in test runner, zero deps)
node --test review-loop/*.test.cjs chat-arch-thrash-detect/*.test.cjs

# Python tests (stdlib unittest)
python -m unittest discover -s gemini-image/tests -p 'test_*.py'
python pattern-retrospective/lib/krippendorff_alpha.py --test
```

Requirements:

- **Node:** name tests `*.test.cjs` and make them pass under `node --test` with
  no dependencies. A skill that touches `~/.claude/settings.json` must include
  the install/uninstall tests from §2.
- **Python:** put `test_*.py` under the skill's `tests/` so
  `unittest discover` finds them, or expose a `--test` self-check entry point
  (as `pattern-retrospective/lib/krippendorff_alpha.py` does). Tests must run
  on stdlib alone; do not require a third-party package to test.
- **Do not** make CI depend on `sync.py --check` or on a live `~/.claude` tree.
- New test files must be wired into the CI command set (extend the relevant
  glob) so they actually run.

A skill PR without runnable tests for the code it adds is incomplete.

---

## Reviewer checklist (quick reference)

- [ ] `name` in frontmatter equals the directory name exactly.
- [ ] `description` covers what + when + example trigger phrases.
- [ ] Optional keys (`metadata.type`, `allowed-tools`, `disable-model-invocation`, `argument-hint`) used correctly where present.
- [ ] No top-level third-party imports; any dep is a lazy import with a `pip install --user` fallback.
- [ ] Node code is zero-dependency.
- [ ] Chat-mining skills route writes through `_guards.assert_safe_out` and output only under `.local-state/`.
- [ ] Skills touching `~/.claude/settings.json` ship `install.cjs` + `uninstall.cjs` + matching `*.test.cjs`.
- [ ] Tests are runnable install-free by the CI matrix and wired into the command set.
- [ ] No mined data, secrets, or `.local-state/` contents committed.
